"""
拟合窗口对比实验（2026-09-28，巴库复盘驱动）
============================================
当前生产模型 fit=(2023,2024)。2025 已完整、2026 已赛 15 轮，检验更新窗口是否更优。
所有窗口均为 2026 赛季的纯样本外评估（podium命中/MAE，越高/越低越好），
评估口径与线上一致（A2 网格阻尼 damp=0.2 启用，stint 特征从简同 backtest_sc_effect）。

运行：python analysis/fit_window_compare.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from common.f1_api import F1API
from common.f1cosmos_api import F1CosmosAPI
from common.prediction_model import RacePredictionModel, BLEND_LAMBDA

SEASON = 2026
EVAL_YEAR = 2026
WINDOWS = [(2023, 2024), (2024, 2025), (2023, 2024, 2025), (2025,)]

_model = RacePredictionModel(F1API(season=SEASON), F1CosmosAPI(season=SEASON))
_model._stint_features = lambda *a, **k: (None, None, None, [])  # 与既有回测口径一致


def evaluate(model, year, damp=0.2):
    dataset = _model.build_season_dataset(year)
    a = model["race_weights"]
    mean_speed = model["mean_speed_index"]
    upgrades_map = _model._upgrades_count_map(year)
    podium, mae, tested = [], [], 0
    for rec in dataset:
        if not rec.get("speed_index"):
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


print(f"{'拟合窗口':<18}{'2026 podium/场':>14}{'2026 MAE':>10}{'场次':>6}")
print("-" * 52)
for w in WINDOWS:
    model = _model.fit(train_years=w)
    p, m, n = evaluate(model, EVAL_YEAR)
    tag = "  <- 当前生产" if w == (2023, 2024) else ""
    print(f"{str(w):<18}{p:>14}{m:>10}{n:>6}{tag}")
    print(f"   race_weights={[round(x,3) for x in model['race_weights']]} race_r2={model['race_r2']}")
