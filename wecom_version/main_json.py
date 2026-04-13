"""
F1赛程提醒机器人 - 企业微信版本（JSON配置文件方式）
使用本地JSON文件存储赛道数据，稳定性高
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


class CircuitsManager:
    """赛道数据管理器（JSON文件方式）"""
    
    def __init__(self, data_file: str = None):
        """
        初始化赛道管理器
        
        Args:
            data_file: JSON数据文件路径，默认使用common/circuits_data.json
        """
        if data_file is None:
            # 默认数据文件路径
            current_dir = os.path.dirname(os.path.abspath(__file__))
            parent_dir = os.path.dirname(current_dir)
            data_file = os.path.join(parent_dir, 'common', 'circuits_data.json')
        
        self.data_file = data_file
        self.circuits = self._load_data()
    
    def _load_data(self) -> Dict:
        """加载赛道数据"""
        try:
            with open(self.data_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            logger.error(f"赛道数据文件未找到: {self.data_file}")
            return {}
        except json.JSONDecodeError as e:
            logger.error(f"赛道数据文件格式错误: {e}")
            return {}
    
    def get_circuit_by_id(self, circuit_id: str) -> Optional[Dict]:
        """
        根据赛道ID获取赛道信息
        
        Args:
            circuit_id: 赛道ID（如'suzuka', 'silverstone'）
            
        Returns:
            赛道信息字典
        """
        return self.circuits.get(circuit_id)
    
    def get_circuit_by_name(self, circuit_name: str) -> Optional[Dict]:
        """
        根据赛道名称获取赛道信息（支持中英文匹配）
        
        Args:
            circuit_name: 赛道名称
            
        Returns:
            赛道信息字典
        """
        for circuit_id, data in self.circuits.items():
            if (circuit_name in data.get('name', '') or 
                circuit_name in data.get('name_en', '') or
                data.get('name') in circuit_name or
                data.get('name_en') in circuit_name):
                return data
        return None


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
        """
        发送比赛周预告（周一推送）
        
        Args:
            race_data: 比赛数据
            circuit_data: 赛道数据
        """
        from pytz import timezone
        
        # 提取比赛和赛道信息
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
        
        # 添加正赛
        sessions.append({
            'type': 'race',
            'name': '正赛',
            'date': race_data['date'],
            'time': race_data['time']
        })
        
        # 按时间排序
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
        
        # 构建消息
        flag = circuit_data.get('flag', '🏁')
        circuit_name = circuit_data.get('name', race_data['Circuit']['circuitName'])
        
        message = f"""🎉 **欢迎来到比赛周！**

本周为{flag}**{race_name}**
📍{circuit_name}

📊 **赛道信息**
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
• 🎉 每周一比赛周预告
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
    
    def __init__(self, f1_api, notify_bot, config, circuits_manager):
        super().__init__(f1_api, notify_bot, config)
        self.circuits_manager = circuits_manager
    
    def add_weekly_preview_job(self):
        """
        添加每周一比赛周预告任务
        每周一早上9:00检查本周是否有比赛，如果有则发送预告
        """
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
            
            # 获取当前日期（周一）
            from datetime import timezone
            now = datetime.now(timezone.utc)
            
            # 获取本周的赛程（周一到周日）
            week_start = now
            week_end = now + timedelta(days=7)
            
            # 获取赛程
            schedule = self.f1_api.get_schedule()
            
            # 查找本周的比赛
            this_week_race = None
            for race in schedule:
                race_date = datetime.strptime(race['date'], '%Y-%m-%d')
                race_date = race_date.replace(tzinfo=timezone.utc)
                
                if week_start <= race_date <= week_end:
                    this_week_race = race
                    break
            
            if this_week_race:
                logger.info(f"本周有比赛: {this_week_race['raceName']}")
                
                # 获取赛道信息
                circuit_id = this_week_race['Circuit'].get('circuitId', '')
                circuit_name = this_week_race['Circuit']['circuitName']
                
                # 先尝试通过ID获取
                circuit_data = self.circuits_manager.get_circuit_by_id(circuit_id)
                
                # 如果找不到，通过名称匹配
                if not circuit_data:
                    circuit_data = self.circuits_manager.get_circuit_by_name(circuit_name)
                
                # 如果还是找不到，使用默认数据
                if not circuit_data:
                    logger.warning(f"未找到赛道数据: {circuit_id} / {circuit_name}")
                    circuit_data = {
                        'name': circuit_name,
                        'flag': '🏁',
                        'first_race': '未知',
                        'lap_length_km': '未知',
                        'laps': '未知',
                        'race_distance_km': '未知',
                        'lap_record': {'time': '未知', 'driver': '未知', 'year': '未知'}
                    }
                
                # 发送预告
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
        self.circuits_manager = None
        self.running = False
        
    def initialize(self) -> bool:
        """初始化所有组件"""
        self.logger.info("=" * 60)
        self.logger.info("F1赛程提醒机器人[企业微信版-JSON方式]正在启动...")
        self.logger.info("=" * 60)
        
        if "YOUR_KEY_HERE" in WECOM_WEBHOOK_URL:
            self.logger.error("错误：请先配置企业微信Webhook地址！")
            return False
        
        # 初始化赛道数据管理器
        try:
            self.circuits_manager = CircuitsManager()
            circuit_count = len(self.circuits_manager.circuits)
            self.logger.info(f"✓ 赛道数据加载成功，共 {circuit_count} 条赛道")
        except Exception as e:
            self.logger.error(f"✗ 赛道数据加载失败: {e}")
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
                self.f1_api, self.notify_bot, config, self.circuits_manager
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
            self.scheduler.add_weekly_preview_job()  # 添加周一预告任务
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
