"""LARA discovery via UDP broadcast on port 61695 (decoded from ELKO Finder)."""
from __future__ import annotations

import socket
import time

import elkoproto as ep


def discover(timeout: float = 2.0, retries: int = 2, broadcasts=None) -> dict:
    """Broadcast the probe and collect LARA replies. Returns {mac: {ip,name,mac,fw,hw}}.

    One broadcast to 255.255.255.255:61695 returns every LARA's ip+name+mac+fw. Radios are
    keyed by MAC (stable across DHCP). broadcasts: optional list of directed-broadcast addrs.
    """
    if broadcasts is None:
        broadcasts = ["255.255.255.255"]
    found: dict[str, dict] = {}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except OSError:
        pass
    # Bind to the control port so LARA's reply (sent back to our source port) reaches us,
    # exactly like ELKO Finder. Fall back to an ephemeral port if 61695 is taken.
    try:
        sock.bind(("", ep.DISCOVERY_PORT))
    except OSError:
        sock.bind(("", 0))
    sock.settimeout(0.3)

    try:
        for attempt in range(max(1, retries)):
            probe = ep.build_discovery_probe(seq=attempt)
            for bc in broadcasts:
                try:
                    sock.sendto(probe, (bc, ep.DISCOVERY_PORT))
                except OSError:
                    pass
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    data, addr = sock.recvfrom(2048)
                except socket.timeout:
                    continue
                except OSError:
                    break
                rec = ep.parse_discovery_reply(data)
                if rec:
                    if not rec.get("ip"):
                        rec["ip"] = addr[0]
                    found[rec["mac"]] = rec
    finally:
        sock.close()
    return found


if __name__ == "__main__":
    import json

    print(json.dumps(discover(), indent=2, ensure_ascii=False))
