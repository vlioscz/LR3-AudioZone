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

    # Taking a radio out of the group now has to settle first (0.4.4), so one tick is not
    # enough: handing a session over lights both librespots up for a moment.
    events.clear(); active_mounts = {"all", ctl.mount_for(A)}
    await ctl.tick()
    assert events == [], "a hand-over must not be acted on in the same second"
    ctl._zone_pending[A] = (ctl.mount_for(A), time.monotonic() - C.ZONE_SETTLE - 1)
    await ctl.tick()
    assert events[0] == ("push", A, ctl.mount_for(A)), events
    assert not any(e[0] == "push" and e[1] == B for e in events), events
    assert ctl.target[A] == ctl.mount_for(A) and ctl.target[B] == "all"
    print("7) a radio's own device beats the group; the others keep playing")

    events.clear(); active_mounts = {"all"}
    await ctl.tick()
    ctl._zone_pending[A] = ("all", time.monotonic() - C.ZONE_SETTLE - 1)   # let it settle
    await ctl.tick()
    assert ("push", A, "all") in events, events
    events.clear(); active_mounts = set()
    await ctl.tick()
    assert events == [], events
    ctl.idle_since[A] = ctl.idle_since[B] = time.monotonic() - 5
    await ctl.tick()
    assert ("stop", A) in events, events
    assert ctl.target[A] is None and ctl.target[B] is None
    assert ("park",) not in events, "the 61695 park is off by default since 0.3.6"
    print("8) idle past idle_timeout -> stream stopped, radio left alone by default")

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
    # 64 KB @ 192 kbps — the only threshold ever validated on a real LARA.
    assert (c.buffer_kb, c.buffer_seconds, c.idle_timeout) == (64, 2.7, 60)
    print("12) buffer seconds -> KB per bitrate; defaults 2.7 s / 60 s idle")

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

    # park_on_zone_off on, so these cases still exercise the 61695 path end to end.
    ctl = mk({"idle_timeout": 8, "park_on_zone_off": True}, radios=[(A, "Koupelna")])
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
    P3.title = P3.artist = ""; P3.stat_seq = 100
    p3 = P3(); ctl.slim.players[A] = p3
    events.clear(); active_mounts = {mine}; clk.advance(1)
    await ctl.tick()
    assert ctl.target[A] == mine
    events.clear(); p3.mode = "stop"            # STMu
    clk.advance(1); await ctl.tick()
    assert events == [], "a radio that has not spoken since the underrun must be left alone"
    p3.stat_seq += 1                            # it answers again -> still alive
    clk.advance(1); await ctl.tick()
    assert events == [], "not within the cooldown that every push starts (0.5.0)"
    clk.advance(C.REPUSH_COOLDOWN); await ctl.tick()
    assert ("push", A, mine) in events, events
    events.clear(); clk.advance(1); await ctl.tick()
    assert events == [], "the re-push must be rate limited, not sent every tick"
    print("17) an underrun is recovered, but only from a radio that is still answering")

    # The fatal case at site 5: one underrun, then never another word. We pushed a stream at
    # it one second later; it was dead for 38 hours. Now it gets nothing.
    ctl2 = mk({"idle_timeout": 8}, radios=[(A, "Koupelna")])
    class P4: mac, ip, name, current_mount, mode = A, "10.0.0.9", "LARA", mine, "play"
    P4.title = P4.artist = ""; P4.stat_seq = 500
    dead = P4(); ctl2.slim.players[A] = dead
    active_mounts = {mine}; clk.advance(1); await ctl2.tick()
    events.clear(); dead.mode = "stop"           # its last word ever
    for _ in range(30):
        clk.advance(10); await ctl2.tick()       # five minutes of silence from it
    assert events == [], f"nothing may be sent to a radio that has gone quiet: {events}"
    print("18) a radio whose last word was the underrun is never pushed again")

    C.time = real_time

    # The echo does not politely wait for us to finish: park_on_radio is two TCP round trips
    # on :61695 and takes seconds, the CLI dispatches in its own task, and the radio answers
    # right after our strm-q — i.e. squarely inside that window. Real clock here on purpose.
    class SlowDev:
        def park_on_radio(self): time.sleep(0.4); events.append(("park",)); return True

    ctl = C.Controller({"idle_timeout": 8, "park_on_zone_off": True})
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
    print("19) an echo landing while we are still parking does not park the radio twice")

    # --- never touch a radio we are not driving (0.3.6) -------------------------
    # 48 of 82 switch-offs at a customer's site fired on a radio that had never been pushed,
    # each one an unsolicited write over 61695 — and one of those was the last thing the
    # add-on ever sent to a unit that then froze solid.
    ctl = mk({"park_on_zone_off": True}, radios=[(A, "Koupelna"), (B, "Obývák")])
    events.clear()
    assert ctl.target.get(A) is None
    await ctl.on_cli_command(A, "stop")         # the state-sync stop after its CLI handshake
    assert events == [], events
    print("20) a stop from a radio we never switched on touches nothing at all")

    # And with the park off — the shipping default — a real switch-off writes nothing to 61695.
    ctl = mk(radios=[(A, "Koupelna")])
    assert ctl.park_on_off is False, "the 61695 park must be off by default"
    mine = ctl.mount_for(A)
    active_mounts = {mine}
    await ctl.tick()
    events.clear(); active_mounts = set()
    ctl.idle_since[A] = time.monotonic() - 99
    await ctl.tick()
    assert ("stop", A) in events and ("power", A, False) in events, events
    assert ("park",) not in events, "no write to the radio's config port by default"
    print("21) with the park off, a switch-off is SlimProto only — no 61695 write")

    # control_mode=off is a diagnostic escape hatch, and leaving it on is silent: Spotify
    # behaves perfectly and the radio simply never joins in. It cost a customer two days.
    import logging
    said = []
    class Catch(logging.Handler):
        def emit(self, r): said.append(r.getMessage())
    h = Catch(); h.setLevel(logging.WARNING); C.log.addHandler(h)
    ctl = mk({"control_mode": "off"}, radios=[(A, "Koupelna")])
    said.clear()          # the librespot --help probe warns on a dev box; not what we test
    active_mounts = set()
    ctl.nag_if_muzzled()
    assert said == [], "nothing is playing, so there is nothing to complain about"
    active_mounts = {ctl.mount_for(A)}
    ctl.nag_if_muzzled()
    assert any("control_mode=off" in m and "LARA Koupelna" in m for m in said), said
    n = len(said)
    ctl.nag_if_muzzled()
    assert len(said) == n, "the warning must be rate limited, not once per second"
    C.log.removeHandler(h)
    print("22) control_mode=off says so when Spotify is playing into the void")

    # Hand-picked groups: "just the inside ones", asked for from the field because
    # "LARA All" is all-or-nothing.
    C3 = "00:0a:59:11:22:44"
    g = [{"name": "Dovnitr", "radios": ["Koupelna", "LARA Obývák"]}]
    ctl = mk({"groups": g}, radios=[(A, "Koupelna"), (B, "Obývák"), (C3, "Terasa")])
    names = [z.name for z in ctl.zones]
    assert names == ["LARA Koupelna", "LARA Obývák", "LARA Terasa", "Dovnitr", "LARA All"], names
    grp = next(z for z in ctl.zones if z.name == "Dovnitr")
    assert sorted(grp.radios) == sorted([A, B]) and not grp.covers(C3)
    # precedence: own room > hand-picked group > everything
    assert ctl.zone_for(A, {"all", grp.mount, ctl.mount_for(A)}) == ctl.mount_for(A)
    assert ctl.zone_for(A, {"all", grp.mount}) == grp.mount
    assert ctl.zone_for(C3, {"all", grp.mount}) == "all", "a non-member must not follow it"
    print("23) a hand-picked group plays to its members only, and sits below their own zones")

    # names are matched the way a person types them, and nonsense is refused rather than
    # silently producing a device that drives the wrong rooms
    loose = mk({"groups": [{"name": "X", "radios": ["  koupelna ", "obývák"]}]},
               radios=[(A, "Koupelna"), (B, "Obývák"), (C3, "Terasa")])
    assert sorted(next(z for z in loose.zones if z.name == "X").radios) == sorted([A, B])
    bad = mk({"groups": [{"name": "Y", "radios": ["Koupelna", "Neexistuje"]},
                         {"name": "", "radios": ["Koupelna", "Obývák"]}]},
             radios=[(A, "Koupelna"), (B, "Obývák")])
    assert [z.name for z in bad.zones] == ["LARA Koupelna", "LARA Obývák", "LARA All"],         "a group with a missing member or no name must be dropped, not half-built"
    print("24) member names are matched loosely; a broken group is dropped with a warning")

    # A group naming every radio is just "LARA All" wearing a different hat.
    whole = mk({"groups": [{"name": "Vsechno", "radios": ["Koupelna", "Obývák"]}]},
               radios=[(A, "Koupelna"), (B, "Obývák")])
    assert [z.name for z in whole.zones] == ["LARA Koupelna", "LARA Obývák", "LARA All"],         [z.name for z in whole.zones]
    # ...but the same two rooms out of three is a real group.
    C4 = "00:0a:59:11:22:55"
    part = mk({"groups": [{"name": "Dovnitr", "radios": ["Koupelna", "Obývák"]}]},
              radios=[(A, "Koupelna"), (B, "Obývák"), (C4, "Terasa")])
    assert "Dovnitr" in [z.name for z in part.zones]
    print("25) a group covering every radio is refused; a genuine subset is kept")

    # The wobble this guards: moving a Spotify session between a radio's own device and the
    # group leaves both reporting "playing" for a second, so the choice flipped there and
    # straight back — five times in one afternoon, always a pair one second apart. Since 0.4.3
    # each bounce terminates the stream and re-buffers, which the customer feels as a long,
    # stuttering switch.
    ctl = mk({"idle_timeout": 30}, radios=[(A, "Koupelna"), (B, "Obývák")])
    mine = ctl.mount_for(A)
    events.clear(); active_mounts = {"all"}
    await ctl.tick()
    assert ctl.target[A] == "all", events
    events.clear()
    for _ in range(3):                       # both zones alight, flapping between ticks
        active_mounts = {"all", mine}; await ctl.tick()
        active_mounts = {"all"};       await ctl.tick()
    assert events == [], f"a wobble must move nothing: {events}"
    assert ctl.target[A] == "all"
    # ...but a hand-over that actually holds is honoured.
    active_mounts = {"all", mine}
    await ctl.tick()
    ctl._zone_pending[A] = (mine, time.monotonic() - C.ZONE_SETTLE - 1)
    await ctl.tick()
    assert ("push", A, mine) in events and ctl.target[A] == mine, events
    # And a zone going quiet must still reach the idle countdown immediately, or it never
    # switches off at all.
    events.clear(); active_mounts = set()
    await ctl.tick()
    assert A in ctl.idle_since, "the idle countdown must start the moment nothing is active"
    print("26) a flapping hand-over is ignored; a real one, and going quiet, are not")

    # --- 0.4.0: output samplerate and the volume that actually does something ----
    # At 44.1 kHz every LARA measured drains its input buffer ~39 B/s and underruns every
    # 26 minutes, on two unrelated sites. The output moves to 48 kHz; librespot still emits
    # 44100, so that has to be stated explicitly or the PCM is read 8.8 % too fast.
    ctl = mk(radios=[(A, "Koupelna")])
    assert ctl.samplerate == 48000, "48 kHz is the default from 0.4.0"
    body = open(ctl.render_liq(ctl.zones[0]), encoding="utf-8").read()
    assert "%%" not in body, [l for l in body.splitlines() if "%%" in l]
    assert "settings.frame.audio.samplerate.set(48000)" in body
    assert "samplerate=44100," in body, "librespot's own rate must stay pinned at 44100"
    assert body.index("settings.frame.audio.samplerate.set") < body.index("input.external"),         "the frame rate must be set before any source is created"
    back = mk({"samplerate": 44100}, radios=[(A, "Koupelna")])
    b2 = open(back.render_liq(back.zones[0]), encoding="utf-8").read()
    assert "settings.frame.audio.samplerate.set(44100)" in b2 and "samplerate=44100," in b2
    print("27) the output runs at 48 kHz while librespot stays pinned to 44100")

    # zone_volume used to reach only audg, which is inaudible on this firmware, while the
    # Spotify slider sat hardcoded at 100 — "I set 50 and it plays at 100".
    for zv, want in ((50, 50), (0, 100), (90, 90), (150, 100)):
        c = mk({"zone_volume": zv}, radios=[(A, "K")])
        assert c.initial_volume() == want, (zv, c.initial_volume())
        cmd = next(l for l in open(c.render_liq(c.zones[0]), encoding="utf-8")
                   if "librespot --name" in l)
        assert f"--initial-volume {want} " in cmd, cmd
    print("28) zone_volume now sets where the Spotify slider starts")

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
    print("29) remote access off -> librespot is told not to store the login")

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
    print("30) remote access off deletes a login stored earlier, keeping the audio cache")

    # A radio switched off while the switch is flipped is not in this boot's zone set, so a
    # per-zone loop would leave its login on disk for ever — and in every HA backup.
    absent = stored_login("lara_deadbe")
    legacy_absent = stored_login("lara_deadbe", "legacy")
    ctl = mk(radios=[(A, "Koupelna")])
    assert ctl.purge_stored_logins() >= 2
    assert not os.path.exists(absent) and not os.path.exists(legacy_absent)
    print("31) logins of radios that are switched off right now are released too")

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
    print("32) remote access on migrates a pre-0.3.5 login and drops the superseded copy")

    ctl = mk(radios=[(A, "Koupelna")])           # remote access off
    ctl.cred_cache_flag_ok = False
    assert "--disable-credential-cache" not in ctl.librespot_cache_args(mine), \
        "never pass a flag this librespot does not know — it would exit on start-up"
    ctl.cred_cache_flag_ok = True
    assert "--disable-credential-cache" in ctl.librespot_cache_args(mine)
    for part in (C.audio_cache_dir(mine), C.login_cache_dir(mine)):
        assert f'"{part}"' in ctl.librespot_cache_args(mine), "paths must be quoted for sh -c"
    print("33) the flag follows the probe, and the paths are quoted")

    # The audio cache used to be a hard-coded 1 GB per zone, i.e. 4 GB on a four-zone site,
    # written to the soldered eMMC of an HA Green.
    assert C.Controller({}).audio_cache_mb == 200
    args = mk({"audio_cache_mb": 500}, radios=[(A, "K")]).librespot_cache_args(mine)
    assert "--cache-size-limit 500M" in args and f'--cache "{C.audio_cache_dir(mine)}"' in args
    off = mk({"audio_cache_mb": 0}, radios=[(A, "K")]).librespot_cache_args(mine)
    assert "--cache-size-limit" not in off and "--cache " not in off, off
    assert f'--system-cache "{C.login_cache_dir(mine)}"' in off, \
        "the login dir must stay even with the audio cache off — it is what the switch clears"
    print("34) the audio cache is sized by the option, and 0 drops it without losing the rest")

    # The line the whole feature hangs on: start_zone must actually call prepare_credentials.
    # Without this, deleting that one call leaves every other case green.
    ctl = mk(radios=[(A, "Koupelna")])
    ctl.rate_match = False
    ctl.render_liq = lambda z: os.path.join(C.STATE_DIR, "unused.liq")
    left = stored_login(mine)
    try:
        await ctl.start_zone(ctl.zones[0])
    except Exception:
        pass                                     # liquidsoap is not installed here; fine
    assert not os.path.exists(left), "start_zone must release the login before spawning"
    print("35) start_zone releases the stored login before librespot can be started")

    # 0.4.3 sends strm-q before a mount change; the radio answers STMf and reports "stop"
    # until its new stream starts a few seconds later. recover_if_stalled took that for an
    # underrun and pushed a second time — 6 of 19 switches in a colleague's log.
    C.time = clk
    ctl = mk({"idle_timeout": 30}, radios=[(A, "Koupelna"), (B, "Obývák")])
    mine = ctl.mount_for(A)

    class P5: mac, ip, name, current_mount, mode = A, "10.0.0.9", "LARA", None, "play"
    P5.title = P5.artist = ""; P5.stat_seq = 1
    p5 = P5(); ctl.slim.players[A] = p5
    active_mounts = {"all"}; events.clear()
    clk.advance(1); await ctl.tick()
    assert ("push", A, "all") in events, events
    for _ in range(40):                          # a while into the group session
        clk.advance(1); p5.stat_seq += 1; await ctl.tick()
    active_mounts = {mine}; events.clear()
    for _ in range(int(C.ZONE_SETTLE) + 2):
        clk.advance(1); await ctl.tick()
    assert events.count(("push", A, mine)) == 1, events
    events.clear(); p5.mode = "stop"            # STMf: its answer to our own strm-q
    for _ in range(6):
        clk.advance(1); p5.stat_seq += 1; await ctl.tick()
    assert ("push", A, mine) not in events, f"a switch must not be pushed twice: {events}"
    C.time = real_time
    print("36) a mount switch is pushed once, not again when the radio confirms the stop")

    # The zone list in the log used to name only a group's first radio, so a two-room group
    # read as one radio.
    said = []

    class Say(C.logging.Handler):
        def emit(self, r): said.append(r.getMessage())
    h = Say(); C.log.addHandler(h)
    mk({"groups": [{"name": "Dovnitr", "radios": ["Koupelna", "Obývák"]}]},
       radios=[(A, "Koupelna"), (B, "Obývák"), (D, "Terasa")])
    C.log.removeHandler(h)
    line = next(m for m in said if "'Dovnitr'" in m)
    assert "LARA Koupelna" in line and "LARA Obývák" in line, line
    print("37) a group's line in the log names every member")

    # rate_match. A LARA plays a fixed fraction off nominal — at 48 kHz the measured radios
    # took 78 B/s less than we sent — and the surplus used to pile up in Icecast until it
    # dropped the radio after 2 h 16 min. Simulated here for four hours against radios that
    # are off by different amounts, including the wrong sign for the starting guess, with
    # readings as noisy as the real ones.
    import random
    rnd = random.Random(7)
    SIZE, RATE = 131072, 24000.0
    for d in (-3250.0, -2000.0, -5000.0, 1640.0):
        ctl = mk(radios=[(A, "Koupelna")])
        assert ctl.rate_match, "rate_match: auto means on in this release"
        mine = ctl.mount_for(A)

        class PR: mac, mode, _stall_logged, buf_size = A, "play", False, SIZE
        pr = PR(); ctl.slim.players[A] = pr; ctl.target[A] = mine
        ctl.rate_ppm[mine] = ctl.default_ppm()
        x, q, t, lo, q_late = 61440.0, 0.0, 0.0, SIZE, 0.0
        for step in range(4 * 3600 // 5):
            t += 5.0
            q += RATE * (1 + ctl.rate_ppm[mine] / 1e6) * 5     # what we hand Icecast
            out = RATE * (1 + d / 1e6) * 5                     # what the radio plays
            take = min(q, SIZE - (x - out))                    # it pulls what fits
            q -= take; x = x - out + take
            lo = min(lo, x)
            if step > 3600 // 5:
                q_late = max(q_late, q)
            pr.in_buf, pr.in_buf_at = int(x + rnd.uniform(-2500, 2500)), t
            ctl.update_rates(t)
        target = ctl.rate_target(SIZE)
        assert lo > 30000, f"d={d}: the radio's buffer fell to {lo:.0f} B"
        assert q_late < 3 * RATE, f"d={d}: Icecast still built up {q_late:.0f} B"
        assert abs(x - target) < 10000, f"d={d}: buffer settled at {x:.0f}, not ~{target:.0f}"
        assert abs(ctl.rate_base[mine] - d) < 400, f"d={d}: learned {ctl.rate_base[mine]:.0f}"
    # Nothing to steer by (radio gone quiet): hold the rate, do not drift.
    before = ctl.rate_ppm[mine]
    pr.in_buf_at = t - 2 * C.RATE_FRESH
    for _ in range(20):
        t += 5.0; ctl.update_rates(t)
    assert ctl.rate_ppm[mine] == before
    assert float(open(C.Controller.rate_path(mine)).read()) == round(before, 1)
    print("38) rate_match holds the radio's buffer just under full, whatever its offset")

    # What was learned survives a restart — but only at the same sample rate, because the
    # radios' offset flips sign between 44.1 and 48 kHz.
    ctl.save_rates()
    again = C.Controller({})
    again.load_rates()
    assert abs(again.rate_base[mine] - ctl.rate_base[mine]) < 0.1
    other = C.Controller({"samplerate": 44100})
    other.load_rates()
    assert other.rate_base == {} and other.default_ppm() == 1640.0
    off = C.Controller({"rate_match": "off"})
    assert not off.rate_match and C.Controller({"rate_match": "on"}).rate_match
    print("39) learned rates are kept across restarts at the same sample rate only")

    # The pacer gets librespot's exact command line, as a list (a zone name is never shell),
    # and its starting rate.
    ctl = mk(radios=[(A, 'Kou"pel$na')])
    z = ctl.zones[0]
    cfg = __import__("json").load(open(ctl.write_pacer_config(z), encoding="utf-8"))
    argv = cfg["librespot"]
    assert argv[argv.index("--name") + 1] == 'LARA Kou"pel$na'
    for flag in ("--backend", "pipe", "--format", "S16", "--enable-volume-normalisation",
                 "--onevent", "/etc/lr3/spotify_event.sh", "--system-cache"):
        assert flag in argv, flag
    assert cfg["initial_ppm"] == -3250.0 and cfg["samplerate"] == 48000
    assert float(open(cfg["rate_file"]).read()) == -3250.0
    print("40) the pacer is configured with librespot's own flags and the starting rate")

    # Home Assistant sensors: one per radio, the Spotify device it plays or "off". Posted when
    # something changes, and in full every few minutes because HA forgets on restart.
    ctl = mk({"groups": [{"name": "Dovnitr", "radios": ["Koupelna", "Obývák"]}]},
             radios=[(A, "Koupelna"), (B, "Obývák"), (D, "Terasa")])
    sa, sb, sd = (C.Controller.sensor_id(m) for m in (A, B, D))
    ctl.target.update({A: "grp1", B: None, D: "all"})
    st = ctl.sensor_states()
    assert st[sa][0] == "Dovnitr" and st[sb][0] == "off" and st[sd][0] == "LARA All", st
    assert st[sa][1]["radio"] == "LARA Koupelna" and st[sa][1]["mount"] == "grp1"
    posted = []
    ctl.ha_token = "t"
    ctl.post_state = lambda e, s, a: posted.append((e, s))

    async def publish(now):
        await ctl.publish_sensors(now)
        if ctl._ha_task:
            await ctl._ha_task
    await publish(1000.0)
    assert sorted(posted) == sorted([(sa, "Dovnitr"), (sb, "off"), (sd, "LARA All")])
    posted.clear(); await publish(1001.0)
    assert posted == [], "nothing changed, nothing posted"
    ctl.target[B] = ctl.mount_for(B)
    await publish(1002.0)
    assert posted == [(sb, "LARA Obývák")], posted
    posted.clear(); await publish(1000.0 + C.HA_REFRESH + 1)
    assert len(posted) == 3, "the periodic refresh re-posts everything"

    def broken(e, s, a): raise OSError("401 Unauthorized")
    ctl.post_state = broken
    ctl.target[B] = None
    said.clear(); C.log.addHandler(h)
    await publish(2000.0); await publish(2001.0)
    C.log.removeHandler(h)
    assert sum("Home Assistant sensors" in m for m in said) == 1, said
    print("41) one sensor per radio, posted on change, refreshed, failures reported once")


asyncio.run(run())
print("\nALL OK")
