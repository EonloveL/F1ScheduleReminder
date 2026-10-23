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

import matplotlib
matplotlib.use("Agg")  # 进程级一次设定：pyplot 全局状态非线程安全，禁止在渲染函数内重复 use()

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
# QQ 图片代理缓存——实证：数据处理 bug 生成的错误 PNG 被永久复用，修代码也不再生效
CHART_VER = "v2"


def _race_completed_gate(f1_api, season: int, round_num: int, path: str) -> str:
    """正赛未完赛（开始+5h 未到，覆盖拖堂/红旗延长）时把正式文件名改为 _fb{月日}
    日期戳：赛中残缺数据（部分 stint/圈速/赛会消息）不得写永久文件名——
    exists 复用会把半成品永久定格（生产 review 实证风险）。
    排位类图表不适用此闸门（排位数据排位结束后即完整），调用方自行决定"""
    try:
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        sched = (f1_api.get_schedule_for_year(season) or []) if f1_api else []
        race = next((r for r in sched if int(r.get("round", 0)) == round_num), None)
        if race and race.get("date"):
            rt = (race.get("time") or "00:00:00Z").replace("Z", "")
            start = _dt.fromisoformat(f"{race['date']}T{rt}").replace(tzinfo=_tz.utc)
            if _dt.now(_tz.utc) < start + _td(hours=5):
                stamp = _dt.now(_tz.utc).strftime("%m%d")
                return os.path.splitext(path)[0] + f"_fb{stamp}.png"
    except Exception:
        pass
    return path


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
    logger.warning("[chart] 未找到 CJK 字体，图表中文将显示为方块（Docker 镜像需 fonts-noto-cjk）")
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
        from .livetiming_client import LiveTimingClient
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
        # 完整归一化姓氏（de_vries→"de vries" 而非 "vries"）：livetiming 配方键与
        # 完赛名次匹配（_finish_pos 后缀匹配）都以全姓为准
        surname = _norm_name(drv.replace("_", " "))
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
    from matplotlib import pyplot as plt
    _pick_cjk_font()
    fig = plt.figure(figsize=figsize, facecolor="#1a1d26")
    if suptitle:
        fig.suptitle(suptitle, color="#f0f0f0", fontsize=17, fontweight="bold",
                     x=0.05, ha="left")
    return fig, plt


def _savefig_atomic(fig, path: str):
    """tmp + os.replace 原子出图：防并发渲染写同一文件、防 /exports 路由读到半截 PNG、
    防崩溃留下损坏文件被永久复用"""
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        fig.savefig(tmp, facecolor=fig.get_facecolor(), bbox_inches="tight")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def _norm_name(s: str) -> str:
    """姓名归一化：小写 + 去重音 + 压缩空白（跨源匹配：entries 全名 vs OpenF1/Ergast 姓氏）"""
    import unicodedata
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", (s or "").lower())
                            if not unicodedata.combining(c)).split())


def _finish_pos(finish_full: List[str], surname: str) -> int:
    """车手姓氏在完赛名次列表中的位置：归一化后全等或后缀匹配（de vries 命中
    nyck de vries，perez 命中 sergio pérez），未命中返回 99"""
    s = _norm_name(surname)
    if not s:
        return 99
    last_tok = s.split()[-1]
    for i, full in enumerate(finish_full):
        if full == s or full.endswith(" " + s) or _norm_name(full).split()[-1] == last_tok:
            return i
    return 99


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
        from .stint_analysis import OPENF1_BASE
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
        from .livetiming_client import LiveTimingClient
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
                    from .circuits_manager import CircuitsManager
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
            # 以成绩名单为全集播种（零进站车手如红旗换胎不进 pitstops 表，不得漏画），
            # 再按进站圈切段
            for e in entries:
                did = e.get("driver_id") or ""
                surname = id_to_surname.get(did)
                if not surname:
                    continue
                bounds = [1] + sorted(set(pits_by_drv.get(did, []))) + [total_laps]
                # 进站圈 = 该 stint 结束圈（in-lap 归旧胎）；下一段从进站圈+1 起（边界圈不重叠画两遍）
                out[surname] = [{"lap_start": (bounds[i] if i == 0 else bounds[i] + 1),
                                 "lap_end": bounds[i + 1],
                                 "compound": "UNKNOWN", "stint": i + 1}
                                for i in range(len(bounds) - 1)
                                if (bounds[i] if i == 0 else bounds[i] + 1) <= bounds[i + 1]]
            if out:
                logger.info(f"[chart] 策略时间线走 Jolpica pitstops 兜底（无配方）: "
                            f"{season} R{round_num}")
                return out
    except Exception as e:
        logger.warning(f"[chart] 策略 Jolpica 兜底失败: {e}")
    return None


def render_strategy_chart(season: int, round_num: int, race_name: str = "",
                          f1_api=None) -> Optional[str]:
    """轮胎策略时间线：横轴圈数、纵轴车手（按完赛名次），stint 色块按配方着色"""
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    path = os.path.join(EXPORTS_DIR, f"strategy_chart_{CHART_VER}_{season}_R{round_num}.png")
    path = _race_completed_gate(f1_api, season, round_num, path)  # 未完赛跑日期戳文件名
    if os.path.exists(path):
        return path
    from .stint_analysis import StintAnalyzer
    analyzer = StintAnalyzer(f1_api=f1_api)
    entries = _race_entries(f1_api, season, round_num) if f1_api else []
    data = _strategy_stints(analyzer, season, round_num, entries)
    if not data:
        return None
    # Jolpica 兜底渲染（无配方全灰）：写日期戳 fb 文件名，OpenF1 恢复后仍可生成正式版
    _is_fb = all(s["compound"] == "UNKNOWN" for lst in data.values() for s in lst)
    if _is_fb:
        from datetime import datetime as _dt
        if "_fb" not in os.path.basename(path):  # 完赛闸门可能已改日期戳，防 _fb_fb 叠加
            path = os.path.splitext(path)[0] + f"_fb{_dt.now().strftime('%m%d')}.png"
        if os.path.exists(path):
            return path

    finish_order = [_norm_name(e.get("driver_name"))
                    for e in sorted(entries, key=lambda x: _safe_int(x.get("position"), 99) or 99)
                    if (_safe_int(e.get("position"), 99) or 99) < 90]
    # 完赛名次排序，缺成绩的排尾（归一化匹配，防重音/多词姓氏错位）
    names = list(data.keys())
    names.sort(key=lambda n: _finish_pos(finish_order, n))

    fig, plt = _setup_dark_fig((14, max(6, len(names) * 0.55 + 2)),
                               f"{season} {race_name} · 轮胎策略时间线" if race_name
                               else f"{season} R{round_num} 轮胎策略")
    ax = fig.add_axes([0.09, 0.08, 0.88, 0.82])
    ax.set_facecolor("#1a1d26")
    max_lap = 1
    for y, name in enumerate(names):
        for s in sorted(data[name], key=lambda x: x["stint"]):
            ls, le = s["lap_start"], s["lap_end"]
            max_lap = max(max_lap, le)
            ax.barh(y, le - ls + 1, left=ls - 1, height=0.62,
                    color=COMPOUND_COLORS.get(s["compound"], "#888888"),
                    edgecolor="#1a1d26", linewidth=0.6)
    ax.set_yticks(range(len(names)))
    ax.set_yticklabels([n.upper()[:3] for n in names],
                       color="#eeeeee", fontsize=10)
    ax.invert_yaxis()  # 冠军在最上
    ax.set_xlabel("圈数", color="#cccccc")
    ax.set_xlim(0, max_lap + 1)
    ax.grid(axis="x", color="#333842", linewidth=0.6, alpha=0.7)
    ax.tick_params(colors="#aaaaaa")
    for spine in ax.spines.values():
        spine.set_color("#444")
    from matplotlib.lines import Line2D
    used = {s["compound"] for n in names for s in data[n]}
    order = [c for c in ("SOFT", "MEDIUM", "HARD", "INTERMEDIATE", "WET", "UNKNOWN")
             if c in used]
    if order:
        ax.legend(handles=[Line2D([0], [0], marker="s", color="none",
                                  markerfacecolor=COMPOUND_COLORS[c], markersize=10,
                                  label=COMPOUND_CN.get(c, c)) for c in order],
                  loc="lower right", frameon=False, fontsize=10, labelcolor="#dddddd",
                  ncols=max(1, len(order)))
    _savefig_atomic(fig, path)
    plt.close(fig)
    logger.info(f"[chart] 策略时间线已生成: {path}")
    return path


