"""
F1赛程提醒机器人 - F1新闻速递

数据源：F1Cosmos 新闻聚合（https://api.f1cosmos.com/news，与其官网
/dashboard/news 页同源：Speedcafe/Motorsport/Autosport 等英文媒体聚合）

推送策略：每日 8:00 / 19:00（本地时区）各推一次，内容为
"上次推送至本次"周期内发布的新闻，经 LLM 精简翻译为中文卡片。

状态文件 data/news_state.json：{"last_push_ts": <unix>}（首次运行以24h前为界）
"""

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.f1cosmos.com"
DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
STATE_FILE = os.path.join(DATA_DIR, "news_state.json")

MAX_ITEMS = 10          # 单次推送最多条目
DM_POOL = 20            # 翻译候选池（群卡片取前10，DM 从池内按偏好过滤，保证 DM 条目也有译文）
MAX_PAGES = 5           # 每次最多翻页（20条/页）
FIRST_RUN_LOOKBACK_H = 24  # 无状态文件时的回看窗口
TOPIC_OVERLAP = 0.5     # 标题 token 重叠系数阈值（|A∩B|/min(|A|,|B|)，>= 判同主题）
TOPIC_MEMORY_H = 72     # 跨批次主题记忆时长（小时）

_NEWS_DIGEST_SYSTEM = (
    "你是F1新闻编辑。输入是英文F1新闻列表（JSON数组，含 i/title/summary/source）。"
    "为每条输出：title_cn（中文标题，不超过20字）、summary_cn（中文一句话摘要，不超过50字，"
    "保留车手/车队标准译名）。输出为JSON数组 [{\"i\": 序号, \"title_cn\": \"...\", \"summary_cn\": \"...\"}]，"
    "只输出JSON，不要任何解释或前后缀。"
)


