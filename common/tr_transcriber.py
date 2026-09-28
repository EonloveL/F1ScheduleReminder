"""
F1车队无线电（Team Radio）文字转写模块
基于 faster-whisper 本地转写 OpenF1 提供的 TR 音频（mp3）

设计：
- 懒加载模型（首次转写时才下载/加载）
- 后台线程池异步转写，避免阻塞请求
- 按 URL 内存缓存转写结果
- 优雅降级：faster-whisper 未安装 / 模型加载失败 / 转写失败，均返回 None，
  调用方回退为"仅音频链接"展示

依赖：pip install faster-whisper
模型：默认 base（可通过 WHISPER_MODEL 环境变量指定 tiny/base/small/...）
HF 镜像：国内环境建议设置 HF_ENDPOINT=https://hf-mirror.com
"""

import logging
import os
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

DEFAULT_MODEL = os.getenv("WHISPER_MODEL", "base")
DEFAULT_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "en")


class TeamRadioTranscriber:
    """TR 音频转文字，异步 + 缓存 + 降级"""

    def __init__(self, model_size: str = None, max_workers: int = 1):
        self.model_size = model_size or DEFAULT_MODEL
        self.language = DEFAULT_LANGUAGE or None
        self._model = None
        self._load_tried = False
        self._lock = threading.Lock()
        self._results: Dict[str, Dict] = {}  # url -> {"status": pending/done/error, "text": ...}
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="tr-transcribe")

    @property
    def available(self) -> bool:
        """faster-whisper 是否可用（首次转写尝试后才确定）"""
        return self._model is not None

    def _load_model(self):
        if self._load_tried:
            return self._model
        self._load_tried = True
        try:
            from faster_whisper import WhisperModel
            self._model = WhisperModel(self.model_size, device="cpu", compute_type="int8")
            logger.info(f"✓ faster-whisper 模型加载成功: {self.model_size}")
        except Exception as e:
            logger.warning(f"⚠️ faster-whisper 不可用（TR文字转写禁用）: {e}")
            self._model = None
        return self._model

    MAX_PENDING = 30  # 待转写队列上限，超出丢弃新任务（防 TR 突发积压拖垮小内存机器）

    def submit(self, url: str):
        """提交一条 TR 音频转写任务（幂等：同一 URL 只处理一次）"""
        if not url:
            return
        with self._lock:
            if url in self._results:
                return
            pending = sum(1 for r in self._results.values() if r.get("status") == "pending")
            if pending >= self.MAX_PENDING:
                logger.warning(f"TR转写队列已满({pending})，丢弃新任务")
                return
            self._results[url] = {"status": "pending"}
        self._executor.submit(self._transcribe, url)

    def _transcribe(self, url: str):
        try:
            model = self._load_model()
            if not model:
                self._set(url, "error", None)
                return
            text = self._download_and_transcribe(model, url)
            if text:
                self._set(url, "done", text)
            else:
                self._set(url, "error", None)
        except Exception as e:
            logger.warning(f"TR转写失败: {e}")
            self._set(url, "error", None)

    def _download_and_transcribe(self, model, url: str) -> Optional[str]:
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        try:
            resp = requests.get(url, timeout=25,
                                headers={"User-Agent": "F1-Reminder-Bot/1.0"})
            resp.raise_for_status()
            with open(path, "wb") as f:
                f.write(resp.content)
            segments, _info = model.transcribe(path, language=self.language, beam_size=1)
            text = " ".join(s.text.strip() for s in segments).strip()
            return text or None
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def _set(self, url: str, status: str, text: Optional[str]):
        with self._lock:
            self._results[url] = {"status": status, "text": text}

    def get(self, url: str) -> Optional[str]:
        """返回转写文本；未完成/失败返回 None"""
        with self._lock:
            r = self._results.get(url)
            if r and r.get("status") == "done":
                return r.get("text")
        return None

    def is_pending(self, url: str) -> bool:
        with self._lock:
            r = self._results.get(url)
            return bool(r and r.get("status") == "pending")
