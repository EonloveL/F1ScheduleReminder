"""供应商注册表（PROVIDERS）与供应商配置/管理 mixin"""

import logging
import os
from typing import Optional, Dict, Any, List

import requests

logger = logging.getLogger(__name__)


PROVIDERS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1/chat/completions",
        "key_env": "DEEPSEEK_API_KEY",
        "model_env": "DEEPSEEK_MODEL",
        "default_model": "deepseek-chat",
        "web_search": False,
    },
    "kimi": {
        "base_url": "https://api.moonshot.cn/v1/chat/completions",
        "key_env": "MOONSHOT_API_KEY",
        "model_env": "LLM_MODEL",
        # moonshot-v1-auto 已下线（404）。kimi-k2.6 为账户实测可用且支持 $web_search 的默认模型
        "default_model": "kimi-k2.6",
        "web_search": True,
    },
    # OpenAI 兼容通用端点（任意自托管/第三方兼容服务，需三个 env 齐全才启用）
    "openai_compat": {
        "base_url_env": "OPENAI_COMPAT_BASE_URL",
        "key_env": "OPENAI_COMPAT_KEY",
        "model_env": "OPENAI_COMPAT_MODEL",
        "default_model": "",
        "web_search": False,
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "key_env": "OPENROUTER_API_KEY",
        "model_env": "OPENROUTER_MODEL",
        "default_model": "openai/gpt-4o-mini",
        "web_search": False,
    },
    "gemini": {
        # Google 官方 OpenAI 兼容端点
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        "key_env": "GEMINI_API_KEY",
        "model_env": "GEMINI_MODEL",
        "default_model": "gemini-2.5-flash",
        "web_search": False,
    },
    # 本地推理服务器（Ollama / llama.cpp / vLLM / LM Studio，OpenAI 兼容端点）
    # 面向有高配服务器的用户：免 key（本地端点大多不验），模型必须显式配置
    # 用法：LOCAL_LLM_MODEL=qwen2.5:7b（env）或 /llm setmodel local <模型>（运行时）
    "local": {
        "base_url_env": "LOCAL_LLM_BASE_URL",
        "key_env": "LOCAL_LLM_API_KEY",
        "model_env": "LOCAL_LLM_MODEL",
        "default_base_url": "http://localhost:11434/v1/chat/completions",
        "default_model": "",
        "web_search": False,
        "key_optional": True,
    },
}

# 问答默认顺序：Kimi联网优先（F1知识需核实），DeepSeek兜底（可用 LLM_PROVIDER 环境变量覆盖）
DEFAULT_ASK_ORDER = ["kimi", "deepseek", "openai_compat", "openrouter", "gemini", "local"]

# 翻译默认顺序：DeepSeek快速便宜优先（翻译无需联网搜索），Kimi兜底
TRANSLATE_ORDER = ["deepseek", "kimi", "openai_compat", "openrouter", "gemini", "local"]


