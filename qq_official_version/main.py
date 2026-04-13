"""
F1赛程提醒机器人 - QQ官方Bot版本
基于QQ官方Bot API V2实现
支持：频道消息、日程管理、双频道配置

部署说明：
1. 在QQ开放平台 (https://q.qq.com) 申请机器人账号
2. 获取AppID和AppSecret
3. 将机器人添加到QQ频道并设置为管理员
4. 填写下方配置
5. 运行：python main.py

目录结构：
   /opt/f1-bot/
   ├── common/
   │   ├── __init__.py
   │   ├── f1_api.py
   │   ├── scheduler.py
   │   └── qq_official_bot.py
   └── qq_official_version/
       ├── main.py
       └── requirements.txt
"""

import logging
import sys
import signal
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

# 添加common目录到路径
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

try:
    from common import F1API, ReminderScheduler
    from common.qq_official_bot import QQOfficialBot, ScheduleManager
except ImportError as e:
    print(f"❌ 导入错误：{e}")
    print("请确保common目录存在，并且包含必要的模块文件")
    sys.exit(1)

logger = logging.getLogger(__name__)


# ==================== 配置区域 ====================

# QQ开放平台配置（必填）
# 获取地址：https://q.qq.com -> 你的机器人 -> 开发设置
APPID = "YOUR_APPID_HERE"          # ← 替换为你的AppID
APP_SECRET = "YOUR_SECRET_HERE"    # ← 替换为你的AppSecret

# 频道配置（必填）
# 获取方法：在QQ频道中右键子频道 -> 复制子频道ID
ANNOUNCEMENT_CHANNEL_ID = "YOUR_ANNOUNCEMENT_CHANNEL_ID"  # 公告/赛程子频道
LIVE_DATA_CHANNEL_ID = "YOUR_LIVE_DATA_CHANNEL_ID"        # 实时数据子频道
SCHEDULE_CHANNEL_ID = "YOUR_SCHEDULE_CHANNEL_ID"          # 日程子频道（如果不填则使用公告频道）

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

# ==================== 日程更新配置 ====================
# 正常：每周一上午8:00创建本周日程
# 拉斯维加斯周：周二上午8:00创建（正赛在次周周一中午）
SCHEDULE_UPDATE_DAY = 0      # 0=周一, 1=周二
SCHEDULE_UPDATE_HOUR = 8     # 更新小时
SCHEDULE_UPDATE_MINUTE = 0   # 更新分钟

