"""
F1赛程提醒机器人 - 共用核心模块
F1数据API - 智能故障转移版本
主API: f1api.dev
备用API: Jolpica (Ergast兼容)
本地缓存: JSON文件
"""

import requests
import json
import os
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Any, Tuple
import logging

logger = logging.getLogger(__name__)

# 缓存目录 - 相对于项目根目录
def _get_cache_dir():
    """获取缓存目录路径"""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(current_dir)
    cache_dir = os.path.join(project_root, "data", "cache")
    os.makedirs(cache_dir, exist_ok=True)
    return cache_dir

CACHE_DIR = _get_cache_dir()


class LocalCache:
    """本地JSON缓存管理器"""
    
    def __init__(self, cache_dir: str = None):
        self.cache_dir = cache_dir or CACHE_DIR
        os.makedirs(self.cache_dir, exist_ok=True)
    
    def save(self, key: str, data: Any, ttl_hours: Optional[int] = None) -> None:
        """
        保存数据到本地JSON
        
        Args:
            key: 缓存键
            data: 要缓存的数据
            ttl_hours: 过期时间（小时），None表示永不过期
        """
        cache_file = os.path.join(self.cache_dir, f"{key}.json")
        cache_data = {
            "data": data,
            "cached_at": datetime.now().isoformat(),
            "expires_at": (datetime.now() + timedelta(hours=ttl_hours)).isoformat() if ttl_hours else None
        }
        try:
            with open(cache_file, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
            logger.debug(f"缓存已保存: {key}")
        except Exception as e:
            logger.error(f"保存缓存失败 {key}: {e}")
    
    def load(self, key: str) -> Optional[Any]:
        """
        从本地加载数据，自动检查过期
        
        Args:
            key: 缓存键
            
        Returns:
            缓存数据，过期或不存在返回None
        """
        cache_file = os.path.join(self.cache_dir, f"{key}.json")
        if not os.path.exists(cache_file):
            return None
        
        try:
            with open(cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
            
            # 检查是否过期
            expires_at = cache_data.get("expires_at")
            if expires_at:
                expiry = datetime.fromisoformat(expires_at)
                if datetime.now() > expiry:
                    logger.debug(f"缓存已过期: {key}")
                    return None
            
            logger.debug(f"缓存已加载: {key}")
            return cache_data.get("data")
        except Exception as e:
            logger.error(f"加载缓存失败 {key}: {e}")
            return None
    
    def clear(self, key: Optional[str] = None) -> None:
        """
        清除缓存
        
        Args:
            key: 指定键清除，None清除所有
        """
        if key:
            cache_file = os.path.join(self.cache_dir, f"{key}.json")
            if os.path.exists(cache_file):
                os.remove(cache_file)
                logger.info(f"缓存已清除: {key}")
        else:
            for filename in os.listdir(self.cache_dir):
                if filename.endswith('.json'):
                    os.remove(os.path.join(self.cache_dir, filename))
            logger.info("所有缓存已清除")


class BaseF1Provider(ABC):
    """F1数据Provider基类"""
    
    def __init__(self, name: str):
        self.name = name
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'F1-Reminder-Bot/1.0'
        })
    
    def _make_request(self, url: str, timeout: int = 10) -> Optional[Dict]:
        """
        发送HTTP请求
        
        Args:
            url: 请求URL
            timeout: 超时时间
            
        Returns:
            API响应数据，失败返回None
        """
        try:
            response = self.session.get(url, timeout=timeout)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.warning(f"[{self.name}] 请求失败: {url}, 错误: {e}")
            return None
    
    @abstractmethod
    def get_schedule(self, season: int) -> List[Dict[str, Any]]:
        """获取赛季赛程"""
        pass
    
    @abstractmethod
    def get_race_results(self, season: int, round_num: int) -> Optional[Dict[str, Any]]:
        """获取比赛结果"""
        pass
    
    @abstractmethod
    def get_standings(self, season: int) -> Dict[str, Any]:
        """获取积分榜"""
        pass


