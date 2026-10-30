# -*- coding: utf-8 -*-
"""纯逻辑单测：查询解析 / 升级分类 / 积分榜投影判定（不触网）"""
import copy

from common.f1_tools import parse_upgrade_query, parse_race_query, _upgrade_type_class
from common.f1_api import F1APIManager


class TestParseUpgradeQuery:
    def test_year_only(self):
        info = parse_upgrade_query("2024")
        assert info == {"year": 2024, "round": None, "team": None, "gp": None}

    def test_team_and_gp(self):
        info = parse_upgrade_query("法拉利 蒙扎")
        assert info["team"] == "法拉利"
        assert info["gp"] == "蒙扎"

    def test_round_and_year(self):
        info = parse_upgrade_query("R5 2024")
        assert info["round"] == 5
        assert info["year"] == 2024

    def test_combined(self):
        info = parse_upgrade_query("法拉利 R16 2024")
        assert info["team"] == "法拉利"
        assert info["round"] == 16
        assert info["year"] == 2024

    def test_redbull_not_eaten_by_short_alias(self):
        # “小红牛”必须先于“红牛”匹配
        info = parse_upgrade_query("小红牛")
        assert info["team"] == "小红牛"

    def test_empty(self):
        info = parse_upgrade_query("")
        assert info == {"year": None, "round": None, "team": None, "gp": None}


class TestParseRaceQuery:
    def test_gp_and_year(self):
        assert parse_race_query("蒙扎 2024") == {"year": 2024, "round": None, "gp": "蒙扎"}

    def test_round(self):
        assert parse_race_query("R13")["round"] == 13

    def test_plain(self):
        assert parse_race_query("斯帕") == {"year": None, "round": None, "gp": "斯帕"}


class TestUpgradeTypeClass:
    def test_performance(self):
        assert _upgrade_type_class("Performance - Local Load") == "performance"
        assert _upgrade_type_class("Performance – Flow Conditioning") == "performance"
        assert _upgrade_type_class("Flow Conditioning") == "performance"
        assert _upgrade_type_class("Local load and flow conditioning.") == "performance"

    def test_circuit(self):
        assert _upgrade_type_class("Circuit specific - Drag Range") == "circuit_specific"
        assert _upgrade_type_class("Circuit Specific – Cooling Range") == "circuit_specific"
        assert _upgrade_type_class("Cooling Range") == "circuit_specific"
        assert _upgrade_type_class("Balance range") == "circuit_specific"

    def test_other(self):
        assert _upgrade_type_class("Reliability") == "other"
        assert _upgrade_type_class("") == "other"
        assert _upgrade_type_class(None) == "other"


def _mk_standings(total):
    return {"drivers": {"MRData": {"StandingsTable": {"StandingsLists": [{"DriverStandings": [
        {"position": "1", "points": str(total - 50), "wins": "1",
         "Driver": {"familyName": "Alpha", "code": "ALP"}, "Constructors": [{"name": "TeamA"}]},
        {"position": "2", "points": "50", "wins": "0",
         "Driver": {"familyName": "Beta", "code": "BET"}, "Constructors": [{"name": "TeamB"}]},
    ]}]}}}, "constructors": None}


_SPRINT = [{"position": 1, "driver_name": "X Alpha", "driver_code": "ALP",
            "team_name": "TeamA", "points": None},
           {"position": 2, "driver_name": "Y Beta", "driver_code": "BET",
            "team_name": "TeamB", "points": None}]


def _make_api(official_sum, baseline=1000.0):
    api = F1APIManager(season=2026)
    api._pending_points_sessions = lambda schedule=None: [
        {"round": "20", "type": "sprint", "name": "冲刺赛", "race_name": "T"}]
    api._fast_classification = lambda sess, schedule=None, openf1_sessions=None: copy.deepcopy(_SPRINT)
    api._observe_standings_sum = lambda psum: None
    api._read_standings_baseline = lambda: baseline
    api.get_schedule = lambda: []
    api.get_current_standings = lambda force_refresh=False: _mk_standings(official_sum)
    return api


class TestStandingsProjection:
    def test_projects_in_gap(self):
        # 官方未含冲刺分（baseline==official）→ 投影 +15（8+7）
        api = _make_api(1000)
        out, prov = api.get_standings_with_projection()
        assert prov is not None
        assert api._standings_points_sum(out) == 1015.0

    def test_no_double_count_after_official_update(self):
        # 官方已含（official=1015, baseline=1000）→ 不得双计
        api = _make_api(1015)
        out, prov = api.get_standings_with_projection()
        assert prov is None
        assert api._standings_points_sum(out) == 1015.0

    def test_no_projection_without_baseline(self):
        api = _make_api(1000, baseline=0.0)
        out, prov = api.get_standings_with_projection()
        assert prov is None

    def test_apply_session_points_race_win(self):
        api = _make_api(1000)
        standings = _mk_standings(1000)
        race = [{"position": 1, "driver_name": "Y Beta", "driver_code": "BET",
                 "team_name": "TeamB", "points": None}]
        awarded = api._apply_session_points(standings, race, api.RACE_POINTS, count_win=True)
        assert awarded == 25.0
        e = standings["drivers"]["MRData"]["StandingsTable"]["StandingsLists"][0]["DriverStandings"][1]
        assert e["Driver"]["familyName"] == "Beta" and e["points"] == "75" and e["wins"] == "1"

    def test_official_points_override_table(self):
        # 半程减半计分：entries 带官方 points 时用官方值
        api = _make_api(1000)
        standings = _mk_standings(1000)
        half = [{"position": 1, "driver_name": "X Alpha", "driver_code": "ALP",
                 "team_name": "TeamA", "points": "12.5"}]
        awarded = api._apply_session_points(standings, half, api.RACE_POINTS, count_win=True)
        assert awarded == 12.5
