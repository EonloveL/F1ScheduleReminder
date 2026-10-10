"""
F1官方 livetiming 静态归档客户端（FastF1 同源数据源）
=====================================================
背景：OpenF1 在比赛进行中（现场环节窗口）对未认证用户全站 401
（2026-10-09 新加坡 FP 期间服务器/本地同步实证），而 livetiming.formula1.com
静态归档**免费、无认证、无赛中锁定**——是 OpenF1 不可用期的最佳兜底源。

可用文件（每场正赛目录下）：
- TimingAppData.json  车手 stint 配方（Compound + TotalLaps/StartLaps 推导圈数边界）
- DriverList.json     车号 → 姓名/车队
- RaceControlMessages.json  赛会消息（SC/VSC/红旗，含 Lap 字段）
- WeatherData.json    天气（AirTemp/TrackTemp/Rainfall/WindSpeed，按键为时间戳）

stint 边界口径（2026-10-09 用巴库 R15 与 OpenF1 逐车手交叉验证实证）：
  每条 Stint 记录含 TotalLaps（该胎累计圈数）与 StartLaps（上场前已用胎龄），
  本段圈数 = TotalLaps - StartLaps；各段按数组顺序首尾相接，从第 1 圈起。
  LapNumber 字段只是记录刷新圈，**不是**边界（勿用）。
  TyresNotChanged=="1" 的条目 = 红旗重启未换胎，并入上一段（延长期末圈）。

URL 推导：{date}_{GP名空格转下划线}；"Bahrain Grand Prix in Malaysia" 这类
带后缀名需逐级砍尾部单词重试（实证雪邦 2026 目录是 Bahrain_Grand_Prix）。
"""
import json
import logging
import unicodedata
from typing import Any, Dict, List, Optional

import requests

# curl_cffi（Chrome TLS 指纹伪装）：livetiming.formula1.com 对部分机房 IP
# 按 TLS 指纹 403（2026-10-10 阿里云服务器 python-requests 403 实证），
# 装了 curl_cffi 就走浏览器指纹，没装回退 requests
try:
    from curl_cffi import requests as _cffi_requests
    _HAS_CURL_CFFI = True
except ImportError:
    _HAS_CURL_CFFI = False

logger = logging.getLogger(__name__)

BASE = "https://livetiming.formula1.com/static"


def _norm_name(name: str) -> str:
    """去组合符（São→Sao），供目录名候选生成"""
    s = unicodedata.normalize("NFKD", str(name or ""))
    return "".join(c for c in s if not unicodedata.combining(c))


