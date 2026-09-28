"""
F1赛程提醒机器人 - 群知识沉淀与用户画像

从用户对话中沉淀知识，两层闸门设计（2026-09-15 用户确认）：
1. 事实校对（自动）：候选知识先经 LLM+数据工具核验，核对失败直接淘汰
2. 人工审核（管理员 /kb 指令）：校对通过的进待审队列，管理员批准才入库

四个存储（JSON + 锁 + 原子写，与项目存储惯例一致）：
- data/qa_archive.jsonl       问答归档（append-only，5MB×3 轮转），蒸馏原料
- data/knowledge_candidates.json  候选区（pending/factcheck_failed/approved/rejected）
- data/group_knowledge.json   正式知识库（审核通过的 F1 事实，注入 L2 成文上下文）
- data/user_habits.json       用户画像（提问中命中的车手/车队/话题统计），注入 L2

隐私边界：用户已确认无需群内告知；问答原文仅存服务器本地归档文件，不外发。
"""

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
QA_ARCHIVE_FILE = os.path.join(DATA_DIR, "qa_archive.jsonl")
CANDIDATES_FILE = os.path.join(DATA_DIR, "knowledge_candidates.json")
KNOWLEDGE_FILE = os.path.join(DATA_DIR, "group_knowledge.json")
HABITS_FILE = os.path.join(DATA_DIR, "user_habits.json")

QA_MAX_BYTES = 5 * 1024 * 1024  # 归档单文件 5MB
QA_BACKUP_COUNT = 3
MAX_KNOWLEDGE_ITEMS = 50        # 正式知识库上限（注入上下文成本控制）


