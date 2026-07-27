#!/usr/bin/env python3
"""LR3 LARA controller — discover LARAs and switch them to our streams on Spotify-active.

control_mode:
  off       — discover + log radios only, never switch (safe default until validated on device).
  preset    — path A: LARA plays our Icecast mount as a stored radio preset (select_station).
  slimproto — path B2: push the Icecast URL to LARA via a SlimProto server on :3483.

Everything below the mount (Icecast/Liquidsoap/librespot) is unchanged; the controller only
aims radios at mounts, driven by the per-mount Spotify-active flag that librespot --onevent writes.
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
from slimproto import SlimProtoServer  # noqa: E402

logging.basicConfig(level=logging.INFO, format="[lr3ctl] %(levelname)s %(message)s")
log = logging.getLogger("lr3.ctl")

OPTIONS = "/data/options.json"
STATE_DIR = "/tmp"
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


class Controller:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.mode = opt(cfg, "control_mode", "off")
        self.user = opt(cfg, "lara_username", "admin")
        self.password = opt(cfg, "lara_password", "elkoep")
        self.hosts = opt(cfg, "lara_hosts", [])
        self.fallback_enabled = opt(cfg, "fallback_enabled", True)
        self.port = opt(cfg, "port", 8121)
        zones = opt(cfg, "zones", [])
        self.mounts = ["default"] + [z["mount"] for z in zones if z.get("mount")]
        # mount -> preset name on the radio (defaults to the mount name)
        self.preset_name = {z["mount"]: z.get("preset", z["mount"]) for z in zones if z.get("mount")}
        self.preset_name.setdefault("default", opt(cfg, "default_preset", "default"))
        # mount -> radio identifiers (mac/ip/name); 'default' => every radio
        self.zone_map = {z["mount"]: z["radios"] for z in zones if z.get("mount") and z.get("radios")}
        self.our_ip = host_ip()
        self.radios: dict[str, dict] = {}       # key -> {rec, dev}
        self.radio_target: dict[str, str | None] = {}
        self.slim: SlimProtoServer | None = None

    # --- inventory -------------------------------------------------------------
    def discover(self):
        for mac, rec in discovery.discover(timeout=2.0, retries=2).items():
            if mac not in self.radios:
                self.radios[mac] = {"rec": rec, "dev": laradev.LaraDevice(rec["ip"], self.user, self.password)}
                log.info("radio: %-16s %-15s fw=%s %s", rec["name"], rec["ip"], rec["fw"], mac)
        for h in self.hosts:
            if any(r["rec"].get("ip") == h for r in self.radios.values()):
                continue
            key = "ip:" + h
            if key not in self.radios:
                self.radios[key] = {"rec": {"ip": h, "name": h, "mac": key},
                                    "dev": laradev.LaraDevice(h, self.user, self.password)}
                log.info("radio (manual): %s", h)

    def radios_for_mount(self, mount: str) -> list[str]:
        if mount == "default":
            return list(self.radios.keys())
        ids = self.zone_map.get(mount, [])
        return [k for k, r in self.radios.items()
                if k in ids or r["rec"].get("ip") in ids or r["rec"].get("name") in ids]

    # --- actions ---------------------------------------------------------------
    async def route(self, key: str, mount: str):
        if self.radio_target.get(key) == mount:
            return
        r = self.radios[key]
        log.info("route %s (%s) -> /%s", r["rec"]["name"], r["rec"].get("ip"), mount)
        if self.mode == "slimproto" and self.slim:
            ok = await self.slim.push_stream(r["rec"].get("mac", key), mount)
            if not ok:
                log.info("  (LARA %s not connected to SlimProto yet)", key)
                return
        elif self.mode == "preset":
            ok = await asyncio.to_thread(r["dev"].play_preset, self.preset_name.get(mount, mount))
            if not ok:
                log.warning("  preset '%s' not found on %s", self.preset_name.get(mount, mount), r["rec"].get("ip"))
                return
        self.radio_target[key] = mount

    async def stop_radio(self, key: str):
        r = self.radios[key]
        if self.mode == "slimproto" and self.slim:
            await self.slim.stop(r["rec"].get("mac", key))
        elif self.mode == "preset":
            await asyncio.to_thread(r["dev"].stop)
        self.radio_target[key] = None

    async def tick(self):
        active = [m for m in self.mounts if spotify_active(m)]
        default_active = "default" in active
        for key in list(self.radios.keys()):
            zone_active = None
            for m in active:
                if m != "default" and key in self.radios_for_mount(m):
                    zone_active = m  # last assigned-zone wins
            if zone_active:
                await self.route(key, zone_active)
            elif default_active:
                await self.route(key, "default")
            elif not self.fallback_enabled and self.radio_target.get(key):
                await self.stop_radio(key)

    # --- main loop -------------------------------------------------------------
    async def run(self):
        log.info("controller mode=%s our_ip=%s mounts=%s", self.mode, self.our_ip, self.mounts)
        if self.mode == "off":
            log.info("control_mode=off — inventory only, no switching. Set control_mode to "
                     "'preset' or 'slimproto' once a LARA is present.")
        if self.mode == "slimproto":
            self.slim = SlimProtoServer(self.our_ip, self.port)
            await self.slim.start()
        last_discover = 0.0
        while True:
            if time.monotonic() - last_discover > 60:
                try:
                    await asyncio.to_thread(self.discover)
                except Exception:
                    log.exception("discovery failed")
                last_discover = time.monotonic()
            if self.mode in ("preset", "slimproto"):
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
