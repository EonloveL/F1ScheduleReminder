"""
F1 Schedule Bot — Discord 版（全功能，对齐 QQ 官方群版）

功能：定时推送（环节提醒/赛后成绩/积分榜/每周预告）+ 斜杠指令 + /ask AI 问答

部署：
1. https://discord.com/developers/applications 创建应用 → Bot → 获取 Token
   （Bot 页打开 Message Content Intent）
2. OAuth2 → URL Generator → scope: bot + applications.commands → 邀请进服务器
3. 复制 .env.example 为 .env，填入 DISCORD_BOT_TOKEN / DISCORD_CHANNEL_IDS
4. pip install -r requirements.txt && python main.py
   或 docker compose up -d --build
"""
import logging
import os
import signal
import sys
import threading
import time
from datetime import datetime, timedelta

current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(current_dir, ".env"))
except ImportError:
    pass

# 图表/头像默认不走公网 URL：伪协议由 DiscordBot._resolve_image 映射为本地文件上传。
# 须在 import common 模块前设置（f1_tools/scheduler 运行期读取该值拼图表 URL）
# 用户配置了真实 RATING_BASE_URL（自行部署 web/rating_server.py）时跳过伪协议，
# 图表走 URL embed，同时启用评分/遥测等 Web 依赖功能
_RATING_BASE_REAL = os.getenv("RATING_BASE_URL", "").rstrip("/")
WEB_FEATURES_ENABLED = _RATING_BASE_REAL.startswith("http")
if not WEB_FEATURES_ENABLED:
    os.environ.setdefault("RATING_BASE_URL", "local://files")

from common.f1_api import F1API
from common.scheduler import ReminderScheduler
from common.user_prefs import UserPrefsStore
from common.pushed_store import PushedResultsStore
from common.circuits_manager import CircuitsManager
from common.f1cosmos_api import F1CosmosAPI

from bot import DiscordBot
from i18n import LANG

logger = logging.getLogger(__name__)

# ==================== 配置 ====================
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN", "")


def _parse_id_list(env_name: str) -> list:
    """逗号分隔 ID 列表解析（空段/非数字段跳过并告警，防 import 期 ValueError 崩进程）"""
    out = []
    for x in os.getenv(env_name, "").split(","):
        x = x.strip()
        if not x:
            continue
        if not x.isdigit():
            logging.getLogger(__name__).warning(f"{env_name} 含非法 ID「{x}」，已跳过")
            continue
        out.append(int(x))
    return out


DISCORD_CHANNEL_IDS = _parse_id_list("DISCORD_CHANNEL_IDS")
DISCORD_GUILD_IDS = _parse_id_list("DISCORD_GUILD_IDS") or None
PROXY_URL = os.getenv("PROXY_URL", "") or None
ADMIN_USER_IDS = [x.strip() for x in os.getenv("ADMIN_USER_IDS", "").split(",") if x.strip()]

TIMEZONE = os.getenv("TIMEZONE", "UTC")
# compose 传空串时 os.getenv 拿到 "" 而非缺省值，int("") 会崩（review 阻断 #1）——or 兜底
SEASON = int(os.getenv("SEASON") or "0") or datetime.now().year
PING_EVERYONE = os.getenv("PING_EVERYONE", "false").lower() == "true"
SEND_STARTUP_MESSAGE = os.getenv("SEND_STARTUP_MESSAGE", "true").lower() == "true"

REMINDER_TIMES = {"fp1": 30, "fp2": 30, "fp3": 30,
                  "qualifying": 30, "sprint": 30, "sprint_qualifying": 30, "race": 60}
POST_RACE_DELAY = int(os.getenv("POST_RACE_DELAY", "30"))

