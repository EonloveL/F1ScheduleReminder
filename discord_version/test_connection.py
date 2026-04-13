"""
Discord连接诊断工具
用于排查连接问题
"""
import asyncio
import aiohttp
import ssl
import socket
import os

target = "discord.com"
port = 443

print("=" * 60)
print("Discord Connection Diagnostic")
print("=" * 60)

# Test 1: DNS resolution
print("\n[1] DNS Resolution Test...")
try:
    ip = socket.getaddrinfo(target, None)[0][4][0]
    print(f"  OK - {target} -> {ip}")
except Exception as e:
    print(f"  FAIL - DNS resolution failed: {e}")

# Test 2: TCP connection with proper handling
print("\n[2] TCP Connection Test...")
try:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10)
    sock.setblocking(True)
    sock.connect((target, port))
    print(f"  OK - TCP connection successful")
    sock.close()
except socket.timeout:
    print(f"  FAIL - TCP connection timeout")
except Exception as e:
    print(f"  FAIL - TCP connection error: {e}")

# Test 3: Standard socket HTTPS
print("\n[3] Socket HTTPS Test...")
import urllib.request
import urllib.error
try:
    response = urllib.request.urlopen(f"https://{target}", timeout=10)
    print(f"  OK - HTTPS request successful, status: {response.getcode()}")
except Exception as e:
    print(f"  FAIL - HTTPS request failed: {type(e).__name__}: {e}")

# Test 4: aiohttp HTTPS request with proxy
print("\n[4] aiohttp HTTPS Request Test...")
async def test_aiohttp():
    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"https://{target}") as response:
                print(f"  OK - HTTPS request successful, status: {response.status}")
    except Exception as e:
        print(f"  FAIL - HTTPS request failed: {type(e).__name__}: {e}")

asyncio.run(test_aiohttp())

# Test 5: Check proxy environment variables
print("\n[5] Proxy Environment Variables Check...")
http_proxy = os.environ.get('HTTP_PROXY', 'Not set')
https_proxy = os.environ.get('HTTPS_PROXY', 'Not set')
print(f"  HTTP_PROXY: {http_proxy}")
print(f"  HTTPS_PROXY: {https_proxy}")

# Test 6: SSL certificates
print("\n[6] SSL Certificate Check...")
try:
    import certifi
    print(f"  OK - certifi installed: {certifi.where()}")
except:
    print(f"  FAIL - certifi not installed")

# Test 7: Windows specific checks
print("\n[7] Windows Network Check...")
import platform
if platform.system() == "Windows":
    print(f"  OS: {platform.system()} {platform.release()}")
    try:
        import subprocess
        result = subprocess.run(['ping', '-n', '1', '-w', '3000', target], 
                              capture_output=True, text=True)
        if result.returncode == 0:
            print(f"  OK - Ping successful")
        else:
            print(f"  FAIL - Ping failed")
    except Exception as e:
        print(f"  WARN - Could not run ping: {e}")

print("\n" + "=" * 60)
print("Diagnostic Complete")
print("=" * 60)
print("\nTROUBLESHOOTING GUIDE:")
print("-" * 60)
print("If TCP/HTTPS tests fail:")
print("1. Check Windows Firewall - allow Python through firewall")
print("2. Check antivirus software - may block Python network access")
print("3. Try running as Administrator")
print("4. Try using a different network (mobile hotspot)")
print("5. Consider deploying to a cloud server instead")
print("\nFor Mainland China users:")
print("- Discord is blocked, must use VPN/proxy")
print("- Make sure VPN is in 'Global Mode' or 'TUN Mode'")
print("- Try Clash Verge with 'System Proxy' enabled")
print("=" * 60)
