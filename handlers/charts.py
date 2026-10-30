# -*- coding: utf-8 -*-
"""指令处理：统计图表/技术数据
（/pace /racechart /trend /qualigap /pitstops /sc /car /pu——从 main.py 指令分发链原样迁移，行为零变化）"""
import logging

logger = logging.getLogger(__name__)


async def handle_pace(ctx, message, arg, member_openid, is_dm):
    # 正赛 pace 分布统计图（OpenF1 圈速箱线图+配方着色+中位差条形图）
    f1_api = ctx.f1_api
    SEASON = ctx.season
    import asyncio as _aio
    loop = _aio.get_running_loop()

    def _gen_pace():
        from common.chart_factory import render_pace_chart
        from common.f1_api import find_race_by_circuit
        from datetime import datetime as _dtx
        schedule = f1_api.get_schedule_for_year(SEASON) or []
        race = None
        if arg:
            import re as _re
            m = _re.search(r"^[Rr]\s?0*(\d{1,2})$", arg.strip())
            if m:
                race = next((r for r in schedule
                             if int(r.get("round", 0)) == int(m.group(1))), None)
            else:
                race = find_race_by_circuit(schedule, arg)
        else:
            today = _dtx.now().date().isoformat()
            past = [r for r in schedule
                    if r.get("date") and r["date"] < today]
            race = past[-1] if past else None
        if not race:
            return None, None
        race_dt = None
        try:
            from datetime import timezone as _tzu2
            race_dt = _dtx.fromisoformat(
                f"{race['date']}T{race.get('time', '00:00:00Z').replace('Z', '')}"
            ).replace(tzinfo=_tzu2.utc)
        except Exception:
            pass
        if race_dt and _dtx.now(_tzu2.utc) < race_dt:
            return race, None  # 未赛
        p = render_pace_chart(SEASON, int(race["round"]),
                              race.get("raceName", ""), f1_api=f1_api)
        return race, p

    race, chart_path = await loop.run_in_executor(None, _gen_pace)
    if race is None:
        await ctx.reply_text(message, "未找到分站（用法：/pace 或 /pace 雪邦、/pace R16）")
    elif chart_path is None:
        await ctx.reply_text(message,
                             f"{race.get('raceName')} 暂无 pace 数据（分站未赛或 OpenF1 未收录）")
    else:
        import os as _os
        await ctx.reply_image(message,
                              f"{ctx.rating_base_url}/exports/{_os.path.basename(chart_path)}",
                              content=f"📊 {race.get('raceName')} 正赛 Pace 分布统计图")


async def handle_racechart(ctx, message, arg, member_openid, is_dm):
    # 赛后三联统计图（pace分布+轮胎策略时间线+名次变化）
    f1_api = ctx.f1_api
    SEASON = ctx.season
    import asyncio as _aio
    loop = _aio.get_running_loop()

    def _gen_report():
        from common.chart_factory import render_race_report_chart
        from common.f1_api import find_race_by_circuit
        from datetime import datetime as _dtx
        schedule = f1_api.get_schedule_for_year(SEASON) or []
        race = None
        if arg:
            import re as _re
            m = _re.search(r"^[Rr]\s?0*(\d{1,2})$", arg.strip())
            if m:
                race = next((r for r in schedule
                             if int(r.get("round", 0)) == int(m.group(1))), None)
            else:
                race = find_race_by_circuit(schedule, arg)
        else:
            today = _dtx.now().date().isoformat()
            past = [r for r in schedule
                    if r.get("date") and r["date"] < today]
            race = past[-1] if past else None
        if not race:
            return None, None
        race_dt = None
        try:
            from datetime import timezone as _tzu2
            race_dt = _dtx.fromisoformat(
                f"{race['date']}T{race.get('time', '00:00:00Z').replace('Z', '')}"
            ).replace(tzinfo=_tzu2.utc)
        except Exception:
            pass
        if race_dt and _dtx.now(_tzu2.utc) < race_dt:
            return race, None
        p = render_race_report_chart(SEASON, int(race["round"]),
                                     race.get("raceName", ""), f1_api=f1_api)
        return race, p

    race, chart_path = await loop.run_in_executor(None, _gen_report)
    if race is None:
        await ctx.reply_text(message, "未找到分站（用法：/racechart 或 /racechart 雪邦、/racechart R16）")
    elif chart_path is None:
        await ctx.reply_text(message,
                             f"{race.get('raceName')} 暂无统计数据（分站未赛或数据未收录）")
    else:
        import os as _os
        await ctx.reply_image(message,
                              f"{ctx.rating_base_url}/exports/{_os.path.basename(chart_path)}",
                              content=f"📊 {race.get('raceName')} 赛后统计图（Pace/策略/名次变化）")


