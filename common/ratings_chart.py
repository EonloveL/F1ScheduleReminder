"""
F1车手评分 - 统计图表生成（matplotlib）
生成平均分柱状图PNG
"""

import logging
import os
from typing import Dict, List, Any, Optional

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager

logger = logging.getLogger(__name__)

EXPORTS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "exports"
)


def _pick_cjk_font():
    """选择一个可用的中文字体"""
    candidates = ["Microsoft YaHei", "SimHei", "Noto Sans CJK SC", "WenQuanYi Zen Hei", "DejaVu Sans"]
    installed = {f.name for f in font_manager.fontManager.ttflist}
    for name in candidates:
        if name in installed:
            return name
    return None


def render_ratings_chart(race_name: str, board: List[Dict[str, Any]],
                         driver_names: Dict[str, str], dotd: Optional[str] = None,
                         race_key: str = "latest") -> Optional[str]:
    """
    生成评分柱状图

    Args:
        race_name: 比赛名称
        board: aggregate() 结果 [{"driver_id","avg","count"}]
        driver_names: {driver_id: 显示名}
        dotd: 最佳车手driver_id（高亮）

    Returns:
        PNG文件路径，失败None
    """
    if not board:
        return None

    try:
        os.makedirs(EXPORTS_DIR, exist_ok=True)

        font = _pick_cjk_font()
        if font:
            plt.rcParams["font.family"] = font
        plt.rcParams["axes.unicode_minus"] = False

        labels = [driver_names.get(r["driver_id"], r["driver_id"]) for r in board]
        avgs = [r["avg"] for r in board]
        counts = [r["count"] for r in board]
        colors = ["#e10600" if r["driver_id"] == dotd else "#3d5a80" for r in board]

        fig_h = max(4, 0.45 * len(board) + 2)
        fig, ax = plt.subplots(figsize=(9, fig_h), dpi=150)
        fig.patch.set_facecolor("#0f1115")
        ax.set_facecolor("#0f1115")

        bars = ax.barh(range(len(board)), avgs, color=colors, height=0.62)
        ax.set_yticks(range(len(board)))
        ax.set_yticklabels(labels, color="#eee", fontsize=10)
        ax.invert_yaxis()
        ax.set_xlim(0, 10)
        ax.set_xlabel("平均分", color="#ccc")
        ax.tick_params(axis="x", colors="#888")
        for spine in ax.spines.values():
            spine.set_visible(False)

        for i, (bar, avg, cnt) in enumerate(zip(bars, avgs, counts)):
            ax.text(avg + 0.08, i, f"{avg} ({cnt}票)", va="center", color="#ffd166", fontsize=9)

        title = f"{race_name} · 群友评分"
        if dotd:
            title += f"  |  DOTD: {driver_names.get(dotd, dotd)}"
        ax.set_title(title, color="#fff", fontsize=13, pad=12)

        plt.tight_layout()
        safe_key = "".join(c if c.isalnum() or c in "-_" else "_" for c in race_key)
        path = os.path.join(EXPORTS_DIR, f"ratings_chart_{safe_key}.png")
        fig.savefig(path, facecolor=fig.get_facecolor(), bbox_inches="tight")
        plt.close(fig)
        logger.info(f"评分图表已生成: {path}")
        return path
    except Exception as e:
        logger.error(f"生成评分图表失败: {e}")
        return None
