# -*- coding: utf-8 -*-
"""指令处理：管理员
（/models /model /llm /kb /bili_login——从 main.py 指令分发链原样迁移，行为零变化）"""
import logging

logger = logging.getLogger(__name__)


async def _handle_model_impl(ctx, message, arg, member_openid, cmd):
    # LLM 分层模型配置（管理员）：/models [refresh] 查看 · /model set <层> <供应商/模型> · /model reset [层]
    if not ctx.admin_openids:
        await ctx.reply_text(message, "💡 未配置 ADMIN_OPENIDS")
    elif member_openid not in ctx.admin_openids:
        await ctx.reply_text(message, "⛔ 该指令仅限管理员使用")
    elif not ctx.llm or not ctx.llm.enabled:
        await ctx.reply_text(message, "💡 LLM 未启用（未配置任何供应商 KEY）")
    else:
        llm = ctx.llm
        from common.llm_prefs import (all_layers as _lp_all, set_layer as _lp_set,
                                      reset_layer as _lp_reset, LAYERS as _LP_LAYERS,
                                      LAYER_NAMES_CN as _LP_CN)
        a = (arg or "").strip()
        import asyncio as _aio
        loop = _aio.get_running_loop()

        def _layer_defaults():
            d_l1 = next((p for p in llm.ask_order if not llm.available[p]["web_search"]), None)
            d_l2 = next((p for p in llm.ask_order if llm.available[p]["web_search"]), None)
            return {"l0": d_l1, "l1": d_l1, "l2": d_l2, "deep": d_l2,
                    "vision": "kimi", "memory": d_l1}

        if cmd == "/models" or not a:
            force = "refresh" in a
            if force:
                await ctx.reply_text(message, "🔄 正在向各供应商拉取模型清单…")
            allm = await loop.run_in_executor(None, lambda: llm.list_all_models(force=force))
            prefs = _lp_all()
            defaults = _layer_defaults()
            lines = ["## 🤖 LLM 模型配置\r", "**当前生效**（层：供应商/模型，*=管理员覆盖）\r"]
            for layer in _LP_LAYERS:
                prov, mdl = llm._resolve_layer(layer, defaults[layer])
                mdl = mdl or (llm.available.get(prov) or {}).get("model", "?")
                mark = "*" if layer in prefs else ""
                lines.append(f"{_LP_CN[layer]}（{layer}）: {prov}/{mdl}{mark}\r")
            lines.append("\r**各供应商可用模型**\r")
            for prov, info in allm.items():
                ms = info.get("models") or []
                shown = "、".join(ms[:12]) + (f" 等{len(ms)}个" if len(ms) > 12 else "")
                lines.append(f"{prov}（当前 {info['current']}{'·联网' if info.get('web_search') else ''}）: {shown or '清单拉取失败'}\r")
            lines.append("\r用法: `/model set l2 kimi/kimi-k3` · `/model reset l2` · `/models refresh`")
            await ctx.reply_md(message, "\n".join(lines).replace("\r", "\n"),
                               "".join(lines))
        elif a.startswith("set "):
            parts = a[4:].split()
            if len(parts) != 2 or "/" not in parts[1]:
                await ctx.reply_text(message, "用法: /model set <层> <供应商/模型>\n层: l0/l1/l2/deep/vision/memory，如 /model set l2 kimi/kimi-k3")
                return
            layer, pm = parts
            prov, mdl = pm.split("/", 1)
            if layer not in _LP_LAYERS:
                await ctx.reply_text(message, f"未知层「{layer}」，可选: {'/'.join(_LP_LAYERS)}")
                return
            if prov not in llm.available:
                await ctx.reply_text(message, f"供应商「{prov}」未配置 KEY，当前可用: {', '.join(llm.available)}")
                return
            known = await loop.run_in_executor(None, lambda: llm.list_provider_models(prov))
            warns = []
            if known and mdl not in known:
                warns.append(f"模型「{mdl}」不在 {prov} 已拉到的清单中（可能是清单陈旧或键入错误）")
            if layer == "vision" and prov != "kimi":
                warns.append("该供应商模型需支持图片输入（base64 image_url），不支持的模型识图会失败")
            if layer == "l2" and not llm.available[prov]["web_search"]:
                warns.append("该供应商无联网搜索能力，L2 切到它后需要核实的回答将不再联网")
            if layer == "l1" and prov == "kimi":
                warns.append("kimi-k2.x/k3 强制 temperature=1，L1 已内置自动免传重试，首轮会多一次 400 往返")
            _lp_set(layer, prov, mdl)
            tail = ("\n⚠️ " + "；".join(warns)) if warns else ""
            await ctx.reply_text(message, f"✅ 已设置 {_LP_CN[layer]}（{layer}）→ {prov}/{mdl}{tail}\n立即生效，/model reset {layer} 恢复默认")
        elif a.startswith("reset"):
            layer = a[5:].strip()
            if layer and layer not in _LP_LAYERS:
                await ctx.reply_text(message, f"未知层「{layer}」，可选: {'/'.join(_LP_LAYERS)}")
                return
            _lp_reset(layer or None)
            await ctx.reply_text(message, f"✅ 已重置{'层「' + layer + '」' if layer else '全部层'}为默认模型")
        else:
            await ctx.reply_text(message, "用法: /models [refresh] 查看 · /model set <层> <供应商/模型> · /model reset [层]")


