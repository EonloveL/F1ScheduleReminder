# -*- coding: utf-8 -*-
"""LLM 运行时管理指令（管理员私聊专属，QQ/Discord 双平台共享）

指令族（arg 为 /llm 后的文本）：
  /llm                        查看供应商状态（key 一律脱敏显示）
  /llm use <provider>         切换问答首选供应商（立即热生效）
  /llm setkey <provider> <key>   配置/更换该供应商 API Key（运行时，优先于 env）
  /llm setmodel <provider> <model> 更换该供应商默认模型
  /llm seturl <provider> <url>   配置 base_url（openai_compat 等自托管端点用）
  /llm delkey <provider>      删除运行时 key（回退 env；env 也没有则该供应商下线）
  /llm reset                  清空全部运行时覆盖（回退 env 配置）

安全约束：
- 本模块只返回脱敏文本；key 明文只进 data/llm_secrets.json 与 secret_guard 注册表
- 调用方负责：管理员校验 + 私聊通道校验（群聊必须拒绝，防 key 留在群消息记录）
"""
import logging
from typing import Any, Optional

from . import llm_secrets
from .secret_guard import mask_secret

logger = logging.getLogger(__name__)

# 管理员速查卡（启动时 DM 推送给管理员 + /llm help 同款）
ADMIN_QUICKREF = """## 🔧 管理员配置指令速查

**LLM 服务商与密钥（私聊/DM 专属）**
`/llm` 查看供应商状态（key 脱敏）
`/llm use <供应商>` 切换问答首选
`/llm setkey <供应商> <key>` 配置/更换 API Key
`/llm setmodel <供应商> <模型>` 换默认模型
`/llm seturl <供应商> <url>` 自定义端点
`/llm delkey <供应商>` 回退 env · `/llm reset` 清空
供应商: kimi / deepseek / openai_compat / openrouter / gemini / local（本地推理端点）

**分层模型**（QQ 群内可用）
`/models` 查看六层模型 · `/model set l2 kimi/kimi-k3` · `/model reset l2`
层: l0改写/l1取数/l2成文/deep深度/vision识图/**memory记忆总结**（/clear 归档用，配便宜模型或本地模型省成本）

**会话记忆（三层衰减记忆）**
`/memory` 用户自查记忆构成 · `/memory forget <id>` 删除条目
`/clear` = 归档式清空（总结进长期记忆，非物理删除）
本地模型：`/llm setmodel local qwen2.5:7b` + `/llm use local`（需自建 Ollama 等 OpenAI 兼容端点）

**其他**
`/kb` 知识库审核 · `/bili_login` B站登录（QQ 侧）

⚠️ 含 key 的消息发完后请立即撤回/删除"""


