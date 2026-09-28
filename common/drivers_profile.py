"""
F1赛程提醒机器人 - 车手/车队档案（统一数据源）

数据源优先级：data/drivers_profile.json（可修改 profile，随部署包分发）
             -> 本模块内嵌默认值（文件缺失/损坏时兜底）

profile 结构：
{
  "_meta": {"season": 2026, "last_verified": "2026-09-07"},
  "drivers": {
    "<driver_id>": {
      "name_en": "Max Verstappen",      # 标准英文全名
      "last_name": "Verstappen",        # F1Cosmos 风格姓
      "name_cn": "维斯塔潘",             # 标准中文名
      "aliases": ["潘子"],               # 中文别名/昵称
      "team": "red_bull",               # 车队 id
      "roster_status": "regular"        # regular/rookie/reserve/development
    }
  },
  "teams": {
    "<team_id>": {"name_en": "Red Bull Racing", "aliases": ["红牛"]}
  }
}

维护窗口（重要）：赛季结束后至次年开赛前为关键维护期——车手市场变动
（转会/新秀晋升/替补签约）集中在该时段。赛季初加载时若 profile 赛季
与当前赛季不符或 last_verified 过旧，needs_review() 返回 True，
main.py 启动时输出警告日志提醒人工/AI 联网核查更新。

roster_status 说明：
- regular     正式车手（含一年级新生中已确认为正式席位的）
- rookie      新秀车手（首个 F1 赛季的正式车手，可与 regular 语义区分用于追踪）
- reserve     替补/后备车手
- development 发展/青训车手
"""

import json
import logging
import os
import threading
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

_PROFILE_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "data", "drivers_profile.json")

# ==================== 内嵌默认值（2026 赛季，profile 文件缺失时兜底） ====================

_DEFAULT_META = {"season": 2026, "last_verified": "2026-09-09"}

