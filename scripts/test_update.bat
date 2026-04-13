@echo off
chcp 65001 > nul
REM F1赛道数据更新 - Windows快速测试

echo ========================================
echo F1赛道数据更新 - 快速测试
echo ========================================
echo.

cd /d "%~dp0"

echo 1️⃣  测试API连接...
python update_data.py --test-api

echo.
echo 2️⃣  检查当前数据状态...
python update_data.py --check

echo.
echo 是否执行数据更新? (y/n)
set /p response=""

if /i "%response%"=="y" (
    echo.
    echo 3️⃣  执行数据更新...
    python update_data.py
    echo.
    echo ✅ 更新完成！
) else (
    echo.
    echo ⏭️  跳过更新
)

echo.
echo ========================================
echo 提示: 使用 'python update_data.py --force' 强制更新所有数据
echo ========================================
pause
