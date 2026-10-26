"""
F1赛程提醒机器人 - F1数据工具层（供AI function calling主动调用）

对应Agent架构中的工具薄适配层：F1_TOOLS 为 OpenAI 风格的 function schema，
build_handlers() 返回 {tool_name: callable} 处理函数字典，
由 LLMAssistant 的工具循环（_chat_once / _l1_collect）执行。

升级件查询参数解析与分站定位（parse_upgrade_query / find_race_for_query）
同时被 /upgrades、/upgrade 群指令与AI工具共用，故一并收敛到本模块。
"""

import logging
import threading as _threading
from typing import Dict

logger = logging.getLogger(__name__)

# B站ASR in-flight 去重：同 bvid 并发时后到者等首个结果，防双份下载+ASR占用LLM槽位
_bili_asr_inflight: Dict[str, _threading.Event] = {}
_bili_asr_inflight_lock = _threading.Lock()


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


_UPGRADE_CORE_COMPONENTS = (
    "floor", "diffuser", "sidepod", "coke", "engine cover",
    "front wing", "rear wing", "beam wing", "nose", "chassis", "suspension",
)
_UPGRADE_MAJOR_PACKAGE_MIN = 3
_UPGRADE_MAJOR_MAX_PER_TEAM = 8


def _upgrade_type_class(type_str: str) -> str:
    t = (type_str or "").strip().lower()
    if t.startswith("performance") or any(
            k in t for k in ("flow conditioning", "local load", "mechanical setup", "drag reduction")):
        return "performance"
    if t.startswith("circuit") or "cooling" in t or "balance range" in t:
        return "circuit_specific"
    return "other"


