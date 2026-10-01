"""
F1赛程提醒机器人 - 天气数据模块（Open-Meteo，免费无鉴权）

用途：为 Agent 预测分析提供比赛时段的赛道当地天气：
- 未来比赛（16天内）：逐小时预报——气温/体感/降水概率/降水量/风力/0cm土壤温度（赛道地面温度代理）
- 历史比赛：Open-Meteo 归档实测（同样字段）
- 降水时序：计算"正赛开始多久后可能降水"等关键预测依据

赛道坐标来自赛历 Circuit.Location 的 lat/long（Jolpica Ergast 格式自带）。
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

HOURLY_VARS = ("temperature_2m,apparent_temperature,precipitation_probability,"
               "precipitation,weather_code,wind_speed_10m,wind_direction_10m,"
               "soil_temperature_0cm,soil_temperature_0_to_7cm,shortwave_radiation")

# 沥青赛道温度估算模型（线性回归，2023-2025 全年分站 Race/Qualifying 共 122 组
# F1 livetiming 实测沥青温度 vs Open-Meteo 归档土壤表层温度/气温/短波辐射拟合）：
#   track ≈ INTERCEPT + C_SOIL*soil + C_AIR*air + C_RAD*shortwave_radiation
# 拟合质量: R²=0.813, RMSE≈3.9°C, MAE≈2.9°C, P80误差≈4.6°C（辐射是关键变量，仅土壤 R² 仅 0.51）
ASPHALT_MODEL = {"intercept": 1.4339, "soil": 0.472, "air": 0.6139, "radiation": 0.018}
ASPHALT_MODEL_NOTE = "沥青温度估算模型：2023-2025共122组实测拟合（R²=0.81，RMSE≈3.9°C），基于土壤表层温度+气温+短波辐射"


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
                      cache_key: str = None, ttl_hours: int = 3) -> Optional[Dict[str, Any]]:
        """抓取指定时间窗的逐小时数据（未来走forecast，过去走archive，自动判断）"""
        now = datetime.now(timezone.utc)
        if cache_key and self.cache:
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
            url = ARCHIVE_URL
            ttl_hours = None  # 历史实测永久缓存
        else:
            if start > now + timedelta(days=FORECAST_MAX_DAYS):
                return {"error": f"超出{FORECAST_MAX_DAYS}天预报范围"}
            # 预报默认只回7天：显式指定日期范围，覆盖 8-16 天窗口
            params["start_date"] = start.strftime("%Y-%m-%d")
            params["end_date"] = end.strftime("%Y-%m-%d")
            url = FORECAST_URL

        try:
            resp = self.session.get(url, params=params, timeout=20)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.warning(f"[Weather] 请求失败: {e}")
            return None
        if "hourly" not in data:
            return {"error": data.get("reason", "无数据")}

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
            for j, i in enumerate(idx):
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

        cache_key = f"weather_measured_{year}_{race.get('round', '')}_{session_type}"
        if self.cache:
            cached = self.cache.load(cache_key)
            if cached is not None:
                return cached or None

        try:
            from .openf1_api import OpenF1API
            api = OpenF1API()
            circuit = race.get("Circuit", {}) or {}
            loc = circuit.get("Location", {}) or {}
            targets = {self._norm(x) for x in (
                circuit.get("circuitName", ""), loc.get("locality", ""),
                loc.get("country", ""), race.get("raceName", "")) if x}
            meeting = None
            for m in api.get_meetings(year):
                cands = {self._norm(x) for x in (
                    m.get("circuit_short_name", ""), m.get("location", ""),
                    m.get("country_name", ""), m.get("meeting_name", "")) if x}
                # 赛道短名/城市名精确命中，或赛事名互相包含
                if cands & targets or any(self._norm(m.get("meeting_name")) in t
                                          or t in self._norm(m.get("meeting_name"))
                                          for t in targets if t):
                    meeting = m
                    break
            if not meeting:
                return None
            session = None
            for s in api.get_sessions(meeting_key=meeting["meeting_key"]):
                if s.get("session_name") in names:
                    session = s
                    break
            if not session:
                return None
            series = api._make_request("weather", {"session_key": session["session_key"]})
            if not series:
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
            wind = _nums("wind_speed")
            humid = _nums("humidity")
            rain = any(str(w.get("rainfall")) in ("1", "1.0", "True") for w in series)
            out = {
                "measured": True,
                "source": "OpenF1 实测",
                "track_temp_measured": ({"min": round(min(track), 1), "max": round(max(track), 1)}
                                        if track else None),
                "air_temp_measured": ({"min": round(min(air), 1), "max": round(max(air), 1)}
                                      if air else None),
                "rainfall_measured": rain,
                "wind_max_kmh": round(max(wind), 1) if wind else None,
                "humidity_avg": round(sum(humid) / len(humid), 1) if humid else None,
            }
            if self.cache:
                self.cache.save(cache_key, out, ttl_hours=(None if year < datetime.now().year else 72))
            return out
        except Exception as e:
            logger.info(f"[Weather] OpenF1 实测不可用({year} {race.get('raceName','')} {session_type}): {e}")
            return None

    # ==================== 比赛天气 ====================

    def get_race_weather(self, race: Dict[str, Any], sessions: List[Dict[str, Any]] = None,
                         year: int = None) -> Dict[str, Any]:
        """
        获取分站天气分析包。

        Args:
            race: Ergast 格式比赛 dict（需含 Circuit.Location.lat/long 与 date/time）
            sessions: f1_api.get_all_sessions(race) 的结果（可选，用于逐环节天气）
            year: 赛季年份（OpenF1 实测用；缺省从 race.season 推断）

        Returns:
            {"circuit", "location", "is_forecast", "sessions": [...], "race": {...}, "error": ...}
        """
        loc = (race.get("Circuit", {}) or {}).get("Location", {}) or {}
        try:
            lat, lon = float(loc.get("lat")), float(loc.get("long"))
        except (TypeError, ValueError):
            return {"error": "赛历缺少赛道坐标"}

        race_name = race.get("raceName", "")
        locality = loc.get("locality", "")
        now = datetime.now(timezone.utc)

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
        cache_key = f"weather_{race_name.replace(' ', '')}_{win_start.strftime('%Y%m%d')}"
        # 预报3h缓存（越临近越新）；历史永久
        data = self._fetch_hourly(lat, lon, win_start, win_end,
                                  cache_key=cache_key, ttl_hours=3)
        if not data:
            return {"error": "天气数据获取失败"}
        if "error" in data:
            return {"error": data["error"]}
        hourly = data["hourly"]

        result: Dict[str, Any] = {
            "circuit": (race.get("Circuit", {}) or {}).get("circuitName", ""),
            "race": race_name,
            "location": f"{locality}, {loc.get('country', '')}",
            "data_kind": "forecast(预报)" if is_forecast else "archive(实测归档)",
        }

        # 逐环节天气
        sess_out = []
        if sessions:
            for s in sessions:
                dt = s.get("datetime")
                if not dt:
                    continue
                st = int((dt - timedelta(minutes=30)).timestamp())
                dur = 7500 if s.get("type") == "race" else 3900  # 正赛覆盖全场(~2h)，其余环节1h余量
                en = int(dt.timestamp()) + dur
                summ = self._window_summary(hourly, st, en, ref_start_ts=int(dt.timestamp()))
                # 已结束环节：合并 OpenF1 实测（真实沥青温度，替代代理估算）
                if dt + timedelta(minutes=30) < now:
                    measured = self._openf1_measured(
                        year or int(race.get("season", now.year) or now.year),
                        race, s.get("type", ""))
                    if measured:
                        summ.update(measured)
                if summ:
                    summ["session"] = s.get("name", "")
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

        # 当前时刻天气（比赛周末期间访问时）：前后各1小时窗口
        if win_start <= now <= win_end:
            cur = self._window_summary(hourly, int((now - timedelta(hours=1)).timestamp()),
                                       int((now + timedelta(hours=1)).timestamp()))
            if cur:
                result["current"] = cur

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
                if tt_max >= 40:
                    notes.append(f"赛道温度高（{tt_note}）——偏向轮胎退化快的设定")
                elif 0 < tt_max <= 18:
                    notes.append(f"赛道温度低（{tt_note}）——轮胎升温困难，偏向软胎/高下压力窗口")
                if so.get("rainfall_measured"):
                    notes.append("实测有降雨——雨战")
                if so.get("wind_max_kmh", 0) >= 35:
                    notes.append(f"大风（峰值{so['wind_max_kmh']}km/h）——影响刹车点与尾流稳定性")
        result["analysis_notes"] = notes
        return result
