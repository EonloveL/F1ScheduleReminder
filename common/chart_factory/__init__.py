"""
F1赛程提醒机器人 - 统计图表工厂（包入口）

保持与原 common/chart_factory.py 相同的公共 API，所有 import 路径不变。
"""
from .core import (
    DATA_DIR,
    EXPORTS_DIR,
    COMPOUND_COLORS,
    COMPOUND_CN,
    OUTLIER_RATIO,
    MIN_VALID_LAPS,
    CHART_VER,
)
from .strategy import render_strategy_chart
from .positions import render_positions_chart
from .race_report import render_race_report_chart
from .quali_gap import render_quali_gap_chart
from .points_trend import render_points_trend_chart
from .pace import render_pace_chart
from .pitstop import render_pitstop_chart
from .sc_timeline import get_sc_events, render_sc_timeline
from .destructors import render_destructors_chart
from .team_radar import render_team_strength_radar
from .weather import render_weather_chart
from .pu_usage import render_pu_usage_chart

__all__ = [
    "DATA_DIR",
    "EXPORTS_DIR",
    "COMPOUND_COLORS",
    "COMPOUND_CN",
    "OUTLIER_RATIO",
    "MIN_VALID_LAPS",
    "CHART_VER",
    "render_strategy_chart",
    "render_positions_chart",
    "render_race_report_chart",
    "render_quali_gap_chart",
    "render_points_trend_chart",
    "render_pace_chart",
    "render_pitstop_chart",
    "get_sc_events",
    "render_sc_timeline",
    "render_destructors_chart",
    "render_team_strength_radar",
    "render_weather_chart",
    "render_pu_usage_chart",
]
