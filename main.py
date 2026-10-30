"""
F1赛程提醒机器人 - QQ官方群聊版本
基于QQ官方Bot API V2实现
支持：群聊消息推送、定时提醒

部署说明：
1. 在QQ开放平台 (https://q.qq.com) 申请机器人账号
2. 获取AppID和AppSecret
3. 将机器人添加到QQ群并设置为管理员（用于@全体成员）
4. 填写下方配置
5. 运行：python main.py

目录结构：
   /opt/f1-bot/
   ├── common/
   │   ├── __init__.py
   │   ├── f1_api.py
   │   ├── scheduler.py
   │   └── qq_group_bot.py
   └── qq_group_official_version/
       ├── main.py
       └── requirements.txt
"""

import logging
import sys
import signal
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# 添加common目录到路径
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, current_dir)

try:
    from common import (F1API, ReminderScheduler, UserPrefsStore,
                        LLMAssistant, CircuitsManager, F1CosmosAPI, PushedResultsStore)
    from common.ratings_store import RatingsStore
    from common.qq_group_bot import QQGroupBot
    from common.f1_tools import F1_TOOLS, build_handlers
    from common.metrics import incr as _metric_incr  # LLM问答/识图/指令多处共用（复审H1：局部导入导致平级函数 NameError）
except ImportError as e:
    print(f"❌ 导入错误：{e}")
    print("请确保common目录存在，并且包含必要的模块文件")
    sys.exit(1)

# 回复助手已抽至 replies.py（行为零变化）；此处 re-export 保持 main.reply_* 原名可用
#（LLM 接线在用；测试经替换 main.reply_text/reply_md/reply_image 注入记录器）
try:
    from . import replies as _replies
    from .replies import (reply_text, reply_md, reply_image, reply_long_md,
                          _split_lines, _next_msg_seq, _is_msg_id_expired)
except ImportError:  # main.py 作为脚本直跑（无包上下文）
    import replies as _replies
    from replies import (reply_text, reply_md, reply_image, reply_long_md,
                         _split_lines, _next_msg_seq, _is_msg_id_expired)

logger = logging.getLogger(__name__)


# ==================== 配置区域 ====================
# 从环境变量或 .env 文件读取（优先级：环境变量 > .env > 默认值）

# QQ开放平台配置（必填）
# 获取地址：https://q.qq.com -> 你的机器人 -> 开发设置
APPID = os.getenv("QQ_APPID", "YOUR_APPID_HERE")
APP_SECRET = os.getenv("QQ_APP_SECRET", "YOUR_SECRET_HERE")

# 是否使用沙箱环境（新机器人默认只能在沙箱环境测试）
USE_SANDBOX = os.getenv("USE_SANDBOX", "true").lower() == "true"

# QQ群配置（必填）
# 多群：逗号分隔的 group_openid 列表；单群向后兼容 QQ_GROUP_OPENID
QQ_GROUP_OPENID = os.getenv("QQ_GROUP_OPENID", "YOUR_GROUP_OPENID_HERE")
QQ_GROUP_OPENIDS_RAW = os.getenv("QQ_GROUP_OPENIDS", "")
QQ_GROUP_OPENIDS = [g.strip() for g in QQ_GROUP_OPENIDS_RAW.split(",") if g.strip()] if QQ_GROUP_OPENIDS_RAW else [QQ_GROUP_OPENID]
DEFAULT_GROUP = QQ_GROUP_OPENIDS[0] if QQ_GROUP_OPENIDS else QQ_GROUP_OPENID

# ==================== 提醒时间配置（分钟）====================
REMINDER_TIMES = {
    "fp1": 30,           # 第一节练习赛
    "fp2": 30,           # 第二节练习赛
    "fp3": 30,           # 第三节练习赛
    "qualifying": 30,    # 排位赛
    "sprint_qualifying": 30,  # 冲刺排位赛
    "sprint": 30,        # 冲刺赛
    "race": 60,          # 正赛（提前1小时）
}

# ==================== 赛后结果推送 ====================
POST_RACE_DELAY = 30  # 比赛结束后多久推送结果（分钟）

# ==================== 时区和赛季配置 ====================
TIMEZONE = "Asia/Shanghai"
SEASON = datetime.now().year  # 自动获取当前年份

# ==================== 日志配置 ====================
LOG_LEVEL = "INFO"
LOG_FILE = "f1_reminder.log"

# ==================== 其他配置 ====================
SEND_STARTUP_MESSAGE = True

# ==================== LLM配置（Kimi联网问答）====================
# @机器人 自由提问F1技术/规则/历史问题，由Kimi联网搜索后回答
# 在 .env 中配置 MOONSHOT_API_KEY 即可启用，未配置则自动禁用
LLM_ASK_ENABLED = os.getenv("LLM_ASK_ENABLED", "true").lower() == "true"
LLM_ASK_COOLDOWN = 20          # 同一成员提问冷却时间（秒），防止刷屏
LLM_ASK_MAX_LEN = 8000         # 单次回答硬安全上限（防超长生成；展示时按 LLM_REPLY_CHUNK 分多条发送）
LLM_REPLY_CHUNK = 1500         # 每条消息最大字符数（长回答分多条连续发送，不再截断丢弃）
# 问答看门狗：超过 LLM_ASK_NOTICE_AFTER 秒未答完发进度提示（含阶段信息）；LLM_ASK_HARD_TIMEOUT 秒硬超时
#（硬超时即向后任务发取消信号：轮次边界停止并释放并发槽，已有部分内容由迟到投递补发）
LLM_ASK_NOTICE_AFTER = int(os.getenv("LLM_ASK_NOTICE_AFTER", "45"))
LLM_ASK_HARD_TIMEOUT = int(os.getenv("LLM_ASK_HARD_TIMEOUT", "600"))
# LLM并发上限：超出排队等待，防止触发API限流/烧配额（DeepSeek/Kimi均有并发限制）
LLM_MAX_CONCURRENCY = int(os.getenv("LLM_MAX_CONCURRENCY", "4"))

# ==================== 知识沉淀（对话→知识蒸馏）====================
# 两层闸门：①LLM事实校对（自动，工具核验）→ ②管理员人工审核（/kb 指令），
# 全部通过才入正式知识库并注入后续问答上下文（2026-09-15 用户确认：无需群内告知）
KB_ENABLED = os.getenv("KB_ENABLED", "true").lower() == "true"
# 管理员 openid 列表（逗号分隔），/kb 审核指令与候选 DM 通知的接收人
ADMIN_OPENIDS = [x.strip() for x in os.getenv("ADMIN_OPENIDS", "").split(",") if x.strip()]
KB_DISTILL_DAY = 0            # 每周蒸馏日（0=周一）
KB_DISTILL_HOUR = 10          # 蒸馏执行小时
# 纠错意图词：命中且引用了机器人消息时，把该轮问答作为知识候选入队
CORRECTION_PATTERNS = ("不对", "错了", "有误", "应该是", "纠正", "胡说", "瞎说", "搞错", "其实")

import threading as _threading
from concurrent.futures import ThreadPoolExecutor as _ThreadPoolExecutor
_LLM_EXECUTOR = _ThreadPoolExecutor(max_workers=LLM_MAX_CONCURRENCY, thread_name_prefix="llm-ask")
_llm_slots = _threading.BoundedSemaphore(LLM_MAX_CONCURRENCY)

# ==================== 每周比赛周预告（含赛道纪录LLM核查）====================
WEEKLY_PREVIEW_ENABLED = os.getenv("WEEKLY_PREVIEW_ENABLED", "true").lower() == "true"
WEEKLY_PREVIEW_DAY = 0         # 0=周一
WEEKLY_PREVIEW_HOUR = 9        # 推送小时
WEEKLY_PREVIEW_MINUTE = 0      # 推送分钟
# 是否用LLM联网核查赛道圈速纪录（不一致时以LLM为准并回写本地json）
WEEKLY_VERIFY_LAP_RECORD = os.getenv("WEEKLY_VERIFY_LAP_RECORD", "true").lower() == "true"

# ==================== 升级件/PU数据源 ====================
# fia=FIA官方文档（默认，权威，含备注描述）; cosmos=第三方F1Cosmos
UPGRADE_DATA_SOURCE = os.getenv("UPGRADE_DATA_SOURCE", "fia").strip().lower()

# ==================== 车手评分（DOTD投票）====================
# 评分网页表单服务地址（部署在服务器上，群友点击访问）
RATING_BASE_URL = os.getenv("RATING_BASE_URL", "https://your-domain.example.com")
RATING_ENABLED = os.getenv("RATING_ENABLED", "true").lower() == "true"
# 环节成绩私聊推送开关：给设置了关注车手（/setdriver）且可加私聊的用户单发其车手成绩
DM_SESSION_RESULT = os.getenv("DM_SESSION_RESULT", "true").lower() == "true"
# F1新闻速递开关（每日 8:00/19:00 推送；LLM 不可用时推送英文原标题）
NEWS_ENABLED = os.getenv("NEWS_ENABLED", "true").lower() == "true"

# ==================== 实时计时面板 ====================
# 实时面板链接（默认复用 RATING_BASE_URL，可单独配置 LIVE_BASE_URL）
LIVE_BASE_URL = os.getenv("LIVE_BASE_URL") or RATING_BASE_URL

# ==================== 群指令监听器（Webhook 模式） ====================
COMMAND_LISTENER_ENABLED = True
# QQ Webhook 签名密钥（在 q.qq.com → 开发设置 → 验证密钥 中获取）
WEBHOOK_SECRET = os.getenv("BOT_WEBHOOK_SECRET", "")
# Webhook 回调路径 QQ 平台配置为: https://your-domain.example.com/bot/callback
WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", "8090"))

