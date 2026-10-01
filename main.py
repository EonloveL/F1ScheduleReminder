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
    from common import (F1API, ReminderScheduler, UserPrefsStore, resolve_driver, resolve_team,
                        LLMAssistant, CircuitsManager, F1CosmosAPI, PushedResultsStore)
    from common.ratings_store import RatingsStore
    from common.qq_group_bot import QQGroupBot
    from common.f1_tools import F1_TOOLS, build_handlers, parse_upgrade_query, find_race_for_query, parse_race_query
except ImportError as e:
    print(f"❌ 导入错误：{e}")
    print("请确保common目录存在，并且包含必要的模块文件")
    sys.exit(1)

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


_msg_seq_counters = {}
_msg_seq_lock = _threading.Lock()
_msg_seq_ts = {}      # msg_id -> 最后使用时间（TTL 清理用）
_MSG_SEQ_TTL = 3600   # 回复链1小时内有效，超时淘汰防内存无限增长

# 环节参数别名（/last 与 /gp 共用）
SESSION_ARG_ALIASES = {
    "fp1": "fp1", "fp2": "fp2", "fp3": "fp3",
    "qualy": "qualifying", "qualifying": "qualifying", "排位": "qualifying", "排位赛": "qualifying",
    "sprint": "sprint", "冲刺": "sprint", "冲刺赛": "sprint",
    "sq": "sprint_qualifying", "sprintqualy": "sprint_qualifying", "冲刺排位": "sprint_qualifying", "冲刺排位赛": "sprint_qualifying",
    "race": "race", "正赛": "race",
}


# parse_upgrade_query / find_race_for_query 已迁移至 common/f1_tools.py（顶部导入）


def _next_msg_seq(msg_id: str) -> int:
    """同一条消息的多次回复需要递增的msg_seq，否则被平台去重(40054005)
    加锁防并发重复分配（webhook每事件独立线程）；TTL淘汰防计数器无限增长"""
    import itertools
    import time as _t
    now = _t.time()
    with _msg_seq_lock:
        # 惰性淘汰过期条目
        if len(_msg_seq_ts) > 500:
            for k in [k for k, v in _msg_seq_ts.items() if now - v > _MSG_SEQ_TTL]:
                _msg_seq_ts.pop(k, None)
                _msg_seq_counters.pop(k, None)
        counter = _msg_seq_counters.get(msg_id)
        if counter is None:
            counter = _msg_seq_counters[msg_id] = itertools.count(1)
        _msg_seq_ts[msg_id] = now
        return next(counter)


# 被动回复 msg_id 过期（QQ 40034005，有效期约5-7分钟）时的主动推送兜底钩子，
# 由 create_command_client 注入（需要闭包内的 qq_bot）。长时间分析（>5分钟）的回答
# 靠它才能送达——否则答案算出来也发不出去（2026-09-15 事故：qid=4a6f02 答案被丢弃）
_proactive_send_hook = None


def _is_msg_id_expired(err) -> bool:
    """判定 QQ 被动回复消息过期错误。
    已知两种错误码：40034005（msg_id已过期）/ 40034031（msgid已经过期,不能回复），
    群聊与私聊均会出现，有效期约 5-7 分钟"""
    s = str(err)
    return ("40034005" in s or "40034031" in s
            or ("过期" in s and ("msg_id" in s or "msgid" in s)))


async def reply_text(message, content: str):
    """纯文本回复（自动分配msg_seq；msg_id过期自动降级主动推送）"""
    try:
        await message.reply(content=content, msg_seq=_next_msg_seq(message.id))
    except Exception as e:
        if _is_msg_id_expired(e) and _proactive_send_hook:
            logger.warning(f"被动回复msg_id已过期，降级主动推送: {content[:40]}")
            await _proactive_send_hook(message, content, None)
        else:
            raise


async def reply_md(message, text: str, md: str):
    """优先Markdown卡片回复，失败降级纯文本；msg_id过期降级主动推送"""
    try:
        await message.reply(msg_type=2, markdown={"content": md}, msg_seq=_next_msg_seq(message.id))
    except Exception as e:
        if _is_msg_id_expired(e) and _proactive_send_hook:
            logger.warning(f"被动回复msg_id已过期，降级主动推送(md): {text[:40]}")
            await _proactive_send_hook(message, text, md)
            return
        logger.warning(f"Markdown回复失败，降级纯文本: {e}")
        await reply_text(message, text)


