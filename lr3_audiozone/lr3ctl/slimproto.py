"""SlimProto (Squeezebox) server — the way we drive a LARA's audio zone.

Drives a SlimProto player (LARA with "Audio zone function" enabled + slim-server IP = us)
to fetch + play our Icecast MP3 mount, with play/stop/power/volume control. Byte layouts
verified against squeezelite + music-assistant/aioslimproto AND a real LARA (fw 3.7.001).

The player fetches audio DIRECTLY from server_ip:server_port (our Icecast), not proxied.

Power: SlimProto has no "power" opcode — LMS powers a player down by muting its outputs
(`aude 0 0`) after stopping the stream, and tells the UI over the CLI (`<mac> power 0`).
That is what `set_power()` does; `lmscli.py` mirrors it to the LARA's CLI connection.
"""
from __future__ import annotations

import asyncio
import logging
import re
import struct

log = logging.getLogger("lr3.slim")

SLIMPROTO_PORT = 3483
# We heartbeat every 5 s and the player answers each one with a STAT, so a session that has
# said nothing for this long is dead however healthy TCP thinks it is.
SESSION_SILENCE = 90
_MP3_CODEC = b"m\x3f\x3f\x3f\x3f"  # 'm' = mp3 + 4 ignored pcm bytes

# STAT payload (player -> server), big-endian, unpadded. Fields:
#   0 event  1 num_crlf  2 mas_init  3 mas_mode  4 rptr_buf_size  5 rptr_buf_fullness
#   6 bytes_received  7 signal_strength  8 jiffies  9 output_buf_size  10 output_buf_fullness
#   11 elapsed_seconds  12 voltage  13 elapsed_ms  14 server_timestamp  [15 error_code]
# LARA fw 3.7.001 sends **51 bytes** — it omits the trailing error_code. Try the long layout
# first, fall back to the short one.
_STAT_FMT = "!4sBBBLLQHLLLLHLLH"
_STAT_FMT_SHORT = "!4sBBBLLQHLLLLHLL"
_STAT_LEN = struct.calcsize(_STAT_FMT)
_STAT_LEN_SHORT = struct.calcsize(_STAT_FMT_SHORT)

# Player event codes -> the mode string the LMS CLI reports.
_MODE_BY_EVENT = {
    b"STMs": "play", b"STMr": "play", b"STMl": "play",
    b"STMp": "pause",
    b"STMf": "stop", b"STMu": "stop", b"STMo": "stop", b"STMn": "stop",
}


def _frame(command: bytes, payload: bytes = b"") -> bytes:
    """Server->player frame: 2-byte BE length (incl. 4-byte command) + command + payload."""
    body = command + payload
    return struct.pack("!H", len(body)) + body


def _strm_body(cmd: bytes, *, autostart: bytes = b"0", flags: int = 0, server_port: int = 0,
               server_ip: int = 0, replay_gain: int = 0, threshold: int = 0,
               output_threshold: int = 0, http: bytes = b"", codec: bytes = _MP3_CODEC) -> bytes:
    """The 24-byte strm struct (+ optional embedded HTTP request)."""
    return struct.pack(
        "!cc5sBcBcBBBLHL",
        cmd,                       # b's' start, b'q' stop, b'p' pause, b'u' unpause, b't' status
        autostart,                 # b'0'..b'3'  ('3' = direct + autostart)
        codec,                     # 5 bytes: format + 4 pcm bytes
        threshold & 0xFF,          # KB to buffer before autostart
        b"0",                      # spdif: '0' = auto
        0,                         # transition duration (s)
        b"0",                      # transition type: '0' = none
        flags & 0xFF,              # 0x20 if https
        output_threshold & 0xFF,   # output buffer (tenths of a second)
        0,                         # reserved
        replay_gain & 0xFFFFFFFF,  # 16.16 gain; doubles as the strm-'t' heartbeat id
        server_port & 0xFFFF,
        server_ip & 0xFFFFFFFF,    # 0 => player uses the control-connection IP
    ) + http


class Player:
    def __init__(self, mac: str, dev_id: int, caps: str, writer: asyncio.StreamWriter):
        self.mac = mac
        self.dev_id = dev_id
        self.caps = caps
        self.writer = writer
        self.ip = (writer.get_extra_info("peername") or ("", 0))[0]
        self.current_mount: str | None = None
        self.powered = False
        self.mode = "stop"          # play | pause | stop  (from STAT events)
        self.elapsed = 0.0          # seconds into the stream
        self.volume = 90
        self.title = ""    # track name, as the LARA's display shows it
        self.artist = ""

    @property
    def name(self) -> str:
        m = re.search(r"ModelName=([^,]+)", self.caps)
        return m.group(1) if m else "LARA"

    def has_codec(self, name: str) -> bool:
        return name.lower() in self.caps.lower()