HELP_TEXT = """🏎️ F1机器人命令列表：

/drivers - 当前车手积分榜
/teams - 当前车队积分榜
/calendar - 全年赛历
/next [地点/赛道/R几] - 分站周末赛程（如 /next 蒙扎、/next R16；不带参数为下一站）
/last [环节] - 上一场比赛成绩（环节可选: fp1/fp2/fp3/qualy/sprint/race）
/gp 地点 [年份] [环节] - 分站成绩查询（如 /gp 蒙扎、/gp 斯帕 2022 race）
/rate - 获取评分链接+验证码（验证码一次有效）
/ratings [地点] - 查看车手评分榜
/dotd - 赛季最佳车手累积榜
/watch - 观赛直播链接
/live - 实时计时面板（比赛周内可用，含悬浮窗）
/predict [地点] - 本站排位/正赛名次统计模型预测（如 /predict 蒙扎）
/upgrades [车队] [地点/R几] [年份] - 升级件汇总（如 /upgrades 蒙扎 2024、/upgrades R5、/upgrades 法拉利 蒙扎 2024）
/upgrade 车队/部件 [地点/R几] [年份] - 升级件详细说明（含FIA文档链接）
/pu [车手/车队] [年份] - 动力单元部件用量（如 /pu 法拉利、/pu 维斯塔潘 2023）
/car [车队] - 事故维修成本
/pace [地点/R几] - 正赛pace分布统计图（箱线图+轮胎配方着色）
/racechart [地点/R几] - 赛后三联统计图（pace+轮胎策略+名次变化）
/trend - 本赛季车手积分走势图
/pitstops [地点/R几] - 进站时长车队对比箱线图
/sc [地点/R几] - 安全车/VSC/红旗事件时间线
/qualigap [地点/R几] - 排位差距图（杆位金色，含冲刺排位 /qualigap 雪邦 sq）
/tr 车手 引语 - DIY转播风格Team Radio卡片（如 /tr 维斯塔潘 simply lovely）
/telemetry - 遥测/圈速/赛果可视化分析（F1Cosmos仪表盘链接）
/setdriver 车手名 - 添加关注车手（如 /setdriver 维斯塔潘，最多3位）
/setteam 车队名 - 添加关注车队（如 /setteam 法拉利，最多3支）
/me - 查看我的设置
/unbind [车手|车队] [名字] - 解绑偏好（不带参数解绑全部）
/news on|off - 开关关注车手/主队的定向新闻私聊推送
/help - 显示本帮助

💬 @我 直接提问：F1技术、规则、历史赛季等问题（联网核实，支持多轮上下文追问）
如：@机器人 DRS的使用规则是什么？
/clear - 清空我的AI对话上下文
/kb - 知识库审核（管理员）

设置后，每站正赛结束会推送你关注的车手/车队排名⭐"""

WATCH_LINKS_TEXT = """📺 F1观赛链接：

腾讯体育 https://sports.qq.com/
腾讯F1专区 https://sports.qq.com/kbsweb/index.htm#100360
Overtake Fans https://overtakefans.com/

所有链接使用手机浏览器访问"""

WATCH_LINKS_MD = """## 📺 F1观赛链接

[腾讯体育](https://sports.qq.com/)
[腾讯F1专区](https://sports.qq.com/kbsweb/index.htm#100360)
[Overtake Fans](https://overtakefans.com/)

所有链接使用手机浏览器访问"""


def _help_button(i, label, data):
    return {
        "id": str(i),
        "render_data": {"label": label, "visited_label": label, "style": 1},
        "action": {
            "type": 2,
            "permission": {"type": 2},
            "data": data,
            "enter": True,
            "at_bot_show_channel_list": True,
            "unsupport_tips": "客户端版本过低，请升级QQ",
        },
    }


HELP_KEYBOARD = {
    "rows": [
        {"buttons": [_help_button(1, "🏆车手积分榜", "/drivers"), _help_button(2, "🏎️车队积分榜", "/teams")]},
        {"buttons": [_help_button(3, "📅全年赛历", "/calendar"), _help_button(4, "🏁下一站", "/next")]},
        {"buttons": [_help_button(5, "⏮️上一场成绩", "/last"), _help_button(6, "🔧升级件", "/upgrades")]},
        {"buttons": [_help_button(7, "🔋PU部件", "/pu"), _help_button(8, "📋我的设置", "/me")]},
        {"buttons": [_help_button(9, "⭐评分链接", "/rate"), _help_button(10, "📺观赛链接", "/watch")]},
    ]
}

HELP_MARKDOWN = """## 🏎️ F1机器人指令面板

**查询类**
/drivers 车手积分榜 · /teams 车队积分榜
/calendar 全年赛历 · /next [地点/赛道/R几] 分站周末赛程（如 /next 蒙扎、/next R16）
/weather [地点/R几] 分站天气/赛道温度/风速风向
/last [环节] 上一场成绩（fp1/fp2/fp3/qualy/sq/sprint/race）
/gp 地点 [年份] [环节] 分站成绩（如 /gp 蒙扎、/gp 斯帕 2022 race）
/weather [地点/R几] 分站天气与赛道温度（如 /weather 蒙扎）
/watch 观赛直播链接 · /live 实时计时面板

**🔧 技术数据**
/upgrades [车队] [地点/R几] [年份] 升级件汇总（如 /upgrades 蒙扎 2024、/upgrades 法拉利 R16 2024）
/upgrade 车队/部件 [地点/R几] [年份] 升级件详细说明（含FIA文档）
/pu [车手/车队] [年份] 动力单元部件用量（历史赛季为FIA赛季末累计快照）
/predict [地点] 本站排位/正赛名次统计模型预测（回测基准：领奖台命中2.3/场）
/telemetry 遥测/圈速/赛果可视化分析
/pace [地点/R几] 正赛pace分布统计图（箱线图+配方着色+中位差条形图）
/racechart [地点/R几] 赛后三联统计图（pace+轮胎策略+名次变化）
/trend 本赛季车手积分走势图
/pitstops [地点/R几] 进站时长车队对比箱线图
/sc [地点/R几] 安全车/VSC/红旗事件时间线
/qualigap [地点/R几] 排位差距图（含冲刺排位，加 sq 后缀）
/tr 车手 引语 DIY转播风格TR卡片
/car [车队] 事故维修成本

**⭐ 车手评分**
/rate 获取专属评分链接（正赛后开启）
/ratings [地点] 评分榜 · /dotd 赛季最佳车手榜

**个性化设置**
/setdriver 车手名 — 添加关注车手（最多3位）
/setteam 车队名 — 添加关注车队（最多3支）
/me 查看我的设置 · /unbind [车手|车队] [名字] 解绑
/news on|off 定向新闻推送开关（与关注车手/主队相关的新闻私聊推送）

**💬 AI问答**
@我 直接提问F1技术/规则/历史问题（联网核实，支持多轮上下文追问）
/clear 清空我的AI对话上下文

设置后，每站正赛结束会推送你关注的车手/车队排名⭐"""


# 环节参数别名（/last 与 /gp 共用）
SESSION_ARG_ALIASES = {
    "fp1": "fp1", "fp2": "fp2", "fp3": "fp3",
    "qualy": "qualifying", "qualifying": "qualifying", "排位": "qualifying", "排位赛": "qualifying",
    "sprint": "sprint", "冲刺": "sprint", "冲刺赛": "sprint",
    "sq": "sprint_qualifying", "sprintqualy": "sprint_qualifying", "冲刺排位": "sprint_qualifying", "冲刺排位赛": "sprint_qualifying",
    "race": "race", "正赛": "race",
}


# parse_upgrade_query / find_race_for_query 已迁移至 common/f1_tools.py（顶部导入）


def _get_driver_names_for_race(f1_api, race: dict) -> dict:
    """获取某场次车手显示名 {driver_id: name}"""
    try:
        result = f1_api.get_session_results(race["round"], "race", season=race["season"])
        if result and result.get("entries"):
            return {e["driver_id"]: e["driver_name"] for e in result["entries"]}
    except Exception:
        pass
    return {}


# 已知指令名（不带斜杠），用于兼容QQ指令面板点选
# QQ指令面板点选后发送的是不带/的指令名，客户端不会自动补/
COMMAND_NAMES = frozenset([
    "help", "drivers", "teams", "calendar", "next", "last", "gp",
    "watch", "weather", "setdriver", "setteam", "me", "unbind", "news", "rate", "ratings", "dotd",
    "upgrades", "upgrade", "pu", "car", "clear", "memory", "live", "telemetry", "predict", "pace",
    "racechart", "trend", "pitstops", "sc", "qualigap",
    "kb", "bili_login", "models", "model", "tr", "llm",
])


def _parse_command(content: str):
    """解析指令，兼容两种格式：
    - 用户手输: /drivers（带斜杠）
    - QQ指令面板点选: drivers（不带斜杠）
    Returns: (cmd, arg) 或 (None, None) 非指令
    """
    c = (content or "").strip()
    if not c:
        return None, None
    parts = c.split(None, 1)
    first = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    if first.startswith("/"):
        return first, arg
    if first in COMMAND_NAMES:
        return "/" + first, arg
    return None, None


def _extract_mentioned_entities(text: str):
    """从提问文本提取命中的车手/车队标准名（画像统计用，别名全表子串扫描）"""
    drivers, teams = set(), set()
    t = (text or "").lower()
    if not t:
        return drivers, teams
    try:
        from common.drivers_profile import get_driver_aliases, get_team_aliases
        for alias, canon in get_driver_aliases().items():
            if alias and alias.lower() in t:
                drivers.add(canon)
        for alias, canon in get_team_aliases().items():
            if alias and alias.lower() in t:
                teams.add(canon)
    except Exception:
        pass
    return drivers, teams


# 知识候选事实校对钩子（create_command_client 注入，每周蒸馏任务复用）
_kb_factcheck_hook = None


