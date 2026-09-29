# Docker 部署指南

一条命令拉起全部服务：机器人（指令 + 定时推送 + Webhook）、评分网页、实时计时面板、Caddy 反向代理（自动 HTTPS）。

## 前置条件

- 一台有公网 IP 的服务器（Docker + docker compose 插件）
- 一个已解析到服务器的域名（QQ 平台回调强制 HTTPS）
- QQ 开放平台机器人：`AppID` / `AppSecret` / 验证密钥（https://q.qq.com → 开发设置）

## 步骤

```bash
git clone https://github.com/EonloveL/F1ScheduleReminder.git
cd F1ScheduleReminder/deploy

# 1. 配置环境变量
cp .env.example .env
#    编辑 .env，必填：QQ_APPID / QQ_APP_SECRET / QQ_GROUP_OPENID / BOT_WEBHOOK_SECRET

# 2. 配置域名
cp Caddyfile.example Caddyfile
#    把 your-domain.example.com 替换为你的域名
#    同步把 .env 里的 RATING_BASE_URL 改为 https://你的域名

# 3. 一键部署
chmod +x deploy.sh && ./deploy.sh
```

## 在 QQ 开放平台完成回调配置

1. 机器人管理 → 功能配置 → 消息推送地址：`https://你的域名/bot/callback`
2. 群设置 → 机器人管理 → 开启「允许主动消息」（否则定时推送会 40034105）

## 服务清单

| 容器 | 说明 | 端口（内部） |
|---|---|---|
| f1-qqbot | 机器人主程序 | 8090（Webhook） |
| f1-qqbot-web | 评分网页（gunicorn） | 8080 |
| f1-qqbot-live | 实时计时面板（gunicorn） | 8091 |
| f1-caddy | HTTPS 反代 + 头像静态服务 | 80/443（对外） |

## 数据持久化

- `../data`（仓库根目录 `data/`）挂载进容器：用户偏好、评分、缓存、会话记忆都在其中，重建容器不丢数据
- Caddy 证书存于 docker volume `caddy_data`

## 常用命令

```bash
docker compose logs -f f1-qqbot     # 机器人日志
docker compose restart f1-caddy     # 改了 Caddyfile 后必须重启（bind 挂载内容变化 compose 检测不到）
docker compose up -d --build        # 代码更新后重建
```
