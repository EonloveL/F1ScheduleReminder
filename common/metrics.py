# -*- coding: utf-8 -*-
"""轻量指标计数：进程内计数器 + 定期落盘 data/metrics.json（重启不清零）。
单进程多线程（webhook Flask threaded + APScheduler + 指令线程），写入走锁+原子写。
约束：单写者进程（仅 f1-qqbot bot 容器写此文件；web/live 容器不 import 本模块，
若未来引入第二写者需先补跨进程文件锁，参考 ratings.json.lock 模式）。"""
import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_counters = {}
_start_ts = time.time()
_dirty = 0
_last_flush = 0.0
FLUSH_INTERVAL = 60
FLUSH_EVERY_N = 20


def _path():
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data", "metrics.json")


def _load_file():
    try:
        with open(_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def incr(name: str, n: int = 1):
    """计数器 +n（内存累计，60s 或每 20 次落盘一次）"""
    global _dirty, _last_flush
    with _lock:
        _counters[name] = _counters.get(name, 0) + n
        _dirty += n
        now = time.time()
        if _dirty >= FLUSH_EVERY_N or now - _last_flush > FLUSH_INTERVAL:
            _flush_locked(now)


def _flush_locked(now=None):
    global _dirty, _last_flush
    pending = dict(_counters)  # 快照先行：写失败时内存计数保留待重试，不丢增量
    if not pending:
        _dirty = 0
        _last_flush = now or time.time()
        return
    try:
        base = _load_file().get("counters") or {}
        for k, v in pending.items():
            base[k] = base.get(k, 0) + v
        tmp = _path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"counters": base,
                       "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                      f, ensure_ascii=False)
        os.replace(tmp, _path())
        for k, v in pending.items():  # 只减快照量，期间新增的不动
            _counters[k] = _counters.get(k, 0) - v
            if not _counters[k]:
                del _counters[k]
        _dirty = 0
        _last_flush = now or time.time()
    except Exception as e:
        logger.warning(f"metrics 落盘失败（计数保留待重试）: {e}")


def snapshot():
    """返回 (累计计数器, 进程启动至今秒数)；合并文件基线 + 内存未落盘部分"""
    with _lock:
        base = _load_file().get("counters") or {}
        for k, v in _counters.items():
            base[k] = base.get(k, 0) + v
        return base, int(time.time() - _start_ts)


def render_text() -> str:
    counters, uptime = snapshot()
    lines = [f"uptime_seconds {uptime}"]
    for k in sorted(counters):
        lines.append(f"{k} {counters[k]}")
    return "\n".join(lines) + "\n"
