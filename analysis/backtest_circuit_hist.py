"""
预测模型权重受控回测工具（2026-09-21 起作为模型权重变更的标准验证方法）

用途：
  质疑模型某特征权重（如 circuit_hist_q 赛道历史）是否合理时，不要拍脑袋——
  跑本脚本，用 2025 留一法回测对比"结构变体"和"权重乘数扫描"两条曲线，用数据决定。

两种实验：
  A. 结构变体：换特征口径（如 名次制历史 / 无历史 / 圈速制历史），各自重拟合再回测
  B. 权重乘数：固定 baseline 拟合权重，把目标特征系数乘以 0~1.5 扫描，看泛化精度曲线

运行（项目根目录）：
  python analysis/backtest_circuit_hist.py            # 默认：quali circuit_hist_q(w3) 全套
  python analysis/backtest_circuit_hist.py --mult-only # 只跑权重乘数扫描（快）

结论参考（2026-09-21 实测，844 样本）：
  - 结构变体：baseline(名次史) Top3=2.09 > no_hist(2.04) > pace_hist(1.91) → 历史有效，不可删
  - 权重乘数：w3=1.0(当前) Top3 峰值 2.09，降权(0.5-0.85) Top3 掉到 2.00 → 当前权重即最优
  → 诺里斯巴库 circuit_hist_q=12.0 被压低，是为全局 Top3 最优必须接受的个例代价，应靠呈现层解决

注意：
  - quali 向量不用 stint 特征，脚本内 monkeypatch 跳过 stint 拉取（避免几十场 OpenF1 抓取）。
  - 若改 race 向量（含 race_pace_idx/tyre_deg），不要跳过 stint 特征，且需先确保训练年 stint 缓存已暖。
"""
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.f1_api import F1API
from common.f1cosmos_api import F1CosmosAPI
from common.prediction_model import RacePredictionModel

SEASON = 2026
TRAIN_YEARS = (2023, 2024)   # 与模型 FIT_YEAR 口径一致
BACKTEST_YEAR = 2025

# quali 向量特征索引：[0]=intercept [1]=stand_pos [2]=form_quali [3]=circuit_hist_q [4]=type_affinity [5]=upgrade [6]=tm_gap_quali
QUALI_W3_IDX = 3


def make_model():
    model = RacePredictionModel(F1API(season=SEASON), F1CosmosAPI(season=SEASON))
    # quali 不用 stint 特征，跳过重载 stint 抓取（改 race 向量实验时去掉此行）
    model._stint_features = lambda *a, **k: (None, None, None, [])
    return model


def fit_quali(model, cq_fn, train_years=TRAIN_YEARS):
    """按 cq_fn(特征口径) 重拟合 quali 权重。cq_fn(f, fq) -> circuit_hist_q 取值"""
    Xq, Yq = [], []
    for ty in train_years:
        dataset = model.build_season_dataset(ty)
        upgrades_map = model._upgrades_count_map(ty)
        speeds = [r["speed_index"] for r in dataset if r.get("speed_index")]
        mean_speed = sum(speeds) / len(speeds) if speeds else 0.0
        for rec in dataset:
            if not rec.get("speed_index"):
                continue
            slopes = model._team_power_slopes(ty, upto_round=rec["round"])
            for e in rec["entries"]:
                f = model._driver_features(e["driver_id"], e["team"], ty, rec["round"],
                                           rec["circuit_id"], rec["speed_index"], slopes, mean_speed)
                if f["stand_pos"] is None:
                    continue
                up = -min(upgrades_map.get((rec["round"], f["team_key"]), 0), 6) / 6.0
                fq = f["form_quali"] if f["form_quali"] is not None else 20.0
                cq = cq_fn(f, fq)
                tq = f["tm_gap_quali"] if f["tm_gap_quali"] is not None else 0.0
                if e["quali_pos"] < 90:
                    Xq.append([1.0, f["stand_pos"], fq, cq, f["type_affinity"], up, tq])
                    Yq.append(e["quali_pos"])
    Xq, Yq = np.array(Xq), np.array(Yq)
    w, *_ = np.linalg.lstsq(Xq, Yq, rcond=None)
    return w, len(Yq)


