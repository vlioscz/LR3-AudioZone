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
import glob
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import discovery  # noqa: E402
import laradev  # noqa: E402
from lmscli import DEFAULT_CLI_PORT, LmsCliServer  # noqa: E402
from slimproto import SlimProtoServer  # noqa: E402

# Timestamps are not decoration: reconstructing a customer's frozen radio meant inferring the
# time of every controller line from the Liquidsoap lines around it, which is how a two-hour
# bracket became the best answer available.
logging.basicConfig(level=logging.INFO,
                    format="[lr3ctl] %(asctime)s %(levelname)s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("lr3.ctl")

OPTIONS = "/data/options.json"
STATE_DIR = "/tmp"
DATA_DIR = "/data"          # add-on persistent storage; survives restarts and updates
LIQ_TEMPLATE = "/etc/lr3/radio.liq.tpl"
GROUP_MOUNT = "all"        # the "every radio" zone
FALLBACK_MOUNT = "default"  # used only when no radio could be found at start-up
ACTIVE_EVENTS = {"playing", "started", "track_changed", "changed", "loading", "preloading"}
LIBRESPOT_LOG_BURST = 40      # most lines copied out of one librespot log in a single pass
LIBRESPOT_LOG_CHUNK = 256_000  # most bytes read from one log per pass, so a huge file cannot
                               # stall the tick loop (and with it the SlimProto heartbeat)
# Our own `strm-q` makes the LARA report `stop` back over the LMS CLI, seconds later — it polls
# on a 5 s cycle. That echo is not a button press. Ignore transport stops from a radio we have
# just stopped ourselves, for longer than the poll interval.
STOP_ECHO_GRACE = 6.0
# After an underrun the LARA stops playing but keeps the control connection, so `target` still
# says "playing" and nothing ever re-pushes. Re-push, but not more often than this.
REPUSH_COOLDOWN = 15.0


def opt(cfg, key, default):
    v = cfg.get(key, default)
    return default if v is None else v


# Container paths, so they are built with "/" rather than os.path.join — these strings also go
# onto the librespot command line, where a backslash would be nonsense.
def audio_cache_dir(mount: str) -> str:
    return f"{DATA_DIR}/librespot_{mount}"


def login_cache_dir(mount: str) -> str:
    return f"{DATA_DIR}/spotify_login/{mount}"


def credentials_file(mount: str) -> str:
    return f"{login_cache_dir(mount)}/credentials.json"


def legacy_credentials_file(mount: str) -> str:
    """Where the login lived up to 0.3.4, mixed in with the audio cache."""
    return f"{audio_cache_dir(mount)}/credentials.json"


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
        self.remote_access = bool(opt(cfg, "spotify_remote_access", False))
        self.park_on_off = bool(opt(cfg, "park_on_zone_off", False))
        # MB of Spotify audio kept on disk per zone; 0 = none. It used to be a hard-coded 1 GB
        # *each*, so a four-zone site could put 4 GB of streamed music on the soldered eMMC of
        # an HA Green — constant writes, for a cache that only pays off when the same track is
        # replayed soon afterwards.
        self.audio_cache_mb = max(0, int(opt(cfg, "audio_cache_mb", 200)))
        self.fallback_name = opt(cfg, "zone_name", "Audio zóna")
        self.group_name = opt(cfg, "group_name", "LARA All")
        self.name_prefix = bool(opt(cfg, "lara_name_prefix", True))
        self.idle_timeout = max(0, int(opt(cfg, "idle_timeout", 60)))
        self.volume = int(opt(cfg, "zone_volume", 90))
        # How much audio the LARA buffers before it starts = the dominant latency in the
        # chain. Expressed in seconds and converted to the KB the strm packet wants, so the
        # delay stays the same whatever the bitrate is.
        self.bitrate = max(32, int(opt(cfg, "bitrate", 192)))
        # 2.7 s = 64 KB at 192 kbps, the only threshold ever validated on hardware. 1.5 s
        # (36 KB) shipped from 0.2.1 to 0.3.5 and was never probed; the probe table in
        # CLAUDE.md records 20 KB underrunning within seconds.
        self.buffer_seconds = max(0.2, float(opt(cfg, "buffer_seconds", 2.7)))
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
        self._stopped_at: dict[str, float] = {}    # mac -> when WE last sent it a stop
        self._repushed_at: dict[str, float] = {}   # mac -> when we last recovered an underrun
        self.applied_volume: dict[str, int] = {}   # mac -> volume we last sent
        self.slim: SlimProtoServer | None = None
        self.cli: LmsCliServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._warned_offline: set[str] = set()
        self._log_offsets: dict[str, int] = {}     # mount -> bytes of its librespot log copied
        self.cred_cache_flag_ok = self.probe_cred_cache_flag()

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

    # --- Spotify availability ---------------------------------------------------
    def librespot_cache_args(self, mount: str) -> str:
        """The --cache group for one zone, per the spotify_remote_access switch.

        The login and the audio cache live in separate directories on purpose, in both modes,
        so the layout does not change when the switch is flipped and so releasing a login does
        not throw away up to 1 GB of cached audio per zone. Paths are quoted: they end up in a
        `sh -c` line, and an unquoted space would split one argument into two and make
        librespot exit on a usage error — a zone that is silent for ever.
        """
        args = [f'--system-cache "{login_cache_dir(mount)}"']
        if self.audio_cache_mb > 0:
            args += [f'--cache "{audio_cache_dir(mount)}"',
                     f"--cache-size-limit {self.audio_cache_mb}M"]
        if not self.remote_access and self.cred_cache_flag_ok:
            args.append("--disable-credential-cache")
        return " ".join(args)

    def purge_stored_logins(self) -> int:
        """Delete every stored Spotify login under /data, not just this boot's zones.

        Deliberately a glob rather than a loop over `self.zones`: the zone set is rebuilt from
        whatever discovery finds, so a radio that is unplugged (or renamed, or replaced) while
        the switch is flipped would keep its login on disk indefinitely — and a Spotify auth
        blob goes into every HA backup. `<mount>` here is whatever a previous version created.
        """
        found = (glob.glob(f"{DATA_DIR}/spotify_login/*/credentials.json")
                 + glob.glob(f"{DATA_DIR}/librespot_*/credentials.json"))
        gone = 0
        for path in found:
            try:
                os.remove(path)
                gone += 1
            except FileNotFoundError:
                pass
            except OSError:
                log.exception("could not delete the stored Spotify login %s", path)
        if gone:
            log.warning("deleted %d stored Spotify login(s) — remote access is off, so no "
                        "account stays logged in. Select each zone in the Spotify app again.",
                        gone)
        return gone

    def prepare_credentials(self, mount: str):
        """Release or migrate one zone's stored Spotify login before it starts.

        With remote access off the real protection is `--disable-credential-cache`, which in
        librespot 0.8.0 makes the credential path `None` outright — it neither writes nor
        reads one. Deleting the file is belt-and-braces: it keeps an auth blob for someone's
        account out of `/data` (and out of every HA backup), and it is the only protection
        left in the fallback where that flag turned out to be unavailable.
        """
        current, legacy = credentials_file(mount), legacy_credentials_file(mount)
        if not self.remote_access:
            for path in (current, legacy):
                try:
                    os.remove(path)
                    log.info("zone /%s: released the stored Spotify login", mount)
                except FileNotFoundError:
                    pass
                except OSError:
                    log.exception("zone /%s: could not delete %s", mount, path)
            return
        if not os.path.exists(legacy):
            return
        if os.path.exists(current):
            # Both layouts present: the new one wins, the ≤0.3.4 leftover would otherwise sit
            # in the audio cache for ever.
            try:
                os.remove(legacy)
            except OSError:
                log.exception("zone /%s: could not remove the superseded login %s",
                              mount, legacy)
            return
        try:
            os.makedirs(login_cache_dir(mount), exist_ok=True)
            os.replace(legacy, current)
            log.info("zone /%s: moved the stored Spotify login to %s", mount, current)
        except OSError:
            log.exception("zone /%s: could not move the stored Spotify login", mount)

    @staticmethod
    def probe_cred_cache_flag() -> bool:
        """Does this librespot know --disable-credential-cache? (0.4.0 and later do.)

        Fails **safe**, i.e. towards passing the flag: the Dockerfile pins librespot 0.8.0,
        which has it, so a probe that cannot run at all (librespot not yet on PATH, the
        timeout firing on a loaded box) says nothing about the binary. Answering "no" there
        would silently drop the one thing that stops an account being stored, while the UI
        still promised it — a privacy failure nobody would ever see. Answering "yes" wrongly
        would instead make librespot exit on an unknown flag, which is loud, immediate, and
        visible in the log now that librespot's stderr reaches it.
        """
        try:
            out = subprocess.run(["librespot", "--help"], capture_output=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            log.warning("could not run `librespot --help` to check for "
                        "--disable-credential-cache; assuming it is supported")
            return True
        return b"disable-credential-cache" in out.stdout + out.stderr

    # --- audio pipelines -------------------------------------------------------
    def render_liq(self, zone: Zone) -> str:
        with open(LIQ_TEMPLATE, encoding="utf-8") as f:
            tpl = f.read()
        for key, val in (("PORT", self.port), ("SOURCE_PASSWORD", self.source_password),
                         ("BITRATE", self.bitrate), ("SPOTIFY_BITRATE", self.spotify_bitrate),
                         ("MOUNT", zone.mount), ("ZONE_NAME", zone.name),
                         ("LIBRESPOT_CACHE_ARGS", self.librespot_cache_args(zone.mount))):
            tpl = tpl.replace(f"%%{key}%%", str(val))
        path = os.path.join(STATE_DIR, f"zone_{zone.mount}.liq")
        with open(path, "w", encoding="utf-8") as f:
            f.write(tpl)
        return path

    async def start_zone(self, zone: Zone):
        try:
            os.makedirs(audio_cache_dir(zone.mount), exist_ok=True)
            os.makedirs(login_cache_dir(zone.mount), exist_ok=True)
        except OSError:
            # A full or read-only /data must not take the whole controller down with it —
            # librespot copes without its caches, and the radios still get driven.
            log.exception("zone /%s: could not create its cache directories under %s",
                          zone.mount, DATA_DIR)
        self.prepare_credentials(zone.mount)
        open(os.path.join(STATE_DIR, f"librespot_{zone.mount}.log"), "a").close()
        path = self.render_liq(zone)
        try:
            self.procs[zone.mount] = await asyncio.create_subprocess_exec("liquidsoap", path)
            log.info("zone /%s started (Spotify device %r)", zone.mount, zone.name)
        except Exception:
            log.exception("could not start Liquidsoap for zone /%s", zone.mount)

    def pump_librespot_logs(self):
        """Copy librespot's own stderr into the add-on log.

        `radio.liq.tpl` sends it to /tmp/librespot_<mount>.log, and it has to: stdout carries
        the raw PCM, so nothing may be written there. But that file is invisible from the HA
        UI, and it is exactly where the answers are when Spotify discovery or the connection
        to Spotify's backend misbehaves — "Published zeroconf service", "Authenticated as",
        "Spirc shut down unexpectedly". Diagnosing that used to need shell access to the
        container, which nobody supporting an add-on remotely has.
        """
        for zone in self.zones:
            path = os.path.join(STATE_DIR, f"librespot_{zone.mount}.log")
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            off = self._log_offsets.get(zone.mount, 0)
            if size < off:          # the zone was restarted and the file replaced
                off = 0
            if size == off:
                continue
            try:
                with open(path, "rb") as f:
                    f.seek(off)
                    raw = f.read(LIBRESPOT_LOG_CHUNK)
            except OSError:
                continue
            # Stop at the last complete line and leave the rest for the next pass, so a line
            # written while we read is not split — and a multi-byte character not torn in half.
            cut = raw.rfind(b"\n") + 1
            if not cut:
                continue
            self._log_offsets[zone.mount] = off + cut
            chunk = raw[:cut].decode("utf-8", "replace")
            lines = [ln.strip() for ln in chunk.splitlines() if ln.strip()]
            if len(lines) > LIBRESPOT_LOG_BURST:
                dropped = len(lines) - LIBRESPOT_LOG_BURST
                lines = lines[-LIBRESPOT_LOG_BURST:]
                log.info("[librespot %s] (%d earlier lines skipped)", zone.mount, dropped)
            for line in lines:
                log.info("[librespot %s] %s", zone.mount, line)

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
        self._stopped_at.pop(player.mac, None)
        self._repushed_at.pop(player.mac, None)

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
            # Not necessarily a button press: it is also how the radio acknowledges the
            # `strm-q` we just sent it. `zone_off` filters that out by time, which also
            # covers the echo landing after the next tick already restarted the zone —
            # that used to kill music a second or two after the user resumed it.
            #
            # And a radio we are not driving has nothing to switch off. Measured on a
            # customer's log: 48 of 82 switch-offs fired on a radio that had never been
            # pushed, each one an unsolicited write over 61695 — and one of those was the
            # last thing the add-on ever sent to a unit that then froze. Whatever wedges
            # these radios, we have no business poking one we never turned on.
            if self.target.get(mac) is None:
                log.info("CLI stop from %s while its zone is off — recorded, nothing to do",
                         mac)
                return
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
        which stays lit showing a dead audio zone. A source switch over 61695
        (`select_source(RADIO)` + `stop`) leaves the radio prepared but not playing instead.

        That switch is **off by default** since 0.3.6 (`park_on_zone_off`). It is the one thing
        we do that no Slim server does — a write on the vendor's config port, into a unit that
        is in the middle of an audio-zone teardown — and it sits inside the death sequence of
        both radios that froze at a customer's site. Leaving the zone on the display is
        cosmetic; a frozen radio is somebody walking to a wall unit with a screwdriver.

        Idempotent on purpose. Our own `strm-q` makes the LARA report `stop` back on the LMS
        CLI, which lands in `on_cli_command` — so every switch-off used to run twice, parking
        the radio over 61695 twice.

        The bookkeeping happens **before the first await**: `park_on_radio` is two TCP round
        trips on :61695 and can take seconds, the CLI runs in its own task, and the echo lands
        squarely inside that window. Recording it afterwards would leave the guard shut exactly
        when it is needed.
        """
        now = time.monotonic()
        if now - self._stopped_at.get(key, -1e9) < STOP_ECHO_GRACE:
            log.debug("LARA %s was stopped %.1fs ago — ignoring the repeat (the radio is "
                      "echoing our own stop back over the CLI)",
                      key, now - self._stopped_at[key])
            return
        self._stopped_at[key] = now
        self.target[key] = None
        if self.slim:
            await self.slim.stop(key)
            await self.slim.set_power(key, False)
        dev = self.radios.get(key, {}).get("dev") if self.park_on_off else None
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

    async def recover_if_stalled(self, key: str, mount: str, now: float):
        """Push the stream again if the radio stopped playing but stayed connected.

        On an underrun the LARA reports `STMu` and stops, without dropping the control
        connection. Nothing else notices: `target` still names the mount, so `route()` returns
        early and never pushes again, and the radio stays silent for as long as it keeps the
        connection open — minutes, in the wild, until it finally reconnects. Since `tick()`
        treats `target == mount` as "playing", this is the one place that can tell it is not.
        """
        p = self.slim.players.get(key) if self.slim else None
        if p is None or p.mode != "stop":
            return
        if now - self._repushed_at.get(key, -1e9) < REPUSH_COOLDOWN:
            return
        self._repushed_at[key] = now
        log.warning("LARA %s stopped playing on its own (underrun?) while /%s is still "
                    "streaming — pushing it again", key, mount)
        self.target.pop(key, None)
        await self.route(key, mount)

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
                    # Restart the idle countdown on every playing tick. Without this the
                    # timestamp set by the first blip stays put — `route()` returns early
                    # once the radio is already on this mount, so it never reaches the
                    # `idle_since.pop()` there — and from idle_timeout seconds after that
                    # blip onwards, a SINGLE idle tick (the gap between two tracks) trips
                    # the timeout instantly. That is the zone switching off mid-album and
                    # coming straight back, over and over.
                    self.idle_since.pop(key, None)
                    await self.recover_if_stalled(key, mount, now)
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
        if self.remote_access:
            log.info("Spotify remote access is ON — the last account to select a zone stays "
                     "logged in and sees it from anywhere, not just on this network")
        else:
            log.info("Spotify remote access is OFF — zones are offered to everyone on this "
                     "network and to nobody outside it; no Spotify login is stored")
        if not self.remote_access:
            if not self.cred_cache_flag_ok:
                log.warning("this librespot does not know --disable-credential-cache, so it "
                            "may store a login while it runs; it is deleted at every start")
            self.purge_stored_logins()
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
                try:
                    self.pump_librespot_logs()
                except Exception:
                    log.exception("copying the librespot logs failed")
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
