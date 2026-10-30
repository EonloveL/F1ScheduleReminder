# -*- coding: utf-8 -*-
"""全指令分发链路冒烟测试：真实走 on_group_at_message_create（含闭包/胶水层），
回复函数替换为记录器，数据层走真实缓存+网络。

运行：python -m pytest tests/test_commands_smoke.py -q
网络依赖（f1api.dev/Jolpica/OpenF1/FIA/F1Cosmos），离线环境会大量失败，属预期。
"""
import asyncio
import os
import tempfile

import pytest

try:
    from qq_group_official_version import main as m  # 生产仓布局（包）
except ImportError:
    import main as m  # OSS 仓布局（根目录 main.py）
from common.f1_api import F1API
from common.fia_docs import FIADocsAPI
from common.user_prefs import UserPrefsStore
from common.ratings_store import RatingsStore

SENT = []

# 指令 → 回复中禁止出现的字样的额外断言（可选）
CASES = {
    "/help": None,
    "/drivers": None,
    "/teams": None,
    "/gp madring 2026 race": "正赛",
    "/gp madring 2026": "练习赛",          # 全环节汇总路径（2026-10-15 事故路径）
    "/gp 斯帕 2021 sprint": "Belgian",
    "/watch": None,
    "/live": None,
    "/weather": None,
    "/weather 新加坡": "Singapore",
    "/upgrades": None,
    "/upgrades 2026": "升级",             # 赛季汇总（全量数+大升级）
    "/upgrade 法拉利": "Ferrari",
    "/pu": None,
    "/pitstops": None,
    "/sc": None,
    "/qualigap": None,
    "/racechart": None,
    "/trend": None,
    "/pace": None,
    "/car": None,
    "/telemetry": None,
    "/rate": None,
    "/ratings": None,
    "/dotd": None,
    "/calendar": None,
    "/next": None,
    "/last": None,
    "/setdriver 维斯塔潘": "Verstappen",
    "/setteam 法拉利": "Ferrari",
    "/me": None,
    "/unbind 车手 维斯塔潘": "移除",
    "/news off": None,
    "/clear": None,
    "/kb list": None,
    "/models": None,
    "/model set l2 kimi/kimi-k2.6": None,
    "/bili_login": None,
    "/gp 不存在的赛道": "未找到",
    "/unknowncmd": "未知命令",
    "/dirvers": "你是不是想用",   # 指令纠错建议
    "/tr 维斯塔潘 simply lovely": None,  # TR 卡内置路径（无 TR_STUDIO_URL 时）
}

FAIL_MARKERS = ("指令处理失败", "处理失败", "Traceback")


async def _rec_text(message, content):
    SENT.append(("text", content))


async def _rec_md(message, text, md):
    SENT.append(("md", text or ""))


async def _rec_img(message, url, content=""):
    SENT.append(("img", f"{content} {url}"))


m.reply_text = _rec_text
m.reply_md = _rec_md
m.reply_image = _rec_img


class _FakeAuthor:
    member_openid = "TESTUSER0001"
    id = "TESTUSER0001"


class _FakeMessage:
    def __init__(self, content):
        self.content = content
        self.id = "msg001"
        self.group_openid = "TESTGROUP"
        self.author = _FakeAuthor()
        self.attachments = []

    async def reply(self, **kwargs):
        SENT.append(("reply", str(kwargs.get("content") or kwargs.get("markdown") or "")))


@pytest.fixture(scope="session")
def handler():
    os.environ.setdefault("KB_ENABLED", "false")
    tmp = tempfile.mkdtemp()
    f1_api = F1API(season=int(os.getenv("TEST_SEASON", "0")) or None)
    f1cosmos = FIADocsAPI(season=f1_api.season)
    cls = m.create_command_client(
        f1_api, UserPrefsStore(file_path=os.path.join(tmp, "prefs.json")),
        llm=None, ratings_store=RatingsStore(file_path=os.path.join(tmp, "ratings.json")),
        qq_bot=None, f1cosmos=f1cosmos)
    return cls.on_group_at_message_create


@pytest.mark.parametrize("cmdline,expect", [(c, e) for c, e in CASES.items()],
                         ids=[c for c in CASES])
def test_command(handler, cmdline, expect):
    SENT.clear()
    asyncio.run(asyncio.wait_for(handler(None, _FakeMessage(cmdline)), timeout=600))
    assert SENT, f"{cmdline}: 无任何回复（静默失败）"
    payload = "\n".join(p for _, p in SENT)
    for marker in FAIL_MARKERS:
        assert marker not in payload, f"{cmdline}: 回复含失败标记「{marker}」"
    if expect:
        assert expect in payload, f"{cmdline}: 回复缺少预期内容「{expect}」"
