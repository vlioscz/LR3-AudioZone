#!/usr/bin/env python3
"""Offline checks for pacer.py's arithmetic. No librespot, no ffmpeg, no network.

    python tools/tests/test_pacer.py

The pacer is the clock of every zone when rate_match is on, so the two things that must never
break are: over time it hands over exactly rate × (1 + ppm/10⁶) frames a second, and it never
splits a stereo frame (which would turn the rest of the stream into noise).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "lr3_audiozone", "lr3ctl"))
import pacer as P  # noqa: E402

# --- rate: an hour at -3250 ppm, ticking every 20 ms with jitter -----------------------------
t = 1000.0
pc = P.Pacer(t, ppm=-3250)
total = 0
for i in range(3600 * 50):
    t += 0.02 + (0.004 if i % 7 == 0 else -0.0006)    # late wake-ups, paid back later
    n = pc.due(t)
    pc.took(n)
    total += n
elapsed = t - 1000.0
want = elapsed * 44100 * (1 - 3250e-6)
assert abs(total - want) < 44100 * 0.12, (total, want)
print(f"pacer: {elapsed:.0f} s at -3250 ppm -> {total} frames, {total - want:+.0f} off the ideal")

# --- a rate change re-anchors: nothing becomes due retroactively ------------------------------
pc = P.Pacer(0.0, ppm=0)
pc.took(pc.due(10.0))
pc.set_ppm(-5000, 10.0)
assert pc.due(10.0) == 0, "a new rate must apply from now, not from the start"
n = pc.due(10.05)
assert abs(n - 0.05 * 44100 * 0.995) <= 1, n
print("pacer: a new rate applies from the moment it is set")

# --- one call never hands over a slab, and a long stall is forgiven, not paid in silence ------
pc = P.Pacer(0.0)
assert pc.due(0.5) == int(P.MAX_STEP * 44100), "at most MAX_STEP per call"
pc = P.Pacer(0.0)
assert pc.due(5.0) == 0 and pc.skipped > 4.9, "five seconds asleep: skip, do not burst"
assert 0 < pc.due(5.02) < 44100 * 0.03
print("pacer: catch-up is capped per call, and a long stall is skipped rather than bursted")

# --- frames are never split ------------------------------------------------------------------
data = bytes(range(10))                 # 2.5 frames
chunk, carry, pad = P.take_frames(data, 12)
assert chunk == bytes(range(8)) + bytes(4) and carry == bytes([8, 9]) and pad == 4
chunk, carry, pad = P.take_frames(carry + bytes([10, 11, 12, 13, 14, 15]), 4)
assert chunk == bytes([8, 9, 10, 11]) and carry == bytes([12, 13, 14, 15]) and pad == 0
chunk, carry, pad = P.take_frames(b"", 8)
assert chunk == bytes(8) and pad == 8, "nothing from librespot = silence, the mount stays fed"
print("pacer: whole frames only; a partial one waits; nothing at all becomes silence")

# --- the rate file: unreadable keeps the current value, absurd values are clamped -------------
assert P.read_ppm("/nonexistent/lr3_rate_x", -3250.0) == -3250.0
pc = P.Pacer(0.0, ppm=1e9)
assert pc.ppm == P.PPM_LIMIT
print("pacer: a missing rate file keeps the rate; absurd ones are clamped")

print("\nALL OK")
