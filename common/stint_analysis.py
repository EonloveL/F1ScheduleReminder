"""
F1赛程提醒机器人 - stint 长距离节奏 / 轮胎衰减分析

数据源（2026-09-21 修订，原 Jolpica laps 实现有缺陷——Jolpica /laps 分页口径是"计时条数"
而非圈数，limit=1000 也只回 5 圈，且无轮胎配方/进站圈）：
- 主源 OpenF1（2023+，数据完整）：
    /stints → 每个 stint 的 compound(SOFT/MEDIUM/HARD)+stint_number+lap_start/lap_end+tyre_age
    /laps   → 全量圈速 lap_duration + is_pit_out_lap 标记
    /drivers→ driver_number→姓氏 映射
- 兜底 Jolpica（pre-2023，无轮胎配方，圈速数据可能不完整，仅做尽力分析）

计算口径：
- stint 划分：直接来自 OpenF1 stints 的 lap_start/lap_end（无需再按进站圈猜）
- 有效圈：剔除 out-lap（stint 首圈）、in-lap（stint 末圈）、is_pit_out_lap、>stint中位数×107% 异常圈
- stint 配速：有效圈中位圈速；衰减：有效圈 圈速~圈号 线性拟合斜率（ms/圈，正=衰减）
- 车手汇总：avg_deg_ms、best_stint_median_s、strategy（各 stint 轮胎配方序列）

缓存：data/cache/stints_{season}_{round}.json（已完赛分站永久缓存）
"""

import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

OPENF1_BASE = "https://api.openf1.org/v1"
JOLPICA_BASE = "https://api.jolpi.ca/ergast/f1"
OUTLIER_RATIO = 1.07
MIN_LAPS_FOR_DEG = 4

# 轮胎配方中文（QQ 卡片/问答用）
COMPOUND_CN = {"SOFT": "红(软)", "MEDIUM": "黄(中)", "HARD": "白(硬)", "INTERMEDIATE": "绿(半雨)", "WET": "蓝(雨)"}


def _parse_laptime(t: str) -> Optional[float]:
    """'1:32.123' / '92.123' → 秒；无法解析返回 None"""
    if not t:
        return None
    s = str(t).strip()
    try:
        if ":" in s:
            m, sec = s.split(":", 1)
            return int(m) * 60 + float(sec)
        return float(s)
    except (ValueError, TypeError):
        return None


