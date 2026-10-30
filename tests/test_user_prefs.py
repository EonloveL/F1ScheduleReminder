# -*- coding: utf-8 -*-
"""user_prefs 全量单测：多关注 CRUD / 迁移 / 时区 / 新闻开关 / 模糊解析（离线，tmp 文件）"""
import pytest

from common.user_prefs import UserPrefsStore, resolve_driver, resolve_team


@pytest.fixture
def store(tmp_path):
    return UserPrefsStore(str(tmp_path / "user_prefs.json"))


class TestWatchCRUD:
    def test_set_and_get_driver(self, store):
        assert store.set_user_driver("u1", "verstappen", "维斯塔潘")
        pref = store.get_user_prefs("u1")
        assert UserPrefsStore.pref_drivers(pref) == ["verstappen"]
        assert UserPrefsStore.pref_driver_names(pref)["verstappen"] == "维斯塔潘"

    def test_multi_watch_up_to_3(self, store):
        for d in ("verstappen", "norris", "leclerc"):
            assert store.set_user_driver("u1", d, d)
        assert not store.set_user_driver("u1", "hamilton", "hamilton")  # 第4个超限
        assert len(UserPrefsStore.pref_drivers(store.get_user_prefs("u1"))) == 3

    def test_idempotent_resets(self, store):
        store.set_user_driver("u1", "verstappen", "维斯塔潘")
        assert store.set_user_driver("u1", "verstappen", "维斯塔潘")  # 重复=幂等
        assert UserPrefsStore.pref_drivers(store.get_user_prefs("u1")) == ["verstappen"]

    def test_team_and_driver_independent(self, store):
        store.set_user_driver("u1", "verstappen", "V")
        store.set_user_team("u1", "red_bull", "红牛")
        pref = store.get_user_prefs("u1")
        assert UserPrefsStore.pref_drivers(pref) == ["verstappen"]
        assert UserPrefsStore.pref_teams(pref) == ["red_bull"]

    def test_remove_driver(self, store):
        store.set_user_driver("u1", "verstappen", "V")
        store.set_user_driver("u1", "norris", "N")
        assert store.remove_user_driver("u1", "verstappen")
        assert UserPrefsStore.pref_drivers(store.get_user_prefs("u1")) == ["norris"]
        assert not store.remove_user_driver("u1", "nonexist")

    def test_unset_by_kind(self, store):
        store.set_user_driver("u1", "verstappen", "V")
        store.set_user_team("u1", "red_bull", "R")
        n = store.unset_user_pref("u1", "driver")
        assert n >= 1
        assert not UserPrefsStore.pref_drivers(store.get_user_prefs("u1"))
        assert UserPrefsStore.pref_teams(store.get_user_prefs("u1")) == ["red_bull"]  # 车队不受影响

    def test_unset_all(self, store):
        store.set_user_driver("u1", "verstappen", "V")
        store.set_user_team("u1", "red_bull", "R")
        store.unset_user_pref("u1", "driver", "team")
        pref = store.get_user_prefs("u1")
        assert not UserPrefsStore.pref_drivers(pref) and not UserPrefsStore.pref_teams(pref)


class TestNewsAndTz:
    def test_news_default_on(self, store):
        assert store.news_dm_enabled("u1") is True
        store.set_news_dm("u1", False)
        assert store.news_dm_enabled("u1") is False

    def test_tz(self, store):
        assert store.get_user_tz("u1") is None
        store.set_user_tz("u1", "Europe/London")
        assert store.get_user_tz("u1") == "Europe/London"

    def test_get_all_user_prefs(self, store):
        store.set_user_driver("u1", "verstappen", "V")
        store.set_user_driver("u2", "norris", "N")
        assert set(store.get_all_user_prefs().keys()) == {"u1", "u2"}


class TestPersistence:
    def test_reload_from_disk(self, tmp_path):
        p = str(tmp_path / "p.json")
        s1 = UserPrefsStore(p)
        s1.set_user_driver("u1", "verstappen", "V")
        s1.set_user_tz("u1", "Asia/Tokyo")
        s2 = UserPrefsStore(p)
        assert UserPrefsStore.pref_drivers(s2.get_user_prefs("u1")) == ["verstappen"]
        assert s2.get_user_tz("u1") == "Asia/Tokyo"


class TestResolve:
    def _drivers(self):
        return [{"Driver": {"driverId": "verstappen", "givenName": "Max", "familyName": "Verstappen"}},
                {"Driver": {"driverId": "norris", "givenName": "Lando", "familyName": "Norris"}}]

    def test_resolve_by_cn_alias(self):
        hit = resolve_driver("维斯塔潘", self._drivers())
        assert hit and hit[0] == "max_verstappen"  # 归一化到 Ergast driverId

    def test_resolve_by_en_surname(self):
        hit = resolve_driver("norris", self._drivers())
        assert hit and hit[0] == "norris"

    def test_resolve_miss(self):
        assert resolve_driver("不存在的车手", self._drivers()) is None

    def test_resolve_team(self):
        teams = [{"Constructor": {"constructorId": "red_bull", "name": "Red Bull Racing"}}]
        hit = resolve_team("红牛", teams)
        assert hit and hit[0] == "red_bull"
