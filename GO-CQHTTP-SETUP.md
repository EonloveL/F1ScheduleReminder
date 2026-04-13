# F1赛程提醒机器人 - go-cqhttp 账号配置指南

## 🚀 快速配置（3分钟完成）

### 步骤1：下载 go-cqhttp

**Windows用户**：
1. 访问：https://github.com/Mrs4s/go-cqhttp/releases
2. 下载 `go-cqhttp_windows_amd64.exe`
3. 新建文件夹（如 `E:\go-cqhttp`），把 exe 放进去

**Linux/Mac用户**：
```bash
mkdir -p /opt/go-cqhttp && cd /opt/go-cqhttp
wget https://github.com/Mrs4s/go-cqhttp/releases/latest/download/go-cqhttp_linux_amd64.tar.gz
tar -zxvf go-cqhttp_linux_amd64.tar.gz
chmod +x go-cqhttp
```

### 步骤2：初始化配置

**Windows**：
```cmd
cd E:\go-cqhttp
go-cqhttp_windows_amd64.exe
```

**Linux**：
```bash
cd /opt/go-cqhttp
./go-cqhttp
```

看到提示时输入：`0`（选择HTTP通信）

> 这会生成 `config.yml` 文件

### 步骤3：修改配置文件

编辑 `config.yml`，修改以下字段：

```yaml
account:
  # 你的QQ号（必须修改！）
  uin: 123456789
  
  # QQ密码（可选，建议留空扫码登录）
  password: ''
  
  # 其他保持默认...

servers:
  - http:
      # HTTP服务配置（保持默认即可）
      host: 127.0.0.1    # 本机访问用127.0.0.1，如需外网改为0.0.0.0
      port: 5700         # 端口
      # 其他保持默认...
```

**只改这两处**：
- `account.uin` → 你的QQ号
- `account.password` → 留空（推荐扫码登录）

### 步骤4：登录QQ

再次运行 go-cqhttp：

```bash
# Windows
go-cqhttp_windows_amd64.exe

# Linux
./go-cqhttp
```

**扫码登录**（推荐）：
1. 控制台显示二维码
2. 用手机QQ扫描
3. 确认登录

**密码登录**（如果设置了密码）：
1. 自动尝试登录
2. 可能需要短信验证（按提示操作）
3. 可能需要滑块验证（按提示操作）

### 步骤5：验证登录成功

看到以下信息表示成功：
```
登录成功: 你的QQ号
在线: 你的QQ号
开始处理HTTP请求
```

测试HTTP接口：
```bash
# Windows浏览器访问：
http://localhost:5700/get_version_info

# 或命令行
curl http://localhost:5700/get_version_info
```

应该返回JSON格式的版本信息。

### 步骤6：加入QQ群

1. 用手机QQ将登录的QQ号加入群：**513802183**
2. **建议设为群管理员**（以便@全体成员）

### 步骤7：启动F1机器人

```bash
cd E:\F1ScheduleReminder\qq_version
pip install -r requirements.txt
python main.py
```

看到以下信息表示成功：
```
✓ QQ机器人初始化成功 (群号: 513802183)
✓ F1 API初始化成功 (赛季: 2026)
✓ 应用启动成功！
```

---

## ⚠️ 重要安全提醒

### 账号选择（ critical！）

| 账号类型 | 风险等级 | 建议 |
|---------|---------|------|
| **新注册小号（<6个月）** | 🔴 极高 | ❌ 不要用，24小时内必封 |
| **注册6-12个月** | 🟡 中等 | ⚠️ 可用，但有风险 |
| **注册1年以上老号** | 🟢 低 | ✅ 推荐，相对稳定 |
| **注册2年+老号** | 🟢 很低 | 🌟 最佳选择 |
| **你的主号** | 🔴 极高 | ❌ 绝对不要用！ |

### 风控预防措施

1. **用老号**：至少注册1年以上，有正常聊天记录
2. **保持IP稳定**：不要频繁切换网络
3. **不要24小时在线**：可以晚上关机，早上再开
4. **养号**：如果是买的小号，先登录几天加几个好友
5. **开启设备锁**：手机QQ → 设置 → 账号安全 → 登录设备管理

### 配置文件保护

- **device.json**：保存了登录状态，不要删除！
- **session.token**：重要凭证，妥善保存
- **config.yml**：下次登录直接用，不需要重新配置

---

## 🔧 常见问题

### Q1: 提示"当前上网环境异常"
**原因**：腾讯风控检测到非常用登录

**解决**：
1. 先用手机QQ登录一次该账号
2. 等待2-4小时再试
3. 使用常用IP（家里或公司的网络）

### Q2: 需要短信验证
**解决**：按提示发送短信即可，这是正常的

### Q3: 提示"密码错误或账号被冻结"
**解决**：
1. 检查密码是否正确
2. 改config.yml里 `password: ''` 用扫码登录
3. 确认账号没有被QQ安全中心冻结

### Q4: HTTP接口访问不了
**解决**：
1. 检查防火墙是否拦截了5700端口
2. 检查config.yml里的host是否为 `0.0.0.0`（如需外网访问）
3. 重启go-cqhttp试试

### Q5: 被封号了怎么办？
**轻度限制**：手机QQ解除限制，发短信验证
**中度限制**：找好友辅助解封，或等待7-15天
**永久封号**：很少见，如需申诉：腾讯客服小程序 → 账号问题

---

## 📝 完整配置示例

### config.yml（最小可用配置）

```yaml
account:
  uin: 123456789              # 你的QQ号
  password: ''                # 留空，扫码登录
  encrypt: false
  status: 0
  relogin:
    delay: 3
    interval: 3
    max_times: 0

heartbeat:
  interval: 5

message:
  post_format: "string"

output:
  log_level: "warn"
  debug: false

default-middlewares:
  access-token: ""
  filter: ""
  rate-limit:
    enabled: false

servers:
  - http:
      disabled: false
      host: 127.0.0.1
      port: 5700
      max-row-concurrency: 1000
```

---

## 🎉 完成！

配置完成后，机器人会自动：
- ✅ 每天早上8:00检查并更新赛程
- ✅ 赛前提醒（练习赛30分钟前，正赛60分钟前）
- ✅ @全体成员提醒
- ✅ 比赛结束后推送结果

**查看日志**：`tail -f f1_reminder.log`

**重启方法**：Ctrl+C 停止，再运行 `python main.py`

---

## 💡 推荐部署方式

### 方案A：本地电脑（简单）
- 适合：个人使用，不需要24小时在线
- 操作：开机时手动启动 go-cqhttp 和 F1机器人
- 缺点：电脑关机时机器人离线

### 方案B：云服务器（推荐）
- 适合：需要24小时在线推送
- 推荐：腾讯云/阿里云（国内IP，风控较低）
- 配置：1核2G足够，Windows或Linux都可以

### 方案C：树莓派/NAS（极客）
- 适合：有硬件设备的用户
- 优点：24小时在线，电费低
- 注意：保持网络稳定

---

**还有问题？** 请检查：
1. go-cqhttp 是否正常登录？
2. QQ号是否在群 513802183 中？
3. 查看错误日志获取详细信息
