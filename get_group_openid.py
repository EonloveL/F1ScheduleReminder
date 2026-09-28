"""
获取机器人所在群的 group_openid
运行后：把机器人拉进测试群，或在群里 @机器人 发一条消息
脚本会打印 group_openid，捕获到后自动退出
"""
import asyncio
import sys
import os
from functools import partial
import botpy
from botpy.message import GroupMessage

print = partial(print, flush=True)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

APPID = os.getenv("QQ_APPID", "YOUR_APPID_HERE")
SECRET = os.getenv("QQ_APP_SECRET", "YOUR_SECRET_HERE")

captured = []


class MyClient(botpy.Client):
    async def on_ready(self):
        print(f"[OK] 机器人已上线: {self.robot.name} (id={self.robot.id})")
        print("[INFO] 等待群事件... 请在测试群中 @机器人 发一条消息，或将机器人重新拉群")

    async def on_group_add_robot(self, event):
        print(f"[EVENT] 机器人被拉入群!")
        print(f"  group_openid = {event.group_openid}")
        captured.append(event.group_openid)
        await self.close()
        os._exit(0)

    async def on_group_at_message_create(self, message: GroupMessage):
        print(f"[EVENT] 收到群@消息!")
        print(f"  group_openid = {message.group_openid}")
        print(f"  内容 = {message.content}")
        captured.append(message.group_openid)

        try:
            await message.reply(content=f"收到! 本群 group_openid = {message.group_openid}")
            print("[OK] 已回复群消息")
        except Exception as e:
            print(f"[WARN] 回复失败: {e}")

        await self.close()
        os._exit(0)


intents = botpy.Intents(public_messages=True)
client = MyClient(intents=intents)

print("=" * 60)
print("获取 group_openid")
print("=" * 60)
print(f"AppID: {APPID}")
print("等待事件（请在群里 @机器人 发消息）...")
print("=" * 60)

try:
    client.run(appid=APPID, secret=SECRET)
except SystemExit:
    pass
except Exception as e:
    print(f"[ERROR] {e}")