def _split_lines(s: str, limit: int) -> list:
    """按行边界把长文本切成不超过 limit 字符的块（不截断丢弃内容）。
    优先在 Markdown 标题/空行边界切分，避免表格与列表被拦腰截断导致卡片解析破碎"""
    s = (s or "").strip()
    if not s:
        return []
    if len(s) <= limit:
        return [s]

    # 先按"软分段"（空行或标题行开头）组织成块，再按长度合并
    soft_blocks: list = []
    cur = ""
    for line in s.split("\n"):
        is_boundary = (not line.strip()) or line.lstrip().startswith("#")
        if is_boundary and cur.strip():
            soft_blocks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        soft_blocks.append(cur)

    chunks, cur = [], ""
    for block in soft_blocks:
        # 单块超长则按行硬切
        while len(block) > limit:
            if cur:
                chunks.append(cur.rstrip())
                cur = ""
            cut = block.rfind("\n", 0, limit)
            if cut <= 0:
                cut = limit
            chunks.append(block[:cut].rstrip())
            block = block[cut:].lstrip("\n")
        if cur and len(cur) + len(block) > limit:
            chunks.append(cur.rstrip())
            cur = ""
        cur += block
    if cur.strip():
        chunks.append(cur.rstrip())
    return chunks


async def reply_long_md(message, answer: str, title: str = "🏎️ F1专家解答"):
    """长回答按行分块、多条消息连续发送（保留全部内容，不截断）"""
    chunks = _split_lines(answer, LLM_REPLY_CHUNK)
    total = len(chunks)
    for i, part in enumerate(chunks, 1):
        header = title + (f"（{i}/{total}）" if total > 1 else "")
        md = f"## {header}\r\r{part}"
        await reply_md(message, part, md)


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
    "upgrades", "upgrade", "pu", "car", "clear", "live", "telemetry", "predict",
    "kb",
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

    global _proactive_send_hook
    _proactive_send_hook = _proactive_send

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
                logger.info(f"LLM问答完成 (用户 {member_openid[:8]}...) "
                            f"耗时 {_time.time() - t_start:.0f}s，回答 {len(answer)} 字")
                answer += _build_source_footer(ask_meta)
                await reply_long_md(message, answer)
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
            else:
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

            logger.info(f"收到指令: {cmd} {arg} (from {member_openid})")

            try:
                if cmd == "/help":
                    try:
                        await message.reply(
                            msg_type=2,
                            markdown={"content": HELP_MARKDOWN + "\r\r点击下方按钮可直接执行👇"},
                            keyboard={"content": HELP_KEYBOARD},
                            msg_seq=_next_msg_seq(message.id),
                        )
                    except Exception as e:
                        logger.warning(f"键盘卡片回复失败，降级纯文本: {e}")
                        await reply_md(message, HELP_TEXT, HELP_MARKDOWN)

                elif cmd == "/drivers":
                    standings = f1_api.get_current_standings()
                    entries = QQGroupBot._parse_driver_standings(standings.get("drivers") or {})
                    if entries:
                        await reply_md(
                            message,
                            QQGroupBot.format_driver_standings(QQGroupBot, entries),
                            QQGroupBot.format_driver_standings_md(QQGroupBot, entries),
                        )
                    else:
                        await reply_text(message, "暂无车手积分榜数据")

                elif cmd == "/teams":
                    standings = f1_api.get_current_standings()
                    entries = QQGroupBot._parse_constructor_standings(standings.get("constructors") or {})
                    if entries:
                        await reply_md(
                            message,
                            QQGroupBot.format_constructor_standings(QQGroupBot, entries),
                            QQGroupBot.format_constructor_standings_md(QQGroupBot, entries),
                        )
                    else:
                        await reply_text(message, "暂无车队积分榜数据")

                elif cmd == "/gp":
                    if not arg:
                        await reply_text(message, "用法: /gp 地点 [年份] [环节]\n如: /gp 蒙扎\n/gp 斯帕 2022\n/gp 比利时 2021 sprint\n环节可选: fp1/fp2/fp3/sq/sprint/qualy/race")
                        return

                    tokens = arg.split()
                    year = None
                    session_type = None
                    kw_parts = []
                    for t in tokens:
                        tl = t.lower()
                        if t.isdigit() and len(t) == 4:
                            year = int(t)
                        elif tl in SESSION_ARG_ALIASES:
                            session_type = SESSION_ARG_ALIASES[tl]
                        else:
                            kw_parts.append(t)
                    keyword = "".join(kw_parts)

                    if not keyword:
                        await reply_text(message, "用法: /gp 地点 [年份] [环节]\n如: /gp 蒙扎 race")
                        return

                    import asyncio as _aio
                    from functools import partial as _partial
                    from common.f1_api import find_race_by_circuit
                    loop = _aio.get_running_loop()
                    target_year = year or SEASON

                    schedule = await loop.run_in_executor(None, _partial(f1_api.get_schedule_for_year, target_year))
                    if not schedule:
                        await reply_text(message, f"❌ 无法获取 {target_year} 赛季赛程")
                        return

                    race = find_race_by_circuit(schedule, keyword)
                    if not race:
                        await reply_text(message, f"❌ {target_year} 赛季未找到与「{keyword}」匹配的分站\n可试试赛道/城市/国家名，如: 蒙扎、斯帕、银石、铃鹿")
                        return

                    round_num = int(race["round"])

                    if session_type:
                        await reply_text(message, f"🔍 正在获取 {race['raceName']} {F1API.SESSION_NAMES_CN.get(session_type, session_type)} 成绩…")
                        result = await loop.run_in_executor(
                            None, _partial(f1_api.get_session_results, round_num, session_type, season=target_year))
                        if result and result.get("entries"):
                            text, md = QQGroupBot.format_session_result(result)
                            await reply_md(message, text, md)
                        else:
                            await reply_text(message, f"暂无 {race['raceName']} {F1API.SESSION_NAMES_CN.get(session_type, session_type)} 成绩数据")
                        return

                    # 未指定环节：汇总该站已结束环节的领奖台
                    await reply_text(message, f"🔍 正在获取 {race['raceName']} 各环节成绩，可能需要几秒…")

                    def _fetch_gp_summary():
                        from concurrent.futures import ThreadPoolExecutor
                        from datetime import timezone as _tz2
                        sessions = f1_api.get_all_sessions(race)
                        now_utc = datetime.now(_tz2.utc)
                        completed = [s for s in sessions if s["datetime"] < now_utc]
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
                    await reply_md(message, text, md)

                elif cmd == "/watch":
                    await reply_md(message, WATCH_LINKS_TEXT, WATCH_LINKS_MD)

                elif cmd == "/live":
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
                    await reply_md(message, text, md)

                elif cmd == "/weather":
                    # 分站天气/赛道温度/风速风向：/weather [地点/赛道/R几]，缺省下一站
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
                                return None
                        else:
                            race = f1_api.get_next_race()
                        if not race:
                            return None
                        sessions = f1_api.get_all_sessions(race)
                        return WeatherAPI().get_race_weather(race, sessions)

                    result = await loop.run_in_executor(None, _fetch_weather)
                    if not result:
                        head = (f"未找到「{arg}」对应的分站" if arg else "本赛季已无剩余比赛")
                        await reply_text(message,
                            head + "\n用法: /weather [地点/赛道/R几]，如 /weather 蒙扎、/weather R14")
                        return
                    if result.get("error"):
                        await reply_text(message, f"⚠️ 天气数据获取失败：{result['error']}")
                        return
                    text, md = QQGroupBot.format_race_weather(result)
                    await reply_md(message, text, md)

                elif cmd == "/upgrades":
                    if not f1cosmos:
                        await reply_text(message, "F1Cosmos数据源未启用")
                        return
                    import asyncio as _aio
                    loop = _aio.get_running_loop()

                    # 支持组合查询：年份 + 轮次(R5/第5站) + 车队 + 地点
                    # 如 /upgrades 蒙扎 2024、/upgrades R5、/upgrades 法拉利 蒙扎 2024
                    info = parse_upgrade_query(arg)
                    season = info["year"] or SEASON

                    # 历史赛季车队整年查询需逐站解析FIA官方PDF，首次较慢，先回提示
                    if info["team"] and not info["gp"] and not info["round"] and season != SEASON:
                        await reply_text(message, f"⏳ 正在抓取{season}赛季FIA官方升级申报文档（首次查询需解析全年PDF，约需半分钟）...")

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
                        if info["year"]:
                            return ("", "")  # 只给了年份没有车队/分站，无法查询
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
                        await reply_text(message, f"未找到「{arg}」对应的分站或车队\n用法: /upgrades [车队] [地点/R几] [年份]，如 /upgrades 蒙扎、/upgrades R16 2024、/upgrades 法拉利 蒙扎 2024")
                    else:
                        await reply_md(message, text, md)

                elif cmd == "/upgrade":
                    if not arg:
                        await reply_text(message, "用法: /upgrade 车队/部件 [地点/R几] [年份]\n如: /upgrade 法拉利、/upgrade 红牛 蒙扎 2024、/upgrade 前翼 2023")
                        return
                    if not f1cosmos:
                        await reply_text(message, "F1Cosmos数据源未启用")
                        return
                    import asyncio as _aio
                    loop = _aio.get_running_loop()

                    info = parse_upgrade_query(arg)
                    season = info["year"] or SEASON

                    if info["team"] and season != SEASON and not info["gp"] and not info["round"]:
                        await reply_text(message, f"⏳ 正在抓取{season}赛季FIA官方升级申报文档（首次查询需解析全年PDF，约需半分钟）...")

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
                        await reply_text(message, f"未找到「{arg}」相关的升级件数据")
                    else:
                        await reply_md(message, text, md)

                elif cmd == "/pu":
                    if not f1cosmos:
                        await reply_text(message, "F1Cosmos数据源未启用")
                        return
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
                        await reply_text(message, f"未找到「{arg}」对应的车手或车队\n用法: /pu [车手/车队] [年份]，如 /pu 法拉利、/pu 维斯塔潘 2023")
                    else:
                        await reply_md(message, text, md)

                elif cmd == "/car":
                    if not f1cosmos:
                        await reply_text(message, "F1Cosmos数据源未启用")
                        return
                    import asyncio as _aio
                    from functools import partial as _partial
                    loop = _aio.get_running_loop()
                    text, md = await loop.run_in_executor(None, _partial(f1cosmos.format_destructors, arg, SEASON))
                    await reply_md(message, text, md)

                elif cmd == "/predict":
                    # 统计模型预测（直接调模型，不走LLM；排位+正赛名次+概率）
                    import asyncio as _aio
                    loop = _aio.get_running_loop()

                    def _fetch_prediction():
                        from common.prediction_model import RacePredictionModel
                        pm = RacePredictionModel(f1_api, f1cosmos)
                        return pm.predict(arg or "", season=SEASON)

                    await reply_text(message, "📊 正在运行统计预测模型（名次制回归+蒙特卡洛）…")
                    r = await loop.run_in_executor(None, _fetch_prediction)
                    if not r or "error" in r:
                        await reply_text(message, f"预测失败：{(r or {}).get('error', '未知错误')}")
                        return
                    # 渲染卡片：正赛前10 + 排位前6 + 回测基准
                    meta = r.get("model_meta", {})
                    bt = meta.get("backtest") or {}
                    text = f"🔮 【{r['gp']} 预测】（统计模型 v{meta.get('version')}）\n\n"
                    md = f"## 🔮 {r['gp']} 预测\r\r> 统计模型：名次制回归 + 残差蒙特卡洛\r> 回测基准（2025留一法）：领奖台命中 {bt.get('podium_hits_per_race')}/场 · 名次MAE {bt.get('mean_abs_pos_error')}\r\r"
                    md += "**正赛预测**\r"
                    text += "正赛预测：\n"
                    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
                    for p in r["predicted_race"][:10]:
                        pen = f"（发车P{p['grid']}，罚退+{p['penalty']}）" if p.get("penalty") else (f"（发车P{p['grid']}）" if p["grid"] != p["pos"] else "")
                        line = f"{medals.get(p['pos'], 'P' + str(p['pos']))} {p['driver']}（{p['team']}）{pen}"
                        text += line + "\n"
                        md += line + "\r"
                    q_header = ("**排位（已结束，真实成绩）**" if r.get("qualifying_actual")
                                else "**排位预测（含Top3概率）**")
                    md += f"\r{q_header}\r"
                    text += "\n排位预测（Top3概率）：\n"
                    for p in r["predicted_qualifying"][:6]:
                        if r.get("qualifying_actual"):
                            line = f"P{p['pos']} {p['driver']}（{p['team']}）"
                        else:
                            line = f"P{p['pos']} {p['driver']}（{p['team']}）{p['quali_top3_prob']*100:.0f}%"
                        text += line + "\n"
                        md += line + "\r"
                    note = ("⚠️ 统计学口径：名次 = 名次制线性回归的数学期望；"
                            "Top3/前十概率 = 1000次蒙特卡洛残差采样频率；"
                            "回测基准为2025赛季留一法。安全车/事故/机械故障不可预测")
                    # 高 SC 赛道警示（sc_warning 由模型结果生成：概率放宽A1 + 顺位阻尼A2 口径说明）
                    if r.get("sc_warning"):
                        note += "\n" + r["sc_warning"]
                    text += "\n" + note
                    md += f"\r\r{note}"
                    await reply_md(message, text, md)

                elif cmd == "/telemetry":
                    # 遥测/圈速/赛果分析 -> 自有遥测页 + F1Cosmos 仪表盘（含透明度可调嵌入页）
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
                    await reply_md(message, text, md)

                elif cmd == "/rate":
                    if not RATING_ENABLED or not ratings_store:
                        await reply_text(message, "车手评分功能未启用")
                        return
                    races = list(ratings_store._data.get("races", {}).items())  # 快照防迭代期并发修改
                    open_races = [(k, r) for k, r in races if not r.get("closed")]
                    if not open_races:
                        await reply_text(message, "当前没有进行中的车手评分\n比赛周会自动创建本场投票，正赛开始1小时后开放打分!")
                        return
                    open_races.sort(key=lambda x: (x[1]["season"], x[1]["round"]))
                    race_key, race = open_races[-1]
                    link_base = f"{RATING_BASE_URL}/r/{race_key}"
                    # 比赛周预创建条目：未到开启时刻（正赛开始+1h）时提示开放时间，链接可先收藏
                    pending_tip = ""
                    try:
                        from datetime import timezone
                        from zoneinfo import ZoneInfo
                        _oa = ratings_store.race_open_at(race)
                        if _oa and datetime.now(timezone.utc) < _oa:
                            _bj = _oa.astimezone(ZoneInfo("Asia/Shanghai")).strftime('%m-%d %H:%M')
                            pending_tip = f"\n⏰ 投票将于 {_bj}（北京时间）开启（正赛开始1小时后），可先收藏链接"
                    except Exception:
                        pass

                    if is_dm:
                        # 私聊：直接发专属token链接（私密可见）
                        token = ratings_store.get_or_create_token(race_key, member_openid)
                        if not token:
                            await reply_text(message, "评分链接生成失败，请稍后重试")
                            return
                        link = f"{link_base}/{token}"
                        await reply_md(message,
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
                        await reply_text(message, "评分链接生成失败，请稍后重试")
                        return
                    await reply_md(message,
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

                elif cmd == "/ratings":
                    if not ratings_store:
                        await reply_text(message, "车手评分功能未启用")
                        return
                    race_key = None
                    if arg:
                        # 按地点找场次
                        from common.f1_api import find_race_by_circuit
                        import asyncio as _a2
                        from functools import partial as _p2
                        loop2 = _a2.get_running_loop()
                        schedule = await loop2.run_in_executor(None, _p2(f1_api.get_schedule_for_year, SEASON))
                        race_hit = find_race_by_circuit(schedule, arg) if schedule else None
                        if race_hit:
                            race_key = ratings_store.race_key(SEASON, race_hit["round"])
                        if not race_key or not ratings_store.get_race(race_key):
                            await reply_text(message, f"未找到「{arg}」的评分记录")
                            return
                    else:
                        races = dict(ratings_store._data.get("races", {}))  # 快照防迭代期并发修改
                        if not races:
                            await reply_text(message, "暂无评分记录")
                            return
                        race_key = sorted(races.keys(), key=lambda k: (races[k]["season"], races[k]["round"]))[-1]

                    race = ratings_store.get_race(race_key)
                    board = ratings_store.aggregate(race_key)
                    driver_names = _get_driver_names_for_race(f1_api, race)
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
                    board_url = f"{RATING_BASE_URL}/board/{race_key}"
                    md += f"\r\r[📈 网页版榜单]({board_url})"
                    text += f"\n网页版: {board_url}"
                    await reply_md(message, text, md)

                elif cmd == "/dotd":
                    if not ratings_store:
                        await reply_text(message, "车手评分功能未启用")
                        return
                    board = ratings_store.season_dotd_board(SEASON)
                    if not board:
                        await reply_text(message, f"{SEASON}赛季暂无最佳车手记録\n每场正赛评分截止后产生一位DOTD")
                        return
                    # 用积分榜名单做显示名
                    name_map = {}
                    try:
                        standings = f1_api.get_current_standings()
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
                    await reply_md(message, text, md)

                elif cmd == "/calendar":
                    schedule = f1_api.get_schedule()
                    if schedule:
                        text, md = QQGroupBot.format_calendar(schedule)
                        await reply_md(message, text, md)
                    else:
                        await reply_text(message, "暂无赛历数据")

                elif cmd == "/next":
                    race = None
                    if arg:
                        # 指定分站查询：地点/赛道/大奖赛名/R几（如 /next 蒙扎、/next R16、/next 马德里）
                        info = parse_race_query(arg)
                        race = find_race_for_query(f1_api, info["year"] or SEASON,
                                                   info["round"], info["gp"])
                        if not race:
                            await reply_text(message,
                                f"未找到「{arg}」对应的分站\n"
                                "用法: /next [地点/赛道/大奖赛名/R几]，如 /next 蒙扎、/next R16、/next 红牛环")
                            return
                    else:
                        race = f1_api.get_next_race()
                    if race:
                        sessions = f1_api.get_all_sessions(race)
                        text, md = QQGroupBot.format_next_race(race, sessions)
                        await reply_md(message, text, md)
                    else:
                        await reply_text(message, "本赛季已无剩余比赛")

                elif cmd == "/last":
                    type_aliases = SESSION_ARG_ALIASES
                    if arg:
                        session_type = type_aliases.get(arg.lower().replace(" ", ""))
                        if not session_type:
                            await reply_text(message, f"未知环节「{arg}」\n可选: fp1/fp2/fp3/qualy/sq/sprint/race")
                            return
                        session_info = None
                        session_year = None
                        schedule = f1_api.get_schedule()
                        if not schedule:
                            await reply_text(message, "⚠️ 赛程数据暂时不可用，请稍后重试")
                            return
                        from datetime import timezone as _tzu
                        now_utc = datetime.now(_tzu.utc)
                        for race in reversed(schedule):
                            for s in f1_api.get_all_sessions(race):
                                if s['type'] == session_type and s['datetime'] < now_utc:
                                    session_info = s
                                    break
                            if session_info:
                                break
                        # 赛季初（1-3月）当前赛季尚无已结束环节时，回退上一赛季赛程
                        if not session_info and now_utc.month <= 3:
                            prev_schedule = f1_api.get_schedule_for_year(now_utc.year - 1)
                            for race in reversed(prev_schedule or []):
                                for s in f1_api.get_all_sessions(race):
                                    if s['type'] == session_type and s['datetime'] < now_utc:
                                        session_info = s
                                        break
                                if session_info:
                                    session_year = now_utc.year - 1
                                    break
                    else:
                        session_info = f1_api.get_last_completed_session()
                        session_year = None

                    if not session_info:
                        await reply_text(message, "未找到已结束的比赛环节")
                        return

                    await reply_text(message, f"🔍 正在获取 {session_info['race_name']} {session_info['name']} 成绩...")
                    # 跨年回退时显式传赛季，避免默认当前赛季查不到历史成绩
                    result = f1_api.get_session_results(int(session_info['round']), session_info['type'],
                                                        season=session_year)
                    if result and result.get("entries"):
                        text, md = QQGroupBot.format_session_result(result)
                        await reply_md(message, text, md)
                    else:
                        await reply_text(message, f"暂无 {session_info['name']} 成绩数据")

                elif cmd == "/setdriver":
                    if not arg:
                        await reply_text(message, "用法: /setdriver 车手名\n如: /setdriver 维斯塔潘\n最多可同时关注3位车手")
                        return
                    standings = f1_api.get_current_standings()
                    entries = QQGroupBot._parse_driver_standings(standings.get("drivers") or {})
                    result = resolve_driver(arg, entries)
                    if result:
                        driver_id, driver_name = result
                        ok = prefs_store.set_user_driver(member_openid, driver_id, driver_name)
                        if not ok:
                            await reply_text(message,
                                f"⚠️ 最多同时关注 {UserPrefsStore.MAX_WATCH} 位车手\n"
                                f"可先 /unbind 车手 {driver_name} 移除一位再添加")
                            return
                        names = list(UserPrefsStore.pref_driver_names(
                            prefs_store.get_user_prefs(member_openid)).values())
                        await reply_text(message,
                            f"✅ 已添加关注车手: {driver_name}\n"
                            f"当前关注: {'、'.join(names)}\n"
                            "每站正赛结束后会为你推送Ta们的排名⭐")
                    else:
                        await reply_text(message, f"❌ 未找到车手「{arg}」，请检查名字（支持中英文）")

                elif cmd == "/setteam":
                    if not arg:
                        await reply_text(message, "用法: /setteam 车队名\n如: /setteam 法拉利\n最多可同时关注3支车队")
                        return
                    standings = f1_api.get_current_standings()
                    entries = QQGroupBot._parse_constructor_standings(standings.get("constructors") or {})
                    result = resolve_team(arg, entries)
                    if result:
                        team_id, team_name = result
                        ok = prefs_store.set_user_team(member_openid, team_id, team_name)
                        if not ok:
                            await reply_text(message,
                                f"⚠️ 最多同时关注 {UserPrefsStore.MAX_WATCH} 支车队\n"
                                f"可先 /unbind 车队 {team_name} 移除一支再添加")
                            return
                        names = list(UserPrefsStore.pref_team_names(
                            prefs_store.get_user_prefs(member_openid)).values())
                        await reply_text(message,
                            f"✅ 已添加关注车队: {team_name}\n"
                            f"当前关注: {'、'.join(names)}\n"
                            "每站正赛结束后会为你推送车队排名⭐")
                    else:
                        await reply_text(message, f"❌ 未找到车队「{arg}」，请检查名字（支持中英文）")

                elif cmd == "/me":
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
                        await reply_text(message, "\n".join(lines))
                    else:
                        await reply_text(message, "你还没有设置偏好\n使用 /setdriver 和 /setteam 进行设置")

                elif cmd == "/unbind":
                    # 解绑偏好：
                    # /unbind                    全部解绑
                    # /unbind 车手|车队          清空该类
                    # /unbind 车手 维斯塔潘      移除指定一位
                    a = (arg or "").strip()
                    al = a.lower()
                    if not al:
                        kinds = ["driver", "team"]
                        removed = prefs_store.unset_user_pref(member_openid, *kinds)
                        if removed:
                            await reply_text(message, "✅ 已解绑全部偏好，后续不再接收个性化推送")
                        else:
                            await reply_text(message, "你当前没有偏好绑定\n使用 /me 查看现有设置")
                        return
                    parts = a.split(None, 1)
                    kind_word, target = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
                    kind_map = {"车手": "driver", "driver": "driver",
                                "车队": "team", "team": "team"}
                    kind = kind_map.get(kind_word)
                    if not kind:
                        await reply_text(message, "用法: /unbind [车手|车队] [名字]\n不带参数则解绑全部偏好")
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
                            await reply_text(message, f"你的关注列表里没有「{target}」，/me 查看现有设置")
                            return
                        ok = (prefs_store.remove_user_driver(member_openid, hit) if kind == "driver"
                              else prefs_store.remove_user_team(member_openid, hit))
                        if ok:
                            await reply_text(message, f"✅ 已移除关注: {names[hit]}")
                        else:
                            await reply_text(message, "解绑失败，请稍后重试")
                        return
                    removed = prefs_store.unset_user_pref(member_openid, kind)
                    if removed:
                        label = {"driver": "车手", "team": "车队"}
                        await reply_text(message, f"✅ 已解绑全部{label[kind]}偏好")
                    else:
                        await reply_text(message, "你当前没有对应的偏好绑定\n使用 /me 查看现有设置")

                elif cmd == "/news":
                    # 定向新闻推送开关（需先 /setdriver 或 /setteam 设置关注对象）
                    a = (arg or "").strip().lower()
                    if a in ("on", "开", "开启"):
                        prefs_store.set_news_dm(member_openid, True)
                        await reply_text(message, "✅ 已开启定向新闻推送\n与你关注车手/主队相关的新闻将私聊推送给你（每日 8:00/19:00）")
                    elif a in ("off", "关", "关闭"):
                        prefs_store.set_news_dm(member_openid, False)
                        await reply_text(message, "✅ 已关闭定向新闻推送（群内的每日新闻速递不受影响）")
                    else:
                        cur = prefs_store.news_dm_enabled(member_openid)
                        pref = prefs_store.get_user_prefs(member_openid)
                        has_watch = bool(pref.get("driver") or pref.get("team"))
                        state = "开启" if cur else "关闭"
                        hint = "" if has_watch else "\n⚠️ 你还未设置关注对象，先用 /setdriver 或 /setteam 设置"
                        await reply_text(message,
                            f"📰 定向新闻推送当前状态：{state}{hint}\n"
                            f"用法: /news on 开启 · /news off 关闭")

                elif cmd == "/clear":
                    if llm and llm.clear_context(member_openid):
                        await reply_text(message, "🧹 已清空你的AI对话上下文，换个话题重新聊吧")
                    else:
                        await reply_text(message, "当前没有进行中的对话上下文")

                elif cmd == "/kb":
                    # 知识库审核（管理员专用）：/kb 看待审 · /kb ok <id> 入库 · /kb no <id> 拒绝
                    if not ADMIN_OPENIDS:
                        await reply_text(message, "💡 知识库审核未启用（未配置 ADMIN_OPENIDS）")
                    elif member_openid not in ADMIN_OPENIDS:
                        await reply_text(message, "⛔ 该指令仅限管理员使用")
                    else:
                        from common.knowledge_store import KnowledgeStore
                        ks = KnowledgeStore.get()
                        a = (arg or "").strip().lower()
                        if a.startswith(("ok ", "yes ", "no ")):
                            try:
                                cid = int(a.split()[1])
                            except (IndexError, ValueError):
                                await reply_text(message, "用法: /kb ok <编号> 或 /kb no <编号>")
                                return
                            approve = not a.startswith("no ")
                            c = ks.review(cid, approve, reviewer=member_openid[:8])
                            if c:
                                verdict = "✅ 已入知识库" if approve else "🗑️ 已拒绝"
                                await reply_text(message, f"{verdict}：候选 #{cid}\n{c['claim'][:100]}")
                            else:
                                await reply_text(message, f"⚠️ 候选 #{cid} 不存在或不处于待审核状态")
                        else:
                            pending = ks.pending_candidates()
                            if not pending:
                                await reply_text(message, "📚 暂无待审核的知识候选")
                            else:
                                lines = [f"📚 待审核知识候选（{len(pending)} 条）："]
                                for c in pending[-10:]:
                                    lines.append(f"\n#{c['id']}（{c.get('source','')}·{c.get('ts','')}）\n"
                                                 f"{c['claim'][:150]}\n校对依据: {(c.get('evidence') or '无')[:100]}")
                                lines.append("\n回复 /kb ok <编号> 入库 · /kb no <编号> 拒绝")
                                await reply_text(message, "\n".join(lines))

                else:
                    await reply_text(message, f"未知命令: {cmd}\n发送 /help 查看命令列表")

            except Exception as e:
                logger.error(f"处理指令失败: {e}")
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
