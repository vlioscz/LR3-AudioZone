#!/usr/bin/env python3
"""On-device test of the full audio-zone stack — SlimProto (:3483) + LMS CLI (:9595).

Runs the REAL `lr3ctl/slimproto.py` and `lr3ctl/lmscli.py` outside the add-on, with verbose
logging, so you can watch exactly what a LARA does: whether it dials in on 3483, whether it
ALSO opens the CLI connection, and which CLI commands it sends. That is the open question
this tool exists to answer.

Prereq: point the LARA at this host first (web UI -> "Audio zone function" = on,
slim server IP = this machine, CLI port = 9595) and allow inbound TCP 3483 + 9595 in the
Windows firewall (see docs/HANDOFF.md).

    python tools/zone_test.py <our-ip> [--port 8121] [--mount default] [--cli-port 9595]

Type commands at the prompt:  on | off | vol <0-100> | status | quit
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
from urllib.parse import urlsplit

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "lr3_audiozone", "lr3ctl"))
from lmscli import LmsCliServer  # noqa: E402
from slimproto import SlimProtoServer  # noqa: E402

logging.basicConfig(level=logging.DEBUG,
                    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("zone_test")


async def start_proxy(port: int, upstream: str):
    """Serve `upstream` on OUR port, so the audio comes from the same host as the slim server.

    That is the add-on's real topology (Icecast and SlimProto both on the HA), and it is the
    only one a LARA seems to accept: fw 3.7.001 appears to ignore the `server_ip` field of the
    strm packet and always fetches from the slim server's own address. It also makes the
    "did the LARA actually connect for audio?" question answerable — every fetch is logged.
    """
    u = urlsplit(upstream)
    uhost, uport = u.hostname, u.port or 80
    upath = u.path or "/"

    async def handle(reader, writer):
        peer = writer.get_extra_info("peername")
        log.info(">>> AUDIO FETCH from %s — the LARA is pulling the stream <<<", peer)
        try:
            while True:                                  # swallow the request headers
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
            ur, uw = await asyncio.open_connection(uhost, uport)
            uw.write(f"GET {upath} HTTP/1.0\r\nHost: {uhost}:{uport}\r\n"
                     f"Connection: close\r\nAccept: */*\r\n\r\n".encode())
            await uw.drain()
            total = 0
            while True:
                chunk = await ur.read(8192)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
                total += len(chunk)
        except (ConnectionError, OSError) as e:
            log.info("audio fetch from %s ended (%s)", peer, e.__class__.__name__)
        finally:
            log.info("<<< audio fetch from %s closed", peer)
            for w in (locals().get("uw"), writer):
                try:
                    w.close()
                except Exception:
                    pass

    await asyncio.start_server(handle, "0.0.0.0", port)
    log.info("audio proxy on :%d -> %s", port, upstream)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("our_ip", help="IP of THIS machine on the LAN (the LARA fetches audio from it)")
    ap.add_argument("--port", type=int, default=8121, help="Icecast port serving the mount")
    ap.add_argument("--mount", default="default", help="Icecast mount name")
    ap.add_argument("--cli-port", type=int, default=9595, help="LMS CLI port the LARA connects to")
    ap.add_argument("--auto", action="store_true",
                    help="push the stream as soon as a LARA connects")
    ap.add_argument("--proxy", metavar="URL",
                    help="serve this upstream MP3 URL on our own --port, so the audio comes "
                         "from this host (the add-on's topology). Logs every fetch.")
    args = ap.parse_args()

    loop = asyncio.get_running_loop()
    if args.proxy:
        await start_proxy(args.port, args.proxy)
    slim = SlimProtoServer(args.our_ip, args.port)
    cli = LmsCliServer(slim, port=args.cli_port, zone_name="Audio zóna", mount=args.mount)
    slim.on_state = lambda player, what: cli.notify(player, what)

    def on_connect(player):
        log.info("=== LARA %s connected (codecs: %s) ===", player.mac, player.caps)
        if args.auto:
            loop.create_task(slim.push_stream(player.mac, args.mount))

    slim.on_connect = on_connect
    await slim.start()
    await cli.start()
    log.info("Waiting for a LARA on :3483 (audio %s) and :%d (CLI)...",
             slim.stream_url(args.mount), args.cli_port)
    log.info("Commands: on | off | vol <0-100> | status | quit")

    stop = asyncio.Event()

    def reader():
        for line in sys.stdin:
            parts = line.strip().split()
            if not parts:
                continue
            asyncio.run_coroutine_threadsafe(handle(parts), loop)
            if parts[0] in ("quit", "exit"):
                return

    async def handle(parts: list[str]):
        cmd = parts[0].lower()
        macs = list(slim.players.keys())
        if cmd in ("quit", "exit"):
            stop.set()
        elif not macs:
            log.warning("no LARA connected yet")
        elif cmd == "on":
            for m in macs:
                await slim.push_stream(m, args.mount)
        elif cmd == "off":
            for m in macs:
                await slim.stop(m)
                await slim.set_power(m, False)
        elif cmd == "vol" and len(parts) > 1:
            for m in macs:
                await slim.set_volume(m, int(parts[1]))
        elif cmd == "status":
            for m, p in slim.players.items():
                log.info("%s power=%s mode=%s mount=%s vol=%d elapsed=%.1fs",
                         m, p.powered, p.mode, p.current_mount, p.volume, p.elapsed)
        else:
            log.warning("unknown command %r", cmd)

    threading.Thread(target=reader, daemon=True).start()
    try:
        await stop.wait()
    except KeyboardInterrupt:
        pass
    for m in list(slim.players.keys()):
        await slim.stop(m)
        await slim.set_power(m, False)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        pass
