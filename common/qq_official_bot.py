"""
F1赛程提醒机器人 - QQ官方Bot API封装
支持：频道消息发送、日程管理
基于 botpy SDK 实现
"""

import logging
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List
import botpy
from botpy import BotAPI
from botpy.types.message import Message
import asyncio
from functools import wraps

logger = logging.getLogger(__name__)


def async_to_sync(func):
    """将异步函数转换为同步的装饰器"""
    @wraps(func)
    def wrapper(*args, **kwargs):
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        return loop.run_until_complete(func(*args, **kwargs))
    return wrapper


class QQOfficialBot:
    """
    QQ官方机器人封装类
    支持：频道消息、日程管理
    """
    
    def __init__(self, appid: str, secret: str, 
                 announcement_channel_id: str, live_data_channel_id: str,
                 schedule_channel_id: str = None):
        """
        初始化QQ官方机器人
        
        Args:
            appid: QQ开放平台AppID
            secret: QQ开放平台AppSecret
            announcement_channel_id: 公告/赛程子频道ID
            live_data_channel_id: 实时数据子频道ID
            schedule_channel_id: 日程子频道ID（如果不指定则使用announcement_channel_id）
        """
        self.appid = appid
        self.secret = secret
        self.announcement_channel_id = announcement_channel_id
        self.live_data_channel_id = live_data_channel_id
        self.schedule_channel_id = schedule_channel_id or announcement_channel_id
        
        # 初始化API客户端（同步调用）
        self.api = None
        self._init_api()
        
        # 日程创建记录（用于限制检查）
        self._schedule_creation_log = []
        
        logger.info(f"✓ QQ官方机器人初始化成功")
        logger.info(f"  - 公告频道: {self.announcement_channel_id}")
        logger.info(f"  - 数据频道: {self.live_data_channel_id}")
        logger.info(f"  - 日程频道: {self.schedule_channel_id}")
    
    def _init_api(self):
        """初始化API客户端"""
        try:
            # 使用 botpy 的 API 客户端
            # 注意：我们不需要运行完整的Bot客户端，只需要API功能
            intents = botpy.Intents()
            self.client = botpy.Client(intents=intents)
            self.api = self.client.api
            logger.info("✓ Bot API 客户端初始化成功")
        except Exception as e:
            logger.error(f"✗ Bot API 客户端初始化失败: {e}")
            raise
    
    @async_to_sync
    async def _ensure_api_ready(self):
        """确保API已准备好（异步初始化）"""
        if not self.api:
            # 重新初始化
            intents = botpy.Intents()
            self.client = botpy.Client(intents=intents)
            self.api = self.client.api
    
    @async_to_sync
    async def send_channel_message(self, channel_id: str, content: str) -> bool:
        """
        发送消息到指定子频道
        
        Args:
            channel_id: 子频道ID
            content: 消息内容
            
        Returns:
            发送是否成功
        """
        try:
            await self._ensure_api_ready()
            
            # 使用 botpy API 发送消息
            await self.api.post_message(
                channel_id=channel_id,
                content=content
            )
            
            logger.info(f"✓ 消息发送成功 (频道: {channel_id})")
            return True
            
        except Exception as e:
            logger.error(f"✗ 发送消息失败: {e}")
            return False
    
    @async_to_sync
    async def create_schedule(self, name: str, start_time: datetime, 
                             end_time: datetime, description: str = "",
                             remind_type: str = "5", jump_channel_id: str = None) -> bool:
        """
        创建频道日程
        
        Args:
            name: 日程名称
            start_time: 开始时间（datetime对象）
            end_time: 结束时间（datetime对象）
            description: 日程描述
            remind_type: 提醒类型 (0=不提醒, 1=开始时, 2=5分钟前, 3=15分钟前, 4=30分钟前, 5=60分钟前)
            jump_channel_id: 跳转子频道ID
            
        Returns:
            创建是否成功
        """
        try:
            # 检查频率限制
            if not self._check_schedule_limit():
                logger.warning("⚠️ 今日日程创建次数已达上限（10次）")
                return False
            
            await self._ensure_api_ready()
            
            # 转换时间为毫秒时间戳
            start_timestamp = str(int(start_time.timestamp() * 1000))
            end_timestamp = str(int(end_time.timestamp() * 1000))
            
            # 构建参数
            kwargs = {
                "name": name,
                "start_timestamp": start_timestamp,
                "end_timestamp": end_timestamp,
                "remind_type": remind_type
            }
            
            if description:
                kwargs["description"] = description
            if jump_channel_id:
                kwargs["jump_channel_id"] = jump_channel_id
            
            # 调用API创建日程
            await self.api.create_schedule(
                channel_id=self.schedule_channel_id,
                **kwargs
            )
            
            # 记录创建日志
            self._log_schedule_creation()
            
            logger.info(f"✓ 日程创建成功: {name} ({start_time.strftime('%Y-%m-%d %H:%M')})")
            return True
            
        except Exception as e:
            logger.error(f"✗ 创建日程失败: {e}")
            return False
    
    def _check_schedule_limit(self) -> bool:
        """检查今日日程创建次数是否超过限制（10次/天）"""
        today = datetime.now().date()
        
        # 清理非今天的记录
        self._schedule_creation_log = [
            log for log in self._schedule_creation_log 
            if log.date() == today
        ]
        
        # 检查数量
        return len(self._schedule_creation_log) < 10
    
    def _log_schedule_creation(self):
        """记录日程创建"""
        self._schedule_creation_log.append(datetime.now())
    
    # ==================== 业务方法 ====================
    
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
        
        # @全体成员
        message = f"""{icon} 【F1提醒】{icon}

@全体成员
━━━━━━━━━━━━━━━
🏆 {session['race_name']}
📍 {session['circuit']}

【{session['name']}】即将开始

⏰ {date_str} {weekday} {time_str} (北京时间)

敬请期待精彩比赛！🏎️💨
━━━━━━━━━━━━━━━
#F1 #Formula1"""
        
        # 发送到公告频道
        return self.send_channel_message(self.announcement_channel_id, message)
    
    def send_race_result(self, race_data: Dict[str, Any], results: Dict[str, Any]) -> bool:
        """发送比赛结果"""
        race_name = race_data.get('raceName', 'F1大奖赛')
        
        message = f"""🏁 【{race_name} 比赛结果】

━━━━━━━━━━━━━━━
"""
        
        if "Results" in results:
            message += "🥇 前十名:\n\n"
            for i, result in enumerate(results["Results"][:10], 1):
                driver = result.get("Driver", {})
                family_name = driver.get("familyName", "Unknown")
                team = result.get("Constructor", {}).get("name", "Unknown")
                
                medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
                message += f"{medal} {family_name} ({team})\n"
            
            fastest_lap = results["Results"][0].get("FastestLap", {})
            if fastest_lap and fastest_lap.get("rank") == "1":
                fastest_driver = results["Results"][0].get("Driver", {}).get("familyName", "")
                lap_time = fastest_lap.get("Time", {}).get("time", "")
                message += f"\n⚡ 最快圈速: {fastest_driver} - {lap_time}\n"
        
        message += """
━━━━━━━━━━━━━━━
#F1 #Formula1"""
        
        # 发送到公告频道
        return self.send_channel_message(self.announcement_channel_id, message)
    
    def send_daily_schedule(self, sessions: List[Dict[str, Any]]) -> bool:
        """发送每日赛程汇总"""
        if not sessions:
            return False
        
        from pytz import timezone
        local_tz = timezone('Asia/Shanghai')
        
        race_name = sessions[0]['race_name']
        circuit = sessions[0]['circuit']
        
        message = f"""📅 【今日F1赛程】{race_name}

📍 {circuit}
━━━━━━━━━━━━━━━
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
            message += f"\n{icon} {session['name']}: {time_str}"
        
        message += """

