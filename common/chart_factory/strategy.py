import os
from typing import Optional

from .core import (
    CHART_VER,
    COMPOUND_CN,
    COMPOUND_COLORS,
    EXPORTS_DIR,
    _race_entries,
    _safe_int,
    _setup_dark_fig,
    _strategy_stints,
    logger,
)


def render_strategy_chart(season: int, round_num: int, race_name: str = "",
                          f1_api=None) -> Optional[str]:
    """轮胎策略时间线：横轴圈数、纵轴车手（按完赛名次），stint 色块按配方着色"""
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    path = os.path.join(EXPORTS_DIR, f"strategy_chart_{CHART_VER}_{season}_R{round_num}.png")
    if os.path.exists(path):
        return path
    from ..stint_analysis import StintAnalyzer
    analyzer = StintAnalyzer(f1_api=f1_api)
    entries = _race_entries(f1_api, season, round_num) if f1_api else []
    data = _strategy_stints(analyzer, season, round_num, entries)
    if not data:
        return None
    # Jolpica 兜底渲染（无配方全灰）：写日期戳 fb 文件名，OpenF1 恢复后仍可生成正式版
    _is_fb = all(s["compound"] == "UNKNOWN" for lst in data.values() for s in lst)
    if _is_fb:
        from datetime import datetime as _dt
        path = path.replace(".png", f"_fb{_dt.now().strftime('%m%d')}.png")
        if os.path.exists(path):
            return path

    finish_order = [(e.get("driver_name") or "").split()[-1].lower()
                    for e in sorted(entries, key=lambda x: _safe_int(x.get("position"), 99) or 99)
                    if (_safe_int(e.get("position"), 99) or 99) < 90]
    # 完赛名次排序，缺成绩的排尾
    names = list(data.keys())
    names.sort(key=lambda n: (finish_order.index(n) if n in finish_order else 99))

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
    fig.savefig(path, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    logger.info(f"[chart] 策略时间线已生成: {path}")
    return path