def render_positions_chart(season: int, round_num: int, race_name: str = "",
                           f1_api=None) -> Optional[str]:
    """名次变化图：发车格→完赛 名次得/失条形图（绿升红降），按完赛名次排序"""
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    path = os.path.join(EXPORTS_DIR, f"positions_chart_{CHART_VER}_{season}_R{round_num}.png")
    path = _race_completed_gate(f1_api, season, round_num, path)  # 未完赛跑日期戳文件名
    if os.path.exists(path):
        return path
    entries = _race_entries(f1_api, season, round_num) if f1_api else []
    rows = []
    for e in entries:
        pos = _safe_int(e.get("position"), 99)
        grid = _safe_int(e.get("grid"), 0)
        if pos is None or pos >= 90:
            continue
        g = grid if grid and grid < 90 else 22  # 维修区/队尾起步按末位
        rows.append({"name": (e.get("driver_name") or "").split()[-1],
                     "pos": pos, "grid": g, "delta": g - pos})
    if not rows:
        return None
    rows.sort(key=lambda x: x["pos"])

    fig, plt = _setup_dark_fig((13, max(6, len(rows) * 0.5 + 2)),
                               f"{season} {race_name} · 名次变化（发车→完赛）" if race_name
                               else f"{season} R{round_num} 名次变化")
    ax = fig.add_axes([0.12, 0.08, 0.82, 0.82])
    ax.set_facecolor("#1a1d26")
    ys = range(len(rows))
    colors = ["#39b54a" if r["delta"] > 0 else "#ff5252" if r["delta"] < 0 else "#888888"
              for r in rows]
    ax.barh(ys, [r["delta"] for r in rows], color=colors, height=0.62, alpha=0.9)
    for y, r in zip(ys, rows):
        if r["delta"] != 0:
            ax.text(r["delta"] + (0.15 if r["delta"] > 0 else -0.15), y,
                    f"{r['delta']:+d}", va="center",
                    ha="left" if r["delta"] > 0 else "right",
                    color="#dddddd", fontsize=10)
    ax.set_yticks(list(ys))
    ax.set_yticklabels([f"P{r['pos']} {r['name'].upper()[:3]}"
                        for r in rows], color="#eeeeee", fontsize=10)
    ax.invert_yaxis()
    ax.axvline(0, color="#666", linewidth=1)
    ax.set_xlabel("名次变化（+ = 上升）", color="#cccccc")
    ax.grid(axis="x", color="#333842", linewidth=0.6, alpha=0.7)
    ax.tick_params(colors="#aaaaaa")
    for spine in ax.spines.values():
        spine.set_color("#444")
    _savefig_atomic(fig, path)
    plt.close(fig)
    logger.info(f"[chart] 名次变化图已生成: {path}")
    return path


