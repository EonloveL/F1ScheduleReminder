# F1 Schedule Reminder Bot（QQ 官方群机器人）

基于 QQ 官方 Bot API V2 的 F1 群机器人：定时推送（赛前提醒 / 成绩 / 积分榜 / 新闻）、群指令查询、@机器人 AI 问答（联网 LLM + 16 个 F1 数据工具）、车手评分（DOTD）与实时计时面板 Web 服务。

## 功能概览

- **定时推送**：练习赛 / 排位 / 冲刺 / 正赛赛前提醒（双时区 + 天气）、赛后成绩与积分榜、每日新闻速递
- **群指令**：`/drivers` `/teams` `/calendar` `/next` `/gp` `/predict` `/upgrades` `/pu` `/telemetry` 等（见 `/help`）
- **AI 问答**：@机器人 提问，分层链路（查询改写 → 工具取数 → 成文），支持联网核实、多轮上下文、图片识别
- **车手评分**：每站正赛后群友网页评分 + DOTD 累积榜（`web/rating_server.py`）
- **实时面板**：比赛周实时计时 / 遥测悬浮窗（`web/live_server.py`）

## 项目结构

```
├── main.py                 # 机器人主入口（指令 + Webhook + 调度 + AI 接线）
├── get_group_openid.py     # 工具：获取群 group_openid
├── requirements.txt
├── .env.example            # 配置模板（复制为 .env 填写）
├── common/                 # 核心库
│   ├── f1_api.py           # 赛程/成绩 API（f1api.dev 主 + Jolpica 备 + 缓存）
│   ├── qq_group_bot.py     # QQ 官方群 API 封装
│   ├── webhook_handler.py  # Webhook 回调（Ed25519 验签）
│   ├── llm_assistant.py    # LLM 助手（DeepSeek/Kimi，分层问答/视觉）
│   ├── f1_tools.py         # AI 工具层（16 个 schema + handlers）
│   ├── scheduler.py        # APScheduler 定时调度
│   ├── ratings_store.py    # 评分存储（文件锁 + 原子写）
│   ├── prediction_model.py # 名次预测统计模型
│   └── prompts/            # LLM 提示词（外置可改）
├── web/
│   ├── rating_server.py    # 评分 Web 服务（含车手头像/车队图标路由）
│   ├── live_server.py      # 实时计时面板
│   └── static/avatars/     # 车手头像 / 车队图标静态资源
├── deploy/                 # Docker 一键部署（见 deploy/README.md）
│   ├── docker-compose.yml  # 机器人 + 评分 + 实时面板 + Caddy 自动 HTTPS
│   ├── Dockerfile / Dockerfile.live
│   ├── Caddyfile.example   # 反代配置模板（替换域名即可）
│   ├── .env.example        # 完整环境变量模板
│   └── deploy.sh           # 一键部署脚本（含配置预检）
├── analysis/               # 开发者工具：预测模型回测/校准（普通部署不需要）
│   ├── backtest_circuit_hist.py  # 模型权重受控回测（改权重前先跑）
│   ├── backtest_sc_effect.py     # 安全车修正系数回测
│   ├── verify_sc_calibration.py  # 概率校准对比
│   ├── compute_sc_rates.py       # 赛道安全车率年度统计（刷新 circuits_data.json）
│   └── fit_window_compare.py     # 拟合窗口对比
└── data/                   # 随包分发的档案数据（运行时数据已 gitignore）
    ├── drivers_profile.json    # 车手/车队档案
    ├── pu_limits.json          # PU 部件赛季上限（按赛季）
    ├── circuits_data.json      # 赛道档案（圈速纪录/时区/安全车率）
    └── prediction_*.json       # 预测模型权重与历史数据集
```

## 快速开始

### 1. 准备 QQ 机器人

在 [QQ 开放平台](https://q.qq.com) 创建机器人，获取：

- `AppID` / `AppSecret`（开发设置）
- `BOT_WEBHOOK_SECRET`（验证密钥，Webhook 验签用）
- 群 `group_openid`（可先运行 `python get_group_openid.py`，把机器人拉进群后 @它 获取）

### 2. 配置

```bash
cp .env.example .env
# 编辑 .env 填写必填项：
#   QQ_APPID / QQ_APP_SECRET / QQ_GROUP_OPENID / BOT_WEBHOOK_SECRET
# 可选项（AI 问答）：
#   MOONSHOT_API_KEY（Kimi，成文/视觉）  DEEPSEEK_API_KEY（改写/取数决策）
#   TAVILY_API_KEY / BOCHA_API_KEY（联网搜索增强）
```

### 3. 安装运行（本地）

```bash
pip install -r requirements.txt

python main.py                # 机器人（指令 + 定时推送 + Webhook :8090）
python web/rating_server.py   # 评分服务（:8080，群友评分入口）
python web/live_server.py     # 实时计时面板（:8091，比赛周可用）
```

### 4. Docker 一键部署（推荐生产环境）

```bash
cd deploy
cp .env.example .env && cp Caddyfile.example Caddyfile
# 编辑 .env 和 Caddyfile（填入密钥 + 你的域名）
./deploy.sh
```

详见 [deploy/README.md](deploy/README.md)：一条命令拉起机器人、评分网页、实时面板和 Caddy 反代（自动申请/续期 HTTPS 证书）。部署后到 QQ 开放平台把回调地址配置为 `https://你的域名/bot/callback`。

## 数据源

| 数据 | 来源 |
|---|---|
| 赛程 / 成绩 / 积分榜 | f1api.dev（主）· Jolpica（备） |
| 实时天气 / 无线电 / 圈速 | OpenF1 |
| 天气预报 / 归档 | Open-Meteo |
| 升级件 / PU 用量 | FIA 官方文档（PDF 解析）· F1Cosmos |
| 新闻聚合 | F1Cosmos `/news` |
| LLM | Kimi（Moonshot）· DeepSeek（OpenAI 兼容端点） |

## 说明

- Python 3.8+，Windows / Linux / Docker 均可运行
- 所有密钥与群 ID 均通过环境变量配置，仓库不包含任何私密信息
- 运行时数据（用户偏好、评分、会话记忆、缓存等）写入 `data/`，已在 `.gitignore` 中排除

## License

[MIT](LICENSE)
