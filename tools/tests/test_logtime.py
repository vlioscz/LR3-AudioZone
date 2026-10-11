#!/usr/bin/env python3
"""Offline checks for logtime.py: the add-on log is in the site's local time, offset written.

    python tools/tests/test_logtime.py

Up to 0.5.3 every log line was UTC without saying so, because s6-overlay hides the TZ that
Home Assistant gives the add-on. Customer reports are in local time; the log has to be too.
"""
import logging
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "lr3_audiozone", "lr3ctl"))
import logtime as L  # noqa: E402

# TZ is found where s6 keeps it when the environment does not have it
d = tempfile.mkdtemp(prefix="s6env_")
saved = os.environ.pop("TZ", None)
assert L.container_env("TZ", (d,)) is None
open(os.path.join(d, "TZ"), "w").write("Europe/Prague\n")
assert L.container_env("TZ", (d,)) == "Europe/Prague"
os.environ["TZ"] = "UTC"
assert L.container_env("TZ", (d,)) == "UTC", "the environment wins when it has it"
if saved is None:
    os.environ.pop("TZ", None)
else:
    os.environ["TZ"] = saved
print("logtime: TZ is read from the environment, else from where s6 keeps it")

# the offset is written out, so a line stands on its own
rec = logging.LogRecord("x", logging.INFO, __file__, 1, "msg", None, None)
rec.created = 1760121685.0                          # 2025-10-10 18:41:25 UTC
stamp = L.LocalFormatter("%(asctime)s").formatTime(rec)
t = time.localtime(rec.created)
off = t.tm_gmtoff or 0
want = time.strftime("%Y-%m-%d %H:%M:%S", t) + ("+" if off >= 0 else "-") + \
    f"{abs(off) // 3600:02d}{abs(off) % 3600 // 60:02d}"
assert stamp == want and len(stamp) == 24, stamp
print(f"logtime: a line is stamped like {stamp}")

if hasattr(time, "tzset"):                          # POSIX only — the add-on's platform
    os.environ.pop("TZ", None)
    real = L.S6_ENV_DIRS
    L.S6_ENV_DIRS = (d,)
    try:
        assert L.container_env("TZ", L.S6_ENV_DIRS) == "Europe/Prague"
        os.environ["TZ"] = "Europe/Prague"; time.tzset()
        s = L.LocalFormatter("%(asctime)s").formatTime(rec)
        assert s == "2025-10-10 20:41:25+0200", s
        print("logtime: in Europe/Prague that line reads", s)
    finally:
        L.S6_ENV_DIRS = real
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()

print("\nALL OK")
