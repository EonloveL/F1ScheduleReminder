"""
Discord 连接诊断工具

排查：DNS 解析 / TCP 连通性 / Bot Token 有效性 / 频道可达性
用法：python test_connection.py  （读取同目录 .env 或环境变量）
"""
import os
import socket
import ssl
import sys

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

import requests

PROXY = os.getenv("PROXY_URL", "") or None
proxies = {"http": PROXY, "https": PROXY} if PROXY else None

print("=" * 60)
print("Discord Connection Diagnostic")
print("=" * 60)

print("\n[1] DNS 解析 discord.com ...")
try:
    ip = socket.getaddrinfo("discord.com", None)[0][4][0]
    print(f"  OK - discord.com -> {ip}")
except Exception as e:
    print(f"  FAIL - {e}")

print("\n[2] TCP 443 连接 ...")
try:
    sock = socket.create_connection(("discord.com", 443), timeout=10)
    ctx = ssl.create_default_context()
    ctx.wrap_socket(sock, server_hostname="discord.com").close()
    print("  OK - TLS 握手成功")
except Exception as e:
    print(f"  FAIL - {e}（大陆网络需配置 PROXY_URL）")

print("\n[3] API 可达性（GET /api/v10/gateway）...")
try:
    r = requests.get("https://discord.com/api/v10/gateway", timeout=15, proxies=proxies)
    print(f"  OK - HTTP {r.status_code} url={r.json().get('url')}")
except Exception as e:
    print(f"  FAIL - {e}")

token = os.getenv("DISCORD_BOT_TOKEN", "")
print("\n[4] Bot Token 校验（GET /users/@me）...")
if not token:
    print("  SKIP - DISCORD_BOT_TOKEN 未配置")
else:
    try:
        r = requests.get("https://discord.com/api/v10/users/@me",
                         headers={"Authorization": f"Bot {token}"},
                         timeout=15, proxies=proxies)
        if r.ok:
            me = r.json()
            print(f"  OK - 登录身份: {me.get('username')}#{me.get('discriminator')} (ID: {me.get('id')})")
        else:
            print(f"  FAIL - HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"  FAIL - {e}")

channel_ids = [x for x in os.getenv("DISCORD_CHANNEL_IDS", "").split(",") if x.strip()]
print("\n[5] 推送频道可达性 ...")
if not token or not channel_ids:
    print("  SKIP - 需要 DISCORD_BOT_TOKEN + DISCORD_CHANNEL_IDS")
else:
    for cid in channel_ids:
        try:
            r = requests.get(f"https://discord.com/api/v10/channels/{cid.strip()}",
                             headers={"Authorization": f"Bot {token}"},
                             timeout=15, proxies=proxies)
            if r.ok:
                print(f"  OK - #{r.json().get('name')} ({cid.strip()})")
            else:
                print(f"  FAIL - {cid.strip()}: HTTP {r.status_code}（Bot 未邀请进服务器或频道 ID 错误）")
        except Exception as e:
            print(f"  FAIL - {cid.strip()}: {e}")

print("\n" + "=" * 60)
print("诊断完成")
sys.exit(0)
