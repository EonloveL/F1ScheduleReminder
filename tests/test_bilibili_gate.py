# -*- coding: utf-8 -*-
"""B站视频导读：链接解析与 F1 门禁（离线单测）"""
from common.bilibili_summary import BiliVideoClient


class TestParseBvid:
    def test_bv(self):
        assert BiliVideoClient.parse_bvid("https://www.bilibili.com/video/BV1RBnB6pEGr/?spm=1") == "BV1RBnB6pEGr"

    def test_bv_in_text(self):
        assert BiliVideoClient.parse_bvid("看看这个 BV1RBnB6pEGr 气动分析") == "BV1RBnB6pEGr"

    def test_no_link(self):
        assert BiliVideoClient.parse_bvid("今天比赛几点开始") is None


class TestF1Gate:
    def _info(self, title, desc="", owner="", tname="", tags=""):
        return {"title": title, "desc": desc, "owner": owner, "tname": tname,
                "tags": tags}

    def test_f1_title(self):
        ok, hits = BiliVideoClient.is_f1_related(self._info("2026新加坡大奖赛正赛复盘"))
        assert ok and "大奖赛" in hits

    def test_f1_aero(self):
        ok, _ = BiliVideoClient.is_f1_related(self._info("深度解析红牛底板与扩散器设计"))
        assert ok

    def test_f1_driver_alias(self):
        ok, _ = BiliVideoClient.is_f1_related(self._info("维斯塔潘这个赛季有多强"))
        assert ok

    def test_f1_english(self):
        ok, _ = BiliVideoClient.is_f1_related(self._info("F1 Aero Analysis: Floor Edge Wings", tname="知识"))
        assert ok

    def test_non_f1(self):
        ok, _ = BiliVideoClient.is_f1_related(self._info("荣耀WIN2上手体验：游戏手机", tname="数码"))
        assert not ok

    def test_non_f1_car(self):
        ok, _ = BiliVideoClient.is_f1_related(self._info("家用轿车保养全攻略", tname="汽车"))
        assert not ok

    def test_ascii_alias_word_boundary(self):
        # “kimi”短别名不得命中“Kimi AI 助手”类无关视频（2026-10-23 review 修复）
        ok, _ = BiliVideoClient.is_f1_related(self._info("Kimi AI 助手深度体验", tname="科技"))
        assert not ok
        # 但 kimi 作为独立词（Kimi Antonelli 讨论）应命中
        ok2, _ = BiliVideoClient.is_f1_related(self._info("Kimi Antonelli 新秀赛季表现"))
        assert ok2

    def test_f1_short_keyword_survives(self):
        # f1 仅 2 字符，词边界+白名单豁免后仍须命中
        ok, _ = BiliVideoClient.is_f1_related(self._info("这场 F1 太精彩了"))
        assert ok
        ok2, _ = BiliVideoClient.is_f1_related(self._info("尼康F100胶片机评测"))
        assert not ok2

    def test_gate_reads_tags(self):
        """2026-10-30 误拦回归：标题/简介无 F1 关键词，tag 里是分站名+车手名
        （UP 主为平台推流只打具体 tag）→ 必须放行"""
        ok, hits = BiliVideoClient.is_f1_related(self._info(
            "太刺激了！最后一圈绝杀", desc="新赛季最精彩一战",
            tags="日本大奖赛 维斯塔潘 铃鹿 赛车"))
        assert ok, f"tag 中的分站名/车手名/赛道名未命中门禁: {hits}"

    def test_gate_tags_no_false_positive(self):
        """tag 全无关仍然拦截（防门禁被 tag 通道放水）"""
        ok, _ = BiliVideoClient.is_f1_related(self._info(
            "家用轿车保养全攻略", tname="汽车", tags="汽车 保养 机油"))
        assert not ok
