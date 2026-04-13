# F1赛程提醒机器人 - 服务器部署指南

## 🚀 推荐方案对比

| 方案 | 推荐度 | 配置难度 | 稳定性 | 费用 | 适用场景 |
|------|--------|----------|--------|------|----------|
| **企业微信** | ⭐⭐⭐⭐⭐ | 中等 | 极高 | 免费 | 有企业微信的群 |
| **PushPlus** | ⭐⭐⭐⭐ | 简单 | 高 | 免费(200条/天) | 想直接发到个人微信 |
| **个人微信** | ⭐⭐ | 复杂 | 低 | 免费 | 仅在本地测试使用 |

**推荐**：服务器部署首选 **企业微信** 或 **PushPlus**

---

## 📋 部署前准备

### 1. 购买云服务器

**阿里云推荐**（选择其一）：

**方案A：轻量应用服务器**（最推荐）
- 配置：1核1G / 40GB SSD / 不限流量
- 价格：约 ¥9-14/月
- 系统：Ubuntu 22.04 LTS

**方案B：ECS突发性能实例 t6**
- 配置：1核1G / 1M带宽
- 价格：约 ¥8/月（按量付费更便宜）
- 系统：Ubuntu 22.04 LTS

**购买步骤**：
1. 访问 https://www.aliyun.com/
2. 搜索"轻量应用服务器"
3. 选择地域（建议选离你近的）
4. 选择系统镜像：Ubuntu 22.04
5. 购买并创建实例

### 2. 获取推送服务的密钥

选择你要使用的方案并获取密钥：

---

## 🤖 方案一：企业微信机器人（推荐）

### 配置步骤

**Step 1：创建企业**
1. 手机下载"企业微信"APP
2. 点击"全新创建企业"
3. 选择"个人组建团队"
4. 填写企业名称（如：F1提醒）
5. 完成创建

**Step 2：创建群机器人**
1. 在企业微信中创建一个群
2. 点击右上角"..." → "群机器人"
3. 点击"添加机器人"
4. 填写机器人名称（如：F1助手）
5. 点击"添加"后**复制Webhook地址**

**Step 3：配置代码**
```python
# config.py
NOTIFY_TYPE = "wecom"
WECOM_WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxxxxxx"
```

### 特点
- ✅ 完全免费，无限制
- ✅ 支持Markdown格式，消息美观
- ✅ 稳定性极高
- ❌ 需要下载企业微信APP
- ❌ 不能发到普通微信群

---

## 📱 方案二：PushPlus（简单直接）

### 配置步骤

**Step 1：注册账号**
1. 访问 http://www.pushplus.plus/
2. 使用微信扫码登录
3. 关注"PushPlus推送加"公众号

**Step 2：获取Token**
1. 登录后点击"一对一消息"
2. 找到你的Token（一串字母数字组合）
3. 复制Token

**Step 3：配置代码**
```python
# config.py
NOTIFY_TYPE = "pushplus"
PUSHPLUS_TOKEN = "你的Token"
```

### 特点
- ✅ 配置最简单，3分钟搞定
- ✅ 直接发到个人微信
- ✅ 支持HTML格式
- ⚠️ 免费版每天200条（对F1提醒完全够用）
- ⚠️ 第三方服务，稳定性略低于企业微信

---

## 🔧 服务器部署步骤

### Step 1：连接服务器

**Windows用户**：
1. 下载 PuTTY 或 Xshell
2. 输入服务器公网IP
3. 用户名：`root`
4. 密码：购买时设置的密码

**Mac/Linux用户**：
```bash
ssh root@你的服务器IP
```

### Step 2：安装Python环境

```bash
# 更新软件包
apt update && apt upgrade -y

# 安装Python和pip
apt install python3 python3-pip python3-venv -y

# 验证安装
python3 --version
pip3 --version
```

### Step 3：上传代码

**方法一：使用Git（推荐）**
```bash
# 在服务器上安装git
apt install git -y

# 克隆你的仓库（如果你上传到了GitHub/GitLab）
git clone https://github.com/你的用户名/F1ScheduleReminder.git
cd F1ScheduleReminder
```

**方法二：使用SFTP**
- Windows：使用 FileZilla 或 WinSCP
- 连接到服务器，上传整个项目文件夹

**方法三：直接创建文件**
```bash
mkdir F1ScheduleReminder
cd F1ScheduleReminder

# 然后使用nano或vim创建各个文件
nano config.py
# ... 粘贴代码
```

### Step 4：安装依赖

