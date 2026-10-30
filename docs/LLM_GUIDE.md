# 大模型接入手册（LLM Guide）

> 适用：QQ 版 + Discord 版（两版共用同一套 LLM 链路，`common/llm*`）。
> 所有配置均可通过 `.env`（环境变量）完成；**运行时可改**的部分用管理员私聊指令 `/llm`、`/model`（见文末「运行时管理」）。
> 不配置任何 Key 时，机器人其余功能（定时推送/指令/图表）完全正常，仅 AI 问答禁用。

## 一、功能 ↔ 模型分层对照表

机器人把不同任务分配给不同"层"，每层可以单独选模型（`/model set <层> <供应商/模型>`）：

| 层 | 代号 | 干什么 | 默认模型（按可用供应商自动选） | 选型建议 |
|---|---|---|---|---|
| 查询改写 | `l0` | 把追问/代词/相对时间解析成独立问题（"那他队友呢"→"皮亚斯特里积分多少"） | ask_order 首个非联网供应商 | 快、便宜即可 |
| 取数 | `l1` | 决定调用哪些 F1 数据工具（不直接回答） | 同上 | 需要 JSON 输出稳定 |
| 成文 | `l2` | 把工具数据写成最终回答（可联网核实） | ask_order 首个**联网**供应商 | 需要联网搜索能力（Kimi） |
| 深度分析 | `deep` | 升级件效果等深度话题（强制联网+推理模型） | kimi-k3 | 推理模型 |
| 识图 | `vision` | 图片理解（截图问排名等） | kimi-k2.6 | 需视觉能力 |
| 记忆总结 | `memory` | `/clear` 归档、1h 超时会话压缩成主题摘要 | ask_order 首个非联网供应商 |  cheapest 即可；本地模型零成本 |
| 翻译 | （内部） | 新闻速递/升级件说明 英→中 | deepseek 优先 | 快、便宜 |

其他消费 LLM 的功能：每周预告圈速纪录核查（kimi 联网）、B站视频导读成文、知识沉淀每周蒸馏、新闻聚合精简翻译。

## 二、供应商配置（用哪家的模型）

支持 6 个供应商，**配了 Key 就自动启用**；问答按 `LLM_PROVIDER` 指定的首选 + 内置顺序轮询兜底。

| 供应商 | Key 环境变量 | 模型环境变量 | 默认模型 | 联网 | 备注 |
|---|---|---|---|---|---|
| Kimi（月之暗面） | `MOONSHOT_API_KEY` | `LLM_MODEL` | kimi-k2.6 | ✅ | **联网+识图主力**，platform.moonshot.cn 注册 |
| DeepSeek | `DEEPSEEK_API_KEY` | `DEEPSEEK_MODEL` | deepseek-chat | ❌ | 改写/取数/翻译主力，便宜快 |
| OpenAI 兼容端点 | `OPENAI_COMPAT_KEY` | `OPENAI_COMPAT_MODEL` | —（必填） | ❌ | 任意自托管/第三方兼容服务，还需 `OPENAI_COMPAT_BASE_URL` |
| OpenRouter | `OPENROUTER_API_KEY` | `OPENROUTER_MODEL` | openai/gpt-4o-mini | ❌ | 一个 Key 用百家模型 |
| Gemini | `GEMINI_API_KEY` | `GEMINI_MODEL` | gemini-2.5-flash | ❌ | Google 官方 OpenAI 兼容端点 |
| **本地模型**（Ollama/llama.cpp/vLLM/LM Studio） | `LOCAL_LLM_API_KEY`（可空） | `LOCAL_LLM_MODEL` | —（必填才启用） | ❌ | 免 key；`LOCAL_LLM_BASE_URL` 缺省 `http://localhost:11434/v1` |

问答兜底顺序（可用 `LLM_PROVIDER` 提首选）：
`kimi → deepseek → openai_compat → openrouter → gemini → local`
（翻译顺序相反：deepseek 优先——翻译不需要联网）

```env
# 最小可用组合（二选一即可开 AI 问答）：
MOONSHOT_API_KEY=sk-...        # 联网问答+识图
DEEPSEEK_API_KEY=sk-...        # 改写/取数/翻译（强烈建议一起配，分层省成本）
```

## 三、行为调优项（都有合理默认值，一般不用改）

