# -*- coding: utf-8 -*-
"""指令处理：个性化偏好
（/setdriver /setteam /me /unbind /news——从 main.py 指令分发链原样迁移，行为零变化）"""
import logging

from common import UserPrefsStore, resolve_driver, resolve_team
from common.qq_group_bot import QQGroupBot

logger = logging.getLogger(__name__)


async def handle_setdriver(ctx, message, arg, member_openid, is_dm):
    if not arg:
        await ctx.reply_text(message, "用法: /setdriver 车手名\n如: /setdriver 维斯塔潘\n最多可同时关注3位车手")
        return
    prefs_store = ctx.prefs_store
    standings = ctx.f1_api.get_current_standings()
    entries = QQGroupBot._parse_driver_standings(standings.get("drivers") or {})
    result = resolve_driver(arg, entries)
    if result:
        driver_id, driver_name = result
        ok = prefs_store.set_user_driver(member_openid, driver_id, driver_name)
        if not ok:
            await ctx.reply_text(message,
                f"⚠️ 最多同时关注 {UserPrefsStore.MAX_WATCH} 位车手\n"
                f"可先 /unbind 车手 {driver_name} 移除一位再添加")
            return
        names = list(UserPrefsStore.pref_driver_names(
            prefs_store.get_user_prefs(member_openid)).values())
        await ctx.reply_text(message,
            f"✅ 已添加关注车手: {driver_name}\n"
            f"当前关注: {'、'.join(names)}\n"
            "每站正赛结束后会为你推送Ta们的排名⭐")
    else:
        await ctx.reply_text(message, f"❌ 未找到车手「{arg}」，请检查名字（支持中英文）")


async def handle_setteam(ctx, message, arg, member_openid, is_dm):
    if not arg:
        await ctx.reply_text(message, "用法: /setteam 车队名\n如: /setteam 法拉利\n最多可同时关注3支车队")
        return
    prefs_store = ctx.prefs_store
    standings = ctx.f1_api.get_current_standings()
    entries = QQGroupBot._parse_constructor_standings(standings.get("constructors") or {})
    result = resolve_team(arg, entries)
    if result:
        team_id, team_name = result
        ok = prefs_store.set_user_team(member_openid, team_id, team_name)
        if not ok:
            await ctx.reply_text(message,
                f"⚠️ 最多同时关注 {UserPrefsStore.MAX_WATCH} 支车队\n"
                f"可先 /unbind 车队 {team_name} 移除一支再添加")
            return
        names = list(UserPrefsStore.pref_team_names(
            prefs_store.get_user_prefs(member_openid)).values())
        await ctx.reply_text(message,
            f"✅ 已添加关注车队: {team_name}\n"
            f"当前关注: {'、'.join(names)}\n"
            "每站正赛结束后会为你推送车队排名⭐")
    else:
        await ctx.reply_text(message, f"❌ 未找到车队「{arg}」，请检查名字（支持中英文）")


async def handle_me(ctx, message, arg, member_openid, is_dm):
    prefs_store = ctx.prefs_store
    pref = prefs_store.get_user_prefs(member_openid)
    if pref:
        lines = ["📋 你的设置："]
        d_names = list(UserPrefsStore.pref_driver_names(pref).values())
        t_names = list(UserPrefsStore.pref_team_names(pref).values())
        if d_names:
            lines.append(f"🏎️ 关注车手（{len(d_names)}/{UserPrefsStore.MAX_WATCH}）: {'、'.join(d_names)}")
        if t_names:
            lines.append(f"🏁 关注车队（{len(t_names)}/{UserPrefsStore.MAX_WATCH}）: {'、'.join(t_names)}")
        news_on = pref.get("news_dm", True)
        lines.append(f"📰 定向新闻推送: {'开' if news_on else '关'}（/news on|off）")
        lines.append("添加：/setdriver /setteam；解绑：/unbind 车手 <名字> 或 /unbind 清空全部")
        await ctx.reply_text(message, "\n".join(lines))
    else:
        await ctx.reply_text(message, "你还没有设置偏好\n使用 /setdriver 和 /setteam 进行设置")


