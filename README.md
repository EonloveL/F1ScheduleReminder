# F1 Schedule Reminder Bot - Multi-Version
# F1赛程提醒机器人 - 多版本

支持多种消息推送方式的F1赛程提醒机器人，各版本独立管理，方便部署。

## 📁 项目结构

```
F1ScheduleReminder/
├── common/                    # 共用核心模块
│   ├── __init__.py           # 包初始化
│   ├── f1_api.py             # F1数据API
│   └── scheduler.py          # 定时任务调度器
│
├── wecom_version/            # 企业微信版本 ⭐推荐
│   ├── main.py               # 入口文件
│   ├── requirements.txt      # 依赖
│   └── README.md             # 版本说明
│
├── pushplus_version/         # PushPlus版本 ⭐推荐
│   ├── main.py               # 入口文件
│   ├── requirements.txt      # 依赖
│   └── README.md             # 版本说明
│
├── wechat_version/           # 个人微信版本 ⚠️本地测试
│   ├── main.py               # 入口文件
│   ├── requirements.txt      # 依赖
│   └── README.md             # 版本说明
│
├── README.md                 # 本文件 - 总说明
└── .gitignore               # Git忽略配置
```

## 🎯 版本选择指南

| 版本 | 推荐度 | 适用场景 | 稳定性 | 配置难度 |
|------|--------|----------|--------|----------|
| **企业微信** | ⭐⭐⭐⭐⭐ | 服务器部署、企业环境 | 极高 | 中等 |
| **PushPlus** | ⭐⭐⭐⭐ | 快速部署、个人使用 | 高 | 简单 |
| **个人微信** | ⭐⭐ | 本地测试 | 低 | 复杂 |

## 🚀 快速开始

### 1. 选择版本

根据你的需求进入对应目录：

```bash
# 企业微信版本（推荐服务器部署）
cd wecom_version

# PushPlus版本（推荐个人使用）
cd pushplus_version

# 个人微信版本（仅本地测试）
cd wechat_version
```

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置

编辑 `main.py` 文件，填写必要的配置信息（Webhook URL 或 Token）。

### 4. 运行

```bash
# 测试运行
python main.py

# 后台运行（Linux/Mac）
nohup python main.py > output.log 2>&1 &

# Windows后台运行
# 使用 pythonw main.py 或计划任务
```

## 📖 详细文档

每个版本目录下都有独立的README.md，包含：
- 详细的配置说明
- 获取Token/Webhook的方法
- 部署步骤
- 常见问题

## 🛠️ 开发说明

### 共用模块

`common/` 目录包含共用的核心功能：
- `F1API`: F1数据获取（Ergast API）
- `ReminderScheduler`: 定时任务调度

如需修改核心功能，只需修改 `common/` 下的文件，所有版本都会生效。

### 添加新版本

如需添加新的推送方式：

1. 创建新目录：`mkdir new_version`
2. 创建 `main.py`、`requirements.txt`、`README.md`
3. 导入共用模块：`from common import F1API, ReminderScheduler`
4. 实现消息发送类（参考其他版本）

## 💻 服务器部署

所有版本都支持服务器部署，推荐配置：
- **服务器**: 阿里云/腾讯云轻量应用服务器（1核1G，约¥10/月）
- **系统**: Ubuntu 20.04/22.04 LTS
- **Python**: 3.8+

详细部署步骤请参考各版本的README.md。

## 🐳 Docker部署（可选）

如需Docker部署，在各版本目录下创建Dockerfile：

```dockerfile
FROM python:3.9-slim

WORKDIR /app

# 复制共用模块
COPY ../common ./common

# 复制当前版本文件
COPY . .

RUN pip install -r requirements.txt

CMD ["python", "main.py"]
```

## 📝 更新日志

### v2.0.0 (2024)
- ✨ 重构为多版本架构
- ✨ 提取共用核心模块
- ✨ 各版本独立管理
- 📝 完善各版本文档

### v1.0.0 (2024)
- ✨ 初始版本
- ✨ 支持多种推送方式

## 🤝 贡献

欢迎提交Issue和Pull Request！

## 📄 License

MIT License

---

**Enjoy F1! 🏎️💨**