async def handle_models(ctx, message, arg, member_openid, is_dm):
    await _handle_model_impl(ctx, message, arg, member_openid, "/models")


async def handle_model(ctx, message, arg, member_openid, is_dm):
    await _handle_model_impl(ctx, message, arg, member_openid, "/model")


async def handle_llm(ctx, message, arg, member_openid, is_dm):
    # LLM 运行时配置（管理员私聊专属）：切换服务商 / 配置 API Key / 模型 / base_url
    # 群内一律拒绝：密钥出现在群聊消息记录里即泄露面
    if not is_dm:
        await ctx.reply_text(message, "🔒 /llm 涉及密钥与服务商配置，仅限管理员私聊我操作（群内禁止使用）")
    elif not ctx.admin_openids:
        await ctx.reply_text(message, "💡 未配置 ADMIN_OPENIDS")
    elif member_openid not in ctx.admin_openids:
        await ctx.reply_text(message, "⛔ 该指令仅限管理员使用")
    elif not ctx.llm:
        await ctx.reply_text(message, "❌ LLM 助手初始化失败，请检查日志后重启")
    else:
        from common.llm_admin import handle_llm_admin
        reply = handle_llm_admin(ctx.llm, arg)
        await ctx.reply_md(message, reply.replace("**", ""), reply.replace("\n", "\r"))


async def handle_kb(ctx, message, arg, member_openid, is_dm):
    # 知识库审核（管理员专用）：/kb 看待审 · /kb ok <id> 入库 · /kb no <id> 拒绝
    if not ctx.admin_openids:
        await ctx.reply_text(message, "💡 知识库审核未启用（未配置 ADMIN_OPENIDS）")
    elif member_openid not in ctx.admin_openids:
        await ctx.reply_text(message, "⛔ 该指令仅限管理员使用")
    else:
        from common.knowledge_store import KnowledgeStore
        ks = KnowledgeStore.get()
        a = (arg or "").strip().lower()
        if a.startswith(("ok ", "yes ", "no ")):
            try:
                cid = int(a.split()[1])
            except (IndexError, ValueError):
                await ctx.reply_text(message, "用法: /kb ok <编号> 或 /kb no <编号>")
                return
            approve = not a.startswith("no ")
            c = ks.review(cid, approve, reviewer=member_openid[:8])
            if c:
                verdict = "✅ 已入知识库" if approve else "🗑️ 已拒绝"
                await ctx.reply_text(message, f"{verdict}：候选 #{cid}\n{c['claim'][:100]}")
            else:
                await ctx.reply_text(message, f"⚠️ 候选 #{cid} 不存在或不处于待审核状态")
        else:
            pending = ks.pending_candidates()
            if not pending:
                await ctx.reply_text(message, "📚 暂无待审核的知识候选")
            else:
                lines = [f"📚 待审核知识候选（{len(pending)} 条）："]
                for c in pending[-10:]:
                    lines.append(f"\n#{c['id']}（{c.get('source','')}·{c.get('ts','')}）\n"
                                 f"{c['claim'][:150]}\n校对依据: {(c.get('evidence') or '无')[:100]}")
                lines.append("\n回复 /kb ok <编号> 入库 · /kb no <编号> 拒绝")
                await ctx.reply_text(message, "\n".join(lines))


