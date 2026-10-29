"""
Discord 版双语文案（zh / en）

BOT_LANGUAGE=zh|en 环境变量切换，默认 en（面向海外用户）。
适配层（提醒/成绩/积分榜/指令回复）文案全部走这里；
scheduler 内嵌文案（评分卡/新闻聚合等）v1 仍为中文，用对应开关门控。
"""
import os

LANG = (os.getenv("BOT_LANGUAGE", "en") or "en").strip().lower()
if LANG not in ("zh", "en"):
    LANG = "en"

_SESSION_NAMES = {
    "zh": {
        "fp1": "一练", "fp2": "二练", "fp3": "三练",
        "qualifying": "排位赛", "sprint": "冲刺赛",
        "sprint_qualifying": "冲刺排位", "race": "正赛",
    },
    "en": {
        "fp1": "FP1", "fp2": "FP2", "fp3": "FP3",
        "qualifying": "Qualifying", "sprint": "Sprint",
        "sprint_qualifying": "Sprint Qualifying", "race": "Race",
    },
}

_WEEKDAYS = {
    "zh": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"],
    "en": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
}

_STRINGS = {
    "zh": {
        # 启动
        "startup_title": "✅ F1 赛程提醒机器人已启动",
        "startup_body": ("将自动推送：\n"
                         "🎉 每周比赛周预告（含赛道信息）\n"
                         "🏎️ 练习赛 / ⏱️ 排位赛 / 🏁 正赛提醒\n"
                         "📊 赛后成绩与积分榜\n\n"
                         "用 `/ask` 直接向 AI 提问 F1 技术/规则/赛况问题"),
        # 环节提醒
        "reminder_title": "{icon} F1 提醒：{race}",
        "reminder_starting": "**【{session}】即将开始**",
        "countdown_days": "⏳ 距离开始还有约 {d}天{h}小时",
        "countdown_hours": "⏳ 距离开始还有约 {h}小时{m}分钟",
        "countdown_mins": "⏳ 距离开始还有约 {m}分钟",
        "enjoy": "敬请期待精彩比赛！🏎️💨",
        "field_circuit": "📍 赛道",
        "field_time": "⏰ 时间",
        "field_weather": "🌤️ 天气",
        # 每日赛程
        "daily_title": "📅 今日 F1 赛程：{race}",
        "daily_footer": "记得准时观看！🏎️💨",
        # 每周预告
        "weekly_title": "🎉 欢迎来到比赛周！",
        "weekly_this_week": "本周为 {flag} **{race}**",
        "circuit_info": "📊 赛道信息",
        "first_race": "首次办赛：{year}年",
        "lap_length": "单圈长度：{km}km",
        "race_laps": "正赛圈数：{laps}圈",
        "race_distance": "正赛距离：{km}km",
        "lap_record": "圈速纪录：{time}（{driver}，{year}）",
        "record_verified": "🔎 圈速纪录已联网核实更新",
        "no_circuit_data": "暂无赛道详细数据",
        "session_times": "⏰ 比赛时间",
        "weekly_footer": "🏎️💨 准备好享受速度与激情了吗？",
        # 比赛结果
        "race_result_title": "🏁 {race} 比赛结果",
        "fastest_lap": "⚡ 最快圈速：**{driver}** - {time}",
        "dnf_line": "🛠️ 退赛: {names}",
        # 积分榜
        "driver_standings_after": "📊 {race} 后车手积分榜",
        "driver_standings_plain": "📊 车手积分榜更新",
        "constructor_standings": "🏎️ 车队积分榜",
        "driver_standings": "🏆 车手积分榜",
        "points_unit": "分",
        "wins_fmt": "({wins}胜)",
        "provisional_note": "⚠️ 含预估积分：{note}",
        # 环节成绩
        "session_result_title": "🏁 {session}成绩",
        "watched_driver": "（你关注的车手）",
        # 指令通用
        "no_data": "暂无数据",
        "error_retry": "❌ 查询失败，请稍后重试",
        "usage_gp": "用法：`/gp 地点 [年份] [环节]`，如 `/gp monza race`",
        # 天气
        "weather_title": "🌤️ {race} 天气",
        "weather_current": "🌡️ 当前",
        "weather_note": "沥青温度为模型估算/代理值时以实际为准",
        # 偏好
        "pref_driver_set": "✅ 已添加关注车手：**{name}**",
        "pref_team_set": "✅ 已添加关注车队：**{name}**",
        "pref_not_found": "❌ 未找到{kind}：{query}",
        "pref_limit": "⚠️ 最多关注 {n} 个，请先 /unbind 移除",
        "pref_me_title": "⭐ 你的偏好",
        "pref_none": "尚未设置关注，用 /setdriver /setteam 添加",
        "pref_unbound": "✅ 已清除全部关注",
        "pref_news_on": "✅ 已开启新闻定向推送（每日 DM）",
        "pref_news_off": "✅ 已关闭新闻定向推送",
        "kind_driver": "车手",
        "kind_team": "车队",
        # AI
        "ask_thinking": "🔍 正在查询数据并分析，请稍候…",
        "ask_failed": "❌ AI 暂时不可用或回答超时，请稍后再试",
        "ask_disabled": "ℹ️ 未配置 LLM API Key，AI 问答未启用",
        "ask_busy": "⏳ 你的上一个问题还在回答中，请稍候",
        "context_cleared": "✅ 已清空 AI 对话上下文",
        # 帮助
        "help_title": "🏎️ F1 机器人指令",
        "help_body": (
            "**赛程**\n"
            "`/next` 下一站 · `/last` 上一场成绩 · `/calendar` 全年赛历 · `/gp` 任意分站成绩\n"
            "**积分**\n"
            "`/drivers` 车手积分榜 · `/teams` 车队积分榜 · `/trend` 积分走势图\n"
            "**数据**\n"
            "`/weather` 天气 · `/upgrades` 升级件 · `/pu` 动力单元 · `/pitstops` 进站 · `/sc` 安全车\n"
            "**图表**\n"
            "`/pace` 圈速分布 · `/qualigap` 排位差距 · `/racechart` 赛后三联图 · `/car` 事故成本\n"
            "**AI**\n"
            "`/ask` 提问（联网+数据工具） · `/predict` 名次预测 · `/clear` 清空对话\n"
            "**个人**\n"
            "`/setdriver` `/setteam` 关注 · `/me` 我的偏好 · `/unbind` 解绑 · `/news` 新闻开关\n"
            "**管理员**\n"
            "私聊机器人发送 `/llm` 查看配置指令（切换 AI 服务商/密钥）"
        ),
    },
    "en": {
        "startup_title": "✅ F1 Schedule Bot is online",
        "startup_body": ("Auto-pushes:\n"
                         "🎉 Weekly race-week preview (with circuit info)\n"
                         "🏎️ Practice / ⏱️ Qualifying / 🏁 Race reminders\n"
                         "📊 Post-race results & standings\n\n"
                         "Use `/ask` to ask the AI anything about F1"),
        "reminder_title": "{icon} F1 Reminder: {race}",
        "reminder_starting": "**{session} starts soon**",
        "countdown_days": "⏳ Starts in ~{d}d {h}h",
        "countdown_hours": "⏳ Starts in ~{h}h {m}m",
        "countdown_mins": "⏳ Starts in ~{m} min",
        "enjoy": "Enjoy the action! 🏎️💨",
        "field_circuit": "📍 Circuit",
        "field_time": "⏰ Time",
        "field_weather": "🌤️ Weather",
        "daily_title": "📅 Today's F1 Schedule: {race}",
        "daily_footer": "Don't miss it! 🏎️💨",
        "weekly_title": "🎉 It's Race Week!",
        "weekly_this_week": "This week: {flag} **{race}**",
        "circuit_info": "📊 Circuit Info",
        "first_race": "First race: {year}",
        "lap_length": "Lap length: {km}km",
        "race_laps": "Race laps: {laps}",
        "race_distance": "Race distance: {km}km",
        "lap_record": "Lap record: {time} ({driver}, {year})",
        "record_verified": "🔎 Lap record verified & updated online",
        "no_circuit_data": "No detailed circuit data",
        "session_times": "⏰ Session Times",
        "weekly_footer": "🏎️💨 Ready for some racing?",
        "race_result_title": "🏁 {race} — Race Results",
        "fastest_lap": "⚡ Fastest lap: **{driver}** - {time}",
        "dnf_line": "🛠️ DNF: {names}",
        "driver_standings_after": "📊 Driver Standings after {race}",
        "driver_standings_plain": "📊 Driver Standings Update",
        "constructor_standings": "🏎️ Constructor Standings",
        "driver_standings": "🏆 Driver Standings",
        "points_unit": "pts",
        "wins_fmt": "({wins} wins)",
        "provisional_note": "⚠️ Includes projected points: {note}",
        "session_result_title": "🏁 {session} Results",
        "watched_driver": "(your driver)",
        "no_data": "No data available",
        "error_retry": "❌ Query failed, please try again later",
        "usage_gp": "Usage: `/gp <place> [year] [session]`, e.g. `/gp monza race`",
        "weather_title": "🌤️ {race} Weather",
        "weather_current": "🌡️ Now",
        "weather_note": "Track temp is a model estimate/proxy unless measured",
        "pref_driver_set": "✅ Now following driver: **{name}**",
        "pref_team_set": "✅ Now following team: **{name}**",
        "pref_not_found": "❌ {kind} not found: {query}",
        "pref_limit": "⚠️ Max {n} followed — /unbind first",
        "pref_me_title": "⭐ Your Preferences",
        "pref_none": "No favourites yet — use /setdriver /setteam",
        "pref_unbound": "✅ All favourites cleared",
        "pref_news_on": "✅ News DM subscription enabled (daily)",
        "pref_news_off": "✅ News DM subscription disabled",
        "kind_driver": "Driver",
        "kind_team": "Team",
        "ask_thinking": "🔍 Gathering data and thinking…",
        "ask_failed": "❌ AI unavailable or timed out, please try again later",
        "ask_disabled": "ℹ️ No LLM API key configured — AI Q&A disabled",
        "ask_busy": "⏳ Still answering your previous question, hold on",
        "context_cleared": "✅ AI conversation context cleared",
        "help_title": "🏎️ F1 Bot Commands",
        "help_body": (
            "**Schedule**\n"
            "`/next` next race · `/last` latest results · `/calendar` season calendar · `/gp` any GP results\n"
            "**Standings**\n"
            "`/drivers` driver standings · `/teams` constructor standings · `/trend` points trend\n"
            "**Data**\n"
            "`/weather` weather · `/upgrades` upgrades · `/pu` power unit · `/pitstops` pit stops · `/sc` safety car\n"
            "**Charts**\n"
            "`/pace` pace distribution · `/qualigap` quali gaps · `/racechart` race report · `/car` crash costs\n"
            "**AI**\n"
            "`/ask` ask anything (web + data tools) · `/predict` race prediction · `/clear` reset context\n"
            "**You**\n"
            "`/setdriver` `/setteam` follow · `/me` my prefs · `/unbind` unbind · `/news` news toggle\n"
            "**Admin**\n"
            "DM the bot `/llm` for config commands (AI provider/keys)"
        ),
    },
}


def T(key: str, **kwargs) -> str:
    s = _STRINGS[LANG].get(key) or _STRINGS["en"].get(key) or key
    return s.format(**kwargs) if kwargs else s


def session_name(session: dict) -> str:
    """按界面语言返回环节名（优先用 type 映射，zh 回退数据原名）"""
    stype = session.get("type", "")
    if LANG == "zh":
        return session.get("name") or _SESSION_NAMES["zh"].get(stype, stype)
    return _SESSION_NAMES["en"].get(stype) or session.get("name", stype)


def weekday(dt) -> str:
    return _WEEKDAYS[LANG][dt.weekday()]