async def handle_trend(ctx, message, arg, member_openid, is_dm):
    # 本赛季车手积分走势图（逐站累计，前6名）
    SEASON = ctx.season
    import asyncio as _aio
    loop = _aio.get_running_loop()

    def _gen_trend():
        from common.chart_factory import render_points_trend_chart
        return render_points_trend_chart(SEASON, f1_api=ctx.f1_api)

    chart_path = await loop.run_in_executor(None, _gen_trend)
    if not chart_path:
        await ctx.reply_text(message, "积分走势数据不足（赛季前3站后可用）")
    else:
        import os as _os
        fname = _os.path.basename(chart_path)
        await ctx.reply_image(message,
                              f"{ctx.rating_base_url}/exports/{fname}",
                              content=f"📈 {SEASON} 车手积分走势（逐站累计，含冲刺赛）")


async def handle_qualigap(ctx, message, arg, member_openid, is_dm):
    # 排位/冲刺排位差距图（杆位金色；/qualigap 雪邦 sq 查冲刺排位）
    f1_api = ctx.f1_api
    SEASON = ctx.season
    import asyncio as _aio
    loop = _aio.get_running_loop()

    def _gen_qg():
        from common.chart_factory import render_quali_gap_chart
        from common.f1_api import find_race_by_circuit
        from datetime import datetime as _dtx
        q = (arg or "").strip()
        stype = "qualifying"
        if q.lower() == "sq":
            # /qualigap sq：最近一场冲刺排位（无地名）
            stype = "sprint_qualifying"
            q = ""
        elif q.lower().endswith(" sq"):
            stype = "sprint_qualifying"
            q = q[:-3].strip()
        schedule = f1_api.get_schedule_for_year(SEASON) or []
        race = None
        if q:
            import re as _re
            m = _re.search(r"^[Rr]\s?0*(\d{1,2})$", q)
            if m:
                race = next((r for r in schedule
                             if int(r.get("round", 0)) == int(m.group(1))), None)
            else:
                race = find_race_by_circuit(schedule, q)
        else:
            today = _dtx.now().date().isoformat()
            past = [r for r in schedule
                    if r.get("date") and r["date"] < today]
            race = past[-1] if past else None
        if not race:
            return None, None, stype
        race_dt = None
        try:
            from datetime import timezone as _tzu2
            race_dt = _dtx.fromisoformat(
                f"{race['date']}T{race.get('time', '00:00:00Z').replace('Z', '')}"
            ).replace(tzinfo=_tzu2.utc)
        except Exception:
            pass
        if race_dt and _dtx.now(_tzu2.utc) < race_dt:
            return race, None, stype
        p = render_quali_gap_chart(SEASON, int(race["round"]),
                                   race.get("raceName", ""), f1_api=f1_api,
                                   session_type=stype)
        return race, p, stype

    race, chart_path, stype = await loop.run_in_executor(None, _gen_qg)
    if race is None:
        await ctx.reply_text(message, "未找到分站（用法：/qualigap 或 /qualigap 雪邦、/qualigap R16、/qualigap 雪邦 sq）")
    elif chart_path is None:
        _t0 = "冲刺排位" if stype == "sprint_qualifying" else "排位"
        await ctx.reply_text(message,
                             f"{race.get('raceName')} 暂无{_t0}成绩（分站未赛或{_t0}未进行）")
    else:
        import os as _os
        _tag = "冲刺排位" if stype == "sprint_qualifying" else "排位"
        await ctx.reply_image(message,
                              f"{ctx.rating_base_url}/exports/{_os.path.basename(chart_path)}",
                              content=f"⏱️ {race.get('raceName')} {_tag}差距图")


