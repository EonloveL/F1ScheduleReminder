"""
简单测试QQ群消息API
直接使用QQ官方API测试
"""
import requests
import json
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

APPID = os.getenv("QQ_APPID", "YOUR_APPID_HERE")
SECRET = os.getenv("QQ_APP_SECRET", "YOUR_SECRET_HERE")
GROUP_ID = os.getenv("QQ_GROUP_ID", "YOUR_GROUP_ID_HERE")

print("=== QQ群消息API测试 ===")
print(f"AppID: {APPID}")
print(f"Group: {GROUP_ID}")

# 1. 获取Token
print("\n1. 获取Access Token...")
token_resp = requests.post(
    "https://bots.qq.com/app/getAppAccessToken",
    json={"appId": APPID, "clientSecret": SECRET},
    headers={"Content-Type": "application/json"}
)
print(f"Token响应: {token_resp.status_code}")

if token_resp.status_code == 200:
    token_data = token_resp.json()
    if "access_token" in token_data:
        access_token = token_data["access_token"]
        print(f"Token获取成功!")
        
        # 2. 尝试发送消息
        print("\n2. 尝试发送群消息...")
        
        headers = {
            "Authorization": f"QQBot {access_token}",
            "Content-Type": "application/json"
        }
        
        # 最简单的消息
        data = {"content": "测试消息", "msg_type": 0}
        
        # 方法A: 正式环境
        print("\n方法A: 正式环境 /v2/groups/{group_id}/messages")
        url = f"https://api.sgroup.qq.com/v2/groups/{GROUP_ID}/messages"
        resp = requests.post(url, headers=headers, json=data, timeout=10)
        print(f"状态: {resp.status_code}")
        print(f"响应: {resp.text}")
        
        # 方法B: 沙箱环境
        print("\n方法B: 沙箱环境 /v2/groups/{group_id}/messages")
        url_sandbox = f"https://sandbox.api.sgroup.qq.com/v2/groups/{GROUP_ID}/messages"
        resp2 = requests.post(url_sandbox, headers=headers, json=data, timeout=10)
        print(f"状态: {resp2.status_code}")
        print(f"响应: {resp2.text}")
        
        # 方法C: 使用openid格式（尝试）
        print("\n方法C: 尝试不同的group_id格式")
        # 有些API需要 "101" 前缀
        group_id_alt = f"101{GROUP_ID}"
        url_alt = f"https://api.sgroup.qq.com/v2/groups/{group_id_alt}/messages"
        resp3 = requests.post(url_alt, headers=headers, json=data, timeout=10)
        print(f"Group ID: {group_id_alt}")
        print(f"状态: {resp3.status_code}")
        print(f"响应: {resp3.text}")
        
    else:
        print(f"Token获取失败: {token_data}")
else:
    print(f"获取Token失败: {token_resp.status_code}")
