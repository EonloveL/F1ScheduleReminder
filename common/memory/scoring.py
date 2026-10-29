# -*- coding: utf-8 -*-
"""记忆打分：衰减(Δt) × 相关性 × 显著性

score = recency × relevance × salience
- recency:   指数衰减 0.5 ** (Δt / 半衰期)；L1 工作记忆 1h，L2 主题摘要 3 天，L3 锚定不衰减
- relevance: 实体 Jaccard（默认）或向量余弦（配置 embedding 后端时）；零交集 = 0（不召回）
- salience:  显著性倍率（纠错/明示"记住"/主题重复自动加权）
"""
import math
import time
from typing import List, Optional, Set

HALF_LIFE_L1 = 3600.0          # 工作记忆半衰期 1 小时
HALF_LIFE_L2 = 3 * 86400.0     # 主题摘要半衰期 3 天
# L3 锚定记忆不衰减（recency 恒 1）

NEUTRAL_REL = 0.3              # 双方都无实体（闲聊/通用问题）时的兜底相关性
THRESHOLD_L1 = 0.10            # L1 召回阈值
THRESHOLD_L2 = 0.20            # L2 召回阈值
MAX_INJECT_L1 = 6              # 最多注入工作记忆轮数
MAX_INJECT_L2 = 3              # 最多注入主题摘要块
MAX_INJECT_L3 = 5              # 最多注入锚定事实


def recency(ts: float, now: float = None, half_life: float = None) -> float:
    """指数时间衰减；half_life=None 表示不衰减（锚定记忆）"""
    if half_life is None:
        return 1.0
    now = now or time.time()
    dt = max(0.0, now - ts)
    return 0.5 ** (dt / half_life)


def cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if not na or not nb:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def relevance(q_entities: Set[str], item_entities: Set[str],
              q_vec: Optional[List[float]] = None,
              item_vec: Optional[List[float]] = None) -> float:
    """相关性：实体 Jaccard 优先；配置了向量且双方都有向量时用余弦（取两者较大值）。
    双方都无实体/向量 → NEUTRAL 兜底（多轮闲聊不被误杀）"""
    from .topics import jaccard
    ent = jaccard(q_entities, item_entities)
    vec = cosine(q_vec, item_vec) if (q_vec and item_vec) else 0.0
    rel = max(ent, vec)
    if rel == 0.0 and not q_entities and not item_entities:
        return NEUTRAL_REL
    return rel


def score(recency_ts: float, half_life: Optional[float], salience: float,
          rel: float, now: float = None) -> float:
    return recency(recency_ts, now, half_life) * rel * max(0.1, salience)
