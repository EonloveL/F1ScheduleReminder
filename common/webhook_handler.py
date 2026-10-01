"""
QQ 机器人 Webhook 处理
- 回调URL验证（op=13）：签名 plain_token 并返回
- 事件接收（op=0）：Ed25519 验签后分发到 CommandClient
- 密钥派生：secret → repeat → seed(32B) → Ed25519 keypair
"""

import json
import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict
from flask import Flask, request, jsonify

logger = logging.getLogger(__name__)

# 事件分发线程池：有界worker替代每事件无界创建线程，防突发流量线程爆炸
# 队列无界（事件丢失会导致QQ功能无响应，比积压更严重），仅在积压过高时告警
EVENT_WORKERS = int(os.getenv("WEBHOOK_EVENT_WORKERS", "10"))
_event_executor = ThreadPoolExecutor(max_workers=EVENT_WORKERS, thread_name_prefix="qq-event")
_event_backlog = 0
_event_backlog_lock = threading.Lock()
_BACKLOG_WARN_THRESHOLD = 50


def _submit_event(fn, *args):
    """提交事件到线程池，跟踪积压量并在过高时告警"""
    global _event_backlog
    with _event_backlog_lock:
        _event_backlog += 1
        backlog = _event_backlog
    if backlog > _BACKLOG_WARN_THRESHOLD:
        logger.warning(f"⚠️ 事件分发积压过高: {backlog} 个待处理（worker={EVENT_WORKERS}）")

    def _run():
        global _event_backlog
        try:
            fn(*args)
        except Exception as e:
            logger.error(f"事件分发执行异常: {e}")
        finally:
            with _event_backlog_lock:
                _event_backlog -= 1

    _event_executor.submit(_run)

try:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False


class WebhookCrypto:
    """QQ Webhook Ed25519 签名/验签"""

    def __init__(self, bot_secret: str):
        """
        从 Bot Secret 派生 Ed25519 密钥对
        QQ 算法：seed = 反复重复 secret 直到长度 ≥ 32，取前 32 字节作为 Ed25519 种子
        """
        if not bot_secret or not _HAS_CRYPTO:
            self._valid = False
            return

        seed = bot_secret
        while len(seed) < 32:
            seed = seed * 2
        seed = seed[:32]
        seed_bytes = seed.encode()
        logger.info(f"Ed25519 密钥派生: secret_len={len(bot_secret)} seed_len={len(seed)} seed_bytes_len={len(seed_bytes)}")

        self._private_key = Ed25519PrivateKey.from_private_bytes(seed_bytes)
        self._public_key = self._private_key.public_key()
        self._valid = True
        logger.info(f"Ed25519 密钥对已生成 (seed 长度: 32)")

    @property
    def valid(self) -> bool:
        return self._valid

    def sign_validation(self, event_ts: str, plain_token: str) -> str:
        """对 op=13 回调验证请求签名：sign(event_ts + plain_token) → hex"""
        msg = (event_ts + plain_token).encode()
        sig = self._private_key.sign(msg)
        return sig.hex()

    def verify_event(self, timestamp: str, raw_body: bytes, signature_hex: str) -> bool:
        """验证事件签名：verify(timestamp + body)"""
        try:
            msg = timestamp.encode() + raw_body
            sig_bytes = bytes.fromhex(signature_hex)
            self._public_key.verify(sig_bytes, msg)
            return True
        except Exception:
            return False


# 全局单例
_webhook_secret = ""
_crypto = None
_command_client = None
_qq_bot = None
_appid = ""
_secret_str = ""
_dispatch_fn = None  # register_webhook 注入的事件分发函数

# ==================== 图文合并防抖 ====================
# 手机端无法图片+文字同发，用户通常先发图再发问题。
# 纯图片事件先缓冲 IMAGE_MERGE_WINDOW 秒：窗口内同一用户的文字事件到达则
# 把图片attachments注入文字事件合并处理（一次回答）；超时则按原图事件单独识别
IMAGE_MERGE_WINDOW = int(os.getenv("IMAGE_MERGE_WINDOW", "10"))
_image_buffer: Dict[str, Dict[str, Any]] = {}
_image_buffer_lock = threading.Lock()


def _has_image_atts(event_data: dict) -> bool:
    atts = event_data.get("attachments") or []
    return any(str(a.get("content_type", "")).startswith("image/") for a in atts if isinstance(a, dict))


def _buffer_image_event(event_type: str, event_data: dict, event_id: str, author_id: str):
    """缓冲纯图片事件；同用户已有缓冲时合并attachments（上限4张，计时不重置）"""
    with _image_buffer_lock:
        existing = _image_buffer.get(author_id)
        if existing:
            merged = (existing["event_data"].get("attachments") or []) + (event_data.get("attachments") or [])
            existing["event_data"]["attachments"] = merged[:4]
            logger.info(f"图片事件合并: 用户 {author_id[:8]}... 共 {len(existing['event_data']['attachments'])} 张")
            return

        def _fire():
            with _image_buffer_lock:
                pending = _image_buffer.pop(author_id, None)
            if pending and _dispatch_fn:
                logger.info(f"图片合并窗口({IMAGE_MERGE_WINDOW}s)超时，单独识别: 用户 {author_id[:8]}...")
                _submit_event(_dispatch_fn, pending["event_type"],
                              pending["event_data"], pending["event_id"], author_id)

        timer = threading.Timer(IMAGE_MERGE_WINDOW, _fire)
        timer.daemon = True
        _image_buffer[author_id] = {
            "event_type": event_type, "event_data": event_data,
            "event_id": event_id, "timer": timer,
        }
        timer.start()
        logger.info(f"图片事件已缓冲，等待 {IMAGE_MERGE_WINDOW}s 内同用户文字: {author_id[:8]}...")