NEWS_ENABLED = os.getenv("NEWS_ENABLED", "false").lower() == "true"
WEEKLY_PREVIEW_ENABLED = os.getenv("WEEKLY_PREVIEW_ENABLED", "true").lower() == "true"
WEEKLY_PREVIEW_DAY = int(os.getenv("WEEKLY_PREVIEW_DAY", "0"))   # 0=周一
WEEKLY_PREVIEW_HOUR = int(os.getenv("WEEKLY_PREVIEW_HOUR", "9"))
WEEKLY_VERIFY_LAP_RECORD = os.getenv("WEEKLY_VERIFY_LAP_RECORD", "true").lower() == "true"
# 升级件追更/环节成绩 DM 的文案为中文（scheduler/common 内嵌），en 界面默认关；
# "auto"/空串 = 按语言取默认（compose 传 auto 哨兵，防空串吃掉默认值）
def _lang_default_flag(env_name: str) -> bool:
    v = (os.getenv(env_name) or "auto").strip().lower()
    if v in ("auto", ""):
        return LANG == "zh"
    return v == "true"

UPGRADES_WATCH_ENABLED = _lang_default_flag("UPGRADES_WATCH_ENABLED")
DM_SESSION_RESULT = _lang_default_flag("DM_SESSION_RESULT")
UPGRADE_DATA_SOURCE = os.getenv("UPGRADE_DATA_SOURCE", "fia")

# ============ Web 依赖功能（可选，需自行部署 web/rating_server.py，见 README） ============
# RATING_BASE_URL 为真实 http(s) 地址时启用：评分生命周期推送 + /rate /ratings /dotd /telemetry
RATING_ENABLED = WEB_FEATURES_ENABLED and os.getenv("RATING_ENABLED", "true").lower() == "true"
# 实时计时面板地址（默认复用 RATING_BASE_URL；配置即启用 /live）
LIVE_BASE_URL = (os.getenv("LIVE_BASE_URL", "").rstrip("/") or _RATING_BASE_REAL)
# 观赛链接（自定义，格式：名称|URL，多条用分号或换行分隔；配置即启用 /watch）
# 例：WATCH_LINKS=F1 TV|https://f1tv.formula1.com;Sky Sports|https://www.skysports.com/f1
WATCH_LINKS = []
for _pair in os.getenv("WATCH_LINKS", "").replace("\n", ";").split(";"):
    if "|" in _pair:
        _name, _url = _pair.split("|", 1)
        if _name.strip() and _url.strip().startswith("http"):
            WATCH_LINKS.append((_name.strip(), _url.strip()))
# 启动时把管理员指令速查卡 DM 给全部管理员（默认 true）
ADMIN_GUIDE_PUSH = os.getenv("ADMIN_GUIDE_PUSH", "true").lower() == "true"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.path.join(current_dir, "f1_discord.log")


def setup_logging():
    fmt = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter(fmt))
    # 文件日志轮转（与 QQ 版一致：5MB×5，防 restart:always 下无限膨胀——review 中 #8）
    from logging.handlers import RotatingFileHandler
    file_h = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024,
                                 backupCount=5, encoding="utf-8")
    file_h.setLevel(logging.DEBUG)
    file_h.setFormatter(logging.Formatter(fmt))
    logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                        format=fmt, handlers=[console, file_h])

    # 出网门禁：注册已知 env 密钥 + 运行时密钥，日志通道挂脱敏过滤器
    from common import secret_guard
    secret_guard.auto_register_env()
    try:
        from common import llm_secrets
        llm_secrets.register_all_to_guard()
    except Exception:
        pass
    secret_guard.install_log_filter()


def _seed_data_dir():
    """Docker 部署：镜像内 data/ 档案作为种子（/app/data_seed），
    首次挂载空 volume 时补缺（已存在的运行时文件不覆盖）"""
    import shutil
    seed = os.path.join(parent_dir, "data_seed")
    dst = os.path.join(parent_dir, "data")
    if not os.path.isdir(seed):
        return
    os.makedirs(dst, exist_ok=True)
    for f in os.listdir(seed):
        s, d = os.path.join(seed, f), os.path.join(dst, f)
        if os.path.isfile(s) and not os.path.exists(d):
            try:
                shutil.copy2(s, d)
            except Exception:
                pass