# ===== 工具 schema =====
F1_TOOLS = [
    {"type": "function", "function": {"name": "get_driver_standings", "description": "获取F1车手积分榜（排名/姓名/车队/积分/胜场）。year为赛季年份（如2025），留空为当前赛季；历史赛季同样可用（1950年至今）。冲刺/正赛刚结束而官方积分榜未更新时，返回含预估积分的投影榜并附 provisional 字段说明，回答必须提及该预估口径", "parameters": {"type": "object", "properties": {"year": {"type": "integer", "description": "赛季年份，留空为当前赛季"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_constructor_standings", "description": "获取F1车队积分榜。year为赛季年份（如2025），留空为当前赛季；刚结束的冲刺/正赛积分未入账时返回预估投影并附 provisional 字段，回答必须提及", "parameters": {"type": "object", "properties": {"year": {"type": "integer", "description": "赛季年份，留空为当前赛季"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_next_race", "description": "获取下一站比赛（名称/赛道/各环节北京时间）", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "get_race_results", "description": "获取某站某环节成绩完整排名。session可选fp1/fp2/fp3/qualifying/sprint/race，默认race；year为赛季年份（如2025），留空为当前赛季，历史赛季同样可用", "parameters": {"type": "object", "properties": {"round": {"type": "integer", "description": "分站轮次"}, "session": {"type": "string", "description": "环节"}, "year": {"type": "integer", "description": "赛季年份，留空为当前赛季"}}, "required": ["round"]}}},
    {"type": "function", "function": {"name": "get_season_results", "description": "获取一个赛季已完成各站正赛成绩。year为赛季年份（如2025），留空为当前赛季（仅最近10站、每站前10）；指定year时返回该赛季全部已完成站次，每站名次由top_n控制：问冠军/冠军榜传1（默认），问冠亚季军/领奖台传3，问亚军传2，问积分区传10，问完整排名传20。用于分析车手积分/名次趋势或整理历史赛季成绩", "parameters": {"type": "object", "properties": {"year": {"type": "integer", "description": "赛季年份，留空为当前赛季"}, "top_n": {"type": "integer", "description": "每站返回前N名，指定year时默认1"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_pu_quota", "description": "获取每位车手动力单元各部件用量及总量，评估超额罚退风险。year为赛季年份（如2023），留空为当前赛季；历史赛季返回该赛季末的累计用量快照（FIA官方文档，2019年起有；2019-2025含MGU-H列，2026起取消MGU-H）", "parameters": {"type": "object", "properties": {"year": {"type": "integer", "description": "赛季年份，留空为当前赛季"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_upgrades", "description": "获取赛车升级件明细（支持历史赛季、轮次与车队+分站组合）。gp_or_team为分站地点、车队名（中英文）、轮次（如R5）或其组合（如“法拉利 蒙扎”），留空则取下一站；year为赛季年份（如2024），留空为当前赛季。只传year不传gp_or_team时，一次返回该赛季各车队升级数量汇总（含 performance/circuit_specific 分类计数）+ major_upgrades 重点大升级清单（性能类核心部件：底盘/扩散器/侧箱/翼片/悬挂等，或单站多件套装升级包）——回答赛季升级问题时必须以 major_upgrades 为主体呈现、赛道适配小改一句带过，全量数量表格保留；“哪支车队升级最多”类问题务必用这种方式，不要逐站逐队多次调用", "parameters": {"type": "object", "properties": {"gp_or_team": {"type": "string", "description": "分站地点/车队名/轮次，可组合；只问赛季汇总时留空"}, "year": {"type": "integer", "description": "赛季年份"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_circuit_info", "description": "获取赛道档案（长度/弯数/正赛圈数/圈速纪录/下压力档/轮胎负荷档/海拔/时区/环境备注）。gp为赛道或分站名（中英文/轮次R5），留空取下一站。赛道介绍/特性/攻略类问题必须调用本工具，禁止凭印象编造赛道参数；返回 found_local=false 表示本地库无档案（如停办多年回归的赛道），L2 必须联网检索并在回答中标注信息来源", "parameters": {"type": "object", "properties": {"gp": {"type": "string", "description": "赛道或分站名（中英文/轮次）"}}, "required": []}}},
    {"type": "function", "function": {"name": "verify_lap_record", "description": "联网核查赛道F1正赛圈速纪录（以F1官网为权威源），返回官网纪录与本地数据对比，不一致时自动更新本地。gp为赛道或分站名（中英文），留空则核查下一站", "parameters": {"type": "object", "properties": {"gp": {"type": "string", "description": "赛道或分站名"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_upgrade_analysis_data", "description": "升级件效果分析数据包：某站各车队升级件申报明细（含意图/差异/工作原理备注）+ 相关车队近4站完赛名次与积分趋势 + 跨赛道可比的归一化圈速 pace_index（全场最快圈中位数/本队最快圈×100，=100为中位）。gp为分站名（中英文），留空取最近一站。用于深度分析升级效果；跨赛道圈速对比必须用pace_index，禁止直接比较不同赛道的绝对圈速", "parameters": {"type": "object", "properties": {"gp": {"type": "string", "description": "分站名"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_race_prediction", "description": "获取分站排位与正赛名次的统计模型预测（名次制线性回归：积分榜/近期状态/赛道历史/车队赛道类型敏感度/升级件/罚退前提，含蒙特卡洛概率与回测基准）。gp为分站名（中英文/轮次R5），留空取下一站；premises_penalties为罚退（{\"车手名\":位数}），premises_standins为代打（{\"下车手\":\"代打者\"}）。预测/竞猜/前瞻类问题必须调用本工具，禁止凭感觉预测", "parameters": {"type": "object", "properties": {"gp": {"type": "string", "description": "分站名或轮次"}, "premises_penalties": {"type": "object", "description": "罚退前提 {车手: 位数}"}, "premises_standins": {"type": "object", "description": "代打前提 {下车手: 代打者}"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_race_weather", "description": "获取分站比赛时段的赛道当地天气：已结束环节用 OpenF1 实测（真实沥青赛道温度/降雨/湿度/风，2023+）；未来环节用 Open-Meteo 预报+沥青温度估算模型 track_temp_estimated（土壤+气温+短波辐射回归，R²=0.81 RMSE≈3.9°C，优于土壤代理）。含降水概率/降水ETA(开始多久后可能降水)/风力。gp为分站名（中英文/轮次R5），year为赛季年份，均留空取下一站。预测/分析类问题（下雨概率、轮胎策略、升温窗口）必须调用；引用赛道温度时优先级 measured(实测) > estimated(模型估算) > proxy(土壤代理)", "parameters": {"type": "object", "properties": {"gp": {"type": "string", "description": "分站名或轮次"}, "year": {"type": "integer", "description": "赛季年份"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_telemetry_chart", "description": "获取F1Cosmos遥测可视化仪表盘链接（网页内自选年份/分站/环节/车手，速度/油门/刹车曲线对比，分段与测速点分析）。遥测/圈速对比类问题时调用，把url放进回答", "parameters": {"type": "object", "properties": {"drivers": {"type": "string", "description": "车手列表（可选）"}, "gp": {"type": "string", "description": "分站名（可选）"}, "session": {"type": "string", "description": "环节（可选）"}, "lap": {"type": "integer", "description": "圈号（可选）"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_team_strengths", "description": "车队实力画像（数据推导，非主观）：从本赛季排位pace_index+完赛名次+FIA升级件计算——各车队 赛季/近期pace与趋势、按赛道下压力档/轮胎负荷档/高海拔的相对强弱（负值=该类型相对更强）、sunday_delta正赛兑现率（负=正赛强于排位=正赛节奏与轮胎管理代理指标）、升级件前后pace变化。team为车队名（中英文），留空返回全部车队。回答“某队为什么在X类赛道强/升级有没有用/谁正赛节奏好”类问题必须调用本工具拿数据说话，禁止凭印象声称车队特性", "parameters": {"type": "object", "properties": {"team": {"type": "string", "description": "车队名，留空为全部"}, "gp": {"type": "string", "description": "分站名（可选，给出则按该站赛道类型返回各队匹配强弱）"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_stint_analysis", "description": "正赛 stint 长距离节奏与轮胎衰减分析（OpenF1 数据，2023+ 含轮胎配方与每段起止圈；更早赛季用 Jolpica 兜底但无配方）：每位车手各 stint 的中位圈速、衰减斜率（ms/圈）、轮胎配方序列（红软/黄中/白硬）。gp 为分站名/轮次（留空=最近一场已完赛），driver 指定车手（留空=全场按最快 stint 排序）。问正赛节奏/轮胎管理/长距离速度/衰减/stint 表现时必须调用本工具拿数据，禁止凭印象回答", "parameters": {"type": "object", "properties": {"gp": {"type": "string", "description": "分站名或轮次（如 蒙扎/R13），留空取最近一场已完赛"}, "driver": {"type": "string", "description": "车手名（中英文/别名），留空为全场"}, "year": {"type": "integer", "description": "赛季年份，留空为当前赛季"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_race_tech_params", "description": "获取分站 FIA Power Unit Information 官方技术参数（2026 新规逐站发布，数值逐站不同禁止跨站套用）：每圈能量回收上限（正赛 Overtake 未激活/激活、排位、练习、出场圈分别的 MJ 值）、PU 功率削减率（kW/s）、Power Limited 距离、Overtake 超车模式检测线/激活线位置与跟车差距门槛、赛道中心线长度。问某站能量回收/功率限制/超车模式参数/Overtake 检测点在哪时必须调用本工具，禁止凭印象。gp为分站名（中英文/轮次R5），留空取下一站", "parameters": {"type": "object", "properties": {"gp": {"type": "string", "description": "分站名或轮次"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_safety_car_events", "description": "获取分站正赛安全车/VSC/红旗事件时间线（部署圈数-结束圈数+原因消息+统计），OpenF1 race_control 主源、F1 livetiming 归档兜底（含 chart_md 时间线图可嵌入回答）。问某站安全车出动/VSC/红旗/事故中断时必须调用本工具，禁止凭印象编造圈数。gp为分站名（中英文/轮次R5），留空取最近一场已完赛", "parameters": {"type": "object", "properties": {"gp": {"type": "string", "description": "分站名或轮次"}, "year": {"type": "integer", "description": "赛季年份，留空为当前赛季"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_race_charts", "description": "获取分站可视化统计图表（返回 chart_md 图片链接，必须原文嵌入回答展示给用户）。chart 类型：race_report=赛后三联图（pace分布+轮胎策略+名次变化，默认）、pace=正赛pace分布箱线图、strategy=轮胎策略时间线（按配方着色）、positions=发车→完赛名次变化、quali_gap=排位差距（杆位金色）、sprint_quali_gap=冲刺排位差距、pitstop=进站时长车队对比箱线图。问某站 pace 分布/轮胎策略/名次变化/排位差距/进站耗时/赛后统计图时必须调用本工具并嵌入 chart_md，禁止只说「没有图」。gp为分站名（中英文/轮次R5），留空取最近一场已完赛", "parameters": {"type": "object", "properties": {"gp": {"type": "string", "description": "分站名或轮次"}, "chart": {"type": "string", "description": "图表类型：race_report(默认)/pace/strategy/positions/quali_gap/sprint_quali_gap/pitstop"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_destructors", "description": "获取车手/车队事故维修成本统计（destructors，部件申报价×更换数量估算）+ 事故成本柱图（chart_md 必须原文嵌入）。query 留空=全赛季车手榜+车队榜；query 为车手或车队名（中英文）= 该对象分站明细。问事故成本/修车费用/谁撞车最多/撞车损失时必须调用，禁止凭印象", "parameters": {"type": "object", "properties": {"query": {"type": "string", "description": "车手或车队名，留空为全部"}, "year": {"type": "integer", "description": "赛季年份，留空为当前赛季"}}, "required": []}}},
    {"type": "function", "function": {"name": "get_championship_outlook", "description": "总冠军争夺推演：剩余赛程满分推算（正赛25/冲刺8）+ 理论存活判定 + 领跑者锁冠分站 + 两种概率数值（均匀随机蒙特卡洛的理论概率 / 名次制回归+残差蒙特卡洛的模型概率）。问\"谁还有理论夺冠可能/理论夺冠概率/总冠军概率/争冠形势\"时必须调用，给出具体数值，禁止只说定性判断", "parameters": {"type": "object", "properties": {}, "required": []}}},
    {"type": "function", "function": {"name": "get_video_summary", "description": "B站视频内容导读：用户发来B站（bilibili/b23.tv）视频链接希望AI总结/讲解/帮助理解视频内容（如气动分析、战术复盘、赛事解读）时调用。返回视频字幕文本（或ASR转写），据此生成中文导读：一句话结论→分节要点（保留关键数据与结论）→F1术语解释。返回 is_f1=false 表示非F1相关视频（关键词门禁），直接告知用户不予总结、禁止再调其他工具；返回 no_text=true 表示无字幕且转写不可用，直接告知该视频暂不支持导读，禁止编造内容", "parameters": {"type": "object", "properties": {"url": {"type": "string", "description": "B站视频链接或BV号"}}, "required": ["url"]}}},
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
        provisional = None
        if year and int(year) != season:
            s = f1_api.get_standings_for_year(int(year))
        else:
            s, provisional = f1_api.get_standings_with_projection(force_refresh=True)
        entries = QQGroupBot._parse_driver_standings(s.get("drivers") or {})
        result = {"season": int(year) if year else season,
                  "standings": [{"pos": e.get("position"), "driver": e.get("Driver", {}).get("familyName"),
                                 "team": (e.get("Constructors") or [{}])[0].get("name"),
                                 "points": e.get("points"), "wins": e.get("wins")} for e in entries]}
        if provisional:
            result["provisional"] = provisional["note"]
            result["note"] = "积分榜含预估积分，回答时必须说明该预估口径（" + provisional["note"] + "）"
        # 当前赛季积分榜附积分走势图（chart_md 由 L2 嵌图，多一张图多一分直观）
        if not (year and int(year) != season):
            try:
                import os as _os
                from .chart_factory import render_points_trend_chart
                p = render_points_trend_chart(season, f1_api=f1_api)
                base = _os.getenv("RATING_BASE_URL", "").rstrip("/")
                if p and base:
                    fname = _os.path.basename(p)
                    result["chart_md"] = f"![pic#800px #450px]({base}/exports/{fname})"
                    chart_note = ("chart_md 为本赛季车手积分走势图（逐站累计），"
                                  "回答积分榜/争冠类问题时必须原文嵌入该图")
                    result["note"] = (result["note"] + "；" + chart_note) if result.get("note") else chart_note
            except Exception:
                pass
        return result

    def _ctr_standings(year=None):
        from common.qq_group_bot import QQGroupBot
        provisional = None
        if year and int(year) != season:
            s = f1_api.get_standings_for_year(int(year))
        else:
            s, provisional = f1_api.get_standings_with_projection(force_refresh=True)
        entries = QQGroupBot._parse_constructor_standings(s.get("constructors") or {})
        result = {"season": int(year) if year else season,
                  "standings": [{"pos": e.get("position"), "team": e.get("Constructor", {}).get("name"),
                                 "points": e.get("points"), "wins": e.get("wins")} for e in entries]}
        if provisional:
            result["provisional"] = provisional["note"]
            result["note"] = "积分榜含预估积分，回答时必须说明该预估口径（" + provisional["note"] + "）"
        return result

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
        gp_kw = None  # 解析后的干净赛道关键词（剥离年份/轮次 token）
        if gp:
            info = parse_race_query(gp)
            gp_kw = info["gp"]
            race = find_race_for_query(f1_api, info["year"] or season,
                                       info["round"], info["gp"])
            if not race:
                # 指定赛道不在赛历：直接按名称查本地库，不再静默替换为下一站
                hit = cm.search_circuit(gp_kw or gp)
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
                return {"circuit": gp_kw or gp, "circuit_id": None, "race": None,
                        "found_local": False, "needs_web_search": True,
                        "search_hint": f"{gp_kw or gp} F1 circuit length corners lap record characteristics",
                        "note": "本地赛道库与本赛季赛历均无此赛道，必须联网检索获取，"
                                "禁止凭印象编造参数；回答中标注信息来自网络检索"}
        if not race:
            race = f1_api.get_next_race()
        if not race:
            return {"error": "本赛季已无剩余比赛，且未指定可解析的赛道/分站"}
        circuit = (race or {}).get("Circuit", {}) or {}
        cid = circuit.get("circuitId", "")
        cname = circuit.get("circuitName", "") or gp_kw or gp
        # 本地库：circuitId 直查 → 名称模糊查（用解析后的干净关键词，原始输入可能含年份/轮次）
        meta = cm.get_circuit(cid) if cid else None
        if not meta and (cname or gp_kw):
            hit = cm.search_circuit(gp_kw or cname)
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
            out["error"] = f"{q_season}赛季暂无PU部件数据（FIA约2021年起才有官方文档）"
        # 当前赛季附 PU 用量堆叠柱图（chart_md 由 L2 嵌图）
        if elements and q_season == season:
            try:
                import os as _os
                from .chart_factory import render_pu_usage_chart
                base = _os.getenv("RATING_BASE_URL", "").rstrip("/")
                if not base:
                    p = None
                else:
                    p = render_pu_usage_chart(season, f1cosmos=f1cosmos)
                if p and base:
                    out["chart_md"] = f"![pic#800px #600px]({base}/exports/{_os.path.basename(p)})"
                    out.setdefault("note", "")
                    out["note"] = (out["note"] + "；" if out["note"] else "") + \
                        "chart_md 为 PU 部件用量堆叠柱图（[超限]=超赛季上限），回答 PU 用量类问题时必须原文嵌入该图"
            except Exception:
                pass
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
        if not (info["round"] or info["gp"] or info["team"]):
            # 空查询或纯年份（含 LLM 把年份误塞进 gp_or_team 的情况）
            if year or info["year"]:
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
                    entry = per_team.setdefault(key, {"variants": {}, "gps": set(),
                                                      "cls": {"performance": 0, "circuit_specific": 0, "other": 0},
                                                      "events": {}})
                    entry["variants"][u["team"]] = entry["variants"].get(u["team"], 0) + 1
                    if u.get("gp_name"):
                        entry["gps"].add(u["gp_name"])
                    cls = _upgrade_type_class(u.get("type"))
                    entry["cls"][cls] += 1
                    if cls == "performance":
                        ev_key = (u.get("round") or 0, u.get("gp_name") or "")
                        ev = entry["events"].setdefault(ev_key, {"components": [], "core": False, "n": 0})
                        ev["n"] += 1
                        comp = (u.get("component") or "").strip()
                        if comp and comp not in ev["components"]:
                            ev["components"].append(comp)
                        if any(k in comp.lower() for k in _UPGRADE_CORE_COMPONENTS):
                            ev["core"] = True
                summary = []
                major_by_team: Dict[str, dict] = {}
                for entry in per_team.values():
                    display = max(entry["variants"], key=entry["variants"].get)
                    summary.append({
                        "team": display,
                        "total_upgrades": sum(entry["variants"].values()),
                        "gps_with_upgrades": len(entry["gps"]),
                        "performance": entry["cls"]["performance"],
                        "circuit_specific": entry["cls"]["circuit_specific"],
                        "other": entry["cls"]["other"],
                    })
                    events = [{"round": rk, "gp": gname, "components": ev["components"][:6],
                               "components_total": len(ev["components"]),
                               "n_performance": ev["n"]}
                              for (rk, gname), ev in entry["events"].items()
                              if ev["core"] or ev["n"] >= _UPGRADE_MAJOR_PACKAGE_MIN]
                    events.sort(key=lambda e: (e["round"], e["gp"]))
                    rec = {"team": display, "major_events": events}
                    if len(events) > _UPGRADE_MAJOR_MAX_PER_TEAM:
                        keep = sorted(events, key=lambda e: (-e["n_performance"], e["round"]))[
                               :_UPGRADE_MAJOR_MAX_PER_TEAM]
                        keep.sort(key=lambda e: e["round"])
                        rec["major_events"] = keep
                        rec["more_events"] = len(events) - len(keep)
                    major_by_team[display] = rec
                summary.sort(key=lambda x: -x["total_upgrades"])
                major_upgrades = [major_by_team[s["team"]] for s in summary]
                return {"season": q_season, "team_upgrade_counts": summary,
                        "major_upgrades": major_upgrades,
                        "note": ("team_upgrade_counts=FIA全量申报条目数（表格照旧完整给出，已附 performance/circuit_specific/other 分类计数）；"
                                 "major_upgrades=有显著提升效果的大升级（性能类核心部件：底盘/扩散器/侧箱/翼片/悬挂等，或单站≥3件的多件升级包），"
                                 "每队最多列8个事件（超出以 more_events 计数），每事件 components 最多列6项（components_total 为实际件数）——"
                                 "用户最关心这些，回答必须以 major_upgrades 为主体逐队呈现（分站+主要部件+差异说明），"
                                 "Circuit Specific 赛道适配与可靠性小改只需一句带过；问某车队/某分站明细时再传 gp_or_team")}
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

    def _safety_car_events(gp="", year=None):
        """安全车/VSC/红旗事件（chart_factory.get_sc_events 共享提取）"""
        from datetime import datetime as _dt
        from .chart_factory import get_sc_events, render_sc_timeline
        from .f1_api import find_race_by_circuit
        q_season = int(year) if year else season
        schedule = f1_api.get_schedule_for_year(q_season) or []
        race = None
        if gp:
            import re as _re
            m = _re.search(r"^[Rr]\s?0*(\d{1,2})$", str(gp).strip())
            if m:
                race = next((r for r in schedule if int(r.get("round", 0)) == int(m.group(1))), None)
            else:
                race = find_race_by_circuit(schedule, str(gp))
        else:
            today = _dt.now().date().isoformat()
            past = [r for r in schedule if r.get("date") and r["date"] < today]
            race = past[-1] if past else None
        if not race:
            return {"error": f"未找到分站（{gp or '最近已完赛'}）"}
        # 未赛判定按正赛开始时间戳（正赛日当天完赛后可正常查询——
        # 按日期字符串比较会把正赛日全天误挡，2026-10-10 review 实证）
        from datetime import timezone as _tzu2
        race_dt = None
        try:
            rt = race.get("time", "00:00:00Z").replace("Z", "")
            race_dt = _dt.fromisoformat(f"{race['date']}T{rt}").replace(tzinfo=_tzu2.utc)
        except Exception:
            pass
        if race_dt and _dt.now(_tzu2.utc) < race_dt:
            return {"race": race.get("raceName"), "error": "分站未赛，无安全车事件"}
        evts = get_sc_events(q_season, int(race["round"]), f1_api)
        if evts is None:
            # 双源皆不可达：诚实报"数据不可用"，禁止断言本站干净
            return {"race": race.get("raceName"), "data_available": False,
                    "error": "安全车数据源当前不可用",
                    "note": "OpenF1 当前 401（现场环节锁定，赛后恢复）且 livetiming 归档不可达，"
                            "无法确认本站安全车/VSC/红旗事件。禁止断言'本站干净/无安全车'，"
                            "应如实告知数据暂不可用并建议赛后重试"}
        kind_cn = {"sc": "安全车SC", "vsc": "虚拟安全车VSC", "red": "红旗"}
        result = {"race": race.get("raceName"), "season": q_season,
                  "round": int(race["round"]),
                  "counts": {"sc": sum(1 for e in evts if e["kind"] == "sc"),
                             "vsc": sum(1 for e in evts if e["kind"] == "vsc"),
                             "red_flag": sum(1 for e in evts if e["kind"] == "red")},
                  "events": [{"type": kind_cn.get(e["kind"], e["kind"]),
                              "deploy_lap": e["start_lap"], "end_lap": e["end_lap"],
                              "duration_laps": e["end_lap"] - e["start_lap"],
                              "reason": e["message"]} for e in evts]}
        if not evts:
            result["note"] = "本站无安全车/VSC/红旗事件记录"
        else:
            result["note"] = "事件按部署圈排序；duration_laps 为持续圈数（红旗为标记值非实际停赛时长）"
        # 时间线图（有事件才出图；chart_md 由 L2 嵌图）
        try:
            import os as _os
            base = _os.getenv("RATING_BASE_URL", "").rstrip("/")
            if base and evts:
                p = render_sc_timeline(q_season, int(race["round"]),
                                       race.get("raceName", ""), f1_api=f1_api)
                if p:
                    result["chart_md"] = (f"![pic#800px #260px]({base}/exports/"
                                          f"{_os.path.basename(p)})")
                    result["note"] += "；chart_md 为安全车事件时间线图，回答中必须原文嵌入该图"
        except Exception:
            pass
        return result

    def _race_charts(gp="", chart="race_report"):
        """分站可视化图表（统一入口，chart_md 由 L2 嵌图）"""
        from datetime import datetime as _dt
        from .f1_api import find_race_by_circuit
        from . import chart_factory as cf
        q = (gp or "").strip()
        chart = (chart or "race_report").strip().lower()
        render_map = {
            "race_report": cf.render_race_report_chart,
            "pace": cf.render_pace_chart,
            "strategy": cf.render_strategy_chart,
            "positions": cf.render_positions_chart,
            "pitstop": cf.render_pitstop_chart,
        }
        schedule = f1_api.get_schedule_for_year(season) or []
        race = None
        if q:
            import re as _re
            m = _re.search(r"^[Rr]\s?0*(\d{1,2})$", q)
            race = next((r for r in schedule if int(r.get("round", 0)) == int(m.group(1))), None) if m \
                else find_race_by_circuit(schedule, q)
        else:
            today = _dt.now().date().isoformat()
            past = [r for r in schedule if r.get("date") and r["date"] < today]
            race = past[-1] if past else None
        if not race:
            return {"error": f"未找到分站（{q or '最近已完赛'}）"}
        # 未赛判定按环节开始时间戳：排位类图表看排位时间（排位结束即可查，
        # 周六晚问排位差距图不该被"分站未赛"挡住），正赛类看正赛时间
        from datetime import timezone as _tzu2, timedelta as _td2
        race_dt = None
        try:
            if chart in ("quali_gap", "sprint_quali_gap"):
                sess_key = "SprintQualifying" if chart == "sprint_quali_gap" else "Qualifying"
                sess_info = race.get(sess_key) or {}
                rt = (sess_info.get("time") or "00:00:00Z").replace("Z", "")
                race_dt = _dt.fromisoformat(f"{sess_info.get('date', race['date'])}T{rt}").replace(tzinfo=_tzu2.utc)
            else:
                rt = race.get("time", "00:00:00Z").replace("Z", "")
                race_dt = _dt.fromisoformat(f"{race['date']}T{rt}").replace(tzinfo=_tzu2.utc)
        except Exception:
            pass
        if race_dt and _dt.now(_tzu2.utc) < race_dt + _td2(hours=2):
            return {"race": race.get("raceName"), "error": "环节未进行，暂无统计数据"}
        round_num = int(race["round"])
        race_name = race.get("raceName", "")

        if chart in ("quali_gap", "sprint_quali_gap"):
            stype = "sprint_qualifying" if chart == "sprint_quali_gap" else "qualifying"
            p = cf.render_quali_gap_chart(season, round_num, race_name,
                                          f1_api=f1_api, session_type=stype)
            kind_cn = "冲刺排位差距图" if stype == "sprint_qualifying" else "排位差距图"
        elif chart in render_map:
            p = render_map[chart](season, round_num, race_name, f1_api=f1_api)
            kind_cn = {"race_report": "赛后三联统计图（pace分布/轮胎策略/名次变化）",
                       "pace": "正赛pace分布箱线图", "strategy": "轮胎策略时间线",
                       "positions": "名次变化图", "pitstop": "进站时长车队对比图"}[chart]
        else:
            return {"error": f"未知图表类型「{chart}」（可选 race_report/pace/strategy/positions/quali_gap/sprint_quali_gap/pitstop）"}
        if not p:
            return {"race": race_name, "error": f"{kind_cn}数据不可用（分站未赛或数据源未收录）"}

        result = {"race": race_name, "season": season, "round": round_num,
                  "chart_type": chart, "kind": kind_cn}
        import os as _os
        base = _os.getenv("RATING_BASE_URL", "").rstrip("/")
        if base:
            result["chart_md"] = f"![pic#900px #600px]({base}/exports/{_os.path.basename(p)})"
            result["note"] = (f"chart_md 为{kind_cn}，回答中必须原文嵌入该图（它是图片链接，"
                              "QQ 会直接渲染成图），禁止只描述不嵌图")
        else:
            result["note"] = ("当前环境无公网图床（RATING_BASE_URL 未配置），chart_md 不可用，"
                              "请以文字描述图表结论，禁止声称无图")
        return result

    def _destructors(query="", year=None):
        """事故维修成本（destructors + 柱图，chart_md 由 L2 嵌图）"""
        if not f1cosmos:
            return {"error": "数据源未启用"}
        q_season = int(year) if year else season
        text, md = f1cosmos.format_destructors(query or "", q_season)
        result = {"season": q_season, "formatted_text": text}
        try:
            import os as _os
            from .chart_factory import render_destructors_chart
            base = _os.getenv("RATING_BASE_URL", "").rstrip("/")
            if base:
                p = render_destructors_chart(q_season, f1cosmos=f1cosmos)
                if p:
                    result["chart_md"] = (f"![pic#800px #600px]({base}/exports/"
                                          f"{_os.path.basename(p)})")
                    result["note"] = ("chart_md 为事故维修成本柱图（车手榜+车队榜），"
                                      "回答中必须原文嵌入该图")
        except Exception:
            pass
        return result

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
        # 天气时间序列图：已完赛=OpenF1 实测，未赛=Open-Meteo 预报（chart_md 由 L2 嵌图）
        try:
            import os as _os
            base = _os.getenv("RATING_BASE_URL", "").rstrip("/")
            if base:
                from .chart_factory import render_weather_chart
                p = render_weather_chart(q_season, int(race["round"]),
                                         race.get("raceName", ""), f1_api=f1_api)
                if p:
                    fname = _os.path.basename(p)
                    result["chart_md"] = f"![pic#800px #400px]({base}/exports/{fname})"
                    result["note"] = ("chart_md 为本站天气时间序列图（气温/沥青温度/降水/风速），"
                                      "回答本站天气类问题时必须原文嵌入该图")
        except Exception:
            pass
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
        result = {"season": season, "teams": all_s}
        # 附车队实力雷达图（随赛季进程自动按累计成绩+升级件重算，文件名带轮次破缓存）
        try:
            import os as _os
            from .chart_factory import render_team_strength_radar
            p = render_team_strength_radar(season, f1_api=f1_api, f1cosmos=f1cosmos)
            base = _os.getenv("RATING_BASE_URL", "").rstrip("/")
            if p and base:
                result["chart_md"] = f"![pic#600px #540px]({base}/exports/{_os.path.basename(p)})"
                result["note"] = ("chart_md 为车队实力雷达图（6维 min-max 归一化，外圈=更强），"
                                  "回答车队实力对比类问题时必须原文嵌入该图")
        except Exception:
            pass
        return result

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
        result = {"race": race.get("raceName"), "season": q_season, "round": round_num,
                  "ranking_by_best_stint": ranked[:10],
                  "note": "median_s=stint中位圈速(秒)；deg_ms_per_lap=衰减斜率(ms/圈，正=衰减)；"
                          "avg_deg_ms=各stint衰减均值"}
        # pace 分布图（已完赛分站）：chart_url 交给 L2 以图片形式嵌入回答
        if is_completed:
            try:
                import os as _os
                from .chart_factory import render_pace_chart
                base = _os.getenv("RATING_BASE_URL", "").rstrip("/")
                p = (render_pace_chart(q_season, round_num, race.get("raceName", ""),
                                       f1_api=f1_api)
                     if base else None)
                if p:
                    fname = _os.path.basename(p)
                    result["chart_url"] = f"{base}/exports/{fname}"
                    result["chart_md"] = (f"![pic#800px #450px]({result['chart_url']})")
                    result["note"] += ("；chart_md 为本场 pace 分布统计图（箱线图+轮胎配方着色+"
                                       "中位差条形图），回答中必须用 chart_md 原文嵌入该图")
            except Exception:
                pass
        return result

    def _race_tech_params(gp=""):
        """分站 FIA Power Unit Information 技术参数（FIADocsAPI 提供）"""
        if not (f1cosmos and hasattr(f1cosmos, "get_event_pu_info")):
            return {"error": "FIA文档数据源未启用"}
        gp = (gp or "").strip()
        race = None
        if gp:
            info = parse_race_query(gp)
            race = find_race_for_query(f1_api, info["year"] or season,
                                       info["round"], info["gp"])
        if not race:
            race = f1_api.get_next_race()
        if not race:
            return {"error": "本赛季已无剩余比赛，且未指定可解析的分站"}
        race_name = race.get("raceName", "")
        q_season = int(race.get("season", season) or season)
        result = f1cosmos.get_event_pu_info(race_name, q_season)
        if not result:
            return {"race": race_name, "error": "该站 Power Unit Information 文档尚未发布"
                                                "（通常比赛周四挂出）或不存在"}
        return {"race": race_name, **result}

    def _video_summary(url=""):
        """B站视频导读：F1关键词门禁 → 字幕 → ASR兜底（common/bilibili_summary）。
        结果按 bvid 缓存（字幕/ASR文本不变长缓存，无文本负结果6h）——同视频重复提问不重跑ASR"""
        import os as _os
        from .bilibili_summary import BiliVideoClient
        client = BiliVideoClient()
        bvid = BiliVideoClient.parse_bvid(url or "")
        if not bvid:
            return {"error": "未识别到B站视频链接（支持 BV号/av号/b23短链）"}
        cache = getattr(f1_api, "cache", None)
        cache_key = f"bili_summary_v1_{bvid}"
        info = client.get_video_info(bvid)
        if not info:
            return {"error": f"视频信息获取失败（{bvid}），可能已删除/审核中或接口风控"}
        if not info.get("cid"):
            return {"error": f"视频 {bvid} 缺少 cid（分P信息异常），无法取字幕"}
        base = {"title": info["title"], "uploader": info["owner"],
                "duration_min": round(info["duration"] / 60, 1), "bvid": bvid}
        is_f1, hits = BiliVideoClient.is_f1_related(info)
        if not is_f1:
            return {**base, "is_f1": False,
                    "note": "该视频未命中F1关键词门禁，直接回复用户\"该视频似乎与F1无关，不予总结\"，禁止再调用任何工具"}
        if cache:
            cached = cache.load(cache_key)
            if cached:
                return {**base, "is_f1": True, **cached}
        text, status = client.get_subtitle(bvid, info["cid"])
        source = "subtitle"
        if not text and _os.getenv("BILI_VIDEO_ASR_ENABLED", "false").lower() == "true":
            # ASR 为分钟级阻塞（占 L1 槽位至结束）：默认上限 8 分钟，防长视频堵死并发
            try:
                max_min = float(_os.getenv("BILI_VIDEO_ASR_MAX_MIN", "8"))
            except ValueError:
                max_min = 8.0
            if info["duration"] <= max_min * 60:
                # in-flight 去重：同 bvid 并发时后到者等首个结果（最长等 4 分钟）
                with _bili_asr_inflight_lock:
                    ev = _bili_asr_inflight.get(bvid)
                    if ev is None:
                        ev = _threading.Event()
                        _bili_asr_inflight[bvid] = ev
                        leader = True
                    else:
                        leader = False
                if not leader:
                    ev.wait(timeout=240)
                    if cache:
                        cached = cache.load(cache_key)
                        if cached:
                            return {**base, "is_f1": True, **cached}
                else:
                    try:
                        audio_urls = client.get_audio_url_candidates(bvid, info["cid"])
                        if audio_urls:
                            text = client.transcribe_audio(audio_urls)
                            if text:
                                source = "asr"
                                status = f"ASR转写（{status}）"
                    finally:
                        with _bili_asr_inflight_lock:
                            _bili_asr_inflight.pop(bvid, None)
                        ev.set()
        if not text:
            login_hint = ("" if client.logged_in else
                          "；机器人未配置B站登录态，管理员可发 /bili_login 扫码解锁字幕")
            out = {**base, "is_f1": True, "no_text": True,
                   "note": f"字幕不可用（{status}{login_hint}），直接回复用户该视频暂不支持导读，禁止编造内容"}
            if cache:
                cache.save(cache_key, {"no_text": True,
                                       "note": out["note"]}, ttl_hours=6)
            return out
        try:
            cap = int(_os.getenv("BILI_VIDEO_TEXT_CAP", "12000"))
        except ValueError:
            cap = 12000
        payload = {"source": source, "text_status": status,
                   "text": text[:cap], "truncated": len(text) > cap,
                   "note": "这是视频字幕/转写文本，据此生成中文导读（一句话结论→分节要点→F1术语解释）；"
                           "truncated=true 时注明仅覆盖前部分内容"}
        if cache:
            cache.save(cache_key, payload, ttl_hours=None)  # 视频字幕/转写不可变，永久缓存
        return {**base, "is_f1": True, **payload}

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
        "get_race_tech_params": _race_tech_params,
        "get_safety_car_events": _safety_car_events,
        "get_race_charts": _race_charts,
        "get_destructors": _destructors,
        "get_video_summary": _video_summary,
    }
