"""
F1赛程提醒机器人 - 企业微信版本（API方式）
使用f1api.dev获取赛道数据，自动更新
"""

import requests
import json
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
import sys
import signal
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# 添加common目录到路径
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

try:
    from common import F1API, ReminderScheduler
except ImportError as e:
    print(f"❌ 导入错误：{e}")
    sys.exit(1)

logger = logging.getLogger(__name__)


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
        """
        获取赛道详细信息
        
        Args:
            circuit_id: 赛道ID（如'suzuka', 'silverstone'）
            
        Returns:
            赛道信息字典
        """
        data = self._make_request(f"circuits/{circuit_id}")
        if data:
            return self._format_circuit_data(data)
        return None
    
    def search_circuit(self, query: str) -> Optional[Dict]:
        """
        搜索赛道
        
        Args:
            query: 搜索关键词（赛道名、国家、城市）
            
        Returns:
            赛道信息字典
        """
        data = self._make_request(f"circuits/search?q={query}")
        if data and 'circuits' in data and len(data['circuits']) > 0:
            # 返回第一个匹配结果
            return self._format_circuit_data(data['circuits'][0])
        return None
    
    def _format_circuit_data(self, api_data: Dict) -> Dict:
        """
        将API数据格式化为统一格式
        
        Args:
            api_data: API返回的原始数据
            
        Returns:
            格式化后的赛道数据
        """
        # 转换长度（米到公里）
        length_m = api_data.get('circuitLength', 0)
        length_km = length_m / 1000 if length_m else 0
        
        # 计算正赛距离（如果没有提供）
        laps = api_data.get('numberOfLaps', 0)
        race_distance = length_km * laps if length_km and laps else 0
        
        # 构建圈速记录信息
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
            'lap_length_km': round(length_km, 3),
            'laps': laps,
            'race_distance_km': round(race_distance, 3),
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
        """翻译赛道名称（简化版，实际可用完整映射）"""
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
        """翻译车手名字（简化版）"""
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


class WeComBot:
    """企业微信机器人封装类"""
    
    def __init__(self, webhook_url: str):
        self.webhook_url = webhook_url
        
    def send_markdown(self, content: str) -> bool:
        """发送Markdown消息"""
        data = {
            "msgtype": "markdown",
            "markdown": {"content": content}
        }
        
        try:
            response = requests.post(
                self.webhook_url,
                headers={'Content-Type': 'application/json; charset=utf-8'},
                data=json.dumps(data, ensure_ascii=False).encode('utf-8'),
                timeout=10
            )
            result = response.json()
            if result.get('errcode') == 0:
                logger.info("消息发送成功")
                return True
            else:
                logger.error(f"消息发送失败: {result}")
                return False
        except Exception as e:
            logger.error(f"发送消息异常: {e}")
            return False
    
    def send_session_reminder(self, session: Dict[str, Any]) -> bool:
        """发送比赛环节提醒"""
        from pytz import timezone
        
        utc_time = session['datetime']
        local_tz = timezone('Asia/Shanghai')
        local_time = utc_time.astimezone(local_tz)
        
        date_str = local_time.strftime("%Y年%m月%d日")
        time_str = local_time.strftime("%H:%M")
        weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][local_time.weekday()]
        
        icons = {"fp1": "🏎️", "fp2": "🏎️", "fp3": "🏎️", "qualifying": "⏱️", "sprint": "⚡", "race": "🏁"}
        icon = icons.get(session['type'], "🏎️")
        
        message = f"""{icon} **F1提醒** {icon}

> **{session['race_name']}**
> 📍 {session['circuit']}

**{session['name']}** 即将开始
⏰ **{date_str} {weekday} {time_str}** (北京时间)

敬请期待精彩比赛！🏎️💨"""
        
        return self.send_markdown(message)
    
    def send_race_result(self, race_data: Dict[str, Any], results: Dict[str, Any]) -> bool:
        """发送比赛结果"""
        race_name = race_data.get('raceName', 'F1大奖赛')
        
        message = f"🏁 **{race_name} 比赛结果**\n\n"
        message += "🥇 **前十名**:\n\n"
        
        if "Results" in results:
            for i, result in enumerate(results["Results"][:10], 1):
                driver = result.get("Driver", {})
                family_name = driver.get("familyName", "Unknown")
                team = result.get("Constructor", {}).get("name", "Unknown")
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
                message += f"{medal} **{family_name}** ({team})\n"
        
        message += "\n---\n#F1 #Formula1"
        return self.send_markdown(message)
    
    def send_weekly_preview(self, race_data: Dict[str, Any], circuit_data: Dict[str, Any]) -> bool:
        """发送比赛周预告（周一推送）"""
        from pytz import timezone
        
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
            dt = datetime.fromisoformat(dt_str)
            local_dt = dt.astimezone(local_tz)
            
            date_str = local_dt.strftime("%m月%d日")
            time_str = local_dt.strftime("%H:%M")
            
            if date_str != current_date:
                schedule_lines.append(f"**{date_str}**")
                current_date = date_str
            
            schedule_lines.append(f"  {session['name']}：{time_str}")
        
        schedule_text = '\n'.join(schedule_lines)
        
        flag = circuit_data.get('flag', '🏁')
        circuit_name = circuit_data.get('name', race_data['Circuit']['circuitName'])
        
        message = f"""🎉 **欢迎来到比赛周！**

本周为{flag}**{race_name}**
📍{circuit_name}

📊 **赛道信息**（来自实时数据）
• 首次办赛：{circuit_data.get('first_race', '未知')}年
• 单圈长度：{circuit_data.get('lap_length_km', '未知')}km
• 正赛圈数：{circuit_data.get('laps', '未知')}圈
• 正赛距离：{circuit_data.get('race_distance_km', '未知')}km
• 圈速记录：{circuit_data.get('lap_record', {}).get('time', '未知')} 
  ({circuit_data.get('lap_record', {}).get('driver', '未知')}，{circuit_data.get('lap_record', {}).get('year', '未知')})

⏰ **比赛时间（北京时间）**
{schedule_text}

---
🏎️💨 准备好享受速度与激情了吗？"""
        
        return self.send_markdown(message)
    
    def send_startup_message(self) -> bool:
        """发送启动通知"""
        message = """✅ **F1赛程提醒机器人已启动**

将为您自动推送：
• 🎉 每周一比赛周预告（API实时数据）
• 🏎️ 练习赛提醒
• ⏱️ 排位赛提醒
• 🏁 正赛提醒
• 📊 赛后结果

---
**Formula 1** 🏎️💨"""
        return self.send_markdown(message)


