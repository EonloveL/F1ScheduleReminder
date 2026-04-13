"""
F1赛程提醒机器人 - PushPlus版本
通过PushPlus将消息推送到个人微信
推荐用于：快速部署、个人使用
"""
import time

import requests
import logging
import sys
import signal
import os
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta

# 加载环境变量
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# 本地导入（自包含版本，无需common目录）
from f1_api import F1API
from scheduler import ReminderScheduler

logger = logging.getLogger(__name__)


class PushPlusBot:
    """PushPlus推送机器人封装类"""
    
    BASE_URL = "http://www.pushplus.plus/send"
    
    def __init__(self, token: str, topic: str = None):
        """
        初始化PushPlus机器人
        
        Args:
            token: PushPlus的Token
            topic: 群组编码（一对多消息），可选
        """
        self.token = token
        self.topic = topic
        
    def send_message(self, title: str, content: str, template: str = "html") -> bool:
        """发送消息"""
        data = {
            "token": self.token,
            "title": title,
            "content": content,
            "template": template
        }
        
        if self.topic:
            data["topic"] = self.topic
            
        try:
            response = requests.post(
                self.BASE_URL,
                data=data,
                timeout=10
            )
            response.raise_for_status()
            
            result = response.json()
            if result.get("code") == 200:
                logger.info(f"消息发送成功: {title}")
                return True
            else:
                logger.error(f"消息发送失败: {result}")
                return False
                
        except Exception as e:
            logger.error(f"发送消息异常: {e}")
            return False
    
    def send_simple_message(self, content: str) -> bool:
        """发送简单消息"""
        return self.send_message("F1提醒", content, template="txt")
    
    def send_session_reminder(self, session: Dict[str, Any]) -> bool:
        """发送比赛环节提醒消息"""
        from pytz import timezone
        
        utc_time = session['datetime']
        local_tz = timezone('Asia/Shanghai')
        local_time = utc_time.astimezone(local_tz)
        
        date_str = local_time.strftime("%Y年%m月%d日")
        time_str = local_time.strftime("%H:%M")
        weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][local_time.weekday()]
        
        icons = {
            "fp1": "🏎️", "fp2": "🏎️", "fp3": "🏎️",
            "qualifying": "⏱️", "sprint": "⚡", "race": "🏁"
        }
        
        icon = icons.get(session['type'], "🏎️")
        title = f"{icon} F1提醒 - {session['name']}"
        
        content = f"""
        <h3>{icon} F1提醒 {icon}</h3>
        <hr/>
        <p><strong>📍 {session['race_name']}</strong></p>
        <p>🏟️ {session['circuit']}</p>
        <hr/>
        <h4>🔥 {session['name']} 即将开始</h4>
        <p><strong>⏰ {date_str} {weekday} {time_str} (北京时间)</strong></p>
        <p>敬请期待精彩比赛！🏎️💨</p>
        <hr/>
        <p><small>Formula 1 | {datetime.now().strftime('%Y-%m-%d %H:%M')}</small></p>
        """
        
        return self.send_message(title, content, template="html")
    
    def send_race_result(self, race_data: Dict[str, Any], results: Dict[str, Any]) -> bool:
        """发送比赛结果"""
        race_name = race_data.get('raceName', 'F1大奖赛')
        title = f"🏁 {race_name} 比赛结果"
        
        content = f"<h3>🏁 {race_name} 比赛结果</h3><hr/>"
        
        if "Results" in results:
            content += "<h4>🥇 前十名:</h4><ol>"
            for i, result in enumerate(results["Results"][:10], 1):
                driver = result.get("Driver", {})
                family_name = driver.get("familyName", "Unknown")
                team = result.get("Constructor", {}).get("name", "Unknown")
                
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
                content += f"<li><strong>{medal} {family_name}</strong> ({team})</li>"
            
            content += "</ol>"
            
            fastest_lap = results["Results"][0].get("FastestLap", {})
            if fastest_lap and fastest_lap.get("rank") == "1":
                fastest_driver = results["Results"][0].get("Driver", {}).get("familyName", "")
                lap_time = fastest_lap.get("Time", {}).get("time", "")
                content += f"<p><strong>⚡ 最快圈速:</strong> {fastest_driver} - {lap_time}</p>"
        
        content += "<hr/><p><small>#F1 #Formula1</small></p>"
        
        return self.send_message(title, content, template="html")
    
    def send_daily_schedule(self, sessions: List[Dict[str, Any]]) -> bool:
        """发送每日赛程汇总"""
        if not sessions:
            return False
        
        from pytz import timezone
        local_tz = timezone('Asia/Shanghai')
        
        race_name = sessions[0]['race_name']
        circuit = sessions[0]['circuit']
        
        title = f"📅 今日F1赛程 - {race_name}"
        
        content = f"""
        <h3>📅 今日F1赛程</h3>
        <hr/>
        <p><strong>{race_name}</strong></p>
        <p>📍 {circuit}</p>
        <hr/>
        """
        
        icons = {
            "fp1": "🏎️", "fp2": "🏎️", "fp3": "🏎️",
            "qualifying": "⏱️", "sprint": "⚡", "race": "🏁"
        }
        
        for session in sessions:
            utc_time = session['datetime']
            local_time = utc_time.astimezone(local_tz)
            time_str = local_time.strftime("%H:%M")
            icon = icons.get(session['type'], "🏎️")
            content += f"<p>{icon} <strong>{session['name']}</strong>: {time_str}</p>"
        
        content += "<hr/><p>记得准时观看！🏎️💨</p>"
        
        return self.send_message(title, content, template="html")
    
    def send_startup_message(self) -> bool:
        """发送启动通知"""
        title = "✅ F1赛程提醒机器人已启动"
        content = """
        <h3>✅ F1赛程提醒机器人已启动</h3>
        <hr/>
        <p>将为您自动推送：</p>
        <ul>
            <li>🎉 每周一比赛周预告（含赛道信息）</li>
            <li>🏎️ 练习赛提醒</li>
            <li>⏱️ 排位赛提醒</li>
            <li>🏁 正赛提醒</li>
            <li>📊 赛后结果</li>
        </ul>
        <hr/>
        <p><strong>Formula 1 🏎️💨</strong></p>
        """
        return self.send_message(title, content, template="html")
    
    def send_test_message(self) -> bool:
        """发送测试消息"""
        return self.send_message(
            "🧪 测试消息", 
            "<p>如果您收到这条消息，说明PushPlus配置成功！</p>",
            template="html"
        )
    
    def send_weekly_preview(self, race_data: Dict[str, Any], circuit_data: Dict[str, Any]) -> bool:
        """发送比赛周预告（周一推送）"""
        from pytz import timezone
        from dateutil import parser as date_parser
        
        race_name = race_data.get('raceName', 'F1大奖赛')
        
        # 获取所有比赛环节
        sessions = []
        session_types = [
            ('FirstPractice', 'fp1', '一练'),
            ('SecondPractice', 'fp2', '二练'),
            ('ThirdPractice', 'fp3', '三练'),
            ('Qualifying', 'qualifying', '排位赛'),
            ('Sprint', 'sprint', '冲刺赛')
        ]
        
        for key, s_type, s_name in session_types:
            if key in race_data:
                sessions.append({
                    'type': s_type,
                    'name': s_name,
                    'date': race_data[key]['date'],
                    'time': race_data[key]['time']
                })
        
        sessions.append({
            'type': 'race',
            'name': '正赛',
            'date': race_data['date'],
            'time': race_data['time']
        })
        
        sessions.sort(key=lambda x: f"{x['date']} {x['time']}")
        
        # 格式化比赛时间
        local_tz = timezone('Asia/Shanghai')
        schedule_lines = []
        current_date = None
        
        for session in sessions:
            dt_str = f"{session['date']}T{session['time'].replace('Z', '')}"
            dt = date_parser.isoparse(dt_str)
            local_dt = dt.astimezone(local_tz)
            
            date_str = local_dt.strftime("%m月%d日")
            time_str = local_dt.strftime("%H:%M")
            weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][local_dt.weekday()]
            
            if date_str != current_date:
                schedule_lines.append(f"<p><strong>{date_str} ({weekday})</strong></p>")
                current_date = date_str
            
            schedule_lines.append(f"<p>&nbsp;&nbsp;{session['name']}：{time_str}</p>")
        
        schedule_text = '\n'.join(schedule_lines)
        
        flag = circuit_data.get('flag', '🏁')
        circuit_name = circuit_data.get('name', race_data['Circuit']['circuitName'])
        
        title = f"🎉 欢迎来到比赛周！{flag} {race_name}"
        
        content = f"""
        <h2>🎉 欢迎来到比赛周！</h2>
        <hr/>
        <h3>{flag} {race_name}</h3>
        <p>📍 {circuit_name}</p>
        <hr/>
        <h4>📊 赛道信息</h4>
        <ul>
            <li>首次办赛：{circuit_data.get('first_race', '未知')}年</li>
            <li>单圈长度：{circuit_data.get('lap_length_km', '未知')}km</li>
            <li>正赛圈数：{circuit_data.get('laps', '未知')}圈</li>
            <li>正赛距离：{circuit_data.get('race_distance_km', '未知')}km</li>
            <li>圈速记录：{circuit_data.get('lap_record', {}).get('time', '未知')} 
                ({circuit_data.get('lap_record', {}).get('driver', '未知')}，{circuit_data.get('lap_record', {}).get('year', '未知')})</li>
        </ul>
        <hr/>
        <h4>⏰ 比赛时间（北京时间）</h4>
        {schedule_text}
        <hr/>
        <p><strong>🏎️💨 准备好享受速度与激情了吗？</strong></p>
        """
        
        return self.send_message(title, content, template="html")