def create_command_client(f1_api, prefs_store, llm=None, ratings_store=None, qq_bot=None, f1cosmos=None):
    """创建指令监听botpy客户端"""
    import botpy
    from botpy.message import GroupMessage

    # 被动回复 msg_id 过期时的主动推送兜底（群聊发到来源群，私聊发 DM）
    async def _proactive_send(message, text: str, md: str = None):
        if not qq_bot:
            return
        gid = getattr(message, "group_openid", None)
        author = getattr(message, "author", None)
        uid = None
        for attr in ("member_openid", "id", "user_openid"):
            uid = getattr(author, attr, None) if author is not None else None
            if uid:
                break
        try:
            if gid:
                qq_bot._tl.gid = gid  # 显式绑定来源群，防串群
                try:
                    if md:
                        qq_bot.send_markdown_message(md, fallback_text=text)
                    else:
                        qq_bot.send_group_message(text)
                finally:
                    qq_bot._tl.gid = None
            elif uid:
                if md:
                    qq_bot.send_dm_markdown(uid, md, fallback_text=text)
                else:
                    qq_bot.send_dm_message(uid, text)
            logger.info(f"msg_id过期降级主动推送完成 (群={bool(gid)} 用户={(uid or '?')[:8]})")
        except Exception as se:
            logger.warning(f"主动推送兜底也失败: {se}")

    _replies.set_proactive_send_hook(_proactive_send)

    # 图片发送后端注入（闭包 qq_bot；模块级 reply_image 无权访问该闭包，
    # 必须走钩子——见 _reply_image_backend 注释的事故记录）
    def _reply_image_backend_impl(openid, url, content, is_dm, msg_id, msg_seq):
        if not qq_bot:
            return False
        return qq_bot.reply_image_url(openid, url, content=content, is_dm=is_dm,
                                      msg_id=msg_id, msg_seq=msg_seq)

    _replies.set_reply_image_backend(_reply_image_backend_impl)

    def _kb_factcheck(cid: int, claim: str):
        """知识沉淀第一层闸门（事实校对）：LLM+数据工具核验候选说法，
        结果回填候选区；校对通过的 DM 通知管理员人工审核（第二层闸门）"""
        if not llm or not llm.enabled:
            return
        from common.knowledge_store import KnowledgeStore
        ks = KnowledgeStore.get()
        _llm_slots.acquire()
        try:
            verify_q = (
                "请核实以下群内提出的 F1 相关说法是否属实。可调用数据工具获取真实数据佐证。\n"
                "只输出一个 JSON 对象：{\"verdict\": \"属实\" | \"有误\" | \"无法确定\", "
                "\"evidence\": \"依据与数据来源（200字内）\"}\n\n说法：\n" + claim)
            verdict_text = llm.ask_with_tools("kb-factcheck", verify_q,
                                              F1_TOOLS, f1_tool_handlers, 800)
            passed, evidence = False, ""
            if verdict_text:
                import json as _json, re as _re2
                m = _re2.search(r"\{.*\}", verdict_text, _re2.S)
                if m:
                    try:
                        v = _json.loads(m.group(0))
                        verdict = v.get("verdict", "")
                        evidence = v.get("evidence", "")
                        passed = (verdict == "属实")
                        if verdict == "无法确定":
                            # 不确定不淘汰（可能是真冷门知识），交人工裁决
                            passed = True
                            evidence = "（校对无法确定，请人工判断）" + evidence
                    except Exception:
                        evidence = verdict_text[:200]
            ks.set_factcheck_result(cid, passed, evidence)
            if passed and ADMIN_OPENIDS and qq_bot:
                plain = (f"📚 新知识候选 #{cid} 待审核\n{claim[:400]}\n"
                         f"校对依据：{evidence[:300]}\n"
                         f"私聊回复 /kb ok {cid} 入库 · /kb no {cid} 拒绝")
                md = f"## 📚 新知识候选 #{cid} 待审核\r\r{claim[:400]}\r\r校对依据：{evidence[:300]}\r\r私聊回复 /kb ok {cid} 入库 · /kb no {cid} 拒绝"
                for admin in ADMIN_OPENIDS:
                    try:
                        qq_bot.send_dm_markdown(admin, md, fallback_text=plain)
                    except Exception:
                        pass
        except Exception as e:
            logger.warning(f"候选 #{cid} 事实校对异常: {e}")
            ks.set_factcheck_result(cid, True, f"（校对流程异常 {e}，请人工判断）")
        finally:
            _llm_slots.release()

    def _kb_factcheck_bg(cid: int, claim: str):
        _threading.Thread(target=_kb_factcheck, args=(cid, claim),
                          daemon=True, name="kb-factcheck").start()

    global _kb_factcheck_hook
    _kb_factcheck_hook = _kb_factcheck_bg

    # LLM提问冷却记录 {member_openid: last_timestamp}
    _llm_cooldowns = {}

    # 同用户在途请求互斥（文字/识图共用）：上一条未答完时拒绝新提问，
    # 对应Agent架构的会话级存活流互斥（深度分析最长180s，20s冷却拦不住同用户并发）
    _llm_inflight = set()
    # /bili_login 防重入状态（type 造轻量命名空间，属性可写）
    _bili_login_state = type("BiliLoginState", (), {"inflight": False})()
    _llm_inflight_lock = _threading.Lock()

    # AI数据快照缓存（避免每次提问都重建，TTL 5分钟）
    _data_ctx_cache = {"text": None, "ts": 0}

    def _build_f1_data_context() -> str:
        """构建当前最新F1数据快照，注入AI上下文供引用（积分榜/下一站/最近成绩/PU/升级件）"""
        import time as _t
        from datetime import datetime as _dt, timezone as _tzu
        from pytz import timezone as _tz

        now = _t.time()
        if _data_ctx_cache["text"] and now - _data_ctx_cache["ts"] < 300:
            return _data_ctx_cache["text"]

        parts = []
        local_tz = _tz('Asia/Shanghai')

        # 车手/车队积分榜（top 8，强制刷新避免AI引用旧数据）
        try:
            standings = f1_api.get_current_standings(force_refresh=True)
            d_entries = QQGroupBot._parse_driver_standings(standings.get("drivers") or {})
            if d_entries:
                tops = [f"P{e.get('position')} {e.get('Driver',{}).get('familyName','')} {e.get('points','0')}分" for e in d_entries[:8]]
                parts.append("车手积分榜: " + "; ".join(tops))
            t_entries = QQGroupBot._parse_constructor_standings(standings.get("constructors") or {})
            if t_entries:
                tops = [f"P{e.get('position')} {e.get('Constructor',{}).get('name','')} {e.get('points','0')}分" for e in t_entries[:8]]
                parts.append("车队积分榜: " + "; ".join(tops))
        except Exception:
            pass

        # 下一站比赛 + 各环节时间
        try:
            race = f1_api.get_next_race()
            if race:
                line = f"下一站: {race['raceName']} @ {race['Circuit']['circuitName']}"
                sess = f1_api.get_all_sessions(race)
                if sess:
                    times = []
                    for s in sess:
                        lt = s['datetime'].astimezone(local_tz)
                        times.append(f"{s['name']} {lt.strftime('%m月%d日%H:%M')}")
                    line += " | " + ", ".join(times)
                parts.append(line)
        except Exception:
            pass

        # 最近一场正赛结果 top 6
        try:
            last = f1_api.get_last_completed_session()
            if last and last.get('type') == 'race':
                res = f1_api.get_session_results(int(last['round']), 'race')
                if res and res.get('entries'):
                    tops = [f"P{e['position']} {e['driver_name']}" for e in res['entries'][:6]]
                    parts.append(f"最近正赛({res.get('race_name','')})结果: " + "; ".join(tops))
        except Exception:
            pass

        # 动力单元用量（超量高亮）
        if f1cosmos:
            try:
                elements = f1cosmos.get_elements()
                if elements:
                    high = [e for e in elements if e.get('total', 0) >= 25]
                    if high:
                        names = [f"{e['last_name']}({e['total']})" for e in high]
                        parts.append("动力单元部件用量较高（接近罚退）: " + ", ".join(names))
            except Exception:
                pass

        text = "\n".join(parts)
        # 防误判提示：快照只是当前赛季；历史赛季必须走带 year 参数的工具
        text += (f"\n\n注意：以上快照仅为当前赛季（{SEASON}）数据。"
                 f"用户问历史赛季（如2025）的成绩/积分榜时，必须调用 get_season_results / "
                 f"get_race_results / get_driver_standings / get_constructor_standings 并传 year 参数获取真实数据；"
                 f"这些工具支持1950年至今全部赛季。禁止把当前赛季数据标注为其他年份，禁止凭记忆编造名次。")
        _data_ctx_cache["text"] = text
        _data_ctx_cache["ts"] = now
        return text

    # ===== F1 数据工具（供 AI function calling 主动调用） =====
    # schema 与 handler 已收敛至 common/f1_tools.py（薄适配层）
    f1_tool_handlers = build_handlers(f1_api, f1cosmos, SEASON)

    # 预测模型后台预热：首次 predict 冷启动 ~184s（建本赛季数据集+车队实力分析，
    # 2026-09-15 实测），挪到启动阶段后台执行。冠军推演内部会对剩余全部分站逐站
    # predict，一次预热全部吃热缓存（否则冠军推演首次调用要逐站冷启动 ~22s×N站）
    def _warm_prediction_model():
        try:
            f1_tool_handlers["get_championship_outlook"]()
            logger.info("✓ 预测模型预热完成（含剩余分站逐站缓存）")
        except Exception as e:
            logger.warning(f"预测模型预热失败（不影响功能）: {e}")
        try:
            f1_tool_handlers["get_race_prediction"]()
        except Exception:
            pass
    _threading.Thread(target=_warm_prediction_model, daemon=True).start()

    # F1Cosmos 可视化仪表盘（遥测/圈速/赛果分析，车友社区免费项目）
    F1COSMOS_DASH_BASE = "https://f1cosmos.com/zh/dashboard/race"

    # ===== 指令处理器（handlers 包）装配 =====
    # 回复函数注入 main 模块级代理：调用时动态解析 main.reply_* 全局名，
    # 外部替换 main.reply_*（如测试注入记录器）对全部分发链路即时生效
    try:
        from .handlers import COMMAND_HANDLERS, HandlerContext
    except ImportError:
        from handlers import COMMAND_HANDLERS, HandlerContext

    async def _ctx_reply_text(message, content):
        await reply_text(message, content)

    async def _ctx_reply_md(message, text, md):
        await reply_md(message, text, md)

    async def _ctx_reply_image(message, image_url, content="📊 统计图"):
        await reply_image(message, image_url, content)

    handler_ctx = HandlerContext(
        f1_api=f1_api, prefs_store=prefs_store, llm=llm,
        ratings_store=ratings_store, qq_bot=qq_bot, f1cosmos=f1cosmos,
        season=SEASON, session_arg_aliases=SESSION_ARG_ALIASES,
        admin_openids=ADMIN_OPENIDS,
        rating_base_url=RATING_BASE_URL, rating_enabled=RATING_ENABLED,
        live_base_url=LIVE_BASE_URL, f1cosmos_dash_base=F1COSMOS_DASH_BASE,
        help_text=HELP_TEXT, help_markdown=HELP_MARKDOWN, help_keyboard=HELP_KEYBOARD,
        watch_links_text=WATCH_LINKS_TEXT, watch_links_md=WATCH_LINKS_MD,
        bili_login_state=_bili_login_state,
        get_driver_names_for_race=lambda race: _get_driver_names_for_race(f1_api, race),
        reply_text=_ctx_reply_text, reply_md=_ctx_reply_md, reply_image=_ctx_reply_image,
    )

    # 工具调用声明的展示名映射
    TOOL_LABELS = {
        "get_driver_standings": "车手积分榜", "get_constructor_standings": "车队积分榜",
        "get_next_race": "赛程", "get_race_results": "分站成绩", "get_season_results": "赛季成绩",
        "get_pu_quota": "动力单元用量", "get_upgrades": "升级件", "get_race_weather": "天气",
        "get_upgrade_analysis_data": "升级分析数据包", "verify_lap_record": "圈速纪录核查",
        "get_telemetry_chart": "遥测链接", "get_race_prediction": "统计预测模型",
        "get_team_strengths": "车队实力画像", "get_championship_outlook": "总冠军推演",
        "get_stint_analysis": "stint节奏分析",
        "web_search": "联网搜索", "$web_search": "联网搜索",
    }

    def _build_source_footer(meta: dict) -> str:
        """数据来源与工具调用声明尾注（meta 由 ask_layered/ask_with_tools 的 meta_out 回填）"""
        if not meta:
            return ""
        tools = meta.get("tools") or []
        used = []
        for t in tools:
            label = TOOL_LABELS.get(t, t)
            if label == "联网搜索":
                continue  # 搜索只在 🌐 部分体现，避免重复声明
            if label not in used:
                used.append(label)
        parts = []
        if used:
            parts.append("🛠️ 数据工具: " + "、".join(used))
        if meta.get("web_search"):
            parts.append("🌐 联网搜索: 已执行")
        if parts:
            return "\n\n---\n📎 来源声明 | " + " | ".join(parts) + "（另附当前赛季实时快照）"
        return ""

    async def handle_llm_question(message, question: str, member_openid: str):
        """处理@机器人的自由提问，转发给Kimi联网LLM"""
        import time as _time
        import asyncio as _asyncio

        if not llm or not llm.enabled:
            await reply_text(message, "💡 AI问答功能未启用（未配置MOONSHOT_API_KEY）\n发送 /help 查看可用指令")
            return

        # 冷却+在途互斥合并为一次原子检查（防并发穿透；被拒绝时不刷新冷却窗口）
        now_ts = _time.time()
        with _llm_inflight_lock:
            last_ts = _llm_cooldowns.get(member_openid, 0)
            if now_ts - last_ts < LLM_ASK_COOLDOWN:
                reject = f"⏳ 提问太频繁啦，请 {int(LLM_ASK_COOLDOWN - (now_ts - last_ts))} 秒后再试"
            elif member_openid in _llm_inflight:
                reject = "⏳ 上一条还在回答中，请稍候…"
            else:
                reject = None
                _llm_cooldowns[member_openid] = now_ts
                _llm_inflight.add(member_openid)
                # 惰性清理过期冷却条目，防字典无限增长
                if len(_llm_cooldowns) > 1000:
                    for k in [k for k, v in _llm_cooldowns.items() if now_ts - v > 3600]:
                        _llm_cooldowns.pop(k, None)
        if reject:
            await reply_text(message, reject)
            return

        try:
            # 引用消息：用户引用（回复）机器人此前推送的卡片/消息时，取回其内容注入AI上下文
            full_question = question
            ref_content = None
            try:
                # B站链接确定性前置拦截（裸链接无指令词时 L1 可能不调
                # get_video_summary 直接凭印象拒答——提示词不可靠，意图注入代码兜底）
                from common.bilibili_summary import BiliVideoClient as _BVC
                _bvid = _BVC.parse_bvid(question)
                if _bvid:
                    full_question = (
                        f"用户发来B站视频链接（BV号 {_bvid}），希望AI总结/讲解该视频内容。"
                        f"必须调用 get_video_summary 工具（url 传原始链接或 {_bvid}）获取字幕文本后再回答，"
                        "禁止直接说无法访问视频。\n"
                        f"【用户原始消息】{question}")
            except Exception:
                pass
            try:
                ref = getattr(message, "message_reference", None)
                ref_id = getattr(ref, "message_id", None) if ref else None
                if ref_id and qq_bot:
                    ref_content = qq_bot.get_referenced_content(ref_id)
                    if ref_content:
                        full_question = (
                            "【最高优先级上下文 · 用户引用的消息】\n"
                            f"{ref_content}\n"
                            "（引用内容是本次问题的主题与事实基准：其中已发生环节的成绩/"
                            "数据为既定事实，禁止用预测值或记忆覆盖；分析必须基于引用内容展开）\n\n"
                            f"【用户的问题】\n{question}")
                        logger.info(f"检测到引用机器人消息(id={ref_id[:12]}...)，已注入AI上下文(最高优先级)")
                    else:
                        # 引用的是群友消息：QQ平台无接口读取，机器人只能读取自己发过的消息
                        await reply_text(message, "💡 我看不到你引用的群友消息（QQ平台限制），接下来仅按你的文字问题回答；如需针对引用内容，请复制原文发给我")
            except Exception as e:
                logger.warning(f"引用消息处理失败: {e}")

            # 知识沉淀接线：用户画像统计（偏好习惯）+ 引用机器人消息的纠错捕获标记
            is_correction = False
            if KB_ENABLED:
                try:
                    from common.knowledge_store import KnowledgeStore
                    _kd, _kt = _extract_mentioned_entities(question)
                    KnowledgeStore.get().note_question(member_openid, sorted(_kd), sorted(_kt))
                except Exception:
                    pass
                is_correction = bool(ref_content) and any(p in question for p in CORRECTION_PATTERNS)

            # "记住这个/请记住" 明示锚定：直接写入该用户 L3 长期记忆（不衰减）
            if llm and any(p in question for p in ("记住", "记一下", "请记住", "别忘了")):
                try:
                    llm._memory.mark_anchor(member_openid, question, source="explicit", salience=3.0)
                except Exception:
                    pass

            await reply_text(message, "🔍 收到，正在联网查询…")
            ask_meta: dict = {}  # 工具调用声明回填
            from common.llm_assistant import new_qid
            qid = new_qid()  # 本次问答链路 trace ID（贯穿 L0/L1/L2 日志与工具审计）
            # 取消事件：硬超时后置位，后台任务在最近轮次边界停止并释放并发槽
            #（此前超时任务继续跑满 LLM 内部轮次，最坏再占槽数分钟，4个即可瘫痪全群AI并发）
            cancel_event = _threading.Event()

            # 通过信号量限流：并发已满时先提示排队，任务在LLM专用线程池中等待空位
            got_slot = _llm_slots.acquire(blocking=False)
            if not got_slot:
                await reply_text(message, "⏳ 当前提问人数较多，已加入排队…")
                logger.info(f"LLM并发已满({LLM_MAX_CONCURRENCY})，用户提问进入排队")

            def _ask_with_slot():
                if not got_slot:
                    _llm_slots.acquire()
                try:
                    # 排队等到槽位时可能已被硬超时取消：不再启动耗时问答，直接释放
                    if cancel_event.is_set():
                        logger.info(f"排队任务已被取消，跳过执行 (用户 {member_openid[:8]}...)")
                        return None
                    return llm.ask_layered(member_openid, full_question, F1_TOOLS, f1_tool_handlers,
                                           LLM_ASK_MAX_LEN, data_context=_build_f1_data_context(),
                                           group_openid=getattr(message, 'group_openid', None),
                                           meta_out=ask_meta, cancel_event=cancel_event, qid=qid)
                finally:
                    _llm_slots.release()

            def _register_late_delivery(fut):
                """硬超时后注册迟到投递：后台任务最终完成时，把答案（含来源尾注）补发给用户。
                注意用 qq_bot 同步REST发送：webhook事件线程的事件循环在超时回复后已结束，async投递不可达"""
                def _deliver(f):
                    try:
                        late_answer = f.result()
                    except Exception as e:
                        logger.warning(f"迟到任务异常结束 (用户 {member_openid[:8]}...): {e}")
                        return
                    if not late_answer:
                        logger.info(f"迟到任务无结果 (用户 {member_openid[:8]}...)")
                        return
                    try:
                        late_answer += _build_source_footer(ask_meta)
                        chunks = _split_lines(late_answer, LLM_REPLY_CHUNK)
                        total = len(chunks)
                        # 多群部署：executor 线程的 thread-local 未设置目标群，
                        # 需显式绑定本条消息来源群，否则迟到答案会发到默认群（串群泄漏）
                        src_gid = getattr(message, 'group_openid', None)
                        for i, part in enumerate(chunks, 1):
                            header = f"⏰ 深度分析虽迟但到" + (f"（{i}/{total}）" if total > 1 else "")
                            md = f"## {header}\r\r{part}"
                            txt = header + "\n" + part
                            if src_gid and qq_bot:
                                qq_bot._tl.gid = src_gid
                                try:
                                    qq_bot.send_markdown_message(md, fallback_text=txt)
                                finally:
                                    qq_bot._tl.gid = None
                            elif qq_bot:
                                qq_bot.send_dm_markdown(member_openid, md, fallback_text=txt)
                        logger.info(f"✓ 迟到答案已补发 (用户 {member_openid[:8]}...)，"
                                    f"总耗时 {_time.time() - t_start:.0f}s")
                    except Exception as se:
                        logger.warning(f"迟到答案补发失败 (用户 {member_openid[:8]}...): {se}")
                fut.add_done_callback(_deliver)
                logger.info(f"已注册迟到投递 (用户 {member_openid[:8]}...)")

            try:
                loop = _asyncio.get_running_loop()
                t_start = _time.time()
                logger.info(f"LLM问答开始 qid={qid} (用户 {member_openid[:8]}...): {full_question[:60]}")
                fut = loop.run_in_executor(_LLM_EXECUTOR, _ask_with_slot)
                answer = None
                timed_out = False
                waited = 0.0
                # 分段等待：60s 首次提示，之后每 120s 进度提示，直到硬超时
                while waited < LLM_ASK_HARD_TIMEOUT:
                    step = (LLM_ASK_NOTICE_AFTER if waited == 0
                            else min(120, LLM_ASK_HARD_TIMEOUT - waited))
                    try:
                        answer = await _asyncio.wait_for(_asyncio.shield(fut), timeout=step)
                        break
                    except _asyncio.TimeoutError:
                        waited += step
                        if waited >= LLM_ASK_HARD_TIMEOUT:
                            break
                        # 按任务实际阶段给出进度提示（stage 由 ask_layered 回填）；
                        # 发送失败（如 msg_id 临期）仅记日志，绝不能中断问答等待
                        stage = ask_meta.get("stage", "")
                        stage_hint = {"rewrite": "正在理解问题",
                                      "collect": "正在调取F1数据（历史赛季FIA文档可能较慢）",
                                      "compose": "数据已就位，正在生成回答"}.get(stage, "正在处理")
                        try:
                            if waited <= LLM_ASK_NOTICE_AFTER:
                                await reply_text(message, f"⏳ {stage_hint}…")
                            else:
                                await reply_text(message, f"⏳ 仍在深度分析（{stage_hint}，已等待约{int(waited // 60)}分钟，完成后立即发出）…")
                        except Exception as ne:
                            logger.warning(f"进度提示发送失败（问答继续）qid={qid}: {ne}")
                        logger.info(f"LLM问答已等待 {waited:.0f}s stage={stage} (用户 {member_openid[:8]}...)")
                if answer is None and fut.done():
                    # 超时与完成撞车：任务恰好在等待超时时刻结束（best-effort 内容也可能已产出），
                    # 直接取结果发出，不能误报"查询失败"丢弃已到手的答案
                    try:
                        answer = fut.result()
                        if answer:
                            logger.info(f"LLM问答在超时边界完成，直接发出答案 (用户 {member_openid[:8]}...)")
                    except Exception as fe:
                        logger.error(f"超时边界取结果失败 (用户 {member_openid[:8]}...): {fe}")
                        answer = None
                if answer is None and not fut.done():
                    # 硬超时：取消后台任务（轮次边界停止、释放并发槽），
                    # 任务停止时若已有部分内容会尽力返回，由迟到投递补发
                    timed_out = True
                    cancel_event.set()
                    logger.error(f"LLM问答超时（>{LLM_ASK_HARD_TIMEOUT}s，已发取消信号+注册迟到投递）"
                                 f"(用户 {member_openid[:8]}...): {full_question[:60]}")
                    _register_late_delivery(fut)
            except Exception as e:
                logger.error(f"LLM问答失败 qid={qid}: {e}")
                answer = None
                timed_out = False
                # 异常退出（如进度提示 msg_id 过期）但后台任务仍在跑：注册迟到投递，不丢答案
                try:
                    if "fut" in dir() and fut is not None and not fut.done():
                        logger.info(f"异常退出但后台任务仍在运行，注册迟到投递 qid={qid}")
                        _register_late_delivery(fut)
                except Exception:
                    pass

            if answer:
                _metric_incr("llm_answer_ok")
                logger.info(f"LLM问答完成 (用户 {member_openid[:8]}...) "
                            f"耗时 {_time.time() - t_start:.0f}s，回答 {len(answer)} 字")
                answer_body = answer  # 页脚前原文（知识候选不带来源页脚）
                answer += _build_source_footer(ask_meta)
                await reply_long_md(message, answer)
                # 视频导读沉淀：本次问答调用了视频工具 → 导读要点入知识候选（视频=知识来源），
                # 走同一两层闸门：自动校对→管理员审核
                try:
                    if "get_video_summary" in (ask_meta.get("tools") or []) and _kb_factcheck_hook:
                        from common.knowledge_store import KnowledgeStore
                        claim = (f"视频导读知识（用户分享的F1视频）\n"
                                 f"原始消息：{question[:150]}\n导读要点：{answer_body[:300]}")
                        cid = KnowledgeStore.get().add_candidate(
                            claim, "video", qid=qid, user_id=member_openid)
                        if cid:
                            _kb_factcheck_hook(cid, claim)
                except Exception as _ve:
                    logger.warning(f"视频导读知识候选入队失败: {_ve}")
                # 纠错捕获：候选入队 → 后台事实校对（第一层）→ 管理员审核（第二层）
                if is_correction and _kb_factcheck_hook:
                    try:
                        from common.knowledge_store import KnowledgeStore
                        claim = (f"用户纠错主张：{question[:200]}\n"
                                 f"被纠错的机器人回答：{answer[:300]}\n"
                                 f"引用上下文：{(ref_content or '')[:300]}")
                        cid = KnowledgeStore.get().add_candidate(
                            claim, "correction", qid=qid, user_id=member_openid)
                        if cid:
                            _kb_factcheck_hook(cid, claim)
                    except Exception as ke:
                        logger.warning(f"纠错候选入队失败: {ke}")
                    # 纠错同时写入用户锚定记忆（L3，不衰减）：该用户指出的错误要长期记住
                    try:
                        if llm:
                            llm._memory.mark_anchor(
                                member_openid,
                                f"用户纠错：{question[:150]}（被纠正的回答要点: {answer[:100]}）",
                                source="correction", salience=3.0)
                    except Exception as _me:
                        logger.debug(f"纠错锚定写入失败: {_me}")
            else:
                _metric_incr("llm_answer_timeout" if timed_out else "llm_answer_fail")
                # 区分供应商级故障（余额/模型下线/限流）与一般失败，方便管理员排查
                # qid 键控读取：并发问答下实例级 last_error 会被其他用户覆盖
                provider_err = (llm.get_error(qid) if hasattr(llm, "get_error")
                                else getattr(llm, "last_error", None))
                try:
                    if timed_out:
                        # 硬超时：任务已被取消（轮次边界停止），如有部分内容会迟到补发
                        await reply_text(message, "⚠️ 查询等待超时，已中止后续分析；若已得出部分结果会自动补发")
                    elif provider_err:
                        logger.error(f"LLM供应商故障: {provider_err}")
                        await reply_text(message, "⚠️ AI服务暂时不可用（供应商配置/余额异常，请联系管理员检查 LLM 密钥与模型配置）")
                    else:
                        await reply_text(message, "⚠️ 查询失败，请稍后重试或换个问法")
                except Exception as send_err:
                    logger.error(f"超时/失败回复发送失败 (用户 {member_openid[:8]}...): {send_err}")
        finally:
            with _llm_inflight_lock:
                _llm_inflight.discard(member_openid)

    async def handle_vision_question(message, question: str, image_atts: list, member_openid: str):
        """处理@机器人带图片的消息，走Kimi视觉识别"""
        import time as _time
        import asyncio as _asyncio

        if not llm or not llm.enabled:
            await reply_text(message, "💡 图片识别功能未启用（未配置MOONSHOT_API_KEY）")
            return

        # 冷却+在途互斥合并为一次原子检查（与文字问答共用节奏）
        now_ts = _time.time()
        with _llm_inflight_lock:
            last_ts = _llm_cooldowns.get(member_openid, 0)
            if now_ts - last_ts < LLM_ASK_COOLDOWN:
                reject = f"⏳ 提问太频繁啦，请 {int(LLM_ASK_COOLDOWN - (now_ts - last_ts))} 秒后再试"
            elif member_openid in _llm_inflight:
                reject = "⏳ 上一条还在回答中，请稍候…"
            else:
                reject = None
                _llm_cooldowns[member_openid] = now_ts
                _llm_inflight.add(member_openid)
                if len(_llm_cooldowns) > 1000:
                    for k in [k for k, v in _llm_cooldowns.items() if now_ts - v > 3600]:
                        _llm_cooldowns.pop(k, None)
        if reject:
            await reply_text(message, reject)
            return

        try:
            # 提取图片URL（QQ附件可能是协议相对URL）
            urls = []
            for att in image_atts[:4]:
                u = att.get("url") if isinstance(att, dict) else getattr(att, "url", "")
                if u and u.startswith("//"):
                    u = "https:" + u
                if u:
                    urls.append(u)
            if not urls:
                await reply_text(message, "⚠️ 图片地址获取失败，请重新发送")
                # 即时失败不占冷却额度
                _llm_cooldowns.pop(member_openid, None)
                return

            logger.info(f"收到图片识别请求: {len(urls)} 张图 (from {member_openid})")
            await reply_text(message, "🔍 收到图片，正在识别…")
            from common.llm_assistant import new_qid
            vision_qid = new_qid()

            q = (question or "").strip() or (
                "请识别这张图片。如果是F1赛程表/成绩/积分榜截图，提取关键信息"
                "（时间统一换算为北京时间）；如果是官方赛程表，逐项列出各环节时间便于核对。"
            )

            def _vision_with_slot():
                _llm_slots.acquire()
                try:
                    return llm.ask_vision(q, urls, user_id=member_openid,
                                          group_openid=getattr(message, 'group_openid', None),
                                          qid=vision_qid)
                finally:
                    _llm_slots.release()

            def _register_vision_late_delivery(fut):
                """识图硬超时后注册迟到投递（与文字问答同款：完成时经 qq_bot REST 补发）"""
                def _deliver(f):
                    try:
                        late_answer = f.result()
                    except Exception as e:
                        logger.warning(f"识图迟到任务异常结束 (用户 {member_openid[:8]}...): {e}")
                        return
                    if not late_answer:
                        return
                    try:
                        chunks = _split_lines(late_answer, LLM_REPLY_CHUNK)
                        total = len(chunks)
                        src_gid = getattr(message, 'group_openid', None)
                        for i, part in enumerate(chunks, 1):
                            header = "⏰ 图片识别虽迟但到" + (f"（{i}/{total}）" if total > 1 else "")
                            md = f"## {header}\r\r{part}"
                            txt = header + "\n" + part
                            if src_gid and qq_bot:
                                qq_bot._tl.gid = src_gid
                                try:
                                    qq_bot.send_markdown_message(md, fallback_text=txt)
                                finally:
                                    qq_bot._tl.gid = None
                            elif qq_bot:
                                qq_bot.send_dm_markdown(member_openid, md, fallback_text=txt)
                        logger.info(f"✓ 识图迟到答案已补发 (用户 {member_openid[:8]}...)")
                    except Exception as se:
                        logger.warning(f"识图迟到答案补发失败 (用户 {member_openid[:8]}...): {se}")
                fut.add_done_callback(_deliver)

            try:
                loop = _asyncio.get_running_loop()
                t_start = _time.time()
                fut = loop.run_in_executor(_LLM_EXECUTOR, _vision_with_slot)
                try:
                    answer = await _asyncio.wait_for(_asyncio.shield(fut),
                                                     timeout=LLM_ASK_NOTICE_AFTER)
                except _asyncio.TimeoutError:
                    await reply_text(message, "⏳ 图片识别仍在处理…")
                    try:
                        answer = await _asyncio.wait_for(
                            _asyncio.shield(fut),
                            timeout=LLM_ASK_HARD_TIMEOUT - LLM_ASK_NOTICE_AFTER)
                    except _asyncio.TimeoutError:
                        logger.error(f"图片识别超时（>{LLM_ASK_HARD_TIMEOUT}s）"
                                     f"(用户 {member_openid[:8]}...)")
                        answer = None
                        if fut.done():
                            # 超时与完成撞车：任务恰好结束，直接取结果发出
                            try:
                                answer = fut.result()
                            except Exception as fe:
                                logger.error(f"识图超时边界取结果失败: {fe}")
                        elif not fut.done():
                            _register_vision_late_delivery(fut)
            except Exception as e:
                logger.error(f"图片识别失败: {e}")
                answer = None

            if answer:
                logger.info(f"图片识别完成 (用户 {member_openid[:8]}...) "
                            f"耗时 {_time.time() - t_start:.0f}s")
                await reply_long_md(message, answer + "\n\n💡 可直接继续追问这张图；其他群友可引用本条消息提问")
            else:
                await reply_text(message, "⚠️ 图片识别失败或超时，请稍后重试或改用文字提问")
        finally:
            with _llm_inflight_lock:
                _llm_inflight.discard(member_openid)

    class CommandClient(botpy.Client):
        async def on_ready(self):
            logger.info(f"✓ 指令监听器已上线: {self.robot.name}")

        async def on_group_add_robot(self, event):
            """机器人被拉入群时自动发欢迎卡片"""
            logger.info(f"机器人加入新群: {event.group_openid}")
            try:
                if qq_bot:
                    hello_md = ("## 🏎️ F1赛程助手已上线\r\r"
                                "自动推送F1比赛提醒/成绩/积分榜\r"
                                "比赛后可给车手打分（Driver of the Day）\r"
                                "任意F1问题直接@我提问（联网LLM回答）\r\r"
                                "发送 **/help** 查看完整功能列表")
                    qq_bot._tl.gid = event.group_openid
                    try:
                        qq_bot.send_markdown_message(hello_md, fallback_text="F1赛程助手已上线！发送 /help 查看功能")
                    finally:
                        qq_bot._tl.gid = None
            except Exception as e:
                logger.warning(f"新群欢迎消息发送失败: {e}")

        async def on_friend_add(self, event):
            """用户添加机器人为好友时自动发私聊欢迎消息"""
            logger.info(f"用户添加机器人: {event.openid}")
            try:
                hello = ("🏎️ F1赛程助手来啦！\r\r"
                         "在这里你可以：\r"
                         "- 查询积分榜/赛历/比赛成绩：发送 /drivers /teams /calendar /last\r"
                         "- 设置最喜爱车手/主队：发送 /setdriver 维斯塔潘\r"
                         "- 比赛后给车手打分：发送 /rate 获取评分链接\r"
                         "- 任意F1问题直接发给我（联网LLM回答）\r\r"
                         "发送 **/help** 查看完整指令列表\r"
                         "设置偏好后，每站正赛结束会私聊推送你关注的车手排名⭐")
                qq_bot.send_dm_markdown(event.openid, hello, fallback_text="F1赛程助手来啦！发送 /help 查看功能")
            except Exception as e:
                logger.warning(f"好友欢迎消息发送失败: {e}")

        async def on_group_at_message_create(self, message: GroupMessage):
            content = (message.content or "").strip()
            group_openid = getattr(message, 'group_openid', None)
            member_openid = getattr(getattr(message, "author", None), "member_openid", None)
            is_dm = not group_openid  # 与上方 getattr 探测保持一致（属性存在但为 None 时也按私聊处理）

            # 解析指令（兼容带/和不带/两种格式）
            cmd, arg = _parse_command(content)

            # 非指令文本：图片走视觉识别，文字转发给LLM问答，裸@给引导
            if cmd is None:
                attachments = getattr(message, "attachments", None) or []
                image_atts = [
                    a for a in attachments
                    if ((a.get("content_type") if isinstance(a, dict) else getattr(a, "content_type", "")) or "").startswith("image/")
                ]
                if image_atts:
                    try:
                        await handle_vision_question(message, content, image_atts, member_openid or "unknown")
                    except Exception as e:
                        logger.error(f"处理图片提问失败: {e}")
                elif content and LLM_ASK_ENABLED:
                    logger.info(f"收到LLM提问: {content} (from {member_openid})")
                    try:
                        await handle_llm_question(message, content, member_openid or "unknown")
                    except Exception as e:
                        logger.error(f"处理LLM提问失败: {e}")
                elif content:
                    await reply_text(message, "💡 AI问答功能未启用\n发送 /help 查看可用指令")
                else:
                    # 裸@（无文字无图）：可能是想发图/引用但机器人收不到对应内容
                    ref = getattr(message, "message_reference", None)
                    if ref and getattr(ref, "message_id", None):
                        await reply_text(message, "💡 我看不到你引用的群友消息（QQ平台限制机器人只能读取@我的消息）\n请把要讨论的内容复制发给我，或直接引用我发的卡片提问")
                    else:
                        await reply_text(message, "🏎️ 我在！发送 /help 查看指令，或直接输入F1问题\n（发图提问请把图片和@我放在同一条消息里）")
                return

            # /llm 参数可能含 API key：日志不落明文（此时 key 尚未注册进 secret_guard，
            # 过滤器拦不住——从源头脱敏）
            log_arg = "<redacted>" if cmd == "/llm" else arg
            logger.info(f"收到指令: {cmd} {log_arg} (from {member_openid})")
            _metric_incr("command_total")

            try:
                handler = COMMAND_HANDLERS.get(cmd.lstrip("/"))
                if handler:
                    await handler(handler_ctx, message, arg, member_openid, is_dm)
                else:
                    # 指令纠错建议：difflib 模糊匹配最接近的指令（确定性，不烧 LLM）
                    import difflib as _difflib
                    guess = _difflib.get_close_matches(cmd.lstrip("/"), sorted(COMMAND_NAMES),
                                                       n=1, cutoff=0.6)
                    hint = f"\n你是不是想用 /{guess[0]} ？" if guess else ""
                    await reply_text(message, f"未知命令: {cmd}{hint}\n发送 /help 查看命令列表")

            except Exception as e:
                logger.exception(f"处理指令失败 [{cmd} {arg}]: {e}")
                _metric_incr("command_error")
                try:
                    await reply_text(message, "⚠️ 指令处理失败，请稍后重试")
                except Exception:
                    pass

    return CommandClient


