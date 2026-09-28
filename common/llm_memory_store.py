"""
F1赛程提醒机器人 - LLM对话记忆持久化

对应Agent架构中的 JdbcChatMemoryStore：会话记忆落盘（data/llm_memory.json），
Docker容器重启/重建后上下文不丢。

窗口语义与原有内存实现完全一致：每用户最近 6 轮问答、30 分钟无交互过期。
存储惯例与 pushed_store / user_prefs 一致：JSON文件 + threading.Lock。
键为统一身份 member_openid（author.id，群聊/私聊互通）。
"""

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
MEMORY_FILE = os.path.join(DATA_DIR, "llm_memory.json")

MAX_TURNS = 6               # 每个用户保留最近 N 轮问答
SESSION_TTL = 30 * 60       # 30 分钟无交互自动过期


class LLMMemoryStore:
    """LLM多轮会话记忆（JSON文件持久化，线程安全）"""

    def __init__(self, file_path: str = None):
        self.file_path = file_path or MEMORY_FILE
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        self._data: Dict[str, Dict[str, Any]] = self._load()
        # 启动时顺手清理过期会话，控制文件体积
        self._purge_expired()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    logger.info(f"✓ LLM会话记忆已加载: {len(data)} 个用户")
                    return data
            except Exception as e:
                logger.error(f"读取LLM会话记忆失败: {e}")
        return {}

    def _save(self):
        """原子写：临时文件+rename，防崩溃留下半截JSON导致会话记忆全丢"""
        try:
            tmp_path = self.file_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.file_path)
        except Exception as e:
            logger.error(f"保存LLM会话记忆失败: {e}")

    def _purge_expired(self):
        """清理过期会话（需在锁内调用或由调用方保证）"""
        now = time.time()
        expired = [uid for uid, conv in self._data.items()
                   if now - conv.get("ts", 0) > SESSION_TTL]
        for uid in expired:
            self._data.pop(uid, None)
        return len(expired)

    def get_turns(self, user_id: str) -> List[Dict[str, str]]:
        """取该用户的会话历史（过期自动清除），返回 [{role, content}, ...]"""
        if not user_id:
            return []
        with self._lock:
            conv = self._data.get(user_id)
            if not conv:
                return []
            if time.time() - conv.get("ts", 0) > SESSION_TTL:
                self._data.pop(user_id, None)
                self._save()
                return []
            return list(conv.get("turns", []))

    def append_turn(self, user_id: str, question: str, answer: str):
        """把一轮问答追加到该用户会话并落盘（保留最近 MAX_TURNS 轮）"""
        if not user_id:
            return
        with self._lock:
            conv = self._data.setdefault(user_id, {"turns": [], "ts": 0})
            conv["turns"].append({"role": "user", "content": question})
            conv["turns"].append({"role": "assistant", "content": answer})
            conv["ts"] = time.time()
            conv["turns"] = conv["turns"][-(MAX_TURNS * 2):]
            self._purge_expired()
            self._save()

    def clear(self, user_id: str) -> bool:
        """清空该用户会话上下文，返回是否清掉了已有会话"""
        with self._lock:
            existed = self._data.pop(user_id, None) is not None
            if existed:
                self._save()
            return existed
