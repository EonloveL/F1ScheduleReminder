# Docker Deployment Guide

[中文](README.md) | **English**

One command brings up the full stack: bot (commands + scheduled pushes + webhook), rating web, live timing panel, and Caddy reverse proxy with automatic HTTPS.

## Prerequisites

- A server with a public IP, Docker + docker compose plugin
- A domain pointed at the server (QQ platform callbacks require HTTPS)
- QQ Open Platform bot credentials: `AppID` / `AppSecret` / verification key (https://q.qq.com, developer settings)

## Steps

```bash
git clone https://github.com/EonloveL/F1ScheduleReminder.git
cd F1ScheduleReminder/deploy

# 1. Configure environment
cp .env.example .env
#    edit .env — required: QQ_APPID / QQ_APP_SECRET / QQ_GROUP_OPENID / BOT_WEBHOOK_SECRET

# 2. Configure the reverse proxy
cp Caddyfile.example Caddyfile
#    replace your-domain.example.com with your domain
#    and set RATING_BASE_URL in .env to https://your-domain

# 3. One-command deploy
chmod +x deploy.sh && ./deploy.sh
```

## Wire the callback in the QQ platform

Feature config → message push URL: `https://your-domain/bot/callback`

## Get the group_openid

Temporarily set `USE_SANDBOX=true`, restart the bot container, @ the bot in the group,
and read the group_openid from the logs:

```bash
docker compose restart f1-qqbot
docker compose logs -f f1-qqbot
```

Put it into `QQ_GROUP_OPENID` in `.env`, set `USE_SANDBOX=false`, then:

```bash
docker compose up -d
```

## Services

| Container | Port | Role |
|---|---|---|
| f1-qqbot | 8090 | Bot main process (webhook receiver) |
| f1-qqbot-web | 8080 | Rating web pages |
| f1-qqbot-live | 8091 | Live timing panel |
| f1-caddy | 443 | HTTPS reverse proxy + auto certificates |

## Updating

```bash
git pull
docker compose up -d --build
# if Caddyfile changed: docker compose restart f1-caddy
```

Runtime data (ratings, user prefs, memory, caches) lives in `../data` — preserved across updates.
