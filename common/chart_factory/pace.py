import os
from typing import Optional

from .core import (
    CHART_VER,
    COMPOUND_CN,
    COMPOUND_COLORS,
    EXPORTS_DIR,
    _boxplot_compat,
    _fetch_pace_laps,
    logger,
)


def render_pace_chart(season: int, round_num: int, race_name: str = "",
                      f1_api=None) -> Optional[str]:
    """渲染 pace 分布图 → PNG 路径，失败 None。已生成直接复用（完赛数据不可变）"""
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    path = os.path.join(EXPORTS_DIR, f"pace_chart_{CHART_VER}_{season}_R{round_num}.png")
    if os.path.exists(path):
        return path

    from ..stint_analysis import StintAnalyzer
    analyzer = StintAnalyzer(f1_api=f1_api)
    pace, err = _fetch_pace_laps(analyzer, season, round_num)
    if not pace:
        logger.info(f"[chart] pace 数据不可用: {err}")
        return None
    # Jolpica 兜底渲染（无配方全灰）：写日期戳 fb 文件名，OpenF1 恢复后仍可生成正式版
    _is_fb = all(c == "UNKNOWN" for laps in pace.values() for _, c in laps)
    if _is_fb:
        from datetime import datetime as _dt
        path = path.replace(".png", f"_fb{_dt.now().strftime('%m%d')}.png")
        if os.path.exists(path):
            return path

    import matplotlib
    matplotlib.use("Agg")
    import numpy as np
    from matplotlib import pyplot as plt

    from .core import _pick_cjk_font
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
    _boxplot_compat(ax, data, horizontal=False, positions=range(len(names)),
                    widths=0.55, patch_artist=True, showfliers=False,
                    medianprops={"color": "#ff8c00", "linewidth": 2},
                    whiskerprops={"color": "#cccccc", "linewidth": 1},
                    capprops={"color": "#cccccc"},
                    boxprops={"facecolor": "#2a2e3d", "edgecolor": "#cccccc", "alpha": 0.8})
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

    fig.savefig(path, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    logger.info(f"[chart] pace 分布图已生成: {path}")
    return path
