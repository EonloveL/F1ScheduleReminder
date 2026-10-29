"""
Ergast 格式积分榜解析（平台无关）

从 qq_group_bot.QQGroupBot 抽取，供 scheduler / f1_tools / 各平台适配层共用，
避免非 QQ 平台反向依赖 QQ 适配层。
"""
from typing import Any, Dict


def parse_driver_standings(standings: Dict[str, Any]) -> list:
    """解析Ergast格式车手积分榜"""
    try:
        lists = standings["MRData"]["StandingsTable"]["StandingsLists"]
        return lists[0].get("DriverStandings", []) if lists else []
    except (KeyError, IndexError, TypeError):
        return []


def parse_constructor_standings(standings: Dict[str, Any]) -> list:
    """解析Ergast格式车队积分榜"""
    try:
        lists = standings["MRData"]["StandingsTable"]["StandingsLists"]
        return lists[0].get("ConstructorStandings", []) if lists else []
    except (KeyError, IndexError, TypeError):
        return []
