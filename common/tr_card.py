# -*- coding: utf-8 -*-
"""F1 转播风格 Team Radio 静态卡片渲染（/tr 指令 DIY）。

clean-room 说明：wertewrt/F1_team_radio 无开源许可证（保留所有权利），其代码、
重建字形、字体、队标图集均不可复制。本实现仅按其 README 公开记录的视觉规范
（卡片 329:176、九区块无缝波形横贯底边、姓名右上队色+RADIO 白色、号码左下、
队标右下、斜向暗纹、对话模式下文字区在卡片下方）独立实现；素材用本项目自有
头像/队标（web/static/avatars/），字体用系统 CJK/DejaVu（不内置 F1 专有字体）。
"""
import hashlib
import logging
import os
import random
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# 2026 现役车手：姓 key → (号码, 车队键)。队色为公开转播规范色值
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


# 版式/队色/号码表/头像素材刷新（赛季初 prepare_avatars.py 重跑）变更必须递增
# （QQ 图片代理按 URL 长期缓存旧图，同头像 _AVATAR_PATH_VER 铁律）
_TR_CARD_VER = 2
_TR_CARD_KEEP_DAYS = 7  # 自定义引语卡片定期清扫（防变成公开图床无限膨胀）


def resolve_tr_driver(query: str) -> Optional[Tuple[str, str, str]]:
    """车手名解析（drivers_profile 别名+姓名）→ (surname_key, 显示名大写, team_key)。
    档案匹配但不在 2026 现役名单（如周冠宇/代打）继续遍历其余档案"""
    from .drivers_profile import get_drivers
    q = _norm(query)
    if not q:
        return None
    # TR 卡本地别名（不进共享档案：≤3 字符 ASCII 别名会被画像/stint 的子串匹配误命中，
    # 复审实证 "okay"/"karting" 含 "ka"）
    q = {"ka": "kimi"}.get(q, q)
    for key, p in get_drivers().items():
        cand = {_norm(p.get("last_name", "")), _norm(p.get("name_en", "")),
                _norm(p.get("name_cn", "")), _norm(key)}
        cand |= {_norm(a) for a in (p.get("aliases") or [])}
        # 全名子集匹配（"Kimi Antonelli" 命中 "Andrea Kimi Antonelli"）：
        # 查询的每个词都在全名中且包含姓
        name_en_tokens = set(_norm(p.get("name_en", "")).split())
        q_tokens = set(q.split())
        partial = (len(q_tokens) >= 2 and q_tokens <= name_en_tokens
                   and _norm(p.get("last_name", "")) in q_tokens)
        if not (q in cand or partial):
            continue
        surname = _norm(p.get("last_name") or key)
        if surname not in _DRIVERS:
            continue  # 档案外/非现役：继续找，不中断
        return surname, (p.get("last_name") or key).upper(), _DRIVERS[surname][1]
    return None


def _font(size: int):
    """CJK 兼容粗体（family+weight 分开指定；"Noto Sans CJK SC Bold" 这种整体名
    matplotlib 永不匹配——复审实证），PIL truetype"""
    from matplotlib import font_manager
    from PIL import ImageFont
    for fname in ("Noto Sans CJK SC", "Microsoft YaHei", "SimHei"):
        try:
            fp = font_manager.findfont(
                font_manager.FontProperties(family=fname, weight="bold"),
                fallback_to_default=False)
            if fp:
                return ImageFont.truetype(fp, size)
        except Exception:
            continue
    try:
        fp = font_manager.findfont(font_manager.FontProperties(family="DejaVu Sans", weight="bold"))
        return ImageFont.truetype(fp, size)
    except Exception:
        logger.warning("[TR卡] 字体全部缺失，回退位图字体")
        return ImageFont.load_default()


