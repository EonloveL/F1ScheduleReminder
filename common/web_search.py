"""
F1赛程提醒机器人 - 客户端联网搜索（为无内建联网能力的供应商补充 web_search）

背景：Kimi 有服务端内建 $web_search，DeepSeek 官方 API 无内建联网
（实测 builtin_function 返回 400 unknown variant，2026-09）。
本模块在客户端执行搜索，把结果作为普通 function tool 回给模型。

支持引擎（环境变量配置，均未配置则自动禁用）：
- Tavily（全球，免费 1000 次/月）：TAVILY_API_KEY  https://tavily.com
- 博查 Bocha（国内，注册有免费额度）：BOCHA_API_KEY  https://bochaai.com
- 通用覆盖：SEARCH_PROVIDER=tavily|bocha + SEARCH_API_KEY=...
"""

import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional
import requests

logger = logging.getLogger(__name__)

TAVILY_URL = "https://api.tavily.com/search"
BOCHA_URL = "https://api.bochaai.com/v1/web-search"

# 提供给模型的 function tool schema（DeepSeek 等无内建联网的供应商用）
WEB_SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": "联网搜索互联网上的最新信息（新闻/规则/规格参数/背景资料）。query为搜索关键词",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string", "description": "搜索关键词"}},
                       "required": ["query"]},
    },
}


class WebSearchClient:
    """客户端联网搜索（单例式轻量封装）"""

    def __init__(self):
        self.provider = os.getenv("SEARCH_PROVIDER", "").strip().lower()
        self.api_key = os.getenv("SEARCH_API_KEY", "").strip()
        # 引擎专属 key 优先
        if not self.api_key:
            tavily_key = os.getenv("TAVILY_API_KEY", "").strip()
            bocha_key = os.getenv("BOCHA_API_KEY", "").strip()
            if tavily_key:
                self.provider, self.api_key = "tavily", tavily_key
            elif bocha_key:
                self.provider, self.api_key = "bocha", bocha_key
        if self.provider and not self.api_key:
            self.provider = ""  # 有引擎无 key 视为未配置
        if not self.provider and self.api_key:
            self.provider = "tavily"  # 只给通用 key 默认 tavily
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "F1-Reminder-Bot/1.0"})
        if self.enabled:
            logger.info(f"✓ 客户端联网搜索已启用: {self.provider}")

    @property
    def enabled(self) -> bool:
        return bool(self.provider and self.api_key)

    def search(self, query: str, max_results: int = 5) -> List[Dict[str, str]]:
        """搜索 -> [{"title", "url", "snippet"}]，失败返回空列表"""
        if not self.enabled or not query:
            return []
        try:
            if self.provider == "bocha":
                resp = self.session.post(
                    BOCHA_URL,
                    headers={"Authorization": f"Bearer {self.api_key}",
                             "Content-Type": "application/json"},
                    json={"query": query, "count": max_results, "summary": True},
                    timeout=20)
                resp.raise_for_status()
                pages = ((resp.json().get("data") or {}).get("webPages") or {}).get("value") or []
                return [{"title": p.get("name", ""), "url": p.get("url", ""),
                         "snippet": p.get("summary") or p.get("snippet", "")} for p in pages]
            # 默认 tavily
            resp = self.session.post(
                TAVILY_URL,
                json={"api_key": self.api_key, "query": query,
                      "max_results": max_results, "search_depth": "basic"},
                timeout=20)
            resp.raise_for_status()
            return [{"title": r.get("title", ""), "url": r.get("url", ""),
                     "snippet": r.get("content", "")} for r in resp.json().get("results", [])]
        except Exception as e:
            logger.warning(f"[WebSearch] {self.provider} 搜索失败: {e}")
            return []

    def search_text(self, query: str, max_results: int = 5) -> str:
        """搜索并格式化为注入模型的文本"""
        results = self.search(query, max_results)
        if not results:
            return "（联网搜索无结果或搜索服务不可用）"
        lines = [f"【联网搜索结果：{query}】"]
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']}\n   {r['snippet'][:300]}\n   来源: {r['url']}")
        return "\n".join(lines)


_shared: Optional[WebSearchClient] = None
_shared_lock = threading.Lock()


def get_web_searcher() -> WebSearchClient:
    """进程级共享实例（双重检查锁，防并发重复构造）"""
    global _shared
    if _shared is None:
        with _shared_lock:
            if _shared is None:
                _shared = WebSearchClient()
    return _shared


def web_search_handler(query: str = "", **_kwargs) -> str:
    """供 LLMAssistant 工具循环直接调用的 handler 形态"""
    return get_web_searcher().search_text(query)