class SlimProtoServer:
    def __init__(self, our_ip: str, icecast_port: int = 8121, buffer_kb: int = 36,
                 on_connect=None, on_disconnect=None, on_state=None):
        self.our_ip = our_ip
        self.icecast_port = icecast_port
        # KB the player buffers before it starts. This IS the dominant latency in the chain,
        # so it is derived from the bitrate (see Controller) rather than fixed. Must stay well
        # under the 131072 B input buffer the LARA reports.
        self.buffer_kb = max(8, min(120, int(buffer_kb)))
        self.players: dict[str, Player] = {}
        self.on_connect = on_connect        # callback(Player)
        self.on_disconnect = on_disconnect  # callback(Player)
        self.on_state = on_state            # callback(Player, what: str) — for the LMS CLI
        self._server: asyncio.AbstractServer | None = None
        self._tasks: set[asyncio.Task] = set()   # strong refs to the per-player heartbeats

    async def start(self):
        self._server = await asyncio.start_server(self._handle, "0.0.0.0", SLIMPROTO_PORT)
        log.info("SlimProto server listening on :%d", SLIMPROTO_PORT)

    def _notify(self, player: Player, what: str):
        if not self.on_state:
            return
        try:
            self.on_state(player, what)
        except Exception:
            log.exception("on_state callback failed")

    async def _send(self, player: Player, command: bytes, payload: bytes = b""):
        try:
            player.writer.write(_frame(command, payload))
            await player.writer.drain()
        except Exception:
            pass

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        player: Player | None = None
        peer = writer.get_extra_info("peername")
        try:
            while True:
                # A live player answers our 5 s heartbeat with a STAT, so silence this long
                # means the socket is dead even though TCP still calls it ESTABLISHED. One of
                # these sat open for eight hours against a radio that had frozen, with nothing
                # in the log to say so.
                hdr = await asyncio.wait_for(reader.readexactly(8), SESSION_SILENCE)
                op = hdr[:4]
                length = struct.unpack("!I", hdr[4:8])[0]
                data = await reader.readexactly(length) if length else b""
                if op == b"HELO":
                    player = await self._on_helo(data, writer)
                elif op == b"STAT":
                    if player:
                        self._on_stat(player, data)
                elif op == b"BYE!":
                    break
                else:
                    log.debug("slim <- %r from %s (%d B)", op, peer, length)
        except asyncio.TimeoutError:
            log.warning("LARA %s sent nothing for %ds although it answers our heartbeat every "
                        "5s — treating the session as dead and closing it",
                        player.mac if player else peer, SESSION_SILENCE)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            if player and self.players.get(player.mac) is player:
                del self.players[player.mac]
                log.info("LARA %s disconnected", player.mac)
                if self.on_disconnect:
                    try:
                        self.on_disconnect(player)
                    except Exception:
                        log.exception("on_disconnect callback failed")
            try:
                writer.close()
            except Exception:
                pass

    async def _on_helo(self, data: bytes, writer: asyncio.StreamWriter) -> Player:
        dev_id, _rev, mac = struct.unpack("BB6s", data[:8])
        mac_str = ":".join("%02x" % b for b in mac)
        # Capabilities are an ASCII "k=v,...,codec,codec" string whose byte offset varies by
        # firmware (LARA fw 3.7.001 puts it at ~34, not 24). Find the first long printable run
        # after dev_id+mac instead of assuming a fixed offset.
        m = re.search(rb"[ -~]{8,}", data[8:])
        caps = m.group(0).decode("latin1", "replace") if m else ""
        old = self.players.get(mac_str)
        player = Player(mac_str, dev_id, caps, writer)
        self.players[mac_str] = player
        if old is not None and old.writer is not writer:
            # A second HELO from a MAC we already hold means the radio reconnected without
            # closing the first socket. Real LMS closes the old one on sight; leaving it open
            # keeps a socket alive on a device that has very few of them.
            log.info("LARA %s reconnected — closing its previous session", mac_str)
            try:
                old.writer.close()
            except Exception:
                pass
        log.info("LARA connected: mac=%s dev=%d caps=%r", mac_str, dev_id, caps)
        # Setup handshake that makes the player ready to emit audio.
        await self._send(player, b"vers", b"7.9")
        await self._send(player, b"setd", bytes([0xFE]))
        await self._send(player, b"setd", bytes([0x00]))
        # Start powered-down: outputs muted until Spotify actually plays. Without this the
        # LARA can sit in the audio zone silently "on" from the moment it dials in.
        await self._send(player, b"aude", bytes([0, 0]))
        await self.set_volume(mac_str, player.volume)
        # Keep a reference: a bare create_task can be garbage-collected mid-flight, and losing
        # the heartbeat costs us the connection ~17 s later.
        task = asyncio.create_task(self._heartbeat(player))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        if self.on_connect:
            try:
                self.on_connect(player)
            except Exception:
                log.exception("on_connect callback failed")
        self._notify(player, "connect")
        return player

    def _on_stat(self, player: Player, data: bytes):
        """Track mode + elapsed time from the player's STAT frames (feeds the LMS CLI)."""
        if len(data) >= _STAT_LEN:
            f = struct.unpack(_STAT_FMT, data[:_STAT_LEN])
        elif len(data) >= _STAT_LEN_SHORT:
            f = struct.unpack(_STAT_FMT_SHORT, data[:_STAT_LEN_SHORT])
        else:
            if len(data) >= 4:
                log.debug("LARA %s runt STAT %r (%d B)", player.mac, data[:4], len(data))
                self._apply_event(player, data[:4])
            return
        player.elapsed = f[13] / 1000.0 if f[13] else float(f[11])
        log.debug("LARA %s STAT %r out_buf=%d/%d in_buf=%d/%d bytes_rx=%d elapsed=%.1f",
                  player.mac, f[0], f[10], f[9], f[5], f[4], f[6], player.elapsed)
        self._apply_event(player, f[0])

    def _apply_event(self, player: Player, event: bytes):
        mode = _MODE_BY_EVENT.get(event)
        if mode and mode != player.mode:
            player.mode = mode
            log.info("LARA %s %s -> mode=%s", player.mac, event.decode("ascii", "replace"), mode)
            self._notify(player, "mode")

    async def _heartbeat(self, player: Player):
        hb = 0
        while self.players.get(player.mac) is player:
            hb = (hb + 1) & 0xFFFFFFFF
            await self._send(player, b"strm", _strm_body(b"t", replay_gain=hb))
            await asyncio.sleep(5)

    # --- public API ------------------------------------------------------------
    async def push_stream(self, mac: str, mount: str) -> bool:
        """Power the LARA up and tell it to play http://<us>:<icecast_port>/<mount>.

        Parameters below are not free choices — they were found by probing a real LARA
        (fw 3.7.001); every other combination left it in STMc with bytes_received=0:

        * ``server_ip=0`` — **the** fix. A LARA ignores an explicit address here and only
          fetches when told "use the IP of the control connection". Harmless for us: the
          add-on serves Icecast from the same host it runs the SlimProto server on.
        * ``autostart='1'`` (buffer, then start) rather than '3' — this firmware does not
          take the "direct streaming" variants.
        * ``threshold`` in KB must fit the player's input buffer, which it reports as
          131072 B — 200 KB was simply unreachable. It is also the biggest single source
          of latency, so it comes from ``buffer_kb`` (the controller derives it from the
          bitrate). 20 KB underran within seconds; ~1.5 s of audio is the working floor.
        """
        p = self.players.get(mac)
        if not p:
            return False
        await self.set_power(mac, True)
        path = "/" + mount.lstrip("/")
        host = f"{self.our_ip}:{self.icecast_port}"
        http = (
            f"GET {path} HTTP/1.0\r\nHost: {host}\r\n"
            "Connection: close\r\nAccept: */*\r\n\r\n"
        ).encode()
        body = _strm_body(
            b"s", autostart=b"1", threshold=self.buffer_kb, output_threshold=10,
            server_port=self.icecast_port, server_ip=0,
            http=http,
        )
        await self._send(p, b"strm", body)
        p.current_mount = mount
        p.mode = "play"
        log.info("LARA %s -> play %s", mac, path)
        self._notify(p, "play")
        return True

    async def stop(self, mac: str):
        p = self.players.get(mac)
        if p:
            await self._send(p, b"strm", _strm_body(b"q"))
            p.current_mount = None
            p.mode = "stop"
            p.elapsed = 0.0
            p.title = p.artist = ""   # the track is gone; don't leave it on the display
            self._notify(p, "stop")

    async def pause(self, mac: str, paused: bool = True):
        p = self.players.get(mac)
        if p:
            await self._send(p, b"strm", _strm_body(b"p" if paused else b"u"))
            p.mode = "pause" if paused else "play"
            self._notify(p, "mode")

    async def set_power(self, mac: str, on: bool):
        """LMS-style power: enable/mute the player's outputs. Off = the zone goes dark."""
        p = self.players.get(mac)
        if not p or p.powered == on:
            return
        await self._send(p, b"aude", bytes([1, 1]) if on else bytes([0, 0]))
        p.powered = on
        log.info("LARA %s power=%s", mac, "on" if on else "off")
        self._notify(p, "power")

    async def set_volume(self, mac: str, vol: int):
        p = self.players.get(mac)
        if not p:
            return
        vol = int(max(0, min(100, vol)))
        g = int(vol / 100.0 * 65536)
        await self._send(p, b"audg", struct.pack("!LLBBLL", g, g, 1, 255, g, g))
        p.volume = vol
        self._notify(p, "volume")

    def stream_url(self, mount: str) -> str:
        return f"http://{self.our_ip}:{self.icecast_port}/{mount.lstrip('/')}"