# ==================== 配置 ====================
# PushPlus配置（从环境变量读取，优先级高于 .env 文件）
# 获取Token：访问 http://www.pushplus.plus/ -> 微信扫码登录 -> 复制Token
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "YOUR_TOKEN_HERE")

# 可选：群组编码（一对多推送），默认None表示一对一推送
PUSHPLUS_TOPIC = os.getenv("PUSHPLUS_TOPIC") or None

# ==================== 提醒时间配置（分钟）====================
REMINDER_TIMES = {
    "fp1": 30,           # 第一节练习赛
    "fp2": 30,           # 第二节练习赛
    "fp3": 30,           # 第三节练习赛
    "qualifying": 30,    # 排位赛
    "sprint": 30,        # 冲刺赛
    "race": 60,          # 正赛（提前1小时）
}

# ==================== 赛后结果推送 ====================
POST_RACE_DELAY = 30  # 比赛结束后多久推送结果（分钟）

# ==================== 时区和赛季配置 ====================
TIMEZONE = "Asia/Shanghai"
SEASON = datetime.now().year  # 自动获取当前年份，无需手动修改

# ==================== 日志配置 ====================
LOG_LEVEL = "INFO"
LOG_FILE = "f1_reminder.log"

# ==================== 其他配置 ====================
SEND_STARTUP_MESSAGE = True