━━━━━━━━━━━━━━━
记得准时观看！🏎️💨"""
        
        # 发送到公告频道
        return self.send_channel_message(self.announcement_channel_id, message)
    
    def send_live_data_message(self, content: str) -> bool:
        """
        发送实时数据消息到专门的子频道
        
        Args:
            content: 消息内容
            
        Returns:
            发送是否成功
        """
        return self.send_channel_message(self.live_data_channel_id, content)
    
    def send_startup_message(self) -> bool:
        """发送启动通知"""
        message = """✅ 【F1赛程提醒机器人已启动】

将为您自动推送：
🏎️ 练习赛提醒
⏱️ 排位赛提醒  
🏁 正赛提醒
📅 每周一自动创建本周比赛日程
📊 赛后结果

━━━━━━━━━━━━━━━
Formula 1 🏎️💨"""
        
        return self.send_channel_message(self.announcement_channel_id, message)
    
    def send_test_message(self) -> bool:
        """发送测试消息"""
        message = f"""🧪 【测试消息】

如果您收到这条消息，说明QQ官方机器人配置成功！

当前时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
公告频道: {self.announcement_channel_id}
数据频道: {self.live_data_channel_id}"""
        
        return self.send_channel_message(self.announcement_channel_id, message)
    
    def send_weekly_schedule_preview(self, sessions: List[Dict[str, Any]]) -> bool:
        """
        发送本周赛程预览（创建日程后发送）
        
        Args:
            sessions: 本周所有比赛环节
            
        Returns:
            发送是否成功
        """
        if not sessions:
            return False
        
        from pytz import timezone
        local_tz = timezone('Asia/Shanghai')
        
        race_name = sessions[0]['race_name']
        circuit = sessions[0]['circuit']
        
        message = f"""📅 【本周F1赛程预告】

🏆 {race_name}
📍 {circuit}