def start_command_listener(f1_api, prefs_store, llm=None, ratings_store=None, qq_bot=None, f1cosmos=None):
    """启动 Webhook HTTP 服务（替代 WebSocket 监听，支持 C2C/好友/群事件）"""
    import botpy

    CommandClient = create_command_client(f1_api, prefs_store, llm, ratings_store, qq_bot, f1cosmos)
    cmd_client = CommandClient(intents=botpy.Intents())

    from flask import Flask as _Flask
    webhook_app = _Flask("f1-bot-webhook")

    from common.webhook_handler import register_webhook
    register_webhook(webhook_app, WEBHOOK_SECRET, cmd_client, qq_bot, APPID, APP_SECRET)

    def _run():
        webhook_app.run(host="0.0.0.0", port=WEBHOOK_PORT, debug=False, use_reloader=False)

    import threading
    t = threading.Thread(target=_run, daemon=True, name="webhook-server")
    t.start()
    logger.info(f"✓ Webhook HTTP 服务已启动 (端口 {WEBHOOK_PORT})")
    return t


# ==================== 日志配置函数 ====================
def setup_logging():
    """配置日志（文件日志轮转，防无限膨胀）
    日志行含 qid 字段：问答链路（L0/L1/L2/工具/审计）经 thread-local 自动注入，
    排障时 grep "qid=xxxxxx" 捞出一次问答的完整链路"""
    from logging.handlers import RotatingFileHandler
    from common.llm_assistant import QIDLogFilter
    log_format = '%(asctime)s - %(name)s - %(levelname)s - [%(qid)s] %(message)s'

    qid_filter = QIDLogFilter()
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.addFilter(qid_filter)
    console_handler.setFormatter(logging.Formatter(log_format))

    # 文件日志轮转：单文件最大 5MB，最多保留 5 个（共约 25MB 上限）
    file_handler = RotatingFileHandler(
        LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=5, encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.addFilter(qid_filter)
    file_handler.setFormatter(logging.Formatter(log_format))

    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL),
        format=log_format,
        handlers=[console_handler, file_handler]
    )

    # 出网门禁：注册已知 env 密钥 + 运行时密钥，日志通道挂脱敏过滤器
    from common import secret_guard
    secret_guard.auto_register_env()
    try:
        from common import llm_secrets
        llm_secrets.register_all_to_guard()
    except Exception:
        pass
    secret_guard.install_log_filter()


