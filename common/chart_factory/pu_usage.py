import os
from typing import Dict, Optional

from .core import (
    logger,
    EXPORTS_DIR,
    _setup_dark_fig,
)


def render_pu_usage_chart(season: int, f1cosmos=None) -> Optional[str]:
    """动力单元部件用量堆叠柱图（每车手一行横条，达赛季上限标 ⚠️）"""
    if not f1cosmos:
        return None
    elements = f1cosmos.get_elements(season)
    if not elements:
        return None
    from ..f1cosmos_api import get_pu_limits
    pu_limits = get_pu_limits(season)
    # 每车手取最新 round 快照（与 format_pu_quota 同口径）
    latest: Dict[str, Dict] = {}
    for e in elements:
        key = e["last_name"] or e.get("tla") or ""
        if key and (key not in latest or e["round"] >= latest[key]["round"]):
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
    cols = [k for k in order if any(k in e["usages"] for e in latest.values())]
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
    fig.savefig(path, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    logger.info(f"[chart] PU 用量图已生成: {path}")
    return path
