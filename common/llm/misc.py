"""杂项内部功能 mixin：chat_simple / chat_for_layer / translate_en_to_zh /
verify_lap_record / name_glossary / rules_anchor"""

import logging
from typing import Optional, Dict, Any

from ..drivers_profile import get_driver_en_map, get_team_en_map
from .prompts import _load_prompt
from .providers import TRANSLATE_ORDER

logger = logging.getLogger(__name__)


class MiscMixin:
    """内部复用小功能：chat_simple / chat_for_layer / translate_en_to_zh /
    verify_lap_record / name_glossary / rules_anchor"""

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

    def chat_for_layer(self, layer: str, system: str, user: str, timeout: int = 45) -> Optional[str]:
        """按分层偏好调用指定供应商/模型（/model set <层> 配置），缺省=ask_order 首个非联网。
        供记忆归档总结等内部功能使用；失败返回 None（调用方有兜底）"""
        if not self.enabled or not self.ask_order:
            return None
        default = next((p for p in self.ask_order if not (self.available.get(p) or {}).get("web_search", False)),
                       self.ask_order[0])
        provider, model = self._resolve_layer(layer, default)
        if provider not in self.available:
            return None
        try:
            return self._chat_once(provider,
                                   [{"role": "system", "content": system},
                                    {"role": "user", "content": user}],
                                   temperature=0.2, timeout=timeout,
                                   enable_search=False, model=model)
        except Exception as e:
            logger.warning(f"chat_for_layer[{layer}→{provider}] 失败: {e}")
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

    # 现役车手/车队标准英文名锚定表（数据源：data/drivers_profile.json，
    # 见 common/drivers_profile.py；此处为导入时快照兜底，name_glossary 每次实时读取）
    _DRIVER_EN = get_driver_en_map()
    _TEAM_EN = get_team_en_map()

    @classmethod
    def name_glossary(cls) -> str:
        """生成车手/车队标准姓名锚定表（数据源：drivers_profile，实时读取）"""
        from .. import drivers_profile as dp
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
