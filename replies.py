# -*- coding: utf-8 -*-
"""回复助手（从 main.py 原样抽出，行为零变化）：
被动回复 msg_seq 管理、msg_id 过期降级主动推送、长文本按行分块、图片发送后端钩子。

钩子（_proactive_send_hook / _reply_image_backend）由 create_command_client 经
set_proactive_send_hook / set_reply_image_backend 注入（需要其闭包内的 qq_bot）。
"""
import logging
import threading as _threading

logger = logging.getLogger(__name__)

_msg_seq_counters = {}
_msg_seq_lock = _threading.Lock()  # 急切初始化（惰性 check-then-set 在首批并发回复下有双建锁窗口）
_msg_seq_ts = {}      # msg_id -> 最后使用时间（TTL 清理用）
_MSG_SEQ_TTL = 3600   # 回复链1小时内有效，超时淘汰防内存无限增长


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

# 图片发送后端钩子（同理必须由 create_command_client 注入闭包 qq_bot——
# 2026-10-09 事故：reply_image 在模块级直接引用 qq_bot 全局名，webhook 部署形态下
# 该全局从未赋值 → NameError → /pace /trend 等所有图片指令 100% 崩溃）
_reply_image_backend = None


def set_proactive_send_hook(hook):
    """注入 msg_id 过期时的主动推送兜底钩子（create_command_client 调用）"""
    global _proactive_send_hook
    _proactive_send_hook = hook


def set_reply_image_backend(backend):
    """注入图片发送后端（create_command_client 调用）"""
    global _reply_image_backend
    _reply_image_backend = backend


def _is_msg_id_expired(err) -> bool:
    """判定 QQ 被动回复消息过期错误。
    已知两种错误码：40034005（msg_id已过期）/ 40034031（msgid已经过期,不能回复），
    群聊与私聊均会出现，有效期约 5-7 分钟"""
    s = str(err)
    return ("40034005" in s or "40034031" in s
            or ("过期" in s and ("msg_id" in s or "msgid" in s)))


async def reply_text(message, content: str):
    """纯文本回复（自动分配msg_seq；msg_id过期自动降级主动推送；出站过 secret_guard 脱敏）"""
    from common.secret_guard import scrub_text
    content = scrub_text(content)
    try:
        await message.reply(content=content, msg_seq=_next_msg_seq(message.id))
    except Exception as e:
        if _is_msg_id_expired(e) and _proactive_send_hook:
            logger.warning(f"被动回复msg_id已过期，降级主动推送: {content[:40]}")
            await _proactive_send_hook(message, content, None)
        else:
            raise


async def reply_md(message, text: str, md: str):
    """优先Markdown卡片回复，失败降级纯文本；msg_id过期降级主动推送；出站过 secret_guard 脱敏"""
    from common.secret_guard import scrub_text
    text, md = scrub_text(text), scrub_text(md)
    try:
        await message.reply(msg_type=2, markdown={"content": md}, msg_seq=_next_msg_seq(message.id))
    except Exception as e:
        if _is_msg_id_expired(e) and _proactive_send_hook:
            logger.warning(f"被动回复msg_id已过期，降级主动推送(md): {text[:40]}")
            await _proactive_send_hook(message, text, md)
            return
        logger.warning(f"Markdown回复失败，降级纯文本: {e}")
        await reply_text(message, text)


async def reply_image(message, image_url: str, content: str = "📊 统计图"):
    """图片回复（群聊/私聊自动路由，被动回复绑定 msg_id 省主动额度）。

    2026-10-09 事故修复：指令里直调 qq_bot.send_image_url 是群广播，
    私聊发指令图片会串到群聊。本函数按消息来源路由：
    私聊→/v2/users/{openid}，群聊→/v2/groups/{group_openid}，均带 msg_id 被动回复。"""
    is_dm = not hasattr(message, 'group_openid')
    if is_dm:
        openid = getattr(message, 'openid', None) or getattr(
            getattr(message, "author", None), "id", None) or getattr(
            getattr(message, "author", None), "member_openid", None) or getattr(
            getattr(message, "author", None), "user_openid", None)
    else:
        openid = getattr(message, 'group_openid', None)
    if not openid:
        logger.warning("reply_image 无法确定目标 openid")
        return
    ok = False
    if _reply_image_backend:
        ok = _reply_image_backend(openid, image_url, content, is_dm,
                                  message.id, _next_msg_seq(message.id))
        if ok is False:
            # msg_id 过期（被动回复约5分钟有效，图表生成可能超时）→ 主动发送兜底
            logger.warning(f"图片被动回复失败（msg_id过期?），主动发送兜底: {image_url[-40:]}")
            _reply_image_backend(openid, image_url, content, is_dm, None, None)
    else:
        logger.warning("reply_image 后端未注入（create_command_client 未初始化）")


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
    import sys as _sys
    # 分块上限取主模块配置（脚本直跑时为 __main__），缺省 1500
    _m = (_sys.modules.get("qq_group_official_version.main")
          or _sys.modules.get("main") or _sys.modules.get("__main__"))
    chunk = getattr(_m, "LLM_REPLY_CHUNK", 1500)
    chunks = _split_lines(answer, chunk)
    total = len(chunks)
    for i, part in enumerate(chunks, 1):
        header = title + (f"（{i}/{total}）" if total > 1 else "")
        md = f"## {header}\r\r{part}"
        await reply_md(message, part, md)
