"""
F1赛程提醒机器人 - 车手评分存储（Driver of the Day 投票）

数据结构 data/ratings.json：
{
  "races": {
    "2026-R12": {
      "race_name": "Dutch Grand Prix",
      "season": 2026,
      "round": 12,
      "tokens": {"a8f3...": {"member_openid": "...", "created": 1234.5}},
      "votes": {"member_openid": {"driver_id": 9, ...}},
      "closed": false,
      "dotd": null
    }
  },
  "season_dotd": {"2026": {"verstappen": 3, "norris": 2}}
}

规则：
- 一人一票制：member_openid 为主键，重复提交=改票
- token 绑定 member_openid，专属链接防冒名
- DOTD 需 >=2 票才有效
"""

import json
import logging
import os
import secrets
import threading
import time
from contextlib import contextmanager
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)

DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
RATINGS_FILE = os.path.join(DATA_DIR, "ratings.json")
EXPORTS_DIR = os.path.join(DATA_DIR, "exports")

DOTD_MIN_VOTES = 2


class RatingsStore:
    """车手评分存储（JSON文件，线程安全+跨进程文件锁）

    bot 容器与 web 容器共享 ratings.json：所有 读取-修改-写入 事务除进程内
    RLock 外，再持文件锁（.lock 附加创建），防双进程交错写导致丢票/丢标记。
    文件锁带 30s 陈旧兜底，进程崩溃遗留的锁文件不会永久卡死。
    """

    _FILELOCK_STALE_SEC = 30

    def __init__(self, file_path: str = None):
        self.file_path = file_path or RATINGS_FILE
        self._lock = threading.RLock()  # 可重入锁（close_race内会调用aggregate等加锁方法）
        self._flock_path = self.file_path + ".lock"
        os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
        os.makedirs(EXPORTS_DIR, exist_ok=True)
        self._data = self._load()

    # ---- 跨进程文件锁（无第三方依赖：O_CREAT|O_EXCL 附加创建） ----

    def _acquire_filelock(self, timeout: float = 10.0):
        import time as _t
        deadline = _t.time() + timeout
        while True:
            try:
                fd = os.open(self._flock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return True
            except FileExistsError:
                # 陈旧锁兜底：超时未释放（持锁进程崩溃）则强制接管
                try:
                    if _t.time() - os.path.getmtime(self._flock_path) > self._FILELOCK_STALE_SEC:
                        os.remove(self._flock_path)
                        continue
                except OSError:
                    pass
                if _t.time() > deadline:
                    logger.warning("评分文件锁获取超时，降级为无锁写入")
                    return False
                _t.sleep(0.05)
            except OSError:
                return False

    def _release_filelock(self, held: bool):
        if held:
            try:
                os.remove(self._flock_path)
            except OSError:
                pass

    @contextmanager
    def _xlock(self):
        """进程内锁 + 跨进程文件锁 组合临界区"""
        self._lock.acquire()
        held = self._acquire_filelock()
        try:
            yield
        finally:
            self._release_filelock(held)
            self._lock.release()

    def _load(self) -> Dict[str, Any]:
        if os.path.exists(self.file_path):
            try:
                with open(self.file_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.error(f"读取评分文件失败: {e}")
        return {"races": {}, "season_dotd": {}}

    def _reload(self):
        """跨进程场景下读取前刷新内存数据（bot容器与web容器共享ratings.json）"""
        with self._lock:
            self._data = self._load()

    def _save(self):
        """原子写：临时文件+rename，防止进程崩溃/双容器并发读时留下半截JSON"""
        try:
            tmp_path = self.file_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.file_path)
        except Exception as e:
            logger.error(f"保存评分文件失败: {e}")

    @staticmethod
    def race_key(season: int, round_num) -> str:
        return f"{season}-R{round_num}"

    def open_race(self, season: int, round_num, race_name: str, open_at=None) -> str:
        """开启一场比赛的评分（幂等，已存在不覆盖）

        open_at: 可选，投票开启时刻（ISO 时间串，= 正赛开始+1h）；
                 比赛周预创建条目时传入，此刻仅可领链接、submit_votes 拒绝（防提前刷票）；
                 缺省/老数据无此字段 = 立即开放
        """
        key = self.race_key(season, round_num)
        with self._xlock():
            self._data = self._load()  # 写前刷新，避免覆盖web进程写入的票
            if key not in self._data["races"]:
                entry = {
                    "race_name": race_name,
                    "season": season,
                    "round": int(round_num),
                    "tokens": {},
                    "votes": {},
                    "closed": False,
                    "dotd": None,
                }
                if open_at:
                    entry["open_at"] = (open_at.isoformat() if hasattr(open_at, "isoformat")
                                        else str(open_at))
                self._data["races"][key] = entry
                self._save()
                logger.info(f"评分已开启: {key} {race_name}"
                            + (f"（投票 {entry.get('open_at')} 开放）" if open_at else ""))
        return key

    @staticmethod
    def race_open_at(race: Dict[str, Any]):
        """条目投票开启时刻（aware datetime）；无 open_at 字段（老数据）= None（视为已开放）"""
        raw = race.get("open_at")
        if not raw:
            return None
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(str(raw))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None

    def get_race(self, key: str) -> Optional[Dict[str, Any]]:
        self._reload()
        return self._data["races"].get(key)

    def get_or_create_token(self, key: str, member_openid: str) -> Optional[str]:
        """为成员生成/获取专属token；场次不存在或未开放返回None"""
        with self._xlock():
            self._data = self._load()  # 写前刷新，避免覆盖web进程写入的票
            race = self._data["races"].get(key)
            if not race or race.get("closed"):
                return None
            for token, info in race["tokens"].items():
                if info["member_openid"] == member_openid:
                    return token
            token = secrets.token_hex(8)
            race["tokens"][token] = {"member_openid": member_openid, "created": time.time()}
            self._save()
            return token

    def resolve_token(self, key: str, token: str) -> Optional[str]:
        """token -> member_openid，无效返回None"""
        self._reload()
        race = self._data["races"].get(key)
        if not race:
            return None
        info = race["tokens"].get(token)
        return info["member_openid"] if info else None

    def submit_votes(self, key: str, token: str, scores: Dict[str, int]) -> Optional[Any]:
        """
        提交评分（覆盖式）
        Returns: True成功 / False已截止 / "pending"未到开启时刻（比赛周预创建条目，防提前刷票）
                 / None无效token或场次
        """
        member = self.resolve_token(key, token)
        if not member:
            return None
        with self._xlock():
            self._data = self._load()  # 写前刷新，避免覆盖web进程写入的票
            race = self._data["races"][key]
            if race.get("closed"):
                return False
            open_at = self.race_open_at(race)
            if open_at is not None:
                from datetime import datetime, timezone
                if datetime.now(timezone.utc) < open_at:
                    return "pending"
            clean = {}
            for driver_id, score in scores.items():
                try:
                    s = int(score)
                    if 1 <= s <= 10:
                        clean[driver_id] = s
                except (TypeError, ValueError):
                    continue
            race["votes"][member] = clean
            self._save()
            logger.info(f"收到评分: {key} member={member[:8]}... 共{len(clean)}位车手")
            return True

    def get_member_votes(self, key: str, member_openid: str) -> Dict[str, int]:
        self._reload()
        race = self._data["races"].get(key)
        return dict(race["votes"].get(member_openid, {})) if race else {}

    def aggregate(self, key: str) -> List[Dict[str, Any]]:
        """
        聚合统计：每位车手的平均分/票数，按平均分降序
        Returns: [{"driver_id", "avg", "count"}, ...]
        """
        self._reload()
        return self._aggregate_data(self._data, key)

    @staticmethod
    def _aggregate_data(data: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
        """对给定数据快照聚合（不触发 _reload，供写事务内部使用：
        close_race 等持锁写路径禁止调用会 reload 的方法，否则 self._data
        被整体替换，先前写入的 closed/dotd 会随旧对象被孤立丢失）"""
        race = data["races"].get(key)
        if not race:
            return []
        sums: Dict[str, List[int]] = {}
        for votes in race["votes"].values():
            for driver_id, score in votes.items():
                sums.setdefault(driver_id, []).append(score)
        board = [
            {"driver_id": d, "avg": round(sum(sc) / len(sc), 2), "count": len(sc)}
            for d, sc in sums.items()
        ]
        board.sort(key=lambda x: (-x["avg"], -x["count"]))
        return board

    def close_race(self, key: str) -> Optional[Dict[str, Any]]:
        """
        截止评分并结算DOTD（幂等）
        Returns: {"board": [...], "dotd": driver_id或None, "min_votes_met": bool} 或 None(场次不存在)
        """
        with self._xlock():
            self._data = self._load()  # 结算前刷新，确保包含web进程收集的全部票
            race = self._data["races"].get(key)
            if not race:
                return None
            if race.get("closed"):
                board = self._aggregate_data(self._data, key)
                return {"board": board, "dotd": race.get("dotd"), "min_votes_met": bool(race.get("dotd"))}

            race["closed"] = True
            board = self._aggregate_data(self._data, key)
            dotd = None
            min_votes_met = False
            if board and board[0]["count"] >= DOTD_MIN_VOTES:
                dotd = board[0]["driver_id"]
                min_votes_met = True
                race["dotd"] = dotd
                season = str(race["season"])
                self._data["season_dotd"].setdefault(season, {})
                self._data["season_dotd"][season][dotd] = \
                    self._data["season_dotd"][season].get(dotd, 0) + 1
            self._save()
            logger.info(f"评分已截止: {key} DOTD={dotd}")
            return {"board": board, "dotd": dotd, "min_votes_met": min_votes_met}

    def close_stale_races(self, schedule: List[Dict[str, Any]], now=None,
                          close_hours: int = 24) -> List[str]:
        """陈旧未截止场次清扫（两层兜底，每日调度调用；静默关闭=close_race 结算记 DOTD、
        不发群推送——群推送只走调度器正常截止路径）：

        ① 正赛结束+close_hours 已过且仍 closed=False → 立即关闭
           （解历史存量锚定：容器停机/老版本错过截止任务后条目永久未关，
            /rate 会一直锚定它——2026-09-23 荷兰站实证）
        ② 新比赛周开始（today >= 正赛日-4天，AGENTS.md 比赛周口径）→
           所有 round < 当前比赛周 round 的未关闭条目强制关闭（防新增漏网）

        Returns: 被关闭的 race_key 列表
        """
        from datetime import datetime, timedelta, timezone
        now = now or datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        def _race_start(race):
            try:
                d = datetime.fromisoformat(f"{race.get('date', '')}T{race.get('time') or '13:00:00Z'}")
                return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                return None

        starts = {}
        for race in schedule or []:
            st = _race_start(race)
            if st:
                starts[int(race.get("round", 0))] = st
        # 当前比赛周 round：正赛日-4天已到的最大 round
        current_round = 0
        for rnd, st in starts.items():
            if now >= st - timedelta(days=4):
                current_round = max(current_round, rnd)

        closed_keys = []
        self._reload()
        for key, race in list(self._data.get("races", {}).items()):
            if race.get("closed"):
                continue
            rnd = int(race.get("round", 0))
            st = starts.get(rnd)
            race_end_passed = bool(st and now >= st + timedelta(hours=2 + close_hours))
            superseded = bool(current_round and rnd < current_round)
            if race_end_passed or superseded:
                if self.close_race(key):
                    closed_keys.append(key)
                    logger.warning(f"陈旧评分场次已静默关闭: {key} "
                                   f"({'正赛结束超' + str(close_hours) + 'h' if race_end_passed else '新比赛周开始'})")
        return closed_keys

    def season_dotd_board(self, season: int) -> List[tuple]:
        """赛季DOTD累积榜 [(driver_id, 次数), ...] 按次数降序"""
        self._reload()
        counts = self._data["season_dotd"].get(str(season), {})
        return sorted(counts.items(), key=lambda x: -x[1])

    # ==================== 验证码（群聊链接防冒用） ====================

    def create_verification_code(self, key: str, member_openid: str) -> Optional[str]:
        """生成一次性验证码（6位数字+字母），5分钟过期，绑定群成员的验证入口"""
        import string as _string
        import time as _time
        chars = _string.ascii_uppercase + _string.digits
        code = ''.join(secrets.choice(chars) for _ in range(6))  # CSPRNG，防在线爆破冒用身份
        with self._xlock():
            self._data = self._load()  # 写前刷新，避免覆盖web进程写入的票
            # 场次存在性校验（防止给不存在/已截止场次发码）
            race = self._data["races"].get(key)
            if not race or race.get("closed"):
                return None
            self._data.setdefault("codes", {})
            self._data["codes"][code] = {
                "race_key": key, "member_openid": member_openid,
                "created": _time.time(), "used": False
            }
            self._save()
        return code

    def verify_code(self, code: str) -> Optional[str]:
        """验证码 → 创建个人token并作废验证码。返回token, 失败None"""
        import time as _time
        with self._xlock():
            self._data = self._load()
            entry = self._data.get("codes", {}).get(code, {})
            if not entry or entry.get("used"):
                return None
            if _time.time() - entry.get("created", 0) > 300:  # 5分钟过期
                del self._data["codes"][code]
                self._save()
                return None
            entry["used"] = True
            # 为该成员在该场次生成个人token
            key = entry["race_key"]
            member = entry["member_openid"]
            token = self.get_or_create_token_inner(key, member)
            self._save()
            return token

    def get_or_create_token_inner(self, key: str, member_openid: str) -> Optional[str]:
        """内部创建token（不加锁，由调用方持锁）"""
        race = self._data["races"].get(key)
        if not race or race.get("closed"):
            return None
        for t, info in race["tokens"].items():
            if info["member_openid"] == member_openid:
                return t
        import secrets
        t = secrets.token_hex(8)
        race["tokens"][t] = {"member_openid": member_openid, "created": __import__('time').time()}
        return t
