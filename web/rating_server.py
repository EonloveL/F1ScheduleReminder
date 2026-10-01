"""
F1车手评分 - 网页表单服务（Flask）

端点：
- GET  /health                 健康检查
- GET  /r/<race_key>/<token>   个人评分表单页
- POST /r/<race_key>/<token>   提交评分（覆盖式）
- GET  /board/<race_key>       实时榜单页
- GET  /exports/<filename>     截止后CSV下载

数据源：
- ratings_store: 票数存储
- f1_api: 当场正赛参赛名单（缓存友好）

运行：python web/rating_server.py  （监听 0.0.0.0:8080）
"""

import logging
import os
import sys
import time
from typing import Any, Dict

from flask import Flask, abort, jsonify, render_template_string, request, send_from_directory

current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
sys.path.insert(0, project_root)

from common import F1API
from common.ratings_store import RatingsStore, EXPORTS_DIR

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
store = RatingsStore()
f1_api = F1API()

PAGE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }}</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, "PingFang SC", "Microsoft YaHei", sans-serif;
         background: #0f1115; color: #eee; padding: 16px; max-width: 640px; margin: 0 auto; }
  h1 { font-size: 20px; margin-bottom: 4px; }
  .sub { color: #999; font-size: 13px; margin-bottom: 16px; }
  .card { background: #1a1d26; border-radius: 12px; padding: 14px; margin-bottom: 10px; }
  .driver { display: flex; align-items: center; justify-content: space-between; gap: 8px; }
  .dname { font-weight: 600; font-size: 15px; }
  .dteam { color: #888; font-size: 12px; margin-top: 2px; }
  .scores { display: flex; gap: 4px; flex-wrap: wrap; justify-content: flex-end; }
  .scores button { width: 30px; height: 34px; border: 1px solid #333; border-radius: 6px;
                   background: #242836; color: #ccc; font-size: 13px; cursor: pointer; }
  .scores button.on { background: #e10600; border-color: #e10600; color: #fff; font-weight: 700; }
  .submit { width: 100%; padding: 14px; border: 0; border-radius: 10px; background: #e10600;
            color: #fff; font-size: 16px; font-weight: 700; margin-top: 8px; }
  .submit:disabled { background: #444; }
  .msg { text-align: center; padding: 10px; font-size: 14px; }
  .ok { color: #4caf50; } .err { color: #ff5252; }
  .row { display: flex; justify-content: space-between; padding: 8px 4px; border-bottom: 1px solid #262a38; }
  .row:last-child { border-bottom: 0; }
  .avg { color: #ffd166; font-weight: 700; }
  a { color: #6ea8fe; }
</style>
</head>
<body>
{{ body | safe }}
</body>
</html>"""

FORM_BODY = """
<h1>🏁 {{ race_name }}</h1>
<div class="sub">车手评分 · 1-10分 · 可随时修改重新提交 · {{ status_text }}</div>
{% if pending %}
<div class="card" style="text-align:center;color:#ffd166">⏰ 投票尚未开启<br>
<span style="font-size:13px;color:#999">{{ pending_tip }}，开启后可回到本页打分（防提前刷票）</span></div>
{% endif %}
<form id="f">
{% for d in drivers %}
<div class="card driver">
  <div>
    <div class="dname">P{{ d.position }} {{ d.name }}</div>
    <div class="dteam">{{ d.team }}</div>
  </div>
  <div class="scores" data-driver="{{ d.driver_id }}">
    {% for i in range(1, 11) %}
    <button type="button" data-s="{{ i }}" class="{{ 'on' if votes.get(d.driver_id) == i }}">{{ i }}</button>
    {% endfor %}
  </div>
</div>
{% endfor %}
<button class="submit" id="sub" type="submit"{{ ' disabled' if pending }}>{{ '更新我的评分' if votes else '提交评分' }}</button>
<div class="msg" id="msg"></div>
</form>
<script>
const picked = {{ votes_js }};
const PENDING = {{ 'true' if pending else 'false' }};
document.querySelectorAll('.scores').forEach(box => {
  box.addEventListener('click', e => {
    if (e.target.tagName !== 'BUTTON') return;
    box.querySelectorAll('button').forEach(b => b.classList.remove('on'));
    e.target.classList.add('on');
    picked[box.dataset.driver] = parseInt(e.target.dataset.s);
  });
});
document.getElementById('f').addEventListener('submit', async e => {
  e.preventDefault();
  if (PENDING) { msg('⚠️ 投票尚未开启，正赛开始1小时后开放', 'err'); return; }
  if (!Object.keys(picked).length) { msg('请先给至少一位车手打分', 'err'); return; }
  document.getElementById('sub').disabled = true;
  try {
    const r = await fetch(location.pathname, {method: 'POST',
      headers: {'Content-Type': 'application/json'}, body: JSON.stringify(picked)});
    const j = await r.json();
    if (j.ok) { msg('✅ ' + j.message, 'ok'); document.getElementById('sub').textContent = '更新我的评分'; }
    else { msg('⚠️ ' + (j.message || '提交失败'), 'err'); }
  } catch (e2) { msg('⚠️ 网络错误，请重试', 'err'); }
  document.getElementById('sub').disabled = PENDING ? true : false;
});
function msg(t, c) { const m = document.getElementById('msg'); m.textContent = t; m.className = 'msg ' + c; }
</script>
"""

BOARD_BODY = """
<h1>📊 {{ race_name }} · 评分榜</h1>
<div class="sub">{{ status_text }}</div>
<div class="card">
{% for row in board %}
<div class="row"><span>{{ loop.index }}. {{ names.get(row.driver_id, row.driver_id) }}</span>
<span><span class="avg">{{ row.avg }}</span> 分 · {{ row.count }}票</span></div>
{% endfor %}
{% if not board %}<div class="row">暂无评分数据</div>{% endif %}
</div>
{% if dotd %}<div class="card">🏆 本场最佳车手：<b>{{ names.get(dotd, dotd) }}</b></div>{% endif %}
"""

ERROR_BODY = """<div class="card"><h1>⚠️ {{ message }}</h1>
<div class="sub">{{ hint }}</div></div>"""


def render(body_template: str, title: str, **ctx) -> str:
    body = render_template_string(body_template, **ctx)
    return render_template_string(PAGE, title=title, body=body)


def _roster_from_results(round_num, session_type: str, season: int) -> Dict[str, Dict]:
    try:
        result = f1_api.get_session_results(round_num, session_type, season=season)
        entries = (result or {}).get("entries") or []
        return {
            e["driver_id"]: {"name": e["driver_name"], "team": e["team_name"], "position": e["position"]}
            for e in entries
        }
    except Exception as e:
        logger.error(f"获取参赛名单失败({session_type} R{round_num}): {e}")
        return {}


_ROSTER_TTL = 3600  # 临时名册（排位/上场名单）缓存 1h；正赛完赛后定格永久缓存
_roster_cache: Dict[tuple, tuple] = {}  # (season, round) -> (过期时间戳, 名册)


def get_race_drivers(season: int, round_num: int) -> Dict[str, Dict]:
    """当场正赛参赛名单 {driver_id: {name, team, position}}

    分层名册源（2026-09-23 评分前置改造：投票开启=正赛开始+1h，此刻正赛在跑、
    正赛成绩必然为空，旧实现只认正赛成绩 → 表单必然空白）：
    正赛成绩（完赛后定格永久缓存）→ 排位成绩（比赛周六后可用）→ 最近一场已完赛正赛名单
    """
    key = (season, int(round_num))
    hit = _roster_cache.get(key)
    if hit and time.time() < hit[0]:
        return hit[1]
    drivers = _roster_from_results(round_num, "race", season)
    if drivers:
        _roster_cache[key] = (time.time() + 10 * 365 * 86400, drivers)  # 完赛定格
        return drivers
    drivers = _roster_from_results(round_num, "qualifying", season)
    if not drivers:
        for r in range(int(round_num) - 1, 0, -1):
            drivers = _roster_from_results(r, "race", season)
            if drivers:
                break
    _roster_cache[key] = (time.time() + _ROSTER_TTL, drivers)
    return drivers


def get_driver_names(season: int, round_num: int) -> Dict[str, str]:
    return {d: info["name"] for d, info in get_race_drivers(season, round_num).items()}


@app.route("/health")
def health():
    return jsonify({"ok": True, "service": "f1-rating"})


# ==================== 车手头像/车队图标（打包静态优先 + 代理兜底） ====================
# 车手头像（OpenF1/F1.com 缩略图）与车队图标（用户提供）已打包进 web/static/avatars/，
# 随部署包分发，QQ 直接取本站静态文件；打包外的车手（如季中新代打）走磁盘缓存代理兜底。
AVATAR_BUNDLED_DRIVERS = os.path.join(current_dir, "static", "avatars", "drivers")
AVATAR_BUNDLED_TEAMS = os.path.join(current_dir, "static", "avatars", "teams")
AVATAR_CACHE_DIR = os.path.join(project_root, "data", "avatar_cache")
AVATAR_TTL = 30 * 24 * 3600   # 磁盘缓存 30 天
_AVATAR_FALLBACK_PREFIX = "https://media.formula1.com/d_driver_fallback_image.png"
# 1x1 透明 PNG：全部来源失败时返回，避免 QQ 卡片显示"资源加载失败"
_TRANSPARENT_PNG = __import__("base64").b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNgYAAAAAMAAWmmWQ8AAAAASUVORK5CYII=")


def _avatar_source_url(surname: str):
    """解析头像源 URL（兜底链用）：OpenF1 元数据（剥 fallback 前缀）→ F1.com 命名规则构造"""
    try:
        from common.f1_api import LocalCache
        meta = LocalCache().load("openf1_driver_meta") or {}
        m = meta.get(surname)
        if m and m.get("headshot"):
            url = m["headshot"]
            # fallback 前缀路径 QQ 加载不稳：剥前缀走标准路径（同源同图，已实测 200）
            if url.startswith(_AVATAR_FALLBACK_PREFIX):
                url = "https://media.formula1.com" + url[len(_AVATAR_FALLBACK_PREFIX):]
            return url
    except Exception:
        pass
    # OpenF1 无头像（如角田）：按 F1.com 命名规则构造（名3字母+姓3字母+01，已实测 200）
    try:
        from common import drivers_profile as dp
        for did, p in dp.get_drivers().items():
            if did.split("_")[-1].lower() == surname:
                parts = (p.get("name_en") or "").split()
                if len(parts) >= 2:
                    g, f = parts[0], parts[-1]
                    code = (g[:3] + f[:3]).upper() + "01"
                    return (f"https://media.formula1.com/content/dam/fom-website/drivers/"
                            f"{f[0].upper()}/{code}_{g}_{f}/{code.lower()}.png.transform/1col/image.png")
    except Exception:
        pass
    return None


@app.route("/avatar/<surname>.png")
@app.route("/v2/avatar/<surname>.png")
@app.route("/v3/avatar/<surname>.png")
@app.route("/v4/avatar/<surname>.png")
@app.route("/v5/avatar/<surname>.png")
@app.route("/v6/avatar/<surname>.png")
@app.route("/v7/avatar/<surname>.png")
@app.route("/v8/avatar/<surname>.png")
def avatar_image(surname):
    """车手头像：打包静态文件优先 → 磁盘缓存 → 源站抓取 → 1px 透明兜底"""
    import re as _re
    import time as _t
    import requests as _rq
    if not _re.fullmatch(r"[a-z0-9]+", surname or ""):
        abort(404)
    filename = f"{surname}.png"
    # ① 打包静态（随部署包分发，最优先）
    if os.path.exists(os.path.join(AVATAR_BUNDLED_DRIVERS, filename)):
        return send_from_directory(AVATAR_BUNDLED_DRIVERS, filename, mimetype="image/png",
                                   max_age=7 * 86400)
    # ② 磁盘缓存（打包外车手抓取过一次后 30 天免抓）
    os.makedirs(AVATAR_CACHE_DIR, exist_ok=True)
    path = os.path.join(AVATAR_CACHE_DIR, filename)
    if (os.path.exists(path) and os.path.getsize(path) > 0
            and _t.time() - os.path.getmtime(path) < AVATAR_TTL):
        return send_from_directory(AVATAR_CACHE_DIR, filename, mimetype="image/png",
                                   max_age=7 * 86400)
    # ③ 源站抓取（新代打等打包外车手）
    url = _avatar_source_url(surname)
    if url:
        try:
            r = _rq.get(url, timeout=15,
                        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                        proxies={"http": None, "https": None})
            if r.status_code == 200 and (r.headers.get("Content-Type") or "").startswith("image/"):
                with open(path, "wb") as f:
                    f.write(r.content)
                return send_from_directory(AVATAR_CACHE_DIR, filename, mimetype="image/png",
                                           max_age=7 * 86400)
            logger.warning(f"头像源不可用 {surname}: HTTP {r.status_code}")
        except Exception as e:
            logger.warning(f"头像抓取失败 {surname}: {e}")
    # ④ 1px 透明兜底（卡片不出现"资源加载失败"）
    return app.response_class(_TRANSPARENT_PNG, mimetype="image/png",
                              headers={"Cache-Control": "public, max-age=3600"})


@app.route("/avatar/team/<key>.png")
@app.route("/v2/avatar/team/<key>.png")
@app.route("/v3/avatar/team/<key>.png")
@app.route("/v4/avatar/team/<key>.png")
@app.route("/v5/avatar/team/<key>.png")
@app.route("/v6/avatar/team/<key>.png")
@app.route("/v7/avatar/team/<key>.png")
@app.route("/v8/avatar/team/<key>.png")
def avatar_team_image(key):
    """车队图标：打包静态文件（web/static/avatars/teams/），缺失返回 1px 透明图"""
    import re as _re
    if not _re.fullmatch(r"[a-z0-9_]+", key or ""):
        abort(404)
    filename = f"{key}.png"
    if os.path.exists(os.path.join(AVATAR_BUNDLED_TEAMS, filename)):
        return send_from_directory(AVATAR_BUNDLED_TEAMS, filename, mimetype="image/png",
                                   max_age=7 * 86400)
    return app.response_class(_TRANSPARENT_PNG, mimetype="image/png",
                              headers={"Cache-Control": "public, max-age=3600"})


# ==================== 遥测对比（F1官方livetiming归档，免费） ====================

_telemetry_sessions_cache: Dict[str, Any] = {}  # (year, round, session) -> TelemetrySession


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(os.path.join(current_dir, "static"), filename)


@app.route("/telemetry")
def telemetry_page():
    with open(os.path.join(current_dir, "telemetry.html"), encoding="utf-8") as f:
        return f.read()


@app.route("/telemetry/pro")
def telemetry_overlay_page():
    """第三方遥测仪表盘（F1Cosmos）iframe 嵌入页，带透明度/亮度/对比度悬浮控制条"""
    with open(os.path.join(current_dir, "telemetry_overlay.html"), encoding="utf-8") as f:
        return f.read()


@app.route("/telemetry/api/sessions")
def telemetry_sessions():
    """最近6场已结束分站（含可用环节），供页面下拉选择"""
    from datetime import datetime, timezone
    out = []
    now = datetime.now(timezone.utc)
    for race in reversed(f1_api.get_schedule()):
        try:
            rt = race.get("time", "00:00:00Z").replace("Z", "")
            rdt = datetime.fromisoformat(f"{race['date']}T{rt}").replace(tzinfo=timezone.utc)
            if rdt >= now:
                continue
        except Exception:
            continue
        sess_types = [s["type"] for s in f1_api.get_all_sessions(race)]
        out.append({"year": f1_api.season, "round": race["round"],
                    "raceName": race.get("raceName", ""), "date": race.get("date", ""),
                    "sessions": sess_types})
        if len(out) >= 6:
            break
    return jsonify(out)


def _get_telemetry_session(year: int, round_num: str, session_type: str):
    """带进程内缓存的 TelemetrySession（避免重复解析流文件）"""
    from common.telemetry_analysis import TelemetrySession
    key = f"{year}_{round_num}_{session_type}"
    ts = _telemetry_sessions_cache.get(key)
    if ts:
        return ts
    race = None
    for r in f1_api.get_schedule_for_year(year):
        if r.get("round") == str(round_num):
            race = r
            break
    if not race:
        return None
    ts = TelemetrySession(year, race.get("raceName", ""), session_type, schedule_race=race)
    if len(_telemetry_sessions_cache) > 6:
        _telemetry_sessions_cache.clear()
    _telemetry_sessions_cache[key] = ts
    return ts


@app.route("/telemetry/api/meta")
def telemetry_meta():
    """环节元数据：车手列表（TLA/姓名/车队/配色）+ 每人圈数（只解析圈速流，不加载遥测）"""
    year_arg = request.args.get("year", "")
    if not year_arg.isdigit():
        return jsonify({"error": "year 参数非法"}), 400
    year = int(year_arg)
    rnd = request.args.get("round", "")
    sess = request.args.get("session", "race")
    try:
        ts = _get_telemetry_session(year, rnd, sess)
        if not ts:
            return jsonify({"error": "分站不存在"}), 404
        ts._load_lap_windows()  # 轻量：只读TimingData，圈窗口UTC换算在compare时才做
        drivers = []
        for tla, d in ts.drivers.items():
            laps = ts._raw_laps.get(tla, [])
            fastest = min(laps, key=lambda l: l["time"]) if laps else None
            drivers.append({"tla": tla, "name": d["name"], "team": d["team"],
                            "color": d["color"], "laps": len(laps),
                            "fastest_no": fastest["no"] if fastest else None,
                            "fastest_time": fastest["time"] if fastest else None})
        return jsonify({"event": ts.event_name, "session": sess, "drivers": drivers})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.exception("telemetry meta失败")
        return jsonify({"error": f"数据加载失败: {type(e).__name__}: {e}"}), 500


@app.route("/telemetry/api/compare")
def telemetry_compare():
    """遥测对比数据：drivers=NOR,VER&lap=圈号或fastest"""
    year_arg = request.args.get("year", "")
    if not year_arg.isdigit():
        return jsonify({"error": "year 参数非法"}), 400
    year = int(year_arg)
    rnd = request.args.get("round", "")
    sess = request.args.get("session", "race")
    tlas = [t.strip().upper() for t in request.args.get("drivers", "").split(",") if t.strip()][:4]
    lap_arg = request.args.get("lap", "")
    lap_no = int(lap_arg) if lap_arg.isdigit() else None

    if len(tlas) < 2:
        return jsonify({"error": "至少选择2位车手"}), 400
    try:
        ts = _get_telemetry_session(year, rnd, sess)
        if not ts:
            return jsonify({"error": "分站不存在"}), 404
        ts._interest = tlas
        data = ts.get_comparison_json(tlas, lap_no)
        if not data:
            return jsonify({"error": "遥测数据不足（车手可能未完赛或该环节无数据）"}), 404
        return jsonify(data)
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        logger.exception("telemetry compare失败")
        return jsonify({"error": f"数据加载失败: {type(e).__name__}: {e}"}), 500



@app.route("/r/<key>", methods=["GET", "POST"])
def rate_entry(key):
    """群聊评分入口：展示验证码输入页；验证通过后跳转到个人token表单"""
    code = request.args.get("code", "")
    if request.method == "POST":
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "message": "请求格式错误"}), 400
        code_input = str(payload.get("code", ""))
        token = store.verify_code(code_input)
        if token:
            return jsonify({"ok": True, "token": token})
        return jsonify({"ok": False, "message": "验证码无效、已使用或已过期（5分钟）"}), 400

    race = store.get_race(key)
    if not race:
        return render(ERROR_BODY, "场次不存在", message="该评分场次不存在", hint=""), 404

    # 标记：验证码来自群聊 /rate, 链接可公开但验证码作废
    page_body = f"""<h1>🔒 身份验证</h1>
<div class="sub">{race['race_name']} · 车手评分 · 验证码一次有效</div>
<div class="card">
<form id="f">
  <label style="color:#ccc;display:block;margin-bottom:10px">请输入群内机器人发给你的 <b>6位验证码</b></label>
  <input type="text" id="code" name="code" placeholder="如：AB3X9K" maxlength="6" autocomplete="off"
         style="width:100%;padding:12px;border:1px solid #333;border-radius:8px;background:#242836;color:#fff;font-size:20px;text-align:center;letter-spacing:4px;text-transform:uppercase">
  <button type="submit" class="submit" style="margin-top:12px">验证身份</button>
</form>
<div class="msg" id="msg"></div>
</div>
<div class="card" style="margin-top:8px"><a href="javascript:history.back()">← 返回重试</a></div>
<script>
document.getElementById('f').addEventListener('submit', async e => {{
  e.preventDefault();
  const cd = document.getElementById('code').value.trim();
  if (!cd) {{ msg('请输入验证码', 'err'); return; }}
  const r = await fetch(location.pathname, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body:JSON.stringify({{code:cd}})}});
  const j = await r.json();
  if (j.ok) {{ location.href = '/r/{key}/' + j.token; }}
  else {{ msg('⚠️ ' + (j.message || '验证失败'), 'err'); }}
}});
function msg(t, c) {{ const m = document.getElementById('msg'); m.textContent = t; m.className = 'msg ' + c; }}
</script>"""
    return render_template_string(PAGE, title=f"{race['race_name']} 身份验证", body=page_body)


@app.route("/r/<key>/<token>", methods=["GET", "POST"])
def rate(key, token):
    race = store.get_race(key)
    if not race:
        return render(ERROR_BODY, "链接无效", message="评分链接无效", hint="请在群里发送 /rate 获取你的专属链接"), 404

    member = store.resolve_token(key, token)
    if not member:
        return render(ERROR_BODY, "链接无效", message="评分链接无效或已过期", hint="请在群里发送 /rate 重新获取"), 403

    if request.method == "POST":
        scores = request.get_json(silent=True)
        if not isinstance(scores, dict):
            return jsonify({"ok": False, "message": "请求格式错误"}), 400
        result = store.submit_votes(key, token, scores)
        if result is True:
            return jsonify({"ok": True, "message": f"已记录你对 {len(scores)} 位车手的评分（可再次修改）"})
        if result is False:
            return jsonify({"ok": False, "message": "本场评分已截止"}), 400
        if result == "pending":
            return jsonify({"ok": False, "message": "投票尚未开启，正赛开始1小时后开放"}), 400
        return jsonify({"ok": False, "message": "提交无效"}), 400

    import json as _json
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    votes = store.get_member_votes(key, member)
    drivers_map = get_race_drivers(race["season"], race["round"])
    drivers = sorted(
        ({"driver_id": d, **info} for d, info in drivers_map.items()),
        key=lambda x: x["position"]
    )
    # 比赛周预创建条目：未到开启时刻（正赛开始+1h）表单只读，展示开放时间
    open_at = store.race_open_at(race)
    pending = bool(open_at and datetime.now(timezone.utc) < open_at)
    pending_tip = ""
    if pending:
        pending_tip = ("投票将于 "
                       + open_at.astimezone(ZoneInfo("Asia/Shanghai")).strftime('%m月%d日 %H:%M')
                       + "（北京时间）开启")
    if race.get("closed"):
        status = "评分已截止，仅可查看"
    elif pending:
        status = "投票未开启"
    else:
        status = "评分进行中"
    return render(
        FORM_BODY, f"{race['race_name']} 车手评分",
        race_name=race["race_name"], drivers=drivers, votes=votes,
        votes_js=_json.dumps(votes), status_text=status,
        pending=pending, pending_tip=pending_tip
    )


@app.route("/board/<key>")
def board(key):
    race = store.get_race(key)
    if not race:
        return render(ERROR_BODY, "不存在", message="场次不存在", hint=""), 404
    agg = store.aggregate(key)
    names = get_driver_names(race["season"], race["round"])
    closed = race.get("closed", False)
    if closed:
        status = "已截止"
    else:
        from datetime import datetime as _dt, timezone as _tz
        _oa = store.race_open_at(race)
        status = ("投票未开启（正赛开始1小时后开放）"
                  if _oa and _dt.now(_tz.utc) < _oa else "评分进行中（实时）")
    return render(
        BOARD_BODY, f"{race['race_name']} 评分榜",
        race_name=race["race_name"], board=agg, names=names, dotd=race.get("dotd"),
        closed=closed, status_text=status
    )


@app.route("/exports/<path:filename>")
def exports(filename):
    # 仅允许图表PNG（QQ拉取图片需要公网URL），CSV等数据文件不对外提供
    if not filename.endswith(".png"):
        abort(404)
    if not (filename.startswith("ratings_chart_") or filename.startswith("telemetry_")):
        abort(404)
    return send_from_directory(EXPORTS_DIR, filename)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("RATING_PORT", "8080")))
