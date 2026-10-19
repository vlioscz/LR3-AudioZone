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

C.spotify_active = lambda mount, ev=None: mount in active_mounts


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
        def time(self): return 1.7e9 + self.t
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
    # ...and it stays off while that Spotify session goes on (0.5.4: it used to be pushed
    # straight back on the next tick — stop, back on, stop, back on, at site 5 at 03:13).
    events.clear()
    for _ in range(5):
        clk.advance(1); await ctl.tick()
    assert not [e for e in events if e[0] == "push"], events
    # When Spotify is paused and started again, the radio follows it once more.
    open(os.path.join(C.STATE_DIR, f"spotify_state_{mine}"), "w").write("paused")
    active_mounts = set(); clk.advance(1); await ctl.tick()
    assert A not in ctl._held
    active_mounts = {mine}
    print("16) a genuine stop pressed on the radio switches the zone off, and it stays off")

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

    # Since 0.5.11 a group may be a second name for one radio, or for all of them, or for the
    # same radios as another group: that is how a relay tells "terrace, seating only" from
    # "terrace, every speaker" (the colleague's "ALL Terasa" / "ALL Auta" were refused until
    # then). Only a name already taken is refused — two devices of one name are one to a person.
    C4 = "00:0a:59:11:22:55"
    rooms = [(A, "Koupelna"), (B, "Obývák"), (C4, "Terasa")]
    groups = [{"name": "ALL Terasa", "radios": ["Koupelna", "Obývák", "Terasa"]},
              {"name": "ALL Auta", "radios": ["Koupelna", "Obývák", "Terasa"]},
              {"name": "Terasa sezení", "radios": ["Terasa"]},
              {"name": "LARA Terasa", "radios": ["Terasa"]},          # = the radio's own device
              {"name": "lara all", "radios": ["Terasa", "Obývák"]},    # = the group device
              {"name": "all auta", "radios": ["Terasa"]}]              # = an earlier group
    said.clear(); h.setLevel(logging.INFO); C.log.addHandler(h)
    many = mk({"groups": groups}, radios=rooms)
    C.log.removeHandler(h); h.setLevel(logging.WARNING)
    names = [z.name for z in many.zones]
    assert names == ["LARA Koupelna", "LARA Obývák", "LARA Terasa", "Terasa sezení",
                     "ALL Terasa", "ALL Auta", "LARA All"], names       # groups smallest first
    assert sum("already exists" in s for s in said) == 3, said
    assert any("'Terasa sezení'  (LARA Terasa)" in s for s in said), \
        "a one-radio group's line names the radio, not its MAC"
    t_all, t_auta, t_seat = (next(z for z in many.zones if z.name == n)
                             for n in ("ALL Terasa", "ALL Auta", "Terasa sezení"))
    assert sorted(t_auta.radios) == sorted([A, B, C4]) and t_seat.radios == [C4]
    assert t_seat.mount == "grp3", "mounts keep the config index, so learned rates follow"
    # precedence: own device > the smaller group > the bigger one > LARA All — whatever order
    # the groups were listed in (a guest picking "Terasa sezení" while the house plays "ALL
    # Auta" gets the terrace)
    assert many.zone_for(C4, {"all", t_auta.mount, t_seat.mount}) == t_seat.mount
    assert many.zone_for(C4, {"all", t_auta.mount}) == t_auta.mount
    assert many.zone_for(C4, {"all", t_seat.mount, many.mount_for(C4)}) == many.mount_for(C4)
    # ...and the terrace's sensor names whichever of them it plays
    many.target[C4] = t_seat.mount
    assert many.sensor_states()[C.Controller.sensor_id(C4)][0] == "Terasa sezení"
    # a single radio may have a second name too (the colleague's other site has one LARA)
    one = mk({"groups": [{"name": "Terasa relé", "radios": ["Terasa"]}]}, radios=[(C4, "Terasa")])
    assert [z.name for z in one.zones] == ["LARA Terasa", "Terasa relé"]
    # a radio that is not here is warned about and the group built from the others, as before
    # 0.5.11 (a group that worked must not vanish because one radio was off at start-up); and
    # nobody has to type the diacritics
    half = mk({"groups": [{"name": "Tri", "radios": ["Koupelna", "Obyvak", "Sklep"]}]},
              radios=rooms)
    tri = next(z for z in half.zones if z.name == "Tri")
    assert sorted(tri.radios) == sorted([A, B]), tri.radios
    none = mk({"groups": [{"name": "Nic", "radios": ["Sklep"]}]}, radios=rooms)
    assert "Nic" not in [z.name for z in none.zones]
    print("25) groups of one radio, of all of them, or of the same radios under another name; "
          "a taken name is refused")

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
    assert other.rate_base == {} and other.default_ppm() == 0.0
    # A file from 0.5.0/0.5.1 was learned through the lossy pacer: ~3800 ppm off. Ignore it.
    old = os.path.join(C.DATA_DIR, "rate_ppm.json")
    data = __import__("json").load(open(old)); data.pop("v")
    __import__("json").dump(data, open(old, "w"))
    said.clear(); C.log.addHandler(h)
    stale = C.Controller({}); stale.load_rates()
    C.log.removeHandler(h)
    assert stale.rate_base == {}, "rates saved by the 0.5.0 pacer must not be reused"
    assert any("not reused" in m for m in said), "and the log says so"
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
    assert cfg["initial_ppm"] == C.DEFAULT_PPM[48000] == -3250.0 and cfg["samplerate"] == 48000
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
    assert sorted(posted) == sorted([(sa, "Dovnitr"), (sb, "off"), (sd, "LARA All"),
                                     (C.STATUS_SENSOR, ctl.version)]), posted
    posted.clear(); await publish(1001.0)
    assert posted == [], "nothing changed, nothing posted"
    ctl.target[B] = ctl.mount_for(B)
    await publish(1002.0)
    assert posted == [(sb, "LARA Obývák")], posted
    posted.clear(); await publish(1000.0 + C.HA_REFRESH + 1)
    assert len(posted) == 4, "the periodic refresh re-posts everything"

    def broken(e, s, a): raise OSError("401 Unauthorized")
    ctl.post_state = broken
    ctl.target[B] = None
    said.clear(); C.log.addHandler(h)
    await publish(2000.0); await publish(2001.0)
    C.log.removeHandler(h)
    assert sum("Home Assistant sensors" in m for m in said) == 1, said
    print("41) one sensor per radio, posted on change, refreshed, failures reported once")

    # The add-on boots through s6-overlay, which hides the container environment from our CMD
    # and keeps it as files. 0.5.0 looked only at the environment: no sensors, first site.
    s6 = tempfile.mkdtemp(prefix="s6env_")
    saved_dirs, saved_env = C.S6_ENV_DIRS, {k: os.environ.pop(k, None)
                                           for k in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN")}
    C.S6_ENV_DIRS = (s6,)
    assert C.supervisor_token() is None
    open(os.path.join(s6, "SUPERVISOR_TOKEN"), "w").write("abc123\n")
    assert C.supervisor_token() == "abc123"
    os.environ["SUPERVISOR_TOKEN"] = "fromenv"
    assert C.supervisor_token() == "fromenv", "the environment still wins when it has it"
    C.S6_ENV_DIRS = saved_dirs
    for k, v in saved_env.items():
        os.environ.pop(k, None)
        if v is not None:
            os.environ[k] = v
    print("42) the Supervisor token is found where s6-overlay keeps it")

    # The backlog line compared sessions that had nothing to do with each other: a poll from
    # the evening against one from the next morning (+2.6 MB/s), or one from before a quick
    # off/on against one after it (same listener count, different connections).
    ctl = mk(radios=[(A, "Koupelna")])
    m = ctl.mount_for(A)
    xml = {"v": ""}

    class Resp:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return xml["v"].encode()
    saved_open = C.urllib.request.urlopen
    C.urllib.request.urlopen = lambda req, timeout=5: Resp()
    C.time = clk

    def poll(read, sent, lis, pushed=False):
        xml["v"] = (f"<icestats><source mount='/{m}'><total_bytes_read>{read}</total_bytes_read>"
                    f"<total_bytes_sent>{sent}</total_bytes_sent><listeners>{lis}</listeners>"
                    f"</source></icestats>")
        if pushed:                                   # a radio joined between the two polls
            clk.advance(10); ctl._mount_pushed_at[m] = clk.monotonic()
        clk.advance(C.BACKLOG_EVERY)
        said.clear(); C.log.addHandler(h)
        ctl.measure_backlog()
        C.log.removeHandler(h)
        return [s for s in said if "backlog" in s]
    poll(1_000_000, 900_000, 1)
    assert poll(1_000_000 + 24000 * 300, 900_000 + 24000 * 300, 1), "a normal reading is logged"
    poll(9_000_000, 8_000_000, 0)                    # zone off overnight
    assert poll(30_000_000, 8_100_000, 1) == [], "first poll of a new session: no comparison"
    assert poll(30_000_000 + 7_200_000, 8_100_000 + 7_100_000, 1, pushed=True) == [], \
        "a radio rejoined since the last poll: not like for like"
    r0, s0 = 30_000_000 + 2 * 7_200_000, 8_100_000 + 7_100_000 + 7_200_000
    line = poll(r0, s0, 1)
    assert line and "source" not in line[0], "still filling: no source rate yet"
    ctl._rate_settled.add(m)                         # the initial fill is over
    line = poll(r0 + 7_200_000, s0 + 7_200_000, 1)
    assert line and "source" not in line[0], "the steady span starts here"
    line = poll(r0 + 2 * 7_200_000, s0 + 2 * 7_200_000, 1)
    assert line and "source 24000 B/s (+0 ±16667 ppm over 5 min)" in line[0], line
    # The two clocks disagreeing (the suspected cause of "+600 ppm" at the first site) is said.
    real_wall = clk.time
    clk.time = lambda: 1.7e9 + clk.t * (1 - 0.0038)
    said.clear(); C.log.addHandler(h)
    clk.advance(C.BACKLOG_EVERY); ctl.measure_backlog()
    clk.advance(C.BACKLOG_EVERY); ctl.measure_backlog()
    C.log.removeHandler(h)
    assert any("monotonic clock ran +38" in s for s in said), said
    clk.time = real_wall
    C.urllib.request.urlopen = saved_open
    C.time = real_time
    print("43) the backlog is only compared within one unbroken listening session")

    # "The radio switched itself off" has needed this line every time: which event made the
    # zone go quiet. Logged on the change, with the event, once — not every tick.
    ctl = mk(radios=[(A, "Koupelna")])
    m = ctl.mount_for(A)
    state = os.path.join(C.STATE_DIR, f"spotify_state_{m}")
    saved_active = C.spotify_active
    C.spotify_active = lambda mount, ev=None: (C.spotify_event(mount) if ev is None else ev) in C.ACTIVE_EVENTS
    said.clear(); C.log.addHandler(h)
    for ev in ("playing", "playing", "end_of_track", "loading", "paused", "paused", "playing"):
        open(state, "w").write(ev)
        await ctl.tick()
    C.log.removeHandler(h)
    C.spotify_active = saved_active
    lines = [s for s in said if s.startswith("Spotify on")]
    assert lines == [f"Spotify on /{m} is playing (playing)",
                     f"Spotify on /{m} went quiet (end_of_track)",
                     f"Spotify on /{m} is playing (loading)",     # the next track, same session
                     f"Spotify on /{m} went quiet (paused)",
                     f"Spotify on /{m} is playing (playing)"], lines
    print("44) each change of Spotify's state on a zone is logged once, with the event")

    # The site 5 complaint: "the radio switches off while I'm listening to radio". The LARA
    # sends `play` from its own buttons on its own stations too, and we used to answer by
    # pulling it into a silent audio zone. Only Spotify actually playing to it may do that.
    C.time = clk
    ctl = mk({"idle_timeout": 60}, radios=[(A, "Koupelna"), (B, "Obývák")])
    mine = ctl.mount_for(A)
    active_mounts = set(); events.clear()
    await ctl.on_cli_command(A, "play")
    await ctl.on_cli_command(A, "power_on")
    assert events == [], f"no Spotify playing -> the radio stays on its station: {events}"
    active_mounts = {"all"}
    await ctl.on_cli_command(A, "play")
    assert events == [], "nor with Spotify playing: only Spotify starts the zone (by decision)"
    await ctl.on_cli_command(A, "pause")
    assert events == [], "pause is ignored too (0.5.5): pausing one radio on a live stream only " \
                         "makes it fall behind, and past ~20 s Icecast drops it"
    print("45) play on a radio's own buttons never switches it into the audio zone")

    # Its own stop button (or switching to its own station) while Spotify plays on: off, and
    # it stays off until that Spotify session ends — then it follows Spotify again.
    active_mounts = {mine}; events.clear()
    clk.advance(C.ZONE_SETTLE + 1); await ctl.tick()
    clk.advance(C.ZONE_SETTLE + 1); await ctl.tick()
    assert ctl.target.get(A) == mine, ctl.target
    clk.advance(C.STOP_ECHO_GRACE + 1)
    await ctl.on_cli_command(A, "stop")
    assert ctl._held.get(A) == mine and ctl.target.get(A) is None
    events.clear()
    for _ in range(10):
        clk.advance(1); await ctl.tick()
    assert not [e for e in events if e[0] == "push" and e[1] == A], events
    # Site 5, 0.5.4: a track ending reads as a second of quiet, and that released the hold —
    # the radio its owner had silenced switched itself back on at 15:42, 17:39 and 00:14.
    state = os.path.join(C.STATE_DIR, f"spotify_state_{mine}")
    for gap in (1, 8):                          # a normal track change; a track failing to load
        open(state, "w").write("end_of_track")
        active_mounts = set()
        for _ in range(gap):
            clk.advance(1); await ctl.tick()
        open(state, "w").write("loading")
        active_mounts = {mine}; clk.advance(1); await ctl.tick()
        assert ctl._held.get(A) == mine, f"a {gap}s gap between tracks must not end the hold"
    assert not [e for e in events if e[0] == "push" and e[1] == A], events
    # A power cut at the breaker (the customer's next move) must not bring it back either.
    ctl.on_slim_disconnect(type("P", (), {"mac": A, "ip": ""})())
    clk.advance(5); await ctl.tick()
    assert ctl._held.get(A) == mine and not [e for e in events if e[0] == "push"]
    # Pausing Spotify itself ends the hold; so does a queue that ran out (quiet long enough).
    open(state, "w").write("paused")
    active_mounts = set(); clk.advance(1); await ctl.tick()       # Spotify paused
    assert A not in ctl._held
    ctl._held[A] = mine
    open(state, "w").write("end_of_track")
    for _ in range(int(max(C.HOLD_RELEASE_QUIET, ctl.idle_timeout)) + 2):
        clk.advance(1); await ctl.tick()
    assert A not in ctl._held, "a playlist that simply ended must release it eventually"
    active_mounts = {mine}; events.clear()
    clk.advance(1); await ctl.tick()
    assert ("push", A, mine) in events, "a new Spotify session takes the radio again"
    # The echo of our OWN strm-q is not a press and must not hold anything.
    clk.advance(C.ZONE_SETTLE + 1)
    await ctl.zone_off(A)
    await ctl.on_cli_command(A, "stop")
    assert A not in ctl._held
    C.time = real_time
    print("46) a stop from the radio itself is respected until that Spotify session ends")

    # The owner's rule (2026-10-11): stop on the radio silences the radio; when it was the
    # last radio on that zone, Spotify itself is stopped too. Own zone = always the last one.
    C.time = clk
    ctl = mk({"idle_timeout": 60}, radios=[(A, "Koupelna"), (B, "Obývák")])
    ended = []
    ctl.end_spotify_session = lambda m: ended.append(m) or 1
    mine = ctl.mount_for(A)
    active_mounts = {mine}
    for _ in range(2):
        clk.advance(C.ZONE_SETTLE + 1); await ctl.tick()
    clk.advance(C.STOP_ECHO_GRACE + 1)
    await ctl.on_cli_command(A, "stop")
    assert ended == [mine], ended
    # a group: the first radio to stop only silences itself, the last one ends Spotify
    ended.clear(); ctl._held.clear()
    active_mounts = {"all"}
    for _ in range(3):
        clk.advance(C.ZONE_SETTLE + 1); await ctl.tick()
    assert ctl.target.get(A) == "all" and ctl.target.get(B) == "all", ctl.target
    clk.advance(C.STOP_ECHO_GRACE + 1)
    await ctl.on_cli_command(A, "stop")
    assert ended == [] and ctl.target.get(B) == "all", "the others play on"
    await ctl.on_cli_command(B, "stop")
    assert ended == ["all"], ended
    # the echo of our own strm-q is never a press, and never ends anything
    ended.clear(); ctl._held.clear()
    for _ in range(2):
        clk.advance(C.ZONE_SETTLE + 1); await ctl.tick()
    await ctl.zone_off(A)
    await ctl.on_cli_command(A, "stop")
    assert ended == []
    C.time = real_time

    # ...and the session is ended by finding that zone's librespot (either engine) via /proc
    proc = tempfile.mkdtemp(prefix="proc_")
    for pid, exe, mount in (("101", "librespot", "all"), ("102", "/usr/local/bin/librespot",
                            "lara_x"), ("103", "/bin/sh", "all")):
        os.makedirs(os.path.join(proc, pid))
        open(os.path.join(proc, pid, "cmdline"), "wb").write(exe.encode() + b"\0--name\0Z\0")
        open(os.path.join(proc, pid, "environ"), "wb").write(
            b"PATH=/bin\0LR3_MOUNT=" + mount.encode() + b"\0")
    killed = []
    saved_proc, saved_kill = C.PROC, C.os.kill
    C.PROC, C.os.kill = proc, (lambda pid, sig: killed.append(pid))
    try:
        assert C.Controller({}).end_spotify_session("all") == 1
    finally:
        C.PROC, C.os.kill = saved_proc, saved_kill
    assert killed == [101], killed
    assert open(os.path.join(C.STATE_DIR, "spotify_state_all")).read() == "stopped"
    print("47) the last radio on a zone stopping ends Spotify there; one of a group does not")

    # The add-on's own sensor: version, which release, and how many versions ran here. The
    # history lives in /data, gains an entry only when the version changes, and starts at the
    # first version that kept one.
    import json
    import re
    C.VERSIONS_FILE = os.path.join(tempfile.mkdtemp(prefix="lr3ver_"), "versions.json")
    C.CHANGELOG = os.path.join(REPO, "lr3_audiozone", "CHANGELOG.md")
    shipped = sum(1 for line in open(C.CHANGELOG, encoding="utf-8") if line.startswith("## "))
    assert C.release_number() == shipped and shipped > 30
    assert C.release_number(C.CHANGELOG + ".missing") is None
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d", C.local_now())
    ctl = mk(radios=[(A, "Koupelna"), (B, "Obývák")])
    assert ctl.release == shipped
    ctl.version = "?"
    assert ctl.record_version() == [] and not os.path.exists(C.VERSIONS_FILE), \
        "an unknown version is never recorded"
    ctl.version = "0.5.8"
    said.clear(); C.log.addHandler(h)
    ctl.record_version(); ctl.record_version()               # a restart does not count
    C.log.removeHandler(h)
    assert [v["version"] for v in ctl.versions] == ["0.5.8"]
    assert sum("first version recorded" in m for m in said) == 1, said
    ctl = mk(radios=[(A, "Koupelna"), (B, "Obývák")])
    ctl.version = "0.5.9"
    ctl.record_version()
    assert [v["version"] for v in C.load_versions(C.VERSIONS_FILE)] == ["0.5.8", "0.5.9"]
    state, attrs = ctl.status_state()
    assert state == "0.5.9" and attrs["versions_here"] == 2 and attrs["counted_since"] == "0.5.8"
    assert attrs["history"][0].startswith("0.5.9 (") and attrs["history"][1].startswith("0.5.8 (")
    assert attrs["updated"] == ctl.versions[-1]["since"][:10] and attrs["release"] == shipped
    assert re.fullmatch(r"\d{4}-\d\d-\d\d", attrs["updated"])
    assert attrs["radios"] == 2 and "addon" not in attrs
    for n in range(10, 30):                                  # the sensor lists only the latest
        ctl.version = f"0.5.{n}"; ctl.record_version()
    _, attrs = ctl.status_state()
    assert attrs["versions_here"] == 22 and len(attrs["history"]) == C.HISTORY_SHOWN
    assert attrs["history"][0].startswith("0.5.29 ") and attrs["counted_since"] == "0.5.8"
    open(C.VERSIONS_FILE, "w").write('{"history": [{"version": "0.5.8", "since": 1760000000}]}')
    assert C.load_versions(C.VERSIONS_FILE) == [], "an entry status_state() cannot read is dropped"
    open(C.VERSIONS_FILE, "w").write("{not json")
    assert C.load_versions(C.VERSIONS_FILE) == []
    ctl.record_version()
    assert [v["version"] for v in C.load_versions(C.VERSIONS_FILE)] == ["0.5.29"], \
        "a damaged file starts the count again rather than stopping the add-on"

    # the restart button needs the Supervisor's id for the add-on; ask, and survive a refusal
    class Info:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return json.dumps({"result": "ok",
                                           "data": {"slug": "0f1e2d3c_lr3_audiozone"}}).encode()
    asked = []

    def info(req, timeout=5):
        asked.append((req.full_url, req.get_header("Authorization")))
        return Info()
    saved_open = C.urllib.request.urlopen
    C.urllib.request.urlopen = info
    ctl.ha_token = "tok"
    try:
        await ctl.learn_addon_slug()
    finally:
        C.urllib.request.urlopen = saved_open
    assert asked == [("http://supervisor/addons/self/info", "Bearer tok")], asked
    assert ctl.addon_slug == "0f1e2d3c_lr3_audiozone"
    assert ctl.status_state()[1]["addon"] == "0f1e2d3c_lr3_audiozone"

    def refused(req, timeout=5): raise OSError("403 Forbidden")
    C.urllib.request.urlopen = refused
    ctl.addon_slug = None
    said.clear(); C.log.addHandler(h)
    try:
        await ctl.learn_addon_slug()
    finally:
        C.urllib.request.urlopen = saved_open
        C.log.removeHandler(h)
    assert ctl.addon_slug is None and any("could not ask the Supervisor" in m for m in said)
    # ...in which case the container's name gives it away: the slug with "-" for "_"
    saved_host = C.socket.gethostname
    C.socket.gethostname = lambda: "162678ec-lr3-audiozone"
    C.urllib.request.urlopen = refused
    try:
        await ctl.learn_addon_slug()
        assert ctl.addon_slug == "162678ec_lr3_audiozone"
        C.socket.gethostname = lambda: "homeassistant"
        assert C.Controller.slug_from_hostname() is None
    finally:
        C.urllib.request.urlopen = saved_open
        C.socket.gethostname = saved_host
    print("48) sensor.lr3_audiozone: version, release number, this installation's history")

    # The learned base waits for each session's initial fill: filling the radio from ~60 KB is
    # us sending fast on purpose, not the radio playing fast. Integrating it made every short
    # session save a rate ~1000 ppm fast (-2256 where the radios take -3260, 2026-10-14).
    def session(minutes, start_x=61440.0):
        ctl = mk(radios=[(A, "Koupelna")])
        mine = ctl.mount_for(A)
        pr = PR(); ctl.slim.players[A] = pr; ctl.target[A] = mine
        d = ctl.default_ppm()                       # the radio plays exactly where we start
        ctl.rate_ppm[mine] = d
        x, q, t = start_x, 0.0, 0.0
        for _ in range(minutes * 60 // 5):
            t += 5.0
            q += RATE * (1 + ctl.rate_ppm[mine] / 1e6) * 5
            out = RATE * (1 + d / 1e6) * 5
            take = min(q, SIZE - (x - out))
            q -= take; x = x - out + take
            pr.in_buf, pr.in_buf_at = int(x + rnd.uniform(-2500, 2500)), t
            ctl.update_rates(t)
        return ctl.rate_base[mine] - d
    for minutes in (12, 240):
        assert abs(session(minutes)) < 300, f"{minutes} min: learned {session(minutes):+.0f}"
    saved_band = C.RATE_SETTLE_BAND
    C.RATE_SETTLE_BAND = 1e9                        # = 0.5.2–0.5.8: learning from the first reading
    try:
        assert session(12) > 600, "the test must see the old windup"
    finally:
        C.RATE_SETTLE_BAND = saved_band
    # A base far enough off can hold the buffer outside the band on the proportional term
    # alone; after RATE_SETTLE_MAX it learns anyway, or it never would.
    ctl = mk(radios=[(A, "Koupelna")])
    mine = ctl.mount_for(A)
    pr = PR(); ctl.slim.players[A] = pr; ctl.target[A] = mine
    b0, t = ctl.default_ppm(), 0.0
    for _ in range(int(C.RATE_SETTLE_MAX // 5) - 1):
        t += 5.0; pr.in_buf, pr.in_buf_at = 70000, t; ctl.update_rates(t)
    assert ctl.rate_base[mine] == b0, "still filling: the base has not moved"
    for _ in range(3):
        t += 5.0; pr.in_buf, pr.in_buf_at = 70000, t; ctl.update_rates(t)
    assert ctl.rate_base[mine] > b0, "fifteen minutes in, it learns even outside the band"
    pr.in_buf_at = t - 2 * C.RATE_FRESH
    t += 5.0; ctl.update_rates(t)
    assert mine not in ctl._rate_settled, "readings gone: the next ones begin a new session"
    # ...and so does a radio joining a group that is already playing (or re-pushed after an
    # underrun): the other radio keeps the session alive, so only the push can say so.
    def join(band=None):
        saved = C.RATE_SETTLE_BAND
        if band is not None:
            C.RATE_SETTLE_BAND = band
        try:
            ctl = mk(radios=[(A, "Koupelna"), (B, "Obývák")])
            g = C.GROUP_MOUNT
            d = ctl.default_ppm()
            ctl.rate_ppm[g] = d

            class R: mode, _stall_logged, buf_size = "play", False, SIZE
            ra, rb = R(), R()
            ctl.slim.players.update({A: ra, B: rb}); ctl.target[A] = g
            xs, qs, t = {A: ctl.rate_target(SIZE), B: 61440.0}, {A: 0.0, B: 0.0}, 100.0
            for step in range(60 * 60 // 5 + 10 * 60 // 5):    # A alone for 1 h, then B too
                t += 5.0
                if step == 60 * 60 // 5:
                    ctl.target[B] = g
                    ctl._mount_pushed_at[g] = ctl._repushed_at[B] = t - 2.0
                for mac, r in ((A, ra), (B, rb)):
                    if ctl.target.get(mac) != g:
                        continue
                    qs[mac] += RATE * (1 + ctl.rate_ppm[g] / 1e6) * 5
                    out = RATE * (1 + d / 1e6) * 5
                    take = min(qs[mac], SIZE - (xs[mac] - out))
                    qs[mac] -= take; xs[mac] = xs[mac] - out + take
                    r.in_buf, r.in_buf_at = int(xs[mac] + rnd.uniform(-2500, 2500)), t
                ctl.update_rates(t)
            return ctl.rate_base[g] - d
        finally:
            C.RATE_SETTLE_BAND = saved
    assert abs(join()) < 300, f"a radio joining a playing group: learned {join():+.0f}"
    assert join(band=1e9) > 600, "the test must see the windup the push now prevents"
    # A reading from before a radio's latest push belongs to its old stream (push_stream sets
    # mode=play at once): it must not start the new mount's session as if already full.
    ctl = mk(radios=[(A, "Koupelna")])
    g = C.GROUP_MOUNT

    class R2: mode, _stall_logged, buf_size = "play", False, SIZE
    r = R2(); r.in_buf, r.in_buf_at = 108000, 50.0
    ctl.slim.players[A] = r
    ctl.target[A] = g
    ctl._repushed_at[A] = ctl._mount_pushed_at[g] = 51.0
    ctl.update_rates(52.0)
    assert g not in ctl._rate_settled and g not in ctl._rate_x, "the old stream's reading counts"
    print("49) the learned rate ignores every fill — a session's, a joining radio's — and "
          "cannot get stuck waiting for it")

    # The source rate in the backlog line. Icecast refreshes its byte counter only every 5 s, so
    # the 5-minute difference read 0.17 % low nine times in ten and 1.5 % high the tenth — and
    # the low reading became DEFAULT_PPM -4800. Measured from one steady point it converges on
    # the truth, and the ± it prints always covers the error.
    ctl = mk(radios=[(A, "Koupelna")])
    m = ctl.mount_for(A)
    ctl._rate_settled.add(m)
    true = 24000 * (1 - 3250e-6)
    t0 = clk.t
    C.time = clk
    C.urllib.request.urlopen = lambda req, timeout=5: Resp()
    said.clear(); C.log.addHandler(h)
    try:
        for _ in range(80):
            clk.advance(C.BACKLOG_EVERY + 0.49)
            n = int(true * 5 * ((clk.t - t0 + 2.3) // 5))     # what Icecast last published
            xml["v"] = (f"<icestats><source mount='/{m}'><total_bytes_read>{10**9 + n}"
                        f"</total_bytes_read><total_bytes_sent>{n}</total_bytes_sent>"
                        f"<listeners>1</listeners></source></icestats>")
            ctl.measure_backlog()
    finally:
        C.log.removeHandler(h)
        C.urllib.request.urlopen = saved_open
        C.time = real_time
    got = [__import__("re").search(r"source \d+ B/s \(([-+]\d+) ±(\d+) ppm", s) for s in said]
    got = [(int(g.group(1)), int(g.group(2))) for g in got if g]
    assert len(got) >= 77, said[:3]
    assert all(abs(ppm + 3250) <= err + 1 for ppm, err in got), got
    assert got[-1][1] < 220 and abs(got[-1][0] + 3250) < 220, got[-1]
    print("50) the source rate converges on the truth, and its ± is honest")

    # Site 5, 2026-10-13 05:32: librespot lost its connection, took the evening's session over
    # by itself, loaded the last track and paused a second later — and KP2 was already in its
    # audio zone. Only Spotify actually playing may switch a radio on; loading alone may not.
    C.time = clk
    saved_active = C.spotify_active
    C.spotify_active = lambda mount, ev=None: (C.spotify_event(mount) if ev is None else ev) in C.ACTIVE_EVENTS
    ctl = mk(radios=[(A, "Koupelna")])
    m = ctl.mount_for(A)
    state = os.path.join(C.STATE_DIR, f"spotify_state_{m}")

    def pushes():
        return [e for e in events if e[0] == "push"]
    events.clear(); said.clear(); C.log.addHandler(h)
    try:
        open(state, "w").write("stopped")                   # last night's session, over
        clk.advance(1); await ctl.tick()
        for ev in ("loading", "track_changed", "paused"):    # the takeover
            open(state, "w").write(ev)
            clk.advance(1); await ctl.tick()
        assert not pushes(), events
        assert sum("without playing it" in s for s in said) == 1, said
        # one that does not even pause: said once after a while, and still nothing switched on
        open(state, "w").write("loading")
        for _ in range(int(C.PREPLAY_NOTE_AFTER) + 3):
            clk.advance(1); await ctl.tick()
        assert sum("has been loading" in s for s in said) == 1, said
        assert not pushes(), events
        # a real start: the track plays a second later -> on
        open(state, "w").write("playing")
        clk.advance(C.ZONE_SETTLE + 1); await ctl.tick()
        clk.advance(C.ZONE_SETTLE + 1); await ctl.tick()
        assert ctl.target.get(A) == m and len(pushes()) == 1, events
        # inside the session, the next track's loading keeps it on: no OFF, no second push
        events.clear()
        for ev in ("end_of_track", "loading", "track_changed", "playing", "end_of_track",
                   "loading"):
            open(state, "w").write(ev)
            clk.advance(1); await ctl.tick()
        assert ctl.target.get(A) == m and not events, events
        # a playlist that simply ran out (no pause, no stop) closes the session after a minute
        # of quiet, so a takeover hours later does not find it still open
        open(state, "w").write("end_of_track")
        for _ in range(int(max(C.HOLD_RELEASE_QUIET, ctl.idle_timeout)) + 3):
            clk.advance(1); await ctl.tick()
        assert ctl.target.get(A) is None, "the zone went off as usual"
        events.clear()
        open(state, "w").write("loading")
        for _ in range(5):
            clk.advance(1); await ctl.tick()
        assert not pushes(), events
    finally:
        C.log.removeHandler(h)
        C.spotify_active = saved_active
        C.time = real_time
    print("51) loading alone (librespot resuming a session by itself) never switches a radio on")

    # A start within 30 s of a track's end: `preloading` (the next track) follows `playing` in
    # well under the second between two ticks, so the state file never shows `playing`. The
    # hook's separate mark does. An old mark, from before the add-on started, starts nothing.
    C.time = clk
    saved_active = C.spotify_active
    C.spotify_active = lambda mount, ev=None: (C.spotify_event(mount) if ev is None else ev) in C.ACTIVE_EVENTS
    ctl = mk(radios=[(A, "Koupelna")])
    m = ctl.mount_for(A)
    state = os.path.join(C.STATE_DIR, f"spotify_state_{m}")
    mark = os.path.join(C.STATE_DIR, f"spotify_played_{m}")
    events.clear()
    try:
        open(mark, "w").write("1000")                       # yesterday's
        open(state, "w").write("preloading")
        for _ in range(3):
            clk.advance(1); await ctl.tick()
        assert not pushes(), "an old mark is not a start"
        open(state, "w").write("stopped")
        clk.advance(1); await ctl.tick()
        # loading -> track_changed -> playing -> preloading, all between two ticks
        open(mark, "w").write("2000"); open(state, "w").write("preloading")
        clk.advance(C.ZONE_SETTLE + 1); await ctl.tick()
        clk.advance(C.ZONE_SETTLE + 1); await ctl.tick()
        assert ctl.target.get(A) == m, "the mark says it played"
        # ...a failed preload in between (unavailable, then the replacement's preloading)
        open(state, "w").write("paused"); clk.advance(1); await ctl.tick()
        for _ in range(int(ctl.idle_timeout) + 2):
            clk.advance(1); await ctl.tick()
        assert ctl.target.get(A) is None
        events.clear()
        open(mark, "w").write("3000"); open(state, "w").write("unavailable")
        clk.advance(1); await ctl.tick()
        open(state, "w").write("preloading")
        clk.advance(C.ZONE_SETTLE + 1); await ctl.tick()
        clk.advance(C.ZONE_SETTLE + 1); await ctl.tick()
        assert ctl.target.get(A) == m, events
        # played and paused again within one tick is not a start
        open(state, "w").write("paused"); clk.advance(1); await ctl.tick()
        for _ in range(int(ctl.idle_timeout) + 2):
            clk.advance(1); await ctl.tick()
        events.clear()
        open(mark, "w").write("4000"); open(state, "w").write("paused")
        for _ in range(3):
            clk.advance(1); await ctl.tick()
        assert not pushes(), events
        # The radio's stop in the gap between two tracks of a running session is a press too:
        # it holds the radio, instead of the next song switching it straight back on.
        open(mark, "w").write("5000"); open(state, "w").write("playing")
        clk.advance(C.ZONE_SETTLE + 1); await ctl.tick()
        clk.advance(C.ZONE_SETTLE + 1); await ctl.tick()
        assert ctl.target.get(A) == m
        open(state, "w").write("end_of_track"); clk.advance(1); await ctl.tick()
        clk.advance(C.STOP_ECHO_GRACE + 1)
        ctl.end_spotify_session = lambda mount: 0
        await ctl.on_cli_command(A, "stop")
        assert ctl._held.get(A) == m, "a stop between two songs holds the radio"
        open(state, "w").write("loading"); clk.advance(1); await ctl.tick()
        open(mark, "w").write("6000"); open(state, "w").write("playing")
        events.clear()
        for _ in range(3):
            clk.advance(C.ZONE_SETTLE + 1); await ctl.tick()
        assert not pushes(), events
    finally:
        C.spotify_active = saved_active
        C.time = real_time
    print("52) a start the state file missed is caught by the hook's mark; a stop between two "
          "songs holds the radio")

    # The loop's one-second pause is cut short by a new `playing` mark (Icecast gives a radio
    # no burst, so every fraction of a second late is cut off the first song) — and a stop in
    # the same second as a pause in the app does not end that freshly paused session.
    ctl = mk(radios=[(A, "Koupelna")])
    m = ctl.mount_for(A)
    mark = os.path.join(C.STATE_DIR, f"spotify_played_{m}")
    open(mark, "w").write("7000")
    stop_ev = asyncio.Event()

    async def bump():
        await asyncio.sleep(0.25)
        open(mark, "w").write("7001")
    t0 = time.monotonic()
    await asyncio.gather(ctl.pause(stop_ev), bump())
    assert time.monotonic() - t0 < 0.7, "a new mark ends the pause early"
    t0 = time.monotonic()
    await ctl.pause(stop_ev)
    assert 0.9 < time.monotonic() - t0 < 1.5, "without one it is the usual second"
    stop_ev.set(); t0 = time.monotonic()
    await ctl.pause(stop_ev)
    assert time.monotonic() - t0 < 0.1, "and shutting down is not delayed"
    C.time = clk
    saved_active = C.spotify_active
    C.spotify_active = lambda mount, ev=None: (
        C.spotify_event(mount) if ev is None else ev) in C.ACTIVE_EVENTS
    state = os.path.join(C.STATE_DIR, f"spotify_state_{m}")
    try:
        open(state, "w").write("playing")
        clk.advance(C.ZONE_SETTLE + 1); await ctl.tick()
        assert ctl.target.get(A) == m
        open(state, "w").write("paused")                     # the app pauses...
        clk.advance(C.STOP_ECHO_GRACE + 1)
        ended = []
        ctl.end_spotify_session = lambda mount: ended.append(mount)
        await ctl.on_cli_command(A, "stop")                  # ...and the radio's stop, before a tick
        assert A not in ctl._held and ended == [], (ctl._held, ended)
    finally:
        C.spotify_active = saved_active
        C.time = real_time
    print("53) a new `playing` cuts the loop's pause short; a stop right after a pause in the app "
          "leaves that session alone")

    # 0.5.12: no leaked-password defaults (the Supervisor flagged "changeme" / "elkoep" as
    # insecure secrets). Icecast's password is whatever run.sh configured it with — the option,
    # or a random one when the option is empty; the radios' login falls back to the factory one.
    saved_env = os.environ.pop("LR3_SOURCE_PASSWORD", None)
    try:
        os.environ["LR3_SOURCE_PASSWORD"] = "0f1e2d3c4b5a69788796a5b4c3d2e1f0abcd"
        ctl = mk({"source_password": ""}, radios=[(A, "Koupelna")])
        assert ctl.source_password == "0f1e2d3c4b5a69788796a5b4c3d2e1f0abcd"
        cfg = __import__("json").load(open(ctl.write_pacer_config(ctl.zones[0]), encoding="utf-8"))
        assert cfg["source_password"] == ctl.source_password, "the pacer gets the same one"
        os.environ["LR3_SOURCE_PASSWORD"] = "mine"                # run.sh exports the option too
        assert C.Controller({"source_password": "mine"}).source_password == "mine"
        del os.environ["LR3_SOURCE_PASSWORD"]
        assert C.Controller({"source_password": "x"}).source_password == "x"
        assert C.Controller({}).source_password == "changeme", "only without run.sh (tests)"
    finally:
        os.environ.pop("LR3_SOURCE_PASSWORD", None)
        if saved_env is not None:
            os.environ["LR3_SOURCE_PASSWORD"] = saved_env
    lara = C.Controller({"lara_password": "", "lara_username": ""})
    assert (lara.user, lara.password) == ("admin", "elkoep"), "empty = the radios' factory login"
    lara = C.Controller({"lara_password": "tajne", "lara_username": "jan"})
    assert (lara.user, lara.password) == ("jan", "tajne")
    manifest = open(os.path.join(REPO, "lr3_audiozone", "config.yaml"), encoding="utf-8").read()
    for opt_line in ('  source_password: ""', '  lara_password: ""',
                     "  source_password: password?", "  lara_password: password?"):
        assert opt_line in manifest.splitlines(), opt_line
    print("54) no leaked-password defaults: a random Icecast password, the radios' factory login")

    # stream_watch is a diagnostic: a typo in it must never stop the add-on (it ran before the
    # Slim servers start, so one ValueError used to leave every radio without its server).
    ctl = C.Controller({"stream_watch": ["http://82.99.175.141:8o/x.mp3", "ftp://h/x", " ", 7]})
    said.clear(); h.setLevel(C.logging.INFO); C.log.addHandler(h)
    try:
        ctl.start_stream_watch()
    finally:
        C.log.removeHandler(h)
    assert ctl.watchers is None and sum("stream_watch: ignoring" in s for s in said) == 2, said
    real = C.streamwatch.Watchers
    C.streamwatch.Watchers = lambda urls: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        ctl = C.Controller({"stream_watch": ["http://127.0.0.1:9/x.mp3"]})
        ctl.start_stream_watch()                       # must not raise
        assert ctl.watchers is None
    finally:
        C.streamwatch.Watchers = real
    print("55) a broken stream_watch entry is reported and skipped, never fatal")


asyncio.run(run())
print("\nALL OK")
