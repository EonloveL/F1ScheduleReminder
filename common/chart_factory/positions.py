import os
from typing import Optional

from .core import (
    EXPORTS_DIR,
    _race_entries,
    _safe_int,
    _setup_dark_fig,
    logger,
)


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
    fig.savefig(path, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    logger.info(f"[chart] 名次变化图已生成: {path}")
    return path
