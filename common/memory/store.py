# -*- coding: utf-8 -*-
"""记忆持久化：三层结构 + JSON 后端（协议预留 SQLite 后端）

数据结构（data/memory_store.json）：
{
  "users": {
    "<uid>": {
      "working":  [{"id","q","a","ts","entities":[],"salience":1.0,"vec":[]?}],   # L1 工作记忆（原文轮次）
      "episodes": [{"id","topic","summary","entities":[],"ts_start","ts_end",
                    "salience":1.0,"source":"clear|timeout|overflow","vec":[]?}], # L2 主题摘要
      "anchors":  [{"id","fact","entities":[],"ts","salience":3.0,
                    "source":"correction|explicit|recurrence"}]                   # L3 锚定记忆
    }
  }
}

存储纪律与 user_prefs/pushed_store 一致：JSON + threading.Lock + 原子写。
容量上限（防长尾膨胀，轻量服务器友好）：working ≤12 轮 / episodes ≤50 / anchors ≤30。
"""
import json
import logging
import os
import threading
import uuid
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data")
DEFAULT_PATH = os.path.join(DATA_DIR, "memory_store.json")

MAX_WORKING = 12    # L1 原始轮次上限（超过即批量压缩进 L2）
MAX_EPISODES = 50
MAX_ANCHORS = 30


def new_id() -> str:
    return uuid.uuid4().hex[:8]


class MemoryStore:
    """JSON 后端。接口即协议：未来 SQLite 后端实现同一组方法即可热替换"""

    def __init__(self, file_path: str = None):
        self.file_path = file_path or DEFAULT_PATH
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        self._data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("users"), dict):
                    return data
            except Exception as e:
                logger.error(f"读取记忆库失败: {e}")
        return {"users": {}}

    def _save(self):
        try:
            tmp = self.file_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False)
            os.replace(tmp, self.file_path)
        except Exception as e:
            logger.error(f"保存记忆库失败: {e}")

    @staticmethod
    def _blank() -> Dict[str, List]:
        return {"working": [], "episodes": [], "anchors": []}

    def load_user(self, user_id: str) -> Dict[str, List]:
        with self._lock:
            u = self._data["users"].get(user_id)
            if not u:
                return self._blank()
            for k in ("working", "episodes", "anchors"):
                if not isinstance(u.get(k), list):
                    u[k] = []
            return {"working": list(u["working"]), "episodes": list(u["episodes"]),
                    "anchors": list(u["anchors"])}

    def save_user(self, user_id: str, data: Dict[str, List]):
        with self._lock:
            data["working"] = data.get("working", [])[-MAX_WORKING:]
            data["episodes"] = data.get("episodes", [])[-MAX_EPISODES:]
            # 锚定分层驱逐（review H2）：超限时先淘汰 recurrence 自动锚定，
            # correction/explicit（人工纠错/明示"记住"）价值最高，留到最后
            anchors = data.get("anchors", [])
            if len(anchors) > MAX_ANCHORS:
                keep = [a for a in anchors if a.get("source") != "recurrence"]
                recur = [a for a in anchors if a.get("source") == "recurrence"]
                room = MAX_ANCHORS - len(keep)
                anchors = keep + (recur[-room:] if room > 0 else [])
                anchors = anchors[-MAX_ANCHORS:]
            data["anchors"] = anchors
            self._data["users"][user_id] = data
            self._save()

    def delete_user(self, user_id: str):
        with self._lock:
            self._data["users"].pop(user_id, None)
            self._save()

    def remove_item(self, user_id: str, item_id: str) -> bool:
        """按 id 删除任一层的条目（/memory forget）"""
        with self._lock:
            u = self._data["users"].get(user_id)
            if not u:
                return False
            for layer in ("working", "episodes", "anchors"):
                items = u.get(layer) or []
                new_items = [x for x in items if x.get("id") != item_id]
                if len(new_items) != len(items):
                    u[layer] = new_items
                    self._save()
                    return True
            return False

    def all_user_ids(self) -> List[str]:
        with self._lock:
            return list(self._data["users"].keys())
