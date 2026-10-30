"""三层衰减记忆（common/memory）测试

核心断言：
- 无关话题零交集 → 不召回（防污染，本机制的存在意义）
- 时间衰减单调：越久召回分越低；锚定记忆不衰减
- /clear 归档式清空（进 L2 而非删除）
- 1h 超时惰性归档
- 总结失败兜底不丢内容
"""
import time

import pytest

from common.memory import MemoryFacade, scoring, topics
from common.memory.store import MemoryStore


class FakeLLM:
    """假 LLM：enabled=False → summarizer 走兜底拼接（不依赖真实 API）"""
    enabled = False

    def chat_for_layer(self, *a, **kw):
        return None


@pytest.fixture
def mem(tmp_path):
    store = MemoryStore(str(tmp_path / "memory_store.json"))
    return MemoryFacade(llm=FakeLLM(), store=store)


@pytest.fixture(autouse=True)
def no_embedding(monkeypatch):
    """测试环境强制关闭 embedding 后端（纯实体匹配，确定性）"""
    monkeypatch.setenv("MEMORY_EMBEDDING", "off")
    import common.memory.embeddings as emb
    monkeypatch.setattr(emb, "_backend", None)
    monkeypatch.setattr(emb, "_backend_tried", False)
    yield


# ---------- 打分 ----------

def test_recency_monotonic_decay():
    now = time.time()
    recent = scoring.recency(now - 600, now, scoring.HALF_LIFE_L1)
    old = scoring.recency(now - 3600, now, scoring.HALF_LIFE_L1)
    older = scoring.recency(now - 7200, now, scoring.HALF_LIFE_L1)
    assert recent > old > older
    assert abs(old - 0.5) < 0.01  # 半衰期点


def test_anchor_no_decay():
    assert scoring.recency(time.time() - 365 * 86400, half_life=None) == 1.0


def test_zero_overlap_zero_relevance():
    assert scoring.relevance({"drv:verstappen"}, {"drv:hamilton"}) == 0.0


def test_neutral_when_both_empty():
    assert scoring.relevance(set(), set()) == scoring.NEUTRAL_REL


def test_entities_extraction():
    ents = topics.extract_entities("维斯塔潘和红牛这站的 DRS 怎么样")
    assert any(e.startswith("topic:drs") for e in ents)


# ---------- 召回（防污染核心） ----------

def test_unrelated_history_not_recalled(mem):
    """问积分后问天气：第二轮不得携带第一轮历史（污染回归测试）"""
    uid = "u1"
    mem.append_turn(uid, "维斯塔潘现在积分多少", "维斯塔潘当前 450 分领先")
    ctx = mem.build_context(uid, "今天天气怎么样")
    assert ctx["history"] == []          # 零实体交集 → 不召回
    assert ctx["memory_block"] is None


def test_related_history_recalled(mem):
    uid = "u2"
    mem.append_turn(uid, "维斯塔潘现在积分多少", "维斯塔潘当前 450 分领先")
    ctx = mem.build_context(uid, "维斯塔潘领先第二名多少")
    assert len(ctx["history"]) == 2      # 同车手实体命中 → 召回
    assert ctx["history"][0]["role"] == "user"


def test_pronoun_followup_recalled(mem):
    """代词追问（无实体+指代词）必须召回上文，否则 L0 改写解析不了"他"——
    与"无实体换话题"（test_unrelated_history_not_recalled）是指代词区分的两条路径"""
    uid = "u12"
    mem.append_turn(uid, "维斯塔潘现在积分多少", "维斯塔潘当前 450 分领先")
    ctx = mem.build_context(uid, "那他队友呢")
    assert len(ctx["history"]) == 2      # 指代词触发强制召回
    assert "维斯塔潘" in ctx["history"][0]["content"]


def test_old_turn_fades_out(mem):
    """同主题但 4 小时前（4 个半衰期）→ 分数跌破阈值不召回"""
    uid = "u3"
    mem.append_turn(uid, "维斯塔潘积分", "450 分")
    data = mem._store.load_user(uid)
    data["working"][0]["ts"] = time.time() - 4 * 3600
    mem._store.save_user(uid, data)
    ctx = mem.build_context(uid, "维斯塔潘冠军")
    assert ctx["history"] == []


