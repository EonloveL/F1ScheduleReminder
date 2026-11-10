import os
from typing import Optional

from .core import (
    EXPORTS_DIR,
    _setup_dark_fig,
    logger,
)


def render_team_strength_radar(season: int, f1_api=None, f1cosmos=None) -> Optional[str]:
    """车队实力雷达图（6维，min-max 归一化到全围场，外圈=更强）。
    数据随赛季进程自动更新（TeamStrengthAnalyzer.compute 按累计成绩+升级件重算，
    缓存 24h）；文件名含最近完赛轮次，每站后 URL 变化自动破 QQ 缓存"""
    if not f1_api:
        return None
    from ..team_strength import TeamStrengthAnalyzer
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
    fig.savefig(path, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    logger.info(f"[chart] 车队雷达图已生成: {path}")
    return path
