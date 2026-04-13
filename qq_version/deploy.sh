#!/bin/bash
# F1赛程提醒机器人 - QQ版本一键部署脚本

set -e

echo "========================================"
echo "F1赛程提醒机器人[QQ版]一键部署"
echo "========================================"

# 检查root权限
if [ "$EUID" -ne 0 ]; then 
    echo "请使用 sudo 运行"
    exit 1
fi

# 1. 更新系统
echo ""
echo "[1/6] 更新系统..."
apt update && apt upgrade -y

# 2. 安装必要软件
echo ""
echo "[2/6] 安装必要软件..."
apt install -y wget tar screen python3 python3-pip

# 3. 安装 go-cqhttp
echo ""
echo "[3/6] 安装 go-cqhttp..."
if [ ! -d "/opt/go-cqhttp" ]; then
    mkdir -p /opt/go-cqhttp
    cd /opt/go-cqhttp
    
    # 检测架构
    ARCH=$(uname -m)
    if [ "$ARCH" = "x86_64" ]; then
        URL="https://github.com/Mrs4s/go-cqhttp/releases/latest/download/go-cqhttp_linux_amd64.tar.gz"
    elif [ "$ARCH" = "aarch64" ]; then
        URL="https://github.com/Mrs4s/go-cqhttp/releases/latest/download/go-cqhttp_linux_arm64.tar.gz"
    else
        echo "不支持的架构: $ARCH"
        exit 1
    fi
    
    wget "$URL" -O go-cqhttp.tar.gz
    tar -zxvf go-cqhttp.tar.gz
    chmod +x go-cqhttp
    rm go-cqhttp.tar.gz
    
    echo "✓ go-cqhttp 下载完成"
else
    echo "✓ go-cqhttp 已存在"
fi

# 4. 配置 go-cqhttp
echo ""
echo "[4/6] 配置 go-cqhttp..."
if [ ! -f "/opt/go-cqhttp/config.yml" ]; then
    cd /opt/go-cqhttp
    
    echo "请配置QQ号信息..."
    read -p "请输入QQ号: " QQ_NUMBER
    
    cat > config.yml << EOF
# go-cqhttp 默认配置文件
account:
  uin: $QQ_NUMBER
  password: ''
  encrypt: false
  status: 0
  relogin:
    delay: 3
    interval: 3
    max-times: 0
  
heartbeat:
  interval: 5

message:
  post-format: string
  ignore-invalid-cqcode: false
  force-fragment: false
  fix-url: false
  proxy-rewrite: ''
  report-self-message: false
  remove-reply-at: false
  extra-reply-data: false
  skip-mime-scan: false

output:
  log-level: info
  log-aging: 15
  log-force-new: true
  log-colorful: true
  debug: false

default-middlewares: &default
  access-token: ''
  filter: ''
  rate-limit:
    enabled: false
    frequency: 1
    bucket: 1

database:
  leveldb:
    enable: true
  sqlite3:
    enable: false
    cachettl: 3600000000000

servers:
  - http:
      host: 0.0.0.0
      port: 5700
      max-row-concurrency: 1000
      middlewares:
        <<: *default
EOF
    
    echo "✓ 配置文件已创建"
    echo ""
    echo "⚠️  重要：请手动运行以下命令登录QQ："
    echo "   cd /opt/go-cqhttp"
    echo "   ./go-cqhttp"
    echo ""
    echo "登录成功后，按 Ctrl+C 停止，然后继续部署"
    exit 0
else
    echo "✓ 配置文件已存在"
fi

# 5. 部署F1机器人
echo ""
echo "[5/6] 部署F1机器人..."
if [ ! -d "/opt/f1-bot" ]; then
    echo "请先将F1机器人代码上传到 /opt/f1-bot"
    echo "可以使用: scp -r qq_version root@你的服务器IP:/opt/f1-bot/"
    exit 1
fi

cd /opt/f1-bot/qq_version
pip3 install -r requirements.txt

# 6. 创建启动脚本
echo ""
echo "[6/6] 创建启动脚本..."

cat > /opt/start-f1-bot.sh << 'EOF'
#!/bin/bash
cd /opt/go-cqhttp
screen -dmS cqhttp ./go-cqhttp
sleep 5
cd /opt/f1-bot/qq_version
screen -dmS f1bot python3 main.py
echo "F1机器人已启动"
echo "查看 go-cqhttp: screen -r cqhttp"
echo "查看 F1机器人: screen -r f1bot"
EOF

chmod +x /opt/start-f1-bot.sh

cat > /opt/stop-f1-bot.sh << 'EOF'
#!/bin/bash
screen -S cqhttp -X quit 2>/dev/null
screen -S f1bot -X quit 2>/dev/null
pkill -f go-cqhttp
pkill -f "python3 main.py"
echo "F1机器人已停止"
EOF

chmod +x /opt/stop-f1-bot.sh

echo ""
echo "========================================"
echo "✓ 部署完成！"
echo "========================================"
echo ""
echo "使用说明："
echo "  启动: /opt/start-f1-bot.sh"
echo "  停止: /opt/stop-f1-bot.sh"
echo ""
echo "查看日志："
echo "  go-cqhttp: tail -f /opt/go-cqhttp/cqhttp.log"
echo "  F1机器人: tail -f /opt/f1-bot/qq_version/f1_reminder.log"
echo ""
echo "⚠️  重要提醒："
echo "  1. 请确保使用1年以上的老QQ号"
echo "  2. 不要用主号！"
echo "  3. 详细风控说明请查看 README.md"
echo ""
