# 管理员配置手册（可分发给管理员）

> 适用范围：QQ 官方群版 + Discord 版。
> 管理员身份：QQ 版 = `ADMIN_OPENIDS` 环境变量（member_openid，逗号分隔）；
> Discord 版 = `ADMIN_USER_IDS`（Discord 用户 ID，逗号分隔）。
> **所有密钥配置指令仅限私聊/DM 使用**——QQ 侧在群内执行 `/llm` 会被拒绝，防止 key 留在群聊记录。

## 1. LLM 服务商与密钥（/llm 族，私聊专属）

| 指令 | 作用 |
|---|---|
| `/llm` | 查看供应商状态：模型、key 脱敏值与来源（env/运行时）、是否在问答顺序中 |
| `/llm use <供应商>` | 切换问答首选供应商，立即热生效，无需重启 |
| `/llm setkey <供应商> <key>` | 配置/更换该供应商 API Key（运行时存储，优先于 env；可启用 env 未配置的服务商） |
| `/llm setmodel <供应商> <模型>` | 更换该供应商默认模型 |
| `/llm seturl <供应商> <url>` | 配置 base_url（openai_compat 等自托管端点用） |
| `/llm delkey <供应商>` | 删除运行时配置，回退 env（env 也没有则该供应商下线） |
| `/llm reset` | 清空全部运行时覆盖 |
| `/llm help` | 速查卡 |

供应商：`kimi`（联网搜索，L2 成文推荐）/ `deepseek`（L0/L1 推荐）/ `openai_compat` / `openrouter` / `gemini`

典型场景：
- 切换 AI 服务商：`/llm use gemini`
- 新增一个服务商：`/llm setkey openrouter sk-or-xxx` → `/llm use openrouter`
- 换 key：`/llm setkey deepseek sk-新key`（直接覆盖旧的）

⚠️ **发完含 key 的消息请立即撤回/删除**（QQ 长按消息撤回；Discord 右键删除）。
key 落地位置：`data/llm_secrets.json`（0o600 权限，git/docker/部署包/OSS 全部隔离）。

## 2. 分层模型（/models /model，QQ 群内可用）

问答分六层：l0=查询改写 · l1=取数 · l2=成文 · deep=深度分析 · vision=识图 · **memory=记忆总结**

| 指令 | 作用 |
|---|---|
| `/models` | 查看当前生效的六层模型 + 各供应商可用模型清单 |
| `/models refresh` | 强制刷新模型清单 |
| `/model set <层> <供应商/模型>` | 指定某层模型，如 `/model set l2 kimi/kimi-k3` |
| `/model reset [层]` | 恢复默认（缺省全部） |

注意：`/llm use` 影响的是**默认顺序**；已被 `/model set` 钉住的层不受影响。

💡 memory 层建议配便宜模型（`/models` 可查各服务商清单），如 `/model set memory gemini/gemini-2.5-flash`；
有本地 LLM 时 `/model set memory local/qwen2.5:7b` 可实现归档零 API 成本。

## 2.5 会话记忆机制（三层衰减记忆）

用户侧指令（两版均有，非管理员也可用）：

| 指令 | 作用 |
|---|---|
| `/memory` | 查看自己的记忆构成（工作记忆/主题摘要/长期锚定） |
| `/memory forget <id>` | 删除指定记忆条目 |
| `/clear` | **归档式清空**：当前会话由 LLM 总结成主题摘要存入长期记忆，而非物理删除 |

机制：召回打分 = 时间衰减 × 话题相关性 × 显著性——无关历史不会污染新问题；
1h 无交互的会话自动归档；纠错与用户明示"记住"会写入不衰减的锚定记忆。

向量召回增强（可选）：`MEMORY_EMBEDDING=provider` + `EMBEDDING_BASE_URL/KEY/MODEL`
（远程或本地 Ollama 均可）；高配服务器可 `MEMORY_EMBEDDING=local` +
`pip install -r discord_version/requirements-local.txt`（进程内 fastembed，需 ≥2GB 空闲内存）。

## 3. 本地大模型（local 供应商，高配服务器用户）

任意 OpenAI 兼容本地端点（Ollama / llama.cpp / vLLM / LM Studio）：

```bash
# env 方式
LOCAL_LLM_BASE_URL=http://localhost:11434/v1   # 缺省即此值
LOCAL_LLM_MODEL=qwen2.5:7b                      # 必须显式配置模型名
# 或运行时（管理员私聊）：
/llm setmodel local qwen2.5:7b
/llm use local                                  # 提为首选
```

Docker 容器访问宿主机 Ollama：compose 的 bot 服务加
`extra_hosts: ["host.docker.internal:host-gateway"]`，base_url 用 `http://host.docker.internal:11434/v1`。

资源参考：7B 模型（Q4 量化）≈ 6GB 内存；14B ≈ 12GB。轻量服务器（≤2GB 空闲）不建议启用。


## 4. 其他管理员指令（仅 QQ 版）

| 指令 | 作用 |
|---|---|
| `/kb` | 知识库候选审核（`/kb ok <id>` 入库 / `/kb no <id>` 拒绝） |
| `/bili_login` | B站扫码登录（解锁视频字幕接口） |

## 5. 出网门禁（两版共有，无需操作）

- 所有出站消息经 secret_guard 脱敏：已注册密钥出现在文本中自动替换为 `***REDACTED***`
- 日志同样过脱敏过滤器，密钥不落日志文件
- 运行时密钥文件不进 git / Docker 镜像 / 部署包 / OSS 仓

## 6. Web 依赖功能（可选部署，Discord 版）

Discord 版以下功能默认隐藏，部署 Web 服务并配置 env 后自动出现：

| env | 启用的功能 |
|---|---|
| `RATING_BASE_URL=https://你的域名` | 评分生命周期推送 + `/rate` `/ratings` `/dotd` `/telemetry` |
| `LIVE_BASE_URL=https://...`（缺省复用上者） | `/live` 实时计时面板 |
| `WATCH_LINKS=名称\|URL;名称\|URL` | `/watch` 观赛链接 |

Web 服务启动（rating_server，含评分表单/榜单/遥测页/ICS 日历）：

```bash
cd discord_version
docker compose --profile web up -d --build   # 附带启动 f1-discord-web（8080 端口）
```

再用任意反代（nginx/Caddy）把 8080 暴露为 https 域名，填进 `RATING_BASE_URL` 即可。
