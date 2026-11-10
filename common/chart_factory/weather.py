import os
from typing import Optional

from .core import (
    CHART_VER,
    EXPORTS_DIR,
    _openf1_session_key,
    _setup_dark_fig,
    logger,
)


def render_weather_chart(season: int, round_num: int, race_name: str = "",
                         f1_api=None) -> Optional[str]:
    """正赛天气时间序列：气温/赛道温度双线（左轴）+ 降雨色带+风速。
    已完赛：OpenF1 weather 分钟级实测（2023+）；未赛：Open-Meteo 逐小时预报
    （气温 + 沥青温度回归模型估算 + 降水概率%）。"""
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    path = os.path.join(EXPORTS_DIR, f"weather_chart_{CHART_VER}_{season}_R{round_num}.png")
    if os.path.exists(path):
        return path  # 实测版永久复用
    from ..stint_analysis import StintAnalyzer
    analyzer = StintAnalyzer(f1_api=f1_api)
    sk = _openf1_session_key(analyzer, season, round_num, "Race")
    rows = analyzer._openf1_get("weather", {"session_key": sk}) if sk else []
    if rows and len(rows) >= 10:
        measured = _weather_chart_openf1(rows, path, season, round_num, race_name)
        if measured:
            return measured
        # 实测有数据但过滤后不可用（传感器整场掉线）：穿透到归档/预报
    # 预报版独立文件名（带日期戳防陈旧），赛后实测版生成后走实测文件名，互不挡路
    from datetime import datetime as _dt
    fc_path = os.path.join(
        EXPORTS_DIR,
        f"weather_fc_{season}_R{round_num}_{_dt.now().strftime('%m%d')}.png")
    if os.path.exists(fc_path):
        return fc_path
    return _weather_chart_forecast(season, round_num, race_name, f1_api, fc_path)


def _weather_chart_forecast(season: int, round_num: int, race_name: str,
                            f1_api, path: str) -> Optional[str]:
    """未赛分站预报图：Open-Meteo 逐小时（气温/降水概率/风速 + 沥青温度回归估算）"""
    if not f1_api:
        return None
    try:
        schedule = f1_api.get_schedule_for_year(season) or []
        race = next((r for r in schedule
                     if int(r.get("round", 0)) == round_num), None)
        if not race:
            return None
        loc = (race.get("Circuit", {}) or {}).get("Location", {}) or {}
        lat, lon = float(loc.get("lat")), float(loc.get("long"))
        from datetime import datetime as _dt
        from datetime import timedelta as _td
        from datetime import timezone as _tz
        rd = _dt.strptime(race.get("date", ""), "%Y-%m-%d").replace(tzinfo=_tz.utc)
        from ..weather_api import WeatherAPI
        hourly = WeatherAPI()._fetch_hourly(
            lat, lon, rd - _td(days=1), rd + _td(days=1),
            cache_key=f"weather_chart_fc_{season}_{round_num}", ttl_hours=3)
        if not hourly or "hourly" not in hourly:
            return None
        h = hourly["hourly"]
        times = [_dt.fromtimestamp(t, tz=_tz.utc) for t in h["time"]]
        if not times:
            return None
        t0 = times[0]
        xs = [((t - t0).total_seconds() / 3600) for t in times]
        air = h.get("temperature_2m") or []
        precip = h.get("precipitation_probability") or [0] * len(xs)
        wind = h.get("wind_speed_10m") or [0] * len(xs)
        soil = h.get("soil_temperature_0cm") or []
        rad = h.get("shortwave_radiation") or []
        from ..weather_api import ASPHALT_MODEL
        track = []
        for i in range(len(xs)):
            a = air[i] if i < len(air) else None
            s = soil[i] if i < len(soil) else None
            r = rad[i] if i < len(rad) else None
            if a is None:
                track.append(None)
                continue
            track.append(ASPHALT_MODEL["intercept"] + ASPHALT_MODEL["air"] * a
                         + (ASPHALT_MODEL["soil"] * s if s is not None else 0)
                         + (ASPHALT_MODEL["radiation"] * r if r is not None else 0))
        return _weather_chart_draw(xs, air, track, precip, wind, path,
                                   f"{season} {race_name} · 比赛周天气预报（逐小时）",
                                   xlabel="距窗口开始（小时）", precip_is_prob=True)
    except Exception as e:
        logger.warning(f"[chart] 天气预报图生成失败: {e}")
        return None


