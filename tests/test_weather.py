# -*- coding: utf-8 -*-
"""天气功能回归测试（离线；monkeypatch 网络层）

核心守护：逐环节时间窗时长——正赛 3h（覆盖 SC/红旗/雨战延迟）、排位 2h、
练习赛 75min（生产曾写反：race=65min 漏掉后半程、FP=2h 混入无关时段，2026-10-30 修复）。
另覆盖：预报/归档判定、沥青温度三档优先级（estimated > proxy 降级）。
"""
from datetime import datetime, timedelta, timezone

import pytest

from common.weather_api import WeatherAPI, estimate_track_temp, ASPHALT_MODEL


def _race(dt):
    return {
        "raceName": "Test GP",
        "Circuit": {"circuitName": "Test Circuit",
                    "Location": {"lat": "1.0", "long": "2.0",
                                 "locality": "Testville", "country": "Testland"}},
        "date": dt.strftime("%Y-%m-%d"),
        "time": dt.strftime("%H:%M:%SZ"),
        "season": dt.year,
    }


def _hourly_window(center: datetime, before_min=60, after_min=240, step_min=5):
    """构造 centered 前后覆盖的逐 5 分钟 hourly 数据；
    temperature_2m 用"距 center 的分钟数"编码，便于断言窗口边界。"""
    times, temps = [], []
    t = center - timedelta(minutes=before_min)
    while t <= center + timedelta(minutes=after_min):
        times.append(int(t.timestamp()))
        temps.append((t - center).total_seconds() / 60.0)
        t += timedelta(minutes=step_min)
    return {"time": times, "temperature_2m": temps}


@pytest.fixture
def api(monkeypatch):
    a = WeatherAPI()
    monkeypatch.setattr(a, "_openf1_measured", lambda *args, **kw: None)
    return a


def _patch_fetch(a, monkeypatch, center):
    monkeypatch.setattr(a, "_fetch_hourly",
                        lambda lat, lon, start, end, **kw: {"hourly": _hourly_window(center)})


class TestSessionWindows:
    @staticmethod
    def _run(a, monkeypatch, stype):
        center = datetime(2026, 5, 10, 14, 0, tzinfo=timezone.utc)
        _patch_fetch(a, monkeypatch, center)
        sessions = [{"type": stype, "name": "S", "datetime": center}]
        res = a.get_race_weather(_race(center), sessions)
        assert not res.get("error"), res
        s = res["sessions"][0]
        return s["air_temp"]["min"], s["air_temp"]["max"]

    def test_race_window_covers_full_race(self, api, monkeypatch):
        """正赛窗口 = 前30min → 后3h（覆盖 SC/红旗/雨战发车延迟，2024 巴西实证）"""
        lo, hi = self._run(api, monkeypatch, "race")
        assert hi >= 170, f"正赛天气窗口只到 +{hi}min（应覆盖 3h 延时场景）"
        assert lo <= -25

    def test_practice_window_is_one_hour(self, api, monkeypatch):
        """练习赛窗口 = 前30min → 后75min（60min+余量）"""
        lo, hi = self._run(api, monkeypatch, "fp1")
        assert 70 <= hi <= 80, f"FP1 窗口到 +{hi}min（应约 75min）"
        assert lo <= -25

    def test_qualifying_window_two_hours(self, api, monkeypatch):
        """排位窗口 = 前30min → 后2h（多次红旗/雨战中断）"""
        lo, hi = self._run(api, monkeypatch, "qualifying")
        assert 115 <= hi <= 125, f"排位窗口到 +{hi}min（应约 120min）"


class TestDataKind:
    def test_future_is_forecast(self, api, monkeypatch):
        center = datetime.now(timezone.utc) + timedelta(days=2)
        _patch_fetch(api, monkeypatch, center)
        res = api.get_race_weather(_race(center), [{"type": "race", "name": "S",
                                                    "datetime": center}])
        assert res["data_kind"].startswith("forecast")

    def test_past_is_archive(self, api, monkeypatch):
        center = datetime.now(timezone.utc) - timedelta(days=10)
        _patch_fetch(api, monkeypatch, center)
        res = api.get_race_weather(_race(center), [{"type": "race", "name": "S",
                                                    "datetime": center}])
        assert res["data_kind"].startswith("archive")


