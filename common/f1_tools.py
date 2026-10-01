"""
F1赛程提醒机器人 - F1数据工具层（供AI function calling主动调用）

对应Agent架构中的工具薄适配层：F1_TOOLS 为 OpenAI 风格的 function schema，
build_handlers() 返回 {tool_name: callable} 处理函数字典，
由 LLMAssistant 的工具循环（_chat_once / _l1_collect）执行。

升级件查询参数解析与分站定位（parse_upgrade_query / find_race_for_query）
同时被 /upgrades、/upgrade 群指令与AI工具共用，故一并收敛到本模块。
"""

import logging
from typing import Dict

logger = logging.getLogger(__name__)


def _lap_to_sec(t) -> float:
    """'1:32.123' / '92.123' 格式的圈速转秒数；无法解析返回 0"""
    if not t:
        return 0.0
    s = str(t).strip().lstrip("FL").strip()
    try:
        if ":" in s:
            m, sec = s.split(":", 1)
            return int(m) * 60 + float(sec)
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _median(vals):
    """中位数"""
    vals = sorted(v for v in vals if v)
    n = len(vals)
    if not n:
        return 0.0
    mid = n // 2
    return vals[mid] if n % 2 else (vals[mid - 1] + vals[mid]) / 2


# ===== 工具 schema =====
F1_TOOLS = [
    {"type": "function", "function": {"name": "get_driver_standings", "description": "获取F1车手积分榜（排名/姓名/车队/积分/胜场）。year为赛季年份（如2025），留空为当前赛季；历史赛季同样可用（1950年至今）", "parameters": {"type": "object", "properties": {"year": {"type": "integer", "description": "赛季年份，留空为当前赛季"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_constructor_standings", "description": "获取F1车队积分榜。year为赛季年份（如2025），留空为当前赛季", "parameters": {"type": "object", "properties": {"year": {"type": "integer", "description": "赛季年份，留空为当前赛季"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_next_race", "description": "获取下一站比赛（名称/赛道/各环节北京时间）", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "get_race_results", "description": "获取某站某环节成绩完整排名。session可选fp1/fp2/fp3/qualifying/sprint/race，默认race；year为赛季年份（如2025），留空为当前赛季，历史赛季同样可用", "parameters": {"type": "object", "properties": {"round": {"type": "integer", "description": "分站轮次"}, "session": {"type": "string", "description": "环节"}, "year": {"type": "integer", "description": "赛季年份，留空为当前赛季"}}, "required": ["round"]}}},
    {"type": "function", "function": {"name": "get_season_results", "description": "获取一个赛季已完成各站正赛成绩。year为赛季年份（如2025），留空为当前赛季（仅最近10站、每站前10）；指定year时返回该赛季全部已完成站次，每站名次由top_n控制：问冠军/冠军榜传1（默认），问冠亚季军/领奖台传3，问亚军传2，问积分区传10，问完整排名传20。用于分析车手积分/名次趋势或整理历史赛季成绩", "parameters": {"type": "object", "properties": {"year": {"type": "integer", "description": "赛季年份，留空为当前赛季"}, "top_n": {"type": "integer", "description": "每站返回前N名，指定year时默认1"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_pu_quota", "description": "获取每位车手动力单元各部件用量及总量，评估超额罚退风险。year为赛季年份（如2023），留空为当前赛季；历史赛季返回该赛季末的累计用量快照（FIA官方文档，2019年起有；2019-2025含MGU-H列，2026起取消MGU-H）", "parameters": {"type": "object", "properties": {"year": {"type": "integer", "description": "赛季年份，留空为当前赛季"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_upgrades", "description": "获取赛车升级件明细（支持历史赛季、轮次与车队+分站组合）。gp_or_team为分站地点、车队名（中英文）、轮次（如R5）或其组合（如“法拉利 蒙扎”），留空则取下一站；year为赛季年份（如2024），留空为当前赛季。只传year不传gp_or_team时，一次返回该赛季各车队升级数量汇总——“哪支车队升级最多”类问题务必用这种方式，不要逐站逐队多次调用", "parameters": {"type": "object", "properties": {"gp_or_team": {"type": "string", "description": "分站地点/车队名/轮次，可组合；只问赛季汇总时留空"}, "year": {"type": "integer", "description": "赛季年份"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_circuit_info", "description": "获取赛道档案（长度/弯数/正赛圈数/圈速纪录/下压力档/轮胎负荷档/海拔/时区/环境备注）。gp为赛道或分站名（中英文/轮次R5），留空取下一站。赛道介绍/特性/攻略类问题必须调用本工具，禁止凭印象编造赛道参数；返回 found_local=false 表示本地库无档案（如停办多年回归的赛道），L2 必须联网检索并在回答中标注信息来源", "parameters": {"type": "object", "properties": {"gp": {"type": "string", "description": "赛道或分站名（中英文/轮次）"}}, "required": []}}},
    {"type": "function", "function": {"name": "verify_lap_record", "description": "联网核查赛道F1正赛圈速纪录（以F1官网为权威源），返回官网纪录与本地数据对比，不一致时自动更新本地。gp为赛道或分站名（中英文），留空则核查下一站", "parameters": {"type": "object", "properties": {"gp": {"type": "string", "description": "赛道或分站名"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_upgrade_analysis_data", "description": "升级件效果分析数据包：某站各车队升级件申报明细（含意图/差异/工作原理备注）+ 相关车队近4站完赛名次与积分趋势 + 跨赛道可比的归一化圈速 pace_index（全场最快圈中位数/本队最快圈×100，=100为中位）。gp为分站名（中英文），留空取最近一站。用于深度分析升级效果；跨赛道圈速对比必须用pace_index，禁止直接比较不同赛道的绝对圈速", "parameters": {"type": "object", "properties": {"gp": {"type": "string", "description": "分站名"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_race_prediction", "description": "获取分站排位与正赛名次的统计模型预测（名次制线性回归：积分榜/近期状态/赛道历史/车队赛道类型敏感度/升级件/罚退前提，含蒙特卡洛概率与回测基准）。gp为分站名（中英文/轮次R5），留空取下一站；premises_penalties为罚退（{\"车手名\":位数}），premises_standins为代打（{\"下车手\":\"代打者\"}）。预测/竞猜/前瞻类问题必须调用本工具，禁止凭感觉预测", "parameters": {"type": "object", "properties": {"gp": {"type": "string", "description": "分站名或轮次"}, "premises_penalties": {"type": "object", "description": "罚退前提 {车手: 位数}"}, "premises_standins": {"type": "object", "description": "代打前提 {下车手: 代打者}"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_race_weather", "description": "获取分站比赛时段的赛道当地天气：已结束环节用 OpenF1 实测（真实沥青赛道温度/降雨/湿度/风，2023+）；未来环节用 Open-Meteo 预报+沥青温度估算模型 track_temp_estimated（土壤+气温+短波辐射回归，R²=0.81 RMSE≈3.9°C，优于土壤代理）。含降水概率/降水ETA(开始多久后可能降水)/风力。gp为分站名（中英文/轮次R5），year为赛季年份，均留空取下一站。预测/分析类问题（下雨概率、轮胎策略、升温窗口）必须调用；引用赛道温度时优先级 measured(实测) > estimated(模型估算) > proxy(土壤代理)", "parameters": {"type": "object", "properties": {"gp": {"type": "string", "description": "分站名或轮次"}, "year": {"type": "integer", "description": "赛季年份"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_telemetry_chart", "description": "获取F1Cosmos遥测可视化仪表盘链接（网页内自选年份/分站/环节/车手，速度/油门/刹车曲线对比，分段与测速点分析）。遥测/圈速对比类问题时调用，把url放进回答", "parameters": {"type": "object", "properties": {"drivers": {"type": "string", "description": "车手列表（可选）"}, "gp": {"type": "string", "description": "分站名（可选）"}, "session": {"type": "string", "description": "环节（可选）"}, "lap": {"type": "integer", "description": "圈号（可选）"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_team_strengths", "description": "车队实力画像（数据推导，非主观）：从本赛季排位pace_index+完赛名次+FIA升级件计算——各车队 赛季/近期pace与趋势、按赛道下压力档/轮胎负荷档/高海拔的相对强弱（负值=该类型相对更强）、sunday_delta正赛兑现率（负=正赛强于排位=正赛节奏与轮胎管理代理指标）、升级件前后pace变化。team为车队名（中英文），留空返回全部车队。回答“某队为什么在X类赛道强/升级有没有用/谁正赛节奏好”类问题必须调用本工具拿数据说话，禁止凭印象声称车队特性", "parameters": {"type": "object", "properties": {"team": {"type": "string", "description": "车队名，留空为全部"}, "gp": {"type": "string", "description": "分站名（可选，给出则按该站赛道类型返回各队匹配强弱）"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_stint_analysis", "description": "正赛 stint 长距离节奏与轮胎衰减分析（OpenF1 数据，2023+ 含轮胎配方与每段起止圈；更早赛季用 Jolpica 兜底但无配方）：每位车手各 stint 的中位圈速、衰减斜率（ms/圈）、轮胎配方序列（红软/黄中/白硬）。gp 为分站名/轮次（留空=最近一场已完赛），driver 指定车手（留空=全场按最快 stint 排序）。问正赛节奏/轮胎管理/长距离速度/衰减/stint 表现时必须调用本工具拿数据，禁止凭印象回答", "parameters": {"type": "object", "properties": {"gp": {"type": "string", "description": "分站名或轮次（如 蒙扎/R13），留空取最近一场已完赛"}, "driver": {"type": "string", "description": "车手名（中英文/别名），留空为全场"}, "year": {"type": "integer", "description": "赛季年份，留空为当前赛季"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_championship_outlook", "description": "总冠军争夺推演：剩余赛程满分推算（正赛25/冲刺8）+ 理论存活判定 + 领跑者锁冠分站 + 两种概率数值（均匀随机蒙特卡洛的理论概率 / 名次制回归+残差蒙特卡洛的模型概率）。问\"谁还有理论夺冠可能/理论夺冠概率/总冠军概率/争冠形势\"时必须调用，给出具体数值，禁止只说定性判断", "parameters": {"type": "object", "properties": {}, "required": []}}},
]


def parse_upgrade_query(arg: str) -> dict:
    """
    解析升级查询参数，支持任意组合：年份 / 轮次(R5、第5站) / 车队别名 / 地点关键词
    如: "蒙扎 2024"、"法拉利 R16 2024"、"red bull 斯帕"、"R5"
    Returns: {"year": int|None, "round": int|None, "team": str|None, "gp": str|None}
    """
    import re as _re
    from common.f1cosmos_api import TEAM_ALIASES_CN
    q = (arg or "").strip()
    year = None
    # 注意不能用 \b：CJK 字符在 Python re 中算 \w，"法拉利蒙扎2024" 无空格时 \b 不成立
    m = _re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", q)
    if m:
        year = int(m.group(1))
        q = (q[:m.start()] + q[m.end():]).strip()
    round_num = None
    m = _re.search(r"[Rr]\s?0*(\d{1,2})(?=\s|$)|第\s*(\d{1,2})\s*站", q)
    if m:
        round_num = int(m.group(1) or m.group(2))
        q = (q[:m.start()] + q[m.end():]).strip()
    # 车队别名提取（最长优先，避免“红牛”先于“小红牛”被吃掉）
    team_q = None
    for alias in sorted(TEAM_ALIASES_CN, key=len, reverse=True):
        if alias and alias in q:
            team_q = alias
            q = q.replace(alias, " ", 1).strip()
            break
    # 英文队名提取（如 "ferrari monza 2024"、"red bull 斯帕"）：
    # 对别名目标名及其去后缀短名做忽略空格/大小写的子串匹配
    if not team_q:
        nq = _re.sub(r"\s+", "", q.lower())
        best = None  # (alias, 匹配长度, 用于移除的候选文本)
        for alias, value in TEAM_ALIASES_CN.items():
            targets = list(value) if isinstance(value, (list, tuple)) else [value]
            for t in targets:
                short = _re.sub(r"(?i)\s+(racing|f1\s*team|formula\s*1|formula\s*one|team)$", "", t)
                for cand in {t, short}:
                    nc = _re.sub(r"\s+", "", cand.lower())
                    if len(nc) >= 3 and nc in nq and (not best or len(nc) > best[1]):
                        best = (alias, len(nc), cand)
        if best:
            team_q = best[0]
            pattern = _re.escape(best[2]).replace(r"\ ", r"\s*")
            q = _re.sub(pattern, " ", q, count=1, flags=_re.I).strip()
    gp_q = " ".join(q.split()) or None
    return {"year": year, "round": round_num, "team": team_q, "gp": gp_q}


def parse_race_query(arg: str) -> dict:
    """解析分站查询参数（/next 用）：年份 / 轮次(R5、第5站) / 地点关键词。
    与 parse_upgrade_query 的区别：不做车队别名剥离（"红牛环" 的"红牛"是赛道名一部分）
    Returns: {"year": int|None, "round": int|None, "gp": str|None}
    """
    import re as _re
    q = (arg or "").strip()
    year = None
    m = _re.search(r"(?<!\d)((?:19|20)\d{2})(?!\d)", q)
    if m:
        year = int(m.group(1))
        q = (q[:m.start()] + q[m.end():]).strip()
    round_num = None
    m = _re.search(r"[Rr]\s?0*(\d{1,2})(?=\s|$)|第\s*(\d{1,2})\s*站", q)
    if m:
        round_num = int(m.group(1) or m.group(2))
        q = (q[:m.start()] + q[m.end():]).strip()
    gp_q = " ".join(q.split()) or None
    return {"year": year, "round": round_num, "gp": gp_q}


def find_race_for_query(f1_api, season: int, round_num: int = None, gp_q: str = None):
    """按轮次或地点关键词在指定赛季赛历中找分站（Ergast格式）"""
    from common.f1_api import find_race_by_circuit
    schedule = f1_api.get_schedule_for_year(season)
    if round_num:
        for r in schedule:
            try:
                if int(r.get("round", 0)) == round_num:
                    return r
            except (TypeError, ValueError):
                continue
        return None
    if gp_q:
        return find_race_by_circuit(schedule, gp_q)
    return None


def build_handlers(f1_api, f1cosmos=None, season: int = None):
    """
    构建AI工具处理函数字典 {tool_name: callable(**args) -> dict}

    Args:
        f1_api: F1API 实例（赛程/成绩/积分榜）
        f1cosmos: F1CosmosAPI 实例（PU/升级件数据源），未启用时传 None
        season: 当前赛季年份，缺省取当前年
    """
    from datetime import datetime as _dt, timezone as _tzu
    from pytz import timezone as _tz

    if season is None:
        season = _dt.now().year
    tz = _tz('Asia/Shanghai')

    def _drv_standings(year=None):
        from common.qq_group_bot import QQGroupBot
        if year and int(year) != season:
            s = f1_api.get_standings_for_year(int(year))
        else:
            s = f1_api.get_current_standings(force_refresh=True)
        entries = QQGroupBot._parse_driver_standings(s.get("drivers") or {})
        return {"season": int(year) if year else season,
                "standings": [{"pos": e.get("position"), "driver": e.get("Driver", {}).get("familyName"),
                               "team": (e.get("Constructors") or [{}])[0].get("name"),
                               "points": e.get("points"), "wins": e.get("wins")} for e in entries]}

    def _ctr_standings(year=None):
        from common.qq_group_bot import QQGroupBot
        if year and int(year) != season:
            s = f1_api.get_standings_for_year(int(year))
        else:
            s = f1_api.get_current_standings(force_refresh=True)
        entries = QQGroupBot._parse_constructor_standings(s.get("constructors") or {})
        return {"season": int(year) if year else season,
                "standings": [{"pos": e.get("position"), "team": e.get("Constructor", {}).get("name"),
                               "points": e.get("points"), "wins": e.get("wins")} for e in entries]}

    def _next_race():
        race = f1_api.get_next_race()
        if not race:
            return {"error": "本赛季已无剩余比赛"}
        sess = f1_api.get_all_sessions(race)
        return {"race": race.get("raceName"), "circuit": race.get("Circuit", {}).get("circuitName"),
                "date": race.get("date"),
                "sessions": [{"name": s["name"], "time": s["datetime"].astimezone(tz).strftime("%m月%d日%H:%M")} for s in sess]}

    def _circuit_info(gp=""):
        """赛道档案：本地 circuits_data.json 优先；缺失（如停办多年回归的雪邦）标
        needs_web_search，由问答链路升级为 L2 联网检索（2026-09-24 雪邦实证：
        本地无档案时机器人凭印象答、未联网校对）"""
        from common.circuits_manager import CircuitsManager
        cm = CircuitsManager()
        gp = (gp or "").strip()
        race = None
        if gp:
            info = parse_race_query(gp)
            race = find_race_for_query(f1_api, info["year"] or season,
                                       info["round"], info["gp"])
            if not race:
                # 指定赛道不在赛历：直接按名称查本地库，不再静默替换为下一站
                hit = cm.search_circuit(info["gp"] or gp)
                if hit:
                    meta = hit[1]
                    return {"circuit": meta.get("name_en"), "circuit_id": hit[0],
                            "race": None, "found_local": True,
                            "name": meta.get("name"), "name_en": meta.get("name_en"),
                            "location": meta.get("location"), "country": meta.get("country"),
                            "timezone": meta.get("timezone"), "first_race": meta.get("first_race"),
                            "lap_length_km": meta.get("lap_length_km"), "laps": meta.get("laps"),
                            "race_distance_km": meta.get("race_distance_km"),
                            "corners": meta.get("corners"), "altitude_m": meta.get("altitude_m"),
                            "downforce": meta.get("downforce"), "tire_stress": meta.get("tire_stress"),
                            "env_note": meta.get("env_note"), "lap_record": meta.get("lap_record")}
                return {"circuit": gp, "circuit_id": None, "race": None,
                        "found_local": False, "needs_web_search": True,
                        "search_hint": f"{gp} F1 circuit length corners lap record characteristics",
                        "note": "本地赛道库与本赛季赛历均无此赛道，必须联网检索获取，"
                                "禁止凭印象编造参数；回答中标注信息来自网络检索"}
        if not race:
            race = f1_api.get_next_race()
        circuit = (race or {}).get("Circuit", {}) or {}
        cid = circuit.get("circuitId", "")
        cname = circuit.get("circuitName", "") or gp
        # 本地库：circuitId 直查 → 名称模糊查
        meta = cm.get_circuit(cid) if cid else None
        if not meta and (cname or gp):
            hit = cm.search_circuit(gp or cname)
            if hit:
                meta = hit[1]
        base = {"circuit": cname, "circuit_id": cid,
                "race": (race or {}).get("raceName")}
        if meta:
            return {**base, "found_local": True,
                    "name": meta.get("name"), "name_en": meta.get("name_en"),
                    "location": meta.get("location"), "country": meta.get("country"),
                    "timezone": meta.get("timezone"), "first_race": meta.get("first_race"),
                    "lap_length_km": meta.get("lap_length_km"), "laps": meta.get("laps"),
                    "race_distance_km": meta.get("race_distance_km"),
                    "corners": meta.get("corners"), "altitude_m": meta.get("altitude_m"),
                    "downforce": meta.get("downforce"), "tire_stress": meta.get("tire_stress"),
                    "env_note": meta.get("env_note"), "lap_record": meta.get("lap_record")}
        return {**base, "found_local": False, "needs_web_search": True,
                "search_hint": f"{cname or gp} F1 circuit length corners lap record characteristics",
                "note": "本地赛道库无此赛道档案（可能为停办多年回归赛道），必须联网检索获取，"
                        "禁止凭印象编造参数；回答中标注信息来自网络检索"}

    def _race_results(round, session="race", year=None):
        q_season = int(year) if year else None
        res = f1_api.get_session_results(int(round), session or "race", season=q_season)
        if not res or not res.get("entries"):
            return {"error": f"{q_season or season}赛季 R{round} 无数据"}
        return {"season": q_season or season, "race": res.get("race_name"),
                "session": res.get("session_name"),
                "results": [{"pos": e["position"], "driver": e["driver_name"], "team": e["team_name"],
                             "grid": e.get("grid"), "time": e.get("time"),
                             "fast_lap": e.get("fast_lap"), "points": e.get("points")}
                            for e in res["entries"]]}

    def _season_results(year=None, top_n=None):
        q_season = int(year) if year else season
        sched = f1_api.get_schedule_for_year(q_season)
        now = _dt.now(_tzu.utc)
        # 指定赛季：全部已完成站次，每站前 top_n 名（默认只给分站冠军，控制token）；
        # 未指定（当前赛季）：保持原行为——最近10站、每站前10
        if year:
            try:
                limit = max(1, min(int(top_n), 20)) if top_n else 1
            except (TypeError, ValueError):
                limit = 1
        else:
            limit = 10
        out = []
        for race in sched:
            try:
                rt = race.get("time", "00:00:00Z").replace("Z", "")
                rdt = _dt.fromisoformat(f"{race['date']}T{rt}").replace(tzinfo=_tzu.utc)
                if rdt >= now:
                    continue
            except Exception:
                continue
            try:
                round_num = int(race["round"])
            except (KeyError, TypeError, ValueError):
                continue
            res = f1_api.get_session_results(round_num, "race", season=q_season)
            if res and res.get("entries"):
                out.append({"round": race["round"], "race": res.get("race_name"),
                            "date": race.get("date"),
                            "podium": [{"pos": e["position"], "driver": e["driver_name"],
                                        "team": e.get("team_name"), "points": e.get("points")}
                                       for e in res["entries"][:limit]]})
        if year:
            return {"season": q_season, "top_n": limit, "races": out}
        return {"season": q_season, "races": out[-10:]}  # 只保留最近10站，控制token

    def _pu_quota(year=None):
        if not f1cosmos:
            return {"error": "数据源未启用"}
        q_season = int(year) if year else season
        elements = f1cosmos.get_elements(q_season)
        note = "历史赛季为该赛季末（最后一站）的累计用量快照" if q_season != season else None
        out = {"season": q_season,
               "data": [{"driver": e["last_name"], "team": e["team"],
                         "usages": e["usages"], "total": e["total"]} for e in elements]}
        if note:
            out["note"] = note
        if not elements:
            out["error"] = f"{q_season}赛季无PU用量数据（FIA约2021年起才有此类官方文档）"
        return out

    def _upgrades(gp_or_team="", year=None):
        if not f1cosmos:
            return {"error": "数据源未启用"}
        q_season = int(year) if year else season
        info = parse_upgrade_query(gp_or_team or "")
        q_season = info["year"] or q_season

        def _payload(items):
            return [{"team": u["team"], "gp": u.get("gp_name", ""), "round": u.get("round", 0),
                     "component": u["component"], "type": u["type"],
                     "difference": u.get("difference", ""),
                     "description": u.get("brief_description", "")} for u in items]

        def _gp_items(race):
            loc = (race.get("Circuit", {}).get("Location", {}) or {})
            city = loc.get("locality", "")
            items = []
            if hasattr(f1cosmos, "get_event_upgrades"):
                items = f1cosmos.get_event_upgrades(race.get("raceName", ""), q_season, city=city)
            if not items:
                items = f1cosmos._match_updates(f1cosmos.get_updates(q_season), city,
                                                race.get("raceName", ""))
            # Cosmos兜底仅限当前赛季（其season参数无效，历史查询会错返回当前赛季数据）
            if not items and q_season == season and hasattr(f1cosmos, "get_updates_cosmos"):
                items = f1cosmos._match_updates(f1cosmos.get_updates_cosmos(q_season), city,
                                                race.get("raceName", ""))
            return items

        race = None
        if info["round"] or info["gp"]:
            race = find_race_for_query(f1_api, q_season, info["round"], info["gp"])
            if not race:
                return {"error": f"{q_season}赛季未找到分站「{info['gp'] or ('R' + str(info['round']))}」"}

        if info["team"]:
            if race:
                items = _gp_items(race)
                team = f1cosmos.match_team(info["team"], items)
                if not team:
                    return {"note": f"{q_season} {race.get('raceName')} 未找到 {info['team']} 的升级申报"}
                return _payload(f1cosmos._team_items(items, team))
            updates = f1cosmos._season_updates(q_season)
            team = f1cosmos.match_team(info["team"], updates)
            if not team:
                from common.f1cosmos_api import upgrades_no_data_reason
                return {"error": f"{q_season}赛季未找到车队「{info['team']}」{upgrades_no_data_reason(q_season)}"}
            return _payload(f1cosmos._team_items(updates, team))
        if race:
            return _payload(_gp_items(race))

        q = (gp_or_team or "").strip()
        if not q:
            if year:
                # 只给了年份：返回该赛季各车队升级数量汇总（一站答完"升级最多的车队"类问题，
                # 避免逐站/逐队扇出几十次工具调用）
                updates = f1cosmos._season_updates(q_season)
                if not updates:
                    from common.f1cosmos_api import upgrades_no_data_reason
                    return {"error": f"{q_season}赛季暂无升级件数据{upgrades_no_data_reason(q_season)}"}
                from common.f1cosmos_api import canonical_team_key
                per_team: Dict[str, dict] = {}
                for u in updates:
                    key = canonical_team_key(u["team"])
                    entry = per_team.setdefault(key, {"variants": {}, "gps": set()})
                    entry["variants"][u["team"]] = entry["variants"].get(u["team"], 0) + 1
                    if u.get("gp_name"):
                        entry["gps"].add(u["gp_name"])
                summary = []
                for entry in per_team.values():
                    display = max(entry["variants"], key=entry["variants"].get)
                    summary.append({
                        "team": display,
                        "total_upgrades": sum(entry["variants"].values()),
                        "gps_with_upgrades": len(entry["gps"]),
                    })
                summary.sort(key=lambda x: -x["total_upgrades"])
                return {"season": q_season, "team_upgrade_counts": summary,
                        "note": "按FIA官方申报条目数统计；问某车队/某分站明细时再传 gp_or_team"}
            race = f1_api.get_next_race()
            if not race:
                return {"error": "无下一站"}
            items = _gp_items(race)
            if not items and hasattr(f1cosmos, "current_event_name"):
                # 本周升级文档通常周五才发布，尚未发布时给最近已发布分站的数据
                cur = f1cosmos.current_event_name()
                if cur and cur != race.get("raceName"):
                    cur_race = {"raceName": cur, "Circuit": {"Location": {"locality": ""}}}
                    items = _gp_items(cur_race)
                    if items:
                        return {"note": f"本周({race.get('raceName')})升级文档尚未发布，以下为最近已发布分站({cur})数据",
                                "items": _payload(items)}
            return _payload(items) if items else {"note": "本站升级件尚未公布"}
        # 整串兜底：车队或地点
        updates = f1cosmos._season_updates(q_season)
        team = f1cosmos.match_team(q, updates)
        if team:
            return _payload(f1cosmos._team_items(updates, team))
        race = find_race_for_query(f1_api, q_season, None, q)
        if race:
            return _payload(_gp_items(race))
        return {"error": f"未找到 {q} 相关升级件"}

    def _verify_lap_record(gp=""):
        """以F1官网为权威源核查赛道圈速纪录，不一致自动更新本地"""
        from common.circuits_manager import CircuitsManager
        from common.f1_api import find_race_by_circuit as _find_race
        cm = CircuitsManager()
        circuit_id = ""
        circuit_name = ""
        q = (gp or "").strip()
        if q:
            found = cm.search_circuit(q)
            if found:
                circuit_id, cdata = found
                circuit_name = cdata.get("name", q)
            else:
                race = _find_race(f1_api.get_schedule(), q)
                if race:
                    circuit_id = race.get("Circuit", {}).get("circuitId", "")
                    circuit_name = race.get("raceName", q)
        if not circuit_id:
            race = f1_api.get_next_race()
            if not race:
                return {"error": "未找到赛道且本赛季已无下一站"}
            circuit_id = race.get("Circuit", {}).get("circuitId", "")
            circuit_name = race.get("raceName", "")

        local = cm.get_circuit_record(circuit_id) or {}
        official = cm.fetch_official_lap_record(circuit_id)
        if not official:
            return {"circuit": circuit_name, "error": "F1官网核查失败（无slug映射或抓取失败）",
                    "local": local}
        updated = False
        local_sec = CircuitsManager.time_to_seconds(local.get("time", ""))
        official_sec = CircuitsManager.time_to_seconds(official["time"])
        if official_sec and official_sec != local_sec:
            updated = cm.apply_official_record(circuit_id, official)
        return {"circuit": circuit_name, "official": official,
                "local_before": local, "updated": updated}

    def _upgrade_analysis_data(gp=""):
        """升级件效果分析数据包：申报明细（含备注）+ 相关车队近4站完赛趋势"""
        if not f1cosmos:
            return {"error": "数据源未启用"}

        from common.f1_api import find_race_by_circuit as _find_race
        sched = f1_api.get_schedule()
        now = _dt.now(_tzu.utc)
        q = (gp or "").strip()
        race = None
        if q:
            race = _find_race(sched, q)
            if not race:
                return {"error": f"未找到分站「{q}」"}
        else:
            # 默认最近一场已完成的比赛
            for r in reversed(sched):
                try:
                    rt = r.get("time", "00:00:00Z").replace("Z", "")
                    rdt = _dt.fromisoformat(f"{r['date']}T{rt}").replace(tzinfo=_tzu.utc)
                    if rdt < now:
                        race = r
                        break
                except Exception:
                    continue
        if not race:
            return {"error": "本赛季暂无已完成比赛"}

        race_name = race.get("raceName", "")
        city = (race.get("Circuit", {}).get("Location", {}) or {}).get("locality", "")

        # 该站升级件申报（FIA官方，含意图/差异/描述备注）
        updates = f1cosmos.get_updates(season)
        items = f1cosmos._match_updates(updates, city, race_name)
        upgrade_list = [{
            "team": u["team"], "component": u["component"],
            "reason": u.get("reason") or u.get("type", ""),
            "difference": u.get("difference", ""),
            "description": u.get("brief_description", ""),
            "fia_doc": u.get("fia_url", ""),
        } for u in items]

        # 涉及车队近4站完赛趋势（每站最佳名次+积分合计）
        # 队名匹配用 canonical_team_key（FIA申报名/成绩表名/年份变体统一归一，
        # 如 "SCUDERIA FERRARI HP"≈"Scuderia Ferrari"≈"Ferrari"）
        from common.f1cosmos_api import canonical_team_key
        involved_teams = sorted({u["team"] for u in items})
        involved_keys: Dict[str, str] = {t: canonical_team_key(t) for t in involved_teams}
        past_races = []
        for r in sched:
            try:
                rt = r.get("time", "00:00:00Z").replace("Z", "")
                rdt = _dt.fromisoformat(f"{r['date']}T{rt}").replace(tzinfo=_tzu.utc)
                if rdt < now:
                    past_races.append(r)
            except Exception:
                continue
        trends: Dict[str, list] = {t: [] for t in involved_teams}
        pace_trends: Dict[str, list] = {t: [] for t in involved_teams}
        for r in past_races[-4:]:
            res = f1_api.get_session_results(int(r["round"]), "race")
            if not res or not res.get("entries"):
                continue
            per_team: Dict[str, dict] = {}
            for e in res["entries"]:
                tn = e.get("team_name", "")
                try:
                    pos = int(e.get("position", 99))
                except (TypeError, ValueError):
                    pos = 99
                try:
                    pts = float(e.get("points", 0) or 0)
                except (TypeError, ValueError):
                    pts = 0.0
                cur = per_team.setdefault(tn, {"best": 99, "pts": 0.0})
                cur["best"] = min(cur["best"], pos)
                cur["pts"] += pts

            # 跨赛道可比圈速指标：pace_index = 全场最快圈中位数 / 本队最快圈 × 100
            # （=100 为中位水平，>100 快于中位；不同赛道长度/特性下绝对圈速不可直接比较）
            # 数据源为 Jolpica 正赛 FastestLap（f1api.dev 的 fastLap 字段恒为 null）
            fl_list = f1_api.get_race_fastest_laps(season, int(r["round"]))
            field_median = _median([x["fastest_lap"] for x in fl_list])

            for tn in involved_teams:
                tkey = involved_keys[tn]
                for actual_tn, v in per_team.items():
                    if canonical_team_key(actual_tn) != tkey:
                        continue
                    trends[tn].append({
                        "race": res.get("race_name", r.get("raceName", "")),
                        "best_finish": v["best"], "points": v["pts"],
                    })
                    if field_median:
                        team_laps = [x["fastest_lap"] for x in fl_list
                                     if canonical_team_key(x["team"]) == tkey]
                        if team_laps:
                            pace_trends[tn].append({
                                "race": res.get("race_name", r.get("raceName", "")),
                                "pace_index": round(field_median / min(team_laps) * 100, 2),
                                "fastest_lap": round(min(team_laps), 3),
                                "field_median_lap": round(field_median, 3),
                            })
                    break

        return {
            "gp": race_name, "round": race.get("round"),
            "upgrades": upgrade_list,
            "team_trends": trends,
            "pace_trends": pace_trends,
            "note": "升级为FIA官方申报（含意图备注）；team_trends为近4站各车队最佳完赛名次+积分合计；"
                    "pace_trends为跨赛道可比的归一化圈速指标：pace_index=全场最快圈中位数/本队最快圈×100，"
                    "=100为中位水平、>100快于中位。禁止直接比较不同赛道的绝对圈速（赛道长度/特性不同），"
                    "跨赛道对比必须使用pace_index或同赛道历年对比",
        }

    _pred_model = None  # 进程内单例（模型权重/赛季数据集驻留内存，避免每次重建）
    import threading as _threading
    _pred_model_lock = _threading.Lock()  # 首次构建为分钟级重活，加锁防并发重复构建

    def _race_prediction(gp="", premises_penalties=None, premises_standins=None):
        """分站排位/正赛统计模型预测（模型计算，AI 只呈现）"""
        nonlocal _pred_model
        from common.prediction_model import RacePredictionModel
        if _pred_model is None:
            with _pred_model_lock:
                if _pred_model is None:
                    _pred_model = RacePredictionModel(f1_api, f1cosmos)
        premises = {}
        if premises_penalties:
            premises["penalties"] = premises_penalties
        if premises_standins:
            premises["standins"] = premises_standins
        result = _pred_model.predict(gp or "", season=season, premises=premises)
        # 附带赛道工程特性档案（海拔/下压力/轮胎负荷/环境备注），供成文层解读
        if result and "error" not in result and result.get("circuit_id"):
            try:
                from common.circuits_manager import CircuitsManager
                c = CircuitsManager().get_circuit(result["circuit_id"])
                if c:
                    result["circuit_profile"] = {
                        "altitude_m": c.get("altitude_m"),
                        "downforce": c.get("downforce"),
                        "tire_stress": c.get("tire_stress"),
                        "env_note": c.get("env_note"),
                        "corners": c.get("corners"),
                        "lap_length_km": c.get("lap_length_km"),
                    }
            except Exception:
                pass
            # 数据推导的车队强弱（该站赛道类型维度），供成文层引用
            try:
                from common.team_strength import TeamStrengthAnalyzer
                result["team_strengths_for_circuit"] = TeamStrengthAnalyzer(
                    f1_api, f1cosmos, season).strengths_for_circuit(result["circuit_id"])
            except Exception:
                pass
        return result

    def _race_weather(gp="", year=None):
        """分站比赛时段天气分析包（预报/实测归档）"""
        from common.weather_api import WeatherAPI
        q_season = int(year) if year else season
        q = (gp or "").strip()
        race = None
        if q:
            # 支持轮次写法（R5）
            import re as _re
            m = _re.search(r"^[Rr]\s?0*(\d{1,2})$", q)
            race = find_race_for_query(f1_api, q_season,
                                       int(m.group(1)) if m else None,
                                       None if m else q)
            if not race:
                return {"error": f"{q_season}赛季未找到分站「{q}」"}
        else:
            race = f1_api.get_next_race()
            if not race:
                return {"error": "本赛季已无剩余比赛"}
        sessions = f1_api.get_all_sessions(race)
        result = WeatherAPI().get_race_weather(race, sessions, year=q_season)
        # 载荷瘦身：逐环节的 note 字段是同一句长说明，重复5次会撑爆 L2 注入截断上限
        for s in (result.get("sessions") or []):
            for k in ("track_temp_proxy", "track_temp_estimated"):
                if isinstance(s.get(k), dict):
                    s[k].pop("note", None)
        return result

    def _telemetry_chart(drivers="", gp="", session="race", lap=None):
        """返回遥测可视化链接（本站自建页 + F1Cosmos 仪表盘 + 透明度可调嵌入页）"""
        import os as _os
        out = {
            "url": "https://f1cosmos.com/zh/dashboard/race/telemetry",
            "laptime_url": "https://f1cosmos.com/zh/dashboard/race/laptime",
            "result_url": "https://f1cosmos.com/zh/dashboard/race/result",
            "note": "把遥测链接发给用户；页面内可自选年份/分站/环节/车手做曲线对比",
        }
        base = _os.getenv("RATING_BASE_URL", "").rstrip("/")
        if base:
            out["self_hosted_telemetry"] = f"{base}/telemetry"
            out["adjustable_overlay"] = f"{base}/telemetry/pro"
            out["note"] += ("；本站自建遥测页用 self_hosted_telemetry；"
                            "adjustable_overlay 是透明度/亮度可调的嵌入面板（适合夜间观赛或叠加场景）")
        return out

    def _team_strengths(team="", gp=""):
        """车队实力画像（数据推导）"""
        from common.team_strength import TeamStrengthAnalyzer
        analyzer = TeamStrengthAnalyzer(f1_api, f1cosmos, season)
        q = (gp or "").strip()
        if q:
            race = find_race_for_query(f1_api, season, None, q)
            if not race:
                return {"error": f"未找到分站「{gp}」"}
            cid = (race.get("Circuit", {}) or {}).get("circuitId", "")
            return {"gp": race.get("raceName"), "circuit_id": cid,
                    "team_strengths_for_circuit": analyzer.strengths_for_circuit(cid)}
        all_s = analyzer.compute()
        if not all_s:
            return {"error": "本赛季暂无足够数据"}
        t = (team or "").strip()
        if t:
            from common.f1cosmos_api import TEAM_ALIASES_CN, canonical_team_key
            target = TEAM_ALIASES_CN.get(t, t)
            if isinstance(target, list):
                target = target[0]
            key = canonical_team_key(target)
            return {"team": key, "profile": all_s.get(key) or {"error": f"无「{t}」数据"}}
        return {"season": season, "teams": all_s}

    def _championship_outlook():
        """总冠军争夺推演（数学理论层 + 均匀MC理论概率 + 回归模型概率）"""
        nonlocal _pred_model
        from common.prediction_model import RacePredictionModel
        if _pred_model is None:
            _pred_model = RacePredictionModel(f1_api, f1cosmos)
        return _pred_model.championship_projection(season)

    def _stint_analysis(gp="", driver="", year=None):
        """正赛 stint 节奏/轮胎衰减分析（OpenF1 主源，2023+ 含配方）"""
        from datetime import datetime as _dt
        from .stint_analysis import StintAnalyzer
        from .f1_api import find_race_by_circuit

        q_season = int(year) if year else season
        schedule = f1_api.get_schedule_for_year(q_season) or []
        race = None
        if gp:
            import re as _re
            m = _re.search(r"^[Rr]\s?0*(\d{1,2})$", str(gp).strip())
            if m:
                def _round_eq(r, target):
                    try:
                        return int(r.get("round", 0)) == target
                    except (TypeError, ValueError):
                        return False
                race = next((r for r in schedule if _round_eq(r, int(m.group(1)))), None)
            else:
                race = find_race_by_circuit(schedule, str(gp))
        else:
            # 留空取最近一场已完赛
            today = _dt.now().date()
            past = [r for r in schedule
                    if r.get("date") and r["date"] < today.isoformat()]
            race = past[-1] if past else None
        if not race:
            return {"error": f"未找到分站（{gp or '最近已完赛'}）"}

        round_num = int(race.get("round", 0))
        sa = StintAnalyzer(f1_api)
        # 仅已完赛分站走永久缓存；比赛周末进行中禁缓存防污染
        is_completed = race.get("date", "") < _dt.now().date().isoformat()
        data = sa.get_race_stints(q_season, round_num, use_cache=is_completed)
        if "error" in data:
            return data

        # 车手名映射（driverId → 中文名/英文名）
        from . import drivers_profile as dp
        profiles = dp.get_drivers()

        def _disp(did: str) -> str:
            p = profiles.get(did) or {}
            return p.get("name_cn") or p.get("name_en") or did

        drivers_data = data["drivers"]
        if driver:
            # 指定车手：别名解析（子串匹配）
            from .drivers_profile import get_driver_aliases
            q = str(driver).strip().lower()
            hit = None
            for alias, canon in get_driver_aliases().items():
                if alias and alias.lower() in q:
                    hit = canon
                    break
            if not hit:
                hit = next((d for d in drivers_data if q in d.lower()), None)
            if not hit or hit not in drivers_data:
                return {"error": f"未找到车手「{driver}」在该站的数据"}
            d = drivers_data[hit]
            return {"race": race.get("raceName"), "season": q_season, "round": round_num,
                    "driver": _disp(hit), "driver_id": hit, **d,
                    "note": "median_s=该stint有效圈中位圈速(秒)；deg_ms_per_lap=衰减斜率(ms/圈，正=衰减)；"
                            "avg_deg_ms=各stint衰减均值；best_stint_median_s=最快stint中位圈速"}

        # 全场：按最快 stint 中位圈速排序，输出前 10
        ranked = sorted(
            ({"driver_id": did, "driver": _disp(did), **v}
             for did, v in drivers_data.items() if v.get("best_stint_median_s")),
            key=lambda x: x["best_stint_median_s"])
        return {"race": race.get("raceName"), "season": q_season, "round": round_num,
                "ranking_by_best_stint": ranked[:10],
                "note": "median_s=stint中位圈速(秒)；deg_ms_per_lap=衰减斜率(ms/圈，正=衰减)；"
                        "avg_deg_ms=各stint衰减均值"}

    return {
        "get_driver_standings": _drv_standings,
        "get_constructor_standings": _ctr_standings,
        "get_next_race": _next_race,
        "get_circuit_info": _circuit_info,
        "get_race_results": _race_results,
        "get_season_results": _season_results,
        "get_pu_quota": _pu_quota,
        "get_upgrades": _upgrades,
        "verify_lap_record": _verify_lap_record,
        "get_upgrade_analysis_data": _upgrade_analysis_data,
        "get_race_weather": _race_weather,
        "get_race_prediction": _race_prediction,
        "get_telemetry_chart": _telemetry_chart,
        "get_team_strengths": _team_strengths,
        "get_championship_outlook": _championship_outlook,
        "get_stint_analysis": _stint_analysis,
    }
