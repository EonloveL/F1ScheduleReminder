# -*- coding: utf-8 -*-
"""B站扫码登录：管理员一次扫码取得 SESSDATA，解锁字幕/元数据接口
（阿里云 IP 被 B站按端点风控 412，登录态可放行，2026-10-24 实证）。

流程：/bili_login（管理员指令）→ passport 生成二维码 → web 容器 /exports 出图
→ 管理员手机 B站 App 扫码 → 后台轮询 poll → 成功后 Cookie 落盘 data/bili_cookie.json。
Cookie 读取优先级：BILIBILI_SESSDATA env > data/bili_cookie.json。
"""
import json
import logging
import os
import time
from typing import Any, Dict, Optional

import requests

logger = logging.getLogger(__name__)

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "Chrome/120.0 Safari/537.36")
PASSPORT = "https://passport.bilibili.com"

_COOKIE_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "data", "bili_cookie.json")


def load_cookie() -> str:
    """读取持久化的 SESSDATA（env 优先，文件兜底；空串表示无登录态）"""
    env = os.getenv("BILIBILI_SESSDATA", "").strip()
    if env:
        return env
    try:
        with open(_COOKIE_PATH, encoding="utf-8") as f:
            return (json.load(f).get("SESSDATA") or "").strip()
    except Exception:
        return ""


def save_cookie(sessdata: str, extra: Dict[str, Any] = None):
    """SESSDATA 原子写落盘（生产端独立保留——已加入打包排除清单）"""
    tmp = _COOKIE_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"SESSDATA": sessdata, "extra": extra or {},
                   "saved": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())},
                  f, ensure_ascii=False)
    os.replace(tmp, _COOKIE_PATH)


def qrcode_generate() -> Optional[Dict[str, str]]:
    """生成登录二维码。Returns {"qrcode_key", "url"} 或 None"""
    try:
        r = requests.get(f"{PASSPORT}/x/passport-login/web/qrcode/generate",
                         headers={"User-Agent": UA}, timeout=10)
        d = r.json()
        if d.get("code") == 0 and d.get("data"):
            return {"qrcode_key": d["data"]["qrcode_key"], "url": d["data"]["url"]}
        logger.warning(f"[BiliLogin] 二维码生成失败: code={d.get('code')} {d.get('message')}")
    except Exception as e:
        logger.warning(f"[BiliLogin] 二维码生成异常: {e}")
    return None


def qrcode_render_png(content: str, out_path: str) -> bool:
    """把二维码内容渲染为 PNG（qrcode+Pillow，matplotlib 已带入 Pillow）"""
    try:
        import qrcode
        img = qrcode.make(content)
        img.save(out_path)
        return True
    except Exception as e:
        logger.warning(f"[BiliLogin] 二维码渲染失败: {e}")
        return False


def qrcode_poll(qrcode_key: str, timeout_sec: int = 180) -> Optional[Dict[str, Any]]:
    """轮询扫码结果。成功返回 {"SESSDATA": ..., "raw": {...}}；超时/失败返回 None"""
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com"})
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        try:
            r = s.get(f"{PASSPORT}/x/passport-login/web/qrcode/poll",
                      params={"qrcode_key": qrcode_key}, timeout=10)
            d = r.json()
            code = ((d.get("data") or {}).get("code"))
            if code == 0:
                # 登录成功：SESSDATA 在 Set-Cookie 里
                sessdata = s.cookies.get("SESSDATA") or ""
                if not sessdata:
                    for c in r.headers.get("Set-Cookie", "").split(","):
                        c = c.strip()
                        if c.startswith("SESSDATA="):
                            sessdata = c.split(";", 1)[0].split("=", 1)[1]
                            break
                if sessdata:
                    return {"SESSDATA": sessdata, "raw": d.get("data")}
                logger.warning("[BiliLogin] 登录成功但未取到 SESSDATA Cookie")
                return None
            if code == 86038:  # 二维码失效
                logger.info("[BiliLogin] 二维码已过期")
                return None
            # 86101 未扫码 / 86090 已扫未确认：继续等
        except Exception as e:
            logger.warning(f"[BiliLogin] 轮询异常: {e}")
        time.sleep(2)
    return None


def validate_cookie(sessdata: str) -> Dict[str, Any]:
    """验证 Cookie 有效性 + 阿里云风控绕过实证（view API 是否放行）"""
    s = requests.Session()
    s.trust_env = False
    s.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com"})
    s.cookies.set("SESSDATA", sessdata, domain=".bilibili.com")
    out = {"login_ok": False, "view_ok": False}
    try:
        nav = s.get("https://api.bilibili.com/x/web-interface/nav", timeout=10).json()
        out["login_ok"] = nav.get("code") == 0
        out["uname"] = ((nav.get("data") or {}).get("uname")) or ""
    except Exception as e:
        out["nav_error"] = str(e)[:100]
    try:
        v = s.get("https://api.bilibili.com/x/web-interface/view",
                  params={"bvid": "BV1Dnnc6gEZE"}, timeout=10)
        out["view_ok"] = v.status_code == 200 and v.json().get("code") == 0
    except Exception as e:
        out["view_error"] = str(e)[:100]
    return out
