"""
F1赛程提醒机器人 - LLM工具执行审计

对应Agent架构中的 execution_record / chat_tool_failure 归档：
记录AI每次工具调用（入参/耗时/成败），用于"AI答错数据"类问题的事后追溯。

存储：append-only JSONL（data/llm_tool_audit.jsonl），超 5MB 轮转（保留5份），线程安全。
审计失败只记日志不抛出，绝不阻塞主流程。
"""

import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
AUDIT_FILE = os.path.join(DATA_DIR, "llm_tool_audit.jsonl")

MAX_BYTES = 5 * 1024 * 1024  # 单文件 5MB
BACKUP_COUNT = 5             # 轮转保留份数


class LLMToolAudit:
    """LLM工具调用审计记录器（单例，JSONL追加写）"""

    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self, file_path: str = None):
        self.file_path = file_path or AUDIT_FILE
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)

    @classmethod
    def get(cls) -> "LLMToolAudit":
        """进程级单例"""
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    def _rotate_if_needed(self):
        """超过大小上限时轮转：.4->.5 ... .1->.2, 当前->.1"""
        try:
            if not os.path.exists(self.file_path):
                return
            if os.path.getsize(self.file_path) < MAX_BYTES:
                return
            for i in range(BACKUP_COUNT - 1, 0, -1):
                src = f"{self.file_path}.{i}"
                dst = f"{self.file_path}.{i + 1}"
                if os.path.exists(src):
                    os.replace(src, dst)  # .5 由 .4 覆盖（超出份数的旧档自然淘汰）
            os.replace(self.file_path, f"{self.file_path}.1")
            logger.info(f"审计日志已轮转: {self.file_path}")
        except Exception as e:
            logger.warning(f"审计日志轮转失败: {e}")

    def record(self, tool: str, args=None, duration_ms: int = 0,
               status: str = "success", error: str = None, user_id: str = None,
               qid: str = None):
        """
        记录一次工具调用

        Args:
            tool: 工具名
            args: 调用入参（dict 或原始JSON字符串，截断至500字符）
            duration_ms: 执行耗时（毫秒）
            status: success / error
            error: 失败原因（可选）
            user_id: 触发用户 openid（调用链上游可知时传入）
            qid: 问答链路 trace ID（贯穿一次问答的 L0/L1/L2 全部日志与审计）
        """
        try:
            if isinstance(args, str):
                args_str = args
            else:
                args_str = json.dumps(args or {}, ensure_ascii=False)
            entry = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "tool": tool,
                "args": args_str[:500],
                "duration_ms": duration_ms,
                "status": status,
            }
            if qid:
                entry["qid"] = qid
            if user_id:
                entry["user_id"] = user_id
            if error:
                entry["error"] = str(error)[:300]
            line = json.dumps(entry, ensure_ascii=False)
            with self._lock:
                self._rotate_if_needed()
                with open(self.file_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception as e:
            logger.warning(f"审计记录写入失败（已忽略）: {e}")
