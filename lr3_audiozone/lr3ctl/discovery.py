"""LARA discovery on port 61695 — UDP broadcast (ELKO Finder) and a TCP fallback.

⚠️ **LARA fw 3.7.001 does not answer UDP discovery at all** — not to a broadcast, not to a
directed broadcast, not even to a unicast probe, whatever the variant byte. Verified against a
real device. The very same probe sent over **TCP** answers immediately with the full record,
device name included, and `parse_discovery_reply` takes it unchanged.

So `find_radios()` does both: the UDP broadcast (still the cheap path, and other firmware may
answer it) plus a TCP sweep of the local /24. The sweep is what actually finds anything here,
and the name it returns is what the add-on advertises as a Spotify Connect device.
"""
from __future__ import annotations

import concurrent.futures
import logging
import socket
import time

import elkoproto as ep

log = logging.getLogger("lr3.discovery")


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


def probe_tcp(ip: str, timeout: float = 1.5) -> dict | None:
    """Ask one host over TCP 61695 who it is. Returns the same dict as a UDP reply, or None.

    This is the only thing that works on fw 3.7.001, and the only source of the device's
    user-assigned name (e.g. "LARA Koupelna").
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    data = b""
    try:
        s.connect((ip, ep.CONTROL_PORT))
        s.sendall(ep.build_discovery_probe(0))
        while len(data) < 128:
            # The read timeout is the normal way this ends — the LARA sends one reply and then
            # just sits there. Catch it INSIDE the loop: socket.timeout is an OSError, so
            # letting it reach the outer handler would throw away the reply we already have.
            try:
                chunk = s.recv(256)
            except socket.timeout:
                break
            if not chunk:
                break
            data += chunk
            if len(chunk) < 256:
                break
    except OSError:
        return None
    finally:
        s.close()
    rec = ep.parse_discovery_reply(data)
    if rec and not rec.get("ip"):
        rec["ip"] = ip
    return rec


def _port_open(ip: str, timeout: float) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, ep.CONTROL_PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def scan_subnet(prefix: str, timeout: float = 0.6, workers: int = 64) -> dict:
    """TCP-sweep <prefix>.1-254 for port 61695, then identify each hit. {mac: rec}."""
    hosts = [f"{prefix}.{i}" for i in range(1, 255)]
    found: dict[str, dict] = {}
    with concurrent.futures.ThreadPoolExecutor(workers) as ex:
        open_hosts = [h for h, ok in zip(hosts, ex.map(lambda h: _port_open(h, timeout), hosts)) if ok]
        for rec in ex.map(probe_tcp, open_hosts):
            if rec:
                found[rec["mac"]] = rec
    return found


def local_prefix() -> str | None:
    """The /24 this host sits on, e.g. '10.0.0'."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("1.1.1.1", 9))
        return s.getsockname()[0].rsplit(".", 1)[0]
    except OSError:
        return None
    finally:
        s.close()


def find_radios(hosts=(), subnet: str | None = None, scan: bool = True) -> dict:
    """Everything we can find: UDP broadcast + explicit hosts + a TCP sweep of the /24."""
    found = discover(timeout=1.5, retries=1)
    if found:
        log.info("UDP discovery answered with %d radio(s)", len(found))
    for h in hosts:
        rec = probe_tcp(h)
        if rec:
            found.setdefault(rec["mac"], rec)
        else:
            log.warning("no LARA answered on %s:%d", h, ep.CONTROL_PORT)
    if scan:
        prefix = subnet or local_prefix()
        if prefix:
            log.info("scanning %s.0/24 for LARAs on TCP %d ...", prefix, ep.CONTROL_PORT)
            for mac, rec in scan_subnet(prefix).items():
                found.setdefault(mac, rec)
    return found


if __name__ == "__main__":
    import json

    logging.basicConfig(level=logging.INFO)
    print(json.dumps(find_radios(), indent=2, ensure_ascii=False))
