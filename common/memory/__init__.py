# -*- coding: utf-8 -*-
"""稀疏注意力式会话记忆 — MemoryFacade（对 llm_assistant 的唯一接口）

设计：三层记忆 + 打分召回（score = 衰减 × 相关性 × 显著性），替代旧的
"最近 6 轮全量注入"滑动窗口（llm_memory_store，已废弃）。

- L1 工作记忆 working：原文问答轮次，半衰期 1h，阈值过滤后最多注入 6 轮
- L2 主题记忆 episodes：/clear、1h 超时、L1 溢出时 LLM 归档摘要，半衰期 3 天，最多注入 3 块
- L3 锚定记忆 anchors：纠错/明示"记住"/主题重复的高显著事实，不衰减，最多注入 5 条

治污核心：新问题与历史条目实体零交集 → score=0 不召回，无关历史不再污染新话题。
"""
import logging
import threading
import time
from typing import Any, Dict, List, Optional

from . import scoring, topics
from .embeddings import get_embedding_backend
from .store import MemoryStore, new_id
from .summarizer import summarize_turns

logger = logging.getLogger(__name__)

SESSION_TTL = 3600          # 1h 无交互后会话归档（替代旧 30min 物理删除）
OVERFLOW_COMPRESS = 6       # L1 超过上限时压缩最老的 N 轮进 L2
RECURRENCE_BOOST = 0.1      # 同实体主题每重复一次显著性加成（cap 见 append_turn）
RECURRENCE_ANCHOR_MIN = 3   # 同实体主题出现 N 次自动锚定


