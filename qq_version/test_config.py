"""
快速测试 go-cqhttp QQ机器人配置
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

print("=" * 60)
print("F1 Schedule Reminder - go-cqhttp QQ Version Config Test")
print("=" * 60)

# 测试1: 检查配置
try:
    print("\n[1/3] Checking configuration...")
    from qq_version.main import QQ_GROUP_ID, GO_CQHTTP_URL
    
    print("  QQ Group ID: {}".format(QQ_GROUP_ID))
    print("  go-cqhttp URL: {}".format(GO_CQHTTP_URL))
    
    if QQ_GROUP_ID == 123456789:
        print("  WARNING: Using default group ID, please change to your own")
    else:
        print("  OK: Group ID configured: {}".format(QQ_GROUP_ID))
        
except Exception as e:
    print("  ERROR: Configuration check failed: {}".format(e))
    sys.exit(1)

# 测试2: 检查依赖
try:
    print("\n[2/3] Checking dependencies...")
    import requests
    import pytz
    from apscheduler.schedulers.background import BackgroundScheduler
    print("  OK: All dependencies installed")
except ImportError as e:
    print("  ERROR: Missing dependency: {}".format(e))
    print("  Please run: pip install -r requirements.txt")
    sys.exit(1)

# 测试3: 检查 go-cqhttp 连接
try:
    print("\n[3/3] Checking go-cqhttp connection...")
    import requests
    
    response = requests.get("{}/get_version_info".format(GO_CQHTTP_URL), timeout=5)
    data = response.json()
    
    if data.get("retcode") == 0:
        version = data.get("data", {}).get("app_version", "unknown")
        print("  OK: go-cqhttp connected")
        print("  Version: {}".format(version))
    else:
        print("  ERROR: go-cqhttp returned error: {}".format(data))
        
except requests.exceptions.ConnectionError:
    print("  ERROR: Cannot connect to go-cqhttp ({})".format(GO_CQHTTP_URL))
    print("  Please make sure go-cqhttp is running")
    sys.exit(1)
except Exception as e:
    print("  ERROR: Connection exception: {}".format(e))
    sys.exit(1)

print("\n" + "=" * 60)
print("Configuration test passed!")
print("=" * 60)
print("\nNext steps:")
print("  1. Make sure go-cqhttp is logged in")
print("  2. Make sure bot QQ is in group {}".format(QQ_GROUP_ID))
print("  3. Run: python main.py")
print("\nNote:")
print("  - First run will send test message to QQ group")
print("  - Check logs: tail -f f1_reminder.log")