class F1APIDevProvider(BaseF1Provider):
    """
    f1api.dev Provider
    主API，数据丰富，包含获胜者、最快圈等信息
    """
    
    BASE_URL = "https://f1api.dev/api"
    
    def __init__(self):
        super().__init__("f1api.dev")
    
    def _convert_race_format(self, race: Dict[str, Any], season: int) -> Dict[str, Any]:
        """
        将 f1api.dev 格式转换为标准格式
        
        Args:
            race: f1api.dev 格式的比赛数据
            season: 赛季年份
            
        Returns:
            标准格式的比赛数据
        """
        schedule = race.get("schedule", {})
        circuit = race.get("circuit", {})
        
        converted = {
            "season": str(season),
            "round": str(race.get("round", "")),
            "raceName": race.get("raceName", ""),
            "url": race.get("url", ""),
            "date": schedule.get("race", {}).get("date", ""),
            "time": schedule.get("race", {}).get("time", ""),
            "Circuit": {
                "circuitId": circuit.get("circuitId", ""),
                "url": circuit.get("url", ""),
                "circuitName": circuit.get("circuitName", ""),
                "Location": {
                    "lat": "",
                    "long": "",
                    "locality": circuit.get("city", ""),
                    "country": circuit.get("country", "")
                }
            },
            "_source": "f1api.dev",
            "_f1api_dev": {
                "raceId": race.get("raceId", ""),
                "winner": race.get("winner"),
                "teamWinner": race.get("teamWinner"),
                "fastLap": race.get("fast_lap"),
                "laps": race.get("laps")
            }
        }
        
        # 练习赛
        fp1 = schedule.get("fp1")
        if fp1 and fp1.get("date"):
            converted["FirstPractice"] = {"date": fp1["date"], "time": fp1.get("time", "")}
        
        fp2 = schedule.get("fp2")
        if fp2 and fp2.get("date"):
            converted["SecondPractice"] = {"date": fp2["date"], "time": fp2.get("time", "")}
        
        fp3 = schedule.get("fp3")
        if fp3 and fp3.get("date"):
            converted["ThirdPractice"] = {"date": fp3["date"], "time": fp3.get("time", "")}
        
        # 排位赛
        qualy = schedule.get("qualy")
        if qualy and qualy.get("date"):
            converted["Qualifying"] = {"date": qualy["date"], "time": qualy.get("time", "")}
        
        # 冲刺赛
        sprint = schedule.get("sprintRace")
        if sprint and sprint.get("date"):
            converted["Sprint"] = {"date": sprint["date"], "time": sprint.get("time", "")}
        
        return converted
    
    def get_schedule(self, season: int) -> List[Dict[str, Any]]:
        """获取赛季赛程"""
        url = f"{self.BASE_URL}/{season}"
        data = self._make_request(url)
        
        if not data or "races" not in data:
            logger.error(f"[{self.name}] 无法获取 {season} 赛季赛程")
            return []
        
        races = data["races"]
        converted = [self._convert_race_format(race, season) for race in races]
        logger.info(f"[{self.name}] 成功获取 {season} 赛季赛程，共 {len(converted)} 场比赛")
        return converted
    
    def get_race_results(self, season: int, round_num: int) -> Optional[Dict[str, Any]]:
        """获取比赛结果"""
        url = f"{self.BASE_URL}/{season}/{round_num}/race"
        data = self._make_request(url)
        
        if not data or "races" not in data or not data["races"]:
            logger.warning(f"[{self.name}] 无法获取比赛结果 {season} 第 {round_num} 站")
            return None
        
        return self._convert_race_format(data["races"][0], season)
    
    def get_standings(self, season: int) -> Dict[str, Any]:
        """获取积分榜"""
        driver_data = self._make_request(f"{self.BASE_URL}/{season}/drivers/championship")
        constructor_data = self._make_request(f"{self.BASE_URL}/{season}/constructors/championship")
        
        return {
            "drivers": driver_data,
            "constructors": constructor_data,
            "_source": "f1api.dev"
        }


