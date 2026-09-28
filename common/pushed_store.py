"""
F1赛程提醒机器人 - 成绩推送记录
记录已推送的环节成绩，用于轮询补推去重与容器重启自愈
"""

import json
import os
import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
PUSHED_FILE = os.path.join(DATA_DIR, "pushed_results.json")


class PushedResultsStore:
    """成绩推送记录（JSON文件，线程安全）"""

    def __init__(self, file_path: str = None):
        self.file_path = file_path or PUSHED_FILE
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        self._data = self._load()

    def _load(self):
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"读取推送记录失败: {e}")
        return {}

    def _save(self):
        try:
            with open(self.file_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存推送记录失败: {e}")

    @staticmethod
    def _key(season, round_num, session_type) -> str:
        return f"{season}-{round_num}-{session_type}"

    def is_pushed(self, season, round_num, session_type) -> bool:
        return self._key(season, round_num, session_type) in self._data

    def mark_pushed(self, season, round_num, session_type):
        with self._lock:
            key = self._key(season, round_num, session_type)
            if key in self._data:
                return
            self._data[key] = datetime.now(timezone.utc).isoformat()
            self._save()
            logger.info(f"✓ 成绩推送已记录: {key}")
