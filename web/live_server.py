"""
F1 实时计时面板 - 网页服务（Flask）+ Document Picture-in-Picture 悬浮窗

端点：
- GET /live           实时面板页（含"开启悬浮窗"按钮）
- GET /live/pip       悬浮窗独立 HTML（自包含，轮询 /live/state）
- GET /live/state     聚合实时数据 JSON（服务端 TTL 缓存，多客户端共享）
- GET /health         健康检查

数据源：OpenF1 (api.openf1.org/v1)，仅 2023+ 且比赛周才有实时数据。

运行：python web/live_server.py （监听 0.0.0.0:8091）
"""

import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import requests
from flask import Flask, jsonify, render_template_string, request
import importlib.util  # noqa: E402

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.insert(0, project_root)


def _load_standalone(name: str, filename: str):
    """按文件路径加载 common/ 下的独立模块，绕过 common/__init__.py 的重依赖
    （live 容器只装了 flask/requests/faster-whisper，不能触发 scheduler/matplotlib 等）"""
    path = os.path.join(project_root, "common", filename)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_openf1 = _load_standalone("openf1_api", "openf1_api.py")
_tr = _load_standalone("tr_transcriber", "tr_transcriber.py")
OpenF1API = _openf1.OpenF1API
TeamRadioTranscriber = _tr.TeamRadioTranscriber

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
api = OpenF1API()
transcriber = TeamRadioTranscriber()

# 各数据源的缓存 TTL（秒）
TTL = {
    "session": 60,
    "drivers": 120,
    "position": 2,
    "intervals": 5,
    "race_control": 5,
    "weather": 30,
    "laps": 10,
    "pit": 10,
    "stints": 10,
    "team_radio": 10,
}


