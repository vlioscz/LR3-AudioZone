#!/usr/bin/env python3
"""Verify an MP3 stream URL is live before pushing it to a LARA (play_test.py).
   python check_stream.py <url>
   e.g.  python check_stream.py http://ice1.somafm.com/groovesalad-128-mp3
"""
import sys, socket
from urllib.parse import urlparse

url = sys.argv[1] if len(sys.argv) > 1 else "http://ice1.somafm.com/groovesalad-128-mp3"
u = urlparse(url); host = u.hostname; port = u.port or 80
path = (u.path or "/") + (("?" + u.query) if u.query else "")
try:
    ip = socket.gethostbyname(host)
except Exception as e:
    print("DNS fail:", e); sys.exit(1)
try:
    s = socket.create_connection((ip, port), timeout=6)
    s.sendall((f"GET {path} HTTP/1.0\r\nHost: {host}:{port}\r\nUser-Agent: LR3\r\n"
               "Icy-MetaData: 0\r\nConnection: close\r\nAccept: */*\r\n\r\n").encode())
    data = s.recv(2048); s.close()
except Exception as e:
    print("connect/read fail:", e); sys.exit(1)
head = data.split(b"\r\n\r\n", 1)[0].decode("latin1", "replace")
print(f"resolved {host} -> {ip}:{port}{path}")
print("--- response head ---"); print(head[:700])
low = head.lower()
ok = ("audio/mpeg" in low) or ("icy" in low and "200" in low) or ("200 ok" in low)
print("--- verdict:", "LIVE MP3 STREAM" if ok else "unclear (inspect headers)")
