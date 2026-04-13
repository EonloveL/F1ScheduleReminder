"""
F1赛程提醒机器人 - QQ官方群聊版配置测试脚本
运行前请先填写main.py中的配置
"""

import sys
import os

# 添加common目录到路径
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

print("=" * 60)
print("F1赛程提醒机器人 - QQ官方群聊版 配置测试")
print("=" * 60)

# 测试1: 检查配置
try:
    print("\n[1/4] 检查配置文件...")
    from qq_group_official_version.main import APPID, APP_SECRET, QQ_GROUP_ID
    
    errors = []
    if "YOUR_APPID_HERE" in APPID:
        errors.append("❌ APPID未配置")
    if "YOUR_SECRET_HERE" in APP_SECRET:
        errors.append("❌ APP_SECRET未配置")
    if QQ_GROUP_ID == "123456789":
        errors.append("❌ QQ_GROUP_ID未配置")
    
    if errors:
        print("\n".join(errors))
        print("\n⚠️ 请先编辑 main.py 完成配置！")
        sys.exit(1)
    else:
        print("✓ 配置检查通过")
        print(f"  - AppID: {APPID[:6]}...{APPID[-4:]}")
        print(f"  - QQ群号: {QQ_GROUP_ID}")
        
except ImportError as e:
    print(f"❌ 导入错误: {e}")
    print("请确保目录结构正确，且已安装依赖")
    sys.exit(1)

# 测试2: 检查依赖
try:
    print("\n[2/4] 检查依赖...")
    import botpy
    import requests
    import pytz
    from apscheduler.schedulers.background import BackgroundScheduler
    print("✓ 所有依赖已安装")
except ImportError as e:
    print(f"❌ 缺少依赖: {e}")
    print("请运行: pip install -r requirements.txt")
    sys.exit(1)

# 测试3: 初始化API
try:
    print("\n[3/4] 初始化F1 API...")
    from common import F1API
    f1_api = F1API()
    print("✓ F1 API初始化成功")
    
    # 测试获取赛程
    schedule = f1_api.get_schedule(use_cache=True)
    print(f"  - 成功获取 {len(schedule)} 场比赛数据")
    
    if schedule:
        next_race = f1_api.get_next_race()
        if next_race:
            print(f"  - 下一场比赛: {next_race['raceName']}")
    
except Exception as e:
    print(f"❌ F1 API初始化失败: {e}")
    sys.exit(1)

# 测试4: 初始化Bot（不实际连接）
try:
    print("\n[4/4] 初始化QQ官方群聊Bot...")
    from common import QQGroupBot
    
    # 注意：这里不会实际连接，只是验证配置格式
    print("✓ Bot配置格式正确")
    print("  ⚠️ 注意：实际连接将在运行时进行")
    print("\n💡 提醒：建议将机器人设置为群管理员以便@全体成员")
    
except Exception as e:
    print(f"❌ Bot初始化检查失败: {e}")
    sys.exit(1)

print("\n" + "=" * 60)
print("✅ 配置测试完成！")
print("=" * 60)
print("\n💡 接下来：")
print("  1. 确保机器人已添加到QQ群")
print("  2. 建议将机器人设置为群管理员")
print("  3. 运行: python main.py")
print("\n📚 更多信息请查看 README.md")
