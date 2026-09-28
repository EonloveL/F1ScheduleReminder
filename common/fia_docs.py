"""
F1赛程提醒机器人 - FIA官方文档数据源
从FIA官网Decision Documents抓取并解析赛事文档（PDF），替代F1Cosmos第三方源：
- 升级件申报: Car Presentation Submissions（含部件描述/备注）
- PU部件用量: PU Elements used per Driver up to now
- 本站新增PU: New PU elements for this Competition

赛季文档索引页（SSR直出，无需JS）:
  https://www.fia.com/documents/championships/fia-formula-one-world-championship-14/season/season-{year}-{page_id}
页面默认列出最近一站的全部文档；PDF地址规律:
  /system/files/decision-document/{year}_{event_slug}_-_{doc_slug}.pdf

接口与 F1CosmosAPI 兼容（继承自它），FIA抓取/解析失败时自动回退Cosmos。
"""

import io
import logging
import re
import unicodedata
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

from .f1cosmos_api import F1CosmosAPI, UPGRADES_MIN_YEAR
from .drivers_profile import get_known_driver_names

logger = logging.getLogger(__name__)

BASE = "https://www.fia.com"
SEASON_PAGE = BASE + "/documents/championships/fia-formula-one-world-championship-14"
# 年份 -> 赛季页ID（页面选择框实证）；未知年份时自动从选择框解析
SEASON_PAGE_IDS = {2025: 2071, 2026: 2072,
                   2024: 2043, 2023: 2042, 2022: 2005, 2021: 1108,
                   2020: 1059, 2019: 971, 2015: 249}

# 车队标题识别（大小写不敏感，覆盖历史曾用名：
# SCUDERIA FERRARI / MONEYGRAM HAAS / Stake F1 Team KICK Sauber / Visa Cash App RB 等）
TEAM_HEADER_RE = re.compile(
    r"(?i)(red\s*bull|racing\s*bulls|toro\s*rosso|alpha\s*tauri|alfa\s*romeo"
    r"|aston\s*martin|racing\s*point|force\s*india|mercedes|ferrari|mclaren"
    r"|williams|renault|alpine|sauber|\bRB\b|audi|cadillac|haas)")

# 2026 车队官方名称关键词（用于在PDF中识别车队标题行，保留兼容旧代码引用）
TEAM_HEADER_KEYWORDS = [
    "McLaren", "Red Bull", "Ferrari", "Mercedes", "Aston Martin",
    "Alpine", "Williams", "Racing Bulls", "Audi", "Cadillac", "Haas",
]

# 现役车手全名（用于PU表格中"车队+车手"列的切分，最长后缀优先）
# 数据源：data/drivers_profile.json（common/drivers_profile.py，含 roster_status 追踪）
KNOWN_DRIVERS_2026 = get_known_driver_names()

# FIA文档部件代码 -> 内部统一键
PU_CODE_MAP = {"ICE": "ICE", "TC": "TC", "EXH": "EXH", "EX": "EXH", "ES": "ES",
               "MGU-H": "MGU-H", "MGU-K": "MGU-K", "CE": "PU-CE", "ANC": "PU-ANC"}

# PU Elements used 表的列布局（按表头正则识别；FIA 各赛季列序不同，实证见下）
# - 2026+: 无 MGU-H，ICE TC EXH ES MGU-K PU-CE PU-ANC（表头跨行换行，只匹配首行）
# - 2020-2025: ICE TC MGU-H MGU-K ES CE EX
# - 2019: ICE TC MGU-H MGU-K ES CE（无 EX 列）
_PU_TABLE_LAYOUTS = [
    (re.compile(r"Driver\s+ICE\s+TC\s+EXH\s+ES"),
     ["ICE", "TC", "EXH", "ES", "MGU-K", "PU-CE", "PU-ANC"]),
    (re.compile(r"Driver\s+ICE\s+TC\s+MGU-H\s+MGU-K\s+ES\s+CE\s+EX"),
     ["ICE", "TC", "MGU-H", "MGU-K", "ES", "PU-CE", "EXH"]),
    (re.compile(r"Driver\s+ICE\s+TC\s+MGU-H\s+MGU-K\s+ES\s+CE\s*$"),
     ["ICE", "TC", "MGU-H", "MGU-K", "ES", "PU-CE"]),
]

DOC_SLUGS = {
    "upgrades": "car_presentation_submissions",
    "pu_used": "pu_elements_used_per_driver_up_to_now",
    "pu_new": "new_pu_elements_for_this_competition",
}


def _norm_event(text: str) -> str:
    """赛事名 -> FIA PDF中的event_slug（去重音、小写、空格转下划线）"""
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")