def render_tr_card(driver_query: str, quote: str) -> Tuple[Optional[str], str]:
    """渲染 TR 卡片 PNG（转播规范 clean-room 实现）。Returns (path|None, 错误提示)"""
    resolved = resolve_tr_driver(driver_query)
    if not resolved:
        return None, f"未识别车手「{driver_query}」（支持中文名/英文名/别名，限 2026 现役）"
    surname, display_name, team = resolved
    quote = (quote or "").strip() or "BOX. BOX."
    quote = quote.upper()
    number, _ = _DRIVERS[surname]
    color = TEAM_COLORS[team]

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None, "Pillow 不可用"

    # 规范比例：卡片 329:176；引语区按内容高度自适应（参照其对话模式"只展开到
    # 当前内容需要的高度"），不再固定 276 高度
    CARD_W, CARD_H = 1316, 704
    # 先算引语换行（需要 draw 上下文，用临时画布量宽度）
    from PIL import Image, ImageDraw
    quote_lines = []
    _probe = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    f_quote = _font(64)
    _asc, _desc = f_quote.getmetrics()
    line_h = _asc + _desc + 8  # CJK 字形高于 size（复审实证 Noto 1.45em），动态行距
    max_w = CARD_W - 120

    def _push_hard_wrap(text: str):
        """无空格长词（CJK 整句）按字贪心换行，防水平溢出裁切"""
        buf = ""
        for ch in text:
            t = buf + ch
            if _probe.textlength(t, font=f_quote) > max_w and buf:
                quote_lines.append(buf)
                buf = ch
            else:
                buf = t
        if buf:
            quote_lines.append(buf)

    words, cur = quote.split(), ""
    for w in words:
        t = (cur + " " + w).strip()
        if _probe.textlength(t, font=f_quote) > max_w:
            if cur:
                quote_lines.append(cur)
                cur = w
            else:
                _push_hard_wrap(w)  # 单词本身超宽（CJK 无空格整句）
                cur = ""
        else:
            cur = t
    if cur:
        quote_lines.append(cur)
    if len(quote_lines) > 12:
        quote_lines = quote_lines[:12]
        quote_lines[-1] += " …"  # 截断可视标记
    TEXT_H = 40 + line_h * len(quote_lines) + 40
    W, H = CARD_W, CARD_H + TEXT_H
    img = Image.new("RGB", (W, H), "#0b0d12")
    dr = ImageDraw.Draw(img)

    # ---- 卡片区 ----
    dr.rectangle([0, 0, CARD_W, CARD_H], fill="#14161c")
    # 斜向暗纹（45° 细斜条带）
    for x in range(-CARD_H, CARD_W, 30):
        dr.line([(x, 0), (x + CARD_H, CARD_H)], fill="#1b1f29", width=8)

    # 右上：姓名（队色）+ RADIO（白）——右上对齐
    f_name = _font(58)
    f_radio = _font(34)
    nw = dr.textlength(display_name, font=f_name)
    dr.text((CARD_W - 48 - nw, 44), display_name, font=f_name, fill=color)
    rw = dr.textlength("RADIO", font=f_radio)
    dr.text((CARD_W - 48 - rw, 122), "RADIO", font=f_radio, fill="#ffffff")

    # 左侧头像圆（规范外增益：利用卡片左侧空白，转播原卡无头像但我们用户爱看脸）
    avatar_path = os.path.join(_STATIC, "drivers", f"{surname}.png")
    if os.path.exists(avatar_path):
        try:
            with Image.open(avatar_path) as _im:
                av = _im.convert("RGBA").resize((220, 220))
            mask = Image.new("L", (220, 220), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, 220, 220), fill=255)
            img.paste(av, (44, 60), mask)
            dr.ellipse((44, 60, 264, 280), outline=color, width=6)
        except Exception as e:
            logger.debug(f"[TR卡] 头像加载失败 {surname}: {e}")

    # 左下大号码（队色填充+细白边）
    f_num = _font(210)
    nx, ny = 44, CARD_H - 268
    for dx, dy in ((-3, 0), (3, 0), (0, -3), (0, 3)):
        dr.text((nx + dx, ny + dy), number, font=f_num, fill="#ffffff")
    dr.text((nx, ny), number, font=f_num, fill=color)

    # 右下队标（深色系队标加白色圆底衬底，2026-10-28 用户实证梅奔深青描边压深底不可读）
    logo_path = os.path.join(_STATIC, "teams", f"{team}.png")
    if os.path.exists(logo_path):
        try:
            with Image.open(logo_path) as _im:
                logo = _im.convert("RGBA").copy()
            ratio = 150 / max(logo.size)
            logo = logo.resize((int(logo.size[0] * ratio), int(logo.size[1] * ratio)))
            lx, ly = CARD_W - 48 - logo.size[0], CARD_H - 148 - logo.size[1]
            if team in {"mercedes", "aston_martin"}:  # 仅低对比度队标加白底（白色标加白底反消失）
                pad = 14
                dr.ellipse([lx - pad, ly - pad, lx + logo.size[0] + pad, ly + logo.size[1] + pad],
                           fill="#f5f6f8")
            img.paste(logo, (lx, ly), logo)
        except Exception as e:
            logger.debug(f"[TR卡] 队标加载失败 {team}: {e}")

    # 底部九区块波形：无缝隙、横贯整个底边（含号码与队标下方）、中间高两端低、
    # 左右不对称（左肩略宽、右侧回落更快）、最外侧保留小突起
    rng = random.Random(int(hashlib.md5(quote.encode()).hexdigest()[:8], 16))
    base_y = CARD_H
    bw = CARD_W / 9
    for i in range(9):
        center_boost = 1.0 - abs(i - 4) / 4.5 * (0.55 if i < 4 else 0.75)
        h = int((26 + rng.random() * 96) * center_boost) + 6
        dr.rectangle([int(i * bw), base_y - h, int((i + 1) * bw), base_y], fill=color)

    # ---- 下方引语区（对话模式：车手发言=队色靠右）----
    qy = CARD_H + 40
    for ln in quote_lines:
        lw = dr.textlength(ln, font=f_quote)
        dr.text((W - 60 - lw, qy), ln, font=f_quote, fill=color)  # 靠右（车手发言）
        qy += line_h

    from .ratings_store import EXPORTS_DIR
    os.makedirs(EXPORTS_DIR, exist_ok=True)
    # 定期清扫：自定义引语卡片超期删除（防无限膨胀/变相公开图床）
    try:
        import time as _t
        cutoff = _t.time() - _TR_CARD_KEEP_DAYS * 86400
        for f in os.listdir(EXPORTS_DIR):
            fp = os.path.join(EXPORTS_DIR, f)
            if f.startswith(("tr_card_", "tr_studio_")) and os.path.getmtime(fp) < cutoff:
                os.remove(fp)
    except Exception:
        pass
    fname = f"tr_card_v{_TR_CARD_VER}_{surname}_{hashlib.md5(quote.encode()).hexdigest()[:8]}.png"
    path = os.path.join(EXPORTS_DIR, fname)
    # 原子写+并发同名竞态：tmp 名带 uuid（复审实证同引语并发 replace 撞车）
    import uuid as _uuid
    tmp_path = f"{path}.{os.getpid()}.{_uuid.uuid4().hex[:6]}.tmp.png"
    img.save(tmp_path)
    os.replace(tmp_path, path)
    return path, ""