async def handle_bili_login(ctx, message, arg, member_openid, is_dm):
    # B站扫码登录（管理员）：解锁字幕/元数据接口（阿里云IP匿名访问被412风控）
    if not ctx.admin_openids:
        await ctx.reply_text(message, "💡 未配置 ADMIN_OPENIDS，扫码登录不可用")
    elif member_openid not in ctx.admin_openids:
        await ctx.reply_text(message, "⛔ 该指令仅限管理员使用")
    elif not ctx.qq_bot:
        await ctx.reply_text(message, "⚠️ QQ推送未就绪")
    else:
        # 防重入：同时只允许一个扫码流程（2026-10-24 实证：连发两次会产生
        # 一张已过期二维码的迟到的"已过期"误报）
        _bili_login_state = ctx.bili_login_state
        if getattr(_bili_login_state, "inflight", False):
            await ctx.reply_text(message, "⏳ 已有一张待扫码的登录二维码，请先完成或等3分钟过期")
            return
        _bili_login_state.inflight = True
        # 外层 try/except（OSS review 修复回灌）：线程启动前任何异常都必须释放
        # inflight 标记，否则 /bili_login 永久卡"已有一张待扫码"只能重启
        try:
            from common import bili_auth as _ba
            qr = _ba.qrcode_generate()
            if not qr:
                _bili_login_state.inflight = False
                await ctx.reply_text(message, "⚠️ 二维码生成失败，请稍后重试")
                return
            import os as _os
            import time as _t
            import threading as _th
            from common.ratings_store import EXPORTS_DIR as _EXP
            fname = f"bili_qr_{int(_t.time())}.png"
            fpath = _os.path.join(_EXP, fname)
            _os.makedirs(_EXP, exist_ok=True)
            if not _ba.qrcode_render_png(qr["url"], fpath):
                _bili_login_state.inflight = False
                await ctx.reply_text(message, "⚠️ 二维码渲染失败（qrcode库未安装，需重建镜像）")
                return
            img_url = f"{ctx.rating_base_url}/exports/{fname}"
            # 二维码私聊发给操作者本人，防群内他人误扫绑定错账号
            ok = ctx.qq_bot.reply_image_url(member_openid, img_url,
                                            content="📱 B站扫码登录（3分钟内有效）",
                                            is_dm=True)
            await ctx.reply_text(message,
                "📱 登录二维码已私聊发你，用B站App扫码确认（3分钟内有效）" if ok
                else f"⚠️ 私聊发图失败，请手动打开（注意保密，3分钟有效）: {img_url}")
        except Exception:
            _bili_login_state.inflight = False
            raise

        def _poll_bili_login():
            try:
                result = _ba.qrcode_poll(qr["qrcode_key"], timeout_sec=180)
                if result:
                    _ba.save_cookie(result["SESSDATA"], {"via": "qrcode"})
                    v = _ba.validate_cookie(result["SESSDATA"])
                    status = (f"登录账号：{v.get('uname', '?')}\r"
                              f"登录态：{'✅' if v.get('login_ok') else '❌'}\r"
                              f"元数据接口（view）：{'✅ 已解锁' if v.get('view_ok') else '❌ 仍被风控'}")
                    ctx.qq_bot.send_dm_markdown(
                        member_openid,
                        f"## ✅ B站扫码登录成功\r\r{status}",
                        fallback_text=f"B站扫码登录成功\n{status}")
                else:
                    ctx.qq_bot.send_dm_message(
                        member_openid, "B站登录二维码已过期或取消，需要时重新发 /bili_login")
            except Exception as e:
                logger.warning(f"[BiliLogin] 后台轮询异常: {e}")
            finally:
                _bili_login_state.inflight = False
                try:
                    _os.remove(fpath)  # 登录二维码用完即删
                except OSError:
                    pass

        _th.Thread(target=_poll_bili_login, daemon=True,
                   name="bili-login-poll").start()


HANDLERS = {
    "models": handle_models,
    "model": handle_model,
    "llm": handle_llm,
    "kb": handle_kb,
    "bili_login": handle_bili_login,
}
