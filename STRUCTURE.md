# 项目结构说明

本文档详细说明F1赛程提醒机器人的项目结构。

## 📁 目录结构

```
F1ScheduleReminder/                    # 项目根目录
│
├── common/                             # 共用核心模块（Python包）
│   ├── __init__.py                    # 包初始化，导出F1API和ReminderScheduler
│   ├── f1_api.py                      # F1数据API封装（Ergast API）
│   └── scheduler.py                   # 定时任务调度器（APScheduler）
│
├── wecom_version/                      # 企业微信版本 ⭐推荐服务器部署
│   ├── main.py                        # 入口文件，包含完整代码和配置
│   ├── requirements.txt               # 依赖：requests, APScheduler, pytz
│   ├── README.md                      # 版本详细说明
│   └── deploy.sh                      # Linux部署脚本（可选）
│
├── pushplus_version/                   # PushPlus版本 ⭐推荐个人使用
│   ├── main.py                        # 入口文件
│   ├── requirements.txt               # 依赖
│   └── README.md                      # 版本详细说明
│
├── wechat_version/                     # 个人微信版本 ⚠️仅本地测试
│   ├── main.py                        # 入口文件
│   ├── requirements.txt               # 依赖（包含itchat）
│   └── README.md                      # 版本详细说明
│
├── README.md                           # 项目总说明（选择版本、快速开始）
├── DEPLOY_GUIDE.md                     # 详细部署指南（服务器部署步骤）
├── start.sh                            # Linux/Mac快速启动脚本
├── start.bat                           # Windows快速启动脚本
└── .gitignore                          # Git忽略配置

# 以下文件为旧版兼容保留，新用户使用版本目录
├── main.py                             # 旧版单文件入口（兼容）
├── config.py                           # 旧版配置文件（兼容）
├── f1_api.py                           # 旧版F1 API（兼容）
├── scheduler.py                        # 旧版调度器（兼容）
├── wecom_bot.py                        # 旧版企业微信（兼容）
├── pushplus_bot.py                     # 旧版PushPlus（兼容）
└── wechat_bot.py                       # 旧版微信（兼容）
```

## 🎯 版本目录详解

### 1. common/ - 共用核心模块

**作用**: 包含所有版本共用的核心功能

**文件**:
- `__init__.py`: 包初始化，导出F1API和ReminderScheduler类
- `f1_api.py`: F1数据获取模块，封装Ergast API
- `scheduler.py`: 定时任务调度器，使用APScheduler

**使用方式**: 在各版本的main.py中通过`from common import F1API, ReminderScheduler`导入

### 2. wecom_version/ - 企业微信版本

**适用场景**: 
- 服务器长期运行
- 企业环境
- 需要高稳定性

**包含文件**:
- `main.py`: 完整独立程序，包含WeComBot类和主逻辑
- `requirements.txt`: 仅依赖requests, APScheduler, pytz（无itchat）
- `README.md`: 详细的配置和部署说明
- `deploy.sh`: Linux一键部署脚本

**配置方式**: 编辑main.py中的WECOM_WEBHOOK_URL

### 3. pushplus_version/ - PushPlus版本

**适用场景**:
- 快速部署
- 个人使用
- 想直接发到个人微信

**包含文件**:
- `main.py`: 完整独立程序，包含PushPlusBot类
- `requirements.txt`: 依赖
- `README.md`: 配置说明

**配置方式**: 编辑main.py中的PUSHPLUS_TOKEN

### 4. wechat_version/ - 个人微信版本

**适用场景**:
- 本地测试
- 临时使用

**⚠️ 不推荐**: 服务器部署（稳定性差，需扫码登录）

**包含文件**:
- `main.py`: 包含WeChatBot类（使用itchat）
- `requirements.txt`: 依赖（包含itchat）
- `README.md`: 使用说明和警告

## 🚀 使用方式

### 本地开发

```bash
# 方法1: 使用启动脚本
./start.sh wecom    # 启动企业微信版本
./start.sh pushplus # 启动PushPlus版本

# 方法2: 直接进入版本目录
cd wecom_version
python main.py
```

### 服务器部署

**推荐方式**:
```bash
# 1. 上传整个项目到服务器
scp -r F1ScheduleReminder root@server:/opt/

# 2. 连接服务器
ssh root@server

# 3. 进入版本目录
cd /opt/F1ScheduleReminder/wecom_version

# 4. 安装依赖
pip3 install -r requirements.txt

# 5. 配置（编辑main.py）
nano main.py

# 6. 后台运行
nohup python3 main.py > output.log 2>&1 &
```

**Systemd服务方式**（推荐）:
```bash
# 查看DEPLOY_GUIDE.md获取详细步骤
```

## 📝 开发说明

### 如何修改共用模块

1. 修改 `common/f1_api.py` 或 `common/scheduler.py`
2. 所有版本会自动生效（因为都导入common模块）

### 如何添加新版本

1. 创建新目录：`mkdir new_version`
2. 复制模板：
   ```bash
   cp wecom_version/main.py new_version/main.py
   cp wecom_version/requirements.txt new_version/requirements.txt
   ```
3. 修改main.py：
   - 替换消息发送类（WeComBot → 你的新类）
   - 修改配置部分
4. 创建README.md说明

### 依赖管理

**共用依赖**（所有版本都需要）:
- requests: HTTP请求
- APScheduler: 定时任务
- pytz: 时区处理

**版本特定依赖**:
- wechat_version: itchat（微信登录）

## 🔍 文件依赖关系

```
wecom_version/main.py
    ├── from common import F1API, ReminderScheduler
    ├── import requests, APScheduler, pytz
    └── WeComBot类（本文件定义）

pushplus_version/main.py
    ├── from common import F1API, ReminderScheduler
    ├── import requests, APScheduler, pytz
    └── PushPlusBot类（本文件定义）

wechat_version/main.py
    ├── from common import F1API, ReminderScheduler
    ├── import itchat, APScheduler, pytz
    └── WeChatBot类（本文件定义）

common/f1_api.py
    └── import requests

common/scheduler.py
    └── import APScheduler, pytz
```

## 🎨 设计原则

1. **单一职责**: 每个版本目录包含该版本所需的所有代码
2. **DRY原则**: 共用逻辑抽取到common模块
3. **独立部署**: 每个版本可以单独部署到服务器
4. **易于维护**: 修改共用模块，所有版本生效

## 📦 打包发布

如果需要将某个版本单独打包：

```bash
# 进入版本目录
cd wecom_version

# 复制common目录（临时）
cp -r ../common .

# 打包
tar -czvf f1-reminder-wecom.tar.gz *

# 删除临时复制的common
rm -rf common
```

这样打包后的文件包含完整依赖，可以独立部署。

## ❓ FAQ

**Q: 为什么common目录在根目录，而不是在每个版本里？**

A: 共用模块放在根目录，避免重复代码。各版本通过sys.path.insert导入。

**Q: 如果只部署一个版本，需要上传整个项目吗？**

A: 是的，需要上传common目录和你选择的版本目录。

**Q: 可以同时运行多个版本吗？**

A: 可以！它们互不影响。只需为每个版本配置不同的服务名。

**Q: 旧版文件（根目录的main.py等）还有用吗？**

A: 保留是为了向后兼容，新用户建议使用版本目录。

---

**理解项目结构有助于更好地使用和维护代码！** 🏎️💨