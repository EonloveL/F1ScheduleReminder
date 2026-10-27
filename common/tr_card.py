# -*- coding: utf-8 -*-
"""F1 转播风格 Team Radio 静态卡片渲染（/tr 指令 DIY）。

视觉参照 wertewrt/F1_team_radio 项目的转播卡片重建规范（329:176 卡片、队色、
九区块波形），自研 PIL 实现静态版（QQ 群图片链路不支持视频）。
素材复用项目打包头像/队标（web/static/avatars/，Caddy 同源分发）。
"""
import hashlib
import logging
import os
import random
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# 2026 现役车手：姓 key → (号码, 车队键)。队色取自该项目 README 的预设表
_DRIVERS = {
    "russell": ("63", "mercedes"), "antonelli": ("12", "mercedes"),
    "leclerc": ("16", "ferrari"), "hamilton": ("44", "ferrari"),
    "norris": ("1", "mclaren"), "piastri": ("81", "mclaren"),
    "verstappen": ("3", "red_bull"), "hadjar": ("6", "red_bull"),
    "lawson": ("30", "racing_bulls"), "lindblad": ("41", "racing_bulls"),
    "tsunoda": ("22", "racing_bulls"),
    "gasly": ("10", "alpine"), "colapinto": ("43", "alpine"),
    "ocon": ("31", "haas"), "bearman": ("87", "haas"),
    "hulkenberg": ("27", "audi"), "bortoleto": ("5", "audi"),
    "albon": ("23", "williams"), "sainz": ("55", "williams"),
    "alonso": ("14", "aston_martin"), "stroll": ("18", "aston_martin"),
    "perez": ("11", "cadillac"), "bottas": ("77", "cadillac"),
}

TEAM_COLORS = {
    "mercedes": "#26E7C8", "ferrari": "#ED123F", "mclaren": "#FF8A00",
    "red_bull": "#3876CD", "racing_bulls": "#80A2FF", "alpine": "#00A7DF",
    "haas": "#E8E9EB", "audi": "#FF3C14", "williams": "#246DCB",
    "aston_martin": "#239D7D", "cadillac": "#B8BAC0",
}

_STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "web", "static", "avatars")


def _norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "")
    # 去组合符（Pérez→perez 重音折叠）但保留 CJK（不能用 ascii ignore，中文会被吞）
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def resolve_tr_driver(query: str) -> Optional[Tuple[str, str, str]]:
    """车手名解析（drivers_profile 别名+姓名）→ (surname_key, 显示名大写, team_key)"""
    from .drivers_profile import get_drivers
    q = _norm(query)
    if not q:
        return None
    for key, p in get_drivers().items():
        cand = {_norm(p.get("last_name", "")), _norm(p.get("name_en", "")),
                _norm(p.get("name_cn", "")), _norm(key)}
        cand |= {_norm(a) for a in (p.get("aliases") or [])}
        if q in cand:
            surname = _norm(p.get("last_name") or key)
            if surname not in _DRIVERS:
                return None
            return surname, (p.get("last_name") or key).upper(), _DRIVERS[surname][1]
    return None


def _font(size: int, bold: bool = True):
    """CJK 兼容字体（与 chart_factory 同优先级），PIL truetype"""
    from matplotlib import font_manager
    from PIL import ImageFont
    for fname in ("Noto Sans CJK SC", "Noto Sans CJK", "Microsoft YaHei", "SimHei"):
        try:
            fp = font_manager.findfont(font_manager.FontProperties(family=fname),
                                       fallback_to_default=False)
            if fp:
                return ImageFont.truetype(fp, size)
        except Exception:
            continue
    return ImageFont.load_default()


