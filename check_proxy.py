#!/usr/bin/env python3
"""Проверка прокси, который реально использует бот.

Примеры:
    python check_proxy.py http://user:pass@host:port
    PEPPER_TG_PROXY=... python check_proxy.py
"""
import os
import socket
import sys
from urllib.parse import urlparse

from config import CONFIG


def check_proxy(host: str, port: int) -> bool:
    """Проверить доступность прокси по TCP."""
    try:
        sock = socket.create_connection((host, port), timeout=5)
        sock.settimeout(5)
        sock.close()
        return True
    except Exception as e:
        print(f"Error checking {host}:{port}: {e}")
        return False

proxy_url = (sys.argv[1] if len(sys.argv) > 1 else
             os.environ.get("PEPPER_TG_PROXY", CONFIG.telegram_proxy)).strip()
if not proxy_url:
    print("Usage: python check_proxy.py PROXY_URL")
    print("or set PEPPER_TG_PROXY=http://user:pass@host:port")
    sys.exit(2)

parsed = urlparse(proxy_url)
if not parsed.hostname or not parsed.port:
    print("Invalid proxy URL; expected http://[user:pass@]host:port")
    sys.exit(2)
proxy_host = parsed.hostname
proxy_port = parsed.port

print(f"Checking proxy {proxy_host}:{proxy_port}...")
if check_proxy(proxy_host, proxy_port):
    print("✓ Proxy is reachable")
    print("Note: this checks TCP only; it does not verify Telegram/Pepper access.")
    sys.exit(0)
else:
    print("✗ Proxy is NOT reachable")
    print("\nPossible reasons:")
    print("1. Proxy server is down or the port is closed")
    print("2. Firewall blocks connection from this server")
    print("3. Proxy only accepts connections from an allowlisted IP")
    sys.exit(1)
