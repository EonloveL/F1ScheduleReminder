"""国际化文案层（zh / en）。

原位于 discord_version/i18n.py，上提到 common/ 后供 QQ 与 Discord 两侧共享。
默认语言通过 BOT_LANGUAGE 环境变量控制；未配置时回退到英文，保持与 Discord 版一致。
QQ 侧可逐步把硬编码中文替换为 T(key) 调用，无需一次性全改。
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
        # 偏好（QQ 与 Discord 共有）
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
        # 通用
        "no_data": "暂无数据",
        "error_retry": "❌ 查询失败，请稍后重试",
        "context_cleared": "✅ 已清空 AI 对话上下文",
    },
    "en": {
        "startup_title": "✅ F1 Schedule Bot is online",
        "startup_body": ("Auto-pushes:\n"
                         "🎉 Weekly race-week preview (with circuit info)\n"
                         "🏎️ Practice / ⏱️ Qualifying / 🏁 Race reminders\n"
                         "📊 Post-race results & standings\n\n"
                         "Use `/ask` to ask the AI anything about F1"),
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
        "no_data": "No data available",
        "error_retry": "❌ Query failed, please try again later",
        "context_cleared": "✅ AI conversation context cleared",
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