def backtest_quali(model, w, cq_fn, year=BACKTEST_YEAR):
    """2025 留一法回测：qualiTop3命中/场 + qualiMAE"""
    dataset = model.build_season_dataset(year)
    speeds = [r["speed_index"] for r in dataset if r.get("speed_index")]
    mean_speed = sum(speeds) / len(speeds) if speeds else 0.0
    upgrades_map = model._upgrades_count_map(year)
    top3_hits, mae, tested = [], [], 0
    for rec in dataset:
        if not rec.get("speed_index"):
            continue
        slopes = model._team_power_slopes(year, upto_round=rec["round"])
        feats = []
        for e in rec["entries"]:
            f = model._driver_features(e["driver_id"], e["team"], year, rec["round"],
                                       rec["circuit_id"], rec["speed_index"], slopes, mean_speed)
            if f["stand_pos"] is None:
                continue
            up = -min(upgrades_map.get((rec["round"], f["team_key"]), 0), 6) / 6.0
            fq = f["form_quali"] if f["form_quali"] is not None else 20.0
            cq = cq_fn(f, fq)
            tq = f["tm_gap_quali"] if f.get("tm_gap_quali") is not None else 0.0
            exp_q = (w[0] + w[1] * f["stand_pos"] + w[2] * fq + w[3] * cq
                     + w[4] * f["type_affinity"] + w[5] * up + w[6] * tq)
            feats.append((e["driver_id"], e["quali_pos"], exp_q))
        if len(feats) < 10:
            continue
        pred = [d for d, _, _ in sorted(feats, key=lambda x: x[2])]
        actual_ids = [d for d, _, _ in sorted([x for x in feats if x[1] < 90], key=lambda x: x[1])]
        top3_hits.append(len(set(pred[:3]) & set(actual_ids[:3])))
        pred_pos = {d: i + 1 for i, d in enumerate(pred)}
        for i, d in enumerate(actual_ids):
            if d in pred_pos:
                mae.append(abs(pred_pos[d] - (i + 1)))
        tested += 1
    return (round(sum(top3_hits) / max(tested, 1), 2),
            round(sum(mae) / max(len(mae), 1), 2))


def cq_baseline(f, fq):
    return f["circuit_hist_q"] if f["circuit_hist_q"] is not None else fq


def run_structural_variants(model):
    print("=" * 62)
    print("A. 结构变体（换 circuit_hist 口径，各自重拟合+回测）")
    print("-" * 62)
    print(f"{'变体':<22} {'qualiTop3/场':>12} {'qualiMAE':>10} {'样本':>6}")
    variants = [
        ("baseline 名次制历史", cq_baseline),
        ("no_hist  无赛道历史", lambda f, fq: fq),
        ("pace_hist 圈速制历史", lambda f, fq: (f["circuit_hist_pace"]
                                                 if f["circuit_hist_pace"] is not None else 100.0)),
    ]
    results = {}
    for name, fn in variants:
        w, n = fit_quali(model, fn)
        t3, mae = backtest_quali(model, w, fn)
        results[name] = (t3, mae)
        print(f"{name:<22} {t3:>12} {mae:>10} {n:>6}")
    base = results["baseline 名次制历史"]
    for name, _ in variants[1:]:
        t3, mae = results[name]
        print(f"  vs baseline: {name}  Top3 {t3-base[0]:+.2f}  MAE {mae-base[1]:+.2f}")


def run_weight_sweep(model):
    print("=" * 62)
    print("B. 权重乘数扫描（固定 baseline 权重，仅缩放 w3=circuit_hist_q）")
    print("-" * 62)
    w_base, _ = fit_quali(model, cq_baseline)
    base_t3, base_mae = backtest_quali(model, w_base, cq_baseline)
    print(f"拟合权重 w3(circuit_hist_q) = {w_base[QUALI_W3_IDX]:.4f}")
    print(f"{'w3乘数':>8} {'等效w3':>9} {'qualiTop3/场':>13} {'qualiMAE':>10} {'vs 1.0':>20}")
    for mult in [0.0, 0.25, 0.5, 0.7, 0.85, 1.0, 1.2, 1.5]:
        w = w_base.copy()
        w[QUALI_W3_IDX] = w_base[QUALI_W3_IDX] * mult
        t3, mae = backtest_quali(model, w, cq_baseline)
        star = " <-- 当前" if mult == 1.0 else ""
        print(f"{mult:>8.2f} {w[QUALI_W3_IDX]:>9.4f} {t3:>13} {mae:>10}   "
              f"Top3 {t3-base_t3:+.2f}  MAE {mae-base_mae:+.2f}{star}")
    print("解读：找 qualiTop3 最高且 qualiMAE 最低的乘数点；1.0=当前权重")


if __name__ == "__main__":
    m = make_model()
    if "--mult-only" in sys.argv:
        run_weight_sweep(m)
    else:
        run_structural_variants(m)
        run_weight_sweep(m)
