# -*- coding: utf-8 -*-
"""管理员告警钩子：common/ 层模块（无 bot 引用）发现数据源异常时上报。
main.py 在 qq_bot 就绪后注册 hook（DM ADMIN_OPENIDS）；未注册时仅记日志。
按 key 去重（默认 12h 冷却），防解析循环反复轰炸。"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

_hook = None
_lock = threading.Lock()
_last_sent = {}


def set_admin_alert_hook(fn):
    """注册告警回调 fn(title: str, detail: str)，由装配层（main.py）注入"""
    global _hook
    _hook = fn


def alert_admin(title: str, detail: str, key: str = None, cooldown_hours: float = 12):
    """发管理员告警（始终记 ERROR 日志；hook 存在且冷却期外才 DM）。
    hook 在 daemon 线程异步发送（调用方多为抓取线程，防 DM 阻塞 worker）；
    发送失败只记 10min 短冷却重试，防告警静默丢失"""
    logger.error(f"[admin-alert] {title}: {detail}")
    if not _hook:
        return
    k = key or title
    now = time.time()
    with _lock:
        last = _last_sent.get(k, 0)
        ok_cooldown = cooldown_hours * 3600 if _last_sent.get((k, "ok")) else 600
        if now - last < ok_cooldown:
            return
        _last_sent[k] = now  # 立即占位防并发重入，失败时下面会改写短冷却
    def _send():
        try:
            _hook(title, detail)
            with _lock:
                _last_sent[(k, "ok")] = time.time()
        except Exception as e:
            logger.warning(f"[admin-alert] 告警发送失败（10min 后允许重试）: {e}")
            with _lock:
                _last_sent.pop((k, "ok"), None)
    threading.Thread(target=_send, daemon=True, name="admin-alert").start()
