# F1 Schedule Bot — Discord 版

面向海外用户的全功能 F1 群机器人：定时推送 + 26 条斜杠指令 + `/ask` AI 问答。
与 QQ 官方群版共用 `common/` 数据层（赛程/成绩/图表/LLM 工具链）。

English guide below · 中文在下

---

## Quick Start (English)

### 1. Create the Discord application

1. Go to <https://discord.com/developers/applications> → **New Application**
2. **Bot** page → **Reset Token** → copy the token (shown once)
3. On the same page enable **Message Content Intent**
4. **OAuth2 → URL Generator**: scopes `bot` + `applications.commands` → open the invite URL and add the bot to your server

### 2. Configure

```bash
cp .env.example .env
```

Fill in at minimum:

| Variable | Description |
|---|---|
| `DISCORD_BOT_TOKEN` | Bot token from step 1 |
| `DISCORD_CHANNEL_IDS` | Comma-separated channel IDs for scheduled pushes (enable Developer Mode in Discord → right-click channel → Copy Channel ID) |

Recommended:

| Variable | Default | Description |
|---|---|---|
| `BOT_LANGUAGE` | `en` | UI language: `en` / `zh` |
| `TIMEZONE` | `UTC` | Scheduler timezone (message timestamps render in each user's local time automatically) |
| `DISCORD_GUILD_IDS` | — | Your server ID(s). Filled: slash commands appear **instantly**; empty: global registration takes up to 1 hour |

AI Q&A (`/ask`) is optional — set `DEEPSEEK_API_KEY` and/or `MOONSHOT_API_KEY` to enable. All other features work without LLM keys.

### 3. Run

**Docker (recommended)**

```bash
cd discord_version
docker compose up -d --build
```

**Bare metal** (Python 3.8+)

```bash
pip install -r requirements.txt
python main.py
```

### 4. Verify

- Bot logs show `✓ Discord Bot 已就绪`
- The startup card appears in your push channel
- Type `/` in the channel — you should see `/help /next /drivers /ask ...`

### Commands

`/help` `/next` `/last` `/calendar` `/gp` `/drivers` `/teams` `/weather`
`/upgrades` `/pu` `/pitstops` `/sc` `/qualigap` `/racechart` `/trend` `/pace`
`/car` `/predict` `/ask` `/clear` `/setdriver` `/setteam` `/me` `/unbind` `/news`

### Notes & limitations

- **Charts are uploaded as local files** — no public URL / domain / reverse proxy needed
- **Web-dependent features are opt-in**: deploy the bundled web service (`docker compose --profile web up -d`) and set `RATING_BASE_URL` to its https URL — this unlocks driver rating pushes + `/rate` `/ratings` `/dotd` `/telemetry`. Set `LIVE_BASE_URL` for `/live`, and `WATCH_LINKS=Name|URL;...` for `/watch`. Unset = commands stay hidden
- Scheduled news digest / upgrade-watch / per-user DM result cards are Chinese-language (shared `common/` copy); they are **off by default** under `BOT_LANGUAGE=en` (`NEWS_ENABLED`, `UPGRADES_WATCH_ENABLED`, `DM_SESSION_RESULT`)
- `/ask` answers follow `BOT_LANGUAGE`; ask in any language you like
- Mainland China servers need `PROXY_URL` — discord.com is not directly reachable there
- Runtime data persists in `discord_version/data/` (Docker volume); config profiles (drivers/PU limits/circuit data) are seeded from the image automatically
- **Admin config**: DM the bot `/llm` to switch AI provider / set API keys at runtime (see `docs/ADMIN_GUIDE.md`; admin quickref is DM'd to `ADMIN_USER_IDS` on startup)

---

## 中文指南

### 1. 创建 Discord 应用

1. 打开 <https://discord.com/developers/applications> → New Application
2. Bot 页面 → Reset Token 拿 Token（只显示一次）
3. 同页打开 **Message Content Intent**
4. OAuth2 → URL Generator → 勾选 `bot` 和 `applications.commands` → 用生成的链接把 Bot 邀请进服务器

### 2. 配置

```bash
cp .env.example .env
```

必填 `DISCORD_BOT_TOKEN` 和 `DISCORD_CHANNEL_IDS`（推送频道，逗号分隔）。
推荐设置 `BOT_LANGUAGE=zh`、`TIMEZONE=Asia/Shanghai`、`DISCORD_GUILD_IDS`（填了指令立即生效）。
AI 问答可选：配置 `DEEPSEEK_API_KEY` / `MOONSHOT_API_KEY` 即启用 `/ask`。

### 3. 运行

```bash
cd discord_version
docker compose up -d --build   # Docker（推荐）
# 或裸机：pip install -r requirements.txt && python main.py
```

### 4. 验证

日志出现 `✓ Discord Bot 已就绪`，频道收到启动卡片，输入 `/` 能看到指令列表即成。

### 已知边界

- 图表以本地文件直接上传，无需公网域名
- **Web 依赖功能可选开启**：`docker compose --profile web up -d` 部署配套 Web 服务并把 https 域名填入 `RATING_BASE_URL`，即解锁评分推送 + `/rate` `/ratings` `/dotd` `/telemetry`；`LIVE_BASE_URL` → `/live`；`WATCH_LINKS=名称|URL;...` → `/watch`。不配置则指令不出现
- 新闻聚合/升级件追更/环节成绩 DM 为中文文案（common/ 共享），`BOT_LANGUAGE=en` 时默认关闭
- 大陆服务器需配置 `PROXY_URL`（discord.com 大陆不可直连）
- **管理员配置**：私聊机器人 `/llm` 可在运行时切换 AI 服务商/配置密钥（详见 `docs/ADMIN_GUIDE.md`；启动时自动 DM 速查卡给 `ADMIN_USER_IDS`）
