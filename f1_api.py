"""
F1赛程提醒机器人 - F1数据API模块
使用Ergast F1 API获取赛程数据
API文档: http://ergast.com/mrd/
"""

import requests
import json
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
import logging

logger = logging.getLogger(__name__)

BASE_URL = "https://ergast.com/api/f1"

class F1API:
    """F1数据API封装类"""
    
    def __init__(self, season: int = None):
        """
        初始化F1 API客户端
        
        Args:
            season: 赛季年份，默认为当前年份
        """
        self.season = season or datetime.now().year
        
    def _make_request(self, endpoint: str) -> Optional[Dict]:
        """
        发送HTTP请求到Ergast API
        
        Args:
            endpoint: API端点路径
            
        Returns:
            API响应数据，失败返回None
        """
        url = f"{BASE_URL}/{endpoint}.json"
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"API请求失败: {url}, 错误: {e}")
            return None
    
    def get_schedule(self) -> List[Dict[str, Any]]:
        """
        获取赛季完整赛程
        
        Returns:
            比赛列表，每个比赛包含完整信息
        """
        data = self._make_request(f"{self.season}")
        if not data or "MRData" not in data:
            logger.error("无法获取赛程数据")
            return []
        
        races = data["MRData"]["RaceTable"]["Races"]
        logger.info(f"成功获取 {self.season} 赛季赛程，共 {len(races)} 场比赛")
        return races
    
    def get_next_race(self) -> Optional[Dict[str, Any]]:
        """
        获取下一场比赛信息
        
        Returns:
            下一场比赛的详细信息
        """
        data = self._make_request(f"{self.season}/next")
        if not data or "MRData" not in data:
            return None
        
        races = data["MRData"]["RaceTable"]["Races"]
        return races[0] if races else None
    
    def get_race_results(self, round_num: int) -> Optional[Dict[str, Any]]:
        """
        获取某场比赛的结果
        
        Args:
            round_num: 比赛轮次
            
        Returns:
            比赛结果数据
        """
        data = self._make_request(f"{self.season}/{round_num}/results")
        if not data or "MRData" not in data:
            return None
        
        races = data["MRData"]["RaceTable"]["Races"]
        return races[0] if races else None
    
    def get_current_standings(self) -> Dict[str, Any]:
        """
        获取当前积分榜
        
        Returns:
            车手和车队积分榜
        """
        driver_data = self._make_request(f"{self.season}/driverStandings")
        constructor_data = self._make_request(f"{self.season}/constructorStandings")
        
        return {
            "drivers": driver_data,
            "constructors": constructor_data
        }
    
    @staticmethod
    def parse_session_datetime(date_str: str, time_str: str) -> datetime:
        """
        解析比赛日期和时间
        
        Args:
            date_str: 日期字符串 (YYYY-MM-DD)
            time_str: 时间字符串 (HH:MM:SSZ)
            
        Returns:
            UTC时区的datetime对象
        """
        # 去除时间字符串末尾的Z
        time_str = time_str.replace("Z", "")
        datetime_str = f"{date_str}T{time_str}"
        return datetime.fromisoformat(datetime_str)
    
    def get_all_sessions(self, race: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        从比赛数据中提取所有环节（练习赛、排位赛、冲刺赛、正赛）
        
        Args:
            race: 单场比赛数据
            
        Returns:
            所有环节列表，每个环节包含类型、时间、名称
        """
        sessions = []
        
        # 练习赛
        for i in range(1, 4):
            fp_key = f"FirstPractice" if i == 1 else f"SecondPractice" if i == 2 else f"ThirdPractice"
            if fp_key in race:
                session = race[fp_key]
                sessions.append({
                    "type": f"fp{i}",
                    "name": f"第{i}节练习赛",
                    "date": session["date"],
                    "time": session["time"],
                    "datetime": self.parse_session_datetime(session["date"], session["time"]),
                    "circuit": race["Circuit"]["circuitName"],
                    "race_name": race["raceName"],
                    "round": race["round"]
                })
        
        # 排位赛
        if "Qualifying" in race:
            qual = race["Qualifying"]
            sessions.append({
                "type": "qualifying",
                "name": "排位赛",
                "date": qual["date"],
                "time": qual["time"],
                "datetime": self.parse_session_datetime(qual["date"], qual["time"]),
                "circuit": race["Circuit"]["circuitName"],
                "race_name": race["raceName"],
                "round": race["round"]
            })
        
        # 冲刺赛（如果有）
        if "Sprint" in race:
            sprint = race["Sprint"]
            sessions.append({
                "type": "sprint",
                "name": "冲刺赛",
                "date": sprint["date"],
                "time": sprint["time"],
                "datetime": self.parse_session_datetime(sprint["date"], sprint["time"]),
                "circuit": race["Circuit"]["circuitName"],
                "race_name": race["raceName"],
                "round": race["round"]
            })
        
        # 正赛
        sessions.append({
            "type": "race",
            "name": "正赛",
            "date": race["date"],
            "time": race["time"],
            "datetime": self.parse_session_datetime(race["date"], race["time"]),
            "circuit": race["Circuit"]["circuitName"],
            "race_name": race["raceName"],
            "round": race["round"]
        })
        
        # 按时间排序
        sessions.sort(key=lambda x: x["datetime"])
        
        return sessions
    
    def get_upcoming_sessions(self, hours_ahead: int = 168) -> List[Dict[str, Any]]:
        """
        获取未来指定小时内的所有比赛环节
        
        Args:
            hours_ahead: 提前多少小时，默认一周(168小时)
            
        Returns:
            即将进行的比赛环节列表
        """
        from datetime import timezone
        
        schedule = self.get_schedule()
        now = datetime.now(timezone.utc)
        cutoff = now + timedelta(hours=hours_ahead)
        
        upcoming = []
        for race in schedule:
            sessions = self.get_all_sessions(race)
            for session in sessions:
                if now <= session["datetime"] <= cutoff:
                    upcoming.append(session)
        
        return upcoming


# 使用示例
if __name__ == "__main__":
    # 测试API
    logging.basicConfig(level=logging.INFO)
    
    f1 = F1API(2024)
    
    # 获取完整赛程
    print("=== 获取赛程 ===")
    schedule = f1.get_schedule()
    print(f"共 {len(schedule)} 场比赛")
    
    # 获取下一场比赛
    print("\n=== 下一场比赛 ===")
    next_race = f1.get_next_race()
    if next_race:
        print(f"比赛: {next_race['raceName']}")
        print(f"赛道: {next_race['Circuit']['circuitName']}")
        print(f"日期: {next_race['date']}")
    
    # 获取即将进行的环节
    print("\n=== 即将进行的环节 ===")
    upcoming = f1.get_upcoming_sessions(72)  # 未来3天
    for session in upcoming:
        print(f"{session['race_name']} - {session['name']}: {session['datetime']}")