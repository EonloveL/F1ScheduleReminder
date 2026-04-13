"""
F1赛程提醒机器人 - Discord版本
使用Discord Bot发送消息
推荐用于：Discord服务器、社区群组

部署说明：
1. 将此目录（discord_version）和上级目录的common目录一起上传到服务器
2. 在Discord Developer Portal创建Bot并获取Token
3. 将Bot邀请到你的Discord服务器
4. 目录结构应为：
   /opt/f1-bot/
   ├── common/
   │   ├── __init__.py
   │   ├── f1_api.py
   │   └── scheduler.py
   └── discord_version/
       ├── main.py
       └── requirements.txt
5. 在discord_version目录下运行：python main.py
"""
import asyncio
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime
import sys
import signal
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# 添加common目录到路径（支持多种运行方式）
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

try:
    from common import F1API, ReminderScheduler
except ImportError as e:
    print(f"❌ 导入错误：{e}")
    print("请确保common目录存在，并且包含必要的模块文件")
    print("目录结构应为：")
    print("  project/")
    print("    ├── common/")
    print("    └── discord_version/")
    sys.exit(1)

import discord
from discord.ext import commands, tasks
import aiohttp

logger = logging.getLogger(__name__)


class DiscordBot:
    """Discord机器人封装类"""
    
    def __init__(self, bot_token: str, channel_id: int, proxy: Optional[str] = None):
        """
        初始化Discord机器人
        
        Args:
            bot_token: Discord Bot的Token
            channel_id: 要发送消息的频道ID
            proxy: 代理服务器地址（如 http://127.0.0.1:7890）
        """
        self.bot_token = bot_token
        self.channel_id = channel_id
        self.channel = None
        self.proxy = proxy
        
        # 创建Discord Bot实例，配置代理
        intents = discord.Intents.default()
        intents.message_content = True
        
        # 配置aiohttp使用代理
        if proxy:
            connector = aiohttp.TCPConnector(limit=100, ttl_dns_cache=300)
            self.bot = commands.Bot(
                command_prefix='!', 
                intents=intents,
                proxy=proxy,
                connector=connector
            )
            logger.info(f"已配置代理: {proxy}")
        else:
            self.bot = commands.Bot(command_prefix='!', intents=intents)
        
        # 注册事件处理器
        self.bot.event(self.on_ready)
        
        # 存储发送消息的队列（用于非异步环境调用）
        self.message_queue = asyncio.Queue()
        
    async def on_ready(self):
        """Bot就绪时调用"""
        logger.info(f'✓ Discord Bot已登录: {self.bot.user.name} (ID: {self.bot.user.id})')
        
        # 获取目标频道
        self.channel = self.bot.get_channel(self.channel_id)
        if self.channel:
            logger.info(f'✓ 已连接到频道: {self.channel.name} (ID: {self.channel_id})')
        else:
            logger.error(f'✗ 无法找到频道 ID: {self.channel_id}')
            logger.error('请确保Bot已被邀请到此频道，且频道ID正确')
        
    async def send_message(self, content: str, embed: Optional[discord.Embed] = None) -> bool:
        """发送消息到Discord频道"""
        if not self.channel:
            logger.error("Discord频道未就绪，无法发送消息")
            return False
            
        try:
            if embed:
                await self.channel.send(content=content, embed=embed)
            else:
                await self.channel.send(content=content)
            logger.info(f"消息发送成功")
            return True
        except Exception as e:
            logger.error(f"发送消息失败: {e}")
            return False
    
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
        
        # 创建Discord Embed
        embed = discord.Embed(
            title=f"{icon} F1提醒 {icon}",
            description=f"**{session['race_name']}**",
            color=discord.Color.red()
        )
        
        embed.add_field(name="📍 赛道", value=session['circuit'], inline=False)
        embed.add_field(name="🏁 环节", value=session['name'], inline=True)
        embed.add_field(name="⏰ 时间", value=f"{date_str} {weekday} {time_str} (北京时间)", inline=False)
        embed.set_footer(text="Formula 1 🏎️💨")
        
        # 使用asyncio运行异步发送
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.send_message("", embed=embed),
                self.bot.loop
            )
            return future.result(timeout=10)
        except Exception as e:
            logger.error(f"发送提醒消息失败: {e}")
            return False
    
    def send_race_result(self, race_data: Dict[str, Any], results: Dict[str, Any]) -> bool:
        """发送比赛结果"""
        race_name = race_data.get('raceName', 'F1大奖赛')
        
        embed = discord.Embed(
            title=f"🏁 {race_name} 比赛结果",
            color=discord.Color.gold()
        )
        
        if "Results" in results:
            podium = []
            other_results = []
            
            for i, result in enumerate(results["Results"][:10], 1):
                driver = result.get("Driver", {})
                family_name = driver.get("familyName", "Unknown")
                team = result.get("Constructor", {}).get("name", "Unknown")
                
                if i <= 3:
                    medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
                    podium.append(f"{medal} **{family_name}** ({team})")
                else:
                    other_results.append(f"{i}. {family_name} ({team})")
            
            if podium:
                embed.add_field(name="🏆 领奖台", value="\n".join(podium), inline=False)
            
            if other_results:
                embed.add_field(name="📊 第4-10名", value="\n".join(other_results), inline=False)
            
            # 最快圈速
            fastest_lap = results["Results"][0].get("FastestLap", {})
            if fastest_lap and fastest_lap.get("rank") == "1":
                fastest_driver = results["Results"][0].get("Driver", {}).get("familyName", "")
                lap_time = fastest_lap.get("Time", {}).get("time", "")
                embed.add_field(name="⚡ 最快圈速", value=f"{fastest_driver} - {lap_time}", inline=False)
        
        embed.set_footer(text="#F1 #Formula1")
        
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.send_message("", embed=embed),
                self.bot.loop
            )
            return future.result(timeout=10)
        except Exception as e:
            logger.error(f"发送比赛结果失败: {e}")
            return False
    
    def send_daily_schedule(self, sessions: List[Dict[str, Any]]) -> bool:
        """发送每日赛程汇总"""
        if not sessions:
            return False
        
        from pytz import timezone
        local_tz = timezone('Asia/Shanghai')
        
        race_name = sessions[0]['race_name']
        circuit = sessions[0]['circuit']
        
        embed = discord.Embed(
            title=f"📅 今日F1赛程 - {race_name}",
            description=f"📍 {circuit}",
            color=discord.Color.blue()
        )
        
        icons = {
            "fp1": "🏎️", "fp2": "🏎️", "fp3": "🏎️",
            "qualifying": "⏱️", "sprint": "⚡", "race": "🏁"
        }
        
        schedule_text = []
        for session in sessions:
            utc_time = session['datetime']
            local_time = utc_time.astimezone(local_tz)
            time_str = local_time.strftime("%H:%M")
            icon = icons.get(session['type'], "🏎️")
            schedule_text.append(f"{icon} **{session['name']}**: {time_str}")
        
        embed.add_field(name="📋 赛程安排", value="\n".join(schedule_text), inline=False)
        embed.set_footer(text="记得准时观看！🏎️💨")
        
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.send_message("", embed=embed),
                self.bot.loop
            )
            return future.result(timeout=10)
        except Exception as e:
            logger.error(f"发送每日赛程失败: {e}")
            return False
    
    def send_startup_message(self) -> bool:
        """发送启动通知"""
        embed = discord.Embed(
            title="✅ F1赛程提醒机器人已启动",
            description="将为您自动推送F1相关通知",
            color=discord.Color.green()
        )
        
        embed.add_field(
            name="📋 功能列表",
            value="• 🏎️ 练习赛提醒\n• ⏱️ 排位赛提醒\n• 🏁 正赛提醒\n• 📊 赛后结果",
            inline=False
        )
        
        embed.set_footer(text="Formula 1 🏎️💨")
        
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.send_message("", embed=embed),
                self.bot.loop
            )
            return future.result(timeout=10)
        except Exception as e:
            logger.error(f"发送启动消息失败: {e}")
            return False
    
    def send_test_message(self) -> bool:
        """发送测试消息"""
        embed = discord.Embed(
            title="🧪 测试消息",
            description="如果您收到这条消息，说明Discord机器人配置成功！",
            color=discord.Color.purple()
        )
        
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.send_message("", embed=embed),
                self.bot.loop
            )
            return future.result(timeout=10)
        except Exception as e:
            logger.error(f"发送测试消息失败: {e}")
            return False
    
    async def start_bot(self):
        """启动Discord Bot"""
        await self.bot.start(self.bot_token)
    
    def run(self):
        """运行Bot（阻塞调用）"""
        self.bot.run(self.bot_token)