# ==================== 主程序 ====================
class F1ReminderApp:
    """F1提醒应用主类"""
    
    def __init__(self):
        self.logger = logging.getLogger(__name__)
        self.f1_api = None
        self.qq_bot = None
        self.scheduler = None
        self.prefs_store = None
        self.ratings_store = None
        self.llm = None
        self.circuits_manager = None
        self.f1cosmos = None
        self.listener_thread = None
        self.running = False
        
    def initialize(self) -> bool:
        """初始化所有组件"""
        self.logger.info("=" * 60)
        self.logger.info("F1赛程提醒机器人[QQ官方群聊版]正在启动...")
        self.logger.info("=" * 60)
        
        # 检查配置
        has_error = False
        if "YOUR_APPID_HERE" in APPID:
            self.logger.error("❌ 错误：请先配置APPID！")
            self.logger.error("   编辑 qq_group_official_version/main.py 或 .env")
            has_error = True
        
        if "YOUR_SECRET_HERE" in APP_SECRET:
            self.logger.error("❌ 错误：请先配置APP_SECRET！")
            has_error = True
        
        if QQ_GROUP_OPENIDS == ["YOUR_GROUP_OPENID_HERE"]:
            self.logger.error("❌ 错误：请先配置QQ_GROUP_OPENID或QQ_GROUP_OPENIDS！")
            self.logger.error("   运行 get_group_openid.py 查看 group_openid")
            has_error = True
        
        if has_error:
            self.logger.error("\n⚠️ 请先完成配置后再运行！")
            return False
        
        # 初始化F1 API
        try:
            self.f1_api = F1API(season=SEASON)
            self.logger.info(f"✓ F1 API初始化成功 (赛季: {SEASON})")
        except Exception as e:
            self.logger.error(f"❌ F1 API初始化失败: {e}")
            return False
        
        # 初始化赛道数据管理器
        try:
            self.circuits_manager = CircuitsManager()
            self.logger.info("✓ 赛道数据管理器初始化成功")
        except Exception as e:
            self.logger.warning(f"⚠️ 赛道数据管理器初始化失败: {e}")
            self.circuits_manager = None

        # 初始化升级件/PU数据源（UPGRADE_DATA_SOURCE=fia 默认FIA官方文档，cosmos 回退第三方）
        try:
            from common.fia_docs import FIADocsAPI
            if UPGRADE_DATA_SOURCE == "fia":
                self.f1cosmos = FIADocsAPI(season=SEASON)
                # 注入赛程提供者，用于给事件级升级数据补充分站轮次
                self.f1cosmos.schedule_provider = self.f1_api.get_schedule_for_year
                self.logger.info("✓ 升级件/PU数据源初始化成功（FIA官方文档优先，Cosmos兜底）")
            else:
                self.f1cosmos = F1CosmosAPI(season=SEASON)
                self.logger.info("✓ F1Cosmos数据源初始化成功")
        except Exception as e:
            self.logger.warning(f"⚠️ 升级件/PU数据源初始化失败: {e}")
            self.f1cosmos = None

        # 初始化Kimi联网LLM助手（未配置API Key时自动禁用，不影响其他功能）
        try:
            self.llm = LLMAssistant()
            if self.llm.enabled:
                self.logger.info("✓ Kimi联网LLM助手初始化成功")
            else:
                self.logger.warning("⚠️ 未配置MOONSHOT_API_KEY，LLM问答与纪录核查已禁用")
        except Exception as e:
            self.logger.warning(f"⚠️ LLM助手初始化失败: {e}")
            self.llm = None

        # F1新闻采集器（每日 8:00/19:00 推送上一周期新闻，LLM 精简翻译）
        self.news_collector = None
        if NEWS_ENABLED:
            try:
                from common.f1_news import F1NewsCollector
                self.news_collector = F1NewsCollector()
            except Exception as e:
                self.logger.warning(f"新闻采集器初始化失败: {e}")

        # 初始化QQ群机器人
        try:
            self.qq_bot = QQGroupBot(
                appid=APPID,
                secret=APP_SECRET,
                group_ids=QQ_GROUP_OPENIDS,
                use_sandbox=USE_SANDBOX
            )
        except Exception as e:
            self.logger.error(f"❌ QQ机器人初始化失败: {e}")
            return False

        # 注册管理员告警钩子（common/ 层数据源异常 → DM ADMIN_OPENIDS）
        try:
            from common.admin_alert import set_admin_alert_hook

            def _admin_alert(title, detail):
                if not (ADMIN_OPENIDS and self.qq_bot):
                    return
                plain = f"⚠️ {title}\n{detail}"
                md = f"## ⚠️ {title}\r\r{detail}"
                for admin in ADMIN_OPENIDS:
                    try:
                        self.qq_bot.send_dm_markdown(admin, md, fallback_text=plain)
                    except Exception:
                        pass

            set_admin_alert_hook(_admin_alert)
        except Exception as e:
            self.logger.warning(f"管理员告警钩子注册失败: {e}")

        # 初始化调度器
        try:
            self.prefs_store = UserPrefsStore()
            self.pushed_store = PushedResultsStore()
            self.ratings_store = RatingsStore() if RATING_ENABLED else None
            config = {
                'TIMEZONE': TIMEZONE,
                'REMINDER_TIMES': REMINDER_TIMES,
                'POST_RACE_DELAY': POST_RACE_DELAY,
                'prefs_store': self.prefs_store,
                'pushed_store': self.pushed_store,
                'group_openid': DEFAULT_GROUP,
                'lap_record_tracker': self.circuits_manager,
                'ratings_store': self.ratings_store,
                'rating_base_url': RATING_BASE_URL,
                'SEASON': SEASON,
                'f1cosmos': self.f1cosmos,
                'dm_session_result': DM_SESSION_RESULT,
                'news_collector': self.news_collector,
                'llm': self.llm,
            }
            self.scheduler = ReminderScheduler(self.f1_api, self.qq_bot, config)
            self.logger.info("✓ 调度器初始化成功")
        except Exception as e:
            self.logger.error(f"❌ 调度器初始化失败: {e}")
            return False
        
        return True
    
    def start(self):
        """启动应用"""
        if not self.initialize():
            self.logger.error("应用初始化失败，退出")
            sys.exit(1)
        
        # 发送启动消息
        if SEND_STARTUP_MESSAGE:
            try:
                self.qq_bot.send_startup_message()
            except Exception as e:
                self.logger.warning(f"发送启动消息失败: {e}")
            # 启动时推送下一站详情卡片
            try:
                next_race = self.f1_api.get_next_race()
                if next_race:
                    sessions = self.f1_api.get_all_sessions(next_race)
                    text, md = QQGroupBot.format_next_race(next_race, sessions)
                    self.qq_bot.send_markdown_message(md, fallback_text=text)
            except Exception as e:
                self.logger.warning(f"推送下一站卡片失败: {e}")

        # 管理员指令速查卡 DM 推送（/llm /model /kb 等配置指令不在公开 /help 中）
        if os.getenv("ADMIN_GUIDE_PUSH", "true").lower() == "true" and ADMIN_OPENIDS and self.qq_bot:
            try:
                from common.llm_admin import ADMIN_QUICKREF
                for _admin in ADMIN_OPENIDS:
                    self.qq_bot.send_dm_markdown(
                        _admin, ADMIN_QUICKREF,
                        fallback_text=ADMIN_QUICKREF.replace("**", "").replace("`", ""))
            except Exception as e:
                self.logger.warning(f"管理员速查卡推送失败: {e}")
        
        # 启动调度器
        try:
            self.scheduler.start()
            self.scheduler.add_daily_update_job()
            self.scheduler.add_news_jobs()  # F1新闻速递（每日 8:00/19:00）
        except Exception as e:
            self.logger.error(f"启动调度器失败: {e}")
            sys.exit(1)

        # 车手档案赛季初核查提醒（profile 过旧时输出警告日志，见 data/drivers_profile.json）
        try:
            from common.drivers_profile import log_review_reminder
            log_review_reminder(SEASON)
        except Exception as e:
            self.logger.warning(f"车手档案核查提醒失败: {e}")
        
        # 启动群指令监听器
        if COMMAND_LISTENER_ENABLED:
            try:
                self.listener_thread = start_command_listener(self.f1_api, self.prefs_store, self.llm, self.ratings_store, self.qq_bot, self.f1cosmos)
            except Exception as e:
                self.logger.warning(f"启动指令监听器失败: {e}")

        # 添加每周比赛周预告任务（含赛道纪录LLM核查）
        if WEEKLY_PREVIEW_ENABLED:
            try:
                self._add_weekly_preview_job()
            except Exception as e:
                self.logger.warning(f"添加每周预告任务失败: {e}")

        # 添加升级件追更任务（周四18:00起每2小时探测FIA本周升级件文档，发布即推送）
        try:
            self._add_upgrades_watch_job()
        except Exception as e:
            self.logger.warning(f"添加升级件追更任务失败: {e}")

        # 添加每周知识蒸馏任务（从问答归档提取候选知识，校对+审核后入知识库）
        if KB_ENABLED:
            try:
                self._add_kb_distill_job()
            except Exception as e:
                self.logger.warning(f"添加知识蒸馏任务失败: {e}")

        self.running = True
        self.logger.info("✓ 应用启动成功！")
        self.logger.info("=" * 60)
        
        # 打印即将进行的比赛
        self._print_upcoming_races()
        
        # 主循环
        self._main_loop()
    
    def _add_kb_distill_job(self):
        """添加每周知识蒸馏任务（周一上午，从问答归档提取候选知识）"""
        from apscheduler.triggers.cron import CronTrigger
        from pytz import timezone
        self.scheduler.scheduler.add_job(
            func=self._weekly_kb_distill,
            trigger=CronTrigger(day_of_week=KB_DISTILL_DAY, hour=KB_DISTILL_HOUR,
                                minute=0, timezone=timezone(TIMEZONE)),
            id='weekly_kb_distill', replace_existing=True
        )
        self.logger.info(f"✓ 已添加每周知识蒸馏任务（周{KB_DISTILL_DAY + 1} {KB_DISTILL_HOUR:02d}:00）")

    def _weekly_kb_distill(self):
        """每周蒸馏：近7天问答归档 → LLM 提取候选事实 → 逐条事实校对 → 待人工审核队列。
        两层闸门与实时纠错捕获共用（事实校对走 _kb_factcheck_hook，审核走 /kb）"""
        from common.knowledge_store import KnowledgeStore
        ks = KnowledgeStore.get()
        try:
            # 兜底校对：无实时钩子来源的候选（scheduler 的 data_update 等），每周一并校对
            if _kb_factcheck_hook:
                for c in ks.pending_factcheck_candidates():
                    if c.get("source") != "distill":  # distill 本批下面会走
                        _kb_factcheck_hook(c["id"], c["claim"])
            entries = ks.read_qa_since(7)
            if len(entries) < 5:
                self.logger.info(f"知识蒸馏跳过：近7天归档不足（{len(entries)} 条）")
                return
            if not (self.llm and self.llm.enabled):
                self.logger.info("知识蒸馏跳过：LLM 不可用")
                return
            blob = "\n".join(f"Q:{e['q'][:150]}\nA:{e['a'][:250]}" for e in entries[-150:])
            text = self.llm.chat_simple(
                "你是F1知识管理员。从以下群问答记录中提取值得长期记住的 F1 事实"
                "（规则澄清、数据结论、反复出现的知识点）。忽略一次性预测、闲聊、纯查询。"
                "只输出 JSON 数组，最多5条：[{\"claim\": \"事实陈述（100字内）\"}]，无则输出 []",
                blob[:12000], timeout=90)
            if not text:
                return
            import json as _json, re as _re
            m = _re.search(r"\[.*\]", text, _re.S)
            if not m:
                return
            claims = _json.loads(m.group(0))
            added = 0
            for item in claims[:5]:
                claim = (item.get("claim") or "").strip() if isinstance(item, dict) else ""
                if not claim:
                    continue
                cid = ks.add_candidate(claim, "distill")
                if cid and _kb_factcheck_hook:
                    _kb_factcheck_hook(cid, claim)  # 后台线程逐条校对
                    added += 1
            self.logger.info(f"✓ 每周知识蒸馏完成：{len(entries)} 条归档 → {added} 条候选入校对")
        except Exception as e:
            self.logger.warning(f"每周知识蒸馏失败: {e}")

    def _add_weekly_preview_job(self):
        """添加每周比赛周预告任务"""
        from apscheduler.triggers.cron import CronTrigger
        from pytz import timezone

        local_tz = timezone(TIMEZONE)
        self.scheduler.scheduler.add_job(
            func=self._weekly_race_preview,
            trigger=CronTrigger(
                day_of_week=WEEKLY_PREVIEW_DAY,
                hour=WEEKLY_PREVIEW_HOUR,
                minute=WEEKLY_PREVIEW_MINUTE,
                timezone=local_tz
            ),
            id='weekly_race_preview',
            replace_existing=True
        )
        day_name = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][WEEKLY_PREVIEW_DAY]
        self.logger.info(f"✓ 已添加每周比赛周预告任务（{day_name} {WEEKLY_PREVIEW_HOUR:02d}:{WEEKLY_PREVIEW_MINUTE:02d}）")

    def _weekly_race_preview(self):
        """
        每周比赛周预告流程：
        1. 找到本周比赛
        2. 从本地circuits_data.json获取赛道数据
        3. LLM联网核查圈速纪录：一致用本地，不一致以LLM为准并回写本地json
        4. 发送比赛周预告
        """
        from datetime import timezone as _tzu

        try:
            self.logger.info("🔄 检查本周是否有比赛...")

            now = datetime.now(_tzu.utc)
            week_end = now + timedelta(days=7)

            schedule = self.f1_api.get_schedule()
            if not schedule:
                self.logger.warning("赛程数据不可用，跳过比赛周预告")
                return
            this_week_race = None
            for race in schedule:
                race_date = datetime.strptime(race['date'], '%Y-%m-%d').replace(tzinfo=_tzu.utc)
                if now <= race_date <= week_end:
                    this_week_race = race
                    break

            if not this_week_race:
                self.logger.info("本周没有比赛，跳过比赛周预告")
                return

            race_name = this_week_race['raceName']
            self.logger.info(f"本周比赛: {race_name}")

            # 获取本周所有环节
            sessions = self.f1_api.get_all_sessions(this_week_race)

            # 获取赛道数据（本地json）
            circuit_id = this_week_race.get('Circuit', {}).get('circuitId', '')
            circuit_name = this_week_race.get('Circuit', {}).get('circuitName', '')
            circuit_data = None
            if self.circuits_manager:
                circuit_data = self.circuits_manager.get_circuit(circuit_id)
                if not circuit_data:
                    found = self.circuits_manager.search_circuit(circuit_name)
                    if found:
                        circuit_id, circuit_data = found

            if not circuit_data:
                circuit_data = {
                    'name': circuit_name,
                    'flag': '🏁',
                    'lap_record': {}
                }

            # 圈速纪录核查：F1官网权威源优先，抓取失败才回退LLM联网核查
            llm_verified = False
            official_checked = False
            if self.circuits_manager and circuit_id:
                try:
                    local_record = circuit_data.get('lap_record', {})
                    official = self.circuits_manager.fetch_official_lap_record(circuit_id, SEASON)
                    if official:
                        official_checked = True
                        local_sec = self.circuits_manager.time_to_seconds(local_record.get('time', ''))
                        official_sec = self.circuits_manager.time_to_seconds(official['time'])
                        if official_sec and official_sec != local_sec:
                            self.logger.info(
                                f"圈速纪录与官网不一致，以官网为准: "
                                f"{local_record.get('time')} -> {official['time']}"
                            )
                            if self.circuits_manager.apply_official_record(circuit_id, official):
                                circuit_data['lap_record'] = official
                                llm_verified = True  # 复用"已核实更新"卡片标记
                        else:
                            self.logger.info("✓ 圈速纪录与F1官网一致")
                except Exception as e:
                    self.logger.warning(f"F1官网圈速纪录核查失败: {e}")

            # LLM联网核查（官网抓取失败时的兜底）
            if not official_checked and WEEKLY_VERIFY_LAP_RECORD and self.llm and self.llm.enabled and self.circuits_manager and circuit_id:
                try:
                    self.logger.info(f"🔍 LLM联网核查 {circuit_name} 圈速纪录...")
                    circuit_name_en = circuit_data.get('name_en') or circuit_name
                    local_record = circuit_data.get('lap_record', {})
                    llm_record = self.llm.verify_lap_record(circuit_name_en, local_record)

                    if llm_record:
                        if llm_record.get('changed'):
                            # 不一致：以LLM为准，回写本地json并更新推送数据
                            self.logger.info(
                                f"圈速纪录不一致，以LLM为准: {local_record.get('time')} -> {llm_record['time']}"
                            )
                            if self.circuits_manager.apply_llm_verified_record(circuit_id, llm_record):
                                circuit_data['lap_record'] = {
                                    'time': llm_record['time'],
                                    'driver': llm_record.get('driver') or llm_record.get('driver_en', ''),
                                    'driver_en': llm_record.get('driver_en', ''),
                                    'year': llm_record.get('year', '')
                                }
                                llm_verified = True
                        else:
                            self.logger.info("✓ 圈速纪录与本地数据一致，使用本地数据")
                except Exception as e:
                    self.logger.warning(f"LLM圈速纪录核查失败（使用本地数据）: {e}")

            self.qq_bot.send_weekly_preview(this_week_race, sessions, circuit_data, llm_verified=llm_verified)
            self.logger.info("✓ 比赛周预告发送成功")

            # 升级件汇总已挪到周四追更任务（FIA升级件申报文档约周五发布，周一尚无数据）

        except Exception as e:
            self.logger.error(f"✗ 发送比赛周预告失败: {e}")

    # 升级件追更：周四18:00起每2小时探测，最多21次（至周六12:00）
    UPGRADES_WATCH_MAX_ATTEMPTS = 21

    def _add_upgrades_watch_job(self):
        """添加升级件追更任务（FIA升级件申报文档约周五08:00CET发布，发布即推送）"""
        from apscheduler.triggers.cron import CronTrigger
        from pytz import timezone

        local_tz = timezone(TIMEZONE)
        self.scheduler.scheduler.add_job(
            func=self._upgrades_watch_step,
            args=[0],
            trigger=CronTrigger(day_of_week='thu', hour=18, minute=0, timezone=local_tz),
            id='upgrades_watch',
            replace_existing=True
        )
        self.logger.info("✓ 已添加升级件追更任务（周四18:00起，每2小时探测FIA文档）")

    def _upgrades_watch_step(self, attempt: int = 0):
        """探测本周分站升级件文档：发布则推送并标记，未发布则2小时后重试"""
        from datetime import timezone as _tzu
        from apscheduler.triggers.date import DateTrigger
        from pytz import timezone as _ptz

        try:
            if not self.f1cosmos:
                return

            now = datetime.now(_tzu.utc)
            week_end = now + timedelta(days=4)
            race = None
            for r in (self.f1_api.get_schedule() or []):
                race_date = datetime.strptime(r['date'], '%Y-%m-%d').replace(tzinfo=_tzu.utc)
                if now <= race_date <= week_end:
                    race = r
                    break
            if not race:
                self.logger.info("近4天无比赛，升级件追更跳过")
                return

            # 已推送过则跳过（容器重启容错）
            store = self.pushed_store
            if store and store.is_pushed(SEASON, race['round'], 'upgrades'):
                self.logger.info(f"升级件已推送过，跳过: {race['raceName']}")
                return

            up_text, up_md = self.f1cosmos.format_upgrades_by_gp(race, SEASON, llm=self.llm)
            if up_text and "暂无升级" not in up_text:
                self.qq_bot.send_markdown_message(up_md, fallback_text=up_text)
                self.logger.info(f"✓ 升级件汇总已推送: {race['raceName']}")
                if store:
                    store.mark_pushed(SEASON, race['round'], 'upgrades')
                # 主队升级个性化私聊卡片（设置了主队的用户，其主队本站有升级时推送明细）
                self._push_favorite_team_upgrades(race)
                return

            if attempt < self.UPGRADES_WATCH_MAX_ATTEMPTS:
                run_time = datetime.now(_ptz(TIMEZONE)) + timedelta(hours=2)
                self.scheduler.scheduler.add_job(
                    func=self._upgrades_watch_step,
                    args=[attempt + 1],
                    trigger=DateTrigger(run_date=run_time),
                    id=f'upgrades_watch_retry_{attempt}',
                    replace_existing=True
                )
                self.logger.info(f"升级件文档未发布，2小时后重试（第{attempt + 1}次）: {race['raceName']}")
            else:
                self.logger.warning(f"升级件追更超时放弃（本站可能无升级申报）: {race['raceName']}")
        except Exception as e:
            self.logger.warning(f"升级件追更失败: {e}")

    # constructorId -> 中文别名（用于把 /setteam 保存的主队匹配到 FIA 申报名）
    TEAM_ID_TO_CN_ALIAS = {
        "red_bull": "红牛", "ferrari": "法拉利", "mercedes": "梅赛德斯",
        "mclaren": "迈凯伦", "aston_martin": "阿斯顿马丁", "alpine": "阿尔派",
        "williams": "威廉姆斯", "rb": "小红牛", "racing_bulls": "小红牛",
        "sauber": "索伯", "audi": "奥迪", "haas": "哈斯", "cadillac": "凯迪拉克",
        "alpha_tauri": "小红牛", "alphatauri": "小红牛", "alfa_romeo": "阿尔法罗密欧",
        "racing_point": "赛点", "renault": "雷诺",
    }

    def _push_favorite_team_upgrades(self, race: Dict[str, Any]):
        """
        主队升级个性化推送：FIA升级申报文档发布后，
        向设置了主队（/setteam）的用户私聊推送其主队本站升级明细卡片（含具体描述）。
        主队本站未提交升级时不打扰。
        """
        try:
            if not self.prefs_store or not hasattr(self.qq_bot, 'send_dm_markdown'):
                return
            users = self.prefs_store.get_all_user_prefs()
            if not users:
                return

            circuit = race.get("Circuit", {}) or {}
            city = (circuit.get("Location", {}) or {}).get("locality", "")
            race_name = race.get("raceName", "")

            items = []
            if hasattr(self.f1cosmos, "get_event_upgrades"):
                items = self.f1cosmos.get_event_upgrades(race_name, SEASON, city=city)
            if not items:
                items = self.f1cosmos._match_updates(
                    self.f1cosmos.get_updates(SEASON), city, race_name)
            if not items:
                return

            sent = 0
            for user_id, pref in users.items():
                team_ids = UserPrefsStore.pref_teams(pref)
                if not team_ids:
                    continue
                try:
                    # 多主队：任一关注车队本站有升级即推（命中第一个）
                    matched = None
                    t_names = UserPrefsStore.pref_team_names(pref)
                    for team_id in team_ids:
                        # 匹配优先级：constructorId中文别名 > 保存的队名 > constructorId英文
                        cn_alias = self.TEAM_ID_TO_CN_ALIAS.get(team_id)
                        if cn_alias:
                            matched = self.f1cosmos.match_team(cn_alias, items)
                        if not matched and t_names.get(team_id):
                            matched = self.f1cosmos.match_team(t_names[team_id], items)
                        if not matched:
                            matched = self.f1cosmos.match_team(str(team_id).replace("_", " "), items)
                        if matched:
                            break
                    if not matched:
                        continue  # 关注车队本站均未提交升级，不推送

                    text, md = self.f1cosmos.format_team_gp_upgrades(
                        matched, race, SEASON, llm=self.llm)
                    if not md:
                        continue
                    header = f"🔔 你的主队 {matched} 在本周{race_name}提交了升级！"
                    ok = self.qq_bot.send_dm_markdown(
                        user_id, f"> {header}\r\r" + md, fallback_text=f"{header}\n\n{text}")
                    if ok:
                        sent += 1
                except Exception as ue:
                    self.logger.warning(f"主队升级卡片推送失败(用户{str(user_id)[:8]}...): {ue}")
            if sent:
                self.logger.info(f"✓ 主队升级个性化卡片已私聊推送 {sent} 位用户: {race_name}")
        except Exception as e:
            self.logger.warning(f"主队升级个性化推送失败: {e}")

    def _print_upcoming_races(self):
        """打印即将进行的比赛"""
        try:
            upcoming = self.f1_api.get_upcoming_sessions(hours_ahead=168)
            if upcoming:
                self.logger.info("\n📅 未来7天即将进行的F1环节：")
                from pytz import timezone
                local_tz = timezone(TIMEZONE)
                
                for session in upcoming[:5]:
                    local_time = session['datetime'].astimezone(local_tz)
                    time_str = local_time.strftime('%m月%d日 %H:%M')
                    self.logger.info(f"  • {session['race_name']} - {session['name']}: {time_str}")
            else:
                self.logger.info("📅 未来7天没有F1比赛")
        except Exception as e:
            self.logger.warning(f"获取即将进行的比赛失败: {e}")
    
    def _main_loop(self):
        """主循环"""
        import time
        self.logger.info("\n💡 提示：")
        self.logger.info("  - 按 Ctrl+C 停止程序")
        self.logger.info("  - 查看日志文件了解详细运行情况")
        self.logger.info("  - 提醒消息会自动@全体成员")
        self.logger.info("\n🤖 机器人正在运行中...\n")
        
        try:
            while self.running:
                time.sleep(1)
        except KeyboardInterrupt:
            self.logger.info("\n收到停止信号，正在关闭...")
        finally:
            self.shutdown()
    
    def shutdown(self):
        """关闭应用"""
        self.running = False
        self.logger.info("正在关闭应用...")
        
        if self.scheduler:
            try:
                self.scheduler.stop()
                self.logger.info("✓ 调度器已停止")
            except Exception as e:
                self.logger.error(f"✗ 停止调度器失败: {e}")
        
        self.logger.info("=" * 60)
        self.logger.info("F1赛程提醒机器人[QQ官方群聊版]已关闭")
        self.logger.info("=" * 60)


def signal_handler(signum, frame):
    """处理系统信号"""
    logger = logging.getLogger(__name__)
    logger.info(f"收到信号 {signum}，准备退出...")
    sys.exit(0)


def main():
    """程序入口函数"""
    setup_logging()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    app = F1ReminderApp()
    app.start()


if __name__ == "__main__":
    main()
