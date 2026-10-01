"""
F1赛程提醒机器人 - 联网LLM助手（多供应商版）

供应商：
- Kimi/Moonshot（默认首选）：内置$web_search联网搜索，F1知识问答+必须联网的数据核查（赛道圈速纪录）
- DeepSeek（备用，计费便宜）：Kimi 无响应时兜底，无联网能力；分层问答中承担 L0 改写/L1 取数

配置（环境变量）：
- DEEPSEEK_API_KEY: DeepSeek API密钥
- MOONSHOT_API_KEY: Kimi API密钥
- LLM_PROVIDER: 问答优先供应商，kimi/deepseek，默认kimi（未配置时自动跳过无key的供应商）
- DEEPSEEK_MODEL: 默认deepseek-chat
- LLM_MODEL: Kimi模型，默认kimi-k2.6（moonshot-v1系列已于2026-09被平台下线）
- TAVILY_API_KEY / BOCHA_API_KEY（可选）: 客户端联网搜索，为 DeepSeek 等无内建联网的供应商
  补充 web_search 能力（Kimi 走服务端内建搜索，无需此项）
"""

import json
import logging
import os
import re
from typing import Optional, Dict, Any, List

import requests

from .drivers_profile import get_driver_en_map, get_team_en_map

logger = logging.getLogger(__name__)

# ==================== 全链路 trace ID ====================
# 一次问答生成一个 qid，经 thread-local 贯穿 L0/L1/L2 全部日志与工具审计。
# QIDLogFilter 会给该线程内所有日志记录（含 f1_api 等下游模块）附加 qid 字段，
# 排障时 grep "qid=xxxxxx" 即可捞出完整链路，无需肉眼翻找。
import secrets as _secrets
import threading as _threading

_QID_LOCAL = _threading.local()


def new_qid() -> str:
    """生成短随机问答链路 ID（6位hex）"""
    return _secrets.token_hex(3)


def set_qid(qid):
    _QID_LOCAL.qid = qid


def get_qid():
    return getattr(_QID_LOCAL, "qid", None)


class QIDLogFilter(logging.Filter):
    """给日志记录注入 qid 字段（无上下文时为 '-'），配合日志格式中的 %(qid)s 使用"""
    def filter(self, record):
        record.qid = get_qid() or "-"
        return True

# ==================== 提示词外置（common/prompts/*.txt） ====================
# 优先从文件加载，缺失/读取失败回退内嵌默认值，调提示词无需改代码

_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")


def _load_prompt(name: str, default: str) -> str:
    """从 common/prompts/<name>.txt 加载提示词，失败回退内嵌默认值"""
    try:
        path = os.path.join(_PROMPTS_DIR, name + ".txt")
        with open(path, "r", encoding="utf-8") as f:
            text = f.read().strip()
        if text:
            return text
        logger.warning(f"提示词文件为空，使用内嵌默认: {name}")
    except FileNotFoundError:
        logger.warning(f"提示词文件缺失，使用内嵌默认: {name}")
    except Exception as e:
        logger.warning(f"提示词文件加载失败，使用内嵌默认: {name} ({e})")
    return default


PROVIDERS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1/chat/completions",
        "key_env": "DEEPSEEK_API_KEY",
        "model_env": "DEEPSEEK_MODEL",
        "default_model": "deepseek-chat",
        "web_search": False,
    },
    "kimi": {
        "base_url": "https://api.moonshot.cn/v1/chat/completions",
        "key_env": "MOONSHOT_API_KEY",
        "model_env": "LLM_MODEL",
        # moonshot-v1-auto 已下线（404）。kimi-k2.6 为账户实测可用且支持 $web_search 的默认模型
        "default_model": "kimi-k2.6",
        "web_search": True,
    },
}

# 问答默认顺序：Kimi联网优先（F1知识需核实），DeepSeek兜底（可用 LLM_PROVIDER 环境变量覆盖）
DEFAULT_ASK_ORDER = ["kimi", "deepseek"]

# 翻译默认顺序：DeepSeek快速便宜优先（翻译无需联网搜索），Kimi兜底
TRANSLATE_ORDER = ["deepseek", "kimi"]


