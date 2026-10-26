# -*- coding: utf-8 -*-
"""B站视频摘要数据层：链接解析 / 视频元数据 / 字幕获取 / F1相关性门禁 / ASR兜底。

字幕接口（player/wbi/v2）2024 起需登录态，否则字幕列表恒为空（2026-10-23 实证：
热门榜 50 视频无登录全部 0 字幕轨）——需配置 BILIBILI_SESSDATA 环境变量
（建议专用小号）。无登录态时只能做 F1 门禁判定并提示"字幕不可用"。

无字幕视频兜底：playurl dash 音频下载 + faster-whisper 转写（复用 TR 转写设施），
由 BILI_VIDEO_ASR_ENABLED=true 开启，时长超 BILI_VIDEO_ASR_MAX_MIN（默认20分钟）不做。
"""
import hashlib
import json
import logging
import os
import re
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests

logger = logging.getLogger(__name__)

# Whisper 模型模块级懒加载单例 + 双重检查锁（review 修复：原实现每次 ASR 重建模型，
# small ~1GB 权重多 5-30s 加载；复审M1：无锁并发首触发会双份加载。
# 注：与 TR 转写模型尺寸/实例独立——TR 默认 base 英文、本模块默认 small 自动检测，
# 两者同时启用时会有两份模型常驻内存，资源紧张环境注意）
import threading as _threading

_whisper_model = None
_whisper_load_tried = False
_whisper_lock = _threading.Lock()


def _get_whisper():
    global _whisper_model, _whisper_load_tried
    if _whisper_load_tried:
        return _whisper_model
    with _whisper_lock:
        if _whisper_load_tried:
            return _whisper_model
        try:
            from faster_whisper import WhisperModel
            model_size = os.getenv("WHISPER_MODEL_BILI", "small")
            _whisper_model = WhisperModel(model_size, device="cpu", compute_type="int8")
            logger.info(f"✓ B站ASR faster-whisper 模型加载成功: {model_size}")
        except Exception as e:
            logger.warning(f"⚠️ faster-whisper 不可用，B站ASR兜底禁用: {e}")
            _whisper_model = None
        _whisper_load_tried = True
        return _whisper_model

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "Chrome/120.0 Safari/537.36")
API = "https://api.bilibili.com"

_WBI_MIXIN_TAB = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
                  27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
                  37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
                  22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52]

BV_RE = re.compile(r"(?i)\b(BV[0-9A-Za-z]{10})\b")
AV_RE = re.compile(r"(?i)\bav(\d{5,})\b")
B23_RE = re.compile(r"(?i)(b23\.tv/[0-9A-Za-z]+)")

# F1 强关键词：命中其一即判 F1 相关（中英车手/车队别名与赛道名在运行时并入）
_F1_STRONG = {
    "f1", "formula 1", "formula1", "formula one", "大奖赛", "grand prix",
    "方程式", "f2", "f3", "围场", "杆位", "排位赛", "正赛", "drs", "倍耐力",
    "安全车", "维修区", "领奖台", "发车格", "进站", "undercut", "overcut",
    "海豚跳", "底板", "扩散器", "尾翼", "侧箱", "动力单元", "空力", "气动",
}


