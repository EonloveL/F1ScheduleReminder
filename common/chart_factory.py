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


def _fetch_pace_laps(analyzer, season: int, round_num: int
                     ) -> Tuple[Optional[Dict[str, List[Tuple[float, str]]]], str]:
    """取每位车手有效圈 [(圈速s, 配方)]；返回 ({surname: laps}, error_msg)"""
    sk = analyzer._openf1_session_key(season, round_num)
    if not sk:
        return None, f"{season} R{round_num} 无 OpenF1 数据（2023 前或分站未赛）"
    stints = analyzer._openf1_get("stints", {"session_key": sk})
    laps = analyzer._openf1_get("laps", {"session_key": sk})
    drivers = analyzer._openf1_get("drivers", {"session_key": sk})
    if not stints or not laps:
        return None, f"{season} R{round_num} OpenF1 无 stint/圈速数据"

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
            ls, le = int(s.get("lap_start") or 0), int(s.get("lap_end") or 0)
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
    防崩溃留下损坏文件被 exists 检查永久复用"""
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


def _style_ax(ax):
    ax.set_facecolor("#1a1d26")
    ax.tick_params(colors="#aaaaaa")
    for spine in ax.spines.values():
        spine.set_color("#444")
    ax.grid(axis="y", color="#333842", linewidth=0.6, alpha=0.7)


def _openf1_session_key(analyzer, season: int, round_num: int,
                        session_type: str = "Race") -> Optional[int]:
    """按赛程日期定位 OpenF1 环节（Race=正赛当天；Qualifying=正赛前1-2天，
    日期窗口往前扩 3 天再按 session_name 精确匹配）"""
    race_date = None
    if analyzer.f1_api:
        try:
            schedule = analyzer.f1_api.get_schedule_for_year(season) or []
            race = next((r for r in schedule
                         if int(r.get("round", 0)) == round_num), None)
            race_date = race.get("date") if race else None
        except Exception:
            pass
    params = {"session_type": session_type}
    if race_date:
        if session_type == "Race":
            params["date_start"] = race_date
        else:
            from datetime import datetime as _dt, timedelta as _td
            try:
                d0 = _dt.strptime(race_date, "%Y-%m-%d") - _td(days=3)
                params["date_start"] = d0.strftime("%Y-%m-%d")
            except Exception:
                params["year"] = season
    else:
        params["year"] = season
    try:
        from .stint_analysis import OPENF1_BASE
        resp = analyzer.session.get(f"{OPENF1_BASE}/sessions", params=params,
                                    timeout=20)
        data = resp.json()
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


def _norm_name(s: str) -> str:
    """姓名归一化：小写 + 去重音 + 压缩空白（跨源匹配：entries 全名 vs OpenF1 last_name）"""
    import unicodedata
    return " ".join("".join(c for c in unicodedata.normalize("NFKD", (s or "").lower())
                            if not unicodedata.combining(c)).split())


def _finish_pos(finish_full: List[str], openf1_last: str) -> int:
    """OpenF1 姓氏在完赛名次全名列表中的位置：后缀匹配（de vries 命中 nyck de vries，
    perez 命中 sergio pérez），未命中返回 99"""
    s = _norm_name(openf1_last)
    if not s:
        return 99
    for i, full in enumerate(finish_full):
        if full == s or full.endswith(" " + s):
            return i
    return 99


def render_strategy_chart(season: int, round_num: int, race_name: str = "",
                          f1_api=None) -> Optional[str]:
    """轮胎策略时间线：横轴圈数、纵轴车手（按完赛名次），stint 色块按配方着色"""
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    path = os.path.join(EXPORTS_DIR, f"strategy_chart_{season}_R{round_num}.png")
    if os.path.exists(path):
        return path
    from .stint_analysis import StintAnalyzer
    analyzer = StintAnalyzer(f1_api=f1_api)
    sk = _openf1_session_key(analyzer, season, round_num, "Race")
    if not sk:
        return None
    stints = analyzer._openf1_get("stints", {"session_key": sk})
    drivers = analyzer._openf1_get("drivers", {"session_key": sk})
    entries = _race_entries(f1_api, season, round_num) if f1_api else []
    if not stints:
        return None

    num_to_surname = {d.get("driver_number"): (d.get("last_name") or "").strip().lower()
                      for d in drivers if d.get("driver_number")}
    finish_order = [_norm_name(e.get("driver_name"))
                    for e in sorted(entries, key=lambda x: _safe_int(x.get("position"), 99) or 99)
                    if (_safe_int(e.get("position"), 99) or 99) < 90 ]
    stints_by_drv: Dict[int, List[Dict]] = defaultdict(list)
    for s in stints:
        stints_by_drv[s.get("driver_number")].append(s)
    # 完赛名次排序，缺成绩的排尾
    nums = [n for n in stints_by_drv if num_to_surname.get(n)]
    nums.sort(key=lambda n: _finish_pos(finish_order, num_to_surname[n]))

    fig, plt = _setup_dark_fig((14, max(6, len(nums) * 0.55 + 2)),
                               f"{season} {race_name} · 轮胎策略时间线" if race_name
                               else f"{season} R{round_num} 轮胎策略")
    ax = fig.add_axes([0.09, 0.08, 0.88, 0.82])
    ax.set_facecolor("#1a1d26")
    max_lap = 1
    for y, n in enumerate(nums):
        for s in sorted(stints_by_drv[n], key=lambda x: int(x.get("stint_number", 0))):
            ls = int(s.get("lap_start") or 0)
            le = int(s.get("lap_end", 0))
            max_lap = max(max_lap, le)
            comp = (s.get("compound") or "UNKNOWN").upper()
            ax.barh(y, le - ls + 1, left=ls - 1, height=0.62,
                    color=COMPOUND_COLORS.get(comp, "#888888"),
                    edgecolor="#1a1d26", linewidth=0.6)
    ax.set_yticks(range(len(nums)))
    ax.set_yticklabels([num_to_surname[n].upper()[:3] for n in nums],
                       color="#eeeeee", fontsize=10)
    ax.invert_yaxis()  # 冠军在最上
    ax.set_xlabel("圈数", color="#cccccc")
    ax.set_xlim(0, max_lap + 1)
    ax.grid(axis="x", color="#333842", linewidth=0.6, alpha=0.7)
    ax.tick_params(colors="#aaaaaa")
    for spine in ax.spines.values():
        spine.set_color("#444")
    from matplotlib.lines import Line2D
    used = { (s.get("compound") or "UNKNOWN").upper()
             for n in nums for s in stints_by_drv[n] }
    order = [c for c in ("SOFT", "MEDIUM", "HARD", "INTERMEDIATE", "WET", "UNKNOWN")
             if c in used]
    ax.legend(handles=[Line2D([0], [0], marker="s", color="none",
                              markerfacecolor=COMPOUND_COLORS[c], markersize=10,
                              label=COMPOUND_CN.get(c, c)) for c in order],
              loc="lower right", frameon=False, fontsize=10, labelcolor="#dddddd",
              ncols=len(order))
    _savefig_atomic(fig, path)
    plt.close(fig)
    logger.info(f"[chart] 策略时间线已生成: {path}")
    return path


def render_positions_chart(season: int, round_num: int, race_name: str = "",
                           f1_api=None) -> Optional[str]:
    """名次变化图：发车格→完赛 名次得/失条形图（绿升红降），按完赛名次排序"""
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    path = os.path.join(EXPORTS_DIR, f"positions_chart_{season}_R{round_num}.png")
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
    path = os.path.join(EXPORTS_DIR, f"race_report_{season}_R{round_num}.png")
    if os.path.exists(path):
        return path

    from .stint_analysis import StintAnalyzer
    analyzer = StintAnalyzer(f1_api=f1_api)
    pace, _ = _fetch_pace_laps(analyzer, season, round_num)
    sk = _openf1_session_key(analyzer, season, round_num, "Race")
    stints = analyzer._openf1_get("stints", {"session_key": sk}) if sk else []
    drivers = analyzer._openf1_get("drivers", {"session_key": sk}) if sk else []
    entries = _race_entries(f1_api, season, round_num) if f1_api else []
    if not pace or not entries:
        return None

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

    # ② 策略时间线
    ax2 = fig.add_subplot(gs[1])
    ax2.set_facecolor("#1a1d26")
    num_to_surname = {d.get("driver_number"): (d.get("last_name") or "").strip().lower()
                      for d in drivers if d.get("driver_number")}
    finish_order = [_norm_name(e.get("driver_name"))
                    for e in sorted(entries, key=lambda x: x.get("position", 99))
                    if (e.get("position") or 99) < 90]
    stints_by_drv: Dict[int, List[Dict]] = defaultdict(list)
    for s in stints:
        stints_by_drv[s.get("driver_number")].append(s)
    nums = [n for n in stints_by_drv if num_to_surname.get(n)]
    nums.sort(key=lambda n: _finish_pos(finish_order, num_to_surname[n]))
    max_lap = 1
    for y, n in enumerate(nums):
        for s in sorted(stints_by_drv[n], key=lambda x: int(x.get("stint_number", 0))):
            ls, le = int(s.get("lap_start") or 0), int(s.get("lap_end") or 0)
            max_lap = max(max_lap, le)
            comp = (s.get("compound") or "UNKNOWN").upper()
            ax2.barh(y, le - ls + 1, left=ls - 1, height=0.62,
                     color=COMPOUND_COLORS.get(comp, "#888888"),
                     edgecolor="#1a1d26", linewidth=0.6)
    ax2.set_yticks(range(len(nums)))
    ax2.set_yticklabels([num_to_surname[n].upper()[:3] for n in nums],
                        color="#eeeeee", fontsize=9)
    ax2.invert_yaxis()
    ax2.set_xlim(0, max_lap + 1)
    ax2.set_title("轮胎策略时间线", color="#dddddd", fontsize=13, loc="left")
    ax2.grid(axis="x", color="#333842", linewidth=0.6, alpha=0.7)
    ax2.tick_params(colors="#aaaaaa")
    for spine in ax2.spines.values():
        spine.set_color("#444")
    from matplotlib.lines import Line2D
    used = {(s.get("compound") or "UNKNOWN").upper()
            for n in nums for s in stints_by_drv[n]}
    order = [c for c in ("SOFT", "MEDIUM", "HARD", "INTERMEDIATE", "WET", "UNKNOWN")
             if c in used]
    if order:
        ax2.legend(handles=[Line2D([0], [0], marker="s", color="none",
                                   markerfacecolor=COMPOUND_COLORS[c], markersize=9,
                                   label=COMPOUND_CN.get(c, c)) for c in order],
                   loc="lower right", frameon=False, fontsize=9, labelcolor="#dddddd",
                   ncols=len(order))

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
                           f1_api=None) -> Optional[str]:
    """排位差距条形图：各车手排位最快圈与杆位的差值。
    数据源用归一化排位成绩（Jolpica/f1api.dev，time=最快圈）——OpenF1 部分分站
    只有正赛环节无排位（2026 雪邦实证），不可用 OpenF1 排位圈速"""
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    path = os.path.join(EXPORTS_DIR, f"quali_gap_{season}_R{round_num}.png")
    if os.path.exists(path):
        return path
    if not f1_api:
        return None
    try:
        res = f1_api.get_session_results(round_num, "qualifying", retries=1,
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
    rows = sorted(((s, v - pole) for s, v in best.items()),
                  key=lambda x: (order_hint.get(x[0], 99), x[1]))

    fig, plt = _setup_dark_fig((14, 7),
                               f"{season} {race_name} · 排位差距（与杆位）" if race_name
                               else f"{season} R{round_num} 排位差距")
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
        for sess in ("race", "sprint"):
            try:
                res = f1_api.get_session_results(rnd, sess, retries=1, season=season) or {}
            except Exception:
                continue
            for e in res.get("entries") or []:
                pts = e.get("points")
                if pts:
                    name = (e.get("driver_name") or "").split()[-1]
                    awarded[name] = awarded.get(name, 0) + float(pts)
        for n, p in awarded.items():
            if n not in cum:
                # 季中加入车手：首秀之前的轮次前部补零（此前在尾部补值会把曲线整体左移）
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
            series = series + [series[-1] if series else 0] * (len(rounds) - len(series))
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
    path = os.path.join(EXPORTS_DIR, f"pace_chart_{season}_R{round_num}.png")
    if os.path.exists(path):
        return path

    from .stint_analysis import StintAnalyzer
    analyzer = StintAnalyzer(f1_api=f1_api)
    pace, err = _fetch_pace_laps(analyzer, season, round_num)
    if not pace:
        logger.info(f"[chart] pace 数据不可用: {err}")
        return None

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
    path = os.path.join(EXPORTS_DIR, f"destructors_chart_{season}_R{n_round}.png")
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
    path = os.path.join(EXPORTS_DIR, f"pu_usage_{season}_R{n_round}.png")
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
                v = int(e["usages"].get(k) or 0)
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
                v = int(e["usages"].get(k) or 0)
            except (TypeError, ValueError):
                continue
            if lim is not None and v >= lim:
                warns.append(k)
        label = f" {e['total']}" + (f"  [超限: {','.join(warns)}]" if warns else "")
        ax.text(left + 0.05, y, label, va="center", color="#dddddd", fontsize=9)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([(e["last_name"] or "?") for e in rows], color="#eeeeee", fontsize=10)
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
    path = os.path.join(EXPORTS_DIR, f"pitstop_chart_{season}_R{round_num}.png")
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

def render_sc_timeline(season: int, round_num: int, race_name: str = "",
                       f1_api=None) -> Optional[str]:
    """安全车事件时间线（SC 黄底/VSC 蓝底/红旗红底 + 部署圈标记）。
    无事件返回 None（调用方跳过发送）"""
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    path = os.path.join(EXPORTS_DIR, f"sc_timeline_{season}_R{round_num}.png")
    if os.path.exists(path):
        return path
    from .stint_analysis import StintAnalyzer
    analyzer = StintAnalyzer(f1_api=f1_api)
    sk = _openf1_session_key(analyzer, season, round_num, "Race")
    if not sk:
        return None
    rc = analyzer._openf1_get("race_control", {"session_key": sk})
    if not rc:
        return None
    # 事件配对：deploy → in/clear/ending/resume
    events = []  # (start_lap, end_lap, kind)
    open_ev = {}
    for e in rc:
        msg = (e.get("message") or "").lower()
        lap = e.get("lap_number")
        if lap is None:
            continue
        if "virtual safety car" in msg:
            if "deployed" in msg:
                open_ev["vsc"] = lap
            elif "ending" in msg or "ending" in msg.replace("  ", " ") or "clear" in msg:
                if "vsc" in open_ev:
                    events.append((open_ev.pop("vsc"), lap, "vsc"))
        elif "safety car" in msg:
            if "deployed" in msg:
                open_ev["sc"] = lap
            elif "in this lap" in msg or "in this" in msg or "ending" in msg:
                if "sc" in open_ev:
                    events.append((open_ev.pop("sc"), lap, "sc"))
        elif "red flag" in msg:
            events.append((lap, lap + 1, "red"))
        elif "green flag" in msg or ("resume" in msg and "red" in [k for k in open_ev]):
            pass
    for k, lap in open_ev.items():  # 未配对收尾（到比赛结束）
        events.append((lap, lap + 2, k))
    if not events:
        return None

    # 规范化成绩条目无 laps 字段：改从赛程 _f1api_dev.laps 取正赛圈数，取不到再回退事件末圈+5
    total_laps = 0
    if f1_api:
        try:
            for r in (f1_api.get_schedule_for_year(season) or []):
                if int(r.get("round", 0)) == int(round_num):
                    total_laps = int((r.get("_f1api_dev") or {}).get("laps") or 0)
                    break
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


# ==================== 渲染并发保护 ====================
# pyplot 全局状态非线程安全：渲染可能由调度线程/指令线程/LLM 工具线程并发触发，
# 模块级锁串行 + finally 清理 pyplot 注册表（任何异常路径都不泄漏 figure 防内存膨胀）
import threading as _threading

_RENDER_LOCK = _threading.Lock()


def _render_guard(fn):
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        with _RENDER_LOCK:
            try:
                return fn(*args, **kwargs)
            finally:
                try:
                    from matplotlib import pyplot as _plt
                    _plt.close("all")
                except Exception:
                    pass

    return wrapper


for _pub in ("render_strategy_chart", "render_positions_chart", "render_race_report_chart",
             "render_quali_gap_chart", "render_points_trend_chart", "render_pace_chart",
             "render_team_strength_radar", "render_destructors_chart", "render_pu_usage_chart",
             "render_pitstop_chart", "render_sc_timeline"):
    if _pub in globals():
        globals()[_pub] = _render_guard(globals()[_pub])
del _pub
