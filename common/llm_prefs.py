# -*- coding: utf-8 -*-
"""LLM 分层模型偏好（管理员 /model 指令）+ 供应商可用模型清单缓存。

prefs: data/llm_prefs.json — {"layers": {"l2": {"provider": "kimi", "model": "kimi-k2.6"}}}
  层定义：l0=查询改写 l1=取数 l2=成文 deep=深度分析 vision=识图
  默认（无配置）= 现状：l0/l1 deepseek，l2 kimi，deep kimi-k3，vision kimi-k2.6
models 缓存: data/llm_models_cache.json — {"kimi": {"models": [...], "fetched": ts}}
两个文件都是运行时数据，已列入打包排除清单（勿覆盖生产端）。
"""
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

LAYERS = ("l0", "l1", "l2", "deep", "vision")
LAYER_NAMES_CN = {"l0": "查询改写", "l1": "取数", "l2": "成文", "deep": "深度分析", "vision": "识图"}

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
_PREFS_PATH = os.path.join(_DATA_DIR, "llm_prefs.json")
_MODELS_PATH = os.path.join(_DATA_DIR, "llm_models_cache.json")

_lock = threading.Lock()
_prefs_cache = None
_prefs_mtime = 0.0


def _atomic_write(path: str, data: Dict[str, Any]):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _load_prefs() -> Dict[str, Any]:
    global _prefs_cache, _prefs_mtime
    try:
        mt = os.path.getmtime(_PREFS_PATH)
    except OSError:
        mt = 0
    if _prefs_cache is not None and mt == _prefs_mtime:
        return _prefs_cache
    try:
        with open(_PREFS_PATH, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data.get("layers"), dict):
            data = {"layers": {}}
    except Exception:
        data = {"layers": {}}
    _prefs_cache = data
    _prefs_mtime = mt
    return data


def get_layer(layer: str) -> Optional[Dict[str, str]]:
    """该层的管理员指定 {"provider","model"}，无配置返回 None"""
    if layer not in LAYERS:
        return None
    with _lock:
        p = (_load_prefs()["layers"].get(layer) or {})
    if p.get("provider") and p.get("model"):
        return {"provider": p["provider"], "model": p["model"]}
    return None


def set_layer(layer: str, provider: str, model: str) -> bool:
    if layer not in LAYERS:
        return False
    global _prefs_cache
    with _lock:
        data = _load_prefs()
        data["layers"][layer] = {"provider": provider, "model": model,
                                 "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        _atomic_write(_PREFS_PATH, data)
        _prefs_cache = None  # 强制下次重读
    return True


def reset_layer(layer: str = None):
    """重置指定层或全部"""
    global _prefs_cache
    with _lock:
        data = _load_prefs()
        if layer:
            data["layers"].pop(layer, None)
        else:
            data["layers"] = {}
        _atomic_write(_PREFS_PATH, data)
        _prefs_cache = None


def all_layers() -> Dict[str, Dict[str, str]]:
    with _lock:
        return dict(_load_prefs()["layers"])


# ---------- 供应商模型清单缓存 ----------

def get_models_cache(provider: str, ttl_hours: int = 24) -> Optional[List[str]]:
    try:
        with open(_MODELS_PATH, encoding="utf-8") as f:
            entry = (json.load(f).get(provider) or {})
        if entry.get("models") and time.time() - entry.get("fetched", 0) < ttl_hours * 3600:
            return entry["models"]
    except Exception:
        pass
    return None


def save_models_cache(provider: str, models: List[str]):
    with _lock:  # 与 prefs 共用锁；tmp 名带线程 id 防并发写冲突
        try:
            data = {}
            if os.path.exists(_MODELS_PATH):
                with open(_MODELS_PATH, encoding="utf-8") as f:
                    data = json.load(f)
        except Exception:
            data = {}
        data[provider] = {"models": models, "fetched": time.time()}
        tmp = f"{_MODELS_PATH}.{threading.get_ident()}.tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, _MODELS_PATH)
        except Exception as e:
            logger.warning(f"模型清单缓存写入失败: {e}")
