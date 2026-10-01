"""
A1 概率校准对比：高 SC vs 低 SC 赛道的概率分布平坦度
=====================================================
A1（残差缩放）只改概率分布不改期望排序，backtest 的 podium/MAE 测不出，
本脚本用确定性蒙特卡洛直接验证：同一组期望名次下，sc_strength 越高，
Top3/Top10 概率分布越平（头部确定感下降、尾部爆冷上升、熵增大）。

口径与线上一致：resid = quali_resid_std × (1 + SC_RESID_K × sc_strength)，
sc_strength 取真实赛道数据（_sc_strength，读 circuits_data.json）。

运行：python analysis/verify_sc_calibration.py
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.f1_api import F1API
from common.f1cosmos_api import F1CosmosAPI
from common.prediction_model import RacePredictionModel, SC_RESID_K, MC_SAMPLES

SEASON = 2026
CIRCUITS = ["albert_park", "monaco", "baku", "bahrain", "marina_bay",
            "hungaroring", "yas_marina", "monza"]

_model = RacePredictionModel(F1API(season=SEASON), F1CosmosAPI(season=SEASON))
model = _model.load_or_fit()
BASE_RESID = model["quali_resid_std"] or 2.0

# 合成 20 车手期望名次（等差 1~20，接近真实 exp_q 分布形态）
N = 20
exp_q = np.arange(1, N + 1, dtype=float)


def mc_probs(resid):
    rng = np.random.default_rng(42)
    top3 = np.zeros(N)
    top10 = np.zeros(N)
    for _ in range(MC_SAMPLES):
        order = np.argsort(exp_q + rng.normal(0, resid, N))
        top3[order[:3]] += 1
        top10[order[:10]] += 1
    return top3 / MC_SAMPLES, top10 / MC_SAMPLES


def entropy(p):
    p = p[p > 0]
    return float(-(p * np.log(p)).sum())


print("=" * 84)
print(f"基准残差 quali_resid_std={BASE_RESID:.2f}，MC={MC_SAMPLES}，合成期望名次 1~20")
print(f"A1 口径：resid × (1 + {SC_RESID_K}×sc_strength)，sc_strength=min(场均事件/3,1)")
print("-" * 84)
print(f"{'赛道':<14}{'sc_strength':>11}{'resid':>8}{'杆位Top3%':>10}{'P10 Top3%':>10}"
      f"{'P20 Top10%':>11}{'Top10熵':>9}")
print("-" * 84)
for cid in CIRCUITS:
    s = _model._sc_strength(cid)
    resid = BASE_RESID * (1.0 + SC_RESID_K * s)
    top3, top10 = mc_probs(resid)
    print(f"{cid:<14}{s:>11.2f}{resid:>8.2f}{top3[0]*100:>9.1f}{top3[9]*100:>9.1f}"
          f"{top10[19]*100:>10.1f}{entropy(top10):>9.3f}")
print("=" * 84)
print("预期：sc_strength 越高 → 杆位 Top3% 越低、P10/P20 爆冷概率越高、熵越大（分布更平）")
