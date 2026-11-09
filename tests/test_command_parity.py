# -*- coding: utf-8 -*-
"""双平台指令一致性测试。

QQ 侧 handlers 与 Discord 侧 slash commands 行为不应悄悄分叉。
短期落地：命令名交集一致 + 同名命令参数语义一致（白名单允许平台独占）。
"""
import ast
import os
from typing import Dict, Set

import pytest


@pytest.fixture
def qq_commands():
    """QQ 侧指令名集合。"""
    from handlers import COMMAND_HANDLERS
    return set(COMMAND_HANDLERS.keys())


@pytest.fixture
def discord_commands():
    """Discord 侧 slash command 名集合（AST 提取，不依赖 py-cord 运行）。"""
    discord_py = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "discord_version", "commands.py")
    with open(discord_py, encoding="utf-8") as f:
        tree = ast.parse(f.read())

    cmds: Set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "slash_command":
                for kw in node.keywords:
                    if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                        cmds.add(kw.value.value)
    return cmds


# 允许单边独占的指令（有明确平台原因）
QQ_ONLY_WHITELIST = {"bili_login", "kb", "llm", "model", "models", "tr", "upgrade"}
DISCORD_ONLY_WHITELIST = {"ask", "tz"}

# 核心指令：两边都必须存在且语义对应
CORE_COMMANDS = {
    "calendar", "car", "clear", "dotd", "drivers", "gp", "help", "last",
    "live", "me", "memory", "news", "next", "pace", "pitstops", "predict",
    "pu", "qualigap", "racechart", "rate", "ratings", "sc", "setdriver",
    "setteam", "teams", "telemetry", "trend", "unbind", "upgrades", "watch",
    "weather",
}


class TestCommandParity:
    def test_core_commands_present_on_both_sides(self, qq_commands, discord_commands):
        """核心指令在 QQ 和 Discord 两侧同时存在。"""
        missing_qq = CORE_COMMANDS - qq_commands
        missing_discord = CORE_COMMANDS - discord_commands
        assert not missing_qq, f"核心指令在 QQ 侧缺失: {sorted(missing_qq)}"
        assert not missing_discord, f"核心指令在 Discord 侧缺失: {sorted(missing_discord)}"

    def test_qq_only_whitelist(self, qq_commands, discord_commands):
        """QQ 独占指令必须在白名单内（防止新指令只加了一边）。"""
        qq_only = qq_commands - discord_commands - QQ_ONLY_WHITELIST
        assert not qq_only, (
            f"QQ 侧新增独占指令未声明：{sorted(qq_only)}，"
            f"请加入 QQ_ONLY_WHITELIST 或同步到 Discord")

    def test_discord_only_whitelist(self, qq_commands, discord_commands):
        """Discord 独占指令必须在白名单内。"""
        discord_only = discord_commands - qq_commands - DISCORD_ONLY_WHITELIST
        assert not discord_only, (
            f"Discord 侧新增独占指令未声明：{sorted(discord_only)}，"
            f"请加入 DISCORD_ONLY_WHITELIST 或同步到 QQ")

    def test_no_unexpected_discord_only_core(self, qq_commands, discord_commands):
        """Discord 侧没有核心指令缺失（反向验证）。"""
        # 与 test_core_commands_present_on_both_sides 互补，显式检查核心交集
        assert CORE_COMMANDS.issubset(qq_commands & discord_commands)
