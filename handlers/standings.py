# -*- coding: utf-8 -*-
"""指令处理：积分榜/评分
（/drivers /teams /dotd /ratings /rate——从 main.py 指令分发链原样迁移，行为零变化）"""
import logging

from common.qq_group_bot import QQGroupBot

logger = logging.getLogger(__name__)


async def handle_drivers(ctx, message, arg, member_openid, is_dm):
    import asyncio as _aio
    loop = _aio.get_running_loop()
    standings, provisional = await loop.run_in_executor(
        None, ctx.f1_api.get_standings_with_projection)
    entries = QQGroupBot._parse_driver_standings(standings.get("drivers") or {})
    if entries:
        prov_text = f"\n⚠️ {provisional['note']}" if provisional else ""
        prov_md = f"\r\r> ⚠️ {provisional['note']}" if provisional else ""
        await ctx.reply_md(
            message,
            QQGroupBot.format_driver_standings(QQGroupBot, entries) + prov_text,
            QQGroupBot.format_driver_standings_md(QQGroupBot, entries) + prov_md,
        )
    else:
        await ctx.reply_text(message, "暂无车手积分榜数据")


async def handle_teams(ctx, message, arg, member_openid, is_dm):
    import asyncio as _aio
    loop = _aio.get_running_loop()
    standings, provisional = await loop.run_in_executor(
        None, ctx.f1_api.get_standings_with_projection)
    entries = QQGroupBot._parse_constructor_standings(standings.get("constructors") or {})
    if entries:
        prov_text = f"\n⚠️ {provisional['note']}" if provisional else ""
        prov_md = f"\r\r> ⚠️ {provisional['note']}" if provisional else ""
        await ctx.reply_md(
            message,
            QQGroupBot.format_constructor_standings(QQGroupBot, entries) + prov_text,
            QQGroupBot.format_constructor_standings_md(QQGroupBot, entries) + prov_md,
        )
    else:
        await ctx.reply_text(message, "暂无车队积分榜数据")


async def handle_dotd(ctx, message, arg, member_openid, is_dm):
    if not ctx.ratings_store:
        await ctx.reply_text(message, "车手评分功能未启用")
        return
    SEASON = ctx.season
    ratings_store = ctx.ratings_store
    board = ratings_store.season_dotd_board(SEASON)
    if not board:
        await ctx.reply_text(message, f"{SEASON}赛季暂无最佳车手记録\n每场正赛评分截止后产生一位DOTD")
        return
    # 用积分榜名单做显示名
    name_map = {}
    try:
        standings = ctx.f1_api.get_current_standings()
        for e in QQGroupBot._parse_driver_standings(standings.get("drivers") or {}):
            d = e.get("Driver", {})
            name_map[d.get("driverId")] = d.get("familyName", "")
    except Exception:
        pass
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    md = f"## 🏆 {SEASON}赛季最佳车手榜\r> 每场正赛群友评分选出\r\r"
    text = f"🏆 {SEASON}赛季最佳车手榜\n\n"
    for i, (driver_id, count) in enumerate(board, 1):
        name = name_map.get(driver_id, driver_id)
        rank = medals.get(i, f"**{i}.**")
        md += f"{rank} **{name}** — {count}次\r"
        text += f"{i}. {name} - {count}次\n"
    await ctx.reply_md(message, text, md)


async def handle_ratings(ctx, message, arg, member_openid, is_dm):
    if not ctx.ratings_store:
        await ctx.reply_text(message, "车手评分功能未启用")
        return
    SEASON = ctx.season
    ratings_store = ctx.ratings_store
    race_key = None
    if arg:
        # 按地点找场次
        from common.f1_api import find_race_by_circuit
        import asyncio as _a2
        from functools import partial as _p2
        loop2 = _a2.get_running_loop()
        schedule = await loop2.run_in_executor(None, _p2(ctx.f1_api.get_schedule_for_year, SEASON))
        race_hit = find_race_by_circuit(schedule, arg) if schedule else None
        if race_hit:
            race_key = ratings_store.race_key(SEASON, race_hit["round"])
        if not race_key or not ratings_store.get_race(race_key):
            await ctx.reply_text(message, f"未找到「{arg}」的评分记录")
            return
    else:
        races = dict(ratings_store._data["races"])  # 快照：scheduler 线程并发写防 RuntimeError（OSS 修复回灌）
        if not races:
            await ctx.reply_text(message, "暂无评分记录")
            return
        race_key = sorted(races.keys(), key=lambda k: (races[k]["season"], races[k]["round"]))[-1]

    race = ratings_store.get_race(race_key)
    board = ratings_store.aggregate(race_key)
    driver_names = ctx.get_driver_names_for_race(race)
    status = "已截止" if race.get("closed") else "进行中（实时）"
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    md = f"## 📊 {race['race_name']} 评分榜\r> {status}\r\r"
    text = f"📊 {race['race_name']} 评分榜（{status}）\n\n"
    for i, row in enumerate(board, 1):
        name = driver_names.get(row["driver_id"], row["driver_id"])
        rank = medals.get(i, f"**{i}.**")
        md += f"{rank} **{name}** — {row['avg']}分（{row['count']}票）\r"
        text += f"{i}. {name} - {row['avg']}分（{row['count']}票）\n"
    if not board:
        md += "\r暂无评分"
        text += "暂无评分\n"
    if race.get("dotd"):
        name = driver_names.get(race["dotd"], race["dotd"])
        md += f"\r\r🏆 本场最佳车手：**{name}**"
        text += f"\n🏆 本场最佳车手：{name}"
    board_url = f"{ctx.rating_base_url}/board/{race_key}"
    md += f"\r\r[📈 网页版榜单]({board_url})"
    text += f"\n网页版: {board_url}"
    await ctx.reply_md(message, text, md)


