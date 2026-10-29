# -*- coding: utf-8 -*-
"""实体与主题词抽取（记忆相关性计算的默认引擎）

实体三类：
- drv:xxx    车手（drivers_profile 别名全表命中，归一到 driverId）
- team:xxx   车队（同上，归一到 constructorId 风格键）
- topic:xxx  F1 主题词（内置词表，中文/英文/术语）

设计原则：零依赖、确定性、毫秒级；向量后端（embeddings.py）是可选升级而非替代。
"""
import logging
import re
from typing import Set

logger = logging.getLogger(__name__)

# F1 主题词表（小写匹配；覆盖技术/赛事/规则/商业高频话题）
TOPIC_WORDS = {
    # 技术
    "halo", "drs", "ers", "mguk", "mguh", "undercut", "overcut", "diffuser", "downforce",
    "尾翼", "前翼", "底板", "扩散器", "侧箱", "下压力", "尾流", "滑流", "slipstream",
    "动力单元", "引擎", "发动机", "涡轮", "电池", "混动", "变速箱", "悬挂", "刹车",
    "空气动力", "气动", "风洞", "cfd", "模拟器",
    # 赛事
    "排位", "正赛", "冲刺", "练习赛", "杆位", "最快圈", "领奖台", "积分", "总冠军",
    "世界冠军", "车手冠军", "车队冠军", "退赛", "碰撞", "事故", "罚时", "罚退", "维修区",
    "进站", "换胎", "轮胎", "软胎", "中性胎", "硬胎", "雨胎", "半雨胎", "全雨胎",
    "安全车", "虚拟安全车", "黄旗", "红旗", "蓝旗", "黑旗", "drs区", "发车", "起步",
    "pole", "podium", "pitstop", "stint", "pace", "telemetry", "遥测", "策略",
    # 规则/商业
    "预算帽", "车检", "规则", "违规", "上诉", "罚款", "超级驾照", "转会", "合同", "续约",
    "领队", "技术总监", " Reserve".lower(), "预备车手", "青训",
}


def extract_entities(text: str) -> Set[str]:
    """从文本提取标准实体键集合（小写子串匹配，别名归一）"""
    if not text:
        return set()
    t = text.lower()
    out: Set[str] = set()
    try:
        from ..drivers_profile import get_driver_aliases, get_team_aliases
        for alias, canon in get_driver_aliases().items():
            if alias and alias.lower() in t:
                out.add(f"drv:{canon}")
        for alias, canon in get_team_aliases().items():
            if alias and alias.lower() in t:
                out.add(f"team:{canon}")
    except Exception as e:
        logger.debug(f"车手/车队别名抽取失败: {e}")
    for w in TOPIC_WORDS:
        if w in t:
            out.add(f"topic:{w}")
    return out


def jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / len(a | b)


# 会话指代词：新问题无实体但含这些词 → 追问上文（"那他队友呢"），强制召回最近工作记忆
# 两条教训（写死勿回退）：
# ① 仅收会话指代词——相对时间词（今天/下周/上一场）指向赛历而非对话历史，
#    收进来会把"今天天气怎么样"误判成追问（2026-10-29 测试实证）
# ② 不收泛疑问词（为什么/是多少/呢）与裸单字——泛疑问句在新话题同样高频，
#    强制召回面过宽会削弱"零交集不召回"的核心承诺（review H3）
REFERENCE_HINTS = (
    "那场", "那次", "这支", "这位", "那个", "那人", "此人", "这场", "这站",
    "刚才", "刚刚", "上面", "前面说的", "队友", "对手", "接着说", "继续说",
    "那他们", "他们", "她们", "他呢", "她呢", "它呢",
    "that race", "this one", "teammate", "his teammate", "their",
)


def has_reference_hint(text: str) -> bool:
    t = (text or "").lower()
    if not t:
        return False
    # 单字"他/她/它"单独判定：排除"其他/吉他/她俩"等误命中构词
    for h in REFERENCE_HINTS:
        if h in t:
            return True
    for pron in ("他", "她", "它"):
        if pron in t and f"其{pron}" not in t and "吉他" not in t:
            return True
    return False
