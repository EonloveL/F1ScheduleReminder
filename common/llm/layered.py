"""两段式分层问答 mixin：L0 改写 / L1 取数 / L2 成文 + 单调用降级路径"""

import json
import logging
import os
from typing import Optional, Dict, Any, List

import requests

from .prompts import _load_prompt
from .tracing import get_qid, set_qid, new_qid

logger = logging.getLogger(__name__)


class LayeredMixin:
    """两段式分层问答：ask_layered / _ask_layered_impl / _rewrite_query / _l1_collect /
    _l1_tool_manifest / _l1_post / _l1_collect_json / _l1_collect_native /
    ask_with_tools / _ask_with_tools_impl / ask / ask_with_context"""

    # L1 伪工具：标记需要联网检索，真正搜索在 L2 由 Kimi $web_search 执行
    _WEB_SEARCH_PSEUDO_TOOL = {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "当问题需要最新新闻/传闻/车手市场/背景资料等非结构化信息时调用，query为搜索关键词",
            "parameters": {"type": "object",
                           "properties": {"query": {"type": "string", "description": "搜索关键词"}},
                           "required": ["query"]},
        },
    }

    _DEFAULT_L1_SYSTEM = (
        "你是F1数据检索助手，不负责直接回答用户问题。"
        "根据用户问题判断需要哪些数据，调用提供的工具获取（积分榜/成绩/赛程/动力单元/升级件等）；"
        "不要凭记忆编造数据，不要输出最终答案。"
        "竞猜/预测/对比类多问句：把每个子问题映射到对应工具取基线数据"
        "（名次/最快圈/发车位grid/完赛位对比→get_race_results；积分差→standings；天气→get_race_weather；"
        "升级→get_upgrades），一轮可并行调用多个工具；数据不能直接给出结果的（如预测），取基线供成文层推理。"
        "数据工具支持历史赛季：问题涉及历史年份（如2025赛季）时，get_season_results / get_race_results /"
        " get_driver_standings / get_constructor_standings 必须传 year 参数；"
        "整理整年成绩用 get_season_results(year=…)，按用户要求设置 top_n（冠军榜=1，冠亚季军=3，积分区=10）。"
        "问升级最多/升级数量对比时，get_upgrades 只传 year 不传 gp_or_team，一次拿回全赛季汇总，禁止逐站逐队多次调用。"
        "问总冠军/世界冠军/理论夺冠可能/争冠形势时，必须调用 get_championship_outlook 拿数学推算与两种概率数值。"
        "问正赛长距离节奏/轮胎管理/衰减/stint 表现时，必须调用 get_stint_analysis 拿圈速数据推导，禁止凭印象回答。"
        "问车手“近N站 pace/近期状态/近期节奏/状态起伏/有没有竞争力”时，必须调用 get_race_prediction（feature_breakdown 含每车手近4场 form_quali/form_finish）；"
        "车队层面近4场 pace 用 get_team_strengths（pace_recent_avg）；单站逐圈 pace/轮胎衰减用 get_stint_analysis；禁止未取数就声称“没有 pace/状态数据”。"
        "问“与某赛道相似/同类型的其他赛道 pace/强弱”时，调用 get_team_strengths（by_downforce/by_tire_stress/by_altitude 赛道类型维度）+ get_race_prediction（type_affinity/circuit_profile）。"
        "预测/前瞻类问题必须调用 get_race_weather 获取比赛时段天气（气温/赛道地面温度代理/降水概率与ETA/风力）。"
        "技术规格/规则数值类问题（电池容量/MGU-K功率/最低重量等）工具无法覆盖，必须调用 web_search 记录检索需求，"
        "不得凭记忆回答具体数值，也不得声称无法联网。"
        "若问题还涉及最新新闻/传闻/背景资料，额外调用 web_search 工具记录检索需求。"
        "问题涉及的各站轮次(round)可先通过赛程数据推断。"
    )
    _L1_SYSTEM = _load_prompt("l1_collect", _DEFAULT_L1_SYSTEM)

    _DEFAULT_REWRITE_SYSTEM = (
        "你是查询改写助手。结合对话历史和当前时间，把用户的最新问题改写为独立完整的问题：\n"
        "1. 补全代词和省略指代（他/她/它/那场/这支/他们）为具体名称\n"
        "2. 把相对时间（上周/昨天/刚才/上一场/下一站）换算为具体日期或赛事名称\n"
        "3. 保持用户原意，不要扩写、不要回答、不要解释\n"
        "4. 问题本身已独立完整时原样输出\n"
        "5. 必须完整保留用户给出的前提、假设与给定条件（如“已知…将…”“假设…”），禁止省略或概括\n"
        "只输出改写后的问题文本。"
    )
    _REWRITE_SYSTEM = _load_prompt("query_rewrite", _DEFAULT_REWRITE_SYSTEM)

    # 触发深度分析模式的工具（需要联网评论+推理模型保证质量）
    DEEP_MODE_TOOLS = {"get_upgrade_analysis_data"}
    # 触发统计预测呈现模式的工具（模型已算好数字，AI 只解读，禁止改动）
    PREDICTION_MODE_TOOLS = {"get_race_prediction", "get_championship_outlook"}

    _DEFAULT_PREDICTION = (
        "【统计预测呈现模式】\n"
        "预测名次与概率由统计模型计算完成，必须原样使用，禁止自行重排或改动数字；"
        "你在成文阶段无法调用工具（工具已在取数阶段执行完毕，结果已注入上文），"
        "禁止声称'我现在调用工具'或凭空构造原始 JSON 或编造字段名——"
        "排位字段 exp_quali_pos/quali_top3_prob/top10_prob，正赛字段 grid/exp_finish，名单字段 pos/driver/team；"
        "区块里没有的字段就是没有，不得补造；"
        "用 feature_breakdown（积分榜/近期状态/赛道历史/赛道类型适配/升级件数）解释原因；"
        "必须同时呈现 form(近期状态) 与 circuit_hist(赛道历史) 的张力：近期好但赛道历史差时，"
        "禁止只凭一个断言'没竞争力'，要把两个数值都摆出并说明模型口径（赛道历史已用中位数抗单次事故污染）；"
        "正赛预期必须引用 race_pace_idx（近4场 stint 长距离配速指数，>100=正赛快于中位）与 tyre_deg_ms"
        "（轮胎衰减斜率，正值=进站压力）；当 form/circuit_hist 与 race_pace_idx 不一致时"
        "（排位强但长距离弱，或排位弱但长距离强），必须点出'该车排位预期X，但长距离配速显示其实更Y'；"
        "声明概率性质（安全车/事故不可预测）；罚退/代打前提已在模型中应用；"
        "竞猜类问题逐题引用模型输出给预测值；卡片格式输出。"
    )
    _PREDICTION_PROMPT = "\n\n" + _load_prompt("prediction", _DEFAULT_PREDICTION)

    _DEFAULT_DEEP_ANALYSIS = (
        "【升级件效果分析模式】\n"
        "用户在进行升级件效果分析，请按以下框架深度作答：\n"
        "1. 申报意图：对照工具取回的升级件申报数据（更新原因/与旧版差异/工作原理描述），转述每支车队想解决什么问题\n"
        "2. 实测趋势：结合近期完赛名次/积分趋势（team_trends）与归一化圈速指标（pace_trends）观察变化，跨站趋势优先，单站不做定论\n"
        "3. 圈速纪律：不同赛道长度与特性不同，绝对圈速不能直接跨赛道比较；跨赛道对比必须使用 pace_index"
        "（=100 为全场中位水平，>100 快于中位），或做同赛道历年对比；赛道内对比可用分段/测速点绝对值\n"
        "4. 舆论参考：联网检索媒体与技术评论，转述主流观点并注明来源\n"
        "5. 严谨边界：明确区分事实（申报内容、完赛数据）与推断；升级效果只能表述为相关性而非因果，"
        "需提示赛道特性/天气/车手发挥/最快圈策略等干扰因素\n"
        "6. 按车队分节输出，结构清晰，结论给出置信度"
    )
    # 追加到系统提示末尾，保留原有 "\n\n" 前缀语义
    _DEEP_ANALYSIS_PROMPT = "\n\n" + _load_prompt("deep_analysis", _DEFAULT_DEEP_ANALYSIS)

    def ask_layered(self, user_id: str, question: str,
                    fn_tools: List[Dict[str, Any]],
                    fn_handlers: Dict[str, Any],
                    max_len: int = 1800,
                    data_context: str = None,
                    group_openid: str = None,
                    meta_out: Dict[str, Any] = None,
                    cancel_event=None,
                    qid: str = None) -> Optional[str]:
        """统一问答入口（含 trace ID 生命周期管理），实现见 _ask_layered_impl"""
        prev = get_qid()
        set_qid(qid or prev or new_qid())
        try:
            return self._ask_layered_impl(user_id, question, fn_tools, fn_handlers,
                                          max_len, data_context, group_openid,
                                          meta_out, cancel_event)
        finally:
            self._sc_notices.pop(get_qid(), None)  # 失败路径防泄漏（成功路径已在 _finalize_answer pop）
            set_qid(prev)

    def _ask_layered_impl(self, user_id: str, question: str,
                    fn_tools: List[Dict[str, Any]],
                    fn_handlers: Dict[str, Any],
                    max_len: int = 1800,
                    data_context: str = None,
                    group_openid: str = None,
                    meta_out: Dict[str, Any] = None,
                    cancel_event=None) -> Optional[str]:
        """
        两段式分层问答（唯一问答路径，单调用是同路径的降级分支，见 ask_with_tools）：
        - L0 查询改写（DeepSeek）：结合历史+当前时间把追问/代词/相对时间解析为独立问题
        - L1 取数层（DeepSeek，不联网）：只做工具调用决策，客户端执行工具收集结构化数据
        - L2 成文层（Kimi）：注入L1数据+标准姓名锚定表生成回答；仅当L1判定需要时启用联网搜索
        供应商不齐（缺DeepSeek或Kimi）时自动回退单调用 ask_with_tools
        meta_out（可选 dict）：回填 {"tools": [实际调用的工具名], "web_search": bool, "provider": str,
                                    "stage": 当前阶段(rewrite/collect/compose)，供等待提示展示进度}
        cancel_event（可选 threading.Event）：置位后轮次边界停止，尽力返回已有内容
        """
        if not self.enabled:
            return None
        self.last_error = None

        def _set_stage(stage: str):
            if meta_out is not None:
                meta_out["stage"] = stage

        def _cancelled() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        with self._providers_lock:
            _ask_order = list(self.ask_order)
            _available = dict(self.available)  # 快照防 reload_runtime 中途撕裂（review M2）
        l1_provider = next((p for p in _ask_order if not _available[p]["web_search"]), None)
        l2_provider = next((p for p in _ask_order if _available[p]["web_search"]), None)
        # 管理员分层偏好覆盖（默认 None 不影响现状）
        l1_provider, l1_model = self._resolve_layer("l1", l1_provider)
        l2_provider, l2_pref_model = self._resolve_layer("l2", l2_provider)
        # L0 改写层独立可配（缺省回退 L1 供应商）；此前 /model set l0 是死配置（review M1）
        l0_provider, l0_model = self._resolve_layer("l0", l1_provider)
        if not l1_provider or not l2_provider:
            return self.ask_with_tools(user_id, question, fn_tools, fn_handlers, max_len, data_context,
                                       meta_out=meta_out, cancel_event=cancel_event,
                                       group_openid=group_openid)

        # L0 查询改写：让L1/L2都能拿到指代完整的问题。
        # 智能跳过：无会话历史且问题不含指代词/相对时间词时，改写无收益，省去一轮 HTTP（5-20s）
        _set_stage("rewrite")
        _REF_HINTS = ("他", "她", "它", "他们", "那场", "这支", "那个", "这位", "此人",
                      "昨天", "今天", "明天", "刚才", "上周", "下周", "上一场",
                      "下一站", "最近一场", "刚刚", "前年", "去年", "今年")
        if not self._memory.has_active_session(user_id) and not any(h in question for h in _REF_HINTS):
            standalone = question
        else:
            standalone = self._rewrite_query(l0_provider, user_id, question, model=l0_model)

        if _cancelled():
            return None
        _set_stage("collect")
        collected, search_queries = self._l1_collect(l1_provider, standalone, fn_tools, fn_handlers,
                                                     user_id=user_id, cancel_event=cancel_event,
                                                     model=l1_model)
        if collected is None:
            if _cancelled():
                return None
            logger.warning("L1取数失败，回退单调用模式")
            return self.ask_with_tools(user_id, standalone, fn_tools, fn_handlers, max_len, data_context,
                                       meta_out=meta_out, cancel_event=cancel_event,
                                       group_openid=group_openid, skip_providers={l1_provider})

        # 数据驱动联网升级：工具结果标 needs_web_search（如本地赛道库无档案的
        # get_circuit_info，2026-09-24 雪邦实证：缺档案时凭印象答、未联网校对），
        # 取 search_hint 注入 search_queries，让 L2 带 $web_search 成文
        for _name, _res in collected:
            if '"needs_web_search": true' not in (_res or ""):
                continue
            try:
                _hint = (json.loads(_res) or {}).get("search_hint")
            except (ValueError, TypeError):
                _hint = None
            _q = _hint or f"{standalone} 最新信息"
            if _q not in search_queries:
                search_queries.append(_q)
                logger.info(f"工具[{_name}]标记缺本地数据，升级联网检索: {_q[:50]}")

        # 深度分析模式：分析类工具被调用 → 强制联网 + 推理模型
        deep_mode = any(name in self.DEEP_MODE_TOOLS for name, _ in collected)
        l2_model = l2_pref_model  # 管理员分层偏好（/model set l2 ...），None=供应商默认
        # L2 输出预算：kimi-k2.6 实测生成速度仅 ~31 tok/s（2026-09-15 探测：
        # 29k输入+2000输出=78s），不封顶时长回答必超读超时（150s）。
        # 1800 tok ≈ 60s ≈ 覆盖1500字回答；被截断由 finish_reason=length 续写逻辑接管
        # （仍有界：60s×2 ≈ 120s < 150s）。深度模式不封顶（k3 推理 token 计入上限，封顶会截断思考）
        l2_extra = {"max_tokens": int(os.getenv("LLM_L2_MAX_TOKENS", "1800"))}
        # kimi-k2.6 是推理模型：默认档 249字回答烧 1135 推理 token、low 档在大 payload 下
        # 推理仍耗尽 1800 max_tokens → finish=length 且 content 为空 → L2 误判失败
        # （2026-09-23 部署前验证实证，四轮落 deepseek 兜底；none 档推理≈0、L2 恢复正常）。
        # L2 是成文层（数据已注入，禁止重新预测），推理无收益纯烧钱，默认 none；
        # reasoning_effort 仅 kimi 支持故按供应商限定
        if l2_provider == "kimi":
            # 白名单校验（OSS review 修复回灌）：非法值原样发给 kimi 会 400，
            # L2 误判失败落兜底且排障困难
            _re_eff = os.getenv("LLM_L2_REASONING", "none").strip().lower()
            if _re_eff not in ("none", "low", "medium", "high"):
                logger.warning(f"LLM_L2_REASONING 非法值 '{_re_eff}'，回退 none")
                _re_eff = "none"
            l2_extra["reasoning_effort"] = _re_eff
        if deep_mode:
            _deep_provider, _deep_model = self._resolve_layer("deep", l2_provider)
            # 深度模式强制联网（$web_search 为 kimi 专属能力）：偏好供应商无联网能力时
            # 不切换 provider，同时丢弃其模型名（模型不跨供应商通用，否则 404）
            if _deep_provider != l2_provider:
                if self.available.get(_deep_provider, {}).get("web_search"):
                    l2_provider = _deep_provider
                    l2_model = _deep_model
                else:
                    logger.warning(f"deep 层偏好供应商 {_deep_provider} 无联网能力，忽略其覆盖")
            else:
                l2_model = _deep_model
            if not l2_model:
                # 深度默认推理模型仅 kimi 适用；其他供应商用其默认模型
                l2_model = os.getenv("LLM_DEEP_MODEL", "kimi-k3") if l2_provider == "kimi" else None
            # 深度模式整体替换 l2_extra（不封顶——推理 token 计入上限）；
            # reasoning_effort 仅 kimi 系支持，护栏供应商（当前联网供应商结构性只有 kimi，
            # 但配置可扩展，防未来误传给不支持的供应商）
            # 深度模式整体替换 l2_extra（不封顶——推理 token 计入上限）；
            # reasoning_effort 仅 kimi 系支持，护栏供应商（当前联网供应商结构性只有 kimi，
            # 但配置可扩展，防未来误传给不支持的供应商）；白名单校验防 400（回灌）
            _deep_eff = os.getenv("LLM_DEEP_REASONING", "high").strip().lower()
            if _deep_eff not in ("none", "low", "medium", "high"):
                logger.warning(f"LLM_DEEP_REASONING 非法值 '{_deep_eff}'，回退 high")
                _deep_eff = "high"
            l2_extra = ({"reasoning_effort": _deep_eff}
                        if l2_provider == "kimi" else {})
            if not search_queries:
                search_queries = ["媒体对该站升级件效果的技术评论"]
            logger.info(f"进入深度分析模式: model={l2_model} 强制联网")

        # L1 漏记联网需求的启发式兜底：问题含核实/最新/变动/规则类意图时必须联网，
        # 否则 L2 无搜索工具可用，会（正确地）声称"无法联网"并可能陷入重复念叨
        if not search_queries:
            import re as _re
            if _re.search(r"核实|确认|最新|新闻|变动|罚退|规则|传闻|真假|是否属实|消息|搜索|查证", standalone):
                search_queries = [standalone[:120]]
                logger.info("L1未记录联网需求，按意图启发式注入 web_search")

        system = self.SYSTEM_PROMPT + self.name_glossary() + self.rules_anchor()
        if deep_mode:
            system += self._DEEP_ANALYSIS_PROMPT
        # 统计预测呈现模式（模型已算好数字，注入呈现纪律）
        pred_mode = any(name in self.PREDICTION_MODE_TOOLS for name, _ in collected)
        if pred_mode:
            system += self._PREDICTION_PROMPT
            logger.info("进入统计预测呈现模式（模型计算+AI解读）")
        if collected:
            # 工具结果注入上限：get_race_prediction 返回完整 20 车手排位+正赛+特征分解，
            # 实测 >4000 字符；旧 4000 截断会把 JSON 拦腰斩断（正好切在 Norris P8），
            # 模型拿到残缺 JSON 后自我怀疑甚至编造字段名（2026-09-21 事故）。
            # 提升到 16000 保证预测类大结果完整，可 env 调 LLM_TOOL_BLOB_MAX
            _blob_max = int(os.getenv("LLM_TOOL_BLOB_MAX", "16000"))
            blob = "\n".join(f"[{name}] {result[:_blob_max]}" for name, result in collected)
            system += ("\n\n【工具取回的实时数据，回答中的数据/姓名以此为准，优先于联网搜索】\n" + blob)
        if data_context:
            system += ("\n\n【当前最新F1数据（机器人实时获取，优先以此为准）】\n" + data_context)
        system = self._inject_group_vision(system, group_openid, user_id, standalone)
        # 群知识库（事实校对+人工审核过的沉淀事实）+ 提问者画像注入
        try:
            from ..knowledge_store import KnowledgeStore
            _ks = KnowledgeStore.get()
            system += _ks.knowledge_block() + _ks.habit_block(user_id)
        except Exception:
            pass

        user_msg = standalone
        if search_queries:
            if (self.available.get(l2_provider) or {}).get("web_search", False):  # 热重载判空
                user_msg += "\n\n（请联网检索以下方面补充回答: " + "; ".join(search_queries) + "）"
            else:
                # L2 被管理员切到无联网供应商（/model set l2 ...）：注入联网指令
                # 只会诱发"假装联网"幻觉，改为记日志让模型基于已注入数据回答
                logger.warning(f"L2 供应商 {l2_provider} 无联网能力，忽略 {len(search_queries)} 条检索需求")

        mem_ctx = self._get_memory_context(user_id, standalone)
        if mem_ctx.get("memory_block"):
            system += "\n\n" + mem_ctx["memory_block"]
        history = mem_ctx["history"]
        messages = ([{"role": "system", "content": system}]
                    + history
                    + [{"role": "user", "content": user_msg}])

        l2_tools_log: list = []
        _set_stage("compose")
        answer = self._chat_once(l2_provider, messages,
                                 enable_search=bool(search_queries),
                                 model=l2_model, extra_payload=l2_extra,
                                 timeout=int(os.getenv("LLM_DEEP_TIMEOUT", "180")) if deep_mode
                                 else int(os.getenv("LLM_L2_TIMEOUT", "150")),
                                 tools_log=l2_tools_log,
                                 cancel_event=cancel_event)
        if not answer:
            if _cancelled():
                return None
            logger.warning("L2成文失败，回退单调用模式")
            return self.ask_with_tools(user_id, standalone, fn_tools, fn_handlers, max_len, data_context,
                                       meta_out=meta_out, cancel_event=cancel_event,
                                       group_openid=group_openid, skip_providers={l2_provider})
        if deep_mode:
            max_len = max(max_len, 3000)  # 分析报告更长，放宽截断
        all_tools = [name for name, _ in collected] + l2_tools_log
        # web_search 仅反映真实执行（tools_log 中出现搜索工具），而非 L1 记录了联网需求
        return self._finalize_answer(user_id, standalone, answer, max_len,
                                     meta_out, all_tools, l2_provider)

    def _rewrite_query(self, provider: str, user_id: str, question: str, model: str = None) -> str:
        """
        L0 查询改写：结合会话历史+当前北京时间，把追问/代词/相对时间解析为独立完整问题。
        失败静默回退原问题，不阻塞主流程。model: 层偏好覆盖（llm_prefs l0/l1）
        """
        try:
            from datetime import datetime, timezone, timedelta
            now_cn = datetime.now(timezone(timedelta(hours=8)))
            system = (self._REWRITE_SYSTEM
                      + f"\n\n当前时间: {now_cn.strftime('%Y年%m月%d日 %H:%M')} (北京时间)")
            history = self._get_history(user_id, question)
            messages = ([{"role": "system", "content": system}]
                        + history[-6:]
                        + [{"role": "user", "content": question}])
            rewritten = self._chat_once(provider, messages, temperature=0.1,
                                        timeout=20, enable_search=False, model=model)
            if rewritten:
                r = rewritten.strip().strip('"').strip()
                # 剥掉改写模型偶发的元叙述前缀（"改写如下：/改写后的问题是："之类），
                # 契约是"一句话问题"，多行输出取最后一个非空行
                #（2026-09-16 事故：元叙述随 standalone 进入 L2，被当成指令复述给用户）
                lines = [ln.strip() for ln in r.split("\n") if ln.strip()]
                if len(lines) > 1:
                    logger.warning(f"查询改写含多行/元叙述，取末行: {r[:60]}")
                    r = lines[-1]
                if r and len(r) <= 300:
                    if r != question:
                        logger.info(f"查询改写: 「{question[:30]}」-> 「{r[:60]}」")
                    return r
        except Exception as e:
            logger.warning(f"查询改写失败，使用原问题: {e}")
        return question

    def _l1_collect(self, provider: str, question: str,
                    fn_tools: List[Dict[str, Any]],
                    fn_handlers: Dict[str, Any],
                    user_id: str = None,
                    cancel_event=None,
                    model: str = None):
        """
        L1 取数层：驱动模型做取数决策，客户端执行工具并收集结果。
        默认 JSON mode（LLM_L1_JSON_MODE=true）：模型只能输出结构化 JSON，
        物理上无法"直接作答/输出前言"，消除原生 tool_calls 模式下偶发的违规回答；
        置 false 回退原生 function calling 路径。
        Returns:
            (collected: [(tool_name, result_str)], search_queries: [str])，失败返回 (None, None)
        """
        if os.getenv("LLM_L1_JSON_MODE", "true").strip().lower() == "true":
            return self._l1_collect_json(provider, question, fn_tools, fn_handlers,
                                         user_id=user_id, cancel_event=cancel_event, model=model)
        return self._l1_collect_native(provider, question, fn_tools, fn_handlers,
                                       user_id=user_id, cancel_event=cancel_event, model=model)

    def _l1_tool_manifest(self, fn_tools: List[Dict[str, Any]]) -> str:
        """把 OpenAI 风格 function schema 渲染为 JSON mode 提示词用的紧凑工具清单"""
        manifest = []
        for t in fn_tools:
            fn = t.get("function", {}) or {}
            props = ((fn.get("parameters") or {}).get("properties") or {})
            required = set((fn.get("parameters") or {}).get("required") or [])
            args_desc = ", ".join(
                f"{k}{'(必填)' if k in required else '(可选)'}: {(v or {}).get('description', '')}"
                for k, v in props.items())
            manifest.append(f"- {fn.get('name')}: {fn.get('description', '')}"
                            + (f" | 参数: {args_desc}" if args_desc else ""))
        return "\n".join(manifest)

    def _l1_post(self, provider: str, headers: Dict, payload: Dict, req_timeout):
        """L1 请求带兼容性重试（2026-10-24 分层选模型引入的非默认供应商适配）：
        - temperature 不兼容（400 且报错含 temperature）→ 去掉重试并记忆（_no_temp_models）
        - response_format json_object 不支持（400 且报错含 response_format）→ 去掉重试并记忆
          （JSON 契约由提示词+校验重试兜底）
        循环结构：每次重试后重新提取报错文本，兼容两种特性都不支持的链式 400"""
        cfg = self.available.get(provider)  # 热重载判空
        if not cfg:
            return None
        eff_model = payload.get("model") or cfg["model"]
        if eff_model in self._no_temp_models:
            payload.pop("temperature", None)
        if eff_model in self._no_json_mode_models:
            payload.pop("response_format", None)
        response = None
        for _ in range(3):
            response = requests.post(cfg["base_url"], headers=headers, json=payload,
                                     timeout=req_timeout, proxies={"http": None, "https": None})
            if response.status_code != 400:
                break
            try:
                msg = (response.json().get("error") or {}).get("message", "").lower()
            except Exception:
                msg = ""
            if "temperature" in payload and "temperature" in msg:
                logger.warning(f"L1[{provider}] temperature 不兼容（{eff_model}），已记忆免传")
                self._no_temp_models.add(eff_model)
                payload.pop("temperature", None)
                continue
            if "response_format" in payload and "response_format" in msg:
                logger.warning(f"L1[{provider}] response_format 不支持（{eff_model}），已记忆去字段")
                self._no_json_mode_models.add(eff_model)
                payload.pop("response_format", None)
                continue
            break
        return response

    def _l1_collect_json(self, provider: str, question: str,
                         fn_tools: List[Dict[str, Any]],
                         fn_handlers: Dict[str, Any],
                         user_id: str = None,
                         cancel_event=None,
                         model: str = None):
        """
        L1 取数层（JSON mode）：response_format=json_object 强制模型只输出
        {"calls": [{"name": ..., "args": {...}}], "web_search": ["检索词", ...]}，
        calls 为空即取数结束。JSON 解析/校验失败引导重试一次，仍失败返回 (None, None)。
        """
        cfg = self.available.get(provider)  # 热重载判空
        if not cfg:
            return None, None
        headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
        req_timeout = (int(os.getenv("LLM_CONNECT_TIMEOUT", "10")),
                       int(os.getenv("LLM_L1_TIMEOUT", "60")))
        known_tools = {((t.get("function") or {}).get("name")) for t in fn_tools}
        system = (self._L1_SYSTEM
                  + "\n\n【可用工具清单】\n" + self._l1_tool_manifest(fn_tools)
                  + "\n\n【输出契约（强制）】只输出一个 JSON 对象，禁止输出任何其他文字：\n"
                    '{"calls": [{"name": "工具名", "args": {"参数名": 值}}], "web_search": ["检索词"]}\n'
                    "- 需要数据时把工具调用填入 calls（一轮可多个，将并行执行），args 按工具参数说明填写\n"
                    "- 需要最新新闻/背景时把检索词填入 web_search（无则空数组）\n"
                    "- 数据已足够或无需取数时输出 {\"calls\": [], \"web_search\": [...]} 结束\n"
                    "- 禁止调用清单外的工具名")
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": question}]
        collected = []
        search_queries = []
        parse_retried = False

        for round_num in range(8):
            if cancel_event is not None and cancel_event.is_set():
                logger.warning(f"L1取数[{provider}] 收到取消信号，第{round_num + 1}轮前停止")
                break
            payload = {"model": model or cfg["model"], "messages": messages,
                       "temperature": 0.1,
                       "response_format": {"type": "json_object"}}
            try:
                response = self._l1_post(provider, headers, payload, req_timeout)
            except requests.RequestException as e:
                logger.error(f"L1取数[{provider}]请求异常: {e}")
                self._set_error(f"{provider}: L1请求异常 {e}")
                return None, None
            if response.status_code != 200:
                logger.error(f"L1取数[{provider}]请求失败: HTTP {response.status_code} - {response.text[:200]}")
                if response.status_code in (402, 404, 429):
                    hint = {402: "余额不足", 404: "模型不存在或已下线", 429: "触发限流"}.get(response.status_code, "")
                    self._set_error(f"{provider}: HTTP {response.status_code} {hint}")
                return None, None
            try:
                data = response.json()
                content = (data["choices"][0]["message"].get("content") or "").strip()
            except (ValueError, KeyError, IndexError) as e:
                logger.error(f"L1取数[{provider}]响应解析失败: {e}")
                return None, None

            # 结构化校验：必须是 dict + calls/web_search 字段类型正确
            decision = None
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict):
                    calls = parsed.get("calls") or []
                    searches = parsed.get("web_search") or []
                    if (isinstance(calls, list) and isinstance(searches, list)
                            and all(isinstance(c, dict) and isinstance(c.get("name"), str)
                                    and isinstance(c.get("args") or {}, dict) for c in calls)
                            and all(isinstance(s, str) for s in searches)):
                        decision = (calls, searches)
            except (ValueError, TypeError):
                pass
            if decision is None:
                if not parse_retried:
                    parse_retried = True
                    logger.warning(f"L1取数[{provider}] 输出违反JSON契约，引导重试一次: {content[:80]}")
                    messages.append({"role": "assistant", "content": content})
                    messages.append({"role": "user", "content":
                        "输出不符合契约。只输出 JSON 对象："
                        '{"calls": [{"name": "工具名", "args": {...}}], "web_search": ["检索词"]}，'
                        "禁止输出任何其他文字。"})
                    continue
                logger.warning(f"L1取数[{provider}] 重试后仍违反JSON契约，判定失败: {content[:80]}")
                return None, None
            calls, searches = decision

            # 过滤清单外的幻觉工具名（记入日志但不执行）
            valid_calls = []
            for c in calls:
                cname = c["name"]
                if cname in known_tools and cname in fn_handlers:
                    valid_calls.append((cname, c.get("args") or {}))
                else:
                    logger.warning(f"L1取数[{provider}] 忽略清单外工具: {cname}")
            search_queries.extend(s.strip() for s in searches if s.strip())

            if not valid_calls:
                break  # 取数结束

            # 并行执行本轮工具（保序收集；LocalCache 有锁，线程安全）
            if len(valid_calls) > 1:
                from concurrent.futures import ThreadPoolExecutor as _TPE
                qid = get_qid()
                with _TPE(max_workers=min(4, len(valid_calls))) as pool:
                    results = list(pool.map(
                        lambda c: self._execute_tool(c[0], json.dumps(c[1], ensure_ascii=False),
                                                     fn_handlers, user_id=user_id, qid=qid),
                        valid_calls))
            else:
                results = [self._execute_tool(c[0], json.dumps(c[1], ensure_ascii=False),
                                              fn_handlers, user_id=user_id)
                           for c in valid_calls]
            feedback = []
            for (cname, _), result in zip(valid_calls, results):
                collected.append((cname, result))
                feedback.append(f"[{cname}] {result}")
            messages.append({"role": "assistant", "content": content})
            messages.append({"role": "user", "content":
                "工具执行结果：\n" + "\n".join(feedback)
                + "\n\n数据足够则输出 {\"calls\": [], \"web_search\": [...]} 结束；"
                  "还需更多数据则继续输出 calls。"})
            logger.debug(f"L1取数[{provider}]JSON模式第{round_num + 1}轮: {len(valid_calls)} 个工具")

        logger.info(f"L1取数完成(JSON模式): {len(collected)} 个数据工具, {len(search_queries)} 条联网需求")
        return collected, search_queries

    def _l1_collect_native(self, provider: str, question: str,
                    fn_tools: List[Dict[str, Any]],
                    fn_handlers: Dict[str, Any],
                    user_id: str = None,
                    cancel_event=None,
                    model: str = None):
        """
        L1 取数层（原生 function calling 路径，LLM_L1_JSON_MODE=false 时使用）
        同轮多个工具调用并行执行（LocalCache 有锁，线程安全），显著缩短取数耗时
        Returns:
            (collected: [(tool_name, result_str)], search_queries: [str])，失败返回 (None, None)
        """
        cfg = self.available.get(provider)  # 热重载判空
        if not cfg:
            return None, None
        headers = {"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"}
        # L1 是轻量决策层（不联网、输出短），读超时收紧到 LLM_L1_TIMEOUT（默认60s）
        req_timeout = (int(os.getenv("LLM_CONNECT_TIMEOUT", "10")),
                       int(os.getenv("LLM_L1_TIMEOUT", "60")))
        tools = list(fn_tools) + [self._WEB_SEARCH_PSEUDO_TOOL]
        messages = [{"role": "system", "content": self._L1_SYSTEM},
                    {"role": "user", "content": question}]
        collected = []
        search_queries = []

        for round_num in range(8):
            if cancel_event is not None and cancel_event.is_set():
                logger.warning(f"L1取数[{provider}] 收到取消信号，第{round_num + 1}轮前停止")
                break
            payload = {"model": model or cfg["model"], "messages": messages,
                       "temperature": 0.1, "tools": tools}
            try:
                response = self._l1_post(provider, headers, payload, req_timeout)
            except requests.RequestException as e:
                logger.error(f"L1取数[{provider}]请求异常: {e}")
                self._set_error(f"{provider}: L1请求异常 {e}")
                return None, None
            if response.status_code != 200:
                logger.error(f"L1取数[{provider}]请求失败: HTTP {response.status_code} - {response.text[:200]}")
                if response.status_code in (402, 404, 429):
                    hint = {402: "余额不足", 404: "模型不存在或已下线", 429: "触发限流"}.get(response.status_code, "")
                    self._set_error(f"{provider}: HTTP {response.status_code} {hint}")
                return None, None
            try:
                data = response.json()
                choice = data["choices"][0]
                message = choice["message"]
            except (ValueError, KeyError, IndexError) as e:
                logger.error(f"L1取数[{provider}]响应解析失败: {e}")
                return None, None

            tool_calls = message.get("tool_calls")
            # 仅当没有工具调用时才结束；部分兼容端点会同时返回 finish_reason=stop 和 tool_calls
            # （OSS review 修复回灌：拆分时误把 finish_reason==stop 加进了 break 条件，
            #  此类端点的工具调用会被静默丢弃）
            if not tool_calls:
                break
            messages.append(message)
            # 同轮多个数据工具并行执行（保序回填 tool 消息，不改变消息序列语义）；
            # web_search 伪工具只记录需求，仍按原顺序串行处理
            data_calls = []
            for tc in tool_calls:
                fn = tc.get("function", {}) or {}
                tc_name = fn.get("name", "")
                if tc_name == "web_search":
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except (ValueError, TypeError):
                        args = {}
                    q = (args.get("query") or "").strip()
                    if q:
                        search_queries.append(q)
                    messages.append({"role": "tool", "tool_call_id": tc["id"],
                                     "name": tc_name, "content": "联网需求已记录，将在成文阶段执行"})
                else:
                    data_calls.append((tc, tc_name, fn.get("arguments")))
            if len(data_calls) > 1:
                from concurrent.futures import ThreadPoolExecutor as _TPE
                qid = get_qid()  # 子线程不继承 thread-local，显式传递
                with _TPE(max_workers=min(4, len(data_calls))) as pool:
                    results = list(pool.map(
                        lambda c: self._execute_tool(c[1], c[2], fn_handlers,
                                                     user_id=user_id, qid=qid),
                        data_calls))
            else:
                results = [self._execute_tool(c[1], c[2], fn_handlers, user_id=user_id)
                           for c in data_calls]
            for (tc, tc_name, _), result in zip(data_calls, results):
                collected.append((tc_name, result))
                messages.append({"role": "tool", "tool_call_id": tc["id"],
                                 "name": tc_name, "content": result})
            logger.debug(f"L1取数[{provider}]工具调用第{round_num + 1}轮")

        logger.info(f"L1取数完成: {len(collected)} 个数据工具, {len(search_queries)} 条联网需求")
        return collected, search_queries

    def ask_with_tools(self, user_id: str, question: str,
                       fn_tools: List[Dict[str, Any]],
                       fn_handlers: Dict[str, Any],
                       max_len: int = 1800,
                       data_context: str = None,
                       meta_out: Dict[str, Any] = None,
                       cancel_event=None,
                       group_openid: str = None,
                       qid: str = None,
                       skip_providers: set = None) -> Optional[str]:
        """单调用模式统一入口（含 trace ID 生命周期管理），实现见 _ask_with_tools_impl

        skip_providers: 本轮跳过的供应商（分层路径某供应商刚失败时传入，
                        避免兜底路径让同一供应商再白跑一轮——2026-09-15 事故中浪费134s）"""
        prev = get_qid()
        set_qid(qid or prev or new_qid())
        try:
            return self._ask_with_tools_impl(user_id, question, fn_tools, fn_handlers,
                                             max_len, data_context, meta_out, cancel_event,
                                             group_openid, skip_providers)
        finally:
            self._sc_notices.pop(get_qid(), None)
            set_qid(prev)

    def _ask_with_tools_impl(self, user_id: str, question: str,
                       fn_tools: List[Dict[str, Any]],
                       fn_handlers: Dict[str, Any],
                       max_len: int = 1800,
                       data_context: str = None,
                       meta_out: Dict[str, Any] = None,
                       cancel_event=None,
                       group_openid: str = None,
                       skip_providers: set = None) -> Optional[str]:
        """
        带工具调用 + 上下文记忆的多轮问答：AI可主动调用 F1 数据工具（积分榜/成绩/PU/升级件等），
        再结合联网搜索生成内容。按用户区分会话记忆。

        Args:
            user_id: 用户 openid
            question: 当前问题（可含引用内容）
            fn_tools: OpenAI 风格的 function tool schema 列表
            fn_handlers: {tool_name: callable(**args) -> dict/str}
            max_len: 回答最大长度
            data_context: 当前最新数据快照（注入系统提示）

        Returns:
            回答文本，全部失败返回 None
        """
        if not self.enabled:
            return None

        system = self.SYSTEM_PROMPT + self.name_glossary() + self.rules_anchor() + (
            "\n\n你可以调用提供的 F1 数据工具获取实时准确数据（积分榜、比赛成绩、动力单元用量、升级件等）。"
            "涉及现行数据/统计/预测时，先调用工具获取数据，再结合作答；需要最新新闻/背景时可联网搜索。"
        )
        if data_context:
            system += ("\n\n【当前最新F1数据（机器人实时获取，优先以此为准）】\n" + data_context)
        system = self._inject_group_vision(system, group_openid, user_id, question)
        # 群知识库 + 提问者画像注入（与分层路径一致）
        try:
            from ..knowledge_store import KnowledgeStore
            _ks = KnowledgeStore.get()
            system += _ks.knowledge_block() + _ks.habit_block(user_id)
        except Exception:
            pass

        mem_ctx = self._get_memory_context(user_id, question)
        if mem_ctx.get("memory_block"):
            system += "\n\n" + mem_ctx["memory_block"]
        history = mem_ctx["history"]
        messages = ([{"role": "system", "content": system}]
                    + history
                    + [{"role": "user", "content": question}])

        tools_log: list = []
        for provider in self.ask_order:
            if skip_providers and provider in skip_providers:
                logger.info(f"跳过刚失败的供应商[{provider}]，直接尝试下一个")
                continue
            if cancel_event is not None and cancel_event.is_set():
                logger.warning(f"ask_with_tools 收到取消信号，跳过[{provider}]及后续供应商")
                return None
            # 无内建联网的供应商（DeepSeek）：若配置了客户端搜索（Tavily/博查），
            # 注入普通 function 形态的 web_search 工具，由客户端执行搜索回填
            p_tools, p_handlers = fn_tools, fn_handlers
            if not (self.available.get(provider) or {}).get("web_search", False):  # 热重载判空
                from ..web_search import WEB_SEARCH_TOOL, web_search_handler, get_web_searcher
                if get_web_searcher().enabled and fn_tools is not None:
                    if not any((t.get("function") or {}).get("name") == "web_search" for t in fn_tools):
                        p_tools = list(fn_tools) + [WEB_SEARCH_TOOL]
                        p_handlers = dict(fn_handlers or {})
                        p_handlers["web_search"] = web_search_handler
            answer = self._chat_once(provider, messages,
                                     fn_tools=p_tools, fn_handlers=p_handlers,
                                     audit_user=user_id, tools_log=tools_log,
                                     cancel_event=cancel_event)
            if answer:
                if provider != self.ask_order[0]:
                    logger.info(f"LLM问答由兜底供应商[{provider}]完成")
                return self._finalize_answer(user_id, question, answer, max_len,
                                             meta_out, tools_log, provider)
            logger.warning(f"LLM[{provider}]无响应，尝试下一个供应商")

        logger.error("所有LLM供应商均无响应")
        return None

    def ask(self, question: str, max_len: int = 1800) -> Optional[str]:
        """
        F1知识问答：按供应商顺序尝试（默认DeepSeek优先，Kimi联网兜底）

        Args:
            question: 用户问题
            max_len: 回答最大长度（超出截断）

        Returns:
            回答文本，全部失败返回None
        """
        if not self.enabled:
            return None

        messages = [
            {"role": "system", "content": self.SYSTEM_PROMPT},
            {"role": "user", "content": question}
        ]

        for provider in self.ask_order:
            answer = self._chat_once(provider, messages)
            if answer:
                if provider != self.ask_order[0]:
                    logger.info(f"LLM问答由兜底供应商[{provider}]完成")
                if len(answer) > max_len:
                    answer = answer[:max_len] + "…"
                return answer
            logger.warning(f"LLM[{provider}]无响应，尝试下一个供应商")

        logger.error("所有LLM供应商均无响应")
        return None

    def ask_with_context(self, user_id: str, question: str,
                         max_len: int = 1800,
                         data_context: str = None) -> Optional[str]:
        """
        带上下文记忆的多轮问答（按用户区分）

        Args:
            user_id: 用户 openid（跨群/私聊统一）
            question: 当前问题
            max_len: 回答最大长度
            data_context: 当前最新F1数据快照（积分榜/赛程/成绩等），注入系统提示供AI引用

        Returns:
            回答文本，全部失败返回 None
        """
        if not self.enabled:
            return None

        system = self.SYSTEM_PROMPT
        if data_context:
            system += (
                "\n\n【当前最新F1数据（机器人实时获取，回答现行数据问题时以此为准，优先于联网搜索）】\n"
                + data_context
            )

        mem_ctx = self._get_memory_context(user_id, question)
        if mem_ctx.get("memory_block"):
            system += "\n\n" + mem_ctx["memory_block"]
        history = mem_ctx["history"]
        messages = ([{"role": "system", "content": system}]
                    + history
                    + [{"role": "user", "content": question}])

        for provider in self.ask_order:
            answer = self._chat_once(provider, messages)
            if answer:
                if provider != self.ask_order[0]:
                    logger.info(f"LLM问答由兜底供应商[{provider}]完成")
                if len(answer) > max_len:
                    answer = answer[:max_len] + "…"
                self._append_turn(user_id, question, answer)
                return answer
            logger.warning(f"LLM[{provider}]无响应，尝试下一个供应商")

        logger.error("所有LLM供应商均无响应")
        return None
