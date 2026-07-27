#!/usr/bin/env python3
"""Play test: run the REAL lr3_audiozone/lr3ctl/slimproto.py server, then push a strm-s so the LARA
plays an MP3 URL. The real server does the full handshake (vers/setd/aude/audg) + strm-t heartbeat,
which keeps the LARA connected; we push a strm-s pointing straight at a (public) MP3 stream so the
LARA fetches audio directly — no local Icecast / extra firewall port needed for the test.

  python play_test.py <our-ip> [mp3-url]
  e.g.  python play_test.py 10.0.0.80 http://ice1.somafm.com/groovesalad-128-mp3

Point a LARA at <our-ip> first (Audio zone function + slim server IP) and allow inbound TCP 3483.
"""
import sys, os, asyncio, ipaddress, socket, logging
from urllib.parse import urlparse

LR3CTL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "lr3_audiozone", "lr3ctl")
sys.path.insert(0, LR3CTL)
import slimproto as S  # the REAL add-on module

logging.basicConfig(level=logging.INFO, format="[play] %(levelname)s %(message)s")
log = logging.getLogger("play")

OUR_IP = sys.argv[1] if len(sys.argv) > 1 else "0.0.0.0"
URL = sys.argv[2] if len(sys.argv) > 2 else "http://ice1.somafm.com/groovesalad-128-mp3"

u = urlparse(URL)
HOST, PORT = u.hostname, (u.port or 80)
PATH = (u.path or "/") + (("?" + u.query) if u.query else "")
TARGET_IP = socket.gethostbyname(HOST)

server = S.SlimProtoServer(OUR_IP, icecast_port=PORT)

async def push(player):
    await asyncio.sleep(1.2)
    http = (f"GET {PATH} HTTP/1.0\r\nHost: {HOST}:{PORT}\r\n"
            "User-Agent: LR3\r\nIcy-MetaData: 0\r\nConnection: close\r\nAccept: */*\r\n\r\n").encode()
    body = S._strm_body(b"s", autostart=b"3", threshold=200, output_threshold=20,
                        server_port=PORT, server_ip=int(ipaddress.ip_address(TARGET_IP)), http=http)
    await server._send(player, b"strm", body)
    log.info("pushed strm-s -> http://%s:%s%s  (target ip=%s)", HOST, PORT, PATH, TARGET_IP)

def on_connect(player):
    log.info("LARA connected mac=%s dev=%s caps=%r", player.mac, player.dev_id, player.caps)
    asyncio.create_task(push(player))

server.on_connect = on_connect

async def main():
    await server.start()
    log.info("REAL slimproto server up on :3483  our_ip=%s  target=%s", OUR_IP, URL)
    n = 0
    while True:
        await asyncio.sleep(5); n += 1
        log.info("t+%2ds  connected players: %s", n * 5, list(server.players.keys()) or "(none)")

try:
    asyncio.run(main())
except KeyboardInterrupt:
    pass
