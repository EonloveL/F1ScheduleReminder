"""全链路 trace ID（qid）工具：thread-local 贯穿 L0/L1/L2 全部日志与工具审计"""

import logging

logger = logging.getLogger(__name__)

# ==================== 全链路 trace ID ====================
# 一次问答生成一个 qid，经 thread-local 贯穿 L0/L1/L2 全部日志与工具审计。
# QIDLogFilter 会给该线程内所有日志记录（含 f1_api 等下游模块）附加 qid 字段，
# 排障时 grep "qid=xxxxxx" 即可捞出完整链路，无需肉眼翻找。
import secrets as _secrets
import threading as _threading

_QID_LOCAL = _threading.local()


def new_qid() -> str:
    """生成短随机问答链路 ID（6位hex）"""
    return _secrets.token_hex(3)


def set_qid(qid):
    _QID_LOCAL.qid = qid


def get_qid():
    return getattr(_QID_LOCAL, "qid", None)


class QIDLogFilter(logging.Filter):
    """给日志记录注入 qid 字段（无上下文时为 '-'），配合日志格式中的 %(qid)s 使用"""
    def filter(self, record):
        record.qid = get_qid() or "-"
        return True
