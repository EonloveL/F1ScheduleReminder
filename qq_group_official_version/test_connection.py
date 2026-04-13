"""测试QQ机器人连接"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from common.qq_group_bot import QQGroupBot

APPID = os.getenv("QQ_APPID", "YOUR_APPID_HERE")
SECRET = os.getenv("QQ_APP_SECRET", "YOUR_SECRET_HERE")
GROUP_ID = os.getenv("QQ_GROUP_ID", "YOUR_GROUP_ID_HERE")
USE_SANDBOX = os.getenv("USE_SANDBOX", "false").lower() == "true"

print("Testing QQ Group Bot...")
print(f"AppID: {APPID}")
print(f"Group ID: {GROUP_ID}")
print(f"Using sandbox environment: {USE_SANDBOX}")

bot = QQGroupBot(
    appid=APPID,
    secret=SECRET,
    group_id=GROUP_ID,
    use_sandbox=USE_SANDBOX
)

print("\nTrying to send test message...")
result = bot.send_test_message()
print(f"Send result: {result}")
