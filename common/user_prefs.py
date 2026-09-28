"""
F1赛程提醒机器人 - 群成员偏好存储
保存每个群成员的主队/最喜爱车手设置，支持中英文模糊匹配
"""

import json
import os
import logging
import threading
from typing import Dict, Any, Optional, Tuple, List

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
PREFS_FILE = os.path.join(DATA_DIR, "user_prefs.json")

from .drivers_profile import get_driver_aliases, get_team_aliases

# 车手/车队中文别名表：数据源 data/drivers_profile.json（common/drivers_profile.py），
# 此处为模块加载时快照；profile 更新后重启或调 drivers_profile.reload_profile() 生效
DRIVER_ALIASES = get_driver_aliases()
TEAM_ALIASES = get_team_aliases()


class UserPrefsStore:
    """群成员偏好存储（JSON文件，线程安全）"""

    def __init__(self, file_path: str = None):
        self.file_path = file_path or PREFS_FILE
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        self._data = self._load()
        self._migrate_legacy()

    def _load(self) -> Dict[str, Any]:
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"读取偏好文件失败: {e}")
        return {}

    def _migrate_legacy(self):
        """
        迁移旧命名空间到统一的 users 维度：
        - 群维度 {group_openid: {member_openid: prefs}} → users[member_openid]
        - dm_users {user_openid: prefs} → users[user_openid]
        （author.id 跨群/私聊统一，member_openid == user_openid == id）
        """
        changed = False
        users = self._data.setdefault("users", {})

        # 群维度（顶层 key 是 group_openid，value 是 {member_openid: prefs}）
        for key in list(self._data.keys()):
            if key in ("users", "dm_users"):
                continue
            group_data = self._data[key]
            if isinstance(group_data, dict):
                for member_openid, prefs in group_data.items():
                    if isinstance(prefs, dict) and prefs:
                        users.setdefault(member_openid, {}).update(prefs)
                del self._data[key]
                changed = True

        # dm_users 维度
        dm = self._data.pop("dm_users", {})
        for user_openid, prefs in dm.items():
            if isinstance(prefs, dict) and prefs:
                users.setdefault(user_openid, {}).update(prefs)
        if dm:
            changed = True

        if changed:
            self._save()
            logger.info(f"✓ 用户偏好已迁移到统一 users 维度（{len(users)} 位用户）")

    def _save(self):
        """原子写：临时文件+rename，防崩溃留下半截JSON导致偏好全丢"""
        try:
            tmp_path = self.file_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.file_path)
        except Exception as e:
            logger.error(f"保存偏好文件失败: {e}")

    def set_driver(self, group_openid: str, member_openid: str, driver_id: str, driver_name: str):
        with self._lock:
            prefs = self._data.setdefault(group_openid, {}).setdefault(member_openid, {})
            prefs["driver"] = driver_id
            prefs["driver_name"] = driver_name
            self._save()

    def set_team(self, group_openid: str, member_openid: str, team_id: str, team_name: str):
        with self._lock:
            prefs = self._data.setdefault(group_openid, {}).setdefault(member_openid, {})
            prefs["team"] = team_id
            prefs["team_name"] = team_name
            self._save()

    def get_member_prefs(self, group_openid: str, member_openid: str) -> Dict[str, Any]:
        return self._data.get(group_openid, {}).get(member_openid, {})

    def get_group_prefs(self, group_openid: str) -> Dict[str, Dict[str, Any]]:
        return self._data.get(group_openid, {})

    # ==================== 私聊用户偏好（dm_users 命名空间） ====================
    # 注意：私聊 user_openid 与群内 member_openid 是两套标识，偏好不互通

    def set_dm_driver(self, user_openid: str, driver_id: str, driver_name: str):
        with self._lock:
            prefs = self._data.setdefault("dm_users", {}).setdefault(user_openid, {})
            prefs["driver"] = driver_id
            prefs["driver_name"] = driver_name
            self._save()

    def set_dm_team(self, user_openid: str, team_id: str, team_name: str):
        with self._lock:
            prefs = self._data.setdefault("dm_users", {}).setdefault(user_openid, {})
            prefs["team"] = team_id
            prefs["team_name"] = team_name
            self._save()

    def get_dm_prefs(self, user_openid: str) -> Dict[str, Any]:
        return self._data.get("dm_users", {}).get(user_openid, {})

    def get_all_dm_prefs(self) -> Dict[str, Dict[str, Any]]:
        """所有私聊用户的偏好 {user_openid: prefs}"""
        return self._data.get("dm_users", {})

    # ==================== 统一用户偏好（users 命名空间） ====================
    # author.id 是跨群、跨私聊统一的用户标识（member_openid == user_openid == id）
    # 群聊和私聊的偏好统一存到这里，实现数据互通
    #
    # 多关注对象（2026-09-09 扩容）：drivers/teams 为 id 列表（各类上限 MAX_WATCH=3），
    # *_names 为 {id: 显示名}。旧单值键 driver/team/driver_name/team_name 保留
    # 同步（=列表首元素），供未升级的旧读取方兼容。

    MAX_WATCH = 3  # 每类关注对象上限（车手/车队各 3）

    def set_user_driver(self, user_id: str, driver_id: str, driver_name: str):
        """添加关注车手（多值，上限 MAX_WATCH；重复设置同一人为幂等）"""
        with self._lock:
            pref = self._data.setdefault("users", {}).setdefault(user_id, {})
            drivers = pref.setdefault("drivers", [])
            names = pref.setdefault("driver_names", {})
            if pref.get("driver") and pref["driver"] not in drivers:
                drivers.insert(0, pref["driver"])  # 旧单值迁移入列表
                if pref.get("driver_name"):
                    names.setdefault(pref["driver"], pref["driver_name"])
            if driver_id in drivers:
                names[driver_id] = driver_name
                pref["driver"], pref["driver_name"] = drivers[0], names.get(drivers[0], driver_name)
                self._save()
                return True
            if len(drivers) >= self.MAX_WATCH:
                return False
            drivers.append(driver_id)
            names[driver_id] = driver_name
            pref["driver"], pref["driver_name"] = drivers[0], names.get(drivers[0], driver_name)
            self._save()
            return True

    def set_user_team(self, user_id: str, team_id: str, team_name: str):
        """添加关注车队（多值，上限 MAX_WATCH）"""
        with self._lock:
            pref = self._data.setdefault("users", {}).setdefault(user_id, {})
            teams = pref.setdefault("teams", [])
            names = pref.setdefault("team_names", {})
            if pref.get("team") and pref["team"] not in teams:
                teams.insert(0, pref["team"])
                if pref.get("team_name"):
                    names.setdefault(pref["team"], pref["team_name"])
            if team_id in teams:
                names[team_id] = team_name
                pref["team"], pref["team_name"] = teams[0], names.get(teams[0], team_name)
                self._save()
                return True
            if len(teams) >= self.MAX_WATCH:
                return False
            teams.append(team_id)
            names[team_id] = team_name
            pref["team"], pref["team_name"] = teams[0], names.get(teams[0], team_name)
            self._save()
            return True

    def get_user_prefs(self, user_id: str) -> Dict[str, Any]:
        return self._data.get("users", {}).get(user_id, {})

    @staticmethod
    def pref_drivers(pref: Dict[str, Any]) -> List[str]:
        """规范化读取关注车手 id 列表（兼容旧单值键）"""
        drivers = list(pref.get("drivers") or [])
        if pref.get("driver") and pref["driver"] not in drivers:
            drivers.insert(0, pref["driver"])
        return drivers

    @staticmethod
    def pref_teams(pref: Dict[str, Any]) -> List[str]:
        teams = list(pref.get("teams") or [])
        if pref.get("team") and pref["team"] not in teams:
            teams.insert(0, pref["team"])
        return teams

    @staticmethod
    def pref_driver_names(pref: Dict[str, Any]) -> Dict[str, str]:
        """{driver_id: 显示名}（兼容旧单值键）"""
        names = dict(pref.get("driver_names") or {})
        if pref.get("driver") and pref.get("driver_name"):
            names.setdefault(pref["driver"], pref["driver_name"])
        return names

    @staticmethod
    def pref_team_names(pref: Dict[str, Any]) -> Dict[str, str]:
        names = dict(pref.get("team_names") or {})
        if pref.get("team") and pref.get("team_name"):
            names.setdefault(pref["team"], pref["team_name"])
        return names

    def remove_user_driver(self, user_id: str, driver_id: str) -> bool:
        """移除单个关注车手（多值结构 + 旧单值键同步）"""
        with self._lock:
            pref = self._data.get("users", {}).get(user_id)
            if not pref:
                return False
            drivers = pref.get("drivers") or []
            if driver_id not in drivers and pref.get("driver") != driver_id:
                return False
            if driver_id in drivers:
                drivers.remove(driver_id)
            (pref.get("driver_names") or {}).pop(driver_id, None)
            # 旧单值键同步为首元素
            if drivers:
                pref["driver"] = drivers[0]
                pref["driver_name"] = (pref.get("driver_names") or {}).get(drivers[0], drivers[0])
            else:
                pref.pop("driver", None)
                pref.pop("driver_name", None)
                pref.pop("drivers", None)
                pref.pop("driver_names", None)
            if not pref:
                del self._data["users"][user_id]
            self._save()
            return True

    def remove_user_team(self, user_id: str, team_id: str) -> bool:
        """移除单个关注车队"""
        with self._lock:
            pref = self._data.get("users", {}).get(user_id)
            if not pref:
                return False
            teams = pref.get("teams") or []
            if team_id not in teams and pref.get("team") != team_id:
                return False
            if team_id in teams:
                teams.remove(team_id)
            (pref.get("team_names") or {}).pop(team_id, None)
            if teams:
                pref["team"] = teams[0]
                pref["team_name"] = (pref.get("team_names") or {}).get(teams[0], teams[0])
            else:
                pref.pop("team", None)
                pref.pop("team_name", None)
                pref.pop("teams", None)
                pref.pop("team_names", None)
            if not pref:
                del self._data["users"][user_id]
            self._save()
            return True

    def unset_user_pref(self, user_id: str, *kinds: str) -> int:
        """解绑偏好：kinds 为 'driver'/'team'，返回实际解绑的项数（0=无绑定）。
        多值结构（drivers/teams 列表）与旧单值键一并清理。"""
        with self._lock:
            pref = self._data.get("users", {}).get(user_id)
            if not pref:
                return 0
            removed = 0
            for kind in kinds:
                keys = [kind, f"{kind}_name", f"{kind}s", f"{kind}_names"]
                had = any(k in pref for k in keys)
                for k in keys:
                    pref.pop(k, None)
                if had:
                    removed += 1
            # 剩余键只剩开关类（news_dm 等）时保留条目
            if not pref:
                del self._data["users"][user_id]
            if removed:
                self._save()
            return removed

    # ---- 新闻 DM 开关（news_dm 键，缺省 True：有偏好的用户默认接收定向新闻） ----

    def set_news_dm(self, user_id: str, enabled: bool):
        with self._lock:
            pref = self._data.setdefault("users", {}).setdefault(user_id, {})
            pref["news_dm"] = bool(enabled)
            self._save()

    def news_dm_enabled(self, user_id: str) -> bool:
        return self.get_user_prefs(user_id).get("news_dm", True)

    def get_all_user_prefs(self) -> Dict[str, Dict[str, Any]]:
        """所有用户的偏好 {user_id: prefs}（用于赛后私聊个性化推送）"""
        return self._data.get("users", {})


