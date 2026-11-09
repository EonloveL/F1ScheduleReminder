"""
F1赛程提醒机器人 - 统计图表工厂（可视化统计）

pace 分布箱线图（仿 RaceFans/@iamuhammedkaya 风格）：
- 上半：每位车手有效圈速箱线图，散点按轮胎配方着色（红软/黄中/白硬/绿半雨/蓝全雨）
- 下半：各车手与全场最快的中位圈速差条形图（升序）
- 深色底（#1a1d26，与评分页/卡片同色系）

数据源：OpenF1 laps+stints（复用 stint_analysis 的 session key 与有效圈口径：
排除进出站圈、stint 首尾圈、>107% 中位异常圈）。已完赛分站 PNG 落盘即永久复用
（data/exports/pace_chart_{season}_R{round}.png，经 /exports/ 路由公网可访问）。

出口：赛后推送（scheduler._do_post_race_push）、/pace 指令、get_stint_analysis
工具返回 chart_url（AI 问询时 L2 嵌入图片）。
"""
import logging
import os
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
EXPORTS_DIR = os.path.join(DATA_DIR, "exports")

# 轮胎配方着色（与国际通行一致）
COMPOUND_COLORS = {
    "SOFT": "#ff3333", "MEDIUM": "#ffd700", "HARD": "#e8e8e8",
    "INTERMEDIATE": "#39b54a", "WET": "#00a3e0", "UNKNOWN": "#888888",
}
COMPOUND_CN = {"SOFT": "红软", "MEDIUM": "黄中", "HARD": "白硬",
               "INTERMEDIATE": "半雨", "WET": "全雨", "UNKNOWN": "未知"}

OUTLIER_RATIO = 1.07  # 与 stint_analysis 口径一致
MIN_VALID_LAPS = 5    # 有效圈过少的车手不入图

# 图表文件名版本：内容修复/口径变更时递增，破服务器磁盘缓存（永久复用逻辑）与
# QQ 图片代理缓存——2026-10-09 实证：date_end bug 生成的空策略面板 PNG 被永久复用，
# 修代码也不再生效，必须换文件名
CHART_VER = "v2"


def _pick_cjk_font():
    import matplotlib
    for fname in ("Noto Sans CJK SC", "Noto Sans CJK", "Microsoft YaHei", "SimHei"):
        try:
            from matplotlib import font_manager
            if font_manager.findfont(fname, fallback_to_default=False):
                matplotlib.rcParams["font.sans-serif"] = [fname, "DejaVu Sans"]
                matplotlib.rcParams["axes.unicode_minus"] = False
                return
        except Exception:
            continue
    matplotlib.rcParams["axes.unicode_minus"] = False


