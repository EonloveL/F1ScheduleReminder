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
    """本地JSON缓存管理器（原子写+锁，防并发/崩溃产生半截文件）"""

    def __init__(self, cache_dir: str = None):
        import threading
        self.cache_dir = cache_dir or CACHE_DIR
        self._lock = threading.Lock()
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
            with self._lock:
                tmp_file = cache_file + ".tmp"
                with open(tmp_file, 'w', encoding='utf-8') as f:
                    json.dump(cache_data, f, ensure_ascii=False, indent=2)
                os.replace(tmp_file, cache_file)
            logger.debug(f"缓存已保存: {key}")
        except Exception as e:
            logger.error(f"保存缓存失败 {key}: {e}")

    def load(self, key: str, allow_expired: bool = False) -> Optional[Any]:
        """
        从本地加载数据，自动检查过期

        Args:
            key: 缓存键
            allow_expired: True 时过期数据也返回（API 全挂时的兜底）

        Returns:
            缓存数据，过期或不存在返回None
        """
        cache_file = os.path.join(self.cache_dir, f"{key}.json")
        if not os.path.exists(cache_file):
            return None

        try:
            with self._lock:
                with open(cache_file, 'r', encoding='utf-8') as f:
                    cache_data = json.load(f)

            # 检查是否过期
            expires_at = cache_data.get("expires_at")
            if expires_at and not allow_expired:
                expiry = datetime.fromisoformat(expires_at)
                if datetime.now() > expiry:
                    logger.debug(f"缓存已过期: {key}")
                    return None

            logger.debug(f"缓存已加载: {key}{'（过期兜底）' if allow_expired else ''}")
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
        try:
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
        except FileNotFoundError:
            # 并行工具同时 force_refresh 同一缓存键时的 TOCTOU 窗口
            #（exists 通过后另一线程已删除），无害忽略
            pass
        except Exception as e:
            logger.warning(f"清除缓存失败 {key}: {e}")


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
        
        # 冲刺排位赛
        sprint_qualy = schedule.get("sprintQualy")
        if sprint_qualy and sprint_qualy.get("date"):
            converted["SprintQualifying"] = {"date": sprint_qualy["date"], "time": sprint_qualy.get("time", "")}
        
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
        """获取积分榜（转换为Ergast兼容格式，与Jolpica输出统一）"""
        driver_data = self._make_request(f"{self.BASE_URL}/{season}/drivers-championship")
        constructor_data = self._make_request(f"{self.BASE_URL}/{season}/constructors-championship")
        
        if not driver_data or not constructor_data:
            logger.warning(f"[{self.name}] {season} 赛季积分榜数据不可用")
            return None
        
        driver_standings = []
        for e in driver_data.get("drivers_championship", []):
            d = e.get("driver") or {}
            t = e.get("team") or {}
            driver_standings.append({
                "position": str(e.get("position", "")),
                "points": str(e.get("points", "0")),
                "wins": str(e.get("wins", "0")),
                "Driver": {
                    "driverId": e.get("driverId") or d.get("driverId", ""),
                    "code": d.get("shortName", ""),
                    "givenName": d.get("name", ""),
                    "familyName": d.get("surname", ""),
                },
                "Constructors": [{
                    "constructorId": e.get("teamId") or t.get("teamId", ""),
                    "name": t.get("teamName", ""),
                }],
            })
        
        constructor_standings = []
        for e in constructor_data.get("constructors_championship", []):
            t = e.get("team") or {}
            constructor_standings.append({
                "position": str(e.get("position", "")),
                "points": str(e.get("points", "0")),
                "wins": str(e.get("wins", "0")),
                "Constructor": {
                    "constructorId": e.get("teamId") or t.get("teamId", ""),
                    "name": t.get("teamName", ""),
                },
            })
        
        drivers = {"MRData": {"StandingsTable": {"StandingsLists": [{"DriverStandings": driver_standings}]}}}
        constructors = {"MRData": {"StandingsTable": {"StandingsLists": [{"ConstructorStandings": constructor_standings}]}}}
        
        return {
            "drivers": drivers,
            "constructors": constructors,
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
    
    def get_standings(self, season: int) -> Optional[Dict[str, Any]]:
        """获取积分榜（两个请求全失败时返回None，让上层故障转移/错误处理生效）"""
        driver_data = self._make_request(f"{self.BASE_URL}/{season}/driverStandings.json")
        constructor_data = self._make_request(f"{self.BASE_URL}/{season}/constructorStandings.json")

        if driver_data is None and constructor_data is None:
            logger.warning(f"[{self.name}] {season} 积分榜两个请求均失败")
            return None

        return {
            "drivers": driver_data,
            "constructors": constructor_data,
            "_source": "Jolpica"
        }


# 赛道关键词别名表：中文/英文关键词 -> Ergast circuitId 候选列表
CIRCUIT_ALIASES = {
    "蒙扎": ["monza"], "monza": ["monza"],
    "伊莫拉": ["imola"], "imola": ["imola"],
    "艾米利亚": ["imola"], "艾米利亚罗马涅": ["imola"], "艾米利亚-罗马涅": ["imola"],
    "亚斯码头": ["yas_marina"], "阿布扎比": ["yas_marina"], "yas": ["yas_marina"],
    "斯帕": ["spa"], "spa": ["spa"], "比利时": ["spa"],
    "雪邦": ["sepang"], "sepang": ["sepang"], "马来西亚": ["sepang"],
    "巴林": ["bahrain"], "bahrain": ["bahrain"],
    "铃鹿": ["suzuka"], "suzuka": ["suzuka"], "日本": ["suzuka"],
    "上海": ["shanghai"], "中国": ["shanghai"], "shanghai": ["shanghai"],
    "新加坡": ["marina_bay"], "marina": ["marina_bay"],
    "奥斯汀": ["americas"], "美国": ["americas"], "austin": ["americas"],
    "墨西哥": ["rodriguez"], "mexico": ["rodriguez"],
    "英特拉格斯": ["interlagos"], "圣保罗": ["interlagos"], "巴西": ["interlagos"], "interlagos": ["interlagos"],
    "拉斯维加斯": ["vegas"], "vegas": ["vegas"],
    "卢塞尔": ["lusail", "losail"], "卡塔尔": ["lusail", "losail"], "lusail": ["lusail", "losail"], "losail": ["lusail", "losail"],
    "赞德沃特": ["zandvoort"], "荷兰": ["zandvoort"], "zandvoort": ["zandvoort"],
    "摩纳哥": ["monaco"], "monaco": ["monaco"],
    "银石": ["silverstone"], "英国": ["silverstone"], "silverstone": ["silverstone"],
    "亨格罗宁": ["hungaroring"], "匈牙利": ["hungaroring"], "hungaroring": ["hungaroring"],
    "巴库": ["baku"], "阿塞拜疆": ["baku"], "baku": ["baku"],
    "迈阿密": ["miami"], "miami": ["miami"],
    "蒙特利尔": ["villeneuve"], "加拿大": ["villeneuve"], "维伦纽夫": ["villeneuve"],
    "红牛环": ["red_bull_ring"], "奥地利": ["red_bull_ring"], "斯皮尔伯格": ["red_bull_ring"],
    "马德里": ["madring"], "madring": ["madring"],
    "西班牙": ["madring", "catalunya"], "加泰罗尼亚": ["catalunya"], "catalunya": ["catalunya"],
    "巴塞罗那": ["catalunya"], "barcelona": ["catalunya"],
    "吉达": ["jeddah"], "沙特": ["jeddah"], "jeddah": ["jeddah"],
    "阿尔伯特": ["albert_park"], "墨尔本": ["albert_park"], "澳大利亚": ["albert_park"], "albert": ["albert_park"],
    "意大利": ["monza", "imola"],
}


def find_race_by_circuit(schedule: List[Dict[str, Any]], keyword: str) -> Optional[Dict[str, Any]]:
    """
    按地点关键词在赛程中查找分站

    Args:
        schedule: 赛季赛程列表
        keyword: 赛道/城市/国家关键词（中英文均可）

    Returns:
        匹配的分站数据或 None
    """
    q = keyword.strip().lower().replace(" ", "")
    if not q:
        return None

    candidates = CIRCUIT_ALIASES.get(keyword.strip()) or CIRCUIT_ALIASES.get(q) or []
    if isinstance(candidates, str):
        candidates = [candidates]

    # 1. 别名精确匹配 circuitId（多命中时取日期最接近今天者：
    # 2026 起西班牙有两站——巴塞罗那 R7(catalunya) 与马德里西班牙大奖赛 R14(madring)，
    # 赛程顺序优先会导致"西班牙"永远命中较早一站）
    if candidates:
        hits = [race for race in schedule
                if race.get("Circuit", {}).get("circuitId", "") in candidates]
        if len(hits) == 1:
            return hits[0]
        if hits:
            from datetime import date as _date
            today = _date.today()

            def _dist(r):
                try:
                    return abs((datetime.strptime(r["date"], "%Y-%m-%d").date() - today).days)
                except (KeyError, ValueError, TypeError):
                    return 9999

            hits.sort(key=_dist)
            return hits[0]

    # 2. 子串匹配 circuitId/circuitName/locality/country/raceName
    for race in schedule:
        c = race.get("Circuit", {})
        loc = c.get("Location", {})
        fields = [
            c.get("circuitId", ""), c.get("circuitName", ""),
            loc.get("locality", ""), loc.get("country", ""),
            race.get("raceName", ""),
        ]
        for f in fields:
            fn = str(f).lower().replace(" ", "")
            if q in fn:
                return race
    return None


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

        # force_refresh single-flight：L1 并行工具（车手/车队积分榜）会同时
        # force_refresh 同一缓存键，串行化防并发拉取+缓存文件 TOCTOU；
        # 60s 内的重复 force_refresh 直接吃上一个调用刚写入的新鲜缓存
        import threading as _th
        self._standings_sf_lock = _th.Lock()
        self._standings_fresh_ts = 0.0

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
        
        # 3. 全部失败，尝试加载过期缓存兜底（赛程/积分榜在API双源全挂时仍可用旧数据）
        logger.error(f"[{operation}] 所有API均失败")
        stale = self.cache.load(cache_key, allow_expired=True)
        if stale is not None:
            logger.warning(f"[{operation}] 使用过期缓存兜底: {cache_key}")
            return stale, "stale-cache"

        return None, "failed"
    
    def get_schedule(self, use_cache: bool = True) -> List[Dict[str, Any]]:
        """获取当前赛季完整赛程"""
        return self.get_schedule_for_year(self.season, use_cache=use_cache)

    # 各环节键（Ergast格式）及中文名，用于双源赛程比对
    _SCHEDULE_SESSION_KEYS = [
        ("FirstPractice", "FP1"), ("SecondPractice", "FP2"), ("ThirdPractice", "FP3"),
        ("SprintQualifying", "冲刺排位"), ("Sprint", "冲刺赛"), ("Qualifying", "排位赛"),
    ]

    @staticmethod
    def _session_dt(date_str: str, time_str: str):
        """解析环节日期时间为UTC datetime，失败返回None"""
        if not date_str:
            return None
        try:
            t = (time_str or "00:00:00Z").replace("Z", "")
            return datetime.fromisoformat(f"{date_str}T{t}").replace(tzinfo=timezone.utc)
        except Exception:
            return None

    def _cross_validate_schedule(self, primary: List[Dict[str, Any]], secondary: List[Dict[str, Any]]):
        """
        双源赛程交叉校验：逐站逐环节比对时间，偏差>15分钟告警（以主源Jolpica为准）。
        背景：2026荷兰站实证f1api.dev冲刺周末时间偏早30-60分钟，Jolpica与官方一致。
        """
        sec_by_round = {str(r.get("round")): r for r in secondary}
        mismatches = 0
        for race in primary:
            other = sec_by_round.get(str(race.get("round")))
            if not other:
                continue
            checks = [(race.get("date"), race.get("time"), other.get("date"), other.get("time"), "正赛")]
            for key, name in self._SCHEDULE_SESSION_KEYS:
                a = race.get(key) or {}
                b = other.get(key) or {}
                if not a.get("date") or not b.get("date"):
                    continue
                checks.append((a.get("date"), a.get("time"), b.get("date"), b.get("time"), name))
            for d1, t1, d2, t2, name in checks:
                dt1 = self._session_dt(d1, t1)
                dt2 = self._session_dt(d2, t2)
                if not dt1 or not dt2:
                    continue
                diff_min = abs((dt1 - dt2).total_seconds()) / 60
                if diff_min > 15:
                    mismatches += 1
                    logger.warning(
                        f"[赛程校验] {race.get('raceName')} {name} 双源时间不一致: "
                        f"Jolpica={d1} {t1} vs f1api.dev={d2} {t2} (差{int(diff_min)}分钟)，以Jolpica为准"
                    )
        if mismatches:
            logger.warning(f"[赛程校验] 共 {mismatches} 处双源时间不一致（已采用Jolpica）")
        else:
            logger.info("[赛程校验] 双源赛程时间一致 ✓")

    def get_schedule_for_year(self, year: int, use_cache: bool = True) -> List[Dict[str, Any]]:
        """
        获取指定赛季完整赛程（Jolpica主源 + f1api.dev双源交叉校验）

        - Jolpica为唯一采用源（2026荷兰站实证与官方一致）
        - 拉取成功后与f1api.dev逐环节比对，偏差>15分钟记warning
        - Jolpica失败才降级f1api.dev，且缓存缩短至6小时加速自愈

        Args:
            year: 赛季年份
            use_cache: 是否使用缓存

        Returns:
            比赛列表
        """
        # v2缓存键：旧版本可能缓存了f1api.dev的错误时间（2026蒙扎/赞德沃特实证±1h），升级即失效
        cache_key = f"schedule_v2_{year}"
        jolpica, f1dev = self.providers[1], self.providers[0]
        cache_ttl = 24 if year >= self.season else None  # 历史赛季永久缓存

        if use_cache:
            cached = self.cache.load(cache_key)
            if cached:
                logger.info(f"[get_schedule] 从缓存加载数据 ({year})")
                return cached

        primary = None
        try:
            primary = jolpica.get_schedule(year)
        except Exception as e:
            logger.warning(f"[get_schedule] [Jolpica] 获取 {year} 失败: {e}")

        if primary:
            if year >= self.season:
                try:
                    secondary = f1dev.get_schedule(year)
                    if secondary:
                        self._cross_validate_schedule(primary, secondary)
                except Exception as e:
                    logger.warning(f"[get_schedule] 双源校验跳过: {e}")
            self.cache.save(cache_key, primary, ttl_hours=cache_ttl)
            logger.info(f"get_schedule({year}) 完成，来源: Jolpica")
            return primary

        # 降级：f1api.dev（冲刺周末时间可能偏早30-60分钟，缩短缓存加速自愈回Jolpica）
        logger.warning("[get_schedule] Jolpica不可用，降级f1api.dev（当前赛季缓存缩短至6h）")
        fallback = None
        try:
            fallback = f1dev.get_schedule(year)
        except Exception as e:
            logger.warning(f"[get_schedule] [f1api.dev] 获取 {year} 失败: {e}")

        if not fallback:
            logger.error(f"获取赛程数据失败: {year}")
            return []

        fallback_ttl = 6 if year >= self.season else None
        self.cache.save(cache_key, fallback, ttl_hours=fallback_ttl)
        logger.info(f"get_schedule({year}) 完成，来源: f1api.dev (降级)")
        return fallback
    
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
    
    # session_type -> (f1api.dev端点, 结果字段名)
    SESSION_RESULT_CONFIG = {
        'fp1': ('fp1', 'fp1Results'),
        'fp2': ('fp2', 'fp2Results'),
        'fp3': ('fp3', 'fp3Results'),
        'qualifying': ('qualy', 'qualyResults'),
        'race': ('race', 'results'),
        'sprint_qualifying': ('sprint/qualy', 'sprintQualyResults'),
        'sprint': ('sprint/race', 'sprintRaceResults'),
    }

    SESSION_NAMES_CN = {
        'fp1': '第一节练习赛', 'fp2': '第二节练习赛', 'fp3': '第三节练习赛',
        'qualifying': '排位赛', 'sprint_qualifying': '冲刺排位赛',
        'sprint': '冲刺赛', 'race': '正赛',
    }

    def get_session_results(self, round_num: int, session_type: str, retries: int = 2, season: int = None) -> Optional[Dict[str, Any]]:
        """
        获取任意环节的比赛结果（f1api.dev全环节 + Jolpica兜底qualy/sprint/race）

        Args:
            round_num: 轮次
            session_type: fp1/fp2/fp3/qualifying/sprint_qualifying/sprint/race
            retries: f1api.dev连接失败重试次数
            season: 赛季年份，默认当前赛季

        Returns:
            {"race_name", "round", "session_type", "session_name", "entries": [...]} 或 None
            entries元素: {"position", "driver_name", "driver_code", "team_name", "time", "points"}
        """
        if session_type not in self.SESSION_RESULT_CONFIG:
            logger.error(f"未知的环节类型: {session_type}")
            return None

        season = season or self.season
        cache_key = f"session_result_{season}_{round_num}_{session_type}"
        cached = self.cache.load(cache_key)
        if cached:
            logger.info(f"[get_session_results] 从缓存加载: {cache_key}")
            return cached

        endpoint, result_key = self.SESSION_RESULT_CONFIG[session_type]

        # 1. f1api.dev（支持全部环节）
        url = f"{F1APIDevProvider.BASE_URL}/{season}/{round_num}/{endpoint}"
        for attempt in range(retries + 1):
            try:
                resp = self.providers[0].session.get(url, timeout=25)
                if resp.status_code == 404:
                    logger.info(f"[get_session_results] [{session_type}] Round {round_num} 无数据(404)")
                    break
                resp.raise_for_status()
                data = resp.json()
                race_data = data.get("races") or {}
                raw_entries = race_data.get(result_key) or []
                if raw_entries:
                    result = self._normalize_f1api_session_results(race_data, raw_entries, session_type)
                    result["_source"] = "f1api.dev"
                    # 历史赛季成绩不再变化，永久缓存
                    self.cache.save(cache_key, result,
                                    ttl_hours=72 if season >= self.season else None)
                    logger.info(f"[get_session_results] [f1api.dev] {session_type} Round {round_num} 成功")
                    return result
                break
            except Exception as e:
                logger.warning(f"[get_session_results] [f1api.dev] 第{attempt + 1}次尝试失败: {e}")
                if attempt < retries:
                    import time as _time
                    _time.sleep(2)

        # 2. Jolpica兜底（仅支持 qualifying/sprint/race）
        jolpica_map = {'qualifying': 'qualifying', 'sprint': 'sprint', 'race': 'results'}
        if session_type in jolpica_map:
            try:
                jolpica_url = f"{JolpicaProvider.BASE_URL}/{season}/{round_num}/{jolpica_map[session_type]}.json"
                resp = self.providers[1].session.get(jolpica_url, timeout=15)
                resp.raise_for_status()
                races = resp.json()["MRData"]["RaceTable"]["Races"]
                if races:
                    result = self._normalize_jolpica_session_results(races[0], session_type)
                    if result and result.get("entries"):
                        result["_source"] = "Jolpica"
                        self.cache.save(cache_key, result,
                                        ttl_hours=72 if season >= self.season else None)
                        logger.info(f"[get_session_results] [Jolpica] {session_type} Round {round_num} 成功")
                        return result
            except Exception as e:
                logger.warning(f"[get_session_results] [Jolpica] 失败: {e}")

        logger.warning(f"[get_session_results] {session_type} Round {round_num} 无可用数据")
        return None

    def _normalize_f1api_session_results(self, race_data: Dict, entries: List[Dict], session_type: str) -> Dict[str, Any]:
        """统一f1api.dev各环节结果为通用格式"""
        normalized = []
        for idx, e in enumerate(entries, 1):
            driver = e.get("driver") or {}
            team = e.get("team") or {}
            pos = e.get("position") or e.get("gridPosition") or idx
            try:
                pos = int(pos)
            except (TypeError, ValueError):
                pos = idx
            if session_type in ('qualifying',):
                time_str = e.get("q3") or e.get("q2") or e.get("q1") or ""
            elif session_type in ('sprint_qualifying',):
                time_str = e.get("sq3") or e.get("sq2") or e.get("sq1") or ""
            else:
                time_str = e.get("time") or (e.get("fastLap") and f"FL {e['fastLap']}") or ""
            normalized.append({
                "position": int(pos),
                "driver_name": f"{driver.get('name', '')} {driver.get('surname', '')}".strip(),
                "driver_code": driver.get("shortName", ""),
                "driver_id": e.get("driverId") or driver.get("driverId", ""),
                "team_name": team.get("teamName", ""),
                "team_id": e.get("teamId") or team.get("teamId", ""),
                "time": time_str or "",
                "points": e.get("points"),
                "grid": e.get("grid"),  # 发车位置（正赛位置变化/罚退分析用）
                "fast_lap": e.get("fastLap") or "",  # 正赛个人最快圈（如 "1:32.123"），跨赛道pace分析用
            })
        normalized.sort(key=lambda x: x["position"])
        return {
            "race_name": race_data.get("raceName", ""),
            "round": str(race_data.get("round", "")),
            "session_type": session_type,
            "session_name": self.SESSION_NAMES_CN.get(session_type, session_type),
            "entries": normalized,
        }

    def _normalize_jolpica_session_results(self, race: Dict, session_type: str) -> Optional[Dict[str, Any]]:
        """统一Jolpica(Ergast)结果为通用格式"""
        key_map = {'qualifying': 'QualifyingResults', 'sprint': 'SprintResults', 'race': 'Results'}
        raw = race.get(key_map[session_type]) or []
        normalized = []
        for idx, e in enumerate(raw, 1):
            driver = e.get("Driver") or {}
            constructor = e.get("Constructor") or {}
            try:
                pos = int(e.get("position", idx))
            except (TypeError, ValueError):
                pos = idx
            if session_type == 'qualifying':
                time_str = e.get("Q3") or e.get("Q2") or e.get("Q1") or ""
            else:
                time_str = (e.get("Time") or {}).get("time") or e.get("status", "")
            normalized.append({
                "position": pos,
                "driver_name": f"{driver.get('givenName', '')} {driver.get('familyName', '')}".strip(),
                "driver_code": driver.get("code", ""),
                "driver_id": driver.get("driverId", ""),
                "team_name": constructor.get("name", ""),
                "team_id": constructor.get("constructorId", ""),
                "time": time_str,
                "points": e.get("points"),
                "grid": e.get("grid"),
                "fast_lap": ((e.get("FastestLap") or {}).get("Time") or {}).get("time", ""),
            })
        normalized.sort(key=lambda x: x["position"])
        return {
            "race_name": race.get("raceName", ""),
            "round": str(race.get("round", "")),
            "session_type": session_type,
            "session_name": self.SESSION_NAMES_CN.get(session_type, session_type),
            "entries": normalized,
        }

    def get_qualifying_pace(self, season: int, round_num: int) -> List[Dict[str, Any]]:
        """
        排位赛各车手最佳圈速（q1/q2/q3 取最快）+ 全场中位数归一化 pace_index。
        pace_index = 全场中位数 / 车手最佳圈 × 100（=100 中位水平，>100 快于中位；
        跨赛道/跨年份可比，预测模型特征用）。历史赛季永久缓存由 get_session_results 承担。
        """
        res = self.get_session_results(round_num, "qualifying", season=season)
        if not res or not res.get("entries"):
            return []
        from .circuits_manager import CircuitsManager
        best = []
        for e in res["entries"]:
            sec = CircuitsManager.time_to_seconds(e.get("time", ""))
            if sec:
                best.append((e, sec))
        if not best:
            return []
        times = sorted(s for _, s in best)
        n = len(times)
        median = times[n // 2] if n % 2 else (times[n // 2 - 1] + times[n // 2]) / 2
        out = []
        for e, sec in best:
            out.append({
                "driver": e.get("driver_name", ""),
                "driver_id": e.get("driver_id", ""),
                "team": e.get("team_name", ""),
                "team_id": e.get("team_id", ""),
                "position": e.get("position"),
                "best_time": sec,
                "pace_index": round(median / sec * 100, 2),
            })
        out.sort(key=lambda x: -x["pace_index"])
        return out

    def get_race_fastest_laps(self, season: int, round_num: int) -> List[Dict[str, Any]]:
        """
        正赛各车手最快圈（秒）-> [{"driver", "team", "fastest_lap": 秒数}]
        数据源固定 Jolpica（f1api.dev 的 fastLap 字段恒为 null），跨赛道 pace 分析用。
        历史数据永久缓存，当前赛季 72h。
        """
        cache_key = f"fastlaps_{season}_{round_num}"
        cached = self.cache.load(cache_key)
        if cached is not None:
            return cached
        out: List[Dict[str, Any]] = []
        try:
            from .circuits_manager import CircuitsManager
            jolpica = self.providers[1]
            race = jolpica.get_race_results(season, round_num)
            if race is None:
                # API 故障返回 None：不缓存空结果（历史赛季 TTL=None 会永久固化空数据）
                logger.warning(f"[get_race_fastest_laps] {season} R{round_num} 数据源失败，不缓存")
                return []
            for e in race.get("Results", []):
                sec = CircuitsManager.time_to_seconds(
                    ((e.get("FastestLap") or {}).get("Time") or {}).get("time", ""))
                if sec:
                    d = e.get("Driver") or {}
                    out.append({
                        "driver": f"{d.get('givenName', '')} {d.get('familyName', '')}".strip(),
                        "team": (e.get("Constructor") or {}).get("name", ""),
                        "fastest_lap": sec,
                    })
        except Exception as e:
            logger.warning(f"[get_race_fastest_laps] {season} R{round_num} 失败: {e}")
            return []
        self.cache.save(cache_key, out, ttl_hours=72 if season >= self.season else None)
        return out

    def get_last_completed_session(self, min_duration_hours: float = 1.5) -> Optional[Dict[str, Any]]:
        """
        获取最近一个已结束的环节（任意类型）

        Args:
            min_duration_hours: 环节开始多久后视为已结束

        Returns:
            环节信息（type/name/round/race_name/datetime）或 None
        """
        schedule = self.get_schedule()
        if not schedule:
            return None

        now = datetime.now(timezone.utc)
        completed = []
        for race in schedule:
            for session in self.get_all_sessions(race):
                if session['datetime'] + timedelta(hours=min_duration_hours) < now:
                    completed.append(session)

        return completed[-1] if completed else None

    def _fill_standings_gaps(self, data: Dict[str, Any], season: int) -> Dict[str, Any]:
        """车手积分榜空位补全：主源 f1api.dev 在季中换人后偶发漏掉新车手
        （实证：2026 蒙扎后 Tsunoda 升小红牛，主源缺 P20），
        检测名次不连续时用 Jolpica 补全缺失条目并回写缓存"""
        try:
            drivers = data.get("drivers") or {}
            lists = drivers.get("MRData", {}).get("StandingsTable", {}).get("StandingsLists") or []
            entries = lists[0].get("DriverStandings", []) if lists else []
            if not entries:
                return data
            positions = sorted(int(e["position"]) for e in entries
                               if str(e.get("position", "")).isdigit())
            if not positions:
                return data
            missing_pos = sorted(set(range(1, max(positions) + 1)) - set(positions))
            if not missing_pos:
                return data

            jolpica = next((p for p in self.providers if p.name == "Jolpica"), None)
            if not jolpica:
                return data
            backup = jolpica.get_standings(season)
            b_lists = ((backup or {}).get("drivers") or {}).get(
                "MRData", {}).get("StandingsTable", {}).get("StandingsLists") or []
            b_entries = b_lists[0].get("DriverStandings", []) if b_lists else []
            # 按名次补全（不用 driverId：双源命名不一致，如 lindblad vs arvid_lindblad）
            added = [e for e in b_entries
                     if str(e.get("position", "")).isdigit()
                     and int(e["position"]) in missing_pos]
            if added:
                entries.extend(added)
                entries.sort(key=lambda e: int(str(e.get("position", "999"))))
                data["_source"] = str(data.get("_source", "")) + "+Jolpica补全"
                logger.warning(f"车手积分榜缺 P{missing_pos}，已从Jolpica补全: "
                               + ", ".join(e.get("Driver", {}).get("familyName", "?") for e in added))
                # 修复后回写缓存，避免每次请求都重复补全
                ttl = 12 if season >= self.season else None
                self.cache.save(f"standings_{season}", data, ttl_hours=ttl)
        except Exception as e:
            logger.warning(f"积分榜空位检测/补全失败: {e}")
        return data

    @staticmethod
    def _standings_points_sum(standings: Dict[str, Any]) -> float:
        """车手积分总和（赛季内单调不减，用作双源新鲜度比较基准）"""
        try:
            lists = ((standings.get("drivers") or {}).get("MRData", {})
                     .get("StandingsTable", {}).get("StandingsLists") or [])
            if not lists:
                return 0.0
            return sum(float(e.get("points", 0) or 0)
                       for e in lists[0].get("DriverStandings", []))
        except (ValueError, TypeError, AttributeError):
            return 0.0

    def get_standings_freshest(self, season: int = None) -> Tuple[Optional[Dict[str, Any]], str, float]:
        """双源积分榜取最新者：绕开 _execute_with_fallback 的"首个非None即采纳"，
        两个 Provider 都查，车手积分总和更大者更新鲜（总和赛季内单调不减）。

        背景（2026-09-28 巴库实证）：f1api.dev 积分榜官方口径赛后 24-48h 滞后，
        周六完赛时首源返回上周旧数据且非 None → fallback 不触发 → 推了滞后积分榜，
        用户看到"周六晚没更新、周日晚才更新"。

        Returns: (standings, source_name, points_sum)；双源全挂 (None, "failed", 0.0)
        """
        season = season or self.season
        best = None
        for p in self.providers:
            try:
                data = p.get_standings(season)
            except Exception as e:
                logger.warning(f"[get_standings_freshest] [{p.name}] 失败: {e}")
                continue
            if not data:
                continue
            psum = self._standings_points_sum(data)
            logger.info(f"[get_standings_freshest] [{p.name}] 积分总和={psum}")
            if psum and (best is None or psum > best[1]):
                best = (data, psum, p.name)
        if not best:
            return None, "failed", 0.0
        data, psum, name = best
        data["_source"] = name
        return self._fill_standings_gaps(data, season), f"{name} (freshest)", psum

    def get_current_standings(self, force_refresh: bool = False) -> Dict[str, Any]:
        """
        获取当前积分榜

        Args:
            force_refresh: 跳过缓存强制拉取最新数据（赛后推送用，避免72h缓存推旧榜）

        Returns:
            车手和车队积分榜
        """
        cache_key = f"standings_{self.season}"
        if force_refresh:
            # single-flight：L1 并行工具（车手/车队积分榜）同时 force_refresh 同一缓存键时，
            # 串行化合并为一次拉取——后到者直接吃先到者刚写入的新鲜缓存，
            # 防并发重复请求 + 缓存文件 TOCTOU（2026-09-15 事故）
            import time as _t
            with self._standings_sf_lock:
                if _t.time() - self._standings_fresh_ts > 60:
                    self.cache.clear(cache_key)
                data, source = self._execute_with_fallback(
                    "get_standings", cache_key,
                    "get_standings", self.season,
                    cache_ttl=12  # 12小时缓存（比赛周末需新鲜数据，AI问答/赛后推送会force_refresh）
                )
                self._standings_fresh_ts = _t.time()
        else:
            data, source = self._execute_with_fallback(
                "get_standings", cache_key,
                "get_standings", self.season,
                cache_ttl=12
            )

        if not data:
            return {"drivers": None, "constructors": None}

        data = self._fill_standings_gaps(data, self.season)
        # 代打车手自动注册（仅当前赛季；历史赛季含大量退役车手不注册）
        try:
            from . import drivers_profile as _dp
            _lists = (data.get("drivers") or {}).get("MRData", {}).get(
                "StandingsTable", {}).get("StandingsLists") or []
            _entries = _lists[0].get("DriverStandings", []) if _lists else []
            if _entries:
                _dp.auto_register_substitutes(_entries)
        except Exception as e:
            logger.debug(f"代打车手注册检查跳过: {e}")
        logger.info(f"get_current_standings 完成，来源: {source}")
        return data if isinstance(data, dict) else {"drivers": None, "constructors": None}

    def get_standings_for_year(self, year: int) -> Dict[str, Any]:
        """
        获取指定年份积分榜（支持历史赛季，AI工具历史查询用）

        Args:
            year: 赛季年份

        Returns:
            车手和车队积分榜 {"drivers": ..., "constructors": ...}
        """
        # 历史赛季积分榜不再变化，永久缓存；当前/未来赛季12小时缓存
        cache_ttl = 12 if year >= self.season else None
        data, source = self._execute_with_fallback(
            "get_standings", f"standings_{year}",
            "get_standings", year,
            cache_ttl=cache_ttl
        )
        if not data:
            return {"drivers": None, "constructors": None}
        data = self._fill_standings_gaps(data, year)
        logger.info(f"get_standings_for_year({year}) 完成，来源: {source}")
        return data if isinstance(data, dict) else {"drivers": None, "constructors": None}
    
    @staticmethod
    def parse_session_datetime(date_str: str, time_str: str) -> datetime:
        """
        解析比赛日期和时间
        
        Args:
            date_str: 日期字符串 (YYYY-MM-DD)
            time_str: 时间字符串 (HH:MM:SSZ)
            
        Returns:
            UTC时区的datetime对象（f1api.dev返回的是UTC时间）
        """
        time_str = time_str.replace("Z", "")
        datetime_str = f"{date_str}T{time_str}"
        dt = datetime.fromisoformat(datetime_str)
        
        # f1api.dev 返回的是UTC时间（带Z后缀）
        # 直接将其标记为UTC时区，然后转换为北京时间（UTC+8）
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
        
        # 冲刺排位赛
        if "SprintQualifying" in race:
            sprint_qualy = race["SprintQualifying"]
            sessions.append({
                "type": "sprint_qualifying",
                "name": "冲刺排位赛",
                "date": sprint_qualy["date"],
                "time": sprint_qualy["time"],
                "datetime": self.parse_session_datetime(sprint_qualy["date"], sprint_qualy["time"]),
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
    
    OPENF1_SESSION_NAME_MAP = {
        'fp1': 'Practice 1', 'fp2': 'Practice 2', 'fp3': 'Practice 3',
        'qualifying': 'Qualifying', 'sprint_qualifying': 'Sprint Qualifying',
        'sprint': 'Sprint', 'race': 'Race',
    }

    def get_session_weather_text(self, session: Dict[str, Any]) -> Optional[str]:
        """
        获取指定环节的天气信息（OpenF1，格式化文本）

        Args:
            session: get_all_sessions() 风格的环节信息

        Returns:
            格式化天气文本，无数据返回 None
        """
        if not self.openf1_enabled:
            return None

        expected_name = self.OPENF1_SESSION_NAME_MAP.get(session.get('type'))
        if not expected_name:
            return None

        try:
            openf1_sessions = self.openf1.get_sessions(year=self.season)
            target_dt = session['datetime']
            best_key = None
            best_diff = None
            for s in openf1_sessions:
                if s.get('session_name') != expected_name:
                    continue
                try:
                    start = datetime.fromisoformat(s['date_start'].replace('Z', '+00:00'))
                except Exception:
                    continue
                diff = abs((start - target_dt).total_seconds())
                if diff < 6 * 3600 and (best_diff is None or diff < best_diff):
                    best_diff = diff
                    best_key = s.get('session_key')

            if not best_key:
                logger.info(f"[weather] 未找到匹配的OpenF1环节: {session.get('name')}")
                return None

            weather = self.openf1.get_weather(best_key)
            if not weather:
                logger.info(f"[weather] 环节无天气数据: {session.get('name')} (key={best_key})")
                return None

            return self.openf1.format_weather_for_chat(weather)
        except Exception as e:
            logger.warning(f"[weather] 获取天气失败: {e}")
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