已为您创建以下比赛日程：
"""
        
        icons = {
            "fp1": "🏎️", "fp2": "🏎️", "fp3": "🏎️",
            "qualifying": "⏱️", "sprint": "⚡", "race": "🏁"
        }
        
        for session in sessions:
            utc_time = session['datetime']
            local_time = utc_time.astimezone(local_tz)
            date_str = local_time.strftime("%m月%d日")
            time_str = local_time.strftime("%H:%M")
            weekday = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][local_time.weekday()]
            icon = icons.get(session['type'], "🏎️")
            
            message += f"\n{icon} {session['name']}: {date_str}({weekday}) {time_str}"
        
        message += """

所有比赛已添加到频道日程，记得查看！
🏎️💨"""
        
        return self.send_channel_message(self.announcement_channel_id, message)


# ==================== 日程管理 ====================

class ScheduleManager:
    """
    日程管理器
    负责智能创建比赛日程，处理拉斯维加斯站特殊逻辑
    """
    
    def __init__(self, bot: QQOfficialBot, f1_api):
        """
        初始化日程管理器
        
        Args:
            bot: QQOfficialBot实例
            f1_api: F1API实例
        """
        self.bot = bot
        self.f1_api = f1_api
        self.logger = logging.getLogger(__name__)
    
    def create_weekly_schedules(self) -> bool:
        """
        创建本周比赛日程
        
        正常情况：每周一创建
        特殊情况：如果本周有拉斯维加斯站正赛（次周周一中午12点），则周二创建
        
        Returns:
            是否成功创建
        """
        try:
            # 获取未来7天的所有比赛环节
            upcoming_sessions = self.f1_f1_api.get_upcoming_sessions(hours_ahead=168)
            
            if not upcoming_sessions:
                self.logger.info("本周没有F1比赛")
                return True
            
            # 检查是否是拉斯维加斯特殊周
            is_las_vegas_week = self._is_las_vegas_week(upcoming_sessions)
            
            if is_las_vegas_week:
                self.logger.info("检测到拉斯维加斯站，调整日程创建时间")
            
            # 按比赛分组
            races = self._group_sessions_by_race(upcoming_sessions)
            
            created_count = 0
            for race_name, sessions in races.items():
                for session in sessions:
                    if self._create_session_schedule(session):
                        created_count += 1
            
            self.logger.info(f"✓ 成功创建 {created_count} 个比赛日程")
            
            # 发送赛程预览消息
            if created_count > 0:
                self.bot.send_weekly_schedule_preview(upcoming_sessions)
            
            return True
            
        except Exception as e:
            self.logger.error(f"✗ 创建本周日程失败: {e}")
            return False
    
    def _is_las_vegas_week(self, sessions: List[Dict[str, Any]]) -> bool:
        """
        检查是否是拉斯维加斯特殊周
        拉斯维加斯站正赛通常在次周周一中午12点（北京时间）
        """
        for session in sessions:
            if session['type'] == 'race' and 'vegas' in session['race_name'].lower():
                return True
            if session['type'] == 'race' and '拉斯维加斯' in session['race_name']:
                return True
        return False
    
    def _group_sessions_by_race(self, sessions: List[Dict[str, Any]]) -> Dict[str, List[Dict]]:
        """按比赛分组"""
        races = {}
        for session in sessions:
            race_name = session['race_name']
            if race_name not in races:
                races[race_name] = []
            races[race_name].append(session)
        return races
    
    def _create_session_schedule(self, session: Dict[str, Any]) -> bool:
        """
        为单个比赛环节创建日程
        
        Args:
            session: 比赛环节信息
            
        Returns:
            是否成功创建
        """
        try:
            # 构建日程名称
            schedule_name = f"{session['race_name']} - {session['name']}"
            
            # 开始时间
            start_time = session['datetime']
            
            # 结束时间（根据类型估算）
            duration_hours = {
                "fp1": 1.5, "fp2": 1.5, "fp3": 1.0,
                "qualifying": 1.5, "sprint": 0.5, "race": 2.0
            }
            duration = duration_hours.get(session['type'], 1.5)
            end_time = start_time + timedelta(hours=duration)
            
            # 构建描述
            description = f"""📍 赛道: {session['circuit']}

F1 {session['race_name']} {session['name']}

记得准时观看！🏎️💨"""
            
            # 创建日程（提前60分钟提醒）
            return self.bot.create_schedule(
                name=schedule_name,
                start_time=start_time,
                end_time=end_time,
                description=description,
                remind_type="5",  # 60分钟前提醒
                jump_channel_id=self.bot.announcement_channel_id
            )
            
        except Exception as e:
            self.logger.error(f"✗ 创建日程失败 {session['name']}: {e}")
            return False


# ==================== 兼容性别名 ====================

# 保持与企业微信版本相同的接口名称
class QQBot(QQOfficialBot):
    """QQOfficialBot的别名，用于兼容性"""
    pass
