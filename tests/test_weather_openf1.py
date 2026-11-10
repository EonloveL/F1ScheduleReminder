"""OpenF1 实测天气链路测试（fake OpenF1API + 内存缓存，离线）

守护 2026-10-30 两轮 review 修复：
- 同国家双站会议打分匹配（巴塞罗那/马德里不张冠李戴）
- 进行中环节不取实测（date_end 未过 → None + 短负缓存）
- wind_speed m/s → km/h 换算
- 负缓存命中不再重复请求
- 缓存命中时 fetched_at 保持真实抓取时间
"""
from datetime import datetime, timedelta, timezone

import pytest

from common.weather_api import WeatherAPI


class FakeCache:
    def __init__(self):
        self.d = {}

    def load(self, k):
        return self.d.get(k)

    def save(self, k, v, ttl_hours=None):
        self.d[k] = v


def _race(locality="Madrid", circuit="Madring", rid="madring", rnd=14):
    return {
        "round": rnd, "raceName": "Madrid Grand Prix", "season": 2026,
        "Circuit": {"circuitId": rid, "circuitName": circuit,
                    "Location": {"locality": locality, "country": "Spain",
                                 "lat": "40.4", "long": "-3.6"}},
        "date": "2026-09-13", "time": "13:00:00Z",
    }


class FakeOpenF1:
    """两个西班牙站：巴塞罗那（R7）在前、马德里（R14）在后——旧逻辑会错配到巴塞罗那"""
    MEETINGS = [
        {"meeting_key": 1, "meeting_name": "Barcelona Grand Prix",
         "circuit_short_name": "Barcelona", "location": "Barcelona",
         "country_name": "Spain"},
        {"meeting_key": 2, "meeting_name": "Madrid Grand Prix",
         "circuit_short_name": "Madring", "location": "Madrid",
         "country_name": "Spain"},
    ]

    def __init__(self, date_end=None, wind=10.0, track_temp=55.0, calls=None):
        self.date_end = date_end
        self.wind = wind
        self.track_temp = track_temp
        self.calls = calls if calls is not None else {"meetings": 0}

    def get_meetings(self, year):
        self.calls["meetings"] += 1
        return self.MEETINGS

    def get_sessions(self, meeting_key=None):
        end = self.date_end or (datetime.now(timezone.utc) - timedelta(days=1))
        start = end - timedelta(hours=2)
        return [{"session_key": 100 + meeting_key, "session_name": "Race",
                 "date_start": start.isoformat(), "date_end": end.isoformat()}]

    def _make_request(self, endpoint, params):
        now = datetime.now(timezone.utc)
        return [
            {"date": (now - timedelta(minutes=30)).isoformat(), "track_temperature": self.track_temp,
             "air_temperature": 24.0, "wind_speed": self.wind, "humidity": 50,
             "rainfall": 0},
            {"date": (now - timedelta(minutes=10)).isoformat(), "track_temperature": self.track_temp + 2,
             "air_temperature": 25.0, "wind_speed": self.wind + 1, "humidity": 52,
             "rainfall": 0},
        ]


@pytest.fixture
def api(monkeypatch):
    a = WeatherAPI()
    a.cache = FakeCache()
    yield a


def _install(monkeypatch, **kw):
    import common.openf1_api as of1
    fake = FakeOpenF1(**kw)
    monkeypatch.setattr(of1, "OpenF1API", lambda *a, **k: fake)
    return fake


class TestMeetingMatching:
    def test_same_country_two_races_picks_correct(self, api, monkeypatch):
        _install(monkeypatch, track_temp=55.0)
        out = api._openf1_measured(2026, _race(), "race")
        assert out is not None
        assert out["track_temp_measured"]["max"] == 57.0  # 马德里数据，不是巴塞罗那

    def test_unrelated_race_rejected(self, api, monkeypatch):
        _install(monkeypatch)
        race = _race(locality="Tokyo", circuit="Fuji Speedway", rid="fuji", rnd=3)
        race["raceName"] = "Japanese Grand Prix"
        race["Circuit"]["Location"]["country"] = "Japan"
        assert api._openf1_measured(2026, race, "race") is None


class TestInProgressGuard:
    def test_future_date_end_returns_none(self, api, monkeypatch):
        _install(monkeypatch, date_end=datetime.now(timezone.utc) + timedelta(minutes=20))
        assert api._openf1_measured(2026, _race(), "race") is None

    def test_in_progress_writes_negative_cache(self, api, monkeypatch):
        fake = _install(monkeypatch, date_end=datetime.now(timezone.utc) + timedelta(minutes=20))
        api._openf1_measured(2026, _race(), "race")
        api._openf1_measured(2026, _race(), "race")
        assert fake.calls["meetings"] == 1  # 第二次命中负缓存，不再请求

    def test_finished_race_returns_measured(self, api, monkeypatch):
        _install(monkeypatch, date_end=datetime.now(timezone.utc) - timedelta(hours=2))
        out = api._openf1_measured(2026, _race(), "race")
        assert out and out["measured"] is True


class TestWindUnits:
    def test_ms_to_kmh(self, api, monkeypatch):
        _install(monkeypatch, wind=10.0)
        out = api._openf1_measured(2026, _race(), "race")
        assert out["wind_max_kmh"] == pytest.approx(11.0 * 3.6, abs=0.1)
        assert out["wind_min_kmh"] == pytest.approx(10.0 * 3.6, abs=0.1)


class TestMeasuredCache:
    def test_fetched_at_survives_cache_hit(self, api, monkeypatch):
        center = datetime.now(timezone.utc) + timedelta(days=1)
        api.cache.d["weather_v2_TestGP_" + center.strftime("%Y%m%d")] = {
            "hourly": {"time": []}, "_fetched_at": "2026-10-30 08:00 UTC"}
        monkeypatch.setattr(api, "_fetch_hourly", lambda *a, **k: api.cache.d[
            "weather_v2_TestGP_" + center.strftime("%Y%m%d")])
        race = _race()
        race["date"] = center.strftime("%Y-%m-%d")
        race["time"] = center.strftime("%H:%M:%SZ")
        res = api.get_race_weather(race, [{"type": "race", "name": "S", "datetime": center}])
        assert res["fetched_at"] == "2026-10-30 08:00 UTC"
