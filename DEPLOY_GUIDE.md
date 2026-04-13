# F1赛程提醒机器人 - 部署指南

本文档详细说明如何将F1提醒机器人部署到服务器。

## 📋 准备工作

### 1. 选择版本

根据你的需求选择一个版本：

- **wecom_version** (企业微信版) - 最稳定，推荐服务器部署
- **pushplus_version** (PushPlus版) - 最简单，推荐个人使用
- **wechat_version** (个人微信版) - 不推荐服务器部署

### 2. 准备服务器

**推荐配置**：
- **服务器**: 阿里云/腾讯云轻量应用服务器
- **配置**: 1核1G / 40GB SSD
- **系统**: Ubuntu 20.04/22.04 LTS
- **价格**: 约 ¥9-15/月

**购买步骤**：
1. 访问阿里云或腾讯云官网
2. 搜索"轻量应用服务器"
3. 选择地域（建议离你近的）
4. 选择系统镜像：Ubuntu 22.04
5. 购买并记录服务器IP和密码

### 3. 准备推送服务

根据选择的版本，准备相应的Token或Webhook：

**企业微信版**：
- 下载企业微信APP
- 创建企业 → 创建群 → 添加机器人
- 复制Webhook URL

**PushPlus版**：
- 访问 http://www.pushplus.plus/
- 微信扫码登录
- 复制Token

## 🚀 部署步骤

### Step 1: 连接服务器

**Windows**: 使用 PuTTY 或 Xshell

**Mac/Linux**: 
```bash
ssh root@你的服务器IP
```

### Step 2: 安装Python环境

```bash
# 更新系统
apt update && apt upgrade -y

# 安装Python和pip
apt install python3 python3-pip python3-venv -y

# 验证安装
python3 --version
pip3 --version
```

### Step 3: 上传代码

**方式1: 使用Git**（推荐，如果你将代码推送到GitHub）

```bash
# 安装git
apt install git -y

# 克隆仓库
git clone https://github.com/你的用户名/F1ScheduleReminder.git
cd F1ScheduleReminder
```

**方式2: 使用SCP**（从本地上传）

在本地终端执行：
```bash
# 上传整个项目
scp -r F1ScheduleReminder root@你的服务器IP:/opt/

# 或者只上传特定版本
scp -r F1ScheduleReminder/wecom_version root@你的服务器IP:/opt/
scp -r F1ScheduleReminder/common root@你的服务器IP:/opt/
```

**方式3: 直接下载**（使用wget/curl）

```bash
cd /opt

# 下载zip文件（如果你发布了Release）
wget https://github.com/你的用户名/F1ScheduleReminder/archive/refs/tags/v2.0.0.zip
unzip v2.0.0.zip
mv F1ScheduleReminder-2.0.0 F1ScheduleReminder
```

### Step 4: 进入版本目录并安装依赖

```bash
cd /opt/F1ScheduleReminder/wecom_version  # 或 pushplus_version

# 安装依赖
pip3 install -r requirements.txt
```

### Step 5: 配置

编辑 `main.py`，配置你的Token或Webhook：

```bash
nano main.py
```

找到配置部分并修改：

**企业微信版**：
```python
WECOM_WEBHOOK_URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的实际Key"
```

**PushPlus版**：
```python
PUSHPLUS_TOKEN = "你的实际Token"
```

保存：`Ctrl+O`，然后 `Enter`，然后 `Ctrl+X`

### Step 6: 测试运行

```bash
python3 main.py
```

如果看到以下输出，说明成功了：
```
✓ F1 API初始化成功
✓ 机器人初始化成功
✓ 调度器初始化成功
✓ 应用启动成功！
```

你应该会收到一条微信消息：
```
✅ F1赛程提醒机器人已启动
```

按 `Ctrl+C` 停止测试。

### Step 7: 后台运行（重要）

**方式1: 使用nohup（简单）**

```bash
nohup python3 main.py > output.log 2>&1 &

# 查看日志
tail -f output.log

# 停止程序
ps aux | grep main.py
kill 进程ID
```

