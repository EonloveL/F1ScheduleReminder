"""
Discord 版斜杠指令层（py-cord）

将 QQ 官方群版（qq_group_official_version/main.py 指令 dispatch 区）的指令集
移植为 Discord slash command：
- 数据逻辑复用 common/ 平台无关模块（f1_api / f1cosmos / chart_factory / f1_tools）
- QQ markdown 卡片经 DiscordBot.convert_md 转换（\\r→\\n、图片语法剥出上传）
- 积分榜/环节成绩因 QQ 版 md 内含头像图片语法（会产生 20 张附件），
  改为 Discord 原生 Embed 自写格式
- QQ 专属机制（msg_seq / keyboard / @全体 / 头像 md / msg_id 过期兜底）不移植

所有 common/ 调用均为同步阻塞，统一经 ThreadPoolExecutor 包装，避免卡事件循环。
"""
import asyncio
import functools
import logging
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone as _tzu
from typing import Any, Dict, List, Optional

import discord

try:
    from bot import DiscordBot, split_text
    from i18n import T, LANG
    import formatting as fmt
except ImportError:  # 包形态（discord_version.commands）运行时的回退
    from discord_version.bot import DiscordBot, split_text
    from discord_version.i18n import T, LANG
    from discord_version import formatting as fmt

from common.f1_api import find_race_by_circuit
from common.f1_tools import parse_race_query, parse_upgrade_query, find_race_for_query
from common.qq_group_bot import QQGroupBot
from common.secret_guard import scrub_text
from common.standings_format import parse_driver_standings, parse_constructor_standings
from common.user_prefs import UserPrefsStore, resolve_driver, resolve_team

logger = logging.getLogger(__name__)

# 环节参数别名（对齐 QQ 版 SESSION_ARG_ALIASES，/last 与 /gp 共用）
SESSION_ARG_ALIASES = {
    "fp1": "fp1", "fp2": "fp2", "fp3": "fp3",
    "qualy": "qualifying", "qualifying": "qualifying", "排位": "qualifying", "排位赛": "qualifying",
    "sprint": "sprint", "冲刺": "sprint", "冲刺赛": "sprint",
    "sq": "sprint_qualifying", "sprintqualy": "sprint_qualifying",
    "冲刺排位": "sprint_qualifying", "冲刺排位赛": "sprint_qualifying",
    "race": "race", "正赛": "race",
}

_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


def _d(zh: str, en: str) -> str:
    """按界面语言取文案（i18n.py 未覆盖的短文案用内联分支）"""
    return zh if LANG == "zh" else en


class CommandContext:
    """指令装配上下文：main.py 注入的依赖 + 并发设施（executor / 用户互斥锁 / 全局信号量）"""

    def __init__(self, f1_api=None, f1cosmos=None, circuits_manager=None, llm=None,
                 prefs_store=None, pushed_store=None, scheduler=None, discord_bot=None,
                 season: int = None, guild_ids: Optional[List[int]] = None,
                 admin_user_ids: Optional[List[str]] = None,
                 ratings_store=None, rating_base_url: str = "",
                 live_base_url: str = "", watch_links: Optional[list] = None):
        self.f1_api = f1_api
        self.f1cosmos = f1cosmos
        self.circuits_manager = circuits_manager
        self.llm = llm
        self.prefs_store = prefs_store
        self.pushed_store = pushed_store
        self.scheduler = scheduler
        self.discord_bot = discord_bot
        self.season = season
        self.guild_ids = guild_ids
        self.admin_user_ids = admin_user_ids or []
        # Web 依赖功能（未配置对应 base URL 时相关指令不注册）
        self.ratings_store = ratings_store
        self.rating_base_url = rating_base_url
        self.live_base_url = live_base_url
        self.watch_links = watch_links or []

        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="f1-cmd")
        # LLM 专用线程池（/ask 长任务最长 600s，若与指令共用 4 线程池，4 个并发 /ask
        # 会饿死全部其他指令——review 高 #3）；容量对齐全局并发信号量
        self.llm_executor = ThreadPoolExecutor(
            max_workers=int(os.getenv("LLM_MAX_CONCURRENCY", "4")), thread_name_prefix="f1-llm")
        # /ask 每用户互斥锁（须在各请求协程内创建，故用 dict 惰性填充）
        self.ask_locks: Dict[str, asyncio.Lock] = {}
        self._ask_semaphore: Optional[asyncio.Semaphore] = None
        # AI 工具 schema 与 handler（register_commands 时构建一次）
        self.f1_tools_schema: List[Dict[str, Any]] = []
        self.f1_handlers: Optional[Dict[str, Any]] = None

    def get_ask_semaphore(self) -> asyncio.Semaphore:
        """全局 LLM 并发信号量（惰性创建，保证绑定运行中的事件循环）"""
        if self._ask_semaphore is None:
            self._ask_semaphore = asyncio.Semaphore(int(os.getenv("LLM_MAX_CONCURRENCY", "4")))
        return self._ask_semaphore


# ==================== 通用收发与格式化 ====================

async def _run(cc: CommandContext, fn, *args, **kwargs):
    """同步阻塞调用包装到线程池；120s 兜底超时防后端挂死拖垮事件循环等待链"""
    loop = asyncio.get_running_loop()
    return await asyncio.wait_for(
        loop.run_in_executor(cc.executor, functools.partial(fn, *args, **kwargs)),
        timeout=120)


async def _run_llm(cc: CommandContext, fn, *args, **kwargs):
    """LLM 调用走专用线程池（不占指令线程池；超时由 LLM_ASK_HARD_TIMEOUT 语义控制）"""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(cc.llm_executor, functools.partial(fn, *args, **kwargs))


async def _reply_text(ictx: discord.ApplicationContext, text: str):
    """长文本按行切块逐条发送（出站过 secret_guard 脱敏）"""
    for chunk in split_text(scrub_text(text or T("no_data"))):
        await ictx.followup.send(content=chunk)


async def _reply_qq_md(ictx: discord.ApplicationContext, md: str, fallback_text: str = None):
    """QQ markdown 卡片 → Discord：convert_md 转换文本、剥出的图片建 files/embeds 随首块发送"""
    text, images = DiscordBot.convert_md(md or "")
    if not text:
        text = fallback_text or ""
    text = scrub_text(text)  # 出网门禁脱敏
    files, embeds = DiscordBot._build_image_payload(images)
    chunks = split_text(text) if text else []
    if not chunks and not files and not embeds:
        await ictx.followup.send(content=T("no_data"))
        return
    if not chunks:
        chunks = [""]
    for i, chunk in enumerate(chunks):
        kwargs: Dict[str, Any] = {}
        if chunk:
            kwargs["content"] = chunk
        if i == 0:
            if files:
                kwargs["files"] = files
            if embeds:
                kwargs["embeds"] = embeds
        if not kwargs:
            kwargs["content"] = "…"
        await ictx.followup.send(**kwargs)


async def _reply_image(ictx: discord.ApplicationContext, path: str, content: str):
    """本地 PNG 上传 + attachment 引用"""
    fname = os.path.basename(path)
    await ictx.followup.send(
        content=content,
        file=discord.File(path, filename=fname),
        embed=discord.Embed().set_image(url=f"attachment://{fname}"))


async def _reply_error(ictx: discord.ApplicationContext):
    try:
        await ictx.followup.send(content=T("error_retry"))
    except Exception:
        pass


def _session_result_embed(result: Dict[str, Any]) -> discord.Embed:
    """环节成绩 Embed（自写格式，QQ 版 format_session_result 含头像 md 不复用）"""
    lines = []
    for e in result.get("entries", []):
        pos = e.get("position")
        rank = _MEDALS.get(pos, f"**{pos}.**")
        tstr = f" `{e['time']}`" if e.get("time") else ""
        pts = f" (+{e['points']} {T('points_unit')})" if e.get("points") else ""
        lines.append(f"{rank} **{e.get('driver_name', '?')}**{tstr}{pts}")
    return discord.Embed(
        title=T("session_result_title", session=result.get("session_name", "")),
        description=(f"> {result.get('race_name', '')}\n" + "\n".join(lines))[:3900],
        color=discord.Color.blue())


def _driver_standings_embed(entries: list, provisional: Optional[dict] = None) -> discord.Embed:
    """车手积分榜 Embed（自写格式，含预估口径标注）"""
    lines = []
    for e in entries:
        pos = int(e.get("position", 0))
        name = e.get("Driver", {}).get("familyName", "Unknown")
        points = e.get("points", "0")
        wins = e.get("wins", "0")
        rank = _MEDALS.get(pos, f"**{pos}.**")
        win_str = f" {T('wins_fmt', wins=wins)}" if wins != "0" else ""
        lines.append(f"{rank} **{name}** — {points} {T('points_unit')}{win_str}")
    emb = discord.Embed(title=T("driver_standings"),
                        description="\n".join(lines)[:3900],
                        color=discord.Color.blue())
    if provisional:
        emb.set_footer(text=T("provisional_note", note=provisional.get("note", "")))
    return emb


