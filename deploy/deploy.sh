#!/bin/bash
# F1 QQ 官方群机器人 - 一键部署脚本
# 用法：在 deploy/ 目录下执行 ./deploy.sh

set -e

cd "$(dirname "$0")"

if [ ! -f .env ]; then
    echo "✗ 未找到 .env，请先执行: cp .env.example .env 并填写 QQ_APPID / QQ_APP_SECRET / QQ_GROUP_OPENID"
    exit 1
fi

chmod 600 .env 2>/dev/null

if grep -q "QQ_APP_SECRET=YOUR_SECRET_HERE" .env; then
    echo "✗ .env 中 QQ_APP_SECRET 未填写（仍为占位符）"
    exit 1
fi

if [ ! -f Caddyfile ]; then
    echo "✗ 未找到 Caddyfile，请执行: cp Caddyfile.example Caddyfile 并把 your-domain.example.com 替换为你的域名"
    exit 1
fi

if grep -q "your-domain.example.com" Caddyfile; then
    echo "✗ Caddyfile 中域名未替换（仍为 your-domain.example.com）"
    exit 1
fi

grep -q "^BOT_WEBHOOK_SECRET=.\+" .env || echo "⚠ BOT_WEBHOOK_SECRET 未配置，webhook 将 fail-closed（拒绝所有群指令事件），请配置后重启"
grep -q "^DEEPSEEK_API_KEY=.\+" .env || grep -q "^MOONSHOT_API_KEY=.\+" .env \
    || echo "⚠ 未配置任何 LLM API Key，AI 问答功能将禁用"

if ! command -v docker &> /dev/null; then
    echo "✗ Docker 未安装"
    exit 1
fi

if docker compose version &> /dev/null; then
    COMPOSE="docker compose"
elif command -v docker-compose &> /dev/null; then
    COMPOSE="docker-compose"
else
    echo "✗ docker compose 不可用"
    exit 1
fi

echo "==> 构建镜像..."
$COMPOSE build

echo "==> 启动服务..."
$COMPOSE up -d

echo "==> 状态:"
$COMPOSE ps

echo ""
echo "✓ 部署完成"
echo "查看日志: $COMPOSE logs -f f1-qqbot"
echo ""
echo "别忘了到 QQ 开放平台后台把回调地址配置为: https://你的域名/bot/callback"
