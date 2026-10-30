# -*- coding: utf-8 -*-
"""LLM 分层模型偏好 + 供应商注册表（离线单测）"""
import importlib
import os

import pytest

from common import llm_prefs
from common.llm_assistant import LLMAssistant, PROVIDERS


@pytest.fixture(autouse=True)
def isolate_prefs(tmp_path, monkeypatch):
    monkeypatch.setattr(llm_prefs, "_PREFS_PATH", str(tmp_path / "llm_prefs.json"))
    monkeypatch.setattr(llm_prefs, "_prefs_cache", None)
    monkeypatch.setattr(llm_prefs, "_prefs_mtime", 0.0)
    yield
    monkeypatch.setattr(llm_prefs, "_prefs_cache", None)


class TestLayerPrefs:
    def test_default_empty(self):
        assert llm_prefs.get_layer("l2") is None

    def test_set_get_reset(self):
        assert llm_prefs.set_layer("l2", "kimi", "kimi-k3")
        assert llm_prefs.get_layer("l2") == {"provider": "kimi", "model": "kimi-k3"}
        llm_prefs.reset_layer("l2")
        assert llm_prefs.get_layer("l2") is None

    def test_invalid_layer(self):
        assert not llm_prefs.set_layer("nope", "kimi", "x")
        assert llm_prefs.get_layer("nope") is None

    def test_reset_all(self):
        llm_prefs.set_layer("l1", "deepseek", "deepseek-chat")
        llm_prefs.set_layer("vision", "gemini", "gemini-2.5-flash")
        llm_prefs.reset_layer()
        assert llm_prefs.all_layers() == {}


class TestProviderRegistry:
    def test_new_providers_registered(self):
        for name in ("openai_compat", "openrouter", "gemini"):
            assert name in PROVIDERS

    def test_openai_compat_requires_full_env(self, monkeypatch):
        monkeypatch.delenv("OPENAI_COMPAT_KEY", raising=False)
        monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://x/v1/chat/completions")
        monkeypatch.setenv("OPENAI_COMPAT_MODEL", "m")
        llm = LLMAssistant()
        assert "openai_compat" not in llm.available

    def test_openai_compat_full_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_COMPAT_KEY", "sk-x")
        monkeypatch.setenv("OPENAI_COMPAT_BASE_URL", "https://x/v1/chat/completions")
        monkeypatch.setenv("OPENAI_COMPAT_MODEL", "my-model")
        llm = LLMAssistant()
        assert llm.available["openai_compat"]["model"] == "my-model"
        assert llm.available["openai_compat"]["base_url"] == "https://x/v1/chat/completions"

    def test_layer_resolve_fallback(self, monkeypatch):
        monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-x")
        llm = LLMAssistant()
        # prefs 指向未配置的供应商 → 回退默认
        llm_prefs.set_layer("l2", "openrouter", "openai/gpt-4o")
        prov, mdl = llm._resolve_layer("l2", "kimi")
        assert (prov, mdl) == ("kimi", None)
        # prefs 指向已配置供应商 → 生效
        llm_prefs.set_layer("l2", "deepseek", "deepseek-chat")
        prov, mdl = llm._resolve_layer("l2", "kimi")
        assert (prov, mdl) == ("deepseek", "deepseek-chat")