def _constructor_standings_embed(entries: list, provisional: Optional[dict] = None) -> discord.Embed:
    """车队积分榜 Embed"""
    lines = []
    for e in entries:
        pos = int(e.get("position", 0))
        name = e.get("Constructor", {}).get("name", "Unknown")
        points = e.get("points", "0")
        wins = e.get("wins", "0")
        rank = _MEDALS.get(pos, f"**{pos}.**")
        win_str = f" {T('wins_fmt', wins=wins)}" if wins != "0" else ""
        lines.append(f"{rank} **{name}** — {points} {T('points_unit')}{win_str}")
    emb = discord.Embed(title=T("constructor_standings"),
                        description="\n".join(lines)[:3900],
                        color=discord.Color.blue())
    if provisional:
        emb.set_footer(text=T("provisional_note", note=provisional.get("note", "")))
    return emb


# ==================== 分站解析（图表指令共用） ====================

def _resolve_race_for_chart(f1_api, season: int, arg: Optional[str]):
    """图表类指令通用分站解析：R# / 地点模糊 / 缺省=最近一场已赛。
    Returns: (race, started) — race=None 未找到；started=False 分站未赛"""
    schedule = f1_api.get_schedule_for_year(season) or []
    race = None
    if arg:
        m = re.search(r"^[Rr]\s?0*(\d{1,2})$", arg.strip())
        if m:
            race = next((r for r in schedule
                         if int(r.get("round", 0)) == int(m.group(1))), None)
        else:
            race = find_race_by_circuit(schedule, arg)
    else:
        today = datetime.now().date().isoformat()
        past = [r for r in schedule if r.get("date") and r["date"] < today]
        race = past[-1] if past else None
    if not race:
        return None, False
    race_dt = None
    try:
        race_dt = datetime.fromisoformat(
            f"{race['date']}T{race.get('time', '00:00:00Z').replace('Z', '')}"
        ).replace(tzinfo=_tzu.utc)
    except Exception:
        pass
    started = not (race_dt and datetime.now(_tzu.utc) < race_dt)
    return race, started


def _driver_entries_for_resolve(cc: CommandContext) -> list:
    """resolve_driver 用的 Ergast 格式条目：当前积分榜优先，休赛期回退 drivers_profile 档案"""
    try:
        standings = cc.f1_api.get_current_standings()
        entries = parse_driver_standings(standings.get("drivers") or {})
        if entries:
            return entries
    except Exception:
        pass
    from common.drivers_profile import get_drivers
    out = []
    for did, d in get_drivers().items():
        name_en = d.get("name_en") or did
        parts = name_en.rsplit(" ", 1)
        out.append({"Driver": {"driverId": did,
                               "givenName": parts[0] if len(parts) > 1 else "",
                               "familyName": parts[-1]}})
    return out


def _team_entries_for_resolve(cc: CommandContext) -> list:
    try:
        standings = cc.f1_api.get_current_standings()
        entries = parse_constructor_standings(standings.get("constructors") or {})
        if entries:
            return entries
    except Exception:
        pass
    from common.drivers_profile import get_teams
    return [{"Constructor": {"constructorId": tid, "name": t.get("name_en") or tid}}
            for tid, t in get_teams().items()]


# ==================== 指令注册 ====================

