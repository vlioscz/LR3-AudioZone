#!/usr/bin/env python3
"""One zone's audio when `rate_match` is on: librespot -> paced PCM -> ffmpeg -> Icecast.

It replaces Liquidsoap for that zone, for one reason: Liquidsoap emits audio in exact
wall-clock real time, and a LARA does not play in exact real time. Measured on a third-party
three-radio install on 2026-10-09 (0.4.7's backlog lines): at 48 kHz the radios take about
78 B/s less than the 24 000 B/s we send at 192 kbps, i.e. they run ~0.33 % slow. The surplus
first fills the radio's own buffer, then piles up in Icecast at ~12 s of audio per hour, and
after 2 h 16 min — to the minute, on two different days — Icecast reaches its queue limit and
drops the listener. That is the "30 s behind after an hour", and the dropout that follows it.
(At 44.1 kHz the same radios ran ~0.16 % *fast* and underran every 26 minutes instead.)

Nothing inside Liquidsoap 2.1 can be told to run slow: its clock sleeps to an absolute CPU
deadline, and every operator that changes speed still fills the same number of samples per
tick. The byte rate per wall-clock second is the one quantity that has to change, so this
process is the clock instead. It reads librespot's PCM at 44 100 × (1 + ppm/10⁶) frames a
second and hands it to an ffmpeg that has no clock of its own: ffmpeg resamples and encodes
whatever it is given as soon as it arrives, so the MP3 reaching Icecast runs at exactly our
rate. The controller watches the radios' buffers and steers `ppm` through a one-line file
(see Controller.update_rates). The pitch does not move — that is set by the radio's crystal,
as it always was; what changes is only how fast we ask Spotify for the next second of music.

What it keeps from radio.liq.tpl:
- librespot is reader-paced (it decodes faster than real time and blocks on a full pipe), so
  reading it at our rate is what sets the speed of Spotify's playback, and nothing piles up.
- When librespot writes nothing (paused, between sessions) we send digital silence, so the
  mount never drops: the radio must be able to fetch it the moment it is told to.
- librespot is restarted 3 s after it exits, like the old `; sleep 3` loop.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time

RATE_IN = 44100          # librespot always emits 44.1 kHz S16LE stereo, whatever we output
FRAME = 4                # bytes per stereo S16 frame
TICK = 0.02              # pacing granularity, seconds
MAX_STEP = 0.1           # most audio handed over in one go, seconds
FORGIVE = 1.0            # if we fall this far behind, skip ahead instead of bursting
PPM_LIMIT = 20000.0      # sanity bound on what the rate file may ask for
RATE_POLL = 1.0          # how often the rate file is re-read
LIBRESPOT_RESTART = 3.0  # same pause as the old `; sleep 3`
ICECAST_RETRY = 2.0
FFMPEG_RESTART = 2.0
# Audio queued for ffmpeg beyond this means it has stopped reading; restart it rather than
# grow without bound.
FFMPEG_STUCK = 2.0

logging.basicConfig(level=logging.INFO,
                    format="[lr3ctl] %(asctime)s %(levelname)s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("lr3.pacer")


class Pacer:
    """How many PCM frames are due by `now`, at rate × (1 + ppm/10⁶). Pure arithmetic.

    Due-ness is measured against an anchor, not accumulated tick by tick, so a late wake-up is
    paid back by the next one and timing error cannot build up — the same reason Liquidsoap's
    own clock was never the problem.
    """

    def __init__(self, now: float, ppm: float = 0.0, rate: int = RATE_IN):
        self.rate = rate
        self.ppm = self._clamp(ppm)
        self.emitted = 0
        self.skipped = 0.0          # seconds forgiven after stalls, for the log
        self._t0, self._n0 = now, 0

    @staticmethod
    def _clamp(ppm: float) -> float:
        return max(-PPM_LIMIT, min(PPM_LIMIT, float(ppm)))

    def _target(self, now: float) -> float:
        return self._n0 + (now - self._t0) * self.rate * (1.0 + self.ppm / 1e6)

    def set_ppm(self, ppm: float, now: float):
        ppm = self._clamp(ppm)
        if ppm != self.ppm:
            # Re-anchor where the OLD rate had got to by now, so the new one applies from here
            # on without rewriting the past. 0.5.0 anchored at `emitted` instead, which quietly
            # forgave whatever had come due since the last tick — ~25 ms, every time the
            # controller nudged the rate, i.e. every 5 s. That is 0.5 % of the output, and it
            # is exactly what the first site showed: the loop settled at "+600 ppm" while
            # Icecast counted 23 885 B/s actually leaving (-4800).
            self._t0, self._n0 = now, self._target(now)
            self.ppm = ppm

    def due(self, now: float) -> int:
        behind = self._target(now) - self.emitted
        if behind > FORGIVE * self.rate:
            # We were not scheduled for a while (a stalled disk, a busy box). Paying the whole
            # debt at once would be mostly silence — librespot keeps only ~0.4 s in its pipe —
            # so let it go and carry on from here. The radio's buffer absorbs the hole.
            self.skipped += behind / self.rate
            self._t0, self._n0 = now, self.emitted
            return 0
        return max(0, min(int(behind), int(MAX_STEP * self.rate)))

    def took(self, frames: int):
        self.emitted += frames


def take_frames(data: bytes, want: int) -> tuple[bytes, bytes, int]:
    """Split `want` bytes of whole frames off `data`, padding with silence if it is short.

    Returns (chunk, carry, padded_bytes). Never splits a frame: a partial frame stays in the
    carry, otherwise every sample after it would be read a byte out of phase — which is
    full-scale noise, not a glitch.
    """
    usable = min(len(data) - len(data) % FRAME, want)
    chunk = data[:usable]
    pad = want - usable
    return (chunk + bytes(pad) if pad else chunk), data[usable:], pad


def read_ppm(path: str, default: float) -> float:
    try:
        with open(path) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return default


class IcecastSender(threading.Thread):
    """Copies ffmpeg's MP3 into an Icecast source connection, reconnecting when it drops.

    Done here rather than with ffmpeg's own icecast:// output so that the source password never
    has to appear in a URL — ffmpeg echoes URLs into its error messages, and those go to the
    add-on log — and so that Icecast going away costs a reconnect, not an encoder restart.
    """

    def __init__(self, cfg: dict, stop: threading.Event):
        super().__init__(daemon=True)
        self.cfg = cfg
        self.stop = stop
        self.src = None             # ffmpeg stdout, set by the main loop
        self.sock: socket.socket | None = None
        self._warned = False

    def connect(self) -> bool:
        c = self.cfg
        auth = base64.b64encode(f"source:{c['source_password']}".encode()).decode()
        head = (f"SOURCE /{c['mount']} HTTP/1.0\r\n"
                f"Authorization: Basic {auth}\r\n"
                f"User-Agent: lr3-pacer\r\n"
                f"Content-Type: audio/mpeg\r\n"
                f"ice-name: {c['name']}\r\n"
                f"ice-description: LR3 AudioZone\r\n"
                f"ice-genre: Various\r\n"
                f"ice-public: 0\r\n\r\n")
        try:
            s = socket.create_connection(("127.0.0.1", int(c["port"])), timeout=5)
            s.sendall(head.encode("utf-8"))
            status = s.recv(256).split(b"\r\n", 1)[0]
            if b" 200" not in status:
                raise OSError(f"Icecast answered {status!r}")
            s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            s.settimeout(10)
        except OSError as e:
            if not self._warned:
                self._warned = True
                log.warning("[pacer %s] cannot connect to Icecast as a source (%s) — retrying",
                            c["mount"], e)
            return False
        if self._warned:
            log.info("[pacer %s] connected to Icecast", c["mount"])
        self._warned = False
        self.sock = s
        return True

    def run(self):
        retry_at = 0.0
        while not self.stop.is_set():
            src = self.src
            if src is None:
                time.sleep(0.1)
                continue
            try:
                data = os.read(src.fileno(), 4096)
            except (OSError, ValueError):
                data = b""
            if not data:
                # ffmpeg exited; the main loop restarts it. Only forget *this* pipe — a new
                # one may already have been handed over while we sat in read().
                if self.src is src:
                    self.src = None
                continue
            if self.sock is None:
                # While Icecast is unreachable the audio is dropped, but ffmpeg is still
                # drained: a full pipe would stall the encoder, the pacing behind it, and
                # librespot behind that.
                now = time.monotonic()
                if now < retry_at:
                    continue
                if not self.connect():
                    retry_at = now + ICECAST_RETRY
                    continue
            try:
                self.sock.sendall(data)
            except OSError:
                log.warning("[pacer %s] Icecast dropped the source connection — reconnecting",
                            self.cfg["mount"])
                self.close()

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None


def pump_stderr(stream, prefix: str):
    for raw in iter(stream.readline, b""):
        line = raw.decode("utf-8", "replace").rstrip()
        if line:
            log.info("%s %s", prefix, line)


class Pipeline:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.mount = cfg["mount"]
        self.stop = threading.Event()
        self.librespot: subprocess.Popen | None = None
        self.ffmpeg: subprocess.Popen | None = None
        self.sender = IcecastSender(cfg, self.stop)
        self.ls_next = 0.0
        self.ff_next = 0.0
        self.carry = b""
        self.pending = bytearray()
        self.silent_since: float | None = None

    # --- children ----------------------------------------------------------------
    def start_librespot(self):
        c = self.cfg
        env = dict(os.environ, LR3_MOUNT=self.mount)
        try:
            err = open(c["librespot_log"], "ab")
        except OSError:
            err = subprocess.DEVNULL
        try:
            self.librespot = subprocess.Popen(c["librespot"], stdout=subprocess.PIPE,
                                              stdin=subprocess.DEVNULL, stderr=err, env=env)
            os.set_blocking(self.librespot.stdout.fileno(), False)
        except OSError as e:
            log.error("[pacer %s] cannot start librespot: %s", self.mount, e)
            self.librespot = None
        finally:
            if err is not subprocess.DEVNULL:
                err.close()
        self.carry = b""

    def start_ffmpeg(self):
        c = self.cfg
        cmd = ["ffmpeg", "-hide_banner", "-nostats", "-loglevel", "warning",
               # -ch_layout rather than -ac: same thing, minus a "Guessed Channel Layout"
               # warning in the add-on log every time a zone starts (ffmpeg 5.1, bookworm).
               "-f", "s16le", "-ar", str(RATE_IN), "-ch_layout", "stereo", "-i", "pipe:0",
               "-ar", str(c["samplerate"]), "-ac", "2",
               "-c:a", "libmp3lame", "-b:a", f"{int(c['bitrate'])}k",
               # An endless stream: no Xing/LAME length header and no ID3 tag at its start.
               "-write_xing", "0", "-id3v2_version", "0",
               # Hand over every frame as soon as it is encoded; the default 32 KB output
               # buffer would be 1.4 s of extra latency at 192 kbps.
               "-flush_packets", "1",
               "-f", "mp3", "pipe:1"]
        try:
            self.ffmpeg = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                           stderr=subprocess.PIPE)
        except OSError as e:
            log.error("[pacer %s] cannot start ffmpeg: %s", self.mount, e)
            self.ffmpeg = None
            return
        os.set_blocking(self.ffmpeg.stdin.fileno(), False)
        threading.Thread(target=pump_stderr, args=(self.ffmpeg.stderr, f"[ffmpeg {self.mount}]"),
                         daemon=True).start()
        self.sender.src = self.ffmpeg.stdout
        self.pending.clear()

    @staticmethod
    def kill(proc: subprocess.Popen | None):
        if proc is None:
            return
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
            except OSError:
                pass
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if pipe is not None:
                    pipe.close()
            except OSError:
                pass

    # --- one pass ------------------------------------------------------------------
    def read_librespot(self, n: int) -> bytes:
        if self.librespot is None:
            return b""
        try:
            data = os.read(self.librespot.stdout.fileno(), n)
        except BlockingIOError:
            return b""
        except OSError:
            data = b""
        if data:
            return data
        rc = self.librespot.poll()
        if rc is None:
            return b""
        log.warning("[pacer %s] librespot exited (%s) — restarting in %.0fs",
                    self.mount, rc, LIBRESPOT_RESTART)
        self.kill(self.librespot)
        self.librespot = None
        self.ls_next = time.monotonic() + LIBRESPOT_RESTART
        return b""

    def feed_ffmpeg(self):
        if self.ffmpeg is None or not self.pending:
            return
        try:
            n = os.write(self.ffmpeg.stdin.fileno(), self.pending)
        except BlockingIOError:
            n = 0
        except OSError:
            n = -1
        if n > 0:
            del self.pending[:n]
        stuck = len(self.pending) > FFMPEG_STUCK * RATE_IN * FRAME
        if n < 0 or stuck or self.ffmpeg.poll() is not None:
            log.warning("[pacer %s] ffmpeg %s — restarting in %.0fs", self.mount,
                        "stopped reading" if stuck else f"exited ({self.ffmpeg.poll()})",
                        FFMPEG_RESTART)
            self.kill(self.ffmpeg)
            self.ffmpeg = None
            self.sender.src = None
            self.pending.clear()
            self.ff_next = time.monotonic() + FFMPEG_RESTART

    def run(self):
        c = self.cfg
        rate_file = c["rate_file"]
        pacer = Pacer(time.monotonic(), read_ppm(rate_file, float(c.get("initial_ppm", 0.0))))
        log.info("[pacer %s] pacing Spotify for %r at %+.0f ppm", self.mount, c["name"],
                 pacer.ppm)
        self.sender.start()
        next_poll = 0.0
        reported_skip = 0.0
        while not self.stop.is_set():
            now = time.monotonic()
            if self.librespot is None and now >= self.ls_next:
                self.start_librespot()
            if self.ffmpeg is None and now >= self.ff_next:
                self.start_ffmpeg()
            if now >= next_poll:
                next_poll = now + RATE_POLL
                pacer.set_ppm(read_ppm(rate_file, pacer.ppm), now)
                if pacer.skipped - reported_skip >= 0.5:
                    log.info("[pacer %s] was not scheduled for a while — skipped %.1fs ahead",
                             self.mount, pacer.skipped - reported_skip)
                    reported_skip = pacer.skipped
            frames = pacer.due(now)
            if frames:
                want = frames * FRAME
                data = self.carry
                if len(data) < want:
                    data += self.read_librespot(want - len(data))
                chunk, self.carry, _pad = take_frames(data, want)
                pacer.took(frames)
                if self.ffmpeg is not None:
                    self.pending += chunk
            self.feed_ffmpeg()
            time.sleep(max(0.0, TICK - (time.monotonic() - now)))

    def shutdown(self):
        self.stop.set()
        self.kill(self.librespot)
        self.kill(self.ffmpeg)
        self.sender.close()


def main(argv: list[str]) -> int:
    with open(argv[1], encoding="utf-8") as f:
        cfg = json.load(f)
    pipe = Pipeline(cfg)

    def on_term(*_):
        pipe.stop.set()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    try:
        pipe.run()
    finally:
        pipe.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
