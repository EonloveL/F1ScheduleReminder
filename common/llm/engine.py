"""核心对话引擎 mixin：_chat_once 工具循环 / _execute_tool / _finalize_answer + 静态启发式"""

import json
import logging
import os
import re
from typing import Optional, Dict, Any, List

import requests

from .tracing import get_qid

logger = logging.getLogger(__name__)


class EngineMixin:
    """单次对话引擎与工具执行：_chat_once / _execute_tool / _finalize_answer /
    _strip_tool_markup / _looks_degenerate / _looks_like_preamble"""

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
        cfg = self.available.get(provider)
        if not cfg:
            # reload_runtime（管理员 /llm delkey/reset）恰好在途问答中途移除了该供应商：
            # 判空跳过而非 KeyError 假故障（review M2）
            logger.warning(f"供应商[{provider}]运行时已移除，本次调用跳过")
            return None
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
                # 拼接结果仍可能是前言/退化（OSS review 修复回灌：拆分丢失的复检）
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
        # 轮次耗尽时的截断续写前缀可能尚未拼回（截断续写轮之后又走了工具调用轮、
        # 没经过合并路径），需在此补拼（OSS review 修复回灌：拆分丢失的补拼逻辑）
        final = last_content
        if length_prefix:
            if not final:
                final = length_prefix
            elif not final.startswith(length_prefix):
                final = (length_prefix + "\n" + final).strip()
        return final if (final
                         and not self._looks_like_preamble(final)
                         and not self._looks_degenerate(final)) else None

    def _execute_tool(self, name: str, arguments_json, handlers: Dict[str, Any],
                      user_id: str = None, qid: str = None) -> str:
        """执行自定义函数工具，返回 JSON 字符串结果；每次调用写入审计日志"""
        import time as _t
        from ..llm_audit import LLMToolAudit
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
            from ..knowledge_store import KnowledgeStore
            if not (user_id or "").startswith("kb-"):  # 排除知识库校对等内部伪用户
                KnowledgeStore.get().archive_qa(get_qid() or "", user_id, question, answer,
                                                tools=tools_log, provider=provider)
        except Exception:
            pass
        return answer
