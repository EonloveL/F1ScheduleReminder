"""
F1赛程提醒机器人 - OpenF1 API增强模块
提供实时数据：天气、赛事控制、圈速等
API文档: https://openf1.org
注意: 仅支持2023年及以后的数据
"""

import requests
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any
import logging

logger = logging.getLogger(__name__)

BASE_URL = "https://api.openf1.org/v1"
TOKEN_URL = "https://api.openf1.org/token"

import os


class OpenF1API:
    """OpenF1 API 封装类 - 提供实时F1数据（实时数据需 OAuth2 sponsor 订阅）"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'F1-Reminder-Bot/1.0'
        })
        # OAuth2 认证（实时数据需要 sponsor 订阅；未配置则仅历史数据可用，
        # 且比赛进行期间 OpenF1 会对所有请求返回 401）
        self._username = os.getenv("OPENF1_USERNAME", "")
        self._password = os.getenv("OPENF1_PASSWORD", "")
        self._token = None
        self._token_expires_at = 0  # epoch 秒
        self._auth_enabled = bool(self._username and self._password)
        if self._auth_enabled:
            logger.info("[OpenF1] OAuth2 认证已配置")
        else:
            logger.info("[OpenF1] 未配置 OPENF1_USERNAME/PASSWORD，仅历史数据可用（比赛进行中接口会401）")

    # ==================== OAuth2 认证 ====================

    def _get_token(self) -> Optional[str]:
        """获取/刷新 OAuth2 access_token（1小时过期，提前60秒刷新）"""
        if not self._auth_enabled:
            return None
        import time as _time
        if self._token and _time.time() < self._token_expires_at - 60:
            return self._token
        try:
            resp = requests.post(
                TOKEN_URL,
                data={"username": self._username, "password": self._password},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            token = data.get("access_token")
            expires_in = int(data.get("expires_in") or 3600)  # key存在但值为null时防 TypeError
            if token:
                self._token = token
                self._token_expires_at = _time.time() + expires_in
                logger.info("[OpenF1] access_token 获取成功")
                return token
        except requests.RequestException as e:
            logger.error(f"[OpenF1] 获取token失败: {e}")
        return None

    def _make_request(self, endpoint: str, params: Dict = None) -> Optional[List[Dict]]:
        """
        发送HTTP请求到 OpenF1 API（自动附带 Bearer token，若已配置认证）

        Args:
            endpoint: API端点
            params: 查询参数

        Returns:
            API响应数据列表，失败返回None
        """
        url = f"{BASE_URL}/{endpoint}"
        headers = {}
        if self._auth_enabled:
            token = self._get_token()
            if token:
                headers["Authorization"] = f"Bearer {token}"
                headers["accept"] = "application/json"
        try:
            response = self.session.get(url, params=params, headers=headers or None, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"[OpenF1] 请求失败: {url}, 错误: {e}")
            return None
    
    def get_meetings(self, year: int = None) -> List[Dict[str, Any]]:
        """
        获取赛事会议列表
        
        Args:
            year: 年份，默认当前年份
            
        Returns:
            赛事会议列表
        """
        year = year or datetime.now().year
        params = {"year": year}
        data = self._make_request("meetings", params)
        return data if data else []
    
    def get_sessions(self, meeting_key: int = None, year: int = None) -> List[Dict[str, Any]]:
        """
        获取赛事会话列表
        
        Args:
            meeting_key: 会议唯一标识
            year: 年份
            
        Returns:
            会话列表
        """
        params = {}
        if meeting_key:
            params["meeting_key"] = meeting_key
        if year:
            params["year"] = year
            
        data = self._make_request("sessions", params)
        return data if data else []
    
    def get_weather(self, session_key: int) -> Optional[Dict[str, Any]]:
        """
        获取赛事天气数据（适合赛前推送）
        
        Args:
            session_key: 会话唯一标识
            
        Returns:
            最新天气数据
        """
        params = {"session_key": session_key}
        data = self._make_request("weather", params)
        
        if not data:
            return None
        
        # 返回最新的天气数据
        return data[-1] if data else None
    
    def get_race_control(self, session_key: int) -> List[Dict[str, Any]]:
        """
        获取赛事控制事件（适合赛中推送）
        
        Args:
            session_key: 会话唯一标识
            
        Returns:
            赛事控制事件列表（旗帜、安全车等）
        """
        params = {"session_key": session_key}
        data = self._make_request("race_control", params)
        return data if data else []
    
    def get_latest_race_control_events(self, session_key: int, 
                                       minutes: int = 5) -> List[Dict[str, Any]]:
        """
        获取最近N分钟的赛事控制事件
        
        Args:
            session_key: 会话唯一标识
            minutes: 最近多少分钟，默认5分钟
            
        Returns:
            最近的赛事控制事件
        """
        events = self.get_race_control(session_key)
        if not events:
            return []
        
        # 计算时间阈值（event_time 为 aware UTC，now 必须同为 aware，否则比较抛 TypeError）
        now = datetime.now(timezone.utc)
        threshold = now - timedelta(minutes=minutes)

        # 筛选最近的事件
        recent_events = []
        for event in events:
            try:
                event_time = datetime.fromisoformat((event.get("date") or "").replace("Z", "+00:00"))
                if event_time >= threshold:
                    recent_events.append(event)
            except (TypeError, ValueError):
                continue
        
        return recent_events
    
    def get_fastest_lap(self, session_key: int) -> Optional[Dict[str, Any]]:
        """
        获取当前最快圈速（适合赛中/赛后推送）
        
        Args:
            session_key: 会话唯一标识
            
        Returns:
            最快圈速信息
        """
        params = {"session_key": session_key}
        data = self._make_request("laps", params)
        
        if not data:
            return None
        
        # 找到最快圈
        fastest_lap = None
        for lap in data:
            lap_time = lap.get("lap_duration")
            if lap_time and (not fastest_lap or lap_time < fastest_lap["lap_duration"]):
                fastest_lap = lap
        
        return fastest_lap
    
    def get_pit_stops(self, session_key: int) -> List[Dict[str, Any]]:
        """
        获取进站数据（适合赛后分析）
        
        Args:
            session_key: 会话唯一标识
            
        Returns:
            进站数据列表
        """
        params = {"session_key": session_key}
        data = self._make_request("pit", params)
        return data if data else []
    
    def get_latest_session_key(self) -> Optional[int]:
        """
        获取最新的会话标识符

        Returns:
            最新会话的session_key
        """
        # 使用 "latest" 参数获取最新会话
        params = {"session_key": "latest"}
        data = self._make_request("sessions", params)

        if data and len(data) > 0:
            return data[0].get("session_key")

        return None

    def get_driver_meta(self) -> Dict[str, Dict[str, str]]:
        """
        车手元数据（头像URL/队色/车号/姓名缩写），供推送卡片视觉增强。
        数据源：OpenF1 /v1/drivers（最近一场 session 的阵容）；同一会话内同一车手的
        多条记录按最新日期优先。缓存 30 天（头像/队色极少变化）。
        Returns:
            {姓氏小写: {"headshot": url, "team_colour": "RRGGBB", "number": int,
                        "acronym": str, "full_name": str}}
        """
        from .f1_api import LocalCache
        cache = LocalCache()
        cached = cache.load("openf1_driver_meta")
        if cached is not None:
            return cached

        session_key = self.get_latest_session_key()
        if not session_key:
            return {}
        data = self._make_request("drivers", {"session_key": session_key})
        if not data:
            return {}

        meta: Dict[str, Dict[str, str]] = {}
        for d in data:
            last = (d.get("last_name") or "").strip().lower()
            if not last:
                continue
            # 同姓氏（如历史上兄弟/父子同队）取日期较新的记录
            existing = meta.get(last)
            if existing and (existing.get("_date") or "") > (d.get("date") or ""):
                continue
            meta[last] = {
                "headshot": d.get("headshot_url"),  # 可能为 None（如角田），代理由 F1.com 规则构造兜底
                "team_colour": d.get("team_colour") or "",
                "number": d.get("driver_number"),
                "acronym": d.get("name_acronym") or "",
                "full_name": d.get("full_name") or "",
                "_date": d.get("date") or "",
            }
        for v in meta.values():
            v.pop("_date", None)
        if meta:
            cache.save("openf1_driver_meta", meta, ttl_hours=30 * 24)
            logger.info(f"✓ OpenF1 车手元数据已缓存: {len(meta)} 人")
        return meta

    @staticmethod
    def _resolve_surname(driver_name: str) -> Optional[str]:
        """把车手姓名（中/英/带重音/全名/别名）解析为姓氏键（与打包文件 web/static/avatars/
        drivers/<surname>.png 对齐）。以 drivers_profile 为准（含全部 23 车手），
        重音归一化（Pérez→perez），解决 OpenF1 元数据缺 hadjar / 重音不匹配导致无照片"""
        import unicodedata
        def norm(s: str) -> str:
            return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                           if not unicodedata.combining(c)).lower().replace(" ", "")
        q = norm(driver_name)
        if not q:
            return None
        try:
            from . import drivers_profile as dp
            for did, p in dp.get_drivers().items():
                surname = did.split("_")[-1].lower()
                name_en = norm(p.get("name_en") or "")
                # 输入即姓氏 / 姓氏是输入子串（Pérez→perez）/ 输入含完整姓名
                if q == surname or surname in q or (name_en and name_en in q):
                    return surname
        except Exception:
            pass
        # 中文别名兜底：维斯塔潘 → max_verstappen → verstappen
        try:
            from .drivers_profile import get_driver_aliases
            for alias, canon in get_driver_aliases().items():
                if alias and norm(alias) in q:
                    return canon.split("_")[-1].lower()
        except Exception:
            pass
        return None

    @staticmethod
    def avatar_md(meta: Dict[str, Dict[str, str]], driver_name: str,
                  px: int = 28) -> str:
        """按车手姓名生成 QQ Markdown 头像语法（走自建代理，代理负责源解析/缓存/透明兜底）"""
        surname = OpenF1API._resolve_surname(driver_name)
        if not surname:
            return ""
        base = (os.getenv("AVATAR_BASE_URL") or os.getenv("RATING_BASE_URL")
                or "https://your-domain.example.com").rstrip("/")
        # Caddy 静态服务：/av{ver}/drivers/{surname}.png（绕过 gunicorn，见 Caddyfile）
        return f"![pic#{px}px #{px}px]({base}/av{_AVATAR_PATH_VER}/drivers/{surname}.png)"

    def format_weather_for_chat(self, weather: Dict[str, Any]) -> str:
        """
        格式化天气数据为群聊消息

        Args:
            weather: 天气数据

        Returns:
            格式化后的消息
        """
        if not weather:
            return "暂无天气数据"

        air_temp = weather.get("air_temperature", "N/A")
        track_temp = weather.get("track_temperature", "N/A")
        humidity = weather.get("humidity", "N/A")
        rainfall = weather.get("rainfall", 0)
        wind_speed = weather.get("wind_speed", "N/A")
        pressure = weather.get("pressure", "N/A")

        rain_status = "降雨中" if rainfall else "无降雨"

        return (
            f"赛道天气:\n"
            f"气温: {air_temp}°C\n"
            f"赛道温度: {track_temp}°C\n"
            f"湿度: {humidity}%\n"
            f"气压: {pressure}mbar\n"
            f"风速: {wind_speed}m/s\n"
            f"降雨: {rain_status}"
        )

    def format_race_control_event(self, event: Dict[str, Any]) -> str:
        """
        格式化赛事控制事件为群聊消息

        Args:
            event: 赛事控制事件

        Returns:
            格式化后的消息
        """
        if not event:
            return ""

        category = event.get("category", "")
        message = event.get("message", "")
        flag = event.get("flag", "")

        # 根据不同类别格式化
        if category == "Flag":
            flag_emojis = {
                "GREEN": "",
                "YELLOW": "",
                "DOUBLE YELLOW": "",
                "RED": "",
                "CHEQUERED": "",
                "SAFETY CAR": "",
                "VIRTUAL SAFETY CAR": ""
            }
            emoji = flag_emojis.get(flag, "")
            return f"{emoji} {message}"

        elif category == "SafetyCar":
            return f" {message}"

        elif category == "CarEvent":
            return f" {message}"

        else:
            return f"{message}"

    def format_fastest_lap(self, lap: Dict[str, Any], driver_name: str = None) -> str:
        """
        格式化最快圈速为群聊消息

        Args:
            lap: 圈速数据
            driver_name: 车手姓名

        Returns:
            格式化后的消息
        """
        if not lap:
            return "暂无最快圈速数据"

        driver_num = lap.get("driver_number", "N/A")
        lap_time = lap.get("lap_duration", 0)
        lap_num = lap.get("lap_number", "N/A")

        # 格式化圈速时间
        if lap_time:
            minutes = int(lap_time // 60)
            seconds = lap_time % 60
            time_str = f"{minutes}:{seconds:05.2f}"
        else:
            time_str = "N/A"

        if driver_name:
            return f"⚡ 最快圈速: {driver_name} (#{driver_num}) - {time_str} (第{lap_num}圈)"
        else:
            return f"⚡ 最快圈速: #{driver_num} - {time_str} (第{lap_num}圈)"

    # ==================== 实时数据（实时计时面板用） ====================

    def get_position(self, session_key: int, date_from: str = None) -> List[Dict[str, Any]]:
        """
        获取实时车手位置
        参数: session_key, date_from(ISO格式, 如 2026-07-26T00:00:00+00:00)
        """
        params = {"session_key": session_key}
        if date_from:
            params["date>"] = date_from
        data = self._make_request("position", params)
        return data if data else []

    def get_intervals(self, session_key: int, date_from: str = None) -> List[Dict[str, Any]]:
        """
        获取实时车距（gap_to_leader / interval）
        注意: 数据量大, 必须传 date_from 时间窗过滤, 否则会超时
        """
        params = {"session_key": session_key}
        if date_from:
            params["date>"] = date_from
        data = self._make_request("intervals", params)
        return data if data else []

    def get_stints(self, session_key: int) -> List[Dict[str, Any]]:
        """获取轮胎进站策略 (compound / lap_start / lap_end / stint_number)"""
        params = {"session_key": session_key}
        data = self._make_request("stints", params)
        return data if data else []

    def get_laps(self, session_key: int, date_from: str = None) -> List[Dict[str, Any]]:
        """获取圈速数据 (lap_duration / lap_number / driver_number)，可用时间窗过滤"""
        params = {"session_key": session_key}
        if date_from:
            params["date>"] = date_from
        data = self._make_request("laps", params)
        return data if data else []

    def get_team_radio(self, session_key: int, date_from: str = None) -> List[Dict[str, Any]]:
        """
        获取车队无线电（仅音频 recording_url + driver_number + date, 无文字稿）
        """
        params = {"session_key": session_key}
        if date_from:
            params["date>"] = date_from
        data = self._make_request("team_radio", params)
        return data if data else []

    def get_drivers(self, session_key: int) -> List[Dict[str, Any]]:
        """获取车手名单 (driver_number -> full_name / name_acronym / team_name)"""
        params = {"session_key": session_key}
        data = self._make_request("drivers", params)
        return data if data else []

    def get_current_session(self, year: int = None, allow_replay: bool = False) -> Optional[Dict[str, Any]]:
        """
        找到当前正在进行的 session。
        allow_replay=True 时，无进行中则返回最近一个已结束的 session（用于回放）。
        """
        from datetime import timezone as _tz
        sessions = self.get_sessions(year=year)
        if not sessions:
            return None
        now = datetime.now(_tz.utc)

        def _parse(s):
            try:
                return (datetime.fromisoformat(s["date_start"].replace("Z", "+00:00")),
                        datetime.fromisoformat(s["date_end"].replace("Z", "+00:00")))
            except Exception:
                return None

        active = None
        recent = None
        for s in sessions:
            p = _parse(s)
            if not p:
                continue
            start, end = p
            if start <= now <= end:
                active = s
                break
            if allow_replay and end < now and (recent is None or end > recent[0]):
                recent = (end, s)

        return active or (recent[1] if recent else None)



# ==================== 模块级头像便捷入口 ====================

# 头像 URL 路径版本：QQ 图片代理按 URL 长期缓存（含失败结果与旧图片内容），
# 每次头像内容批量更新必须递增版本号破缓存。URL 前缀为 /av{版本号}/（Caddy 静态直出）。
_AVATAR_PATH_VER = "8"


def get_avatar_md(driver_name: str, px: int = 28) -> str:
    """按车手姓名生成 QQ Markdown 头像语法（未命中/失败返回空串，绝不抛异常）"""
    try:
        return OpenF1API.avatar_md(None, driver_name, px)
    except Exception:
        return ""


def get_team_icon_md(team_name: str, px: int = 28) -> str:
    """按车队名生成 QQ Markdown 车队图标语法（打包静态图标，未命中返回空串）"""
    if not team_name:
        return ""
    try:
        from .f1cosmos_api import canonical_team_key
        key = canonical_team_key(str(team_name))
        if not key:
            return ""
        base = (os.getenv("AVATAR_BASE_URL") or os.getenv("RATING_BASE_URL")
                or "https://your-domain.example.com").rstrip("/")
        # Caddy 静态服务：/av{ver}/teams/{key}.png（绕过 gunicorn，见 Caddyfile）
        return f"![pic#{px}px #{px}px]({base}/av{_AVATAR_PATH_VER}/teams/{key}.png)"
    except Exception:
        return ""


# 兼容旧代码，提供统一接口
class F1API(OpenF1API):
    """向后兼容的别名"""
    pass


# 测试代码
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    from datetime import datetime
    current_year = datetime.now().year
    
    print("=" * 60)
    print("OpenF1 API 测试")
    print(f"当前年份: {current_year}")
    print("=" * 60)
    
    api = OpenF1API()
    
    # 测试1: 获取当前赛季会议
    print(f"\n1. 获取{current_year}赛季会议...")
    meetings = api.get_meetings(current_year)
    print(f"   共 {len(meetings)} 个会议")
    if meetings:
        print(f"   首场比赛: {meetings[0]['meeting_name']}")
    
    # 测试2: 获取会话
    if meetings:
        print("\n2. 获取会话列表...")
        meeting_key = meetings[0]["meeting_key"]
        sessions = api.get_sessions(meeting_key=meeting_key)
        print(f"   共 {len(sessions)} 个会话")
        if sessions:
            print(f"   首个会话: {sessions[0]['session_name']}")
            session_key = sessions[0]["session_key"]
            
            # 测试3: 获取天气
            print("\n3. 获取天气数据...")
            weather = api.get_weather(session_key)
            if weather:
                print(f"   气温: {weather.get('air_temperature')}°C")
                print(f"   赛道温度: {weather.get('track_temperature')}°C")
                print(f"   降雨: {'是' if weather.get('rainfall') else '否'}")
                print("\n   群聊格式:")
                print(api.format_weather_for_chat(weather))
            else:
                print("   暂无天气数据")
    
    print("\n" + "=" * 60)
    print("测试完成!")
    print("=" * 60)
