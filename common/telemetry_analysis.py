"""
F1赛程提醒机器人 - 赛后遥测分析（F1官方livetiming归档数据，免费）

数据源: https://livetiming.formula1.com/static/ （无需鉴权）
- Index.json: 赛季分站/环节索引（含文件路径）
- DriverList.json: 车手/车队/车队配色（TeamColour hex）
- TimingData.jsonStream: 圈速事件流（圈结束时间戳+圈时长）
- CarData.z.jsonStream: 遥测流（RPM/速度/档位/油门/刹车，2026起无DRS通道）

注意: F1已停止生成聚合快照（CarData.z.json 为空壳），真实数据在 *.jsonStream 流文件中。
fastf1 3.6.1 的 car_data 解析依赖已消失的 DRS 通道45，对2026数据会报错，故本模块自研解析。
"""

import base64
import json
import logging
import os
import re
import zlib
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

STATIC_BASE = (os.getenv("TELEMETRY_BASE_URL", "").strip()
               or "https://livetiming.formula1.com/static").rstrip("/")
# 浏览器UA：F1 CDN对机房IP+机器人UA双重风控，浏览器UA实测可通过
TELEMETRY_UA = os.getenv(
    "TELEMETRY_UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
)
CACHE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "telemetry_cache"
)
EXPORTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "exports"
)

# 环节类型 -> livetiming Session Name
SESSION_NAME_MAP = {
    "race": "Race",
    "qualifying": "Qualifying",
    "sprint": "Sprint",
    "sprint_qualifying": "Sprint Qualifying",
    "fp1": "Practice 1", "fp2": "Practice 2", "fp3": "Practice 3",
}

# CarData 通道: 0=RPM 2=Speed 3=nGear 4=Throttle 5=Brake （2026起无DRS通道45）
_CH_RPM, _CH_SPEED, _CH_GEAR, _CH_THROTTLE, _CH_BRAKE = "0", "2", "3", "4", "5"