def _pace_laps_jolpica(analyzer, season: int, round_num: int, why: str
                       ) -> Tuple[Optional[Dict[str, List[Tuple[float, str]]]], str]:
    """pace 圈速的 Jolpica 兜底：全部 stint 圈速、剔进站圈±1、>107% 中位异常圈，
    无配方（UNKNOWN）。圈速端点分页重，但这是 OpenF1 不可达时的唯一 pace 来源"""
    laps = analyzer._jolpica_fetch(f"{season}/{round_num}/laps")
    if not laps:
        return None, f"{season} R{round_num} OpenF1 不可用（{why}）且 Jolpica 无圈速"
    stops = analyzer._jolpica_fetch(f"{season}/{round_num}/pitstops")
    drv_pits: Dict[str, set] = defaultdict(set)
    for s in stops:
        try:
            drv_pits[s["driverId"]].add(int(s["lap"]))
        except (KeyError, ValueError, TypeError):
            continue
    per_drv: Dict[str, Dict[int, float]] = defaultdict(dict)
    for lap in laps:
        try:
            n = int(lap.get("number", 0))
        except (ValueError, TypeError):
            continue
        for t in lap.get("Timings", []) or []:
            sec = _parse_laptime_fb(t.get("time"))
            if sec and t.get("driverId"):
                per_drv[t["driverId"]][n] = sec
    result: Dict[str, List[Tuple[float, str]]] = {}
    # livetiming 配方映射（surname+lap → compound），让 Jolpica 兜底的 pace 图也能按配方着色
    lt_compounds: Dict[str, List[Dict]] = {}
    try:
        from ..livetiming_client import LiveTimingClient
        _raw = LiveTimingClient(f1_api=analyzer.f1_api).get_stints(season, round_num) or {}
        # 键统一重音归一化（pérez→perez），与 Jolpica driverId 派生姓氏对齐
        import unicodedata as _ud
        lt_compounds = {"".join(c for c in _ud.normalize("NFKD", k)
                                if not _ud.combining(c)).lower(): v
                        for k, v in _raw.items()}
    except Exception:
        pass

    def _compound_of(surname: str, lap: int) -> str:
        # 重音归一化（livetiming LastName 可能带重音 "pérez"，Jolpica driverId 派生 "perez"）
        import unicodedata as _ud
        key = "".join(c for c in _ud.normalize("NFKD", surname)
                      if not _ud.combining(c)).lower()
        for s in lt_compounds.get(key, []):
            if s["lap_start"] <= lap <= s["lap_end"]:
                return s["compound"]
        return "UNKNOWN"

    for drv, lapmap in per_drv.items():
        surname = drv.replace("_", " ").split()[-1].lower()
        pits = drv_pits.get(drv, set())
        vals = [(n, s) for n, s in sorted(lapmap.items())
                if n not in pits and (n - 1) not in pits]
        if not vals:
            continue
        med = sorted(s for _, s in vals)[len(vals) // 2]
        laps_of = [(n, s) for n, s in vals if s <= med * OUTLIER_RATIO]
        if len(laps_of) >= MIN_VALID_LAPS:
            # 与主源一致的展示层过滤：剔 >110% 个人全场中位极端慢圈（SC/事故圈压扁箱线）
            overall_med = sorted(s for _, s in laps_of)[len(laps_of) // 2]
            laps_of = [(n, s) for n, s in laps_of if s <= overall_med * 1.10]
        if len(laps_of) >= MIN_VALID_LAPS:
            result[surname] = [(v, _compound_of(surname, n)) for n, v in laps_of]
    if not result:
        return None, f"{season} R{round_num} Jolpica 兜底无有效圈（OpenF1: {why}）"
    logger.info(f"[chart] pace 圈速走 Jolpica 兜底"
                f"{'（livetiming 配方叠加）' if lt_compounds else '（无配方着色）'}: "
                f"{season} R{round_num}")
    return result, ""


def _parse_laptime_fb(t) -> Optional[float]:
    """chart_factory 本地 laptime 解析（stint_analysis._parse_laptime 的副本口径）"""
    if not t:
        return None
    try:
        s = str(t)
        if ":" in s:
            m, sec = s.split(":")
            return int(m) * 60 + float(sec)
        return float(s)
    except (ValueError, TypeError):
        return None


def _fetch_pace_laps(analyzer, season: int, round_num: int
                     ) -> Tuple[Optional[Dict[str, List[Tuple[float, str]]]], str]:
    """取每位车手有效圈 [(圈速s, 配方)]；返回 ({surname: laps}, error_msg)
    OpenF1 主源；不可达时 Jolpica laps 兜底（无配方，全部 UNKNOWN 灰色）——
    2026-10-09 实证阿里云服务器访问 OpenF1 返回 401"""
    sk = analyzer._openf1_session_key(season, round_num)
    if not sk:
        return _pace_laps_jolpica(analyzer, season, round_num, "session 定位失败")
    stints = analyzer._openf1_get("stints", {"session_key": sk})
    laps = analyzer._openf1_get("laps", {"session_key": sk})
    drivers = analyzer._openf1_get("drivers", {"session_key": sk})
    if not stints or not laps:
        return _pace_laps_jolpica(analyzer, season, round_num, "OpenF1 stint/圈速为空")

    num_to_surname = {}
    for d in drivers:
        n, last = d.get("driver_number"), (d.get("last_name") or "").strip().lower()
        if n and last:
            num_to_surname[n] = last

    drv_laps: Dict[int, Dict[int, tuple]] = defaultdict(dict)
    for l in laps:
        n, lp = l.get("driver_number"), l.get("lap_number")
        if n and lp is not None:
            drv_laps[n][lp] = (l.get("lap_duration"), bool(l.get("is_pit_out_lap")))

    stints_by_drv: Dict[int, List[Dict]] = defaultdict(list)
    for s in stints:
        stints_by_drv[s.get("driver_number")].append(s)

    result: Dict[str, List[Tuple[float, str]]] = {}
    for num, s_list in stints_by_drv.items():
        surname = num_to_surname.get(num)
        if not surname:
            continue
        laps_map = drv_laps.get(num, {})
        valid: List[Tuple[float, str]] = []
        for s in s_list:
            ls = _safe_int(s.get("lap_start"), 0) or 0
            le = _safe_int(s.get("lap_end"), 0) or 0
            if le < ls:
                continue  # 脏 stint（null 边界）跳过
            compound = (s.get("compound") or "UNKNOWN").upper()
            raw = [(lp, laps_map[lp][0], laps_map[lp][1])
                   for lp in range(ls, le + 1)
                   if lp in laps_map and laps_map[lp][0] is not None]
            med = sorted(v for _, v, _ in raw)[len(raw) // 2] if raw else None
            valid += [(v, compound) for lp, v, pitout in raw
                      if lp != ls and lp != le and not pitout
                      and (med is None or v <= med * OUTLIER_RATIO)]
        if len(valid) >= MIN_VALID_LAPS:
            # 展示口径二次过滤：剔除 >110% 个人全场中位的极端慢圈（安全车/事故圈套圈），
            # 否则纵轴被拉长、箱线图被压扁（2026 雪邦起步事故实证）
            overall_med = sorted(v for v, _ in valid)[len(valid) // 2]
            valid = [(v, c) for v, c in valid if v <= overall_med * 1.10]
            result[surname] = valid
    result = {k: v for k, v in result.items() if len(v) >= MIN_VALID_LAPS}
    if not result:
        return None, f"{season} R{round_num} 无足够有效圈数据"
    return result, ""


def _setup_dark_fig(figsize, suptitle=""):
    import matplotlib
    matplotlib.use("Agg")
    from matplotlib import pyplot as plt
    _pick_cjk_font()
    fig = plt.figure(figsize=figsize, facecolor="#1a1d26")
    if suptitle:
        fig.suptitle(suptitle, color="#f0f0f0", fontsize=17, fontweight="bold",
                     x=0.05, ha="left")
    return fig, plt


def _boxplot_compat(ax, data, horizontal: bool = False, **kw):
    """matplotlib 3.10+ 用 orientation，旧版用 vert——双写兼容（OSS 修复回灌 2026-10-30：
    旧版本 matplotlib 传 orientation 直接 TypeError，图表指令整体失败）"""
    try:
        return ax.boxplot(data, orientation=("horizontal" if horizontal else "vertical"), **kw)
    except TypeError:
        return ax.boxplot(data, vert=(not horizontal), **kw)


def _style_ax(ax):
    ax.set_facecolor("#1a1d26")
    ax.tick_params(colors="#aaaaaa")
    for spine in ax.spines.values():
        spine.set_color("#444")
    ax.grid(axis="y", color="#333842", linewidth=0.6, alpha=0.7)


def _openf1_session_key(analyzer, season: int, round_num: int,
                        session_type: str = "Race") -> Optional[int]:
    """按赛程日期定位 OpenF1 环节（Race=正赛当天起；Qualifying=正赛前3天起）。
    注意：OpenF1 sessions 的 date_end 参数会滤掉目标环节（2026-10-09 实证 R16 查无结果），
    上界必须客户端过滤；赛程查不到日期时宁缺勿错（按 year 兜底会锁错分站并永久缓存）"""
    race_date = None
    if analyzer.f1_api:
        try:
            schedule = analyzer.f1_api.get_schedule_for_year(season) or []
            race = next((r for r in schedule
                         if int(r.get("round", 0)) == round_num), None)
            race_date = race.get("date") if race else None
        except Exception:
            pass
    if not race_date:
        return None
    from datetime import datetime as _dt, timedelta as _td
    try:
        d_race = _dt.strptime(race_date, "%Y-%m-%d")
    except Exception:
        return None
    params = {"session_type": session_type}
    if session_type == "Race":
        params["date_start"] = race_date
    else:
        params["date_start"] = (d_race - _td(days=3)).strftime("%Y-%m-%d")
    try:
        from ..stint_analysis import OPENF1_BASE
        resp = analyzer.session.get(f"{OPENF1_BASE}/sessions", params=params,
                                    headers=analyzer._of1_headers() or None,
                                    timeout=20)
        data = resp.json()
        if not isinstance(data, list) or not data:
            return None
        # 客户端上界过滤（OpenF1 date_end 参数不可靠）：不晚于正赛日+1天
        upper = (d_race + _td(days=1)).strftime("%Y-%m-%d")
        data = [s for s in data
                if str(s.get("date_start", ""))[:10] <= upper]
        if not data:
            return None
        # 窗口内可能有冲刺排位等其他环节，按 session_name 精确匹配
        if session_type != "Race":
            exact = [s for s in data if s.get("session_name") == session_type]
            if exact:
                data = exact
        data.sort(key=lambda x: x.get("date_start", ""))
        return data[-1]["session_key"] if session_type != "Race" else data[0]["session_key"]
    except Exception:
        return None


def _safe_int(v, default=None):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _race_entries(f1_api, season: int, round_num: int) -> List[Dict[str, Any]]:
    res = f1_api.get_session_results(round_num, "race", retries=1, season=season) or {}
    return res.get("entries") or []


def _strategy_stints(analyzer, season: int, round_num: int,
                     entries: List[Dict[str, Any]]) -> Optional[Dict[str, List[Dict]]]:
    """策略时间线数据：OpenF1 stints 主源（含配方）→ F1 livetiming 静态归档兜底
    （TimingAppData 含配方！OpenF1 401/赛中锁定时的配方来源，2026-10-09 实证雪邦可用）
    → Jolpica pitstops 兜底（无配方，灰色块）。
    返回 {surname: [{"lap_start","lap_end","compound","stint"}]} 或 None"""
    sk = _openf1_session_key(analyzer, season, round_num, "Race")
    stints = analyzer._openf1_get("stints", {"session_key": sk}) if sk else []
    drivers = analyzer._openf1_get("drivers", {"session_key": sk}) if sk else []
    if stints and drivers:
        num_to_surname = {d.get("driver_number"): (d.get("last_name") or "").strip().lower()
                          for d in drivers if d.get("driver_number")}
        out: Dict[str, List[Dict]] = defaultdict(list)
        for s in stints:
            surname = num_to_surname.get(s.get("driver_number"))
            ls = _safe_int(s.get("lap_start"), 0) or 0
            le = _safe_int(s.get("lap_end"), 0) or 0
            if surname and le >= ls:
                out[surname].append({"lap_start": ls, "lap_end": le,
                                     "compound": (s.get("compound") or "UNKNOWN").upper(),
                                     "stint": _safe_int(s.get("stint_number"), 0) or 0})
        if out:
            return dict(out)
    # livetiming 兜底（含配方）：OpenF1 401/不可达时的配方数据源
    try:
        from ..livetiming_client import LiveTimingClient
        lt_stints = LiveTimingClient(f1_api=analyzer.f1_api).get_stints(season, round_num)
        if lt_stints:
            logger.info(f"[chart] 策略走 livetiming 归档兜底（含配方）: {season} R{round_num}")
            return lt_stints
    except Exception as e:
        logger.warning(f"[chart] livetiming 兜底失败: {e}")
    # Jolpica 兜底（无配方）：只取 pitstops（轻量单页）——策略时间线只需要进站圈
    # 画 stint 边界，不需要圈速（laps 端点分页重且慢，2026-10-09 实证超时）
    try:
        stops = analyzer._jolpica_fetch(f"{season}/{round_num}/pitstops")
        if stops:
            id_to_surname = {(e.get("driver_id") or ""): (e.get("driver_name") or "").split()[-1].lower()
                             for e in entries}
            # 总圈数：成绩 entries 的 laps 字段（最可靠），缺则赛道档案
            total_laps = max((_safe_int(e.get("laps"), 0) or 0 for e in entries),
                             default=0)
            if not total_laps and analyzer.f1_api:
                try:
                    sched = analyzer.f1_api.get_schedule_for_year(season) or []
                    race = next((r for r in sched
                                 if int(r.get("round", 0)) == round_num), None)
                    cid = (race or {}).get("Circuit", {}).get("circuitId", "")
                    from ..circuits_manager import CircuitsManager
                    total_laps = int((CircuitsManager().get_circuit(cid) or {})
                                     .get("laps") or 0)
                except Exception:
                    pass
            if not total_laps:
                return None
            pits_by_drv: Dict[str, List[int]] = defaultdict(list)
            for s in stops:
                did = s.get("driverId") or ""
                lap = _safe_int(s.get("lap"), 0)
                if did and lap:
                    pits_by_drv[did].append(lap)
            out = {}
            for did, pits in pits_by_drv.items():
                surname = id_to_surname.get(did)
                if not surname:
                    continue
                bounds = [1] + sorted(set(pits)) + [total_laps]
                # 进站圈 = 该 stint 结束圈
                out[surname] = [{"lap_start": bounds[i], "lap_end": bounds[i + 1],
                                 "compound": "UNKNOWN", "stint": i + 1}
                                for i in range(len(bounds) - 1)]
            if out:
                logger.info(f"[chart] 策略时间线走 Jolpica pitstops 兜底（无配方）: "
                            f"{season} R{round_num}")
                return out
    except Exception as e:
        logger.warning(f"[chart] 策略 Jolpica 兜底失败: {e}")
    return None
