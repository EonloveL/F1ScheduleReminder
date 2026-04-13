"""
使用botpy库发送群消息
"""
import asyncio
import botpy
from botpy.message import GroupMessage
import sys

import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

APPID = os.getenv("QQ_APPID", "YOUR_APPID_HERE")
SECRET = os.getenv("QQ_APP_SECRET", "YOUR_SECRET_HERE")
GROUP_ID = os.getenv("QQ_GROUP_ID", "YOUR_GROUP_ID_HERE")

class MyClient(botpy.Client):
    async def on_ready(self):
        print(f"[OK] Robot [{self.robot.name}] is ready!")
        print(f"[INFO] Robot ID: {self.robot.id}")
        
        # 尝试发送群消息
        try:
            print(f"\n[INFO] Trying to send message to group {GROUP_ID}...")
            result = await self.api.post_group_message(
                group_id=GROUP_ID,
                content="Hello from F1 Bot!",
                msg_type=0
            )
            print(f"[SUCCESS] Message sent!")
            print(f"[RESULT] {result}")
        except Exception as e:
            print(f"[ERROR] Failed to send: {e}")
            import traceback
            traceback.print_exc()
        
        # 发送后退出
        print("\n[INFO] Closing connection...")
        await self.close()
        sys.exit(0)

# 配置 intents
intents = botpy.Intents()
client = MyClient(intents=intents)

# 运行
print("="*60)
print("QQ Bot Test - Using botpy")
print("="*60)
print(f"AppID: {APPID}")
print(f"Group: {GROUP_ID}")
print("="*60)

# 设置超时
try:
    client.run(appid=APPID, secret=SECRET)
except Exception as e:
    print(f"\n[ERROR] {e}")
    import traceback
    traceback.print_exc()