def _norm(text: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", "", s.lower())


class TelemetrySession:
    """单场环节的遥测会话（官方livetiming归档）"""

    def __init__(self, year: int, gp_query: str, session_type: str = "race",
                 schedule_race: Dict[str, Any] = None,
                 drivers_of_interest: List[str] = None):
        """
        Args:
            year: 赛季年份
            gp_query: 分站名/地点（中英文均可，按名称模糊匹配Index）
            session_type: race/qualifying/sprint/sprint_qualifying/fp1-3
            schedule_race: Ergast格式比赛dict（可选，用date精确匹配分站更稳）
            drivers_of_interest: 只加载这些车手(TLA)的遥测（流是增量编码，前向填充后按需保留，省内存）
        """
        self.year = year
        self.session_type = session_type
        self._interest = [d.upper() for d in (drivers_of_interest or [])]
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": TELEMETRY_UA})
        # 代理出口：阿里云等机房IP被F1 CDN 403时，用 TELEMETRY_PROXY 走代理
        proxy = os.getenv("TELEMETRY_PROXY", "").strip()
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        else:
            self.session.proxies = {"http": None, "https": None}
        os.makedirs(CACHE_DIR, exist_ok=True)

        self.path = self._find_session_path(gp_query, session_type, schedule_race)
        if not self.path:
            raise ValueError(f"未找到遥测数据: {year} {gp_query} {session_type}")

        self.drivers = self._load_drivers()          # {tla: {name, team, color, number}}
        self._car_df: Dict[str, Any] = {}            # {tla: DataFrame}
        self._lap_windows: Dict[str, List[Dict]] = {}  # {tla: [{no, time, start, end}]}
        self.event_name = self.path.split("/")[1].replace("_", " ")

    # ==================== 索引与下载 ====================

    def _get(self, url: str, cache_name: str = None) -> bytes:
        if cache_name:
            cached = os.path.join(CACHE_DIR, cache_name)
            if os.path.exists(cached):
                with open(cached, "rb") as f:
                    return f.read()
        try:
            resp = self.session.get(url, timeout=90)
            resp.raise_for_status()
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 403:
                logger.error(
                    "F1遥测源403：服务器IP被F1 CDN封锁（机房IP风控）。"
                    "解决方案：部署 server_deploy_qq_official/cf_worker_livetiming_relay.js "
                    "到 Cloudflare Worker（免费），然后在 .env 设置 "
                    "TELEMETRY_BASE_URL=https://<你的worker>.workers.dev/static；"
                    "或在 .env 设置 TELEMETRY_PROXY=http://代理:端口"
                )
            raise
        if cache_name:
            with open(cached, "wb") as f:
                f.write(resp.content)
        return resp.content

    def _find_session_path(self, gp_query: str, session_type: str,
                           schedule_race: Dict[str, Any] = None) -> Optional[str]:
        raw = self._get(f"{STATIC_BASE}/{self.year}/Index.json",
                        cache_name=f"index_{self.year}.json")
        index = json.loads(raw.decode("utf-8-sig"))
        want_session = SESSION_NAME_MAP.get(session_type, "Race")
        q = _norm(gp_query)
        race_date = (schedule_race or {}).get("date", "")

        best_meeting = None
        for m in index.get("Meetings", []):
            candidates = [m.get("Name", ""), m.get("OfficialName", ""), m.get("Location", "")]
            if q and any(q in _norm(c) or _norm(c) in q for c in candidates if c):
                best_meeting = m
                break
            # 无名称匹配时按正赛日期兜底
            if race_date:
                for s in m.get("Sessions", []):
                    if s.get("Name") == "Race" and str(s.get("StartDate", "")).startswith(race_date):
                        best_meeting = m
                        break
            if best_meeting:
                break
        if not best_meeting:
            return None
        for s in best_meeting.get("Sessions", []):
            if s.get("Name") == want_session:
                return s.get("Path")
        logger.warning(f"该分站无 {want_session} 环节（2026冲刺周末无FP2/FP3）")
        return None

    def _load_drivers(self) -> Dict[str, Dict[str, Any]]:
        raw = self._get(STATIC_BASE + "/" + self.path + "DriverList.json",
                        cache_name=self.path.replace("/", "_") + "DriverList.json")
        data = json.loads(raw.decode("utf-8-sig"))
        drivers = {}
        for num, d in data.items():
            drivers[d.get("Tla", num)] = {
                "number": num,
                "name": d.get("FullName", d.get("Tla", num)).title(),
                "team": d.get("TeamName", ""),
                "color": "#" + d.get("TeamColour", "888888"),
            }
        return drivers

    def resolve_driver(self, query: str) -> Optional[str]:
        """中文别名/英文姓/TLA -> TLA"""
        q = (query or "").strip()
        if not q:
            return None
        if q.upper() in self.drivers:
            return q.upper()
        # 中文别名 -> driverId(英文姓小写) -> TLA
        from .user_prefs import DRIVER_ALIASES
        target = DRIVER_ALIASES.get(q, q)
        nq = _norm(target)
        if not nq:
            return None  # 纯中文且不在别名表：归一化后为空，不能模糊匹配
        for tla, d in self.drivers.items():
            last = d["name"].split()[-1]
            if _norm(last) == nq or nq in _norm(d["name"]):
                return tla
        return None

    # ==================== 圈速窗口（TimingData流） ====================

    @staticmethod
    def _parse_utc(text: str) -> datetime:
        """解析F1时间戳（7位小数秒+Z，兼容Python3.8/3.11）：截断微秒到6位"""
        s = text.replace("Z", "+00:00")
        m = re.match(r"^(.*\.\d{6})\d*(\+00:00)$", s)
        if m:
            s = m.group(1) + m.group(2)
        return datetime.fromisoformat(s)

    @staticmethod
    def _parse_stream_time(text: str) -> timedelta:
        m = re.match(r"^(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)$", text.strip())
        if not m:
            return timedelta(0)
        h = int(m.group(1) or 0)
        return timedelta(hours=h, minutes=int(m.group(2)), seconds=float(m.group(3)))

    @staticmethod
    def _lap_time_seconds(text: str) -> Optional[float]:
        """圈速/分段时间转秒：兼容 "1:14.321" 和纯秒 "22.485" """
        s = str(text or "").strip()
        if not s:
            return None
        m = re.match(r"^(\d+):(\d+(?:\.\d+)?)$", s)
        if m:
            return int(m.group(1)) * 60 + float(m.group(2))
        try:
            return float(s)
        except ValueError:
            return None

    @staticmethod
    def _split_stream_records(text: str):
        """jsonStream 切分：同一物理行可能串联多个记录；记录边界 = 时间戳后紧跟 { 或 \" """
        parts = re.split(r'(?=\d{2}:\d{2}:\d{2}\.\d{3}[{\"])', text)
        for p in parts:
            p = p.strip()
            if not p:
                continue
            m = re.match(r'^(\d{2}:\d{2}:\d{2}\.\d{3})(.*)$', p, re.DOTALL)
            if not m:
                continue
            yield m.group(1), m.group(2)

    def _load_lap_windows(self):
        """
        解析TimingData流：每位车手每圈的（圈号, 圈速秒, end_offset）
        圈完成判定：最后一段(S2/第三段)出现 Value 时圈结束；圈速 = LastLapTime 或三段合计。
        不用 NumberOfLaps 判定（维修区起步车手不发该字段，如2026荷兰站维斯塔潘）。
        """
        if getattr(self, "_raw_laps", None):
            return
        raw = self._get(STATIC_BASE + "/" + self.path + "TimingData.jsonStream",
                        cache_name=self.path.replace("/", "_") + "TimingData.jsonStream")
        text = raw.decode("utf-8-sig", errors="replace")

        num_to_tla = {d["number"]: tla for tla, d in self.drivers.items()}
        state: Dict[str, Dict[str, Any]] = {}  # tla -> {sectors{idx:sec}, lap_count}

        for prefix, payload_text in self._split_stream_records(text):
            try:
                payload = json.loads(payload_text)
            except ValueError:
                continue
            lines_data = payload.get("Lines")
            if not lines_data:
                continue
            t = self._parse_stream_time(prefix)
            for num, d in lines_data.items():
                tla = num_to_tla.get(num)
                if not tla or not isinstance(d, dict):
                    continue
                st = state.setdefault(tla, {"sectors": {}, "speeds": {}, "laps": [],
                                            "lap_no": 0, "last_end_t": None})

                # 累计分段成绩（Sectors 可能是 dict 或 list）
                sectors = d.get("Sectors")
                if isinstance(sectors, dict):
                    sector_iter = sectors.items()
                elif isinstance(sectors, list):
                    sector_iter = ((str(i), v) for i, v in enumerate(sectors))
                else:
                    sector_iter = ()
                for s_idx, s_val in sector_iter:
                    if not isinstance(s_val, dict):
                        continue
                    v = self._lap_time_seconds(s_val.get("Value"))
                    if not v:
                        continue
                    # 圈计数更新后S2晚到约0.1s：上一圈刚记完且S2缺失时回填上一圈
                    if (not st["sectors"] and st["laps"] and s_idx == "2"
                            and st["last_end_t"] is not None
                            and 0 < (t - st["last_end_t"]).total_seconds() < 5
                            and "2" not in st["laps"][-1]["sectors"]):
                        st["laps"][-1]["sectors"]["2"] = v
                        continue
                    st["sectors"][s_idx] = v

                # 累计测速点（I1/I2/ST/FL）
                speeds = d.get("Speeds") or {}
                if isinstance(speeds, dict):
                    for sp_key, sp_val in speeds.items():
                        if isinstance(sp_val, dict) and str(sp_val.get("Value", "")).strip():
                            try:
                                st["speeds"][sp_key] = float(sp_val["Value"])
                            except (TypeError, ValueError):
                                pass

                lap_sec = self._lap_time_seconds((d.get("LastLapTime") or {}).get("Value"))
                n_laps = d.get("NumberOfLaps")

                if n_laps is not None and n_laps > st["lap_no"]:
                    # 主路径：圈计数递增 = 圈完成
                    time_val = lap_sec
                    if not time_val and all(k in st["sectors"] for k in ("0", "1", "2")):
                        time_val = sum(st["sectors"].values())
                    if time_val:
                        st["laps"].append({"no": n_laps, "time": time_val,
                                           "end_offset": t,
                                           "sectors": dict(st["sectors"]),
                                           "speeds": dict(st["speeds"])})
                    st["lap_no"] = n_laps
                    st["sectors"] = {}
                    st["speeds"] = {}
                    st["last_end_t"] = t
                elif lap_sec:
                    if st["sectors"] and all(k in st["sectors"] for k in ("0", "1", "2")):
                        # 无圈计数车手（维修区起步等）：LastLapTime+分段齐 -> 新圈
                        st["laps"].append({"no": len(st["laps"]) + 1, "time": lap_sec,
                                           "end_offset": t,
                                           "sectors": dict(st["sectors"]),
                                           "speeds": dict(st["speeds"])})
                        st["sectors"] = {}
                        st["speeds"] = {}
                    elif st["laps"] and abs(st["laps"][-1]["time"] - lap_sec) < 1.0:
                        # 主路径已记录该圈（分段合计），回填精确圈速
                        st["laps"][-1]["time"] = lap_sec
                    elif not st["laps"]:
                        st["laps"].append({"no": 1, "time": lap_sec, "end_offset": t,
                                           "sectors": {}, "speeds": dict(st["speeds"])})
                        st["speeds"] = {}

        self._raw_laps = {tla: st["laps"] for tla, st in state.items()}

    def load_car_data(self):
        """下载并解析 CarData.z.jsonStream -> 每车手 DataFrame(Date, Speed, Throttle, Brake, RPM, Gear)"""
        if self._car_df:
            return
        import pandas as pd

        self._load_lap_windows()
        raw = self._get(STATIC_BASE + "/" + self.path + "CarData.z.jsonStream",
                        cache_name=self.path.replace("/", "_") + "CarData.z.jsonStream")
        text = raw.decode("utf-8-sig", errors="replace")

        num_to_tla = {d["number"]: tla for tla, d in self.drivers.items()}
        interest_nums = {d["number"] for tla, d in self.drivers.items()
                         if not self._interest or tla in self._interest}
        rows: Dict[str, list] = {}
        last_ch: Dict[str, Dict[str, int]] = {}  # 增量编码 -> 每车最近通道值
        offset = None

        for prefix, payload in self._split_stream_records(text):
            payload = payload.strip().strip('"')
            if not payload:
                continue
            try:
                data = json.loads(zlib.decompress(base64.b64decode(payload), -zlib.MAX_WBITS)
                                  .decode("utf-8-sig"))
            except Exception:
                continue
            for entry in data.get("Entries", []):
                try:
                    utc = self._parse_utc(entry["Utc"])
                except Exception:
                    continue
                if offset is None:
                    offset = utc - self._parse_stream_time(prefix)
                    self._session_start_utc = offset
                cars = entry.get("Cars", {})
                for num, car in cars.items():
                    if num not in interest_nums:
                        continue
                    ch = car.get("Channels", {})
                    last_ch.setdefault(num, {}).update(ch)
                # 前向填充：每条entry为全部关注车手出样（遥测流是增量编码）
                for num in interest_nums:
                    ch = last_ch.get(num)
                    if not ch:
                        continue
                    try:
                        tla = num_to_tla[num]
                        rows.setdefault(tla, []).append((
                            utc,
                            int(ch[_CH_SPEED]),
                            int(ch[_CH_THROTTLE]),
                            int(ch[_CH_BRAKE]),
                            int(ch[_CH_RPM]),
                            int(ch[_CH_GEAR]),
                        ))
                    except (KeyError, TypeError, ValueError):
                        continue

        for tla, r in rows.items():
            if not r:
                continue
            df = pd.DataFrame(r, columns=["Date", "Speed", "Throttle", "Brake", "RPM", "Gear"])
            df = df.sort_values("Date").drop_duplicates("Date").reset_index(drop=True)
            self._car_df[tla] = df
        logger.info(f"✓ 遥测数据加载: {self.event_name} {self.session_type}, "
                    f"{len(self._car_df)} 位车手")

        # 圈窗口换算为UTC
        self._lap_windows = {}
        for tla, lap_list in getattr(self, "_raw_laps", {}).items():
            wins = []
            for l in sorted(lap_list, key=lambda x: x["no"]):
                end = self._session_start_utc + l["end_offset"]
                start = end - timedelta(seconds=l["time"])
                wins.append({"no": l["no"], "time": l["time"], "start": start, "end": end,
                             "sectors": l.get("sectors", {}), "speeds": l.get("speeds", {})})
            self._lap_windows[tla] = wins

    # ==================== 圈选择与遥测切片 ====================

    def get_fastest_lap(self, tla: str) -> Optional[Dict[str, Any]]:
        wins = self._lap_windows.get(tla, [])
        return min(wins, key=lambda w: w["time"]) if wins else None

    def get_lap_telemetry(self, tla: str, lap_no: int = None,
                          ref_window: Dict[str, Any] = None):
        """
        取某圈遥测切片并积分出距离（米）。lap_no=None 用最快圈。
        ref_window: 该车手无圈速窗口时（维修区起步等特殊情况），借用参照车手的同圈窗口对齐
        """
        self.load_car_data()
        wins = self._lap_windows.get(tla, [])
        win = None
        if wins:
            if lap_no is None:
                win = self.get_fastest_lap(tla)
            else:
                win = next((w for w in wins if w["no"] == lap_no), None)
        if win is None:
            win = ref_window
        if not win:
            return None, None
        df = self._car_df.get(tla)
        if df is None:
            return None, None
        seg = df[(df["Date"] >= win["start"]) & (df["Date"] <= win["end"])].copy()
        if len(seg) < 10:
            return None, None
        dt = seg["Date"].diff().dt.total_seconds().fillna(0).clip(0, 5)
        seg["Distance"] = (seg["Speed"] / 3.6 * dt).cumsum()
        return win, seg

    # ==================== 图表 ====================

    def plot_comparison(self, tlas: List[str], lap_no: int = None,
                        out_dir: str = None) -> Optional[str]:
        """
        多车手同圈遥测对比图（速度/油门/刹车 vs 距离，车队配色）

        Returns:
            图表PNG文件名（存于 data/exports/），失败返回None
        """
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        plt.rcParams["font.sans-serif"] = ["Noto Sans CJK SC", "SimHei", "Microsoft YaHei"]
        plt.rcParams["axes.unicode_minus"] = False

        traces = []
        ref_window = None
        for tla in tlas:
            win, seg = self.get_lap_telemetry(tla, lap_no)
            if seg is not None:
                if ref_window is None:
                    ref_window = win
                traces.append((tla, win, seg))
        # 缺圈速窗口的车手（维修区起步等）：借用参照窗口对齐同一时间范围
        for tla in tlas:
            if all(t[0] != tla for t in traces) and ref_window is not None:
                win, seg = self.get_lap_telemetry(tla, lap_no, ref_window=ref_window)
                if seg is not None:
                    win = dict(ref_window)
                    win["time"] = None  # 无官方圈速，图例只显示圈号
                    traces.append((tla, win, seg))
                    logger.info(f"{tla} 无独立圈速窗口，已按参照圈窗口对齐")
        if len(traces) < 2:
            logger.warning("有效遥测不足2人，无法对比")
            return None

        fig, axes = plt.subplots(3, 1, figsize=(12, 11), sharex=True)
        fig.patch.set_facecolor("#15151e")
        panels = [("Speed", "速度 km/h", axes[0]),
                  ("Throttle", "油门 %", axes[1]),
                  ("Brake", "刹车", axes[2])]
        for ax in axes:
            ax.set_facecolor("#15151e")
            ax.tick_params(colors="white")
            ax.grid(True, alpha=0.2, color="white")
            for spine in ax.spines.values():
                spine.set_color("#444")

        used_colors = {}
        for tla, win, seg in traces:
            info = self.drivers.get(tla, {})
            color = info.get("color", "#ffffff")
            # 同队同色区分：第二名及以后的同队车手用虚线
            linestyle = "-"
            if color in used_colors:
                linestyle = "--"
            used_colors[color] = used_colors.get(color, 0) + 1
            if win.get("time"):
                lap_str = f"{int(win['time'] // 60)}:{win['time'] % 60:06.3f}"
                label = f"{tla}  第{win['no']}圈  {lap_str}"
            else:
                label = f"{tla}  第{win['no']}圈（同窗口）"
            for col, _ylabel, ax in panels:
                ax.plot(seg["Distance"], seg[col], color=color, linewidth=1.6,
                        linestyle=linestyle, label=label)

        for _col, ylabel, ax in panels:
            ax.set_ylabel(ylabel, color="white")
            ax.legend(loc="best", fontsize=9, facecolor="#22222c", labelcolor="white")
        axes[2].set_xlabel("距离 (m)", color="white")
        lap_desc = "最快圈" if lap_no is None else f"第{lap_no}圈"
        fig.suptitle(f"{self.event_name} · {SESSION_NAME_MAP.get(self.session_type, '')} · {lap_desc}遥测对比",
                     color="white", fontsize=14)

        out_dir = out_dir or EXPORTS_DIR
        os.makedirs(out_dir, exist_ok=True)
        fname = f"telemetry_{_norm(self.event_name)}_{self.session_type}_{'_'.join(tlas)}_{lap_no or 'fastest'}.png"
        path = os.path.join(out_dir, fname)
        fig.savefig(path, dpi=110, bbox_inches="tight", facecolor=fig.get_facecolor())
        plt.close(fig)
        logger.info(f"✓ 遥测对比图已生成: {fname}")
        return fname

    # ==================== Web JSON 输出（交互式遥测对比页） ====================

    RESAMPLE_STEP_M = 5  # 距离网格步长（米）

    def get_comparison_json(self, tlas: List[str], lap_no: int = None) -> Optional[Dict[str, Any]]:
        """
        多车手同圈遥测对比（JSON，供网页交互图表）
        遥测重采样到共享距离网格（5m步长），刹车用开关量展示
        """
        import numpy as np

        traces = []
        ref_window = None
        for tla in tlas:
            win, seg = self.get_lap_telemetry(tla, lap_no)
            if seg is not None:
                if ref_window is None:
                    ref_window = win
                traces.append((tla, win, seg, False))
        for tla in tlas:
            if all(t[0] != tla for t in traces) and ref_window is not None:
                win, seg = self.get_lap_telemetry(tla, lap_no, ref_window=ref_window)
                if seg is not None:
                    traces.append((tla, dict(ref_window, time=None, sectors={}, speeds={}),
                                   seg, True))
        if len(traces) < 2:
            return None

        drivers_out = []
        for tla, win, seg, shared in traces:
            info = self.drivers.get(tla, {})
            grid = np.arange(0.0, float(seg["Distance"].max()), self.RESAMPLE_STEP_M)
            dist = seg["Distance"].to_numpy()
            speed = np.interp(grid, dist, seg["Speed"].to_numpy())
            throttle = np.interp(grid, dist, seg["Throttle"].to_numpy())
            brake = np.interp(grid, dist, seg["Brake"].to_numpy(), left=0, right=0)
            brake = (brake > 50).astype(int) * 100  # 刹车按开关量展示
            drivers_out.append({
                "tla": tla,
                "name": info.get("name", tla),
                "team": info.get("team", ""),
                "color": info.get("color", "#ffffff"),
                "lap_no": win.get("no"),
                "lap_time": win.get("time"),
                "shared_window": shared,
                "sectors": win.get("sectors", {}),
                "speeds": win.get("speeds", {}),
                "distance": [round(float(x), 1) for x in grid],
                "speed": [round(float(x), 1) for x in speed],
                "throttle": [round(float(x), 1) for x in throttle],
                "brake": [int(x) for x in brake],
            })

        return {
            "event": self.event_name,
            "session": self.session_type,
            "session_name": SESSION_NAME_MAP.get(self.session_type, self.session_type),
            "lap": lap_no or "fastest",
            "drivers": drivers_out,
        }

    def list_laps(self, tla: str) -> List[Dict[str, Any]]:
        """车手全部圈（圈号/圈速/分段/测速），供网页圈号选择器"""
        self.load_car_data()
        return [{"no": w["no"], "time": w["time"],
                 "sectors": w.get("sectors", {}), "speeds": w.get("speeds", {})}
                for w in self._lap_windows.get(tla, [])]
