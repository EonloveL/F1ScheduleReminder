"""
LLM 助手内部实现包（2026-10-29 由 common/llm_assistant.py 拆分而来）

对外门脸仍是 common/llm_assistant.py 的 LLMAssistant（多 mixin 组合）；
本包各模块不许互相 import mixin，共享小工具（tracing/prompts/providers 数据）
由使用方按需 import。
"""
