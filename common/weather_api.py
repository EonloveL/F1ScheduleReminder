"""
F1赛程提醒机器人 - 天气数据模块（默认 Open-Meteo，免费无鉴权）

用途：为 Agent 预测分析提供比赛时段的赛道当地天气：
- 未来比赛（16天内）：逐小时预报 + 15 分钟级降水/降水概率 + 实时 current 块
- 历史比赛：Open-Meteo 归档实测（同样字段）
- 降水时序："正赛开始多久后可能降水"（优先 15 分钟级精确 ETA）
- 赛道级精度：坐标取自赛历赛道经纬度；elevation 参数修正模型海拔偏差
  （data/circuit_elevation.json 可编辑，山区/高原赛道关键）

可配置数据源（GitHub 用户自助）：
- WEATHER_BASE_URL / WEATHER_ARCHIVE_URL：指向任意 Open-Meteo 兼容端点
  （自建 Open-Meteo 或返回同构 JSON 的代理/商业 API），未配置用官方源
- WEATHER_MODEL：Open-Meteo models 参数（如 ecmwf_ifs04、gfs_seamless、
  icon_seamless；缺省 best_match 自动选最优模型组合）
- WEATHER_ELEVATION：全局海拔覆盖（米）；缺省按 data/circuit_elevation.json
  查赛道，查不到则用模型默认海拔
- 任一配置失效/请求失败 → 自动回退默认 Open-Meteo 源
"""

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
DEFAULT_ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

HOURLY_VARS = ("temperature_2m,apparent_temperature,precipitation_probability,"
               "precipitation,weather_code,wind_speed_10m,wind_direction_10m,"
               "soil_temperature_0cm,soil_temperature_0_to_7cm,shortwave_radiation")

# 15 分钟级（雷达外推/临近预报分辨率）——雨战"瞬间降雨"判定用
MINUTELY_VARS = "precipitation,precipitation_probability,weather_code"
CURRENT_VARS = ("temperature_2m,apparent_temperature,precipitation,weather_code,"
                "wind_speed_10m,wind_direction_10m,relative_humidity_2m")

# 逐环节天气窗口（秒，均含开始前 30 分钟）
# race 3h：安全车/VSC/红旗/雨战发车延迟都可能大幅延长（2024 巴西站排位/正赛顺延实证）；
# 排位类 2h：多次红旗/雨战中断常见；冲刺 1.5h；练习赛 75min（60min+余量）
SESSION_WINDOW_SECONDS = {
    "race": 10800,
    "qualifying": 7200,
    "sprint_qualifying": 7200,
    "sprint": 5400,
    "fp1": 4500, "fp2": 4500, "fp3": 4500,
}
DEFAULT_WINDOW_SECONDS = 5400

# 沥青赛道温度估算模型（线性回归，2023-2025 全年分站 Race/Qualifying 共 122 组
# F1 livetiming 实测沥青温度 vs Open-Meteo 归档土壤表层温度/气温/短波辐射拟合）：
#   track ≈ INTERCEPT + C_SOIL*soil + C_AIR*air + C_RAD*shortwave_radiation
# 拟合质量: R²=0.813, RMSE≈3.9°C, MAE≈2.9°C, P80误差≈4.6°C（辐射是关键变量，仅土壤 R² 仅 0.51）
ASPHALT_MODEL = {"intercept": 1.4339, "soil": 0.472, "air": 0.6139, "radiation": 0.018}
ASPHALT_MODEL_NOTE = "沥青温度估算模型：2023-2025共122组实测拟合（R²=0.81，RMSE≈3.9°C），基于土壤表层温度+气温+短波辐射"

_ELEVATION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "circuit_elevation.json")


def _parse_dt(text) -> Optional[datetime]:
    """ISO 时间字符串 → aware UTC datetime（naive 按 UTC 解释，全模块统一口径）"""
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(str(text))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _forecast_url() -> str:
    return (os.getenv("WEATHER_BASE_URL", "").strip() or DEFAULT_FORECAST_URL)


def _archive_url() -> str:
    return (os.getenv("WEATHER_ARCHIVE_URL", "").strip() or DEFAULT_ARCHIVE_URL)


_elev_cache = {"mtime": None, "data": {}}


