# PushPlus版本 - F1赛程提醒机器人

通过PushPlus将消息推送到个人微信，配置最简单，适合快速部署。

## ✨ 特点

- ✅ **配置最简单**，3分钟搞定
- ✅ **直接发到个人微信**，无需额外APP
- ✅ **支持HTML格式**，消息美观
- ✅ **免费额度充足**（200条/天，F1提醒完全够用）

## 📋 配置步骤

### Step 1: 注册PushPlus

1. 访问 http://www.pushplus.plus/
2. 使用微信扫码登录
3. 关注"PushPlus推送加"公众号

### Step 2: 获取Token

1. 登录后点击"一对一消息"
2. 复制你的Token（一串字母数字组合）

### Step 3: 配置代码

编辑 `main.py`，找到以下配置：

```python
# 配置PushPlus Token
PUSHPLUS_TOKEN = "YOUR_TOKEN_HERE"
```

将 `YOUR_TOKEN_HERE` 替换为你的实际Token。

### Step 4: 一对多推送（可选）

如果想推送给多个人：

1. 在PushPlus控制台创建"群组"
2. 复制"群组编码"
3. 配置：

```python
PUSHPLUS_TOPIC = "你的群组编码"  # 默认None表示一对一
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

```bash
# 上传到服务器
scp -r pushplus_version root@your-server:/opt/

# 连接服务器并运行
ssh root@your-server
cd /opt/pushplus_version
pip3 install -r requirements.txt
nohup python3 main.py > output.log 2>&1 &
```

### Systemd服务

```bash
# 创建服务
sudo nano /etc/systemd/system/f1-pushplus.service
```

内容：

```ini
[Unit]
Description=F1 Reminder Bot (PushPlus)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/pushplus_version
ExecStart=/usr/bin/python3 /opt/pushplus_version/main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

启动：

```bash
sudo systemctl daemon-reload
sudo systemctl start f1-pushplus
sudo systemctl enable f1-pushplus
```

## 🔍 常见问题

### Q: 免费版够用吗？

完全够用！免费版每天200条限制：
- 一场F1比赛约6-8个环节
- 每周约1场比赛
- 每月约4场比赛
- 每月约30条消息，远低于200条限制

### Q: 额度用完了怎么办？

1. 升级到付费版（¥9.9/月，无限条数）
2. 或等待第二天重置额度

### Q: 可以推送给多个人吗？

可以！使用"一对多"功能：
1. 在PushPlus创建群组
2. 获取群组编码
3. 配置 `PUSHPLUS_TOPIC`
4. 让其他人关注公众号并加入群组

### Q: 消息格式可以自定义吗？

可以修改 `main.py` 中的消息模板，支持HTML格式。

## 💰 费用说明

- **代码**: 完全免费
- **PushPlus**: 
  - 免费版：200条/天（够用）
  - 付费版：¥9.9/月（无限条数）
- **服务器**: 约 ¥10/月

---

**配置最简单，推荐个人用户** 🏎️💨