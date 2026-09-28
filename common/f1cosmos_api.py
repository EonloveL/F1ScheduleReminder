"""
F1 COSMOS 数据源模块
提供 F1 升级件 / 动力单元部件配额 / 事故维修成本 数据
API: https://api.f1cosmos.com/dashboard/v2（无鉴权）
文档站: https://f1cosmos.com

数据端点:
- 升级件:  GET /dashboard/v2/updates?season=YYYY
- PU 配额: GET /dashboard/v2/elements?season=YYYY
- 维修成本: GET /dashboard/destructors?year=YYYY  (后端暂未上线, 404 时返回空)
"""

import json
import logging
import os
import re
import unicodedata
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple

import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://api.f1cosmos.com"

# 车队中文别名 -> F1Cosmos broadcast_name（2026 赛季：索伯已更名为奥迪）
# 值为列表时表示历史曾用名候选，按赛季数据实际存在的名称匹配：
# 如 "索伯" 在 2026 赛季匹配 Audi，在 2024/2025 赛季匹配 Sauber
TEAM_ALIASES_CN = {
    "红牛": "Red Bull Racing",
    "小红牛": ["Racing Bulls", "RB", "AlphaTauri", "Toro Rosso"],
    "二牛": ["Racing Bulls", "RB", "AlphaTauri", "Toro Rosso"],
    "红牛二队": ["Racing Bulls", "RB", "AlphaTauri", "Toro Rosso"],
    "梅赛德斯": "Mercedes", "奔驰": "Mercedes", "梅奔": "Mercedes",
    "法拉利": "Ferrari", "跃马": "Ferrari",
    "迈凯伦": "McLaren", "迈凯轮": "McLaren",
    "阿斯顿马丁": "Aston Martin", "马丁": "Aston Martin",
    "阿尔派": "Alpine", "alpine": "Alpine", "雷诺": ["Alpine", "Renault"],
    "威廉姆斯": "Williams",
    "哈斯": "Haas F1 Team",
    "奥迪": ["Audi", "Sauber"], "索伯": ["Audi", "Sauber"], "索博": ["Audi", "Sauber"],
    "阿尔法罗密欧": ["Alfa Romeo", "Sauber"], "阿罗": ["Alfa Romeo", "Sauber"],
    "赛点": ["Racing Point", "Aston Martin"], "印度力量": "Force India",
    "凯迪拉克": "Cadillac",
}

# 车手中文别名 -> F1Cosmos last_name
# 数据源：data/drivers_profile.json（common/drivers_profile.py），模块加载时快照
from .drivers_profile import get_driver_lastname_aliases as _gdla
DRIVER_ALIASES_CN = _gdla()
del _gdla

# 动力单元各部件赛季上限：外置 data/pu_limits.json 按赛季维护（FIA 规则每年可能调整），
# 文件缺失/无对应赛季键时回退内置默认值；空 dict 则不提示上限
_PU_LIMITS_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "data", "pu_limits.json")
_PU_LIMITS_DEFAULT: Dict[str, Dict[str, int]] = {
    # 2026 新规取消 MGU-H；额度含 1 个 bonus 奖励部件（来源：F1.com 2026-06-27）
    "2026": {"ICE": 4, "TC": 4, "MGU-K": 3, "ES": 3, "PU-CE": 3, "EXH": 4, "PU-ANC": 6},
    "2027": {"ICE": 3, "TC": 3, "MGU-K": 2, "ES": 2, "PU-CE": 2, "EXH": 3},
}
_pu_limits_cache: Dict[str, Any] = {"mtime": 0, "data": None}


def get_pu_limits(season: int = None) -> Dict[str, int]:
    """读取指定赛季的部件上限（data/pu_limits.json 优先，内置默认兜底）；无配置返回空 dict"""
    try:
        mtime = os.path.getmtime(_PU_LIMITS_FILE)
        if _pu_limits_cache["data"] is None or mtime != _pu_limits_cache["mtime"]:
            with open(_PU_LIMITS_FILE, "r", encoding="utf-8") as f:
                _pu_limits_cache["data"] = json.load(f)
            _pu_limits_cache["mtime"] = mtime
        data = _pu_limits_cache["data"]
    except Exception as e:
        logger.warning(f"读取 pu_limits.json 失败，使用内置默认值: {e}")
        data = _PU_LIMITS_DEFAULT
    limits = data.get(str(season)) if isinstance(data, dict) else None
    return dict(limits) if isinstance(limits, dict) else {}


