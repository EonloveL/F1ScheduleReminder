# 企业微信版本 - F1赛程提醒机器人

使用企业微信机器人发送F1赛程提醒，适合服务器部署和企业环境。

## ✨ 特点

- ✅ **完全免费**，无消息数量限制
- ✅ **稳定性极高**，腾讯官方服务
- ✅ **支持Markdown**，消息格式丰富
- ✅ **无需登录**，通过Webhook直接发送
- ✅ **支持@所有人**

## 📋 配置步骤

### Step 1: 创建企业微信

1. 手机下载"企业微信"APP
2. 注册企业，选择"个人组建团队"（免费，最多5人）
3. 填写企业名称，如"F1提醒"

### Step 2: 创建群机器人

1. 在企业微信中创建一个群
2. 点击右上角"..." → "群机器人"
3. 点击"添加机器人"
4. 填写机器人名称，如"F1助手"
5. **复制Webhook地址**

### Step 3: 配置代码

编辑 `main.py`，找到以下配置：

```python
# 配置企业微信Webhook地址
WECOM_WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=YOUR_KEY_HERE"
```

将 `YOUR_KEY_HERE` 替换为你的实际Key。

### Step 4: 自定义提醒时间（可选）

```python
REMINDER_TIMES = {
    "fp1": 30,        # 练习赛前30分钟
    "qualifying": 30, # 排位赛前30分钟
    "race": 60,       # 正赛前60分钟
}
```

## 🚀 部署运行

### 本地运行

```bash
# 安装依赖
pip install -r requirements.txt

# 运行
python main.py
```

### 服务器部署

#### 方法1: 直接运行

```bash
# 上传到服务器
scp -r wecom_version root@your-server:/opt/

# 连接服务器
ssh root@your-server

# 进入目录
cd /opt/wecom_version

# 安装依赖
pip3 install -r requirements.txt

# 后台运行
nohup python3 main.py > output.log 2>&1 &

# 查看日志
tail -f f1_reminder.log
```

#### 方法2: Systemd服务（推荐）

创建服务文件：

```bash
sudo nano /etc/systemd/system/f1-wecom.service
```

粘贴内容：

```ini
[Unit]
Description=F1 Reminder Bot (WeCom)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/wecom_version
ExecStart=/usr/bin/python3 /opt/wecom_version/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

启动服务：

```bash
sudo systemctl daemon-reload
sudo systemctl start f1-wecom
sudo systemctl enable f1-wecom

# 查看状态
sudo systemctl status f1-wecom

# 查看日志
sudo journalctl -u f1-wecom -f
```

## 🔍 常见问题

### Q: 启动后没有收到消息？

1. 检查Webhook URL是否正确配置
2. 查看日志 `f1_reminder.log` 中的错误
3. 测试发送：运行 `python -c "from main import WeComBot; bot = WeComBot('你的URL'); bot.send_test_message()"`

### Q: 如何修改提醒时间？

编辑 `main.py` 中的 `REMINDER_TIMES` 字典，然后重启程序。

### Q: 如何停止程序？

```bash
# 如果是直接运行
ps aux | grep main.py
kill 进程ID

# 如果是systemd服务
sudo systemctl stop f1-wecom
```

### Q: 可以发送到多个群吗？

需要为每个群创建机器人，复制整个目录，修改不同的Webhook URL，分别运行。

## 📝 消息格式示例

启动消息：
```
✅ F1赛程提醒机器人已启动

将为您自动推送：
- 🏎️ 练习赛提醒
- ⏱️ 排位赛提醒
- 🏁 正赛提醒
- 📊 赛后结果
```

比赛提醒：
```
🏁 F1提醒 🏁

> 新加坡大奖赛
> 
> 📍 滨海湾市街赛道

正赛 即将开始

⏰ 2024年09月22日 周日 20:00 (北京时间)
```

## 💰 费用说明

- **代码**: 完全免费
- **推送服务**: 企业微信机器人完全免费
- **服务器**: 约 ¥10-15/月（阿里云/腾讯云轻量服务器）

---

**有问題请提交Issue** 🏎️💨