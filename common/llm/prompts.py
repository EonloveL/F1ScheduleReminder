"""提示词外置加载（common/prompts/*.txt）：优先文件，缺失/失败回退内嵌默认值"""

import logging
import os

logger = logging.getLogger(__name__)

# ==================== 提示词外置（common/prompts/*.txt） ====================
# 优先从文件加载，缺失/读取失败回退内嵌默认值，调提示词无需改代码
# 注意：本文件位于 common/llm/，提示词目录在 common/prompts，需向上一级
_PROMPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")


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