class F1DiscordApp:
    """应用装配：数据源 / 调度器 / Discord Bot / 指令"""

    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.f1_api = None
        self.f1cosmos = None
        self.circuits_manager = None
        self.llm = None
        self.news_collector = None
        self.prefs_store = None
        self.pushed_store = None
        self.ratings_store = None
        self.discord_bot = None
        self.scheduler = None
        self.running = False

    def initialize(self) -> bool:
        self.logger.info("=" * 60)
        self.logger.info(f"F1 Schedule Bot [Discord] starting... (lang={LANG}, tz={TIMEZONE})")
        self.logger.info("=" * 60)

        _seed_data_dir()

        if not DISCORD_BOT_TOKEN:
            self.logger.error("DISCORD_BOT_TOKEN 未配置（.env 或环境变量）")
            return False
        if not DISCORD_CHANNEL_IDS:
            self.logger.error("DISCORD_CHANNEL_IDS 未配置（推送目标频道，逗号分隔）")
            return False

        try:
            self.f1_api = F1API(season=SEASON)
            self.logger.info(f"✓ F1 API 初始化成功 (赛季: {SEASON})")
        except Exception as e:
            self.logger.error(f"F1 API 初始化失败: {e}")
            return False

        try:
            self.circuits_manager = CircuitsManager()
        except Exception as e:
            self.logger.warning(f"赛道数据管理器初始化失败: {e}")
            self.circuits_manager = None

        try:
            if UPGRADE_DATA_SOURCE == "fia":
                from common.fia_docs import FIADocsAPI
                self.f1cosmos = FIADocsAPI(season=SEASON)
                self.f1cosmos.schedule_provider = self.f1_api.get_schedule_for_year
                self.logger.info("✓ 升级件/PU 数据源初始化成功（FIA 官方文档）")
            else:
                self.f1cosmos = F1CosmosAPI(season=SEASON)
        except Exception as e:
            self.logger.warning(f"升级件/PU 数据源初始化失败: {e}")
            self.f1cosmos = None

        try:
            from common.llm_assistant import LLMAssistant
            self.llm = LLMAssistant()
            if self.llm.enabled:
                self.logger.info("✓ LLM 助手初始化成功")
            else:
                self.logger.warning("未配置 LLM API Key，/ask 问答已禁用（其余功能正常）")
        except Exception as e:
            self.logger.warning(f"LLM 助手初始化失败: {e}")
            self.llm = None

        if NEWS_ENABLED:
            try:
                from common.f1_news import F1NewsCollector
                self.news_collector = F1NewsCollector()
            except Exception as e:
                self.logger.warning(f"新闻采集器初始化失败: {e}")

        try:
            self.discord_bot = DiscordBot(
                token=DISCORD_BOT_TOKEN,
                channel_ids=DISCORD_CHANNEL_IDS,
                proxy=PROXY_URL,
                tz=TIMEZONE,
                ping_everyone=PING_EVERYONE,
                guild_ids=DISCORD_GUILD_IDS,
            )
        except Exception as e:
            self.logger.error(f"Discord Bot 初始化失败: {e}")
            return False

        try:
            from common.admin_alert import set_admin_alert_hook

            def _admin_alert(title, detail):
                if not (ADMIN_USER_IDS and self.discord_bot):
                    return
                for admin in ADMIN_USER_IDS:
                    try:
                        self.discord_bot.send_dm_markdown(
                            admin, f"## ⚠️ {title}\n\n{detail}",
                            fallback_text=f"⚠️ {title}\n{detail}")
                    except Exception:
                        pass

            set_admin_alert_hook(_admin_alert)
        except Exception as e:
            self.logger.warning(f"管理员告警钩子注册失败: {e}")

        try:
            self.prefs_store = UserPrefsStore()
            self.pushed_store = PushedResultsStore()
            self.ratings_store = None
            if RATING_ENABLED:
                try:
                    from common.ratings_store import RatingsStore
                    self.ratings_store = RatingsStore()
                    self.logger.info(f"✓ 车手评分已启用（Web 服务: {_RATING_BASE_REAL}）")
                except Exception as e:
                    self.logger.warning(f"评分存储初始化失败，评分功能禁用: {e}")
            config = {
                "TIMEZONE": TIMEZONE,
                "REMINDER_TIMES": REMINDER_TIMES,
                "POST_RACE_DELAY": POST_RACE_DELAY,
                "SEASON": SEASON,
                "prefs_store": self.prefs_store,
                "pushed_store": self.pushed_store,
                "ratings_store": self.ratings_store,
                "rating_base_url": _RATING_BASE_REAL or None,
                "group_openid": None,
                "lap_record_tracker": self.circuits_manager,
                "f1cosmos": self.f1cosmos,
                "dm_session_result": DM_SESSION_RESULT,
                "news_collector": self.news_collector,
                "llm": self.llm,
            }
            self.scheduler = ReminderScheduler(self.f1_api, self.discord_bot, config)
            self.logger.info("✓ 调度器初始化成功")
        except Exception as e:
            self.logger.error(f"调度器初始化失败: {e}")
            return False

        return True

    def start(self):
        if not self.initialize():
            self.logger.error("初始化失败，退出")
            sys.exit(1)

        from commands import register_commands, CommandContext
        ctx = CommandContext(
            f1_api=self.f1_api, f1cosmos=self.f1cosmos,
            circuits_manager=self.circuits_manager, llm=self.llm,
            prefs_store=self.prefs_store, pushed_store=self.pushed_store,
            scheduler=self.scheduler, discord_bot=self.discord_bot,
            season=SEASON, guild_ids=DISCORD_GUILD_IDS,
            admin_user_ids=ADMIN_USER_IDS,
            ratings_store=self.ratings_store,
            rating_base_url=_RATING_BASE_REAL,
            live_base_url=LIVE_BASE_URL,
            watch_links=WATCH_LINKS,
        )
        register_commands(self.discord_bot.bot, ctx)
        self._register_dm_admin(ctx)

        bot_thread = threading.Thread(target=self._run_bot, daemon=True)
        bot_thread.start()

        self.logger.info("等待 Discord 连接...")
        waited = 0
        while not self.discord_bot.bot.is_ready() and waited < 90:
            time.sleep(1)
            waited += 1
        if not self.discord_bot.bot.is_ready():
            self.logger.error("Discord 连接超时（检查 Token / 代理 / 网络）")
            sys.exit(1)
        self.logger.info("✓ Discord Bot 已就绪")

        if SEND_STARTUP_MESSAGE:
            try:
                time.sleep(2)
                self.discord_bot.send_startup_message()
            except Exception as e:
                self.logger.warning(f"启动消息发送失败: {e}")

        # 管理员指令速查卡 DM 推送（/llm 等配置指令不在公开 /help 中）
        if ADMIN_GUIDE_PUSH and ADMIN_USER_IDS:
            try:
                from common.llm_admin import ADMIN_QUICKREF
                for admin in ADMIN_USER_IDS:
                    self.discord_bot.send_dm_markdown(
                        admin, ADMIN_QUICKREF,
                        fallback_text=ADMIN_QUICKREF.replace("**", "").replace("`", ""))
            except Exception as e:
                self.logger.warning(f"管理员速查卡推送失败: {e}")

        try:
            self.scheduler.start()
            self.scheduler.add_daily_update_job()
            if self.news_collector:
                self.scheduler.add_news_jobs()
        except Exception as e:
            self.logger.error(f"调度器启动失败: {e}")
            sys.exit(1)

        try:
            from common.drivers_profile import log_review_reminder
            log_review_reminder(SEASON)
        except Exception:
            pass

        if WEEKLY_PREVIEW_ENABLED:
            self._add_weekly_preview_job()
        if UPGRADES_WATCH_ENABLED:
            self._add_upgrades_watch_job()

        self.running = True
        self.logger.info("✓ 启动完成")
        self.logger.info("=" * 60)
        self._print_upcoming()
        self._main_loop()

    def _register_dm_admin(self, ctx):
        """私聊管理员指令：/llm 族（切换 AI 服务商 / 配置 API Key）
        走文本消息而非 slash command：guild 注册的 slash 指令在 DM 不可用，
        且密钥参数不应经过指令自动补全/历史记录"""
        import discord as _discord

        @self.discord_bot.bot.listen("on_message")
        async def _dm_admin(message):
            if message.author.bot:
                return
            if not isinstance(message.channel, _discord.DMChannel):
                return
            content = (message.content or "").strip()
            # 精确前缀："/llm" 或 "/llm xxx"（"/llmfoo" 不吃——review 低 #12）
            if not (content.lower() == "/llm" or content.lower().startswith("/llm ")):
                return
            uid = str(message.author.id)
            if not ADMIN_USER_IDS:
                await self.discord_bot._send_channel(
                    message.channel, "💡 ADMIN_USER_IDS 未配置，/llm 管理指令不可用")
                return
            if uid not in ADMIN_USER_IDS:
                await self.discord_bot._send_channel(message.channel, "⛔ 该指令仅限管理员使用")
                return
            if not ctx.llm:
                await self.discord_bot._send_channel(
                    message.channel, "❌ LLM 助手初始化失败，请检查日志后重启")
                return
            try:
                from common.llm_admin import handle_llm_admin
                reply = handle_llm_admin(ctx.llm, content[4:].strip(), lang=LANG)
                await self.discord_bot._send_channel(message.channel, reply)
            except Exception as e:
                self.logger.error(f"/llm 管理指令失败: {e}")
                await self.discord_bot._send_channel(message.channel, "❌ 执行失败，请查看日志")

    def _run_bot(self):
        try:
            self.discord_bot.run()
        except Exception as e:
            self.logger.error(f"Discord Bot 运行失败: {e}")

    # ==================== 每周预告 / 升级件追更 ====================

    def _add_weekly_preview_job(self):
        from apscheduler.triggers.cron import CronTrigger
        from pytz import timezone as _ptz
        self.scheduler.scheduler.add_job(
            func=self._weekly_race_preview,
            trigger=CronTrigger(day_of_week=WEEKLY_PREVIEW_DAY, hour=WEEKLY_PREVIEW_HOUR,
                                minute=0, timezone=_ptz(TIMEZONE)),
            id="weekly_race_preview", replace_existing=True)
        self.logger.info(f"✓ 每周预告任务（周{WEEKLY_PREVIEW_DAY + 1} {WEEKLY_PREVIEW_HOUR:02d}:00）")

    def _weekly_race_preview(self):
        from datetime import timezone as _tzu
        try:
            now = datetime.now(_tzu.utc)
            week_end = now + timedelta(days=7)
            schedule = self.f1_api.get_schedule()
            if not schedule:
                return
            race = None
            for r in schedule:
                race_date = datetime.strptime(r["date"], "%Y-%m-%d").replace(tzinfo=_tzu.utc)
                if now <= race_date <= week_end:
                    race = r
                    break
            if not race:
                return

            sessions = self.f1_api.get_all_sessions(race)
            circuit_id = race.get("Circuit", {}).get("circuitId", "")
            circuit_name = race.get("Circuit", {}).get("circuitName", "")
            circuit_data = None
            if self.circuits_manager:
                circuit_data = self.circuits_manager.get_circuit(circuit_id)
                if not circuit_data:
                    found = self.circuits_manager.search_circuit(circuit_name)
                    if found:
                        circuit_id, circuit_data = found
            if not circuit_data:
                circuit_data = {"name": circuit_name, "flag": "🏁", "lap_record": {}}

            llm_verified = False
            official_checked = False
            if self.circuits_manager and circuit_id:
                try:
                    local_record = circuit_data.get("lap_record", {})
                    official = self.circuits_manager.fetch_official_lap_record(circuit_id, SEASON)
                    if official:
                        official_checked = True
                        local_sec = self.circuits_manager.time_to_seconds(local_record.get("time", ""))
                        official_sec = self.circuits_manager.time_to_seconds(official["time"])
                        if official_sec and official_sec != local_sec:
                            if self.circuits_manager.apply_official_record(circuit_id, official):
                                circuit_data["lap_record"] = official
                                llm_verified = True
                except Exception as e:
                    self.logger.warning(f"官网圈速纪录核查失败: {e}")

            if (not official_checked and WEEKLY_VERIFY_LAP_RECORD
                    and self.llm and self.llm.enabled and self.circuits_manager and circuit_id):
                try:
                    name_en = circuit_data.get("name_en") or circuit_name
                    llm_record = self.llm.verify_lap_record(name_en, circuit_data.get("lap_record", {}))
                    if llm_record and llm_record.get("changed"):
                        if self.circuits_manager.apply_llm_verified_record(circuit_id, llm_record):
                            circuit_data["lap_record"] = {
                                "time": llm_record["time"],
                                "driver": llm_record.get("driver") or llm_record.get("driver_en", ""),
                                "driver_en": llm_record.get("driver_en", ""),
                                "year": llm_record.get("year", ""),
                            }
                            llm_verified = True
                except Exception as e:
                    self.logger.warning(f"LLM 圈速纪录核查失败: {e}")

            self.discord_bot.send_weekly_preview(race, sessions, circuit_data,
                                                 llm_verified=llm_verified)
            self.logger.info(f"✓ 比赛周预告已发送: {race.get('raceName')}")
        except Exception as e:
            self.logger.error(f"比赛周预告失败: {e}")

    UPGRADES_WATCH_MAX_ATTEMPTS = 21

    def _add_upgrades_watch_job(self):
        from apscheduler.triggers.cron import CronTrigger
        from pytz import timezone as _ptz
        self.scheduler.scheduler.add_job(
            func=self._upgrades_watch_step, args=[0],
            trigger=CronTrigger(day_of_week="thu", hour=18, minute=0,
                                timezone=_ptz(TIMEZONE)),
            id="upgrades_watch", replace_existing=True)
        self.logger.info("✓ 升级件追更任务（周四18:00起每2小时探测）")

    def _upgrades_watch_step(self, attempt: int = 0):
        from datetime import timezone as _tzu
        from apscheduler.triggers.date import DateTrigger
        from pytz import timezone as _ptz
        try:
            if not self.f1cosmos:
                return
            now = datetime.now(_tzu.utc)
            race = None
            for r in (self.f1_api.get_schedule() or []):
                race_date = datetime.strptime(r["date"], "%Y-%m-%d").replace(tzinfo=_tzu.utc)
                if now <= race_date <= now + timedelta(days=4):
                    race = r
                    break
            if not race:
                return
            if self.pushed_store and self.pushed_store.is_pushed(SEASON, race["round"], "upgrades"):
                return
            up_text, up_md = self.f1cosmos.format_upgrades_by_gp(race, SEASON, llm=self.llm)
            if up_text and "暂无升级" not in up_text:
                self.discord_bot.send_markdown_message(up_md, fallback_text=up_text)
                if self.pushed_store:
                    self.pushed_store.mark_pushed(SEASON, race["round"], "upgrades")
                return
            if attempt < self.UPGRADES_WATCH_MAX_ATTEMPTS:
                run_time = datetime.now(_ptz(TIMEZONE)) + timedelta(hours=2)
                self.scheduler.scheduler.add_job(
                    func=self._upgrades_watch_step, args=[attempt + 1],
                    trigger=DateTrigger(run_date=run_time),
                    id=f"upgrades_watch_retry_{attempt}", replace_existing=True)
        except Exception as e:
            self.logger.warning(f"升级件追更失败: {e}")

    # ==================== 主循环 ====================

    def _print_upcoming(self):
        try:
            upcoming = self.f1_api.get_upcoming_sessions(hours_ahead=168)
            if upcoming:
                self.logger.info("未来7天环节：")
                for s in upcoming[:5]:
                    self.logger.info(f"  • {s['race_name']} - {s['name']}: {s['datetime']}")
            else:
                self.logger.info("未来7天没有 F1 比赛")
        except Exception as e:
            self.logger.warning(f"获取赛程失败: {e}")

    def _main_loop(self):
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.logger.info("收到停止信号")
        finally:
            self.shutdown()

    def shutdown(self):
        self.running = False
        if self.scheduler:
            try:
                self.scheduler.stop()
            except Exception:
                pass
        self.logger.info("F1 Schedule Bot [Discord] 已关闭")


def main():
    setup_logging()
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    F1DiscordApp().start()


if __name__ == "__main__":
    main()