_DEFAULT_DRIVERS: Dict[str, Dict[str, Any]] = {
    "max_verstappen": {"name_en": "Max Verstappen", "last_name": "Verstappen",
                       "name_cn": "维斯塔潘", "aliases": ["潘子"],
                       "team": "red_bull", "roster_status": "regular"},
    "hadjar": {"name_en": "Isack Hadjar", "last_name": "Hadjar",
               "name_cn": "哈贾尔", "aliases": [],
               "team": "red_bull", "roster_status": "reserve", "verified": False,
               "note": "2026季中被换下（劳森接替大红牛席位），是否留任储备车手待核实"},
    "lawson": {"name_en": "Liam Lawson", "last_name": "Lawson",
               "name_cn": "劳森", "aliases": [],
               "team": "red_bull", "roster_status": "regular",
               "note": "2026季中由小红牛升入大红牛（接替哈贾尔）"},
    "arvid_lindblad": {"name_en": "Arvid Lindblad", "last_name": "Lindblad",
                       "name_cn": "林德布拉德", "aliases": [],
                       "team": "rb", "roster_status": "rookie"},
    "leclerc": {"name_en": "Charles Leclerc", "last_name": "Leclerc",
                "name_cn": "勒克莱尔", "aliases": ["乐扣"],
                "team": "ferrari", "roster_status": "regular"},
    "hamilton": {"name_en": "Lewis Hamilton", "last_name": "Hamilton",
                 "name_cn": "汉密尔顿", "aliases": ["老汉"],
                 "team": "ferrari", "roster_status": "regular"},
    "russell": {"name_en": "George Russell", "last_name": "Russell",
                "name_cn": "拉塞尔", "aliases": [],
                "team": "mercedes", "roster_status": "regular"},
    "antonelli": {"name_en": "Andrea Kimi Antonelli", "last_name": "Antonelli",
                  "name_cn": "安东内利", "aliases": ["小Kimi", "kimi"],
                  "team": "mercedes", "roster_status": "regular"},
    "norris": {"name_en": "Lando Norris", "last_name": "Norris",
               "name_cn": "诺里斯", "aliases": [],
               "team": "mclaren", "roster_status": "regular"},
    "piastri": {"name_en": "Oscar Piastri", "last_name": "Piastri",
                "name_cn": "皮亚斯特里", "aliases": [],
                "team": "mclaren", "roster_status": "regular"},
    "alonso": {"name_en": "Fernando Alonso", "last_name": "Alonso",
               "name_cn": "阿隆索", "aliases": ["头哥"],
               "team": "aston_martin", "roster_status": "regular"},
    "stroll": {"name_en": "Lance Stroll", "last_name": "Stroll",
               "name_cn": "斯特罗尔", "aliases": ["少爷"],
               "team": "aston_martin", "roster_status": "regular"},
    "gasly": {"name_en": "Pierre Gasly", "last_name": "Gasly",
              "name_cn": "加斯利", "aliases": [],
              "team": "alpine", "roster_status": "regular"},
    "colapinto": {"name_en": "Franco Colapinto", "last_name": "Colapinto",
                  "name_cn": "科拉平托", "aliases": [],
                  "team": "alpine", "roster_status": "regular"},
    "albon": {"name_en": "Alexander Albon", "last_name": "Albon",
              "name_cn": "阿尔本", "aliases": [],
              "team": "williams", "roster_status": "regular"},
    "sainz": {"name_en": "Carlos Sainz", "last_name": "Sainz",
              "name_cn": "塞恩斯", "aliases": ["赛恩斯"],
              "team": "williams", "roster_status": "regular"},
    "hulkenberg": {"name_en": "Nico Hulkenberg", "last_name": "Hulkenberg",
                   "name_cn": "霍肯伯格", "aliases": ["霍肯博格"],
                   "team": "audi", "roster_status": "regular"},
    "bortoleto": {"name_en": "Gabriel Bortoleto", "last_name": "Bortoleto",
                  "name_cn": "博托莱托", "aliases": [],
                  "team": "audi", "roster_status": "regular"},
    "ocon": {"name_en": "Esteban Ocon", "last_name": "Ocon",
             "name_cn": "奥康", "aliases": [],
             "team": "haas", "roster_status": "regular"},
    "bearman": {"name_en": "Oliver Bearman", "last_name": "Bearman",
                "name_cn": "贝尔曼", "aliases": [],
                "team": "haas", "roster_status": "regular"},
    "perez": {"name_en": "Sergio Perez", "last_name": "Perez",
              "name_cn": "佩雷兹", "aliases": ["佩雷斯"],
              "team": "cadillac", "roster_status": "regular"},
    "bottas": {"name_en": "Valtteri Bottas", "last_name": "Bottas",
               "name_cn": "博塔斯", "aliases": [],
               "team": "cadillac", "roster_status": "regular"},
    # ---- 替补/发展车手（待核实项见各 note 字段；维护窗口内重点核查） ----
    "tsunoda": {"name_en": "Yuki Tsunoda", "last_name": "Tsunoda",
                "name_cn": "角田裕毅", "aliases": ["角田"],
                "team": "rb", "roster_status": "regular",
                "note": "2026季中回归小红牛（接替劳森席位）"},
    "zhou": {"name_en": "Zhou Guanyu", "last_name": "Zhou",
             "name_cn": "周冠宇", "aliases": [],
             "team": "cadillac", "roster_status": "reserve", "verified": False},
    "doohan": {"name_en": "Jack Doohan", "last_name": "Doohan",
               "name_cn": "杜汉", "aliases": [],
               "team": "alpine", "roster_status": "reserve", "verified": False},
}

_DEFAULT_TEAMS: Dict[str, Dict[str, Any]] = {
    "red_bull": {"name_en": "Red Bull Racing", "aliases": ["红牛"]},
    "rb": {"name_en": "Racing Bulls", "aliases": ["小红牛", "二牛", "racing bulls"]},
    "ferrari": {"name_en": "Ferrari", "aliases": ["法拉利", "跃马"]},
    "mercedes": {"name_en": "Mercedes", "aliases": ["梅赛德斯", "奔驰", "梅奔"]},
    "mclaren": {"name_en": "McLaren", "aliases": ["迈凯伦", "迈凯轮"]},
    "aston_martin": {"name_en": "Aston Martin", "aliases": ["阿斯顿马丁", "马丁"]},
    "alpine": {"name_en": "Alpine", "aliases": ["阿尔派", "alpine"]},
    "williams": {"name_en": "Williams", "aliases": ["威廉姆斯"]},
    "audi": {"name_en": "Audi", "aliases": ["奥迪", "索伯", "索博"]},
    "haas": {"name_en": "Haas", "aliases": ["哈斯"]},
    "cadillac": {"name_en": "Cadillac", "aliases": ["凯迪拉克"]},
}