```bash
cd F1ScheduleReminder

# 创建虚拟环境（推荐）
python3 -m venv venv
source venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

**注意**：如果使用企业微信或PushPlus，需要更新requirements.txt：
```
# 删除 itchat，因为不需要了
requests>=2.28.0
APScheduler>=3.10.0
pytz>=2022.1
```

### Step 5：配置机器人

```bash
# 编辑配置文件
nano config.py
```

根据你选择的方案修改配置：

**企业微信方案**：
```python
NOTIFY_TYPE = "wecom"
WECOM_WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的密钥"
```

**PushPlus方案**：
```python
NOTIFY_TYPE = "pushplus"
PUSHPLUS_TOKEN = "你的Token"
```

### Step 6：测试运行

```bash
# 运行测试
python3 main.py
```

如果看到以下输出，说明成功了：
```
✓ F1 API初始化成功
✓ 消息机器人初始化成功
✓ 调度器初始化成功
✓ 应用启动成功！
```

你应该会收到一条微信消息：
```
✅ F1赛程提醒机器人已启动
```

按 `Ctrl+C` 停止测试。

### Step 7：后台运行（重要）

为了让程序在关闭SSH连接后继续运行：

**方法一：使用 screen（推荐）**
```bash
# 安装screen
apt install screen -y

# 创建新会话
screen -S f1bot

# 运行程序
python3 main.py

# 按 Ctrl+A 然后按 D， detach会话
# 程序会在后台继续运行

# 重新连接到会话
screen -r f1bot

# 查看所有会话
screen -ls
```

**方法二：使用 nohup**
```bash
# 运行程序
nohup python3 main.py > output.log 2>&1 &

# 查看运行状态
tail -f output.log

# 停止程序
ps aux | grep main.py
kill 进程ID
```

**方法三：使用 systemd 服务（最专业）**

创建服务文件：
```bash
nano /etc/systemd/system/f1reminder.service
```

粘贴内容：
```ini
[Unit]
Description=F1 Schedule Reminder Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/F1ScheduleReminder
Environment=PYTHONPATH=/root/F1ScheduleReminder
ExecStart=/root/F1ScheduleReminder/venv/bin/python3 main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

启动服务：
```bash
# 重载systemd
systemctl daemon-reload

# 启动服务
systemctl start f1reminder

# 设置开机自启
systemctl enable f1reminder

# 查看状态
systemctl status f1reminder

# 查看日志
journalctl -u f1reminder -f

# 停止服务
systemctl stop f1reminder
```

---

## 🐳 Docker部署（进阶）

如果你想使用Docker部署：

**创建 Dockerfile**：
```dockerfile
FROM python:3.9-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["python3", "main.py"]
```

**构建和运行**：
```bash
# 构建镜像
docker build -t f1-reminder .

# 运行容器
docker run -d --name f1bot --restart always f1-reminder

# 查看日志
docker logs -f f1bot
```

---

## 🔍 运维和监控

### 查看日志
```bash
# 如果使用screen
tail -f f1_reminder.log

# 如果使用nohup
tail -f output.log

# 如果使用systemd
journalctl -u f1reminder -f
```

### 重启服务
```bash
# screen方式
screen -r f1bot
# 按Ctrl+C停止，然后重新运行

# systemd方式
systemctl restart f1reminder
```

### 检查程序是否运行
```bash
# 查看Python进程
ps aux | grep python

# 查看网络连接（如果使用企业微信）
netstat -tlnp | grep python
```

---

## 💰 费用预估

**阿里云轻量应用服务器（1核1G）**：
- 包年包月：约 ¥108/年（¥9/月）
- 按量付费：约 ¥0.008/小时（约 ¥6/月，但需持续运行）

**流量费用**：
- 轻量应用服务器：不限流量
- ECS：1M带宽足够（每天请求API约1MB）

**总费用**：
- **约 ¥10-15/月**，一年约 ¥120-180

---

## 🆘 常见问题

**Q: 程序启动后没有收到微信消息？**
- 检查Webhook URL或Token是否正确
- 查看日志文件 f1_reminder.log 中的错误信息
- 尝试运行测试脚本验证配置

**Q: 服务器重启后程序没有自动启动？**
- 如果使用screen/nohup，需要手动重新启动
- 推荐使用systemd方式，设置开机自启

**Q: 如何修改提醒时间？**
- 编辑 config.py 中的 REMINDER_TIMES
- 重启程序生效

**Q: PushPlus显示"今日额度已用完"？**
- 免费版每天200条，对F1提醒完全够用（一周最多几十条）
- 如果不够，可以升级到付费版

**Q: 可以同时在多个群推送吗？**
- 企业微信：需要为每个群创建机器人，复制多个Webhook
- PushPlus：使用topic功能可以实现一对多推送

---

## 📞 获取帮助

如果遇到问题：
1. 查看日志文件 `f1_reminder.log`
2. 检查配置文件是否正确
3. 测试API连接：`python3 f1_api.py`
4. 测试消息推送：`python3 wecom_bot.py` 或 `python3 pushplus_bot.py`

祝你部署顺利！🏎️💨