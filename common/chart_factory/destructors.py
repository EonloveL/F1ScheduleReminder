import os
from collections import defaultdict
from typing import Optional

from .core import (
    logger,
    EXPORTS_DIR,
    _setup_dark_fig,
)


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
        by_driver[r["driver"].title()] += r["cost"]
        by_team[r["team"]] += r["cost"]
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
    fig.savefig(path, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    logger.info(f"[chart] 事故成本图已生成: {path}")
    return path
