"""
赛道安全车率统计工具（一次性，2026-09-21）
=============================================
从 OpenF1 统计各赛道 2023-2026 的 SC+VSC+RedFlag 率，写回 common/circuits_data.json
的 sc_rate / sc_events_per_race / sc_years / sc_note 字段，供预测模型 A1+A2 使用。

双源：
  - race_control（主源，官方分类）：SAFETY CAR DEPLOYED / VIRTUAL SAFETY CAR DEPLOYED / RED FLAG
  - laps 圈速尖峰（交叉校验）：全场中位圈速 >1.15×基线 的连续窗口 → 实体 SC 窗口

API 响应全部走 LocalCache 永久缓存（data/cache/sc_*.json），重跑秒级。
OpenF1 免费层 3 req/s、30 req/min，全量首次约 6-10 分钟。

运行：python analysis/compute_sc_rates.py
"""
import json
import os
import statistics
import sys
import time
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from common.f1_api import LocalCache

YEARS = [2023, 2024, 2025, 2026]
BASE = "https://api.openf1.org/v1"
OUT_FILES = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data", "circuits_data.json"),        # CircuitsManager 运行时真源
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "common", "circuits_data.json"),      # 圈速纪录等其它读取方，保持同步
]

_session = requests.Session()
_session.trust_env = False
_session.headers.update({"User-Agent": "Mozilla/5.0"})
_cache = LocalCache()


def cached_get(key, url, params=None):
    data = _cache.load(key)
    if data is not None:
        return data
    for attempt in range(4):
        try:
            r = _session.get(url, params=params, timeout=30)
            if r.status_code == 200:
                data = r.json()
                _cache.save(key, data, ttl_hours=None)
                return data
            if r.status_code == 429:
                time.sleep(20)
                continue
        except Exception:
            time.sleep(3)
    return []


def field_median_laps(laps):
    """逐圈全场中位圈速：{lap_number: median_duration}"""
    bylap = defaultdict(list)
    for l in laps:
        d = l.get("lap_duration")
        if d and l.get("lap_number"):
            bylap[l["lap_number"]].append(float(d))
    return {n: statistics.median(v) for n, v in bylap.items() if v}


def pace_spike_windows(laps):
    """圈速尖峰检测：全场中位圈速 >1.15×基线 的连续圈 → 实体 SC 窗口数。
    排除 L1-2（静止起步圈速天然偏慢，非安全车）；用全场中位圈速故对个别进站圈鲁棒"""
    meds = field_median_laps(laps)
    if len(meds) < 15:
        return 0, []
    # 基线：L3+ 圈速中位数（多数为干净圈，SC 圈占比小不显著抬高基线）
    racing = [v for n, v in sorted(meds.items()) if n >= 3]
    baseline = statistics.median(racing)
    thr = baseline * 1.15
    windows, in_win = [], False
    for n in sorted(meds):
        if n < 3:
            continue  # 排除起步圈
        hot = meds[n] > thr
        if hot and not in_win:
            in_win = True
            windows.append(n)
        elif not hot:
            in_win = False
    return len(windows), windows


# OpenF1 circuit_short_name → circuits_data.json 键（Ergast circuitId 风格）
CID_ALIAS = {
    "baku": "baku", "las vegas": "las_vegas", "spielberg": "red_bull_ring",
    "monte carlo": "monaco", "sakhir": "bahrain", "austin": "americas",
    "melbourne": "albert_park", "mexico city": "rodriguez", "montreal": "villeneuve",
    "singapore": "marina_bay", "spa-francorchamps": "spa",
    "yas marina circuit": "yas_marina", "lusail": "losail",
    "madrid": "madring", "madring": "madring",
}


def to_cid(name: str) -> str:
    """OpenF1 赛道名 → circuits_data.json 键；先别名表，再规范化兜底"""
    n = (name or "").strip()
    if not n:
        return ""
    if n.lower() in CID_ALIAS:
        return CID_ALIAS[n.lower()]
    return n.lower().replace(" ", "_").replace("-", "_")