class JolpicaProvider(BaseF1Provider):
    """
    Jolpica Provider
    备用API，Ergast完全兼容格式
    """
    
    BASE_URL = "https://api.jolpi.ca/ergast/f1"
    
    def __init__(self):
        super().__init__("Jolpica")
    
    def get_schedule(self, season: int) -> List[Dict[str, Any]]:
        """获取赛季赛程"""
        url = f"{self.BASE_URL}/{season}.json"
        data = self._make_request(url)
        
        if not data or "MRData" not in data:
            logger.error(f"[{self.name}] 无法获取 {season} 赛季赛程")
            return []
        
        races = data["MRData"]["RaceTable"]["Races"]
        # 添加来源标记
        for race in races:
            race["_source"] = "Jolpica"
        
        logger.info(f"[{self.name}] 成功获取 {season} 赛季赛程，共 {len(races)} 场比赛")
        return races
    
    def get_race_results(self, season: int, round_num: int) -> Optional[Dict[str, Any]]:
        """获取比赛结果"""
        url = f"{self.BASE_URL}/{season}/{round_num}/results.json"
        data = self._make_request(url)
        
        if not data or "MRData" not in data:
            logger.warning(f"[{self.name}] 无法获取比赛结果 {season} 第 {round_num} 站")
            return None
        
        races = data["MRData"]["RaceTable"]["Races"]
        if not races:
            return None
        
        race = races[0]
        race["_source"] = "Jolpica"
        return race
    
    def get_standings(self, season: int) -> Dict[str, Any]:
        """获取积分榜"""
        driver_data = self._make_request(f"{self.BASE_URL}/{season}/driverStandings.json")
        constructor_data = self._make_request(f"{self.BASE_URL}/{season}/constructorStandings.json")
        
        return {
            "drivers": driver_data,
            "constructors": constructor_data,
            "_source": "Jolpica"
        }


