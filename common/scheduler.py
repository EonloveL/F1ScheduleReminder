"""
F1赛程提醒机器人 - 共用核心模块
定时任务调度器 - 使用 APScheduler 实现定时提醒任务
"""

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, timedelta
from pytz import timezone
import logging
from typing import Dict, Any, List, Callable
import functools

from .user_prefs import UserPrefsStore

logger = logging.getLogger(__name__)


class ReminderScheduler:
    """F1提醒任务调度器"""
    
    def __init__(self, f1_api, notify_bot, config: Dict[str, Any]):
        """
        初始化调度器
        
        Args:
            f1_api: F1API实例
            notify_bot: 消息机器人实例（WeComBot/PushPlusBot/WeChatBot）
            config: 配置字典
        """
        self.f1_api = f1_api
        self.notify_bot = notify_bot
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
        
        # 注册年度数据清理任务
        self.add_yearly_cleanup_job()
        
    def add_yearly_cleanup_job(self):
        """
        每年12月31日 23:50 清理年度统计数据：
        - data/exports/ 图表缓存（全部删除）
        - data/ratings.json 评分数据（归档到 data/backup/ 后重置）
        - data/user_prefs.json 偏好绑定（归档后重置，即"一年一绑"）
        保留：circuits_data.json（赛道数据）、赛程缓存
        """
        job = self.scheduler.add_job(
            func=self._yearly_data_cleanup,
            trigger=CronTrigger(month=12, day=31, hour=23, minute=50, timezone=self.local_tz),
            id='yearly_data_cleanup',
            replace_existing=True
        )
        logger.info("已添加年度数据清理任务（每年12月31日 23:50）")
        return job
    
    def _yearly_data_cleanup(self):
        """年度数据清理：统计/图表/偏好绑定归档后清空，保留必要数据"""
        import os
        import shutil
        
        data_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data"
        )
        exports_dir = os.path.join(data_dir, "exports")
        ratings_file = os.path.join(data_dir, "ratings.json")
        prefs_file = os.path.join(data_dir, "user_prefs.json")
        backup_dir = os.path.join(data_dir, "backup")
        year = datetime.now(self.local_tz).year
        
        logger.info(f"🧹 开始 {year} 年度数据清理...")
        
        # 1. 归档并重置评分数据
        try:
            if os.path.exists(ratings_file):
                os.makedirs(backup_dir, exist_ok=True)
                archive = os.path.join(backup_dir, f"ratings_{year}.json")
                shutil.copy2(ratings_file, archive)
                with open(ratings_file, "w", encoding="utf-8") as f:
                    f.write('{"races": {}, "season_dotd": {}}')
                logger.info(f"✓ 评分数据已归档: {archive}")
        except Exception as e:
            logger.error(f"评分数据归档失败: {e}")
        
        # 2. 归档并重置用户偏好绑定（一年一绑：/setdriver /setteam 绑定下一年需重新设置）
        try:
            if os.path.exists(prefs_file):
                os.makedirs(backup_dir, exist_ok=True)
                archive = os.path.join(backup_dir, f"user_prefs_{year}.json")
                shutil.copy2(prefs_file, archive)
                with open(prefs_file, "w", encoding="utf-8") as f:
                    f.write('{}')
                logger.info(f"✓ 用户偏好绑定已归档: {archive}（下一年需重新设置）")
        except Exception as e:
            logger.error(f"用户偏好归档失败: {e}")
        
        # 3. 归档并重置成绩推送记录
        try:
            pushed_file = os.path.join(data_dir, "pushed_results.json")
            if os.path.exists(pushed_file):
                os.makedirs(backup_dir, exist_ok=True)
                archive = os.path.join(backup_dir, f"pushed_results_{year}.json")
                shutil.copy2(pushed_file, archive)
                with open(pushed_file, "w", encoding="utf-8") as f:
                    f.write('{}')
                logger.info(f"✓ 成绩推送记录已归档: {archive}")
        except Exception as e:
            logger.error(f"成绩推送记录归档失败: {e}")

        # 4. 清理图表缓存
        try:
            if os.path.isdir(exports_dir):
                count = 0
                for fname in os.listdir(exports_dir):
                    fpath = os.path.join(exports_dir, fname)
                    if os.path.isfile(fpath):
                        os.remove(fpath)
                        count += 1
                logger.info(f"✓ 图表缓存已清理 ({count} 个文件)")
        except Exception as e:
            logger.error(f"图表缓存清理失败: {e}")

        # 5. 清理遥测数据缓存（每场约10-20MB，赛季累计可观）
        try:
            telemetry_dir = os.path.join(data_dir, "telemetry_cache")
            if os.path.isdir(telemetry_dir):
                count = 0
                for fname in os.listdir(telemetry_dir):
                    fpath = os.path.join(telemetry_dir, fname)
                    if os.path.isfile(fpath):
                        os.remove(fpath)
                        count += 1
                logger.info(f"✓ 遥测缓存已清理 ({count} 个文件)")
        except Exception as e:
            logger.error(f"遥测缓存清理失败: {e}")
        
        logger.info(f"✓ {year} 年度数据清理完成（已保留赛道数据/赛程缓存）")
        
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
                # 附带OpenF1天气信息（有数据才附加）
                weather_text = None
                try:
                    if hasattr(self.f1_api, 'get_session_weather_text'):
                        weather_text = self.f1_api.get_session_weather_text(session)
                except Exception as e:
                    logger.warning(f"获取天气信息失败: {e}")
                try:
                    self.notify_bot.send_session_reminder(session, weather_text=weather_text)
                except TypeError:
                    self.notify_bot.send_session_reminder(session)
            elif reminder_type == "post_race":
                if self._already_pushed(session):
                    logger.info(f"成绩已推送过，跳过: {session['race_name']}")
                    return
                logger.info(f"获取比赛结果: {session['race_name']}")
                results = self.f1_api.get_race_results(int(session['round']))
                if not results:
                    logger.warning(f"成绩尚未公布: Round {session['round']}，转入轮询补推")
                    self._schedule_poll(session, 0, kind="race_result")
                    return
                if not self._do_post_race_push(session, results):
                    self._schedule_poll(session, 0, kind="race_result")
                    
        except Exception as e:
            logger.error(f"发送提醒失败: {e}")

    # ==================== 成绩轮询补推 ====================
    # API端（f1api.dev官方口径赛后24-48h，Jolpica通常1h内）无主动通知机制，
    # 采用轮询逼近即时：数据一公布即推，最长兜底24小时

    # 成绩轮询节奏（分钟）：前3小时每15分钟，之后每小时，共24小时窗口
    RESULT_POLL_DELAYS = [15] * 12 + [60] * 21
    # 积分榜补推节奏（分钟）：数据源滞后时持续补推直到新鲜（2026-09-28 巴库实证
    # f1api.dev 积分榜赛后24-48h滞后，旧 4×30min=2h 窗口不够，扩到 ~22h 覆盖）
    STANDINGS_POLL_DELAYS = [30] * 4 + [60] * 20

    def _poll_session_key(self, session: Dict[str, Any]):
        season = self.config.get('SEASON') or session['datetime'].year
        return season, session['round'], session['type']

    def _already_pushed(self, session: Dict[str, Any]) -> bool:
        store = self.config.get('pushed_store')
        if not store:
            return False
        season, round_num, stype = self._poll_session_key(session)
        return store.is_pushed(season, round_num, stype)

    def _mark_pushed(self, session: Dict[str, Any]):
        store = self.config.get('pushed_store')
        if not store:
            return
        season, round_num, stype = self._poll_session_key(session)
        store.mark_pushed(season, round_num, stype)

    def _schedule_poll(self, session: Dict[str, Any], attempt: int, kind: str):
        """调度成绩轮询任务：数据未公布时按节奏重试，直到24小时窗口耗尽"""
        if attempt >= len(self.RESULT_POLL_DELAYS):
            logger.warning(f"成绩轮询超24小时仍未公布，放弃: {session['race_name']} - {session['name']}")
            self._notify_result_unavailable(session)
            return
        delay = self.RESULT_POLL_DELAYS[attempt]
        job_id = self._make_job_id(session['type'], session['round'], f"{kind}_poll_{attempt}")
        if self.scheduler.get_job(job_id):
            return
        run_time = datetime.now(self.local_tz) + timedelta(minutes=delay)
        func = self._poll_race_result if kind == "race_result" else self._poll_session_result
        self.scheduler.add_job(
            func=func,
            trigger=DateTrigger(run_date=run_time),
            args=[session, attempt],
            id=job_id,
            replace_existing=True
        )
        logger.info(f"已调度成绩轮询: {job_id} - {delay}分钟后第{attempt + 1}次尝试")

    def _poll_race_result(self, session: Dict[str, Any], attempt: int):
        """轮询正赛成绩：拿到数据后执行完整赛后推送链"""
        try:
            if self._already_pushed(session):
                logger.info(f"成绩已推送过，停止轮询: {session['race_name']}")
                return
            results = self.f1_api.get_race_results(int(session['round']))
            if not results:
                logger.info(f"成绩尚未公布，继续轮询: {session['race_name']} (第{attempt + 1}次)")
                self._schedule_poll(session, attempt + 1, kind="race_result")
                return
            logger.info(f"✓ 轮询获取到比赛结果: {session['race_name']} (第{attempt + 1}次)")
            if not self._do_post_race_push(session, results):
                self._schedule_poll(session, attempt + 1, kind="race_result")
        except Exception as e:
            logger.error(f"轮询比赛结果异常: {e}")
            self._schedule_poll(session, attempt + 1, kind="race_result")

    def _poll_session_result(self, session: Dict[str, Any], attempt: int):
        """轮询环节成绩（排位/冲刺排位/冲刺赛）"""
        try:
            if self._already_pushed(session):
                return
            result = self.f1_api.get_session_results(int(session['round']), session['type'])
            if not result or not result.get('entries'):
                logger.info(f"环节成绩尚未公布，继续轮询: {session['race_name']} - {session['name']} (第{attempt + 1}次)")
                self._schedule_poll(session, attempt + 1, kind="session_result")
                return
            logger.info(f"✓ 轮询获取到环节成绩: {session['race_name']} - {session['name']} (第{attempt + 1}次)")
            if self._do_session_result_push(session, result):
                self._mark_pushed(session)
            else:
                self._schedule_poll(session, attempt + 1, kind="session_result")
        except Exception as e:
            logger.error(f"轮询环节成绩异常: {e}")
            self._schedule_poll(session, attempt + 1, kind="session_result")

    def _notify_result_unavailable(self, session: Dict[str, Any]):
        """24小时轮询仍无成绩时群里告知，避免无声"""
        try:
            if hasattr(self.notify_bot, 'send_group_message'):
                text = f"⏳ {session['race_name']} {session['name']} 成绩超过24小时仍未公布，数据更新后可发送 /last 查询"
                self.notify_bot.send_group_message(text)
        except Exception as e:
            logger.warning(f"发送成绩未公布提示失败: {e}")

    def _do_post_race_push(self, session: Dict[str, Any], results: Dict[str, Any]) -> bool:
        """正赛赛后推送主流程：成绩/圈速纪录/评分开启/积分榜/个性化DM"""
        # 查找race数据
        schedule = self.f1_api.get_schedule()
        race_data = None
        for race in schedule:
            if race['round'] == session['round']:
                race_data = race
                break

        if not race_data:
            logger.error(f"赛程中未找到 Round {session['round']}，无法推送赛后数据")
            self._mark_pushed(session)  # 永久失败，不再轮询
            return True

        # 检测是否打破赛道圈速纪录（pushplus版功能）
        lap_record_info = None
        tracker = self.config.get('lap_record_tracker')
        if tracker:
            try:
                from .circuits_manager import extract_fastest_lap_from_results
                fastest_lap = extract_fastest_lap_from_results(results)
                if fastest_lap:
                    circuit_id = race_data['Circuit'].get('circuitId', '')
                    if circuit_id:
                        is_new_record, record_msg = tracker.check_and_update_record(
                            circuit_id,
                            fastest_lap['time'],
                            fastest_lap['driver_name'],
                            fastest_lap['driver_id'],
                            fastest_lap['year']
                        )
                        if is_new_record:
                            lap_record_info = record_msg
                            logger.info(f"🎉 新圈速纪录: {record_msg}")
                        else:
                            logger.info(f"📊 圈速对比: {record_msg}")
            except Exception as e:
                logger.warning(f"圈速纪录检查失败: {e}")

        try:
            try:
                ok = self.notify_bot.send_race_result(race_data, results, lap_record_info=lap_record_info)
            except TypeError:
                ok = self.notify_bot.send_race_result(race_data, results)
        except Exception as e:
            logger.error(f"推送比赛结果异常: {e}")
            return False

        if ok is False:
            logger.warning("比赛结果推送未成功，稍后轮询重试")
            return False

        self._mark_pushed(session)

        # 正赛结束后推送最新积分榜+个性化DM（带新鲜度门槛：积分总和 ≥ 已知总和+本场颁发分）
        awarded = 0.0
        for e in (results.get("Results") or results.get("results") or []):
            try:
                awarded += float(e.get("points", 0) or 0)
            except (ValueError, TypeError):
                continue
        last_sum = self._last_standings_sum()
        min_sum = (last_sum + awarded) if (last_sum and awarded) else None
        self._push_standings_with_retry(session, race_data, min_points_sum=min_sum)
        return True

    # ==================== 积分榜新鲜度状态 ====================
    # 积分总和赛季内单调不减：记录上次成功推送的总和，赛后要求 新总和 ≥ 旧总和+本场颁发分，
    # 不满足说明双源都滞后（f1api.dev 官方口径赛后24-48h），继续补推而非推旧数据

    def _standings_state_path(self) -> str:
        import os as _os
        return _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                             "data", "standings_state.json")

    def _last_standings_sum(self) -> float:
        try:
            import json as _json
            with open(self._standings_state_path(), encoding="utf-8") as f:
                return float(_json.load(f).get("last_points_sum") or 0.0)
        except Exception:
            return 0.0

    def _record_standings_sum(self, points_sum: float, session: Dict[str, Any]):
        try:
            import json as _json
            with open(self._standings_state_path(), "w", encoding="utf-8") as f:
                _json.dump({"last_points_sum": points_sum,
                            "round": session.get("round"),
                            "updated": datetime.now(self.local_tz).isoformat()}, f,
                           ensure_ascii=False)
        except Exception as e:
            logger.warning(f"积分榜状态写入失败: {e}")

    def _push_standings_with_retry(self, session: Dict[str, Any], race_data: Dict[str, Any],
                                   attempt: int = 0, min_points_sum: float = None):
        """推送最新积分榜+个性化DM；数据未就绪或疑似滞后（积分总和低于 已知+本场颁发）
        时按 STANDINGS_POLL_DELAYS 轮询补推，绝不推滞后数据"""
        try:
            logger.info(f"获取最新积分榜: {session['race_name']}")
            if hasattr(self.f1_api, 'get_standings_freshest'):
                standings, source, psum = self.f1_api.get_standings_freshest()
            else:
                standings = self.f1_api.get_current_standings(force_refresh=True)
                source, psum = "force_refresh", self.f1_api._standings_points_sum(standings or {})
            if standings and (standings.get('drivers') or standings.get('constructors')):
                if min_points_sum is not None and psum and psum < min_points_sum - 0.5:
                    logger.warning(f"积分榜疑似滞后（源 {source}，总和 {psum} < 预期≥{min_points_sum}），转入补推轮询")
                else:
                    logger.info(f"积分榜已就绪（源 {source}，总和 {psum}）")
                    self._push_standings_and_dm(standings, race_data)
                    if psum:
                        self._record_standings_sum(psum, session)
                    return
            else:
                logger.warning(f"积分榜数据未就绪: Round {session['round']}")
        except Exception as e:
            logger.error(f"推送积分榜更新失败: {e}")

        if attempt < len(self.STANDINGS_POLL_DELAYS):
            delay = self.STANDINGS_POLL_DELAYS[attempt]
            job_id = self._make_job_id(session['type'], session['round'], f"standings_poll_{attempt}")
            run_time = datetime.now(self.local_tz) + timedelta(minutes=delay)
            self.scheduler.add_job(
                func=self._push_standings_with_retry,
                trigger=DateTrigger(run_date=run_time),
                args=[session, race_data, attempt + 1],
                kwargs={"min_points_sum": min_points_sum},
                id=job_id,
                replace_existing=True
            )
            logger.info(f"已调度积分榜补推: {job_id} - {delay}分钟后")
        else:
            logger.warning(f"积分榜轮询超时放弃: {session['race_name']}")

    def _push_standings_and_dm(self, standings: Dict[str, Any], race_data: Dict[str, Any]):
        """群积分榜推送 + 个性化私聊播报"""
        race_name = race_data.get('raceName') if race_data else None
        self.notify_bot.send_standings_update(standings, race_name)

        prefs_store = self.config.get('prefs_store')
        if not prefs_store:
            logger.info("未配置偏好存储，跳过个性化私聊推送")
            return
        if not hasattr(self.notify_bot, 'send_dm_markdown'):
            return
        dm_users = prefs_store.get_all_user_prefs()
        if not dm_users:
            logger.info("无用户设置偏好，跳过个性化私聊推送")
            return
        logger.info(f"发送私聊个性化积分播报（{len(dm_users)} 位用户）")
        self._send_dm_personalized_standings(standings, dm_users)

    def _enqueue_missed_result_pushes(self):
        """自愈：扫描过去48小时已结束但未推送成绩的环节，补建轮询任务（容器重启容错）"""
        store = self.config.get('pushed_store')
        if not store:
            return
        try:
            from datetime import timezone as _tz
            now = datetime.now(_tz.utc)
            window_start = now - timedelta(hours=48)
            season = self.config.get('SEASON') or now.year
            existing_ids = {j.id for j in self.scheduler.get_jobs()}
            count = 0

            for race in self.f1_api.get_schedule():
                for s in self.f1_api.get_all_sessions(race):
                    st = s['type']
                    if st != 'race' and st not in self.RESULT_PUSH_TYPES:
                        continue
                    if store.is_pushed(season, s['round'], st):
                        continue
                    end_time = s['datetime'] + timedelta(minutes=self.SESSION_DURATION.get(st, 120))
                    if end_time > now or end_time < window_start:
                        continue
                    prefix = self._make_job_id(st, s['round'])
                    # 带分隔符边界匹配，防 R1 与 R11/R12 前缀互撞
                    if any(jid.startswith(prefix + "_") for jid in existing_ids):
                        continue
                    kind = 'race_result' if st == 'race' else 'session_result'
                    job_id = self._make_job_id(st, s['round'], f"{kind}_poll_0")
                    func = self._poll_race_result if st == 'race' else self._poll_session_result
                    self.scheduler.add_job(
                        func=func,
                        trigger=DateTrigger(run_date=now + timedelta(minutes=2)),
                        args=[s, 0],
                        id=job_id,
                        replace_existing=True
                    )
                    count += 1
                    logger.info(f"补建成绩轮询任务: {s['race_name']} - {s['name']}")
            if count:
                logger.info(f"✓ 自愈扫描完成，补建 {count} 个成绩轮询任务")
        except Exception as e:
            logger.warning(f"自愈扫描失败: {e}")

    def _heal_missed_rating_opens(self):
        """自愈：扫描仍在投票窗口内但从未开启/丢失截止任务的评分场次（容器重启容错）

        背景：BackgroundScheduler 任务不持久化，若容器在正赛结束后重启，
        schedule_rating_open 的 open_time < now 守卫会永久跳过，导致 /rate 无可用场次。
        """
        ratings_store = self.config.get('ratings_store')
        if not ratings_store:
            return
        try:
            from datetime import timezone as _tz
            now = datetime.now(_tz.utc)
            season = self.config.get('SEASON') or now.year
            race_dur = timedelta(minutes=self.SESSION_DURATION.get('race', 120))
            count = 0

            for race in self.f1_api.get_schedule():
                for s in self.f1_api.get_all_sessions(race):
                    if s['type'] != 'race':
                        continue
                    race_end = s['datetime'] + race_dur
                    close_time = race_end + timedelta(hours=self.RATING_CLOSE_HOURS)
                    if race_end > now or now >= close_time:
                        continue  # 未结束，或投票窗口已过
                    key = ratings_store.race_key(season, s['round'])
                    existing = ratings_store.get_race(key)
                    if not existing:
                        # 从未开启：立即补开，截止时间仍按正赛结束+24h（不因补开延后）
                        logger.warning(f"评分开启任务缺失，立即补开: {key} {s['race_name']}")
                        self._open_driver_rating(s, race, close_time=close_time)
                        count += 1
                    elif not existing.get("closed"):
                        # 已开启未截止：检查截止任务是否因重启丢失
                        job_id = f"rating_close_{key}"
                        if not any(j.id == job_id for j in self.scheduler.get_jobs()):
                            self.scheduler.add_job(
                                func=self._close_driver_rating,
                                trigger=DateTrigger(run_date=close_time),
                                args=[key],
                                id=job_id,
                                replace_existing=True
                            )
                            count += 1
                            logger.warning(f"评分截止任务缺失，已补建: {key}")
            if count:
                logger.info(f"✓ 评分自愈完成，补建 {count} 项")
        except Exception as e:
            logger.warning(f"评分自愈扫描失败: {e}")

    def _job_guard(self, job_id: str, run_date) -> bool:
        """任务去重守卫：已存在且时间未变→True跳过；
        已触发（scheduler中不存在）或时间已变更→移除旧任务返回False允许重建。
        修复：原"if job_id in scheduled_jobs: return"守卫使 replace_existing 不可达，
        赛历时间变更后仍按旧时间触发"""
        if job_id not in self.scheduled_jobs:
            return False
        existing = self.scheduler.get_job(job_id)
        if existing is None:
            # 任务已触发/被移除，允许重建
            self.scheduled_jobs.pop(job_id, None)
            return False
        nrt = getattr(existing, "next_run_time", None)
        if nrt is not None and run_date is not None:
            try:
                if abs((nrt - run_date).total_seconds()) < 60:
                    return True
            except TypeError:
                return True  # 时区比较异常时保守跳过
        self.remove_job(job_id)
        logger.info(f"任务时间已变更，重新调度: {job_id}")
        return False

    def schedule_session_reminder(self, session: Dict[str, Any], advance_minutes: int):
        """
        调度单个比赛环节的提醒任务
        
        Args:
            session: 比赛环节信息
            advance_minutes: 提前多少分钟提醒
        """
        job_id = self._make_job_id(session['type'], session['round'], f"reminder_{advance_minutes}")

        # 计算提醒时间
        reminder_time = session['datetime'] - timedelta(minutes=advance_minutes)

        # 任务已存在且时间未变则跳过；时间变更则重排
        if self._job_guard(job_id, reminder_time):
            return
        
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
    
    # 环节预估时长（分钟），用于计算结果推送时间
    SESSION_DURATION = {
        'fp1': 60, 'fp2': 60, 'fp3': 60,
        'qualifying': 60, 'sprint_qualifying': 45,
        'sprint': 45, 'race': 120,
    }

    # 需要自动推送成绩的环节类型（练习赛不推，可用/last查询）
    RESULT_PUSH_TYPES = {'sprint_qualifying', 'sprint', 'qualifying'}

    # 车手评分截止时长（小时）
    RATING_CLOSE_HOURS = 24

    # 评分投票开启延迟：正赛开始后 60 分钟（2026-09-23 用户裁定；
    # 此前为 SESSION_DURATION≈120min 预计完赛时刻。比赛周条目会提前预创建落盘，
    # 此刻前仅可领链接、submit_votes 门控拒绝防提前刷票）
    RATING_OPEN_DELAY_MIN = 60

    # ==================== 车手评分（DOTD投票） ====================

    def schedule_rating_open(self, session: Dict[str, Any]):
        """
        调度评分开启任务：开赛+RATING_OPEN_DELAY_MIN（正赛开始1小时后）开启投票，
        与成绩推送解耦，不受API数据延迟影响
        """
        if session['type'] != 'race':
            return

        job_id = self._make_job_id(session['type'], session['round'], "rating_open")

        open_time = session['datetime'] + timedelta(minutes=self.RATING_OPEN_DELAY_MIN)
        if self._job_guard(job_id, open_time):
            return
        now = datetime.now(session['datetime'].tzinfo)
        if open_time < now:
            return

        job = self.scheduler.add_job(
            func=self._rating_open_job,
            trigger=DateTrigger(run_date=open_time),
            args=[session],
            id=job_id,
            replace_existing=True
        )
        self.scheduled_jobs[job_id] = job
        local_time = open_time.astimezone(self.local_tz)
        logger.info(f"已调度评分开启任务: {job_id} - 将于 {local_time.strftime('%Y-%m-%d %H:%M')} 执行")

    def _rating_open_job(self, session: Dict[str, Any]):
        """评分开启任务入口：按轮次查race_data后开启评分"""
        try:
            race_data = None
            for race in self.f1_api.get_schedule():
                if race['round'] == session['round']:
                    race_data = race
                    break
            if not race_data:
                logger.error(f"评分开启失败：赛程中未找到 Round {session['round']}")
                return
            self._open_driver_rating(session, race_data)
        except Exception as e:
            logger.warning(f"开启车手评分失败: {e}")

    def _open_driver_rating(self, session: Dict[str, Any], race_data: Dict[str, Any],
                            close_time=None):
        """正赛后开启车手评分，发引导卡片并调度截止任务

        Args:
            close_time: 可选，指定的截止时间（aware datetime）；
                        缺省为 当前时间+RATING_CLOSE_HOURS。
                        自愈补开时传入 正赛结束+RATING_CLOSE_HOURS，避免补开延后截止
        """
        ratings_store = self.config.get('ratings_store')
        base_url = self.config.get('rating_base_url')
        group_openid = self.config.get('group_openid')
        if not ratings_store or not base_url:
            return

        season = self.config.get('SEASON') or session['datetime'].year
        race_key = ratings_store.open_race(season, session['round'], race_data.get('raceName', session['race_name']))

        # 截止时间：缺省 now+24h；自愈补开时为 正赛结束+24h（不延后）
        close_time = close_time or (datetime.now(self.local_tz) + timedelta(hours=self.RATING_CLOSE_HOURS))
        if close_time <= datetime.now(self.local_tz):
            # 自愈补开时截止时间已过：直接结算，不发卡片
            logger.info(f"评分截止时间已过，直接结算: {race_key}")
            self._close_driver_rating(race_key)
            return

        close_str = close_time.astimezone(self.local_tz).strftime('%m-%d %H:%M')
        board_url = f"{base_url}/board/{race_key}"
        md = (
            f"## ⭐ 车手评分开启\r"
            f"> {race_data.get('raceName', session['race_name'])}\r\r"
            f"给本场车手打分（1-10分），选出你心中的最佳车手！\r\r"
                    f"**参与方式**：在群里发送 `/rate` 获取评分链接和验证码\r\r"
            f"⏰ {close_str}（北京时间）截止并公布结果\r"
            f"📊 [实时评分榜]({board_url})"
        )
        text = (
            f"⭐ 车手评分开启 - {race_data.get('raceName', session['race_name'])}\n\n"
            f"给本场车手打分（1-10分），选出最佳车手！\n"
            f"参与方式：在群里发送 /rate 获取评分链接和验证码\n"
            f"{close_str}（北京时间）截止\n"
            f"实时榜: {board_url}"
        )
        try:
            self.notify_bot.send_markdown_message(md, fallback_text=text)
            logger.info(f"✓ 评分引导卡片已发送: {race_key}")
        except Exception as e:
            logger.warning(f"评分引导卡片发送失败: {e}")

        # 调度截止任务
        job_id = f"rating_close_{race_key}"
        self.scheduler.add_job(
            func=self._close_driver_rating,
            trigger=DateTrigger(run_date=close_time),
            args=[race_key],
            id=job_id,
            replace_existing=True
        )
        self.scheduled_jobs[job_id] = True
        logger.info(f"已调度评分截止任务: {job_id} - {close_time.strftime('%Y-%m-%d %H:%M')}")

    def _close_driver_rating(self, race_key: str):
        """截止评分：结算→导出CSV→生成图表→群里公布"""
        ratings_store = self.config.get('ratings_store')
        base_url = self.config.get('rating_base_url')
        if not ratings_store:
            return

        try:
            result = ratings_store.close_race(race_key)
            if not result:
                return
            race = ratings_store.get_race(race_key)
            board = result["board"]
            dotd = result["dotd"]

            # 车手显示名
            driver_names = {}
            try:
                race_result = self.f1_api.get_session_results(race["round"], "race", season=race["season"])
                if race_result:
                    driver_names = {e["driver_id"]: e["driver_name"] for e in race_result["entries"]}
            except Exception:
                pass

            # 汇总卡片
            medals = {1: "🥇", 2: "🥈", 3: "🥉"}
            md = f"## 📊 评分结果公布\r> {race['race_name']}\r\r"
            text = f"📊 评分结果公布 - {race['race_name']}\n\n"
            for i, row in enumerate(board, 1):
                name = driver_names.get(row["driver_id"], row["driver_id"])
                rank = medals.get(i, f"**{i}.**")
                md += f"{rank} **{name}** — {row['avg']}分（{row['count']}票）\r"
                text += f"{i}. {name} - {row['avg']}分（{row['count']}票）\n"
            if not board:
                md += "\r本场无人参与评分"
                text += "本场无人参与评分\n"

            if dotd:
                name = driver_names.get(dotd, dotd)
                md += f"\r\r🏆 **本场最佳车手：{name}**"
                text += f"\n🏆 本场最佳车手：{name}"
            elif board:
                md += f"\r\r（有效票不足{2}票，本场不设最佳车手）"
                text += f"\n（有效票不足{2}票，本场不设最佳车手）"

            self.notify_bot.send_markdown_message(md, fallback_text=text)

            # 图表
            if board:
                try:
                    from .ratings_chart import render_ratings_chart
                    chart_path = render_ratings_chart(race["race_name"], board, driver_names, dotd, race_key=race_key)
                    if chart_path and hasattr(self.notify_bot, 'send_image_url'):
                        chart_url = f"{base_url}/exports/ratings_chart_{race_key}.png"
                        self.notify_bot.send_image_url(chart_url, content=f"{race['race_name']} 评分统计图")
                except Exception as e:
                    logger.warning(f"评分图表发送失败: {e}")

            logger.info(f"✓ 评分结果已公布: {race_key} DOTD={dotd}")
        except Exception as e:
            logger.error(f"截止评分失败: {e}")

    def _send_dm_personalized_standings(self, standings: Dict[str, Any], dm_users: Dict[str, Dict[str, Any]]):
        """私聊推送：每位设置偏好的用户收到自己关注的车手/车队最新排名"""
        from .qq_group_bot import QQGroupBot
        d_entries = QQGroupBot._parse_driver_standings(standings.get("drivers") or {})
        t_entries = QQGroupBot._parse_constructor_standings(standings.get("constructors") or {})
        d_map = {e.get("Driver", {}).get("driverId"): e for e in d_entries}
        t_map = {e.get("Constructor", {}).get("constructorId"): e for e in t_entries}

        sent = 0
        failed = 0
        for user_openid, pref in dm_users.items():
            lines = []
            d_names = UserPrefsStore.pref_driver_names(pref)
            for driver_id in UserPrefsStore.pref_drivers(pref):
                if driver_id in d_map:
                    e = d_map[driver_id]
                    name = d_names.get(driver_id) or e.get("Driver", {}).get("familyName", driver_id)
                    lines.append(f"🏎️ 你的车手 **{name}**: P{e.get('position', '?')} - {e.get('points', '0')}分")
                else:
                    lines.append(f"🏎️ 你的车手 {d_names.get(driver_id, driver_id)}: 本赛季暂无积分数据")

            t_names = UserPrefsStore.pref_team_names(pref)
            for team_id in UserPrefsStore.pref_teams(pref):
                if team_id in t_map:
                    e = t_map[team_id]
                    name = t_names.get(team_id) or e.get("Constructor", {}).get("name", team_id)
                    lines.append(f"🏁 你的主队 **{name}**: P{e.get('position', '?')} - {e.get('points', '0')}分")
                else:
                    lines.append(f"🏁 你的主队 {t_names.get(team_id, team_id)}: 本赛季暂无积分数据")

            if not lines:
                continue

            md = "## ⭐ 个性化积分播报\r\r" + "\r".join(lines)
            txt = "⭐ 个性化积分播报\n\n" + "\n".join(l.replace("**", "") for l in lines)
            try:
                ok = self.notify_bot.send_dm_markdown(user_openid, md, fallback_text=txt)
                if ok is False:
                    failed += 1
                else:
                    sent += 1
            except Exception as e:
                failed += 1
                logger.warning(f"私聊推送失败(用户 {user_openid[:8]}...): {e}")

        logger.info(f"✓ 个性化积分播报完成: 成功 {sent}，失败 {failed}")

    def _send_session_result(self, session: Dict[str, Any]):
        """
        推送环节成绩（排位/冲刺排位/冲刺赛）

        Args:
            session: 比赛环节信息
        """
        try:
            if self._already_pushed(session):
                logger.info(f"环节成绩已推送过，跳过: {session['race_name']} - {session['name']}")
                return
            logger.info(f"获取环节成绩: {session['race_name']} - {session['name']}")
            result = self.f1_api.get_session_results(int(session['round']), session['type'])
            if not result or not result.get('entries'):
                logger.warning(f"成绩尚未公布: {session['race_name']} - {session['name']}，转入轮询补推")
                self._schedule_poll(session, 0, kind="session_result")
                return
            if self._do_session_result_push(session, result):
                self._mark_pushed(session)
            else:
                self._schedule_poll(session, 0, kind="session_result")
        except Exception as e:
            logger.error(f"推送环节成绩失败: {e}")

    def _do_session_result_push(self, session: Dict[str, Any], result: Dict[str, Any]) -> bool:
        """发送环节成绩卡片，高亮群成员关注的车手；随后私聊推送各用户关注车手成绩"""
        watch_ids = set()
        prefs_store = self.config.get('prefs_store')
        if prefs_store:
            # 多关注对象（drivers 列表，上限3）；pref_drivers 兼容旧单值键
            for pref in prefs_store.get_all_user_prefs().values():
                watch_ids.update(UserPrefsStore.pref_drivers(pref))

        if hasattr(self.notify_bot, 'send_session_result'):
            ok = self.notify_bot.send_session_result(result, watch_driver_ids=watch_ids)
            if ok is not False and self.config.get('dm_session_result'):
                self._send_dm_session_result(result)
            return ok is not False
        return True

    def _send_dm_session_result(self, result: Dict[str, Any]):
        """环节成绩私聊推送：给设置了关注车手（/setdriver）的用户单发其车手成绩卡片。

        前提：用户已与机器人建立私聊通道（加好友/允许主动消息），
        未建立通道的用户发送失败仅计数，不影响群推送。
        """
        prefs_store = self.config.get('prefs_store')
        if not prefs_store or not hasattr(self.notify_bot, 'send_dm_markdown'):
            return
        entries = result.get('entries') or []
        if not entries:
            return
        by_driver = {e.get('driver_id'): e for e in entries}
        race_name = result.get('race_name', '')
        session_name = result.get('session_name', '')

        sent = failed = 0
        for user_id, pref in prefs_store.get_all_user_prefs().items():
            d_names = UserPrefsStore.pref_driver_names(pref)
            hits = [by_driver[d] for d in UserPrefsStore.pref_drivers(pref)
                    if d in by_driver]
            if not hits:
                continue
            lines_md, lines_txt = [], []
            for e in hits:
                name = d_names.get(e.get('driver_id')) or e.get('driver_name', '')
                time_part = f" — {e['time']}" if e.get('time') else ""
                grid = e.get('grid')
                grid_part = ""
                try:
                    if grid is not None and int(grid) != int(e.get('position', 0)):
                        grid_part = f"（发车 P{grid}）"
                except (TypeError, ValueError):
                    pass
                lines_md.append(f"🏎️ **{name}**：**P{e.get('position', '?')}**{grid_part}{time_part}（{e.get('team_name', '')}）")
                lines_txt.append(f"{name}: P{e.get('position', '?')}{grid_part}{time_part}")
            md = (f"## ⭐ {race_name} {session_name}\r\r" + "\r".join(lines_md))
            txt = (f"⭐ {race_name} {session_name}\n\n" + "\n".join(lines_txt))
            try:
                ok = self.notify_bot.send_dm_markdown(user_id, md, fallback_text=txt)
                if ok is False:
                    failed += 1
                else:
                    sent += 1
            except Exception as ex:
                failed += 1
                logger.warning(f"环节成绩私聊推送失败(用户 {str(user_id)[:8]}...): {ex}")
        if sent or failed:
            logger.info(f"✓ 环节成绩个性化私聊: 成功 {sent}，失败 {failed}（{race_name} {session_name}）")

    def schedule_session_result(self, session: Dict[str, Any], delay_minutes: int = 30):
        """
        调度环节成绩推送任务（环节结束后delay分钟执行）

        Args:
            session: 比赛环节信息
            delay_minutes: 结束后延迟分钟数
        """
        if session['type'] not in self.RESULT_PUSH_TYPES:
            return

        job_id = self._make_job_id(session['type'], session['round'], "session_result")

        duration = self.SESSION_DURATION.get(session['type'], 60)
        push_time = session['datetime'] + timedelta(minutes=duration + delay_minutes)
        if self._job_guard(job_id, push_time):
            return

        now = datetime.now(session['datetime'].tzinfo)
        if push_time < now:
            return

        job = self.scheduler.add_job(
            func=self._send_session_result,
            trigger=DateTrigger(run_date=push_time),
            args=[session],
            id=job_id,
            replace_existing=True
        )

        self.scheduled_jobs[job_id] = job
        local_time = push_time.astimezone(self.local_tz)
        logger.info(f"已调度成绩推送任务: {job_id} - 将于 {local_time.strftime('%Y-%m-%d %H:%M')} 执行")

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

        # 计算推送时间（假设比赛持续2小时）
        post_time = session['datetime'] + timedelta(hours=2, minutes=delay_minutes)

        # 任务已存在且时间未变则跳过；时间变更则重排
        if self._job_guard(job_id, post_time):
            return
        
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

    # ==================== 赛前动力单元部件更换检测 ====================
    # 超量使用PU部件会导致正赛发车格罚退，故在赛前提前播报

    PU_CHECK_ADVANCE_MINUTES = 30  # 正赛开始前30分钟检查

    def _send_pre_race_pu_check(self, session: Dict[str, Any]):
        """赛前检测本站动力单元部件更换并播报（超量可能罚退）"""
        f1cosmos = self.config.get('f1cosmos')
        if not f1cosmos:
            return
        try:
            changed = f1cosmos.detect_pu_changes()
            if changed:
                pu_text, pu_md = f1cosmos.format_pu_changes(changed, context="pre_race")
                if pu_text and hasattr(self.notify_bot, 'send_markdown_message'):
                    self.notify_bot.send_markdown_message(pu_md, fallback_text=pu_text)
                logger.info(f"赛前PU部件更换播报: {len(changed)} 位车手")
            else:
                logger.info("本站无新的动力单元部件更换")
        except Exception as e:
            logger.warning(f"赛前PU部件更换播报失败: {e}")

    def schedule_pre_race_pu_check(self, session: Dict[str, Any]):
        """调度赛前PU部件更换检测任务（仅正赛）"""
        if session['type'] != 'race':
            return

        job_id = self._make_job_id(session['type'], session['round'], "pre_race_pu")
        check_time = session['datetime'] - timedelta(minutes=self.PU_CHECK_ADVANCE_MINUTES)
        if self._job_guard(job_id, check_time):
            return
        now = datetime.now(session['datetime'].tzinfo)
        if check_time < now:
            return

        job = self.scheduler.add_job(
            func=self._send_pre_race_pu_check,
            trigger=DateTrigger(run_date=check_time),
            args=[session],
            id=job_id,
            replace_existing=True
        )
        self.scheduled_jobs[job_id] = job
        local_time = check_time.astimezone(self.local_tz)
        logger.info(f"已调度赛前PU部件更换检测: {job_id} - {local_time.strftime('%Y-%m-%d %H:%M')}")

    # ==================== F1 新闻速递 ====================

    def add_news_jobs(self):
        """每日 8:00 / 19:00 推送上一周期内的 F1 新闻（LLM 精简翻译）"""
        if not self.config.get('news_collector'):
            return
        for hour in (8, 19):
            job = self.scheduler.add_job(
                func=self._push_news_digest,
                trigger=CronTrigger(hour=hour, minute=0, timezone=self.local_tz),
                id=f'news_digest_{hour}',
                replace_existing=True
            )
            logger.info(f"已添加新闻推送任务（每日 {hour}:00）")
        return True

    def _push_news_digest(self):
        """采集 → 主题去重/降权 → DM 预选 → 仅对选中集翻译 → 群卡片 + 定向DM

        翻译范围 = 群卡片条目 ∪ 各用户 DM 命中条目的并集（选中后翻译，
        不翻译不会推送的条目；修复 DM 命中超出前10时无译文的问题）。
        同主题（不同来源同一事件）批内去重、跨批降权标记 🔁。"""
        collector = self.config.get('news_collector')
        llm = self.config.get('llm')
        if not collector:
            return
        try:
            now = datetime.now(self.local_tz).timestamp()
            since = collector.last_push_ts()
            items = collector.fetch_since(since)
            if not items:
                logger.info("本周期无新新闻，跳过推送")
                collector.mark_pushed(now)
                return

            # 1. 同主题去重/降权
            items = collector.filter_topics(items)

            # 2. 选中：群卡片前 N 条 + 各用户 DM 命中（基于原文标题匹配，与译文无关）
            group_items = items[:collector.MAX_ITEMS]
            dm_matches = {}
            prefs_store = self.config.get('prefs_store')
            if prefs_store and hasattr(self.notify_bot, 'send_dm_markdown'):
                for user_id, pref in prefs_store.get_all_user_prefs().items():
                    if not pref.get('news_dm', True):
                        continue
                    matched = collector.match_items_for_pref(items, pref)
                    if matched:
                        dm_matches[user_id] = matched[:collector.MAX_ITEMS]

            # 3. 仅翻译选中并集（按对象 id 去重）
            union, seen_ids = [], set()
            for it in group_items + [m for ms in dm_matches.values() for m in ms]:
                if id(it) not in seen_ids:
                    seen_ids.add(id(it))
                    union.append(it)
            collector.digest_with_llm(union, llm)

            # 4. 群推送
            text, md = collector.build_digest(group_items, period_from=since, period_to=now)
            if self.notify_bot.send_markdown_message(md, fallback_text=text):
                collector.mark_pushed(now, items=union)
                logger.info(f"✓ 新闻速递已推送: {len(group_items)} 条"
                            f"（采集 {len(items)}，翻译 {len(union)}）")
                self._send_dm_news_digest(collector, dm_matches, since, now)
            else:
                logger.warning("新闻速递发送失败，下周期重试（不更新推送时间戳）")
        except Exception as e:
            logger.warning(f"新闻推送任务失败: {e}")

    def _send_dm_news_digest(self, collector, dm_matches: Dict[str, list], since: float, now: float):
        """新闻定向 DM：dm_matches 已由 _push_news_digest 预选+翻译，此处只成卡发送"""
        prefs_store = self.config.get('prefs_store')
        if not prefs_store or not hasattr(self.notify_bot, 'send_dm_markdown'):
            return
        sent = failed = 0
        for user_id, matched in dm_matches.items():
            pref = prefs_store.get_user_prefs(user_id)
            try:
                text, md = collector.build_dm_digest(matched, pref, since, now)
                ok = self.notify_bot.send_dm_markdown(user_id, md, fallback_text=text)
                if ok is False:
                    failed += 1
                else:
                    sent += 1
            except Exception as ex:
                failed += 1
                logger.warning(f"新闻定向DM失败(用户 {str(user_id)[:8]}...): {ex}")
        if sent or failed:
            logger.info(f"✓ 新闻定向DM: 成功 {sent}，失败 {failed}")

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
                # 比赛周预创建评分条目（落盘持久，open_at=开赛+1h）：
                # /rate 比赛周即指向本场链接；开启前 submit_votes 门控拒绝防提前刷票。
                # 不再依赖开启任务成功执行才生成条目（2026-09-23 实证：服务器滚动调度
                # 失效后蒙扎/巴库条目从未生成，/rate 永久锚定荷兰站）
                _rs = self.config.get('ratings_store')
                if _rs:
                    try:
                        _season = self.config.get('SEASON') or session['datetime'].year
                        _rs.open_race(_season, session['round'], session['race_name'],
                                      open_at=session['datetime']
                                      + timedelta(minutes=self.RATING_OPEN_DELAY_MIN))
                    except Exception as _e:
                        logger.warning(f"预创建评分条目失败: {_e}")
                self.schedule_post_race_result(session, post_race_delay)
                self.schedule_pre_race_pu_check(session)
                self.schedule_rating_open(session)
            
            # 排位/冲刺排位/冲刺赛，调度成绩推送
            if session_type in self.RESULT_PUSH_TYPES:
                self.schedule_session_result(session, delay_minutes=30)
        
        logger.info(f"赛程任务更新完成，新调度 {scheduled_count} 个任务")

        # 自愈：补建过去48小时已结束但未推送的成绩轮询任务
        self._enqueue_missed_result_pushes()

        # 自愈：补开/补建投票窗口内缺失的车手评分任务（容器重启容错）
        self._heal_missed_rating_opens()

        # 每日清扫：陈旧未截止评分条目静默关闭（两层兜底：正赛结束+24h已过立即关/
        # 新比赛周开始强制关上一站，防 /rate 永久锚定旧场次）
        _rs = self.config.get('ratings_store')
        if _rs and hasattr(_rs, 'close_stale_races'):
            try:
                _rs.close_stale_races(self.f1_api.get_schedule(),
                                      close_hours=self.RATING_CLOSE_HOURS)
            except Exception as _e:
                logger.warning(f"陈旧评分场次清扫失败: {_e}")
    
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