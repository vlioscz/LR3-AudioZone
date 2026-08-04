#!/usr/bin/env python3
"""Offline checks for controller.py — zones, routing, on/off. No device, no network.

    python tools/tests/test_controller.py

The device set (one Spotify device per radio, plus "LARA All" from two radios up) and the
precedence rule (a radio's own device beats the group) are the parts most likely to be broken
by a refactor, and the hardest to notice on hardware.
"""
import asyncio
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "lr3_audiozone", "lr3ctl"))
import controller as C  # noqa: E402

C.LIQ_TEMPLATE = os.path.join(REPO, "lr3_audiozone", "radio.liq.tpl")
C.STATE_DIR = tempfile.mkdtemp(prefix="lr3test_")

A = "00:0a:59:f2:23:1c"   # "LARA Koupelna"
B = "00:0a:59:aa:bb:cc"   # "Obývák"
D = "00:0a:59:11:22:33"   # no name reported
events = []
active_mounts = set()

C.spotify_active = lambda mount: mount in active_mounts


class FakeSlim:
    def __init__(self): self.players = {}
    async def push_stream(self, mac, mount): events.append(("push", mac, mount)); return True
    async def stop(self, mac): events.append(("stop", mac))
    async def set_power(self, mac, on): events.append(("power", mac, on))
    async def set_volume(self, mac, v): events.append(("vol", mac, v))
    def stream_url(self, m): return f"http://10.0.0.99:8121/{m}"


class FakeDev:
    def park_on_radio(self): events.append(("park",)); return True


def mk(cfg=None, radios=()):
    ctl = C.Controller(cfg or {})
    ctl.slim = FakeSlim()
    for mac, name in radios:
        ctl.radios[mac] = {"rec": {"ip": "10.0.0.9", "name": name, "mac": mac}, "dev": FakeDev()}
    ctl.build_zones()
    return ctl


async def run():
    global active_mounts

    ctl = mk(radios=[(A, "LARA Koupelna")])
    assert [z.name for z in ctl.zones] == ["LARA Koupelna"]
    assert ctl.zones[0].mount == "lara_f2231c"
    print("1) one radio -> its own device only, no group")

    ctl = mk(radios=[(A, "LARA Koupelna"), (B, "Obývák")])
    assert [z.name for z in ctl.zones] == ["LARA Koupelna", "LARA Obývák", "LARA All"]
    assert ctl.zones[-1].is_group and ctl.zones[-1].mount == "all"
    print("2) two radios -> per-radio devices + the group")

    ctl = mk({"lara_name_prefix": False}, radios=[(A, "LARA Koupelna"), (B, "Obývák")])
    assert [z.name for z in ctl.zones] == ["LARA Koupelna", "Obývák", "LARA All"]
    print("3) prefix switch off -> raw names, never doubled up")

    ctl = mk(radios=[(A, "Koupelna"), (B, "Koupelna"), (D, "")])
    names = [z.name for z in ctl.zones]
    assert names[:3] == ["LARA Koupelna", "LARA Koupelna BBCC", "LARA 112233"], names
    print("4) duplicate names disambiguated, unnamed radio falls back to its MAC")

    ctl = mk({"zone_name": "Audio zóna"})
    assert [z.name for z in ctl.zones] == ["Audio zóna"] and ctl.zones[0].is_group
    assert ctl.zones[0].mount == "default"
    print("5) nothing found -> one fallback device that still drives late arrivals")

    ctl = mk({"idle_timeout": 3, "zone_volume": 40}, radios=[(A, "Koupelna"), (B, "Obývák")])
    events.clear(); active_mounts = {"all"}
    await ctl.tick()
    assert ("push", A, "all") in events and ("push", B, "all") in events, events
    print("6) group device feeds every radio")

    events.clear(); active_mounts = {"all", ctl.mount_for(A)}
    await ctl.tick()
    assert events[0] == ("push", A, ctl.mount_for(A)), events
    assert not any(e[0] == "push" and e[1] == B for e in events), events
    assert ctl.target[A] == ctl.mount_for(A) and ctl.target[B] == "all"
    print("7) a radio's own device beats the group; the others keep playing")

    events.clear(); active_mounts = {"all"}
    await ctl.tick()
    assert ("push", A, "all") in events, events
    events.clear(); active_mounts = set()
    await ctl.tick()
    assert events == [], events
    ctl.idle_since[A] = ctl.idle_since[B] = time.monotonic() - 5
    await ctl.tick()
    assert ("stop", A) in events and ("park",) in events, events
    assert ctl.target[A] is None and ctl.target[B] is None
    print("8) idle past idle_timeout -> stopped and parked back on the radio list")

    ctl = mk(radios=[(A, "Koupelna"), (B, "Obývák")])
    class P: mac, ip, name = D, "10.0.0.77", "LARA"
    ctl._loop = asyncio.get_running_loop()
    ctl.on_slim_connect(P())
    assert D in ctl.radios
    events.clear(); active_mounts = {"all"}
    await ctl.tick()
    assert ("push", D, "all") in events, events
    assert ctl.zone_for(D, {ctl.mount_for(D)}) is None
    print("9) a radio seen only on SlimProto follows the group, gets no device until restart")

    ctl = mk({"bitrate": 192, "source_password": "pw"},
             radios=[(A, "Koupelna"), (B, "Obývák")])
    body = open(ctl.render_liq(ctl.zones[0]), encoding="utf-8").read()
    assert "%%" not in body, [l for l in body.splitlines() if "%%" in l]
    assert 'mount="/lara_f2231c"' in body and '--name "LARA Koupelna"' in body
    assert "LR3_MOUNT=lara_f2231c" in body and "/data/librespot_lara_f2231c" in body
    # Check the command line itself, not the comment above it that explains the history.
    cmd = next(l for l in body.splitlines() if "librespot --name" in l)
    assert "--volume-ctrl fixed" not in cmd, "that flag removes the Spotify volume slider"
    other = open(ctl.render_liq(ctl.zones[1]), encoding="utf-8").read()
    assert 'mount="/lara_aabbcc"' in other and '--name "LARA Obývák"' in other
    print("10) rendered .liq per zone: own mount, device name, LR3_MOUNT, cache dir")

    mount = ctl.mount_for(A)
    with open(os.path.join(C.STATE_DIR, f"spotify_track_{mount}"), "w", encoding="utf-8") as f:
        f.write("Bohemian Rhapsody\nQueen\n")
    assert C.spotify_track(mount) == ("Bohemian Rhapsody", "Queen")

    class P2: mac, ip, name, current_mount = A, "10.0.0.98", "LARA", mount
    P2.title = P2.artist = ""
    p = P2(); ctl.slim.players[A] = p
    ctl.update_now_playing(A, mount)
    assert (p.title, p.artist) == ("Bohemian Rhapsody", "Queen"), (p.title, p.artist)
    print("11) now-playing metadata reaches the player (and so the LARA's display)")

    c = C.Controller({"bitrate": 192, "buffer_seconds": 1.5})
    assert c.buffer_kb == 36, c.buffer_kb
    c = C.Controller({"bitrate": 320, "buffer_seconds": 1.5})
    assert c.buffer_kb == 60, c.buffer_kb
    c = C.Controller({})
    assert (c.buffer_kb, c.buffer_seconds, c.idle_timeout) == (36, 1.5, 8)
    print("12) buffer seconds -> KB per bitrate; defaults 1.5 s / 8 s idle")


asyncio.run(run())
print("\nALL OK")
