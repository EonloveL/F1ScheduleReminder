# PushPlus版本 v2.0 更新说明

## 新增功能：每周一比赛周预告

现在机器人会在每周一上午9:00自动检查本周是否有F1比赛，如果有，会推送包含以下内容的预告消息：

### 消息内容

1. **比赛信息**
   - 大奖赛名称 + 国旗
   - 赛道名称

2. **赛道详细信息**（从f1api.dev实时获取）
   - 首次办赛年份
   - 单圈长度（km）
   - 正赛圈数
   - 正赛距离（km）
   - 圈速记录（时间、车手、年份）

3. **本周完整赛程**（北京时间）
   - 一练、二练、三练
   - 排位赛
   - 冲刺赛（如有）
   - 正赛

### 推送时间

- **每周一上午 9:00**（北京时间）
- 如果本周没有比赛，则不会推送任何消息

## 完整的推送列表

现在比赛周你会收到以下消息：

| 时间 | 内容 | 说明 |
|------|------|------|
| **周一 9:00** | 🎉 比赛周预告 | 包含赛道信息+完整赛程 |
| 练习赛前30分钟 | 🏎️ 练习赛提醒 | FP1/FP2/FP3 |
| 排位赛前30分钟 | ⏱️ 排位赛提醒 | Qualifying |
| 冲刺赛前30分钟 | ⚡ 冲刺赛提醒 | Sprint（如有）|
| 正赛前60分钟 | 🏁 正赛提醒 | Race |
| 正赛结束后 | 📊 比赛结果 | 前十名+最快圈速 |

## 部署说明

### 全新部署

```bash
# 1. 上传 pushplus_standalone_v2.zip 到服务器
# 2. 解压并运行
cd /opt
unzip pushplus_standalone_v2.zip -d pushplus_bot
cd pushplus_bot
pip3 install -r requirements.txt

# 3. 配置Token
nano main.py
# 修改 PUSHPLUS_TOKEN = "你的Token"

# 4. 运行
python3 main.py
```

### 从旧版本升级

如果你已经在运行旧版本：

```bash
cd /opt/pushplus_standalone

# 停止现有程序
ps aux | grep main.py
kill <进程ID>

# 备份配置
cp main.py main.py.backup

# 上传新的zip文件并解压
unzip -o pushplus_standalone_v2.zip

# 恢复你的Token配置（或者重新编辑）
# nano main.py

# 重新运行
nohup python3 main.py > output.log 2>&1 &
```

## 技术细节

### 新增组件

1. **CircuitsAPI类**：封装f1api.dev的赛道数据API
2. **WeeklyReminderScheduler类**：继承自ReminderScheduler，添加周一预告功能
3. **send_weekly_preview方法**：格式化并发送比赛周预告消息

### 依赖项

```
requests==2.27.1
APScheduler==3.9.1
pytz==2022.1
python-dateutil==2.8.2  # 新增：用于Python 3.6兼容
```

## 测试

如果你想测试周一预告功能，可以临时修改调度器中的时间：

```python
# 在 WeeklyReminderScheduler.add_weekly_preview_job() 中
# 将 trigger=CronTrigger(day_of_week='mon', hour=9, minute=0)
# 改为 trigger=CronTrigger(day_of_week='fri', hour=16, minute=0) 
# 等等待几分钟后查看是否收到消息
```

## 注意事项

1. 赛道数据从f1api.dev实时获取，如果API暂时不可用，会显示"未知"
2. 周一预告消息使用HTML格式，支持PushPlus的富文本显示
3. 如果本周是特殊比赛（如拉斯维加斯、卡塔尔等跨周比赛），建议手动检查预告时间是否正确

---

**版本**: v2.0  
**更新日期**: 2026-04-13  
**功能**: 新增周一比赛周预告功能
