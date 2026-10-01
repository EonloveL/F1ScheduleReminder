"""
F1赛程提醒机器人 - 车队实力画像分析器（数据驱动，非人工档案）

回答"车队优势点在哪"必须用数据推导。全部指标从赛季数据集
（排位 pace_index/完赛名次）+ 赛道工程特性档案（circuits_data.json meta）
+ FIA 升级件申报（2024+）计算：

- pace_season_avg / pace_recent_avg / trend：车队最快车手排位 pace 赛季均值 vs 近4场
- by_downforce / by_altitude / by_tire_stress：按赛道类型拆分的相对强弱
  （值=该类赛道队pace均值 - 其余赛道队pace均值，负值=该类型相对更强）
- sunday_delta：正赛名次 - 排位名次 均值（负=周日更强→正赛节奏/轮胎管理代理指标）
- upgrade_effects：升级申报前后各2场的队 pace 均值变化（FIA 文档 2024 起）

缓存：data/cache/team_strengths_{season}.json，当前赛季 24h / 历史永久。
"""

import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

ALT_HIGH_M = 500  # 高海拔阈值（红牛环660/英特拉格斯750/墨西哥2240/拉斯维加斯620/马德里650）
RECENT_WINDOW = 4


class TeamStrengthAnalyzer:
    """车队实力画像：赛道类型×升级件×排位圈速×正赛兑现率"""

    def __init__(self, f1_api, f1cosmos=None, season: int = None):
        self.f1_api = f1_api
        self.f1cosmos = f1cosmos
        self.season = season or f1_api.season
        from .f1_api import LocalCache
        self.cache = LocalCache()

    # ---------- 数据组装 ----------

    def _season_tables(self):
        """赛季数据集 -> (每轮车队最快pace表, 车手→车队映射)"""
        from .prediction_model import RacePredictionModel
        from .circuits_manager import CircuitsManager
        from .f1cosmos_api import canonical_team_key

        pm = RacePredictionModel(self.f1_api, self.f1cosmos)
        ds = pm.build_season_dataset(self.season)
        cm = CircuitsManager()

        rounds: List[Dict[str, Any]] = []
        drv_team: Dict[str, str] = {}
        for rec in ds:
            meta = cm.get_circuit(rec["circuit_id"]) or {}
            downforce = (meta.get("downforce") or "中")
            alt = meta.get("altitude_m") or 0
            tire = (meta.get("tire_stress") or "中")
            team_best: Dict[str, float] = {}
            sunday_pairs: List[Dict[str, Any]] = []
            for e in rec["entries"]:
                tk = canonical_team_key(e.get("team", ""))
                if e.get("driver_id"):
                    drv_team[e["driver_id"]] = tk
                if e.get("quali_pace"):
                    if tk not in team_best or e["quali_pace"] < team_best[tk]:
                        team_best[tk] = e["quali_pace"]
                if e.get("finish_pos", 99) < 90 and e.get("quali_pos", 99) < 90:
                    sunday_pairs.append({"team_key": tk,
                                         "delta": e["finish_pos"] - e["quali_pos"]})
            rounds.append({
                "round": rec["round"], "circuit_id": rec["circuit_id"],
                "downforce": downforce, "alt_high": alt >= ALT_HIGH_M,
                "tire": tire, "team_best": team_best, "sunday": sunday_pairs,
            })
        return rounds, drv_team

    # ---------- 主计算 ----------

    def compute(self) -> Dict[str, Dict[str, Any]]:
        """计算全部车队实力画像（带缓存）"""
        cache_key = f"team_strengths_{self.season}"
        ttl = 24 if self.season >= self.f1_api.season else None
        cached = self.cache.load(cache_key)
        if cached is not None:
            return cached

        rounds, drv_team = self._season_tables()
        if not rounds:
            return {}
        teams = sorted({tk for r in rounds for tk in r["team_best"]})
        last_rounds = sorted(r["round"] for r in rounds)[-RECENT_WINDOW:]

        # 轮胎衰减：近 RECENT_WINDOW 场 stint 衰减斜率均值（Jolpica laps+pitstops 推导，
        # 正=衰减快；比 sunday_delta 更直接的轮胎管理证据）
        tire_deg_by_team: Dict[str, List[float]] = {}
        try:
            from .stint_analysis import StintAnalyzer
            from .prediction_model import _norm_surname
            # stint 键为 OpenF1 last_name（verstappen）而 drv_team 键为 Ergast driver_id
            # （max_verstappen）——预建归一化姓氏索引做跨源匹配
            surname_team = {}
            for k, v in drv_team.items():
                surname_team.setdefault(_norm_surname(k.split("_")[-1]), v)
            sa = StintAnalyzer(self.f1_api, cache=self.cache)
            for rd in last_rounds:
                st = sa.get_race_stints(self.season, rd)
                for did, d in (st.get("drivers") or {}).items():
                    deg = d.get("avg_deg_ms")
                    if deg is None:
                        continue
                    tk2 = drv_team.get(did) or surname_team.get(_norm_surname(did))
                    if tk2:
                        tire_deg_by_team.setdefault(tk2, []).append(deg)
        except Exception as e:
            logger.warning(f"[TeamStrength] 轮胎衰减数据获取失败（跳过该维度）: {e}")

        # 升级件轮次映射 {(round, team_key): count}（FIA 2024+）
        upgrades = set()
        if self.f1cosmos and hasattr(self.f1cosmos, "get_season_events_upgrades"):
            try:
                from .f1cosmos_api import canonical_team_key
                for u in self.f1cosmos.get_season_events_upgrades(self.season) or []:
                    if u.get("round"):
                        upgrades.add((int(u["round"]), canonical_team_key(u.get("team", ""))))
            except Exception as e:
                logger.warning(f"[TeamStrength] 升级件数据获取失败: {e}")

        out: Dict[str, Dict[str, Any]] = {}
        for tk in teams:
            paces = [(r["round"], r["team_best"][tk], r) for r in rounds if tk in r["team_best"]]
            if not paces:
                continue
            season_avg = sum(p for _, p, _ in paces) / len(paces)
            recent = [p for rd, p, _ in paces if rd in last_rounds]
            recent_avg = sum(recent) / len(recent) if recent else season_avg
            trend = recent_avg - season_avg  # 负值=变快

            def split_score(pred):
                in_type = [p for _, p, r in paces if pred(r)]
                out_type = [p for _, p, r in paces if not pred(r)]
                if not in_type or not out_type:
                    return None
                return round(sum(in_type) / len(in_type) - sum(out_type) / len(out_type), 2)

            by_downforce = {}
            for lv in ("高", "中高", "中", "中低", "低"):
                v = split_score(lambda r, lv=lv: r["downforce"] == lv)
                if v is not None:
                    by_downforce[lv] = v
            by_tire = {}
            for lv in ("高", "中", "低"):
                v = split_score(lambda r, lv=lv: r["tire"] == lv)
                if v is not None:
                    by_tire[lv] = v
            alt_score = split_score(lambda r: r["alt_high"])

            # 正赛兑现率：队内车手 正赛名次-排位名次 均值（负=周日变强）
            deltas = [p["delta"] for r in rounds for p in r["sunday"] if p["team_key"] == tk]
            sunday_delta = round(sum(deltas) / len(deltas), 2) if deltas else None

            # 升级效果：升级轮前后各2场 队pace 变化
            upgrade_effects = []
            for (rd, utk) in sorted(upgrades):
                if utk != tk:
                    continue
                before = [p for r2, p, _ in paces if rd - 2 <= r2 < rd]
                after = [p for r2, p, _ in paces if rd < r2 <= rd + 2]
                if before and after:
                    upgrade_effects.append({
                        "round": rd,
                        "pace_before": round(sum(before) / len(before), 2),
                        "pace_after": round(sum(after) / len(after), 2),
                        "delta": round(sum(after) / len(after) - sum(before) / len(before), 2),
                    })

            # 轮胎衰减（近4场 stint 衰减斜率均值，ms/圈，正=衰减快）
            deg_list = tire_deg_by_team.get(tk) or []
            tire_deg = round(sum(deg_list) / len(deg_list), 1) if deg_list else None

            out[tk] = {
                "team": tk,
                "races": len(paces),
                "pace_season_avg": round(season_avg, 2),
                "pace_recent_avg": round(recent_avg, 2),
                "trend": round(trend, 2),  # 负=近期变快
                "by_downforce": by_downforce,   # 负=该下压力档位相对更强
                "by_tire_stress": by_tire,      # 负=该轮胎负荷档相对更强
                "by_altitude_high": alt_score,  # 负=高海拔相对更强
                "sunday_delta": sunday_delta,   # 负=正赛强于排位（正赛节奏/轮胎管理代理）
                "tire_deg_ms": tire_deg,        # 正=衰减快（近4场 stint 斜率实测）
                "upgrade_effects": upgrade_effects,
            }
            out[tk]["_legend"] = ("pace_index以100为全场中位，越小越快；各项差值为负=相对更强；"
                                  "sunday_delta为正赛名次-排位名次均值，负=周日表现更好；"
                                  "tire_deg_ms为轮胎衰减斜率实测(ms/圈)，正=衰减快")

        if out:
            self.cache.save(cache_key, out, ttl_hours=ttl)
        return out

    def strengths_for_circuit(self, circuit_id: str) -> Dict[str, Dict[str, Any]]:
        """针对某分站赛道类型，给出各车队对应维度的强弱摘要（预测/分析注入用）"""
        from .circuits_manager import CircuitsManager
        meta = CircuitsManager().get_circuit(circuit_id) or {}
        df = meta.get("downforce") or "中"
        tire = meta.get("tire_stress") or "中"
        alt_high = (meta.get("altitude_m") or 0) >= ALT_HIGH_M
        all_s = self.compute()
        out = {}
        for tk, s in all_s.items():
            dims = {}
            v = s["by_downforce"].get(df)
            if v is not None:
                dims["downforce_match"] = v
            v = s["by_tire_stress"].get(tire)
            if v is not None:
                dims["tire_stress_match"] = v
            if alt_high and s.get("by_altitude_high") is not None:
                dims["altitude_high"] = s["by_altitude_high"]
            dims["trend"] = s["trend"]
            dims["sunday_delta"] = s["sunday_delta"]
            if s.get("tire_deg_ms") is not None:
                dims["tire_deg_ms"] = s["tire_deg_ms"]
            out[tk] = dims
        return out
