# -*- coding: utf-8 -*-
"""指令处理：杂项
（/help /watch /live /telemetry /clear /memory——从 main.py 指令分发链原样迁移，行为零变化）"""
import logging

try:
    from ..replies import _next_msg_seq
except (ImportError, ValueError):  # main.py 作为脚本直跑时 handlers 为顶层包
    from replies import _next_msg_seq

logger = logging.getLogger(__name__)


async def handle_help(ctx, message, arg, member_openid, is_dm):
    try:
        await message.reply(
            msg_type=2,
            markdown={"content": ctx.help_markdown + "\r\r点击下方按钮可直接执行👇"},
            keyboard={"content": ctx.help_keyboard},
            msg_seq=_next_msg_seq(message.id),
        )
    except Exception as e:
        logger.warning(f"键盘卡片回复失败，降级纯文本: {e}")
        await ctx.reply_md(message, ctx.help_text, ctx.help_markdown)


async def handle_watch(ctx, message, arg, member_openid, is_dm):
    await ctx.reply_md(message, ctx.watch_links_text, ctx.watch_links_md)


async def handle_live(ctx, message, arg, member_openid, is_dm):
    LIVE_BASE_URL = ctx.live_base_url
    text = (f"📊 F1实时计时面板\n\n"
            f"{LIVE_BASE_URL}/live\n\n"
            "比赛周内可用：实时排名/车距、旗帜与安全车事件、天气、最快圈、进站轮胎、车队无线电（本地转写文字）。\n"
            "免费替代：F1Cosmos 实时计时（f1cosmos.com/zh/dashboard/live，中文，浏览器直取F1官方livetiming）或 f1-dash.com/dashboard（英文，功能更丰富）。\n"
            "悬浮窗：打开链接点「开启悬浮窗」（Chrome 116+/Edge），可置顶叠放视频播放器上方。\n"
            "叠层扩展：群文件 f1-overlay-extension.zip（桌面Chrome/Edge），半透明叠层直接覆盖直播画面，支持全屏，默认嵌入F1Cosmos免费实时计时。\n"
            "非比赛时段显示「暂无实时数据」属正常。")
    md = (f"## 📊 F1实时计时面板\r\r"
          f"[🚥 进入实时计时面板]({LIVE_BASE_URL}/live)\r\r"
          "比赛周内可用：实时排名/车距、旗帜与安全车事件、天气、最快圈、进站轮胎、车队无线电（本地转写文字）。\r\r"
          "免费替代：[F1Cosmos 实时计时](https://f1cosmos.com/zh/dashboard/live)（中文，浏览器直取F1官方livetiming）· [f1-dash](https://f1-dash.com/dashboard)（英文，功能更丰富）\r\r"
          "悬浮窗：打开链接点「开启悬浮窗」（Chrome 116+/Edge），可置顶叠放视频播放器上方。\r\r"
          "叠层扩展：群文件 f1-overlay-extension.zip（桌面Chrome/Edge），半透明叠层直接覆盖直播画面，支持全屏，默认嵌入F1Cosmos免费实时计时。\r\r"
          "非比赛时段显示「暂无实时数据」属正常。")
    await ctx.reply_md(message, text, md)


async def handle_telemetry(ctx, message, arg, member_openid, is_dm):
    # 遥测/圈速/赛果分析 -> 自有遥测页 + F1Cosmos 仪表盘（含透明度可调嵌入页）
    RATING_BASE_URL = ctx.rating_base_url
    F1COSMOS_DASH_BASE = ctx.f1cosmos_dash_base
    text = ("📊 F1 遥测与比赛分析\n\n"
            f"遥测对比（本站自建）: {RATING_BASE_URL}/telemetry\n"
            f"遥测面板·透明度可调: {RATING_BASE_URL}/telemetry/pro\n"
            f"圈速分析: {F1COSMOS_DASH_BASE}/laptime\n"
            f"比赛结果: {F1COSMOS_DASH_BASE}/result\n\n"
            "页面内可自选年份/分站/环节/车手，支持多车手曲线对比；"
            "透明度可调面板可自由调节透明度/亮度/对比度，适配夜间观赛或叠加场景")
    md = ("## 📊 F1 遥测与比赛分析\r\r"
          f"[🛰️ 遥测对比·本站自建（速度/油门/刹车曲线）]({RATING_BASE_URL}/telemetry)\r\r"
          f"[🎚️ 遥测面板·透明度可调（嵌入F1Cosmos）]({RATING_BASE_URL}/telemetry/pro)\r\r"
          f"[⏱️ 圈速分析（F1Cosmos）]({F1COSMOS_DASH_BASE}/laptime)\r\r"
          f"[🏁 比赛结果（F1Cosmos）]({F1COSMOS_DASH_BASE}/result)\r\r"
          "页面内可自选年份/分站/环节/车手\r"
          "🎚️ 面板支持透明度/亮度/对比度调节，含「夜间观赛」「背景叠加」预设")
    await ctx.reply_md(message, text, md)


async def handle_clear(ctx, message, arg, member_openid, is_dm):
    llm = ctx.llm
    n = llm.clear_context(member_openid) if llm else 0
    if n:
        await ctx.reply_text(message, f"🧹 本轮会话已归档为主题摘要（{n} 轮），核心内容仍会记得，闲聊细节已遗忘\n发送 /memory 查看你的记忆构成")
    else:
        await ctx.reply_text(message, "当前没有进行中的对话上下文")


async def handle_memory(ctx, message, arg, member_openid, is_dm):
    # 记忆自查（三层结构：工作记忆/主题摘要/长期锚定）
    llm = ctx.llm
    if not llm:
        await ctx.reply_text(message, "💡 LLM 未启用")
        return
    sub = (arg or "").strip()
    if sub.lower().startswith("forget "):
        iid = sub[7:].strip()
        ok = llm._memory.forget(member_openid, iid)
        await ctx.reply_text(message, f"✅ 已删除 `{iid}`" if ok else f"❌ 未找到记忆条目 `{iid}`")
    else:
        desc = llm._memory.describe(member_openid)
        await ctx.reply_md(message,
                           desc.replace("**", "").replace("`", ""),
                           desc.replace("\n", "\r"))


HANDLERS = {
    "help": handle_help,
    "watch": handle_watch,
    "live": handle_live,
    "telemetry": handle_telemetry,
    "clear": handle_clear,
    "memory": handle_memory,
}
