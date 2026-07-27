#!/usr/bin/env python3
"""LR3 LARA controller — Spotify plays → LARA switches to the Slim audio zone; idle → LARA off.

There is no fallback radio any more. The Icecast mount carries Spotify (and silence when
Spotify is idle, so the mount never dies), and the LARA is only *in* the audio zone while
Spotify is actually playing:

    Spotify active  ->  strm-s  (+ aude on)   -> LARA switches to "Audio zóna" and plays
    Spotify idle    ->  strm-q  (+ aude off)  -> LARA goes dark after `idle_timeout`
                        (+ optional ELKO 61695 stop, see `lara_off_action`)

Two server sockets drive the LARA:
  * :3483  SlimProto — audio transport + power/volume  (`slimproto.py`)
  * :9595  LMS CLI   — the LARA's control/display channel; also carries its button presses
                       back to us  (`lmscli.py`)

control_mode:
  slimproto — the real thing (default).
  off       — discover + log radios only, never switch (safe for testing).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
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
MOUNT = "default"  # the single audio-zone mount every LARA plays
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


def spotify_active(mount: str = MOUNT) -> bool:
    try:
        with open(os.path.join(STATE_DIR, f"spotify_state_{mount}")) as f:
            return f.read().strip() in ACTIVE_EVENTS
    except OSError:
        return False


def spotify_volume(mount: str = MOUNT) -> int | None:
    """The Spotify app's volume slider, 0-100, or None if it has never been touched.

    librespot is run with `--volume-ctrl fixed` so the slider does NOT attenuate the audio
    (a hidden second volume control is how you end up with a silent zone nobody can explain).
    Instead the position lands here and we send it to the LARA as `audg` — the slider then
    controls the real thing. librespot reports 0-65535; older builds report 0-100.
    """
    try:
        with open(os.path.join(STATE_DIR, f"spotify_volume_{mount}")) as f:
            raw = int(float(f.read().strip()))
    except (OSError, ValueError):
        return None
    return max(0, min(100, round(raw / 65535 * 100) if raw > 100 else raw))


class Controller:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.mode = opt(cfg, "control_mode", "slimproto")
        self.user = opt(cfg, "lara_username", "admin")
        self.password = opt(cfg, "lara_password", "elkoep")
        self.hosts = opt(cfg, "lara_hosts", [])
        self.port = opt(cfg, "port", 8121)
        self.zone_name = opt(cfg, "zone_name", "Audio zóna")
        self.idle_timeout = max(0, int(opt(cfg, "idle_timeout", 20)))
        self.off_action = opt(cfg, "lara_off_action", "radio")
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
        self.target: dict[str, str | None] = {}    # mac -> mount currently pushed
        self.idle_since: dict[str, float] = {}     # mac -> when Spotify went idle
        self.applied_volume: dict[str, int] = {}   # mac -> volume we last sent
        self.slim: SlimProtoServer | None = None
        self.cli: LmsCliServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._warned_offline: set[str] = set()

    # --- inventory -------------------------------------------------------------
    def _add(self, key: str, rec: dict, how: str):
        if key in self.radios:
            return
        ip = rec.get("ip")
        self.radios[key] = {
            "rec": rec,
            "dev": laradev.LaraDevice(ip, self.user, self.password) if ip else None,
        }
        log.info("radio (%s): %-16s %-15s fw=%s %s", how, rec.get("name", "?"),
                 ip, rec.get("fw", "?"), key)

    def discover(self):
        for mac, rec in discovery.discover(timeout=2.0, retries=2).items():
            self._add(mac, rec, "discovery")
        for h in self.hosts:
            if any(r["rec"].get("ip") == h for r in self.radios.values()):
                continue
            self._add("ip:" + h, {"ip": h, "name": h, "mac": "ip:" + h}, "manual")

    # --- SlimProto / CLI callbacks ---------------------------------------------
    def on_slim_connect(self, player):
        """A LARA dialled in. It is a radio we can drive even if UDP discovery missed it."""
        self._add(player.mac, {"ip": player.ip, "name": player.name, "mac": player.mac,
                               "fw": "?"}, "slimproto")
        self._warned_offline.discard(player.mac)
        self.target.pop(player.mac, None)
        if spotify_active() and self._loop:
            self._loop.create_task(self.route(player.mac))

    def on_slim_disconnect(self, player):
        self.target.pop(player.mac, None)
        self.idle_since.pop(player.mac, None)

    def on_slim_state(self, player, what: str):
        if self.cli:
            self.cli.notify(player, what)

    async def on_cli_command(self, mac: str, verb: str):
        """The LARA pressed a button / LMS-style command arrived on :9595."""
        log.info("CLI command from %s: %s", mac, verb)
        if verb in ("play", "power_on"):
            await self.route(mac)
        elif verb in ("stop", "power_off"):
            await self.zone_off(mac)

    # --- actions ---------------------------------------------------------------
    def desired_volume(self) -> int | None:
        """Spotify's slider if it has ever been moved, else `zone_volume`. None = don't touch."""
        vol = spotify_volume()
        if vol is None:
            vol = self.volume
        return vol if vol > 0 else None

    async def apply_volume(self, key: str):
        vol = self.desired_volume()
        if vol is None or self.applied_volume.get(key) == vol:
            return
        await self.slim.set_volume(key, vol)
        self.applied_volume[key] = vol

    async def route(self, key: str):
        """Put a LARA into the audio zone and start it on our mount."""
        if self.target.get(key) == MOUNT:
            return
        if not self.slim:
            return
        ok = await self.slim.push_stream(key, MOUNT)
        if not ok:
            if key not in self._warned_offline:
                self._warned_offline.add(key)
                log.info("LARA %s is not connected to SlimProto (:3483) yet — check that its "
                         "'Audio zone function' is enabled and points at %s", key, self.our_ip)
            return
        self.applied_volume.pop(key, None)
        await self.apply_volume(key)
        self.idle_since.pop(key, None)
        self.target[key] = MOUNT
        rec = self.radios.get(key, {}).get("rec", {})
        log.info("zone ON  %s (%s) -> %s", rec.get("name", key), rec.get("ip", "?"),
                 self.slim.stream_url(MOUNT))

    async def zone_off(self, key: str):
        """Spotify is done — stop the stream and take the LARA back out of the zone.

        Dropping the SlimProto stream is not enough on its own: the LARA stays lit and keeps
        showing the (now dead) audio zone. `radio` therefore also sends it back to the station
        list over 61695, stopped — Spotify is always started from the phone, so nothing is lost
        by leaving the zone, and whoever walks up to the unit finds it where they expect.
        """
        if self.off_action == "none":
            self.target[key] = None
            self.idle_since.pop(key, None)
            return
        if self.slim:
            await self.slim.stop(key)
            await self.slim.set_power(key, False)
        if self.off_action in ("slim_elko", "radio"):
            dev = self.radios.get(key, {}).get("dev")
            if dev:
                action = dev.park_on_radio if self.off_action == "radio" else dev.stop
                try:
                    if not await asyncio.to_thread(action):
                        log.warning("ELKO %s did not take on %s — check lara_username/"
                                    "lara_password (port 61695 needs them)",
                                    self.off_action, key)
                except Exception:
                    log.exception("ELKO %s failed for %s", self.off_action, key)
        self.target[key] = None
        self.idle_since.pop(key, None)
        rec = self.radios.get(key, {}).get("rec", {})
        log.info("zone OFF %s (%s)", rec.get("name", key), rec.get("ip", "?"))

    async def tick(self):
        active = spotify_active()
        now = time.monotonic()
        for key in list(self.radios.keys()):
            if active:
                await self.route(key)
                if self.target.get(key):
                    await self.apply_volume(key)   # follow the Spotify slider while playing
            elif self.target.get(key):
                started = self.idle_since.setdefault(key, now)
                if now - started >= self.idle_timeout:
                    await self.zone_off(key)

    # --- main loop -------------------------------------------------------------
    async def run(self):
        self._loop = asyncio.get_running_loop()
        log.info("controller mode=%s our_ip=%s mount=/%s idle_timeout=%ds off=%s "
                 "buffer=%.1fs (%d KB @ %d kbps)", self.mode, self.our_ip, MOUNT,
                 self.idle_timeout, self.off_action, self.buffer_seconds, self.buffer_kb,
                 self.bitrate)
        if self.mode == "off":
            log.info("control_mode=off — inventory only, no switching. Set control_mode to "
                     "'slimproto' to actually drive the radios.")
        if self.mode == "slimproto":
            self.slim = SlimProtoServer(self.our_ip, self.port, buffer_kb=self.buffer_kb,
                                        on_connect=self.on_slim_connect,
                                        on_disconnect=self.on_slim_disconnect,
                                        on_state=self.on_slim_state)
            await self.slim.start()
            self.cli = LmsCliServer(self.slim, port=self.cli_port, username=self.cli_user,
                                    password=self.cli_pass, zone_name=self.zone_name,
                                    mount=MOUNT, on_command=self.on_cli_command)
            await self.cli.start()
        last_discover = 0.0
        while True:
            if time.monotonic() - last_discover > 60:
                try:
                    await asyncio.to_thread(self.discover)
                except Exception:
                    log.exception("discovery failed")
                last_discover = time.monotonic()
            if self.mode == "slimproto":
                try:
                    await self.tick()
                except Exception:
                    log.exception("route tick failed")
            await asyncio.sleep(1.0)


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