class KnowledgeStore:
    """知识沉淀存储（单例，线程安全）"""

    _instance = None
    _instance_lock = threading.Lock()

    def __init__(self):
        self._lock = threading.Lock()
        os.makedirs(DATA_DIR, exist_ok=True)
        self._candidates: List[Dict[str, Any]] = self._load_json(CANDIDATES_FILE, [])
        self._knowledge: List[Dict[str, Any]] = self._load_json(KNOWLEDGE_FILE, [])
        self._habits: Dict[str, Dict[str, Any]] = self._load_json(HABITS_FILE, {})
        # 正式知识库 mtime 缓存（注入上下文在问答热路径上，避免每次读盘）
        self._kb_cache: Dict[str, Any] = {"text": None, "ts": 0}

    @classmethod
    def get(cls) -> "KnowledgeStore":
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = cls()
            return cls._instance

    # ==================== 基础读写 ====================

    @staticmethod
    def _load_json(path: str, default):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"读取知识存储失败 {os.path.basename(path)}: {e}")
        return default

    @staticmethod
    def _save_json(path: str, data):
        try:
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            logger.error(f"保存知识存储失败 {os.path.basename(path)}: {e}")

    # ==================== 1. 问答归档（蒸馏原料） ====================

    def archive_qa(self, qid: str, user_id: str, question: str, answer: str,
                   tools: list = None, provider: str = None):
        """归档一次问答（截断控制体积；归档失败绝不阻塞主流程）"""
        try:
            entry = {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                "qid": qid, "user": (user_id or "")[:12],
                "q": (question or "")[:500],
                "a": (answer or "")[:2000],
                "tools": tools or [], "provider": provider or "",
            }
            line = json.dumps(entry, ensure_ascii=False)
            with self._lock:
                self._rotate_qa_if_needed()
                with open(QA_ARCHIVE_FILE, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception as e:
            logger.warning(f"问答归档失败（已忽略）: {e}")

    def _rotate_qa_if_needed(self):
        try:
            if not os.path.exists(QA_ARCHIVE_FILE):
                return
            if os.path.getsize(QA_ARCHIVE_FILE) < QA_MAX_BYTES:
                return
            for i in range(QA_BACKUP_COUNT - 1, 0, -1):
                src, dst = f"{QA_ARCHIVE_FILE}.{i}", f"{QA_ARCHIVE_FILE}.{i + 1}"
                if os.path.exists(src):
                    os.replace(src, dst)
            os.replace(QA_ARCHIVE_FILE, f"{QA_ARCHIVE_FILE}.1")
        except Exception as e:
            logger.warning(f"问答归档轮转失败: {e}")

    def read_qa_since(self, days: int = 7) -> List[Dict[str, Any]]:
        """读最近 N 天的归档（每周蒸馏用）"""
        cutoff = time.time() - days * 86400
        out = []
        try:
            with open(QA_ARCHIVE_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        e = json.loads(line)
                        ts = time.mktime(time.strptime(e.get("ts", "2000"), "%Y-%m-%d %H:%M:%S"))
                        if ts >= cutoff:
                            out.append(e)
                    except (ValueError, TypeError):
                        continue
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"读取问答归档失败: {e}")
        return out

    # ==================== 2. 候选区（两层闸门） ====================

    def add_candidate(self, claim: str, source: str, qid: str = "",
                      user_id: str = "") -> Optional[int]:
        """加入候选区，返回候选 id。source: correction(用户纠错) / distill(每周蒸馏)"""
        claim = (claim or "").strip()
        if not claim:
            return None
        with self._lock:
            # 去重：待审/已入库里已有高度相似的不再加
            for c in self._candidates:
                if c.get("status") in ("pending", "approved") and c.get("claim") == claim:
                    return None
            cid = (max((c.get("id", 0) for c in self._candidates), default=0)
                   + 1)
            self._candidates.append({
                "id": cid, "claim": claim[:600], "source": source,
                "qid": qid, "user": (user_id or "")[:12],
                "status": "pending_factcheck",   # → factcheck_passed(待人工) / factcheck_failed
                "evidence": "", "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            self._save_json(CANDIDATES_FILE, self._candidates)
        logger.info(f"知识候选 #{cid} 入队（{source}），待事实校对: {claim[:50]}")
        return cid

    def set_factcheck_result(self, cid: int, passed: bool, evidence: str):
        """事实校对结果回填（第一层闸门）"""
        with self._lock:
            for c in self._candidates:
                if c.get("id") == cid:
                    c["status"] = "pending" if passed else "factcheck_failed"
                    c["evidence"] = (evidence or "")[:800]
                    self._save_json(CANDIDATES_FILE, self._candidates)
                    logger.info(f"知识候选 #{cid} 事实校对{'通过，待人工审核' if passed else '未通过，淘汰'}")
                    return

    def pending_candidates(self) -> List[Dict[str, Any]]:
        """待人工审核（事实校对已通过）"""
        with self._lock:
            return [c for c in self._candidates if c.get("status") == "pending"]

    def review(self, cid: int, approve: bool, reviewer: str = "") -> Optional[Dict[str, Any]]:
        """人工审核（第二层闸门）。批准 → 移入正式知识库"""
        with self._lock:
            for c in list(self._candidates):
                if c.get("id") != cid:
                    continue
                if c.get("status") != "pending":
                    return None
                c["status"] = "approved" if approve else "rejected"
                c["reviewer"] = reviewer
                if approve:
                    self._knowledge.append({
                        "fact": c["claim"], "source": c.get("source"),
                        "evidence": c.get("evidence", "")[:300],
                        "ts": time.strftime("%Y-%m-%d"),
                    })
                    self._knowledge = self._knowledge[-MAX_KNOWLEDGE_ITEMS:]
                    self._save_json(KNOWLEDGE_FILE, self._knowledge)
                    self._kb_cache["ts"] = 0  # 失效注入缓存
                self._save_json(CANDIDATES_FILE, self._candidates)
                return c
        return None

    # ==================== 3. 正式知识库（注入 L2 上下文） ====================

    def knowledge_block(self) -> str:
        """生成注入 L2 系统提示的知识块（30s mtime 缓存）"""
        now = time.time()
        if self._kb_cache["text"] is not None and now - self._kb_cache["ts"] < 30:
            return self._kb_cache["text"]
        with self._lock:
            items = list(self._knowledge)
        if not items:
            text = ""
        else:
            lines = "\n".join(f"- {k['fact']}" for k in items[-20:])
            text = ("\n\n【群知识库（经事实校对+人工审核的群内沉淀事实，可信度高）】\n" + lines)
        self._kb_cache.update({"text": text, "ts": now})
        return text

    # ==================== 4. 用户画像（偏好习惯） ====================

    def note_question(self, user_id: str, drivers: List[str] = None,
                      teams: List[str] = None, topics: List[str] = None):
        """记录一次提问的命中实体（描述性统计，非事实，无误导风险）"""
        if not user_id:
            return
        with self._lock:
            h = self._habits.setdefault(user_id, {
                "drivers": {}, "teams": {}, "topics": {}, "ask_count": 0})
            h["ask_count"] = h.get("ask_count", 0) + 1
            for d in drivers or []:
                h["drivers"][d] = h["drivers"].get(d, 0) + 1
            for t in teams or []:
                h["teams"][t] = h["teams"].get(t, 0) + 1
            for tp in topics or []:
                h["topics"][tp] = h["topics"].get(tp, 0) + 1
            h["last_ts"] = time.time()
            self._save_json(HABITS_FILE, self._habits)

    def habit_block(self, user_id: str) -> str:
        """生成该用户的画像上下文块（无足够数据时返回空）"""
        if not user_id:
            return ""
        with self._lock:
            h = self._habits.get(user_id)
            if not h or h.get("ask_count", 0) < 3:
                return ""
            top_d = sorted(h.get("drivers", {}).items(), key=lambda x: -x[1])[:3]
            top_t = sorted(h.get("teams", {}).items(), key=lambda x: -x[1])[:2]
        parts = []
        if top_d:
            parts.append("常聊车手: " + "、".join(f"{k}({v}次)" for k, v in top_d))
        if top_t:
            parts.append("常聊车队: " + "、".join(f"{k}({v}次)" for k, v in top_t))
        if not parts:
            return ""
        return ("\n\n【提问者画像（历史提问统计，仅作语气与详略参考，禁止当事实引用）】\n"
                + "；".join(parts))
