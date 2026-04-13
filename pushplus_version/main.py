"""
F1赛程提醒机器人 - PushPlus版本
通过PushPlus将消息推送到个人微信
推荐用于：快速部署、个人使用
"""

import requests
import logging
import sys
import signal
from typing import Optional, Dict, Any, List
from datetime import datetime

# 添加common目录到路径
sys.path.insert(0, '..')
from common import F1API, ReminderScheduler

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


# ==================== 配置 ====================
# PushPlus配置
# 获取Token：访问 http://www.pushplus.plus/ -> 微信扫码登录 -> 复制Token
PUSHPLUS_TOKEN = "YOUR_TOKEN_HERE"

# 可选：群组编码（一对多推送），默认None表示一对一推送
PUSHPLUS_TOPIC = None

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
class F1ReminderApp:
    """F1提醒应用主类"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.f1_api = None
        self.notify_bot = None
        self.scheduler = None
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
            self.scheduler = ReminderScheduler(self.f1_api, self.notify_bot, config)
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