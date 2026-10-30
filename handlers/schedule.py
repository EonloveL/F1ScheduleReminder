# -*- coding: utf-8 -*-
"""指令处理：赛程/赛历/分站成绩/天气
（/next /last /calendar /gp /weather——从 main.py 指令分发链原样迁移，行为零变化）"""
import logging
from datetime import datetime

from common import F1API
from common.f1_tools import find_race_for_query, parse_race_query
from common.qq_group_bot import QQGroupBot

logger = logging.getLogger(__name__)


async def handle_next(ctx, message, arg, member_openid, is_dm):
    race = None
    if arg:
        # 指定分站查询：地点/赛道/大奖赛名/R几（如 /next 蒙扎、/next R16、/next 马德里）
        info = parse_race_query(arg)
        race = find_race_for_query(ctx.f1_api, info["year"] or ctx.season,
                                   info["round"], info["gp"])
        if not race:
            await ctx.reply_text(message,
                f"未找到「{arg}」对应的分站\n"
                "用法: /next [地点/赛道/大奖赛名/R几]，如 /next 蒙扎、/next R16、/next 红牛环")
            return
    else:
        race = ctx.f1_api.get_next_race()
    if race:
        sessions = ctx.f1_api.get_all_sessions(race)
        text, md = QQGroupBot.format_next_race(race, sessions)
        await ctx.reply_md(message, text, md)
    else:
        await ctx.reply_text(message, "本赛季已无剩余比赛")


async def handle_last(ctx, message, arg, member_openid, is_dm):
    type_aliases = ctx.session_arg_aliases
    session_year = None
    if arg:
        session_type = type_aliases.get(arg.lower().replace(" ", ""))
        if not session_type:
            await ctx.reply_text(message, f"未知环节「{arg}」\n可选: fp1/fp2/fp3/qualy/sq/sprint/race")
            return
        session_info = None
        schedule = ctx.f1_api.get_schedule()
        if not schedule:
            await ctx.reply_text(message, "⚠️ 赛程数据暂时不可用，请稍后重试")
            return
        from datetime import timezone as _tzu
        now_utc = datetime.now(_tzu.utc)
        for race in reversed(schedule):
            for s in ctx.f1_api.get_all_sessions(race):
                if s['type'] == session_type and s['datetime'] < now_utc:
                    session_info = s
                    break
            if session_info:
                break
        # 赛季初（1-3月）当前赛季无已结束环节：回退上一赛季赛程（OSS review 修复回灌）
        if not session_info and now_utc.month <= 3:
            prev_schedule = ctx.f1_api.get_schedule_for_year(now_utc.year - 1)
            for race in reversed(prev_schedule or []):
                for s in ctx.f1_api.get_all_sessions(race):
                    if s['type'] == session_type and s['datetime'] < now_utc:
                        session_info = s
                        break
                if session_info:
                    session_year = now_utc.year - 1
                    break
    else:
        session_info = ctx.f1_api.get_last_completed_session()

    if not session_info:
        await ctx.reply_text(message, "未找到已结束的比赛环节")
        return

    await ctx.reply_text(message, f"🔍 正在获取 {session_info['race_name']} {session_info['name']} 成绩...")
    result = ctx.f1_api.get_session_results(int(session_info['round']), session_info['type'],
                                            season=session_year or ctx.season)
    if result and result.get("entries"):
        text, md = QQGroupBot.format_session_result(result)
        await ctx.reply_md(message, text, md)
    else:
        await ctx.reply_text(message, f"暂无 {session_info['name']} 成绩数据")


async def handle_calendar(ctx, message, arg, member_openid, is_dm):
    schedule = ctx.f1_api.get_schedule()
    if schedule:
        text, md = QQGroupBot.format_calendar(schedule)
        ics_note = ("\n\n📅 日历订阅（导入手机日历自动同步全年赛程）:\n"
                    f"{ctx.rating_base_url}/f1_calendar.ics")
        ics_md = (f"\r\r📅 [订阅手机日历版赛程（自动同步/本地时区）]"
                  f"({ctx.rating_base_url}/f1_calendar.ics)")
        await ctx.reply_md(message, text + ics_note, md + ics_md)
    else:
        await ctx.reply_text(message, "暂无赛历数据")