async def handle_pitstops(ctx, message, arg, member_openid, is_dm):
    # 进站时长车队对比箱线图（Jolpica pitstops，停站静止时间口径）
    f1_api = ctx.f1_api
    SEASON = ctx.season
    import asyncio as _aio
    loop = _aio.get_running_loop()

    def _gen_pit():
        from common.chart_factory import render_pitstop_chart
        from common.f1_api import find_race_by_circuit
        from datetime import datetime as _dtx
        schedule = f1_api.get_schedule_for_year(SEASON) or []
        race = None
        if arg:
            import re as _re
            m = _re.search(r"^[Rr]\s?0*(\d{1,2})$", arg.strip())
            if m:
                race = next((r for r in schedule
                             if int(r.get("round", 0)) == int(m.group(1))), None)
            else:
                race = find_race_by_circuit(schedule, arg)
        else:
            today = _dtx.now().date().isoformat()
            past = [r for r in schedule
                    if r.get("date") and r["date"] < today]
            race = past[-1] if past else None
        if not race:
            return None, None
        race_dt = None
        try:
            from datetime import timezone as _tzu2
            race_dt = _dtx.fromisoformat(
                f"{race['date']}T{race.get('time', '00:00:00Z').replace('Z', '')}"
            ).replace(tzinfo=_tzu2.utc)
        except Exception:
            pass
        if race_dt and _dtx.now(_tzu2.utc) < race_dt:
            return race, None
        p = render_pitstop_chart(SEASON, int(race["round"]),
                                 race.get("raceName", ""), f1_api=f1_api)
        return race, p

    race, chart_path = await loop.run_in_executor(None, _gen_pit)
    if race is None:
        await ctx.reply_text(message, "未找到分站（用法：/pitstops 或 /pitstops 雪邦、/pitstops R16）")
    elif chart_path is None:
        await ctx.reply_text(message,
                             f"{race.get('raceName')} 暂无进站数据（分站未赛或无有效停站）")
    else:
        import os as _os
        await ctx.reply_image(message,
                              f"{ctx.rating_base_url}/exports/{_os.path.basename(chart_path)}",
                              content=f"🔧 {race.get('raceName')} 进站时长车队对比")


async def handle_sc(ctx, message, arg, member_openid, is_dm):
    # 安全车/VSC/红旗事件时间线（OpenF1 race_control 主源，livetiming 兜底；
    # 无事件返回 None——本站干净不出图）
    f1_api = ctx.f1_api
    SEASON = ctx.season
    import asyncio as _aio
    loop = _aio.get_running_loop()

    def _gen_sc():
        from common.chart_factory import render_sc_timeline
        from common.f1_api import find_race_by_circuit
        from datetime import datetime as _dtx
        schedule = f1_api.get_schedule_for_year(SEASON) or []
        race = None
        if arg:
            import re as _re
            m = _re.search(r"^[Rr]\s?0*(\d{1,2})$", arg.strip())
            if m:
                race = next((r for r in schedule
                             if int(r.get("round", 0)) == int(m.group(1))), None)
            else:
                race = find_race_by_circuit(schedule, arg)
        else:
            today = _dtx.now().date().isoformat()
            past = [r for r in schedule
                    if r.get("date") and r["date"] < today]
            race = past[-1] if past else None
        if not race:
            return None, None
        race_dt = None
        try:
            from datetime import timezone as _tzu2
            race_dt = _dtx.fromisoformat(
                f"{race['date']}T{race.get('time', '00:00:00Z').replace('Z', '')}"
            ).replace(tzinfo=_tzu2.utc)
        except Exception:
            pass
        if race_dt and _dtx.now(_tzu2.utc) < race_dt:
            return race, None
        p = render_sc_timeline(SEASON, int(race["round"]),
                               race.get("raceName", ""), f1_api=f1_api)
        return race, p

    race, chart_path = await loop.run_in_executor(None, _gen_sc)
    if race is None:
        await ctx.reply_text(message, "未找到分站（用法：/sc 或 /sc 雪邦、/sc R16）")
    elif chart_path is None:
        # 区分"数据不可用"与"确认无事件"（双源皆断时禁止误报干净）
        from common.chart_factory import get_sc_events as _gse
        evts = await loop.run_in_executor(
            None, lambda: _gse(SEASON, int(race["round"]), f1_api)
        )
        if evts is None:
            await ctx.reply_text(message,
                                 f"{race.get('raceName')} 安全车数据源当前不可用"
                                 "（OpenF1 锁定/livetiming 不可达），请赛后重试")
        else:
            await ctx.reply_text(message,
                                 f"{race.get('raceName')} 本站无安全车/VSC/红旗事件")
    else:
        import os as _os
        await ctx.reply_image(message,
                              f"{ctx.rating_base_url}/exports/{_os.path.basename(chart_path)}",
                              content=f"🚩 {race.get('raceName')} 安全车事件时间线")


