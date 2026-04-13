#!/bin/bash
# F1赛程提醒机器人 - 快速启动脚本
# 使用方法: ./start.sh [version]
# 示例: ./start.sh wecom

VERSION=${1:-"wecom"}

echo "========================================"
echo "F1赛程提醒机器人启动器"
echo "========================================"
echo ""

# 检查版本是否存在
if [ ! -d "$VERSION" ]; then
    echo "❌ 版本 '$VERSION' 不存在"
    echo ""
    echo "可用版本:"
    echo "  - wecom       (企业微信版，推荐服务器)"
    echo "  - pushplus    (PushPlus版，推荐个人)"
    echo "  - wechat      (个人微信版，仅本地测试)"
    echo ""
    echo "使用方法: ./start.sh [版本名]"
    exit 1
fi

echo "🚀 启动 $VERSION 版本..."
echo ""

cd "$VERSION" || exit

# 检查依赖
if [ ! -f "requirements.txt" ]; then
    echo "❌ 未找到 requirements.txt"
    exit 1
fi

echo "📦 检查依赖..."
pip3 install -q -r requirements.txt

echo ""
echo "⚙️  检查配置..."

# 检查配置
if [ "$VERSION" = "wecom" ]; then
    if grep -q "YOUR_KEY_HERE" main.py; then
        echo "⚠️  请先配置企业微信Webhook地址"
        echo "编辑 wecom_version/main.py 中的 WECOM_WEBHOOK_URL"
        exit 1
    fi
elif [ "$VERSION" = "pushplus" ]; then
    if grep -q "YOUR_TOKEN_HERE" main.py; then
        echo "⚠️  请先配置PushPlus Token"
        echo "编辑 pushplus_version/main.py 中的 PUSHPLUS_TOKEN"
        exit 1
    fi
elif [ "$VERSION" = "wechat" ]; then
    echo "⚠️  注意：个人微信版本仅推荐本地测试使用"
fi

echo "✓ 配置检查通过"
echo ""

# 启动
echo "🎯 正在启动..."
echo ""
python3 main.py