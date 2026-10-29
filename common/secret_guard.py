# -*- coding: utf-8 -*-
"""敏感信息出网门禁（secret guard）

三道防线：
1. 发送 chokepoint 脱敏：QQGroupBot/DiscordBot 的所有出站消息过 scrub_text()，
   已注册密钥出现在文本中一律替换为 ***（防 LLM 回答/错误堆栈/转发意外带出 key）
2. 日志过滤器：SecretScrubFilter 挂到 root logger，日志文件同样不落明文
3. 存储隔离：运行时密钥存 data/llm_secrets.json（gitignore/dockerignore/打包排除/OSS 不同步）

注册的密钥来自两处：进程启动时的已知 env（本模块 auto_register_env），
与管理员运行时配置的 key（llm_secrets 写入时调用 register_secret）。
"""
import logging
import os
import threading
from typing import List

logger = logging.getLogger(__name__)

# 长度阈值：短于此不注册（防误伤正常文本，如 "1234" 这种弱值）
MIN_SECRET_LEN = 8
REDACTED = "***REDACTED***"

# 进程启动时自动注册的敏感环境变量
_ENV_SECRET_NAMES = (
    "QQ_APP_SECRET", "BOT_WEBHOOK_SECRET",
    "DISCORD_BOT_TOKEN",
    "MOONSHOT_API_KEY", "DEEPSEEK_API_KEY", "LLM_API_KEY",
    "OPENAI_COMPAT_KEY", "OPENAI_COMPAT_API_KEY",
    "OPENROUTER_API_KEY", "GEMINI_API_KEY",
    "LOCAL_LLM_API_KEY", "EMBEDDING_KEY",
    "TAVILY_API_KEY", "BOCHA_API_KEY", "SEARCH_API_KEY",
    "OPENF1_PASSWORD", "BILIBILI_SESSDATA",
    "TR_STUDIO_TOKEN",
)

_lock = threading.Lock()
_secrets: List[str] = []


def register_secret(value: str, label: str = ""):
    """注册一个密钥进入脱敏清单（幂等；过短/空值忽略）"""
    v = (value or "").strip()
    if len(v) < MIN_SECRET_LEN:
        return
    with _lock:
        if v not in _secrets:
            _secrets.append(v)
            # 按长度降序，保证最长匹配优先（前缀相同的 key 先替换长的）
            _secrets.sort(key=len, reverse=True)
    if label:
        logger.info(f"secret_guard 已注册密钥: {label}（{mask_secret(v)}）")


def auto_register_env():
    """把已知敏感环境变量注册进清单（进程启动时调用一次）"""
    n = 0
    for name in _ENV_SECRET_NAMES:
        v = os.getenv(name, "")
        if v and len(v.strip()) >= MIN_SECRET_LEN:
            register_secret(v, name)
            n += 1
    return n


def scrub_text(text: str) -> str:
    """把文本中出现的所有已注册密钥替换为 ***REDACTED***"""
    if not text:
        return text
    with _lock:
        secrets = list(_secrets)
    out = text
    for s in secrets:
        if s in out:
            out = out.replace(s, REDACTED)
            logger.warning("secret_guard 拦截：出站内容包含已注册密钥，已脱敏")
    return out


def mask_secret(value: str) -> str:
    """展示用脱敏：sk-ab...yz（保留前5后2，中间打码）
    短 key 全打码：≤14 字符时前5后2 已暴露 70%（review L5）"""
    v = (value or "").strip()
    if len(v) <= 14:
        return "***"
    return f"{v[:5]}...{v[-2:]}"


class SecretScrubFilter(logging.Filter):
    """日志脱敏过滤器：消息文本与异常堆栈（exc_info）都过 scrub"""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
            scrubbed = scrub_text(msg)
            if scrubbed != msg:
                record.msg = scrubbed
                record.args = ()
            # exc_info 由 Formatter 在 filter 之后渲染，须预渲染脱敏（review M4：
            # 异常 repr 可能携带带凭据的 URL/端点，traceback 原文会绕过过滤器）
            if record.exc_info and not record.exc_text:
                import traceback
                record.exc_text = scrub_text(
                    "".join(traceback.format_exception(*record.exc_info)))
                record.exc_info = None  # 已预渲染，防 Formatter 再渲染未脱敏堆栈
        except Exception:
            pass
        return True


def install_log_filter():
    """给 root logger 的所有 handler 挂脱敏过滤器（两个版本 main 的 setup_logging 调用）"""
    root = logging.getLogger()
    for h in root.handlers:
        h.addFilter(SecretScrubFilter())