def _merge_or_release_image(author_id: str, text_event_data: dict, is_command: bool) -> bool:
    """
    文字/指令事件到达时处理图片缓冲：
    - 普通文字：取消计时，图片attachments注入文字事件（合并），返回True表示发生了合并
    - 指令：取消计时，立即分发图片事件单独识别，返回False
    """
    with _image_buffer_lock:
        pending = _image_buffer.pop(author_id, None)
    if not pending:
        return False
    pending["timer"].cancel()
    if is_command:
        logger.info(f"用户发送指令，图片事件立即单独分发: {author_id[:8]}...")
        if _dispatch_fn:
            _submit_event(_dispatch_fn, pending["event_type"],
                          pending["event_data"], pending["event_id"], author_id)
        return False
    imgs = pending["event_data"].get("attachments") or []
    text_event_data["attachments"] = (imgs + (text_event_data.get("attachments") or []))[:4]
    logger.info(f"图文合并: {len(imgs)} 张图注入文字事件 (用户 {author_id[:8]}...)")
    return True


def register_webhook(app: Flask, webhook_secret: str, command_client, qq_bot=None,
                      appid: str = "", secret: str = ""):
    """注册 webhook 回调端点到 Flask app"""
    global _webhook_secret, _crypto, _command_client, _qq_bot, _appid, _secret_str, _dispatch_fn
    _webhook_secret = webhook_secret
    _command_client = command_client
    _qq_bot = qq_bot
    _appid = appid
    _secret_str = secret
    if webhook_secret:
        _crypto = WebhookCrypto(webhook_secret)
        if not _crypto.valid:
            logger.error("Webhook 签名模块初始化失败（cryptography 库未安装？），回调将拒绝所有事件")
    else:
        logger.error("未配置 BOT_WEBHOOK_SECRET，回调将拒绝所有事件（fail-closed，防伪造事件注入）")

    # 事件去重（防QQ超时重试导致重复处理），5分钟窗口；Flask threaded 模式下需加锁
    import time as _time
    _processed_events = {}
    _processed_lock = threading.Lock()
    _EVENT_TTL = 300
    # 签名时间戳容差：防重放（超出窗口的事件即使签名合法也拒绝）
    _SIG_TS_TOLERANCE = 300

    def _is_duplicate(event_id: str) -> bool:
        if not event_id:
            return False
        now = _time.time()
        with _processed_lock:
            for k in [k for k, v in _processed_events.items() if now - v > _EVENT_TTL]:
                del _processed_events[k]
            if event_id in _processed_events:
                return True
            _processed_events[event_id] = now
        return False

    def _dispatch_event(event_type, event_data, event_id, author_id):
        """后台线程处理事件（不阻塞webhook响应，避免QQ超时重试）"""
        import botpy
        import asyncio as _aio
        http = botpy.http.BotHttp(timeout=10, app_id=_appid, secret=_secret_str)
        api = botpy.api.BotAPI(http=http)
        loop = _aio.new_event_loop()
        try:
            if event_type == "GROUP_AT_MESSAGE_CREATE":
                from botpy.message import GroupMessage
                msg = GroupMessage(api=api, event_id=event_id, data=event_data)
                if author_id:
                    msg.author.member_openid = author_id
                loop.run_until_complete(_command_client.on_group_at_message_create(msg))

            elif event_type == "C2C_MESSAGE_CREATE":
                from botpy.message import C2CMessage
                msg = C2CMessage(api=api, event_id=event_id, data=event_data)
                if author_id:
                    msg.author.member_openid = author_id
                loop.run_until_complete(_command_client.on_group_at_message_create(msg))

            elif event_type == "GROUP_ADD_ROBOT" and _qq_bot:
                gid = event_data.get("group_openid", "")
                hello_md = ("## 🏎️ F1赛程助手已上线\r\r"
                            "自动推送F1比赛提醒/成绩/积分榜\r"
                            "比赛后可给车手打分（Driver of the Day）\r"
                            "任意F1问题直接@我提问（联网LLM回答）\r\r"
                            "发送 **/help** 查看完整功能列表")
                _qq_bot._tl.gid = gid
                try:
                    _qq_bot.send_markdown_message(hello_md, fallback_text="F1赛程助手已上线！发送 /help 查看功能")
                finally:
                    _qq_bot._tl.gid = None

            elif event_type == "FRIEND_ADD" and _qq_bot:
                uid = event_data.get("openid", "")
                hello = ("🏎️ F1赛程助手来啦！\r\r"
                         "在这里你可以：\r"
                         "- 查询积分榜/赛历/比赛成绩：发送 /drivers /teams /calendar /last\r"
                         "- 设置最喜爱车手/主队：发送 /setdriver 维斯塔潘\r"
                         "- 比赛后给车手打分：发送 /rate 获取评分链接\r"
                         "- 任意F1问题直接发给我（联网LLM回答）\r\r"
                         "发送 **/help** 查看完整指令列表\r"
                         "设置偏好后，每站正赛结束会私聊推送你关注的车手排名⭐")
                _qq_bot.send_dm_markdown(uid, hello, fallback_text="F1赛程助手来啦！发送 /help 查看功能")
        except Exception as e:
            logger.error(f"事件处理异常: {e}")
        finally:
            # 显式关闭 BotHttp 懒创建的 aiohttp ClientSession：
            # 不关闭则每个事件泄漏一个 session（botpy __del__ 在非主线程无事件循环，无法兜底）
            try:
                loop.run_until_complete(http.close())
            except Exception as ce:
                logger.debug(f"BotHttp session 关闭异常（忽略）: {ce}")
            loop.close()

    _dispatch_fn = _dispatch_event

    @app.route("/bot/callback", methods=["POST"])
    def bot_callback():
        raw_body = request.get_data()

        # --- op=13: 回调URL验证请求 ---
        try:
            data = json.loads(raw_body)
        except json.JSONDecodeError:
            return jsonify({"error": "invalid json"}), 400

        if data.get("op") == 13:
            d = data.get("d", {})
            plain_token = d.get("plain_token", "")
            event_ts = d.get("event_ts", "")
            if not plain_token:
                logger.warning("回调验证缺少 plain_token")
                return jsonify({"error": "bad request"}), 400
            if not (_crypto and _crypto.valid):
                logger.error("回调验证失败：crypto 未初始化（检查 BOT_WEBHOOK_SECRET 与 cryptography 库）")
                return jsonify({"error": "crypto unavailable"}), 503
            signature = _crypto.sign_validation(event_ts, plain_token)
            logger.info(f"回调验证成功: plain_token={plain_token[:8]}... signature={signature[:16]}...")
            return jsonify({"plain_token": plain_token, "signature": signature})

        # --- op=0 或其他：事件推送（fail-closed：验签不可用即拒绝，防伪造事件注入） ---
        if not (_crypto and _crypto.valid):
            logger.error("拒绝事件：webhook 验签不可用（未配置 BOT_WEBHOOK_SECRET 或 cryptography 缺失）")
            return jsonify({"error": "verification unavailable"}), 503

        sig = request.headers.get("X-Signature-Ed25519", "")
        ts = request.headers.get("X-Signature-Timestamp", "")

        # 重放防护：签名时间戳必须在容差窗口内
        try:
            if abs(_time.time() - int(ts)) > _SIG_TS_TOLERANCE:
                logger.warning(f"事件时间戳超出容差窗口，拒绝（疑似重放）: ts={ts}")
                return jsonify({"error": "stale timestamp"}), 403
        except (TypeError, ValueError):
            logger.warning(f"事件时间戳格式非法: {ts!r}")
            return jsonify({"error": "bad timestamp"}), 403

        if not _crypto.verify_event(ts, raw_body, sig):
            logger.warning(f"事件签名验证失败! sig.len={len(sig)} ts={ts}")
            return jsonify({"error": "signature failed"}), 403

        event_type = data.get("t", "")
        event_data = data.get("d", {})
        event_id = data.get("id", "")
        logger.info(f"Webhook 事件: t={event_type} op={data.get('op')}")

        # 统一用户标识：author.id 是跨群、跨私聊一致的用户 openid
        author_id = (event_data.get("author") or {}).get("id", "")

        # 实证日志：确认群@事件是否携带 message_reference（排查引用注入失效）
        if event_type in ("GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"):
            mr = event_data.get("message_reference")
            logger.info(f"引用字段检查: t={event_type} has_ref={bool(mr)} "
                        f"keys={sorted(event_data.keys())} ref={json.dumps(mr)[:120] if mr else None}")

        # 去重：同一事件只处理一次（QQ超时重试会重复推送）
        if _is_duplicate(event_id):
            logger.info(f"忽略重复事件: {event_id[:24]}...")
            return "", 200

        # 图文合并防抖：纯图片事件先缓冲，等待同用户文字；文字到达则注入合并
        if author_id and event_type in ("GROUP_AT_MESSAGE_CREATE", "C2C_MESSAGE_CREATE"):
            text = (event_data.get("content") or "").strip()
            if _has_image_atts(event_data) and not text:
                _buffer_image_event(event_type, event_data, event_id, author_id)
                return "", 200
            if text and not _has_image_atts(event_data):
                _merge_or_release_image(author_id, event_data, is_command=text.startswith("/"))

        # 异步后台处理，立即返回200（避免LLM等耗时操作阻塞导致QQ超时重试）
        # 有界线程池分发，防突发流量线程爆炸
        _submit_event(_dispatch_event, event_type, event_data, event_id, author_id)

        return "", 200
