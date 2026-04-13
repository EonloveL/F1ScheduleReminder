"""
F1赛程提醒机器人 - OpenF1 API增强模块
提供实时数据：天气、赛事控制、圈速等
API文档: https://openf1.org
注意: 仅支持2023年及以后的数据
"""

import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
import logging

logger = logging.getLogger(__name__)

BASE_URL = "https://api.openf1.org/v1"


class OpenF1API:
    """OpenF1 API 封装类 - 提供实时F1数据"""
    
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'F1-Reminder-Bot/1.0'
        })
    
    def _make_request(self, endpoint: str, params: Dict = None) -> Optional[List[Dict]]:
        """
        发送HTTP请求到 OpenF1 API
        
        Args:
            endpoint: API端点
            params: 查询参数
            
        Returns:
            API响应数据列表，失败返回None
        """
        url = f"{BASE_URL}/{endpoint}"
        try:
            response = self.session.get(url, params=params, timeout=10)
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
        
        # 计算时间阈值
        now = datetime.now()
        threshold = now - timedelta(minutes=minutes)
        
        # 筛选最近的事件
        recent_events = []
        for event in events:
            try:
                event_time = datetime.fromisoformat(event.get("date", "").replace("Z", "+00:00"))
                if event_time >= threshold:
                    recent_events.append(event)
            except:
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
        
        rain_status = "降雨中" if rainfall else "无降雨"
        
        return (
            f"赛道天气:\n"
            f"气温: {air_temp}°C\n"
            f"赛道温度: {track_temp}°C\n"
            f"湿度: {humidity}%\n"
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
