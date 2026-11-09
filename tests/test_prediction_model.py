# -*- coding: utf-8 -*-
"""预测模型单测

- 纯函数：_median / _safe_int / _sc_strength
- 回归闸门：离线拟合 + 2025 留一法回测，断言领奖台命中 / MAE / 排位 Top3 命中
  （data/prediction_dataset_2024.json / 2025.json 随包分发，无需网络）
"""
import pytest

from common.f1_api import F1API
from common.prediction_model import FIT_YEAR, RacePredictionModel


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


class TestRegressionBacktest:
    """回归闸门：每次改动 MODEL_VERSION / 特征工程 / 超参时，指标滑坡直接红。

    用 F1API(season=2026) 保证 build_season_dataset 走本地 JSON 缓存
    （2024/2025 < 2026，不触发网络），f1cosmos=None 跳过升级件网络请求。
    """

    THRESHOLD_PODIUM = 2.0
    THRESHOLD_MAE = 3.8
    THRESHOLD_QUALI_TOP3 = 1.8

    @pytest.fixture(scope="class")
    def model_factory(self):
        f1_api = F1API(season=2026)
        return RacePredictionModel(f1_api, f1cosmos=None)

    @pytest.fixture(scope="class")
    def report_default(self, model_factory):
        model = model_factory.fit(train_years=(2023, FIT_YEAR))
        return model_factory.backtest(year=2025, model=model)

    @pytest.fixture(scope="class")
    def report_2024_only(self, model_factory):
        model = model_factory.fit(train_years=(2024,))
        return model_factory.backtest(year=2025, model=model)

    def _assert_metrics(self, report):
        assert report["races_tested"] >= 20, f"回测场次不足: {report['races_tested']}"
        assert report["podium_hits_per_race"] >= self.THRESHOLD_PODIUM, (
            f"领奖台命中 {report['podium_hits_per_race']}/场 低于阈值 {self.THRESHOLD_PODIUM}")
        assert report["mean_abs_pos_error"] <= self.THRESHOLD_MAE, (
            f"MAE {report['mean_abs_pos_error']} 高于阈值 {self.THRESHOLD_MAE}")
        assert report["quali_top3_hits_per_race"] >= self.THRESHOLD_QUALI_TOP3, (
            f"排位Top3命中 {report['quali_top3_hits_per_race']}/场 低于阈值 {self.THRESHOLD_QUALI_TOP3}")

    def test_default_window(self, report_default):
        """生产默认窗口：2023+2024 拟合，2025 回测。"""
        self._assert_metrics(report_default)

    def test_2024_only_window(self, report_2024_only):
        """真实场景窗口：只用上一年（2024）拟合，预测当年（2025）。"""
        self._assert_metrics(report_2024_only)
