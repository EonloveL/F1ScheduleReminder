"""
F1赛程提醒机器人 - QQ官方群聊版本
基于QQ官方Bot API V2实现
支持：群聊消息推送、定时提醒

部署说明：
1. 在QQ开放平台 (https://q.qq.com) 申请机器人账号
2. 获取AppID和AppSecret
3. 将机器人添加到QQ群并设置为管理员（用于@全体成员）
4. 填写下方配置
5. 运行：python main.py

目录结构：
   /opt/f1-bot/
   ├── common/
   │   ├── __init__.py
   │   ├── f1_api.py
   │   ├── scheduler.py
   │   └── qq_group_bot.py
   └── qq_group_official_version/
       ├── main.py
       └── requirements.txt
"""

import logging
import sys
import signal
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

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
    from common.qq_group_bot import QQGroupBot
except ImportError as e:
    print(f"❌ 导入错误：{e}")
    print("请确保common目录存在，并且包含必要的模块文件")
    sys.exit(1)

logger = logging.getLogger(__name__)


# ==================== 配置区域 ====================
# 从环境变量或 .env 文件读取（优先级：环境变量 > .env > 默认值）

# QQ开放平台配置（必填）
# 获取地址：https://q.qq.com -> 你的机器人 -> 开发设置
APPID = os.getenv("QQ_APPID", "YOUR_APPID_HERE")
APP_SECRET = os.getenv("QQ_APP_SECRET", "YOUR_SECRET_HERE")

# 是否使用沙箱环境（新机器人默认只能在沙箱环境测试）
USE_SANDBOX = os.getenv("USE_SANDBOX", "true").lower() == "true"

# QQ群配置（必填）
QQ_GROUP_ID = os.getenv("QQ_GROUP_ID", "YOUR_GROUP_ID_HERE")

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
SEASON = datetime.now().year  # 自动获取当前年份

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
        self.qq_bot = None
        self.scheduler = None
        self.running = False
        
    def initialize(self) -> bool:
        """初始化所有组件"""
        self.logger.info("=" * 60)
        self.logger.info("F1赛程提醒机器人[QQ官方群聊版]正在启动...")
        self.logger.info("=" * 60)
        
        # 检查配置
        if not self._check_config():
            return False
        
        # 初始化F1 API
        try:
            self.f1_api = F1API(SEASON)
            self.logger.info(f"✓ F1 API初始化成功 (赛季: {SEASON})")
        except Exception as e:
            self.logger.error(f"✗ F1 API初始化失败: {e}")
            return False
        
        # 初始化QQ官方群聊机器人
        try:
            self.qq_bot = QQGroupBot(
                appid=APPID,
                secret=APP_SECRET,
                group_id=QQ_GROUP_ID,
                use_sandbox=USE_SANDBOX
            )
            env_name = "沙箱环境" if USE_SANDBOX else "正式环境"
            self.logger.info(f"✓ QQ官方群聊机器人初始化成功 [{env_name}] (群号: {QQ_GROUP_ID})")
        except Exception as e:
            self.logger.error(f"✗ QQ官方群聊机器人初始化失败: {e}")
            self.logger.error("请检查：")
            self.logger.error("  1. AppID和AppSecret是否正确")
            self.logger.error("  2. 机器人是否已添加到QQ群")
            self.logger.error("  3. QQ群号是否正确")
            return False
        
        # 初始化调度器
        try:
            config = {
                'TIMEZONE': TIMEZONE,
                'REMINDER_TIMES': REMINDER_TIMES,
                'POST_RACE_DELAY': POST_RACE_DELAY
            }
            self.scheduler = ReminderScheduler(self.f1_api, self.qq_bot, config)
            self.logger.info("✓ 调度器初始化成功")
        except Exception as e:
            self.logger.error(f"✗ 调度器初始化失败: {e}")
            return False
        
        return True
    
    def _check_config(self) -> bool:
        """检查配置是否完整"""
        has_error = False
        
        if "YOUR_APPID_HERE" in APPID:
            self.logger.error("❌ 错误：请先配置APPID！")
            self.logger.error("   编辑 qq_group_official_version/main.py，修改 APPID")
            has_error = True
        
        if "YOUR_SECRET_HERE" in APP_SECRET:
            self.logger.error("❌ 错误：请先配置APP_SECRET！")
            has_error = True
        
        if QQ_GROUP_ID == "123456789":
            self.logger.error("❌ 错误：请先配置QQ_GROUP_ID！")
            self.logger.error("   获取方法：QQ群资料中查看群号")
            has_error = True
        
        if has_error:
            self.logger.error("\n⚠️ 请先完成配置后再运行！")
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
                self.qq_bot.send_startup_message()
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
        import time
        self.logger.info("\n💡 提示：")
        self.logger.info("  - 按 Ctrl+C 停止程序")
        self.logger.info("  - 查看日志文件了解详细运行情况")
        self.logger.info("  - 提醒消息会自动@全体成员")
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
        self.logger.info("F1赛程提醒机器人[QQ官方群聊版]已关闭")
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
