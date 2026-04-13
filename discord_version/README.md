# F1赛程提醒机器人 - Discord版本

使用Discord Bot发送F1赛程提醒消息到指定频道。

## 功能特性

- 🏎️ 练习赛提醒（提前30分钟）
- ⏱️ 排位赛提醒（提前30分钟）
- 🏁 正赛提醒（提前60分钟）
- 📊 赛后结果推送
- 📅 每日赛程汇总

## 快速开始

### 1. 创建Discord Bot

1. 访问 [Discord Developer Portal](https://discord.com/developers/applications)
2. 点击 "New Application" 创建新应用
3. 进入 "Bot" 页面，点击 "Add Bot"
4. 复制 **Token**（这个Token很重要，请妥善保存）
5. 在 "Privileged Gateway Intents" 中启用 **MESSAGE CONTENT INTENT**

### 2. 邀请Bot到服务器

1. 在 "OAuth2" -> "URL Generator" 页面：
   - 选择 scope: `bot`
   - 选择 permissions: `Send Messages`, `Embed Links`, `Read Message History`
2. 复制生成的URL，在浏览器中打开
3. 选择要添加的服务器和频道，授权

### 3. 获取频道ID

1. 在Discord中开启开发者模式：
   - 设置 -> 高级 -> 开启开发者模式
2. 右键点击目标频道 -> 复制频道ID

### 4. 安装依赖

```bash
pip install -r requirements.txt
```

### 5. 配置机器人

编辑 `main.py` 文件，修改以下配置：

```python
# Discord Bot Token
DISCORD_BOT_TOKEN = "YOUR_BOT_TOKEN_HERE"

# Discord频道ID
DISCORD_CHANNEL_ID = 1234567890123456789
```

### 6. 运行机器人

```bash
python main.py
```

## 目录结构

```
project/
├── common/
│   ├── __init__.py
│   ├── f1_api.py
│   └── scheduler.py
└── discord_version/
    ├── main.py
    ├── requirements.txt
    └── README.md
```

## 配置说明

### 提醒时间配置

```python
REMINDER_TIMES = {
    "fp1": 30,           # 第一节练习赛
    "fp2": 30,           # 第二节练习赛
    "fp3": 30,           # 第三节练习赛
    "qualifying": 30,    # 排位赛
    "sprint": 30,        # 冲刺赛
    "race": 60,          # 正赛（提前1小时）
}
```

### 代理配置（中国大陆用户）

如果遇到连接Discord超时的问题，请配置代理：

```python
PROXY_URL = "http://127.0.0.1:7890"  # 根据你的代理软件修改端口
```

支持的代理格式：
- HTTP代理: `http://127.0.0.1:7890`
- SOCKS5代理: `socks5://127.0.0.1:1080`

### 其他配置

```python
POST_RACE_DELAY = 30  # 比赛结束后多久推送结果（分钟）
TIMEZONE = "Asia/Shanghai"  # 时区
SEND_STARTUP_MESSAGE = True  # 启动时发送通知
```

## 常见问题

### Q: 连接Discord超时？
在中国大陆可能需要配置代理：
1. 编辑 `main.py`，设置 `PROXY_URL`
2. 确保你的代理软件正常运行
3. 重新运行程序

错误信息示例：
```
Cannot connect to host discord.com:443 ssl:default [信号灯超时时间已到]
```

### Q: Bot显示在线但不发送消息？
- 检查频道ID是否正确
- 确认Bot有发送消息的权限
- 检查日志文件了解详细错误

### Q: 如何修改提醒时间？
编辑 `main.py` 中的 `REMINDER_TIMES` 配置。

### Q: 如何添加更多频道？
可以修改 `DiscordBot` 类支持多频道，或创建多个Bot实例。

## 技术支持

如有问题，请查看日志文件 `f1_reminder.log` 获取详细错误信息。
