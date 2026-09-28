"""
F1赛程提醒机器人 - 比赛结果统计预测模型（v2 名次制）

架构原则（用户定）：预测由统计模型计算，AI 只负责解读呈现，禁止改动模型数字。

v1 教训（2026-09-07 回测失败实证）：pace_index 目标值域过窄（约96-103），
拟合 R²≈0.09，领奖台命中 0.64/场（接近随机基线 0.45）。改为**名次制特征**：
积分排名/近期完赛名次等"名次空间"特征对名次的预测力远强于圈速空间。

特征（车手级 + 车队修正，均为名次尺度，越小越好）：
- stand_pos:        本场前车手积分榜排名（按已完赛场次积分累计）
- form_finish:      近4场平均完赛名次（当前状态）
- circuit_hist:     同赛道近2年平均完赛名次（赛道适配；缺则排位名次，再缺车队均值）
- type_affinity:    车队动力敏感度斜率 × (本站速度指标 - 均值)（动力/高下压力赛道适配，数据自动得出）
- upgrade_boost:    本站 FIA 升级申报件数归一化（负向加成=名次前移）
- grid:             发车位置（仅正赛模型）
- penalty/standin:  预测时前提输入（罚退位移/代打降级）

模型：
- 排位: E[quali_pos] = w·[1, stand_pos, form_quali, circuit_hist_q, affinity, upgrade]
- 正赛: E[finish]    = a·[1, stand_pos, form_finish, circuit_hist, grid, affinity, upgrade]
- 2024 赛季最小二乘拟合，2025 留一法回测；蒙特卡洛残差采样出名次概率
- 落盘 data/prediction_model.json
"""

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
MODEL_FILE = os.path.join(DATA_DIR, "prediction_model.json")

FIT_YEAR = 2024
BACKTEST_YEAR = 2025
HISTORY_YEARS = 2

# 2026 积分规则：正赛前10 / 冲刺赛前8（2025 起取消最快圈积分）
RACE_POINTS = [25, 18, 15, 12, 10, 8, 6, 4, 2, 1]
SPRINT_POINTS = [8, 7, 6, 5, 4, 3, 2, 1]

# 预测结果级缓存 TTL（秒）：同一分站+前提组合的预测 30 分钟内复用。
# 2026-09-15 事故：同一站在 L1 取数层与 L2 失败后的兜底路径被重复计算 3 次，
# 每次 ~22s（FP 环节网络请求主导）；别名多样（阿塞拜疆/Baku/Azerbaijan）故键用解析后的 round
PREDICT_CACHE_TTL = 1800
FORM_WINDOW = 4
MC_SAMPLES = 1000
MODEL_VERSION = 7  # v7 race_pace 改为相似特性赛道优先（similar_race_pace_idx，同下压力档位，如巴库低阻→蒙扎/拉斯维加斯，无相似回退通用；用户要求正赛推测看相似赛道race pace）；v6 正赛向量加长距离配速+轮胎衰减；v5 赛道历史改中位数
# 正赛最终得分 = BLEND_λ × 模型回归 + (1-BLEND_λ) × 积分榜名次先验
# λ 搜索史：时间解析修复前 λ=0.55 最优（模型弱需先验兜底）；修复 f1api.dev 历史排位
# "1:29:179" 毫秒冒号格式后，纯模型（λ=1.0）双指标最优（领奖台 2.30/场，MAE 3.37），先验反成拖累
BLEND_LAMBDA = 1.0

# 安全车修正（2026-09-21 起；数据：analysis/compute_sc_rates.py → circuits_data.json sc_* 字段）
# sc_strength = min(sc_events_per_race/3, 1)：出现率多数赛道已饱和 0.6~1.0，
# 场均事件数区分度更好（阿尔伯特公园 5.0 vs 亨格罗宁 1.25）；events 缺失回退 sc_rate
SC_RESID_K = 0.6    # A1：残差缩放 resid × (1 + 0.6×sc_strength)，概率分布摊平（安全车只增方差无方向）
SC_GRID_DAMP = 0.2  # A2：网格阻尼 grid 项 × (1 - 0.2×sc_strength)，高 SC 赛道发车顺位参考性下降
                    # （非线性、真正改变排序；0.2 由 analysis/backtest_sc_effect.py 2023-2026 回测选定：
                    #  podium 2.02→2.05 升、MAE 3.28→3.30 平；0.3/0.5 在 2025/2026 伤指标被否）