class ProvidersMixin:
    """供应商配置与运行时管理：_configure_providers / reload_runtime / provider_status /
    _resolve_layer / list_provider_models / list_all_models"""

    def _configure_providers(self):
        """构建可用供应商表与问答顺序：env 为基础，运行时覆盖（管理员私聊配置）优先"""
        from .. import llm_secrets
        overrides = llm_secrets.load_overrides()
        rt_providers = overrides.get("providers") or {}

        available: Dict[str, Dict[str, Any]] = {}
        for name, cfg in PROVIDERS.items():
            rt = rt_providers.get(name) or {}
            api_key = (rt.get("api_key") or "").strip() or os.getenv(cfg["key_env"], "")
            base_url = ((rt.get("base_url") or "").strip()
                        or cfg.get("base_url") or os.getenv(cfg.get("base_url_env", ""), "")
                        or cfg.get("default_base_url", ""))
            # key_optional（本地推理端点）：key 可空，用占位值过鉴权头
            if not api_key and cfg.get("key_optional"):
                api_key = "local-no-key"
            if api_key and base_url:
                model = ((rt.get("model") or "").strip()
                         or os.getenv(cfg["model_env"], "") or cfg["default_model"])
                if not model:
                    # local 无任何显式配置时静默跳过（避免每次启动刷告警）；
                    # 用户显式配了 base_url/key 却缺模型才告警
                    if rt or os.getenv(cfg["key_env"], "") or os.getenv(cfg.get("base_url_env", ""), ""):
                        logger.warning(f"LLM供应商 {name} 缺模型配置（{cfg['model_env']}），跳过")
                    continue
                available[name] = {
                    "api_key": api_key,
                    "base_url": base_url,
                    "model": model,
                    "web_search": cfg["web_search"],
                }
                src = "运行时配置" if rt else "env"
                logger.info(f"✓ LLM供应商可用: {name} (模型: {model}, 联网: {cfg['web_search']}, 来源: {src})")

        # 问答供应商顺序：运行时 preferred > env LLM_PROVIDER > 默认
        order = list(DEFAULT_ASK_ORDER)
        for preferred in (overrides.get("preferred_provider"),
                          os.getenv("LLM_PROVIDER", "").strip().lower()):
            if preferred in PROVIDERS and preferred in order:
                order.remove(preferred)
                order.insert(0, preferred)
                break  # 运行时配置已表达最新管理意图，命中即不再看 env

        with self._providers_lock:
            self.available = available
            self.ask_order = [n for n in order if n in available]
            self.enabled = bool(available)

        if self.enabled:
            logger.info(f"✓ LLM供应商配置就绪，问答顺序: {' -> '.join(self.ask_order) or '无'}")
            if "kimi" not in available:
                logger.warning("⚠️ 未配置MOONSHOT_API_KEY，联网核查功能（圈速纪录）已禁用")
        else:
            logger.warning("⚠️ 未配置任何LLM API Key，LLM功能已禁用")

    def reload_runtime(self) -> bool:
        """管理员运行时配置变更后热重载（无需重启进程）。返回重载后是否可用"""
        self._configure_providers()
        return self.enabled

    def provider_status(self) -> list:
        """对外展示用供应商状态（key 一律脱敏，绝不返回明文）"""
        from .. import llm_secrets
        from ..secret_guard import mask_secret
        rt = llm_secrets.load_overrides()
        out = []
        for name, cfg in PROVIDERS.items():
            entry = rt.get("providers", {}).get(name) or {}
            env_key = os.getenv(cfg["key_env"], "")
            rt_key = (entry.get("api_key") or "").strip()
            key_src = "runtime" if rt_key else ("env" if env_key else "")
            info = self.available.get(name)
            out.append({
                "name": name,
                "active": bool(info),
                "model": (info or {}).get("model") or (entry.get("model") or cfg["default_model"] or "-"),
                "web_search": cfg["web_search"],
                "key_masked": mask_secret(rt_key or env_key) if (rt_key or env_key) else "",
                "key_source": key_src,
                "in_order": name in self.ask_order,
            })
        return out

    def _resolve_layer(self, layer: str, default_provider: str):
        """管理员分层模型偏好（common/llm_prefs，/model 指令配置）：
        Returns (provider, model_override|None)；prefs 供应商未配置时回退默认并告警"""
        try:
            from ..llm_prefs import get_layer
            p = get_layer(layer)
            if p and p["provider"] in self.available:
                return p["provider"], p["model"]
            if p:
                logger.warning(f"层[{layer}]偏好供应商 {p['provider']} 未配置 key，回退默认 {default_provider}")
        except Exception as e:
            logger.debug(f"层偏好读取失败[{layer}]: {e}")
        return default_provider, None

    def list_provider_models(self, provider: str, force: bool = False) -> Optional[List[str]]:
        """GET /models 拉供应商可用模型清单（OpenAI 兼容端点通用），24h 缓存"""
        from ..llm_prefs import get_models_cache, save_models_cache
        if provider not in self.available:
            return None
        if not force:
            cached = get_models_cache(provider)
            if cached:
                return cached
        cfg = self.available.get(provider)  # 热重载判空：供应商运行时被移除则清单不可用
        if not cfg:
            return None
        base = cfg["base_url"]
        models_url = base.replace("/chat/completions", "/models")
        if models_url == base:
            return None
        try:
            r = requests.get(models_url,
                             headers={"Authorization": f"Bearer {cfg['api_key']}"},
                             timeout=15, proxies={"http": None, "https": None})
            if r.status_code != 200:
                logger.warning(f"[{provider}] /models HTTP {r.status_code}")
                return None
            models = sorted(m.get("id") for m in (r.json().get("data") or []) if m.get("id"))
            if models:
                save_models_cache(provider, models)
            return models or None
        except Exception as e:
            logger.warning(f"[{provider}] 模型清单获取失败: {e}")
            return None

    def list_all_models(self, force: bool = False) -> Dict[str, Any]:
        """各供应商可用模型清单汇总（/models 指令用）：{provider: {"current":…, "models":[…]}}"""
        out = {}
        for name, cfg in self.available.items():
            out[name] = {"current": cfg["model"], "web_search": cfg["web_search"],
                         "models": self.list_provider_models(name, force=force)}
        return out