async def handle_rate(ctx, message, arg, member_openid, is_dm):
    if not ctx.rating_enabled or not ctx.ratings_store:
        await ctx.reply_text(message, "车手评分功能未启用")
        return
    ratings_store = ctx.ratings_store
    races = dict(ratings_store._data["races"])  # 快照：防 scheduler 线程并发写（OSS 修复回灌）
    open_races = [(k, r) for k, r in races.items() if not r.get("closed")]
    if not open_races:
        await ctx.reply_text(message, "当前没有进行中的车手评分\n比赛周会自动创建本场投票，正赛开始1小时后开放打分!")
        return
    # 背靠背分站：R+1 在 R 投票窗口内已被预创建（pending），
    # 优先指向"已开放投票"的最高 round 场次，全部未开放才给最新 pending
    # （2026-09-28 review 实证：/rate 取最后一条会锚定下一场 pending，
    # 整场投票窗口用户拿到的都是"尚未开启"的下周表单）
    from datetime import datetime as _dt4, timezone as _tz4
    _now4 = _dt4.now(_tz4.utc)
    def _is_pending(race_entry):
        _oa = ratings_store.race_open_at(race_entry)
        return bool(_oa and _now4 < _oa)
    active = [x for x in open_races if not _is_pending(x[1])]
    pending = [x for x in open_races if _is_pending(x[1])]
    pool = active or pending
    pool.sort(key=lambda x: (x[1]["season"], x[1]["round"]))
    race_key, race = pool[-1]
    link_base = f"{ctx.rating_base_url}/r/{race_key}"
    # 比赛周预创建条目：未到开启时刻（正赛开始+1h）时提示开放时间，链接可先收藏
    pending_tip = ""
    try:
        from datetime import datetime, timezone
        from pytz import timezone as _ptz
        _oa = ratings_store.race_open_at(race)
        if _oa and datetime.now(timezone.utc) < _oa:
            _bj = _oa.astimezone(_ptz("Asia/Shanghai")).strftime('%m-%d %H:%M')
            pending_tip = f"\n⏰ 投票将于 {_bj}（北京时间）开启（正赛开始1小时后），可先收藏链接"
    except Exception:
        pass

    if is_dm:
        # 私聊：直接发专属token链接（私密可见）
        token = ratings_store.get_or_create_token(race_key, member_openid)
        if not token:
            await ctx.reply_text(message, "评分链接生成失败，请稍后重试")
            return
        link = f"{link_base}/{token}"
        await ctx.reply_md(message,
            f"⭐ {race['race_name']} 车手评分\n\n{link}\n\n可重复进入修改{pending_tip}\n⚠️ 请勿转发",
            f"## ⭐ {race['race_name']} 车手评分\r\r"
            f"[🖱️ 进入我的评分页]({link})\r\r"
            "可重复进入修改\r"
            + (pending_tip.replace("\n", "\r") + "\r" if pending_tip else "")
            + "⚠️ 专属链接，请勿转发")
        return

    # 群聊：发公开链接+一次性验证码（先验证后跳转个人表单）
    code = ratings_store.create_verification_code(race_key, member_openid)
    if not code:
        await ctx.reply_text(message, "评分链接生成失败，请稍后重试")
        return
    await ctx.reply_md(message,
        f"⭐ {race['race_name']} 车手评分\n"
        f"🖱️ 打开：{link_base}\n"
        f"📝 验证码：{code}\n\n"
        f"验证码一次性使用，打开链接后输入验证码即绑定你的身份{pending_tip}",
        f"## ⭐ {race['race_name']} 车手评分\r\r"
        f"[🖱️ 打开评分页]({link_base})\r\r"
        f"📝 验证码：**{code}**\r\r"
        "验证码一次性使用，打开链接后输入验证码即绑定你的身份"
        + pending_tip.replace("\n", "\r")
    )


HANDLERS = {
    "drivers": handle_drivers,
    "teams": handle_teams,
    "dotd": handle_dotd,
    "ratings": handle_ratings,
    "rate": handle_rate,
}