# ---------- 锚定 ----------

def test_anchor_recalled_regardless_of_age(mem):
    uid = "u4"
    mem.mark_anchor(uid, "用户纠错：维斯塔潘 2024 年车手总冠军", source="correction")
    data = mem._store.load_user(uid)
    data["anchors"][0]["ts"] = time.time() - 30 * 86400  # 30 天前
    mem._store.save_user(uid, data)
    ctx = mem.build_context(uid, "维斯塔潘今年冠军形势如何")
    assert ctx["memory_block"] and "用户纠错" in ctx["memory_block"]


def test_anchor_not_recalled_when_unrelated(mem):
    uid = "u5"
    mem.mark_anchor(uid, "用户纠错：维斯塔潘 2024 年总冠军", source="correction")
    ctx = mem.build_context(uid, "明天天气如何")
    assert ctx["memory_block"] is None


# ---------- 归档 ----------

def test_clear_archives_instead_of_delete(mem):
    uid = "u6"
    mem.append_turn(uid, "维斯塔潘积分", "450 分")
    mem.append_turn(uid, "红牛车队积分", "600 分")
    n = mem.archive_and_clear(uid, reason="clear")
    assert n == 2
    data = mem._store.load_user(uid)
    assert data["working"] == []                 # 工作记忆已清
    assert len(data["episodes"]) >= 1            # 兜底摘要进 L2
    assert "维斯塔潘" in data["episodes"][0]["summary"]


def test_clear_empty_returns_zero(mem):
    assert mem.archive_and_clear("nobody") == 0


def test_timeout_lazy_archive(mem):
    uid = "u7"
    mem.append_turn(uid, "维斯塔潘积分", "450 分")
    data = mem._store.load_user(uid)
    data["working"][0]["ts"] = time.time() - 3700  # 超过 1h
    mem._store.save_user(uid, data)
    # 下次交互触发惰性归档（后台线程：锁内摘除 working → 锁外总结回写 episodes）
    mem.build_context(uid, "无关问题")
    for _ in range(50):
        d = mem._store.load_user(uid)
        if not d["working"] and d["episodes"]:
            break
        time.sleep(0.05)
    data = mem._store.load_user(uid)
    assert data["working"] == []
    assert len(data["episodes"]) >= 1


def test_overflow_compress(mem):
    uid = "u8"
    for i in range(13):
        mem.append_turn(uid, f"维斯塔潘问题{i}", f"回答{i}")
    for _ in range(50):
        if len(mem._store.load_user(uid)["working"]) <= 12:
            break
        time.sleep(0.05)
    data = mem._store.load_user(uid)
    assert len(data["working"]) <= 12
    assert len(data["episodes"]) >= 1


def test_recurrence_boosts_salience(mem):
    uid = "u9"
    mem.append_turn(uid, "维斯塔潘积分", "450")
    mem.append_turn(uid, "维斯塔潘胜率", "很高")
    data = mem._store.load_user(uid)
    assert data["working"][1]["salience"] > 1.0  # 同实体重复加权


# ---------- 管理 ----------

def test_describe_and_forget(mem):
    uid = "u10"
    mem.append_turn(uid, "维斯塔潘积分", "450 分")
    desc = mem.describe(uid)
    assert "工作记忆 1 轮" in desc
    iid = mem._store.load_user(uid)["working"][0]["id"]
    assert mem.forget(uid, iid)
    assert mem._store.load_user(uid)["working"] == []
    assert not mem.forget(uid, "nonexist")


def test_has_active_session(mem):
    uid = "u11"
    assert not mem.has_active_session(uid)
    mem.append_turn(uid, "hello", "hi")
    assert mem.has_active_session(uid)
    data = mem._store.load_user(uid)
    data["working"][0]["ts"] = time.time() - 3700
    mem._store.save_user(uid, data)
    assert not mem.has_active_session(uid)
