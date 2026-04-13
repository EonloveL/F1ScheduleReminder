"""
F1赛程提醒机器人 - 个人微信版本
使用 itchat 库实现微信消息推送
推荐用于：本地测试、个人微信群
注意：不推荐服务器部署，稳定性较差
"""

import itchat
from itchat.content import TEXT
import time
import logging
import sys
import signal
from datetime import datetime
from typing import Optional, Dict, Any, List

# 添加common目录到路径
sys.path.insert(0, '..')
from common import F1API, ReminderScheduler

logger = logging.getLogger(__name__)


class WeChatBot:
    """微信机器人封装类"""
    
    def __init__(self, group_name: str):
        """
        初始化微信机器人
        
        Args:
            group_name: 要发送消息的微信群名称
        """
        self.group_name = group_name
        self.group_id = None
        self.is_logged_in = False
        
    def login(self, use_qr_code_image: bool = True) -> bool:
        """
        登录微信
        
        Args:
            use_qr_code_image: 是否使用图片二维码，False则在命令行显示
            
        Returns:
            是否登录成功
        """
        try:
            if use_qr_code_image:
                itchat.auto_login(
                    hotReload=True,
                    enableCmdQR=False,
                    picDir='qr_code.png'
                )
            else:
                itchat.auto_login(
                    hotReload=True,
                    enableCmdQR=2
                )
            
            self.is_logged_in = True
            logger.info("微信登录成功")
            
            # 查找目标群聊
            self._find_group()
            
            return True
            
        except Exception as e:
            logger.error(f"微信登录失败: {e}")
            return False
    
    def _find_group(self) -> bool:
        """查找目标微信群"""
        chatrooms = itchat.get_chatrooms(update=True)
        
        for room in chatrooms:
            if room['NickName'] == self.group_name:
                self.group_id = room['UserName']
                logger.info(f"找到群聊: {self.group_name}")
                return True
        
        logger.warning(f"未找到群聊: {self.group_name}")
        logger.info("可用的群聊列表:")
        for room in chatrooms:
            logger.info(f"  - {room['NickName']}")
        
        return False
    
    def send_message(self, message: str) -> bool:
        """发送消息到微信群"""
        if not self.is_logged_in:
            logger.error("未登录微信，无法发送消息")
            return False
        
        if not self.group_id:
            if not self._find_group():
                logger.error(f"无法找到群聊: {self.group_name}")
                return False
        
        try:
            itchat.send(message, toUserName=self.group_id)
            logger.info(f"消息发送成功: {message[:50]}...")
            return True
            
        except Exception as e:
            logger.error(f"消息发送失败: {e}")
            self._find_group()
            try:
                itchat.send(message, toUserName=self.group_id)
                return True
            except Exception as e2:
                logger.error(f"重试发送失败: {e2}")
                return False
    
    def send_session_reminder(self, session: Dict[str, Any]) -> bool:
        """发送比赛环节提醒消息"""
        from pytz import timezone
        
        utc_time = session['datetime']
        local_tz = timezone('Asia/Shanghai')
        local_time = utc_time.astimezone(local_tz)
        
        time_str = local_time.strftime("%m月%d日 %H:%M")
        weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][local_time.weekday()]
        
        emojis = {
            "fp1": "🏎️", "fp2": "🏎️", "fp3": "🏎️",
            "qualifying": "⏱️", "sprint": "⚡", "race": "🏁"
        }
        
        emoji = emojis.get(session['type'], "🏎️")
        
        message = f"""
{emoji} F1提醒 {emoji}

📍 {session['race_name']}
🏟️ {session['circuit']}

🔥 {session['name']} 即将开始
⏰ {weekday} {time_str} (北京时间)

敬请期待！
        """.strip()
        
        return self.send_message(message)
    
    def send_race_result(self, race_data: Dict[str, Any], results: Dict[str, Any]) -> bool:
        """发送比赛结果"""
        race_name = race_data.get('raceName', 'F1大奖赛')
        
        message = f"🏁 {race_name} 比赛结果\n\n"
        
        if "Results" in results:
            message += "🥇 前十名:\n"
            for i, result in enumerate(results["Results"][:10], 1):
                position = result.get("position", "-")
                driver = result.get("Driver", {})
                family_name = driver.get("familyName", "Unknown")
                given_name = driver.get("givenName", "")
                team = result.get("Constructor", {}).get("name", "Unknown")
                
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
                
                message += f"{medal} {family_name} ({team})\n"
            
            fastest_lap = results["Results"][0].get("FastestLap", {})
            if fastest_lap and fastest_lap.get("rank") == "1":
                fastest_driver = results["Results"][0].get("Driver", {}).get("familyName", "")
                lap_time = fastest_lap.get("Time", {}).get("time", "")
                message += f"\n⚡ 最快圈速: {fastest_driver} - {lap_time}\n"
        
        message += "\n#F1 #Formula1"
        
        return self.send_message(message)
    
    def send_daily_schedule(self, sessions: List[Dict[str, Any]]) -> bool:
        """发送每日赛程汇总"""
        if not sessions:
            return False
        
        from pytz import timezone
        local_tz = timezone('Asia/Shanghai')
        
        race_name = sessions[0]['race_name']
        circuit = sessions[0]['circuit']
        
        message = f"""
📅 今日F1赛程 - {race_name}
🏟️ {circuit}

        """.strip()
        
        for session in sessions:
            utc_time = session['datetime']
            local_time = utc_time.astimezone(local_tz)
            time_str = local_time.strftime("%H:%M")
            
            emojis = {
                "fp1": "🏎️", "fp2": "🏎️", "fp3": "🏎️",
                "qualifying": "⏱️", "sprint": "⚡", "race": "🏁"
            }
            
            emoji = emojis.get(session['type'], "🏎️")
            message += f"{emoji} {session['name']}: {time_str}\n"
        
        message += "\n记得准时观看！🏎️💨"
        
        return self.send_message(message)
    
    def send_startup_message(self) -> bool:
        """发送启动通知"""
        message = "✅ F1赛程提醒机器人已启动\n\n将为您自动推送：\n🏎️ 练习赛提醒\n⏱️ 排位赛提醒\n🏁 正赛提醒\n📊 赛后结果\n\nFormula 1 🏎️💨"
        return self.send_message(message)
    
    def logout(self):
        """登出微信"""
        if self.is_logged_in:
            itchat.logout()
            self.is_logged_in = False
            logger.info("微信已登出")
    
    @staticmethod
    def keep_alive():
        """保持微信连接活跃"""
        pass