def _normalize(text: str) -> str:
    return text.strip().lower().replace(" ", "").replace("-", "_")


def resolve_driver(query: str, driver_entries: List[Dict[str, Any]]) -> Optional[Tuple[str, str]]:
    """
    模糊匹配车手
    Args:
        query: 用户输入（中文名/英文名/代号）
        driver_entries: Ergast格式 DriverStandings 列表
    Returns:
        (driverId, 显示名) 或 None
    """
    q = _normalize(query)

    if query.strip() in DRIVER_ALIASES:
        target = DRIVER_ALIASES[query.strip()]
        for e in driver_entries:
            if e.get("Driver", {}).get("driverId") == target:
                d = e["Driver"]
                return target, f"{d.get('givenName', '')} {d.get('familyName', '')}".strip()
        return target, target

    for e in driver_entries:
        d = e.get("Driver", {})
        candidates = [
            d.get("driverId", ""),
            d.get("familyName", ""),
            d.get("givenName", ""),
            f"{d.get('givenName', '')}{d.get('familyName', '')}",
            d.get("code", ""),
        ]
        for c in candidates:
            cn = _normalize(c)
            if cn and (cn == q or q in cn):
                return d.get("driverId"), f"{d.get('givenName', '')} {d.get('familyName', '')}".strip()
    return None


def resolve_team(query: str, team_entries: List[Dict[str, Any]]) -> Optional[Tuple[str, str]]:
    """
    模糊匹配车队
    Args:
        query: 用户输入（中文名/英文名）
        team_entries: Ergast格式 ConstructorStandings 列表
    Returns:
        (constructorId, 显示名) 或 None
    """
    q = _normalize(query)

    if query.strip() in TEAM_ALIASES:
        target = TEAM_ALIASES[query.strip()]
        for e in team_entries:
            if e.get("Constructor", {}).get("constructorId") == target:
                return target, e["Constructor"].get("name", target)
        return target, target

    for e in team_entries:
        c = e.get("Constructor", {})
        candidates = [c.get("constructorId", ""), c.get("name", "")]
        for cand in candidates:
            cn = _normalize(cand)
            if cn and (cn == q or q in cn):
                return c.get("constructorId"), c.get("name")
    return None
