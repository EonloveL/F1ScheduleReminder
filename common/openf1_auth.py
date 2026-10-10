"""
OpenF1 OAuth2 共享认证提供器
============================
背景：openf1_api.py 与 stint_analysis.py 各有一份 token 获取逻辑，已漂移
（异常捕获范围/失败冷却不一致，2026-10-10 review 实证）。
本模块统一：token 1h 过期提前 60s 刷新 + **失败冷却 5min**
（token 端点挂掉时不至于每次 API 请求都重发一次 token POST，防限流）。
配置：环境变量 OPENF1_USERNAME / OPENF1_PASSWORD（OpenF1 sponsor 订阅）。
未配置时匿名降级（历史数据可用，现场环节窗口 401 → 各调用方走兜底）。
"""
import logging
import os
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

TOKEN_URL = "https://api.openf1.org/token"
_FAILURE_COOLDOWN = 300  # token 获取失败后的冷却秒数


class OpenF1Auth:
    """OpenF1 Bearer token 提供器（模块级单例使用）"""

    def __init__(self):
        self._username = os.getenv("OPENF1_USERNAME", "")
        self._password = os.getenv("OPENF1_PASSWORD", "")
        self._token: Optional[str] = None
        self._token_expires_at = 0.0
        self._last_failure = 0.0
        self.enabled = bool(self._username and self._password)

    def headers(self) -> dict:
        """取 Bearer 头；未配置/冷却中/获取失败返回空 dict（调用方匿名降级）"""
        if not self.enabled:
            return {}
        if self._token and time.time() < self._token_expires_at - 60:
            return {"Authorization": f"Bearer {self._token}"}
        if time.time() - self._last_failure < _FAILURE_COOLDOWN:
            return {}
        try:
            resp = requests.post(
                TOKEN_URL,
                data={"username": self._username, "password": self._password},
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=15,
                proxies={"http": None, "https": None},  # 与业务 session 的 trust_env=False 口径一致
            )
            resp.raise_for_status()
            data = resp.json()
            token = data.get("access_token")
            expires_in = int(data.get("expires_in") or 3600)
            if token:
                self._token = token
                self._token_expires_at = time.time() + expires_in
                self._last_failure = 0.0
                logger.info("[OpenF1Auth] access_token 获取成功")
                return {"Authorization": f"Bearer {token}"}
        except Exception as e:
            self._last_failure = time.time()
            logger.warning(f"[OpenF1Auth] token 获取失败（{_FAILURE_COOLDOWN}s 冷却）: {e}")
        return {}


_auth: Optional[OpenF1Auth] = None


def get_openf1_auth() -> OpenF1Auth:
    """模块级单例"""
    global _auth
    if _auth is None:
        _auth = OpenF1Auth()
        if _auth.enabled:
            logger.info("[OpenF1Auth] OAuth2 认证已配置")
        else:
            logger.info("[OpenF1Auth] 未配置 OPENF1_USERNAME/PASSWORD，匿名模式"
                        "（现场环节窗口 OpenF1 返回 401，走兜底链）")
    return _auth


def openf1_headers() -> dict:
    """便捷入口：取 Bearer 头（未配置/失败返回 {}）"""
    return get_openf1_auth().headers()