def load_elevations() -> Dict[str, float]:
    """读取 data/circuit_elevation.json（可编辑；按 mtime 缓存，失败返回空）"""
    try:
        mt = os.path.getmtime(_ELEVATION_PATH)
        if _elev_cache["mtime"] == mt:
            return _elev_cache["data"]
        with open(_ELEVATION_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        data = {k: float(v) for k, v in raw.items()
                if not str(k).startswith("_") and isinstance(v, (int, float))}
        _elev_cache.update(mtime=mt, data=data)
        return data
    except Exception:
        return {}



def estimate_track_temp(soil: float, air: float, radiation: float) -> float:
    """由土壤表层温度/气温/短波辐射估算沥青赛道温度（°C）"""
    m = ASPHALT_MODEL
    return m["intercept"] + m["soil"] * soil + m["air"] * air + m["radiation"] * radiation

# WMO 天气代码 -> 中文
WMO_CN = {
    0: "晴", 1: "大致晴", 2: "局部多云", 3: "阴",
    45: "雾", 48: "冻雾",
    51: "毛毛雨", 53: "毛毛雨", 55: "较强毛毛雨",
    56: "冻毛毛雨", 57: "冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷暴", 96: "雷暴伴冰雹", 99: "强雷暴伴冰雹",
}

# 预报 API 最远可报天数
FORECAST_MAX_DAYS = 16


class WeatherAPI:
    """Open-Meteo 天气封装（免费无鉴权；含本地缓存）"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "F1-Reminder-Bot/1.0"})
        try:
            from .f1_api import LocalCache
            self.cache = LocalCache()
        except Exception:
            self.cache = None

    # ==================== 基础请求 ====================

    def _fetch_hourly(self, lat: float, lon: float, start: datetime, end: datetime,
                      cache_key: str = None, ttl_hours: float = 3,
                      force_refresh: bool = False,
                      elevation: float = None) -> Optional[Dict[str, Any]]:
        """抓取指定时间窗数据（未来走 forecast、过去走 archive，自动判断）

        Args:
            force_refresh: 跳过缓存读（/weather ... now 实时保险）；仍写缓存
            elevation: 赛道海拔（米）——修正模型海拔偏差（山区/高原赛道）
        """
        now = datetime.now(timezone.utc)
        if cache_key and self.cache and not force_refresh:
            cached = self.cache.load(cache_key)
            if cached is not None:
                return cached

        is_history = end < now - timedelta(hours=6)
        params = {
            "latitude": round(lat, 4), "longitude": round(lon, 4),
            "hourly": HOURLY_VARS, "timeformat": "unixtime",
        }
        if is_history:
            params["start_date"] = start.strftime("%Y-%m-%d")
            params["end_date"] = end.strftime("%Y-%m-%d")
            url = _archive_url()
            ttl_hours = None  # 历史实测永久缓存
        else:
            if start > now + timedelta(days=FORECAST_MAX_DAYS):
                return {"error": f"超出{FORECAST_MAX_DAYS}天预报范围"}
            url = _forecast_url()
            # Open-Meteo forecast 默认只回 7 天：显式按窗口计算 forecast_days/past_days，
            # 否则夏歇/季末 >7 天的窗口会整体为空（OSS 修复等价实现，2026-10-30 交叉验证）
            start_day = start.astimezone(timezone.utc).date()
            end_day = end.astimezone(timezone.utc).date()
            today = now.date()
            params["forecast_days"] = max(1, min(FORECAST_MAX_DAYS, (end_day - today).days + 1))
            params["past_days"] = max(0, min(92, (today - start_day).days))
            # 15 分钟级降水（临近预报分辨率）+ 实时 current 块
            params["minutely_15"] = MINUTELY_VARS
            params["current"] = CURRENT_VARS

        model = os.getenv("WEATHER_MODEL", "").strip()
        if model:
            params["models"] = model
        if elevation is not None:
            params["elevation"] = round(float(elevation), 1)

        custom = url not in (DEFAULT_FORECAST_URL, DEFAULT_ARCHIVE_URL)
        data = None
        net_error = False
        try:
            # 自定义源用较短超时：失败要尽快回退官方源（回退串行总耗时受控）
            resp = self.session.get(url, params=params, timeout=10 if custom else 20)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.HTTPError as e:
            # 4xx/5xx：多为参数不被支持（非法 models/扩展字段）→ 值得降级重试
            logger.warning(f"[Weather] 请求失败(HTTP): {e}")
            data = None
        except Exception as e:
            # 网络/超时：重试同一请求无意义
            logger.warning(f"[Weather] 请求失败: {e}")
            data = None
            net_error = True

        def _request(url_, params_, timeout_=20):
            r = self.session.get(url_, params=params_, timeout=timeout_)
            r.raise_for_status()
            return r.json()

        if custom and (data is None or "hourly" not in data):
            # 回退1：自定义源失败 → 官方 Open-Meteo（先保留扩展字段，只剔除 models；
            # 官方也不吃扩展字段时再降级基础字段——避免一次抖动整段 TTL 丢 15min/current）
            logger.warning(f"[Weather] 自定义源失败，回退官方 Open-Meteo: {url}")
            official = DEFAULT_ARCHIVE_URL if is_history else DEFAULT_FORECAST_URL
            try:
                keep = {k: v for k, v in params.items() if k != "models"}
                data = _request(official, keep)
                if "hourly" not in data and keep.get("minutely_15"):
                    basic = {k: v for k, v in keep.items()
                             if k not in ("minutely_15", "current")}
                    data = _request(official, basic)
            except Exception as e:
                logger.warning(f"[Weather] 官方源回退亦失败: {e}")
                return None
        elif data is None or "hourly" not in data:
            # 官方源：仅"参数类"失败（响应可解析但无 hourly）才值得去掉扩展字段/模型重试；
            # 纯网络异常重试同一请求无意义（还多等一个超时）
            if not net_error and (params.get("minutely_15") or params.get("models")):
                logger.info("[Weather] 扩展字段/模型不被支持，回退基础逐小时字段")
                basic = {k: v for k, v in params.items()
                         if k not in ("minutely_15", "current", "models")}
                try:
                    data = _request(url, basic)
                except Exception as e:
                    logger.warning(f"[Weather] 基础字段回退失败: {e}")
                    return None
            else:
                return None

        if not data or "hourly" not in data:
            return {"error": (data or {}).get("reason", "无数据")}

        # 记录真实抓取时间（缓存命中时也如实反映，防"数据时间=现在"失真）
        data["_fetched_at"] = now.strftime("%Y-%m-%d %H:%M UTC")
        if cache_key and self.cache:
            self.cache.save(cache_key, data, ttl_hours=ttl_hours)
        return data

    # ==================== 窗口汇总 ====================

    @staticmethod
    def _window_summary(hourly: Dict[str, list], start_ts: int, end_ts: int,
                        ref_start_ts: int = None) -> Dict[str, Any]:
        """按时间窗截取并汇总；ref_start_ts 用于计算降水相对ETA（如正赛开始时刻）"""
        times = hourly.get("time") or []
        idx = [i for i, t in enumerate(times) if start_ts <= t <= end_ts]
        if not idx:
            return {}

        def col(name):
            v = hourly.get(name) or []
            return [v[i] for i in idx if i < len(v) and v[i] is not None]

        temps = col("temperature_2m")
        feels = col("apparent_temperature")
        # 预报接口为 soil_temperature_0cm；归档接口为 soil_temperature_0_to_7cm，做回退
        soil = col("soil_temperature_0cm") or col("soil_temperature_0_to_7cm")
        pp = col("precipitation_probability")
        precip = col("precipitation")
        wind = col("wind_speed_10m")
        wdir = col("wind_direction_10m")
        codes = col("weather_code")

        out: Dict[str, Any] = {}
        if temps:
            out["air_temp"] = {"min": round(min(temps), 1), "max": round(max(temps), 1)}
        if feels:
            out["feels_like"] = {"min": round(min(feels), 1), "max": round(max(feels), 1)}
        if soil:
            out["track_temp_proxy"] = {"min": round(min(soil), 1), "max": round(max(soil), 1),
                                       "note": "土壤表层温度，赛道地面温度代理（沥青实测通常更高）"}
        # 逐小时估算沥青温度（经验回归模型），取窗口 min/max
        if soil and temps:
            # 归档接口会返回 soil_temperature_0cm 键但值全为 None（不支持），需按"有值"选择
            sv0 = hourly.get("soil_temperature_0cm") or []
            sv7 = hourly.get("soil_temperature_0_to_7cm") or []
            sv_arr = sv0 if any(v is not None for v in sv0) else sv7
            av_arr = hourly.get("temperature_2m") or []
            rv_arr = hourly.get("shortwave_radiation") or []
            est = []
            for i in idx:
                sv = sv_arr[i] if i < len(sv_arr) else None
                av = av_arr[i] if i < len(av_arr) else None
                rv = rv_arr[i] if i < len(rv_arr) else 0
                if sv is not None and av is not None:
                    est.append(estimate_track_temp(sv, av, rv or 0))
            if est:
                out["track_temp_estimated"] = {"min": round(min(est), 1), "max": round(max(est), 1),
                                               "note": ASPHALT_MODEL_NOTE}
        if pp:
            out["precip_probability_max"] = max(pp)
            # 降水ETA：第一个降水概率>=40%或有小雨+天气码的小时
            rain_i = None
            for _j, i in enumerate(idx):
                p = hourly["precipitation_probability"][i] if i < len(hourly["precipitation_probability"]) else None
                c = hourly["weather_code"][i] if i < len(hourly["weather_code"]) else None
                if (p is not None and p >= 40) or (c is not None and c >= 51):
                    rain_i = i
                    break
            if rain_i is not None:
                ref = ref_start_ts or start_ts
                eta_min = int((times[rain_i] - ref) / 60)
                out["rain_eta_minutes"] = eta_min  # 相对参考时刻（分钟，负值=开始前）
        if precip:
            out["precip_total_mm"] = round(sum(precip), 1)
        if wind:
            out["wind_max_kmh"] = round(max(wind), 1)
            out["wind_min_kmh"] = round(min(wind), 1)
        if wdir:
            # 风向圆周均值（直接算术平均在 0/360 跨界时会错），转中文方位
            import math
            rads = [math.radians(d) for d in wdir]
            mean = math.degrees(math.atan2(sum(math.sin(r) for r in rads),
                                           sum(math.cos(r) for r in rads))) % 360
            sectors = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]
            out["wind_direction"] = sectors[int((mean + 22.5) // 45) % 8] + "风"
        if codes:
            out["weather_desc"] = WMO_CN.get(int(max(codes)), str(max(codes)))
        return out

    @staticmethod
    def _minutely_eta(data: Dict[str, Any], start_ts: int, end_ts: int,
                      ref_start_ts: int) -> Optional[int]:
        """15 分钟级降水 ETA（分钟，相对 ref）。无该分辨率/窗口外返回 None。
        判定同逐小时口径：概率≥40% 或有可见降水（≥0.05mm）"""
        m = data.get("minutely_15") or {}
        times = m.get("time") or []
        if not times:
            return None
        probs = m.get("precipitation_probability") or []
        precs = m.get("precipitation") or []
        for i, t in enumerate(times):
            if not (start_ts <= t <= end_ts):
                continue
            p = probs[i] if i < len(probs) else None
            pr = precs[i] if i < len(precs) else None
            if (p is not None and p >= 40) or (pr is not None and pr >= 0.05):
                return int((t - ref_start_ts) / 60)
        return None

    @staticmethod
    def _current_summary(cur: Dict[str, Any]) -> Dict[str, Any]:
        """Open-Meteo current 块 → 与 _window_summary 兼容的当前天气摘要"""
        out: Dict[str, Any] = {}
        t = cur.get("temperature_2m")
        if t is not None:
            out["air_temp"] = {"min": round(float(t), 1), "max": round(float(t), 1)}
        code = cur.get("weather_code")
        if code is not None:
            out["weather_desc"] = WMO_CN.get(int(code), str(code))
        w = cur.get("wind_speed_10m")
        if w is not None:
            out["wind_max_kmh"] = round(float(w), 1)
            out["wind_min_kmh"] = round(float(w), 1)
        d = cur.get("wind_direction_10m")
        if d is not None:
            sectors = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]
            out["wind_direction"] = sectors[int((float(d) + 22.5) // 45) % 8] + "风"
        pr = cur.get("precipitation")
        if pr is not None:
            out["precip_total_mm"] = round(float(pr), 1)
        h = cur.get("relative_humidity_2m")
        if h is not None:
            out["humidity"] = round(float(h), 0)
        if not out:
            return {}
        out["grain"] = "current"
        return out

    # ==================== OpenF1 实测天气（真实沥青温度，2023+） ====================

    # 内部环节类型 -> OpenF1 会话名（2023 年冲刺排位叫 Sprint Shootout）
    _OPENF1_SESSION_NAMES = {
        "fp1": ("Practice 1",), "fp2": ("Practice 2",), "fp3": ("Practice 3",),
        "qualifying": ("Qualifying",),
        "sprint_qualifying": ("Sprint Qualifying", "Sprint Shootout"),
        "sprint": ("Sprint",), "race": ("Race",),
    }

    @staticmethod
    def _norm(text) -> str:
        import unicodedata
        s = unicodedata.normalize("NFKD", str(text or ""))
        s = "".join(ch for ch in s if not unicodedata.combining(ch))
        return s.lower().replace(" ", "").replace("-", "")

    def _openf1_measured(self, year: int, race: Dict[str, Any],
                         session_type: str) -> Optional[Dict[str, Any]]:
        """
        OpenF1 实测天气聚合（真实沥青赛道温度 TrackTemp，非估算），覆盖 2023+ 已结束环节。
        比赛进行中的未认证请求会 401（返回 None，调用方回退预报/归档代理）。
        """
        names = self._OPENF1_SESSION_NAMES.get(session_type)
        if not names or year < 2023:
            return None

        cache_key = f"weather_measured_v3_{year}_{race.get('round', '')}_{session_type}"
        neg_key = f"weather_measured_neg_v1_{year}_{race.get('round', '')}_{session_type}"
        if self.cache:
            cached = self.cache.load(cache_key)
            if cached is not None:
                return cached or None
            if self.cache.load(neg_key):
                return None  # 短 TTL 负缓存：比赛周末 401/无数据时避免每环节重试拖慢

        try:
            from .openf1_api import OpenF1API
            api = OpenF1API()
            circuit = race.get("Circuit", {}) or {}
            loc = circuit.get("Location", {}) or {}
            race_name_n = self._norm(race.get("raceName", ""))
            # 会议匹配打分（review 高 #1：同国家多站时纯 country 命中会张冠李戴——
            # 巴塞罗那/马德里、Imola/Monza。赛道名/城市名最可信，raceName 包含次之，
            # country 仅作最低权重兜底，同分取更接近的候选则由排序保证稳定）
            best, best_score = None, 0
            for m in api.get_meetings(year):
                score = 0
                short = self._norm(m.get("circuit_short_name", ""))
                city = self._norm(m.get("location", ""))
                country = self._norm(m.get("country_name", ""))
                mname = self._norm(m.get("meeting_name", ""))
                if short and short in {self._norm(circuit.get("circuitName", "")),
                                       self._norm(loc.get("locality", ""))}:
                    score += 100
                if city and city == self._norm(loc.get("locality", "")):
                    score += 80
                if race_name_n and mname:
                    if mname == race_name_n:
                        score += 90
                    elif mname in race_name_n or race_name_n in mname:
                        score += 40
                if short and short and self._norm(circuit.get("circuitName", "")) \
                        and short in self._norm(circuit.get("circuitName", "")):
                    score += 30
                if country and country == self._norm(loc.get("country", "")):
                    score += 5
                if score > best_score:
                    best, best_score = m, score
            if not best or best_score < 30:
                # 低置信匹配放弃（宁可回退模型估算，不给错站实测数据）
                if self.cache:
                    self.cache.save(neg_key, True, ttl_hours=0.5)
                return None
            meeting = best
            sessions = api.get_sessions(meeting_key=meeting["meeting_key"])
            session = next((s for s in sessions if s.get("session_name") in names), None)
            if not session:
                if self.cache:
                    self.cache.save(neg_key, True, ttl_hours=0.5)
                return None
            # 进行中/未结束的环节不取实测（review 高 #2：开赛 30min 后拿到半场样本
            # 会以 72h TTL 缓存，整场结束后仍返回半场数据）——以 OpenF1 date_end 为准
            now = datetime.now(timezone.utc)
            end_dt = _parse_dt(session.get("date_end"))
            start_dt = _parse_dt(session.get("date_start"))
            if end_dt and end_dt + timedelta(minutes=10) > now:
                # 加 10 分钟宽限：date_end 是计划时间，红旗/雨战可能顺延
                if self.cache:
                    self.cache.save(neg_key, True, ttl_hours=0.3)  # 进行中短负缓存，防反复重拉
                return None
            elif start_dt and (now - start_dt) < timedelta(hours=2):
                # 无 date_end 或解析失败：按环节典型时长兜底要求已开始 ≥2h
                if self.cache:
                    self.cache.save(neg_key, True, ttl_hours=0.3)
                return None
            series = api._make_request("weather", {"session_key": session["session_key"]})
            if not series:
                if self.cache:
                    self.cache.save(neg_key, True, ttl_hours=0.5)
                return None

            def _nums(key):
                out = []
                for w in series:
                    try:
                        out.append(float(w.get(key)))
                    except (TypeError, ValueError):
                        continue
                return out

            track = _nums("track_temperature")
            air = _nums("air_temperature")
            # OpenF1 wind_speed 单位 m/s（openf1_api 自身也按 m/s 展示）——
            # 需×3.6 转 km/h，否则与模型 km/h 混排会出现 min>max（review 高 #3）
            wind = [v * 3.6 for v in _nums("wind_speed")]
            humid = _nums("humidity")
            rain = any(str(w.get("rainfall")) in ("1", "1.0", "True") for w in series)
            # 实测时间跨度（epoch 秒，优先 OpenF1 会话起止）：雨战/SC 延长的场次
            # 用它替代预估窗口
            span = None
            _s_dt, _e_dt = _parse_dt(session.get("date_start")), _parse_dt(session.get("date_end"))
            if _s_dt and _e_dt:
                span = (_s_dt.timestamp(), _e_dt.timestamp())
            if not span:
                ts = [d.timestamp() for d in
                      (_parse_dt(w.get("date")) for w in series) if d]
                if len(ts) >= 2:
                    span = (min(ts), max(ts))
            out = {
                "measured": True,
                "source": "OpenF1 实测",
                "track_temp_measured": ({"min": round(min(track), 1), "max": round(max(track), 1)}
                                        if track else None),
                "air_temp_measured": ({"min": round(min(air), 1), "max": round(max(air), 1)}
                                      if air else None),
                "rainfall_measured": rain,
                "wind_max_kmh": round(max(wind), 1) if wind else None,
                "wind_min_kmh": round(min(wind), 1) if wind else None,
                "humidity_avg": round(sum(humid) / len(humid), 1) if humid else None,
            }
            if span:
                out["_span"] = span  # 内部字段：调用方只读不 pop（缓存对象共享，勿原地删）
            if self.cache:
                self.cache.save(cache_key, out, ttl_hours=(None if year < datetime.now().year else 72))
            return out
        except Exception as e:
            logger.info(f"[Weather] OpenF1 实测不可用({year} {race.get('raceName','')} {session_type}): {e}")
            return None

    # ==================== 比赛天气 ====================

    def get_race_weather(self, race: Dict[str, Any], sessions: List[Dict[str, Any]] = None,
                         year: int = None, live: bool = False) -> Dict[str, Any]:
        """
        获取分站天气分析包。

        Args:
            race: Ergast 格式比赛 dict（需含 Circuit.Location.lat/long 与 date/time）
            sessions: f1_api.get_all_sessions(race) 的结果（可选，用于逐环节天气）
            year: 赛季年份（OpenF1 实测用；缺省从 race.season 推断）
            live: 强制实时刷新（跳过缓存读；用于"现在实时天气"场景）。

        Returns:
            {"circuit", "location", "data_kind", "fetched_at", "sessions": [...], ...}
        """
        loc = (race.get("Circuit", {}) or {}).get("Location", {}) or {}
        try:
            lat, lon = float(loc.get("lat")), float(loc.get("long"))
        except (TypeError, ValueError):
            return {"error": "赛历缺少赛道坐标"}

        race_name = race.get("raceName", "")
        circuit = race.get("Circuit", {}) or {}
        locality = loc.get("locality", "")
        now = datetime.now(timezone.utc)

        # 赛道海拔：env 全局覆盖 > 赛道档案（山区/高原赛道气温模型偏差主要来源）
        elevation = None
        env_elev = os.getenv("WEATHER_ELEVATION", "").strip()
        if env_elev:
            try:
                elevation = float(env_elev)
            except ValueError:
                logger.warning(f"[Weather] WEATHER_ELEVATION 非法值「{env_elev}」，忽略")
        if elevation is None:
            cid = str(circuit.get("circuitId") or "")
            if cid:
                elevation = load_elevations().get(cid)

        # 确定时间范围：所有环节覆盖期；无环节列表则用正赛日期
        win_start = now - timedelta(days=1)
        win_end = now + timedelta(days=2)
        if sessions:
            dts = [s["datetime"] for s in sessions if s.get("datetime")]
            if dts:
                win_start = min(dts) - timedelta(hours=2)
                win_end = max(dts) + timedelta(hours=3)
        else:
            try:
                rt = race.get("time", "00:00:00Z").replace("Z", "")
                rdt = datetime.fromisoformat(f"{race['date']}T{rt}").replace(tzinfo=timezone.utc)
                win_start, win_end = rdt - timedelta(hours=1), rdt + timedelta(hours=3)
            except Exception:
                pass

        is_forecast = win_end >= now - timedelta(hours=6)
        # 缓存键 v2：口径变更（15分钟级/current/海拔/窗口时长）必须换键，防旧结构缓存穿透
        cache_key = f"weather_v2_{race_name.replace(' ', '')}_{win_start.strftime('%Y%m%d')}"
        # 自适应 TTL：越临近比赛预报越勤（6h内 30min / 24h内 1h / 更远 3h）；历史永久
        horizon_h = (win_start - now).total_seconds() / 3600
        ttl = 0.5 if horizon_h <= 6 else (1 if horizon_h <= 24 else 3)
        data = self._fetch_hourly(lat, lon, win_start, win_end,
                                  cache_key=cache_key, ttl_hours=ttl,
                                  force_refresh=live, elevation=elevation)
        if not data:
            return {"error": "天气数据获取失败"}
        if "error" in data:
            return {"error": data["error"]}
        hourly = data["hourly"]

        # 15 分钟级是否真正覆盖比赛窗口（远期窗口 minutely_15 数组存在但只覆盖近几天）
        _min_times = (data.get("minutely_15") or {}).get("time") or []
        _ws, _we = win_start.timestamp(), win_end.timestamp()
        _has15 = any(_ws <= t <= _we for t in _min_times)
        result: Dict[str, Any] = {
            "circuit": circuit.get("circuitName", ""),
            "race": race_name,
            "location": f"{locality}, {loc.get('country', '')}",
            "data_kind": ("forecast(预报)" if is_forecast else "archive(实测归档)")
                         + ("·实时刷新" if live else ""),
            "fetched_at": data.get("_fetched_at") or now.strftime("%Y-%m-%d %H:%M UTC"),
            "grain": "15min" if _has15 else "1h",
        }
        if elevation is not None:
            result["elevation_m"] = elevation

        # 逐环节天气
        sess_out = []
        if sessions:
            for s in sessions:
                dt = s.get("datetime")
                if not dt:
                    continue
                stype = s.get("type", "")
                st = int((dt - timedelta(minutes=30)).timestamp())
                # 窗口时长按环节类型（race 3h 覆盖 SC/红旗/发车延迟；见 SESSION_WINDOW_SECONDS）
                dur = SESSION_WINDOW_SECONDS.get(stype, DEFAULT_WINDOW_SECONDS)
                en = int(dt.timestamp()) + dur
                # 已结束环节：OpenF1 实测（真实沥青温度）——若拿到实测时间跨度，
                # 以实际跨度替代预估窗口（雨战/S C 延长的场次窗口自动贴合真实时长）
                measured = None
                if dt + timedelta(minutes=30) < now:
                    measured = self._openf1_measured(
                        year or int(race.get("season", now.year) or now.year),
                        race, stype)
                if measured:
                    span = measured.get("_span")
                    # None 值不覆盖模型有效值（OpenF1 字段缺失时 model 风速/气温应保留）
                    measured = {k: v for k, v in measured.items()
                                if k != "_span" and v is not None}
                    if span:
                        st, en = int(span[0]) - 300, int(span[1]) + 300
                summ = self._window_summary(hourly, st, en, ref_start_ts=int(dt.timestamp()))
                # 15 分钟级降水 ETA 更精确（临近预报）：可用则覆盖逐小时结果
                eta15 = self._minutely_eta(data, st, en, int(dt.timestamp()))
                if eta15 is not None and summ:
                    summ["rain_eta_minutes"] = eta15
                    summ["rain_eta_grain"] = "15min"
                if measured:
                    summ.update(measured)
                if summ:
                    summ["session"] = s.get("name", "")
                    summ["session_type"] = stype
                    summ["datetime"] = dt.isoformat()
                    sess_out.append(summ)
        else:
            summ = self._window_summary(hourly, int(win_start.timestamp()),
                                        int(win_end.timestamp()),
                                        ref_start_ts=int(win_start.timestamp()))
            if summ:
                sess_out.append(summ)
        result["sessions"] = sess_out

        # 本站校准：已完赛环节的 实测-模型 偏差均值，修正未开始环节的模型估算
        # （实证：模型对个别赛道系统性偏低，如 2026 西班牙站 FP2 低约 3°C）
        deltas = []
        for so in sess_out:
            tm, te = so.get("track_temp_measured"), so.get("track_temp_estimated")
            if tm and te:
                deltas.append(((tm["min"] + tm["max"]) / 2) - ((te["min"] + te["max"]) / 2))
        if deltas:
            bias = sum(deltas) / len(deltas)
            if abs(bias) >= 1.0:
                for so in sess_out:
                    te = so.get("track_temp_estimated")
                    if te and not so.get("track_temp_measured"):
                        te["min"] = round(te["min"] + bias, 1)
                        te["max"] = round(te["max"] + bias, 1)
                        te["note"] = f"模型估算 + 本站已完赛环节实测校准（{bias:+.1f}°C）"
                logger.info(f"[Weather] {race_name} 沥青温度本站校准: {bias:+.1f}°C "
                            f"（{len(deltas)} 个已完赛环节实测）")

        # 当前时刻天气：优先 Open-Meteo current 实时块；无该字段/空摘要回退前后各1小时窗口。
        # 仅比赛周窗口附近展示（远期分站展示"当前"会误导）
        cur = {}
        if data.get("current") and win_start - timedelta(days=1) <= now <= win_end + timedelta(days=1):
            cur = self._current_summary(data["current"])
        if not cur and win_start <= now <= win_end:
            cur = self._window_summary(hourly, int((now - timedelta(hours=1)).timestamp()),
                                       int((now + timedelta(hours=1)).timestamp()))
        if cur:
            result["current"] = cur

        # 长窗口提示：正赛/雨战窗口按上限预估，实际以实测/现场为准
        if any(so.get("session_type") == "race" for so in sess_out):
            result["window_note"] = (
                "正赛天气窗口按 3 小时预估（覆盖安全车/红旗/雨战发车延迟）；"
                "已完赛场次以 OpenF1 实测时间跨度为准")

        # 预测分析提示
        notes = []
        for so in sess_out:
            if so.get("session") in ("正赛", "Race") or not so.get("session"):
                pp = so.get("precip_probability_max")
                eta = so.get("rain_eta_minutes")
                if pp is not None and pp >= 40:
                    if eta is not None and eta >= 0:
                        notes.append(f"正赛开始约{eta}分钟后可能出现降水（概率峰值{pp}%）——雨战/换胎策略变量")
                    else:
                        notes.append(f"正赛窗口降水概率峰值{pp}%——存在雨战可能")
                # 赛道温度优先级：实测沥青 > 模型估算 > 土壤代理
                if so.get("track_temp_measured"):
                    tt = so["track_temp_measured"]
                    tt_note = f"实测沥青 {tt['min']}-{tt['max']}°C"
                    tt_max = tt["max"]
                elif so.get("track_temp_estimated"):
                    tt = so["track_temp_estimated"]
                    tt_note = f"模型估算沥青 {tt['min']}-{tt['max']}°C"
                    tt_max = tt["max"]
                else:
                    tt = so.get("track_temp_proxy", {})
                    tt_note = f"地面代理峰值{tt.get('max')}°C"
                    tt_max = tt.get("max", 0)
                if tt_max and tt_max >= 40:
                    notes.append(f"赛道温度高（{tt_note}）——偏向轮胎退化快的设定")
                elif tt_max and 0 < tt_max <= 18:
                    notes.append(f"赛道温度低（{tt_note}）——轮胎升温困难，偏向软胎/高下压力窗口")
                if so.get("rainfall_measured"):
                    notes.append("实测有降雨——雨战")
                if (so.get("wind_max_kmh") or 0) >= 35:
                    notes.append(f"大风（峰值{so['wind_max_kmh']}km/h）——影响刹车点与尾流稳定性")
        result["analysis_notes"] = notes
        return result
