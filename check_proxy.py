#!/usr/bin/env python3
"""Проверка доступности прокси с сервера."""
import socket
import sys

def check_proxy(host, port):
    """Проверить доступность прокси по TCP."""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(5)
        result = sock.connect_ex((host, port))
        sock.close()
        return result == 0
    except Exception as e:
        print(f"Error checking {host}:{port}: {e}")
        return False

proxy_host = "45.130.71.57"
proxy_port = 8000

print(f"Checking proxy {proxy_host}:{proxy_port}...")
if check_proxy(proxy_host, proxy_port):
    print("✓ Proxy is reachable")
    sys.exit(0)
else:
    print("✗ Proxy is NOT reachable")
    print("\nPossible reasons:")
    print("1. Proxy server is down")
    print("2. Firewall blocks connection from your server's location")
    print("3. Proxy only accepts connections from Russian IPs")
    print("\nSolution: Use a different proxy that accepts connections from Germany")
    sys.exit(1)