**方式2: 使用screen（推荐）**

```bash
# 安装screen
apt install screen -y

# 创建新会话
screen -S f1bot

# 运行程序
python3 main.py

# 按 Ctrl+A 然后按 D，detach会话

# 重新连接
screen -r f1bot

# 查看所有会话
screen -ls
```

**方式3: 使用systemd服务（最专业，推荐）**

创建服务文件：

```bash
nano /etc/systemd/system/f1-reminder.service
```

粘贴内容（根据你的版本和路径修改）：

```ini
[Unit]
Description=F1 Schedule Reminder Bot
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/F1ScheduleReminder/wecom_version
Environment=PYTHONPATH=/opt/F1ScheduleReminder
ExecStart=/usr/bin/python3 /opt/F1ScheduleReminder/wecom_version/main.py
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
systemctl start f1-reminder

# 设置开机自启
systemctl enable f1-reminder

# 查看状态
systemctl status f1-reminder

# 查看日志
journalctl -u f1-reminder -f

# 停止服务
systemctl stop f1-reminder
```

## 🐳 Docker部署（可选）

如果你更喜欢Docker：

**创建Dockerfile**（在项目根目录）：

```dockerfile
FROM python:3.9-slim

WORKDIR /app

# 安装依赖
COPY common/ ./common/
COPY wecom_version/requirements.txt .
RUN pip install -r requirements.txt

# 复制代码
COPY wecom_version/ .

CMD ["python", "main.py"]
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

## 🔍 运维监控

### 查看日志

```bash
# 如果使用nohup/screen
tail -f /opt/F1ScheduleReminder/wecom_version/f1_reminder.log

# 如果使用systemd
journalctl -u f1-reminder -f
```

### 重启服务

```bash
# systemd方式
systemctl restart f1-reminder

# 直接运行方式
# 先停止，然后重新运行
```

### 检查程序状态

```bash
# 查看Python进程
ps aux | grep python

# 查看网络连接
netstat -tlnp | grep python
```

## 🆘 常见问题

### Q: 程序启动后立即退出？

检查日志：`tail -f f1_reminder.log`

常见原因：
1. 配置未修改（Token/Webhook还是默认值）
2. 依赖未安装
3. common目录不存在

### Q: 如何更新代码？

```bash
cd /opt/F1ScheduleReminder

# 如果使用git
git pull

# 然后重启服务
systemctl restart f1-reminder
```

### Q: 服务器重启后程序没有自动启动？

确保使用了systemd并设置了开机自启：
```bash
systemctl enable f1-reminder
```

### Q: 如何备份配置？

```bash
# 备份main.py（包含配置）
cp main.py main.py.backup

# 或者单独备份配置
grep -E "^(WECOM|PUSHPLUS|REMINDER)" main.py > config.backup
```

### Q: 多个版本可以同时运行吗？

可以！为每个版本创建不同的systemd服务：
- f1-wecom.service（企业微信版）
- f1-pushplus.service（PushPlus版）

## 💰 费用总结

| 项目 | 费用 | 说明 |
|------|------|------|
| 代码 | ¥0 | 开源免费 |
| 推送服务 | ¥0 | 企业微信免费，PushPlus免费版够用 |
| 服务器 | ¥10-15/月 | 阿里云/腾讯云轻量服务器 |
| **总计** | **约 ¥120-180/年** | |

## 📝 安全建议

1. **修改默认SSH端口**：防止暴力破解
   ```bash
   nano /etc/ssh/sshd_config
   # 修改 Port 22 为其他端口
   systemctl restart sshd
   ```

2. **配置防火墙**：只开放必要端口
   ```bash
   ufw allow 22
   ufw enable
   ```

3. **定期备份**：重要配置文件备份到本地

4. **日志轮转**：防止日志文件过大
   ```bash
   logrotate -f /etc/logrotate.conf
   ```

---

**部署遇到问题？** 请查看日志文件或提交Issue 🏎️💨