# ==================== 特殊赛道处理 ====================
# 这些站次的正赛在次周周一中午，需要周二创建日程
SPECIAL_RACES = [
    "Las Vegas Grand Prix",      # 拉斯维加斯
    "las vegas",                  # 英文别名
    "拉斯维加斯",                 # 中文别名
    "Qatar Grand Prix",          # 卡塔尔（也可能是深夜/凌晨）
    "qatar",
    "卡塔尔",
]

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
        self.schedule_manager = None
        self.running = False
        
    def initialize(self) -> bool:
        """初始化所有组件"""
        self.logger.info("=" * 60)
        self.logger.info("F1赛程提醒机器人[QQ官方版]正在启动...")
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
        
        # 初始化QQ官方机器人
        try:
            schedule_channel = SCHEDULE_CHANNEL_ID if SCHEDULE_CHANNEL_ID and "YOUR" not in SCHEDULE_CHANNEL_ID else None
            self.qq_bot = QQOfficialBot(
                appid=APPID,
                secret=APP_SECRET,
                announcement_channel_id=ANNOUNCEMENT_CHANNEL_ID,
                live_data_channel_id=LIVE_DATA_CHANNEL_ID,
                schedule_channel_id=schedule_channel
            )
            self.logger.info("✓ QQ官方机器人初始化成功")
        except Exception as e:
            self.logger.error(f"✗ QQ官方机器人初始化失败: {e}")
            self.logger.error("请检查：")
            self.logger.error("  1. AppID和AppSecret是否正确")
            self.logger.error("  2. 机器人是否已添加到频道")
            self.logger.error("  3. 机器人是否拥有管理员权限")
            return False
        
        # 初始化日程管理器
        try:
            self.schedule_manager = ScheduleManager(self.qq_bot, self.f1_api)
            self.logger.info("✓ 日程管理器初始化成功")
        except Exception as e:
            self.logger.error(f"✗ 日程管理器初始化失败: {e}")
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
            self.logger.error("   编辑 qq_official_version/main.py，修改 APPID")
            has_error = True
        
        if "YOUR_SECRET_HERE" in APP_SECRET:
            self.logger.error("❌ 错误：请先配置APP_SECRET！")
            has_error = True
        
        if "YOUR_ANNOUNCEMENT_CHANNEL_ID" in ANNOUNCEMENT_CHANNEL_ID:
            self.logger.error("❌ 错误：请先配置ANNOUNCEMENT_CHANNEL_ID！")
            self.logger.error("   获取方法：在QQ频道中右键子频道 -> 复制子频道ID")
            has_error = True
        
        if "YOUR_LIVE_DATA_CHANNEL_ID" in LIVE_DATA_CHANNEL_ID:
            self.logger.error("❌ 错误：请先配置LIVE_DATA_CHANNEL_ID！")
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
        
        # 添加每周日程更新任务
        try:
            self._add_weekly_schedule_job()
        except Exception as e:
            self.logger.error(f"添加日程更新任务失败: {e}")
        
        self.running = True
        self.logger.info("✓ 应用启动成功！")
        self.logger.info("=" * 60)
        
        # 打印即将进行的比赛
        self._print_upcoming_races()
        
        # 主循环
        self._main_loop()
    
    def _add_weekly_schedule_job(self):
        """添加每周日程更新任务"""
        from apscheduler.triggers.cron import CronTrigger
        from pytz import timezone
        
        local_tz = timezone(TIMEZONE)
        
        # 首先创建一次本周日程（如果还没创建的话）
        self.schedule_manager.create_weekly_schedules()
        
        # 添加定时任务
        job = self.scheduler.scheduler.add_job(
            func=self._weekly_schedule_update,
            trigger=CronTrigger(
                day_of_week=SCHEDULE_UPDATE_DAY,
                hour=SCHEDULE_UPDATE_HOUR,
                minute=SCHEDULE_UPDATE_MINUTE,
                timezone=local_tz
            ),
            id='weekly_schedule_update',
            replace_existing=True
        )
        
        day_name = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][SCHEDULE_UPDATE_DAY]
        self.logger.info(f"✓ 已添加每周日程更新任务（{day_name} {SCHEDULE_UPDATE_HOUR:02d}:{SCHEDULE_UPDATE_MINUTE:02d}）")
        
        # 如果是拉斯维加斯周，添加额外的周二更新任务
        job2 = self.scheduler.scheduler.add_job(
            func=self._check_and_update_for_special_races,
            trigger=CronTrigger(
                day_of_week=1,  # 周二
                hour=8,
                minute=0,
                timezone=local_tz
            ),
            id='special_race_schedule_check',
            replace_existing=True
        )
        self.logger.info("✓ 已添加特殊赛程检查任务（周二 08:00）")
    
    def _weekly_schedule_update(self):
        """每周定时更新日程"""
        self.logger.info("🔄 执行每周日程更新...")
        
        # 检查是否是拉斯维加斯周
        if self._is_special_race_week():
            self.logger.info("检测到特殊赛程周，跳过周一更新，等待周二")
            return
        
        self.schedule_manager.create_weekly_schedules()
    
    def _check_and_update_for_special_races(self):
        """检查并更新特殊赛程（拉斯维加斯等）"""
        self.logger.info("🔄 检查特殊赛程...")
        
        if self._is_special_race_week():
            self.logger.info("检测到特殊赛程周，执行日程创建")
            self.schedule_manager.create_weekly_schedules()
    
    def _is_special_race_week(self) -> bool:
        """检查本周是否是特殊赛程周（拉斯维加斯等）"""
        try:
            upcoming = self.f1_api.get_upcoming_sessions(hours_ahead=168)
            for session in upcoming:
                race_name_lower = session['race_name'].lower()
                for special in SPECIAL_RACES:
                    if special.lower() in race_name_lower:
                        return True
            return False
        except Exception as e:
            self.logger.warning(f"检查特殊赛程失败: {e}")
            return False
    
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
                
                # 检查是否是特殊赛程周
                if self._is_special_race_week():
                    self.logger.info("\n⚠️ 本周为特殊赛程（拉斯维加斯等），日程将在周二创建")
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
        self.logger.info("F1赛程提醒机器人[QQ官方版]已关闭")
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
