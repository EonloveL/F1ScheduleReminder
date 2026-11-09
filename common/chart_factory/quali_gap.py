import os
from typing import Dict, Optional

from .core import (
    logger,
    EXPORTS_DIR,
    _setup_dark_fig,
    _safe_int,
)


def render_quali_gap_chart(season: int, round_num: int, race_name: str = "",
                           f1_api=None, session_type: str = "qualifying") -> Optional[str]:
    """排位/冲刺排位差距条形图：各车手排位最快圈与杆位的差值。
    数据源用归一化排位成绩（Jolpica/f1api.dev，time=最快圈）——OpenF1 部分分站
    只有正赛无排位（2026 雪邦实证），不可用 OpenF1 排位圈速。
    session_type: qualifying（正赛排位）/ sprint_qualifying（冲刺排位），
    冲刺排位文件名带 _sq 后缀避免同轮覆盖"""
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    suffix = "_sq" if session_type == "sprint_qualifying" else ""
    path = os.path.join(EXPORTS_DIR, f"quali_gap_{season}_R{round_num}{suffix}.png")
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
    rows = sorted(((s, v - pole) for s, v in best.items()),
                  key=lambda x: (order_hint.get(x[0], 99), x[1]))

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
    fig.savefig(path, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    logger.info(f"[chart] 排位差距图已生成: {path}")
    return path
