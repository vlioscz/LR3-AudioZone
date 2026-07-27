"""Path A control of a single LARA over TCP 61695 (field-proven elkoep_lara byte layouts).

Each command opens a fresh TCP connection (connect, send one obfuscated packet, read reply,
close) — matching the reference library's transaction model.
"""
from __future__ import annotations

import socket

import elkoproto as ep


class LaraDevice:
    def __init__(self, host: str, user: str = "admin", password: str = "elkoep", timeout: float = 2.0):
        self.host = host
        self.user = user
        self.password = password
        self.timeout = timeout
        self.fw: int | None = None
        self.hw: int | None = None
        self.stations: list[str] = []

    # --- transport -------------------------------------------------------------
    def _txn(self, payload: bytes, want: int = 2100) -> bytes:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        data = b""
        try:
            s.connect((self.host, ep.CONTROL_PORT))
            s.sendall(payload)
            while len(data) < want:
                try:
                    chunk = s.recv(want)
                except socket.timeout:
                    break
                if not chunk:
                    break
                data += chunk
                # A complete reply satisfies len == body[6]+2 once decoded; but we can't
                # cheaply peek the length before decode, so stop on a short read instead.
                if len(chunk) < want:
                    break
        except OSError:
            pass
        finally:
            s.close()
        return data

    # --- reads -----------------------------------------------------------------
    def test(self) -> bool:
        """Confirm this really is a LARA and capture firmware/hardware version."""
        res = ep.parse_test_reply(self._txn(ep.build_test_packet(), 64))
        if res:
            self.fw, self.hw = res
        return res is not None

    def load_stations(self) -> list[str]:
        names = [""] * 40
        for page in range(4):
            parsed = ep.parse_stations_reply(self._txn(ep.build_stations(self.user, self.password, page), 2100))
            if parsed:
                _, _, page_names = parsed
                for i, nm in enumerate(page_names):
                    idx = page * 10 + i
                    if idx < 40:
                        names[idx] = nm
        self.stations = names
        return names

    def status(self) -> dict | None:
        return ep.parse_status_reply(self._txn(ep.build_status(self.user, self.password), 64))

    # --- commands --------------------------------------------------------------
    def _remote(self, command: int, attribute: int = 0) -> bool:
        return len(self._txn(ep.build_remote(self.user, self.password, command, attribute), 64)) > 0

    def select_source(self, source: int) -> bool:
        return self._remote(ep.CMD_SOURCE, source)

    def select_station(self, index: int) -> bool:
        return self._remote(ep.CMD_SELECT_STATION, index)

    def play(self) -> bool:
        return self._remote(ep.CMD_PLAY)

    def stop(self) -> bool:
        return self._remote(ep.CMD_STOP)

    def set_volume(self, vol: int) -> bool:
        return self._remote(ep.CMD_VOLUME, max(0, min(100, int(vol))))

    # --- high level ------------------------------------------------------------
    def station_index(self, name: str) -> int | None:
        target = name.strip().lower()
        for i, nm in enumerate(self.stations):
            if nm.strip().lower() == target:
                return i
        return None

    def play_preset(self, name: str) -> bool:
        """Switch to RADIO input and select the preset whose name matches (case-insensitive)."""
        if not any(self.stations):
            self.load_stations()
        idx = self.station_index(name)
        if idx is None:
            return False
        self.select_source(ep.SOURCE_RADIO)
        self.select_station(idx)
        self.play()
        return True