# ==================== 配置 ====================
WECOM_WEBHOOK_URL = os.getenv("WECOM_WEBHOOK_URL", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY_HERE")

REMINDER_TIMES = {
    "fp1": 30, "fp2": 30, "fp3": 30,
    "qualifying": 30, "sprint": 30, "race": 60
}

POST_RACE_DELAY = 30
TIMEZONE = "Asia/Shanghai"
SEASON = datetime.now().year  # 自动获取当前年份，无需手动修改

LOG_LEVEL = "INFO"
LOG_FILE = "f1_reminder.log"
SEND_STARTUP_MESSAGE = True


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
            logger.info("检查本周是否有比赛（使用API方式）...")
            
            from datetime import timezone
            now = datetime.now(timezone.utc)
            week_start = now
            week_end = now + timedelta(days=7)
            
            schedule = self.f1_api.get_schedule()
            
            this_week_race = None
            for race in schedule:
                race_date = datetime.strptime(race['date'], '%Y-%m-%d')
                race_date = race_date.replace(tzinfo=timezone.utc)
                
                if week_start <= race_date <= week_end:
                    this_week_race = race
                    break
            
            if this_week_race:
                logger.info(f"本周有比赛: {this_week_race['raceName']}")
                
                circuit_id = this_week_race['Circuit'].get('circuitId', '')
                circuit_name = this_week_race['Circuit']['circuitName']
                
                # 通过API获取赛道信息
                logger.info(f"正在从API获取赛道信息: {circuit_id}")
                circuit_data = self.circuits_api.get_circuit_info(circuit_id)
                
                if not circuit_data:
                    # 如果ID找不到，尝试搜索
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
                logger.info("比赛周预告发送成功（API数据）")
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
        self.logger.info("F1赛程提醒机器人[企业微信版-API方式]正在启动...")
        self.logger.info("=" * 60)
        
        if "YOUR_KEY_HERE" in WECOM_WEBHOOK_URL:
            self.logger.error("错误：请先配置企业微信Webhook地址！")
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
        
        # 初始化企业微信机器人
        try:
            self.notify_bot = WeComBot(WECOM_WEBHOOK_URL)
            self.logger.info("✓ 企业微信机器人初始化成功")
        except Exception as e:
            self.logger.error(f"✗ 企业微信机器人初始化失败: {e}")
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
        
        if SEND_STARTUP_MESSAGE:
            try:
                self.notify_bot.send_startup_message()
            except Exception as e:
                self.logger.warning(f"发送启动消息失败: {e}")
        
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
        
        self._main_loop()
    
    def _main_loop(self):
        """主循环"""
        import time
        self.logger.info("\n💡 提示：")
        self.logger.info("  - 按 Ctrl+C 停止程序")
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
        self.logger.info("F1赛程提醒机器人已关闭")
        self.logger.info("=" * 60)


def signal_handler(signum, frame):
    """处理系统信号"""
    logger = logging.getLogger(__name__)
    logger.info(f"收到信号 {signum}，准备退出...")
    sys.exit(0)


def main():
    """程序入口函数"""
    setup_logging()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    app = F1ReminderApp()
    app.start()


if __name__ == "__main__":
    main()
