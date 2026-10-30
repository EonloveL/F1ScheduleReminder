# -*- coding: utf-8 -*-
"""并发回归测试：多线程同时写共享 JSON 存储——这正是"读代码读不出来"的那类 bug
（scheduler 线程 × webhook 线程并发写 ratings/user_prefs/memory/pushed_store）。
断言：N 线程并发写后 文件不损坏（json 可解析）+ 写入计数不丢 + 无异常。"""
import json
import threading

import pytest

from common.pushed_store import PushedResultsStore
from common.ratings_store import RatingsStore
from common.memory.store import MemoryStore


THREADS = 8
ITER = 10


def _hammer(target_fn):
    errors = []

    def worker():
        try:
            target_fn()
        except Exception as e:  # noqa: BLE001 - 测试要捕获一切并发异常
            errors.append(e)

    ths = [threading.Thread(target=worker) for _ in range(THREADS)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    return errors


class TestRatingsStoreConcurrency:
    def test_concurrent_open_and_vote(self, tmp_path):
        rs = RatingsStore(str(tmp_path / "ratings.json"))

        def work():
            for i in range(ITER):
                key = rs.open_race(2026, (i % 3) + 1, f"GP{(i % 3) + 1}")
                token = rs.get_or_create_token(key, f"user{i % 4}")
                rs.submit_votes(key, token, {"verstappen": 9, "norris": 8})

        errors = _hammer(work)
        assert not errors
        with open(rs.file_path, encoding="utf-8") as f:
            data = json.load(f)  # 文件必须可解析（原子写无半截）
        assert len(data["races"]) == 3  # 3 个场次全部在


class TestMemoryStoreConcurrency:
    def test_concurrent_append_and_forget(self, tmp_path):
        st = MemoryStore(str(tmp_path / "memory_store.json"))

        def work():
            for i in range(ITER):
                uid = f"u{i % 4}"
                data = st.load_user(uid)
                data["working"].append({"id": f"{threading.get_ident()}-{i}",
                                        "q": "q", "a": "a", "ts": 1.0})
                st.save_user(uid, data)
                st.remove_item(uid, "nonexistent-id")

        errors = _hammer(work)
        assert not errors
        with open(st.file_path, encoding="utf-8") as f:
            data = json.load(f)
        assert len(data["users"]) == 4
        for u in data["users"].values():
            assert len(u["working"]) <= 12  # 容量上限生效


class TestPushedStoreConcurrency:
    def test_concurrent_mark(self, tmp_path):
        ps = PushedResultsStore(str(tmp_path / "pushed.json"))

        def work():
            for i in range(ITER):
                ps.mark_pushed(2026, i % 5, "race")

        errors = _hammer(work)
        assert not errors
        for i in range(5):
            assert ps.is_pushed(2026, i, "race")  # 写入无丢失