# ==================== 日志配置函数 ====================
def setup_logging():
    """配置日志"""
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(log_format))
    
    file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format))
    
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL),
        format=log_format,
        handlers=[console_handler, file_handler]
    )


# ==================== 主程序 ====================
class CircuitsAPI:
    """赛道数据API封装（f1api.dev）"""
    
    BASE_URL = "https://f1api.dev/api"
    
    def __init__(self):
        self.session = requests.Session()
    
    def _make_request(self, endpoint: str) -> Optional[Dict]:
        """发送HTTP请求"""
        url = f"{self.BASE_URL}/{endpoint}"
        try:
            response = self.session.get(url, timeout=10)
            response.raise_for_status()
            return response.json()
        except requests.RequestException as e:
            logger.error(f"API请求失败: {url}, 错误: {e}")
            return None
    
    def get_circuit_info(self, circuit_id: str) -> Optional[Dict]:
        """获取赛道详细信息"""
        data = self._make_request(f"circuits/{circuit_id}")
        if data:
            return self._format_circuit_data(data)
        return None
    
    def search_circuit(self, query: str) -> Optional[Dict]:
        """搜索赛道"""
        data = self._make_request(f"circuits/search?q={query}")
        if data and 'circuits' in data and len(data['circuits']) > 0:
            return self._format_circuit_data(data['circuits'][0])
        return None
    
    def _format_circuit_data(self, api_data: Dict) -> Dict:
        """格式化赛道数据"""
        length_m = api_data.get('circuitLength', 0)
        length_km = length_m / 1000 if length_m else 0
        laps = api_data.get('numberOfLaps', 0)
        race_distance = length_km * laps if length_km and laps else 0
        
        lap_record = {
            'time': api_data.get('lapRecord', '未知'),
            'driver': self._translate_driver_name(api_data.get('fastestLapDriverId', '')),
            'driver_en': api_data.get('fastestLapDriverId', '未知'),
            'year': api_data.get('fastestLapYear', '未知')
        }
        
        return {
            'name': self._translate_circuit_name(api_data.get('circuitName', '')),
            'name_en': api_data.get('circuitName', ''),
            'country': api_data.get('country', '未知'),
            'country_code': api_data.get('countryCode', ''),
            'flag': self._get_country_flag(api_data.get('countryCode', '')),
            'first_race': api_data.get('firstParticipationYear', '未知'),
            'lap_length_km': round(length_km, 3) if length_km else '未知',
            'laps': laps if laps else '未知',
            'race_distance_km': round(race_distance, 3) if race_distance else '未知',
            'lap_record': lap_record,
            'corners': api_data.get('numberOfCorners', '未知'),
            'location': api_data.get('city', '未知'),
            'url': api_data.get('url', '')
        }
    
    def _get_country_flag(self, country_code: str) -> str:
        """根据国家代码获取国旗emoji"""
        flag_map = {
            'JP': '🇯🇵', 'GB': '🇬🇧', 'IT': '🇮🇹', 'BE': '🇧🇪',
            'MC': '🇲🇨', 'AT': '🇦🇹', 'ES': '🇪🇸', 'HU': '🇭🇺',
            'FR': '🇫🇷', 'NL': '🇳🇱', 'US': '🇺🇸', 'BR': '🇧🇷',
            'AE': '🇦🇪', 'SA': '🇸🇦', 'AU': '🇦🇺', 'CN': '🇨🇳',
            'BH': '🇧🇭', 'QA': '🇶🇦', 'CA': '🇨🇦', 'MX': '🇲🇽',
            'AZ': '🇦🇿', 'SG': '🇸🇬', 'PT': '🇵🇹', 'TR': '🇹🇷',
            'DE': '🇩🇪', 'RU': '🇷🇺'
        }
        return flag_map.get(country_code, '🏁')
    
    def _translate_circuit_name(self, name_en: str) -> str:
        """翻译赛道名称"""
        translations = {
            'Suzuka Circuit': '铃鹿赛道',
            'Silverstone Circuit': '银石赛道',
            'Autodromo Nazionale Monza': '蒙扎赛道',
            'Circuit de Spa-Francorchamps': '斯帕-弗朗科尔尚赛道',
            'Circuit de Monaco': '摩纳哥赛道',
            'Red Bull Ring': '红牛环赛道',
            'Circuit de Barcelona-Catalunya': '巴塞罗那-加泰罗尼亚赛道',
            'Hungaroring': '亨格罗林赛道',
            'Circuit Zandvoort': '赞德福特赛道',
            'Circuit of the Americas': '美洲赛道',
            'Autódromo José Carlos Pace': '英特拉格斯赛道',
            'Yas Marina Circuit': '亚斯码头赛道',
            'Jeddah Corniche Circuit': '吉达滨海赛道',
            'Albert Park Circuit': '阿尔伯特公园赛道',
            'Shanghai International Circuit': '上海国际赛车场',
            'Bahrain International Circuit': '巴林国际赛道',
            'Miami International Autodrome': '迈阿密国际赛道',
            'Las Vegas Strip Circuit': '拉斯维加斯街道赛道',
            'Losail International Circuit': '罗赛尔国际赛道',
            'Autodromo Internazionale Enzo e Dino Ferrari': '恩佐与迪诺·法拉利赛道',
            'Autódromo Internacional do Algarve': '阿尔加维国际赛道',
            'Autodromo Internazionale del Mugello': '穆杰罗赛道',
            'Intercity Istanbul Park': '伊斯坦布尔赛道',
            'Nürburgring': '纽博格林赛道',
            'Sochi Autodrom': '索契赛道',
            'Circuit Gilles Villeneuve': '吉尔·维伦纽夫赛道',
            'Autódromo Hermanos Rodríguez': '罗德里格斯兄弟赛道',
            'Baku City Circuit': '巴库城市赛道',
            'Marina Bay Street Circuit': '滨海湾街道赛道',
            'Circuit Paul Ricard': '保罗·里卡尔赛道'
        }
        return translations.get(name_en, name_en)
    
    def _translate_driver_name(self, driver_id: str) -> str:
        """翻译车手名字"""
        translations = {
            'max_verstappen': '马克斯·维斯塔潘',
            'lewis_hamilton': '刘易斯·汉密尔顿',
            'valtteri_bottas': '瓦尔特里·博塔斯',
            'charles_leclerc': '夏尔·勒克莱尔',
            'carlos_sainz': '小卡洛斯·塞恩斯',
            'sergio_perez': '塞尔吉奥·佩雷兹',
            'lando_norris': '兰多·诺里斯',
            'george_russell': '乔治·拉塞尔',
            'fernando_alonso': '费尔南多·阿隆索',
            'sebastian_vettel': '塞巴斯蒂安·维特尔',
            'kimi_raikkonen': '基米·莱科宁',
            'rubens_barrichello': '鲁本斯·巴里切罗',
            'michael_schumacher': '迈克尔·舒马赫',
            'pedro_de_la_rosa': '佩德罗·德拉罗萨',
            'kevin_magnussen': '凯文·马格努森',
            'oscar_piastri': '奥斯卡·皮亚斯特里',
            'juan_pablo_montoya': '胡安·巴布罗·蒙托亚',
            'kimi_antonelli': '基米·安东内利'
        }
        return translations.get(driver_id, driver_id.replace('_', ' ').title())


