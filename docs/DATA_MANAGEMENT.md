# F1赛道数据管理指南

本文档说明如何管理和更新F1赛道数据，保障数据时效性。

## 📂 相关文件

```
F1ScheduleReminder/
├── common/
│   └── circuits_data.json          # 赛道数据文件
├── scripts/
│   ├── update_data.py              # 数据更新脚本 ⭐主要工具
│   ├── test_update.sh              # Linux/Mac快速测试
│   ├── test_update.bat             # Windows快速测试
│   └── README.md                   # 脚本详细说明
└── docs/
    └── DATA_MANAGEMENT.md          # 本文档
```

## 🎯 三种数据管理方式

### 方式1：自动更新脚本（推荐⭐）

**适用场景**：追求数据实时性，愿意定期运行脚本

**优点**：
- ✅ 自动从API获取最新数据
- ✅ 保留数据备份
- ✅ 智能更新策略

**使用步骤**：

```bash
# 1. 进入脚本目录
cd scripts

# 2. 查看当前数据状态
python update_data.py --check

# 3. 执行更新
python update_data.py

# 4. （可选）设置定时自动更新
# Linux/Mac - 每月自动更新
crontab -e
# 添加：0 2 1 * * cd /path/to/scripts && python3 update_data.py
```

### 方式2：手动编辑JSON（简单直接）

**适用场景**：偶尔更新，或修正特定数据

**使用步骤**：

1. 打开 `common/circuits_data.json`
2. 找到需要更新的赛道
3. 修改相应字段：
   ```json
   "lap_record": {
     "time": "1:28.983",        // 新圈速记录
     "driver": "维斯塔潘",       // 新车手
     "driver_en": "Max Verstappen",
     "year": 2024                // 新年份
   }
   ```
4. 保存文件

### 方式3：使用API版本主程序

**适用场景**：不想维护JSON文件

**使用步骤**：

```bash
# 使用API版本的main程序
cd wecom_version
python main_api.py  # 代替 main_json.py
```

**特点**：
- ✅ 每次发送通知时实时获取数据
- ✅ 无需维护JSON文件
- ⚠️ 依赖外部API稳定性

## 📅 建议的维护计划

### 每年3月（新赛季开始前）

```bash
# 1. 强制更新所有数据
python update_data.py --force

# 2. 检查数据质量
python update_data.py --check
```

### 每场比赛后（如果产生新纪录）

```bash
# 更新该赛道的数据
python update_data.py
```

### 每月例行检查

```bash
# 检查数据状态
python update_data.py --check
```

## 🔍 数据质量检查

运行检查命令：

```bash
python update_data.py --check
```

会显示以下信息：

```
============================================================
F1赛道数据状态报告
============================================================

数据文件: /path/to/circuits_data.json
赛道总数: 30

数据新鲜度:
  - 需要更新（超过30天）: 5 条
    silverstone: 45天前
    monza: 42天前
    ...

数据质量检查:
  - 未来年份记录: 3 条
  ⚠️  警告: 以下赛道有未来年份的圈速记录:
    - suzuka: 2025年
    - las_vegas: 2025年
      建议: 请检查并更新为实际的历史记录

============================================================
```

## 🚨 常见问题

### Q1: 更新后的数据还是显示2025年？

**原因**：f1api.dev的数据可能也不准确

**解决**：
1. 手动编辑JSON文件修正
2. 或等待真实比赛后再次更新

### Q2: 脚本运行失败？

**检查清单**：
- [ ] Python 3已安装
- [ ] 网络连接正常
- [ ] f1api.dev服务在线
- [ ] 有写入JSON文件的权限

### Q3: 如何恢复旧版本数据？

```bash
# 查看备份文件
ls -la ../common/circuits_data.json.backup.*

# 恢复特定备份
cp ../common/circuits_data.json.backup.20240115_120000 ../common/circuits_data.json
```

### Q4: 新增赛道如何添加？

1. 编辑 `scripts/update_data.py`
2. 在 `circuit_mappings` 字典中添加映射
3. 运行更新脚本

## 📊 数据字段说明

| 字段 | 说明 | 示例 | 更新频率 |
|------|------|------|----------|
| `name` | 中文赛道名 | 铃鹿赛道 | 不常变 |
| `name_en` | 英文赛道名 | Suzuka Circuit | 不常变 |
| `flag` | 国旗emoji | 🇯🇵 | 不常变 |
| `first_race` | 首次办赛年份 | 1987 | 不常变 |
| `lap_length_km` | 单圈长度 | 5.807 | 不常变 |
| `laps` | 正赛圈数 | 53 | 可能变动 |
| `race_distance_km` | 正赛距离 | 307.471 | 自动计算 |
| `lap_record.time` | 圈速记录 | 1:30.965 | **经常更新** |
| `lap_record.driver` | 记录车手 | 安东内利 | **经常更新** |
| `lap_record.year` | 记录年份 | 2025 | **经常更新** |
| `last_updated` | 最后更新 | 2024-01-15 | 自动更新 |

**重点更新字段**：`lap_record` 相关的三个字段

## 💡 最佳实践

1. **首次部署**：运行 `python update_data.py --force` 获取最新数据

2. **定期检查**：每月运行 `python update_data.py --check` 查看数据状态

3. **备份策略**：保留最近3个月的备份文件

4. **异常处理**：发现未来年份数据时，手动修正或等待真实比赛

5. **自动化**：设置定时任务自动更新（见上文）

## 🎯 推荐配置

**对于生产环境（推荐）**：
- 使用 `main_json.py`（JSON版本）
- 每月自动运行 `update_data.py`
- 定期检查数据质量

**对于测试环境**：
- 使用 `main_api.py`（API版本）
- 无需维护JSON文件
- 实时获取最新数据

## 📞 获取帮助

如遇到问题：
1. 查看 `scripts/README.md` 详细文档
2. 运行 `python update_data.py --test-api` 测试API
3. 查看备份文件恢复数据

---

**保持数据新鲜，享受每个比赛周！** 🏎️💨