# ==================== 配置 ====================
# Discord Bot配置（从环境变量或 .env 文件读取）
# 获取Bot Token方法：
# 1. 访问 https://discord.com/developers/applications
# 2. 找到你的应用
# 3. 进入 Bot 页面
# 4. 点击 "Reset Token" 获取 Token（妥善保存，只显示一次）
# 5. OAuth2 -> URL Generator -> 选择bot权限 -> 复制链接邀请Bot到服务器
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")

# Discord频道ID（数字格式）
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "0")) if os.getenv("DISCORD_CHANNEL_ID") else 0

# 代理配置（解决Discord连接问题）
# 格式: "http://127.0.0.1:7890" 或 "socks5://127.0.0.1:1080"
# 如果为空字符串则不使用代理
PROXY_URL = os.getenv("PROXY_URL", "")

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
import datetime
TIMEZONE = "Asia/Shanghai"
SEASON = datetime.datetime.now().year  # 自动获取当前年份，无需手动修改

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
        self.discord_bot = None
        self.scheduler = None
        self.running = False
        
    def initialize(self) -> bool:
        """初始化所有组件"""
        self.logger.info("=" * 60)
        self.logger.info("F1赛程提醒机器人[Discord版]正在启动...")
        self.logger.info("=" * 60)
        
        # 检查配置
        if "YOUR_BOT_TOKEN_HERE" in DISCORD_BOT_TOKEN:
            self.logger.error("错误：请先配置Discord Bot Token！")
            self.logger.error("编辑 discord_version/main.py 文件，修改 DISCORD_BOT_TOKEN")
            return False
        
        if DISCORD_CHANNEL_ID == 1234567890123456789:
            self.logger.error("错误：请先配置Discord频道ID！")
            self.logger.error("编辑 discord_version/main.py 文件，修改 DISCORD_CHANNEL_ID")
            return False
        
        # 初始化F1 API
        try:
            self.f1_api = F1API(SEASON)
            self.logger.info(f"✓ F1 API初始化成功 (赛季: {SEASON})")
        except Exception as e:
            self.logger.error(f"✗ F1 API初始化失败: {e}")
            return False
        
        # 初始化Discord机器人
        try:
            self.discord_bot = DiscordBot(DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID, PROXY_URL if PROXY_URL else None)
            self.logger.info("✓ Discord机器人初始化成功")
        except Exception as e:
            self.logger.error(f"✗ Discord机器人初始化失败: {e}")
            return False
        
        # 初始化调度器
        try:
            config = {
                'TIMEZONE': TIMEZONE,
                'REMINDER_TIMES': REMINDER_TIMES,
                'POST_RACE_DELAY': POST_RACE_DELAY
            }
            self.scheduler = ReminderScheduler(self.f1_api, self.discord_bot, config)
            self.logger.info("✓ 调度器初始化成功")
        except Exception as e:
            self.logger.error(f"✗ 调度器初始化失败: {e}")
            return False
        
        return True
    
    async def start(self):
        """启动应用"""
        if not self.initialize():
            self.logger.error("应用初始化失败，退出")
            sys.exit(1)
        
        # 在单独的线程中启动Discord Bot
        import threading
        
        def run_bot():
            """在单独线程中运行Discord Bot"""
            try:
                self.discord_bot.run()
            except Exception as e:
                self.logger.error(f"Discord Bot运行失败: {e}")
        
        bot_thread = threading.Thread(target=run_bot, daemon=True)
        bot_thread.start()
        
        # 等待Discord Bot连接就绪
        self.logger.info("等待Discord Bot连接...")
        max_wait = 60  # 增加等待时间到60秒
        waited = 0
        while not self.discord_bot.bot.is_ready() and waited < max_wait:
            await asyncio.sleep(1)
            waited += 1
            if waited % 10 == 0:
                self.logger.info(f"  等待中... {waited}秒")
        
        if not self.discord_bot.bot.is_ready():
            self.logger.error("Discord Bot连接超时")
            sys.exit(1)
        
        self.logger.info("✓ Discord Bot已就绪")
        
        # 发送启动消息
        if SEND_STARTUP_MESSAGE:
            try:
                # 等待一小段时间确保频道已加载
                await asyncio.sleep(2)
                self.discord_bot.send_startup_message()
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
        await self._main_loop()
    
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
    
    async def _main_loop(self):
        """主循环"""
        self.logger.info("\n💡 提示：")
        self.logger.info("  - 按 Ctrl+C 停止程序")
        self.logger.info("  - 查看日志文件了解详细运行情况")
        self.logger.info("\n🤖 机器人正在运行中...\n")
        
        try:
            while self.running:
                await asyncio.sleep(1)
        except KeyboardInterrupt:
            self.logger.info("\n收到停止信号，正在关闭...")
        finally:
            await self.shutdown()
    
    async def shutdown(self):
        """关闭应用"""
        self.running = False
        self.logger.info("正在关闭应用...")
        
        if self.scheduler:
            try:
                self.scheduler.stop()
                self.logger.info("✓ 调度器已停止")
            except Exception as e:
                self.logger.error(f"✗ 停止调度器失败: {e}")
        
        if self.discord_bot and self.discord_bot.bot:
            try:
                await self.discord_bot.bot.close()
                self.logger.info("✓ Discord Bot已关闭")
            except Exception as e:
                self.logger.error(f"✗ 关闭Discord Bot失败: {e}")
        
        self.logger.info("=" * 60)
        self.logger.info("F1赛程提醒机器人[Discord版]已关闭")
        self.logger.info("=" * 60)


def signal_handler(signum, frame):
    """处理系统信号"""
    logger = logging.getLogger(__name__)
    logger.info(f"收到信号 {signum}，准备退出...")
    sys.exit(0)


async def main():
    """程序入口函数"""
    setup_logging()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    app = F1ReminderApp()
    await app.start()


if __name__ == "__main__":
    asyncio.run(main())