async def handle_gp(ctx, message, arg, member_openid, is_dm):
    f1_api = ctx.f1_api
    SEASON = ctx.season
    if not arg:
        await ctx.reply_text(message, "用法: /gp 地点 [年份] [环节]\n如: /gp 蒙扎\n/gp 斯帕 2022\n/gp 比利时 2021 sprint\n环节可选: fp1/fp2/fp3/sq/sprint/qualy/race")
        return

    tokens = arg.split()
    year = None
    session_type = None
    kw_parts = []
    for t in tokens:
        tl = t.lower()
        if t.isdigit() and len(t) == 4:
            year = int(t)
        elif tl in ctx.session_arg_aliases:
            session_type = ctx.session_arg_aliases[tl]
        else:
            kw_parts.append(t)
    keyword = "".join(kw_parts)

    if not keyword:
        await ctx.reply_text(message, "用法: /gp 地点 [年份] [环节]\n如: /gp 蒙扎 race")
        return

    import asyncio as _aio
    from functools import partial as _partial
    from common.f1_api import find_race_by_circuit
    loop = _aio.get_running_loop()
    target_year = year or SEASON

    schedule = await loop.run_in_executor(None, _partial(f1_api.get_schedule_for_year, target_year))
    if not schedule:
        await ctx.reply_text(message, f"❌ 无法获取 {target_year} 赛季赛程")
        return

    race = find_race_by_circuit(schedule, keyword)
    if not race:
        await ctx.reply_text(message, f"❌ {target_year} 赛季未找到与「{keyword}」匹配的分站\n可试试赛道/城市/国家名，如: 蒙扎、斯帕、银石、铃鹿")
        return

    round_num = int(race["round"])

    if session_type:
        await ctx.reply_text(message, f"🔍 正在获取 {race['raceName']} {F1API.SESSION_NAMES_CN.get(session_type, session_type)} 成绩…")
        result = await loop.run_in_executor(
            None, _partial(f1_api.get_session_results, round_num, session_type, season=target_year))
        if result and result.get("entries"):
            text, md = QQGroupBot.format_session_result(result)
            await ctx.reply_md(message, text, md)
        else:
            await ctx.reply_text(message, f"暂无 {race['raceName']} {F1API.SESSION_NAMES_CN.get(session_type, session_type)} 成绩数据")
        return

    # 未指定环节：汇总该站已结束环节的领奖台
    await ctx.reply_text(message, f"🔍 正在获取 {race['raceName']} 各环节成绩，可能需要几秒…")

    def _fetch_gp_summary():
        from concurrent.futures import ThreadPoolExecutor
        from datetime import datetime as _dt3, timezone as _tz2
        sessions = f1_api.get_all_sessions(race)
        now_utc = _dt3.now(_tz2.utc)
        # 环节 datetime 可能为 None（f1api.dev 个别分站缺环节日期，
        # parse_session_datetime 返回 None）——None < datetime 直接 TypeError
        completed = [s for s in sessions if s["datetime"] and s["datetime"] < now_utc]
        results_map = {}
        with ThreadPoolExecutor(max_workers=5) as pool:
            futs = {
                pool.submit(f1_api.get_session_results, int(s["round"]), s["type"], 2, target_year): s
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

    session_results = await loop.run_in_executor(None, _fetch_gp_summary)
    text, md = QQGroupBot.format_gp_summary(race, target_year, session_results)
    await ctx.reply_md(message, text, md)


async def handle_weather(ctx, message, arg, member_openid, is_dm):
    # 分站天气/赛道温度/风速风向：/weather [地点/赛道/R几]，缺省下一站
    f1_api = ctx.f1_api
    SEASON = ctx.season
    import asyncio as _aio
    loop = _aio.get_running_loop()

    def _fetch_weather():
        from common.weather_api import WeatherAPI
        race = None
        if arg:
            info = parse_race_query(arg)
            race = find_race_for_query(f1_api, info["year"] or SEASON,
                                       info["round"], info["gp"])
            if not race:
                return None, None
        else:
            race = f1_api.get_next_race()
        if not race:
            return None, None
        sessions = f1_api.get_all_sessions(race)
        return WeatherAPI().get_race_weather(race, sessions), race

    result, race_obj = await loop.run_in_executor(None, _fetch_weather)
    if not result:
        head = (f"未找到「{arg}」对应的分站" if arg else "本赛季已无剩余比赛")
        await ctx.reply_text(message,
            head + "\n用法: /weather [地点/赛道/R几]，如 /weather 蒙扎、/weather R14")
        return
    if result.get("error"):
        await ctx.reply_text(message, f"⚠️ 天气数据获取失败：{result['error']}")
        return
    text, md = QQGroupBot.format_race_weather(result)
    await ctx.reply_md(message, text, md)
    # 天气时间序列图：已完赛=OpenF1 实测，未赛=Open-Meteo 逐小时预报
    try:
        from common.chart_factory import render_weather_chart
        from functools import partial as _pw
        p = await loop.run_in_executor(
            None, _pw(render_weather_chart, SEASON, int(race_obj["round"]),
                      race_obj.get("raceName", ""), f1_api=f1_api))
        if p:
            import os as _os
            await ctx.reply_image(
                message,
                f"{ctx.rating_base_url}/exports/{_os.path.basename(p)}",
                content=f"🌦️ {race_obj.get('raceName')} 天气时间序列")
    except Exception:
        pass


HANDLERS = {
    "next": handle_next,
    "last": handle_last,
    "calendar": handle_calendar,
    "gp": handle_gp,
    "weather": handle_weather,
}
