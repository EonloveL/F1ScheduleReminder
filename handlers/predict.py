# -*- coding: utf-8 -*-
"""指令处理：预测/Team Radio
（/predict /tr——从 main.py 指令分发链原样迁移，行为零变化）"""
import logging
import os

logger = logging.getLogger(__name__)


async def handle_predict(ctx, message, arg, member_openid, is_dm):
    # 统计模型预测（直接调模型，不走LLM；排位+正赛名次+概率）
    import asyncio as _aio
    loop = _aio.get_running_loop()

    def _fetch_prediction():
        from common.prediction_model import RacePredictionModel
        pm = RacePredictionModel(ctx.f1_api, ctx.f1cosmos)
        return pm.predict(arg or "", season=ctx.season)

    await ctx.reply_text(message, "📊 正在运行统计预测模型（名次制回归+蒙特卡洛）…")
    r = await loop.run_in_executor(None, _fetch_prediction)
    if not r or "error" in r:
        await ctx.reply_text(message, f"预测失败：{(r or {}).get('error', '未知错误')}")
        return
    # 渲染卡片：正赛前10 + 排位前6 + 回测基准
    meta = r.get("model_meta", {})
    bt = meta.get("backtest") or {}
    text = f"🔮 【{r['gp']} 预测】（统计模型 v{meta.get('version')}）\n\n"
    md = f"## 🔮 {r['gp']} 预测\r\r> 统计模型：名次制回归 + 残差蒙特卡洛\r> 回测基准（2025留一法）：领奖台命中 {bt.get('podium_hits_per_race')}/场 · 名次MAE {bt.get('mean_abs_pos_error')}\r\r"
    md += "**正赛预测**\r"
    text += "正赛预测：\n"
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    for p in r["predicted_race"][:10]:
        pen = f"（发车P{p['grid']}，罚退+{p['penalty']}）" if p.get("penalty") else (f"（发车P{p['grid']}）" if p["grid"] != p["pos"] else "")
        line = f"{medals.get(p['pos'], 'P' + str(p['pos']))} {p['driver']}（{p['team']}）{pen}"
        text += line + "\n"
        md += line + "\r"
    q_header = ("**排位（已结束，真实成绩）**" if r.get("qualifying_actual")
                else "**排位预测（含Top3概率）**")
    md += f"\r{q_header}\r"
    text += "\n排位预测（Top3概率）：\n"
    for p in r["predicted_qualifying"][:6]:
        if r.get("qualifying_actual"):
            line = f"P{p['pos']} {p['driver']}（{p['team']}）"
        else:
            line = f"P{p['pos']} {p['driver']}（{p['team']}）{p['quali_top3_prob']*100:.0f}%"
        text += line + "\n"
        md += line + "\r"
    note = ("⚠️ 统计学口径：名次 = 名次制线性回归的数学期望；"
            "Top3/前十概率 = 1000次蒙特卡洛残差采样频率；"
            "回测基准为2025赛季留一法。安全车/事故/机械故障不可预测")
    # 高 SC 赛道警示（sc_warning 由模型结果生成：概率放宽A1 + 顺位阻尼A2 口径说明）
    if r.get("sc_warning"):
        note += "\n" + r["sc_warning"]
    text += "\n" + note
    md += f"\r\r{note}"
    await ctx.reply_md(message, text, md)


async def handle_tr(ctx, message, arg, member_openid, is_dm):
    # Team Radio 卡片：配置了 TR_STUDIO_URL（分离的高保真工作室服务）时
    # 服务端代生成（文本→工作室对话模式 PNG）发图，工作室不可达降级内置卡
    studio_url = os.getenv("TR_STUDIO_URL", "").strip().rstrip("/")
    parts = (arg or "").strip().split(None, 1)
    if studio_url and len(parts) < 2:
        await ctx.reply_text(message,
            "📻 Team Radio 工作室（完整版：语音驱动波形/入场动画/导出视频）\n"
            f"{studio_url}\n\n"
            "快速静态卡直接用: /tr 车手 引语（如 /tr 维斯塔潘 simply lovely）")
        return
    if len(parts) < 2:
        await ctx.reply_text(message, "用法: /tr 车手 引语\n如: /tr 维斯塔潘 simply lovely\n/tr kimi check the car")
        return
    import asyncio as _aio
    import os as _os
    loop = _aio.get_running_loop()
    dq, quote = parts
    from common.tr_card import render_tr_card, resolve_tr_driver
    path, err = None, ""
    fallback_note = ""
    if studio_url:
        resolved = await loop.run_in_executor(None, resolve_tr_driver, dq)
        if not resolved:
            await ctx.reply_text(message, f"⚠️ 未识别车手「{dq}」（支持中文名/英文名/别名，限 2026 现役）")
            return
        surname = resolved[0]
        def _studio_render():
            import hashlib as _hl
            import uuid as _uu
            import requests as _rq
            from common.ratings_store import EXPORTS_DIR as _EXP
            _os.makedirs(_EXP, exist_ok=True)
            r = _rq.post(f"{studio_url}/text",
                         data={"driver": surname, "text": quote},
                         headers={"X-TR-Token": os.getenv("TR_STUDIO_TOKEN", "")},
                         timeout=(10, 120),
                         proxies={"http": None, "https": None})
            if r.status_code != 200:
                raise RuntimeError(f"工作室 HTTP {r.status_code}")
            fname = f"tr_studio_v1_{surname}_{_hl.md5(quote.encode()).hexdigest()[:8]}.png"
            p = _os.path.join(_EXP, fname)
            # 并发同名竞态：tmp 名带 uuid（与 tr_card 同款事故范式）
            tmp = f"{p}.{os.getpid()}.{_uu.uuid4().hex[:6]}.tmp"
            with open(tmp, "wb") as f:
                f.write(r.content)
            _os.replace(tmp, p)
            return p
        try:
            path = await loop.run_in_executor(None, _studio_render)
        except Exception as _se:
            logger.warning(f"TR工作室渲染失败，降级内置卡: {_se}")
            fallback_note = "（工作室暂不可用，已用简化版）"
    if not path:
        path, err = await loop.run_in_executor(None, render_tr_card, dq, quote)
        if not path:
            await ctx.reply_text(message, f"⚠️ {err}")
            return
    await ctx.reply_image(message,
                          f"{ctx.rating_base_url}/exports/{_os.path.basename(path)}",
                          content=f"📻 {dq} 的 Team Radio 卡{fallback_note}")


HANDLERS = {
    "predict": handle_predict,
    "tr": handle_tr,
}