class RacePredictionModel:
    """F1 比赛结果统计预测模型（名次制）"""

    def __init__(self, f1_api, f1cosmos=None, weather=None):
        self.f1_api = f1_api
        self.f1cosmos = f1cosmos
        self.weather = weather
        self._circuits = self._load_circuits()
        self._dataset_cache: Dict[int, List[Dict]] = {}
        self._standings_cache: Dict[tuple, Dict[str, int]] = {}
        # 预测结果缓存 {(season, round, premises_sig): (时间戳, 结果dict)}
        self._predict_cache: Dict[tuple, tuple] = {}
        # predict per-key single-flight：启动预热线程与实时提问并发算同一分站时合并，
        # 后到者等先到者算完直接吃缓存（2026-09-16 事故：容器启动 22s 后的提问与
        # 预热线程并发重复计算 9 个分站，双倍网络请求）
        import threading as _th
        self._predict_locks: Dict[tuple, _th.Lock] = {}
        self._predict_locks_guard = _th.Lock()

    # ==================== 基础数据 ====================

    def _load_circuits(self) -> Dict[str, Dict]:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "circuits_data.json")
        if not os.path.exists(path):
            path = os.path.join(DATA_DIR, "circuits_data.json")
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"[Prediction] 赛道数据加载失败: {e}")
            return {}

    @staticmethod
    def _median(vals: List[float]) -> float:
        vals = sorted(v for v in vals if v)
        n = len(vals)
        if not n:
            return 0.0
        return vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2

    @staticmethod
    def _safe_int(v, default: int = 99) -> int:
        """f1api.dev 的 grid 可能是字符串 "not available"，安全转整型"""
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def _race_record(self, race: Dict[str, Any], season: int) -> Optional[Dict[str, Any]]:
        rnd = int(race["round"])
        circuit_id = (race.get("Circuit", {}) or {}).get("circuitId", "")
        quali = self.f1_api.get_qualifying_pace(season, rnd)
        race_res = self.f1_api.get_session_results(rnd, "race", season=season)
        if not quali or not race_res or not race_res.get("entries"):
            return None

        q_median = self._median([q["best_time"] for q in quali])
        lap_km = (self._circuits.get(circuit_id) or {}).get("lap_length_km")
        speed_index = (lap_km / q_median * 3600) if (lap_km and q_median) else None

        q_by_driver = {q["driver_id"]: q for q in quali}
        entries = []
        for e in race_res["entries"]:
            did = e.get("driver_id", "")
            q = q_by_driver.get(did)
            entries.append({
                "driver_id": did,
                "driver": e.get("driver_name", ""),
                "team_id": e.get("team_id", ""),
                "team": e.get("team_name", ""),
                "finish_pos": self._safe_int(e.get("position")),
                "points": float(e.get("points") or 0),
                "grid": self._safe_int(e.get("grid")),
                "quali_pos": self._safe_int(q["position"]) if q else 99,
                "quali_pace": q["pace_index"] if q else None,
            })
        return {
            "season": season, "round": rnd, "circuit_id": circuit_id,
            "race_name": race.get("raceName", ""), "date": race.get("date", ""),
            "speed_index": speed_index, "entries": entries,
        }

    def build_season_dataset(self, year: int) -> List[Dict[str, Any]]:
        if year in self._dataset_cache:
            return self._dataset_cache[year]
        cache_file = os.path.join(DATA_DIR, f"prediction_dataset_{year}.json")
        if year < self.f1_api.season and os.path.exists(cache_file):
            try:
                with open(cache_file, encoding="utf-8") as f:
                    data = json.load(f)
                    self._dataset_cache[year] = data
                    return data
            except Exception:
                pass
        out = []
        for race in self.f1_api.get_schedule_for_year(year):
            try:
                rec = self._race_record(race, year)
                if rec:
                    out.append(rec)
            except Exception as e:
                logger.warning(f"[Prediction] {year} R{race.get('round')} 组装失败: {e}")
        if out and year < self.f1_api.season:
            try:
                with open(cache_file, "w", encoding="utf-8") as f:
                    json.dump(out, f, ensure_ascii=False)
            except Exception as e:
                logger.warning(f"[Prediction] 数据集落盘失败: {e}")
        self._dataset_cache[year] = out
        return out

    # ==================== 特征工程（名次制） ====================

    def _standings_before(self, year: int, round_num: int) -> Dict[str, int]:
        """本场前的车手积分榜排名 {driver_id: 名次}（按已完赛正赛积分累计，冲刺赛忽略）"""
        key = (year, round_num)
        if key in self._standings_cache:
            return self._standings_cache[key]
        pts: Dict[str, float] = {}
        for rec in self.build_season_dataset(year):
            if rec["round"] >= round_num:
                break
            for e in rec["entries"]:
                pts[e["driver_id"]] = pts.get(e["driver_id"], 0.0) + e.get("points", 0.0)
        ranked = sorted(pts.items(), key=lambda x: -x[1])
        result = {did: i + 1 for i, (did, _) in enumerate(ranked)}
        self._standings_cache[key] = result
        return result

    def _team_power_slopes(self, year: int, upto_round: int = None) -> Dict[str, float]:
        """车队动力敏感度：pace_index ~ speed_index 线性斜率（队名 canonical 归一）"""
        from .f1cosmos_api import canonical_team_key
        dataset = self.build_season_dataset(year)
        points: Dict[str, List] = {}
        speeds = [r["speed_index"] for r in dataset if r.get("speed_index")
                  and (not upto_round or r["round"] < upto_round)]
        mean_speed = sum(speeds) / len(speeds) if speeds else 0
        for rec in dataset:
            if not rec.get("speed_index"):
                continue
            if upto_round and rec["round"] >= upto_round:
                continue
            for e in rec["entries"]:
                if e["quali_pace"]:
                    key = canonical_team_key(e["team"])
                    points.setdefault(key, []).append((rec["speed_index"] - mean_speed, e["quali_pace"]))
        slopes = {}
        for key, pts in points.items():
            if len(pts) < 6:
                continue
            n = len(pts)
            sx = sum(p[0] for p in pts) / n
            sy = sum(p[1] for p in pts) / n
            num = sum((p[0] - sx) * (p[1] - sy) for p in pts)
            den = sum((p[0] - sx) ** 2 for p in pts)
            slopes[key] = (num / den) if den else 0.0
        return slopes

    def _driver_features(self, driver_id: str, team_name: str, season: int,
                         round_num: int, circuit_id: str, speed_index: float,
                         slopes: Dict[str, float], mean_speed: float) -> Dict[str, Any]:
        """车手名次制特征"""
        from .f1cosmos_api import canonical_team_key
        team_key = canonical_team_key(team_name)
        season_ds = self.build_season_dataset(season)

        # 积分榜排名（本场前）；未上榜（未得分/新秀）按榜尾处理，避免早期排除强势新秀
        standings = self._standings_before(season, round_num)
        field_size = max(len(standings), 20)
        stand_pos = standings.get(driver_id, field_size + 1)

        # 近期状态：近 FORM_WINDOW 场平均完赛名次 + 平均排位名次；无记录按榜尾
        finishes, qualis = [], []
        # 车手强度（队友间对比，隔离赛车因素）：本赛季本场前 与队友的场均名次差
        # 负值 = 强于队友（如维斯塔潘常年压制队友→其个人强度被低估时可识别）
        tm_fin_diffs, tm_q_diffs = [], []
        # 车队强度：本场前车队累计积分（车手积分按队求和）的场内排名
        team_pts: Dict[str, float] = {}
        for rec in season_ds:
            if rec["round"] >= round_num:
                continue
            mine = None
            mate = None
            for e in rec["entries"]:
                tk = canonical_team_key(e["team"])
                team_pts[tk] = team_pts.get(tk, 0) + (e.get("points") or 0)
                if e["driver_id"] == driver_id:
                    mine = e
                elif tk == team_key and mate is None:
                    mate = e
            if mine and mate:
                if mine["finish_pos"] < 90 and mate["finish_pos"] < 90:
                    tm_fin_diffs.append(mine["finish_pos"] - mate["finish_pos"])
                if mine["quali_pos"] < 90 and mate["quali_pos"] < 90:
                    tm_q_diffs.append(mine["quali_pos"] - mate["quali_pos"])
            for e in rec["entries"]:
                if e["driver_id"] == driver_id:
                    if e["finish_pos"] < 90:
                        finishes.append(e["finish_pos"])
                    if e["quali_pos"] < 90:
                        qualis.append(e["quali_pos"])
        form_finish = sum(finishes[-FORM_WINDOW:]) / len(finishes[-FORM_WINDOW:]) if finishes else None
        form_quali = sum(qualis[-FORM_WINDOW:]) / len(qualis[-FORM_WINDOW:]) if qualis else None
        tm_gap_finish = sum(tm_fin_diffs) / len(tm_fin_diffs) if tm_fin_diffs else None
        tm_gap_quali = sum(tm_q_diffs) / len(tm_q_diffs) if tm_q_diffs else None

        # 车队强度：本场前车队累计积分排名（实验特征 ctor_pos，未入向量）
        ctor_pts = team_pts.get(team_key, 0)
        ctor_pos = sorted(team_pts.values(), reverse=True).index(ctor_pts) + 1 if ctor_pts in team_pts.values() else len(team_pts) + 1

        # 赛道历史：近2年同赛道平均完赛名次（车手→车队→无）。
        # 2026-09-21 改均值→中位数：均值会被单次机械故障/DNF（P20）严重拉高，
        # 造成"车手近期很快但历史被一次事故判为无竞争力"的不合理结论（用户反馈）
        hist_driver, hist_team, hist_q = [], [], []
        # 圈速制赛道历史（pace_index，抗罚退/事故污染；2024 巴库诺里斯 Q1 出局
        # 但 pace 99.33 快于夺冠的皮亚斯特里 101.21——名次制完全丢失这一信号）
        hist_pace_d, hist_pace_t = [], []
        for y in range(season - HISTORY_YEARS, season):
            for rec in self.build_season_dataset(y):
                if rec["circuit_id"] != circuit_id:
                    continue
                for e in rec["entries"]:
                    if e["driver_id"] == driver_id and e["finish_pos"] < 90:
                        hist_driver.append(e["finish_pos"])
                    if canonical_team_key(e["team"]) == team_key and e["finish_pos"] < 90:
                        hist_team.append(e["finish_pos"])
                    if e["driver_id"] == driver_id and e["quali_pos"] < 90:
                        hist_q.append(e["quali_pos"])
                    if e["driver_id"] == driver_id and e.get("quali_pace"):
                        hist_pace_d.append(e["quali_pace"])
                    if canonical_team_key(e["team"]) == team_key and e.get("quali_pace"):
                        hist_pace_t.append(e["quali_pace"])

        def _robust_avg(vals):
            """中位数（抗单次离群值，如机械故障 DNF）"""
            if not vals:
                return None
            s = sorted(vals)
            n = len(s)
            return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2

        if hist_driver:
            circuit_hist = _robust_avg(hist_driver)
            hist_source = "driver"
        elif hist_team:
            circuit_hist = _robust_avg(hist_team)
            hist_source = "team"
        else:
            circuit_hist = None
            hist_source = "none"
        circuit_hist_q = _robust_avg(hist_q) if hist_q else circuit_hist
        circuit_hist_pace = (_robust_avg(hist_pace_d) if hist_pace_d
                             else (_robust_avg(hist_pace_t) if hist_pace_t
                                   else None))

        slope = slopes.get(team_key, 0.0)
        type_affinity = slope * ((speed_index or mean_speed) - mean_speed) if speed_index else 0.0

        # 长距离配速/轮胎衰减（OpenF1 stint 数据，正赛向量用；2026-09-21 用户建议：
        # 排位单圈强≠长距离强，正赛预期应更看 race pace 与轮胎衰减 + 相似特性赛道）
        race_pace_idx, tyre_deg_ms, similar_race_pace_idx, similar_basis = self._stint_features(
            driver_id, season, round_num, circuit_id)

        return {
            "stand_pos": stand_pos,
            "form_finish": round(form_finish, 2) if form_finish else None,
            "form_quali": round(form_quali, 2) if form_quali else None,
            "circuit_hist": round(circuit_hist, 2) if circuit_hist else None,
            "circuit_hist_q": round(circuit_hist_q, 2) if circuit_hist_q else None,
            "circuit_hist_pace": round(circuit_hist_pace, 3) if circuit_hist_pace else None,
            "race_pace_idx": race_pace_idx,
            "similar_race_pace_idx": similar_race_pace_idx,
            "similar_basis": similar_basis,
            "tyre_deg_ms": tyre_deg_ms,
            "history_source": hist_source,
            "type_affinity": round(type_affinity, 3),
            "team_key": team_key,
            # 车手强度（队友名次差，负值=强于队友）与车队强度（本场前车队积分排名，实验特征未入向量）
            "tm_gap_finish": round(tm_gap_finish, 2) if tm_gap_finish is not None else None,
            "tm_gap_quali": round(tm_gap_quali, 2) if tm_gap_quali is not None else None,
            "ctor_pos": ctor_pos,
        }

    def _sc_meta(self, circuit_id: str) -> Dict[str, Any]:
        """赛道安全车统计（SC+VSC+RedFlag，circuits_data.json sc_* 字段，
        2026-09-21 起由 analysis/compute_sc_rates.py 用 OpenF1 race_control 统计 2023+）。
        查不到（历史/无数据赛道）返回空 dict"""
        try:
            from .circuits_manager import CircuitsManager
            return CircuitsManager().get_circuit(circuit_id) or {}
        except Exception:
            return {}

    def _sc_strength(self, circuit_id: str) -> float:
        """安全车强度 0~1 = min(场均事件数/3, 1)（出现率多数赛道已饱和 0.6~1.0，
        场均事件数区分度更好：阿尔伯特公园 5.0 vs 亨格罗宁 1.25）；
        events 缺失回退 sc_rate，皆无返回 0（不做安全车修正）"""
        meta = self._sc_meta(circuit_id)
        ev = meta.get("sc_events_per_race")
        if ev:
            return min(float(ev) / 3.0, 1.0)
        return float(meta.get("sc_rate") or 0.0)

    def _sc_rate(self, circuit_id: str) -> float:
        """赛道安全车出现率（SC+VSC+RedFlag，0~1）薄封装，呈现层/回测用"""
        return float(self._sc_meta(circuit_id).get("sc_rate") or 0.0)

    def _stint_features(self, driver_id: str, season: int, upto_round: int,
                        circuit_id: str = None):
        """近 FORM_WINDOW 场已完赛的 stint 长距离配速指数与轮胎衰减（OpenF1 2023+），
        以及**相似特性赛道**的长距离配速（与目标赛道同下压力档位，如巴库低阻→蒙扎/拉斯维加斯）。

        race_pace_idx：全场 stint 中位圈速中位数 / 本车手最快 stint 中位圈速 ×100（>100=长距离快于中位）
        tyre_deg_ms：近几场 stint 衰减斜率均值（正值=衰减快/进站压力）
        similar_race_pace_idx：在**同下压力档位**赛道上的长距离配速指数（用户要求：正赛推测应看
                        相似特性赛道的 race pace，而非目标赛道的排位名次史）

        数据缺失返回中性值 None。stint 数据按分站永久缓存。
        """
        from .stint_analysis import StintAnalyzer
        try:
            season_ds = self.build_season_dataset(season)
            recent = [rec["round"] for rec in season_ds
                      if rec["round"] < upto_round][-FORM_WINDOW:]
            if not recent:
                return None, None, None, []
            sa = StintAnalyzer(self.f1_api, cache=self.cache)

            def _pace_idx(rounds):
                idxs = []
                for rd in rounds:
                    try:
                        st = sa.get_race_stints(season, rd)
                    except Exception:
                        continue
                    drivers = st.get("drivers") or {}
                    meds = [d["best_stint_median_s"] for d in drivers.values()
                            if d.get("best_stint_median_s")]
                    d = drivers.get(driver_id) or {}
                    if meds and d.get("best_stint_median_s"):
                        field_med = sorted(meds)[len(meds) // 2]
                        idxs.append(field_med / d["best_stint_median_s"] * 100)
                return (round(sum(idxs) / len(idxs), 2) if idxs else None)

            race_pace_idx = _pace_idx(recent)
            degs = []
            for rd in recent:
                try:
                    st = sa.get_race_stints(season, rd)
                    d = (st.get("drivers") or {}).get(driver_id) or {}
                    if d.get("avg_deg_ms") is not None:
                        degs.append(d["avg_deg_ms"])
                except Exception:
                    continue
            tyre_deg_ms = round(sum(degs) / len(degs), 1) if degs else None

            # 相似特性赛道：同下压力档位的已完赛分站（当前赛季）
            similar_race_pace_idx, similar_basis = None, []
            if circuit_id:
                try:
                    from .circuits_manager import CircuitsManager
                    cm = CircuitsManager()
                    tgt_df = (cm.get_circuit(circuit_id) or {}).get("downforce")
                    if tgt_df:
                        sim_rounds = [rec["round"] for rec in season_ds
                                      if rec["round"] < upto_round
                                      and rec["circuit_id"] != circuit_id
                                      and (cm.get_circuit(rec["circuit_id"]) or {}).get("downforce") == tgt_df]
                        if sim_rounds:
                            similar_race_pace_idx = _pace_idx(sim_rounds)
                            if similar_race_pace_idx is not None:
                                similar_basis = sim_rounds
                except Exception:
                    pass
            return race_pace_idx, tyre_deg_ms, similar_race_pace_idx, similar_basis
        except Exception as e:
            logger.debug(f"[Prediction] stint 特征计算失败（按中性值兜底）: {e}")
            return None, None, None, []

    def _upgrades_count_map(self, year: int) -> Dict[tuple, int]:
        if not self.f1cosmos or not hasattr(self.f1cosmos, "get_season_events_upgrades"):
            return {}
        try:
            from .f1cosmos_api import canonical_team_key
            out: Dict[tuple, int] = {}
            for u in self.f1cosmos.get_season_events_upgrades(year):
                if not u.get("round"):
                    continue
                key = (int(u["round"]), canonical_team_key(u.get("team", "")))
                out[key] = out.get(key, 0) + 1
            return out
        except Exception as e:
            logger.warning(f"[Prediction] 升级件计数获取失败({year}): {e}")
            return {}

    # ==================== 模型拟合 ====================

    def fit(self, train_years=(2023, FIT_YEAR)) -> Dict[str, Any]:
        """拟合排位/正赛权重（默认 2023+2024 两赛季合并，增大样本量）"""
        import numpy as np
        if isinstance(train_years, int):
            train_years = (train_years,)
        upgrades_maps = {y: self._upgrades_count_map(y) for y in train_years}

        Xq, Yq, Xr, Yr = [], [], [], []
        mean_speed = 0.0
        for train_year in train_years:
            dataset = self.build_season_dataset(train_year)
            speeds = [r["speed_index"] for r in dataset if r.get("speed_index")]
            mean_speed = sum(speeds) / len(speeds) if speeds else mean_speed
            upgrades_map = upgrades_maps[train_year]
            for rec in dataset:
                if not rec.get("speed_index"):
                    continue
                slopes = self._team_power_slopes(train_year, upto_round=rec["round"])
                for e in rec["entries"]:
                    f = self._driver_features(e["driver_id"], e["team"], train_year,
                                              rec["round"], rec["circuit_id"],
                                              rec["speed_index"], slopes, mean_speed)
                    if f["stand_pos"] is None:
                        continue
                    up = -min(upgrades_map.get((rec["round"], f["team_key"]), 0), 6) / 6.0  # 负向=名次前移
                    fq = f["form_quali"] if f["form_quali"] is not None else 20.0
                    ff = f["form_finish"] if f["form_finish"] is not None else 20.0
                    cq = f["circuit_hist_q"] or fq
                    cf = f["circuit_hist"] or ff
                    tq = f["tm_gap_quali"] if f["tm_gap_quali"] is not None else 0.0
                    tf = f["tm_gap_finish"] if f["tm_gap_finish"] is not None else 0.0
                    if e["quali_pos"] < 90:
                        Xq.append([1.0, f["stand_pos"], fq, cq, f["type_affinity"], up,
                                   tq])
                        Yq.append(e["quali_pos"])
                    if e["finish_pos"] < 90 and e["grid"] < 90:
                        # 正赛向量不含往年赛道历史（cf）：实证 2025 回测 去除后
                        # 领奖台 2.30→2.39、MAE 3.32→3.25——往年数据受赛车性能代差污染
                        # v6 加入长距离配速(race_pace_idx)+轮胎衰减(tyre_deg_ms)：用户建议
                        # 排位单圈强≠长距离强，正赛预期应更看 race pace 与进站压力
                        # race_pace 优先用相似特性赛道(similar_race_pace_idx)，无相似赛道回退通用
                        _rp = f["similar_race_pace_idx"] or f["race_pace_idx"]
                        rp = _rp if _rp is not None else 100.0
                        td = f["tyre_deg_ms"] if f["tyre_deg_ms"] is not None else 0.0
                        Xr.append([1.0, f["stand_pos"], ff, e["grid"],
                                   f["type_affinity"], up, tf, rp, td])
                        Yr.append(e["finish_pos"])

        Xq, Yq = np.array(Xq), np.array(Yq)
        w, *_ = np.linalg.lstsq(Xq, Yq, rcond=None)
        pred_q = Xq @ w
        q_resid = float(np.std(Yq - pred_q))
        ss_res = float(((Yq - pred_q) ** 2).sum())
        ss_tot = float(((Yq - Yq.mean()) ** 2).sum())
        quali_r2 = 1 - ss_res / ss_tot if ss_tot else 0

        Xr, Yr = np.array(Xr), np.array(Yr)
        a, *_ = np.linalg.lstsq(Xr, Yr, rcond=None)
        pred_r = Xr @ a
        r_resid = float(np.std(Yr - pred_r))
        ss_res_r = float(((Yr - pred_r) ** 2).sum())
        ss_tot_r = float(((Yr - Yr.mean()) ** 2).sum())
        race_r2 = 1 - ss_res_r / ss_tot_r if ss_tot_r else 0

        model = {
            "version": MODEL_VERSION, "fit_year": list(train_years),
            "quali_weights": [round(float(x), 5) for x in w],
            "quali_resid_std": round(q_resid, 3), "quali_r2": round(quali_r2, 3),
            "race_weights": [round(float(x), 5) for x in a],
            "race_resid_std": round(r_resid, 3), "race_r2": round(race_r2, 3),
            "mean_speed_index": round(mean_speed, 2),
            "samples": {"quali": len(Yq), "race": len(Yr)},
        }
        logger.info(f"[Prediction] v2拟合: quali R²={quali_r2:.3f} σ={q_resid:.2f} | "
                    f"race R²={race_r2:.3f} σ={r_resid:.2f} (样本 {len(Yq)}/{len(Yr)})")
        return model

    # ==================== 回测 ====================

    def backtest(self, year: int = BACKTEST_YEAR, model: Dict = None) -> Dict[str, Any]:
        model = model or self.fit()
        dataset = self.build_season_dataset(year)
        w, a = model["quali_weights"], model["race_weights"]
        mean_speed = model["mean_speed_index"]
        upgrades_map = self._upgrades_count_map(year)

        podium_hits, pos_errs, quali_top3_hits, top10_errs = [], [], [], []
        races_tested = 0
        for rec in dataset:
            if not rec.get("speed_index"):
                continue
            slopes = self._team_power_slopes(year, upto_round=rec["round"])
            feats = []
            for e in rec["entries"]:
                f = self._driver_features(e["driver_id"], e["team"], year, rec["round"],
                                          rec["circuit_id"], rec["speed_index"], slopes, mean_speed)
                if f["stand_pos"] is None:
                    continue
                fq = f["form_quali"] if f["form_quali"] is not None else 20.0
                ff = f["form_finish"] if f["form_finish"] is not None else 20.0
                up = -min(upgrades_map.get((rec["round"], f["team_key"]), 0), 6) / 6.0
                tq = f["tm_gap_quali"] if f.get("tm_gap_quali") is not None else 0.0
                tf = f["tm_gap_finish"] if f.get("tm_gap_finish") is not None else 0.0
                exp_q = (w[0] + w[1] * f["stand_pos"] + w[2] * fq
                         + w[3] * (f["circuit_hist_q"] or fq)
                         + w[4] * f["type_affinity"] + w[5] * up
                         + w[6] * tq)
                exp_f = (a[0] + a[1] * f["stand_pos"] + a[2] * ff
                         + a[3] * e["grid"] + a[4] * f["type_affinity"] + a[5] * up
                         + a[6] * tf)
                # 与积分榜先验混合（λ 搜索确定，抗模型过拟合）
                exp_f = BLEND_LAMBDA * exp_f + (1 - BLEND_LAMBDA) * f["stand_pos"]
                feats.append((e, exp_q, exp_f))
            if len(feats) < 10:
                continue

            pred_q_order = [e["driver_id"] for e, _, _ in sorted(feats, key=lambda x: x[1])]
            actual_q = sorted([e for e, _, _ in feats if e["quali_pos"] < 90],
                              key=lambda e: e["quali_pos"])
            quali_top3_hits.append(len(set(pred_q_order[:3]) & {e["driver_id"] for e in actual_q[:3]}))

            pred_race = [e["driver_id"] for e, _, _ in sorted(feats, key=lambda x: x[2])]
            actual_race = sorted([e for e, _, _ in feats if e["finish_pos"] < 90],
                                 key=lambda e: e["finish_pos"])
            actual_ids = [e["driver_id"] for e in actual_race]
            podium_hits.append(len(set(pred_race[:3]) & set(actual_ids[:3])))
            pred_pos = {did: i + 1 for i, did in enumerate(pred_race)}
            for i, did in enumerate(actual_ids):
                if did in pred_pos:
                    pos_errs.append(abs(pred_pos[did] - (i + 1)))
                    if i < 10 or pred_pos[did] <= 10:
                        top10_errs.append(abs(pred_pos[did] - (i + 1)))
            races_tested += 1

        report = {
            "backtest_year": year, "fit_year": model["fit_year"],
            "races_tested": races_tested,
            "podium_hits_per_race": round(sum(podium_hits) / max(races_tested, 1), 2),
            "mean_abs_pos_error": round(sum(pos_errs) / max(len(pos_errs), 1), 2),
            "top10_abs_pos_error": round(sum(top10_errs) / max(len(top10_errs), 1), 2),
            "quali_top3_hits_per_race": round(sum(quali_top3_hits) / max(races_tested, 1), 2),
            "podium_hits_detail": podium_hits,
        }
        logger.info(f"[Prediction] v2回测 {year}: 领奖台 {report['podium_hits_per_race']}/场, "
                    f"MAE {report['mean_abs_pos_error']}, 排位Top3 {report['quali_top3_hits_per_race']}/场")
        return report

    def fit_and_save(self) -> Dict[str, Any]:
        model = self.fit()
        model["backtest"] = self.backtest(model=model)
        try:
            with open(MODEL_FILE, "w", encoding="utf-8") as f:
                json.dump(model, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"[Prediction] 模型落盘失败: {e}")
        return model

    def load_or_fit(self) -> Dict[str, Any]:
        """加载落盘模型；版本不符或缺失才重拟合"""
        if os.path.exists(MODEL_FILE):
            try:
                with open(MODEL_FILE, encoding="utf-8") as f:
                    m = json.load(f)
                if m.get("version") == MODEL_VERSION:
                    return m
            except Exception:
                pass
        return self.fit_and_save()

    # ==================== 预测 ====================

    def predict(self, gp_query: str, season: int = None,
                premises: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        预测入口：分站解析 + 结果缓存 + per-key single-flight，计算主体在 _predict_compute。

        Args:
            gp_query: 分站名/轮次（如 "蒙扎" / "R13"）
            season: 赛季（默认当前）
            premises: {"penalties": {车手名: 罚退位数}, "standins": {下车手: 代打车手}}
        """
        import re
        from .f1_api import find_race_by_circuit

        season = season or self.f1_api.season
        schedule = self.f1_api.get_schedule_for_year(season)
        q = (gp_query or "").strip()
        race = None
        m = re.search(r"^[Rr]\s?0*(\d{1,2})$", q)
        if m:
            race = next((r for r in schedule if int(r.get("round", 0)) == int(m.group(1))), None)
        elif q:
            race = find_race_by_circuit(schedule, q)
        if not race:
            race = self.f1_api.get_next_race()
        if not race:
            return {"error": "未找到分站且本赛季已无剩余比赛"}

        round_num = int(race["round"])
        circuit_id = (race.get("Circuit", {}) or {}).get("circuitId", "")

        # 结果级缓存：同分站+同前提 30 分钟内直接复用（避免 L1/兜底路径重复计算）
        import time as _t
        _prem_sig = json.dumps(premises or {}, sort_keys=True, ensure_ascii=False)
        _cache_key = (season, round_num, _prem_sig)
        _hit = self._predict_cache.get(_cache_key)
        if _hit and _t.time() - _hit[0] < PREDICT_CACHE_TTL:
            logger.debug(f"预测缓存命中: {season} R{round_num}")
            return dict(_hit[1])  # 浅拷贝防调用方（f1_tools 追加 circuit_profile 等）改写缓存
        # 缓存上限：超出时淘汰最旧条目（防长期运行内存膨胀）
        if len(self._predict_cache) >= 50:
            oldest = min(self._predict_cache, key=lambda k: self._predict_cache[k][0])
            self._predict_cache.pop(oldest, None)

        # single-flight：同一 (season, round, premises) 并发计算合并，
        # 后到者拿锁后先复查缓存（先到者可能已算完写入），未命中才自己算
        import threading as _th
        with self._predict_locks_guard:
            lock = self._predict_locks.setdefault(_cache_key, _th.Lock())
        with lock:
            _hit = self._predict_cache.get(_cache_key)
            if _hit and _t.time() - _hit[0] < PREDICT_CACHE_TTL:
                logger.debug(f"预测缓存命中(single-flight合并): {season} R{round_num}")
                return dict(_hit[1])
            return self._predict_compute(race, season, round_num, circuit_id,
                                         premises, _cache_key, _t)

    def _predict_compute(self, race: Dict[str, Any], season: int, round_num: int,
                         circuit_id: str, premises: Dict[str, Any],
                         _cache_key: tuple, _t) -> Dict[str, Any]:
        """预测计算主体（数据集构建/特征/蒙特卡洛），调用方已持 single-flight 锁"""
        import numpy as np

        model = self.load_or_fit()
        w, a = model["quali_weights"], model["race_weights"]
        mean_speed = model["mean_speed_index"]

        # 阵容：最近一场已完赛分站名单
        roster = []
        for rec in reversed(self.build_season_dataset(season)):
            if rec["round"] < round_num and rec["entries"]:
                roster = [{"driver_id": e["driver_id"], "driver": e["driver"],
                           "team_id": e["team_id"], "team": e["team"]} for e in rec["entries"]]
                break
        if not roster:
            return {"error": "本赛季暂无已完成分站，无法建立车手阵容基线"}

        # 本站速度指标：近2年同赛道均值
        past_speeds = [rec["speed_index"] for y in range(season - HISTORY_YEARS, season)
                       for rec in self.build_season_dataset(y)
                       if rec["circuit_id"] == circuit_id and rec.get("speed_index")]
        speed_index = (sum(past_speeds) / len(past_speeds)) if past_speeds else mean_speed

        slopes = self._team_power_slopes(season, upto_round=round_num)
        upgrades_map = self._upgrades_count_map(season)

        # 安全车强度（A1 残差缩放 + A2 网格阻尼共用）与统计元数据（呈现层透传）
        sc_meta = self._sc_meta(circuit_id)
        sc_s = self._sc_strength(circuit_id)

        premises = premises or {}
        penalties = dict(premises.get("penalties") or {})
        standins = dict(premises.get("standins") or {})

        # 练习赛节奏（当前周末已完成的 FP 环节）：车手最佳练习名次，
        # 与近期排位状态各半融合——本站当前节奏的直接证据
        # （实证需求：练习赛明显强势的车手在模型中应被识别，如 2026 西班牙站诺里斯）
        fp_rank: Dict[str, float] = {}
        if season >= self.f1_api.season:
            try:
                for fp_type in ("fp1", "fp2", "fp3"):
                    fp_res = self.f1_api.get_session_results(round_num, fp_type, retries=1, season=season)
                    for e in (fp_res or {}).get("entries") or []:
                        pos = self._safe_int(e.get("position"))
                        if pos is None or pos >= 90:
                            continue
                        if e["driver_id"] not in fp_rank or pos < fp_rank[e["driver_id"]]:
                            fp_rank[e["driver_id"]] = pos
            except Exception:
                pass
        if standins:
            new_roster = []
            for r0 in roster:
                if r0["driver"] in standins or r0["driver_id"] in standins:
                    sub = standins.get(r0["driver"]) or standins.get(r0["driver_id"])
                    new_roster.append({**r0, "driver": sub, "driver_id": sub,
                                       "standin_for": r0["driver"]})
                else:
                    new_roster.append(r0)
            roster = new_roster

        feats = []
        for r0 in roster:
            f = self._driver_features(r0["driver_id"], r0["team"], season, round_num,
                                      circuit_id, speed_index, slopes, mean_speed)
            if f["stand_pos"] is None:
                continue
            up_count = upgrades_map.get((round_num, f["team_key"]), 0)
            up = -min(up_count, 6) / 6.0
            fq = f["form_quali"] if f["form_quali"] is not None else 20.0
            # 练习赛节奏融合：本站 FP 最佳名次与近期排位状态各半
            fpr = fp_rank.get(r0["driver_id"])
            fp_note = None
            if fpr is not None:
                fq = 0.5 * fq + 0.5 * fpr
                fp_note = fpr
            tq = f["tm_gap_quali"] if f.get("tm_gap_quali") is not None else 0.0
            tf = f["tm_gap_finish"] if f.get("tm_gap_finish") is not None else 0.0
            exp_q = (w[0] + w[1] * f["stand_pos"] + w[2] * fq
                     + w[3] * (f["circuit_hist_q"] or fq)
                     + w[4] * f["type_affinity"] + w[5] * up
                     + w[6] * tq)
            feats.append({**r0, **f, "upgrade_count": up_count, "exp_q": exp_q,
                          "fp_rank": fp_note})
        if len(feats) < 10:
            return {"error": "可用车手特征不足"}

        # 若本站排位已结束，以真实排位作为发车格，不重复预测已发生环节
        # （实证：排位后引用预测曾用模型预测格，与真实排位矛盾）
        actual_quali = None
        try:
            qres = self.f1_api.get_session_results(round_num, "qualifying", season=season)
            if qres and qres.get("entries"):
                actual_quali = qres["entries"]
        except Exception:
            pass

        # 蒙特卡洛：排位残差扰动 → 领奖台/前十概率（排位未进行时）
        if not actual_quali:
            # A1：安全车高发赛道放大残差，概率分布更平（不虚高确定感）
            resid = (model["quali_resid_std"] or 2.0) * (1.0 + SC_RESID_K * sc_s)
            rng = np.random.default_rng(42)
            n = len(feats)
            podium_counts = np.zeros(n)
            top10_counts = np.zeros(n)
            for _ in range(MC_SAMPLES):
                sampled = np.array([f["exp_q"] for f in feats]) + rng.normal(0, resid, n)
                order = np.argsort(sampled)
                podium_counts[order[:3]] += 1
                top10_counts[order[:min(10, n)]] += 1

            order = sorted(range(n), key=lambda i: feats[i]["exp_q"])
            pred_quali = []
            for pos, i in enumerate(order, 1):
                f = feats[i]
                pred_quali.append({
                    "pos": pos, "driver": f["driver"], "team": f["team"],
                    "exp_quali_pos": round(f["exp_q"], 2),
                    "quali_top3_prob": round(float(podium_counts[i]) / MC_SAMPLES, 3),
                    "top10_prob": round(float(top10_counts[i]) / MC_SAMPLES, 3),
                })
        else:
            # 真实排位：按实际名次构造 pred_quali（仅特征在册车手）
            import unicodedata as _ud

            def _norm_name(s):
                s = _ud.normalize("NFKD", s or "")
                return "".join(c for c in s if not _ud.combining(c)).lower().replace(" ", "")

            feats_by_name = {_norm_name(f["driver"]): f for f in feats}
            pred_quali = []
            used = set()
            for e in actual_quali:
                f = feats_by_name.get(_norm_name(e.get("driver_name", "")))
                if not f:
                    continue  # 无特征基线的车手（如新代打）跳过
                p = int(e["position"])
                used.add(_norm_name(f["driver"]))
                pred_quali.append({
                    "pos": p, "driver": f["driver"], "team": f["team"],
                    "exp_quali_pos": p,
                    "quali_top3_prob": 1.0 if p <= 3 else 0.0,
                    "top10_prob": 1.0 if p <= 10 else 0.0,
                })
            # 特征在册但排位缺席（退赛/未出场）排最后
            tail = sorted((f for f in feats if _norm_name(f["driver"]) not in used),
                          key=lambda x: x["exp_q"])
            for f in tail:
                pred_quali.append({
                    "pos": len(pred_quali) + 1, "driver": f["driver"], "team": f["team"],
                    "exp_quali_pos": round(f["exp_q"], 2),
                    "quali_top3_prob": 0.0, "top10_prob": 0.0,
                })
            pred_quali.sort(key=lambda x: x["pos"])

        # 发车 = 预测排位 + 罚退位移
        grid_order = [dict(item) for item in pred_quali]
        for item in grid_order:
            pen = penalties.get(item["driver"]) or 0
            if pen:
                item["pos"] += pen
                item["penalty"] = pen
        grid_order.sort(key=lambda x: x["pos"])
        grid_rank = {item["driver"]: i + 1 for i, item in enumerate(grid_order)}

        # 正赛预测
        race_rows = []
        for i, item in enumerate(pred_quali):
            f = feats[[x["driver"] for x in pred_quali].index(item["driver"])]
            fq = f["form_quali"] if f["form_quali"] is not None else 20.0
            ff = f["form_finish"] if f["form_finish"] is not None else 20.0
            _rp = f["similar_race_pace_idx"] or f["race_pace_idx"]
            rp = _rp if _rp is not None else 100.0
            td = f["tyre_deg_ms"] if f["tyre_deg_ms"] is not None else 0.0
            # A2 网格阻尼：高 SC 赛道发车顺位参考性下降（SC 打乱名次与 grid 的关联），
            # 压缩 grid 项贡献，让 form/race_pace 等特征相对主导——非线性，真正改变排序
            exp_f = (a[0] + a[1] * f["stand_pos"] + a[2] * ff
                     + a[3] * grid_rank[item["driver"]] * (1.0 - SC_GRID_DAMP * sc_s)
                     + a[4] * f["type_affinity"]
                     + a[5] * (-min(f["upgrade_count"], 6) / 6.0)
                     + a[6] * (f["tm_gap_finish"] if f.get("tm_gap_finish") is not None else 0.0)
                     + a[7] * rp + a[8] * td)
            exp_f = BLEND_LAMBDA * exp_f + (1 - BLEND_LAMBDA) * f["stand_pos"]
            race_rows.append({"driver": item["driver"], "team": item["team"],
                              "grid": grid_rank[item["driver"]], "exp_finish": exp_f,
                              **({"penalty": item["penalty"]} if item.get("penalty") else {})})
        race_rows.sort(key=lambda x: x["exp_finish"])
        pred_race = [{"pos": pos, **{k: (round(v, 2) if k == "exp_finish" else v)
                                     for k, v in r0.items()}}
                     for pos, r0 in enumerate(race_rows, 1)]

        result = {
            "gp": race.get("raceName"), "round": round_num, "season": season,
            "circuit_id": circuit_id, "speed_index": round(speed_index, 1),
            "qualifying_actual": bool(actual_quali),
            # 安全车统计（呈现层：高 SC 赛道必须声明不确定性放大）
            "sc_rate": float(sc_meta.get("sc_rate") or 0.0),
            "sc_events_per_race": sc_meta.get("sc_events_per_race"),
            "sc_strength": round(sc_s, 3),
            # 高 SC 赛道现成警示句：LLM 引用数据字段远比遵守提示词规则可靠
            # （实证 2026-09-23：prediction.txt 规则 3b 两次被 L2 忽略，sc_warning 字段引用一次到位）
            **({"sc_warning": (
                f"⚠️ 本站为安全车高发赛道（2023+ {float(sc_meta.get('sc_rate') or 0)*100:.0f}% 场次"
                f"出 SC/VSC/红旗"
                + (f"，场均 {sc_meta['sc_events_per_race']} 次" if sc_meta.get("sc_events_per_race") else "")
                + "），安全车会随机打乱名次、放大不确定性；模型已按此口径处理："
                  "概率分布已放宽（头部概率不虚高、尾部爆冷概率上升），发车顺位权重已下调，预测置信度降低"
            )} if float(sc_meta.get("sc_rate") or 0.0) >= 0.6 else {}),
            "predicted_qualifying": pred_quali,
            "predicted_race": pred_race,
            "feature_breakdown": [{
                "driver": f["driver"], "team": f["team"],
                "stand_pos": f["stand_pos"], "form_finish": f["form_finish"],
                "form_quali": f["form_quali"], "circuit_hist": f["circuit_hist"],
                # circuit_hist_q（排位史，排位向量权重最大特征）与 circuit_hist_pace（圈速制历史）
                # 必须输出——此前缺失导致用户看不到"为何某车手被压低"的真实原因（2026-09-21 排查）
                "circuit_hist_q": f.get("circuit_hist_q"),
                "circuit_hist_pace": f.get("circuit_hist_pace"),
                # 长距离配速/轮胎衰减（v6 正赛特征）：race_pace_idx>100=长距离快于中位，
                # tyre_deg_ms 正值=衰减快（进站压力代理），供呈现层解释"正赛预期"
                "race_pace_idx": f.get("race_pace_idx"),
                # 相似特性赛道的长距离配速（与目标赛道同下压力档位，如巴库低阻→蒙扎/拉斯维加斯），
                # similar_basis=参与计算的相似赛道路径（分站轮次）
                "similar_race_pace_idx": f.get("similar_race_pace_idx"),
                "similar_basis": f.get("similar_basis"),
                "tyre_deg_ms": f.get("tyre_deg_ms"),
                "history_source": f["history_source"], "type_affinity": f["type_affinity"],
                "upgrades_this_gp": f["upgrade_count"],
                "tm_gap_finish": f.get("tm_gap_finish"), "tm_gap_quali": f.get("tm_gap_quali"),
                "ctor_pos": f.get("ctor_pos"), "fp_rank": f.get("fp_rank"),
            } for f in sorted(feats, key=lambda x: x["exp_q"])[:10]],
            "premises_applied": {"penalties": penalties, "standins": standins},
            "model_meta": {
                "version": MODEL_VERSION, "fit_year": model["fit_year"],
                "quali_r2": model["quali_r2"], "race_r2": model.get("race_r2"),
                "backtest": model.get("backtest"),
                "note": "统计模型输出（名次制线性回归+残差蒙特卡洛概率）；"
                        "安全车/事故/机械故障不可预测，概率为统计置信",
            },
        }
        self._predict_cache[_cache_key] = (_t.time(), result)
        return result

    # ==================== 总冠军争夺推演 ====================

    def championship_projection(self, season: int = None,
                                mc_uniform: int = 10000, mc_model: int = 5000) -> Dict[str, Any]:
        """
        总冠军争夺推演（三层数值，互补回答"理论可能"与"实力概率"）：

        A. 数学层（确定性）：剩余满分（正赛25/冲刺8）、理论存活判定（满分能否追平领跑者）、
           领跑者 magic number（最早锁冠分站）
        B. 理论概率层（均匀蒙特卡洛）：剩余每环节名次在全部车手中均匀随机，
           回答"纯数学上还有可能吗、多渺茫"——不含任何实力评估
        C. 模型概率层（回归+残差蒙特卡洛）：逐站用名次制回归期望 + race_resid_std 扰动
           采样名次→积分累加，回答"按本赛季实力推演概率多大"
        """
        import numpy as np
        from datetime import datetime as _dt

        season = season or self.f1_api.season
        today = _dt.now().date()

        # 当前积分榜
        standings = self.f1_api.get_current_standings(force_refresh=True) or {}
        lists = ((standings.get("drivers") or {}).get("MRData", {})
                 .get("StandingsTable", {}).get("StandingsLists") or [])
        entries = lists[0].get("DriverStandings", []) if lists else []
        if not entries:
            return {"error": "积分榜数据不可用"}
        drivers = []  # (name, points)
        for e in entries:
            try:
                pts = float(e.get("points", 0))
            except (ValueError, TypeError):
                continue
            d = e.get("Driver", {})
            name = f"{d.get('givenName', '')} {d.get('familyName', '')}".strip() or e.get("driverId", "?")
            drivers.append((name, pts))
        if not drivers:
            return {"error": "积分榜解析为空"}
        drivers.sort(key=lambda x: -x[1])
        leader_name, leader_pts = drivers[0]

        # 剩余赛程（正赛日 >= 今天视为未赛）
        schedule = self.f1_api.get_schedule_for_year(season) or []
        remaining = []
        for race in schedule:
            try:
                rd = _dt.strptime(race.get("date", ""), "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            if rd >= today:
                remaining.append({"round": int(race.get("round", 0)),
                                  "name": race.get("raceName", ""),
                                  "sprint": bool(race.get("Sprint"))})
        n_races = len(remaining)
        n_sprints = sum(1 for r in remaining if r["sprint"])
        max_per_race = 25
        max_per_sprint = 8
        max_remaining = n_races * max_per_race + n_sprints * max_per_sprint
        if not remaining:
            return {"error": "本赛季已无剩余比赛", "season": season}

        names = [n for n, _ in drivers]
        base = np.array([p for _, p in drivers])

        # ---- A. 数学层 ----
        magic = max_remaining - int(leader_pts - drivers[1][1]) + 1 if len(drivers) > 1 else 0
        # 最早锁冠分站：领跑者全胜+追赶者零分的极限路径
        clinch_at = None
        sim_gap = leader_pts - drivers[1][1] if len(drivers) > 1 else 0
        rem_max = max_remaining
        for r in remaining:
            gain = max_per_race + (max_per_sprint if r["sprint"] else 0)
            sim_gap += gain
            rem_max -= gain
            if sim_gap > rem_max:
                clinch_at = r["name"]
                break

        # ---- B. 理论概率层：均匀蒙特卡洛 ----
        rng = np.random.default_rng(2026)
        n = len(drivers)
        uni_counts = np.zeros(n)
        # 积分表长度对齐车手数（截断或补零），防小车手池下积分表长度不齐
        race_table = np.array((RACE_POINTS + [0] * n)[:n])
        sprint_table = np.array((SPRINT_POINTS + [0] * n)[:n])
        session_tables = []
        for r in remaining:
            session_tables.append(race_table)
            if r["sprint"]:
                session_tables.append(sprint_table)
        for _ in range(mc_uniform):
            totals = base.copy()
            for table in session_tables:
                totals += table[rng.permutation(n)]
            win = np.max(totals)
            uni_counts += (totals == win) / max(int((totals == win).sum()), 1)
        uni_prob = uni_counts / mc_uniform

        # ---- C. 模型概率层：逐站回归期望 + 残差扰动 ----
        model = self.load_or_fit()
        resid = model.get("race_resid_std") or 3.0
        exp_map: Dict[str, float] = {}   # round -> {driver: exp_finish}
        for r in remaining:
            try:
                pred = self.predict(f"R{r['round']}", season=season)
            except Exception:
                pred = None
            em = {}
            if pred and "error" not in pred:
                for row in pred.get("predicted_race") or []:
                    em[row["driver"]] = float(row.get("exp_finish") or 20.0)
            exp_map[r["round"]] = em
        # 姓名匹配（predict 用 roster 名，standings 用全名；模糊按姓匹配）
        def _match_exp(em, name):
            if name in em:
                return em[name]
            last = name.split()[-1].lower()
            for k, v in em.items():
                if last and last in k.lower():
                    return v
            return 19.5  # 无特征车手按末位期望

        mod_counts = np.zeros(n)
        rng2 = np.random.default_rng(42)
        n_race_scorers = min(10, n)
        n_sprint_scorers = min(8, n)
        # 分站轮次 → circuit_id（A1 按各分站安全车率分别缩放残差）
        round_to_cid = {int(x.get("round", 0)): (x.get("Circuit", {}) or {}).get("circuitId", "")
                        for x in schedule}
        for _ in range(mc_model):
            totals = base.copy()
            for r in remaining:
                em = exp_map[r["round"]]
                exp = np.array([_match_exp(em, nm) for nm in names])
                # A1：该分站安全车强度放大残差
                resid_r = resid * (1.0 + SC_RESID_K * self._sc_strength(round_to_cid.get(r["round"], "")))
                order = np.argsort(exp + rng2.normal(0, resid_r, n))
                pts = np.zeros(n)
                pts[order[:n_race_scorers]] = RACE_POINTS[:n_race_scorers]
                totals += pts
                if r["sprint"]:
                    order = np.argsort(exp + rng2.normal(0, resid_r, n))
                    pts = np.zeros(n)
                    pts[order[:n_sprint_scorers]] = SPRINT_POINTS[:n_sprint_scorers]
                    totals += pts
            win = np.max(totals)
            mod_counts += (totals == win) / max(int((totals == win).sum()), 1)
        mod_prob = mod_counts / mc_model

        # ---- 汇总 ----
        rows = []
        for i, (nm, pts) in enumerate(drivers):
            max_possible = pts + max_remaining
            gap = leader_pts - pts
            rows.append({
                "pos": i + 1, "driver": nm, "points": pts,
                "gap_to_leader": int(gap),
                "max_possible": int(max_possible),
                "in_contention": max_possible >= leader_pts,
                "required_points_ratio": round((gap + 1) / max_remaining, 3) if gap >= 0 else 0.0,
                "theoretical_prob_uniform": round(float(uni_prob[i]), 5),
                "model_prob": round(float(mod_prob[i]), 5),
            })
        return {
            "season": season,
            "as_of": today.isoformat(),
            "races_left": n_races, "sprints_left": n_sprints,
            "max_points_remaining": max_remaining,
            "leader": {"driver": leader_name, "points": leader_pts,
                       "magic_number": max(magic, 0),
                       "earliest_clinch_race": clinch_at},
            "drivers": rows,
            "method_note": (
                "理论存活=剩余满分能否追平领跑者（纯数学）；"
                f"理论概率=均匀随机名次蒙特卡洛{mc_uniform}次（每环节所有车手等概率，衡量数学渺茫度，不含实力）；"
                f"模型概率=名次制回归逐站期望+残差扰动蒙特卡洛{mc_model}次（含本赛季实力评估）"),
        }