class LLMAssistant:
    """多供应商LLM助手（Kimi联网优先问答 + DeepSeek兜底）"""

    _DEFAULT_SYSTEM_PROMPT = (
        "你是一位F1（世界一级方程式锦标赛）专家，精通F1技术、运动规则、历史规则变迁和历届赛季。"
        "你的核心价值是解答复杂技术规则、动力单元、空气动力学、赛事策略、冷门赛事知识等"
        "车迷日常渠道不常覆盖的深度内容。\n"
        "回答用户关于F1的问题时：\n"
        "1. 涉及具体数据、纪录、规则条文时，如具备联网搜索能力务必使用联网核实，不要凭记忆回答；"
        "禁止声称“无法联网/无法调取实时数据”——需要联网时搜索会自动执行\n"
        "2. 使用简体中文回答，语气专业但易懂\n"
        "3. 简明问题简明作答、重点突出；若涉及详细分析/预测/多数据对比，可给出结构化、较完整的回答\n"
        "4. 关键数据（纪录、年份、数值）注明信息来源\n"
        "5. 工具数据与记忆/联网信息冲突时，一律以工具数据为准；"
        "没有数据支撑时明确回答“暂时没有该数据”，禁止编造名次、积分、日期、人名\n"
        "5b. 机器人具备逐圈 pace/轮胎衰减数据能力（get_stint_analysis，OpenF1 2023+）与车手近4场状态/"
        "车队 pace 能力（get_race_prediction 的 feature_breakdown 含 form_quali/form_finish、"
        "get_team_strengths 含 pace_recent_avg）。禁止声称“没有 pace/stint/遥测数据”，也禁止把工具已返回的"
        "真实圈速/衰减数字说成“误用了无法验证的信息”；被追问车手近期状态/节奏/竞争力时，先调用工具取数再作答\n"
        "6. 数据工具支持历史赛季（1950年至今）：问历史赛季成绩/积分榜时工具必须传year参数，"
        "禁止把当前赛季数据标注为其他年份\n"
        "7. 如果问题与F1无关，礼貌地引导回F1话题\n"
        "8. 输出格式适配QQ群聊Markdown卡片（按空行/标题分条发送）：用##/###标题划分板块、板块间空行分隔；"
        "避免宽表格；领奖台用「🥇 车手（车队）— 理由」单列，P4-P10逐行；"
        "逐环节天气/成绩用紧凑单行（如「周五FP1 18:30：晴 29-32°C，降水0%」）"
    )
    SYSTEM_PROMPT = _load_prompt("f1_expert", _DEFAULT_SYSTEM_PROMPT)

    def __init__(self, timeout: int = 120, max_search_rounds: int = 5):
        """
        初始化LLM助手，自动检测已配置key的供应商

        Args:
            timeout: 单次请求超时（秒），联网搜索可能较慢
            max_search_rounds: 联网搜索最大轮次
        """
        self.timeout = timeout
        self.max_search_rounds = max_search_rounds
        # 最近一次供应商级故障（402余额/404模型下线/429限流），供上层区分"查询失败"与"服务配置异常"
        # 注意：并发问答下实例级 last_error 会串用户，正式读取走 qid 键控 get_error(qid)
        self.last_error: Optional[str] = None
        self._error_by_qid: Dict[str, str] = {}
        self._error_lock = _threading.Lock()
        # 不接受显式 temperature 的模型集合（kimi-k2.x/k3 强制 temperature=1，传值即 400）；
        # 首次 400 重试时记入，之后直接免传，省掉每次请求的浪费往返（2026-09-15 事故中浪费约40s）
        self._no_temp_models: set = set()
        # get_race_prediction 的 sc_warning（高安全车赛道警示）暂存 {qid: 警示句}：
        # _execute_tool 命中时登记，_finalize_answer 确定性追加（实证 2026-09-23：
        # prediction.txt 规则 3b 要求"原样引用 sc_warning"被 L2/兜底模型三次忽略，
        # LLM 遵守提示词不可靠，改代码兜底保证高 SC 声明必达）
        self._sc_notices: Dict[str, str] = {}

        # 检测可用供应商
        self.available: Dict[str, Dict[str, Any]] = {}
        for name, cfg in PROVIDERS.items():
            api_key = os.getenv(cfg["key_env"], "")
            if api_key:
                self.available[name] = {
                    "api_key": api_key,
                    "base_url": cfg["base_url"],
                    "model": os.getenv(cfg["model_env"], "") or cfg["default_model"],
                    "web_search": cfg["web_search"],
                }
                logger.info(f"✓ LLM供应商可用: {name} (模型: {self.available[name]['model']}, 联网: {cfg['web_search']})")

        # 问答供应商顺序
        preferred = os.getenv("LLM_PROVIDER", "").strip().lower()
        order = list(DEFAULT_ASK_ORDER)
        if preferred in PROVIDERS:
            order.remove(preferred)
            order.insert(0, preferred)
        self.ask_order = [n for n in order if n in self.available]

        self.enabled = bool(self.available)
        if self.enabled:
            logger.info(f"✓ LLM助手初始化成功，问答顺序: {' -> '.join(self.ask_order) or '无'}")
            if "kimi" not in self.available:
                logger.warning("⚠️ 未配置MOONSHOT_API_KEY，联网核查功能（圈速纪录）已禁用")
        else:
            logger.warning("⚠️ 未配置任何LLM API Key，LLM功能已禁用")

        # 多轮会话记忆：JSON持久化（data/llm_memory.json），容器重启不丢
        # 窗口语义不变：每用户最近6轮、30分钟无交互过期
        import threading
        from .llm_memory_store import LLMMemoryStore
        self._memory = LLMMemoryStore()
        self._conv_lock = threading.Lock()
        # 群内最近识图摘要：{group_openid: {user_id, question, answer, ts}}
        self._group_vision: Dict[str, Dict[str, Any]] = {}

    @staticmethod
    def _strip_tool_markup(text: str) -> str:
        """剥除误混进正文的文本态函数调用标记。
        背景：Kimi k2.x 走 $web_search 内建搜索时，偶发把 <function_calls><invoke…>
        XML 当正文 content 输出（非 API 结构化 tool_calls），2026-09-15 实战捕获
        该标记原样发给了用户。只剥标记块，不动正常文本。"""
        if not text or "<" not in text:
            return text or ""
        t = re.sub(r"<function_calls>.*?</function_calls>", "", text, flags=re.S)
        t = re.sub(r"<invoke\b[^>]*>.*?</invoke>", "", t, flags=re.S)
        t = re.sub(r"</?(?:function_calls|invoke|parameter)\b[^>]*>", "", t)
        t = re.sub(r"<\|tool_[^|]*\|>", "", t)
        return t.strip()

    @staticmethod
    def _looks_degenerate(text: str) -> bool:
        """退化循环判定：模型陷入重复念叨（如连续多段"让我核实/让我组织回答"车轱辘话）。
        规则：某一行（≥15字）重复出现≥3次（任意长度，真实回答不会触发）；
        或全文重复行字符占比>35%（长文适用）。"""
        t = (text or "").strip()
        if len(t) < 150:
            return False
        lines = [ln.strip() for ln in t.split("\n") if len(ln.strip()) >= 15]
        if not lines:
            return False
        from collections import Counter
        counts = Counter(lines)
        top_line, top_n = counts.most_common(1)[0]
        if top_n >= 3:
            return True
        # 重复行字符占比 > 35% 视为退化（仅长文判定）
        if len(t) >= 400:
            dup_chars = sum(len(ln) * (n - 1) for ln, n in counts.items() if n > 1)
            if dup_chars / max(len(t), 1) > 0.35:
                return True
        return False

    @staticmethod
    def _looks_like_preamble(text: str) -> bool:
        """前言判定：模型"预告要做什么"但未给出实质内容的回复。
        命中需同时满足：长度<300 + 前言式开头/结尾 + 无答案结构（标题/列表/多行展开）。
        防误伤：简短正常回答（"蒙扎赛道在意大利。"）不以这些词开头；带结构短分析有 ##/列表。
        """
        t = (text or "").strip()
        if not t or len(t) >= 300:
            return False
        starters = ("我将", "我会", "让我", "我来", "请稍候", "稍等", "好的，我", "好的,我",
                    "接下来我", "现在我", "首先我将", "我先", "请允许我")
        tail_markers = ("请稍候", "请稍等", "稍候", "稍等", "…", "🔄")
        has_start = any(t.startswith(s) for s in starters)
        has_tail = any(t.endswith(m) or m in t[-20:] for m in tail_markers)
        if not (has_start or has_tail):
            return False
        # 有答案结构则不算前言
        if "##" in t or "\n-" in t or "\n•" in t or "\n🥇" in t or t.count("\n") >= 3:
            return False
        if "：\n" in t or ":\n" in t:
            return False
        return True

    def _chat_once(self, provider: str, messages: List[Dict[str, Any]],
                   temperature: float = 0.3, timeout: int = None,
                   fn_tools: List[Dict[str, Any]] = None,
                   fn_handlers: Dict[str, Any] = None,
                   enable_search: Optional[bool] = None,
                   model: str = None,
                   extra_payload: Dict[str, Any] = None,
                   audit_user: str = None,
                   tools_log: list = None,
                   cancel_event=None) -> Optional[str]:
        """
        调用指定供应商的聊天接口；自动处理工具循环
        - 联网内置工具（Kimi $web_search）：服务端执行，回传空 tool 消息即可
        - 自定义函数工具（fn_tools + fn_handlers）：客户端执行 handler 并回传结果

        Args:
            enable_search: 是否启用联网搜索，None=按供应商默认能力；分层调用时用于按需关闭
            model: 覆盖默认模型（如深度分析模式用推理模型）
            extra_payload: 附加请求字段（如 reasoning_effort）
            audit_user: 工具审计归属用户
            tools_log: 可选 list，实际被调用的工具名逐个追加（工具调用声明用）
            cancel_event: 可选 threading.Event，置位后在轮次边界停止后续请求，
                          尽力返回已收集的内容（超时任务不再无限占用并发槽）

        Returns:
            最终回答文本，失败返回None
        """
        cfg = self.available[provider]
        timeout = timeout or self.timeout
        # connect/read 拆分：连接挂死 10s 内快速失败，read 按层预算（避免 120s 才发现网络故障）
        req_timeout = (int(os.getenv("LLM_CONNECT_TIMEOUT", "10")), timeout)
        use_search = cfg["web_search"] if enable_search is None else (enable_search and cfg["web_search"])
        headers = {
            "Authorization": f"Bearer {cfg['api_key']}",
            "Content-Type": "application/json"
        }
        messages = list(messages)  # 拷贝，避免污染调用方

        has_fn_tools = bool(fn_tools)
        max_rounds = self.max_search_rounds if use_search else (8 if has_fn_tools else 1)
        # +2 轮余量：前言/空内容引导重试与截断续写各占一轮，不能让它们吃掉工具/搜索轮次预算
        #（无工具调用时 max_rounds=1，没有余量会导致重试机制永远到不了第二轮）
        loop_rounds = max_rounds + 2
        last_content = ""          # 最后一轮非空内容（防御前言被当最终答案）
        empty_stop_retried = False  # stop 但空内容时的引导重试标记
        length_retried = False      # finish_reason=length 截断时的续写重试标记
        length_prefix = ""          # 截断续写时的前半段（续写轮只输出后半段，最终需拼接）

        def _best_effort():
            """取消/轮次耗尽时尽力返回：已有内容且非前言/退化则发出"""
            return (last_content if (last_content
                                     and not self._looks_like_preamble(last_content)
                                     and not self._looks_degenerate(last_content))
                    else None)

        for round_num in range(loop_rounds):
            # 轮次边界取消检查：超时任务在下一个边界停止，不再发起新请求（释放并发槽）
            if cancel_event is not None and cancel_event.is_set():
                logger.warning(f"LLM[{provider}] 收到取消信号，第{round_num + 1}轮前停止")
                return _best_effort()
            eff_model = model or cfg["model"]
            payload = {
                "model": eff_model,
                "messages": messages,
                "temperature": temperature,
            }
            if eff_model in self._no_temp_models:
                payload.pop("temperature", None)  # 已知不兼容的模型直接免传，省一次400往返
            if extra_payload:
                payload.update(extra_payload)
            tool_list = []
            if use_search:
                tool_list.append({
                    "type": "builtin_function",
                    "function": {"name": "$web_search"}
                })
            if fn_tools:
                tool_list.extend(fn_tools)
            if tool_list:
                payload["tools"] = tool_list

            try:
                response = requests.post(
                    cfg["base_url"], headers=headers, json=payload,
                    timeout=req_timeout,
                    proxies={"http": None, "https": None}
                )
                # kimi-k2.x/k3部分模型强制temperature=1，显式传值会400，去掉后重试一次；
                # 并记入不兼容集合，后续请求直接免传（省掉每次的浪费往返）
                if response.status_code == 400 and "temperature" in response.text:
                    logger.warning(f"LLM[{provider}] temperature不被接受，移除后重试（已记忆 {eff_model}）")
                    self._no_temp_models.add(eff_model)
                    payload = {k: v for k, v in payload.items() if k != "temperature"}
                    response = requests.post(
                        cfg["base_url"], headers=headers, json=payload,
                        timeout=req_timeout,
                        proxies={"http": None, "https": None}
                    )
                # 429 限流（如 Moonshot 组织级 RPM=3）：等 3 秒重试一次
                if response.status_code == 429 and not (
                        cancel_event is not None and cancel_event.is_set()):
                    import time as _t
                    logger.warning(f"LLM[{provider}] 触发限流(429)，3秒后重试一次")
                    _t.sleep(3)
                    response = requests.post(
                        cfg["base_url"], headers=headers, json=payload,
                        timeout=req_timeout,
                        proxies={"http": None, "https": None}
                    )
            except requests.RequestException as e:
                logger.error(f"LLM[{provider}]请求异常: {e}")
                self._set_error(f"{provider}: 请求异常 {e}")
                return None

            if response.status_code != 200:
                logger.error(f"LLM[{provider}]请求失败: HTTP {response.status_code} - {response.text[:300]}")
                # 供应商级故障（402余额不足/404模型下线/429限流）记录下来供上层友好提示
                if response.status_code in (402, 404, 429):
                    hint = {402: "余额不足", 404: "模型不存在或已下线", 429: "触发限流"}.get(response.status_code, "")
                    self._set_error(f"{provider}: HTTP {response.status_code} {hint}")
                return None

            try:
                data = response.json()
                choice = data["choices"][0]
                message = choice["message"]
            except (ValueError, KeyError, IndexError) as e:
                logger.error(f"LLM[{provider}]响应解析失败: {e} - {response.text[:300]}")
                return None

            content = message.get("content") or ""
            # 文本态函数调用标记泄漏检测：剥除后再走前言/退化判定（剥后可能只剩前言）
            if "<function_calls>" in content or "<invoke " in content or "<|tool_" in content:
                logger.warning(f"LLM[{provider}] 检测到文本态函数调用标记泄漏，已剥离 (qid={get_qid()})")
                content = self._strip_tool_markup(content)
            if content.strip():
                last_content = content
            tool_calls = message.get("tool_calls")
            finish = choice.get("finish_reason")

            if tool_calls:
                # 含工具调用一律执行并继续（不看 finish_reason：
                # 模型可能前言+搜索调用同轮返回，若提前 return 前言即被当最终答案）
                # 同轮多个函数工具并行执行（2026-09-15 事故：兜底路径8站预测串行耗时176s）
                messages.append(message)
                fn_jobs = []   # (序号, tc, 工具名, arguments)
                results = {}   # 序号 -> (tc, 工具名, 结果)
                for idx, tc in enumerate(tool_calls):
                    fn = tc.get("function", {}) or {}
                    tc_name = fn.get("name", "")
                    tc_type = tc.get("type") or ("builtin_function" if tc_name.startswith("$") else "function")
                    if tools_log is not None:
                        tools_log.append(tc_name)  # 工具调用声明（含内建 $web_search）
                    if tc_type == "builtin_function":
                        # 联网内置工具由服务端执行，回传空 tool 消息即可
                        results[idx] = (tc, tc_name, "")
                    else:
                        fn_jobs.append((idx, tc, tc_name, fn.get("arguments")))
                if len(fn_jobs) > 1:
                    from concurrent.futures import ThreadPoolExecutor as _TPE
                    qid = get_qid()  # 子线程不继承 thread-local，显式传递
                    with _TPE(max_workers=min(4, len(fn_jobs))) as pool:
                        outs = list(pool.map(
                            lambda j: self._execute_tool(j[2], j[3], fn_handlers or {},
                                                         user_id=audit_user, qid=qid),
                            fn_jobs))
                else:
                    outs = [self._execute_tool(j[2], j[3], fn_handlers or {}, user_id=audit_user)
                            for j in fn_jobs]
                for (idx, tc, tc_name, _), out in zip(fn_jobs, outs):
                    results[idx] = (tc, tc_name, out)
                # 按原始 tool_calls 顺序回填，保持消息序列语义不变
                for idx in sorted(results):
                    tc, tc_name, out = results[idx]
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tc_name,
                        "content": out
                    })
                logger.debug(f"LLM[{provider}]工具调用第{round_num + 1}轮")
                continue

            # finish_reason=="length"：输出被 max_tokens 截断，引导续写一次；
            # 续写后仍截断则发出已有内容并明确标注，避免"看起来完整实则缺尾"的回答
            if finish == "length":
                truncated_text = content or ("" if length_prefix else last_content)
                if not truncated_text.strip() and not length_prefix:
                    logger.warning(f"LLM[{provider}] length截断且内容为空，判定失败")
                    return None
                if not length_retried:
                    length_retried = True
                    length_prefix = truncated_text  # 保留前半段，续写轮只回后半段
                    logger.warning(f"LLM[{provider}] 输出被截断(finish=length)，引导续写一次")
                    messages.append(message)
                    messages.append({"role": "user",
                                     "content": "回答被长度限制截断了，请从截断处直接继续输出剩余内容，"
                                                "不要重复已说过的部分。"})
                    continue
                logger.warning(f"LLM[{provider}] 续写后仍被截断，发出已有内容并标注")
                combined = (length_prefix + "\n" + truncated_text).strip()
                if self._looks_like_preamble(combined) or self._looks_degenerate(combined):
                    logger.warning(f"LLM[{provider}] 截断拼接结果仍为前言/退化，判定失败")
                    return None
                return combined + "\n\n（注：回答因长度限制可能被截断，可回复\"继续\"获取剩余内容）"

            # stop 但内容为空或仅为前言（搜索后未产出正文）：引导模型直接给分析，重试一次
            if finish == "stop" and (not content.strip() or self._looks_like_preamble(content)):
                if not empty_stop_retried:
                    empty_stop_retried = True
                    if self._looks_like_preamble(content):
                        logger.warning(f"LLM[{provider}] 拒绝前言充当答案: {content[:50]}...")
                    else:
                        logger.warning(f"LLM[{provider}] stop但内容为空，引导重试一次")
                    messages.append(message)
                    messages.append({"role": "user",
                                     "content": "请基于以上信息直接给出完整分析，不要再描述你将要做什么；"
                                                "禁止输出任何 XML/函数调用标记。"})
                    continue
                if self._looks_like_preamble(content):
                    logger.warning(f"LLM[{provider}] 重试后仍为前言，判定失败: {content[:50]}...")
                    return None
                logger.warning(f"LLM[{provider}] 连续空响应，回退最后一轮非空内容")
                return (last_content if last_content and not self._looks_like_preamble(last_content)
                        else None)

            usage = data.get("usage", {})
            if usage:
                logger.debug(f"LLM[{provider}] tokens: {usage}")
            # 截断续写轮：拼接前半段+续写段作为完整答案
            if length_prefix and content.strip():
                content = (length_prefix + "\n" + content).strip()
                last_content = content
                length_prefix = ""
            # 最终答案仍判定为前言/退化循环时不发出（前言+尾注或车轱辘话刷屏会误导用户）
            bad_final = (self._looks_like_preamble(content)
                         or self._looks_degenerate(content)
                         or (not content.strip() and self._looks_degenerate(last_content)))
            if bad_final:
                if not empty_stop_retried:
                    empty_stop_retried = True
                    reason = ("前言" if self._looks_like_preamble(content) else "退化循环")
                    logger.warning(f"LLM[{provider}] 拒绝{reason}充当最终答案: {content[:50]}...")
                    messages.append(message)
                    messages.append({"role": "user",
                                     "content": "请直接给出简洁的最终回答（800字以内），"
                                                "不要重复核实、不要描述你将要做什么；"
                                                "禁止输出任何 XML/函数调用标记。"})
                    continue
                logger.warning(f"LLM[{provider}] 重试后仍为前言/退化，判定失败")
                return None
            return content or (last_content if last_content
                               and not self._looks_like_preamble(last_content)
                               and not self._looks_degenerate(last_content)
                               else None)

        logger.warning(f"LLM[{provider}]联网搜索超过最大轮次")
        # 轮次耗尽：若截断续写的前半段尚未拼回（后续轮带工具调用导致未走合并路径），在此补拼
        final = last_content
        if length_prefix:
            if not final:
                final = length_prefix
            elif not final.startswith(length_prefix):
                final = (length_prefix + "\n" + final).strip()
        return final if (final
                         and not self._looks_like_preamble(final)
                         and not self._looks_degenerate(final)) else None

    def _set_error(self, msg: str):
        """记录供应商级故障：实例级兼容字段 + qid 键控（并发问答不串用户）"""
        self.last_error = msg
        qid = get_qid()
        if qid:
            with self._error_lock:
                if len(self._error_by_qid) > 200:
                    self._error_by_qid.clear()
                self._error_by_qid[qid] = msg

    def get_error(self, qid: str = None) -> Optional[str]:
        """按 qid 取出并清除本次问答的供应商故障；无 qid 时回退实例级字段"""
        if qid:
            with self._error_lock:
                err = self._error_by_qid.pop(qid, None)
            if err:
                return err
        return self.last_error

    def _execute_tool(self, name: str, arguments_json, handlers: Dict[str, Any],
                      user_id: str = None, qid: str = None) -> str:
        """执行自定义函数工具，返回 JSON 字符串结果；每次调用写入审计日志"""
        import time as _t
        from .llm_audit import LLMToolAudit
        qid = qid or get_qid()  # 并行线程中 thread-local 不继承，由调用方显式传入
        audit = LLMToolAudit.get()
        t0 = _t.time()
        try:
            args = json.loads(arguments_json) if arguments_json else {}
        except (ValueError, TypeError):
            args = {}
        handler = handlers.get(name)
        if not handler:
            audit.record(name, args, status="error", error=f"unknown tool {name}",
                         user_id=user_id, qid=qid)
            return json.dumps({"error": f"unknown tool {name}"}, ensure_ascii=False)
        try:
            result = handler(**args)
            # 高 SC 赛道警示登记（_finalize_answer 确定性追加用，见 __init__ 注释）
            if name == "get_race_prediction" and isinstance(result, dict) and qid:
                _warn = result.get("sc_warning")
                if _warn:
                    self._sc_notices[qid] = _warn
            if isinstance(result, str):
                out = result
            else:
                out = json.dumps(result, ensure_ascii=False)
            # 工具返回业务错误（{"error": ...}）也计入失败，便于追溯
            biz_error = result.get("error") if isinstance(result, dict) else None
            audit.record(name, args, duration_ms=int((_t.time() - t0) * 1000),
                         status="error" if biz_error else "success",
                         error=biz_error, user_id=user_id, qid=qid)
            return out
        except Exception as e:
            logger.warning(f"工具[{name}]执行失败: {e}")
            audit.record(name, args, duration_ms=int((_t.time() - t0) * 1000),
                         status="error", error=str(e), user_id=user_id, qid=qid)
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    def _finalize_answer(self, user_id: str, question: str, answer: str,
                         max_len: int, meta_out: Dict[str, Any],
                         tools_log: list, provider: str) -> str:
        """统一收尾（分层/单调用两路径共用，防行为漂移）：截断 + 写会话记忆 + meta_out 回填
        + 问答归档（知识沉淀原料，失败不阻塞）"""
        if len(answer) > max_len:
            answer = answer[:max_len] + "…"
        # 高 SC 赛道警示确定性兜底：模型未自行声明（答案不含"安全车高发"）时强制追加
        notice = self._sc_notices.pop(get_qid(), None)
        if notice and "安全车高发" not in answer:
            answer = answer.rstrip() + "\n\n" + notice
        self._append_turn(user_id, question, answer)
        if meta_out is not None:
            meta_out["tools"] = tools_log
            meta_out["web_search"] = ("$web_search" in tools_log) or ("web_search" in tools_log)
            meta_out["provider"] = provider
        try:
            from .knowledge_store import KnowledgeStore
            if not (user_id or "").startswith("kb-"):  # 排除知识库校对等内部伪用户
                KnowledgeStore.get().archive_qa(get_qid() or "", user_id, question, answer,
                                                tools=tools_log, provider=provider)
        except Exception:
            pass
        return answer

    def _inject_group_vision(self, system: str, group_openid: str,
                             user_id: str, question: str) -> str:
        """群内识图摘要注入（分层/单调用两路径共用）"""
        if not group_openid:
            return system
        group_vision = self.get_group_vision_context(group_openid, user_id, question)
        if group_vision:
            system += "\n\n" + group_vision
            logger.info(f"注入群内识图摘要 (群 {group_openid[:8]}..., 用户 {user_id[:8]}...)")
        return system

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
            from .knowledge_store import KnowledgeStore
            _ks = KnowledgeStore.get()
            system += _ks.knowledge_block() + _ks.habit_block(user_id)
        except Exception:
            pass

        history = self._get_history(user_id)
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
            if not self.available[provider]["web_search"]:
                from .web_search import WEB_SEARCH_TOOL, web_search_handler, get_web_searcher
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

    # ==================== 多轮上下文记忆（按用户区分，JSON持久化） ====================

    # 窗口语义常量保留在本类便于外部引用，实际实现见 llm_memory_store
    from .llm_memory_store import MAX_TURNS, SESSION_TTL

    def _get_history(self, user_id: str) -> List[Dict[str, str]]:
        """取该用户的会话历史（过期自动清空）"""
        return self._memory.get_turns(user_id)

    def _append_turn(self, user_id: str, question: str, answer: str):
        """把一轮问答追加到该用户会话并落盘"""
        self._memory.append_turn(user_id, question, answer)

    def clear_context(self, user_id: str) -> bool:
        """清空该用户会话上下文，返回是否清掉了已有会话"""
        return self._memory.clear(user_id)

    # ==================== 群内识图摘要共享 ====================
    # 场景：群友A发图识别后，群友B追问"这张图里排位赛几点"。
    # B没有A的会话历史，这里按群共享最近一次识图结果（15分钟有效），
    # 仅当B的问题含图片指代词且B不是识图者本人时注入，避免污染无关提问

    VISION_GROUP_TTL = 15 * 60
    VISION_REF_TRIGGERS = (
        "这图", "这张图", "这个图", "图中", "图上", "图里",
        "这表", "这张表", "这个表", "表里", "表格中",
        "图片里", "截图里", "那张图", "那个表", "刚发的图", "发的图",
    )

    def save_group_vision(self, group_openid: str, user_id: str, question: str, answer: str):
        """保存群内最近一次识图结果"""
        if not group_openid:
            return
        import time as _t
        with self._conv_lock:
            self._group_vision[group_openid] = {
                "user_id": user_id, "question": question,
                "answer": answer, "ts": _t.time(),
            }

    def get_group_vision_context(self, group_openid: str, user_id: str, question: str) -> Optional[str]:
        """取群内近期识图摘要（仅指代词触发 + 非识图者本人）"""
        if not group_openid:
            return None
        if not any(t in question for t in self.VISION_REF_TRIGGERS):
            return None
        import time as _t
        with self._conv_lock:
            ctx = self._group_vision.get(group_openid)
        if not ctx:
            return None
        if _t.time() - ctx.get("ts", 0) > self.VISION_GROUP_TTL:
            return None
        if ctx.get("user_id") == user_id:
            return None
        return (f"【群内近期图片识别结果（群友问的是: {ctx['question'][:40]}）】\n"
                f"{ctx['answer'][:800]}\n"
                f"（该用户的问题可能指向这张图片，请结合上述识图结果回答）")

    # ==================== 两段式分层问答（取数层 + 成文层） ====================

    # 现役车手/车队标准英文名锚定表（数据源：data/drivers_profile.json，
    # 见 common/drivers_profile.py；此处为导入时快照兜底，name_glossary 每次实时读取）
    _DRIVER_EN = get_driver_en_map()
    _TEAM_EN = get_team_en_map()

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

    def _rewrite_query(self, provider: str, user_id: str, question: str) -> str:
        """
        L0 查询改写：结合会话历史+当前北京时间，把追问/代词/相对时间解析为独立完整问题。
        失败静默回退原问题，不阻塞主流程。
        """
        try:
            from datetime import datetime, timezone, timedelta
            now_cn = datetime.now(timezone(timedelta(hours=8)))
            system = (self._REWRITE_SYSTEM
                      + f"\n\n当前时间: {now_cn.strftime('%Y年%m月%d日 %H:%M')} (北京时间)")
            history = self._get_history(user_id)
            messages = ([{"role": "system", "content": system}]
                        + history[-6:]
                        + [{"role": "user", "content": question}])
            rewritten = self._chat_once(provider, messages, temperature=0.1,
                                        timeout=20, enable_search=False)
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

    @classmethod
    def name_glossary(cls) -> str:
        """生成车手/车队标准姓名锚定表（数据源：drivers_profile，实时读取）"""
        from . import drivers_profile as dp
        drivers = dp.get_drivers()
        teams = dp.get_teams()
        d = [f"{x['name_en']}（{x.get('name_cn', '')}）"
             for x in drivers.values() if x.get("name_en")]
        t = [f"{x['name_en']}（{(x.get('aliases') or [''])[0]}）"
             for x in teams.values() if x.get("name_en")]
        if not d:  # profile 异常时回退导入时快照
            d = [f"{en}" for en in cls._DRIVER_EN.values()]
            t = [f"{en}" for en in cls._TEAM_EN.values()]
        season = dp.get_meta().get("season", "")
        return (
            f"\n\n【{season}现役车手标准姓名，回答时必须严格使用，禁止替换为他人或自行音译】\n"
            + "; ".join(d)
            + "\n【车队标准名称】\n" + "; ".join(t)
        )

    # 2026 现行规则与动力单元格局锚定（外置提示词，防联网旧闻污染现行规则类回答）
    _DEFAULT_RULES_ANCHOR = (
        "\n\n【2026赛季现行规则锚定（优先于联网搜索结果，冲突时以此为准）】\n"
        "2026年已取消DRS（主动空力X/Z模式+手动电能超频超车）；2026年已取消MGU-H（仅MGU-K制动回收）；"
        "动力单元：Ferrari供应Ferrari/Haas/Cadillac；Mercedes供应Mercedes/McLaren/Williams/Alpine；"
        "Red Bull Powertrains自研内燃机+Ford电气合作供应Red Bull/Racing Bulls；"
        "Honda独家供应Aston Martin；Audi自研自供。"
    )
    _RULES_ANCHOR = "\n\n" + _load_prompt("rules_anchor", _DEFAULT_RULES_ANCHOR.strip())

    @classmethod
    def rules_anchor(cls) -> str:
        """2026 现行规则与动力单元格局锚定块"""
        return cls._RULES_ANCHOR

    def _l1_collect(self, provider: str, question: str,
                    fn_tools: List[Dict[str, Any]],
                    fn_handlers: Dict[str, Any],
                    user_id: str = None,
                    cancel_event=None):
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
                                         user_id=user_id, cancel_event=cancel_event)
        return self._l1_collect_native(provider, question, fn_tools, fn_handlers,
                                       user_id=user_id, cancel_event=cancel_event)

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

    def _l1_collect_json(self, provider: str, question: str,
                         fn_tools: List[Dict[str, Any]],
                         fn_handlers: Dict[str, Any],
                         user_id: str = None,
                         cancel_event=None):
        """
        L1 取数层（JSON mode）：response_format=json_object 强制模型只输出
        {"calls": [{"name": ..., "args": {...}}], "web_search": ["检索词", ...]}，
        calls 为空即取数结束。JSON 解析/校验失败引导重试一次，仍失败返回 (None, None)。
        """
        cfg = self.available[provider]
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
            payload = {"model": cfg["model"], "messages": messages,
                       "temperature": 0.1,
                       "response_format": {"type": "json_object"}}
            try:
                response = requests.post(cfg["base_url"], headers=headers, json=payload,
                                         timeout=req_timeout,
                                         proxies={"http": None, "https": None})
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
                    cancel_event=None):
        """
        L1 取数层（原生 function calling 路径，LLM_L1_JSON_MODE=false 时使用）
        同轮多个工具调用并行执行（LocalCache 有锁，线程安全），显著缩短取数耗时
        Returns:
            (collected: [(tool_name, result_str)], search_queries: [str])，失败返回 (None, None)
        """
        cfg = self.available[provider]
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
            payload = {"model": cfg["model"], "messages": messages,
                       "temperature": 0.1, "tools": tools}
            try:
                response = requests.post(cfg["base_url"], headers=headers, json=payload,
                                         timeout=req_timeout,
                                         proxies={"http": None, "https": None})
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

        l1_provider = next((p for p in self.ask_order if not self.available[p]["web_search"]), None)
        l2_provider = next((p for p in self.ask_order if self.available[p]["web_search"]), None)
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
        if not self._get_history(user_id) and not any(h in question for h in _REF_HINTS):
            standalone = question
        else:
            standalone = self._rewrite_query(l1_provider, user_id, question)

        if _cancelled():
            return None
        _set_stage("collect")
        collected, search_queries = self._l1_collect(l1_provider, standalone, fn_tools, fn_handlers,
                                                     user_id=user_id, cancel_event=cancel_event)
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
        l2_model = None
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
            _re_eff = os.getenv("LLM_L2_REASONING", "none").strip().lower()
            if _re_eff not in ("none", "low", "medium", "high"):
                logger.warning(f"LLM_L2_REASONING 非法值 '{_re_eff}'，回退 none")
                _re_eff = "none"
            l2_extra["reasoning_effort"] = _re_eff
        if deep_mode:
            l2_model = os.getenv("LLM_DEEP_MODEL", "kimi-k3")
            _deep_eff = os.getenv("LLM_DEEP_REASONING", "high").strip().lower()
            if _deep_eff not in ("none", "low", "medium", "high"):
                logger.warning(f"LLM_DEEP_REASONING 非法值 '{_deep_eff}'，回退 high")
                _deep_eff = "high"
            l2_extra = {"reasoning_effort": _deep_eff}
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
            from .knowledge_store import KnowledgeStore
            _ks = KnowledgeStore.get()
            system += _ks.knowledge_block() + _ks.habit_block(user_id)
        except Exception:
            pass

        user_msg = standalone
        if search_queries:
            user_msg += "\n\n（请联网检索以下方面补充回答: " + "; ".join(search_queries) + "）"

        history = self._get_history(user_id)
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

    # ==================== 图片视觉识别 ====================

    _DEFAULT_VISION_SYSTEM = (
        "你是F1专家，负责识别用户发来的图片。"
        "如果是F1赛程表/成绩图/积分榜/新闻截图，提取关键信息（时间统一换算为北京时间，日期保持原样）；"
        "如果是官方赛程表，逐项列出各环节名称和时间，便于用户与机器人推送核对。"
        "用简体中文回答，条理清晰；图片与F1无关时简要描述并礼貌引导回F1话题。"
    )
    VISION_SYSTEM = _load_prompt("vision", _DEFAULT_VISION_SYSTEM)

    @staticmethod
    def _image_to_data_url(url: str, max_bytes: int = 10 * 1024 * 1024) -> Optional[str]:
        """下载图片转为base64 data URL（QQ图床URL可能拒绝LLM服务端直接拉取），失败返回None"""
        import base64
        try:
            resp = requests.get(url, timeout=20,
                                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
                                proxies={"http": None, "https": None})
            if resp.status_code != 200:
                logger.warning(f"图片下载失败: HTTP {resp.status_code} - {url[:80]}")
                return None
            content = resp.content
            if len(content) > max_bytes:
                logger.warning(f"图片过大({len(content) // 1024}KB)，跳过: {url[:60]}")
                return None
            mime = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
            if not mime.startswith("image/"):
                mime = "image/jpeg"
            return f"data:{mime};base64," + base64.b64encode(content).decode()
        except Exception as e:
            logger.warning(f"图片下载异常: {e} - {url[:80]}")
            return None

    def ask_vision(self, question: str, image_urls: List[str], max_len: int = 1500,
                   user_id: str = None, group_openid: str = None,
                   qid: str = None) -> Optional[str]:
        """视觉识别入口（含 trace ID 生命周期管理），实现见 _ask_vision_impl"""
        prev = get_qid()
        set_qid(qid or prev or new_qid())
        try:
            return self._ask_vision_impl(question, image_urls, max_len, user_id, group_openid)
        finally:
            set_qid(prev)

    def _ask_vision_impl(self, question: str, image_urls: List[str], max_len: int = 1500,
                   user_id: str = None, group_openid: str = None) -> Optional[str]:
        """
        Kimi视觉识别：解读图片内容（官方赛程图/成绩截图等）
        图片先下载转base64 data URL上送，规避QQ图床防盗链；下载失败回退原始URL
        附带最近文字对话历史（覆盖"先发文字后发图"场景）；问答文本写入会话记忆（不含图片）

        Args:
            question: 用户随图问题（可为空，调用方给默认提示）
            image_urls: 图片URL列表（最多取前4张）
            max_len: 回答最大长度
            user_id: 用户 openid（提供时启用历史上下文与记忆）

        Returns:
            识别结果文本，失败返回None
        """
        if "kimi" not in self.available:
            logger.warning("图片识别需要Kimi，但未配置MOONSHOT_API_KEY")
            return None
        urls = [u for u in (image_urls or []) if u][:4]
        if not urls:
            return None

        cfg = self.available["kimi"]
        vision_model = os.getenv("LLM_VISION_MODEL", "kimi-k2.6")
        content = [{"type": "text", "text": question}]
        for u in urls:
            content.append({"type": "image_url",
                            "image_url": {"url": self._image_to_data_url(u) or u}})

        history = self._get_history(user_id)[-4:] if user_id else []
        payload = {
            "model": vision_model,
            "messages": ([{"role": "system", "content": self.VISION_SYSTEM}]
                         + history
                         + [{"role": "user", "content": content}]),
            # 不传temperature：kimi-k2.x视觉模型强制temperature=1，显式传0.3会400
        }
        try:
            response = requests.post(
                cfg["base_url"],
                headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
                json=payload, timeout=(int(os.getenv("LLM_CONNECT_TIMEOUT", "10")), self.timeout),
                proxies={"http": None, "https": None}
            )
        except requests.RequestException as e:
            logger.error(f"图片识别请求异常: {e}")
            return None
        if response.status_code != 200:
            logger.error(f"图片识别请求失败: HTTP {response.status_code} - {response.text[:300]}")
            return None
        try:
            answer = response.json()["choices"][0]["message"].get("content")
        except (ValueError, KeyError, IndexError) as e:
            logger.error(f"图片识别响应解析失败: {e}")
            return None
        if answer and len(answer) > max_len:
            answer = answer[:max_len] + "…"
        if answer and user_id:
            self._append_turn(user_id, question, answer)
        if answer:
            self.save_group_vision(group_openid, user_id, question, answer)
        return answer

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

        history = self._get_history(user_id)
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

    def chat_simple(self, system: str, user: str, timeout: int = 30,
                    temperature: float = 0.3) -> Optional[str]:
        """最简单次问答（不联网/不记上下文/无工具），按 ask_order 轮询供应商。
        供翻译/新闻摘要等内部功能复用；全部失败返回 None。"""
        if not self.enabled:
            return None
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": user}]
        for provider in self.ask_order:
            try:
                r = self._chat_once(provider, messages, temperature=temperature,
                                    timeout=timeout, enable_search=False)
                if r and r.strip():
                    return r
            except Exception as e:
                logger.warning(f"chat_simple[{provider}] 失败: {e}")
        return None

    def translate_en_to_zh(self, text: str, timeout: int = 12) -> Optional[str]:
        """
        将英文F1技术描述翻译为简洁中文（用于升级件说明等）

        降级方案：任何一层失败（未启用/超时/报错/卡死/空输出）都返回 None，
        由调用方回退输出英文原文，绝不因翻译失败阻塞主流程。

        Args:
            text: 英文原文
            timeout: 单次请求超时（秒）

        Returns:
            中文译文，失败返回 None
        """
        if not self.enabled or not text or not text.strip():
            return None

        translate_system = _load_prompt("translate",
            "你是F1技术翻译。把用户给出的英文技术描述翻译成简洁的中文，"
            "保留专业术语（如 Front Wing 前翼、Diffuser 扩散器、Sidepod 侧箱）。"
            "只输出译文本身，不要任何解释或前缀。")

        messages = [
            {"role": "system", "content": translate_system},
            {"role": "user", "content": text},
        ]

        translate_order = [n for n in TRANSLATE_ORDER if n in self.available]
        for provider in translate_order:
            answer = None
            try:
                answer = self._chat_once(provider, messages, temperature=0.1, timeout=timeout)
            except Exception as e:
                logger.warning(f"LLM翻译[{provider}]异常: {e}")
                answer = None

            if answer and answer.strip():
                stripped = answer.strip()
                # 过滤明显无效输出（过短、或只是重复原文）
                if len(stripped) < 2 or stripped == text.strip():
                    logger.warning(f"LLM翻译[{provider}]输出无效，回退原文")
                else:
                    return stripped
            logger.warning(f"LLM翻译[{provider}]无有效输出")

        logger.warning("所有LLM供应商翻译失败，回退原文")
        return None

    def verify_lap_record(self, circuit_name_en: str,
                          local_record: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
        """
        联网核查赛道F1正赛最快圈速纪录（必须联网，固定走Kimi）

        Args:
            circuit_name_en: 赛道英文名（如 "Suzuka Circuit"）
            local_record: 本地记录 {"time": ..., "driver": ..., "year": ...}，用于对比

        Returns:
            {
                "time": "1:30.965",
                "driver_en": "Kimi Antonelli",
                "driver": "基米·安东内利",
                "year": 2025,
                "consistent": True/False,   # 与本地记录是否一致
                "changed": True/False       # 是否应以LLM结果为准更新
            }
            查询失败或未配置Kimi返回None
        """
        if "kimi" not in self.available:
            logger.warning("圈速纪录核查需要联网能力，但未配置MOONSHOT_API_KEY")
            return None

        local_hint = ""
        if local_record and local_record.get("time"):
            local_hint = (
                f"\n本地数据库中的记录为：{local_record.get('time')} "
                f"({local_record.get('driver_en') or local_record.get('driver', '')}，"
                f"{local_record.get('year', '')})，请核对其准确性。"
            )

        prompt = (
            f"请联网查询 {circuit_name_en} 赛道的F1正赛最快圈速纪录"
            f"（official race lap record，正赛中创造的最快圈，不是排位赛成绩）。"
            f"{local_hint}\n"
            "请严格以如下JSON格式返回，不要输出其他内容：\n"
            '{"time": "M:SS.sss", "driver_en": "英文全名", "driver": "中文名", "year": 年份数字}\n'
            "如果联网查询后仍无法确认，返回：{\"time\": null}"
        )

        messages = [
            {"role": "system", "content": "你是F1数据核查员，只输出JSON。"},
            {"role": "user", "content": prompt}
        ]

        content = self._chat_once("kimi", messages, temperature=0.1)
        if not content:
            return None

        record = self._parse_json(content)
        if not record or not record.get("time"):
            logger.warning(f"LLM未能确认{circuit_name_en}圈速纪录: {content[:200]}")
            return None

        result = {
            "time": str(record["time"]).strip(),
            "driver_en": record.get("driver_en", ""),
            "driver": record.get("driver", "") or record.get("driver_en", ""),
            "year": record.get("year", ""),
            "consistent": None,
            "changed": False,
        }

        # 与本地记录对比
        if local_record and local_record.get("time"):
            local_sec = self.time_to_seconds(local_record["time"])
            llm_sec = self.time_to_seconds(result["time"])
            if local_sec is not None and llm_sec is not None:
                result["consistent"] = abs(local_sec - llm_sec) < 0.001
                result["changed"] = not result["consistent"]
            else:
                result["consistent"] = None
                result["changed"] = True
        else:
            result["consistent"] = None
            result["changed"] = True

        logger.info(
            f"圈速纪录核查[{circuit_name_en}]: LLM={result['time']} "
            f"({result['driver_en']}, {result['year']}) "
            f"本地={local_record.get('time') if local_record else '无'} "
            f"一致={result['consistent']}"
        )
        return result

    @staticmethod
    def _parse_json(text: str) -> Optional[Dict[str, Any]]:
        """从LLM输出中提取JSON对象"""
        match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except ValueError:
            return None

    @staticmethod
    def time_to_seconds(time_str: str) -> Optional[float]:
        """将圈速时间字符串转换为秒数，如 "1:30.965" -> 90.965"""
        if not time_str:
            return None
        try:
            parts = str(time_str).strip().split(":")
            if len(parts) == 2:
                return float(parts[0]) * 60 + float(parts[1])
            return float(parts[0])
        except (ValueError, IndexError):
            return None
