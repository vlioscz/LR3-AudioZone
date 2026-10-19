#!/usr/bin/env python3
"""Watch internet-radio streams from the HA box the way a radio does, and log every hiccup.

Diagnostic only: it runs only for the URLs listed in the `stream_watch` option. Site 5,
2026-10-19: a LARA playing its own station (Radiožurnál) showed "ČEKÁM…" — waiting for stream
data — for seconds at a time, while a ping to the station's server stayed up and the add-on saw
nothing at all. HA hangs off the same switch and router as that radio, so watching the very same
stream from here tells "the router, the line or the server" (HA loses it at the same moment)
from "the radio itself" (only the radio does).

Two things starve a radio, and both are logged: a pause in the data (GAP or longer, with its
length) and data arriving slower than the stream plays (less than SLOW of the announced bitrate
over a RATE_WINDOW) — the second can empty a radio's buffer without any single long pause.

One thread per stream, so nothing here can ever hold up the loop that talks to the radios. Plain
sockets, not urllib: after one read timeout a urllib response refuses to read any further, and
measuring a pause means timing out over and over on the same connection. HTTP/1.0 and IPv4,
like a radio.
"""
from __future__ import annotations

import logging
import socket
import ssl
import threading
import time
from urllib.parse import urlsplit

log = logging.getLogger("lr3.watch")

GAP = 2.0              # a pause in the data this long is reported, with its exact length
GIVE_UP = 30.0         # no data for this long: drop the connection and open a new one
POLL = 0.25            # how often a waiting read wakes up to look at the clock
CONNECT_TIMEOUT = 10.0
RATE_WINDOW = 10.0     # delivered rate is judged over this long...
SLOW = 0.9             # ...and reported when under this share of the announced bitrate
DEFAULT_KBPS = 128     # when the server does not say (icy-br)
SHORT_LIVED = 10.0     # a connection that ends sooner than this counts as a failed one
SUMMARY_EVERY = 3600.0  # "still fine" once an hour, so silence in the log means silence
BACKOFF = (1, 2, 5, 10, 30, 60)  # seconds between attempts while a stream cannot be reached
FAIL_NOTE_EVERY = 600.0          # while it stays unreachable, say so this often, not every try
REDIRECTS = {"301", "302", "303", "307", "308"}


def check_url(url: str) -> str | None:
    """Why this address cannot be watched, or None. Never raises: one typo must not take the
    add-on — and with it every radio's Slim server — down."""
    try:
        p = urlsplit(url)
        p.port                      # raises ValueError on ":8o", ":99999", ...
    except ValueError as e:
        return str(e)
    if p.scheme not in ("http", "https"):
        return "only http:// and https:// addresses"
    if not p.hostname:
        return "no host name"
    return None


