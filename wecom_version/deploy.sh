#!/bin/bash
# F1赛程提醒机器人 - 企业微信版本部署脚本
# 使用方法: ./deploy.sh

echo "========================================"
echo "F1赛程提醒机器人 - 企业微信版本部署"
echo "========================================"
echo ""

# 检查Python
if ! command -v python3 &> /dev/null; then
    echo "❌ 未找到Python3，请先安装Python3"
    exit 1
fi

echo "✓ Python3已安装"

# 检查pip
if ! command -v pip3 &> /dev/null; then
    echo "❌ 未找到pip3，请先安装pip3"
    exit 1
fi

echo "✓ pip3已安装"

# 安装依赖
echo ""
echo "📦 正在安装依赖..."
pip3 install -r requirements.txt

if [ $? -eq 0 ]; then
    echo "✓ 依赖安装成功"
else
    echo "❌ 依赖安装失败"
    exit 1
fi

# 检查配置
echo ""
echo "⚙️  检查配置..."
if grep -q "YOUR_KEY_HERE" main.py; then
    echo "⚠️  警告：请先在 main.py 中配置企业微信Webhook地址"
    echo ""
    echo "配置方法:"
    echo "1. 打开 main.py 文件"
    echo "2. 找到 WECOM_WEBHOOK_URL 变量"
    echo "3. 替换为你的Webhook地址"
    echo ""
    echo "获取Webhook地址:"
    echo "- 企业微信群 -> 群机器人 -> 添加机器人 -> 复制Webhook地址"
    echo ""
    exit 1
fi

echo "✓ 配置检查通过"

# 运行测试
echo ""
echo "🧪 正在运行测试..."
python3 -c "
import sys
sys.path.insert(0, '..')
from common import F1API
f1 = F1API(2024)
schedule = f1.get_schedule()
print(f'✓ F1 API测试成功，获取到 {len(schedule)} 场比赛')
"

if [ $? -eq 0 ]; then
    echo "✓ 测试通过"
else
    echo "❌ 测试失败"
    exit 1
fi

echo ""
echo "========================================"
echo "✅ 部署完成！"
echo "========================================"
echo ""
echo "启动命令:"
echo "  前台运行: python3 main.py"
echo "  后台运行: nohup python3 main.py > output.log 2>&1 &"
echo ""
echo "查看日志: tail -f f1_reminder.log"
echo ""