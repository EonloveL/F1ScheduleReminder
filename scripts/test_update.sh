#!/bin/bash
# 快速测试数据更新脚本

echo "========================================"
echo "F1赛道数据更新 - 快速测试"
echo "========================================"
echo ""

cd "$(dirname "$0")"

echo "1️⃣  测试API连接..."
python3 update_data.py --test-api

echo ""
echo "2️⃣  检查当前数据状态..."
python3 update_data.py --check

echo ""
echo "3️⃣  是否执行数据更新? (y/n)"
read -r response

if [ "$response" = "y" ] || [ "$response" = "Y" ]; then
    echo ""
    echo "3️⃣  执行数据更新..."
    python3 update_data.py
    echo ""
    echo "✅ 更新完成！"
else
    echo ""
    echo "⏭️  跳过更新"
fi

echo ""
echo "========================================"
echo "提示: 使用 'python3 update_data.py --force' 强制更新所有数据"
echo "========================================"