def handle_llm_admin(llm, arg: str, lang: str = "zh") -> str:
    """执行 /llm 管理指令，返回回复文本（已脱敏）。llm 为 LLMAssistant 实例"""
    from .llm_assistant import PROVIDERS

    a = (arg or "").strip()
    parts = a.split()

    if not a:
        # 状态总览
        pref = llm_secrets.get_preferred_provider()
        head = (f"当前问答顺序: {' → '.join(llm.ask_order) or '无'}"
                f"{'（运行时指定首选: ' + pref + '）' if pref else ''}")
        lines = [head, ""]
        for st in llm.provider_status():
            key_info = (f"key={st['key_masked']}({st['key_source']})" if st["key_masked"]
                        else "key=未配置")
            active = "✅" if st["active"] else "❌"
            order = "·在问答顺序中" if st["in_order"] else ""
            web = "·联网" if st["web_search"] else ""
            lines.append(f"{active} **{st['name']}** {st['model']} {key_info}{web}{order}")
        lines.append("")
        lines.append("用法: /llm use <供应商> · /llm setkey <供应商> <key> · "
                     "/llm setmodel <供应商> <模型> · /llm seturl <供应商> <url> · "
                     "/llm delkey <供应商> · /llm reset")
        lines.append(f"供应商: {' / '.join(PROVIDERS)}")
        return "\n".join(lines)

    cmd = parts[0].lower()

    if cmd == "help":
        return ADMIN_QUICKREF

    if cmd == "use" and len(parts) == 2:
        prov = parts[1].lower()
        if prov not in PROVIDERS:
            return f"❌ 未知供应商「{prov}」，可选: {' / '.join(PROVIDERS)}"
        llm_secrets.set_preferred_provider(prov)
        llm.reload_runtime()
        if prov not in llm.ask_order:
            return (f"⚠️ 已把 {prov} 记为首选，但它当前无可用 key，未进入问答顺序。"
                    f"先 /llm setkey {prov} <key> 配置后再用")
        return f"✅ 问答首选供应商已切换为 **{prov}**\n当前顺序: {' → '.join(llm.ask_order)}\n立即生效，无需重启"

    if cmd == "setkey" and len(parts) == 3:
        prov, key = parts[1].lower(), parts[2].strip()
        if prov not in PROVIDERS:
            return f"❌ 未知供应商「{prov}」，可选: {' / '.join(PROVIDERS)}"
        if len(key) < 8:
            return "❌ key 长度过短，疑似无效"
        llm_secrets.set_provider_config(prov, api_key=key)
        llm.reload_runtime()
        active = prov in llm.available
        msg = (f"✅ {prov} 的 API Key 已配置（{mask_secret(key)}，运行时存储）\n"
               f"该供应商当前状态: {'✅ 已启用' if active else '❌ 未启用（检查 base_url/模型配置）'}")
        if active and prov not in llm.ask_order:
            msg += f"\n💡 它不在问答顺序中，/llm use {prov} 可切为首选"
        msg += "\n⚠️ 安全提醒：请**撤回/删除**你刚才发送的含 key 消息，避免留在聊天记录中"
        return msg

    if cmd == "setmodel" and len(parts) == 3:
        prov, model = parts[1].lower(), parts[2].strip()
        if prov not in PROVIDERS:
            return f"❌ 未知供应商「{prov}」，可选: {' / '.join(PROVIDERS)}"
        llm_secrets.set_provider_config(prov, model=model)
        llm.reload_runtime()
        return (f"✅ {prov} 默认模型已切换为 `{model}`\n"
                f"💡 分层精细控制用 /model set <层> {prov}/{model}（/models 查看）")

    if cmd == "seturl" and len(parts) == 3:
        prov, url = parts[1].lower(), parts[2].strip()
        if prov not in PROVIDERS:
            return f"❌ 未知供应商「{prov}」，可选: {' / '.join(PROVIDERS)}"
        if not url.startswith(("http://", "https://")):
            return "❌ base_url 必须是 http(s) 地址"
        llm_secrets.set_provider_config(prov, base_url=url)
        llm.reload_runtime()
        return f"✅ {prov} 的 base_url 已配置为 `{url}`"

    if cmd == "delkey" and len(parts) == 2:
        prov = parts[1].lower()
        if prov not in PROVIDERS:
            return f"❌ 未知供应商「{prov}」，可选: {' / '.join(PROVIDERS)}"
        if not llm_secrets.clear_provider(prov):
            return f"ℹ️ {prov} 没有运行时配置，无需删除"
        llm.reload_runtime()
        active = prov in llm.available
        return (f"✅ {prov} 的运行时配置已删除，回退 env\n"
                f"当前状态: {'✅ env 配置生效中' if active else '❌ env 也无 key，该供应商已下线'}")

    if cmd == "reset":
        llm_secrets.reset_all()
        llm.reload_runtime()
        return ("✅ 全部运行时覆盖已清空，回退 env 配置\n"
                f"当前问答顺序: {' → '.join(llm.ask_order) or '无（LLM 已禁用）'}")

    return ("用法: /llm 查看 · /llm use <供应商> · /llm setkey <供应商> <key> · "
            "/llm setmodel <供应商> <模型> · /llm seturl <供应商> <url> · "
            "/llm delkey <供应商> · /llm reset")
