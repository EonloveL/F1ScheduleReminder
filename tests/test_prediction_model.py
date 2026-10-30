# -*- coding: utf-8 -*-
"""预测模型纯函数单测（离线；不测需要数据集的 fit/predict 主链路）"""
from common.prediction_model import RacePredictionModel


class TestHelpers:
    def test_median_odd(self):
        assert RacePredictionModel._median([3.0, 1.0, 2.0]) == 2.0

    def test_median_even(self):
        assert RacePredictionModel._median([1.0, 2.0, 3.0, 4.0]) == 2.5

    def test_safe_int(self):
        assert RacePredictionModel._safe_int("3") == 3
        assert RacePredictionModel._safe_int(5) == 5
        assert RacePredictionModel._safe_int(None) == 99
        assert RacePredictionModel._safe_int("abc", default=7) == 7


class TestSCStrength:
    """安全车修正：sc_strength = min(场均SC事件/3, 1)，出现率饱和后用事件数区分"""

    def _model(self):
        # 绕过 __init__ 的数据源依赖：裸实例 + 手工灌 _sc_meta 结果
        m = RacePredictionModel.__new__(RacePredictionModel)
        return m

    def test_zero_events(self, monkeypatch):
        m = self._model()
        monkeypatch.setattr(m, "_sc_meta", lambda cid: {"sc_events_per_race": 0, "sc_rate": 0})
        assert m._sc_strength("monaco") == 0.0

    def test_saturates_at_one(self, monkeypatch):
        m = self._model()
        monkeypatch.setattr(m, "_sc_meta", lambda cid: {"sc_events_per_race": 6.0, "sc_rate": 1.0})
        assert m._sc_strength("monaco") == 1.0

    def test_mid_value(self, monkeypatch):
        m = self._model()
        monkeypatch.setattr(m, "_sc_meta", lambda cid: {"sc_events_per_race": 1.5, "sc_rate": 0.5})
        assert abs(m._sc_strength("baku") - 0.5) < 1e-9

    def test_missing_meta_defaults_zero(self, monkeypatch):
        m = self._model()
        monkeypatch.setattr(m, "_sc_meta", lambda cid: {})
        assert m._sc_strength("nowhere") == 0.0