_lock = threading.Lock()
_cache: Dict[str, Any] = {"mtime": 0, "profile": None}

# 维护窗口：last_verified 早于该日期视为待核查（次年3月1日前必须完成赛季初核查）
_REVIEW_MONTH, _REVIEW_DAY = 3, 1


def _load_profile() -> Dict[str, Any]:
    """读取 profile（带 mtime 缓存）；文件缺失/损坏时优先保留内存中最后一份完好副本，
    均无则回退内嵌默认值（深拷贝，防调用方原地修改污染模块级默认）"""
    import copy
    global _cache
    try:
        mtime = os.path.getmtime(_PROFILE_FILE)
        with _lock:
            if _cache["profile"] is not None and mtime == _cache["mtime"]:
                return _cache["profile"]
            try:
                with open(_PROFILE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data.get("drivers"), dict):
                    raise ValueError("drivers 字段缺失或格式错误")
            except Exception as e:
                # 文件损坏：有 last-good 副本则沿用（告警一次），避免每次调用重读+告警刷屏
                if _cache["profile"] is not None:
                    logger.warning(f"drivers_profile.json 读取失败，沿用内存中最后一份完好副本: {e}")
                    _cache["mtime"] = mtime  # 标记该 mtime 已告警
                    return _cache["profile"]
                raise
            _cache = {"mtime": mtime, "profile": data}
            return data
    except FileNotFoundError:
        logger.warning("drivers_profile.json 不存在，使用内嵌默认档案")
    except Exception as e:
        logger.warning(f"读取 drivers_profile.json 失败，使用内嵌默认值: {e}")
    return {"_meta": dict(_DEFAULT_META),
            "drivers": copy.deepcopy(_DEFAULT_DRIVERS),
            "teams": copy.deepcopy(_DEFAULT_TEAMS)}


def reload_profile():
    """强制下次访问时重读文件（外部修改 profile 后调用）"""
    global _cache
    with _lock:
        _cache = {"mtime": 0, "profile": None}


def get_meta() -> Dict[str, Any]:
    meta = _load_profile().get("_meta")
    return dict(meta) if isinstance(meta, dict) else dict(_DEFAULT_META)


def get_drivers() -> Dict[str, Dict[str, Any]]:
    return _load_profile()["drivers"]


def get_teams() -> Dict[str, Dict[str, Any]]:
    import copy
    return _load_profile().get("teams") or copy.deepcopy(_DEFAULT_TEAMS)


def get_driver_en_map() -> Dict[str, str]:
    """driver_id -> 标准英文全名（llm_assistant 姓名锚定用）"""
    return {did: d["name_en"] for did, d in get_drivers().items() if d.get("name_en")}


def get_team_en_map() -> Dict[str, str]:
    """team_id -> 标准英文名"""
    return {tid: t["name_en"] for tid, t in get_teams().items() if t.get("name_en")}


def get_known_driver_names() -> List[str]:
    """现役车手全名列表（fia_docs PU 表格切分用），最长后缀优先"""
    names = [d["name_en"] for d in get_drivers().values() if d.get("name_en")]
    # Kimi Antonelli 需同时提供短名（FIA 表格两种写法都出现过）
    extra = []
    for n in names:
        if n == "Andrea Kimi Antonelli":
            extra.append("Kimi Antonelli")
    return sorted(names + extra, key=len, reverse=True)


def get_driver_aliases() -> Dict[str, str]:
    """中文名/别名 -> driver_id（user_prefs 偏好匹配用）"""
    out: Dict[str, str] = {}
    for did, d in get_drivers().items():
        if d.get("name_cn"):
            out.setdefault(d["name_cn"], did)
        for a in d.get("aliases") or []:
            out.setdefault(a, did)
    return out


def get_driver_lastname_aliases() -> Dict[str, str]:
    """中文名/别名 -> last_name（f1cosmos 匹配用）"""
    out: Dict[str, str] = {}
    for d in get_drivers().values():
        ln = d.get("last_name")
        if not ln:
            continue
        if d.get("name_cn"):
            out.setdefault(d["name_cn"], ln)
        for a in d.get("aliases") or []:
            out.setdefault(a, ln)
    return out


def get_team_aliases() -> Dict[str, str]:
    """中文名/别名 -> team_id"""
    out: Dict[str, str] = {}
    for tid, t in get_teams().items():
        for a in t.get("aliases") or []:
            out.setdefault(a, tid)
    return out


