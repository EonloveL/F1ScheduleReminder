# F1 Schedule Reminder Bot

一个跑在 QQ 群里的 F1 助手机器人。基于 QQ 官方 Bot API V2，不用任何第三方框架协议，稳定合规。

把它拉进群以后，它会：

- **自动推送**：练习赛/排位/冲刺/正赛开始前提醒（含当地时间+北京时间双时区、赛道天气），正赛结束后推送完整成绩和最新积分榜，每天早晚推 F1 新闻速递
- **响应指令**：查赛程、查积分榜、查历史分站成绩、查升级件、查动力单元用量、预测本站名次……
- **AI 问答**：@它 直接问任何 F1 问题（技术规则、历史数据、本站分析），它会先查真实数据再回答，支持联网核实、多轮追问、识图
- **车手评分**：每站正赛后群里发评分链接，群友网页打分，产出车手评分榜和 DOTD（最佳车手）累积榜
- **实时面板**：比赛周提供网页实时计时面板（名次/圈速/轮胎/Team Radio）

## 快速部署

### 前置条件

| 准备项 | 说明 |
|---|---|
| QQ 机器人 | 到 [QQ 开放平台](https://q.qq.com) 注册并创建机器人（个人开发者即可） |
| 服务器 | 一台有公网 IP 的 Linux 服务器，装好 Docker 和 docker compose 插件 |
| 域名 | 一个已解析到服务器的域名（QQ 平台回调强制 HTTPS） |

只想先本地试试？跳到 [本地运行](#本地运行)。

### 第一步：拿到 QQ 机器人的四样东西

在 QQ 开放平台（q.qq.com）你的机器人后台：

1. **开发设置** → 记下 `AppID` 和 `AppSecret`
2. **开发设置** → 验证密钥 → 记下 `BOT_WEBHOOK_SECRET`
3. 把机器人拉进你的 QQ 群，群设置 → 机器人管理 → **打开「允许主动消息」**（不开的话定时推送会发不出去）
4. `group_openid` 等部署完后用工具获取（见第三步）

### 第二步：部署

```bash
git clone https://github.com/EonloveL/F1ScheduleReminder.git
cd F1ScheduleReminder/deploy

cp .env.example .env          # 编辑：填入 AppID / AppSecret / Webhook 密钥
cp Caddyfile.example Caddyfile # 编辑：把 your-domain.example.com 改成你的域名
                               # 同时把 .env 里的 RATING_BASE_URL 改成 https://你的域名

chmod +x deploy.sh && ./deploy.sh
```

`deploy.sh` 会做配置预检，然后一条命令拉起 4 个容器：

| 容器 | 作用 |
|---|---|
| f1-qqbot | 机器人主程序（定时推送 + 指令 + AI 问答 + Webhook 接收） |
| f1-qqbot-web | 群友评分网页 |
| f1-qqbot-live | 实时计时面板 |
| f1-caddy | HTTPS 反代，自动申请和续期 Let's Encrypt 证书 |

### 第三步：接回调 + 拿 group_openid

1. 回到 QQ 开放平台 → 功能配置 → 消息推送地址填：`https://你的域名/bot/callback`
2. 获取群 `group_openid`：临时把 `.env` 里 `USE_SANDBOX=true`，`docker compose restart f1-qqbot`，然后在群里 @机器人 随便说句话，看日志：

```bash
docker compose logs -f f1-qqbot
```

也可以本地跑 `python get_group_openid.py` 获取。拿到后填进 `.env` 的 `QQ_GROUP_OPENID`，把 `USE_SANDBOX` 改回 `false`，重启：

```bash
docker compose up -d
```

看到日志里机器人上线、群里收到启动消息，就部署完成了。

## 使用方法

### 群指令（节选，群里发 `/help` 看完整列表）

| 指令 | 说明 |
|---|---|
| `/next` | 下一站比赛周末赛程（双时区） |
| `/calendar` | 全年赛历 |
| `/drivers` `/teams` | 车手/车队积分榜 |
| `/last [环节]` | 上一场成绩（fp1/fp2/fp3/qualy/sprint/race） |
| `/gp 蒙扎 2022 race` | 历史分站成绩查询 |
| `/predict 蒙扎` | 本站排位/正赛名次预测（统计模型） |
| `/upgrades 法拉利 蒙扎` | 升级件汇总（FIA 官方文档） |
| `/pu 维斯塔潘 2023` | 动力单元部件用量 |
| `/rate` | 获取本站评分链接（赛后给车手打分） |
| `/ratings` `/dotd` | 车手评分榜 / 最佳车手累积榜 |
| `/watch` | 观赛链接 |
| `/live` | 实时计时面板（比赛周） |
| `/setdriver 维斯塔潘` | 关注车手，赛后私聊推送其成绩 |
| `/news on` | 订阅关注车手/主队的新闻私聊推送 |

### AI 问答

群里 **@机器人** 直接提问：

```
@机器人 DRS 的使用规则是什么？
@机器人 维斯塔潘本赛季拿过几个分站冠军？
@机器人 分析一下上一站法拉利的 stint 节奏
```

它会调用 16 个 F1 数据工具取真实数据后作答，支持追问（30 分钟上下文）。发 `/clear` 清空对话上下文。此功能需要配置 LLM Key（见下）。

## 配置说明

`.env` 中只有 4 项必填，其余都是可选增强：

| 配置 | 必填 | 说明 |
|---|---|---|
| `QQ_APPID` / `QQ_APP_SECRET` | ✅ | QQ 开放平台 → 开发设置 |
| `QQ_GROUP_OPENID` | ✅ | 群 ID，获取方式见部署第三步 |
| `BOT_WEBHOOK_SECRET` | ✅ | QQ 开放平台 → 验证密钥（Webhook 验签） |
| `MOONSHOT_API_KEY` / `DEEPSEEK_API_KEY` | 可选 | AI 问答（Kimi / DeepSeek，[platform.moonshot.cn](https://platform.moonshot.cn) / [platform.deepseek.com](https://platform.deepseek.com) 免费注册），不配则 @机器人 无 AI 回复 |
| `TAVILY_API_KEY` / `BOCHA_API_KEY` | 可选 | 联网搜索增强 |
| `ADMIN_OPENIDS` | 可选 | 管理员 openid（知识库审核 `/kb`） |
| `OPENF1_USERNAME` / `OPENF1_PASSWORD` | 可选 | OpenF1 sponsor 账号，实时面板看实时档用 |

完整配置项见 [deploy/.env.example](deploy/.env.example)，全部有中文注释。

## 本地运行（不想用 Docker）

```bash
pip install -r requirements.txt
cp .env.example .env   # 填写配置

python main.py                # 机器人（:8090 Webhook）
python web/rating_server.py   # 评分网页（:8080）
python web/live_server.py     # 实时面板（:8091）
```

本地运行没有公网 HTTPS 回调时，指令走 WebSocket 通道照常可用；Webhook 仅部署到服务器后需要。

## 常见问题

**群里收不到定时推送？**
群设置 → 机器人管理 → 打开「允许主动消息」（新入群默认关闭）。

**@机器人 没反应 / 提示 AI 未配置？**
`.env` 里没填 LLM Key。填 `MOONSHOT_API_KEY` 或 `DEEPSEEK_API_KEY` 后重启。

**改了 Caddyfile 不生效？**
`docker compose restart f1-caddy`（bind 挂载的内容变化 compose 检测不到）。

**国内服务器构建慢 / whisper 模型下载失败？**
Dockerfile 里已注释好阿里云镜像加速行，取消注释即可；`deploy/.env.example` 里把 `HF_ENDPOINT=https://hf-mirror.com` 取消注释。

更多部署细节见 [deploy/README.md](deploy/README.md)。

## 数据源

| 数据 | 来源 |
|---|---|
| 赛程 / 成绩 / 积分榜 | f1api.dev（主）· Jolpica（备） |
| 实时天气 / 无线电 / 圈速 | OpenF1 |
| 天气预报 / 归档 | Open-Meteo |
| 升级件 / PU 用量 | FIA 官方文档（PDF 解析）· F1Cosmos |
| 新闻聚合 | F1Cosmos |
| LLM | Kimi（Moonshot）· DeepSeek |

## 项目结构

```
├── main.py              # 机器人主入口
├── common/              # 核心库（API 封装 / LLM 助手 / 工具层 / 调度 / 存储）
│   └── prompts/         # LLM 提示词（外置可改）
├── web/                 # 评分网页 + 实时面板 + 车手头像/车队图标静态资源
├── deploy/              # Docker 一键部署（compose / Caddy / 部署脚本）
├── analysis/            # 开发者工具：预测模型回测与校准（普通部署不需要）
└── data/                # 随包分发的档案数据（车手档案 / 赛道数据 / 预测模型权重）
```

- Python 3.8+，密钥与群 ID 全部走环境变量，仓库不含任何私密信息
- 运行时数据（用户偏好、评分、会话记忆、缓存）写入 `data/`，已 gitignore

## License

[MIT](LICENSE)