def register_commands(bot, cc: CommandContext):
    """把全部 slash command 挂到 py-cord Bot 实例上"""

    # AI 工具 schema/handler 构建一次（/ask 与 /predict 共用；handler 内含预测模型单例）
    try:
        from common.f1_tools import F1_TOOLS, build_handlers
        cc.f1_tools_schema = F1_TOOLS
        cc.f1_handlers = build_handlers(cc.f1_api, cc.f1cosmos, cc.season)
    except Exception as e:
        logger.warning(f"F1 工具层构建失败（/ask 与 /predict 降级）: {e}")
        cc.f1_tools_schema = []
        cc.f1_handlers = {}

    # 预测模型后台预热（对齐 QQ 版：首次 predict 冷启动 ~184s 挪到启动阶段，
    # 冠军推演内部逐站 predict 一次预热全部吃热缓存）
    def _warm_prediction_model():
        try:
            cc.f1_handlers["get_championship_outlook"]()
            logger.info("✓ 预测模型预热完成（含剩余分站逐站缓存）")
        except Exception as e:
            logger.warning(f"预测模型预热失败（不影响功能）: {e}")
        try:
            cc.f1_handlers["get_race_prediction"]()
        except Exception:
            pass

    if cc.f1_handlers:
        threading.Thread(target=_warm_prediction_model, daemon=True,
                         name="predict-warmup").start()

    gid = cc.guild_ids

    # ---------------- /help ----------------

    @bot.slash_command(name="help", description=_d("查看全部指令", "Show all commands"),
                       guild_ids=gid)
    async def cmd_help(ictx: discord.ApplicationContext):
        await ictx.defer()
        try:
            emb = discord.Embed(title=T("help_title"), description=T("help_body"),
                                color=discord.Color.red())
            await ictx.followup.send(embed=emb)
        except Exception:
            logger.exception("/help 失败")
            await _reply_error(ictx)

    # ---------------- /next ----------------

    @bot.slash_command(name="next",
                       description=_d("下一站详情（可指定分站/R几）",
                                      "Next race details (optionally a GP/R#)"),
                       guild_ids=gid)
    async def cmd_next(ictx: discord.ApplicationContext,
                       place: discord.Option(str, _d("地点/赛道/大奖赛名/R几（可空）",
                                                     "Place/circuit/GP name/R# (optional)"),
                                             required=False, default=None) = None):
        await ictx.defer()
        try:
            def _fetch():
                race = None
                if place:
                    info = parse_race_query(place)
                    race = find_race_for_query(cc.f1_api, info["year"] or cc.season,
                                               info["round"], info["gp"])
                else:
                    race = cc.f1_api.get_next_race()
                if not race:
                    return None, None
                return race, cc.f1_api.get_all_sessions(race)

            race, sessions = await _run(cc, _fetch)
            if not race:
                await _reply_text(ictx, _d(
                    f"未找到「{place}」对应的分站" if place else "本赛季已无剩余比赛",
                    f"No race matching '{place}'" if place else "No races left this season"))
                return
            # Discord 原生格式化：<t:> 时间戳按每个用户设备时区渲染（不再硬编码北京时间）
            await _reply_text(ictx, fmt.fmt_next_race(race, sessions))
        except Exception:
            logger.exception("/next 失败")
            await _reply_error(ictx)

    # ---------------- /last ----------------

    @bot.slash_command(name="last",
                       description=_d("上一场成绩（可指定环节 fp1/qualy/sprint/race）",
                                      "Latest results (optionally a session)"),
                       guild_ids=gid)
    async def cmd_last(ictx: discord.ApplicationContext,
                       session: discord.Option(str, _d("环节：fp1/fp2/fp3/qualy/sq/sprint/race",
                                                       "Session: fp1/fp2/fp3/qualy/sq/sprint/race"),
                                               required=False, default=None) = None):
        await ictx.defer()
        try:
            session_type = None
            if session:
                session_type = SESSION_ARG_ALIASES.get(session.lower().replace(" ", ""))
                if not session_type:
                    await _reply_text(ictx, _d(
                        f"未知环节「{session}」，可选: fp1/fp2/fp3/qualy/sq/sprint/race",
                        f"Unknown session '{session}'. Options: fp1/fp2/fp3/qualy/sq/sprint/race"))
                    return

            def _fetch_session_info():
                if not session_type:
                    return cc.f1_api.get_last_completed_session()
                schedule = cc.f1_api.get_schedule()
                if not schedule:
                    return None
                now_utc = datetime.now(_tzu.utc)
                for race in reversed(schedule):
                    for s in cc.f1_api.get_all_sessions(race):
                        if s["type"] == session_type and s["datetime"] and s["datetime"] < now_utc:
                            return s
                return None

            session_info = await _run(cc, _fetch_session_info)
            if not session_info:
                await _reply_text(ictx, _d("未找到已结束的比赛环节", "No completed session found"))
                return
            result = await _run(cc, cc.f1_api.get_session_results,
                                int(session_info["round"]), session_info["type"])
            if result and result.get("entries"):
                await ictx.followup.send(embed=_session_result_embed(result))
            else:
                await _reply_text(ictx, _d(
                    f"暂无 {session_info['name']} 成绩数据",
                    f"No results for {session_info['name']} yet"))
        except Exception:
            logger.exception("/last 失败")
            await _reply_error(ictx)

    # ---------------- /calendar ----------------

    @bot.slash_command(name="calendar", description=_d("全年赛历", "Full season calendar"),
                       guild_ids=gid)
    async def cmd_calendar(ictx: discord.ApplicationContext):
        await ictx.defer()
        try:
            schedule = await _run(cc, cc.f1_api.get_schedule)
            if not schedule:
                await _reply_text(ictx, T("no_data"))
                return
            await _reply_text(ictx, fmt.fmt_calendar(schedule))
        except Exception:
            logger.exception("/calendar 失败")
            await _reply_error(ictx)

    # ---------------- /drivers ----------------

    @bot.slash_command(name="drivers", description=_d("车手积分榜", "Driver standings"),
                       guild_ids=gid)
    async def cmd_drivers(ictx: discord.ApplicationContext):
        await ictx.defer()
        try:
            standings, provisional = await _run(cc, cc.f1_api.get_standings_with_projection)
            entries = parse_driver_standings((standings or {}).get("drivers") or {})
            if not entries:
                await _reply_text(ictx, T("no_data"))
                return
            await ictx.followup.send(embed=_driver_standings_embed(entries, provisional))
        except Exception:
            logger.exception("/drivers 失败")
            await _reply_error(ictx)

    # ---------------- /teams ----------------

    @bot.slash_command(name="teams", description=_d("车队积分榜", "Constructor standings"),
                       guild_ids=gid)
    async def cmd_teams(ictx: discord.ApplicationContext):
        await ictx.defer()
        try:
            standings, provisional = await _run(cc, cc.f1_api.get_standings_with_projection)
            entries = parse_constructor_standings((standings or {}).get("constructors") or {})
            if not entries:
                await _reply_text(ictx, T("no_data"))
                return
            await ictx.followup.send(embed=_constructor_standings_embed(entries, provisional))
        except Exception:
            logger.exception("/teams 失败")
            await _reply_error(ictx)

    # ---------------- /gp ----------------

    @bot.slash_command(name="gp",
                       description=_d("任意分站成绩（地点模糊匹配）",
                                      "Results of any GP (fuzzy place match)"),
                       guild_ids=gid)
    async def cmd_gp(ictx: discord.ApplicationContext,
                     place: discord.Option(str, _d("地点/赛道/大奖赛名/R几",
                                                   "Place/circuit/GP name/R#"),
                                           required=True) = None,
                     year: discord.Option(int, _d("年份（可空=当前赛季）",
                                                  "Year (default: current season)"),
                                          required=False, default=None) = None,
                     session: discord.Option(str, _d("环节：fp1/fp2/fp3/qualy/sq/sprint/race",
                                                     "Session: fp1/fp2/fp3/qualy/sq/sprint/race"),
                                             required=False, default=None) = None):
        await ictx.defer()
        try:
            # 对齐 QQ 版参数解析：place 字符串里混入的 4 位年份与环节词也剥离识别
            tokens = (place or "").split()
            kw_parts = []
            session_type = None
            for t in tokens:
                tl = t.lower()
                if t.isdigit() and len(t) == 4 and year is None:
                    year = int(t)
                elif tl in SESSION_ARG_ALIASES and session is None:
                    session_type = SESSION_ARG_ALIASES[tl]
                else:
                    kw_parts.append(t)
            keyword = "".join(kw_parts)
            if session and session_type is None:
                session_type = SESSION_ARG_ALIASES.get(session.lower().replace(" ", ""))
            if session and session_type is None:
                await _reply_text(ictx, _d(
                    f"未知环节「{session}」，可选: fp1/fp2/fp3/qualy/sq/sprint/race",
                    f"Unknown session '{session}'"))
                return
            if not keyword:
                m = re.search(r"^[Rr]\s?0*(\d{1,2})$", (place or "").strip())
                if not m:
                    await _reply_text(ictx, T("usage_gp"))
                    return
            target_year = year or cc.season

            schedule = await _run(cc, cc.f1_api.get_schedule_for_year, target_year)
            if not schedule:
                await _reply_text(ictx, _d(f"❌ 无法获取 {target_year} 赛季赛程",
                                           f"❌ Cannot fetch {target_year} schedule"))
                return
            if keyword:
                race = find_race_by_circuit(schedule, keyword)
            else:
                rnd = int(re.search(r"0*(\d{1,2})", place).group(1))
                race = next((r for r in schedule if int(r.get("round", 0)) == rnd), None)
            if not race:
                await _reply_text(ictx, _d(
                    f"❌ {target_year} 赛季未找到与「{keyword or place}」匹配的分站（可试赛道/城市/国家名）",
                    f"❌ No race matching '{keyword or place}' in {target_year}"))
                return
            round_num = int(race["round"])

            if session_type:
                result = await _run(cc, cc.f1_api.get_session_results,
                                    round_num, session_type, 2, target_year)
                if result and result.get("entries"):
                    await ictx.followup.send(embed=_session_result_embed(result))
                else:
                    sname = cc.f1_api.SESSION_NAMES_CN.get(session_type, session_type)
                    await _reply_text(ictx, _d(
                        f"暂无 {race['raceName']} {sname} 成绩数据",
                        f"No {session_type} results for {race['raceName']} yet"))
                return

            # 未指定环节：汇总该站已结束环节的领奖台（各环节并发抓取）
            def _fetch_gp_summary():
                sessions = cc.f1_api.get_all_sessions(race)
                now_utc = datetime.now(_tzu.utc)
                # 环节 datetime 可能为 None（f1api.dev 个别分站缺环节日期）
                completed = [s for s in sessions if s["datetime"] and s["datetime"] < now_utc]
                results_map = {}
                with ThreadPoolExecutor(max_workers=5) as pool:
                    futs = {
                        pool.submit(cc.f1_api.get_session_results,
                                    int(s["round"]), s["type"], 2, target_year): s
                        for s in completed
                    }
                    for fut, s in futs.items():
                        try:
                            r = fut.result()
                            if r and r.get("entries"):
                                results_map[s["type"]] = (s["name"], r["entries"][:3])
                        except Exception:
                            pass
                ordered = []
                for s in completed:
                    if s["type"] in results_map:
                        ordered.append(results_map[s["type"]])
                return ordered

            session_results = await _run(cc, _fetch_gp_summary)
            await _reply_text(ictx, fmt.fmt_gp_summary(race, target_year, session_results))
        except Exception:
            logger.exception("/gp 失败")
            await _reply_error(ictx)

    # ---------------- /weather ----------------

    @bot.slash_command(name="weather",
                       description=_d("分站天气（缺省下一站）",
                                      "Race weather (default: next race)"),
                       guild_ids=gid)
    async def cmd_weather(ictx: discord.ApplicationContext,
                          gp: discord.Option(str, _d("地点/赛道/R几（可空=下一站）",
                                                     "Place/circuit/R# (default: next race)"),
                                             required=False, default=None) = None):
        await ictx.defer()
        try:
            def _fetch_weather():
                from common.weather_api import WeatherAPI
                race = None
                if gp:
                    info = parse_race_query(gp)
                    race = find_race_for_query(cc.f1_api, info["year"] or cc.season,
                                               info["round"], info["gp"])
                    if not race:
                        return None, None
                else:
                    race = cc.f1_api.get_next_race()
                if not race:
                    return None, None
                sessions = cc.f1_api.get_all_sessions(race)
                return WeatherAPI().get_race_weather(race, sessions), race

            result, race_obj = await _run(cc, _fetch_weather)
            if not result:
                head = (_d(f"未找到「{gp}」对应的分站", f"No race matching '{gp}'")
                        if gp else _d("本赛季已无剩余比赛", "No races left this season"))
                await _reply_text(ictx, head)
                return
            if result.get("error"):
                await _reply_text(ictx, _d(f"⚠️ 天气数据获取失败：{result['error']}",
                                           f"⚠️ Weather fetch failed: {result['error']}"))
                return
            await _reply_text(ictx, fmt.fmt_weather(result))
            # 天气时间序列图：已完赛=OpenF1 实测，未赛=Open-Meteo 预报
            try:
                from common.chart_factory import render_weather_chart
                p = await _run(cc, render_weather_chart, cc.season, int(race_obj["round"]),
                               race_obj.get("raceName", ""), f1_api=cc.f1_api)
                if p:
                    await _reply_image(ictx, p, T("weather_title", race=race_obj.get("raceName", "")))
            except Exception:
                pass
        except Exception:
            logger.exception("/weather 失败")
            await _reply_error(ictx)

    # ---------------- /upgrades ----------------

    @bot.slash_command(name="upgrades",
                       description=_d("升级件汇总（车队/分站/年份组合查询）",
                                      "Upgrade summary (team/GP/year)"),
                       guild_ids=gid)
    async def cmd_upgrades(ictx: discord.ApplicationContext,
                           query: discord.Option(str, _d("车队/地点/R几（可空=下一站）",
                                                         "Team/place/R# (default: next race)"),
                                                 required=False, default=None) = None,
                           year: discord.Option(int, _d("年份（可空=当前赛季）",
                                                        "Year (default: current season)"),
                                                required=False, default=None) = None):
        await ictx.defer()
        try:
            if not cc.f1cosmos:
                await _reply_text(ictx, _d("升级件数据源未启用", "Upgrade data source disabled"))
                return
            # 组合成 QQ 版同款自由串走 parse_upgrade_query（组合查询逻辑照抄）
            arg = ((query or "").strip() + (f" {year}" if year else "")).strip()
            info = parse_upgrade_query(arg)
            season = info["year"] or cc.season

            def _fetch_upgrades():
                race = None
                if info["round"] or info["gp"]:
                    race = find_race_for_query(cc.f1_api, season, info["round"], info["gp"])
                    if not race:
                        return ("", "")  # 指定了分站但找不到，不报车队赛季数据误导
                if info["team"]:
                    if race:
                        return cc.f1cosmos.format_team_gp_upgrades(info["team"], race, season,
                                                                   llm=cc.llm)
                    return cc.f1cosmos.format_upgrades_by_team_summary(info["team"], season,
                                                                       llm=cc.llm)
                if race:
                    return cc.f1cosmos.format_upgrades_by_gp(race, season, llm=cc.llm)
                if info["year"] and not race and not info["team"]:
                    # 纯年份：赛季升级汇总（全量申报数 + 重点大升级）
                    res = (cc.f1_handlers or {}).get("get_upgrades", lambda **_k: {})(
                        gp_or_team="", year=info["year"])
                    if "team_upgrade_counts" in res:
                        return QQGroupBot.format_upgrade_season_summary(res)
                    return (res.get("error") or res.get("note") or "", "")
                race = cc.f1_api.get_next_race()
                if not race:
                    return (_d("本赛季已无剩余比赛", "No races left this season"), "")
                text, md = cc.f1cosmos.format_upgrades_by_gp(race, cc.season, llm=cc.llm)
                if "暂无升级" in text and hasattr(cc.f1cosmos, "current_event_name"):
                    # 本周升级文档通常周五才发布，未发布时展示最近已发布分站数据
                    cur = cc.f1cosmos.current_event_name()
                    if cur and cur != race.get("raceName"):
                        cur_race = {"raceName": cur, "Circuit": {"Location": {"locality": ""}}}
                        t2, m2 = cc.f1cosmos.format_upgrades_by_gp(cur_race, cc.season, llm=cc.llm)
                        if "暂无升级" not in t2:
                            note = _d(
                                f"（本周{race.get('raceName')}升级文档尚未发布，以下为最近已发布分站数据）",
                                f"(This week's {race.get('raceName')} upgrade doc not published yet; showing latest event)")
                            return note + "\n" + t2, f"> {note}\r\r" + m2
                return text, md

            text, md = await _run(cc, _fetch_upgrades)
            if not text:
                await _reply_text(ictx, _d(
                    f"未找到「{arg}」对应的分站或车队（用法: /upgrades [车队/地点/R几] [年份]）",
                    f"No race/team matching '{arg}'"))
            else:
                await _reply_qq_md(ictx, md, fallback_text=text)
        except Exception:
            logger.exception("/upgrades 失败")
            await _reply_error(ictx)

    # ---------------- /pu ----------------

    @bot.slash_command(name="pu",
                       description=_d("动力单元部件用量（含堆叠柱图）",
                                      "Power unit element usage (with chart)"),
                       guild_ids=gid)
    async def cmd_pu(ictx: discord.ApplicationContext,
                     query: discord.Option(str, _d("车手/车队（可空=全部）",
                                                   "Driver/team (default: all)"),
                                           required=False, default=None) = None,
                     year: discord.Option(int, _d("年份（可空=当前赛季）",
                                                  "Year (default: current season)"),
                                          required=False, default=None) = None):
        await ictx.defer()
        try:
            if not cc.f1cosmos:
                await _reply_text(ictx, _d("数据源未启用", "Data source disabled"))
                return
            # 兼容 QQ 版「年份混在查询串里」的写法
            pu_year = year
            pu_q = (query or "").strip()
            m = re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", pu_q)
            if m and pu_year is None:
                pu_year = int(m.group(1))
                pu_q = (pu_q[:m.start()] + pu_q[m.end():]).strip()

            text, md = await _run(cc, cc.f1cosmos.format_pu_quota, pu_q, pu_year or cc.season)
            if not md:
                await _reply_text(ictx, _d(
                    f"未找到「{query}」对应的车手或车队" if query else T("no_data"),
                    f"No driver/team matching '{query}'" if query else T("no_data")))
                return
            await _reply_qq_md(ictx, md, fallback_text=text)
            # 当前赛季附 PU 部件用量堆叠柱图（历史赛季无图）
            if not pu_year or pu_year == cc.season:
                try:
                    from common.chart_factory import render_pu_usage_chart
                    p = await _run(cc, render_pu_usage_chart, cc.season, f1cosmos=cc.f1cosmos)
                    if p:
                        await _reply_image(ictx, p, _d(
                            f"🔋 {cc.season} 动力单元部件用量统计图",
                            f"🔋 {cc.season} Power Unit element usage"))
                except Exception:
                    pass
        except Exception:
            logger.exception("/pu 失败")
            await _reply_error(ictx)

    # ---------------- /pitstops ----------------

    @bot.slash_command(name="pitstops",
                       description=_d("进站时长车队对比箱线图", "Pit stop duration boxplot"),
                       guild_ids=gid)
    async def cmd_pitstops(ictx: discord.ApplicationContext,
                           gp: discord.Option(str, _d("地点/R几（可空=最近已赛）",
                                                      "Place/R# (default: latest race)"),
                                              required=False, default=None) = None):
        await ictx.defer()
        try:
            def _gen():
                from common.chart_factory import render_pitstop_chart
                race, started = _resolve_race_for_chart(cc.f1_api, cc.season, gp)
                if not race or not started:
                    return race, None
                return race, render_pitstop_chart(cc.season, int(race["round"]),
                                                  race.get("raceName", ""), f1_api=cc.f1_api)

            race, chart_path = await _run(cc, _gen)
            if race is None:
                await _reply_text(ictx, _d(f"未找到分站「{gp}」" if gp else T("no_data"),
                                           f"No race matching '{gp}'" if gp else T("no_data")))
            elif chart_path is None:
                await _reply_text(ictx, _d(
                    f"{race.get('raceName')} 暂无进站数据（分站未赛或无有效停站）",
                    f"{race.get('raceName')}: no pit stop data (not raced or no valid stops)"))
            else:
                await _reply_image(ictx, chart_path, _d(
                    f"🔧 {race.get('raceName')} 进站时长车队对比",
                    f"🔧 {race.get('raceName')} pit stop comparison"))
        except Exception:
            logger.exception("/pitstops 失败")
            await _reply_error(ictx)

    # ---------------- /sc ----------------

    @bot.slash_command(name="sc",
                       description=_d("安全车/VSC/红旗事件时间线",
                                      "Safety car / VSC / red flag timeline"),
                       guild_ids=gid)
    async def cmd_sc(ictx: discord.ApplicationContext,
                     gp: discord.Option(str, _d("地点/R几（可空=最近已赛）",
                                                "Place/R# (default: latest race)"),
                                        required=False, default=None) = None):
        await ictx.defer()
        try:
            def _gen():
                from common.chart_factory import render_sc_timeline
                race, started = _resolve_race_for_chart(cc.f1_api, cc.season, gp)
                if not race or not started:
                    return race, None
                return race, render_sc_timeline(cc.season, int(race["round"]),
                                                race.get("raceName", ""), f1_api=cc.f1_api)

            race, chart_path = await _run(cc, _gen)
            if race is None:
                await _reply_text(ictx, _d(f"未找到分站「{gp}」" if gp else T("no_data"),
                                           f"No race matching '{gp}'" if gp else T("no_data")))
            elif chart_path is None:
                # 区分"数据不可用"与"确认无事件"（双源皆断时禁止误报干净）
                from common.chart_factory import get_sc_events
                evts = await _run(cc, get_sc_events, cc.season, int(race["round"]), cc.f1_api)
                if evts is None:
                    await _reply_text(ictx, _d(
                        f"{race.get('raceName')} 安全车数据源当前不可用，请赛后重试",
                        f"{race.get('raceName')}: safety car data source unavailable, retry later"))
                else:
                    await _reply_text(ictx, _d(
                        f"{race.get('raceName')} 本站无安全车/VSC/红旗事件",
                        f"{race.get('raceName')}: no SC/VSC/red flag events"))
            else:
                await _reply_image(ictx, chart_path, _d(
                    f"🚩 {race.get('raceName')} 安全车事件时间线",
                    f"🚩 {race.get('raceName')} safety car timeline"))
        except Exception:
            logger.exception("/sc 失败")
            await _reply_error(ictx)

    # ---------------- /qualigap ----------------

    @bot.slash_command(name="qualigap",
                       description=_d("排位差距图（杆位金色）", "Qualifying gap chart"),
                       guild_ids=gid)
    async def cmd_qualigap(ictx: discord.ApplicationContext,
                           gp: discord.Option(str, _d("地点/R几（可空=最近已赛；地名+空格+sq=冲刺排位）",
                                                      "Place/R# (append ' sq' for sprint quali)"),
                                              required=False, default=None) = None):
        await ictx.defer()
        try:
            q = (gp or "").strip()
            stype = "qualifying"
            if q.lower() == "sq":
                stype, q = "sprint_qualifying", ""
            elif q.lower().endswith(" sq"):
                stype, q = "sprint_qualifying", q[:-3].strip()

            def _gen():
                from common.chart_factory import render_quali_gap_chart
                race, started = _resolve_race_for_chart(cc.f1_api, cc.season, q or None)
                if not race or not started:
                    return race, None
                return race, render_quali_gap_chart(cc.season, int(race["round"]),
                                                    race.get("raceName", ""), f1_api=cc.f1_api,
                                                    session_type=stype)

            race, chart_path = await _run(cc, _gen)
            tag = _d("冲刺排位", "Sprint quali") if stype == "sprint_qualifying" else _d("排位", "Quali")
            if race is None:
                await _reply_text(ictx, _d(f"未找到分站「{gp}」" if gp else T("no_data"),
                                           f"No race matching '{gp}'" if gp else T("no_data")))
            elif chart_path is None:
                await _reply_text(ictx, _d(
                    f"{race.get('raceName')} 暂无{tag}成绩（分站未赛或{tag}未进行）",
                    f"{race.get('raceName')}: no {tag} results yet"))
            else:
                await _reply_image(ictx, chart_path,
                                   f"⏱️ {race.get('raceName')} {tag}")
        except Exception:
            logger.exception("/qualigap 失败")
            await _reply_error(ictx)

    # ---------------- /racechart ----------------

    @bot.slash_command(name="racechart",
                       description=_d("赛后三联图（pace/策略/名次变化）",
                                      "Race report chart (pace/strategy/positions)"),
                       guild_ids=gid)
    async def cmd_racechart(ictx: discord.ApplicationContext,
                            gp: discord.Option(str, _d("地点/R几（可空=最近已赛）",
                                                       "Place/R# (default: latest race)"),
                                               required=False, default=None) = None):
        await ictx.defer()
        try:
            def _gen():
                from common.chart_factory import render_race_report_chart
                race, started = _resolve_race_for_chart(cc.f1_api, cc.season, gp)
                if not race or not started:
                    return race, None
                return race, render_race_report_chart(cc.season, int(race["round"]),
                                                      race.get("raceName", ""), f1_api=cc.f1_api)

            race, chart_path = await _run(cc, _gen)
            if race is None:
                await _reply_text(ictx, _d(f"未找到分站「{gp}」" if gp else T("no_data"),
                                           f"No race matching '{gp}'" if gp else T("no_data")))
            elif chart_path is None:
                await _reply_text(ictx, _d(
                    f"{race.get('raceName')} 暂无统计数据（分站未赛或数据未收录）",
                    f"{race.get('raceName')}: no stats data yet"))
            else:
                await _reply_image(ictx, chart_path, _d(
                    f"📊 {race.get('raceName')} 赛后统计图（Pace/策略/名次变化）",
                    f"📊 {race.get('raceName')} race report (pace/strategy/positions)"))
        except Exception:
            logger.exception("/racechart 失败")
            await _reply_error(ictx)

    # ---------------- /trend ----------------

    @bot.slash_command(name="trend",
                       description=_d("赛季积分走势图", "Season points trend chart"),
                       guild_ids=gid)
    async def cmd_trend(ictx: discord.ApplicationContext):
        await ictx.defer()
        try:
            from common.chart_factory import render_points_trend_chart
            chart_path = await _run(cc, render_points_trend_chart, cc.season, f1_api=cc.f1_api)
            if not chart_path:
                await _reply_text(ictx, _d("积分走势数据不足（赛季前3站后可用）",
                                           "Not enough data yet (available after round 3)"))
            else:
                await _reply_image(ictx, chart_path, _d(
                    f"📈 {cc.season} 车手积分走势（逐站累计，含冲刺赛）",
                    f"📈 {cc.season} driver points trend (cumulative, sprints included)"))
        except Exception:
            logger.exception("/trend 失败")
            await _reply_error(ictx)

    # ---------------- /pace ----------------

    @bot.slash_command(name="pace",
                       description=_d("正赛 pace 分布图", "Race pace distribution chart"),
                       guild_ids=gid)
    async def cmd_pace(ictx: discord.ApplicationContext,
                       gp: discord.Option(str, _d("地点/R几（可空=最近已赛）",
                                                  "Place/R# (default: latest race)"),
                                          required=False, default=None) = None):
        await ictx.defer()
        try:
            def _gen():
                from common.chart_factory import render_pace_chart
                race, started = _resolve_race_for_chart(cc.f1_api, cc.season, gp)
                if not race or not started:
                    return race, None
                return race, render_pace_chart(cc.season, int(race["round"]),
                                               race.get("raceName", ""), f1_api=cc.f1_api)

            race, chart_path = await _run(cc, _gen)
            if race is None:
                await _reply_text(ictx, _d(f"未找到分站「{gp}」" if gp else T("no_data"),
                                           f"No race matching '{gp}'" if gp else T("no_data")))
            elif chart_path is None:
                await _reply_text(ictx, _d(
                    f"{race.get('raceName')} 暂无 pace 数据（分站未赛或 OpenF1 未收录）",
                    f"{race.get('raceName')}: no pace data (not raced or not in OpenF1)"))
            else:
                await _reply_image(ictx, chart_path, _d(
                    f"📊 {race.get('raceName')} 正赛 Pace 分布统计图",
                    f"📊 {race.get('raceName')} race pace distribution"))
        except Exception:
            logger.exception("/pace 失败")
            await _reply_error(ictx)

    # ---------------- /car ----------------

    @bot.slash_command(name="car",
                       description=_d("事故维修成本（车手榜+车队榜+柱图）",
                                      "Crash repair costs (with chart)"),
                       guild_ids=gid)
    async def cmd_car(ictx: discord.ApplicationContext,
                      query: discord.Option(str, _d("车手/车队（可空=全部）",
                                                    "Driver/team (default: all)"),
                                            required=False, default=None) = None):
        await ictx.defer()
        try:
            if not cc.f1cosmos:
                await _reply_text(ictx, _d("数据源未启用", "Data source disabled"))
                return
            text, md = await _run(cc, cc.f1cosmos.format_destructors, query or "", cc.season)
            await _reply_qq_md(ictx, md, fallback_text=text)
            try:
                from common.chart_factory import render_destructors_chart
                p = await _run(cc, render_destructors_chart, cc.season, f1cosmos=cc.f1cosmos)
                if p:
                    await _reply_image(ictx, p, _d(
                        f"💥 {cc.season} 事故维修成本统计图",
                        f"💥 {cc.season} crash repair cost chart"))
            except Exception:
                pass
        except Exception:
            logger.exception("/car 失败")
            await _reply_error(ictx)

    # ---------------- /predict ----------------

    @bot.slash_command(name="predict",
                       description=_d("统计模型名次预测（不走 AI）",
                                      "Statistical model prediction (no AI)"),
                       guild_ids=gid)
    async def cmd_predict(ictx: discord.ApplicationContext,
                          gp: discord.Option(str, _d("地点/R几（可空=下一站）",
                                                     "Place/R# (default: next race)"),
                                             required=False, default=None) = None):
        await ictx.defer()
        try:
            await ictx.followup.send(_d(
                "📊 正在运行统计预测模型（名次制回归+蒙特卡洛）…",
                "📊 Running statistical prediction model (position regression + Monte Carlo)…"))

            def _fetch_prediction():
                # 经 handler 拿预测结果：内部是进程内单例模型（启动时已后台预热）
                handler = (cc.f1_handlers or {}).get("get_race_prediction")
                if not handler:
                    return {"error": _d("预测模型不可用", "Prediction model unavailable")}
                return handler(gp or "")

            r = await _run(cc, _fetch_prediction)
            if not r or "error" in r:
                await _reply_text(ictx, _d(f"预测失败：{(r or {}).get('error', '未知错误')}",
                                           f"Prediction failed: {(r or {}).get('error', 'unknown')}"))
                return

            meta = r.get("model_meta", {})
            bt = meta.get("backtest") or {}
            lines = [_d(f"🔮 **{r['gp']} 预测**（统计模型 v{meta.get('version')}）",
                        f"🔮 **{r['gp']} Prediction** (statistical model v{meta.get('version')})"),
                     _d(f"> 回测基准（2025留一法）：领奖台命中 {bt.get('podium_hits_per_race')}/场 · "
                        f"名次MAE {bt.get('mean_abs_pos_error')}",
                        f"> Backtest (2025 leave-one-out): podium hits {bt.get('podium_hits_per_race')}/race · "
                        f"MAE {bt.get('mean_abs_pos_error')}"),
                     "", _d("**正赛预测**", "**Race prediction**")]
            for p in r["predicted_race"][:10]:
                pen = (_d(f"（发车P{p['grid']}，罚退+{p['penalty']}）",
                          f"(grid P{p['grid']}, penalty +{p['penalty']})") if p.get("penalty")
                       else (_d(f"（发车P{p['grid']}）", f"(grid P{p['grid']})")
                             if p["grid"] != p["pos"] else ""))
                lines.append(f"{_MEDALS.get(p['pos'], 'P' + str(p['pos']))} "
                             f"**{p['driver']}** ({p['team']}){pen}")
            lines.append("")
            lines.append(_d("**排位（已结束，真实成绩）**", "**Qualifying (final results)**")
                         if r.get("qualifying_actual")
                         else _d("**排位预测（含Top3概率）**", "**Qualifying prediction (Top3 probability)**"))
            for p in r["predicted_qualifying"][:6]:
                suffix = "" if r.get("qualifying_actual") else f" {p['quali_top3_prob'] * 100:.0f}%"
                lines.append(f"P{p['pos']} **{p['driver']}** ({p['team']}){suffix}")
            lines.append("")
            lines.append(_d(
                "⚠️ 统计学口径：名次 = 名次制线性回归的数学期望；Top3/前十概率 = 1000次蒙特卡洛残差采样频率；"
                "回测基准为2025赛季留一法。安全车/事故/机械故障不可预测",
                "⚠️ Statistical basis: positions = expected values of a position-based linear regression; "
                "Top3/Top10 probabilities = frequencies over 1000 Monte-Carlo residual samples; "
                "backtest = 2025 leave-one-out. Safety cars / crashes / mechanical failures are not predictable"))
            if r.get("sc_warning"):
                lines.append(r["sc_warning"])
            await _reply_text(ictx, "\n".join(lines))
        except Exception:
            logger.exception("/predict 失败")
            await _reply_error(ictx)

    # ---------------- /setdriver ----------------

    @bot.slash_command(name="setdriver",
                       description=_d("关注车手（最多3位，赛后定向推送）",
                                      "Follow a driver (max 3)"),
                       guild_ids=gid)
    async def cmd_setdriver(ictx: discord.ApplicationContext,
                            name: discord.Option(str, _d("车手名（中英文/别名）",
                                                         "Driver name (EN/CN/alias)"),
                                                 required=True) = None):
        await ictx.defer()
        uid = str(ictx.author.id)
        try:
            entries = await _run(cc, _driver_entries_for_resolve, cc)
            result = resolve_driver(name, entries)
            if not result:
                await _reply_text(ictx, T("pref_not_found", kind=T("kind_driver"), query=name))
                return
            driver_id, driver_name = result
            ok = await _run(cc, cc.prefs_store.set_user_driver, uid, driver_id, driver_name)
            if not ok:
                await _reply_text(ictx, T("pref_limit", n=UserPrefsStore.MAX_WATCH))
                return
            names = list(UserPrefsStore.pref_driver_names(
                cc.prefs_store.get_user_prefs(uid)).values())
            sep = "、" if LANG == "zh" else ", "
            msg = T("pref_driver_set", name=driver_name)
            if names:
                msg += "\n" + _d(f"当前关注: {sep.join(names)}", f"Watching: {sep.join(names)}")
            await _reply_text(ictx, msg)
        except Exception:
            logger.exception("/setdriver 失败")
            await _reply_error(ictx)

    # ---------------- /setteam ----------------

    @bot.slash_command(name="setteam",
                       description=_d("关注车队（最多3支，赛后定向推送）",
                                      "Follow a team (max 3)"),
                       guild_ids=gid)
    async def cmd_setteam(ictx: discord.ApplicationContext,
                          name: discord.Option(str, _d("车队名（中英文/别名）",
                                                       "Team name (EN/CN/alias)"),
                                               required=True) = None):
        await ictx.defer()
        uid = str(ictx.author.id)
        try:
            entries = await _run(cc, _team_entries_for_resolve, cc)
            result = resolve_team(name, entries)
            if not result:
                await _reply_text(ictx, T("pref_not_found", kind=T("kind_team"), query=name))
                return
            team_id, team_name = result
            ok = await _run(cc, cc.prefs_store.set_user_team, uid, team_id, team_name)
            if not ok:
                await _reply_text(ictx, T("pref_limit", n=UserPrefsStore.MAX_WATCH))
                return
            names = list(UserPrefsStore.pref_team_names(
                cc.prefs_store.get_user_prefs(uid)).values())
            sep = "、" if LANG == "zh" else ", "
            msg = T("pref_team_set", name=team_name)
            if names:
                msg += "\n" + _d(f"当前关注: {sep.join(names)}", f"Watching: {sep.join(names)}")
            await _reply_text(ictx, msg)
        except Exception:
            logger.exception("/setteam 失败")
            await _reply_error(ictx)

    # ---------------- /me ----------------

    @bot.slash_command(name="me", description=_d("我的偏好", "My preferences"), guild_ids=gid)
    async def cmd_me(ictx: discord.ApplicationContext):
        await ictx.defer()
        uid = str(ictx.author.id)
        try:
            pref = await _run(cc, cc.prefs_store.get_user_prefs, uid)
            if not pref:
                await _reply_text(ictx, T("pref_none"))
                return
            d_names = list(UserPrefsStore.pref_driver_names(pref).values())
            t_names = list(UserPrefsStore.pref_team_names(pref).values())
            news_on = pref.get("news_dm", True)
            sep = "、" if LANG == "zh" else ", "
            lines = []
            if d_names:
                lines.append(_d(f"🏎️ 关注车手（{len(d_names)}/{UserPrefsStore.MAX_WATCH}）: {sep.join(d_names)}",
                                f"🏎️ Drivers ({len(d_names)}/{UserPrefsStore.MAX_WATCH}): {sep.join(d_names)}"))
            if t_names:
                lines.append(_d(f"🏁 关注车队（{len(t_names)}/{UserPrefsStore.MAX_WATCH}）: {sep.join(t_names)}",
                                f"🏁 Teams ({len(t_names)}/{UserPrefsStore.MAX_WATCH}): {sep.join(t_names)}"))
            lines.append(_d(f"📰 定向新闻推送: {'开' if news_on else '关'}（/news on|off）",
                            f"📰 News DM: {'on' if news_on else 'off'} (/news on|off)"))
            tz = pref.get("tz")
            lines.append(T("tz_current", tz=tz) if tz else T("tz_unset"))
            if not d_names and not t_names:
                lines.append(T("pref_none"))
            emb = discord.Embed(title=T("pref_me_title"), description="\n".join(lines),
                                color=discord.Color.green())
            await ictx.followup.send(embed=emb)
        except Exception:
            logger.exception("/me 失败")
            await _reply_error(ictx)

    # ---------------- /tz ----------------

    @bot.slash_command(name="tz",
                       description=_d("设置你的本地时区（AI 问答时间换算用；消息时间戳本就按设备时区显示）",
                                      "Set your timezone (for AI answers; message timestamps always render locally)"),
                       guild_ids=gid)
    async def cmd_tz(ictx: discord.ApplicationContext,
                     zone: discord.Option(str, _d("IANA 时区名，如 Europe/London（留空查看当前）",
                                                  "IANA name e.g. Europe/London (empty = show current)"),
                                          required=False, default=None) = None):
        await ictx.defer(ephemeral=True)
        uid = str(ictx.author.id)
        try:
            z = (zone or "").strip()
            if not z:
                cur = await _run(cc, cc.prefs_store.get_user_tz, uid)
                await ictx.followup.send(T("tz_current", tz=cur) if cur else T("tz_unset"),
                                         ephemeral=True)
                return
            from pytz import all_timezones_set
            if z not in all_timezones_set:
                await ictx.followup.send(T("tz_invalid", tz=z), ephemeral=True)
                return
            await _run(cc, cc.prefs_store.set_user_tz, uid, z)
            await ictx.followup.send(T("tz_set", tz=z), ephemeral=True)
        except Exception:
            logger.exception("/tz 失败")
            await _reply_error(ictx)

    # ---------------- /unbind ----------------

    @bot.slash_command(name="unbind",
                       description=_d("解绑关注（可指定类别/名字，不带参数清全部）",
                                      "Unbind favourites (all by default)"),
                       guild_ids=gid)
    async def cmd_unbind(ictx: discord.ApplicationContext,
                         kind: discord.Option(str, _d("类别（可选）", "Kind (optional)"),
                                              choices=["driver", "team"],
                                              required=False, default=None) = None,
                         name: discord.Option(str, _d("要移除的名字（可选）",
                                                      "Name to remove (optional)"),
                                              required=False, default=None) = None):
        await ictx.defer()
        uid = str(ictx.author.id)
        try:
            store = cc.prefs_store
            if not kind and not name:
                removed = await _run(cc, store.unset_user_pref, uid, "driver", "team")
                if removed:
                    await _reply_text(ictx, T("pref_unbound"))
                else:
                    await _reply_text(ictx, _d("你当前没有偏好绑定", "No preferences bound"))
                return

            if name:
                # 指定移除：在关注列表内按名字/id 匹配，再走档案别名解析
                pref = store.get_user_prefs(uid)
                kinds = [kind] if kind else ["driver", "team"]
                hit_kind = hit_id = hit_name = None
                for k in kinds:
                    names = (UserPrefsStore.pref_driver_names(pref) if k == "driver"
                             else UserPrefsStore.pref_team_names(pref))
                    hit = next((kk for kk, v in names.items()
                                if name.lower() in (kk.lower(), v.lower())), None)
                    if not hit:
                        from common.drivers_profile import get_driver_aliases, get_team_aliases
                        alias_map = get_driver_aliases() if k == "driver" else get_team_aliases()
                        cand = alias_map.get(name)
                        if cand and cand in names:
                            hit = cand
                    if hit:
                        hit_kind, hit_id, hit_name = k, hit, names[hit]
                        break
                if not hit_id:
                    await _reply_text(ictx, _d(
                        f"你的关注列表里没有「{name}」", f"'{name}' is not in your watch list"))
                    return
                ok = await _run(cc, store.remove_user_driver, uid, hit_id) if hit_kind == "driver" \
                    else await _run(cc, store.remove_user_team, uid, hit_id)
                if ok:
                    await _reply_text(ictx, _d(f"✅ 已移除关注: {hit_name}",
                                               f"✅ Removed: {hit_name}"))
                else:
                    await _reply_text(ictx, T("error_retry"))
                return

            removed = await _run(cc, store.unset_user_pref, uid, kind)
            if removed:
                label = T("kind_driver") if kind == "driver" else T("kind_team")
                await _reply_text(ictx, _d(f"✅ 已解绑全部{label}偏好",
                                           f"✅ All {label} preferences unbound"))
            else:
                await _reply_text(ictx, _d("你当前没有对应的偏好绑定",
                                           "No matching preferences bound"))
        except Exception:
            logger.exception("/unbind 失败")
            await _reply_error(ictx)

    # ---------------- /news ----------------

    @bot.slash_command(name="news",
                       description=_d("新闻定向 DM 推送开关", "Toggle targeted news DMs"),
                       guild_ids=gid)
    async def cmd_news(ictx: discord.ApplicationContext,
                       toggle: discord.Option(str, _d("on=开启 / off=关闭", "on / off"),
                                              choices=["on", "off"],
                                              required=False, default=None) = None):
        await ictx.defer()
        uid = str(ictx.author.id)
        try:
            store = cc.prefs_store
            if toggle == "on":
                await _run(cc, store.set_news_dm, uid, True)
                await _reply_text(ictx, T("pref_news_on"))
            elif toggle == "off":
                await _run(cc, store.set_news_dm, uid, False)
                await _reply_text(ictx, T("pref_news_off"))
            else:
                cur = store.news_dm_enabled(uid)
                pref = store.get_user_prefs(uid)
                has_watch = bool(pref.get("driver") or pref.get("team")
                                 or pref.get("drivers") or pref.get("teams"))
                state = "on" if cur else "off"
                hint = "" if has_watch else _d(
                    "\n⚠️ 你还未设置关注对象，先用 /setdriver 或 /setteam 设置",
                    "\n⚠️ No favourites yet — set one with /setdriver or /setteam first")
                await _reply_text(ictx, _d(
                    f"📰 定向新闻推送当前状态：{state}{hint}\n用法: /news on 开启 · /news off 关闭",
                    f"📰 News DM is currently: {state}{hint}\nUsage: /news on · /news off"))
        except Exception:
            logger.exception("/news 失败")
            await _reply_error(ictx)

    # ---------------- /clear ----------------

    @bot.slash_command(name="clear",
                       description=_d("清空 AI 对话上下文", "Clear AI conversation context"),
                       guild_ids=gid)
    async def cmd_clear(ictx: discord.ApplicationContext):
        await ictx.defer()
        uid = str(ictx.author.id)
        try:
            if not cc.llm or not cc.llm.enabled:
                await _reply_text(ictx, T("ask_disabled"))
                return
            n = await _run(cc, cc.llm.clear_context, uid)
            if n:
                await _reply_text(ictx, _d(
                    f"🧹 本轮会话已归档为主题摘要（{n} 轮），核心内容仍会记得，闲聊细节已遗忘\n用 /memory 查看你的记忆构成",
                    f"🧹 Session archived into topic summaries ({n} turns). Key facts are retained; chit-chat forgotten.\nUse /memory to inspect your memory"))
            else:
                await _reply_text(ictx, _d("当前没有进行中的对话上下文",
                                           "No active conversation context"))
        except Exception:
            logger.exception("/clear 失败")
            await _reply_error(ictx)

    # ---------------- /memory ----------------

    @bot.slash_command(name="memory",
                       description=_d("查看/管理你的会话记忆（三层：工作/摘要/锚定）",
                                      "Inspect/manage your conversation memory"),
                       guild_ids=gid)
    async def cmd_memory(ictx: discord.ApplicationContext,
                         action: discord.Option(str, _d("forget <id> 删除条目", "forget <id> to remove"),
                                                required=False, default=None) = None):
        await ictx.defer(ephemeral=True)  # 个人记忆内容，仅本人可见
        uid = str(ictx.author.id)
        try:
            if not cc.llm:
                await ictx.followup.send(T("ask_disabled"), ephemeral=True)
                return
            sub = (action or "").strip()
            if sub.lower().startswith("forget "):
                iid = sub[7:].strip()
                ok = await _run(cc, cc.llm._memory.forget, uid, iid)
                await ictx.followup.send(f"✅ `{iid}`" if ok else f"❌ `{iid}` not found",
                                         ephemeral=True)
            else:
                desc = await _run(cc, cc.llm._memory.describe, uid)
                for chunk in split_text(scrub_text(desc)):
                    await ictx.followup.send(chunk, ephemeral=True)
        except Exception:
            logger.exception("/memory 失败")
            await _reply_error(ictx)

    # ---------------- /ask ----------------

    @bot.slash_command(name="ask",
                       description=_d("向 AI 提问（联网+数据工具，可带图）",
                                      "Ask the AI (web + data tools, image optional)"),
                       guild_ids=gid)
    async def cmd_ask(ictx: discord.ApplicationContext,
                      question: discord.Option(str, _d("你的问题", "Your question"),
                                               required=True),
                      image: discord.Option(discord.Attachment,
                                            _d("可选图片", "Optional image"),
                                            required=False, default=None) = None):
        uid = str(ictx.author.id)
        if not cc.llm or not cc.llm.enabled:
            await ictx.respond(T("ask_disabled"))
            return

        # 先 defer（3s ack），再抢锁：锁等待不阻塞 ack（review 低 #10 竞态修复）
        await ictx.defer()
        # 每用户互斥（上一条未答完拒绝新提问，不排队）；锁须在事件循环内创建
        lock = cc.ask_locks.setdefault(uid, asyncio.Lock())
        try:
            await asyncio.wait_for(lock.acquire(), timeout=0.5)
        except asyncio.TimeoutError:
            await ictx.followup.send(T("ask_busy"))
            return

        try:
            placeholder = await ictx.followup.send(T("ask_thinking"))
            qid = uuid.uuid4().hex[:6]  # 问答链路 trace ID（grep "qid=xxxxxx" 捞全链路）
            answer = None
            try:
                async with cc.get_ask_semaphore():
                    if image is not None and (getattr(image, "content_type", "") or "").startswith("image/"):
                        answer = await _run_llm(cc, cc.llm.ask_vision, question, [image.url],
                                                user_id=uid, qid=qid)
                    else:
                        data_context = (
                            "IMPORTANT: The user-facing language of this bot is English. "
                            "Answer in English (keep proper nouns like driver/team names in English)."
                            if LANG == "en" else None)
                        # 用户时区注入（/tz 设置）：AI 答案里的环节时间按用户时区换算
                        try:
                            utz = cc.prefs_store.get_user_tz(uid) if cc.prefs_store else None
                        except Exception:
                            utz = None
                        if utz:
                            tz_note = (f"User's timezone: {utz}. When mentioning F1 session "
                                       f"times in your answer, convert them to this timezone.")
                            data_context = f"{data_context}\n{tz_note}" if data_context else tz_note
                        meta: Dict[str, Any] = {}
                        answer = await _run_llm(
                            cc, cc.llm.ask_layered, uid, question,
                            cc.f1_tools_schema, cc.f1_handlers,
                            1800, data_context, None, meta, None, qid)
            except Exception:
                logger.exception(f"/ask 失败 qid={qid} (用户 {uid[:8]}...)")
                answer = None

            if not answer:
                try:
                    await placeholder.edit(content=T("ask_failed"))
                except Exception:
                    await _reply_text(ictx, T("ask_failed"))
                return

            # "记住/请记住/remember this" 明示锚定 → L3 长期记忆（不衰减）
            if any(p in question for p in ("记住", "记一下", "请记住", "别忘了",
                                           "remember this", "remember that")):
                try:
                    await _run(cc, cc.llm._memory.mark_anchor, uid, question,
                               "explicit", 3.0)
                except Exception:
                    pass

            text, images = DiscordBot.convert_md(scrub_text(answer))
            files, embeds = DiscordBot._build_image_payload(images)
            chunks = split_text(text) if text else []
            if not chunks and not files and not embeds:
                try:
                    await placeholder.edit(content=T("ask_failed"))
                except Exception:
                    pass
                return
            if not chunks:
                chunks = [""]
            for i, chunk in enumerate(chunks):
                kwargs: Dict[str, Any] = {}
                if chunk:
                    kwargs["content"] = chunk
                if i == 0:
                    if files:
                        kwargs["files"] = files
                    if embeds:
                        kwargs["embeds"] = embeds
                if not kwargs:
                    kwargs["content"] = "…"
                await ictx.followup.send(**kwargs)
            # 思考占位清理：删除失败则改为完成标记
            try:
                await placeholder.delete()
            except Exception:
                try:
                    await placeholder.edit(content="✅")
                except Exception:
                    pass
        finally:
            lock.release()

    # ---------------- Web 依赖指令（条件注册：未配置对应 base URL 则不出现） ----------------

    if cc.ratings_store and cc.rating_base_url:

        def _driver_names_for_race(race: Dict[str, Any]) -> Dict[str, str]:
            try:
                res = cc.f1_api.get_session_results(race["round"], "race", season=race["season"])
                if res:
                    return {e["driver_id"]: e["driver_name"] for e in res["entries"]}
            except Exception:
                pass
            return {}

        @bot.slash_command(name="rate",
                           description=_d("获取本场车手评分链接（仅自己可见）",
                                          "Get your driver-rating link (ephemeral)"),
                           guild_ids=gid)
        async def cmd_rate(ictx: discord.ApplicationContext):
            await ictx.defer(ephemeral=True)  # 专属 token 链接，仅本人可见
            try:
                rs = cc.ratings_store
                races = rs._data["races"]
                open_races = [(k, r) for k, r in races.items() if not r.get("closed")]
                if not open_races:
                    await ictx.followup.send(_d(
                        "当前没有进行中的车手评分\n比赛周会自动创建本场投票，正赛开始1小时后开放打分！",
                        "No open rating right now — voting opens 1h after race start during race weeks."),
                        ephemeral=True)
                    return
                # 优先"已开放"的最高 round；全部 pending 才取最新 pending（对齐 QQ 版背靠背处理）
                now = datetime.now(_tzu.utc)
                def _pending(entry):
                    oa = rs.race_open_at(entry)
                    return bool(oa and now < oa)
                pool = [x for x in open_races if not _pending(x[1])] or \
                       [x for x in open_races if _pending(x[1])]
                pool.sort(key=lambda x: (x[1]["season"], x[1]["round"]))
                race_key, race = pool[-1]
                token = await _run(cc, rs.get_or_create_token, race_key, str(ictx.author.id))
                if not token:
                    await ictx.followup.send(_d("评分链接生成失败，请稍后重试",
                                                "Failed to create rating link, try later"),
                                             ephemeral=True)
                    return
                link = f"{cc.rating_base_url}/r/{race_key}/{token}"
                tip = ""
                oa = rs.race_open_at(race)
                if oa and now < oa:
                    tip = _d(f"\n⏰ 投票将于 <t:{int(oa.timestamp())}:f> 开启，可先收藏链接",
                             f"\n⏰ Voting opens <t:{int(oa.timestamp())}:f> — bookmark the link")
                await ictx.followup.send(_d(
                    f"⭐ **{race['race_name']}** 车手评分\n🖱️ {link}\n可重复进入修改{tip}\n⚠️ 专属链接，请勿转发",
                    f"⭐ **{race['race_name']}** Driver Rating\n🖱️ {link}\nRe-enter anytime to adjust{tip}\n⚠️ Personal link — do not share"),
                    ephemeral=True)
            except Exception:
                logger.exception("/rate 失败")
                await ictx.followup.send(T("error_retry"), ephemeral=True)

        @bot.slash_command(name="ratings",
                           description=_d("查看评分榜（缺省=最新一场）",
                                          "Rating board (default: latest race)"),
                           guild_ids=gid)
        async def cmd_ratings(ictx: discord.ApplicationContext,
                              place: discord.Option(str, _d("地点/R几（可空）", "Place/R# (optional)"),
                                                    required=False, default=None) = None):
            await ictx.defer()
            try:
                rs = cc.ratings_store
                race_key = None
                if place:
                    schedule = await _run(cc, cc.f1_api.get_schedule_for_year, cc.season)
                    hit = find_race_by_circuit(schedule, place) if schedule else None
                    if hit:
                        race_key = rs.race_key(cc.season, hit["round"])
                    if not race_key or not rs.get_race(race_key):
                        await _reply_text(ictx, _d(f"未找到「{place}」的评分记录",
                                                   f"No rating record for \"{place}\""))
                        return
                else:
                    races = rs._data["races"]
                    if not races:
                        await _reply_text(ictx, T("no_data"))
                        return
                    race_key = sorted(races.keys(),
                                      key=lambda k: (races[k]["season"], races[k]["round"]))[-1]
                race = rs.get_race(race_key)
                board = await _run(cc, rs.aggregate, race_key)
                names = await _run(cc, _driver_names_for_race, race)
                status = _d("已截止", "Closed") if race.get("closed") else _d("进行中（实时）", "Live")
                lines = []
                for i, row in enumerate(board, 1):
                    name = names.get(row["driver_id"], row["driver_id"])
                    rank = _MEDALS.get(i, f"**{i}.**")
                    lines.append(f"{rank} **{name}** — {row['avg']} ({row['count']} "
                                 + _d("票", "votes") + ")")
                if not lines:
                    lines.append(T("no_data"))
                if race.get("dotd"):
                    lines.append(_d(f"\n🏆 本场最佳车手：**{names.get(race['dotd'], race['dotd'])}**",
                                    f"\n🏆 DOTD: **{names.get(race['dotd'], race['dotd'])}**"))
                emb = discord.Embed(title=f"📊 {race['race_name']} — " + _d("评分榜", "Ratings"),
                                    description=f"> {status}\n" + "\n".join(lines)[:3900],
                                    color=discord.Color.gold(),
                                    url=f"{cc.rating_base_url}/board/{race_key}")
                await ictx.followup.send(embed=emb)
            except Exception:
                logger.exception("/ratings 失败")
                await _reply_error(ictx)

        @bot.slash_command(name="dotd",
                           description=_d("赛季最佳车手累积榜", "Season DOTD board"),
                           guild_ids=gid)
        async def cmd_dotd(ictx: discord.ApplicationContext):
            await ictx.defer()
            try:
                board = await _run(cc, cc.ratings_store.season_dotd_board, cc.season)
                if not board:
                    await _reply_text(ictx, _d(f"{cc.season}赛季暂无最佳车手记录",
                                               f"No DOTD yet in {cc.season}"))
                    return
                name_map = {}
                try:
                    standings = await _run(cc, cc.f1_api.get_current_standings)
                    for e in parse_driver_standings(standings.get("drivers") or {}):
                        d = e.get("Driver", {})
                        name_map[d.get("driverId")] = d.get("familyName", "")
                except Exception:
                    pass
                lines = []
                for i, (did, count) in enumerate(board, 1):
                    rank = _MEDALS.get(i, f"**{i}.**")
                    lines.append(f"{rank} **{name_map.get(did, did)}** — {count}x")
                emb = discord.Embed(title=_d(f"🏆 {cc.season}赛季最佳车手榜",
                                             f"🏆 {cc.season} DOTD Board"),
                                    description="\n".join(lines), color=discord.Color.gold())
                await ictx.followup.send(embed=emb)
            except Exception:
                logger.exception("/dotd 失败")
                await _reply_error(ictx)

        @bot.slash_command(name="telemetry",
                           description=_d("遥测/圈速/赛果分析面板", "Telemetry & analysis dashboards"),
                           guild_ids=gid)
        async def cmd_telemetry(ictx: discord.ApplicationContext):
            await ictx.defer()
            base = cc.rating_base_url
            cosmos = os.getenv("F1COSMOS_DASH_BASE", "https://f1cosmos.com/zh/dashboard")
            emb = discord.Embed(
                title=_d("📊 F1 遥测与比赛分析", "📊 F1 Telemetry & Analysis"),
                description=_d(
                    f"[🛰️ 遥测对比（速度/油门/刹车曲线）]({base}/telemetry)\n"
                    f"[🎚️ 遥测面板·透明度可调]({base}/telemetry/pro)\n"
                    f"[⏱️ 圈速分析（F1Cosmos）]({cosmos}/laptime)\n"
                    f"[🏁 比赛结果（F1Cosmos）]({cosmos}/result)\n"
                    "页面内可自选年份/分站/环节/车手，支持多车手曲线对比",
                    f"[🛰️ Telemetry compare (speed/throttle/brake)]({base}/telemetry)\n"
                    f"[🎚️ Telemetry panel · adjustable overlay]({base}/telemetry/pro)\n"
                    f"[⏱️ Lap-time analysis (F1Cosmos)]({cosmos}/laptime)\n"
                    f"[🏁 Race results (F1Cosmos)]({cosmos}/result)\n"
                    "Pick year/GP/session/drivers in-page; multi-driver overlays supported"),
                color=discord.Color.blue())
            await ictx.followup.send(embed=emb)

    if cc.live_base_url:

        @bot.slash_command(name="live",
                           description=_d("实时计时面板链接", "Live timing panel link"),
                           guild_ids=gid)
        async def cmd_live(ictx: discord.ApplicationContext):
            await ictx.defer()
            emb = discord.Embed(
                title=_d("📊 F1 实时计时面板", "📊 F1 Live Timing"),
                description=_d(
                    f"[🚥 进入实时计时面板]({cc.live_base_url}/live)\n\n"
                    "比赛周内可用：实时排名/车距、旗帜与安全车事件、天气、最快圈、进站轮胎。\n"
                    "免费替代：[F1Cosmos 实时计时](https://f1cosmos.com/zh/dashboard/live) · "
                    "[f1-dash](https://f1-dash.com/dashboard)\n"
                    "非比赛时段显示「暂无实时数据」属正常。",
                    f"[🚥 Open live timing]({cc.live_base_url}/live)\n\n"
                    "During race weekends: live positions/gaps, flags & SC events, weather, fastest laps, pit stops & tyres.\n"
                    "Free alternatives: [F1Cosmos live](https://f1cosmos.com/zh/dashboard/live) · "
                    "[f1-dash](https://f1-dash.com/dashboard)\n"
                    "\"No live data\" outside sessions is normal."),
                color=discord.Color.green())
            await ictx.followup.send(embed=emb)

    if cc.watch_links:

        @bot.slash_command(name="watch",
                           description=_d("观赛链接", "Where to watch"),
                           guild_ids=gid)
        async def cmd_watch(ictx: discord.ApplicationContext):
            await ictx.defer()
            lines = [f"[📺 {name}]({url})" for name, url in cc.watch_links]
            emb = discord.Embed(title=_d("📺 观赛链接", "📺 Where to Watch"),
                                description="\n".join(lines), color=discord.Color.red())
            await ictx.followup.send(embed=emb)

    logger.info(f"✓ 已注册 {len(bot.pending_application_commands)} 个 slash 指令"
                f"（guild_ids={gid or 'global'}）")