class TestTrackTempTiers:
    def test_estimated_present_with_soil_and_air(self, api, monkeypatch):
        center = datetime(2026, 5, 10, 14, 0, tzinfo=timezone.utc)
        hourly = _hourly_window(center)
        hourly["soil_temperature_0cm"] = [30.0] * len(hourly["time"])
        monkeypatch.setattr(api, "_fetch_hourly", lambda *a, **k: {"hourly": hourly})
        res = api.get_race_weather(_race(center), [{"type": "race", "name": "S",
                                                    "datetime": center}])
        s = res["sessions"][0]
        assert "track_temp_estimated" in s
        assert "track_temp_proxy" in s
        est_mid = (s["track_temp_estimated"]["min"] + s["track_temp_estimated"]["max"]) / 2
        assert est_mid > 30.0


class TestAsphaltModelFormula:
    def test_formula(self):
        m = ASPHALT_MODEL
        got = estimate_track_temp(30.0, 25.0, 800.0)
        want = m["intercept"] + m["soil"] * 30.0 + m["air"] * 25.0 + m["radiation"] * 800.0
        assert abs(got - want) < 1e-9


class TestLiveAndConfig:
    def test_live_forces_refresh(self, api, monkeypatch):
        captured = {}

        def fake_fetch(lat, lon, start, end, **kw):
            captured.update(kw)
            return {"hourly": _hourly_window(start)}

        monkeypatch.setattr(api, "_fetch_hourly", fake_fetch)
        center = datetime.now(timezone.utc) + timedelta(days=1)
        api.get_race_weather(_race(center), [{"type": "race", "name": "S",
                                             "datetime": center}], live=True)
        assert captured.get("force_refresh") is True

    def test_adaptive_ttl_near_race(self, api, monkeypatch):
        captured = {}

        def fake_fetch(lat, lon, start, end, **kw):
            captured.update(kw)
            return {"hourly": _hourly_window(start)}

        monkeypatch.setattr(api, "_fetch_hourly", fake_fetch)
        center = datetime.now(timezone.utc) + timedelta(hours=3)  # 3h 后开赛
        api.get_race_weather(_race(center), [{"type": "race", "name": "S",
                                             "datetime": center}])
        assert captured.get("ttl_hours") == 0.5  # 临近比赛预报 30min TTL

    def test_custom_source_env(self, monkeypatch):
        from common import weather_api as wa
        monkeypatch.setenv("WEATHER_BASE_URL", "https://wx.example.com/v1/forecast")
        assert wa._forecast_url() == "https://wx.example.com/v1/forecast"
        monkeypatch.delenv("WEATHER_BASE_URL")
        assert wa._forecast_url() == wa.DEFAULT_FORECAST_URL

    def test_elevation_passed_for_mountain_track(self, api, monkeypatch):
        captured = {}

        def fake_fetch(lat, lon, start, end, **kw):
            captured.update(kw)
            return {"hourly": _hourly_window(start)}

        monkeypatch.setattr(api, "_fetch_hourly", fake_fetch)
        center = datetime.now(timezone.utc) + timedelta(days=1)
        race = _race(center)
        race["Circuit"]["circuitId"] = "interlagos"  # 环境文件里 785m
        api.get_race_weather(race, [{"type": "race", "name": "S", "datetime": center}])
        assert captured.get("elevation") == 785

    def test_minutely_eta_preferred(self):
        """15 分钟级降水 ETA：窗口中第一个概率≥40% 的点"""
        base = 1_800_000_000
        data = {"minutely_15": {
            "time": [base + i * 900 for i in range(12)],
            "precipitation_probability": [10, 10, 10, 50, 60, 60, 70, 70, 60, 50, 40, 30],
            "precipitation": [0] * 12,
        }}
        eta = WeatherAPI._minutely_eta(data, base, base + 3 * 3600, base)
        assert eta == 45  # 第 4 个点（45 分钟处）
        assert WeatherAPI._minutely_eta({}, base, base + 3600, base) is None

    def test_current_summary(self):
        cur = {"temperature_2m": 26.4, "weather_code": 61,
               "wind_speed_10m": 12.3, "wind_direction_10m": 180,
               "precipitation": 0.8, "relative_humidity_2m": 78}
        out = WeatherAPI._current_summary(cur)
        assert out["air_temp"] == {"min": 26.4, "max": 26.4}
        assert out["weather_desc"] == "小雨"
        assert out["wind_direction"] == "南风"
        assert out["precip_total_mm"] == 0.8
        assert out["grain"] == "current"

    def test_current_summary_empty_returns_empty(self):
        assert WeatherAPI._current_summary({}) == {}


