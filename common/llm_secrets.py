# -*- coding: utf-8 -*-
"""LLM 运行时配置（管理员私聊配置的服务商密钥/优先顺序）

存储：data/llm_secrets.json
  {"providers": {"gemini": {"api_key": "...", "model": "...", "base_url": "..."}},
   "preferred_provider": "gemini",
   "updated": "..."}

安全约束（出网门禁配套）：
- 本文件绝不进 git（data/ 整体 gitignore）、不进 Docker 镜像（dockerignore）、
  不进 QQ 部署 zip（build_zip 排除清单）、不同步 OSS 仓
- 写入时同步把 key 注册进 secret_guard（出站消息/日志自动脱敏）
- 任何对外展示一律经 secret_guard.mask_secret 脱敏
"""
import json
import logging
import os
import stat
import threading
import time
from typing import Any, Dict, Optional

from . import secret_guard

logger = logging.getLogger(__name__)

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_PATH = os.path.join(_DATA_DIR, "llm_secrets.json")

_lock = threading.Lock()


def _atomic_write(data: Dict[str, Any]):
    tmp = _PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _PATH)
    try:
        os.chmod(_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 0o600，仅属主可读写（POSIX；Windows 忽略失败）
    except OSError:
        pass


def _load() -> Dict[str, Any]:
    try:
        with open(_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data.get("providers"), dict):
            data["providers"] = {}
        return data
    except Exception:
        return {"providers": {}}


def load_overrides() -> Dict[str, Any]:
    """读取全部运行时覆盖（LLMAssistant 初始化/重载用）"""
    with _lock:
        return _load()


def set_provider_config(provider: str, api_key: str = None, model: str = None,
                        base_url: str = None) -> Dict[str, Any]:
    """设置某供应商的运行时配置（None 字段保持不变）"""
    with _lock:
        data = _load()
        entry = data["providers"].setdefault(provider, {})
        if api_key is not None:
            entry["api_key"] = api_key.strip()
        if model is not None:
            entry["model"] = model.strip()
        if base_url is not None:
            entry["base_url"] = base_url.strip()
        entry["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        _atomic_write(data)
    if api_key:
        secret_guard.register_secret(api_key.strip(), f"llm_secrets:{provider}")
    return entry


def clear_provider(provider: str) -> bool:
    """移除某供应商的全部运行时覆盖（回退 env）"""
    with _lock:
        data = _load()
        if provider not in data["providers"]:
            return False
        data["providers"].pop(provider)
        _atomic_write(data)
    return True


def set_preferred_provider(provider: Optional[str]):
    """设置问答首选供应商（None 清除，回退 env LLM_PROVIDER/默认顺序）"""
    with _lock:
        data = _load()
        if provider:
            data["preferred_provider"] = provider
        else:
            data.pop("preferred_provider", None)
        _atomic_write(data)


def get_preferred_provider() -> Optional[str]:
    with _lock:
        return _load().get("preferred_provider")


def reset_all():
    with _lock:
        _atomic_write({"providers": {}})


def register_all_to_guard():
    """进程启动时把已存的运行时 key 注册进出网门禁（main 初始化时调用）"""
    for prov, entry in _load().get("providers", {}).items():
        key = (entry or {}).get("api_key", "")
        if key:
            secret_guard.register_secret(key, f"llm_secrets:{prov}")
