#!/usr/bin/env python3
"""LR3 LARA controller — one Spotify Connect device per radio, plus an "all radios" one.

At start-up the add-on looks for LARAs on the LAN, reads the name each one carries in its own
configuration, and brings up **one audio pipeline per radio**: its own librespot (so it shows
up in the Spotify app under the radio's name), its own Liquidsoap and its own Icecast mount.
With two or more radios it also creates a group device (`group_name`, "LARA All") that feeds
every radio at once. With a single radio the group would be a duplicate of it, so it is left out.

    Spotify plays to "LARA Koupelna"  ->  that radio joins the zone and plays
    Spotify plays to "LARA All"       ->  every radio joins the zone and plays the same mount
    Spotify idle for `idle_timeout`   ->  radios drop out and return to their station list

A radio's own device always wins over the group, so starting playback on one radio pulls it out
of a group session without disturbing the others.

The set of devices is fixed at start-up: a radio switched on later is still driven (it joins the
group and can be pushed), but it gets no Connect device of its own until the add-on restarts.

control_mode: `slimproto` (default) or `off` (stream only, never touch the radios).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import socket
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discovery  # noqa: E402
import laradev  # noqa: E402
from lmscli import DEFAULT_CLI_PORT, LmsCliServer  # noqa: E402
from slimproto import SlimProtoServer  # noqa: E402

logging.basicConfig(level=logging.INFO, format="[lr3ctl] %(levelname)s %(message)s")
log = logging.getLogger("lr3.ctl")

OPTIONS = "/data/options.json"
STATE_DIR = "/tmp"
LIQ_TEMPLATE = "/etc/lr3/radio.liq.tpl"
GROUP_MOUNT = "all"        # the "every radio" zone
FALLBACK_MOUNT = "default"  # used only when no radio could be found at start-up
ACTIVE_EVENTS = {"playing", "started", "track_changed", "changed", "loading", "preloading"}


def opt(cfg, key, default):
    v = cfg.get(key, default)
    return default if v is None else v


def host_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def spotify_active(mount: str) -> bool:
    try:
        with open(os.path.join(STATE_DIR, f"spotify_state_{mount}")) as f:
            return f.read().strip() in ACTIVE_EVENTS
    except OSError:
        return False


def spotify_track(mount: str) -> tuple[str, str]:
    """(title, artist) of what this zone is playing — written by the librespot event hook.

    The LARA polls the LMS CLI for `current_title ?` and `artist ?` roughly every few seconds
    while it plays, so putting real values here is all it takes to get the track on its display
    instead of the zone name. Empty strings when librespot has not reported a track (yet).
    """
    try:
        with open(os.path.join(STATE_DIR, f"spotify_track_{mount}"), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return "", ""
    return (lines[0].strip() if lines else "",
            lines[1].strip() if len(lines) > 1 else "")


class Zone:
    """One Spotify Connect device = one librespot + one Liquidsoap + one Icecast mount."""

    def __init__(self, mount: str, name: str, radios: list[str] | None):
        self.mount = mount
        self.name = name
        self.radios = radios   # None = every radio we know about

    def covers(self, mac: str) -> bool:
        return self.radios is None or mac in self.radios

    @property
    def is_group(self) -> bool:
        return self.radios is None

    def __repr__(self):
        return f"<Zone {self.mount} {self.name!r} {'ALL' if self.is_group else self.radios}>"


class Controller:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.mode = opt(cfg, "control_mode", "slimproto")
        self.user = opt(cfg, "lara_username", "admin")
        self.password = opt(cfg, "lara_password", "elkoep")
        self.hosts = opt(cfg, "lara_hosts", [])
        self.subnet = (opt(cfg, "scan_subnet", "") or "").strip() or None
        self.port = opt(cfg, "port", 8121)
        self.source_password = opt(cfg, "source_password", "changeme")
        self.spotify_bitrate = opt(cfg, "spotify_bitrate", 320)
        self.fallback_name = opt(cfg, "zone_name", "Audio zóna")
        self.group_name = opt(cfg, "group_name", "LARA All")
        self.name_prefix = bool(opt(cfg, "lara_name_prefix", True))
        self.idle_timeout = max(0, int(opt(cfg, "idle_timeout", 8)))
        self.volume = int(opt(cfg, "zone_volume", 90))
        # How much audio the LARA buffers before it starts = the dominant latency in the
        # chain. Expressed in seconds and converted to the KB the strm packet wants, so the
        # delay stays the same whatever the bitrate is.
        self.bitrate = max(32, int(opt(cfg, "bitrate", 192)))
        self.buffer_seconds = max(0.2, float(opt(cfg, "buffer_seconds", 1.5)))
        self.buffer_kb = max(8, int(self.bitrate / 8 * self.buffer_seconds))
        self.cli_port = int(opt(cfg, "cli_port", DEFAULT_CLI_PORT))
        self.cli_user = opt(cfg, "cli_username", "")
        self.cli_pass = opt(cfg, "cli_password", "")
        self.our_ip = host_ip()
        self.radios: dict[str, dict] = {}          # mac -> {rec, dev}
        self.zones: list[Zone] = []                # specific zones first, group last
        self.zone_names: dict[str, str] = {}       # mount -> display name (for the LMS CLI)
        self.procs: dict[str, asyncio.subprocess.Process] = {}
        self.target: dict[str, str | None] = {}    # mac -> mount currently pushed
        self.idle_since: dict[str, float] = {}     # mac -> when its zone went idle
        self.applied_volume: dict[str, int] = {}   # mac -> volume we last sent
        self.slim: SlimProtoServer | None = None
        self.cli: LmsCliServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._warned_offline: set[str] = set()

    # --- inventory -------------------------------------------------------------
    def _add(self, key: str, rec: dict, how: str):
        if key in self.radios:
            return False
        ip = rec.get("ip")
        self.radios[key] = {
            "rec": rec,
            "dev": laradev.LaraDevice(ip, self.user, self.password) if ip else None,
        }
        log.info("radio (%s): %-20s %-15s fw=%s %s", how, rec.get("name", "?"),
                 ip, rec.get("fw", "?"), key)
        return True

    def discover(self):
        """One sweep at start-up. UDP broadcast is useless on fw 3.7.001, the TCP scan is not."""
        found = discovery.find_radios(hosts=self.hosts, subnet=self.subnet)
        for mac, rec in found.items():
            self._add(mac, rec, "discovery")

    # --- zones -----------------------------------------------------------------
    @staticmethod
    def mount_for(mac: str) -> str:
        return "lara_" + mac.replace(":", "").lower()[-6:]

    def display_name(self, rec: dict, mac: str) -> str:
        name = (rec.get("name") or "").strip()
        if not name:
            name = "LARA " + mac.replace(":", "").upper()[-6:]
        if self.name_prefix and not name.lower().startswith("lara"):
            name = f"LARA {name}"
        return name

    def build_zones(self):
        """One zone per radio; plus a group zone when there is more than one radio.

        With a single radio a group device would just be a second name for the same speaker,
        so it is skipped — that is what the user sees in the Spotify app either way.
        """
        macs = list(self.radios.keys())
        zones: list[Zone] = []
        if not macs:
            # Nothing answered. Keep one device alive so Spotify still has somewhere to play;
            # it feeds whatever radio turns up on SlimProto later.
            log.warning("no LARA found on the network — offering a single '%s' device that "
                        "will drive any radio that connects later", self.fallback_name)
            zones.append(Zone(FALLBACK_MOUNT, self.fallback_name, None))
        else:
            used: dict[str, str] = {}
            for mac in macs:
                name = self.display_name(self.radios[mac]["rec"], mac)
                if name in used:   # two radios with the same name — disambiguate by MAC tail
                    name = f"{name} {mac.replace(':', '').upper()[-4:]}"
                used[name] = mac
                zones.append(Zone(self.mount_for(mac), name, [mac]))
            if len(macs) > 1:
                zones.append(Zone(GROUP_MOUNT, self.group_name, None))
        self.zones = zones
        self.zone_names = {z.mount: z.name for z in zones}
        for z in zones:
            log.info("zone /%s  ->  Spotify device %r  (%s)", z.mount, z.name,
                     "všechna rádia" if z.is_group else z.radios[0])

    def zone_for(self, mac: str, active: set[str]) -> str | None:
        """Which mount this radio should play. A radio's own device beats the group device."""
        for z in self.zones:            # specific zones come first, group last
            if z.mount in active and z.covers(mac):
                return z.mount
        return None

    # --- audio pipelines -------------------------------------------------------
    def render_liq(self, zone: Zone) -> str:
        with open(LIQ_TEMPLATE, encoding="utf-8") as f:
            tpl = f.read()
        for key, val in (("PORT", self.port), ("SOURCE_PASSWORD", self.source_password),
                         ("BITRATE", self.bitrate), ("SPOTIFY_BITRATE", self.spotify_bitrate),
                         ("MOUNT", zone.mount), ("ZONE_NAME", zone.name)):
            tpl = tpl.replace(f"%%{key}%%", str(val))
        path = os.path.join(STATE_DIR, f"zone_{zone.mount}.liq")
        with open(path, "w", encoding="utf-8") as f:
            f.write(tpl)
        return path

    async def start_zone(self, zone: Zone):
        os.makedirs(f"/data/librespot_{zone.mount}", exist_ok=True)
        open(os.path.join(STATE_DIR, f"librespot_{zone.mount}.log"), "a").close()
        path = self.render_liq(zone)
        try:
            self.procs[zone.mount] = await asyncio.create_subprocess_exec("liquidsoap", path)
            log.info("zone /%s started (Spotify device %r)", zone.mount, zone.name)
        except Exception:
            log.exception("could not start Liquidsoap for zone /%s", zone.mount)

    async def supervise_zones(self):
        """Restart a pipeline whose Liquidsoap died — otherwise that device vanishes silently."""
        for zone in self.zones:
            proc = self.procs.get(zone.mount)
            if proc is not None and proc.returncode is not None:
                log.warning("Liquidsoap for zone /%s exited (%s) — restarting",
                            zone.mount, proc.returncode)
                self.procs.pop(zone.mount, None)
                await self.start_zone(zone)

    async def stop_zones(self):
        for mount, proc in self.procs.items():
            if proc.returncode is None:
                try:
                    proc.terminate()
                except ProcessLookupError:
                    pass
                log.info("zone /%s stopped", mount)

    # --- SlimProto / CLI callbacks ---------------------------------------------
    def on_slim_connect(self, player):
        """A LARA dialled in. Drive it even if the start-up scan never saw it."""
        if self._add(player.mac, {"ip": player.ip, "name": player.name, "mac": player.mac,
                                  "fw": "?"}, "slimproto"):
            log.info("  (it has no Connect device of its own — restart the add-on to give it "
                     "one; for now it follows %r)",
                     self.zones[-1].name if self.zones else "?")
        self._warned_offline.discard(player.mac)
        self.target.pop(player.mac, None)
        if self._loop:
            self._loop.create_task(self.tick())

    def on_slim_disconnect(self, player):
        self.target.pop(player.mac, None)
        self.idle_since.pop(player.mac, None)

    def on_slim_state(self, player, what: str):
        if self.cli:
            self.cli.notify(player, what)

    async def on_cli_command(self, mac: str, verb: str):
        """The LARA pressed a button / an LMS-style command arrived on :9595."""
        log.info("CLI command from %s: %s", mac, verb)
        if verb in ("play", "power_on"):
            active = {z.mount for z in self.zones if spotify_active(z.mount)}
            mount = self.zone_for(mac, active) or self.zone_for(mac, {z.mount for z in self.zones})
            if mount:
                await self.route(mac, mount)
        elif verb in ("stop", "power_off"):
            await self.zone_off(mac)

    # --- actions ---------------------------------------------------------------
    def desired_volume(self) -> int | None:
        """The level a radio is set to when its zone switches on. None = leave it alone.

        Deliberately NOT tied to the Spotify slider: librespot applies that one in software,
        so mirroring it into `audg` as well would attenuate twice. Two independent controls,
        each in charge of one stage — the app's slider on the stream, the LARA's buttons
        (via the LMS CLI) on the hardware.
        """
        return self.volume if self.volume > 0 else None

    async def apply_volume(self, key: str):
        """Set the starting level once per zone-on; afterwards the LARA's own buttons rule."""
        vol = self.desired_volume()
        if vol is None or self.applied_volume.get(key) == vol:
            return
        await self.slim.set_volume(key, vol)
        self.applied_volume[key] = vol

    async def route(self, key: str, mount: str):
        """Put a LARA into the audio zone and start it on the given mount."""
        if self.target.get(key) == mount or not self.slim:
            return
        ok = await self.slim.push_stream(key, mount)
        if not ok:
            if key not in self._warned_offline:
                self._warned_offline.add(key)
                log.info("LARA %s is not connected to SlimProto (:3483) yet — check that its "
                         "'Audio zone function' is enabled and points at %s", key, self.our_ip)
            return
        self.applied_volume.pop(key, None)
        await self.apply_volume(key)
        self.idle_since.pop(key, None)
        self.target[key] = mount
        rec = self.radios.get(key, {}).get("rec", {})
        log.info("zone ON  %s (%s) -> /%s [%s]", rec.get("name", key), rec.get("ip", "?"),
                 mount, self.zone_names.get(mount, mount))

    async def zone_off(self, key: str):
        """Spotify is done — stop the stream and put the LARA back on its radio list.

        SlimProto alone cannot finish the job: `strm-q` + `aude 0 0` only silences the unit,
        which stays lit showing a dead audio zone. So we follow it with a source switch over
        61695 (`select_source(RADIO)` + `stop`), leaving the radio prepared but not playing.
        Spotify is always started from the phone, so there is no reason to keep the zone warm.
        """
        if self.slim:
            await self.slim.stop(key)
            await self.slim.set_power(key, False)
        dev = self.radios.get(key, {}).get("dev")
        if dev:
            try:
                if not await asyncio.to_thread(dev.park_on_radio):
                    log.warning("LARA %s did not take the switch back to radio — check "
                                "lara_username/lara_password (port 61695 needs them)", key)
            except Exception:
                log.exception("switch back to radio failed for %s", key)
        self.target[key] = None
        self.idle_since.pop(key, None)
        rec = self.radios.get(key, {}).get("rec", {})
        log.info("zone OFF %s (%s)", rec.get("name", key), rec.get("ip", "?"))

    def update_now_playing(self, key: str, mount: str):
        """Keep the LARA's display on the current track rather than the zone name."""
        p = self.slim.players.get(key) if self.slim else None
        if not p:
            return
        title, artist = spotify_track(mount)
        if (p.title, p.artist) != (title, artist):
            p.title, p.artist = title, artist
            log.info("now playing on %s: %s — %s", key, title or "?", artist or "?")
            self.on_slim_state(p, "play")

    async def tick(self):
        active = {z.mount for z in self.zones if spotify_active(z.mount)}
        now = time.monotonic()
        for key in list(self.radios.keys()):
            mount = self.zone_for(key, active)
            if mount:
                await self.route(key, mount)
                if self.target.get(key) == mount:
                    await self.apply_volume(key)
                    self.update_now_playing(key, mount)
            elif self.target.get(key):
                started = self.idle_since.setdefault(key, now)
                if now - started >= self.idle_timeout:
                    await self.zone_off(key)

    # --- main loop -------------------------------------------------------------
    async def run(self):
        self._loop = asyncio.get_running_loop()
        stopping = asyncio.Event()
        for sig in ("SIGTERM", "SIGINT"):
            # run.sh sends SIGTERM on add-on shutdown; without this the process dies before
            # `finally` runs and the Liquidsoap children are orphaned.
            try:
                self._loop.add_signal_handler(getattr(signal, sig), stopping.set)
            except (AttributeError, NotImplementedError, RuntimeError):
                pass
        log.info("controller mode=%s our_ip=%s idle_timeout=%ds buffer=%.1fs (%d KB @ %d kbps)",
                 self.mode, self.our_ip, self.idle_timeout, self.buffer_seconds,
                 self.buffer_kb, self.bitrate)
        try:
            await asyncio.to_thread(self.discover)
        except Exception:
            log.exception("discovery failed")
        self.build_zones()
        for zone in self.zones:
            await self.start_zone(zone)

        if self.mode == "off":
            log.info("control_mode=off — streams only, radios are never switched.")
        if self.mode == "slimproto":
            self.slim = SlimProtoServer(self.our_ip, self.port, buffer_kb=self.buffer_kb,
                                        on_connect=self.on_slim_connect,
                                        on_disconnect=self.on_slim_disconnect,
                                        on_state=self.on_slim_state)
            await self.slim.start()
            self.cli = LmsCliServer(self.slim, port=self.cli_port, username=self.cli_user,
                                    password=self.cli_pass, zone_names=self.zone_names,
                                    fallback_name=self.group_name,
                                    on_command=self.on_cli_command)
            await self.cli.start()
        try:
            while not stopping.is_set():
                await self.supervise_zones()
                if self.mode == "slimproto":
                    try:
                        await self.tick()
                    except Exception:
                        log.exception("route tick failed")
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self.stop_zones()


def main():
    try:
        with open(OPTIONS) as f:
            cfg = json.load(f)
    except OSError:
        cfg = {}
    try:
        asyncio.run(Controller(cfg).run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