class StreamWatch(threading.Thread):
    def __init__(self, url: str, stop: threading.Event):
        super().__init__(daemon=True, name=f"stream watch {url}")
        self.url = url
        self.stop = stop
        p = urlsplit(url)
        self.tls = p.scheme == "https"
        self.host = p.hostname or ""
        self.port = p.port or (443 if self.tls else 80)
        self.path = (p.path or "/") + (f"?{p.query}" if p.query else "")
        self.label = f"{self.host}{p.path or '/'}"
        self.peer = ""
        self.kbps = DEFAULT_KBPS
        self.summary_at = time.monotonic() + SUMMARY_EVERY
        # since the last summary
        self.bytes = 0
        self.gaps = 0
        self.longest = 0.0
        self.drops = 0
        self.slow = 0

    # --- one connection --------------------------------------------------------------------
    def open(self) -> tuple[socket.socket, bytes]:
        """Connect and send the request; returns the socket and whatever came after the header."""
        # IPv4 only, as the radio: a dual-stack host would otherwise test a path it never uses.
        addr = socket.getaddrinfo(self.host, self.port, socket.AF_INET, socket.SOCK_STREAM)[0][4]
        sock = socket.create_connection(addr, timeout=CONNECT_TIMEOUT)
        try:
            self.peer = addr[0]
            if self.tls:
                sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
            sock.sendall(f"GET {self.path} HTTP/1.0\r\nHost: {self.host}\r\n"
                         f"User-Agent: LR3-AudioZone/stream-watch\r\nIcy-MetaData: 0\r\n"
                         f"Connection: close\r\n\r\n".encode("latin-1"))
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = sock.recv(4096)
                if not chunk:
                    raise ConnectionError("closed before the response header")
                head += chunk
                if len(head) > 16384:
                    raise ConnectionError("response header too long")
            header, rest = head.split(b"\r\n\r\n", 1)
            lines = header.decode("latin-1", "replace").split("\r\n")
            parts = lines[0].split()
            fields = {k.strip().lower(): v.strip() for k, _, v in
                      (ln.partition(":") for ln in lines[1:]) if k}
            if len(parts) < 2 or parts[1] != "200":
                where = fields.get("location") if len(parts) > 1 and parts[1] in REDIRECTS else ""
                raise ConnectionError(f"answered {lines[0]!r}" +
                                      (f" → {where} (put that address in stream_watch)"
                                       if where else ""))
            try:
                self.kbps = int(fields.get("icy-br", "").split(",")[0]) or DEFAULT_KBPS
            except ValueError:
                self.kbps = DEFAULT_KBPS
            sock.settimeout(POLL)
            return sock, rest
        except BaseException:
            sock.close()
            raise

    def note_gap(self, quiet: float):
        self.gaps += 1
        self.longest = max(self.longest, quiet)

    def maybe_summary(self):
        if time.monotonic() >= self.summary_at:
            self.summary_at = time.monotonic() + SUMMARY_EVERY
            self.summary()

    def listen(self, sock: socket.socket, first: bytes) -> str:
        """Read until the stream ends, stalls for GIVE_UP or we are stopped. Returns why it ended."""
        start = last = window_at = time.monotonic()
        window = len(first)
        gap_said = slow_said = False
        rate = self.kbps * 125          # bytes per second the stream plays at
        while not self.stop.is_set():
            self.maybe_summary()
            try:
                data = sock.recv(16384)
            except (socket.timeout, ssl.SSLWantReadError):
                quiet = time.monotonic() - last
                if quiet >= GAP and not gap_said:
                    gap_said = True
                    log.info("stream watch %s: no data for %.0f s (%.0f s into the connection)",
                             self.label, quiet, last - start)
                if quiet >= GIVE_UP:
                    self.note_gap(quiet)
                    return f"no data for {quiet:.0f} s — reconnecting"
                data = None
            except OSError as e:
                return f"connection broke ({e})"
            now = time.monotonic()
            if data is not None:
                if not data:
                    if now - last >= GAP:
                        self.note_gap(now - last)
                    return f"the server closed the connection after {now - start:.0f} s"
                quiet = now - last
                if quiet >= GAP:
                    self.note_gap(quiet)
                    log.info("stream watch %s: data again after a %.1f s pause", self.label,
                             quiet)
                gap_said = False
                last = now
                window += len(data)
                self.bytes += len(data)
            if now - window_at >= RATE_WINDOW:
                share = window / ((now - window_at) * rate)
                # The first window holds the server's burst; a pause is already reported above.
                if share < SLOW and window_at > start and not gap_said:
                    if not slow_said:
                        slow_said = True
                        self.slow += 1
                        log.info("stream watch %s: only %.0f %% of the stream's %d kbps "
                                 "arrived in the last %.0f s", self.label, share * 100,
                                 self.kbps, now - window_at)
                elif share >= SLOW and slow_said:
                    slow_said = False
                    log.info("stream watch %s: back to full rate", self.label)
                window_at, window = now, 0
        return ""

    # --- the loop --------------------------------------------------------------------------
    def run(self):
        fails, failing_since, fail_said, ok_once = 0, None, 0.0, False
        while not self.stop.is_set():
            try:
                sock, first = self.open()
            except Exception as e:
                self.failed(e, fails, failing_since, fail_said)
                fails, failing_since, fail_said = self._fail_state
                continue
            if failing_since is not None:
                log.info("stream watch %s: connected again (%s) after %.0f s without it",
                         self.label, self.peer, time.monotonic() - failing_since)
            elif not ok_once:
                log.info("stream watch %s: connected to %s (%s, %d kbps)", self.label,
                         self.peer, "https" if self.tls else "http", self.kbps)
            began = time.monotonic()
            self.bytes += len(first)
            try:
                why = self.listen(sock, first)
            finally:
                sock.close()
            if not why:                 # stopped
                break
            self.drops += 1
            if time.monotonic() - began < SHORT_LIVED:
                # Answered and hung up at once: a playlist address (.pls/.m3u) or an "offline"
                # page. Treat it like a failed connect — backed off, said once in a while.
                self.failed(ConnectionError(f"{why}; is this a playlist (.pls/.m3u) rather "
                                            f"than the stream itself?"),
                            fails, failing_since, fail_said)
                fails, failing_since, fail_said = self._fail_state
                continue
            ok_once = True
            fails, failing_since = 0, None
            log.info("stream watch %s: %s", self.label, why)
            self.stop.wait(1)
            self.maybe_summary()
        self.summary()
        log.info("stream watch %s: stopped", self.label)

    def failed(self, e: Exception, fails: int, failing_since, fail_said: float):
        now = time.monotonic()
        if failing_since is None:
            failing_since, fail_said = now, now
            log.warning("stream watch %s: cannot connect (%s) — trying again", self.label, e)
        elif now - fail_said >= FAIL_NOTE_EVERY:
            fail_said = now
            log.warning("stream watch %s: still cannot connect after %.0f min (%s)",
                        self.label, (now - failing_since) / 60, e)
        self._fail_state = (fails + 1, failing_since, fail_said)
        self.maybe_summary()
        self.stop.wait(BACKOFF[min(fails, len(BACKOFF) - 1)])

    def summary(self):
        log.info("stream watch %s: %.0f MB since the last summary, %d pause(s) of %.0f s or "
                 "more%s, %d slow stretch(es), %d dropped connection(s)", self.label,
                 self.bytes / 1048576, self.gaps, GAP,
                 f" (longest {self.longest:.1f} s)" if self.gaps else "", self.slow, self.drops)
        self.bytes = self.gaps = self.drops = self.slow = 0
        self.longest = 0.0


class Watchers:
    """All of them, with one switch to stop them."""

    def __init__(self, urls: list[str]):
        self.stop = threading.Event()
        self.threads = [StreamWatch(u, self.stop) for u in urls]

    def start(self):
        for t in self.threads:
            t.start()

    def close(self):
        self.stop.set()
