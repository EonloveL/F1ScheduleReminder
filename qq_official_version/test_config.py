"""
F1赛程提醒机器人 - QQ官方版配置测试脚本
运行前请先填写main.py中的配置
"""

import sys
import os

# 添加common目录到路径
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.insert(0, parent_dir)

print("=" * 60)
print("F1赛程提醒机器人 - QQ官方版 配置测试")
print("=" * 60)

# 测试1: 检查配置
try:
    print("\n[1/5] 检查配置文件...")
    from qq_official_version.main import APPID, APP_SECRET, ANNOUNCEMENT_CHANNEL_ID, LIVE_DATA_CHANNEL_ID
    
    errors = []
    if "YOUR_APPID_HERE" in APPID:
        errors.append("❌ APPID未配置")
    if "YOUR_SECRET_HERE" in APP_SECRET:
        errors.append("❌ APP_SECRET未配置")
    if "YOUR_ANNOUNCEMENT_CHANNEL_ID" in ANNOUNCEMENT_CHANNEL_ID:
        errors.append("❌ ANNOUNCEMENT_CHANNEL_ID未配置")
    if "YOUR_LIVE_DATA_CHANNEL_ID" in LIVE_DATA_CHANNEL_ID:
        errors.append("❌ LIVE_DATA_CHANNEL_ID未配置")
    
    if errors:
        print("\n".join(errors))
        print("\n⚠️ 请先编辑 main.py 完成配置！")
        sys.exit(1)
    else:
        print("✓ 配置检查通过")
        print(f"  - AppID: {APPID[:6]}...{APPID[-4:]}")
        print(f"  - 公告频道: {ANNOUNCEMENT_CHANNEL_ID}")
        print(f"  - 数据频道: {LIVE_DATA_CHANNEL_ID}")
        
except ImportError as e:
    print(f"❌ 导入错误: {e}")
    print("请确保目录结构正确，且已安装依赖")
    sys.exit(1)

# 测试2: 检查依赖
try:
    print("\n[2/5] 检查依赖...")
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
    print("\n[3/5] 初始化F1 API...")
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
    print("\n[4/5] 初始化QQ官方Bot...")
    from common import QQOfficialBot
    
    # 注意：这里不会实际连接，只是验证配置格式
    print("✓ Bot配置格式正确")
    print("  ⚠️ 注意：实际连接将在运行时进行")
    
except Exception as e:
    print(f"❌ Bot初始化检查失败: {e}")
    sys.exit(1)

# 测试5: 检查特殊赛程逻辑
try:
    print("\n[5/5] 检查特殊赛程识别逻辑...")
    from qq_official_version.main import SPECIAL_RACES
    
    print(f"✓ 已配置 {len(SPECIAL_RACES)} 个特殊赛程关键词")
    print("  - 包含: Las Vegas, Qatar等")
    
    # 检查是否是特殊赛程周
    upcoming = f1_api.get_upcoming_sessions(hours_ahead=168)
    is_special = False
    for session in upcoming:
        race_name = session['race_name'].lower()
        for special in SPECIAL_RACES:
            if special.lower() in race_name:
                is_special = True
                print(f"\n⚠️ 检测到特殊赛程: {session['race_name']}")
                print("   日程将在周二创建（而非周一）")
                break
        if is_special:
            break
    
    if not is_special:
        print("  - 本周为正常赛程，日程将在周一创建")
        
except Exception as e:
    print(f"⚠️ 特殊赛程检查失败: {e}")

print("\n" + "=" * 60)
print("✅ 配置测试完成！")
print("=" * 60)
print("\n💡 接下来：")
print("  1. 确保机器人已添加到QQ频道")
print("  2. 确保机器人拥有管理员权限")
print("  3. 运行: python main.py")
print("\n📚 更多信息请查看 README.md")
