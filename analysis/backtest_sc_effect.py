"""
安全车修正多赛季回测：A2 网格阻尼系数扫描
==========================================
机制背景：
- A1（残差缩放）只改概率分布不改排序，podium/MAE（基于排序）测不出，不参与本回测
- A2 网格阻尼：exp_f 中 grid 项 × (1 - damp×sc_strength)，高 SC 赛道发车顺位
  参考性下降（SC 打乱名次与 grid 的关联），非线性、真正改变排序
- 旧 A2（名次向均值压缩）已数学证伪（保序变换 Δ=0），2026-09-21 移除

扫描 damp ∈ {0, 0.2, 0.3, 0.5} × 2023/2024/2025/2026，对比 podium 命中 / 名次 MAE。
采纳门槛：damp>0 不得伤 baseline（podium 不降、MAE 不升），最好高 SC 子集改善。

运行：python analysis/backtest_sc_effect.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.f1_api import F1API
from common.f1cosmos_api import F1CosmosAPI
from common.prediction_model import RacePredictionModel, BLEND_LAMBDA

SEASON = 2026
YEARS = [2023, 2024, 2025, 2026]
DAMPS = [0.0, 0.2, 0.3, 0.5]
HIGH_SC = {"albert_park", "monaco", "baku", "silverstone", "zandvoort", "villeneuve"}

_model = RacePredictionModel(F1API(season=SEASON), F1CosmosAPI(season=SEASON))
_model._stint_features = lambda *a, **k: (None, None, None, [])  # 回测 race 向量口径从简（避免重载 stint）


def backtest_year(year, damp, subset=None):
    dataset = _model.build_season_dataset(year)
    model = _model.load_or_fit()
    a = model["race_weights"]
    mean_speed = model["mean_speed_index"]
    upgrades_map = _model._upgrades_count_map(year)
    podium, mae, tested = [], [], 0
    for rec in dataset:
        if not rec.get("speed_index"):
            continue
        if subset and rec["circuit_id"] not in subset:
            continue
        sc_s = _model._sc_strength(rec["circuit_id"])
        grid_factor = 1.0 - damp * sc_s
        slopes = _model._team_power_slopes(year, upto_round=rec["round"])
        rows = []
        for e in rec["entries"]:
            f = _model._driver_features(e["driver_id"], e["team"], year, rec["round"],
                                        rec["circuit_id"], rec["speed_index"], slopes, mean_speed)
            if f["stand_pos"] is None:
                continue
            up = -min(upgrades_map.get((rec["round"], f["team_key"]), 0), 6) / 6.0
            ff = f["form_finish"] if f["form_finish"] is not None else 20.0
            tf = f["tm_gap_finish"] if f.get("tm_gap_finish") is not None else 0.0
            rp = f["race_pace_idx"] or 100.0
            td = f["tyre_deg_ms"] or 0.0
            grid = e["grid"] if e["grid"] < 90 else 20.0
            exp_f = (a[0] + a[1] * f["stand_pos"] + a[2] * ff + a[3] * grid * grid_factor
                     + a[4] * f["type_affinity"] + a[5] * up + a[6] * tf + a[7] * rp + a[8] * td)
            exp_f = BLEND_LAMBDA * exp_f + (1 - BLEND_LAMBDA) * f["stand_pos"]
            rows.append({"driver_id": e["driver_id"], "finish": e["finish_pos"], "exp": exp_f})
        if len(rows) < 10:
            continue
        pred = [r["driver_id"] for r in sorted(rows, key=lambda x: x["exp"])]
        actual = sorted([r for r in rows if r["finish"] < 90], key=lambda x: x["finish"])
        actual_ids = [r["driver_id"] for r in actual]
        podium.append(len(set(pred[:3]) & set(actual_ids[:3])))
        pred_pos = {d: i + 1 for i, d in enumerate(pred)}
        for i, d in enumerate(actual_ids):
            if d in pred_pos:
                mae.append(abs(pred_pos[d] - (i + 1)))
        tested += 1
    return (round(sum(podium) / max(tested, 1), 2),
            round(sum(mae) / max(len(mae), 1), 2), tested)


print("=" * 78)
print("全场次：damp 系数扫描（podium/场 越高越好，MAE 越低越好）")
print("-" * 78)
header = f"{'赛季':<7}" + "".join(f"{('damp=' + str(d)):>22}" for d in DAMPS)
print(header)
for year in YEARS:
    cells = []
    for d in DAMPS:
        p, m, n = backtest_year(year, d)
        cells.append(f"{p:>9}/{m:<11}")
    print(f"{year:<7}" + "".join(f"{c:>22}" for c in cells) + f"  (N={n})")
print("-" * 78)
print("高 SC 赛道子集（阿尔伯特公园/摩纳哥/巴库/银石/赞德沃特/维伦纽夫）：")
for year in YEARS:
    cells = []
    n = 0
    for d in DAMPS:
        p, m, n = backtest_year(year, d, subset=HIGH_SC)
        cells.append(f"{p:>9}/{m:<11}")
    print(f"{year:<7}" + "".join(f"{c:>22}" for c in cells) + f"  (N={n})")
print("=" * 78)
print("单元格 = podium命中/MAE；解读：damp>0 若 podium 不降且 MAE 不升（高 SC 子集改善更佳）即可采纳")
