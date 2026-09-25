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


# The field report this guards: music stops, yet the phone still shows Spotify streaming to
# the zone. A LARA whose fetch has stalled goes on reporting `play`, so bytes_received is the
# only thing that gives it away — and it used to be visible at DEBUG only, on a logger whose
# level cannot be raised from the add-on options.
class _Clock:
    t = 1000.0
    def monotonic(self): return self.t


_clk = _Clock()
sp.time = _clk
stall = sp.Player(MAC, 12, "ModelName=LARA,mp3", W())
stall.mode = "play"
srv._track_progress(stall, in_buf=62000, bytes_rx=1_000_000)
assert not stall._stall_logged
_clk.t += 10; srv._track_progress(stall, in_buf=62000, bytes_rx=1_100_000)
_clk.t += 20; srv._track_progress(stall, in_buf=61000, bytes_rx=1_100_000)
assert not stall._stall_logged, "a short coast on the input buffer is normal"
_clk.t += 25; srv._track_progress(stall, in_buf=400, bytes_rx=1_100_000)
assert stall._stall_logged, "45 s without a single byte while 'playing' must be reported"
_clk.t += 5; srv._track_progress(stall, in_buf=62000, bytes_rx=1_200_000)
assert not stall._stall_logged, "and recovery must be reported too, not left hanging"
stall.mode = "stop"
_clk.t += 999; srv._track_progress(stall, in_buf=0, bytes_rx=1_200_000)
assert not stall._stall_logged, "a radio that is not playing is not stalled"
print("slimproto: a 'playing' LARA that fetches nothing is detected and logged")


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

    # A command naming a radio that is not connected must not be executed against a different
    # one. The old players[0] fallback made one radio's stop land on another; it needs two or
    # more radios to bite, which is why it only ever showed up on a multi-radio site.
    fake.players = {MAC: p}
    assert cli._player("00:0a:59:aa:bb:cc") is None
    assert cli._player(MAC) is p
    print("cli: an unknown MAC resolves to nothing, never to another radio")

    # An idle CLI connection is a healthy one. 0.3.6 closed anything silent for 120 s, on the
    # belief that a LARA polls every 5 s for ever — it does not, it goes quiet when it is not
    # playing. The result was every radio burning a fresh source port every two minutes, which
    # is the shape of the behaviour that preceded a radio freezing solid.
    srv2 = LmsCliServer(FakeSlim(), port=19595, zone_names={}, fallback_name="Z")
    await srv2.start()
    r1, w1 = await asyncio.open_connection("127.0.0.1", 19595)
    await asyncio.sleep(0.4)                       # say nothing at all
    w1.write(b"version ?\n"); await w1.drain()
    assert b"version" in await asyncio.wait_for(r1.readline(), 5), "an idle session was reaped"

    # ...but a reconnect from the same host supersedes the old connection, which is what the
    # firmware actually does: it opens a new one and never closes the one it walked away from.
    r2, w2 = await asyncio.open_connection("127.0.0.1", 19595)
    w2.write(b"version ?\n"); await w2.drain()
    assert b"version" in await asyncio.wait_for(r2.readline(), 5)
    assert await asyncio.wait_for(r1.read(1), 5) == b"", "the superseded connection stayed open"
    for w in (w1, w2):
        w.close()
    print("cli: idle connections survive; a reconnect closes the one it superseded")


asyncio.run(run())
print("\nALL OK")
