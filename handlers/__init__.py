# -*- coding: utf-8 -*-
"""指令处理器注册表（main.py 的 elif 分发链拆分为 handlers 包）。

- HandlerContext：指令处理共享上下文（数据层对象 + main.py 配置常量 + 可变状态 +
  回复函数代理）。回复函数由 main.py 注入模块级代理，保证外部替换
  main.reply_*（如测试记录器）时全链路仍走替换实现。
- COMMAND_HANDLERS：{指令名(不带斜杠): async handler}；main.py 查表分发，
  未命中走未知指令兜底（difflib 近似建议在 main）
"""
import logging

logger = logging.getLogger(__name__)


class HandlerContext:
    """指令处理上下文（属性式容器，字段由 create_command_client 装配）。

    字段：f1_api, prefs_store, llm, ratings_store, qq_bot, f1cosmos,
    season, session_arg_aliases, admin_openids,
    rating_base_url, rating_enabled, live_base_url, f1cosmos_dash_base,
    help_text, help_markdown, help_keyboard, watch_links_text, watch_links_md,
    bili_login_state（可变状态，/bili_login 防重入，语义与原闭包一致——传引用）,
    get_driver_names_for_race（callable race -> {driver_id: name}）,
    reply_text / reply_md / reply_image（main 模块级代理）
    """

    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def __repr__(self):
        return f"HandlerContext({', '.join(sorted(self.__dict__))})"


# handlers 始终以包形式被导入（qq_group_official_version.handlers 或顶层 handlers），
# 子模块内部自行处理 main.py 脚本直跑模式的导入回退（如 misc 的 ..replies → replies）
from . import schedule, standings, charts, upgrades, prefs, predict, admin, misc

COMMAND_HANDLERS = {}
for _m in (schedule, standings, charts, upgrades, prefs, predict, admin, misc):
    COMMAND_HANDLERS.update(_m.HANDLERS)