| 环境变量 | 默认 | 控制什么 |
|---|---|---|
| `LLM_ASK_ENABLED` | true | 总开关（false 时 @机器人/ask 完全关闭） |
| `LLM_MAX_CONCURRENCY` | 4 | AI 问答全局并发上限（防触发供应商限流） |
| `LLM_ASK_NOTICE_AFTER` | 45（秒） | 超过该时长给用户发"正在分析"进度提示 |
| `LLM_ASK_HARD_TIMEOUT` | 600（秒） | 问答硬超时（超时后台任务在轮次边界取消） |
| `LLM_CONNECT_TIMEOUT` | 10（秒） | HTTP 连接超时（快速失败） |
| `LLM_L1_TIMEOUT` | 60（秒） | 取数层读超时（轻量决策，收紧） |
| `LLM_L2_TIMEOUT` | 150（秒） | 成文层读超时 |
| `LLM_DEEP_TIMEOUT` | 180（秒） | 深度分析读超时 |
| `LLM_L2_MAX_TOKENS` | 1800 | 成文层输出预算（防超长回答读超时） |
| `LLM_L2_REASONING` | none | kimi 成文层推理档位（none/low/medium/high；数据已注入，推理纯烧钱） |
| `LLM_DEEP_MODEL` | kimi-k3 | 深度分析模型（仅 kimi） |
| `LLM_DEEP_REASONING` | high | 深度分析推理档位（仅 kimi） |
| `LLM_VISION_MODEL` | kimi-k2.6 | 识图模型 |
| `LLM_L1_JSON_MODE` | true | 取数层结构化输出模式（false 回退原生 function calling） |
| `TAVILY_API_KEY` / `BOCHA_API_KEY` | — | 给无联网供应商（如 DeepSeek）补客户端搜索能力 |

## 四、会话记忆（三层衰减记忆）

| 环境变量 | 默认 | 控制什么 |
|---|---|---|
| `MEMORY_EMBEDDING` | off | 相关性引擎：`off`=实体/主题词匹配（零成本）；`provider`=向量召回（OpenAI 兼容 embeddings，远程或本地 Ollama 均可）；`local`=进程内 fastembed（需 `pip install fastembed`，≥2GB 空闲内存） |
| `EMBEDDING_BASE_URL` / `EMBEDDING_KEY` / `EMBEDDING_MODEL` | — | provider 模式的端点/key/模型（本地 Ollama 时 KEY 可空） |
| `LOCAL_EMBED_MODEL` | BAAI/bge-small-zh-v1.5 | local 模式的 fastembed 模型 |

记忆行为无需配置即可工作：近期对话原文保留（半衰期 1h）、`/clear` 与 1h 超时自动归档为主题摘要（半衰期 3 天）、纠错与"记住这个"长期锚定（不衰减）、无关历史不注入新问题。

## 五、运行时管理（管理员私聊，热生效）

| 指令 | 作用 |
|---|---|
| `/llm` | 查看各供应商状态（key 脱敏显示） |
| `/llm use <供应商>` | 切换问答首选 |
| `/llm setkey <供应商> <key>` | 配置/更换 Key（可启用 env 未配置的供应商） |
| `/llm setmodel <供应商> <模型>` | 换默认模型 |
| `/llm seturl <供应商> <url>` | 自定义端点 |
| `/llm delkey <供应商>` / `/llm reset` | 删除运行时配置 / 清空全部 |
| `/models` `/model set <层> <供应商/模型>` | 分层模型精细管理 |

安全：群内执行 `/llm` 一律拒绝（防 key 留在群聊记录）；运行时密钥存 `data/llm_secrets.json`（0o600，不进 git/镜像/部署包）；所有出站消息与日志自动密钥脱敏。

## 六、常见组合

| 场景 | 配置 |
|---|---|
| 最小成本 | 只配 `DEEPSEEK_API_KEY`（无联网核实能力） |
| 推荐 | `MOONSHOT_API_KEY` + `DEEPSEEK_API_KEY` |
| 白嫖/多模型 | `OPENROUTER_API_KEY`（一个 key 通吃，注意各模型限额） |
| 完全本地（高配服务器） | Ollama 起 `qwen2.5:14b` + `LOCAL_LLM_MODEL=qwen2.5:14b`，`/llm use local`，`/model set memory local/qwen2.5:7b` |
| 国内服务器 | 直连 Kimi/DeepSeek 无需代理；Gemini/OpenRouter 需出网 |

Docker 容器访问宿主机 Ollama：compose 加 `extra_hosts: ["host.docker.internal:host-gateway"]`，base_url 用 `http://host.docker.internal:11434/v1`。
