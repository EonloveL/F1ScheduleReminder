@echo off
chcp 65001 > nul
REM F1赛程提醒机器人 - Windows快速启动脚本
REM 使用方法: start.bat [版本名]
REM 示例: start.bat wecom

set VERSION=%1
if "%VERSION%"=="" set VERSION=wecom

echo ========================================
echo F1赛程提醒机器人启动器
echo ========================================
echo.

REM 检查版本目录是否存在
if not exist "%VERSION%" (
    echo ❌ 版本 '%VERSION%' 不存在
    echo.
    echo 可用版本:
    echo   - wecom       (企业微信版，推荐服务器)
    echo   - pushplus    (PushPlus版，推荐个人)
    echo   - wechat      (个人微信版，仅本地测试)
    echo.
    echo 使用方法: start.bat [版本名]
    pause
    exit /b 1
)

echo 🚀 启动 %VERSION% 版本...
echo.

cd /d "%VERSION%" || exit /b 1

REM 检查Python
python --version > nul 2>&1
if errorlevel 1 (
    echo ❌ 未找到Python，请先安装Python
    pause
    exit /b 1
)

echo 📦 检查依赖...
pip install -q -r requirements.txt

echo.
echo ⚙️  检查配置...

REM 检查配置（简单检查）
if "%VERSION%"=="wecom" (
    findstr "YOUR_KEY_HERE" main.py > nul
    if not errorlevel 1 (
        echo ⚠️  请先配置企业微信Webhook地址
        echo 编辑 wecom_version/main.py 中的 WECOM_WEBHOOK_URL
        pause
        exit /b 1
    )
) else if "%VERSION%"=="pushplus" (
    findstr "YOUR_TOKEN_HERE" main.py > nul
    if not errorlevel 1 (
        echo ⚠️  请先配置PushPlus Token
        echo 编辑 pushplus_version/main.py 中的 PUSHPLUS_TOKEN
        pause
        exit /b 1
    )
) else if "%VERSION%"=="wechat" (
    echo ⚠️  注意：个人微信版本仅推荐本地测试使用
)

echo ✓ 配置检查通过
echo.

REM 启动
echo 🎯 正在启动...
echo.
echo 提示：按 Ctrl+C 可以停止程序
echo.
python main.py

echo.
pause