class FIADocsAPI(F1CosmosAPI):
    """FIA官方文档数据源（FIA优先，Cosmos兜底，接口与F1CosmosAPI一致）"""

    def __init__(self, season: int = None):
        super().__init__(season)
        # 注意：Cosmos兜底调用也走这个session，Accept保持宽松避免影响JSON接口
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Accept": "*/*",
        })
        self._season_url_cache: Dict[int, Optional[str]] = {}
        # 当前分站名缓存 {season: (时间戳, 赛事名|None)}：
        # 服务器访问 fia.com 不稳定（2026-09-15 实测连续 read timeout 20s×2），
        # 成功缓存 30min，失败熔断 10min（熔断期内不再发起请求，避免每次提问白卡 20s）
        self._event_name_cache: Dict[int, tuple] = {}
        self._EVENT_NAME_OK_TTL = 1800
        self._EVENT_NAME_FAIL_TTL = 600
        # no_update_teams 线程保护：get_season_events_upgrades 用线程池并发解析，
        # 共享实例属性需加锁防互相清零/混写
        import threading as _th
        self._no_update_lock = _th.Lock()
        # 可选：外部注入的赛程提供者 callable(season)->Ergast格式比赛列表，
        # 用于给事件级升级数据补充分站轮次（由调用方如QQ机器人主程序设置）
        self.schedule_provider = None

    def get_updates_cosmos(self, season: int = None) -> List[Dict[str, Any]]:
        """历史分站升级件查询的Cosmos赛季数据通道（FIA只有当前分站文档）"""
        return F1CosmosAPI.get_updates(self, season)

    def match_team(self, query: str, updates: List[Dict] = None) -> Optional[str]:
        """车队名匹配：FIA申报名含赞助商标题（如 Oracle Red Bull Racing），做模糊包含匹配；
        中文别名支持历史曾用名候选列表（如 索伯 -> Audi/Sauber 按赛季数据自动匹配）"""
        from .f1cosmos_api import _normalize
        for target in self.alias_targets(query):
            nt = _normalize(target)
            if not nt:
                continue
            for name in {u["team"] for u in (updates or [])}:
                nn = _normalize(name)
                if nt and nn and (nt in nn or nn in nt):
                    return name
        return super().match_team(query, updates)

    def current_event_name(self) -> str:
        """公开：FIA当前已发布文档的分站名"""
        return self._current_event_name()

    # ==================== 赛季页与文档列表 ====================

    def _season_page_url(self, season: int) -> Optional[str]:
        cached = self._season_url_cache.get(season)
        if cached:
            return cached
        page_id = SEASON_PAGE_IDS.get(season)
        if not page_id:
            # 从索引页选择框解析 "SEASON {year}" 对应的链接（value 为相对路径）
            rel_base = SEASON_PAGE.replace(BASE, "")
            try:
                resp = self.session.get(SEASON_PAGE, timeout=20)
                m = re.search(
                    rf'value="({rel_base}/season/season-{season}-\d+)"', resp.text)
                if m:
                    url = BASE + m.group(1)
                    self._season_url_cache[season] = url
                    return url
                logger.warning(f"FIA索引页未找到 {season} 赛季链接")
                return None
            except Exception as e:
                logger.warning(f"FIA索引页请求失败: {e}")
                return None
        url = f"{SEASON_PAGE}/season/season-{season}-{page_id}"
        self._season_url_cache[season] = url
        return url

    def get_season_events(self, season: int = None) -> List[str]:
        """赛季全部分站事件名（来自赛季页选择框，如 "Italian Grand Prix"）。

        历史赛季结果永久缓存；当前赛季 24h 缓存。含季前测试事件（ Bahrain Tests 等）。
        """
        season = season or self.season
        cache_key = f"fia_season_events_{season}"
        ttl = 24 if season >= self.season else None
        if self.cache:
            cached = self.cache.load(cache_key)
            if cached is not None:
                return cached
        url = self._season_page_url(season)
        if not url:
            return []
        try:
            resp = self.session.get(url, timeout=20)
            events = []
            rel = url.replace(BASE, "")  # 选择框 value 为相对路径
            for m in re.finditer(r'value="' + re.escape(rel) + r'/event/([^"]+)"', resp.text):
                from urllib.parse import unquote
                name = unquote(m.group(1))
                if name not in events:
                    events.append(name)
            if events and self.cache:
                self.cache.save(cache_key, events, ttl_hours=ttl)
            return events
        except Exception as e:
            logger.warning(f"FIA赛季事件列表获取失败({season}): {e}")
            return []

    def resolve_event_name(self, event_name: str, season: int = None,
                           city: str = "") -> Optional[str]:
        """把赛历 raceName/地点 解析为 FIA 赛季页中的官方事件名"""
        from .f1cosmos_api import _norm_gp
        events = self.get_season_events(season)
        if not events:
            return None
        target = _norm_gp(event_name)
        city_n = _norm_gp(city)
        for e in events:
            if _norm_gp(e) == target:
                return e
        for e in events:
            en = _norm_gp(e)
            if target and (target in en or en in target):
                return e
            # 城市名匹配（如 Monza -> Italian Grand Prix 无城市信息，跳过）
            if city_n and city_n in en:
                return e
        return None

    @staticmethod
    def _norm_pdf_key(path: str) -> str:
        """PDF路径 -> 归一化匹配键：小写、非字母数字转下划线、去掉扩展名与版本分隔差异
        兼容两种命名：
        - 新: /system/files/decision-document/2025_italian_grand_prix_-_car_presentation_submissions.pdf
        - 旧: /sites/default/files/decision-document/2024 Italian Grand Prix - Car Presentation Submissions.pdf
        """
        from urllib.parse import unquote
        base = unquote(path).rsplit("/", 1)[-1]
        base = re.sub(r"\.pdf$", "", base, flags=re.IGNORECASE)
        key = re.sub(r"[^a-z0-9]+", "_", base.lower()).strip("_")
        return key

    def _parse_doc_rows(self, html: str) -> List[Dict[str, Any]]:
        """从赛季页/事件页HTML解析文档行 -> [{doc_no,title,url,file(归一化键)}]

        兼容两种行格式：
        - 新: "Doc 64 - Championship Points Published on 01.09.24 19:10 CET"
        - 旧(2024早期): "Championship Points Published on 02.03.24 20:20 CET"（无Doc编号）
        """
        docs = []
        for m in re.finditer(r'<li class="document-row[^"]*">(.*?)</li>', html, re.DOTALL):
            row = m.group(1)
            href = re.search(r'href="([^"]+\.pdf)"', row)
            if not href:
                continue
            text = re.sub(r"<[^>]+>", " ", row)
            text = re.sub(r"\s+", " ", text).strip()
            doc_no = 0
            dm = re.match(r"(?:Recalled\s*-\s*)?Doc\s*(\d+)\s*-\s*(.*?)\s*Published on", text)
            if dm:
                doc_no = int(dm.group(1))
                title = dm.group(2).strip()
            else:
                tm = re.match(r"(.*?)\s*Published on", text)
                title = tm.group(1).strip() if tm else text
            pdf_path = href.group(1)
            docs.append({
                "doc_no": doc_no,
                "title": title,
                "url": pdf_path if pdf_path.startswith("http") else BASE + pdf_path,
                "file": self._norm_pdf_key(pdf_path),
            })
        if not docs:
            # 页面无 document-row 结构时退化为纯PDF链接扫描
            for m in re.finditer(r'href="([^"]+\.pdf)"', html):
                pdf_path = m.group(1)
                docs.append({
                    "doc_no": 0, "title": "",
                    "url": pdf_path if pdf_path.startswith("http") else BASE + pdf_path,
                    "file": self._norm_pdf_key(pdf_path),
                })
        return docs

    def get_event_documents(self, event_name: str, season: int = None) -> List[Dict[str, Any]]:
        """
        获取指定分站的文档列表。

        当前赛季快路径：赛季页默认列出最近一站，按文件名前缀 {year}_{event_slug}_-_ 过滤；
        其余情况（历史赛季/当前赛季非最新一站）走 /event/{name} 事件页。
        事件不存在时返回空列表。
        """
        season = season or self.season
        season_url = self._season_page_url(season)
        if not season_url:
            return []

        # 快路径：当前赛季页（仅当所查事件正是赛季页当前列出的事件）
        if season == self.season:
            try:
                resp = self.session.get(season_url, timeout=20)
                if resp.status_code == 200:
                    slug = _norm_event(event_name)
                    prefix = self._norm_pdf_key(f"{season}_{slug}_-_x")[:-2]  # "{season}_{slug}"
                    docs = [d for d in self._parse_doc_rows(resp.text)
                            if d["file"].startswith(prefix)]
                    if docs:
                        return docs
            except Exception as e:
                logger.warning(f"FIA赛季页请求异常: {e}")

        # 事件页路径
        official = self.resolve_event_name(event_name, season)
        if not official:
            return []
        from urllib.parse import quote
        try:
            resp = self.session.get(f"{season_url}/event/{quote(official)}", timeout=20)
            if resp.status_code != 200:
                logger.info(f"FIA事件页请求失败: HTTP {resp.status_code} - {official}")
                return []
            return self._parse_doc_rows(resp.text)
        except Exception as e:
            logger.warning(f"FIA事件页请求异常({official}): {e}")
            return []

    def find_doc_url(self, docs: List[Dict[str, Any]], kind: str) -> Optional[str]:
        """从文档列表中找指定类型文档的PDF地址；多版本(_0/_1后缀)取最新版"""
        urls = self.find_doc_urls(docs, kind)
        return urls[-1] if urls else None

    def find_doc_urls(self, docs: List[Dict[str, Any]], kind: str) -> List[str]:
        """找指定类型文档的全部版本PDF地址，按版本升序（无后缀=初版，_0/1=增补...）

        file 为归一化键（见 _norm_pdf_key），同时兼容新旧两种命名方案：
        新 "2026_dutch_grand_prix_car_presentation_submissions_0"
        旧 "2024_italian_grand_prix_car_presentation_submissions"
        """
        slug = DOC_SLUGS[kind]  # 已是下划线形式，与归一化键一致
        found = []
        for d in docs:
            m = re.search(rf"_{re.escape(slug)}(?:_(\d+))?$", d.get("file", ""))
            if m:
                ver = int(m.group(1)) + 1 if m.group(1) else 0
                found.append((ver, d["url"]))
        return [u for _, u in sorted(found)]

    def _download_pdf(self, url: str) -> Optional[bytes]:
        try:
            resp = self.session.get(url, timeout=30)
            if resp.status_code == 200 and resp.content[:4] == b"%PDF":
                return resp.content
            logger.warning(f"FIA PDF下载失败: HTTP {resp.status_code} - {url[:90]}")
        except Exception as e:
            logger.warning(f"FIA PDF下载异常: {e}")
        return None

    # ==================== PDF解析：PU用量表 ====================

    # 车队/引擎词表（历史车手不在现役名单时的切分兜底：剥离前缀后余下即车手名）
    _TEAM_TOKENS = {
        "red", "bull", "racing", "rbr", "rbpt", "rb", "ford", "honda",
        "ferrari", "mercedes", "mclaren", "alpine", "renault", "williams",
        "atlassian", "alfa", "romeo", "toro", "rosso", "alphatauri", "alpha",
        "tauri", "point", "force", "india", "aston", "martin", "aramco",
        "haas", "sauber", "audi", "cadillac", "bulls", "stake", "kick",
        "moneygram", "visa", "cash", "app", "lotus", "caterham", "marussia",
        "manor", "virgin", "hrt", "jordan", "scuderia",
    }

    @classmethod
    def _split_team_driver(cls, text: str) -> tuple:
        """把 'McLaren Mercedes Oscar Piastri' 切分为 (车队, 车手)；车手按已知全名最长后缀匹配"""
        for name in sorted(KNOWN_DRIVERS_2026, key=len, reverse=True):
            if text.endswith(name):
                return text[: -len(name)].strip(), name
        # 容错：FIA PDF特殊字符编码问题（如 Hülkenberg -> Hlkenberg）
        compact = text.replace("ü", "u").replace("í", "i")
        for name in sorted(KNOWN_DRIVERS_2026, key=len, reverse=True):
            n = name.replace("ü", "u").replace("í", "i")
            if compact.endswith(n):
                driver = text[len(text) - len(n):]
                return text[: len(text) - len(n)].strip(), driver
        # 历史车手兜底：剥离车队/引擎词前缀，余下为车手（如 'Toro Rosso Daniil Kvyat'）
        tokens = text.split()
        i = 0
        while i < len(tokens) and tokens[i].strip(".,").lower() in cls._TEAM_TOKENS:
            i += 1
        if 0 < i < len(tokens):
            return " ".join(tokens[:i]), " ".join(tokens[i:])
        parts = text.rsplit(None, 2)
        if len(parts) == 3:
            return parts[0], f"{parts[1]} {parts[2]}"
        return "", text

    def parse_pu_used_pdf(self, pdf_bytes: bytes, gp_name: str = "") -> List[Dict[str, Any]]:
        """解析 PU Elements used per Driver 表 -> Cosmos elements 同构列表
        列布局按表头识别（_PU_TABLE_LAYOUTS），兼容 2019（无EX）至 2026（无MGU-H）"""
        import pdfplumber
        rows = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages[1:]:
                text = page.extract_text() or ""
                col_order = None
                for line in text.splitlines():
                    line = line.strip()
                    # 表头行：识别列布局（每个文档页重复出现表头）
                    if "Driver" in line and "ICE" in line:
                        for pat, layout in _PU_TABLE_LAYOUTS:
                            if pat.search(line):
                                col_order = layout
                                break
                        continue
                    if not col_order:
                        continue
                    n = len(col_order)
                    m = re.match(r"^(\d{1,2})\s+(.+?)\s+((?:\d+\s+){%d}\d+)\s*$" % (n - 1),
                                 line)
                    if not m:
                        continue
                    team, driver = self._split_team_driver(m.group(2))
                    vals = [int(v) for v in m.group(3).split()]
                    if len(vals) != n:
                        continue
                    usages = dict(zip(col_order, vals))
                    usages["TOTAL_COUNT"] = sum(vals)
                    last_name = driver.split()[-1] if driver else ""
                    rows.append({
                        "team": team, "team_color": "", "round": 0, "gp_name": gp_name,
                        "driver_name": driver, "last_name": last_name, "tla": "",
                        "racing_number": m.group(1), "usages": usages,
                        "total": usages["TOTAL_COUNT"],
                    })
        return rows

    # ==================== PDF解析：本站新增PU ====================

    def parse_pu_new_pdf(self, pdf_bytes: bytes) -> List[Dict[str, Any]]:
        """
        解析 New PU elements for this Competition ->
        [{"driver": 全名, "last_name": 姓, "team": 车队, "element": "ICE", "prev": 3, "now": 4}]
        """
        import pdfplumber
        results = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            full = "\n".join((p.extract_text() or "") for p in pdf.pages[1:])
        # 按段落切分：兼容两种措辞
        #   初版: "will start ... with a new internal combustion engine (ICE):"
        #   更新版: "is using a new internal combustion engine (ICE) for the remainder of the Competition:"
        section_re = re.compile(
            r"(?:with a new|using an?)\s+[^():]*\(([A-Z\-]{2,6})\)[^:]*:\s*([\s\S]*?)"
            r"(?=(?:with a new|using an?)\s+[^():]*\([A-Z\-]{2,6}\)[^:]*:|$)")
        for sm in section_re.finditer(full):
            code = PU_CODE_MAP.get(sm.group(1), sm.group(1))
            body = sm.group(2)
            for line in body.splitlines():
                m = re.match(r"^(\d{1,2})\s+(.+?)\s+(\d+)\s*$", line.strip())
                if not m:
                    continue
                team, driver = self._split_team_driver(m.group(2))
                prev = int(m.group(3))
                results.append({
                    "driver": driver,
                    "last_name": driver.split()[-1] if driver else "",
                    "team": team,
                    "element": code,
                    "prev": prev,
                    "now": prev + 1,
                })
        return results

    # ==================== PDF解析：升级件申报 ====================

    def parse_upgrades_pdf(self, pdf_bytes: bytes, gp_name: str = "",
                           pdf_url: str = "") -> List[Dict[str, Any]]:
        """
        解析 Car Presentation Submissions -> Cosmos updates 同构列表
        完整表格列: 0=编号 1=部件 4=更新原因 7=与旧版差异 9..12=工作描述；
        跨页续表会缩成 5~7 列（0=编号 1=部件 2=原因 3=差异 4+=描述），
        按列数自适应（_col_layout，2026 巴库奥迪续页实证）
        车队标题在表格外文本行，按垂直位置归属最近的下方主表；跨页续表沿用上一页车队
        FIA申报表中同一更新包的连续部件共享原因/差异/描述（合并单元格解析为None），向下继承
        "No updates submitted"车队记入 self.no_update_teams（汇总时明示，避免被误认为遗漏）
        """
        import pdfplumber
        updates = []
        with self._no_update_lock:
            self.no_update_teams = []
        state = {"team": "", "current": None, "last_complete": None}
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages[1:]:
                # 捕获“本站未提交升级”的车队页（整页无表格，仅标题+说明文字）
                page_text = page.extract_text() or ""
                if "no update" in page_text.lower():
                    for line in page_text.splitlines():
                        line = line.strip()
                        if TEAM_HEADER_RE.search(line) and len(line) < 60 \
                                and not line[0].isdigit() and "no update" not in line.lower() \
                                and "Car Presentation" not in line:
                            with self._no_update_lock:
                                self.no_update_teams.append(line.strip("*. ").strip())
                            break
                tables = page.find_tables()
                if not tables:
                    continue
                # 主表 = 面积最大的表
                main = max(tables, key=lambda t: (t.bbox[2] - t.bbox[0]) * (t.bbox[3] - t.bbox[1]))
                team = self._find_team_header(page, main.bbox[1])
                if team:
                    state["team"] = team
                self._parse_upgrades_table(main.extract(), gp_name, pdf_url, updates, state)
        if state["current"]:
            updates.append(state["current"])
        return updates

    @staticmethod
    def _find_team_header(page, table_top: float) -> str:
        """在主表上方找最近的车队标题行（含车队关键词且较短的文本行），清理装饰字符"""
        lines: Dict[int, str] = {}
        for w in page.extract_words():
            key = round(w["top"])
            lines[key] = (lines.get(key, "") + " " + w["text"]).strip()
        best = ""
        best_top = -1
        for top, text in lines.items():
            if top >= table_top:
                continue
            if TEAM_HEADER_RE.search(text) and len(text) < 60 \
                    and not text[0].isdigit():
                if top > best_top:
                    best_top = top
                    best = text
        # 清理PDF装饰字符（加粗星号/结尾句号等）
        return best.strip("*. ").strip()

    @staticmethod
    def _cell(row: list, idx: int) -> str:
        try:
            v = row[idx]
        except IndexError:
            return ""
        return (v or "").replace("\n", " ").strip()

    @staticmethod
    def _clean_reason(text: str) -> str:
        """清理原因列的PDF编码乱码（en-dash被错编码为孤立拉丁字符，仅替换独立出现的）"""
        return re.sub(r"\s+[Câ](?=\s|$)", " –", text or "")

    @staticmethod
    def _col_layout(row: list) -> tuple:
        """按列数判定表结构 -> (原因列, 差异列, 描述起始列)。
        完整表 13列: 4=原因 7=差异 9+=描述；跨页续表缩成 5~7列: 2=原因 3=差异 4+=描述
        （2026 巴库奥迪续页实证：13列→5列，旧固定索引把描述错塞进原因括号位、
        差异/翻译全丢——/upgrades 卡片奥迪后半段裸英文事故）"""
        if len(row) >= 9:
            return 4, 7, 9
        return 2, 3, 4

    def _row_desc(self, row: list) -> str:
        """描述列拼接；完整表从 9 列起，跨页续表从 4 列起（取所有非空尾部单元格）"""
        _, _, desc_start = self._col_layout(row)
        parts = []
        for i in range(desc_start, len(row)):
            v = self._cell(row, i)
            if v:
                parts.append(v)
        return " ".join(parts).strip()

    def _parse_upgrades_table(self, rows: list, gp_name: str,
                              pdf_url: str, out: list, state: dict):
        current = state["current"]
        last_complete = state["last_complete"]
        for row in rows:
            c0 = self._cell(row, 0)
            if c0.isdigit():
                if current:
                    out.append(current)
                reason_idx, diff_idx, _ = self._col_layout(row)
                reason = self._clean_reason(self._cell(row, reason_idx))
                diff = self._cell(row, diff_idx)
                desc = self._row_desc(row)
                # 同一更新包的连续部件：原因/差异/描述为合并单元格(None)，继承上一条
                if not reason and not diff and not desc and last_complete is not None:
                    reason = last_complete["reason"]
                    diff = last_complete["difference"]
                    desc = last_complete["brief_description"]
                current = {
                    "component": self._cell(row, 1),
                    "type": reason,
                    "reason": reason,
                    "difference": diff,
                    "brief_description": desc,
                    "team": state["team"],
                    "team_color": "",
                    "round": 0,
                    "gp_name": gp_name,
                    "gp_city": "",
                    "fia_url": pdf_url,
                    "fia_title": "Car Presentation Submissions",
                    "fia_category": None,
                }
                if reason or diff or desc:
                    last_complete = current
            elif current is not None:
                # 续行：描述列有内容则续写（跳过表头碎片与字数限制说明）
                cont = self._row_desc(row)
                if cont and "min 20" not in cont and "max 100 words" not in cont \
                        and "Brief description" not in cont:
                    current["brief_description"] = (current["brief_description"] + " " + cont).strip()
        state["current"] = current
        state["last_complete"] = last_complete

    # ==================== Cosmos兼容接口（FIA优先，失败回退） ====================

    def _current_event_from_schedule(self, season: int) -> str:
        """从赛程本地推导"FIA 当前文档分站"（免网络，替代抓 FIA 赛季页的 20s 探测）。
        口径：比赛周窗口（正赛前4天~正赛后1天，FIA 通常周四挂出文档）内取该站；
        间隙期取最近一场已完赛分站（间隙期 FIA 页面仍挂上一站文档）。
        双分站撞名（2026 西班牙 R7/R14）由日期窗口消解，比 FIA 抓取更准。"""
        if not self.schedule_provider:
            return ""
        try:
            schedule = self.schedule_provider(season) or []
        except Exception:
            return ""
        from datetime import datetime, timedelta
        today = datetime.now().date()
        last_past = None
        for race in sorted(schedule, key=lambda r: r.get("date") or ""):
            try:
                rd = datetime.strptime(race.get("date", ""), "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            if rd - timedelta(days=4) <= today <= rd + timedelta(days=1):
                return race.get("raceName", "")
            if rd < today - timedelta(days=1):
                last_past = race
            elif rd > today + timedelta(days=1):
                break
        return (last_past or {}).get("raceName", "")

    def _current_event_name(self) -> str:
        """FIA 当前已发布文档的分站名。
        优先赛程本地推导（免网络）；推导不出才抓 FIA 赛季页（带缓存+失败熔断：
        fia.com 从服务器访问不稳，失败一次 10 分钟内不再重复请求）"""
        name = self._current_event_from_schedule(self.season)
        if name:
            return name
        import time as _t
        season = self.season
        cached = self._event_name_cache.get(season)
        if cached:
            ts, val = cached
            ttl = self._EVENT_NAME_OK_TTL if val else self._EVENT_NAME_FAIL_TTL
            if _t.time() - ts < ttl:
                return val or ""
        url = self._season_page_url(season)
        if not url:
            return ""
        result = ""
        try:
            resp = self.session.get(url, timeout=20)
            m = re.search(rf'/system/files/decision-document/{season}_([a-z0-9_]+)_-_', resp.text)
            if m:
                result = m.group(1).replace("_", " ").title()
        except Exception as e:
            logger.warning(f"获取当前分站名失败（熔断10分钟）: {e}")
        self._event_name_cache[season] = (_t.time(), result or None)
        return result

    # ==================== 事件级升级件（支持历史赛季/历史分站） ====================

    def _event_round(self, event_name: str, season: int, city: str = "") -> int:
        """通过注入的赛程提供者补充分站轮次；无提供者或未匹配返回0"""
        if not self.schedule_provider:
            return 0
        from .f1cosmos_api import _norm_gp
        try:
            schedule = self.schedule_provider(season) or []
        except Exception:
            return 0
        en = _norm_gp(event_name)
        city_n = _norm_gp(city)
        for race in schedule:
            rn = _norm_gp(race.get("raceName", ""))
            loc = (race.get("Circuit", {}) or {}).get("Location", {}) or {}
            rc = _norm_gp(loc.get("locality", ""))
            if en and (en == rn or en in rn or rn in en):
                try:
                    return int(race.get("round", 0))
                except (TypeError, ValueError):
                    return 0
            if city_n and rc and city_n == rc:
                try:
                    return int(race.get("round", 0))
                except (TypeError, ValueError):
                    return 0
        return 0

    def get_event_upgrades(self, event_name: str, season: int = None,
                           city: str = "") -> List[Dict[str, Any]]:
        """
        获取指定分站指定年份的升级件申报明细（FIA官方 Car Presentation Submissions）。

        - 当前赛季当前分站：走 get_updates 主流程（FIA优先，Cosmos兜底）
        - 历史赛季/历史分站：抓 FIA 事件页申报文档并解析，结果本地缓存
          （历史赛季永久缓存，当前赛季 6h）；文档未发布/不存在返回空列表
        """
        season = season or self.season
        if not event_name:
            return []
        if season < UPGRADES_MIN_YEAR:
            # FIA 自2024年起才公开发布升级申报文档，更早赛季直接返回空（不抓25个事件页）
            return []

        # 当前赛季当前分站：直接走 FIA 当前文档；申报文档未发布时返回空列表
        # 【重要】这里不能回退 get_updates() 的 Cosmos 全季数据——事件级调用方
        # （如主队个性化推送）期望的是"当站"数据，全季合集会造成错推（2026-09-03 事故）
        if season == self.season:
            cur = self._current_event_name()
            from .f1cosmos_api import _norm_gp
            if cur and (_norm_gp(cur) == _norm_gp(event_name) or _norm_gp(cur) == _norm_gp(city)):
                docs = self.get_event_documents(event_name, season)
                url = self.find_doc_url(docs, "upgrades")
                if url:
                    pdf = self._download_pdf(url)
                    if pdf:
                        rows = self.parse_upgrades_pdf(pdf, gp_name=cur, pdf_url=url)
                        if rows:
                            logger.info(f"✓ 升级件来自FIA当前分站文档（{cur}，{len(rows)}项）")
                            return rows
                return []  # 当前分站申报文档尚未发布

        cache_key = f"fia_event_upgrades_{season}_{re.sub(r'[^a-z0-9]+', '_', event_name.lower()).strip('_')}"
        ttl = 6 if season >= self.season else None  # 历史数据不可变，永久缓存
        if self.cache:
            cached = self.cache.load(cache_key)
            if cached is not None:
                return cached

        items: List[Dict[str, Any]] = []
        fetched_ok = False  # 成功拿到文档列表才允许缓存空结果（网络失败不缓存，避免污染）
        try:
            docs = self.get_event_documents(event_name, season)
            if docs:
                fetched_ok = True
                url = self.find_doc_url(docs, "upgrades")
                if url:
                    pdf = self._download_pdf(url)
                    if pdf:
                        official = self.resolve_event_name(event_name, season) or event_name
                        items = self.parse_upgrades_pdf(pdf, gp_name=official, pdf_url=url)
                        if items:
                            round_num = self._event_round(official, season, city)
                            if round_num:
                                for it in items:
                                    it["round"] = round_num
                            logger.info(f"✓ 升级件来自FIA历史文档（{season} {official}，{len(items)}项）")
        except Exception as e:
            logger.warning(f"FIA事件升级件获取失败({season} {event_name}): {e}")
            return []

        if self.cache and (items or fetched_ok):
            # 空结果可能是文档未发布/该站无申报，短期缓存避免反复抓取；网络失败不缓存
            self.cache.save(cache_key, items, ttl_hours=(ttl if items else 24))
        return items

    def get_season_events_upgrades(self, season: int = None,
                                   max_workers: int = 6) -> List[Dict[str, Any]]:
        """聚合整个赛季各分站升级件（并发抓取，逐站缓存；跳过季前测试）"""
        from concurrent.futures import ThreadPoolExecutor
        season = season or self.season
        if season < UPGRADES_MIN_YEAR:
            return []
        events = [e for e in self.get_season_events(season) if "test" not in e.lower()]
        if not events:
            return []
        all_items: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            for items in pool.map(lambda e: self.get_event_upgrades(e, season), events):
                all_items.extend(items)
        return all_items

    def get_updates(self, season: int = None) -> List[Dict[str, Any]]:
        """
        升级件：当前赛季取FIA最新分站申报文档（含描述备注）；
        历史赛季逐站聚合FIA官方申报文档（FIA赛季页含全部事件，结果逐站永久缓存）。
        注意：历史赛季不再回退Cosmos——其season参数无效，会错误返回当前赛季数据。
        """
        season = season or self.season
        if season != self.season:
            return self.get_season_events_upgrades(season)
        self.no_update_teams = []
        try:
            event = self._current_event_name()
            if event:
                docs = self.get_event_documents(event, season)
                url = self.find_doc_url(docs, "upgrades")
                if url:
                    pdf = self._download_pdf(url)
                    if pdf:
                        rows = self.parse_upgrades_pdf(pdf, gp_name=event, pdf_url=url)
                        if rows:
                            logger.info(f"✓ 升级件数据来自FIA官方文档（{event}，{len(rows)}项，"
                                        f"{len(self.no_update_teams)}支车队未提交）")
                            return rows
            logger.info("FIA升级件文档不可用，回退F1Cosmos")
        except Exception as e:
            logger.warning(f"FIA升级件解析失败，回退F1Cosmos: {e}")
        return super().get_updates(season)

    def _season_updates(self, season: int = None) -> List[Dict[str, Any]]:
        """车队赛季汇总数据源：历史赛季FIA逐站聚合；当前赛季Cosmos全赛季（含轮次/描述）"""
        season = season or self.season
        if season != self.season:
            return self.get_season_events_upgrades(season)
        cosmos = self.get_updates_cosmos(season)
        if cosmos:
            return cosmos
        return self.get_season_events_upgrades(season)

    def get_elements(self, season: int = None) -> List[Dict[str, Any]]:
        """PU用量：FIA最新官方表；历史赛季取该赛季末站的FIA累计用量文档；失败回退Cosmos"""
        season = season or self.season
        if season != self.season:
            return self._get_elements_historical(season)
        try:
            event = self._current_event_name()
            if event:
                docs = self.get_event_documents(event, season)
                url = self.find_doc_url(docs, "pu_used")
                if url:
                    pdf = self._download_pdf(url)
                    if pdf:
                        rows = self.parse_pu_used_pdf(pdf, gp_name=event)
                        if rows:
                            logger.info(f"✓ PU用量数据来自FIA官方文档（{event}，{len(rows)}位车手）")
                            return rows
            logger.info("FIA PU用量文档不可用，回退F1Cosmos")
        except Exception as e:
            logger.warning(f"FIA PU用量解析失败，回退F1Cosmos: {e}")
        return super().get_elements(season)

    def _last_event_of_season(self, season: int) -> Optional[str]:
        """赛季最后一站的FIA事件名（历史PU"赛季末累计"快照用）；需赛程提供者定序"""
        events = [e for e in self.get_season_events(season) if "test" not in e.lower()]
        if not events or not self.schedule_provider:
            return None
        from .f1cosmos_api import _norm_gp
        try:
            schedule = self.schedule_provider(season) or []
        except Exception:
            return None
        best, best_round = None, -1
        for race in schedule:
            rn = _norm_gp(race.get("raceName", ""))
            try:
                rd = int(race.get("round", 0))
            except (TypeError, ValueError):
                continue
            for e in events:
                en = _norm_gp(e)
                if en and (en == rn or en in rn or rn in en) and rd > best_round:
                    best, best_round = e, rd
        return best

    def _get_elements_historical(self, season: int) -> List[Dict[str, Any]]:
        """历史赛季PU用量：该赛季最后一站的 FIA 'PU elements used per driver up to now' 文档
        （赛季末累计快照，实证 FIA 2019 年起有此类文档；结果永久缓存）
        缓存键带解析器版本：v1（固定2026列序）解析的旧布局数据有误，v2 起为表头驱动"""
        cache_key = f"fia_pu_used_v2_{season}_final"
        if self.cache:
            cached = self.cache.load(cache_key)
            if cached is not None:
                return cached
        rows: List[Dict[str, Any]] = []
        fetched_ok = False  # 只有成功拿到文档列表才允许缓存空结果（网络失败不缓存，避免污染）
        try:
            event = self._last_event_of_season(season)
            if event:
                docs = self.get_event_documents(event, season)
                if docs:
                    fetched_ok = True
                    url = self.find_doc_url(docs, "pu_used")
                    if url:
                        pdf = self._download_pdf(url)
                        if pdf:
                            rows = self.parse_pu_used_pdf(pdf, gp_name=event)
                            if rows:
                                logger.info(f"✓ PU用量来自FIA历史文档（{season}赛季末 {event}，{len(rows)}位车手）")
        except Exception as e:
            logger.warning(f"FIA历史PU文档获取失败({season}): {e}")
        if self.cache and (rows or fetched_ok):
            # 空结果仅当确认文档列表可获取（该赛季可能确实无此文档）才短期缓存
            self.cache.save(cache_key, rows, ttl_hours=(None if rows else 24))
        return rows

    def detect_pu_changes(self, season: int = None) -> List[Dict[str, Any]]:
        """
        PU部件更换：直接读FIA「本站新增PU元件」文档（官方权威，无需快照对比）。
        输出形状与Cosmos版一致: [{"driver": 姓, "team": 车队, "diffs": ["ICE 3->4"]}]
        文档不存在（未发布）时返回空列表
        """
        season = season or self.season
        try:
            event = self._current_event_name()
            if not event:
                return []
            docs = self.get_event_documents(event, season)
            urls = self.find_doc_urls(docs, "pu_new")
            if not urls:
                logger.info("FIA新增PU文档尚未发布")
                return []
            # 增补版(_0/_1)与初版是不同编号的增量文档，全部解析并按(车手,部件)合并取最新
            merged: Dict[tuple, Dict[str, Any]] = {}
            for url in urls:
                pdf = self._download_pdf(url)
                if not pdf:
                    continue
                for it in self.parse_pu_new_pdf(pdf):
                    key = (it["last_name"] or it["driver"], it["element"])
                    if key not in merged or it["prev"] >= merged[key]["prev"]:
                        merged[key] = it
            grouped: Dict[str, Dict[str, Any]] = {}
            for it in merged.values():
                key = it["last_name"] or it["driver"]
                g = grouped.setdefault(key, {"driver": key, "team": it["team"], "diffs": []})
                g["diffs"].append(f"{it['element']} {it['prev']}->{it['now']}")
            if grouped:
                logger.info(f"✓ 新增PU数据来自FIA官方文档（{event}，{len(grouped)}位车手）")
            return list(grouped.values())
        except Exception as e:
            logger.warning(f"FIA新增PU解析失败，回退快照对比: {e}")
            return super().detect_pu_changes(season)
