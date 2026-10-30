# -*- coding: utf-8 -*-
"""TR 卡片（/tr 指令）离线单测"""
import os

from common.tr_card import render_tr_card, resolve_tr_driver, _DRIVERS


class TestResolve:
    def test_chinese_name(self):
        assert resolve_tr_driver("维斯塔潘") == ("verstappen", "VERSTAPPEN", "red_bull")

    def test_alias(self):
        assert resolve_tr_driver("老汉")[0] == "hamilton"
        assert resolve_tr_driver("潘子")[0] == "verstappen"

    def test_english(self):
        assert resolve_tr_driver("Kimi Antonelli")[0] == "antonelli"

    def test_accent_fold(self):
        assert resolve_tr_driver("Pérez")[0] == "perez"

    def test_unknown(self):
        assert resolve_tr_driver("不存在的车手") is None

    def test_off_roster_rejected(self):
        # 周冠宇（不在 2026 现役表）解析到档案但不在卡片表 → None
        assert resolve_tr_driver("周冠宇") is None


class TestRender:
    def test_render_ok(self):
        path, err = render_tr_card("维斯塔潘", "SIMPLY LOVELY")
        assert path and os.path.exists(path) and not err
        os.remove(path)

    def test_render_unknown(self):
        path, err = render_tr_card("某某", "hello")
        assert path is None and err

    def test_cjk_quote(self):
        path, err = render_tr_card("kimi", "车队指令是什么鬼")
        assert path and os.path.exists(path)
        os.remove(path)

    def test_all_drivers_render(self):
        for surname in _DRIVERS:
            path, err = render_tr_card(surname, "BOX BOX")
            assert path, f"{surname}: {err}"
            os.remove(path)