class BiliVideoClient:
    """B站视频元数据+字幕客户端（字幕需登录态 Cookie）"""

    def __init__(self, sessdata: str = None, timeout: int = 15):
        self.s = requests.Session()
        self.s.trust_env = False  # 开发机代理 env 会干扰 B站 API（项目历史坑，统一口径）
        self.s.headers.update({"User-Agent": UA, "Referer": "https://www.bilibili.com"})
        self.timeout = timeout
        sd = sessdata if sessdata is not None else self._load_sessdata()
        if sd:
            self.s.cookies.set("SESSDATA", sd, domain=".bilibili.com")
        self._wbi_keys = None  # (img_key, sub_key, fetched_ts)
        self.logged_in = bool(sd)

    @staticmethod
    def _load_sessdata() -> str:
        """Cookie 来源：参数 > BILIBILI_SESSDATA env > data/bili_cookie.json（扫码登录落盘）"""
        env = os.getenv("BILIBILI_SESSDATA", "").strip()
        if env:
            return env
        try:
            from .bili_auth import load_cookie
            return load_cookie()
        except Exception:
            return ""

    # ---------- 链接解析 ----------

    @staticmethod
    def parse_bvid(text: str) -> Optional[str]:
        """从任意文本中提取 BV 号（支持 BV 直链/av 号/b23 短链）"""
        if not text:
            return None
        m = BV_RE.search(text)
        if m:
            return m.group(1)
        m = B23_RE.search(text)
        if m:
            try:
                # GET stream 比 HEAD 稳（个别 CDN/风控对 HEAD 响应不一致）；关代理防开发机 env 干扰
                with requests.get("https://" + m.group(1), headers={"User-Agent": UA},
                                  allow_redirects=True, timeout=10, stream=True,
                                  proxies={"http": None, "https": None}) as r:
                    m2 = BV_RE.search(r.url)
                if m2:
                    return m2.group(1)
            except Exception as e:
                logger.warning(f"[Bili] b23 短链解析失败: {e}")
            return None
        m = AV_RE.search(text)
        if m:
            try:
                r = requests.get(f"{API}/x/web-interface/view", params={"aid": m.group(1)},
                                 headers={"User-Agent": UA, "Referer": "https://www.bilibili.com"},
                                 timeout=10, proxies={"http": None, "https": None}).json()
                return (r.get("data") or {}).get("bvid")
            except Exception as e:
                logger.warning(f"[Bili] av 号解析失败: {e}")
        return None

    # ---------- 元数据 ----------

    def get_video_info(self, bvid: str) -> Optional[Dict[str, Any]]:
        """视频元数据（标题/简介/UP主/分区/时长/分P），门禁判定与展示用。
        API 失败（机房 IP 风控等）时回退抓视频页 HTML 的 __INITIAL_STATE__（2026-10-24
        实证：同一 BV 本地 API 正常、阿里云服务器失败，页面 HTML 仍可取）"""
        try:
            resp = self.s.get(f"{API}/x/web-interface/view", params={"bvid": bvid},
                              timeout=self.timeout)
            r = resp.json()
            d = r.get("data")
            if r.get("code") == 0 and d:
                return self._shape_info(bvid, d)
            logger.warning(f"[Bili] view API 返回异常 {bvid}: http={resp.status_code} "
                           f"code={r.get('code')} msg={r.get('message')}")
        except Exception as e:
            logger.warning(f"[Bili] 视频信息获取失败 {bvid}: {type(e).__name__}: {e}")
        # 回退：视频页 __INITIAL_STATE__
        try:
            html = self.s.get(f"https://www.bilibili.com/video/{bvid}/",
                              timeout=self.timeout).text
            m = re.search(r"__INITIAL_STATE__=(\{.+?\});\(function", html)
            if m:
                state = json.loads(m.group(1))
                vd = state.get("videoData") or {}
                if vd.get("title"):
                    logger.info(f"[Bili] {bvid} 元数据来自页面 __INITIAL_STATE__ 回退")
                    return self._shape_info(bvid, vd)
            logger.warning(f"[Bili] 页面回退也未取到视频数据 {bvid}（HTML {len(html)}B）")
        except Exception as e:
            logger.warning(f"[Bili] 页面回退失败 {bvid}: {type(e).__name__}: {e}")
        # 回退2：pagelist（player 域端点不被风控，2026-10-24 实证；
        # 单P视频 part 即视频标题，desc/owner/tname 缺失）
        try:
            r = self.s.get(f"{API}/x/player/pagelist", params={"bvid": bvid},
                           timeout=self.timeout).json()
            pages = r.get("data") or []
            if r.get("code") == 0 and pages:
                logger.info(f"[Bili] {bvid} 元数据来自 pagelist 回退（无简介/UP主/分区）")
                return {
                    "bvid": bvid,
                    "title": pages[0].get("part", ""),
                    "desc": "",
                    "owner": "",
                    "tname": "",
                    "duration": int(pages[0].get("duration") or 0),
                    "pages": [{"cid": p.get("cid"), "part": p.get("part", "")} for p in pages],
                    "cid": pages[0].get("cid"),
                }
        except Exception as e:
            logger.warning(f"[Bili] pagelist 回退失败 {bvid}: {type(e).__name__}: {e}")
        return None

    @staticmethod
    def _shape_info(bvid: str, d: Dict[str, Any]) -> Dict[str, Any]:
        """统一 view API 与 __INITIAL_STATE__.videoData 的输出结构"""
        return {
            "bvid": bvid,
            "title": d.get("title", ""),
            "desc": d.get("desc", "") or "",
            "owner": (d.get("owner") or {}).get("name", ""),
            "tname": d.get("tname", ""),
            "duration": int(d.get("duration") or 0),
            "pages": [{"cid": p.get("cid"), "part": p.get("part", "")}
                      for p in (d.get("pages") or [])],
            "cid": d.get("cid") or ((d.get("pages") or [{}])[0].get("cid")),
        }

    # ---------- F1 门禁 ----------

    @staticmethod
    def is_f1_related(info: Dict[str, Any]) -> Tuple[bool, str]:
        """确定性关键词门禁（不进 L2 的判定，省 token 防滥用）。
        规则：CJK 词子串匹配即强命中；ASCII 词要求词边界，len>=6 强命中、
        len<6 弱命中（kimi/max 类短别名需另有其他命中佐证，防 "Kimi AI助手" 类误报）；
        纯 ASCII 的内置词豁免短长度（f1/drs 等）。任一强命中或 ≥2 个命中判 F1 相关"""
        hay = " ".join([info.get("title", ""), info.get("desc", ""),
                        info.get("owner", ""), info.get("tname", "")]).lower()
        strong_terms = set(_F1_STRONG)
        weak_terms = set()
        try:
            from .drivers_profile import get_driver_aliases, get_team_aliases, get_known_driver_names
            for src in (get_driver_aliases(), get_team_aliases()):
                for a in src:
                    a = a.lower()
                    if len(a) < 2:
                        continue
                    if a.isascii() and len(a) < 6:
                        weak_terms.add(a)
                    else:
                        strong_terms.add(a)
            # 车手姓名拆分入库（全名+CJK名+姓氏token），防 "Kimi Antonelli" 类
            # 只有短别名+姓氏的场景被拒；ASCII token 同样按 <6 降级为弱词
            for n in get_known_driver_names():
                n = str(n).lower().strip()
                if len(n) < 2:
                    continue
                if n.isascii():
                    for tok in n.split():
                        if len(tok) >= 6:
                            strong_terms.add(tok)
                        elif len(tok) >= 3:
                            weak_terms.add(tok)
                else:
                    strong_terms.add(n)
        except Exception as e:
            logger.debug(f"[Bili] 车手/车队别名加载失败，门禁词库减弱: {e}")
        try:
            from .circuits_manager import CircuitsManager
            for c in CircuitsManager().circuits_data.values():
                for n in (c.get("name", ""), c.get("name_en", "")):
                    if n and len(n) >= 3:
                        strong_terms.add(n.lower())
        except Exception as e:
            logger.debug(f"[Bili] 赛道名录加载失败，门禁词库减弱: {e}")
        strong_hits, weak_hits = [], []
        for k in sorted(strong_terms):
            if k.isascii():
                if len(k) < 4 and k not in ("f1", "f2", "f3", "drs"):
                    continue
                if re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", hay):
                    strong_hits.append(k)
            elif k in hay:
                strong_hits.append(k)
        for k in sorted(weak_terms):
            if re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", hay):
                weak_hits.append(k)
        hits = strong_hits + weak_hits
        is_f1 = bool(strong_hits) or len(hits) >= 2
        return (is_f1, "、".join(hits[:5]))

    # ---------- 字幕 ----------

    def _wbi_signed(self, params: Dict[str, Any]) -> Dict[str, Any]:
        if not self._wbi_keys or time.time() - self._wbi_keys[2] > 3600:
            nav = self.s.get(f"{API}/x/web-interface/nav", timeout=self.timeout).json()
            wbi = (nav.get("data") or {}).get("wbi_img") or {}
            img = wbi.get("img_url", "").rsplit("/", 1)[-1].split(".")[0]
            sub = wbi.get("sub_url", "").rsplit("/", 1)[-1].split(".")[0]
            self._wbi_keys = (img, sub, time.time())
        img, sub, _ = self._wbi_keys
        mixin = "".join((img + sub)[i] for i in _WBI_MIXIN_TAB)[:32]
        params = dict(params, wts=int(time.time()))
        params = {k: "".join(c for c in str(v) if c not in "!'()*")
                  for k, v in sorted(params.items())}
        params["w_rid"] = hashlib.md5((urlencode(params) + mixin).encode()).hexdigest()
        return params

    def get_subtitle(self, bvid: str, cid: int) -> Tuple[Optional[str], str]:
        """获取字幕纯文本。Returns: (text|None, 状态说明)。
        优先中文字幕轨（含 AI 字幕 ai-zh），多轨取第一条中文，无中文取第一条。"""
        try:
            params = self._wbi_signed({"bvid": bvid, "cid": cid})
            p = self.s.get(f"{API}/x/player/wbi/v2", params=params,
                           timeout=self.timeout).json()
            if self.logged_in and p.get("code") == -101:
                # Cookie 过期：告警管理员重新扫码（12h 去重）
                try:
                    from .admin_alert import alert_admin
                    alert_admin("B站登录态失效",
                                "字幕接口返回未登录（-101），SESSDATA 已过期。\n"
                                "请私聊机器人发送 /bili_login 重新扫码",
                                key="bili_cookie_expired")
                except Exception:
                    pass
                return None, "登录态已失效（需管理员重新扫码）"
            subs = (((p.get("data") or {}).get("subtitle") or {}).get("subtitles")) or []
            if not subs:
                return None, ("无字幕轨（未登录或该视频确无字幕）" if not self.logged_in
                              else "该视频无字幕轨")
            zh = [x for x in subs
                  if (x.get("lan") or "").startswith("zh") or "zh" in (x.get("lan") or "")]
            track = (zh or subs)[0]
            url = track.get("subtitle_url", "")
            if url.startswith("//"):
                url = "https:" + url
            body = self.s.get(url, timeout=self.timeout).json()
            lines = [b.get("content", "").strip()
                     for b in (body.get("body") or []) if b.get("content")]
            text = "\n".join(lines)
            if not text:
                return None, "字幕轨内容为空"
            lang_note = "AI字幕" if "ai" in (track.get("lan") or "") else "字幕"
            return text, f"{lang_note}（{track.get('lan_doc') or track.get('lan')}，{len(lines)}行）"
        except Exception as e:
            logger.warning(f"[Bili] 字幕获取失败 {bvid}: {e}")
            return None, f"字幕获取异常: {e}"

    # ---------- ASR 兜底 ----------

    def get_audio_url(self, bvid: str, cid: int) -> Optional[str]:
        """dash 音频流地址（playurl 无需登录可拿低码率音轨）。
        签名 URL 有过期/机房限流风险，取最低码率轨 + 备用 URL 列表存 backup 字段"""
        try:
            params = self._wbi_signed({"bvid": bvid, "cid": cid, "fnval": 16})
            r = self.s.get(f"{API}/x/player/wbi/playurl", params=params,
                           timeout=self.timeout).json()
            audios = (((r.get("data") or {}).get("dash") or {}).get("audio")) or []
            if not audios:
                return None
            best = min(audios, key=lambda a: a.get("bandwidth", 999999))
            url = best.get("baseUrl") or best.get("base_url")
            return url
        except Exception as e:
            logger.warning(f"[Bili] 音频地址获取失败 {bvid}: {e}")
            return None

    def get_audio_url_candidates(self, bvid: str, cid: int) -> List[str]:
        """音频流候选 URL 列表（主 URL + backup_url + 次低码率轨），下载失败逐个重试"""
        try:
            params = self._wbi_signed({"bvid": bvid, "cid": cid, "fnval": 16})
            r = self.s.get(f"{API}/x/player/wbi/playurl", params=params,
                           timeout=self.timeout).json()
            audios = (((r.get("data") or {}).get("dash") or {}).get("audio")) or []
            if not audios:
                return []
            audios.sort(key=lambda a: a.get("bandwidth", 999999))
            urls = []
            for a in audios[:2]:  # 最低两档码率轨
                main = a.get("baseUrl") or a.get("base_url")
                if main:
                    urls.append(main)
                urls.extend(u for u in (a.get("backupUrl") or a.get("backup_url") or []) if u)
            # 去重保序
            seen, out = set(), []
            for u in urls:
                if u not in seen:
                    seen.add(u)
                    out.append(u)
            return out
        except Exception as e:
            logger.warning(f"[Bili] 音频地址获取失败 {bvid}: {e}")
            return []

    def download_audio(self, urls: List[str], max_bytes: int = 200 << 20) -> Optional[str]:
        """按候选列表逐个尝试下载音频到临时文件，返回路径（调用方负责删除）"""
        for url in urls:
            path = None
            try:
                with self.s.get(url, timeout=60, stream=True) as r:
                    r.raise_for_status()
                    fd, path = tempfile.mkstemp(suffix=".m4s")
                    total = 0
                    with os.fdopen(fd, "wb") as f:
                        for chunk in r.iter_content(1 << 16):
                            total += len(chunk)
                            if total > max_bytes:
                                raise ValueError("音频超过大小上限")
                            f.write(chunk)
                if total < 10000:  # 明显残缺
                    raise ValueError(f"音频过小（{total}B），疑似错误页")
                return path
            except Exception as e:
                logger.warning(f"[Bili] 音频下载失败（换候选）: {type(e).__name__}: {e}")
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass
        return None

    def transcribe_audio(self, audio_urls, language: str = None) -> Optional[str]:
        """下载音频并 faster-whisper 转写（模块级模型单例懒加载，CPU int8）。
        audio_urls 接受单个 URL 或候选列表（下载失败自动换候选）。
        language 默认 None=自动检测（英文技术分析视频同样可用，BILI_VIDEO_ASR_LANG 可锁定）"""
        model = _get_whisper()
        if not model:
            return None
        lang = os.getenv("BILI_VIDEO_ASR_LANG", "").strip() or language
        urls = [audio_urls] if isinstance(audio_urls, str) else list(audio_urls)
        path = self.download_audio(urls)
        if not path:
            return None
        try:
            segments, _ = model.transcribe(path, language=lang)
            text = "\n".join(seg.text.strip() for seg in segments if seg.text.strip())
            return text or None
        except Exception as e:
            logger.warning(f"[Bili] ASR 转写失败: {e}")
            return None
        finally:
            if path and os.path.exists(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