class LiveAggregator:
    """OpenF1 实时数据聚合器（服务端缓存，避免多客户端直连 OpenF1 触发限流）"""

    def __init__(self, api: OpenF1API):
        self.api = api
        self._cache = {}
        self._lock = threading.Lock()
        self._inflight = {}  # key -> threading.Event（single-flight：防TTL边界并发回源OpenF1）

    def _cached(self, key, ttl, fn):
        now = time.time()
        with self._lock:
            entry = self._cache.get(key)
            if entry and now - entry[0] < ttl:
                return entry[1]
            leader = key not in self._inflight
            if leader:
                self._inflight[key] = threading.Event()
            else:
                ev = self._inflight[key]
        if not leader:
            # 跟随者：等待领头请求回源完成（最多 ttl 秒），然后读缓存
            ev.wait(timeout=max(ttl, 5))
            with self._lock:
                entry = self._cache.get(key)
                if entry:
                    return entry[1]
            return fn()  # 兜底：领头失败时自行回源
        try:
            data = fn()
            with self._lock:
                self._cache[key] = (time.time(), data)
            return data
        finally:
            with self._lock:
                ev = self._inflight.pop(key, None)
            if ev:
                ev.set()

    @staticmethod
    def _latest_by_driver(rows):
        """每个车手取最新一条记录 {driver_number: row}"""
        latest = {}
        for r in rows:
            n = r.get("driver_number")
            if n is None:
                continue
            if n not in latest or r.get("date", "") > latest[n].get("date", ""):
                latest[n] = r
        return latest

    def _race_session_in_progress(self) -> bool:
        """通过 f1api.dev 赛程（免费、无需认证）判断当前是否正处于某比赛环节，
        用于区分「非比赛时段」与「比赛进行中但 OpenF1 未认证（401）」。"""
        try:
            year = datetime.now(timezone.utc).year
            resp = requests.get(f"https://f1api.dev/api/{year}", timeout=12,
                                headers={"User-Agent": "F1-Reminder-Bot/1.0"})
            races = resp.json().get("races", [])
            now = datetime.now(timezone.utc)
            for race in races:
                schedule = race.get("schedule", {})
                for key in ("fp1", "fp2", "fp3", "qualy", "sprintQualy", "sprintRace", "race"):
                    sess = schedule.get(key)
                    if not sess or not sess.get("date"):
                        continue
                    dt_str = f"{sess['date']}T{(sess.get('time') or '00:00:00Z').replace('Z', '')}"
                    try:
                        start = datetime.fromisoformat(dt_str).replace(tzinfo=timezone.utc)
                    except Exception:
                        continue
                    dur = timedelta(hours=2 if key == "race" else 1.5)
                    if start <= now <= start + dur:
                        return True
            return False
        except Exception as e:
            logger.warning(f"赛程判断失败: {e}")
            return False

    def get_state(self):
        session = self._cached("session", TTL["session"], lambda: self.api.get_current_session())
        if not session:
            result = {"live": False, "updated": datetime.now(timezone.utc).isoformat()}
            # 赛程判断走 60s 缓存：面板每 3s 轮询，直查 f1api.dev 会打爆源站并阻塞 worker
            if self._cached("race_in_progress", 60, self._race_session_in_progress):
                result["race_in_progress"] = True
                result["needs_openf1_auth"] = not getattr(self.api, "_auth_enabled", False)
            return result

        skey = session.get("session_key")
        drivers = self._cached(f"drivers_{skey}", TTL["drivers"], lambda: self.api.get_drivers(skey))

        dmap = {}
        for d in drivers:
            n = d.get("driver_number")
            dmap[n] = {
                "num": n,
                "tla": d.get("name_acronym", ""),
                "name": d.get("last_name") or d.get("full_name", ""),
                "team": d.get("team_name", ""),
                "color": d.get("team_colour", ""),
                "headshot": d.get("headshot_url", ""),
            }

        now = datetime.now(timezone.utc)

        # 实时排名（position 最新一条 + intervals 车距）
        standings = []
        try:
            pos_rows = self._cached(
                f"position_{skey}", TTL["position"],
                lambda: self.api.get_position(skey, (now - timedelta(minutes=3)).isoformat())
            )
            pos_map = self._latest_by_driver(pos_rows)

            gap_map = {}
            try:
                int_rows = self._cached(
                    f"intervals_{skey}", TTL["intervals"],
                    lambda: self.api.get_intervals(skey, (now - timedelta(minutes=2)).isoformat())
                )
                gap_map = self._latest_by_driver(int_rows)
            except Exception as e:
                logger.warning(f"intervals 获取失败: {e}")

            for n, row in sorted(pos_map.items(), key=lambda x: x[1].get("position", 99)):
                info = dmap.get(n, {"tla": str(n), "name": "", "team": "", "color": ""})
                gap = gap_map.get(n, {})
                gap_to_leader = gap.get("gap_to_leader")
                interval = gap.get("interval")
                if gap_to_leader is None and interval is not None:
                    gap_str = f"+{interval:.1f}s" if isinstance(interval, (int, float)) else ""
                elif isinstance(gap_to_leader, str):
                    gap_str = gap_to_leader
                elif isinstance(gap_to_leader, (int, float)):
                    gap_str = "领跑" if gap_to_leader == 0 else f"+{gap_to_leader:.1f}s"
                else:
                    gap_str = ""
                standings.append({
                    "position": row.get("position"),
                    "num": n,
                    "tla": info["tla"],
                    "name": info["name"],
                    "team": info["team"],
                    "color": info["color"],
                    "gap": gap_str,
                })
        except Exception as e:
            logger.warning(f"position 获取失败: {e}")

        # 赛事控制事件（旗帜/安全车）
        events = []
        try:
            rc = self._cached(f"race_control_{skey}", TTL["race_control"],
                              lambda: self.api.get_race_control(skey))
            cutoff = now - timedelta(minutes=30)
            for e in rc:
                try:
                    t = datetime.fromisoformat(e.get("date", "").replace("Z", "+00:00"))
                except Exception:
                    continue
                if t >= cutoff:
                    events.append({
                        "time": t.strftime("%H:%M:%S"),
                        "category": e.get("category", ""),
                        "flag": e.get("flag", ""),
                        "message": e.get("message", ""),
                    })
        except Exception as e:
            logger.warning(f"race_control 获取失败: {e}")

        # 天气
        weather = None
        try:
            w = self._cached(f"weather_{skey}", TTL["weather"],
                             lambda: self.api.get_weather(skey))
            if w:
                weather = {
                    "air": w.get("air_temperature"),
                    "track": w.get("track_temperature"),
                    "humidity": w.get("humidity"),
                    "rain": bool(w.get("rainfall")),
                    "wind": w.get("wind_speed"),
                }
        except Exception as e:
            logger.warning(f"weather 获取失败: {e}")

        # 最快圈
        fastest = None
        try:
            laps = self._cached(f"laps_{skey}", TTL["laps"],
                                lambda: self.api.get_laps(skey, (now - timedelta(minutes=15)).isoformat()))
            best = None
            for lp in laps:
                d = lp.get("lap_duration")
                if d and (best is None or d < best["lap_duration"]):
                    best = lp
            if best:
                d = best["lap_duration"]
                m, s = int(d // 60), d % 60
                fastest = {
                    "num": best.get("driver_number"),
                    "tla": dmap.get(best.get("driver_number"), {}).get("tla", ""),
                    "time": f"{m}:{s:05.2f}",
                    "lap": best.get("lap_number"),
                }
        except Exception as e:
            logger.warning(f"laps 获取失败: {e}")

        # 进站
        pits = []
        try:
            p_rows = self._cached(f"pit_{skey}", TTL["pit"],
                                  lambda: self.api.get_pit_stops(skey))
            for p in p_rows:
                pits.append({
                    "num": p.get("driver_number"),
                    "tla": dmap.get(p.get("driver_number"), {}).get("tla", ""),
                    "lap": p.get("lap_number"),
                    "duration": p.get("pit_duration"),
                })
        except Exception as e:
            logger.warning(f"pit 获取失败: {e}")

        # 轮胎策略
        stints = []
        try:
            s_rows = self._cached(f"stints_{skey}", TTL["stints"],
                                  lambda: self.api.get_stints(skey))
            for s in s_rows:
                stints.append({
                    "num": s.get("driver_number"),
                    "tla": dmap.get(s.get("driver_number"), {}).get("tla", ""),
                    "compound": s.get("compound"),
                    "lap_start": s.get("lap_start"),
                    "lap_end": s.get("lap_end"),
                })
        except Exception as e:
            logger.warning(f"stints 获取失败: {e}")

        # 车队无线电（文字转写优先，音频链接兜底）
        radio = []
        try:
            tr = self._cached(f"team_radio_{skey}", TTL["team_radio"],
                              lambda: self.api.get_team_radio(skey, (now - timedelta(minutes=20)).isoformat()))
            for r in tr[-20:]:
                try:
                    t = datetime.fromisoformat(r.get("date", "").replace("Z", "+00:00"))
                    tstr = t.strftime("%H:%M:%S")
                except Exception:
                    tstr = ""
                url = r.get("recording_url", "")
                if url:
                    transcriber.submit(url)
                radio.append({
                    "num": r.get("driver_number"),
                    "tla": dmap.get(r.get("driver_number"), {}).get("tla", ""),
                    "time": tstr,
                    "url": url,
                    "text": transcriber.get(url) if url else None,
                    "pending": transcriber.is_pending(url) if url else False,
                })
        except Exception as e:
            logger.warning(f"team_radio 获取失败: {e}")

        return {
            "live": True,
            "updated": now.isoformat(),
            "session": {
                "name": session.get("session_name", ""),
                "round": session.get("round") if "round" in session else None,
                "meeting": session.get("meeting_key"),
                "session_key": skey,
            },
            "standings": standings,
            "events": events,
            "weather": weather,
            "fastest": fastest,
            "pit_stops": pits,
            "stints": stints,
            "radio": radio,
        }


aggregator = LiveAggregator(api)

# 简单内存限流：防 /live/state 被刷爆（避免触发外部 OpenF1 请求）
_rate_lock = threading.Lock()
_rate_log = {}


def _rate_limited(ip: str, limit: int = 180, window: int = 60) -> bool:
    """滑动窗口限流，返回 True 表示超限。_rate_log 设上限防伪造IP导致内存无限增长"""
    now = time.time()
    with _rate_lock:
        if len(_rate_log) > 10000:
            # 超限兜底：清空重建（宁可短暂放开限流也不让内存膨胀）
            _rate_log.clear()
            logger.warning("限流表超上限已清空（疑似伪造XFF刷key）")
        times = [t for t in _rate_log.get(ip, []) if now - t < window]
        if len(times) >= limit:
            _rate_log[ip] = times
            return True
        times.append(now)
        _rate_log[ip] = times
    return False


def _client_ip() -> str:
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.remote_addr or "?"


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "f1-live"})


