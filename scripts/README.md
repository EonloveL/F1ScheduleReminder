# F1赛道数据自动更新脚本

自动从f1api.dev获取最新赛道信息并更新JSON文件。

## 📋 功能特性

- ✅ 自动从f1api.dev获取最新赛道数据
- ✅ 自动更新圈速记录、赛道长度等信息
- ✅ 支持数据备份（自动创建备份文件）
- ✅ 智能更新策略（跳过近期更新的数据）
- ✅ 数据质量检查（检测未来年份等异常数据）
- ✅ 支持手动和自动运行

## 🚀 使用方法

### 1. 基本更新

```bash
cd scripts
python update_data.py
```

**效果**：只更新缺失或超过30天未更新的数据

### 2. 强制更新所有数据

```bash
python update_data.py --force
```

**效果**：更新所有赛道数据，无论上次更新时间

### 3. 检查数据状态

```bash
python update_data.py --check
```

**效果**：显示当前数据状态报告，包括：
- 数据文件位置
- 赛道总数
- 需要更新的数据数量
- 数据质量问题（如未来年份记录）

### 4. 测试API连接

```bash
python update_data.py --test-api
```

**效果**：测试与f1api.dev的连接是否正常

## 📊 更新内容

脚本会更新以下信息：

| 字段 | 说明 | 来源 |
|------|------|------|
| `lap_record.time` | 圈速记录时间 | f1api.dev |
| `lap_record.driver` | 圈速记录车手 | f1api.dev |
| `lap_record.year` | 圈速记录年份 | f1api.dev |
| `circuitLength` | 赛道长度 | f1api.dev |
| `numberOfLaps` | 正赛圈数 | f1api.dev |
| `firstParticipationYear` | 首次办赛年份 | f1api.dev |
| `last_updated` | 最后更新时间 | 脚本自动生成 |

**注意**：赛道名称、国旗、中文翻译等信息保持不变。

## ⏰ 建议的更新频率

| 场景 | 建议频率 | 命令 |
|------|---------|------|
| **常规维护** | 每月一次 | `python update_data.py` |
| **赛季开始前** | 3月更新一次 | `python update_data.py --force` |
| **重大比赛后** | 即时更新 | `python update_data.py` |
| **日常检查** | 每周查看 | `python update_data.py --check` |

## 🔄 自动化更新（Linux/Mac）

### 方法1：使用Cron定时任务

```bash
# 编辑crontab
crontab -e

# 添加每月1号自动更新（每月1日凌晨2点）
0 2 1 * * cd /path/to/F1ScheduleReminder/scripts && python3 update_data.py

# 或每周一检查并更新（每周一上午9点）
0 9 * * 1 cd /path/to/F1ScheduleReminder/scripts && python3 update_data.py
```

### 方法2：使用Systemd定时器

创建定时器文件：

```bash
sudo nano /etc/systemd/system/f1-update-data.timer
```

内容：

```ini
[Unit]
Description=Update F1 circuit data weekly

[Timer]
OnCalendar=weekly
Persistent=true

[Install]
WantedBy=timers.target
```

创建服务文件：

```bash
sudo nano /etc/systemd/system/f1-update-data.service
```

内容：

```ini
[Unit]
Description=Update F1 circuit data

[Service]
Type=oneshot
WorkingDirectory=/path/to/F1ScheduleReminder/scripts
ExecStart=/usr/bin/python3 update_data.py
```

启用定时器：

```bash
sudo systemctl daemon-reload
sudo systemctl enable f1-update-data.timer
sudo systemctl start f1-update-data.timer

# 查看状态
systemctl list-timers
```

## 📁 文件说明

```
scripts/
├── update_data.py          # 主脚本
└── update_data.py.backup.*  # 自动生成的备份文件
```

## 🛠️ 技术细节

### 数据源

- **主要源**：f1api.dev API
- **备用源**：（可扩展）
- **映射表**：Ergast API ID ↔ f1api.dev ID

### 更新策略

1. **智能更新**：默认只更新30天以上的数据
2. **强制更新**：`--force` 参数更新所有数据
3. **增量更新**：保留原有数据中的额外字段（如location）

### 数据备份

每次更新前自动创建备份：
- 备份文件名：`circuits_data.json.backup.YYYYMMDD_HHMMSS`
- 备份位置：与原始文件同目录
- 保留策略：手动管理，建议保留最近3-5个备份

## 🔍 故障排查

### 问题1：API连接失败

**症状**：
```
✗ suzuka: Connection timeout
```

**解决**：
1. 检查网络连接
2. 测试API可用性：`python update_data.py --test-api`
3. 检查f1api.dev是否在线
4. 稍后重试

### 问题2：更新后数据异常

**症状**：圈速记录显示未来年份

**解决**：
1. 检查原始数据：`python update_data.py --check`
2. 如果发现未来年份，手动修改JSON文件
3. 或等待下次比赛后自动更新

### 问题3：更新后程序报错

**症状**：主程序无法读取JSON文件

**解决**：
1. 检查JSON格式是否正确
2. 恢复备份文件
3. 重新运行更新脚本

## 📝 更新日志

### v1.0.0 (2024)
- ✨ 初始版本
- ✨ 支持f1api.dev数据源
- ✨ 自动备份功能
- ✨ 数据质量检查

## 🤝 贡献

如需添加新的数据源或功能，欢迎提交PR！

---

**保持数据新鲜，享受F1！** 🏎️💨