def _weather_chart_draw(xs, air, track, rain, wind, path, title,
                        xlabel, precip_is_prob=False) -> str:
    """共用绘制：温度双线 + 降水（实测0/1色带 或 预报概率%）+ 风速"""
    fig, plt = _setup_dark_fig((14, 6.5))
    ax = fig.add_axes([0.06, 0.11, 0.86, 0.76])
    ax.set_facecolor("#1a1d26")
    ax.plot(xs, track, color="#ff5252", linewidth=1.8,
            label="沥青温度（估算模型）" if precip_is_prob else "赛道温度")
    ax.plot(xs, air, color="#00a3e0", linewidth=1.8, label="气温")
    ax.plot(xs, wind, color="#888888", linewidth=1.0, alpha=0.7, label="风速(m/s)")
    ax.set_xlabel(xlabel, color="#cccccc")
    ax.set_ylabel("温度 (°C)", color="#cccccc")
    ax.grid(axis="y", color="#333842", linewidth=0.6, alpha=0.7)
    ax.tick_params(colors="#aaaaaa")
    for spine in ax.spines.values():
        spine.set_color("#444")

    ax2 = ax.twinx()
    ax2.set_facecolor("none")
    if precip_is_prob:
        # 预报：降水概率% 半透明色带
        ax2.fill_between(xs, 0, rain, color="#39b54a", alpha=0.20, label="降水概率%")
        ax2.set_ylim(0, 100)
        ax2.set_yticks([])
    else:
        # 实测：0/1 降雨标记压低到 25% 高度色带
        ax2.bar(xs, rain, width=1.0, color="#39b54a", alpha=0.22, label="降雨")
        ax2.set_ylim(0, max(max(rain) * 4.0, 1))
        ax2.set_yticks([])
    ax2.spines["right"].set_color("#39b54a")

    h1, l1 = ax.get_legend_handles_labels()
    h2, l2 = ax2.get_legend_handles_labels()
    ax.legend(h1 + h2, l1 + l2, loc="upper left", frameon=False, fontsize=10,
              labelcolor="#dddddd", ncols=4)
    fig.suptitle(title, color="#f0f0f0", fontsize=15, fontweight="bold",
                 x=0.06, ha="left")
    fig.savefig(path, facecolor=fig.get_facecolor(), bbox_inches="tight")
    plt.close(fig)
    logger.info(f"[chart] 天气时间序列图已生成: {path}")
    return path


def _weather_chart_openf1(rows, path, season, round_num, race_name) -> Optional[str]:
    """已完赛分站：OpenF1 分钟级实测绘制"""
    from datetime import datetime as _dt
    t0 = None
    xs, air, track, rain, wind = [], [], [], [], []
    for r in rows:
        try:
            t = _dt.fromisoformat(str(r["date"]).replace("Z", "+00:00"))
        except (ValueError, KeyError):
            continue
        # 传感器掉线会产生 0/None 假值（竖直跳水线），剔除
        air_t, track_t = r.get("air_temperature"), r.get("track_temperature")
        if not air_t or not track_t or air_t <= 0 or track_t <= 5:
            continue
        if t0 is None:
            t0 = t
        xs.append((t - t0).total_seconds() / 60)
        air.append(air_t)
        track.append(track_t)
        rain.append(r.get("rainfall") or 0)
        wind.append(r.get("wind_speed") or 0)
    if len(xs) < 10:
        return None  # 过滤后采样不足（传感器整场掉线），不出空图也不落盘
    rained = sum(1 for r in rain if r)
    title = (f"{season} {race_name} · 正赛天气时间序列" if race_name
             else f"{season} R{round_num} 正赛天气")
    title += f"（有降雨 {rained} 个采样点）" if rained else "（无降雨）"
    return _weather_chart_draw(xs, air, track, rain, wind, path, title,
                               xlabel="距首个有效采样（分钟）", precip_is_prob=False)