class F1NewsCollector:
    """F1新闻采集器（F1Cosmos聚合源）"""

    MAX_ITEMS = MAX_ITEMS  # 类属性别名（scheduler 以 collector.MAX_ITEMS 访问）
    DM_POOL = DM_POOL

    def __init__(self, state_file: str = None):
        self.state_file = state_file or STATE_FILE
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "F1-Reminder-Bot/1.0"})

    # ---------- 状态 ----------

    def _load_state(self) -> Dict[str, Any]:
        try:
            with open(self.state_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self, state: Dict[str, Any]):
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False)
            os.replace(tmp, self.state_file)
        except Exception as e:
            logger.warning(f"新闻状态保存失败: {e}")

    def last_push_ts(self) -> float:
        """上次推送时间戳；首次运行回看 FIRST_RUN_LOOKBACK_H 小时"""
        ts = self._load_state().get("last_push_ts")
        if isinstance(ts, (int, float)) and ts > 0:
            return float(ts)
        return time.time() - FIRST_RUN_LOOKBACK_H * 3600

    def mark_pushed(self, ts: float = None, items: List[Dict[str, Any]] = None):
        """推进推送水位 + 记录本批主题指纹（跨批降权用）"""
        ts = ts or time.time()
        state = self._load_state()
        state["last_push_ts"] = ts
        if items:
            topics = [t for t in state.get("topics", [])
                      if ts - t[0] < TOPIC_MEMORY_H * 3600]
            for it in items:
                key = self._topic_key(it.get("title_cn") or it.get("title", ""))
                if key:
                    topics.append([state["last_push_ts"], sorted(key)])
            state["topics"] = topics[-150:]  # 容量上限
        self._save_state(state)

    # ---------- 采集 ----------

    def fetch_since(self, since_ts: float) -> List[Dict[str, Any]]:
        """拉取 since_ts 之后发布的新闻（按发布时间倒序翻页，直到越界）"""
        out: List[Dict[str, Any]] = []
        try:
            for page in range(1, MAX_PAGES + 1):
                resp = self.session.get(f"{BASE_URL}/news", params={"page": page},
                                        timeout=15, proxies={"http": None, "https": None})
                if resp.status_code != 200:
                    logger.warning(f"新闻源请求失败: HTTP {resp.status_code}")
                    break
                data = resp.json()
                items = data.get("data") or []
                if not items:
                    break
                for it in items:
                    ts = self._parse_ts(it.get("published_at"))
                    if ts and ts > since_ts:
                        out.append({
                            "title": it.get("title", ""),
                            "summary": it.get("summary", ""),
                            "url": it.get("link_url", ""),
                            "source": (it.get("news_source") or {}).get("name", ""),
                            "ts": ts,
                        })
                    elif ts:
                        # 列表按时间倒序，越界即终止
                        return out
                if not data.get("hasNextPage"):
                    break
        except Exception as e:
            logger.warning(f"新闻采集失败: {e}")
        return out

    @staticmethod
    def _parse_ts(s: str) -> Optional[float]:
        if not s:
            return None
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError):
            return None

    # ---------- 同主题去重/降权 ----------

    _STOPWORDS = frozenset(
        "the a an of at in on to and for with by from after before over under f1 formula "
        "grand prix gp race racing report news podcast video watch review preview "
        "what who why how will would could should says said new latest".split())

    @classmethod
    def _topic_key(cls, title: str) -> frozenset:
        """标题 -> 主题指纹（实词 token 集合，去停用词/标点/大小写）"""
        import re
        tokens = re.findall(r"[a-z0-9]+|[一-鿿]+", (title or "").lower())
        return frozenset(t for t in tokens if t not in cls._STOPWORDS and len(t) > 1)

    @staticmethod
    def _topic_sim(a: frozenset, b: frozenset) -> float:
        """重叠系数 |A∩B|/min(|A|,|B|)：短标题下比 Jaccard 稳健"""
        if not a or not b:
            return 0.0
        return len(a & b) / min(len(a), len(b))

    def filter_topics(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """批内同主题去重 + 跨批次降权：
        - 批内同主题（标题指纹相似）只保留最新一条，其余丢弃
        - 与上批推送同主题的标 follow_up=True 排末尾（降权而非丢弃）
        返回重排后的列表（新主题在前，follow_up 在后）"""
        state = self._load_state()
        now = time.time()
        recent = [set(t[1]) for t in state.get("topics", [])
                  if now - t[0] < TOPIC_MEMORY_H * 3600]
        seen: List[frozenset] = []
        fresh, followups, dropped = [], [], 0
        for it in items:
            key = self._topic_key(it.get("title_cn") or it.get("title", ""))
            if not key:
                fresh.append(it)
                continue
            if any(self._topic_sim(key, s) >= TOPIC_OVERLAP for s in seen):
                dropped += 1
                continue
            seen.append(key)
            if any(self._topic_sim(key, r) >= TOPIC_OVERLAP for r in recent):
                it["follow_up"] = True
                followups.append(it)
            else:
                fresh.append(it)
        if dropped:
            logger.info(f"新闻批内同主题去重: 丢弃 {dropped} 条")
        if followups:
            logger.info(f"新闻跨批同主题降权: {len(followups)} 条标记为后续报道")
        return fresh + followups

    _DIGEST_BATCH = 5  # 每次调用翻译条数（防单次输出过长被 max_tokens 截断）

    def digest_with_llm(self, items: List[Dict[str, Any]], llm) -> List[Dict[str, Any]]:
        """LLM 分批精简翻译；未覆盖条目补调一次，最终仍缺保持英文原标题。

        修复：旧版单次调用翻译全部条目，输出截断/条数不足时未翻译条目静默
        保持英文。现拆批 5 条/次 + 缺口重试一轮 + 条数不符记日志。
        """
        if not llm or not getattr(llm, "enabled", False) or not items:
            return items

        def _call(batch: List[Dict[str, Any]], index_list: List[int]) -> set:
            """翻译一批（batch[j] 的全局索引为 index_list[j]），返回成功覆盖的索引集合"""
            payload = [{"i": index_list[j], "title": it["title"], "summary": it["summary"],
                        "source": it["source"]} for j, it in enumerate(batch)]
            raw = llm.chat_simple(_NEWS_DIGEST_SYSTEM,
                                  json.dumps(payload, ensure_ascii=False), timeout=60)
            if not raw:
                return set()
            try:
                start, end = raw.find("["), raw.rfind("]")
                arr = json.loads(raw[start:end + 1]) if start >= 0 and end > start else []
            except Exception as e:
                logger.warning(f"新闻摘要JSON解析失败: {e}")
                return set()
            if len(arr) != len(batch):
                logger.warning(f"新闻翻译条数不符: 输入{len(batch)} 返回{len(arr)}，缺口进入重试")
            idx_set = set(index_list)
            covered = set()
            for d in arr:
                i = d.get("i")
                if isinstance(i, int) and i in idx_set:
                    if d.get("title_cn"):
                        items[i]["title_cn"] = str(d["title_cn"]).strip()
                    if d.get("summary_cn"):
                        items[i]["summary_cn"] = str(d["summary_cn"]).strip()
                    covered.add(i)
            return covered

        covered_all: set = set()
        # 第一轮：分批翻译
        for base in range(0, len(items), self._DIGEST_BATCH):
            batch = items[base:base + self._DIGEST_BATCH]
            covered_all |= _call(batch, list(range(base, base + len(batch))))

        # 第二轮：缺口合并补调一次
        missing = [i for i in range(len(items)) if i not in covered_all]
        if missing:
            logger.info(f"新闻翻译缺口重试: {len(missing)} 条")
            covered_all |= _call([items[i] for i in missing], missing)

        still_missing = len(items) - len(covered_all)
        logger.info(f"✓ 新闻精简翻译完成: {len(covered_all)}/{len(items)} 条"
                    + (f"（{still_missing} 条保持英文）" if still_missing else ""))
        return items

    # ---------- 成卡 ----------

    def build_digest(self, items: List[Dict[str, Any]], llm=None,
                     period_from: float = 0, period_to: float = 0) -> tuple:
        """生成 (纯文本, Markdown) 新闻卡片。
        约定：调用方先完成 filter_topics + digest_with_llm（选中集翻译），
        此处只渲染；llm 参数仅为兼容旧调用（传了会先翻译）。"""
        if llm is not None:
            items = self.digest_with_llm(items[:MAX_ITEMS], llm)
        fmt = "%m-%d %H:%M"
        p_from = datetime.fromtimestamp(period_from).strftime(fmt)
        p_to = datetime.fromtimestamp(period_to).strftime(fmt)
        title = f"📰 F1 新闻速递（{p_from} ~ {p_to}）"

        text = f"{title}\n\n"
        md = f"## {title}\r\r"
        for it in items:
            follow = "🔁 " if it.get("follow_up") else ""
            t = it.get("title_cn") or it["title"]
            s = it.get("summary_cn") or it.get("summary", "")
            src, url = it.get("source", ""), it.get("url", "")
            text += f"◆ {follow}{t}\n{s}\n{src} {url}\n\n"
            md += f"**{follow}{t}**\r{s}\r"
            if url:
                md += f"[{src or '原文'}]({url})\r"
            md += "\r"
        if any(it.get("follow_up") for it in items):
            md += "\r🔁 为近期已推送主题的后续报道"
            text += "🔁 = 近期已推送主题的后续报道\n"
        md += "\r> 来源：F1Cosmos 新闻聚合 · LLM 精简翻译"
        text += "来源：F1Cosmos 新闻聚合"
        return text, md

    # ---------- 偏好定向匹配 ----------

    @staticmethod
    def _keywords_for_pref(pref: Dict[str, Any]) -> List[str]:
        """由用户偏好生成匹配关键词集（多关注对象：drivers/teams 列表，兼容旧单值键）"""
        from .drivers_profile import get_drivers, get_teams
        from .user_prefs import UserPrefsStore
        kws: List[str] = []
        d_names = UserPrefsStore.pref_driver_names(pref)
        for did in UserPrefsStore.pref_drivers(pref):
            d = get_drivers().get(did, {})
            for v in [d.get("name_en"), d.get("name_cn"), d.get("last_name"),
                      d_names.get(did)]:
                if v:
                    kws.append(str(v))
            kws.extend(d.get("aliases") or [])
        t_names = UserPrefsStore.pref_team_names(pref)
        for tid in UserPrefsStore.pref_teams(pref):
            t = get_teams().get(tid, {})
            for v in [t.get("name_en"), t_names.get(tid)]:
                if v:
                    kws.append(str(v))
            kws.extend(t.get("aliases") or [])
        # 去重、去短词（<3字符易误配，如"RB"）
        seen, out = set(), []
        for k in kws:
            k = k.strip()
            kl = k.lower()
            if len(k) >= 3 and kl not in seen:
                seen.add(kl)
                out.append(k)
        return out

    def match_items_for_pref(self, items: List[Dict[str, Any]],
                             pref: Dict[str, Any]) -> List[Dict[str, Any]]:
        """按用户偏好过滤新闻：仅匹配标题（原文+译文）。

        只匹配标题不匹配摘要：标题命中≈"关于"该车手/车队；摘要命中往往只是
        "提到"（如迈凯伦主题新闻摘要里提一句 Verstappen），会造成误推。
        """
        kws = self._keywords_for_pref(pref)
        if not kws:
            return []
        matched = []
        for it in items:
            hay = " ".join([it.get("title", ""), it.get("title_cn", "")]).lower()
            if any(k.lower() in hay for k in kws):
                matched.append(it)
        return matched

    def build_dm_digest(self, items: List[Dict[str, Any]], pref: Dict[str, Any],
                        period_from: float, period_to: float) -> tuple:
        """个性化 DM 新闻卡片（条目已由 match_items_for_pref 过滤+翻译）"""
        from .user_prefs import UserPrefsStore
        fmt = "%m-%d %H:%M"
        p_from = datetime.fromtimestamp(period_from).strftime(fmt)
        p_to = datetime.fromtimestamp(period_to).strftime(fmt)
        names = (list(UserPrefsStore.pref_driver_names(pref).values())
                 + list(UserPrefsStore.pref_team_names(pref).values()))
        who = "/".join(names[:3]) if names else "你的关注"
        title = f"📰 {who} 相关 F1 新闻（{p_from} ~ {p_to}）"
        text = f"{title}\n\n"
        md = f"## {title}\r\r"
        for it in items:
            follow = "🔁 " if it.get("follow_up") else ""
            t = it.get("title_cn") or it["title"]
            s = it.get("summary_cn") or it.get("summary", "")
            src, url = it.get("source", ""), it.get("url", "")
            text += f"◆ {follow}{t}\n{s}\n{src} {url}\n\n"
            md += f"**{follow}{t}**\r{s}\r"
            if url:
                md += f"[{src or '原文'}]({url})\r"
            md += "\r"
        md += "\r> 发送 /news off 可关闭定向新闻推送"
        text += "\n发送 /news off 可关闭定向新闻推送"
        return text, md