def render_tr_card(driver_query: str, quote: str) -> Tuple[Optional[str], str]:
    """渲染 TR 卡片 PNG。Returns (path|None, 错误提示)"""
    resolved = resolve_tr_driver(driver_query)
    if not resolved:
        return None, f"未识别车手「{driver_query}」（支持中文名/英文名/别名）"
    surname, display_name, team = resolved
    quote = (quote or "").strip() or "BOX. BOX."
    quote = quote.upper()
    number, _ = _DRIVERS[surname]
    color = TEAM_COLORS[team]

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None, "Pillow 不可用"

    W, H = 1312, 704  # 329:176 卡片 ×4
    img = Image.new("RGB", (W, H), "#101218")
    dr = ImageDraw.Draw(img)

    # 斜向暗纹（45° 细线）
    for x in range(-H, W, 36):
        dr.line([(x, 0), (x + H, H)], fill="#181b24", width=10)

    # 顶部队色斜切条
    dr.polygon([(0, 0), (int(W * 0.34), 0), (int(W * 0.26), 56), (0, 56)], fill=color)

    # 右上：姓名（队色）+ RADIO（白）
    f_name = _font(64)
    f_radio = _font(40, bold=False)
    nw = dr.textlength(display_name, font=f_name)
    dr.text((W - 60 - nw, 60), display_name, font=f_name, fill=color)
    rw = dr.textlength("RADIO", font=f_radio)
    dr.text((W - 60 - rw, 148), "RADIO", font=f_radio, fill="#ffffff")

    # 左侧头像圆
    avatar_path = os.path.join(_STATIC, "drivers", f"{surname}.png")
    if os.path.exists(avatar_path):
        try:
            av = Image.open(avatar_path).convert("RGBA").resize((240, 240))
            mask = Image.new("L", (240, 240), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, 240, 240), fill=255)
            img.paste(av, (60, 110), mask)
        except Exception as e:
            logger.debug(f"[TR卡] 头像加载失败 {surname}: {e}")

    # 中央引语（自动换行，卡片中部）
    f_quote = _font(56)
    max_w = W - 380 - 80
    words, lines, cur = quote.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if dr.textlength(t, font=f_quote) > max_w and cur:
            lines.append(cur)
            cur = w
        else:
            cur = t
    if cur:
        lines.append(cur)
    lines = lines[:4]
    qy = 300
    for ln in lines:
        dr.text((340, qy), ln, font=f_quote, fill="#ffffff")
        qy += 68

    # 左下大号码（队色+细白边）
    f_num = _font(180)
    nx, ny = 60, H - 250
    for dx, dy in ((-2, 0), (2, 0), (0, -2), (0, 2)):
        dr.text((nx + dx, ny + dy), number, font=f_num, fill="#ffffff")
    dr.text((nx, ny), number, font=f_num, fill=color)

    # 右下队标
    logo_path = os.path.join(_STATIC, "teams", f"{team}.png")
    if os.path.exists(logo_path):
        try:
            logo = Image.open(logo_path).convert("RGBA")
            ratio = 150 / max(logo.size)
            logo = logo.resize((int(logo.size[0] * ratio), int(logo.size[1] * ratio)))
            img.paste(logo, (W - 60 - logo.size[0], H - 60 - logo.size[1]), logo)
        except Exception as e:
            logger.debug(f"[TR卡] 队标加载失败 {team}: {e}")

    # 底部九区块波形（伪波形：引语内容哈希做种子，静态确定）
    rng = random.Random(int(hashlib.md5(quote.encode()).hexdigest()[:8], 16))
    bw, gap, base_y = (W - 120) / 9, 0, H - 40
    for i in range(9):
        center_boost = 1.0 - abs(i - 4) / 4.5 * 0.7  # 中间高两端低
        h = int((30 + rng.random() * 90) * center_boost) + 8
        x0 = 60 + i * bw
        dr.rectangle([x0, base_y - h, x0 + bw - 10, base_y], fill=color)

    from .ratings_store import EXPORTS_DIR
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    fname = f"tr_card_{surname}_{hashlib.md5(quote.encode()).hexdigest()[:8]}.png"
    path = os.path.join(EXPORTS_DIR, fname)
    img.save(path)
    return path, ""
