"""Minimal LMS (Logitech Media Server) CLI server — the LARA's "Audio zone" control channel.

The LARA's "Audio zone function" config has, besides the slim-server IP, a **CLI port**
(9595 by default on ELKO's side) plus an LMS username/password. The LARA opens a second,
text-based connection there and speaks the classic LMS command-line protocol: it asks who
the players are, what is playing, and what the volume/power state is, and it sends its own
front-panel button presses back as commands.

SlimProto (`slimproto.py`) alone is enough to make audio come out — but without the CLI the
LARA has no idea what it is playing, its display stays empty, and buttons on the unit do
nothing. This module closes that gap.

Protocol (verbatim from LMS's `Slim::Control::Stdio`):
  * one command per line, `\\n`-terminated;
  * tokens are separated by spaces and each token is individually URL-encoded;
  * the server answers by **echoing the request** with the result appended — a trailing
    `?` in the request is replaced by the value;
  * `listen 1` turns the connection into a subscriber that also receives pushed events.

Pure stdlib. Unknown commands are echoed back unchanged and logged once, so a real device
teaches us what else it wants without breaking the session.
"""
from __future__ import annotations

import asyncio
import logging
import time
from urllib.parse import quote, unquote

log = logging.getLogger("lr3.cli")

DEFAULT_CLI_PORT = 9595
SERVER_VERSION = "7.9.1"

# A LARA (fw 3.7.001) dumps its own state right after `login` — observed verbatim:
#   login admin elkoep / <mac> artist ? / <mac> stop / <mac> mixer volume 95 / playlist tracks ?
# That `stop` is "I am not playing anything", NOT a button press. Acting on it would fight the
# controller (push -> LARA says stop -> we power off -> next tick pushes again). So transport
# commands arriving in the first few seconds of a CLI session are treated as state sync.
HANDSHAKE_GRACE = 5.0

_MAC_CHARS = set("0123456789abcdef:")
_unknown_seen: set[str] = set()


def _is_mac(tok: str) -> bool:
    t = tok.lower()
    return len(t) == 17 and t.count(":") == 5 and set(t) <= _MAC_CHARS


def _enc(tok) -> str:
    return quote(str(tok), safe="")