def render_race_report_chart(season: int, round_num: int, race_name: str = "",
                             f1_api=None) -> Optional[str]:
    """赛后三联图：pace 分布箱线 + 轮胎策略时间线 + 名次变化（一张图一次发送）"""
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    path = os.path.join(EXPORTS_DIR, f"race_report_{CHART_VER}_{season}_R{round_num}.png")
    path = _race_completed_gate(f1_api, season, round_num, path)  # 未完赛跑日期戳文件名
    if os.path.exists(path):
        return path

    from .stint_analysis import StintAnalyzer
    analyzer = StintAnalyzer(f1_api=f1_api)
    pace, _ = _fetch_pace_laps(analyzer, season, round_num)
    entries = _race_entries(f1_api, season, round_num) if f1_api else []
    strat = _strategy_stints(analyzer, season, round_num, entries)
    if not pace or not entries:
        return None
    # Jolpica 兜底渲染（pace 或策略任一全灰）：写日期戳 fb 文件名，OpenF1 恢复后仍可出正式版
    _fb_pace = all(c == "UNKNOWN" for laps in pace.values() for _, c in laps)
    _fb_strat = (not strat) or all(s["compound"] == "UNKNOWN"
                                   for lst in strat.values() for s in lst)
    if _fb_pace or _fb_strat:
        from datetime import datetime as _dt
        if "_fb" not in os.path.basename(path):  # 完赛闸门可能已改日期戳，防 _fb_fb 叠加
            path = os.path.splitext(path)[0] + f"_fb{_dt.now().strftime('%m%d')}.png"
        if os.path.exists(path):
            return path

    import numpy as np
    fig, plt = _setup_dark_fig((16, 15))
    title = f"{season} {race_name} · 赛后统计" if race_name else f"{season} R{round_num} 赛后统计"
    fig.suptitle(title, color="#f0f0f0", fontsize=18, fontweight="bold", x=0.05, ha="left")
    gs = fig.add_gridspec(3, 1, height_ratios=[1.2, 1.0, 0.9], hspace=0.35,
                          left=0.07, right=0.97, top=0.93, bottom=0.05)

    # ① pace 箱线
    ax1 = fig.add_subplot(gs[0])
    _style_ax(ax1)
    stats = sorted(((s, sorted(v for v, _ in lp), lp) for s, lp in pace.items()),
                   key=lambda x: x[1][len(x[1]) // 2])
    names = [s for s, _, _ in stats]
    data = [sd for _, sd, _ in stats]
    laps_by = {s: lp for s, _, lp in stats}
    ax1.boxplot(data, vert=True, positions=range(len(names)), widths=0.55,
                patch_artist=True, showfliers=False,
                medianprops=dict(color="#ff8c00", linewidth=2),
                whiskerprops=dict(color="#cccccc", linewidth=1),
                capprops=dict(color="#cccccc"),
                boxprops=dict(facecolor="#2a2e3d", edgecolor="#cccccc", alpha=0.8))
    rng = np.random.default_rng(42)
    for i, surname in enumerate(names):
        for v, comp in laps_by[surname]:
            ax1.scatter(i + rng.uniform(-0.18, 0.18), v, s=5,
                        color=COMPOUND_COLORS.get(comp, "#888888"), alpha=0.7,
                        linewidths=0, zorder=3)
    ax1.set_xticks(range(len(names)))
    ax1.set_xticklabels([n.upper()[:3] for n in names], color="#eeeeee", fontsize=9)
    ax1.invert_yaxis()
    ax1.set_title("Pace 分布（按配方着色）", color="#dddddd", fontsize=13, loc="left")
    ax1.grid(axis="y", color="#333842", linewidth=0.6, alpha=0.7)

    # ② 策略时间线（OpenF1 主源 + Jolpica 兜底，见 _strategy_stints）
    ax2 = fig.add_subplot(gs[1])
    ax2.set_facecolor("#1a1d26")
    finish_order = [_norm_name(e.get("driver_name"))
                    for e in sorted(entries, key=lambda x: _safe_int(x.get("position"), 99) or 99)
                    if (_safe_int(e.get("position"), 99) or 99) < 90]
    strat = strat or {}
    nums = list(strat.keys())
    nums.sort(key=lambda n: _finish_pos(finish_order, n))
    max_lap = 1
    for y, n in enumerate(nums):
        for s in sorted(strat[n], key=lambda x: x["stint"]):
            ls, le = s["lap_start"], s["lap_end"]
            max_lap = max(max_lap, le)
            ax2.barh(y, le - ls + 1, left=ls - 1, height=0.62,
                     color=COMPOUND_COLORS.get(s["compound"], "#888888"),
                     edgecolor="#1a1d26", linewidth=0.6)
    ax2.set_yticks(range(len(nums)))
    ax2.set_yticklabels([n.upper()[:3] for n in nums],
                        color="#eeeeee", fontsize=9)
    ax2.invert_yaxis()
    ax2.set_xlim(0, max_lap + 1)
    ax2.set_title("轮胎策略时间线", color="#dddddd", fontsize=13, loc="left")
    ax2.grid(axis="x", color="#333842", linewidth=0.6, alpha=0.7)
    ax2.tick_params(colors="#aaaaaa")
    for spine in ax2.spines.values():
        spine.set_color("#444")
    from matplotlib.lines import Line2D
    used = {s["compound"] for n in nums for s in strat[n]}
    order = [c for c in ("SOFT", "MEDIUM", "HARD", "INTERMEDIATE", "WET", "UNKNOWN")
             if c in used]
    if order:
        ax2.legend(handles=[Line2D([0], [0], marker="s", color="none",
                                   markerfacecolor=COMPOUND_COLORS[c], markersize=9,
                                   label=COMPOUND_CN.get(c, c)) for c in order],
                   loc="lower right", frameon=False, fontsize=9, labelcolor="#dddddd",
                   ncols=max(1, len(order)))

    # ③ 名次变化
    ax3 = fig.add_subplot(gs[2])
    ax3.set_facecolor("#1a1d26")
    rows = []
    for e in entries:
        pos = _safe_int(e.get("position"), 99)
        if pos is None or pos >= 90:
            continue
        g = _safe_int(e.get("grid"), 0)
        g = g if g and g < 90 else 22
        rows.append({"name": (e.get("driver_name") or "").split()[-1].upper()[:3],
                     "delta": g - pos})
    rows.sort(key=lambda x: -x["delta"])
    xs = range(len(rows))
    colors = ["#39b54a" if r["delta"] > 0 else "#ff5252" if r["delta"] < 0 else "#888888"
              for r in rows]
    ax3.bar(xs, [r["delta"] for r in rows], color=colors, width=0.62, alpha=0.9)
    ax3.set_xticks(list(xs))
    ax3.set_xticklabels([r["name"] for r in rows], color="#eeeeee", fontsize=9)
    ax3.axhline(0, color="#666", linewidth=1)
    ax3.set_title("名次变化（发车→完赛，+为上升）", color="#dddddd", fontsize=13, loc="left")
    ax3.grid(axis="y", color="#333842", linewidth=0.6, alpha=0.7)
    ax3.tick_params(colors="#aaaaaa")
    for spine in ax3.spines.values():
        spine.set_color("#444")

    _savefig_atomic(fig, path)
    plt.close(fig)
    logger.info(f"[chart] 赛后三联图已生成: {path}")
    return path


def render_quali_gap_chart(season: int, round_num: int, race_name: str = "",
                           f1_api=None, session_type: str = "qualifying") -> Optional[str]:
    """排位/冲刺排位差距条形图：各车手排位最快圈与杆位的差值。
    数据源用归一化排位成绩（Jolpica/f1api.dev，time=最快圈）——OpenF1 部分分站
    只有正赛无排位（2026 雪邦实证），不可用 OpenF1 排位圈速。
    session_type: qualifying（正赛排位）/ sprint_qualifying（冲刺排位）。
    冲刺排位文件名带 _sq 后缀避免同轮覆盖"""
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    suffix = "_sq" if session_type == "sprint_qualifying" else ""
    path = os.path.join(EXPORTS_DIR, f"quali_gap_{CHART_VER}_{season}_R{round_num}{suffix}.png")
    if os.path.exists(path):
        return path
    if not f1_api:
        return None
    try:
        res = f1_api.get_session_results(round_num, session_type, retries=1,
                                         season=season) or {}
    except Exception:
        return None
    entries = res.get("entries") or []

    def _parse_t(t):
        try:
            m, s = str(t).split(":")
            return int(m) * 60 + float(s)
        except (ValueError, AttributeError):
            return None

    best: Dict[str, float] = {}
    order_hint: Dict[str, int] = {}
    for e in entries:
        v = _parse_t(e.get("time"))
        name = (e.get("driver_name") or "").split()[-1]
        if v is None or not name:
            continue
        best[name] = v
        order_hint[name] = _safe_int(e.get("position"), 99) or 99
    if len(best) < 5:
        return None
    pole = min(best.values())
    # 按差距排序（非 position 提示）：杆位=差距 0 必须在首行，否则杆位金色标错人
    rows = sorted(((s, v - pole) for s, v in best.items()),
                  key=lambda x: x[1])

    _sess_label = "冲刺排位" if session_type == "sprint_qualifying" else "排位"
    fig, plt = _setup_dark_fig((14, 7),
                               f"{season} {race_name} · {_sess_label}差距（与杆位）" if race_name
                               else f"{season} R{round_num} {_sess_label}差距")
    ax = fig.add_axes([0.07, 0.12, 0.9, 0.74])
    ax.set_facecolor("#1a1d26")
    xs = range(len(rows))
    bars = ax.bar(xs, [d for _, d in rows], color="#00a19b", width=0.62, alpha=0.9)
    bars[0].set_color("#ffd700")  # 杆位金色
    for i, (_, d) in enumerate(rows):
        ax.text(i, d + max(0.01, rows[-1][1] * 0.02), "杆位" if i == 0 else f"+{d:.3f}",
                ha="center", color="#ffd700" if i == 0 else "#dddddd", fontsize=9)
    ax.set_xticks(list(xs))
    ax.set_xticklabels([s.upper()[:3] for s, _ in rows], color="#eeeeee", fontsize=10)
    ax.set_ylabel("差距 (s)", color="#cccccc")
    ax.grid(axis="y", color="#333842", linewidth=0.6, alpha=0.7)
    ax.tick_params(colors="#aaaaaa")
    for spine in ax.spines.values():
        spine.set_color("#444")
    _savefig_atomic(fig, path)
    plt.close(fig)
    logger.info(f"[chart] 排位差距图已生成: {path}")
    return path


def render_points_trend_chart(season: int, f1_api=None) -> Optional[str]:
    """车手积分走势折线图（本赛季逐站累计，前6名；含冲刺赛积分）。
    文件名含轮次数，每站后 URL 变化自动破 QQ 图片缓存"""
    if not f1_api:
        return None
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    from datetime import datetime as _dt
    schedule = f1_api.get_schedule_for_year(season) or []
    today = _dt.now().date().isoformat()
    completed = [r for r in schedule
                 if r.get("date") and r["date"] < today]
    if len(completed) < 3:
        return None
    path = os.path.join(EXPORTS_DIR,
                        f"points_trend_{season}_R{len(completed)}.png")
    if os.path.exists(path):
        return path

    totals: Dict[str, List[float]] = {}
    rounds = []
    cum: Dict[str, float] = {}
    for r in completed:
        rnd = int(r.get("round", 0))
        rounds.append(rnd)
        awarded: Dict[str, float] = {}
        race_ok = False
        for sess in ("race", "sprint"):
            try:
                res = f1_api.get_session_results(rnd, sess, retries=1, season=season) or {}
            except Exception:
                continue
            if sess == "race" and res.get("entries"):
                race_ok = True
            for e in res.get("entries") or []:
                pts = e.get("points")
                if pts:
                    name = (e.get("driver_name") or "").split()[-1]
                    awarded[name] = awarded.get(name, 0) + float(pts)
        if not race_ok:
            # 某站正赛拉取失败/空数据：图会缺一站积分且被缓存一周，宁可不画
            logger.warning(f"[chart] 积分走势 R{rnd} 成绩拉取失败，放弃渲染（防残缺图被缓存）")
            return None
        for n, p in awarded.items():
            if n not in cum:
                # 季中加入车手：首秀之前的轮次前部补零（尾部补值会把曲线整体左移）
                totals[n] = [0.0] * (len(rounds) - 1)
            cum[n] = cum.get(n, 0) + p
        for n in cum:
            totals[n].append(cum[n])
    if not totals:
        return None
    top6 = sorted(cum.items(), key=lambda x: -x[1])[:6]
    palette = ["#ffd700", "#00a19b", "#ff8c00", "#39b54a", "#00a3e0", "#ff5252"]

    fig, plt = _setup_dark_fig((14, 7),
                               f"{season} 车手积分走势（逐站累计，含冲刺赛）")
    ax = fig.add_axes([0.06, 0.1, 0.9, 0.78])
    ax.set_facecolor("#1a1d26")
    xs = range(len(rounds))
    for (name, _), color in zip(top6, palette):
        series = totals.get(name) or []
        if len(series) < len(rounds):
            # 季中加入者前端补 0（首次得分前无积分），尾部填充会把首场积分错画到 R1
            series = [0.0] * (len(rounds) - len(series)) + series
        ax.plot(xs, series[:len(rounds)], marker="o", markersize=4, linewidth=2,
                color=color, label=name.upper()[:3])
        ax.annotate(f"{series[-1]:.0f}", (len(rounds) - 1, series[-1]),
                    color=color, fontsize=11, fontweight="bold",
                    xytext=(6, 0), textcoords="offset points")
    ax.set_xticks(list(xs))
    ax.set_xticklabels([f"R{r}" for r in rounds], color="#aaaaaa", fontsize=9)
    ax.set_ylabel("累计积分", color="#cccccc")
    ax.legend(loc="upper left", frameon=False, fontsize=11, labelcolor="#dddddd",
              ncols=min(6, len(top6)))
    ax.grid(axis="y", color="#333842", linewidth=0.6, alpha=0.7)
    ax.tick_params(colors="#aaaaaa")
    for spine in ax.spines.values():
        spine.set_color("#444")
    _savefig_atomic(fig, path)
    plt.close(fig)
    logger.info(f"[chart] 积分走势图已生成: {path}")
    return path


def render_pace_chart(season: int, round_num: int, race_name: str = "",
                      f1_api=None) -> Optional[str]:
    """渲染 pace 分布图 → PNG 路径，失败 None。已生成直接复用（完赛数据不可变）"""
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    path = os.path.join(EXPORTS_DIR, f"pace_chart_{CHART_VER}_{season}_R{round_num}.png")
    path = _race_completed_gate(f1_api, season, round_num, path)  # 未完赛跑日期戳文件名
    if os.path.exists(path):
        return path

    from .stint_analysis import StintAnalyzer
    analyzer = StintAnalyzer(f1_api=f1_api)
    pace, err = _fetch_pace_laps(analyzer, season, round_num)
    if not pace:
        logger.info(f"[chart] pace 数据不可用: {err}")
        return None
    # Jolpica 兜底渲染（无配方全灰）：写日期戳 fb 文件名，OpenF1 恢复后仍可生成正式版
    _is_fb = all(c == "UNKNOWN" for laps in pace.values() for _, c in laps)
    if _is_fb:
        from datetime import datetime as _dt
        if "_fb" not in os.path.basename(path):  # 完赛闸门可能已改日期戳，防 _fb_fb 叠加
            path = os.path.splitext(path)[0] + f"_fb{_dt.now().strftime('%m%d')}.png"
        if os.path.exists(path):
            return path

    import numpy as np
    from matplotlib import pyplot as plt
    _pick_cjk_font()

    # 按中位圈速排序
    stats = sorted(
        ((surname, sorted(v for v, _ in laps), laps) for surname, laps in pace.items()),
        key=lambda x: x[1][len(x[1]) // 2])
    names = [s for s, _, _ in stats]
    data = [sd for _, sd, _ in stats]
    laps_by = {s: laps for s, _, laps in stats}
    medians = [sd[len(sd) // 2] for _, sd, _ in stats]
    best = medians[0]

    fig = plt.figure(figsize=(16, 9), facecolor="#1a1d26")
    gs = fig.add_gridspec(2, 1, height_ratios=[3, 1], hspace=0.08,
                          left=0.05, right=0.97, top=0.88, bottom=0.07)
    ax = fig.add_subplot(gs[0])
    ax.set_facecolor("#1a1d26")

    # 箱线图
    bp = ax.boxplot(data, vert=True, positions=range(len(names)),
                    widths=0.55, patch_artist=True, showfliers=False,
                    medianprops=dict(color="#ff8c00", linewidth=2),
                    whiskerprops=dict(color="#cccccc", linewidth=1),
                    capprops=dict(color="#cccccc"),
                    boxprops=dict(facecolor="#2a2e3d", edgecolor="#cccccc", alpha=0.8))
    # 散点按配方着色
    rng = np.random.default_rng(42)
    for i, surname in enumerate(names):
        for v, comp in laps_by[surname]:
            ax.scatter(i + rng.uniform(-0.18, 0.18), v, s=6,
                       color=COMPOUND_COLORS.get(comp, "#888888"), alpha=0.75,
                       linewidths=0, zorder=3)

    ax.set_xticks(range(len(names)))
    ax.set_xticklabels([n.upper()[:3] for n in names], color="#eeeeee", fontsize=10)
    ax.tick_params(colors="#aaaaaa")
    for spine in ax.spines.values():
        spine.set_color("#444")
    ax.grid(axis="y", color="#333842", linewidth=0.6, alpha=0.7)
    ax.set_ylabel("圈速 (s)", color="#cccccc")
    # 纵轴换成 分:秒 可读性更好（保持秒+网格即可，避免过度格式化）
    ax.invert_yaxis()

    # 图例：配方（固定顺序 红软→黄中→白硬→半雨→全雨）
    from matplotlib.lines import Line2D
    used_set = {c for laps in laps_by.values() for _, c in laps}
    used = [c for c in ("SOFT", "MEDIUM", "HARD", "INTERMEDIATE", "WET", "UNKNOWN")
            if c in used_set]
    ax.legend(handles=[Line2D([0], [0], marker="o", color="none", markerfacecolor=COMPOUND_COLORS[c],
                              markersize=8, label=COMPOUND_CN.get(c, c)) for c in used],
              loc="upper left", frameon=False, fontsize=10, labelcolor="#dddddd", ncols=len(used))

    title = f"{season} {race_name} · 正赛 Pace 分布（按轮胎配方）" if race_name \
        else f"{season} R{round_num} 正赛 Pace 分布"
    fig.suptitle(title, color="#f0f0f0", fontsize=17, fontweight="bold", x=0.05, ha="left")

    # 下半：中位圈速差条形图
    ax2 = fig.add_subplot(gs[1])
    ax2.set_facecolor("#1a1d26")
    deltas = [m - best for m in medians]
    ax2.bar(range(len(names)), deltas, color="#00a19b", alpha=0.85, width=0.62)
    for i, d in enumerate(deltas):
        ax2.text(i, d + max(deltas) * 0.02 + 0.005, f"+{d:.2f}", ha="center",
                 color="#dddddd", fontsize=9)
    ax2.set_xticks(range(len(names)))
    ax2.set_xticklabels([n.upper()[:3] for n in names], color="#eeeeee", fontsize=10)
    ax2.tick_params(colors="#aaaaaa")
    for spine in ax2.spines.values():
        spine.set_color("#444")
    ax2.grid(axis="y", color="#333842", linewidth=0.6, alpha=0.7)
    ax2.set_ylabel("与最快中位差 (s)", color="#cccccc")

    _savefig_atomic(fig, path)
    plt.close(fig)
    logger.info(f"[chart] pace 分布图已生成: {path}")
    return path

# ==================== 车队实力雷达图 ====================

def render_team_strength_radar(season: int, f1_api=None, f1cosmos=None) -> Optional[str]:
    """车队实力雷达图（6维，min-max 归一化到全围场，外圈=更强）。
    数据随赛季进程自动更新（TeamStrengthAnalyzer.compute 按累计成绩+升级件重算，
    缓存 24h）；文件名含最近完赛轮次，每站后 URL 变化自动破 QQ 缓存"""
    if not f1_api:
        return None
    from .team_strength import TeamStrengthAnalyzer
    all_s = TeamStrengthAnalyzer(f1_api, f1cosmos, season).compute()
    if len(all_s) < 4:
        return None
    # 文件名新鲜度：当前已完赛轮次数
    from datetime import datetime as _dt
    schedule = f1_api.get_schedule_for_year(season) or []
    today = _dt.now().date().isoformat()
    n_done = sum(1 for r in schedule if r.get("date") and r["date"] < today)
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    path = os.path.join(EXPORTS_DIR, f"team_radar_{season}_R{n_done}.png")
    if os.path.exists(path):
        return path

    # 取近期 pace 前 6 车队（pace_index=全场中位/队最快圈×100，越大越快）
    teams = sorted(all_s.values(), key=lambda s: -(s.get("pace_recent_avg") or 0))[:6]

    def _upg_delta(s):
        eff = s.get("upgrade_effects") or []
        return sum(e["delta"] for e in eff) / len(eff) if eff else None

    axes = [  # (轴名, 取值函数, 越大越强?) 统一 min-max 归一化到 10~100
        ("赛季Pace", lambda s: s.get("pace_season_avg"), True),
        ("近期Pace", lambda s: s.get("pace_recent_avg"), True),
        ("正赛兑现", lambda s: s.get("sunday_delta"), False),
        ("轮胎管理", lambda s: s.get("tire_deg_ms"), False),
        ("升级效果", lambda s: _upg_delta(s), False),
        ("低阻适配", lambda s: (s.get("by_downforce") or {}).get("低"), False),
    ]
    import numpy as np
    values = []
    for _, fn, higher_better in axes:
        raw = [fn(s) for s in teams]
        vals = [v for v in raw if v is not None]
        if not vals:
            values.append([50.0] * len(teams))
            continue
        lo, hi = min(vals), max(vals)
        if hi - lo < 1e-9:
            values.append([70.0] * len(teams))
            continue
        if higher_better:
            values.append([10 + 90 * (v - lo) / (hi - lo) if v is not None else 10.0
                           for v in raw])
        else:
            # 越小越强 → 反转
            values.append([10 + 90 * (hi - v) / (hi - lo) if v is not None else 10.0
                           for v in raw])

    fig, plt = _setup_dark_fig((10, 9))
    ax = fig.add_subplot(111, polar=True)
    ax.set_facecolor("#1a1d26")
    n = len(axes)
    angles = np.linspace(0, 2 * np.pi, n, endpoint=False).tolist()
    angles += angles[:1]
    palette = ["#ffd700", "#00a19b", "#ff8c00", "#39b54a", "#00a3e0", "#ff5252"]
    for i, s in enumerate(teams):
        vals = [values[j][i] for j in range(n)]
        vals += vals[:1]
        ax.plot(angles, vals, linewidth=2, color=palette[i % len(palette)],
                label=s["team"].replace("_", " ").title())
        ax.fill(angles, vals, alpha=0.10, color=palette[i % len(palette)])
    ax.set_xticks(angles[:-1])
    ax.set_xticklabels([a for a, _, _ in axes], color="#eeeeee", fontsize=12)
    ax.set_ylim(0, 100)
    ax.set_yticks([25, 50, 75, 100])
    ax.set_yticklabels(["25", "50", "75", "100"], color="#777", fontsize=8)
    ax.tick_params(colors="#aaaaaa")
    ax.grid(color="#333842", linewidth=0.7, alpha=0.8)
    ax.spines["polar"].set_color("#444")
    ax.legend(loc="upper right", bbox_to_anchor=(1.28, 1.10), frameon=False,
              fontsize=11, labelcolor="#dddddd")
    fig.suptitle(f"{season} 车队实力雷达图（截至 R{n_done}，外圈=更强）",
                 color="#f0f0f0", fontsize=16, fontweight="bold", x=0.5)
    fig.text(0.5, 0.02, "维度：赛季/近期pace（越大越快）、正赛兑现/轮胎管理/升级效果/低阻适配（越小越强）；min-max 归一化，外圈=更强",
             color="#999", fontsize=9, ha="center")
    _savefig_atomic(fig, path)
    plt.close(fig)
    logger.info(f"[chart] 车队雷达图已生成: {path}")
    return path


# ==================== 事故成本柱图 ====================

def render_destructors_chart(season: int, f1cosmos=None) -> Optional[str]:
    """事故维修成本柱图（车手 Top15 + 车队榜），/car 指令附图"""
    if not f1cosmos:
        return None
    records = f1cosmos.get_destructors(season)
    if not records:
        return None
    n_round = max((r.get("round") or 0) for r in records)
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    # 同轮可能追加新事故记录（比赛周内多次更新），文件名加日期戳防永久陈旧
    from datetime import datetime as _dt2
    stamp = _dt2.now().strftime("%m%d")
    path = os.path.join(EXPORTS_DIR, f"destructors_chart_{season}_R{n_round}_{stamp}.png")
    if os.path.exists(path):
        return path

    by_driver = defaultdict(float)
    by_team = defaultdict(float)
    for r in records:
        by_driver[(r.get("driver") or "?").title()] += r.get("cost") or 0
        by_team[r.get("team") or "?"] += r.get("cost") or 0
    top_d = sorted(by_driver.items(), key=lambda x: -x[1])[:15]
    top_t = sorted(by_team.items(), key=lambda x: -x[1])

    fig, plt = _setup_dark_fig((14, 10))
    gs = fig.add_gridspec(2, 1, height_ratios=[1.6, 1.0], hspace=0.45,
                          left=0.16, right=0.96, top=0.9, bottom=0.08)
    fig.suptitle(f"{season} 事故维修成本（截至 R{n_round}，部件申报价×数量估算）",
                 color="#f0f0f0", fontsize=16, fontweight="bold", x=0.16, ha="left")
    ax = fig.add_subplot(gs[0])
    ax.set_facecolor("#1a1d26")
    ys = range(len(top_d))
    ax.barh(ys, [c for _, c in top_d], color="#ff5252", alpha=0.85, height=0.62)
    ax.set_yticks(list(ys))
    ax.set_yticklabels([d for d, _ in top_d], color="#eeeeee", fontsize=10)
    ax.invert_yaxis()
    for y, (_, c) in zip(ys, top_d):
        ax.text(c, y, f" ${c / 1e6:.2f}M", va="center", color="#dddddd", fontsize=9)
    ax.set_title("车手榜（美元）", color="#dddddd", fontsize=13, loc="left")
    ax.grid(axis="x", color="#333842", linewidth=0.6, alpha=0.7)
    ax.tick_params(colors="#aaaaaa")
    for spine in ax.spines.values():
        spine.set_color("#444")

    ax2 = fig.add_subplot(gs[1])
    ax2.set_facecolor("#1a1d26")
    xs = range(len(top_t))
    ax2.bar(xs, [c for _, c in top_t], color="#00a19b", width=0.6, alpha=0.9)
    ax2.set_xticks(list(xs))
    ax2.set_xticklabels([t.split(" F1")[0].split(" Formula")[0] for t, _ in top_t],
                        color="#eeeeee", fontsize=9, rotation=18, ha="right")
    ax2.set_title("车队榜（美元）", color="#dddddd", fontsize=13, loc="left")
    ax2.grid(axis="y", color="#333842", linewidth=0.6, alpha=0.7)
    ax2.tick_params(colors="#aaaaaa")
    for spine in ax2.spines.values():
        spine.set_color("#444")
    _savefig_atomic(fig, path)
    plt.close(fig)
    logger.info(f"[chart] 事故成本图已生成: {path}")
    return path


# ==================== PU 部件用量堆叠柱图 ====================

def render_pu_usage_chart(season: int, f1cosmos=None) -> Optional[str]:
    """动力单元部件用量堆叠柱图（每车手一行横条，达赛季上限标 ⚠️）"""
    if not f1cosmos:
        return None
    elements = f1cosmos.get_elements(season)
    if not elements:
        return None
    from .f1cosmos_api import get_pu_limits
    pu_limits = get_pu_limits(season)
    # 每车手取最新 round 快照（与 format_pu_quota 同口径）
    latest: Dict[str, Dict] = {}
    for e in elements:
        key = e.get("last_name") or e.get("tla") or ""
        if key and (key not in latest or (e.get("round") or 0) >= (latest[key].get("round") or 0)):
            latest[key] = e
    if not latest:
        return None
    n_round = max(e.get("round", 0) for e in latest.values())
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    # 同轮 FIA 可能发多版 PU 文档（周六/周日增补），文件名加日期戳防永久陈旧
    from datetime import datetime as _dt2
    stamp = _dt2.now().strftime("%m%d")
    path = os.path.join(EXPORTS_DIR, f"pu_usage_{season}_R{n_round}_{stamp}.png")
    if os.path.exists(path):
        return path

    order = ["ICE", "TC", "EXH", "MGU-H", "MGU-K", "ES", "PU-CE", "PU-ANC"]
    cols = [k for k in order if any(k in (e.get("usages") or {}) for e in latest.values())]
    palette = {"ICE": "#ff5252", "TC": "#ff8c00", "EXH": "#ffd700", "MGU-H": "#888888",
               "MGU-K": "#00a19b", "ES": "#00a3e0", "PU-CE": "#9b59b6", "PU-ANC": "#39b54a"}
    rows = sorted(latest.values(), key=lambda e: -(e.get("total") or 0))

    fig, plt = _setup_dark_fig((13, max(7, len(rows) * 0.5 + 2)))
    ax = fig.add_axes([0.12, 0.09, 0.78, 0.80])
    ax.set_facecolor("#1a1d26")
    for y, e in enumerate(rows):
        left = 0
        for k in cols:
            try:
                v = int((e.get("usages") or {}).get(k) or 0)
            except (TypeError, ValueError):
                v = 0
            if v:
                ax.barh(y, v, left=left, height=0.62,
                        color=palette.get(k, "#888888"), edgecolor="#1a1d26")
                left += v
        warns = []
        for k in cols:
            lim = pu_limits.get(k)
            try:
                v = int((e.get("usages") or {}).get(k) or 0)
            except (TypeError, ValueError):
                continue
            if lim is not None and v >= lim:
                warns.append(k)
        label = f" {e.get('total') or 0}" + (f"  [超限: {','.join(warns)}]" if warns else "")
        ax.text(left + 0.05, y, label, va="center", color="#dddddd", fontsize=9)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([(e.get("last_name") or "?") for e in rows], color="#eeeeee", fontsize=10)
    ax.invert_yaxis()
    ax.set_xlabel("部件用量（件）", color="#cccccc")
    ax.grid(axis="x", color="#333842", linewidth=0.6, alpha=0.7)
    ax.tick_params(colors="#aaaaaa")
    for spine in ax.spines.values():
        spine.set_color("#444")
    from matplotlib.lines import Line2D
    ax.legend(handles=[Line2D([0], [0], marker="s", color="none",
                              markerfacecolor=palette[k], markersize=10, label=k)
                       for k in cols],
              loc="lower right", frameon=False, fontsize=9, labelcolor="#dddddd",
              ncols=min(len(cols), 4))
    fig.suptitle(f"{season} 动力单元部件用量（截至 R{n_round}，[超限]=达赛季上限）",
                 color="#f0f0f0", fontsize=15, fontweight="bold", x=0.12, ha="left")
    _savefig_atomic(fig, path)
    plt.close(fig)
    logger.info(f"[chart] PU 用量图已生成: {path}")
    return path


# ==================== 进站时长统计（Jolpica pitstops） ====================

def render_pitstop_chart(season: int, round_num: int, race_name: str = "",
                         f1_api=None) -> Optional[str]:
    """进站时长箱线图（车队对比，Jolpica pitstops——真实停站时长，比 OpenF1 的
    进站车道时间更有分析价值；且 Jolpica 与赛程同源、服务器网络实证稳定）"""
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    path = os.path.join(EXPORTS_DIR, f"pitstop_chart_{CHART_VER}_{season}_R{round_num}.png")
    path = _race_completed_gate(f1_api, season, round_num, path)  # 未完赛跑日期戳文件名
    if os.path.exists(path):
        return path
    try:
        import requests
        stops = []
        offset = 0
        while True:  # 分页（默认 30/页）
            resp = requests.get(
                f"https://api.jolpi.ca/ergast/f1/{season}/{round_num}/pitstops.json",
                params={"limit": 100, "offset": offset},
                headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
            data = resp.json()["MRData"]["RaceTable"]["Races"]
            if not data:
                break
            stops += data[0].get("PitStops", [])
            total = int(resp.json()["MRData"].get("total", 0))
            offset += 100
            if offset >= total or not data[0].get("PitStops"):
                break
    except Exception as e:
        logger.warning(f"[chart] Jolpica pitstops 获取失败: {e}")
        return None
    entries = _race_entries(f1_api, season, round_num) if f1_api else []
    drv_team = {(e.get("driver_id") or ""): e.get("team_name", "")
                for e in entries}
    # Jolpica pitstops 的 driverId 与成绩 entries 同源同 id；姓氏兜底（防偶发 id 漂移）
    drv_team.update({(e.get("driver_name") or "").split()[-1].lower(): e.get("team_name", "")
                     for e in entries})
    by_team: Dict[str, List[float]] = defaultdict(list)
    counts: Dict[str, int] = defaultdict(int)
    for s in stops:
        try:
            dur = float(s.get("duration", 0))
        except (TypeError, ValueError):
            continue
        if not (15.0 < dur < 90.0):  # 剔红旗/异常长停
            continue
        team = (drv_team.get(s.get("driverId") or "")
                or drv_team.get((s.get("driverId") or "").split("_")[-1].lower())
                or "")
        if not team:
            continue
        by_team[team].append(dur)
        counts[team] += 1
    by_team = {t: v for t, v in by_team.items() if len(v) >= 2}
    if len(by_team) < 3:
        return None
    stats = sorted(by_team.items(), key=lambda x: sorted(x[1])[len(x[1]) // 2])

    fig, plt = _setup_dark_fig((13, max(6, len(stats) * 0.7 + 2)))
    ax = fig.add_axes([0.16, 0.09, 0.78, 0.80])
    ax.set_facecolor("#1a1d26")
    ax.boxplot([v for _, v in stats], vert=False, positions=range(len(stats)),
               widths=0.55, patch_artist=True, showfliers=True,
               medianprops=dict(color="#ff8c00", linewidth=2),
               whiskerprops=dict(color="#cccccc"), capprops=dict(color="#cccccc"),
               boxprops=dict(facecolor="#2a2e3d", edgecolor="#cccccc", alpha=0.85),
               flierprops=dict(marker="o", markerfacecolor="#666", markersize=4))
    ax.set_yticks(range(len(stats)))
    ax.set_yticklabels([f"{t.split(' F1')[0].split(' Formula')[0]}（{counts[t]}次）"
                        for t, _ in stats], color="#eeeeee", fontsize=10)
    ax.set_xlabel("停站时长（秒，Jolpica 口径=车静止时间；已剔红旗异常长停）", color="#cccccc")
    ax.grid(axis="x", color="#333842", linewidth=0.6, alpha=0.7)
    ax.tick_params(colors="#aaaaaa")
    for spine in ax.spines.values():
        spine.set_color("#444")
    fig.suptitle(f"{season} {race_name} · 进站时长车队对比" if race_name
                 else f"{season} R{round_num} 进站时长",
                 color="#f0f0f0", fontsize=16, fontweight="bold", x=0.16, ha="left")
    _savefig_atomic(fig, path)
    plt.close(fig)
    logger.info(f"[chart] 进站时长图已生成: {path}")
    return path


# ==================== 安全车/VSC/红旗事件时间线 ====================

def get_sc_events(season: int, round_num: int, f1_api=None) -> Optional[List[Dict[str, Any]]]:
    """安全车事件结构化提取（OpenF1 race_control 主源，livetiming 兜底）：
    部署→结束配对为 (start_lap, end_lap, kind, message)，红旗单独记录。
    供 /sc 指令、赛后推送图、LLM 工具 get_safety_car_events 共用。
    返回 None = 双源皆不可达（数据不可用，禁止断言"本站干净"）；
    返回 [] = 有数据源但确实无事件（可断言干净）"""
    from .stint_analysis import StintAnalyzer
    analyzer = StintAnalyzer(f1_api=f1_api)
    sk = _openf1_session_key(analyzer, season, round_num, "Race")
    rc = analyzer._openf1_get("race_control", {"session_key": sk}) if sk else []
    source_ok = bool(rc)  # OpenF1 有响应（哪怕空事件列表也算有数据）
    if not rc:
        # livetiming 归档兜底（OpenF1 401/赛中锁定时）
        try:
            from .livetiming_client import LiveTimingClient
            rc = LiveTimingClient(f1_api=analyzer.f1_api).get_race_control(season, round_num)
            source_ok = source_ok or rc is not None  # None=不可达；[]=有源无事件
        except Exception:
            pass
    if not source_ok:
        return None
    rc = rc or []
    events: List[Dict[str, Any]] = []
    open_ev: Dict[str, tuple] = {}  # kind -> (start_lap, deploy_message)
    for e in rc:
        msg = (e.get("message") or "").lower()
        raw_msg = e.get("message") or ""
        lap = _safe_int(e.get("lap_number"), None)
        if lap is None:
            continue
        # 优先用 flag 字段判定红旗（OpenF1 race_control 有 flag 字段；
        # livetiming 无该字段时退回消息文本判定，排除 chequered/checkered 子串误标）
        is_red = (str(e.get("flag") or "").upper() == "RED"
                  or ("red flag" in msg and "chequered" not in msg
                      and "checkered" not in msg))
        if "virtual safety car" in msg:
            if "deployed" in msg:
                if "vsc" in open_ev:
                    s0, m0 = open_ev.pop("vsc")
                    events.append({"kind": "vsc", "start_lap": s0, "end_lap": s0 + 2,
                                   "message": m0})  # 前段未闭合先落估算段
                    logger.warning(f"[chart] SC 事件同 kind 二次 deploy，前段按估算闭合: L{s0}")
                open_ev["vsc"] = (lap, raw_msg)
            elif "ending" in msg or "clear" in msg:
                if "vsc" in open_ev:
                    s, m = open_ev.pop("vsc")
                    events.append({"kind": "vsc", "start_lap": s, "end_lap": lap, "message": m})
        elif is_red:
            # 红旗：同一红旗可能多条电文（部署+恢复说明），相邻圈去重
            if events and events[-1]["kind"] == "red" \
                    and abs(events[-1]["start_lap"] - lap) <= 1:
                events[-1]["end_lap"] = max(events[-1]["end_lap"], lap + 1)
            else:
                events.append({"kind": "red", "start_lap": lap, "end_lap": lap + 1,
                               "message": raw_msg})
        elif "safety car" in msg:
            if "deployed" in msg:
                if "sc" in open_ev:
                    s0, m0 = open_ev.pop("sc")
                    events.append({"kind": "sc", "start_lap": s0, "end_lap": s0 + 2,
                                   "message": m0})
                    logger.warning(f"[chart] SC 事件同 kind 二次 deploy，前段按估算闭合: L{s0}")
                open_ev["sc"] = (lap, raw_msg)
            elif "in this" in msg or "ending" in msg or "clear" in msg:
                if "sc" in open_ev:
                    s, m = open_ev.pop("sc")
                    events.append({"kind": "sc", "start_lap": s, "end_lap": lap, "message": m})
    for k, (lap, m) in open_ev.items():  # 未配对收尾（到比赛结束）
        events.append({"kind": k, "start_lap": lap, "end_lap": lap + 2, "message": m})
    events.sort(key=lambda x: x["start_lap"])
    return events


def render_sc_timeline(season: int, round_num: int, race_name: str = "",
                       f1_api=None) -> Optional[str]:
    """安全车事件时间线（SC 黄底/VSC 蓝底/红旗红底 + 部署圈标记）。
    无事件返回 None（调用方跳过发送）"""
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    path = os.path.join(EXPORTS_DIR, f"sc_timeline_{CHART_VER}_{season}_R{round_num}.png")
    path = _race_completed_gate(f1_api, season, round_num, path)  # 未完赛跑日期戳文件名
    if os.path.exists(path):
        return path
    evts = get_sc_events(season, round_num, f1_api)
    if not evts:
        return None
    events = [(e["start_lap"], e["end_lap"], e["kind"]) for e in evts]

    # 规范化成绩条目无 laps 字段：改从赛道档案取正赛圈数，取不到再回退事件末圈+5
    total_laps = 0
    if f1_api:
        try:
            sched = f1_api.get_schedule_for_year(season) or []
            race = next((r for r in sched if int(r.get("round", 0)) == round_num), None)
            cid = (race or {}).get("Circuit", {}).get("circuitId", "")
            if cid:
                from .circuits_manager import CircuitsManager
                total_laps = int((CircuitsManager().get_circuit(cid) or {}).get("laps") or 0)
        except Exception:
            pass
    total_laps = total_laps or (max(e[1] for e in events) + 5)
    fig, plt = _setup_dark_fig((14, 4.2))
    ax = fig.add_axes([0.05, 0.18, 0.92, 0.6])
    ax.set_facecolor("#1a1d26")
    kind_style = {"sc": ("#ffd700", "安全车 SC"), "vsc": ("#00a3e0", "虚拟安全车 VSC"),
                  "red": ("#ff3333", "红旗")}
    for s, e, kind in events:
        color, _ = kind_style.get(kind, ("#888", "?"))
        ax.axvspan(s, e, color=color, alpha=0.35)
        ax.axvline(s, color=color, linewidth=2)
        ax.text(s, 0.6, f"L{s}", color=color, fontsize=10, fontweight="bold",
                ha="center")
    ax.set_xlim(0, total_laps)
    ax.set_ylim(0, 1)
    ax.set_yticks([])
    ax.set_xlabel("圈数", color="#cccccc")
    ax.tick_params(colors="#aaaaaa")
    for spine in ax.spines.values():
        spine.set_color("#444")
    from matplotlib.lines import Line2D
    used = sorted({k for _, _, k in events})
    ax.legend(handles=[Line2D([0], [0], marker="s", color="none",
                              markerfacecolor=kind_style[k][0], markersize=10,
                              label=kind_style[k][1]) for k in used],
              loc="upper right", frameon=False, fontsize=10, labelcolor="#dddddd",
              ncols=len(used))
    n_sc = sum(1 for _, _, k in events if k == "sc")
    n_vsc = sum(1 for _, _, k in events if k == "vsc")
    n_red = sum(1 for _, _, k in events if k == "red")
    fig.suptitle(f"{season} {race_name} · 安全车事件时间线"
                 f"（SC×{n_sc} VSC×{n_vsc} 红旗×{n_red}）",
                 color="#f0f0f0", fontsize=14, fontweight="bold", x=0.05, ha="left")
    _savefig_atomic(fig, path)
    plt.close(fig)
    logger.info(f"[chart] 安全车时间线已生成: {path}")
    return path

# ==================== 天气时间序列图 ====================

def render_weather_chart(season: int, round_num: int, race_name: str = "",
                         f1_api=None) -> Optional[str]:
    """正赛天气时间序列：气温/赛道温度双线（左轴）+ 降雨色带+风速。
    已完赛：OpenF1 weather 分钟级实测（2023+）；未赛：Open-Meteo 逐小时预报
    （气温 + 沥青温度回归模型估算 + 降水概率%）。"""
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    path = os.path.join(EXPORTS_DIR, f"weather_chart_{CHART_VER}_{season}_R{round_num}.png")
    path = _race_completed_gate(f1_api, season, round_num, path)  # 未完赛跑日期戳文件名
    if os.path.exists(path):
        return path  # 实测版永久复用
    from .stint_analysis import StintAnalyzer
    analyzer = StintAnalyzer(f1_api=f1_api)
    sk = _openf1_session_key(analyzer, season, round_num, "Race")
    rows = analyzer._openf1_get("weather", {"session_key": sk}) if sk else []
    if rows and len(rows) >= 10:
        measured = _weather_chart_openf1(rows, path, season, round_num, race_name)
        if measured:
            return measured
        # 实测有数据但过滤后不可用（传感器整场掉线）：穿透到归档/预报
    # livetiming 归档实测兜底（OpenF1 401/不可达时；行结构同 OpenF1 weather）
    try:
        from .livetiming_client import LiveTimingClient
        lt_rows = LiveTimingClient(f1_api=analyzer.f1_api).get_weather(season, round_num)
        if lt_rows and len(lt_rows) >= 10:
            measured = _weather_chart_openf1(lt_rows, path, season, round_num, race_name)
            if measured:
                logger.info(f"[chart] 天气图走 livetiming 实测归档兜底: {season} R{round_num}")
                return measured
    except Exception as e:
        logger.warning(f"[chart] livetiming 天气兜底失败: {e}")
    # 预报版独立文件名（带日期戳防陈旧），赛后实测版生成后走实测文件名，互不挡路
    from datetime import datetime as _dt
    fc_path = os.path.join(
        EXPORTS_DIR,
        f"weather_fc_{season}_R{round_num}_{_dt.now().strftime('%m%d')}.png")
    if os.path.exists(fc_path):
        return fc_path
    return _weather_chart_forecast(season, round_num, race_name, f1_api, fc_path)


def _weather_chart_forecast(season: int, round_num: int, race_name: str,
                            f1_api, path: str) -> Optional[str]:
    """未赛分站预报图：Open-Meteo 逐小时（气温/降水概率/风速 + 沥青温度回归估算）"""
    if not f1_api:
        return None
    try:
        schedule = f1_api.get_schedule_for_year(season) or []
        race = next((r for r in schedule
                     if int(r.get("round", 0)) == round_num), None)
        if not race:
            return None
        loc = (race.get("Circuit", {}) or {}).get("Location", {}) or {}
        lat, lon = float(loc.get("lat")), float(loc.get("long"))
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        rd = _dt.strptime(race.get("date", ""), "%Y-%m-%d").replace(tzinfo=_tz.utc)
        from .weather_api import WeatherAPI
        hourly = WeatherAPI()._fetch_hourly(
            lat, lon, rd - _td(days=1), rd + _td(days=1),
            cache_key=f"weather_chart_fc_{season}_{round_num}", ttl_hours=3)
        if not hourly or "hourly" not in hourly:
            return None
        h = hourly["hourly"]
        times = [_dt.fromtimestamp(t, tz=_tz.utc) for t in h["time"]]
        if not times:
            return None
        t0 = times[0]
        xs = [((t - t0).total_seconds() / 3600) for t in times]
        air = h.get("temperature_2m") or []
        # Open-Meteo 可能回嵌套 null，fill_between 遇 None 直接抛 TypeError
        precip = [p or 0 for p in (h.get("precipitation_probability") or [0] * len(xs))]
        wind = [w or 0 for w in (h.get("wind_speed_10m") or [0] * len(xs))]
        soil = h.get("soil_temperature_0cm") or []
        rad = h.get("shortwave_radiation") or []
        from .weather_api import ASPHALT_MODEL
        track = []
        for i in range(len(xs)):
            a = air[i] if i < len(air) else None
            s = soil[i] if i < len(soil) else None
            r = rad[i] if i < len(rad) else None
            if a is None:
                track.append(None)
                continue
            track.append(ASPHALT_MODEL["intercept"] + ASPHALT_MODEL["air"] * a
                         + (ASPHALT_MODEL["soil"] * s if s is not None else 0)
                         + (ASPHALT_MODEL["radiation"] * r if r is not None else 0))
        return _weather_chart_draw(xs, air, track, precip, wind, path,
                                   f"{season} {race_name} · 比赛周天气预报（逐小时）",
                                   xlabel="距窗口开始（小时）", precip_is_prob=True)
    except Exception as e:
        logger.warning(f"[chart] 天气预报图生成失败: {e}")
        return None


def _weather_chart_draw(xs, air, track, rain, wind, path, title,
                        xlabel, precip_is_prob=False) -> str:
    """共用绘制：温度双线 + 降水（实测0/1色带 或 预报概率%）+ 风速"""
    import numpy as np
    fig, plt = _setup_dark_fig((14, 6.5))
    ax = fig.add_axes([0.06, 0.11, 0.86, 0.76])
    ax.set_facecolor("#1a1d26")
    ax.plot(xs, track, color="#ff5252", linewidth=1.8,
            label="沥青温度（估算模型）" if precip_is_prob else "赛道温度")
    ax.plot(xs, air, color="#00a3e0", linewidth=1.8, label="气温")
    ax.plot(xs, wind, color="#888888", linewidth=1.0, alpha=0.7, label="风速(m/s)")
    ax.set_xlabel(xlabel, color="#cccccc")
    ax.set_ylabel("温度 (°C)", color="#cccccc")
    ax.grid(axis="y", color="#333842", linewidth=0.6, alpha=0.7)
    ax.tick_params(colors="#aaaaaa")
    for spine in ax.spines.values():
        spine.set_color("#444")

    ax2 = ax.twinx()
    ax2.set_facecolor("none")
    if precip_is_prob:
        # 预报：降水概率% 半透明色带
        ax2.fill_between(xs, 0, rain, color="#39b54a", alpha=0.20, label="降水概率%")
        ax2.set_ylim(0, 100)
        ax2.set_yticks([])
    else:
        # 实测：0/1 降雨标记压低到 25% 高度色带
        ax2.bar(xs, rain, width=1.0, color="#39b54a", alpha=0.22, label="降雨")
        ax2.set_ylim(0, max(max(rain) * 4.0, 1))
        ax2.set_yticks([])
    ax2.spines["right"].set_color("#39b54a")

    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper left", frameon=False, fontsize=10,
              labelcolor="#dddddd", ncols=4)
    fig.suptitle(title, color="#f0f0f0", fontsize=15, fontweight="bold",
                 x=0.06, ha="left")
    _savefig_atomic(fig, path)
    plt.close(fig)
    logger.info(f"[chart] 天气时间序列图已生成: {path}")
    return path


def _weather_chart_openf1(rows, path, season, round_num, race_name) -> Optional[str]:
    """已完赛分站：OpenF1 分钟级实测绘制"""
    from datetime import datetime as _dt
    t0 = None
    xs, air, track, rain, wind = [], [], [], [], []
    for r in rows:
        try:
            t = _dt.fromisoformat(str(r["date"]).replace("Z", "+00:00"))
        except (ValueError, KeyError):
            continue
        # 传感器掉线会产生 0/None 假值（竖直跳水线），剔除
        air_t, track_t = r.get("air_temperature"), r.get("track_temperature")
        if not air_t or not track_t or air_t <= 0 or track_t <= 5:
            continue
        if t0 is None:
            t0 = t
        xs.append((t - t0).total_seconds() / 60)
        air.append(air_t)
        track.append(track_t)
        rain.append(r.get("rainfall") or 0)
        wind.append(r.get("wind_speed") or 0)
    if len(xs) < 10:
        return None  # 过滤后采样不足（传感器整场掉线），不出空图也不落盘
    rained = sum(1 for r in rain if r)
    title = (f"{season} {race_name} · 正赛天气时间序列" if race_name
             else f"{season} R{round_num} 正赛天气")
    title += f"（有降雨 {rained} 个采样点）" if rained else "（无降雨）"
    return _weather_chart_draw(xs, air, track, rain, wind, path, title,
                               xlabel="距首个有效采样（分钟）", precip_is_prob=False)


# ==================== 渲染并发保护 ====================
# pyplot 全局状态非线程安全：渲染可能由调度线程/指令线程/LLM 工具线程并发触发。
# RLock 串行（可重入防未来公开函数互调死锁）+ finally 只清理本函数新增的 figure
# （close("all") 会误杀 telemetry_analysis/ratings_chart 等其他模块在画的图）
import threading as _threading

_RENDER_LOCK = _threading.RLock()


def _render_guard(fn):
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        from matplotlib import pyplot as _plt
        before = set(_plt.get_fignums())
        with _RENDER_LOCK:
            try:
                return fn(*args, **kwargs)
            finally:
                for fignum in set(_plt.get_fignums()) - before:
                    try:
                        _plt.close(fignum)
                    except Exception:
                        pass

    return wrapper


for _pub in ("render_strategy_chart", "render_positions_chart", "render_race_report_chart",
             "render_quali_gap_chart", "render_points_trend_chart", "render_pace_chart",
             "render_team_strength_radar", "render_destructors_chart", "render_pu_usage_chart",
             "render_pitstop_chart", "render_sc_timeline", "render_weather_chart"):
    if _pub in globals():
        globals()[_pub] = _render_guard(globals()[_pub])
del _pub