class StintAnalyzer:
    """stint 节奏/轮胎衰减分析器（OpenF1 主源 + Jolpica 历史兜底）"""

    def __init__(self, f1_api=None, cache=None):
        from .f1_api import LocalCache
        self.f1_api = f1_api
        self.cache = cache or LocalCache()
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})

    # ---------- OpenF1 数据 ----------

    def _openf1_session_key(self, season: int, round_num: int) -> Optional[int]:
        """赛季+轮次 → OpenF1 正赛 session_key（按赛程日期匹配）"""
        race_date = None
        if self.f1_api:
            try:
                schedule = self.f1_api.get_schedule_for_year(season) or []
                race = next((r for r in schedule
                             if int(r.get("round", 0)) == round_num), None)
                race_date = race.get("date") if race else None
            except Exception:
                pass
        params = {"session_type": "Race"}
        if race_date:
            params["date_start"] = race_date
        else:
            params["year"] = season
        try:
            resp = self.session.get(f"{OPENF1_BASE}/sessions", params=params, timeout=20)
            data = resp.json()
            if not data:
                return None
            # 多个候选时按日期排序取最近（date_start 过滤可能跨年撞）
            data.sort(key=lambda x: x.get("date_start", ""))
            return data[0]["session_key"]
        except Exception as e:
            logger.warning(f"[Stint] OpenF1 定位正赛失败: {e}")
            return None

    def _openf1_get(self, endpoint: str, params: Dict) -> List[Dict]:
        try:
            resp = self.session.get(f"{OPENF1_BASE}/{endpoint}", params=params, timeout=30)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            logger.warning(f"[Stint] OpenF1 {endpoint} 失败: {e}")
            return []

    # ---------- 主计算 ----------

    def get_race_stints(self, season: int, round_num: int,
                        use_cache: bool = True) -> Dict[str, Any]:
        # 缓存键带 v2 版本号：旧版（Jolpica 5 圈残缺数据）用 stints_{season}_{round} 永久缓存，
        # 新版换 OpenF1 主源后若沿用旧键会读到脏缓存（2026-09-21 实战：西班牙站只回 5 圈）
        cache_key = f"stints_v2_{season}_{round_num}"
        if use_cache:
            cached = self.cache.load(cache_key)
            if cached is not None:
                return cached

        if season >= 2023:
            result = self._from_openf1(season, round_num)
        else:
            result = self._from_jolpica(season, round_num)

        if use_cache and result and "error" not in result and result.get("drivers"):
            self.cache.save(cache_key, result, ttl_hours=None)
        return result

    def _from_openf1(self, season: int, round_num: int) -> Dict[str, Any]:
        sk = self._openf1_session_key(season, round_num)
        if not sk:
            return {"error": f"{season} R{round_num} 无 OpenF1 数据（2023 前或分站未赛）"}

        stints = self._openf1_get("stints", {"session_key": sk})
        laps = self._openf1_get("laps", {"session_key": sk})
        drivers = self._openf1_get("drivers", {"session_key": sk})
        if not stints or not laps:
            return {"error": f"{season} R{round_num} OpenF1 无 stint/圈速数据"}

        # number → 姓氏（与打包头像/工具键一致，小写）
        num_to_surname: Dict[int, str] = {}
        for d in drivers:
            n = d.get("driver_number")
            last = (d.get("last_name") or "").strip().lower()
            if n and last:
                num_to_surname[n] = last

        # 圈速：{driver_number: {lap_number: (duration_s, is_pit_out_lap)}}
        drv_laps: Dict[int, Dict[int, tuple]] = defaultdict(dict)
        for l in laps:
            n = l.get("driver_number")
            lp = l.get("lap_number")
            if not n or lp is None:
                continue
            drv_laps[n][lp] = (l.get("lap_duration"), bool(l.get("is_pit_out_lap")))

        # 按车手组织 stints
        stints_by_drv: Dict[int, List[Dict]] = defaultdict(list)
        for s in stints:
            stints_by_drv[s.get("driver_number")].append(s)

        out: Dict[str, Any] = {"season": season, "round": round_num,
                               "source": "OpenF1", "drivers": {}}
        for num, s_list in stints_by_drv.items():
            surname = num_to_surname.get(num)
            if not surname:
                continue
            s_list.sort(key=lambda x: int(x.get("stint_number", 0)))
            laps_map = drv_laps.get(num, {})
            stint_objs = []
            for s in s_list:
                ls, le = int(s.get("lap_start", 0)), int(s.get("lap_end", 0))
                compound = s.get("compound") or "UNKNOWN"
                # 有效圈：排除 out/in 圈 + 非pit出站标记 + 无圈速 + >107%中位
                raw = []
                for lp in range(ls, le + 1):
                    rec = laps_map.get(lp)
                    if not rec or rec[0] is None:
                        continue
                    raw.append((lp, rec[0], rec[1]))
                med = sorted(v for _, v, _ in raw)[len(raw) // 2] if raw else None
                clean = [(lp, v) for lp, v, pitout in raw
                         if lp != ls and lp != le and not pitout
                         and (med is None or v <= med * OUTLIER_RATIO)]
                obj: Dict[str, Any] = {
                    "stint": int(s.get("stint_number", 0)),
                    "lap_start": ls, "lap_end": le,
                    "compound": compound,
                    "compound_cn": COMPOUND_CN.get(compound, compound),
                    "laps": len(raw), "valid_laps": len(clean),
                    "tyre_age_at_start": s.get("tyre_age_at_start"),
                }
                if len(clean) >= 3:
                    cs = sorted(v for _, v in clean)
                    obj["median_s"] = round(cs[len(cs) // 2], 3)
                if len(clean) >= MIN_LAPS_FOR_DEG:
                    import numpy as np
                    xs = [lp for lp, _ in clean]
                    ys = [v for _, v in clean]
                    obj["deg_ms_per_lap"] = round(float(np.polyfit(xs, ys, 1)[0]) * 1000, 1)
                stint_objs.append(obj)

            degs = [s["deg_ms_per_lap"] for s in stint_objs
                    if s.get("deg_ms_per_lap") is not None]
            meds = [s["median_s"] for s in stint_objs if s.get("median_s")]
            strategy = " → ".join(s["compound_cn"] for s in stint_objs)
            out["drivers"][surname] = {
                "stints": stint_objs,
                "strategy": strategy,
                "avg_deg_ms": round(sum(degs) / len(degs), 1) if degs else None,
                "best_stint_median_s": min(meds) if meds else None,
            }
        return out

    def _from_jolpica(self, season: int, round_num: int) -> Dict[str, Any]:
        """历史兜底（pre-2023）：Jolpica laps+pitstops，无轮胎配方。
        注意：Jolpica /laps 分页口径为计时条数，单请求可能只回前几圈，圈速数据可能不完整"""
        laps = self._jolpica_fetch(f"{season}/{round_num}/laps")
        stops = self._jolpica_fetch(f"{season}/{round_num}/pitstops")
        if not laps:
            return {"error": f"{season} R{round_num} 无圈速数据（且 2023 前无 OpenF1）"}

        drv_laps: Dict[str, Dict[int, float]] = defaultdict(dict)
        for lap in laps:
            try:
                n = int(lap.get("number", 0))
            except (ValueError, TypeError):
                continue
            for t in lap.get("Timings", []) or []:
                sec = _parse_laptime(t.get("time"))
                if sec and t.get("driverId"):
                    drv_laps[t["driverId"]][n] = sec

        drv_pits: Dict[str, set] = defaultdict(set)
        for s in stops:
            try:
                drv_pits[s["driverId"]].add(int(s["lap"]))
            except (KeyError, ValueError, TypeError):
                continue

        result: Dict[str, Any] = {"season": season, "round": round_num,
                                  "source": "Jolpica(无轮胎配方)", "drivers": {}}
        for drv, lapmap in drv_laps.items():
            laps_sorted = sorted(lapmap.items())
            pit_laps = drv_pits.get(drv, set())
            stints: List[List] = []
            cur: List = []
            for n, sec in laps_sorted:
                cur.append((n, sec))
                if n in pit_laps:
                    stints.append(cur)
                    cur = []
            if cur:
                stints.append(cur)
            stint_objs = []
            for idx, stint in enumerate(stints, 1):
                times = sorted(sec for _, sec in stint)
                med = times[len(times) // 2]
                clean = [(n, sec) for n, sec in stint
                         if n not in pit_laps and (n - 1) not in pit_laps
                         and sec <= med * OUTLIER_RATIO]
                obj = {"stint": idx, "lap_start": stint[0][0], "lap_end": stint[-1][0],
                       "compound": None, "compound_cn": "?",
                       "laps": len(stint), "valid_laps": len(clean)}
                if len(clean) >= 3:
                    cs = sorted(sec for _, sec in clean)
                    obj["median_s"] = round(cs[len(cs) // 2], 3)
                if len(clean) >= MIN_LAPS_FOR_DEG:
                    import numpy as np
                    xs = [n for n, _ in clean]
                    ys = [sec for _, sec in clean]
                    obj["deg_ms_per_lap"] = round(float(np.polyfit(xs, ys, 1)[0]) * 1000, 1)
                stint_objs.append(obj)
            degs = [s["deg_ms_per_lap"] for s in stint_objs
                    if s.get("deg_ms_per_lap") is not None]
            meds = [s["median_s"] for s in stint_objs if s.get("median_s")]
            result["drivers"][drv] = {
                "stints": stint_objs,
                "strategy": "未知(无配方)",
                "avg_deg_ms": round(sum(degs) / len(degs), 1) if degs else None,
                "best_stint_median_s": min(meds) if meds else None,
            }
        return result

    def _jolpica_fetch(self, path: str) -> List[Dict]:
        url = f"{JOLPICA_BASE}/{path}.json?limit=1000"
        try:
            resp = self.session.get(url, timeout=30)
            resp.raise_for_status()
            races = resp.json().get("MRData", {}).get("RaceTable", {}).get("Races", [])
            if not races:
                return []
            return races[0].get("Laps") or races[0].get("PitStops") or []
        except Exception as e:
            logger.warning(f"[Stint] Jolpica 拉取失败 {path}: {e}")
            return []