class LmsCliServer:
    """Answers the LARA's LMS CLI queries and mirrors SlimProto state to it."""

    def __init__(self, slim, port: int = DEFAULT_CLI_PORT, username: str = "",
                 password: str = "", zone_names: dict | None = None,
                 fallback_name: str = "Audio zóna", on_command=None):
        self.slim = slim
        self.port = port
        self.username = username
        self.password = password
        # mount -> Spotify device name, shared (by reference) with the controller: a radio can
        # be on its own zone or on the group one, and the display should say which.
        self.zone_names = zone_names if zone_names is not None else {}
        self.fallback_name = fallback_name
        self.on_command = on_command   # async callback(player_mac, verb) for play/stop/power
        self._listeners: set[asyncio.StreamWriter] = set()
        self._session_start: dict[int, float] = {}   # id(writer) -> when this CLI session opened
        self._server: asyncio.AbstractServer | None = None

    async def start(self):
        self._server = await asyncio.start_server(self._handle, "0.0.0.0", self.port)
        log.info("LMS CLI server listening on :%d", self.port)

    # --- connection handling ---------------------------------------------------
    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        log.info("CLI client connected: %s", peer)
        self._session_start[id(writer)] = time.monotonic()
        try:
            while True:
                raw = await reader.readline()
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                tokens = [unquote(t) for t in line.split(" ") if t != ""]
                log.debug("cli <- %s", tokens)
                if tokens and tokens[0].lower() in ("exit", "quit"):
                    break
                try:
                    reply = await self._dispatch(tokens, writer)
                except Exception:
                    log.exception("CLI command failed: %r", line)
                    reply = tokens
                if reply is not None:
                    writer.write((" ".join(_enc(t) for t in reply) + "\n").encode())
                    await writer.drain()
        except (ConnectionError, OSError):
            pass
        finally:
            self._listeners.discard(writer)
            self._session_start.pop(id(writer), None)
            log.info("CLI client disconnected: %s", peer)
            try:
                writer.close()
            except Exception:
                pass

    # --- helpers ---------------------------------------------------------------
    @staticmethod
    def _reply(tokens: list[str], *values) -> list[str]:
        """Echo the request; a trailing '?' is replaced by the answer."""
        out = list(tokens)
        if out and out[-1] == "?":
            out.pop()
        out.extend(str(v) for v in values)
        return out

    def _zone(self, player) -> str:
        """Name of the zone this player is currently on — what its display should show."""
        return self.zone_names.get(player.current_mount or "", self.fallback_name)

    def _players(self) -> list:
        return list(self.slim.players.values())

    def _player(self, pid: str | None):
        if pid:
            p = self.slim.players.get(pid.lower())
            if p:
                return p
        players = self._players()
        return players[0] if players else None

    def _player_fields(self, p, index: int) -> list[str]:
        return [
            f"playerindex:{index}",
            f"playerid:{p.mac}",
            f"uuid:{p.mac.replace(':', '')}",
            f"ip:{p.ip}:3483",
            f"name:{p.name}",
            "seq_no:0",
            "model:squeezeslave",
            "modelname:LARA",
            "power:" + ("1" if p.powered else "0"),
            "isplaying:" + ("1" if p.mode == "play" else "0"),
            "displaytype:none",
            "isplayer:1",
            "canpoweroff:1",
            "connected:1",
            "firmware:0",
        ]

    def _status_fields(self, p) -> list[str]:
        title = p.title or self._zone(p)
        return [
            f"player_name:{p.name}",
            "player_connected:1",
            f"player_ip:{p.ip}:3483",
            "power:" + ("1" if p.powered else "0"),
            "signalstrength:0",
            f"mode:{p.mode}",
            f"time:{p.elapsed:.2f}",
            "rate:1",
            "duration:0",
            f"mixer volume:{p.volume}",
            "playlist repeat:0",
            "playlist shuffle:0",
            "playlist mode:off",
            "seq_no:0",
            "playlist_cur_index:0",
            "playlist_timestamp:0",
            "playlist_tracks:1" if p.current_mount else "playlist_tracks:0",
        ]

    def _track_fields(self, p) -> list[str]:
        if not p.current_mount:
            return []
        return [
            "playlist index:0",
            "id:-1",
            f"title:{p.title or self._zone(p)}",
            f"artist:{self._zone(p)}",
            "album:Spotify Connect",
            f"url:{self.slim.stream_url(p.current_mount)}",
            "remote:1",
            "duration:0",
        ]

    async def _invoke(self, mac: str, verb: str):
        if self.on_command:
            await self.on_command(mac, verb)

    # --- command dispatch ------------------------------------------------------
    async def _dispatch(self, tokens: list[str], writer: asyncio.StreamWriter) -> list[str] | None:
        pid = tokens[0] if tokens and _is_mac(tokens[0]) else None
        cmd = tokens[1:] if pid else tokens
        if not cmd:
            return tokens
        verb = cmd[0].lower()
        arg1 = cmd[1] if len(cmd) > 1 else ""
        p = self._player(pid)

        # --- session ---------------------------------------------------------
        if verb == "login":
            user = cmd[1] if len(cmd) > 1 else ""
            passwd = cmd[2] if len(cmd) > 2 else ""
            ok = (not self.username and not self.password) or \
                 (user == self.username and passwd == self.password)
            if not ok:
                log.warning("CLI login mismatch (user=%r) — accepting anyway; check "
                            "cli_username/cli_password against the LARA's Audio-zone config", user)
            return ["login", user, "******"]

        if verb == "listen":
            if arg1 == "0":
                self._listeners.discard(writer)
            else:
                self._listeners.add(writer)
                log.info("CLI client subscribed to notifications")
            return tokens

        if verb in ("subscribe", "can", "pref", "playerpref", "displaynotify"):
            return tokens

        if verb == "version":
            return self._reply(tokens, SERVER_VERSION)

        # --- inventory -------------------------------------------------------
        if verb == "players":
            out = self._reply(tokens, f"count:{len(self._players())}")
            for i, pl in enumerate(self._players()):
                out.extend(self._player_fields(pl, i))
            return out

        if verb == "player":
            what = arg1.lower()
            players = self._players()
            if what == "count":
                return self._reply(tokens, len(players))
            idx = 0
            if len(cmd) > 2 and cmd[2].isdigit():
                idx = int(cmd[2])
            pl = players[idx] if idx < len(players) else None
            if not pl:
                return self._reply(tokens, "")
            return self._reply(tokens, {"id": pl.mac, "name": pl.name, "ip": f"{pl.ip}:3483",
                                        "model": "squeezeslave", "uuid": pl.mac.replace(":", ""),
                                        "isplayer": 1, "displaytype": "none"}.get(what, ""))

        if verb == "serverstatus":
            out = self._reply(tokens, "lastscan:0", "version:" + SERVER_VERSION,
                              "info total albums:0", "info total artists:0",
                              "info total genres:0", "info total songs:0",
                              f"player count:{len(self._players())}")
            for i, pl in enumerate(self._players()):
                out.extend(self._player_fields(pl, i))
            return out

        if not p:
            # Nothing connected over SlimProto yet — answer structurally, not with an error.
            return self._reply(tokens, "0") if tokens[-1] == "?" else tokens

        # --- per-player state ------------------------------------------------
        if verb == "status":
            out = self._reply(tokens)
            out.extend(self._status_fields(p))
            out.extend(self._track_fields(p))
            return out

        if verb == "mode":
            return self._reply(tokens, p.mode)

        if verb == "connected":
            return self._reply(tokens, 1)

        if verb == "signalstrength":
            return self._reply(tokens, 0)

        if verb == "name":
            return self._reply(tokens, p.name)

        if verb == "time":
            return self._reply(tokens, f"{p.elapsed:.2f}")

        if verb == "duration":
            return self._reply(tokens, 0)

        if verb in ("artist", "album", "genre"):
            return self._reply(tokens, self._zone(p) if p.current_mount else "")

        if verb in ("title", "current_title"):
            return self._reply(tokens, (p.title or self._zone(p)) if p.current_mount else "")

        if verb == "path":
            return self._reply(tokens, self.slim.stream_url(p.current_mount) if p.current_mount else "")

        if verb == "power":
            if arg1 in ("0", "1"):
                await self._invoke(p.mac, "power_on" if arg1 == "1" else "power_off")
                return tokens
            return self._reply(tokens, 1 if p.powered else 0)

        if verb == "mixer":
            what = arg1.lower()
            val = cmd[2] if len(cmd) > 2 else "?"
            if what == "volume":
                if val == "?":
                    return self._reply(tokens, p.volume)
                try:
                    vol = p.volume + int(val) if val[0] in "+-" else int(val)
                except ValueError:
                    return tokens
                # The LARA reports the position of its own volume knob here. Take it as state —
                # echoing an `audg` straight back would fight the knob (and the CLI notify would
                # bounce the same value at it again).
                p.volume = max(0, min(100, vol))
                log.debug("LARA %s reports volume=%d", p.mac, p.volume)
                return tokens
            if what == "muting":
                return self._reply(tokens, 0)
            return tokens

        # --- transport (the LARA's own buttons land here) --------------------
        if verb in ("play", "pause", "stop", "button"):
            age = time.monotonic() - self._session_start.get(id(writer), 0.0)
            if age < HANDSHAKE_GRACE:
                log.debug("CLI %r from %s ignored (%.1fs into the session — state sync, "
                          "not a button)", verb, p.mac, age)
                return tokens
            action = verb if verb != "button" else arg1.lower()
            if action in ("play", "play.single", "play.hold"):
                await self._invoke(p.mac, "play")
            elif action in ("stop", "power", "power_off", "powerof"):
                await self._invoke(p.mac, "stop")
            elif action.startswith("pause"):
                await self.slim.pause(p.mac, p.mode == "play")
            else:
                log.info("CLI button %r from %s (unmapped)", action, p.mac)
            return tokens

        if verb == "playlist":
            what = arg1.lower()
            if what in ("play", "load", "resume"):
                if time.monotonic() - self._session_start.get(id(writer), 0.0) >= HANDSHAKE_GRACE:
                    await self._invoke(p.mac, "play")
                return tokens
            if what == "tracks":
                return self._reply(tokens, 1 if p.current_mount else 0)
            if what == "index":
                return self._reply(tokens, 0)
            if what == "path":
                return self._reply(tokens, self.slim.stream_url(p.current_mount) if p.current_mount else "")
            if what == "name":
                return self._reply(tokens, self._zone(p))
            return self._reply(tokens, 0) if tokens[-1] == "?" else tokens

        # --- library browsing: we have no library, say so cleanly ------------
        if verb in ("albums", "artists", "titles", "genres", "playlists", "musicfolder",
                    "favorites", "radios", "apps", "search", "menu", "browselibrary"):
            return self._reply(tokens, "count:0")

        if verb not in _unknown_seen:
            _unknown_seen.add(verb)
            log.info("CLI: unhandled command %r (echoed back) — full line: %s", verb, tokens)
        return tokens

    # --- push notifications ----------------------------------------------------
    def notify(self, player, what: str):
        """Mirror a SlimProto state change to subscribed CLI clients (`listen 1`)."""
        if not self._listeners:
            return
        lines: list[list[str]] = []
        if what in ("play", "connect"):
            lines.append([player.mac, "playlist", "newsong", player.title or self._zone(player), "0"])
        elif what == "stop":
            lines.append([player.mac, "playlist", "stop"])
        elif what == "power":
            lines.append([player.mac, "power", "1" if player.powered else "0"])
        elif what == "volume":
            lines.append([player.mac, "mixer", "volume", str(player.volume)])
        elif what == "mode":
            if player.mode == "play":
                lines.append([player.mac, "playlist", "newsong", player.title or self._zone(player), "0"])
            else:
                lines.append([player.mac, "playlist",
                              "pause" if player.mode == "pause" else "stop"])
        for tokens in lines:
            payload = (" ".join(_enc(t) for t in tokens) + "\n").encode()
            for w in list(self._listeners):
                try:
                    w.write(payload)
                except Exception:
                    self._listeners.discard(w)