async def handle_car(ctx, message, arg, member_openid, is_dm):
    if not ctx.f1cosmos:
        await ctx.reply_text(message, "F1Cosmos数据源未启用")
        return
    SEASON = ctx.season
    f1cosmos = ctx.f1cosmos
    import asyncio as _aio
    from functools import partial as _partial
    loop = _aio.get_running_loop()
    text, md = await loop.run_in_executor(None, _partial(f1cosmos.format_destructors, arg, SEASON))
    await ctx.reply_md(message, text, md)
    # 附事故成本柱图（车手榜+车队榜）
    try:
        from common.chart_factory import render_destructors_chart
        p = await loop.run_in_executor(
            None, _partial(render_destructors_chart, SEASON, f1cosmos=f1cosmos))
        if p:
            import os as _os
            await ctx.reply_image(message,
                                  f"{ctx.rating_base_url}/exports/{_os.path.basename(p)}",
                                  content=f"💥 {SEASON} 事故维修成本统计图")
    except Exception:
        pass


async def handle_pu(ctx, message, arg, member_openid, is_dm):
    if not ctx.f1cosmos:
        await ctx.reply_text(message, "F1Cosmos数据源未启用")
        return
    SEASON = ctx.season
    f1cosmos = ctx.f1cosmos
    import asyncio as _aio
    import re as _re
    from functools import partial as _partial
    loop = _aio.get_running_loop()
    # 可选年份：/pu 维斯塔潘 2023（历史赛季为该赛季末FIA累计用量快照）
    pu_year = None
    pu_q = (arg or "").strip()
    _m = _re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", pu_q)
    if _m:
        pu_year = int(_m.group(1))
        pu_q = (pu_q[:_m.start()] + pu_q[_m.end():]).strip()
    text, md = await loop.run_in_executor(
        None, _partial(f1cosmos.format_pu_quota, pu_q, pu_year or SEASON))
    if not md:
        await ctx.reply_text(message, f"未找到「{arg}」对应的车手或车队\n用法: /pu [车手/车队] [年份]，如 /pu 法拉利、/pu 维斯塔潘 2023")
    else:
        await ctx.reply_md(message, text, md)
        # 当前赛季附 PU 部件用量堆叠柱图（历史赛季无图）
        if not pu_year or pu_year == SEASON:
            try:
                from common.chart_factory import render_pu_usage_chart
                p = await loop.run_in_executor(
                    None, _partial(render_pu_usage_chart, SEASON, f1cosmos=f1cosmos))
                if p:
                    import os as _os
                    await ctx.reply_image(message,
                                          f"{ctx.rating_base_url}/exports/{_os.path.basename(p)}",
                                          content=f"🔋 {SEASON} 动力单元部件用量统计图")
            except Exception:
                pass


HANDLERS = {
    "pace": handle_pace,
    "racechart": handle_racechart,
    "trend": handle_trend,
    "qualigap": handle_qualigap,
    "pitstops": handle_pitstops,
    "sc": handle_sc,
    "car": handle_car,
    "pu": handle_pu,
}
