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

    # The flap that made music stop mid-album on a customer's three-radio install: a single
    # non-playing event (librespot reports end_of_track between two tracks) used to switch the
    # zone off the instant idle_timeout had elapsed since the FIRST such blip of the session,
    # because nothing ever cleared idle_since while the radio kept playing.
    # A fake clock, so these cases can play for minutes without sleeping. Everything under
    # test reads time only through C.time.monotonic().
    class Clock:
        t = 10_000.0
        def monotonic(self): return self.t
        def advance(self, dt): self.t += dt
    clk = Clock()
    real_time = C.time
    C.time = clk

    ctl = mk({"idle_timeout": 8}, radios=[(A, "Koupelna")])
    mine = ctl.mount_for(A)
    events.clear(); active_mounts = {mine}
    await ctl.tick()
    assert ctl.target[A] == mine
    # Play for ten minutes, with a one-tick gap between tracks every 30 s — exactly the shape
    # that used to switch the zone off from the second gap onwards.
    for _ in range(20):
        for _ in range(30):
            clk.advance(1); await ctl.tick()
        active_mounts = set(); clk.advance(1); await ctl.tick()   # end_of_track
        active_mounts = {mine}; clk.advance(1); await ctl.tick()  # next track
        assert ctl.target[A] == mine, "a gap between two tracks must not switch the zone off"
    assert not any(e[0] == "stop" for e in events), events
    print("13) 10 min of playback with a gap every 30 s never switches the zone off")

    events.clear(); active_mounts = set()
    for _ in range(9):                          # a real pause, past idle_timeout
        clk.advance(1); await ctl.tick()
    assert ("stop", A) in events and events.count(("park",)) == 1, events
    assert ctl.target[A] is None
    print("14) a real pause past idle_timeout still stops and parks the radio, once")

    # The radio answers our strm-q with `stop` on the CLI seconds later. That is not a button
    # press, and by then the next tick may already have restarted the zone.
    events.clear(); clk.advance(2)
    await ctl.on_cli_command(A, "stop")
    assert events == [], events
    active_mounts = {mine}; clk.advance(1); await ctl.tick()
    assert ctl.target[A] == mine, events
    events.clear(); clk.advance(1)
    await ctl.on_cli_command(A, "stop")         # the echo, arriving after the re-push
    assert events == [], events
    assert ctl.target[A] == mine, "a late echo of our own stop must not kill the new zone"
    print("15) the radio echoing our own stop back is ignored, even after the zone restarted")

    # ...but a genuine stop from the radio's buttons, long after ours, must still work.
    events.clear(); clk.advance(C.STOP_ECHO_GRACE + 1)
    await ctl.on_cli_command(A, "stop")
    assert ("stop", A) in events and ("park",) in events, events
    print("16) a genuine stop pressed on the radio still switches the zone off")

    # An underrun stops playback but keeps the control connection: `target` still says
    # "playing", so nothing used to re-push and the radio stayed silent for minutes.
    class P3: mac, ip, name, current_mount, mode = A, "10.0.0.9", "LARA", mine, "play"
    P3.title = P3.artist = ""
    p3 = P3(); ctl.slim.players[A] = p3
    events.clear(); active_mounts = {mine}; clk.advance(1)
    await ctl.tick()
    assert ctl.target[A] == mine
    events.clear(); p3.mode = "stop"            # STMu
    clk.advance(1); await ctl.tick()
    assert ("push", A, mine) in events, events
    events.clear(); clk.advance(1); await ctl.tick()
    assert events == [], "the re-push must be rate limited, not sent every tick"
    print("17) an underrun that keeps the connection is noticed and the stream re-pushed")

    C.time = real_time

    # The echo does not politely wait for us to finish: park_on_radio is two TCP round trips
    # on :61695 and takes seconds, the CLI dispatches in its own task, and the radio answers
    # right after our strm-q — i.e. squarely inside that window. Real clock here on purpose.
    class SlowDev:
        def park_on_radio(self): time.sleep(0.4); events.append(("park",)); return True

    ctl = C.Controller({"idle_timeout": 8})
    ctl.slim = FakeSlim()
    ctl.radios[A] = {"rec": {"ip": "10.0.0.9", "name": "Koupelna", "mac": A}, "dev": SlowDev()}
    ctl.build_zones()
    mine = ctl.mount_for(A)
    active_mounts = {mine}
    await ctl.tick()
    events.clear(); active_mounts = set()
    ctl.idle_since[A] = time.monotonic() - 99
    off = asyncio.create_task(ctl.tick())
    await asyncio.sleep(0.1)                    # we are now inside park_on_radio
    await asyncio.gather(off, asyncio.create_task(ctl.on_cli_command(A, "stop")))
    assert events.count(("park",)) == 1, events
    print("18) an echo landing while we are still parking does not park the radio twice")

    # --- spotify_remote_access -------------------------------------------------
    C.DATA_DIR = tempfile.mkdtemp(prefix="lr3data_")
    C.Controller.probe_cred_cache_flag = staticmethod(lambda: True)

    def stored_login(mount, where="new"):
        path = (C.credentials_file(mount) if where == "new"
                else C.legacy_credentials_file(mount))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"username": "someone"}')
        return path

    ctl = mk(radios=[(A, "Koupelna")])          # default: remote access off
    assert ctl.remote_access is False
    mine = ctl.mount_for(A)
    body = open(ctl.render_liq(ctl.zones[0]), encoding="utf-8").read()
    assert "%%" not in body, [l for l in body.splitlines() if "%%" in l]
    cmd = next(l for l in body.splitlines() if "librespot --name" in l)
    assert "--disable-credential-cache" in cmd, cmd
    assert f'--system-cache "{C.login_cache_dir(mine)}"' in cmd, cmd
    assert f'--cache "{C.audio_cache_dir(mine)}"' in cmd, cmd
    print("19) remote access off -> librespot is told not to store the login")

    # A canary in the audio cache: releasing a login must never cost the user up to 1 GB of
    # cached audio per zone, which is what would happen if the two ever shared a directory.
    canary = os.path.join(C.audio_cache_dir(mine), "files", "ab", "cd")
    os.makedirs(canary, exist_ok=True)
    with open(os.path.join(canary, "track"), "w", encoding="utf-8") as f:
        f.write("x" * 64)
    new, old = stored_login(mine), stored_login(mine, "legacy")
    ctl.prepare_credentials(mine)
    assert not os.path.exists(new) and not os.path.exists(old), "the login must be deleted"
    assert os.path.exists(os.path.join(canary, "track")), "the audio cache must survive"
    print("20) remote access off deletes a login stored earlier, keeping the audio cache")

    # A radio switched off while the switch is flipped is not in this boot's zone set, so a
    # per-zone loop would leave its login on disk for ever — and in every HA backup.
    absent = stored_login("lara_deadbe")
    legacy_absent = stored_login("lara_deadbe", "legacy")
    ctl = mk(radios=[(A, "Koupelna")])
    assert ctl.purge_stored_logins() >= 2
    assert not os.path.exists(absent) and not os.path.exists(legacy_absent)
    print("21) logins of radios that are switched off right now are released too")

    ctl = mk({"spotify_remote_access": True}, radios=[(A, "Koupelna")])
    cmd = next(l for l in open(ctl.render_liq(ctl.zones[0]), encoding="utf-8")
               if "librespot --name" in l)
    assert "--disable-credential-cache" not in cmd, cmd
    old = stored_login(mine, "legacy")
    import shutil
    shutil.rmtree(C.login_cache_dir(mine), ignore_errors=True)   # prepare must not need it
    ctl.prepare_credentials(mine)
    assert os.path.exists(C.credentials_file(mine)), "the 0.3.4 login must be migrated, not lost"
    assert not os.path.exists(old)
    stored_login(mine, "legacy")                 # now BOTH exist
    ctl.prepare_credentials(mine)
    assert os.path.exists(C.credentials_file(mine))
    assert not os.path.exists(C.legacy_credentials_file(mine)), "the superseded copy must go"
    print("22) remote access on migrates a pre-0.3.5 login and drops the superseded copy")

    ctl = mk(radios=[(A, "Koupelna")])           # remote access off
    ctl.cred_cache_flag_ok = False
    assert "--disable-credential-cache" not in ctl.librespot_cache_args(mine), \
        "never pass a flag this librespot does not know — it would exit on start-up"
    ctl.cred_cache_flag_ok = True
    assert "--disable-credential-cache" in ctl.librespot_cache_args(mine)
    for part in (C.audio_cache_dir(mine), C.login_cache_dir(mine)):
        assert f'"{part}"' in ctl.librespot_cache_args(mine), "paths must be quoted for sh -c"
    print("23) the flag follows the probe, and the paths are quoted")

    # The line the whole feature hangs on: start_zone must actually call prepare_credentials.
    # Without this, deleting that one call leaves every other case green.
    ctl = mk(radios=[(A, "Koupelna")])
    ctl.render_liq = lambda z: os.path.join(C.STATE_DIR, "unused.liq")
    left = stored_login(mine)
    try:
        await ctl.start_zone(ctl.zones[0])
    except Exception:
        pass                                     # liquidsoap is not installed here; fine
    assert not os.path.exists(left), "start_zone must release the login before spawning"
    print("24) start_zone releases the stored login before librespot can be started")


asyncio.run(run())
print("\nALL OK")