def count_events(race_control):
    """解析 race_control，返回 (sc_deploy, vsc_deploy, red_flag, first_sc_lap)。
    VSC 必须先于 SC 判定——"virtual safety car deployed"含"safety car deployed"子串"""
    sc = vsc = rf = 0
    first_sc_lap = None
    for e in race_control:
        cat = (e.get("category") or "").lower()
        msg = (e.get("message") or "").lower()
        lap = e.get("lap_number")
        if "virtual safety car" in msg and "deployed" in msg:
            vsc += 1
            if first_sc_lap is None:
                first_sc_lap = lap
        elif "safety car deployed" in msg or (cat == "safetycar" and "deployed" in msg):
            sc += 1
            if first_sc_lap is None:
                first_sc_lap = lap
        elif "red flag" in msg:
            rf += 1
    return sc, vsc, rf, first_sc_lap


def main():
    # 收集每赛道统计
    per_circuit = defaultdict(lambda: {"races": 0, "sc_races": 0, "events": 0,
                                       "sc": 0, "vsc": 0, "rf": 0, "pace_windows": 0})
    for year in YEARS:
        sessions = cached_get(f"sc_sessions_{year}",
                              f"{BASE}/sessions",
                              {"year": year, "session_type": "Race"})
        for s in sessions:
            sk = s.get("session_key")
            cid = to_cid(s.get("circuit_short_name") or s.get("meeting_name") or "")
            if not sk or not cid:
                continue
            rc = cached_get(f"sc_rc_{sk}", f"{BASE}/race_control", {"session_key": sk})
            sc, vsc, rf, _ = count_events(rc)
            laps = cached_get(f"sc_laps_{sk}", f"{BASE}/laps", {"session_key": sk})
            pace_wins, _ = pace_spike_windows(laps)
            c = per_circuit[cid]
            c["races"] += 1
            c["events"] += sc + vsc + rf
            c["sc"] += sc
            c["vsc"] += vsc
            c["rf"] += rf
            c["pace_windows"] += pace_wins
            # sc_rate 由 race_control 权威分类判定；pace_windows 仅作交叉校验备注，不参与判定
            if sc + vsc + rf > 0:
                c["sc_races"] += 1
        print(f"  {year} 完成，已统计 {sum(c['races'] for c in per_circuit.values())} 场")

    # 写回所有 circuits_data.json（data/ 运行时真源 + common/ 其它读取方，保持同步）
    updated = 0
    for out_file in OUT_FILES:
        if not os.path.exists(out_file):
            continue
        data = json.load(open(out_file, encoding="utf-8"))
        n = 0
        for key, c in data.items():
            hit = per_circuit.get(key)
            if not hit:
                continue
            if hit["races"] >= 1:
                c["sc_rate"] = round(hit["sc_races"] / hit["races"], 2)
                c["sc_events_per_race"] = round(hit["events"] / hit["races"], 2)
                c["sc_years"] = YEARS
                c["sc_note"] = (f"实体SC×{hit['sc']} VSC×{hit['vsc']} 红旗×{hit['rf']} "
                                f"/ {hit['races']}场；圈速尖峰校验×{hit['pace_windows']}"
                                + ("（样本<3，新赛道参考有限）" if hit["races"] < 3 else ""))
                n += 1
        json.dump(data, open(out_file, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        updated = max(updated, n)
        print(f"  已更新 {n}/{len(data)} 条到 {out_file}")
    print(f"\n安全车率最高 8 条赛道:")
    top = sorted([(k, v.get('sc_rate', 0), v.get('sc_events_per_race', 0))
                  for k, v in data.items() if v.get('sc_rate') is not None],
                 key=lambda x: -x[1])
    for k, r, e in top[:8]:
        print(f"  {k}: sc_rate={r} 场均事件={e}")


if __name__ == "__main__":
    main()
