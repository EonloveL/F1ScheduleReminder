"""
F1赛程提醒机器人 - 主程序入口（支持多种推送方式）
整合所有模块，启动机器人服务
"""

import logging
import sys
import time
import signal
from datetime import datetime

# 导入配置
from config import (
    NOTIFY_TYPE, WECOM_WEBHOOK_URL, PUSHPLUS_TOKEN, PUSHPLUS_TOPIC,
    WECHAT_GROUP_NAME, USE_QR_CODE_IMAGE, REMINDER_TIMES,
    POST_RACE_DELAY, TIMEZONE, SEASON, LOG_LEVEL, LOG_FILE,
    SEND_STARTUP_MESSAGE
)

# 导入模块
from f1_api import F1API
from scheduler import ReminderScheduler

# 根据配置导入对应的消息模块
if NOTIFY_TYPE == "wecom":
    from wecom_bot import WeComBot
elif NOTIFY_TYPE == "pushplus":
    from pushplus_bot import PushPlusBot
elif NOTIFY_TYPE == "wechat":
    from wechat_bot import WeChatBot
else:
    raise ValueError(f"不支持的消息类型: {NOTIFY_TYPE}")


# ==================== 日志配置 ====================
def setup_logging():
    """配置日志"""
    log_format = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    
    # 控制台处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(logging.Formatter(log_format))
    
    # 文件处理器
    file_handler = logging.FileHandler(LOG_FILE, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(log_format))
    
    # 根日志器配置
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL),
        format=log_format,
        handlers=[console_handler, file_handler]
    )


# ==================== 消息机器人工厂 ====================
def create_notify_bot():
    """
    根据配置创建对应的消息机器人
    
    Returns:
        消息机器人实例
    """
    if NOTIFY_TYPE == "wecom":
        if "YOUR_KEY_HERE" in WECOM_WEBHOOK_URL:
            raise ValueError("请先配置企业微信Webhook地址！")
        return WeComBot(WECOM_WEBHOOK_URL)
    
    elif NOTIFY_TYPE == "pushplus":
        if "YOUR_TOKEN" in PUSHPLUS_TOKEN:
            raise ValueError("请先配置PushPlus Token！")
        return PushPlusBot(PUSHPLUS_TOKEN, PUSHPLUS_TOPIC)
    
    elif NOTIFY_TYPE == "wechat":
        bot = WeChatBot(WECHAT_GROUP_NAME)
        if not bot.login(use_qr_code_image=USE_QR_CODE_IMAGE):
            raise RuntimeError("微信登录失败！")
        return bot
    
    else:
        raise ValueError(f"未知的消息类型: {NOTIFY_TYPE}")


# ==================== 主程序类 ====================
class F1ReminderApp:
    """F1提醒应用主类"""
    
    def __init__(self):
        """初始化应用"""
        self.logger = logging.getLogger(__name__)
        self.f1_api = None
        self.notify_bot = None
        self.scheduler = None
        self.running = False
        
    def initialize(self) -> bool:
        """
        初始化所有组件
        
        Returns:
            初始化是否成功
        """
        self.logger.info("=" * 60)
        self.logger.info("F1赛程提醒机器人正在启动...")
        self.logger.info(f"消息推送方式: {NOTIFY_TYPE}")
        self.logger.info("=" * 60)
        
        # 1. 初始化F1 API
        try:
            self.f1_api = F1API(SEASON)
            self.logger.info(f"✓ F1 API初始化成功 (赛季: {SEASON})")
        except Exception as e:
            self.logger.error(f"✗ F1 API初始化失败: {e}")
            return False
        
        # 2. 初始化消息机器人
        try:
            self.notify_bot = create_notify_bot()
            self.logger.info(f"✓ 消息机器人初始化成功 ({NOTIFY_TYPE})")
        except Exception as e:
            self.logger.error(f"✗ 消息机器人初始化失败: {e}")
            return False
        
        # 3. 初始化调度器
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
        """主循环，保持程序运行"""
        self.logger.info("\n💡 提示：")
        self.logger.info("  - 按 Ctrl+C 停止程序")
        self.logger.info("  - 查看日志文件了解详细运行情况")
        self.logger.info("\n🤖 机器人正在运行中...\n")
        
        try:
            while self.running:
                time.sleep(1)
                
                # 如果是微信方式，需要保活
                if NOTIFY_TYPE == "wechat" and hasattr(self.notify_bot, 'keep_alive'):
                    self.notify_bot.keep_alive()
                
        except KeyboardInterrupt:
            self.logger.info("\n收到停止信号，正在关闭...")
        finally:
            self.shutdown()
    
    def shutdown(self):
        """关闭应用"""
        self.running = False
        self.logger.info("正在关闭应用...")
        
        # 停止调度器
        if self.scheduler:
            try:
                self.scheduler.stop()
                self.logger.info("✓ 调度器已停止")
            except Exception as e:
                self.logger.error(f"✗ 停止调度器失败: {e}")
        
        # 登出微信（如果是微信方式）
        if NOTIFY_TYPE == "wechat" and self.notify_bot:
            try:
                self.notify_bot.logout()
                self.logger.info("✓ 微信已登出")
            except Exception as e:
                self.logger.error(f"✗ 登出微信失败: {e}")
        
        self.logger.info("=" * 60)
        self.logger.info("F1赛程提醒机器人已关闭")
        self.logger.info("=" * 60)


# ==================== 信号处理 ====================
def signal_handler(signum, frame):
    """处理系统信号"""
    logger = logging.getLogger(__name__)
    logger.info(f"收到信号 {signum}，准备退出...")
    sys.exit(0)


# ==================== 程序入口 ====================
def main():
    """程序入口函数"""
    # 配置日志
    setup_logging()
    
    # 注册信号处理
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # 创建并启动应用
    app = F1ReminderApp()
    app.start()


if __name__ == "__main__":
    main()