"""图片视觉识别 mixin + 群内识图摘要共享"""

import logging
import os
from typing import Optional, Dict, Any, List

import requests

from .prompts import _load_prompt
from .tracing import get_qid, set_qid, new_qid

logger = logging.getLogger(__name__)


class VisionMixin:
    """视觉识别：ask_vision / _ask_vision_impl / _image_to_data_url
    + 群内识图摘要共享（save_group_vision / get_group_vision_context / _inject_group_vision）"""

    _DEFAULT_VISION_SYSTEM = (
        "你是F1专家，负责识别用户发来的图片。"
        "如果是F1赛程表/成绩图/积分榜/新闻截图，提取关键信息（时间统一换算为北京时间，日期保持原样）；"
        "如果是官方赛程表，逐项列出各环节名称和时间，便于用户与机器人推送核对。"
        "用简体中文回答，条理清晰；图片与F1无关时简要描述并礼貌引导回F1话题。"
    )
    VISION_SYSTEM = _load_prompt("vision", _DEFAULT_VISION_SYSTEM)

    # ==================== 群内识图摘要共享 ====================
    # 场景：群友A发图识别后，群友B追问"这张图里排位赛几点"。
    # B没有A的会话历史，这里按群共享最近一次识图结果（15分钟有效），
    # 仅当B的问题含图片指代词且B不是识图者本人时注入，避免污染无关提问

    VISION_GROUP_TTL = 15 * 60
    VISION_REF_TRIGGERS = (
        "这图", "这张图", "这个图", "图中", "图上", "图里",
        "这表", "这张表", "这个表", "表里", "表格中",
        "图片里", "截图里", "那张图", "那个表", "刚发的图", "发的图",
    )

    def save_group_vision(self, group_openid: str, user_id: str, question: str, answer: str):
        """保存群内最近一次识图结果"""
        if not group_openid:
            return
        import time as _t
        with self._conv_lock:
            self._group_vision[group_openid] = {
                "user_id": user_id, "question": question,
                "answer": answer, "ts": _t.time(),
            }

    def get_group_vision_context(self, group_openid: str, user_id: str, question: str) -> Optional[str]:
        """取群内近期识图摘要（仅指代词触发 + 非识图者本人）"""
        if not group_openid:
            return None
        if not any(t in question for t in self.VISION_REF_TRIGGERS):
            return None
        import time as _t
        with self._conv_lock:
            ctx = self._group_vision.get(group_openid)
        if not ctx:
            return None
        if _t.time() - ctx.get("ts", 0) > self.VISION_GROUP_TTL:
            return None
        if ctx.get("user_id") == user_id:
            return None
        return (f"【群内近期图片识别结果（群友问的是: {ctx['question'][:40]}）】\n"
                f"{ctx['answer'][:800]}\n"
                f"（该用户的问题可能指向这张图片，请结合上述识图结果回答）")

    def _inject_group_vision(self, system: str, group_openid: str,
                             user_id: str, question: str) -> str:
        """群内识图摘要注入（分层/单调用两路径共用）"""
        if not group_openid:
            return system
        group_vision = self.get_group_vision_context(group_openid, user_id, question)
        if group_vision:
            system += "\n\n" + group_vision
            logger.info(f"注入群内识图摘要 (群 {group_openid[:8]}..., 用户 {user_id[:8]}...)")
        return system

    @staticmethod
    def _image_to_data_url(url: str, max_bytes: int = 10 * 1024 * 1024) -> Optional[str]:
        """下载图片转为base64 data URL（QQ图床URL可能拒绝LLM服务端直接拉取），失败返回None"""
        import base64
        try:
            resp = requests.get(url, timeout=20,
                                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                                proxies={"http": None, "https": None})
            if resp.status_code != 200:
                logger.warning(f"图片下载失败: HTTP {resp.status_code} - {url[:80]}")
                return None
            content = resp.content
            if len(content) > max_bytes:
                logger.warning(f"图片过大({len(content) // 1024}KB)，跳过: {url[:60]}")
                return None
            mime = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
            if not mime.startswith("image/"):
                mime = "image/jpeg"
            return f"data:{mime};base64," + base64.b64encode(content).decode()
        except Exception as e:
            logger.warning(f"图片下载异常: {e} - {url[:80]}")
            return None

    def ask_vision(self, question: str, image_urls: List[str], max_len: int = 1500,
                   user_id: str = None, group_openid: str = None,
                   qid: str = None) -> Optional[str]:
        """视觉识别入口（含 trace ID 生命周期管理），实现见 _ask_vision_impl"""
        prev = get_qid()
        set_qid(qid or prev or new_qid())
        try:
            return self._ask_vision_impl(question, image_urls, max_len, user_id, group_openid)
        finally:
            set_qid(prev)

    def _ask_vision_impl(self, question: str, image_urls: List[str], max_len: int = 1500,
                   user_id: str = None, group_openid: str = None) -> Optional[str]:
        """
        Kimi视觉识别：解读图片内容（官方赛程图/成绩截图等）
        图片先下载转base64 data URL上送，规避QQ图床防盗链；下载失败回退原始URL
        附带最近文字对话历史（覆盖"先发文字后发图"场景）；问答文本写入会话记忆（不含图片）

        Args:
            question: 用户随图问题（可为空，调用方给默认提示）
            image_urls: 图片URL列表（最多取前4张）
            max_len: 回答最大长度
            user_id: 用户 openid（提供时启用历史上下文与记忆）

        Returns:
            识别结果文本，失败返回None
        """
        # 视觉层：默认 kimi（LLM_VISION_MODEL env），管理员可通过 /model set vision 覆盖
        # （供应商需支持图片输入，/model 指令侧负责校验提示）
        v_provider, v_model = self._resolve_layer("vision", "kimi")
        if v_provider not in self.available:
            logger.warning(f"图片识别供应商 {v_provider} 未配置")
            return None
        urls = [u for u in (image_urls or []) if u][:4]
        if not urls:
            return None

        cfg = self.available.get(v_provider)  # 热重载判空
        if not cfg:
            logger.warning(f"识图供应商[{v_provider}]运行时已移除")
            return None
        vision_model = v_model or os.getenv("LLM_VISION_MODEL", "kimi-k2.6")
        content = [{"type": "text", "text": question}]
        for u in urls:
            content.append({"type": "image_url",
                            "image_url": {"url": self._image_to_data_url(u) or u}})

        history = self._get_history(user_id, question)[-4:] if user_id else []
        payload = {
            "model": vision_model,
            "messages": ([{"role": "system", "content": self.VISION_SYSTEM}]
                         + history
                         + [{"role": "user", "content": content}]),
            # 不传temperature：kimi-k2.x视觉模型强制temperature=1，显式传0.3会400
        }
        try:
            response = requests.post(
                cfg["base_url"],
                headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
                json=payload, timeout=(int(os.getenv("LLM_CONNECT_TIMEOUT", "10")), self.timeout),
                proxies={"http": None, "https": None}
            )
        except requests.RequestException as e:
            logger.error(f"图片识别请求异常: {e}")
            return None
        if response.status_code != 200:
            logger.error(f"图片识别请求失败: HTTP {response.status_code} - {response.text[:300]}")
            return None
        try:
            answer = response.json()["choices"][0]["message"].get("content")
        except (ValueError, KeyError, IndexError) as e:
            logger.error(f"图片识别响应解析失败: {e}")
            return None
        if answer and len(answer) > max_len:
            answer = answer[:max_len] + "…"
        if answer and user_id:
            self._append_turn(user_id, question, answer)
        if answer:
            self.save_group_vision(group_openid, user_id, question, answer)
        return answer
