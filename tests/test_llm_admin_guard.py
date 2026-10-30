"""出网门禁（secret_guard）+ LLM 运行时管理指令（llm_admin/llm_secrets）测试"""
import json
import os

import pytest

from common import secret_guard, llm_secrets
from common.llm_admin import handle_llm_admin
from common.llm_assistant import LLMAssistant


@pytest.fixture(autouse=True)
def isolated_secrets_store(tmp_path, monkeypatch):
    """llm_secrets 落盘路径隔离到临时目录，不碰真实 data/llm_secrets.json"""
    monkeypatch.setattr(llm_secrets, "_PATH", str(tmp_path / "llm_secrets.json"))
    yield


@pytest.fixture
def fresh_llm(monkeypatch):
    """无 env key 的干净 LLMAssistant"""
    for env in ("DEEPSEEK_API_KEY", "MOONSHOT_API_KEY", "OPENAI_COMPAT_KEY",
                "OPENROUTER_API_KEY", "GEMINI_API_KEY", "LLM_PROVIDER"):
        monkeypatch.delenv(env, raising=False)
    return LLMAssistant()


# ---------- secret_guard ----------

def test_scrub_registered_secret():
    secret_guard.register_secret("sk-test1234567890abcdef", "test")
    out = secret_guard.scrub_text("我的key是 sk-test1234567890abcdef 请保密")
    assert "sk-test1234567890abcdef" not in out
    assert "***REDACTED***" in out


def test_scrub_ignores_short_and_none():
    assert secret_guard.scrub_text(None) is None
    assert secret_guard.scrub_text("") == ""
    secret_guard.register_secret("short")  # 过短不注册
    assert "short" in secret_guard.scrub_text("this is short text")


def test_mask_secret():
    assert secret_guard.mask_secret("sk-abcdefghijklmnop") == "sk-ab...op"
    assert secret_guard.mask_secret("tiny") == "***"


# ---------- llm_admin 指令 ----------

def test_status_overview_no_providers(fresh_llm):
    out = handle_llm_admin(fresh_llm, "")
    assert "未配置" in out
    assert "deepseek" in out and "kimi" in out


def test_setkey_enables_provider(fresh_llm):
    assert not fresh_llm.enabled
    out = handle_llm_admin(fresh_llm, "setkey deepseek sk-runtime-key-abcdef123456")
    assert "✅" in out
    assert "sk-runtime-key-abcdef123456" not in out  # 回复必须脱敏
    assert "sk-ru...56" in out
    assert fresh_llm.enabled
    assert "deepseek" in fresh_llm.available
    assert fresh_llm.available["deepseek"]["api_key"] == "sk-runtime-key-abcdef123456"
    # 落盘文件存在且内容正确
    with open(llm_secrets._PATH, encoding="utf-8") as f:
        saved = json.load(f)
    assert saved["providers"]["deepseek"]["api_key"] == "sk-runtime-key-abcdef123456"
    # key 已注册进出网门禁
    assert "sk-runtime-key-abcdef123456" not in secret_guard.scrub_text("leak sk-runtime-key-abcdef123456!")


def test_use_switches_preferred(fresh_llm):
    handle_llm_admin(fresh_llm, "setkey deepseek sk-aaaaaaaaaaaaaaaa")
    handle_llm_admin(fresh_llm, "setkey gemini gemini-key-bbbbbbbb")
    assert fresh_llm.ask_order[0] == "deepseek"  # 默认顺序
    out = handle_llm_admin(fresh_llm, "use gemini")
    assert "gemini" in out
    assert fresh_llm.ask_order[0] == "gemini"


def test_use_without_key_warns(fresh_llm):
    out = handle_llm_admin(fresh_llm, "use gemini")
    assert "⚠️" in out  # 记录了首选但无 key，提示先 setkey


def test_delkey_falls_back(fresh_llm):
    handle_llm_admin(fresh_llm, "setkey deepseek sk-aaaaaaaaaaaaaaaa")
    assert fresh_llm.enabled
    out = handle_llm_admin(fresh_llm, "delkey deepseek")
    assert "✅" in out
    assert not fresh_llm.enabled  # env 也没有 → 下线


def test_reset_clears_all(fresh_llm):
    handle_llm_admin(fresh_llm, "setkey kimi sk-cccccccccccccccc")
    handle_llm_admin(fresh_llm, "use kimi")
    out = handle_llm_admin(fresh_llm, "reset")
    assert "✅" in out
    assert not fresh_llm.enabled


def test_unknown_provider_rejected(fresh_llm):
    assert "❌" in handle_llm_admin(fresh_llm, "setkey foo sk-aaaaaaaaaaaaaaaa")
    assert "❌" in handle_llm_admin(fresh_llm, "use foo")


def test_short_key_rejected(fresh_llm):
    assert "❌" in handle_llm_admin(fresh_llm, "setkey kimi abc")


# ---------- local 供应商（本地推理端点，免 key） ----------

def test_local_provider_no_key(fresh_llm, monkeypatch):
    """local 供应商免 key：仅配模型即启用，默认 Ollama 端点"""
    monkeypatch.setenv("LOCAL_LLM_MODEL", "qwen2.5:7b")
    llm = LLMAssistant()
    assert "local" in llm.available
    assert "11434" in llm.available["local"]["base_url"]
    assert llm.available["local"]["model"] == "qwen2.5:7b"


def test_local_provider_off_without_model(fresh_llm):
    """未配模型时 local 静默不启用"""
    assert "local" not in fresh_llm.available


def test_memory_layer_in_prefs():
    from common import llm_prefs
    assert "memory" in llm_prefs.LAYERS
    assert llm_prefs.LAYER_NAMES_CN["memory"] == "记忆总结"


def test_setmodel_local_via_llm_admin(fresh_llm):
    """运行时 /llm setmodel local 即可启用本地供应商（免 key）"""
    out = handle_llm_admin(fresh_llm, "setmodel local qwen2.5:7b")
    assert "✅" in out
    assert "local" in fresh_llm.available
    out2 = handle_llm_admin(fresh_llm, "use local")
    assert "✅" in out2
    assert fresh_llm.ask_order[0] == "local"
