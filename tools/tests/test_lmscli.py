#!/usr/bin/env python3
"""Offline checks for slimproto.py + lmscli.py. No device, no network.

    python tools/tests/test_lmscli.py

Covers the things a real LARA taught us and that are easy to break again:
frame/struct sizes, the 51-byte STAT layout, every CLI command we answer,
the handshake grace window, and the volume decision.
"""
import asyncio
import os
import struct
import sys
from urllib.parse import unquote

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "lr3_audiozone", "lr3ctl"))
import lmscli  # noqa: E402
import slimproto as sp  # noqa: E402
from lmscli import LmsCliServer  # noqa: E402

MAC = "00:0a:59:f2:23:1c"


class W:
    def get_extra_info(self, _k): return ("10.0.0.98", 3483)
    def write(self, _d): pass
    async def drain(self): pass


# --- slimproto struct sanity -------------------------------------------------
assert sp._STAT_LEN == 53 and sp._STAT_LEN_SHORT == 51, (sp._STAT_LEN, sp._STAT_LEN_SHORT)
http = b"GET /default HTTP/1.0\r\n\r\n"
body = sp._strm_body(b"s", autostart=b"1", server_port=8121, server_ip=0,
                     threshold=36, output_threshold=10, http=http)
assert body[:1] == b"s" and body[1:2] == b"1"
assert len(body) - len(http) == 24, len(body)
assert struct.unpack("!H", sp._frame(b"strm", body)[:2])[0] == len(body) + 4
print("slimproto: strm body 24 B + HTTP, frame length ok, STAT 53/51 B")

p = sp.Player(MAC, 12, "CSModel=squeezeslave,ModelName=LARA,Firmware=3.7.001,wma,mp3", W())
srv = sp.SlimProtoServer("10.0.0.99", 8121)
# 51-byte STAT — what fw 3.7.001 actually sends (no trailing error_code)
short = struct.pack(sp._STAT_FMT_SHORT, b"STMs", 0, 0, 0, 131072, 63488, 1461962, 0, 99,
                    5000, 4990, 58, 0, 58500, 0)
srv._on_stat(p, short)
assert p.mode == "play" and abs(p.elapsed - 58.5) < 0.01, (p.mode, p.elapsed)
assert p.name == "LARA", p.name
print(f"slimproto: 51-byte STAT parsed -> mode={p.mode} elapsed={p.elapsed}s")


class FakeSlim:
    def __init__(self): self.players = {MAC: p}; self.calls = []
    def stream_url(self, m): return f"http://10.0.0.99:8121/{m}"
    async def set_volume(self, mac, v): self.calls.append(("vol", mac, v)); p.volume = v
    async def pause(self, mac, on): self.calls.append(("pause", mac, on))


fake = FakeSlim()
invoked = []


async def on_cmd(mac, verb):
    invoked.append((mac, verb))


cli = LmsCliServer(fake, username="lms", password="secret",
                   zone_names={"lara_f2231c": "LARA Koupelna", "all": "LARA All"},
                   fallback_name="Audio zóna", on_command=on_cmd)


async def ask(line, writer=None):
    tokens = [unquote(t) for t in line.split(" ") if t]
    out = await cli._dispatch(tokens, writer or W())
    return unquote(" ".join(lmscli._enc(t) for t in out)) if out is not None else None


async def run():
    p.powered, p.mode, p.current_mount, p.volume = True, "play", "lara_f2231c", 90
    p.title, p.artist = "Bohemian Rhapsody", "Queen"

    for line in ["login lms secret", "version ?", "listen 1", "players 0 100",
                 "player count ?", "player id 0 ?", "serverstatus 0 100",
                 f"{MAC} status 0 10 tags:gald", f"{MAC} mode ?", f"{MAC} power ?",
                 f"{MAC} mixer volume ?", f"{MAC} time ?", f"{MAC} playlist tracks ?",
                 f"{MAC} playlist path 0 ?", f"{MAC} connected ?", f"{MAC} albums 0 5",
                 f"{MAC} wibble frobnicate"]:
        r = await ask(line)
        assert r is not None and "\n" not in r, (line, r)
    print("cli: every implemented command answers on a single line")

    # the display: these two polls ARE the LARA's two lines
    assert (await ask(f"{MAC} current_title ?")).endswith("Bohemian Rhapsody")
    assert (await ask(f"{MAC} artist ?")).endswith("Queen")
    assert "title:Bohemian Rhapsody" in await ask(f"{MAC} status 0 10")
    assert "artist:Queen" in await ask(f"{MAC} status 0 10")
    p.title = p.artist = ""
    assert (await ask(f"{MAC} current_title ?")).endswith("LARA Koupelna")
    print("cli: current_title/artist serve the track, falling back to the zone name")

    # volume from the CLI is state only — audg does nothing on fw 3.7.001
    fake.calls.clear()
    await ask(f"{MAC} mixer volume 55")
    assert not [c for c in fake.calls if c[0] == "vol"], fake.calls
    assert p.volume == 55, p.volume
    print("cli: mixer volume recorded, never echoed back as audg")

    # transport commands are honoured outside the handshake window...
    invoked.clear()
    for line in (f"{MAC} play", f"{MAC} stop", f"{MAC} power 0", f"{MAC} button pause"):
        await ask(line)
    assert (MAC, "play") in invoked and (MAC, "stop") in invoked and (MAC, "power_off") in invoked
    assert ("pause", MAC, True) in fake.calls

    # ...and ignored inside it: the LARA sends `stop` right after login as state sync
    invoked.clear()
    fresh = W()
    cli._session_start[id(fresh)] = __import__("time").monotonic()
    for line in (f"{MAC} stop", f"{MAC} play", f"{MAC} playlist play x"):
        await ask(line, fresh)
    assert invoked == [], f"handshake-grace leak: {invoked}"
    print("cli: handshake grace ignores the LARA's initial stop/play")

    # nothing connected must not raise
    fake.players = {}
    for line in ("players 0 100", f"{MAC} status 0 10", f"{MAC} mode ?"):
        assert await ask(line) is not None
    print("cli: survives having no player connected")


asyncio.run(run())
print("\nALL OK")
