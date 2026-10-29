# -*- coding: utf-8 -*-
"""Discord 原生格式化（替代复用 QQGroupBot 的硬编码北京时间格式化器）

核心：所有时间用 Discord 原生 <t:epoch> 时间戳——按每个用户自己设备的
时区/语言自动渲染，海外多时区服务器无需任何设置即"各自看本地时间"。
（Discord 不提供用户时区 API，<t:> 就是时区识别的正解；纯文本场景由
 /tz 用户偏好 + /ask 注入用户时区兜底）
"""
from datetime import datetime, timezone as _tzu
from typing import Any, Dict, List, Optional

from i18n import T, session_name

_ICONS = {"fp1": "🏎️", "fp2": "🏎️", "fp3": "🏎️", "qualifying": "⏱️",
          "sprint": "⚡", "sprint_qualifying": "⏱️", "race": "🏁"}
_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _ts(dt: datetime, style: str = "F") -> str:
    return f"<t:{int(dt.timestamp())}:{style}>"


def _race_dt(race: Dict[str, Any]) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(
            f"{race['date']}T{race.get('time', '00:00:00Z').replace('Z', '')}"
        ).replace(tzinfo=_tzu.utc)
    except Exception:
        return None


def fmt_next_race(race: Dict[str, Any], sessions: List[Dict[str, Any]]) -> str:
    """下一站详情（标准 Discord markdown，直接作为消息文本/Embed description）"""
    name = race.get("raceName", "")
    circuit = race.get("Circuit", {}).get("circuitName", "")
    loc = race.get("Circuit", {}).get("Location", {})
    head = (f"## 🏁 {T('next_title', race=name)}\n"
            f"> 📍 {circuit} · {loc.get('locality', '')}, {loc.get('country', '')}\n")
    lines = []
    for s in sessions:
        icon = _ICONS.get(s["type"], "🏎️")
        lines.append(f"{icon} **{session_name(s)}** — {_ts(s['datetime'], 'F')} ({_ts(s['datetime'], 'R')})")
    return head + "\n".join(lines)


def fmt_calendar(schedule: List[Dict[str, Any]]) -> str:
    """全年赛历（<t:D> 日期戳；✅已结束/🔜未开始）"""
    now = datetime.now(_tzu.utc)
    lines = [f"## 📅 {T('calendar_title')}", ""]
    for race in schedule:
        rnd = race.get("round", "")
        name = race.get("raceName", "").replace("Formula 1 ", "")
        sprint = " ⚡" if "Sprint" in race else ""
        dt = _race_dt(race)
        if dt:
            status = "✅" if dt < now else "🔜"
            when = _ts(dt, "D")
        else:
            status, when = "", race.get("date", "")
        lines.append(f"{status} **R{rnd}** {name}{sprint} — {when}")
    lines.append("")
    lines.append(T("calendar_legend"))
    return "\n".join(lines)


def _weather_line(s: Dict[str, Any], label: str = "") -> str:
    """单时段天气摘要（双语）"""
    parts = []
    if s.get("weather_desc"):
        parts.append(s["weather_desc"])
    if s.get("air_temp"):
        parts.append(T("wx_air", lo=s["air_temp"]["min"], hi=s["air_temp"]["max"]))
    if s.get("track_temp_measured"):
        tt = s["track_temp_measured"]
        parts.append(T("wx_track_measured", lo=tt["min"], hi=tt["max"]))
    elif s.get("track_temp_estimated"):
        tt = s["track_temp_estimated"]
        parts.append(T("wx_track_est", lo=tt["min"], hi=tt["max"]))
    elif s.get("track_temp_proxy"):
        tt = s["track_temp_proxy"]
        parts.append(T("wx_track_proxy", lo=tt["min"], hi=tt["max"]))
    if s.get("precip_probability_max") is not None:
        rain = T("wx_rain", p=s["precip_probability_max"])
        if s.get("rain_eta_minutes") is not None and s["rain_eta_minutes"] >= 0:
            rain += T("wx_rain_eta", m=s["rain_eta_minutes"])
        parts.append(rain)
    if s.get("wind_max_kmh") is not None:
        wd = s.get("wind_direction", "")
        parts.append(T("wx_wind", wd=wd, lo=s.get("wind_min_kmh", "?"),
                       hi=s["wind_max_kmh"]).replace("  ", " "))
    return (f"{label}: " if label else "") + ", ".join(parts)


def fmt_weather(result: Dict[str, Any]) -> str:
    """分站天气分析包（各环节 <t:> 时间戳 + 天气/沥青/风）"""
    race = result.get("race", "")
    head = (f"## 🌤️ {T('weather_title', race=race)}\n"
            f"> 📍 {result.get('circuit', '')} · {result.get('location', '')} · {result.get('data_kind', '')}\n\n")
    lines = []
    if result.get("current"):
        lines.append(f"**{_weather_line(result['current'], T('weather_current'))}**\n")
    for s in result.get("sessions", []):
        when = ""
        if s.get("datetime"):
            try:
                dt = datetime.fromisoformat(s["datetime"])
                if dt.tzinfo is None:
                    # 数据源 naive 时间按北京时间处理（与 QQ 版口径一致）
                    from pytz import timezone as _ptz
                    dt = _ptz("Asia/Shanghai").localize(dt)
                when = f" — {_ts(dt, 'f')}"
            except Exception:
                pass
        lines.append(_weather_line(s, f"**{s.get('session', '')}**{when}"))
    lines.append(f"\n> {T('weather_note')}")
    return head + "\n".join(lines)


def fmt_gp_summary(race: Dict[str, Any], year: int, session_results: List[tuple]) -> str:
    """分站各环节成绩汇总（每环节前三名）"""
    race_name = race.get("raceName", "")
    circuit = race.get("Circuit", {}).get("circuitName", "")
    lines = [f"## 🏁 {race_name}", f"> 📍 {circuit} · {year}", ""]
    has_data = False
    for sname, top3 in session_results:
        if not top3:
            continue
        has_data = True
        names = " ".join(
            f"{_MEDALS[i + 1]}{(e.get('driver_name') or (e.get('Driver') or {}).get('familyName') or '?')}"
            for i, e in enumerate(top3[:3]))
        lines.append(f"**{sname}**\n{names}")
    if not has_data:
        lines.append(T("no_data"))
    lines.append("")
    lines.append(T("gp_summary_hint"))
    return "\n".join(lines)
