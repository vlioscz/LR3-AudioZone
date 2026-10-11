"""Log timestamps in the site's own time zone, with the offset written out.

Up to 0.5.3 every line of the add-on log was in UTC without saying so. Home Assistant hands
each add-on its time zone in `TZ`, but this image boots through s6-overlay, which starts our
process with a scrubbed environment and keeps the container's variables as files instead —
the same trap that hid the Supervisor token in 0.5.0. So Python fell back to UTC, and every
customer report ("it switched off at about two") had to be shifted by hand before it could be
matched against the log, two hours in summer and one in winter.

Now the zone is picked up from wherever it is, applied to this process (and so inherited by
everything it starts), and each line carries its offset — `2026-10-10 20:41:25+0200` — so a
line is unambiguous on its own, and a log that straddles the switch to winter time still adds
up. librespot keeps its own `…Z` (UTC) stamps inside its lines; those are marked as such.
"""
from __future__ import annotations

import logging
import os
import time

S6_ENV_DIRS = ("/run/s6/container_environment", "/var/run/s6/container_environment")


def container_env(name: str, dirs=S6_ENV_DIRS) -> str | None:
    """A variable from the container's environment, wherever s6 has put it."""
    if os.environ.get(name):
        return os.environ[name]
    for d in dirs:
        try:
            with open(os.path.join(d, name)) as f:
                value = f.read().strip()
        except OSError:
            continue
        if value:
            return value
    return None


def use_site_time_zone() -> str | None:
    """Make this process (and its children) run in the site's time zone. Returns its name."""
    tz = container_env("TZ")
    if tz and hasattr(time, "tzset"):
        os.environ["TZ"] = tz
        time.tzset()
    return tz


class LocalFormatter(logging.Formatter):
    """`%(asctime)s` as local time with the UTC offset appended: 2026-10-10 20:41:25+0200."""

    def formatTime(self, record, datefmt=None):
        t = time.localtime(record.created)
        off = t.tm_gmtoff or 0
        sign = "+" if off >= 0 else "-"
        off = abs(off)
        return (time.strftime("%Y-%m-%d %H:%M:%S", t)
                + f"{sign}{off // 3600:02d}{off % 3600 // 60:02d}")


def setup_logging(prefix: str = "lr3ctl") -> str | None:
    """The add-on's log format, in local time. Returns the time zone in use (None = UTC)."""
    tz = use_site_time_zone()
    handler = logging.StreamHandler()
    handler.setFormatter(LocalFormatter(f"[{prefix}] %(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    return tz
