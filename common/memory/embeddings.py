# -*- coding: utf-8 -*-
"""Embedding 后端接口（可选升级，缺省关闭 → 纯实体匹配）

三种模式（MEMORY_EMBEDDING 环境变量）：
- off（默认）：不启用向量，相关性=实体/主题词匹配
- provider：OpenAI 兼容 /v1/embeddings 端点——远程服务商（EMBEDDING_BASE_URL/KEY/MODEL）
  或本地推理服务器（Ollama/LM Studio，base_url 指本机，KEY 可空）
- local：进程内 fastembed/ONNX 轻量模型（LOCAL_EMBED_MODEL，默认 bge-small-zh-v1.5）
  ⚠️ 需 ≥2GB 空闲内存；fastembed 是可选依赖（requirements-local.txt），不进默认镜像

失败策略：任何后端初始化/调用失败都静默回退（返回 None），由 scoring 退化为实体匹配。
"""
import logging
import os
from typing import List, Optional

logger = logging.getLogger(__name__)


class ProviderEmbedBackend:
    """OpenAI 兼容 embeddings 端点（远程服务商或本地 Ollama/LM Studio 通用）"""

    name = "provider"

    def __init__(self, base_url: str, model: str, api_key: str = ""):
        self.url = base_url.rstrip("/") + "/embeddings"
        self.model = model
        self.api_key = api_key

    def embed(self, texts: List[str]) -> Optional[List[List[float]]]:
        import requests
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        # 代理走 PROXY_URL 约定（大陆服务器访问远程 embedding 服务商需要）；
        # 本地端点（localhost/内网）置空代理直连
        proxy = os.getenv("PROXY_URL", "") or None
        is_local = any(h in self.url for h in ("localhost", "127.0.0.1", "host.docker.internal", "192.168.", "10."))
        proxies = None if is_local else ({"http": proxy, "https": proxy} if proxy else {"http": None, "https": None})
        try:
            r = requests.post(self.url, headers=headers, timeout=20,
                              json={"model": self.model, "input": texts},
                              proxies=proxies)
            if r.status_code != 200:
                logger.warning(f"embedding provider HTTP {r.status_code}: {r.text[:150]}")
                return None
            data = r.json().get("data") or []
            data.sort(key=lambda x: x.get("index", 0))
            return [x.get("embedding") for x in data]
        except Exception as e:
            logger.warning(f"embedding provider 调用失败: {e}")
            return None


class LocalFastEmbedBackend:
    """进程内 fastembed/ONNX 本地向量（高配服务器可选；默认模型约 100MB）"""

    name = "local"

    def __init__(self, model: str = "BAAI/bge-small-zh-v1.5"):
        from fastembed import TextEmbedding  # 可选依赖，未装则抛 ImportError 由工厂捕获
        self._model_name = model
        self._emb = TextEmbedding(model_name=model)

    def embed(self, texts: List[str]) -> Optional[List[List[float]]]:
        try:
            return [list(map(float, v)) for v in self._emb.embed(texts)]
        except Exception as e:
            logger.warning(f"本地 embedding 推理失败: {e}")
            return None


_backend = None
_backend_tried = False
import threading as _threading
_backend_lock = _threading.Lock()


def get_embedding_backend():
    """按 MEMORY_EMBEDDING 探测并缓存后端；off/初始化失败 → None（调用方回退实体匹配）。
    加锁防并发首次调用双初始化（local 模式双份模型加载在 1GB 内存服务器有 OOM 风险）"""
    global _backend, _backend_tried
    if _backend_tried:
        return _backend
    with _backend_lock:
        if _backend_tried:
            return _backend
        _backend_tried = True
        mode = (os.getenv("MEMORY_EMBEDDING", "off") or "off").strip().lower()
        try:
            if mode == "provider":
                base = os.getenv("EMBEDDING_BASE_URL", "").strip()
                model = os.getenv("EMBEDDING_MODEL", "").strip()
                key = os.getenv("EMBEDDING_KEY", "").strip()
                if base and model:
                    if key:
                        from ..secret_guard import register_secret
                        register_secret(key, "EMBEDDING_KEY")
                    _backend = ProviderEmbedBackend(base, model, key)
                    logger.info(f"✓ embedding 后端: provider ({base}, {model})")
                else:
                    logger.warning("MEMORY_EMBEDDING=provider 但 EMBEDDING_BASE_URL/MODEL 未配置，回退实体匹配")
            elif mode == "local":
                model = os.getenv("LOCAL_EMBED_MODEL", "BAAI/bge-small-zh-v1.5").strip()
                _backend = LocalFastEmbedBackend(model)
                logger.info(f"✓ embedding 后端: local fastembed ({model})")
        except ImportError:
            logger.warning("fastembed 未安装（pip install fastembed），回退实体匹配")
        except Exception as e:
            logger.warning(f"embedding 后端初始化失败，回退实体匹配: {e}")
        return _backend