class F1APIManager:
    """
    F1 API 智能管理器
    自动故障转移：f1api.dev → Jolpica → 本地缓存
    增强功能：OpenF1 实时数据（天气、赛事控制、圈速）
    """
    
    def __init__(self, season: int = None):
        """
        初始化API管理器
        
        Args:
            season: 赛季年份，默认为当前年份
        """
        self.season = season or datetime.now().year
        self.cache = LocalCache()
        
        # 初始化所有Provider
        self.providers: List[BaseF1Provider] = [
            F1APIDevProvider(),    # 主API
            JolpicaProvider(),      # 备用API #1
        ]
        
        # 初始化OpenF1客户端（用于增强功能）
        try:
            # 使用绝对导入，兼容不同运行方式
            import sys
            if __name__ == "__main__":
                from openf1_api import OpenF1API
            else:
                from .openf1_api import OpenF1API
            self.openf1 = OpenF1API()
            self.openf1_enabled = True
            logger.info("OpenF1 API 已启用")
        except ImportError as e:
            self.openf1_enabled = False
            logger.warning(f"OpenF1 API 不可用: {e}")
        
        logger.info(f"F1APIManager 初始化完成，赛季: {self.season}")
    
    def _execute_with_fallback(self, operation: str, cache_key: str, 
                               provider_method: str, *args, 
                               cache_ttl: Optional[int] = None) -> Tuple[Any, str]:
        """
        执行操作并自动故障转移
        
        Args:
            operation: 操作名称（用于日志）
            cache_key: 缓存键
            provider_method: Provider方法名
            *args: 方法参数
            cache_ttl: 缓存TTL（小时）
            
        Returns:
            (数据, 来源)
        """
        # 1. 尝试从缓存加载
        cached_data = self.cache.load(cache_key)
        if cached_data:
            logger.info(f"[{operation}] 从缓存加载数据")
            source = cached_data.get("_source", "cache") if isinstance(cached_data, dict) else "cache"
            return cached_data, f"cache ({source})"
        
        # 2. 尝试所有Provider
        for provider in self.providers:
            try:
                method = getattr(provider, provider_method)
                data = method(*args)
                
                # 对于列表类型，检查是否为None而不是检查是否为空
                # 空列表是有效数据（表示该赛季没有比赛）
                if data is not None:
                    # 对于列表，如果是空列表但合法，也接受
                    # 但对于赛程，空列表通常表示API调用失败
                    if isinstance(data, list) and len(data) == 0:
                        logger.warning(f"[{operation}] [{provider.name}] 返回空数据，尝试下一个Provider")
                        continue
                    
                    logger.info(f"[{operation}] 成功从 [{provider.name}] 获取数据")
                    # 保存到缓存
                    self.cache.save(cache_key, data, ttl_hours=cache_ttl)
                    return data, provider.name
                    
            except Exception as e:
                logger.warning(f"[{operation}] [{provider.name}] 失败: {e}")
                continue
        
        # 3. 全部失败，尝试加载过期缓存
        logger.error(f"[{operation}] 所有API均失败")
        
        return None, "failed"
    
    def get_schedule(self, use_cache: bool = True) -> List[Dict[str, Any]]:
        """
        获取赛季完整赛程
        
        Args:
            use_cache: 是否使用缓存
            
        Returns:
            比赛列表
        """
        cache_key = f"schedule_{self.season}"
        
        if use_cache:
            data, source = self._execute_with_fallback(
                "get_schedule", cache_key, 
                "get_schedule", self.season,
                cache_ttl=None  # 赛程永久缓存
            )
        else:
            # 不使用缓存，直接调用主API
            data = self.providers[0].get_schedule(self.season)
            source = self.providers[0].name
            if data:
                self.cache.save(cache_key, data)
        
        if not data:
            logger.error("获取赛程数据失败")
            return []
        
        logger.info(f"get_schedule 完成，来源: {source}")
        return data if isinstance(data, list) else []
    
    def get_next_race(self) -> Optional[Dict[str, Any]]:
        """
        获取下一场比赛
        
        Returns:
            下一场比赛信息
        """
        schedule = self.get_schedule()
        if not schedule:
            return None
        
        now = datetime.now(timezone.utc)
        
        for race in schedule:
            try:
                race_time = race.get("time", "00:00:00Z").replace("Z", "")
                race_datetime_str = f"{race['date']}T{race_time}"
                race_datetime = datetime.fromisoformat(race_datetime_str)
                race_datetime = race_datetime.replace(tzinfo=timezone.utc)
                
                if race_datetime > now:
                    return race
            except Exception as e:
                logger.warning(f"解析比赛日期失败: {e}")
                continue
        
        return None
    
    def get_race_results(self, round_num: int) -> Optional[Dict[str, Any]]:
        """
        获取比赛结果
        
        Args:
            round_num: 比赛轮次
            
        Returns:
            比赛结果
        """
        cache_key = f"race_results_{self.season}_{round_num}"
        data, source = self._execute_with_fallback(
            "get_race_results", cache_key,
            "get_race_results", self.season, round_num,
            cache_ttl=168  # 7天缓存
        )
        
        logger.info(f"get_race_results 完成，来源: {source}")
        return data
    
    def get_current_standings(self) -> Dict[str, Any]:
        """
        获取当前积分榜
        
        Returns:
            车手和车队积分榜
        """
        cache_key = f"standings_{self.season}"
        data, source = self._execute_with_fallback(
            "get_standings", cache_key,
            "get_standings", self.season,
            cache_ttl=72  # 3天缓存
        )
        
        if not data:
            return {"drivers": None, "constructors": None}
        
        logger.info(f"get_current_standings 完成，来源: {source}")
        return data if isinstance(data, dict) else {"drivers": None, "constructors": None}
    
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
        time_str = time_str.replace("Z", "")
        datetime_str = f"{date_str}T{time_str}"
        dt = datetime.fromisoformat(datetime_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    
    def get_all_sessions(self, race: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        从比赛数据中提取所有环节
        
        Args:
            race: 单场比赛数据
            
        Returns:
            所有环节列表
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
        
        # 冲刺赛
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
        
        sessions.sort(key=lambda x: x["datetime"])
        return sessions
    
    def get_upcoming_sessions(self, hours_ahead: int = 168) -> List[Dict[str, Any]]:
        """
        获取未来指定小时内的所有比赛环节
        
        Args:
            hours_ahead: 提前多少小时，默认一周
            
        Returns:
            即将进行的比赛环节列表
        """
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
    
    # ==================== OpenF1 增强功能 ====================
    
    def get_weather_for_session(self, session_key: int) -> Optional[Dict[str, Any]]:
        """
        获取赛事天气数据（OpenF1增强功能）
        
        Args:
            session_key: OpenF1会话标识符
            
        Returns:
            天气数据
        """
        if not self.openf1_enabled:
            logger.warning("OpenF1 API 未启用，无法获取天气数据")
            return None
        
        try:
            weather = self.openf1.get_weather(session_key)
            if weather:
                logger.info(f"成功获取天气数据，session_key: {session_key}")
                return weather
        except Exception as e:
            logger.error(f"获取天气数据失败: {e}")
        
        return None
    
    def format_weather_for_chat(self, weather: Dict[str, Any]) -> str:
        """
        格式化天气数据为群聊消息
        
        Args:
            weather: 天气数据
            
        Returns:
            格式化后的消息
        """
        if not self.openf1_enabled or not weather:
            return "暂无天气数据"
        
        return self.openf1.format_weather_for_chat(weather)
    
    def get_race_control_events(self, session_key: int, minutes: int = 5) -> List[Dict[str, Any]]:
        """
        获取最近N分钟的赛事控制事件（OpenF1增强功能）
        
        Args:
            session_key: OpenF1会话标识符
            minutes: 最近多少分钟，默认5分钟
            
        Returns:
            赛事控制事件列表
        """
        if not self.openf1_enabled:
            logger.warning("OpenF1 API 未启用，无法获取赛事控制事件")
            return []
        
        try:
            events = self.openf1.get_latest_race_control_events(session_key, minutes)
            if events:
                logger.info(f"成功获取 {len(events)} 个赛事控制事件")
                return events
        except Exception as e:
            logger.error(f"获取赛事控制事件失败: {e}")
        
        return []
    
    def format_race_control_event(self, event: Dict[str, Any]) -> str:
        """
        格式化赛事控制事件为群聊消息
        
        Args:
            event: 赛事控制事件
            
        Returns:
            格式化后的消息
        """
        if not self.openf1_enabled or not event:
            return ""
        
        return self.openf1.format_race_control_event(event)
    
    def get_fastest_lap(self, session_key: int) -> Optional[Dict[str, Any]]:
        """
        获取当前最快圈速（OpenF1增强功能）
        
        Args:
            session_key: OpenF1会话标识符
            
        Returns:
            最快圈速数据
        """
        if not self.openf1_enabled:
            logger.warning("OpenF1 API 未启用，无法获取最快圈速")
            return None
        
        try:
            lap = self.openf1.get_fastest_lap(session_key)
            if lap:
                logger.info(f"成功获取最快圈速")
                return lap
        except Exception as e:
            logger.error(f"获取最快圈速失败: {e}")
        
        return None
    
    def format_fastest_lap(self, lap: Dict[str, Any], driver_name: str = None) -> str:
        """
        格式化最快圈速为群聊消息
        
        Args:
            lap: 圈速数据
            driver_name: 车手姓名
            
        Returns:
            格式化后的消息
        """
        if not self.openf1_enabled or not lap:
            return "暂无最快圈速数据"
        
        return self.openf1.format_fastest_lap(lap, driver_name)
F1API = F1APIManager


# 使用示例
if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )
    
    # 使用当前年份
    current_year = datetime.now().year
    
    print("=" * 60)
    print("F1 API 智能故障转移测试")
    print(f"当前年份: {current_year}")
    print("=" * 60)
    
    f1 = F1API(current_year)
    
    # 测试 1: 获取赛程
    print("\n1. 测试获取赛程...")
    schedule = f1.get_schedule(use_cache=False)
    print(f"   成功: {len(schedule)} 场比赛")
    if schedule:
        print(f"   首场比赛: {schedule[0]['raceName']}")
        print(f"   来源: {schedule[0].get('_source', 'unknown')}")
    
    # 测试 2: 测试缓存
    print("\n2. 测试缓存机制...")
    schedule_cached = f1.get_schedule(use_cache=True)
    print(f"   缓存加载: {len(schedule_cached)} 场比赛")
    
    # 测试 3: 获取下一场比赛
    print("\n3. 测试获取下一场比赛...")
    next_race = f1.get_next_race()
    if next_race:
        print(f"   比赛: {next_race['raceName']}")
        print(f"   日期: {next_race['date']}")
    else:
        print("   本赛季已无 upcoming 比赛")
    
    # 测试 4: 获取即将进行的环节
    print("\n4. 测试获取即将进行的环节...")
    upcoming = f1.get_upcoming_sessions(168)
    print(f"   未来一周有 {len(upcoming)} 个环节")
    for session in upcoming[:3]:
        print(f"   - {session['name']}: {session['datetime']}")
    
    print("\n" + "=" * 60)
    print("测试完成！")
    print("=" * 60)
