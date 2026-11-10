import os
from typing import Optional

from .core import (
    CHART_VER,
    COMPOUND_CN,
    COMPOUND_COLORS,
    EXPORTS_DIR,
    _boxplot_compat,
    _fetch_pace_laps,
    _race_entries,
    _safe_int,
    _setup_dark_fig,
    _strategy_stints,
    _style_ax,
    logger,
)


def render_race_report_chart(season: int, round_num: int, race_name: str = "",
                             f1_api=None) -> Optional[str]:
    """赛后三联图：pace 分布箱线 + 轮胎策略时间线 + 名次变化（一张图一次发送）"""
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    path = os.path.join(EXPORTS_DIR, f"race_report_{CHART_VER}_{season}_R{round_num}.png")
    if os.path.exists(path):
        return path

    from ..stint_analysis import StintAnalyzer
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
        path = path.replace(".png", f"_fb{_dt.now().strftime('%m%d')}.png")
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
    _boxplot_compat(ax1, data, horizontal=False, positions=range(len(names)), widths=0.55,
                patch_artist=True, showfliers=False,
                medianprops={"color": "#ff8c00", "linewidth": 2},
                whiskerprops={"color": "#cccccc", "linewidth": 1},
                capprops={"color": "#cccccc"},
                boxprops={"facecolor": "#2a2e3d", "edgecolor": "#cccccc", "alpha": 0.8})
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
    finish_order = [(e.get("driver_name") or "").split()[-1].lower()
                    for e in sorted(entries, key=lambda x: _safe_int(x.get("position"), 99) or 99)
                    if (_safe_int(e.get("position"), 99) or 99) < 90]
    strat = strat or {}
    nums = list(strat.keys())
    nums.sort(key=lambda n: (finish_order.index(n) if n in finish_order else 99))
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

    fig.savefig(path, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    logger.info(f"[chart] 赛后三联图已生成: {path}")
    return path