class WeeklyReminderScheduler(ReminderScheduler):
    """扩展的调度器，添加周一比赛周预告"""
    
    def __init__(self, f1_api, notify_bot, config, circuits_api):
        super().__init__(f1_api, notify_bot, config)
        self.circuits_api = circuits_api
    
    def add_weekly_preview_job(self):
        """添加每周一比赛周预告任务"""
        from apscheduler.triggers.cron import CronTrigger
        
        job = self.scheduler.add_job(
            func=self._send_weekly_preview,
            trigger=CronTrigger(day_of_week='mon', hour=9, minute=0, timezone=self.local_tz),
            id='weekly_race_preview',
            replace_existing=True
        )
        logger.info("已添加每周一比赛周预告任务（每周一9:00）")
        return job
    
    def _send_weekly_preview(self):
        """发送本周比赛预告"""
        try:
            logger.info("检查本周是否有比赛...")
            
            from datetime import timezone
            from dateutil import parser as date_parser
            
            now = datetime.now(timezone.utc)
            week_end = now + timedelta(days=7)
            
            schedule = self.f1_api.get_schedule()
            
            this_week_race = None
            for race in schedule:
                race_date = date_parser.parse(race['date'])
                race_date = race_date.replace(tzinfo=timezone.utc)
                
                if now <= race_date <= week_end:
                    this_week_race = race
                    break
            
            if this_week_race:
                logger.info(f"本周有比赛: {this_week_race['raceName']}")
                
                circuit_id = this_week_race['Circuit'].get('circuitId', '')
                circuit_name = this_week_race['Circuit']['circuitName']
                
                logger.info(f"正在从API获取赛道信息: {circuit_id}")
                circuit_data = self.circuits_api.get_circuit_info(circuit_id)
                
                if not circuit_data:
                    logger.info(f"通过ID未找到，尝试搜索: {circuit_name}")
                    circuit_data = self.circuits_api.search_circuit(circuit_name)
                
                if not circuit_data:
                    logger.warning(f"API中未找到赛道数据: {circuit_id}")
                    circuit_data = {
                        'name': circuit_name,
                        'flag': '🏁',
                        'first_race': '未知',
                        'lap_length_km': '未知',
                        'laps': '未知',
                        'race_distance_km': '未知',
                        'lap_record': {'time': '未知', 'driver': '未知', 'year': '未知'}
                    }
                
                self.notify_bot.send_weekly_preview(this_week_race, circuit_data)
                logger.info("比赛周预告发送成功")
            else:
                logger.info("本周没有比赛")
                
        except Exception as e:
            logger.error(f"发送比赛周预告失败: {e}")


