"""
F1赛程提醒机器人 - 定时任务调度模块
使用 APScheduler 实现定时提醒任务
"""

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, timedelta
from pytz import timezone
import logging
from typing import Dict, Any, List, Callable
import functools

logger = logging.getLogger(__name__)


class ReminderScheduler:
    """F1提醒任务调度器"""
    
    def __init__(self, f1_api, wechat_bot, config: Dict[str, Any]):
        """
        初始化调度器
        
        Args:
            f1_api: F1API实例
            wechat_bot: WeChatBot实例
            config: 配置字典
        """
        self.f1_api = f1_api
        self.wechat_bot = wechat_bot
        self.config = config
        
        # 创建后台调度器
        self.scheduler = BackgroundScheduler()
        
        # 时区
        self.local_tz = timezone(config.get('TIMEZONE', 'Asia/Shanghai'))
        
        # 已调度的任务（避免重复调度）
        self.scheduled_jobs = {}
        
    def start(self):
        """启动调度器"""
        self.scheduler.start()
        logger.info("定时任务调度器已启动")
        
        # 立即更新赛程任务
        self.update_schedule_jobs()
        
    def stop(self):
        """停止调度器"""
        self.scheduler.shutdown()
        logger.info("定时任务调度器已停止")
    
    def _make_job_id(self, session_type: str, round_num: str, suffix: str = "") -> str:
        """
        生成任务ID
        
        Args:
            session_type: 环节类型
            round_num: 轮次
            suffix: 后缀
            
        Returns:
            唯一任务ID
        """
        return f"f1_{session_type}_{round_num}_{suffix}" if suffix else f"f1_{session_type}_{round_num}"
    
    def _send_reminder(self, session: Dict[str, Any], reminder_type: str):
        """
        发送提醒的包装函数
        
        Args:
            session: 比赛环节信息
            reminder_type: 提醒类型（reminder/post_race）
        """
        try:
            if reminder_type == "reminder":
                logger.info(f"发送提醒: {session['race_name']} - {session['name']}")
                self.wechat_bot.send_session_reminder(session)
            elif reminder_type == "post_race":
                logger.info(f"获取比赛结果: {session['race_name']}")
                results = self.f1_api.get_race_results(int(session['round']))
                if results:
                    # 查找race数据
                    schedule = self.f1_api.get_schedule()
                    race_data = None
                    for race in schedule:
                        if race['round'] == session['round']:
                            race_data = race
                            break
                    
                    if race_data:
                        self.wechat_bot.send_race_result(race_data, results)
                else:
                    logger.warning(f"未找到比赛结果: Round {session['round']}")
                    
        except Exception as e:
            logger.error(f"发送提醒失败: {e}")
    
    def schedule_session_reminder(self, session: Dict[str, Any], advance_minutes: int):
        """
        调度单个比赛环节的提醒任务
        
        Args:
            session: 比赛环节信息
            advance_minutes: 提前多少分钟提醒
        """
        job_id = self._make_job_id(session['type'], session['round'], f"reminder_{advance_minutes}")
        
        # 如果任务已存在，跳过
        if job_id in self.scheduled_jobs:
            return
        
        # 计算提醒时间
        reminder_time = session['datetime'] - timedelta(minutes=advance_minutes)
        
        # 如果提醒时间已过，跳过
        now = datetime.now(session['datetime'].tzinfo)
        if reminder_time < now:
            logger.debug(f"提醒时间已过，跳过: {job_id}")
            return
        
        # 创建任务
        job = self.scheduler.add_job(
            func=self._send_reminder,
            trigger=DateTrigger(run_date=reminder_time),
            args=[session, "reminder"],
            id=job_id,
            replace_existing=True
        )
        
        self.scheduled_jobs[job_id] = job
        
        local_time = reminder_time.astimezone(self.local_tz)
        logger.info(f"已调度提醒任务: {job_id} - 将于 {local_time.strftime('%Y-%m-%d %H:%M')} 执行")
    
    def schedule_post_race_result(self, session: Dict[str, Any], delay_minutes: int):
        """
        调度赛后结果推送任务
        
        Args:
            session: 比赛环节信息（必须是正赛）
            delay_minutes: 赛后延迟多少分钟推送
        """
        if session['type'] != 'race':
            return
        
        job_id = self._make_job_id(session['type'], session['round'], f"result_{delay_minutes}")
        
        # 如果任务已存在，跳过
        if job_id in self.scheduled_jobs:
            return
        
        # 计算推送时间（假设比赛持续2小时）
        post_time = session['datetime'] + timedelta(hours=2, minutes=delay_minutes)
        
        # 如果时间已过，跳过
        now = datetime.now(session['datetime'].tzinfo)
        if post_time < now:
            return
        
        # 创建任务
        job = self.scheduler.add_job(
            func=self._send_reminder,
            trigger=DateTrigger(run_date=post_time),
            args=[session, "post_race"],
            id=job_id,
            replace_existing=True
        )
        
        self.scheduled_jobs[job_id] = job
        
        local_time = post_time.astimezone(self.local_tz)
        logger.info(f"已调度结果推送任务: {job_id} - 将于 {local_time.strftime('%Y-%m-%d %H:%M')} 执行")
    
    def update_schedule_jobs(self):
        """
        更新赛程任务（定期调用以获取最新赛程）
        """
        logger.info("正在更新赛程任务...")
        
        # 获取未来7天的所有环节
        upcoming_sessions = self.f1_api.get_upcoming_sessions(hours_ahead=168)
        
        reminder_times = self.config.get('REMINDER_TIMES', {
            'fp1': 30, 'fp2': 30, 'fp3': 30,
            'qualifying': 30, 'sprint': 30, 'race': 60
        })
        
        post_race_delay = self.config.get('POST_RACE_DELAY', 30)
        
        scheduled_count = 0
        
        for session in upcoming_sessions:
            session_type = session['type']
            
            # 调度提醒任务
            if session_type in reminder_times:
                advance = reminder_times[session_type]
                self.schedule_session_reminder(session, advance)
                scheduled_count += 1
            
            # 如果是正赛，调度赛后结果推送
            if session_type == 'race':
                self.schedule_post_race_result(session, post_race_delay)
        
        logger.info(f"赛程任务更新完成，新调度 {scheduled_count} 个任务")
    
    def add_daily_update_job(self):
        """
        添加每日赛程更新任务
        每天早上8点更新一次赛程
        """
        job = self.scheduler.add_job(
            func=self.update_schedule_jobs,
            trigger=CronTrigger(hour=8, minute=0, timezone=self.local_tz),
            id='daily_schedule_update',
            replace_existing=True
        )
        logger.info("已添加每日赛程更新任务（每天8:00）")
        return job
    
    def list_jobs(self) -> List[Dict[str, Any]]:
        """
        获取所有已调度的任务列表
        
        Returns:
            任务列表
        """
        jobs = []
        for job in self.scheduler.get_jobs():
            jobs.append({
                'id': job.id,
                'name': job.name,
                'next_run_time': job.next_run_time.isoformat() if job.next_run_time else None,
                'trigger': str(job.trigger)
            })
        return jobs
    
    def remove_job(self, job_id: str) -> bool:
        """
        移除指定任务
        
        Args:
            job_id: 任务ID
            
        Returns:
            是否成功移除
        """
        try:
            self.scheduler.remove_job(job_id)
            if job_id in self.scheduled_jobs:
                del self.scheduled_jobs[job_id]
            logger.info(f"已移除任务: {job_id}")
            return True
        except Exception as e:
            logger.error(f"移除任务失败 {job_id}: {e}")
            return False
    
    def clear_all_jobs(self):
        """清除所有F1相关的提醒任务"""
        jobs_to_remove = [job_id for job_id in self.scheduled_jobs.keys()]
        for job_id in jobs_to_remove:
            self.remove_job(job_id)
        
        self.scheduled_jobs.clear()
        logger.info("已清除所有F1提醒任务")


# 使用示例
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    
    # 模拟配置
    config = {
        'TIMEZONE': 'Asia/Shanghai',
        'REMINDER_TIMES': {
            'fp1': 30, 'fp2': 30, 'fp3': 30,
            'qualifying': 30, 'sprint': 30, 'race': 60
        },
        'POST_RACE_DELAY': 30
    }
    
    # 注意：实际使用时需要传入F1API和WeChatBot实例
    from f1_api import F1API
    from wechat_bot import WeChatBot
    
    f1_api = F1API(2024)
    wechat_bot = WeChatBot("测试群")
    
    # 创建调度器
    scheduler = ReminderScheduler(f1_api, wechat_bot, config)
    
    # 启动调度器（不实际登录微信，仅测试任务调度）
    scheduler.start()
    
    # 查看调度的任务
    print("\n已调度的任务:")
    for job in scheduler.list_jobs():
        print(f"  {job['id']}: {job['next_run_time']}")
    
    # 运行一段时间后停止
    import time
    try:
        print("\n调度器运行中...按Ctrl+C停止")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        scheduler.stop()
        print("调度器已停止")