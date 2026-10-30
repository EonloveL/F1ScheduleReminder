# -*- coding: utf-8 -*-
"""指令处理：升级件
（/upgrades /upgrade——从 main.py 指令分发链原样迁移，行为零变化）"""
import logging

from common.f1_tools import parse_upgrade_query, find_race_for_query
from common.qq_group_bot import QQGroupBot

logger = logging.getLogger(__name__)


async def handle_upgrades(ctx, message, arg, member_openid, is_dm):
    if not ctx.f1cosmos:
        await ctx.reply_text(message, "F1Cosmos数据源未启用")
        return
    f1_api = ctx.f1_api
    f1cosmos = ctx.f1cosmos
    llm = ctx.llm
    SEASON = ctx.season
    import asyncio as _aio
    loop = _aio.get_running_loop()

    # 支持组合查询：年份 + 轮次(R5/第5站) + 车队 + 地点
    # 如 /upgrades 蒙扎 2024、/upgrades R5、/upgrades 法拉利 蒙扎 2024
    info = parse_upgrade_query(arg)
    season = info["year"] or SEASON

    # 历史赛季车队整年查询需逐站解析FIA官方PDF，首次较慢，先回提示
    if info["team"] and not info["gp"] and not info["round"] and season != SEASON:
        await ctx.reply_text(message, f"⏳ 正在抓取{season}赛季FIA官方升级申报文档（首次查询需解析全年PDF，约需半分钟）...")

    def _fetch_upgrades():
        race = None
        if info["round"] or info["gp"]:
            race = find_race_for_query(f1_api, season, info["round"], info["gp"])
            if not race:
                return ("", "")  # 指定了分站但找不到，不报车队赛季数据误导
        if info["team"]:
            if race:
                return f1cosmos.format_team_gp_upgrades(info["team"], race, season, llm=llm)
            return f1cosmos.format_upgrades_by_team_summary(info["team"], season, llm=llm)
        if race:
            return f1cosmos.format_upgrades_by_gp(race, season, llm=llm)
        if info["year"] and not race and not info["team"]:
            # 纯年份：赛季升级汇总（全量申报数 + 重点大升级）
            from common.f1_tools import build_handlers as _bh
            res = _bh(f1_api, f1cosmos, season)["get_upgrades"](
                gp_or_team="", year=info["year"])
            if "team_upgrade_counts" in res:
                return QQGroupBot.format_upgrade_season_summary(res)
            return (res.get("error") or res.get("note") or "", "")
        race = f1_api.get_next_race()
        if not race:
            return ("本赛季已无剩余比赛", "")
        text, md = f1cosmos.format_upgrades_by_gp(race, SEASON, llm=llm)
        if "暂无升级" in text and hasattr(f1cosmos, "current_event_name"):
            # 本周升级文档通常周五才发布，未发布时展示最近已发布分站数据
            cur = f1cosmos.current_event_name()
            if cur and cur != race.get("raceName"):
                cur_race = {"raceName": cur, "Circuit": {"Location": {"locality": ""}}}
                t2, m2 = f1cosmos.format_upgrades_by_gp(cur_race, SEASON, llm=llm)
                if "暂无升级" not in t2:
                    note = f"（本周{race.get('raceName')}升级文档尚未发布，以下为最近已发布分站数据）"
                    return note + "\n" + t2, f"> {note}\r\r" + m2
        return text, md

    text, md = await loop.run_in_executor(None, _fetch_upgrades)
    if not text:
        await ctx.reply_text(message, f"未找到「{arg}」对应的分站或车队\n用法: /upgrades [车队] [地点/R几] [年份]，如 /upgrades 蒙扎、/upgrades R16 2024、/upgrades 法拉利 蒙扎 2024")
    else:
        await ctx.reply_md(message, text, md)


async def handle_upgrade(ctx, message, arg, member_openid, is_dm):
    if not arg:
        await ctx.reply_text(message, "用法: /upgrade 车队/部件 [地点/R几] [年份]\n如: /upgrade 法拉利、/upgrade 红牛 蒙扎 2024、/upgrade 前翼 2023")
        return
    if not ctx.f1cosmos:
        await ctx.reply_text(message, "F1Cosmos数据源未启用")
        return
    f1_api = ctx.f1_api
    f1cosmos = ctx.f1cosmos
    llm = ctx.llm
    SEASON = ctx.season
    import asyncio as _aio
    loop = _aio.get_running_loop()

    info = parse_upgrade_query(arg)
    season = info["year"] or SEASON

    if info["team"] and season != SEASON and not info["gp"] and not info["round"]:
        await ctx.reply_text(message, f"⏳ 正在抓取{season}赛季FIA官方升级申报文档（首次查询需解析全年PDF，约需半分钟）...")

    def _fetch_detail():
        race = None
        if info["round"] or info["gp"]:
            race = find_race_for_query(f1_api, season, info["round"], info["gp"])
            if not race:
                return ("", "")
        if info["team"]:
            if race:
                return f1cosmos.format_team_gp_upgrades(info["team"], race, season, llm=llm)
            return f1cosmos.format_upgrades_by_team(info["team"], season, llm=llm)
        if race:
            return f1cosmos.format_upgrades_by_gp(race, season, llm=llm)
        # 部件查询：用去掉年份/轮次后的原串
        q = info["gp"] or (arg or "").strip()
        return f1cosmos.format_upgrade_by_component(q, season, llm=llm)

    text, md = await loop.run_in_executor(None, _fetch_detail)
    if not text:
        await ctx.reply_text(message, f"未找到「{arg}」相关的升级件数据")
    else:
        await ctx.reply_md(message, text, md)


HANDLERS = {
    "upgrades": handle_upgrades,
    "upgrade": handle_upgrade,
}
