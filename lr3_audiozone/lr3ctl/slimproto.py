"""Minimal SlimProto (Squeezebox) server — path B2, a CONDITIONAL UPGRADE.

Drives a SlimProto player (LARA in 'slim server' mode: slim_server_active + IP_slim_server=<us>)
to fetch + play an arbitrary Icecast MP3 URL, with play/stop/volume/switch control. Byte layouts
verified against squeezelite + music-assistant/aioslimproto. NOT the default path — enable only
after a real LARA is confirmed to dial in on :3483 and advertise 'mp3' (see docs/README).

The player fetches audio DIRECTLY from server_ip:server_port (our Icecast), not proxied — so this
reuses the whole existing Icecast/Liquidsoap/librespot stack unchanged.
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import struct

log = logging.getLogger("lr3.slim")

SLIMPROTO_PORT = 3483
_MP3_CODEC = b"m\x3f\x3f\x3f\x3f"  # 'm' = mp3 + 4 ignored pcm bytes


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
        self.current_mount: str | None = None

    def has_codec(self, name: str) -> bool:
        return name.lower() in self.caps.lower()


class SlimProtoServer:
    def __init__(self, our_ip: str, icecast_port: int = 8121, on_connect=None):
        self.our_ip = our_ip
        self.icecast_port = icecast_port
        self.players: dict[str, Player] = {}
        self.on_connect = on_connect  # callback(Player)
        self._server: asyncio.AbstractServer | None = None

    async def start(self):
        self._server = await asyncio.start_server(self._handle, "0.0.0.0", SLIMPROTO_PORT)
        log.info("SlimProto server listening on :%d", SLIMPROTO_PORT)

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
                hdr = await reader.readexactly(8)     # 4-byte op + 4-byte BE length
                op = hdr[:4]
                length = struct.unpack("!I", hdr[4:8])[0]
                data = await reader.readexactly(length) if length else b""
                if op == b"HELO":
                    player = await self._on_helo(data, writer)
                elif op == b"STAT":
                    pass  # absorb STAT keepalives; keeps the player streaming
                elif op == b"BYE!":
                    break
                else:
                    log.debug("slim <- %r from %s (%d B)", op, peer, length)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            pass
        finally:
            if player and self.players.get(player.mac) is player:
                del self.players[player.mac]
                log.info("LARA %s disconnected", player.mac)
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
        player = Player(mac_str, dev_id, caps, writer)
        self.players[mac_str] = player
        log.info("LARA connected: mac=%s dev=%d caps=%r", mac_str, dev_id, caps)
        # Setup handshake that makes the player ready to emit audio.
        await self._send(player, b"vers", b"7.9")
        await self._send(player, b"setd", bytes([0xFE]))
        await self._send(player, b"setd", bytes([0x00]))
        await self._send(player, b"aude", bytes([1, 1]))   # enable SPDIF + DAC outputs
        await self.set_volume(mac_str, 90)
        asyncio.create_task(self._heartbeat(player))
        if self.on_connect:
            try:
                self.on_connect(player)
            except Exception:
                log.exception("on_connect callback failed")
        return player

    async def _heartbeat(self, player: Player):
        hb = 0
        while self.players.get(player.mac) is player:
            hb = (hb + 1) & 0xFFFFFFFF
            await self._send(player, b"strm", _strm_body(b"t", replay_gain=hb))
            await asyncio.sleep(5)

    # --- public API ------------------------------------------------------------
    async def push_stream(self, mac: str, mount: str) -> bool:
        """Tell LARA <mac> to play http://<us>:<icecast_port>/<mount>."""
        p = self.players.get(mac)
        if not p:
            return False
        path = "/" + mount.lstrip("/")
        host = f"{self.our_ip}:{self.icecast_port}"
        http = (
            f"GET {path} HTTP/1.0\r\nHost: {host}\r\n"
            "Connection: close\r\nAccept: */*\r\n\r\n"
        ).encode()
        body = _strm_body(
            b"s", autostart=b"3", threshold=200, output_threshold=20,
            server_port=self.icecast_port, server_ip=int(ipaddress.ip_address(self.our_ip)),
            http=http,
        )
        await self._send(p, b"strm", body)
        p.current_mount = mount
        log.info("LARA %s -> play %s", mac, path)
        return True

    async def stop(self, mac: str):
        p = self.players.get(mac)
        if p:
            await self._send(p, b"strm", _strm_body(b"q"))
            p.current_mount = None

    async def set_volume(self, mac: str, vol: int):
        p = self.players.get(mac)
        if not p:
            return
        g = int(max(0, min(100, vol)) / 100.0 * 65536)
        await self._send(p, b"audg", struct.pack("!LLBBLL", g, g, 1, 255, g, g))
