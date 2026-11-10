import os
from collections import defaultdict
from typing import Dict, List, Optional

from .core import (
    EXPORTS_DIR,
    _boxplot_compat,
    _race_entries,
    _setup_dark_fig,
    logger,
)


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
    _boxplot_compat(ax, [v for _, v in stats], horizontal=True, positions=range(len(stats)),
               widths=0.55, patch_artist=True, showfliers=True,
               medianprops={"color": "#ff8c00", "linewidth": 2},
               whiskerprops={"color": "#cccccc"}, capprops={"color": "#cccccc"},
               boxprops={"facecolor": "#2a2e3d", "edgecolor": "#cccccc", "alpha": 0.85},
               flierprops={"marker": "o", "markerfacecolor": "#666", "markersize": 4})
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
    fig.savefig(path, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    logger.info(f"[chart] 进站时长图已生成: {path}")
    return path
