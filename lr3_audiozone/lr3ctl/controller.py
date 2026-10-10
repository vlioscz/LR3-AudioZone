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
import base64
import glob
import json
import logging
import os
import signal
import socket
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
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
# A zone change has to hold for this long before we act on it. Moving a Spotify session from a
# radio's own device to "LARA All" leaves both librespots reporting "playing" for a moment, and
# the specific-beats-group rule then picks the individual zone on one tick and the group on the
# next. Harmless until 0.4.3; now that a mount change terminates the old stream properly, each
# bounce costs a real re-buffer, and the customer feels it as "switching took ages".
ZONE_SETTLE = 3.0
# `zone_off` ends with `strm-q`, which makes the LARA **flush** whatever it has buffered and
# not yet played (it answers STMf). The radio is several seconds behind the app — its own
# buffer alone holds ~5 s at 192 kbps, and the backlog in Icecast grows on top of that; ~30 s
# was measured after an hour of listening. So an idle timeout shorter than that lag throws
# away the tail of whatever was playing, which is heard as songs being cut off before they
# end. Warn below this; it is not a hard limit because a short timeout is useful for testing.
IDLE_TIMEOUT_FLOOR = 45
# How often to ask Icecast how far behind the radios are. Same cadence as the progress lines.
BACKLOG_EVERY = 300.0
# Monotonic vs wall clock disagreeing by more than this over one poll is worth a line.
CLOCK_SKEW_NOTE = 300.0
# A connected radio answers our 5 s heartbeat with a STAT. If its STAT counter stops moving
# while the session is still open we are blind to it: no buffer figures, `mode` frozen, and
# `recover_if_stalled` disabled, because that waits for a STAT after the stall. Say so.
STAT_SILENCE = 120.0
# When a radio drops its SlimProto session we go and ask whether the unit is still alive at
# all. Three TCP handshakes per port, spread over two minutes, then we stop — a wedged radio
# costs nothing to probe and a healthy one must not be hammered.
LIVENESS_PROBES = (5.0, 30.0, 120.0)   # seconds after the drop
WEB_PORT = 80
ELKO_PORT = 61695
# How often to repeat the warning that control_mode=off is swallowing playback.
MUZZLE_NAG_EVERY = 600.0

# --- rate_match: sending at the radio's pace instead of the CPU clock's (pacer.py) ------------
PACER = "/opt/lr3ctl/pacer.py"
RATE_MATCH_AUTO = True    # what `rate_match: auto` means in this release
# Where a newly seen mount starts, by output sample rate, before the buffer readings take over.
# Positive = send faster. 48 kHz is measured directly: with the buffer held steady, Icecast
# counted 23 885 B/s leaving for the radios per wall-clock second, three hours running
# (2026-10-10, 0.5.1, 28 readings) — -4800 ppm. The earlier -3250 was relative to Liquidsoap,
# which on that box evidently did not deliver exactly 24 000 B/s either. 44.1 kHz has only
# ever been measured against Liquidsoap, so it starts from nothing and is learned.
DEFAULT_PPM = {48000: -4800.0, 44100: 0.0}
# Bumped when saved rates stop meaning what they meant: 0.5.0/0.5.1 learned theirs through a
# pacer that lost ~0.5 % on every rate change, so those numbers are ~5000 ppm too high.
RATE_FILE_VERSION = 2
RATE_EVERY = 5.0          # one steering decision per heartbeat, which is how often STATs come
RATE_FRESH = 15.0         # a buffer reading older than this is not used
# Aim the radio's own buffer this far below full. A full buffer is the one state we cannot
# read: the radio stops pulling, and every further byte of surplus goes into Icecast unseen.
RATE_HEADROOM = 20480
# Gains. In ppm per byte of error: with the buffer 4 KB off target we lean 1000 ppm, which at
# 24 kB/s moves it ~24 B/s — a time constant of about three minutes, slow enough that the
# few-KB jitter in single readings does nothing audible and fast enough to settle a start.
RATE_KP = 0.25
RATE_TI = 1800.0          # the learned base rate follows the error on a half-hour scale
# A hard bound either side of zero. The measured offsets are -3250 and +1640 ppm; anything
# far past them means the readings are not what we think, and then the damage stays small.
RATE_LIMIT = 8000.0
RATE_SAVE_EVERY = 600.0

# --- Home Assistant sensors (one per radio: which Spotify device it is playing) -------------
HA_API = "http://supervisor/core/api"
HA_REFRESH = 300.0        # re-post everything this often: HA forgets API-set states on restart
HA_RETRY = 60.0


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


S6_ENV_DIRS = ("/run/s6/container_environment", "/var/run/s6/container_environment")


