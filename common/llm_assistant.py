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

实现拆分（2026-10-29）：本文件仅保留门脸类 LLMAssistant（多 mixin 组合）与模块级 re-export，
具体实现见 common/llm/ 包：tracing / prompts / providers / engine / layered / vision / misc。
"""

import json
import logging
import re
import threading as _threading
from typing import Optional, Dict, Any, List

from .llm.tracing import new_qid, set_qid, get_qid, QIDLogFilter
from .llm.prompts import _load_prompt
from .llm.providers import PROVIDERS, DEFAULT_ASK_ORDER, TRANSLATE_ORDER, ProvidersMixin
from .llm.engine import EngineMixin
from .llm.layered import LayeredMixin
from .llm.vision import VisionMixin
from .llm.misc import MiscMixin

logger = logging.getLogger(__name__)


class LLMAssistant(ProvidersMixin, EngineMixin, LayeredMixin, VisionMixin, MiscMixin):
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
        # 双轨：实例级 last_error（兼容旧读取方）+ qid 键控（并发问答不串用户，OSS 侧修复回灌）
        self.last_error: Optional[str] = None
        self._error_by_qid: Dict[str, str] = {}
        self._error_lock = _threading.Lock()
        # 不接受显式 temperature 的模型集合（kimi-k2.x/k3 强制 temperature=1，传值即 400）；
        # 首次 400 重试时记入，之后直接免传，省掉每次请求的浪费往返（2026-09-15 事故中浪费约40s）
        self._no_temp_models: set = set()
        # 不支持 response_format json_object 的模型集合（L1 JSON mode 兼容重试记忆）
        self._no_json_mode_models: set = set()
        # get_race_prediction 的 sc_warning（高安全车赛道警示）暂存 {qid: 警示句}：
        # _execute_tool 命中时登记，_finalize_answer 确定性追加（实证 2026-09-23：
        # prediction.txt 规则 3b 要求"原样引用 sc_warning"被 L2/兜底模型三次忽略，
        # LLM 遵守提示词不可靠，改代码兜底保证高 SC 声明必达）
        self._sc_notices: Dict[str, str] = {}

        # 检测可用供应商（env + 运行时覆盖 llm_secrets.json，后者优先）
        self._providers_lock = _threading.Lock()
        self._configure_providers()

        # 三层会话记忆（common/memory，稀疏注意力式召回）
        import threading
        from .memory import MemoryFacade
        self._memory = MemoryFacade(llm=self)
        self._conv_lock = threading.Lock()
        # 群内最近识图摘要：{group_openid: {user_id, question, answer, ts}}
        self._group_vision: Dict[str, Dict[str, Any]] = {}

    def _set_error(self, msg: str):
        """记录供应商级故障：实例级字段 + qid 键控（并发问答不串用户）"""
        self.last_error = msg
        try:
            from .llm.tracing import get_qid
            qid = get_qid()
        except Exception:
            qid = None
        if qid:
            with self._error_lock:
                self._error_by_qid[qid] = msg
                if len(self._error_by_qid) > 200:  # 防膨胀：只留最近 200 条
                    for k in list(self._error_by_qid.keys())[:-100]:
                        self._error_by_qid.pop(k, None)

    def get_error(self, qid: str = None) -> Optional[str]:
        """qid 键控读取故障（并发安全）；qid 缺省回退实例级 last_error"""
        if qid:
            with self._error_lock:
                v = self._error_by_qid.get(qid)
            if v:
                return v
        return self.last_error

    # ==================== 多轮上下文记忆（稀疏注意力式三层记忆） ====================
    # 设计见 common/memory/__init__.py：L1 工作记忆（半衰期1h）/ L2 主题摘要（3天）/
    # L3 锚定（不衰减）；score=衰减×相关性×显著性，零实体交集不召回（防无关历史污染）

    def _get_memory_context(self, user_id: str, question: str) -> Dict[str, Any]:
        """召回记忆上下文：{"history": [消息列表], "memory_block": str|None}"""
        try:
            return self._memory.build_context(user_id, question)
        except Exception as e:
            logger.warning(f"记忆召回失败（降级无记忆）: {e}")
            return {"history": [], "memory_block": None}

    def _get_history(self, user_id: str, question: str = "") -> List[Dict[str, str]]:
        """兼容接口：仅返回 L1 筛中轮次的消息列表（L0 改写/识图等只需原文历史处用）"""
        return self._get_memory_context(user_id, question)["history"]

    def _append_turn(self, user_id: str, question: str, answer: str):
        """把一轮问答写入 L1 工作记忆（实体抽取/显著性加权/溢出压缩由 facade 负责）"""
        try:
            self._memory.append_turn(user_id, question, answer)
        except Exception as e:
            logger.warning(f"记忆写入失败: {e}")

    def clear_context(self, user_id: str):
        """归档式清空：工作记忆 → LLM 总结 → L2 主题摘要。返回归档轮数（0=无内容）"""
        try:
            return self._memory.archive_and_clear(user_id, reason="clear")
        except Exception as e:
            logger.warning(f"归档式清空失败: {e}")
            return 0

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