class TestRequestParams:
    """请求参数层回归：>7 天窗口（夏歇/季末）必须显式带 forecast_days，
    否则 Open-Meteo 默认 7 天截断导致 sessions 为空。"""

    @staticmethod
    def _capture(api, monkeypatch, payload):
        cap = {}

        class FakeResp:
            def raise_for_status(self):
                pass

            def json(self):
                return payload

        def fake_get(url, params=None, timeout=None):
            cap.update({"url": url, "params": dict(params or {}), "timeout": timeout})
            return FakeResp()

        monkeypatch.setattr(api.session, "get", fake_get)
        return cap

    def test_forecast_days_covers_far_window(self, api, monkeypatch):
        cap = self._capture(api, monkeypatch, {"hourly": {"time": []}})
        start = datetime.now(timezone.utc) + timedelta(days=12)
        end = start + timedelta(hours=3)
        api._fetch_hourly(1.0, 2.0, start, end)
        assert cap["params"].get("minutely_15")  # 预报带 15 分钟级
        assert cap["params"].get("current")      # 带实时块
        assert cap["params"].get("forecast_days", 0) >= 13  # 覆盖 12 天后窗口

    def test_history_uses_archive_with_dates(self, api, monkeypatch):
        cap = self._capture(api, monkeypatch, {"hourly": {"time": []}})
        end = datetime.now(timezone.utc) - timedelta(days=3)
        start = end - timedelta(hours=2)
        api._fetch_hourly(1.0, 2.0, start, end)
        assert "archive" in cap["url"]
        assert "start_date" in cap["params"] and "end_date" in cap["params"]
        assert "minutely_15" not in cap["params"]  # 归档不带扩展字段

    def test_retry_drops_bad_model_and_extensions(self, api, monkeypatch):
        """按参数语义模拟：带 models/minutely 即 400，剔除后才成功——
        防止"无论如何都调用两次"式的假通过"""
        calls = []

        class Resp:
            def __init__(self, params):
                self.params = params

            def raise_for_status(self):
                if "models" in self.params or "minutely_15" in self.params:
                    import requests as _rq; raise _rq.exceptions.HTTPError("400 Bad Request")

            def json(self):
                return {"hourly": {"time": []}}

        def fake_get(url, params=None, timeout=None):
            p = dict(params or {})
            calls.append(p)
            return Resp(p)

        monkeypatch.setenv("WEATHER_MODEL", "not-a-real-model")
        monkeypatch.setattr(api.session, "get", fake_get)
        start = datetime.now(timezone.utc) + timedelta(days=1)
        data = api._fetch_hourly(1.0, 2.0, start, start + timedelta(hours=3))
        assert data is not None and "hourly" in data
        assert "models" in calls[0] and "models" not in calls[1]  # 重试剔除非法模型
        assert "minutely_15" not in calls[1]

    def test_network_error_not_retried(self, api, monkeypatch):
        """纯网络异常不重试同一官方请求（避免 20s+20s 双倍等待）"""
        calls = []

        def fake_get(url, params=None, timeout=None):
            calls.append(url)
            raise ConnectionError("timeout")

        monkeypatch.setattr(api.session, "get", fake_get)
        start = datetime.now(timezone.utc) + timedelta(days=1)
        assert api._fetch_hourly(1.0, 2.0, start, start + timedelta(hours=3)) is None
        assert len(calls) == 1