def supervisor_token() -> str | None:
    """The token the Supervisor gives every add-on for its API.

    It is set in the container's environment — but this image boots through s6-overlay (the HA
    base image's /init), which starts our CMD with a scrubbed environment and keeps the
    container's variables as files under /run/s6/container_environment instead. That is what
    `#!/usr/bin/with-contenv` in other add-ons' scripts is for. 0.5.0 looked only at the
    environment, found nothing, and the first site to try it got no sensors at all.
    """
    for name in ("SUPERVISOR_TOKEN", "HASSIO_TOKEN"):
        if os.environ.get(name):
            return os.environ[name]
        for d in S6_ENV_DIRS:
            try:
                with open(os.path.join(d, name)) as f:
                    tok = f.read().strip()
            except OSError:
                continue
            if tok:
                return tok
    return None


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
        # 48000 by default: at 44.1 kHz every LARA measured so far drains its input buffer at a
        # steady ~39 B/s and underruns every 26 minutes. See radio.liq.tpl for the evidence.
        self.samplerate = int(opt(cfg, "samplerate", 48000))
        self.park_on_off = bool(opt(cfg, "park_on_zone_off", False))
        # MB of Spotify audio kept on disk per zone; 0 = none. It used to be a hard-coded 1 GB
        # *each*, so a four-zone site could put 4 GB of streamed music on the soldered eMMC of
        # an HA Green — constant writes, for a cache that only pays off when the same track is
        # replayed soon afterwards.
        self.audio_cache_mb = max(0, int(opt(cfg, "audio_cache_mb", 200)))
        self.fallback_name = opt(cfg, "zone_name", "Audio zóna")
        self.group_name = opt(cfg, "group_name", "LARA All")
        self.custom_groups = opt(cfg, "groups", []) or []
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
        # auto = whatever this release recommends, so that a later release can change the
        # recommendation; HA keeps a saved true/false for ever, which is why this is not a bool.
        rm = str(opt(cfg, "rate_match", "auto")).lower()
        self.rate_match = rm == "on" or (rm == "auto" and RATE_MATCH_AUTO)
        self.rate_base: dict[str, float] = {}      # mount -> learned rate, ppm
        self.rate_ppm: dict[str, float] = {}       # mount -> rate in force, ppm
        self._rate_written: dict[str, float] = {}  # mount -> last value written for its pacer
        self._rate_x: dict[str, float] = {}        # mount -> smoothed buffer reading, bytes
        self._rate_at = 0.0
        self._rate_saved_at = 0.0
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
        self._repushed_at: dict[str, float] = {}   # mac -> when we last pushed it a stream
        self._stall_seq: dict[str, int] = {}       # mac -> its STAT count when it stalled
        self._zone_pending: dict[str, tuple] = {}  # mac -> (mount it wants, since when)
        self._muzzle_warned_at = -1e9              # last control_mode=off nag
        self.applied_volume: dict[str, int] = {}   # mac -> volume we last sent
        self.slim: SlimProtoServer | None = None
        self.cli: LmsCliServer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._warned_offline: set[str] = set()
        self._log_offsets: dict[str, int] = {}     # mount -> bytes of its librespot log copied
        self._backlog_at = 0.0                     # last Icecast stats poll
        self._backlog_wall = 0.0                   # the same, by the wall clock
        self._backlog_prev: dict[str, tuple] = {}  # mount -> (read, sent, listeners)
        self._mount_pushed_at: dict[str, float] = {}  # mount -> when a radio last joined it
        self._probes: set[asyncio.Task] = set()    # liveness probes in flight
        self._backlog_warned = False
        self._stat_seen: dict[str, tuple] = {}     # mac -> (stat_seq, when it last moved)
        self._blind: set[str] = set()              # macs we have already complained about
        self._stopping = False
        # Home Assistant sensors. The Supervisor hands every add-on a token; with
        # `homeassistant_api: true` in config.yaml it also opens the Core API to it.
        self.ha_token = supervisor_token()
        self._ha_sent: dict[str, tuple] = {}       # entity -> (state, attrs) HA last accepted
        self._ha_refresh_at = 0.0
        self._ha_retry_at = 0.0
        self._ha_failing = False
        self._ha_task: asyncio.Task | None = None
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
            zones.extend(self.custom_group_zones(used))
            if len(macs) > 1:
                zones.append(Zone(GROUP_MOUNT, self.group_name, None))
        self.zones = zones
        self.zone_names = {z.mount: z.name for z in zones}
        for z in zones:
            # Name every member. This used to print only the first radio's MAC, so a two-room
            # group read as a single radio — which is how it looked in a colleague's log while
            # he was trying to work out why his groups behaved as they did.
            if z.is_group:
                who = "všechna rádia"
            elif len(z.radios) == 1:
                who = z.radios[0]
            else:
                who = ", ".join(self.display_name(self.radios[m]["rec"], m) for m in z.radios)
            log.info("zone /%s  ->  Spotify device %r  (%s)", z.mount, z.name, who)

    def custom_group_zones(self, by_name: dict[str, str]) -> list[Zone]:
        """Extra Spotify devices for hand-picked sets of radios (`groups`).

        "LARA All" is all-or-nothing, and a house wants "just the inside ones". Members are
        named the way the user sees them in the Spotify app, which is the only name they have
        ever been shown; a MAC is accepted too, for radios that have no name of their own.
        Matching ignores case and the "LARA " prefix, because nobody will type it consistently.

        These sit **between** the per-radio zones and "LARA All" in `self.zones`, so the
        precedence reads the way people expect: your own room beats a small group, and a small
        group beats everything-at-once.
        """
        def key(s: str) -> str:
            s = (s or "").strip().lower()
            return s[5:].strip() if s.startswith("lara ") else s

        lookup = {key(n): m for n, m in by_name.items()}
        lookup.update({m.lower(): m for m in self.radios})
        out: list[Zone] = []
        for i, g in enumerate(self.custom_groups):
            name = (g.get("name") or "").strip() if isinstance(g, dict) else ""
            members = g.get("radios") or [] if isinstance(g, dict) else []
            if not name:
                log.warning("skipping group #%d: it has no name", i + 1)
                continue
            macs, missing = [], []
            for want in members:
                mac = lookup.get(key(want))
                (macs.append(mac) if mac and mac not in macs else
                 None if mac else missing.append(want))
            if missing:
                log.warning("group %r: no radio here is called %s — check the spelling against "
                            "the names in the log above", name, ", ".join(repr(m) for m in missing))
            if len(macs) < 2:
                log.warning("group %r needs at least two radios that exist; skipping it", name)
                continue
            if len(self.radios) > 1 and set(macs) == set(self.radios):
                log.warning("group %r covers every radio, which is what %r already does — "
                            "skipping it; a group is for picking *some* of the rooms",
                            name, self.group_name)
                continue
            out.append(Zone(f"grp{i + 1}", name, macs))
        return out

    def zone_for(self, mac: str, active: set[str]) -> str | None:
        """Which mount this radio should play. A radio's own device beats the group device."""
        for z in self.zones:            # specific zones come first, group last
            if z.mount in active and z.covers(mac):
                return z.mount
        return None

    def settled_zone_for(self, mac: str, active: set[str], now: float) -> str | None:
        """`zone_for`, but a change has to persist for ZONE_SETTLE before it counts.

        Without this the controller chases the gap between two librespots: handing a session
        from a radio's own device to the group lights both of them up for a second, so the
        choice flips to the individual zone and straight back. Observed five times in one
        afternoon, always a pair one second apart.

        Staying put is never delayed — only a *change* has to hold still. The radio keeps
        playing whatever it is already playing while the wobble settles.
        """
        want = self.zone_for(mac, active)
        current = self.target.get(mac)
        if want == current or want is None or current is None:
            # Only a hand-over between two live zones can wobble. Starting from silence must
            # not be delayed, and "nothing is playing any more" must reach the caller at once
            # or the idle countdown never runs and the zone never switches off at all.
            self._zone_pending.pop(mac, None)
            return want
        pending = self._zone_pending.get(mac)
        if pending is None or pending[0] != want:
            self._zone_pending[mac] = (want, now)
            return current
        if now - pending[1] < ZONE_SETTLE:
            return current
        self._zone_pending.pop(mac, None)
        return want

    # --- Spotify availability ---------------------------------------------------
    def librespot_cache_args(self, mount: str) -> str:
        """The --cache group for one zone, per the spotify_remote_access switch.

        The login and the audio cache live in separate directories on purpose, in both modes,
        so the layout does not change when the switch is flipped and so releasing a login does
        not throw away up to 1 GB of cached audio per zone. Paths are quoted: they end up in a
        `sh -c` line, and an unquoted space would split one argument into two and make
        librespot exit on a usage error — a zone that is silent for ever.
        """
        argv = self.librespot_cache_argv(mount)
        return " ".join(f'"{a}"' if i and argv[i - 1] in ("--system-cache", "--cache") else a
                        for i, a in enumerate(argv))

    def librespot_cache_argv(self, mount: str) -> list[str]:
        """The same flags as a list, for the pacer, which starts librespot without a shell."""
        args = ["--system-cache", login_cache_dir(mount)]
        if self.audio_cache_mb > 0:
            args += ["--cache", audio_cache_dir(mount),
                     "--cache-size-limit", f"{self.audio_cache_mb}M"]
        if not self.remote_access and self.cred_cache_flag_ok:
            args.append("--disable-credential-cache")
        return args

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
                         ("SAMPLERATE", self.samplerate),
                         ("INITIAL_VOLUME", self.initial_volume()),
                         ("LIBRESPOT_CACHE_ARGS", self.librespot_cache_args(zone.mount))):
            tpl = tpl.replace(f"%%{key}%%", str(val))
        path = os.path.join(STATE_DIR, f"zone_{zone.mount}.liq")
        with open(path, "w", encoding="utf-8") as f:
            f.write(tpl)
        return path

    def librespot_argv(self, zone: Zone) -> list[str]:
        """radio.liq.tpl's librespot line, as a list: a zone name is a name, never shell."""
        return (["librespot", "--name", zone.name, "--device-type", "speaker",
                 "--backend", "pipe", "--format", "S16",
                 "--bitrate", str(self.spotify_bitrate),
                 "--initial-volume", str(self.initial_volume())]
                + self.librespot_cache_argv(zone.mount)
                + ["--enable-volume-normalisation", "--onevent", "/etc/lr3/spotify_event.sh"])

    def write_pacer_config(self, zone: Zone) -> str:
        ppm = self.rate_ppm.setdefault(
            zone.mount, self.rate_base.setdefault(zone.mount, self.default_ppm()))
        self.write_rate(zone.mount, ppm, force=True)
        cfg = {"mount": zone.mount, "name": zone.name, "port": self.port,
               "source_password": self.source_password, "bitrate": self.bitrate,
               "samplerate": self.samplerate, "librespot": self.librespot_argv(zone),
               "librespot_log": os.path.join(STATE_DIR, f"librespot_{zone.mount}.log"),
               "rate_file": self.rate_path(zone.mount), "initial_ppm": ppm}
        path = os.path.join(STATE_DIR, f"pacer_{zone.mount}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        return path

    # --- rate_match: keep each radio's buffer just below full -----------------------
    def default_ppm(self) -> float:
        return DEFAULT_PPM.get(self.samplerate, 0.0)

    @staticmethod
    def rate_path(mount: str) -> str:
        return os.path.join(STATE_DIR, f"lr3_rate_{mount}")

    def write_rate(self, mount: str, ppm: float, force: bool = False):
        if not force and abs(ppm - self._rate_written.get(mount, 1e9)) < 2.0:
            return
        path = self.rate_path(mount)
        try:
            with open(path + ".tmp", "w") as f:
                f.write(f"{ppm:.1f}\n")
            os.replace(path + ".tmp", path)
            self._rate_written[mount] = ppm
        except OSError:
            log.exception("could not write the rate for /%s", mount)

    def rate_target(self, size: int) -> float:
        """Where to hold a radio's buffer: comfortably full, never quite full.

        The start threshold is a floor so we never steer a radio below where it began; in
        practice it does not bind, because this firmware starts at ~60 KB whatever it is told.
        """
        target = max(size - RATE_HEADROOM, self.buffer_kb * 1024 + 8192)
        return min(target, size - 4096)

    def load_rates(self):
        """Start from what this site's radios taught us last time, not from the default.

        Only when the output sample rate is unchanged — the radios' offset depends on the rate
        they are fed — and only from a file written by a pacer that counts the same way.
        """
        try:
            with open(os.path.join(DATA_DIR, "rate_ppm.json")) as f:
                saved = json.load(f)
        except (OSError, ValueError):
            return
        if (saved.get("v") != RATE_FILE_VERSION or saved.get("samplerate") != self.samplerate
                or saved.get("bitrate") != self.bitrate):
            return
        for mount, ppm in (saved.get("ppm") or {}).items():
            try:
                self.rate_base[mount] = max(-RATE_LIMIT, min(RATE_LIMIT, float(ppm)))
            except (TypeError, ValueError):
                pass
        if self.rate_base:
            log.info("rate_match: starting from the rates learned last time — %s",
                     ", ".join(f"/{m} {p:+.0f} ppm" for m, p in sorted(self.rate_base.items())))

    def save_rates(self):
        if not self.rate_base:
            return
        try:
            path = os.path.join(DATA_DIR, "rate_ppm.json")
            with open(path + ".tmp", "w") as f:
                json.dump({"v": RATE_FILE_VERSION, "samplerate": self.samplerate,
                           "bitrate": self.bitrate,
                           "ppm": {m: round(p, 1) for m, p in self.rate_base.items()}}, f)
            os.replace(path + ".tmp", path)
        except OSError:
            log.exception("could not save the learned rates")

    def update_rates(self, now: float):
        """Steer each zone's pacer so its radios neither starve nor overflow.

        The LARA reports its input-buffer level in every STAT, every 5 s. If we send faster than
        it plays, the level climbs; slower, it sinks. So hold it at `rate_target` with a slow PI
        loop: the proportional part answers the reading, the integral part (`rate_base`) learns
        the radio's true offset and is saved, so the next start begins where this one ended.

        On a shared mount (a group) the hungriest radio decides: running one dry is a dropout,
        whereas a radio that plays a few ppm slower than its partners just fills up and sheds
        the excess through Icecast's queue, as every radio did before this existed.

        Nothing here sends anything to a radio. It only changes how fast we read Spotify.
        """
        if not self.rate_match or not self.slim or now - self._rate_at < RATE_EVERY:
            return
        dt = min(now - self._rate_at, 3 * RATE_EVERY) if self._rate_at else RATE_EVERY
        self._rate_at = now
        for zone in self.zones:
            m = zone.mount
            readings = []
            for mac, mount in self.target.items():
                p = self.slim.players.get(mac) if mount == m else None
                if (p is None or p.mode != "play" or p._stall_logged or not p.buf_size
                        or now - p.in_buf_at > RATE_FRESH):
                    continue
                readings.append((p.in_buf, p.buf_size))
            if not readings:
                self._rate_x.pop(m, None)     # nothing to steer by: hold the rate as it is
                continue
            raw = min(r[0] for r in readings)
            size = min(r[1] for r in readings)
            x = self._rate_x.get(m)
            x = raw if x is None else x + 0.3 * (raw - x)
            self._rate_x[m] = x
            e = x - self.rate_target(size)
            base = self.rate_base.get(m, self.default_ppm()) - RATE_KP / RATE_TI * e * dt
            base = max(-RATE_LIMIT, min(RATE_LIMIT, base))
            ppm = max(-RATE_LIMIT, min(RATE_LIMIT, base - RATE_KP * e))
            self.rate_base[m] = base
            self.rate_ppm[m] = ppm
            self.write_rate(m, ppm)
        if now - self._rate_saved_at >= RATE_SAVE_EVERY:
            self._rate_saved_at = now
            self.save_rates()

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
        if self.rate_match:
            path = self.write_pacer_config(zone)
            what, argv = "the pacer", ("python3", PACER, path)
        else:
            what, argv = "Liquidsoap", ("liquidsoap", self.render_liq(zone))
        try:
            self.procs[zone.mount] = await asyncio.create_subprocess_exec(*argv)
            log.info("zone /%s started (Spotify device %r)", zone.mount, zone.name)
        except Exception:
            log.exception("could not start %s for zone /%s", what, zone.mount)

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

    def nag_if_muzzled(self):
        """Say so when Spotify is playing to a zone that control_mode=off cannot act on.

        This mode is a diagnostic escape hatch, and the failure it produces when left on by
        accident is silent and baffling: the phone hands playback over, the Connect device
        behaves perfectly, and the radio simply never joins in. It cost a customer two days.
        One line at start-up was not enough, so say it again whenever it actually bites.
        """
        active = [z.name for z in self.zones if spotify_active(z.mount)]
        if not active:
            return
        now = time.monotonic()
        if now - self._muzzle_warned_at < MUZZLE_NAG_EVERY:
            return
        self._muzzle_warned_at = now
        log.warning("Spotify is playing to %s, but control_mode=off — no radio is being "
                    "switched. Set 'Ovládání LARA' back to 'slimproto' and restart the add-on.",
                    ", ".join(active))

    def measure_backlog(self):
        """Measure the one part of the lag we have only ever guessed at.

        The listener is behind the app by: Liquidsoap's 0.4 s, whatever Icecast is holding for
        that listener, and the radio's own buffer (~5.4 s at 192 kbps now that it stays full).
        The middle term is invisible from both ends and is the one that grows — a colleague
        reported ~30 s after an hour of listening, cleared by stopping and starting.

        Icecast's admin stats give, per mount, the bytes read from the source and the bytes
        sent to listeners. Their absolute difference is meaningless (the listener joined after
        the source did), but the **change** in that difference over time is exactly the rate
        at which the backlog is growing. That is the number that decides whether this is a
        real surplus to be engineered away or a one-off fill that settles.
        """
        now = time.monotonic()
        if now - self._backlog_at < BACKLOG_EVERY:
            return
        was, self._backlog_at = self._backlog_at, now
        wall = time.time()
        was_wall, self._backlog_wall = self._backlog_wall, wall
        dt_wall = wall - was_wall if was_wall else 0.0
        # The pacer keeps time by the monotonic clock; Liquidsoap kept it by the wall clock,
        # which NTP corrects. They should agree to a few ppm, and at the first site they did —
        # this line is what ruled the clocks out when the pacer settled ~5000 ppm off (that
        # turned out to be the pacer losing a tick per rate change, fixed in 0.5.2). Kept,
        # because a box whose clocks do disagree would mislead every rate we learn.
        if dt_wall > 0 and was:
            skew = ((now - was) / dt_wall - 1.0) * 1e6
            if abs(skew) > CLOCK_SKEW_NOTE:
                log.info("this machine's monotonic clock ran %+.0f ppm against its wall clock "
                         "over the last %.0f s", skew, dt_wall)
        try:
            req = urllib.request.Request(
                f"http://localhost:{self.port}/admin/stats.xml",
                headers={"Authorization": "Basic " + base64.b64encode(
                    f"admin:{self.source_password}".encode()).decode()})
            with urllib.request.urlopen(req, timeout=5) as r:
                root = ET.fromstring(r.read())
        except Exception as e:
            if not self._backlog_warned:
                self._backlog_warned = True
                log.warning("cannot read Icecast stats at http://localhost:%d/admin/stats.xml "
                            "(%s) — the backlog measurement will stay silent", self.port, e)
            return
        dt = now - was
        for src in root.findall("source"):
            mount = (src.get("mount") or "").lstrip("/")
            if mount not in self.zone_names:
                continue
            def num(tag):
                el = src.find(tag)
                try:
                    return int(el.text)
                except (AttributeError, TypeError, ValueError):
                    return None
            read, sent, lis = num("total_bytes_read"), num("total_bytes_sent"), num("listeners")
            if read is None or sent is None or not lis:
                # Nobody listening: forget the old sample. Keeping it made the first reading
                # of the next session compare against the last one of the previous evening —
                # a whole night of source bytes and none sent, logged as +2.6 MB/s.
                self._backlog_prev.pop(mount, None)
                continue
            prev = self._backlog_prev.get(mount)
            self._backlog_prev[mount] = (read, sent, lis)
            # Not like for like if the listener count changed, if a radio (re)joined this mount
            # since the last poll — same count, different connections — or if Icecast's
            # counters went backwards because the source reconnected.
            if (not prev or prev[2] != lis or dt <= 0 or read < prev[0] or sent < prev[1]
                    or self._mount_pushed_at.get(mount, -1e9) > was):
                continue
            grew = (read - prev[0]) - (sent - prev[1]) / lis
            per_s = grew / dt
            # With rate_match on, this line is the proof it works: the backlog should read ~0
            # for hours, with the radio's buffer parked just under full.
            steer = ""
            if self.rate_match and mount in self.rate_ppm:
                x = self._rate_x.get(mount)
                steer = (f"; pacing {self.rate_ppm[mount]:+.0f} ppm" +
                         (f", radio buffer {x / 1024:.0f} KB" if x is not None else ""))
            if dt_wall > 0:
                # What actually left for the radios, per second of wall-clock time: with the
                # buffer steady, this IS the radios' playback rate, whatever any clock says.
                steer += f"; source {(read - prev[0]) / dt_wall:.0f} B/s"
            log.info("mount /%s: Icecast backlog %+.0f B/s (%+.1f s of audio per hour, "
                     "%d listener(s))%s", mount, per_s,
                     per_s * 3600 / max(1, self.bitrate * 1000 / 8), lis, steer)

    async def supervise_zones(self):
        """Restart a pipeline whose Liquidsoap died — otherwise that device vanishes silently."""
        for zone in self.zones:
            proc = self.procs.get(zone.mount)
            if proc is not None and proc.returncode is not None:
                log.warning("the audio pipeline for zone /%s exited (%s) — restarting",
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

    async def probe_after_disconnect(self, mac: str, ip: str):
        """Answer, in the log, the one question nobody has been able to answer by hand.

        Every freeze at site 5 ends the same way: the SlimProto session dies and some time
        later somebody pulls the breaker. What has never been established is whether the unit
        was still alive in between — a radio that still serves its web page has lost one task,
        a radio that answers nothing has lost everything, and those point at different causes.
        Asking the customer to check has failed three times, so the add-on checks itself.

        Read-only: a TCP handshake on :80 and :61695, closed immediately, three times over two
        minutes. That is nine connections against a device we otherwise heartbeat every 5 s.
        """
        if not ip:
            return
        waited = 0.0
        for delay in LIVENESS_PROBES:
            await asyncio.sleep(delay - waited)
            waited = delay
            if self.slim and mac in self.slim.players:
                log.info("LARA %s is back on SlimProto — liveness probe stopped", mac)
                return
            web, ctl = await asyncio.gather(
                asyncio.to_thread(discovery.port_open, ip, WEB_PORT),
                asyncio.to_thread(discovery.port_open, ip, ELKO_PORT),
            )
            if web or ctl:
                log.warning("LARA %s (%s) dropped its audio-zone session %.0fs ago but the unit "
                            "is alive — web page %s, control port %s. One task died, not the box.",
                            mac, ip, delay, "answers" if web else "silent",
                            "answers" if ctl else "silent")
            else:
                log.warning("LARA %s (%s) answers nothing on :%d or :%d %.0fs after its session "
                            "dropped — the whole unit is wedged, not just the audio zone.",
                            mac, ip, WEB_PORT, ELKO_PORT, delay)

    def on_slim_disconnect(self, player):
        # Not while shutting down: every radio disconnects at that moment, and a probe that
        # outlives the loop is three "Task was destroyed but it is pending!" errors in the log
        # of an otherwise clean stop — which is exactly the kind of noise that wastes an hour
        # the next time something real goes wrong.
        if self._loop and not self._stopping:
            t = self._loop.create_task(self.probe_after_disconnect(player.mac, player.ip))
            self._probes.add(t)
            t.add_done_callback(self._probes.discard)
        self.target.pop(player.mac, None)
        self.idle_since.pop(player.mac, None)
        self._stopped_at.pop(player.mac, None)
        self._stat_seen.pop(player.mac, None)
        self._blind.discard(player.mac)
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
    def initial_volume(self) -> int:
        """Where the Spotify slider starts, which on this firmware IS the volume.

        `zone_volume` used to reach only `audg`, the radio's hardware volume — and `audg` has
        no audible effect on fw 3.7.001. So setting it to 50 changed nothing while the slider
        sat at a hardcoded 100, which is exactly what a user reported. Feed it to librespot
        instead, where the one working volume control lives. 0 keeps the old meaning of "do not
        touch anything", which for the slider means full scale.
        """
        return 100 if self.volume <= 0 else min(100, self.volume)

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
        # Every push starts the re-push cooldown, not just a recovery. Switching mounts sends
        # `strm-q` first, the radio answers STMf ("stopped") and stays in that mode until its
        # new stream starts a few seconds later — which recover_if_stalled read as an underrun
        # and answered with a second push. 6 of 19 switches in one log, each one re-buffering
        # the radio a second time: part of why changing rooms felt slow.
        self._repushed_at[key] = time.monotonic()
        self._stall_seq.pop(key, None)
        self._mount_pushed_at[mount] = time.monotonic()
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

    def warn_if_blind(self, key: str, now: float):
        """Say when a radio is connected but has stopped telling us anything.

        Site 5 ran for over a day like this: the session stayed open, music kept playing, and
        every STAT-derived line in the log simply stopped. Nothing complained, because every
        check we have is driven by the STATs that were missing — including the underrun
        recovery, which waits for a STAT that never comes.
        """
        p = self.slim.players.get(key) if self.slim else None
        if p is None:
            return
        last = self._stat_seen.get(key)
        if last is None or last[0] != p.stat_seq:
            self._stat_seen[key] = (p.stat_seq, now)
            return
        if now - last[1] < STAT_SILENCE or key in self._blind:
            return
        self._blind.add(key)
        log.warning("LARA %s is still connected but has not sent a readable STAT for %.0fs — "
                    "its buffer and playing state are invisible and underrun recovery cannot "
                    "run. Restarting the add-on re-establishes the session.",
                    key, now - last[1])

    async def recover_if_stalled(self, key: str, mount: str, now: float):
        """Push the stream again if the radio stopped playing but stayed connected.

        On an underrun the LARA reports `STMu` and stops, without dropping the control
        connection. Nothing else notices: `target` still names the mount, so `route()` returns
        early and never pushes again, and the radio stays silent for as long as it keeps the
        connection open — minutes, in the wild, until it finally reconnects. Since `tick()`
        treats `target == mount` as "playing", this is the one place that can tell it is not.
        """
        p = self.slim.players.get(key) if self.slim else None
        if p is None:
            return
        if p.mode != "stop":
            self._stall_seq.pop(key, None)
            return
        # Note where its STAT counter stood the moment it stopped, even inside the cooldown —
        # otherwise a stop that lands just after a push would need yet another STAT once the
        # cooldown ends, for no reason.
        seen = self._stall_seq.setdefault(key, p.stat_seq)
        if now - self._repushed_at.get(key, -1e9) < REPUSH_COOLDOWN:
            return
        # Only push into a radio that is still talking to us. On 2026-09-23 a LARA reported one
        # underrun and never said another word; we pushed a fresh stream at it one second later
        # and it stayed dead for 38 hours, until its mains lead was pulled. Whether the push
        # killed it is unproven — 52 other underruns that month recovered through exactly this
        # path — but a device whose last word was "I stopped" is the worst possible one to ask
        # for another connection. So: note where the STAT counter stood when it stalled, and
        # push only once a later STAT proves it is still alive. Costs a few seconds of silence
        # in the healthy case; in the fatal one it sends nothing at all.
        if p.stat_seq <= seen:
            return
        self._stall_seq.pop(key, None)
        self._repushed_at[key] = now
        log.warning("LARA %s stopped playing on its own (underrun?) while /%s is still "
                    "streaming — it is still answering, so pushing it again", key, mount)
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

    # --- Home Assistant sensors ---------------------------------------------------
    @staticmethod
    def sensor_id(mac: str) -> str:
        return "sensor.lr3_lara_" + mac.replace(":", "").lower()[-6:]

    def sensor_states(self) -> dict[str, tuple[str, dict]]:
        """One sensor per radio: the name of the Spotify device it is playing, or "off".

        Read-only, and deliberately plain so Home Assistant can act on it. The case that asked
        for it: a terrace with three speakers on one LARA, two of them behind relays, where the
        Spotify device you pick should decide which speakers come on. The state follows the
        *radio*, not Spotify — it changes when we push the radio a stream and returns to "off"
        when its zone is switched off — so a relay never flips under music still coming out of
        the radio's buffer.
        """
        active = {z.mount for z in self.zones if spotify_active(z.mount)}
        out = {}
        for mac, r in self.radios.items():
            rec = r["rec"]
            radio = self.display_name(rec, mac)
            mount = self.target.get(mac)
            out[self.sensor_id(mac)] = (
                self.zone_names.get(mount, mount) if mount else "off",
                {"friendly_name": f"{radio} – Spotify",
                 "icon": "mdi:speaker" if mount else "mdi:speaker-off",
                 "radio": radio, "mac": mac, "ip": rec.get("ip") or "",
                 "mount": mount or "", "spotify": "playing" if mount in active else "idle"})
        return out

    def post_state(self, entity: str, state: str, attrs: dict):
        """Blocking — run it in a thread. The heartbeat to every radio lives on this loop."""
        req = urllib.request.Request(
            f"{HA_API}/states/{entity}", method="POST",
            data=json.dumps({"state": state, "attributes": attrs}).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.ha_token}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as r:
            r.read()

    async def publish_sensors(self, now: float):
        """Post what changed; re-post everything every few minutes (HA forgets on restart)."""
        if not self.ha_token or (self._ha_task and not self._ha_task.done()):
            return
        if self._ha_failing and now < self._ha_retry_at:
            return
        want = self.sensor_states()
        refresh = now >= self._ha_refresh_at
        todo = {e: v for e, v in want.items() if refresh or self._ha_sent.get(e) != v}
        if todo:
            self._ha_task = asyncio.create_task(self._push_sensors(todo, now, refresh))

    async def _push_sensors(self, todo: dict, now: float, refresh: bool):
        first = not self._ha_sent
        try:
            for entity, (state, attrs) in todo.items():
                await asyncio.to_thread(self.post_state, entity, state, attrs)
                self._ha_sent[entity] = (state, attrs)
        except Exception as e:
            self._ha_retry_at = time.monotonic() + HA_RETRY
            if not self._ha_failing:
                self._ha_failing = True
                log.warning("cannot update the Home Assistant sensors (%s) — retrying every "
                            "%.0fs; playback is not affected", e, HA_RETRY)
            return
        if refresh:
            self._ha_refresh_at = now + HA_REFRESH
        if self._ha_failing:
            self._ha_failing = False
            log.info("the Home Assistant sensors are being updated again")
        if first:
            log.info("Home Assistant sensors: %s",
                     ", ".join(f"{e} ({a['radio']})" for e, (_, a) in sorted(todo.items())))

    async def retire_sensors(self):
        """On the way out, say so — a relay should not hold on to a zone that no longer runs."""
        if self._ha_task and not self._ha_task.done():
            self._ha_task.cancel()
            try:
                await self._ha_task
            except (asyncio.CancelledError, Exception):
                pass
        if not self.ha_token or not self._ha_sent:
            return

        async def go():
            for entity, (_, attrs) in list(self._ha_sent.items()):
                await asyncio.to_thread(self.post_state, entity, "unavailable", attrs)
        try:
            await asyncio.wait_for(go(), 3)
        except (asyncio.TimeoutError, Exception):
            pass

    async def tick(self):
        active = {z.mount for z in self.zones if spotify_active(z.mount)}
        now = time.monotonic()
        for key in list(self.radios.keys()):
            mount = self.settled_zone_for(key, active, now)
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
                    self.warn_if_blind(key, now)
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
        log.info("LR3 AudioZone v%s — mode=%s our_ip=%s idle_timeout=%ds "
                 "buffer=%.1fs (%d KB @ %d kbps)",
                 os.environ.get("LR3_VERSION", "?"), self.mode, self.our_ip, self.idle_timeout,
                 self.buffer_seconds, self.buffer_kb, self.bitrate)
        if 0 < self.idle_timeout < IDLE_TIMEOUT_FLOOR:
            log.warning("idle_timeout is %ds. Switching a zone off flushes whatever the radio "
                        "has buffered but not yet played, and it runs several seconds — "
                        "sometimes tens of seconds — behind the app, so a timeout this short "
                        "cuts the ends off songs. %ds or more is safer.",
                        self.idle_timeout, IDLE_TIMEOUT_FLOOR)
        if self.remote_access:
            log.info("Spotify remote access is ON — the last account to select a zone stays "
                     "logged in and sees it from anywhere, not just on this network")
        else:
            log.info("Spotify remote access is OFF — zones are offered to everyone on this "
                     "network and to nobody outside it; no Spotify login is stored")
        if self.rate_match:
            self.load_rates()
            log.info("rate_match is on — each zone is sent at its radios' own pace (from %+.0f "
                     "ppm at %d Hz), so the lag no longer grows during a long session",
                     self.default_ppm(), self.samplerate)
        else:
            log.info("rate_match is off — Liquidsoap sends in exact real time; at %d Hz the "
                     "radios run %+.0f ppm off that, which builds up as lag",
                     self.samplerate, self.default_ppm())
        if not self.ha_token:
            log.warning("no Supervisor token, neither in the environment nor in %s — the Home "
                        "Assistant sensors are disabled", S6_ENV_DIRS[0])
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
            log.warning("control_mode=off — the Spotify zones work, but NO radio will ever be "
                        "switched into its audio zone. Set 'Ovládání LARA' back to 'slimproto' "
                        "in the add-on configuration to make the radios play again.")
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
                try:
                    self.measure_backlog()
                except Exception:
                    log.exception("measuring the Icecast backlog failed")
                if self.mode == "slimproto":
                    try:
                        await self.tick()
                    except Exception:
                        log.exception("route tick failed")
                    try:
                        self.update_rates(time.monotonic())
                    except Exception:
                        log.exception("steering the zone rates failed")
                else:
                    self.nag_if_muzzled()
                try:
                    await self.publish_sensors(time.monotonic())
                except Exception:
                    log.exception("updating the Home Assistant sensors failed")
                try:
                    await asyncio.wait_for(stopping.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._stopping = True
            for t in list(self._probes):
                t.cancel()
            if self.rate_match:
                self.save_rates()
            await self.retire_sensors()
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
