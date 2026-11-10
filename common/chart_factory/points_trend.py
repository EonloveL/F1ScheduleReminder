import os
from typing import Dict, List, Optional

from .core import (
    EXPORTS_DIR,
    _setup_dark_fig,
    logger,
)


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
            cum[n] = cum.get(n, 0) + p
        for n in cum:
            totals.setdefault(n, []).append(cum[n])
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
    fig.savefig(path, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    logger.info(f"[chart] 积分走势图已生成: {path}")
    return path
