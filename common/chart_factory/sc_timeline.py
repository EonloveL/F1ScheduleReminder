import os
from typing import Any, Dict, List, Optional

from .core import (
    logger,
    EXPORTS_DIR,
    _setup_dark_fig,
    _openf1_session_key,
    _race_entries,
    _safe_int,
)


def get_sc_events(season: int, round_num: int, f1_api=None) -> Optional[List[Dict[str, Any]]]:
    """安全车事件结构化提取（OpenF1 race_control 主源，livetiming 兜底）：
    部署→结束配对为 (start_lap, end_lap, kind, message)，红旗单独记录。
    供 /sc 指令、赛后推送图、LLM 工具 get_safety_car_events 共用。
    返回 None = 双源皆不可达（数据不可用，禁止断言"本站干净"）；
    返回 [] = 有数据源但确实无事件（可断言干净）"""
    from ..stint_analysis import StintAnalyzer
    analyzer = StintAnalyzer(f1_api=f1_api)
    sk = _openf1_session_key(analyzer, season, round_num, "Race")
    rc = analyzer._openf1_get("race_control", {"session_key": sk}) if sk else []
    source_ok = bool(rc)  # OpenF1 有响应（哪怕空事件列表也算有数据）
    if not rc:
        # livetiming 归档兜底（OpenF1 401/赛中锁定时）
        try:
            from ..livetiming_client import LiveTimingClient
            rc = LiveTimingClient(f1_api=analyzer.f1_api).get_race_control(season, round_num)
            source_ok = source_ok or rc is not None  # None=不可达；[]=有源无事件
        except Exception:
            pass
    if not source_ok:
        return None
    rc = rc or []
    events: List[Dict[str, Any]] = []
    open_ev: Dict[str, tuple] = {}  # kind -> (start_lap, deploy_message)
    for e in rc:
        msg = (e.get("message") or "").lower()
        raw_msg = e.get("message") or ""
        lap = _safe_int(e.get("lap_number"), None)
        if lap is None:
            continue
        # 优先用 flag 字段判定红旗（OpenF1 race_control 有 flag 字段；
        # livetiming 无该字段时退回消息文本判定，排除 chequered/checkered 子串误标）
        is_red = (str(e.get("flag") or "").upper() == "RED"
                  or ("red flag" in msg and "chequered" not in msg
                      and "checkered" not in msg))
        if "virtual safety car" in msg:
            if "deployed" in msg:
                if "vsc" in open_ev:
                    s0, m0 = open_ev.pop("vsc")
                    events.append({"kind": "vsc", "start_lap": s0, "end_lap": s0 + 2,
                                   "message": m0})  # 前段未闭合先落估算段
                    logger.warning(f"[chart] SC 事件同 kind 二次 deploy，前段按估算闭合: L{s0}")
                open_ev["vsc"] = (lap, raw_msg)
            elif "ending" in msg or "clear" in msg:
                if "vsc" in open_ev:
                    s, m = open_ev.pop("vsc")
                    events.append({"kind": "vsc", "start_lap": s, "end_lap": lap, "message": m})
        elif is_red:
            # 红旗：同一红旗可能多条电文（部署+恢复说明），相邻圈去重
            if events and events[-1]["kind"] == "red" \
                    and abs(events[-1]["start_lap"] - lap) <= 1:
                events[-1]["end_lap"] = max(events[-1]["end_lap"], lap + 1)
            else:
                events.append({"kind": "red", "start_lap": lap, "end_lap": lap + 1,
                               "message": raw_msg})
        elif "safety car" in msg:
            if "deployed" in msg:
                if "sc" in open_ev:
                    s0, m0 = open_ev.pop("sc")
                    events.append({"kind": "sc", "start_lap": s0, "end_lap": s0 + 2,
                                   "message": m0})
                    logger.warning(f"[chart] SC 事件同 kind 二次 deploy，前段按估算闭合: L{s0}")
                open_ev["sc"] = (lap, raw_msg)
            elif "in this" in msg or "ending" in msg or "clear" in msg:
                if "sc" in open_ev:
                    s, m = open_ev.pop("sc")
                    events.append({"kind": "sc", "start_lap": s, "end_lap": lap, "message": m})
    for k, (lap, m) in open_ev.items():  # 未配对收尾（到比赛结束）
        events.append({"kind": k, "start_lap": lap, "end_lap": lap + 2, "message": m})
    events.sort(key=lambda x: x["start_lap"])
    return events


def render_sc_timeline(season: int, round_num: int, race_name: str = "",
                       f1_api=None) -> Optional[str]:
    """安全车事件时间线（SC 黄底/VSC 蓝底/红旗红底 + 部署圈标记）。
    无事件返回 None（调用方跳过发送）"""
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    path = os.path.join(EXPORTS_DIR, f"sc_timeline_{season}_R{round_num}.png")
    if os.path.exists(path):
        return path
    evts = get_sc_events(season, round_num, f1_api)
    if not evts:
        return None
    events = [(e["start_lap"], e["end_lap"], e["kind"]) for e in evts]

    entries = _race_entries(f1_api, season, round_num) if f1_api else []
    total_laps = max((int(e.get("laps") or 0) for e in entries), default=0) or \
        max(e[1] for e in events) + 5
    fig, plt = _setup_dark_fig((14, 4.2))
    ax = fig.add_axes([0.05, 0.18, 0.92, 0.6])
    ax.set_facecolor("#1a1d26")
    kind_style = {"sc": ("#ffd700", "安全车 SC"), "vsc": ("#00a3e0", "虚拟安全车 VSC"),
                  "red": ("#ff3333", "红旗")}
    for s, e, kind in events:
        color, _ = kind_style.get(kind, ("#888", "?"))
        ax.axvspan(s, e, color=color, alpha=0.35)
        ax.axvline(s, color=color, linewidth=2)
        ax.text(s, 0.6, f"L{s}", color=color, fontsize=10, fontweight="bold",
                ha="center")
    ax.set_xlim(0, total_laps)
    ax.set_ylim(0, 1)
    ax.set_yticks([])
    ax.set_xlabel("圈数", color="#cccccc")
    ax.tick_params(colors="#aaaaaa")
    for spine in ax.spines.values():
        spine.set_color("#444")
    from matplotlib.lines import Line2D
    used = sorted({k for _, _, k in events})
    ax.legend(handles=[Line2D([0], [0], marker="s", color="none",
                              markerfacecolor=kind_style[k][0], markersize=10,
                              label=kind_style[k][1]) for k in used],
              loc="upper right", frameon=False, fontsize=10, labelcolor="#dddddd",
              ncols=len(used))
    n_sc = sum(1 for _, _, k in events if k == "sc")
    n_vsc = sum(1 for _, _, k in events if k == "vsc")
    n_red = sum(1 for _, _, k in events if k == "red")
    fig.suptitle(f"{season} {race_name} · 安全车事件时间线"
                 f"（SC×{n_sc} VSC×{n_vsc} 红旗×{n_red}）",
                 color="#f0f0f0", fontsize=14, fontweight="bold", x=0.05, ha="left")
    fig.savefig(path, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    logger.info(f"[chart] 安全车时间线已生成: {path}")
    return path