# ==================== 配置 ====================
# 要发送消息的微信群名称（必须完全匹配）
WECHAT_GROUP_NAME = "F1车迷群"

# 是否使用图片二维码登录（False则使用命令行显示二维码）
USE_QR_CODE_IMAGE = True

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
        self.wechat_bot = None
        self.scheduler = None
        self.running = False
        
    def initialize(self) -> bool:
        """初始化所有组件"""
        self.logger.info("=" * 60)
        self.logger.info("F1赛程提醒机器人[个人微信版]正在启动...")
        self.logger.info("⚠️  注意：此版本仅推荐本地测试使用，不推荐服务器部署")
        self.logger.info("=" * 60)
        
        # 初始化F1 API
        try:
            self.f1_api = F1API(SEASON)
            self.logger.info(f"✓ F1 API初始化成功 (赛季: {SEASON})")
        except Exception as e:
            self.logger.error(f"✗ F1 API初始化失败: {e}")
            return False
        
        # 初始化微信机器人
        try:
            self.wechat_bot = WeChatBot(WECHAT_GROUP_NAME)
            self.logger.info(f"✓ 微信机器人初始化成功 (目标群: {WECHAT_GROUP_NAME})")
        except Exception as e:
            self.logger.error(f"✗ 微信机器人初始化失败: {e}")
            return False
        
        # 登录微信
        self.logger.info("正在登录微信，请扫描二维码...")
        if not self.wechat_bot.login(use_qr_code_image=USE_QR_CODE_IMAGE):
            self.logger.error("✗ 微信登录失败")
            return False
        self.logger.info("✓ 微信登录成功")
        
        # 初始化调度器
        try:
            config = {
                'TIMEZONE': TIMEZONE,
                'REMINDER_TIMES': REMINDER_TIMES,
                'POST_RACE_DELAY': POST_RACE_DELAY
            }
            self.scheduler = ReminderScheduler(self.f1_api, self.wechat_bot, config)
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
                self.wechat_bot.send_startup_message()
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
                self.wechat_bot.keep_alive()
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
        
        if self.wechat_bot:
            try:
                self.wechat_bot.logout()
                self.logger.info("✓ 微信已登出")
            except Exception as e:
                self.logger.error(f"✗ 登出微信失败: {e}")
        
        self.logger.info("=" * 60)
        self.logger.info("F1赛程提醒机器人[个人微信版]已关闭")
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