def get_roster(*statuses: str) -> Dict[str, Dict[str, Any]]:
    """按 roster_status 过滤车手；不传参返回全部。
    用法：get_roster('reserve', 'development') -> 替补+发展车手"""
    drivers = get_drivers()
    if not statuses:
        return drivers
    return {did: d for did, d in drivers.items()
            if d.get("roster_status", "regular") in statuses}


def needs_review(current_season: int) -> bool:
    """赛季初核查判断：profile 赛季落后 或 last_verified 早于当年3月1日"""
    from datetime import date
    meta = get_meta()
    if int(meta.get("season", 0)) != int(current_season):
        return True
    try:
        verified = date.fromisoformat(str(meta.get("last_verified", "2000-01-01")))
        return verified < date(int(current_season), _REVIEW_MONTH, _REVIEW_DAY)
    except ValueError:
        return True


# ==================== 代打车手自动注册 ====================
# 积分榜出现档案外车手时（季中伤病代打，如吉奥维纳兹代打勒克莱尔），
# 自动注册为 substitute：姓名锚定/新闻定向/PU 兜底即刻生效，无需人工改档案。
# 仅当前赛季触发（历史赛季含大量退役车手，不注册）。

def _registered_keys() -> set:
    """档案内全部标识集合（key + name_en 小写）"""
    keys = set()
    for did, d in get_drivers().items():
        keys.add(did.lower())
        if d.get("name_en"):
            keys.add(d["name_en"].strip().lower())
    return keys


def auto_register_substitutes(entries: List[Dict[str, Any]]) -> List[str]:
    """积分榜条目中的档案外车手自动注册为 substitute（仅当前赛季调用）

    Args:
        entries: Ergast 格式 DriverStandings 列表
    Returns:
        新注册车手全名列表（无新增返回 []）
    """
    known = _registered_keys()
    newcomers: List[Dict[str, str]] = []
    for e in entries:
        d = e.get("Driver") or {}
        did = (d.get("driverId") or "").strip()
        name_en = f"{d.get('givenName', '')} {d.get('familyName', '')}".strip()
        if not did or not name_en:
            continue
        if did.lower() in known or name_en.lower() in known:
            continue
        team = ""
        ctors = e.get("Constructors") or []
        if ctors and isinstance(ctors[0], dict):
            team = ctors[0].get("name", "")
        newcomers.append({"id": did, "name_en": name_en,
                          "last_name": d.get("familyName", ""), "team": team})
        known.add(did.lower())
        known.add(name_en.lower())

    if not newcomers:
        return []

    # 原子写回 profile 文件
    try:
        try:
            with open(_PROFILE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data.get("drivers"), dict):
                raise ValueError("drivers 字段缺失")
        except Exception:
            # 文件不存在/损坏：以当前内存中的档案为底（含内嵌默认回退）
            data = {"_meta": get_meta(), "drivers": get_drivers(),
                    "teams": get_teams()}
        from datetime import date
        today = date.today().isoformat()
        for n in newcomers:
            data["drivers"][n["id"]] = {
                "name_en": n["name_en"], "last_name": n["last_name"],
                "name_cn": "", "aliases": [], "team": n["team"],
                "roster_status": "substitute", "verified": False,
                "note": "自动注册（积分榜出现档案外车手，疑季中代打），待人工核实中文名",
                "registered_at": today,
            }
        tmp = _PROFILE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _PROFILE_FILE)
        reload_profile()  # 缓存立即失效，下游（锚定表/别名/新闻匹配）即刻生效
        names = [n["name_en"] for n in newcomers]
        logger.warning(f"⚠️ 积分榜发现档案外车手，已自动注册为 substitute（待人工核实）: "
                       + ", ".join(names))
        return names
    except Exception as e:
        logger.warning(f"代打车手自动注册失败: {e}")
        return []


def log_review_reminder(current_season: int):
    """启动时调用：profile 待核查时输出警告日志（赛季初拉取最新阵容信息更新）"""
    if needs_review(current_season):
        meta = get_meta()
        logger.warning(
            f"⚠️ drivers_profile 待核查：profile赛季={meta.get('season')} "
            f"last_verified={meta.get('last_verified')}，当前赛季={current_season}。"
            f"请拉取最新车手阵容（正式/新秀/替补/发展）更新 data/drivers_profile.json"
        )
