#!/usr/bin/env python3
"""Offline checks for streamwatch.py against a tiny local stream server.

    python tools/tests/test_streamwatch.py

What it has to get right: a pause in the data is reported once with its length, a server that
hangs up is reported and reconnected to, a dead stream is given up on after GIVE_UP and a
refusal is logged without flooding the log.
"""
import logging
import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.normpath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, os.path.join(REPO, "lr3_audiozone", "lr3ctl"))
import streamwatch as W  # noqa: E402

W.GAP, W.POLL, W.GIVE_UP, W.BACKOFF = 0.5, 0.05, 2.0, (0.2,)
W.SHORT_LIVED = 0.3       # the test streams below are short; real ones last for hours
CRLF, LF = bytes([13, 10]), bytes([10])
said = []


class Catch(logging.Handler):
    def emit(self, r): said.append(r.getMessage())


W.log.addHandler(Catch())
W.log.setLevel(logging.INFO)


def server(behaviour):
    """One-shot-per-connection HTTP server on localhost; `behaviour(conn, n)` serves a client."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)
    count = {"n": 0}

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            count["n"] += 1
            threading.Thread(target=serve, args=(conn, count["n"]), daemon=True).start()

    def serve(conn, n):
        req = b""
        while b"\r\n\r\n" not in req:
            part = conn.recv(1024)
            if not part:
                break
            req += part
        count["req"] = req
        try:
            behaviour(conn, n)
        except OSError:
            pass
        finally:
            conn.close()
    threading.Thread(target=loop, daemon=True).start()
    return srv, srv.getsockname()[1], count


def wait_for(pred, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


# 1) a pause in the data, then the server hangs up: both reported, and we connect again
def pause_then_close(conn, n):
    conn.sendall(b"ICY 200 OK\r\ncontent-type: audio/mpeg\r\n\r\n" + b"x" * 1000)
    for _ in range(5):
        time.sleep(0.05); conn.sendall(b"x" * 1000)
    if n == 1:
        time.sleep(1.0)                              # the hiccup
        for _ in range(5):
            time.sleep(0.05); conn.sendall(b"x" * 1000)
        return                                       # ...and the server closes
    time.sleep(30)


srv, port, count = server(pause_then_close)
w = W.Watchers([f"http://127.0.0.1:{port}/radiozurnal_mp3_128.mp3"])
w.start()
assert wait_for(lambda: count["n"] >= 2), said
w.close()
assert b"GET /radiozurnal_mp3_128.mp3 HTTP/1.0" in count["req"] and b"Host: 127.0.0.1" in count["req"]
lines = [s for s in said if "127.0.0.1" in s]
assert lines[0].startswith("stream watch 127.0.0.1/radiozurnal_mp3_128.mp3: connected"), lines
assert sum("no data for" in s for s in lines) == 1, lines
pause = [s for s in lines if "data again after a" in s]
assert len(pause) == 1 and 0.9 <= float(pause[0].split("after a ")[1].split(" s")[0]) <= 1.6, pause
assert any("the server closed the connection after" in s for s in lines), lines
print("1) a pause is reported once with its length; a hang-up is reported and reconnected")
srv.close()

# 2) a stream that goes silent for GIVE_UP is dropped and opened again
said.clear()


def silent(conn, n):
    conn.sendall(b"HTTP/1.0 200 OK\r\n\r\n" + b"x" * 500)
    time.sleep(10)


srv, port, count = server(silent)
w = W.Watchers([f"http://127.0.0.1:{port}/x"])
w.start()
assert wait_for(lambda: count["n"] >= 2), said
w.close()
assert any("no data for 2 s — reconnecting" in s for s in said), said
print("2) a stream silent for GIVE_UP is dropped and reopened")
srv.close()

# 3) a refusal is logged once, not on every retry
said.clear()


def refuse(conn, n):
    conn.sendall(b"HTTP/1.0 404 Not Found\r\n\r\n")


srv, port, count = server(refuse)
w = W.Watchers([f"http://127.0.0.1:{port}/gone.mp3"])
w.start()
assert wait_for(lambda: count["n"] >= 4), said
w.close()
assert sum("cannot connect" in s for s in said) == 1, said
assert "404" in next(s for s in said if "cannot connect" in s)
print("3) a refused stream is logged once, retried quietly")
srv.close()

# 4) the hourly summary, and stopping
said.clear()
t = W.StreamWatch("https://rozhlas.stream/radiozurnal_mp3_128.mp3", threading.Event())
assert (t.tls, t.host, t.port, t.path) == (True, "rozhlas.stream", 443, "/radiozurnal_mp3_128.mp3")
t2 = W.StreamWatch("http://82.99.175.141/radiozurnal_mp3_128.mp3", threading.Event())
assert (t2.tls, t2.port, t2.label) == (False, 80, "82.99.175.141/radiozurnal_mp3_128.mp3")
t.bytes, t.gaps, t.longest, t.drops, t.slow = 57 * 1048576, 2, 3.25, 1, 1
t.summary()
assert said[-1] == ("stream watch rozhlas.stream/radiozurnal_mp3_128.mp3: 57 MB since the last "
                    "summary, 2 pause(s) of 0 s or more (longest 3.2 s), 1 slow stretch(es), "
                    "1 dropped connection(s)"), said
assert (t.bytes, t.gaps, t.drops, t.slow) == (0, 0, 0, 0)
print("4) the summary says how much came, how many pauses and drops, then starts over")

# 5) a typo in an address is refused up front — it used to raise and stop the whole add-on
for bad in ("http://82.99.175.141:8o/x.mp3", "http://h:99999/x", "http://[::1/x", "ftp://h/x",
            "http:///x", "rozhlas.stream/x"):
    assert W.check_url(bad), bad
for good in ("http://82.99.175.141/radiozurnal_mp3_128.mp3", "https://rozhlas.stream:443/a.mp3"):
    assert W.check_url(good) is None, good
print("5) malformed addresses are caught before anything starts")

# 6) a playlist address answers and hangs up at once: backed off and said once, not 1/s
said.clear()


def playlist(conn, n):
    conn.sendall(b"HTTP/1.0 200 OK" + CRLF + b"Content-Type: audio/x-scpls" + CRLF + CRLF +
                 b"[playlist]" + LF)


saved = W.BACKOFF
W.BACKOFF = (0.3, 0.6, 1.2)
srv, port, count = server(playlist)
w = W.Watchers([f"http://127.0.0.1:{port}/radio.pls"])
w.start()
time.sleep(3.0)
w.close()
W.BACKOFF = saved
assert count["n"] <= 6, f"{count['n']} requests in 3 s — not backed off"
assert sum("playlist" in s for s in said) == 1, said
print("6) an address that hangs up at once is backed off and explained once")
srv.close()

# 7) data that keeps coming but too slowly is reported (it starves a radio without any pause),
#    the summary comes on time during a healthy stream, and a GIVE_UP stall is counted
said.clear()
W.RATE_WINDOW, W.SUMMARY_EVERY = 0.5, 1.0


def trickle(conn, n):
    conn.sendall(b"ICY 200 OK" + CRLF + b"icy-br:128" + CRLF + CRLF)
    end = time.monotonic() + 1.2
    while time.monotonic() < end:                    # full rate first: 16 000 B/s
        conn.sendall(b"x" * 1600); time.sleep(0.1)
    end = time.monotonic() + 1.6
    while time.monotonic() < end:                    # then a third of it, never pausing 0.5 s
        conn.sendall(b"x" * 1600); time.sleep(0.3)
    end = time.monotonic() + 1.2
    while time.monotonic() < end:
        conn.sendall(b"x" * 1600); time.sleep(0.1)
    time.sleep(30)


srv, port, count = server(trickle)
w = W.Watchers([f"http://127.0.0.1:{port}/rz.mp3"])
w.start()
time.sleep(4.5)
w.close()
time.sleep(0.5)
lines = [s for s in said if "rz.mp3" in s]
assert any("128 kbps" in s and "connected to 127.0.0.1" in s for s in lines), lines
slow = [s for s in lines if "% of the stream's 128 kbps" in s]
assert len(slow) == 1, lines
assert any("back to full rate" in s for s in lines), lines
assert sum("since the last summary" in s for s in lines) >= 3, "a summary every second, mid-stream"
assert not any("no data for" in s for s in lines), "a trickle is not a pause"
print("7) slow delivery is reported once, its end too; summaries come mid-stream")
srv.close()

t = W.StreamWatch("http://127.0.0.1:1/x", threading.Event())


class Silent:
    def recv(self, n): raise socket.timeout()


W.GIVE_UP = 0.3
t.listen(Silent(), b"")
assert t.gaps == 1 and t.longest >= 0.3, (t.gaps, t.longest)
print("8) a stall long enough to give up on still counts as a pause")

print("\nALL OK")