async def handle_unbind(ctx, message, arg, member_openid, is_dm):
    # 解绑偏好：
    # /unbind                    全部解绑
    # /unbind 车手|车队          清空该类
    # /unbind 车手 维斯塔潘      移除指定一位
    prefs_store = ctx.prefs_store
    a = (arg or "").strip()
    al = a.lower()
    if not al:
        kinds = ["driver", "team"]
        removed = prefs_store.unset_user_pref(member_openid, *kinds)
        if removed:
            await ctx.reply_text(message, "✅ 已解绑全部偏好，后续不再接收个性化推送")
        else:
            await ctx.reply_text(message, "你当前没有偏好绑定\n使用 /me 查看现有设置")
        return
    parts = a.split(None, 1)
    kind_word, target = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
    kind_map = {"车手": "driver", "driver": "driver",
                "车队": "team", "team": "team"}
    kind = kind_map.get(kind_word)
    if not kind:
        await ctx.reply_text(message, "用法: /unbind [车手|车队] [名字]\n不带参数则解绑全部偏好")
        return
    if target:
        # 指定移除：在用户关注列表内按名字/id 匹配
        pref = prefs_store.get_user_prefs(member_openid)
        names = (UserPrefsStore.pref_driver_names(pref) if kind == "driver"
                 else UserPrefsStore.pref_team_names(pref))
        hit = next((k for k, v in names.items()
                    if target.lower() in (k.lower(), v.lower())), None)
        if not hit:
            # 再走档案别名解析（如"潘子"→max_verstappen）
            from common.drivers_profile import get_driver_aliases, get_team_aliases
            alias_map = get_driver_aliases() if kind == "driver" else get_team_aliases()
            cand = alias_map.get(target)
            if cand and cand in names:
                hit = cand
        if not hit:
            await ctx.reply_text(message, f"你的关注列表里没有「{target}」，/me 查看现有设置")
            return
        ok = (prefs_store.remove_user_driver(member_openid, hit) if kind == "driver"
              else prefs_store.remove_user_team(member_openid, hit))
        if ok:
            await ctx.reply_text(message, f"✅ 已移除关注: {names[hit]}")
        else:
            await ctx.reply_text(message, "解绑失败，请稍后重试")
        return
    removed = prefs_store.unset_user_pref(member_openid, kind)
    if removed:
        label = {"driver": "车手", "team": "车队"}
        await ctx.reply_text(message, f"✅ 已解绑全部{label[kind]}偏好")
    else:
        await ctx.reply_text(message, "你当前没有对应的偏好绑定\n使用 /me 查看现有设置")


async def handle_news(ctx, message, arg, member_openid, is_dm):
    # 定向新闻推送开关（需先 /setdriver 或 /setteam 设置关注对象）
    prefs_store = ctx.prefs_store
    a = (arg or "").strip().lower()
    if a in ("on", "开", "开启"):
        prefs_store.set_news_dm(member_openid, True)
        await ctx.reply_text(message, "✅ 已开启定向新闻推送\n与你关注车手/主队相关的新闻将私聊推送给你（每日 8:00/19:00）")
    elif a in ("off", "关", "关闭"):
        prefs_store.set_news_dm(member_openid, False)
        await ctx.reply_text(message, "✅ 已关闭定向新闻推送（群内的每日新闻速递不受影响）")
    else:
        cur = prefs_store.news_dm_enabled(member_openid)
        pref = prefs_store.get_user_prefs(member_openid)
        has_watch = bool(pref.get("driver") or pref.get("team"))
        state = "开启" if cur else "关闭"
        hint = "" if has_watch else "\n⚠️ 你还未设置关注对象，先用 /setdriver 或 /setteam 设置"
        await ctx.reply_text(message,
            f"📰 定向新闻推送当前状态：{state}{hint}\n"
            f"用法: /news on 开启 · /news off 关闭")


HANDLERS = {
    "setdriver": handle_setdriver,
    "setteam": handle_setteam,
    "me": handle_me,
    "unbind": handle_unbind,
    "news": handle_news,
}
