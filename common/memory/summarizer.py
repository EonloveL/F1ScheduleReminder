# -*- coding: utf-8 -*-
"""会话归档总结器：把一段会话压缩成 1-3 个 L2 主题摘要块

触发点：/clear（归档式清空）、1h 超时、L1 溢出压缩。
模型：llm_prefs 第 6 层 "memory"（管理员 /model set memory <供应商>/<模型> 可配），
缺省 = ask_order 首个非联网供应商（总结不需要联网）；
高配服务器可 /model set memory local/<模型> 走本地 LLM 实现零 API 成本。

失败兜底：LLM 不可用/输出非 JSON → 纯截断拼接单块（内容不丢，质量降级）。
"""
import json
import logging
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_SUMMARIZE_SYSTEM = (
    "你是会话归档助手。把用户与F1机器人的一段对话压缩成 1-3 个主题摘要块，供长期记忆检索。"
    "只输出 JSON 数组，不要任何其他文字："
    '[{"topic": "主题短语（10字内）", "summary": "该主题下用户关心的问题与关键结论'
    "（含具体数据/名字/日期，150字内）\"}]。"
    "规则：按主题聚合而非逐轮复述；纯闲聊/无信息量的轮次跳过；保留事实性内容（积分、名次、时间、结论）；"
    "语言跟随对话原文。"
)


def summarize_turns(llm, turns: List[Dict[str, Any]], max_blocks: int = 3) -> List[Dict[str, str]]:
    """turns: [{"q","a","ts",...}] → [{"topic","summary"}]，失败走兜底"""
    if not turns:
        return []
    convo = "\n".join(f"用户: {t['q'][:200]}\n助手: {t['a'][:400]}" for t in turns)
    convo = convo[:8000]

    text = None
    if llm and getattr(llm, "enabled", False):
        try:
            text = llm.chat_for_layer("memory", _SUMMARIZE_SYSTEM, convo, timeout=45)
        except Exception as e:
            logger.warning(f"会话归档总结失败: {e}")

    if text:
        arr = None
        # 定位首个完整 JSON 数组（raw_decode 防贪婪跨界——模型输出多数组/尾部文字时
        # 正则 \[.*\] 会匹配到尾括号导致解析失败落兜底）
        start = text.find("[")
        if start >= 0:
            try:
                arr = json.JSONDecoder().raw_decode(text[start:])[0]
            except (ValueError, TypeError) as e:
                logger.warning(f"归档总结 JSON 解析失败: {e}")
        if arr:
            try:
                blocks = []
                for item in arr[:max_blocks]:
                    if isinstance(item, dict) and item.get("summary"):
                        blocks.append({"topic": str(item.get("topic") or "会话")[:30],
                                       "summary": str(item["summary"])[:300]})
                if blocks:
                    return blocks
            except (TypeError, KeyError) as e:
                logger.warning(f"归档总结结构异常: {e}")

    # 兜底：截断拼接单块（LLM 不可用时不丢内容）
    qs = "；".join(t["q"][:40] for t in turns[:6])
    return [{"topic": "归档会话",
             "summary": f"用户曾讨论：{qs}"[:300]}]