def _normalize(text: str) -> str:
    return (text or "").strip().lower().replace(" ", "").replace("-", "")


def _norm_gp(text: str) -> str:
    """归一化地名/赛事名（去重音、去空格/连字符，小写）"""
    if not text:
        return ""
    s = unicodedata.normalize("NFKD", str(text))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return s.lower().replace(" ", "").replace("-", "").replace("_", "")


def _fia_url_matches_event(url: str, race_name: str = "", city: str = "") -> bool:
    """FIA 文档链接与分站一致性校验：URL 中不含该分站标识时视为错链（隐藏不展示）。
    历史赛季旧命名（"2024 Italian Grand Prix - ..."）与新命名（2026_italian_grand_prix_...）都按归一化匹配"""
    if not url:
        return False
    u = _norm_gp(url)
    for t in (_norm_gp(race_name), _norm_gp(city)):
        if t and t in u:
            return True
        # 去掉 grandprix 后缀再试（如 italian）
        if t.endswith("grandprix") and t[:-9] and t[:-9] in u:
            return True
    return False


# 车队归一化：FIA申报文档中同一车队在不同分站/年份的标题写法不同
# （如 "KICK F1 Team Sauber" / "STAKE F1 TEAM KICK SAUBER" / "RBR RBPT"(2023)），
# 按关键词归为统一身份用于跨年/跨站过滤；顺序敏感（red_bull 先于 racing_bulls 匹配）
TEAM_CANONICAL_PATTERNS = [
    ("red_bull", r"red\s*bull|\bRBR\b|\bRBPT\b"),
    ("racing_bulls", r"racing\s*bulls|toro\s*rosso|alpha\s*tauri|\bRB\b"),
    ("ferrari", r"ferrari"),
    ("mercedes", r"mercedes"),
    ("mclaren", r"mclaren"),
    ("aston_martin", r"aston\s*martin"),
    ("alpine", r"alpine"),
    ("williams", r"williams"),
    ("sauber", r"sauber|alfa\s*romeo|索伯|索博"),
    # 奥迪与索伯按两支车队处理（奥迪收购索伯但车队归属独立，2026 用户裁定）；
    # 2026 的 "Audi Revolut F1 Team" → audi，历史的 Sauber/Alfa Romeo → sauber
    ("audi", r"\baudi\b|奥迪"),
    ("haas", r"haas"),
    ("cadillac", r"cadillac"),
    ("racing_point", r"racing\s*point"),
    ("force_india", r"force\s*india"),
    ("renault", r"renault"),
]

# FIA 升级申报文档（Car Presentation Submissions）从 2024 赛季才开始公开发布
UPGRADES_MIN_YEAR = 2024


def upgrades_no_data_reason(season: int) -> str:
    """升级件无数据时的原因说明（区分'源不存在'与'本站未公布'）"""
    if season and season < UPGRADES_MIN_YEAR:
        return f"（FIA自{UPGRADES_MIN_YEAR}年起才公开发布升级申报文档，更早赛季无官方明细数据）"
    return ""


def canonical_team_key(name: str) -> str:
    """车队申报名 -> 统一身份键（未识别时返回归一化原名）

    按"最早出现位置"判定：PU表是"车队+引擎供应商"格式（如 "Alfa Romeo Ferrari"、
    "AlphaTauri RBPT"、"McLaren Mercedes"），队名在前，必须取位置最早的匹配，
    否则会被引擎供应商词带偏；同位置时按 TEAM_CANONICAL_PATTERNS 顺序（red_bull 优先于 racing_bulls）。
    """
    n = name or ""
    best_key, best_pos = None, None
    for key, pat in TEAM_CANONICAL_PATTERNS:
        m = re.search(pat, n, re.IGNORECASE)
        if m and (best_pos is None or m.start() < best_pos):
            best_key, best_pos = key, m.start()
    return best_key if best_key else _normalize(n)