class LiveTimingClient:
    """livetiming 静态归档读取（结果可缓存——已完赛分站不可变，永久缓存）"""

    def __init__(self, f1_api=None, cache=None):
        from .f1_api import LocalCache
        self.f1_api = f1_api
        self.cache = cache or LocalCache()
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({"User-Agent": "Mozilla/5.0"})

    # ---------- 路径推导 ----------

    def _session_dir_candidates(self, season: int, round_num: int) -> List[str]:
        """生成候选目录名：[日期_会议名]（全称→逐级砍尾词），仅正赛"""
        if not self.f1_api:
            return []
        try:
            schedule = self.f1_api.get_schedule_for_year(season) or []
            race = next((r for r in schedule
                         if int(r.get("round", 0)) == round_num), None)
        except Exception:
            race = None
        if not race or not race.get("date"):
            return []
        date = race["date"]
        name = _norm_name(race.get("raceName", ""))
        words = [w for w in name.replace("-", " ").split() if w]
        out = []
        for n in range(len(words), 1, -1):
            base = "_".join(words[:n])
            out.append(f"{season}/{date}_{base}/{date}_Race")
        return out

    def _fetch_json(self, rel_path: str) -> Optional[Dict[str, Any]]:
        url = f"{BASE}/{rel_path}"
        try:
            if _HAS_CURL_CFFI:
                resp = _cffi_requests.get(url, impersonate="chrome", timeout=20)
            else:
                resp = self.session.get(url, timeout=20)
            if resp.status_code != 200:
                return None
            return json.loads(resp.content.decode("utf-8-sig"))
        except Exception as e:
            logger.debug(f"[LiveTiming] {rel_path} 失败: {e}")
            return None

    def _race_date(self, season: int, round_num: int) -> Optional[str]:
        if not self.f1_api:
            return None
        try:
            schedule = self.f1_api.get_schedule_for_year(season) or []
            race = next((r for r in schedule
                         if int(r.get("round", 0)) == round_num), None)
            return race.get("date") if race else None
        except Exception:
            return None

    def _ttl_for(self, season: int, round_num: int) -> Optional[int]:
        """缓存 TTL：正赛日+1天前=30min（livetiming 归档赛中是持续更新的活文件，
        永久缓存会把赛中残缺 stint/事件数据冻结——2026-10-10 review 实证）；
        正赛日+1天后=永久（数据不可变）"""
        from datetime import datetime as _dt, timedelta as _td, timezone as _tz
        rd = self._race_date(season, round_num)
        if not rd:
            return None
        try:
            race_day = _dt.strptime(rd, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None
        return None if _dt.now(_tz.utc).date() > race_day + _td(days=1) else 0.5

    def get_race_file(self, season: int, round_num: int, filename: str
                      ) -> Optional[Dict[str, Any]]:
        """取正赛归档文件（缓存：正赛日+1天前 30min / 之后永久）"""
        cache_key = f"livetiming_{season}_{round_num}_{filename}"
        cached = self.cache.load(cache_key)
        if cached is not None:
            return cached or None
        ttl = self._ttl_for(season, round_num)
        for cand in self._session_dir_candidates(season, round_num):
            data = self._fetch_json(f"{cand}/{filename}")
            if data is not None:
                if data:
                    self.cache.save(cache_key, data, ttl_hours=ttl)
                else:
                    # 空 dict：源站真实空响应，短缓存防抖动，不当"查无此档"永久占位
                    self.cache.save(cache_key, {}, ttl_hours=min(ttl or 24, 24))
                return data or None
        # 分站未赛/目录名未中：正赛日前 3h 短缓存（赛后需尽快重试），正赛日后 24h
        rd = self._race_date(season, round_num)
        from datetime import datetime as _dt, timezone as _tz
        short_ttl = 3 if rd and rd >= _dt.now(_tz.utc).date().isoformat() else 24
        self.cache.save(cache_key, {}, ttl_hours=short_ttl)
        return None

    # ---------- 解析 ----------

    def get_stints(self, season: int, round_num: int
                   ) -> Optional[Dict[str, List[Dict[str, Any]]]]:
        """{姓氏: [{lap_start, lap_end, compound, stint}]}（含配方！OpenF1 401 时的配方兜底）"""
        app = self.get_race_file(season, round_num, "TimingAppData.json")
        if not app or "Lines" not in app:
            return None
        drivers = self.get_race_file(season, round_num, "DriverList.json") or {}
        num_to_surname = {str(v.get("RacingNumber")): (v.get("LastName") or "").lower()
                          for v in drivers.values() if isinstance(v, dict)}

        out: Dict[str, List[Dict[str, Any]]] = {}
        for num, line in app["Lines"].items():
            surname = num_to_surname.get(str(num))
            if not surname:
                continue
            raw_stints = line.get("Stints") or []
            # Stints 可能是 dict 键控 {"0": {...}}——迭代 dict 会产出字符串键导致
            # AttributeError 崩溃（2026-10-10 review 实证部分分站为此形态）
            entries = (list(raw_stints.values()) if isinstance(raw_stints, dict)
                       else raw_stints)
            stints = []
            lap_cursor = 1
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                comp = (entry.get("Compound") or "UNKNOWN").upper()
                try:
                    n_laps = int(entry.get("TotalLaps", 0)) - int(entry.get("StartLaps", 0))
                except (TypeError, ValueError):
                    continue
                if entry.get("TyresNotChanged") == "1" and stints:
                    # 红旗重启未换胎：并入上一段
                    stints[-1]["lap_end"] = lap_cursor + n_laps - 1
                    lap_cursor += n_laps
                    continue
                if n_laps <= 0:
                    continue
                stints.append({"lap_start": lap_cursor,
                               "lap_end": lap_cursor + n_laps - 1,
                               "compound": comp, "stint": len(stints) + 1})
                lap_cursor += n_laps
            if stints:
                out[surname] = stints
        return out or None

    def get_race_control(self, season: int, round_num: int) -> Optional[List[Dict]]:
        """赛会消息（转成 OpenF1 race_control 同构：lap_number/message/category）"""
        rc = self.get_race_file(season, round_num, "RaceControlMessages.json")
        if not rc or "Messages" not in rc:
            return None
        messages = rc["Messages"]
        # Messages 同样可能是 dict 键控 {"0": {...}}
        entries = (list(messages.values()) if isinstance(messages, dict) else messages)
        out = []
        for m in entries:
            if not isinstance(m, dict):
                continue
            out.append({
                "lap_number": m.get("Lap"),
                "message": m.get("Message", ""),
                "category": m.get("Category", ""),
                "date": m.get("Utc", ""),
            })
        return out or None

    def get_weather(self, season: int, round_num: int) -> Optional[List[Dict]]:
        """天气（转成 OpenF1 weather 同构行：date/air_temperature/track_temperature/rainfall/wind_speed）"""
        w = self.get_race_file(season, round_num, "WeatherData.json")
        if not w:
            return None
        out = []
        for ts, row in w.items():
            if not isinstance(row, dict):
                continue
            try:
                rainfall = 1.0 if float(row.get("Rainfall") or 0) > 0 else 0.0
            except (TypeError, ValueError):
                rainfall = 0.0
            try:
                out.append({
                    "date": ts,
                    "air_temperature": float(row.get("AirTemp", 0) or 0),
                    "track_temperature": float(row.get("TrackTemp", 0) or 0),
                    "rainfall": rainfall,
                    "wind_speed": float(row.get("WindSpeed", 0) or 0),
                    "humidity": float(row.get("Humidity", 0) or 0),
                })
            except (TypeError, ValueError):
                continue
        return out or None