class F1ReminderApp:
    """F1提醒应用主类"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.f1_api = None
        self.notify_bot = None
        self.scheduler = None
        self.circuits_api = None
        self.running = False
        
    def initialize(self) -> bool:
        """初始化所有组件"""
        self.logger.info("=" * 60)
        self.logger.info("F1赛程提醒机器人[PushPlus版]正在启动...")
        self.logger.info("=" * 60)
        
        # 检查配置
        if "YOUR_TOKEN" in PUSHPLUS_TOKEN:
            self.logger.error("错误：请先配置PushPlus Token！")
            self.logger.error("编辑 pushplus_version/main.py 文件，修改 PUSHPLUS_TOKEN")
            return False
        
        # 初始化赛道API
        try:
            self.circuits_api = CircuitsAPI()
            self.logger.info("✓ 赛道API初始化成功（f1api.dev）")
        except Exception as e:
            self.logger.error(f"✗ 赛道API初始化失败: {e}")
            return False
        
        # 初始化F1 API
        try:
            self.f1_api = F1API(SEASON)
            self.logger.info(f"✓ F1 API初始化成功 (赛季: {SEASON})")
        except Exception as e:
            self.logger.error(f"✗ F1 API初始化失败: {e}")
            return False
        
        # 初始化PushPlus机器人
        try:
            self.notify_bot = PushPlusBot(PUSHPLUS_TOKEN, PUSHPLUS_TOPIC)
            self.logger.info("✓ PushPlus机器人初始化成功")
        except Exception as e:
            self.logger.error(f"✗ PushPlus机器人初始化失败: {e}")
            return False
        
        # 初始化调度器
        try:
            config = {
                'TIMEZONE': TIMEZONE,
                'REMINDER_TIMES': REMINDER_TIMES,
                'POST_RACE_DELAY': POST_RACE_DELAY
            }
            self.scheduler = WeeklyReminderScheduler(
                self.f1_api, self.notify_bot, config, self.circuits_api
            )
            self.logger.info("✓ 调度器初始化成功")
        except Exception as e:
            self.logger.error(f"✗ 调度器初始化失败: {e}")
            return False
        
        return True
    
    def start(self):
        """启动应用"""
        if not self.initialize():
            self.logger.error("应用初始化失败，退出")
            sys.exit(1)
        
        # 发送启动消息
        if SEND_STARTUP_MESSAGE:
            try:
                self.notify_bot.send_startup_message()
            except Exception as e:
                self.logger.warning(f"发送启动消息失败: {e}")
        
        # 启动调度器
        try:
            self.scheduler.start()
            self.scheduler.add_daily_update_job()
            self.scheduler.add_weekly_preview_job()
        except Exception as e:
            self.logger.error(f"启动调度器失败: {e}")
            sys.exit(1)
        
        self.running = True
        self.logger.info("✓ 应用启动成功！")
        self.logger.info("=" * 60)
        
        # 打印即将进行的比赛
        self._print_upcoming_races()
        
        # 主循环
        self._main_loop()
    
    def _print_upcoming_races(self):
        """打印即将进行的比赛"""
        try:
            upcoming = self.f1_api.get_upcoming_sessions(hours_ahead=168)
            if upcoming:
                self.logger.info("\n📅 未来7天即将进行的F1环节：")
                from pytz import timezone
                local_tz = timezone(TIMEZONE)
                
                for session in upcoming[:5]:
                    local_time = session['datetime'].astimezone(local_tz)
                    time_str = local_time.strftime('%m月%d日 %H:%M')
                    self.logger.info(f"  • {session['race_name']} - {session['name']}: {time_str}")
            else:
                self.logger.info("📅 未来7天没有F1比赛")
        except Exception as e:
            self.logger.warning(f"获取即将进行的比赛失败: {e}")
    
    def _main_loop(self):
        """主循环"""
        self.logger.info("\n💡 提示：")
        self.logger.info("  - 按 Ctrl+C 停止程序")
        self.logger.info("  - 查看日志文件了解详细运行情况")
        self.logger.info("\n🤖 机器人正在运行中...\n")
        
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.logger.info("\n收到停止信号，正在关闭...")
        finally:
            self.shutdown()
    
    def shutdown(self):
        """关闭应用"""
        self.running = False
        self.logger.info("正在关闭应用...")
        
        if self.scheduler:
            try:
                self.scheduler.stop()
                self.logger.info("✓ 调度器已停止")
            except Exception as e:
                self.logger.error(f"✗ 停止调度器失败: {e}")
        
        self.logger.info("=" * 60)
        self.logger.info("F1赛程提醒机器人[PushPlus版]已关闭")
        self.logger.info("=" * 60)


def signal_handler(signum, frame):
    """处理系统信号"""
    logger = logging.getLogger(__name__)
    logger.info(f"收到信号 {signum}，准备退出...")
    sys.exit(0)


def main():
    """程序入口函数"""
    import time
    
    setup_logging()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    app = F1ReminderApp()
    app.start()


if __name__ == "__main__":
    main()