class MemoryFacade:
    def __init__(self, llm=None, store: MemoryStore = None):
        self._llm = llm            # 归档总结用（惰性使用，可为 None → 兜底拼接）
        self._store = store or MemoryStore()
        self._lock = threading.Lock()

    # ==================== 召回 ====================

    def build_context(self, user_id: str, question: str) -> Dict[str, Any]:
        """返回 {"history": [user/assistant 消息...], "memory_block": str|None}
        history 为 L1 筛中轮次（按时间序），memory_block 为 L2+L3 的 system 附加段"""
        if not user_id:
            return {"history": [], "memory_block": None}
        self._maybe_archive_expired(user_id)
        data = self._store.load_user(user_id)
        if not any(data.values()):
            return {"history": [], "memory_block": None}

        now = time.time()
        q_ent = topics.extract_entities(question)
        ref_followup = topics.has_reference_hint(question)
        backend = get_embedding_backend()
        q_vec = self._embed(backend, question)

        # L1 工作记忆：打分 → 阈值 → top-N → 恢复时间序
        # 指代追问强制召回仅施加于最近 2 轮（review H3：施加于全部会污染多话题并存的会话）
        working_sorted = sorted(data["working"], key=lambda t: t.get("ts", 0))
        recent_ids = {t["id"] for t in working_sorted[-2:]} if ref_followup else set()
        scored = []
        for t in data["working"]:
            rel = scoring.relevance(q_ent, set(t.get("entities") or []),
                                    q_vec, t.get("vec"))
            if rel == 0.0:
                # 代词追问（"那他队友呢"）：新问题无实体是指代上文而非换话题，
                # 仅给最近 2 轮强制相关性，否则 L0 改写解析不了代词
                if t.get("id") in recent_ids:
                    rel = 0.5
                else:
                    continue  # 零交集且非追问：不召回（防无关历史污染）
            s = scoring.score(t.get("ts", 0), scoring.HALF_LIFE_L1,
                              t.get("salience", 1.0), rel, now)
            if s >= scoring.THRESHOLD_L1:
                scored.append((s, t))
        scored.sort(key=lambda x: -x[0])
        chosen = [t for _, t in scored[:scoring.MAX_INJECT_L1]]
        chosen.sort(key=lambda t: t.get("ts", 0))  # 消息列表恢复时间序
        history: List[Dict[str, str]] = []
        for t in chosen:
            history.append({"role": "user", "content": t["q"]})
            history.append({"role": "assistant", "content": t["a"]})

        # L2 主题摘要（指代追问时给低权重兜底：/clear 或超时归档后 working 已空，
        # episode 零交集直接跳过会导致上下文完全断裂——review 低 #11）
        ep_lines = []
        eps = []
        for ep in data["episodes"]:
            rel = scoring.relevance(q_ent, set(ep.get("entities") or []),
                                    q_vec, ep.get("vec"))
            if rel == 0.0:
                if ref_followup:
                    rel = 0.3
                else:
                    continue
            s = scoring.score(ep.get("ts_end", 0), scoring.HALF_LIFE_L2,
                              ep.get("salience", 1.0), rel, now)
            if s >= scoring.THRESHOLD_L2:
                eps.append((s, ep))
        eps.sort(key=lambda x: -x[0])
        for s, ep in eps[:scoring.MAX_INJECT_L2]:
            age = self._age_str(now - ep.get("ts_end", 0))
            ep_lines.append(f"· [{age}] {ep.get('topic', '')}：{ep.get('summary', '')}")

        # L3 锚定：不衰减，仅相关性过滤
        an_lines = []
        anchors = []
        for an in data["anchors"]:
            rel = scoring.relevance(q_ent, set(an.get("entities") or []),
                                    q_vec, an.get("vec"))
            if rel > 0.0:
                anchors.append((rel * an.get("salience", 1.0), an))
        anchors.sort(key=lambda x: -x[0])
        for _, an in anchors[:scoring.MAX_INJECT_L3]:
            an_lines.append(f"· {an.get('fact', '')}")

        memory_block = None
        parts = []
        if ep_lines:
            parts.append("【你还记得的更早会话摘要】\n" + "\n".join(ep_lines))
        if an_lines:
            parts.append("【与该用户相关的长期记忆】\n" + "\n".join(an_lines))
        if parts:
            memory_block = "\n\n".join(parts)

        return {"history": history, "memory_block": memory_block}

    def has_active_session(self, user_id: str) -> bool:
        """是否存在未过期的工作记忆（L0 改写跳过判定用）"""
        if not user_id:
            return False
        data = self._store.load_user(user_id)
        working = data["working"]
        if not working:
            return False
        return time.time() - max(t.get("ts", 0) for t in working) <= SESSION_TTL

    # ==================== 写入 ====================

    def append_turn(self, user_id: str, question: str, answer: str, salience: float = 1.0):
        """每轮问答写入 L1；主题重复自动加权/锚定；溢出批量压缩进 L2（后台线程）
        embedding 在后台线程异步补算（回答投递路径 5min 窗口内不做网络调用——review M4）"""
        if not user_id or not question:
            return
        ents = sorted(topics.extract_entities(question + " " + answer[:200]))
        # 主题重复只计强实体（drv:/team:）；泛 topic 词（积分/策略）重叠是常态，
        # 计入会导致第 3 轮起必触发锚定洪泛（review H2）
        strong_ents = {e for e in ents if e.startswith(("drv:", "team:"))}
        backend = get_embedding_backend()

        overflow = None
        with self._lock:
            data = self._store.load_user(user_id)
            overlap_n = sum(1 for t in data["working"]
                            if set(t.get("entities") or []) & strong_ents) if strong_ents else 0
            if overlap_n:
                salience = min(2.0, salience + RECURRENCE_BOOST * overlap_n)
            item_id = new_id()
            data["working"].append({
                "id": item_id, "q": question[:500], "a": answer[:1500],
                "ts": time.time(), "entities": ents, "salience": salience,
            })
            # 同强实体主题反复出现 → 自动锚定（去重键=实体指纹，非问题文本）
            if overlap_n >= RECURRENCE_ANCHOR_MIN and strong_ents:
                self._auto_anchor(data, strong_ents)
            if len(data["working"]) > 12:
                overflow = data["working"][:OVERFLOW_COMPRESS]
                data["working"] = data["working"][OVERFLOW_COMPRESS:]
            self._store.save_user(user_id, data)

        if backend:
            threading.Thread(target=self._patch_vec,
                             args=(user_id, "working", item_id, question, backend),
                             daemon=True, name="memory-embed").start()
        if overflow:
            threading.Thread(target=self._archive_detached, args=(user_id, overflow, "overflow"),
                             daemon=True, name="memory-compress").start()

    def _patch_vec(self, user_id: str, layer: str, item_id: str, text: str, backend):
        """后台补算条目向量（vec 缺失时 relevance 自动退化为实体匹配，无碍主流程）"""
        vec = self._embed(backend, text)
        if not vec:
            return
        with self._lock:
            data = self._store.load_user(user_id)
            for t in data.get(layer, []):
                if t.get("id") == item_id and "vec" not in t:
                    t["vec"] = vec
                    self._store.save_user(user_id, data)
                    return

    def archive_and_clear(self, user_id: str, reason: str = "clear") -> int:
        """归档式清空：当前工作记忆 → LLM 总结 → L2。返回归档轮数（0=没有内容可归档）

        锁内只摘除 working（立刻对用户可见"已清"），LLM 总结放锁外同步执行，
        避免总结期间（最坏 45s）阻塞全部用户的记忆读写 → QQ 被动回复超时风险"""
        if not user_id:
            return 0
        with self._lock:
            data = self._store.load_user(user_id)
            turns = data["working"]
            if not turns:
                return 0
            data["working"] = []
            self._store.save_user(user_id, data)
        # 锁外总结（/clear 是用户等待的同步路径，回复需要轮数；episodes 稍后可见可接受）
        self._archive_detached(user_id, turns, reason)
        return len(turns)

    def mark_anchor(self, user_id: str, fact: str, source: str = "explicit",
                    salience: float = 3.0):
        """写入 L3 锚定记忆（纠错捕获 / "记住这个" / 自动主题重复）
        实体抽取与 embedding 均在锁外完成（锁内不做 I/O——review 低 #12）"""
        if not user_id or not fact:
            return
        ents = sorted(topics.extract_entities(fact))
        vec = self._embed(get_embedding_backend(), fact)
        with self._lock:
            data = self._store.load_user(user_id)
            fact_key = fact[:80]
            for an in data["anchors"]:
                if an.get("fact", "")[:80] == fact_key:
                    an["salience"] = max(an.get("salience", 1.0), salience)
                    an["ts"] = time.time()
                    self._store.save_user(user_id, data)
                    return
            data["anchors"].append({
                "id": new_id(), "fact": fact[:300], "ts": time.time(),
                "entities": ents, "salience": salience, "source": source,
                **({"vec": vec} if vec else {}),
            })
            self._store.save_user(user_id, data)
        logger.info(f"锚定记忆写入(用户 {user_id[:8]}..., {source}): {fact[:40]}")

    # ==================== 管理（/memory 指令） ====================

    def describe(self, user_id: str) -> str:
        """用户自查记忆构成（/memory）"""
        data = self._store.load_user(user_id)
        w, eps, ans = data["working"], data["episodes"], data["anchors"]
        if not (w or eps or ans):
            return "当前没有关于你的记忆（/clear 后历史会归档为主题摘要）"
        lines = [f"🧠 你的记忆构成：工作记忆 {len(w)} 轮 · 主题摘要 {len(eps)} 块 · 长期锚定 {len(ans)} 条", ""]
        if w:
            lines.append("**近期会话**（随时间衰减）")
            for t in w[-6:]:
                lines.append(f"`{t['id']}` {t['q'][:40]}")
        if eps:
            lines.append("\n**主题摘要**（归档历史）")
            for ep in eps[-8:]:
                lines.append(f"`{ep['id']}` {ep.get('topic', '')}：{ep.get('summary', '')[:60]}")
        if ans:
            lines.append("\n**长期锚定**")
            for an in ans[-8:]:
                lines.append(f"`{an['id']}` {an.get('fact', '')[:60]}")
        lines.append("\n删除某条：`/memory forget <id>`；清空并归档：`/clear`")
        return "\n".join(lines)

    def forget(self, user_id: str, item_id: str) -> bool:
        # 与 append_turn/_archive_detached 的 load-modify-save 共用 facade 锁，
        # 防 store 层 forget 被并发整体覆盖（lost update——review H1）
        with self._lock:
            return self._store.remove_item(user_id, (item_id or "").strip())

    # ==================== 内部 ====================

    @staticmethod
    def _embed(backend, text: str) -> Optional[List[float]]:
        if not backend:
            return None
        try:
            vecs = backend.embed([text[:1000]])
            if vecs and vecs[0]:
                return [round(x, 4) for x in vecs[0]]
        except Exception:
            pass
        return None

    @staticmethod
    def _age_str(seconds: float) -> str:
        if seconds < 3600:
            return f"{int(seconds // 60)}分钟前"
        if seconds < 86400:
            return f"{int(seconds // 3600)}小时前"
        return f"{int(seconds // 86400)}天前"

    def _maybe_archive_expired(self, user_id: str):
        """惰性 TTL：工作记忆最新轮已超 1h → 整个会话归档进 L2（后台线程，不阻塞回答）"""
        data = self._store.load_user(user_id)
        working = data["working"]
        if not working:
            return
        latest = max(t.get("ts", 0) for t in working)
        if time.time() - latest > SESSION_TTL:
            threading.Thread(target=self._archive_and_remove_working,
                             args=(user_id, "timeout"), daemon=True,
                             name="memory-archive").start()

    def _archive_and_remove_working(self, user_id: str, reason: str):
        with self._lock:
            data = self._store.load_user(user_id)
            turns = data["working"]
            if not turns:
                return
            latest = max(t.get("ts", 0) for t in turns)
            if time.time() - latest <= SESSION_TTL:
                return  # 竞态：归档线程启动期间用户又发问了
            data["working"] = []
            self._store.save_user(user_id, data)
        # 锁外总结回写（本就在后台线程，不占锁）
        self._archive_detached(user_id, turns, reason)

    def _archive_detached(self, user_id: str, turns: List[Dict[str, Any]], reason: str):
        """锁外总结（LLM 可能耗时数十秒），完成后回写 episodes（含 summary 向量）"""
        eps = self._make_episodes(turns, reason)
        if not eps:
            return
        backend = get_embedding_backend()
        if backend:
            for ep in eps:
                vec = self._embed(backend, f"{ep['topic']} {ep['summary']}")
                if vec:
                    ep["vec"] = vec
        with self._lock:
            data = self._store.load_user(user_id)
            data["episodes"].extend(eps)
            self._store.save_user(user_id, data)

    def _compress_overflow(self, user_id: str):
        # 兼容保留：溢出压缩已内联进 append_turn（锁内摘除+锁外回写）
        pass

    def _make_episodes(self, turns: List[Dict[str, Any]], reason: str) -> List[Dict[str, Any]]:
        """turns → 摘要块（LLM 总结）。纯函数，不动存储。
        异常兜底为原始拼接（detach-first 模式下返回空 = 轮次永久丢失——review M5）"""
        try:
            blocks = summarize_turns(self._llm, turns)
        except Exception as e:
            logger.warning(f"归档总结异常，兜底原始拼接: {e}")
            qs = "；".join(t["q"][:40] for t in turns[:6])
            blocks = [{"topic": "归档会话", "summary": f"用户曾讨论：{qs}"[:300]}]
        all_ents = set()
        for t in turns:
            all_ents.update(t.get("entities") or [])
        ts_start = min(t.get("ts", 0) for t in turns)
        ts_end = max(t.get("ts", 0) for t in turns)
        eps = [{
            "id": new_id(), "topic": b["topic"], "summary": b["summary"],
            "entities": sorted(all_ents), "ts_start": ts_start, "ts_end": ts_end,
            "salience": 1.0, "source": reason,
        } for b in blocks]
        logger.info(f"会话归档({reason}): {len(turns)} 轮 → {len(eps)} 块主题摘要")
        return eps

    def _archive_turns(self, data: Dict[str, Any], turns: List[Dict[str, Any]], reason: str):
        """锁内归档（仅遗留兼容；新路径一律 _archive_detached 锁外总结）"""
        data["episodes"].extend(self._make_episodes(turns, reason))

    def _auto_anchor(self, data: Dict[str, Any], strong_ents):
        """同强实体主题反复出现 ≥N 次 → 自动锚定用户关注倾向
        去重键=实体指纹（非问题文本），同指纹只提权不重复新增（review H2）"""
        fp = ",".join(sorted(strong_ents))
        for an in data["anchors"]:
            if an.get("fp") == fp:
                an["salience"] = min(3.0, an.get("salience", 2.0) + 0.2)
                an["ts"] = time.time()
                return
        data["anchors"].append({
            "id": new_id(), "fp": fp, "ts": time.time(),
            "fact": f"用户持续关注实体: {fp}（多次提及，显著性自动加权）",
            "entities": sorted(strong_ents), "salience": 2.0, "source": "recurrence",
        })