class F1CosmosAPI:
    """F1 COSMOS 数据源封装"""

    def __init__(self, season: int = None):
        self.season = season or datetime.now().year
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "F1-Reminder-Bot/1.0",
            "Accept": "application/json",
        })
        try:
            from .f1_api import LocalCache
            self.cache = LocalCache()
        except Exception:
            self.cache = None

    # ==================== 基础请求 ====================

    def _get(self, endpoint: str, params: Dict = None,
             cache_key: str = None, ttl_hours: int = 24) -> Optional[Any]:
        """GET 请求（带可选本地缓存，失败返回 None）"""
        params = params or {}
        if cache_key and self.cache:
            cached = self.cache.load(cache_key)
            if cached is not None:
                return cached

        url = f"{BASE_URL}/{endpoint.lstrip('/')}"
        try:
            resp = self.session.get(url, params=params, timeout=20)
            if resp.status_code == 404:
                logger.info(f"[F1Cosmos] 端点不存在(404): {endpoint}")
                return None
            resp.raise_for_status()
            payload = resp.json()
            # 两种响应形态: {result, data:[...]} 或 {data:[...]}
            if isinstance(payload, dict):
                data = payload.get("data", payload)
            else:
                data = payload
        except requests.RequestException as e:
            logger.warning(f"[F1Cosmos] 请求失败 {endpoint}: {e}")
            return None

        if cache_key and self.cache:
            self.cache.save(cache_key, data, ttl_hours=ttl_hours)
        return data

    # ==================== 数据获取 ====================

    def get_updates(self, season: int = None) -> List[Dict[str, Any]]:
        """获取赛季升级件列表（归一化）"""
        season = season or self.season
        data = self._get("dashboard/v2/updates", {"season": season},
                         cache_key=f"f1cosmos_updates_{season}", ttl_hours=24)
        if not isinstance(data, list):
            return []
        return [self._normalize_update(e) for e in data]

    def get_elements(self, season: int = None) -> List[Dict[str, Any]]:
        """获取赛季动力单元部件用量（归一化）"""
        season = season or self.season
        data = self._get("dashboard/v2/elements", {"season": season},
                         cache_key=f"f1cosmos_elements_{season}", ttl_hours=24)
        if not isinstance(data, list):
            return []
        return [self._normalize_element(e) for e in data]

    def get_destructors(self, season: int = None) -> List[Dict[str, Any]]:
        """获取赛季事故维修成本（后端暂未上线，404 返回空列表）"""
        season = season or self.season
        data = self._get("dashboard/destructors", {"year": season},
                         cache_key=f"f1cosmos_destructors_{season}", ttl_hours=24)
        return data if isinstance(data, list) else []

    # ==================== 归一化 ====================

    @staticmethod
    def _unwrap(value: Any) -> Any:
        """解开 F1Cosmos 的 {$type, value} 包装"""
        if isinstance(value, dict) and "value" in value:
            return value["value"]
        return value

    def _normalize_update(self, e: Dict) -> Dict[str, Any]:
        ctor = e.get("constructor") or {}
        gp = e.get("grandprix") or {}
        doc = e.get("fiaDocument") or {}
        try:
            round_num = int(gp.get("round", 0))
        except (TypeError, ValueError):
            round_num = 0
        category = self._unwrap(doc.get("category"))
        return {
            "component": e.get("component", ""),
            "type": e.get("type", ""),
            "reason": e.get("reason", ""),
            "difference": e.get("difference", ""),
            "brief_description": e.get("brief_description", ""),
            "team": ctor.get("broadcast_name") or ctor.get("full_name", ""),
            "team_color": ctor.get("team_color", ""),
            "round": round_num,
            "gp_name": gp.get("name", ""),
            "gp_city": gp.get("city", ""),
            "fia_url": doc.get("file_url", ""),
            "fia_title": doc.get("title", ""),
            "fia_category": category,
        }

    def _normalize_element(self, e: Dict) -> Dict[str, Any]:
        ctor = e.get("constructor") or {}
        gp = e.get("grandprix") or {}
        drv = e.get("driver") or {}
        usages = {}
        raw_usages = self._unwrap(e.get("usages"))
        if isinstance(raw_usages, str):
            try:
                usages = json.loads(raw_usages)
            except (ValueError, TypeError):
                usages = {}
        elif isinstance(raw_usages, dict):
            usages = raw_usages
        try:
            round_num = int(gp.get("round", 0))
        except (TypeError, ValueError):
            round_num = 0
        return {
            "team": ctor.get("broadcast_name") or ctor.get("full_name", ""),
            "team_color": ctor.get("team_color", ""),
            "round": round_num,
            "gp_name": gp.get("name", ""),
            "driver_name": drv.get("full_name", "") or drv.get("last_name", ""),
            "last_name": drv.get("last_name", ""),
            "tla": drv.get("tla", ""),
            "racing_number": drv.get("racing_number", ""),
            "usages": usages,
            "total": usages.get("TOTAL_COUNT") or 0,  # key存在但值为null时防 None 透传
        }

    # ==================== 匹配 ====================

    @staticmethod
    def alias_targets(query: str) -> List[str]:
        """中文别名 -> 候选目标名列表（兼容历史曾用名）；非别名返回原串"""
        q = (query or "").strip()
        v = TEAM_ALIASES_CN.get(q)
        if v is None:
            return [q] if q else []
        return list(v) if isinstance(v, (list, tuple)) else [v]

    def match_team(self, query: str, updates: List[Dict] = None) -> Optional[str]:
        """把中文/英文车队名匹配为 broadcast_name（别名支持历史曾用名列表）"""
        if not query:
            return None
        targets = self.alias_targets(query)
        names = set()
        if updates:
            names.update(u["team"] for u in updates)
        for target in targets:
            nt = _normalize(target)
            if not nt:
                continue
            for name in names:
                if name and (nt in _normalize(name) or _normalize(name) == nt):
                    return name
        return None

    def match_driver(self, query: str, elements: List[Dict] = None) -> Optional[str]:
        """把中文/英文车手名匹配为 last_name"""
        if not query:
            return None
        q = query.strip()
        if q in DRIVER_ALIASES_CN:
            return DRIVER_ALIASES_CN[q]
        nq = _normalize(q)
        for e in (elements or []):
            for cand in (e.get("last_name", ""), e.get("tla", ""), e.get("driver_name", "")):
                if cand and (nq in _normalize(cand) or _normalize(cand) == nq):
                    return e.get("last_name", "")
        return None

    # ==================== 格式化 ====================

    @staticmethod
    def _upgrade_line(u: Dict) -> str:
        return f"{u['component']}({u['type']})"

    def format_upgrades_by_gp(self, race: Dict[str, Any], season: int = None,
                              llm=None) -> Tuple[str, str]:
        """按分站汇总升级件（含意图/差异/工作原理描述，可选LLM翻译）-> (纯文本, Markdown)。

        race 为 Ergast 格式比赛 dict。用「城市优先、名称兜底」匹配升级件，
        规避两数据源 round 编号不一致的问题（Ergast 荷兰=R12 而 F1Cosmos R12=比利时）。
        FIA官方文档只有当前分站数据；历史分站查询在 FIA 源下自动回退 Cosmos 赛季数据。
        """
        circuit = race.get("Circuit", {}) or {}
        city = (circuit.get("Location", {}) or {}).get("locality", "")
        race_name = race.get("raceName", "")

        # 事件级精确数据源（FIADocsAPI 提供）：按分站直接取 FIA 官方申报文档，
        # 历史赛季/历史分站均为正确的当站数据，不再受 Cosmos 仅当前赛季的限制
        items = []
        if hasattr(self, "get_event_upgrades"):
            items = self.get_event_upgrades(race_name, season, city=city)
        if not items:
            updates = self.get_updates(season)
            items = self._match_updates(updates, city, race_name)

        # 历史分站回退Cosmos赛季数据（仅限当前赛季：Cosmos忽略season参数，历史查询会错返回当前赛季）
        if not items and (season or self.season) == self.season and hasattr(self, "get_updates_cosmos"):
            cosmos_updates = self.get_updates_cosmos(season)
            items = self._match_updates(cosmos_updates, city, race_name)

        title = f"🏎️ 【{race_name} 升级汇总】"
        text = f"{title}\n\n"
        md = f"## {title}\r\r"
        if not items:
            reason_note = upgrades_no_data_reason(season or self.season)
            text += f"本站暂无升级件数据（尚未公布或本周末比赛）{reason_note}\n"
            md += f"本站暂无升级件数据（尚未公布或本周末比赛）{reason_note}"
            return text, md

        grouped: Dict[str, List[Dict]] = {}
        for u in items:
            grouped.setdefault(u["team"], []).append(u)

        for team, us in grouped.items():
            text += f"{team}（{len(us)}项）：\n"
            md += f"**{team}**（{len(us)}项）\r"
            for u in us:
                reason = u.get("reason") or u.get("type") or ""
                line = f"• {u['component']}" + (f"（{reason}）" if reason else "")
                text += f"{line}\n"
                md += f"{line}\r"
                if u.get("difference"):
                    text += f"  差异: {u['difference']}\n"
                    md += f"> 差异: {u['difference']}\r"
                desc = u.get("brief_description") or ""
                if desc:
                    if llm:
                        desc = self._translate_desc(llm, desc)
                    text += f"  {desc}\n"
                    md += f"> {desc}\r"
                # 引用块后必须空行，否则下一条部件行被 CommonMark 懒惰延续吞进
                # 引用块（手机端后续条目全部变成小字缩进——2026-09-24 巴库卡片事故）
                md += "\r"
            text += "\n"
            md += "\r"

        # FIA源下明示未提交升级的车队（避免被误认为数据缺失）
        no_update = getattr(self, "no_update_teams", None)
        if no_update:
            text += "未提交升级：\n" + "\n".join(f"• {t}" for t in no_update) + "\n"
            md += "**未提交升级**\r" + "\r".join(f"• {t}" for t in no_update)
        return text, md

    def _match_updates(self, updates: List[Dict], city: str, name: str) -> List[Dict]:
        """按城市优先、名称兜底匹配 F1Cosmos 升级件"""
        city_n = _norm_gp(city)
        name_n = _norm_gp(name)
        matched = []
        for u in updates:
            c = _norm_gp(u.get("gp_city", ""))
            n = _norm_gp(u.get("gp_name", ""))
            if city_n and c and city_n in c:
                matched.append(u)
            elif name_n and n and (name_n in n or n in name_n):
                matched.append(u)
        return matched

    def _season_updates(self, season: int = None) -> List[Dict[str, Any]]:
        """车队赛季汇总数据源（FIADocsAPI 覆写：历史赛季走FIA逐站聚合）"""
        return self.get_updates(season)

    def _team_items(self, updates: List[Dict], team: str) -> List[Dict]:
        """按统一车队身份过滤（兼容FIA各分站/年份队名写法变体）"""
        key = canonical_team_key(team)
        return [u for u in updates if canonical_team_key(u["team"]) == key]

    @staticmethod
    def _display_team_name(items: List[Dict], fallback: str) -> str:
        """跨分站队名变体取出现次数最多的作为显示名"""
        counts: Dict[str, int] = {}
        for u in items:
            counts[u["team"]] = counts.get(u["team"], 0) + 1
        return max(counts, key=counts.get) if counts else fallback

    def format_upgrades_by_team_summary(self, team_query: str, season: int = None,
                                        llm=None) -> Tuple[str, str]:
        """按车队汇总赛季升级（按分站分组的明细：部件+差异+描述）-> (纯文本, Markdown)"""
        season = season or self.season
        reason_note = upgrades_no_data_reason(season)
        if reason_note:
            # 该年份无官方升级数据源，直接说明原因（不再尝试匹配车队）
            msg = f"{season}赛季无官方升级申报数据{reason_note}"
            return f"🏎️ 【{msg}】\n", f"## 🏎️ {msg}"
        updates = self._season_updates(season)
        team = self.match_team(team_query, updates)
        if not team:
            return "", ""
        items = self._team_items(updates, team)
        team = self._display_team_name(items, team)

        text = f"🏎️ 【{team} {season}赛季升级记录】\n\n"
        md = f"## 🏎️ {team} {season}赛季升级记录\r\r"
        if not items:
            reason_note = upgrades_no_data_reason(season)
            text += f"暂无升级数据{reason_note}\n"
            md += f"暂无升级数据{reason_note}"
            return text, md

        # 按分站分组（保持赛季顺序：round 升序，round=0 按 gp 名称去重）
        groups: Dict[str, List[Dict]] = {}
        order: List[str] = []
        for u in sorted(items, key=lambda x: (x["round"] or 99, x.get("gp_name", ""))):
            key = u.get("gp_name") or f"R{u['round']}"
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(u)

        for key in order:
            us = groups[key]
            first = us[0]
            head = f"R{first['round']} {key}" if first.get("round") else key
            text += f"📍 {head}（{len(us)}项）：\n"
            md += f"**📍 {head}**（{len(us)}项）\r"
            for u in us:
                reason = u.get("reason") or u.get("type") or ""
                line = f"• {u['component']}" + (f"（{reason}）" if reason else "")
                text += f"{line}\n"
                md += f"{line}\r"
                if u.get("difference"):
                    text += f"  差异: {u['difference']}\n"
                    md += f"> 差异: {u['difference']}\r"
                desc = u.get("brief_description") or ""
                if desc:
                    if llm:
                        desc = self._translate_desc(llm, desc)
                    text += f"  {desc}\n"
                    md += f"> {desc}\r"
                md += "\r"  # 空行阻断引用块懒惰延续
            text += "\n"
            md += "\r"
        return text, md

    def format_team_gp_upgrades(self, team_query: str, race: Dict[str, Any],
                                season: int = None, llm=None) -> Tuple[str, str]:
        """指定车队在指定分站指定年份的升级明细 -> (纯文本, Markdown)"""
        circuit = race.get("Circuit", {}) or {}
        city = (circuit.get("Location", {}) or {}).get("locality", "")
        race_name = race.get("raceName", "")

        items = []
        if hasattr(self, "get_event_upgrades"):
            items = self.get_event_upgrades(race_name, season, city=city)
        if not items:
            items = self._match_updates(self.get_updates(season), city, race_name)
        # Cosmos兜底仅限当前赛季（其season参数无效，历史查询会错返回当前赛季数据）
        if not items and (season or self.season) == self.season and hasattr(self, "get_updates_cosmos"):
            items = self._match_updates(self.get_updates_cosmos(season), city, race_name)

        team = self.match_team(team_query, items)
        season = season or self.season
        if not team:
            # 车队本站无申报：已知车队别名也应明确告知原因，而非查无此队
            targets = self.alias_targets(team_query)
            if targets and targets[0] != team_query.strip():
                reason_note = upgrades_no_data_reason(season)
                msg = (f"{season}赛季无官方升级申报数据{reason_note}" if reason_note
                       else f"{targets[0]} 在 {season} {race_name} 未提交升级")
                return f"🏎️ 【{msg}】\n", f"## 🏎️ {msg}"
            return "", ""
        items = self._team_items(items, team)
        team = self._display_team_name(items, team)
        if not items:
            msg = f"{team} 在 {season} {race_name} 未提交升级"
            return f"🏎️ 【{msg}】\n", f"## 🏎️ {msg}"

        title = f"{team} · {season} {race_name} 升级明细"
        text = f"🏎️ 【{title}】\n\n"
        md = f"## 🏎️ {title}\r\r"
        for u in items:
            reason = u.get("reason") or u.get("type") or ""
            diff = u.get("difference") or ""
            desc = u.get("brief_description") or ""
            if llm and desc:
                desc = self._translate_desc(llm, desc)
            text += f"• {u['component']}" + (f"（{reason}）" if reason else "") + "\n"
            if diff:
                text += f"  差异: {diff}\n"
            if desc:
                text += f"  {desc}\n"
            md += f"**{u['component']}**" + (f"（{reason}）" if reason else "") + "\r"
            if diff:
                md += f"> 差异: {diff}\r"
            if desc:
                md += f"> {desc}\r"
            md += "\r"  # 空行阻断引用块懒惰延续
        fia_url = items[0].get("fia_url", "")
        if fia_url and _fia_url_matches_event(fia_url, race_name, city):
            text += f"\n📄 {fia_url}\n"
            md += f"\r📄 [FIA申报文档]({fia_url})"
        return text, md

    def format_upgrades_by_team(self, team_query: str, season: int = None,
                                llm=None) -> Tuple[str, str]:
        """按车队列出升级件明细（按分站分组，含描述，可选 LLM 翻译）-> (纯文本, Markdown)"""
        return self.format_upgrades_by_team_summary(team_query, season, llm=llm)

    def format_upgrade_by_component(self, component_query: str, season: int = None,
                                    llm=None) -> Tuple[str, str]:
        """按部件名列出升级件明细（含描述，可选 LLM 翻译）-> (纯文本, Markdown)"""
        updates = self._season_updates(season)
        nq = _normalize(component_query)
        if not nq:
            return "", ""
        items = [u for u in updates if nq in _normalize(u["component"])]
        if not items:
            return "", ""
        items.sort(key=lambda x: x["round"])
        return self._render_upgrade_detail(items, component_query, llm)

    def _render_upgrade_detail(self, items: List[Dict], title: str, llm=None) -> Tuple[str, str]:
        """渲染升级件明细列表（team/component 共用）"""
        text = f"🏎️ 【{title} 升级明细】\n\n"
        md = f"## 🏎️ {title} 升级明细\r\r"
        for u in items:
            diff = u["difference"] or ""
            desc = u["brief_description"] or ""
            if llm and desc:
                desc = self._translate_desc(llm, desc)
            text += f"R{u['round']} {u['team']} {u['component']}({u['type']})\n"
            text += f"  {diff}\n"
            if desc:
                text += f"  {desc}\n"
            link_ok = u["fia_url"] and _fia_url_matches_event(u["fia_url"], u.get("gp_name", ""))
            if link_ok:
                text += f"  📄 {u['fia_url']}\n"
            text += "\n"

            md += f"**R{u['round']} {u['team']} {u['component']}**\r> 类型 {u['type']}\r"
            if diff:
                md += f"\r{diff}"
            if desc:
                md += f"\r{desc}"
            if link_ok:
                md += f"\r📄 [FIA文档]({u['fia_url']})"
            md += "\r\r"
        return text, md

    def _translate_desc(self, llm, desc: str) -> str:
        """调用 LLM 翻译描述，任何失败回退英文原文"""
        if not llm or not getattr(llm, "enabled", False):
            return desc
        try:
            tr = llm.translate_en_to_zh(desc)
            return tr or desc
        except Exception as e:
            logger.warning(f"升级件描述翻译失败，回退原文: {e}")
            return desc

    def format_pu_quota(self, query: str = "", season: int = None) -> Tuple[str, str]:
        """动力单元部件用量 -> (纯文本, Markdown)。query 可为车队/车手名，空则整队一览；
        历史赛季为该赛季末累计快照（FIA文档）"""
        season = season or self.season
        elements = self.get_elements(season)
        era = f"{season}赛季末累计" if season != self.season else ""
        if not elements:
            msg = f"{season}赛季暂无动力单元部件数据" if era else "暂无动力单元部件数据"
            return msg, msg

        selected = elements
        title = f"🔋 【{era}动力单元部件用量】" if era else "🔋 【动力单元部件用量】"

        if query:
            team = self.match_team(query, elements)
            driver = self.match_driver(query, elements)
            if driver:
                selected = [e for e in elements if e["last_name"] == driver]
                title = f"🔋 【{driver} {era}动力单元部件用量】" if era else f"🔋 【{driver} 动力单元部件用量】"
            else:
                if team:
                    key = canonical_team_key(team)
                    selected = [e for e in elements if canonical_team_key(e["team"]) == key]
                else:
                    # 历史队名变体兜底（如 2023 的 "RBR RBPT"）：用别名的统一身份键直接匹配
                    selected = []
                    for t in self.alias_targets(query):
                        key = canonical_team_key(t)
                        selected = [e for e in elements if canonical_team_key(e["team"]) == key]
                        if selected:
                            team = selected[0]["team"]
                            break
                if not team or not selected:
                    return "", ""
                title = f"🔋 【{team} {era}动力单元部件用量】" if era else f"🔋 【{team} 动力单元部件用量】"

        text = f"{title}\n\n"
        md = f"## {title}\r\r"
        if not selected:
            text += "暂无数据\n"
            md += "暂无数据"
            return text, md

        # 每车手一行；同一车手取最新 round
        latest: Dict[str, Dict] = {}
        for e in selected:
            key = e["last_name"] or e["tla"]
            if key not in latest or e["round"] >= latest[key]["round"]:
                latest[key] = e

        pu_limits = get_pu_limits(season)
        order = ["ICE", "TC", "EXH", "MGU-H", "MGU-K", "ES", "PU-CE", "PU-ANC"]
        # 只展示数据中实际出现的部件列
        cols = [k for k in order if any(k in e["usages"] for e in latest.values())]

        def _cell(k: str, v) -> str:
            try:
                n = int(v)
            except (TypeError, ValueError):
                return "-"  # usages 来自非schema约束的JSON，防 null/非数字崩溃
            lim = pu_limits.get(k)
            mark = "⚠️" if (lim is not None and n >= lim) else ""
            return f"{n}{mark}"

        for e in latest.values():
            usages = e["usages"]
            name = e["last_name"] or e["driver_name"]
            counts = [f"{k} {_cell(k, usages[k])}" for k in cols if k in usages]
            counts.append(f"总 {e['total']}")
            text += f"{name}: {' | '.join(counts)}\n"

        # Markdown 表格
        header = "| 车手 | " + " | ".join(cols) + " | 总 |"
        sep = "|" + "---|" * (len(cols) + 2)
        md += header + "\r" + sep + "\r"
        for e in latest.values():
            usages = e["usages"]
            name = e["last_name"] or e["driver_name"]
            row = f"| **{name}** | " + " | ".join(
                _cell(k, usages[k]) if k in usages else "-" for k in cols
            ) + f" | {e['total']} |"
            md += row + "\r"

        if pu_limits:
            lim_str = " / ".join(f"{k} {v}" for k, v in pu_limits.items() if k in cols)
            if lim_str:
                text += f"\n{season}赛季上限：{lim_str}\n⚠️ 表示已达到/超出赛季部件上限\n"
                md += f"\r\r> {season}赛季上限：{lim_str}\r> ⚠️ 表示已达到/超出赛季部件上限（超出将触发罚退）"
        # 席位继承口径说明（季中换人时配额跟随赛车/席位，由继任者沿用）
        note = "注：部件配额跟随赛车席位，季中换人由继任者沿用（FIA官方口径），被换下车手不再出现在表中"
        text += f"\n{note}\n"
        md += f"\r> {note}"
        return text, md

    # ==================== PU 部件更换检测（赛后播报） ====================

    PU_ORDER = ["ICE", "TC", "EXH", "MGU-K", "ES", "PU-CE", "PU-ANC"]

    def _snapshot_path(self, season: int) -> str:
        data_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, f"pu_snapshot_{season}.json")

    def detect_pu_changes(self, season: int = None) -> List[Dict[str, Any]]:
        """
        对比上次快照，返回新更换动力单元部件的车手。

        由于 F1Cosmos elements 接口只返回最新累计用量（无逐站历史），
        此处用本地快照文件记录上次观测，赛后对比增量。

        Returns:
            [{"driver": last_name, "team": broadcast_name, "diffs": ["ICE 2->3", ...]}]
        """
        season = season or self.season
        current = self.get_elements(season)
        if not current:
            return []

        snap_path = self._snapshot_path(season)
        has_prev = os.path.exists(snap_path)
        prev: Dict[str, Dict] = {}
        if has_prev:
            try:
                with open(snap_path, "r", encoding="utf-8") as f:
                    prev = json.load(f)
            except Exception as e:
                logger.warning(f"读取PU快照失败: {e}")
                has_prev = False

        changed = []
        snapshot = {}
        for e in current:
            key = e["last_name"] or e["tla"] or e["driver_name"]
            cur = e["usages"]
            snapshot[key] = cur
            if not has_prev:
                # 首次运行无基线，只保存快照，不播报
                continue
            prev_u = prev.get(key, {})
            diffs = []
            for k in self.PU_ORDER:
                cur_v = int(cur.get(k, 0) or 0)
                prev_v = int(prev_u.get(k, 0) or 0)
                if cur_v > prev_v:
                    diffs.append(f"{k} {prev_v}->{cur_v}")
            if diffs:
                changed.append({
                    "driver": e["last_name"] or e["driver_name"],
                    "team": e["team"],
                    "diffs": diffs,
                })

        try:
            tmp_path = snap_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, snap_path)  # 原子写，防半截快照导致下次检测漏报
        except Exception as e:
            logger.warning(f"写入PU快照失败: {e}")

        return changed

    def format_pu_changes(self, changed: List[Dict[str, Any]], context: str = "pre_race") -> Tuple[str, str]:
        """格式化PU部件更换播报 -> (纯文本, Markdown)。context=pre_race 时附加罚退提示"""
        title = "🔧 【本站动力单元部件更换】"
        if not changed:
            return "", ""
        text = f"{title}\n\n"
        md = f"## {title}\r\r"
        for c in changed:
            line = f"{c['driver']} ({c['team']}): {', '.join(c['diffs'])}"
            text += f"{line}\n"
            md += f"**{c['driver']}** ({c['team']})\r> {', '.join(c['diffs'])}\r\r"
            # 末尾 \r\r 空行阻断引用块懒惰延续（否则下一位车手被吞进小字缩进，
            # 与 2026-09-24 升级件卡片同款事故）
        if context == "pre_race":
            hint = "⚠️ 部件超额使用可能触发正赛发车格罚退，请留意发车位变化"
            text += f"\n{hint}\n"
            md += f"\r\r{hint}"
        return text, md

    def format_destructors(self, query: str = "", season: int = None) -> Tuple[str, str]:
        """事故维修成本 -> (纯文本, Markdown)。后端未上线时返回提示"""
        data = self.get_destructors(season)
        title = "💥 【事故维修成本】"
        if not data:
            return (f"{title}\n\n该功能数据暂未开放", f"## {title}\r\r该功能数据暂未开放")

        text = f"{title}\n\n"
        md = f"## {title}\r\r"
        # 数据结构未定（后端未上线），先原样输出条目
        for e in data:
            line = json.dumps(e, ensure_ascii=False)
            text += f"{line}\n"
            md += f"{line}\r"
        return text, md