@app.route("/live/state")
def live_state():
    if _rate_limited(_client_ip()):
        return jsonify({"live": False, "error": "rate limited"}), 429
    try:
        return jsonify(aggregator.get_state())
    except Exception as e:
        logger.error(f"live/state 失败: {e}")
        return jsonify({"live": False, "error": str(e)}), 500


PANEL_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>F1 实时计时面板</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", monospace;
         background: #0b0d12; color: #e8e8e8; padding: 16px; }
  h1 { font-size: 18px; }
  .bar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 14px; }
  .btn { padding: 10px 18px; border: 0; border-radius: 8px; background: #e10600; color: #fff;
         font-size: 15px; font-weight: 700; cursor: pointer; }
  .btn:disabled { background: #444; cursor: not-allowed; }
  .hint { color: #777; font-size: 12px; }
  .card { background: #151821; border-radius: 10px; padding: 12px; margin-bottom: 10px; }
  .card h2 { font-size: 14px; color: #ffd166; margin-bottom: 8px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  td, th { padding: 4px 6px; text-align: left; border-bottom: 1px solid #22263a; }
  .pos { width: 28px; color: #999; }
  .bar-color { display: inline-block; width: 4px; height: 14px; border-radius: 2px; margin-right: 6px; vertical-align: -2px; }
  .gap { color: #8ec5ff; text-align: right; }
  .flag { color: #ff5252; }
  .safety { color: #ffb300; }
  .green { color: #4caf50; }
  .evt { font-size: 12px; padding: 3px 0; border-bottom: 1px solid #1e2230; }
  .evt .t { color: #666; margin-right: 8px; }
  .kv { display: grid; grid-template-columns: auto auto; gap: 4px 12px; font-size: 13px; }
  .tr { font-size: 12px; padding: 3px 0; border-bottom: 1px solid #1e2230; }
  .tr a { color: #6ea8fe; text-decoration: none; }
  .off { color: #777; text-align: center; padding: 40px 0; font-size: 15px; }
</style>
</head>
<body>
<div class="bar">
  <h1>🏎️ F1 实时计时面板</h1>
  <div id="pipArea">
    <button class="btn" id="pipBtn" onclick="openPip()">开启悬浮窗</button>
    <div class="hint" style="margin-top:4px">悬浮窗可叠放在视频播放器上方（Chrome 116+/Edge）</div>
  </div>
</div>
<div id="status" class="off">加载中…</div>
<div id="root"></div>
<script>
// 叠层/iframe 内：Document PiP 仅限顶层页面（NotAllowedError），隐藏悬浮窗按钮并提示
if (window.self !== window.top) {
  document.getElementById('pipArea').innerHTML =
    '<div class="hint" style="margin-top:4px">正在叠层/嵌入模式中显示，无需悬浮窗；独立打开本页可使用悬浮窗</div>';
}
</script>
<script>
async function openPip() {
  if (!('documentPictureInPicture' in window)) {
    alert('请使用 Chrome 116+ 或 Edge 浏览器');
    return;
  }
  const btn = document.getElementById('pipBtn');
  btn.disabled = true;
  try {
    const pip = await window.documentPictureInPicture.requestWindow({ width: 340, height: 620 });
    const html = await (await fetch('/live/pip')).text();
    pip.document.open();
    pip.document.write(html);
    pip.document.close();
  } catch (e) {
    alert('开启悬浮窗失败: ' + e);
  } finally {
    btn.disabled = false;
  }
}

function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

function render(s) {
  if (!s.live) {
    document.getElementById('status').className = 'off';
    let msg = '非比赛时段，暂无实时数据';
    if (s.race_in_progress && s.needs_openf1_auth) {
      msg = '🏁 比赛进行中，但实时数据需 OpenF1 订阅认证（约€9.90/月）。配置 OPENF1_USERNAME/PASSWORD 后开启。';
    } else if (s.race_in_progress) {
      msg = '🏁 比赛进行中，正在加载实时数据…';
    }
    document.getElementById('status').textContent = msg;
    document.getElementById('root').innerHTML = '';
    return;
  }
  document.getElementById('status').style.display = 'none';
  let html = '';

  // 排名
  html += '<div class="card"><h2>实时排名</h2><table>';
  s.standings.forEach(r => {
    const c = r.color ? '#' + r.color : '#666';
    html += `<tr><td class="pos">${r.position}</td><td><span class="bar-color" style="background:${c}"></span>${esc(r.tla)} ${esc(r.name)}</td><td class="gap">${esc(r.gap)}</td></tr>`;
  });
  html += '</table></div>';

  // 最快圈
  if (s.fastest) {
    html += `<div class="card"><h2>⚡ 最快圈</h2><div class="kv"><span>${esc(s.fastest.tla)}</span><span>${esc(s.fastest.time)} (L${s.fastest.lap})</span></div></div>`;
  }

  // 天气
  if (s.weather) {
    const rain = s.weather.rain ? '⚠️ 降雨中' : '无降雨';
    html += `<div class="card"><h2>🌤️ 天气</h2><div class="kv">
      <span>气温</span><span>${s.weather.air}°C</span>
      <span>赛道</span><span>${s.weather.track}°C</span>
      <span>湿度</span><span>${s.weather.humidity}%</span>
      <span>风速</span><span>${s.weather.wind}m/s</span>
      <span>降雨</span><span>${rain}</span></div></div>`;
  }

  // 赛事控制事件
  if (s.events.length) {
    html += '<div class="card"><h2>🚩 赛事事件</h2>';
    s.events.slice().reverse().slice(0, 12).forEach(e => {
      let cls = 'evt';
      let msg = esc(e.message);
      if (e.category === 'Flag' && /RED/.test(e.flag)) cls += ' flag';
      else if (/SAFETY CAR|VIRTUAL SAFETY CAR/.test(e.flag)) cls += ' safety';
      else if (/GREEN/.test(e.flag)) cls += ' green';
      html += `<div class="${cls}"><span class="t">${esc(e.time)}</span>${msg}</div>`;
    });
    html += '</div>';
  }

  // 进站 + 轮胎
  if (s.stints.length) {
    html += '<div class="card"><h2>🛞 轮胎策略</h2>';
    s.stints.forEach(st => {
      html += `<div class="tr">${esc(st.tla)} ${esc(st.compound)} L${st.lap_start}${st.lap_end ? '-' + st.lap_end : ''}</div>`;
    });
    html += '</div>';
  }
  if (s.pit_stops.length) {
    html += '<div class="card"><h2>🔧 进站</h2>';
    s.pit_stops.slice().reverse().slice(0, 10).forEach(p => {
      html += `<div class="tr">${esc(p.tla)} L${p.lap} ${esc(p.duration)}s</div>`;
    });
    html += '</div>';
  }

  // 车队无线电
  if (s.radio.length) {
    html += '<div class="card"><h2>📻 车队无线电</h2>';
    s.radio.slice().reverse().forEach(r => {
      let body = r.text ? esc(r.text) : (r.pending ? '<span style="color:#888">转写中…</span>' : `<a href="${esc(r.url)}" target="_blank">▶ 播放</a>`);
      html += `<div class="tr"><span class="t">${esc(r.time)}</span><b>${esc(r.tla)}</b>: ${body}</div>`;
    });
    html += '</div>';
  }

  document.getElementById('root').innerHTML = html;
}

async function tick() {
  try {
    const s = await (await fetch('/live/state')).json();
    render(s);
  } catch (e) {}
}
tick();
setInterval(tick, 3000);
</script>
</body>
</html>"""


PIP_PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>F1 悬浮窗</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", monospace;
         background: var(--bg, #0b0d12); color: #e8e8e8; height: 100vh; }
  .toolbar { position: sticky; top: 0; z-index: 10; display: flex; align-items: center; gap: 4px;
             padding: 4px 6px; background: rgba(20,22,30,0.72); }
  .toolbar button { background: #22263a; color: #ccc; border: 1px solid #333; border-radius: 4px;
                    padding: 1px 8px; font-size: 12px; cursor: pointer; }
  .toolbar button.on { background: #e10600; color: #fff; border-color: #e10600; }
  .toolbar .sep { color: #444; }
  .toolbar .lbl { font-size: 11px; color: #999; }
  .toolbar input[type=range] { width: 56px; }
  .toolbar .spacer { flex: 1; }
  #content { padding: 8px 10px; height: calc(100vh - 32px); overflow-y: auto; }
  #content.compact .sec, #content.compact .kv, #content.compact .tr { display: none; }
  .head { font-size: 13px; color: #ffd166; margin-bottom: 6px; font-weight: 700; }
  .off { color: #777; text-align: center; padding: 30px 10px; font-size: 13px; }
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  td { padding: 3px 4px; border-bottom: 1px solid #22263a; }
  .pos { width: 22px; color: #999; }
  .bar-color { display: inline-block; width: 3px; height: 11px; border-radius: 2px; margin-right: 5px; vertical-align: -1px; }
  .gap { color: #8ec5ff; text-align: right; }
  .sec { font-size: 12px; color: #8ec5ff; margin: 8px 0 4px; font-weight: 700; }
  .evt { font-size: 11px; padding: 2px 0; border-bottom: 1px solid #1e2230; }
  .evt .t { color: #666; margin-right: 6px; }
  .flag { color: #ff5252; } .safety { color: #ffb300; } .green { color: #4caf50; }
  .kv { font-size: 12px; display: grid; grid-template-columns: auto auto; gap: 2px 10px; }
  .tr { font-size: 11px; padding: 2px 0; border-bottom: 1px solid #1e2230; }
  .tr a { color: #6ea8fe; text-decoration: none; }
</style>
</head>
<body>
<div class="toolbar">
  <button onclick="zoom(-0.1)" title="缩小字体">A-</button>
  <button onclick="zoom(+0.1)" title="放大字体">A+</button>
  <button id="compactBtn" onclick="toggleCompact()">紧凑</button>
  <span class="sep">|</span>
  <span class="lbl">明暗</span>
  <input type="range" id="op" min="0" max="100" step="5" oninput="setBright(this.value)">
  <span class="spacer"></span>
  <button onclick="window.close()" title="关闭">✕</button>
</div>
<div id="content"><div id="box"><div class="off">加载中…</div></div></div>
<script>
function esc(s) { return String(s == null ? '' : s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

let z = parseFloat(localStorage.getItem('f1pip_zoom') || '1');
let bright = parseInt(localStorage.getItem('f1pip_bright') || '25');
let compact = localStorage.getItem('f1pip_compact') === '1';

function applyZoom() {
  document.getElementById('content').style.zoom = z;
  localStorage.setItem('f1pip_zoom', z);
}
function zoom(d) { z = Math.min(1.5, Math.max(0.7, Math.round((z + d) * 100) / 100)); applyZoom(); }
function setBright(v) {
  bright = parseInt(v);
  const s = Math.round(5 + 25 * (bright / 100));  // 5..30
  document.body.style.background = `rgb(${s}, ${s + 2}, ${s + 7})`;
  localStorage.setItem('f1pip_bright', bright);
}
function toggleCompact() {
  compact = !compact;
  document.getElementById('content').classList.toggle('compact', compact);
  document.getElementById('compactBtn').classList.toggle('on', compact);
  localStorage.setItem('f1pip_compact', compact ? '1' : '0');
}
document.getElementById('op').value = bright;
applyZoom();
setBright(bright);
if (compact) { document.getElementById('content').classList.add('compact'); document.getElementById('compactBtn').classList.add('on'); }

function render(s) {
  if (!s.live) {
    let msg = '非比赛时段，暂无实时数据';
    if (s.race_in_progress && s.needs_openf1_auth) msg = '比赛进行中，但实时数据需 OpenF1 订阅认证';
    else if (s.race_in_progress) msg = '比赛进行中，正在加载…';
    document.getElementById('box').innerHTML = `<div class="off">${msg}</div>`;
    return;
  }
  let html = '<div class="head">🏎️ 实时计时</div><table>';
  s.standings.forEach(r => {
    const c = r.color ? '#' + r.color : '#666';
    html += `<tr><td class="pos">${r.position}</td><td><span class="bar-color" style="background:${c}"></span>${esc(r.tla)}</td><td class="gap">${esc(r.gap)}</td></tr>`;
  });
  html += '</table>';

  if (s.fastest) html += `<div class="sec">⚡ 最快圈</div><div class="kv"><span>${esc(s.fastest.tla)}</span><span>${esc(s.fastest.time)}</span></div>`;

  if (s.weather) {
    const rain = s.weather.rain ? '⚠️ 降雨中' : '无降雨';
    html += `<div class="sec">🌤️ 天气</div><div class="kv"><span>气温</span><span>${s.weather.air}°C</span><span>赛道</span><span>${s.weather.track}°C</span><span>降雨</span><span>${rain}</span></div>`;
  }

  if (s.events.length) {
    html += '<div class="sec">🚩 事件</div>';
    s.events.slice().reverse().slice(0, 6).forEach(e => {
      let cls = 'evt';
      if (e.category === 'Flag' && /RED/.test(e.flag)) cls += ' flag';
      else if (/SAFETY CAR|VIRTUAL SAFETY CAR/.test(e.flag)) cls += ' safety';
      else if (/GREEN/.test(e.flag)) cls += ' green';
      html += `<div class="${cls}"><span class="t">${esc(e.time)}</span>${esc(e.message)}</div>`;
    });
  }

  if (s.radio.length) {
    html += '<div class="sec">📻 车队无线电</div>';
    s.radio.slice().reverse().slice(0, 8).forEach(r => {
      let body = r.text ? esc(r.text) : (r.pending ? '<span style="color:#777">转写中…</span>' : `<a href="${esc(r.url)}" target="_blank">▶</a>`);
      html += `<div class="tr"><span class="t">${esc(r.time)}</span><b>${esc(r.tla)}</b> ${body}</div>`;
    });
  }

  document.getElementById('box').innerHTML = html;
}
async function tick() {
  try { render(await (await fetch('/live/state')).json()); } catch (e) {}
}
tick();
setInterval(tick, 3000);
</script>
</body>
</html>"""


@app.route("/live")
def live_page():
    return render_template_string(PANEL_PAGE)


@app.route("/live/pip")
def live_pip():
    return render_template_string(PIP_PAGE)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("LIVE_PORT", "8091")))
