#!/bin/bash
# F1赛程提醒机器人 - QQ官方版部署脚本

set -e

echo "=============================================="
echo "F1赛程提醒机器人 - QQ官方版 部署脚本"
echo "=============================================="

# 检查Python版本
echo ""
echo "[1/5] 检查Python版本..."
python_version=$(python3 --version 2>&1 || python --version 2>&1)
echo "  检测到: $python_version"

# 检查是否在正确目录
if [ ! -f "main.py" ]; then
    echo "❌ 错误: 请在 qq_official_version 目录中运行此脚本"
    exit 1
fi

echo "✓ 目录检查通过"

# 安装依赖
echo ""
echo "[2/5] 安装依赖..."
pip install -r requirements.txt -q
echo "✓ 依赖安装完成"

# 运行配置测试
echo ""
echo "[3/5] 运行配置测试..."
python test_config.py || exit 1

# 创建日志目录
echo ""
echo "[4/5] 创建日志目录..."
mkdir -p logs
echo "✓ 日志目录已创建"

# 创建启动脚本
echo ""
echo "[5/5] 创建启动脚本..."
cat > start.sh << 'EOF'
#!/bin/bash
cd "$(dirname "$0")"
echo "启动 F1赛程提醒机器人[QQ官方版]..."
echo "按 Ctrl+C 停止"
python main.py
EOF
chmod +x start.sh

cat > start.bat << 'EOF'
@echo off
cd /d "%~dp0"
echo 启动 F1赛程提醒机器人[QQ官方版]...
echo 按 Ctrl+C 停止
python main.py
pause
EOF

echo "✓ 启动脚本已创建"

echo ""
echo "=============================================="
echo "✅ 部署完成！"
echo "=============================================="
echo ""
echo "💡 启动方式："
echo "  Linux/Mac: ./start.sh"
echo "  Windows:   start.bat"
echo ""
echo "📝 查看日志："
echo "  tail -f f1_reminder.log"
echo ""
