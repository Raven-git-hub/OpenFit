"""
The user's clock: what day it is where they are (TZ1).

The container's clock is UTC, and OpenFit's notion of "today" used to be
the container's - so for someone in Hong Kong it was still yesterday
until 08:00. Every "today" in app/ now comes from here, in the user's
timezone: the one setting `profile.timezone` in the settings table, an
IANA name, read and written only through GET/PUT /api/profile (main.py)
- never through the generic /api/settings/<key>, which refuses the
`profile.` prefix so nothing skips the check.

Unset, or unreadable (not a zone this OpenFit knows), it's UTC: a
request never fails over it, and a bad value is logged once.

    now_utc()                 the current moment, aware, in UTC - the one
                              place app/ asks the system for the time (a
                              test pins it here, and everything follows)
    user_zone(conn)           the user's ZoneInfo, UTC unless set
    user_today(conn)          today's date in it
    today_in(zone)            today's date in a zone the caller already has
    local_midnight(d, zone)   the moment day d begins in zone, as an aware
                              UTC datetime

Stored data doesn't move: dates stay dates and timestamps UTC. Only
which day counts as today - and a day's bounds, for a source that asks
by instant - follows the setting. A test (test_clock.py) holds every
other module in app/ off date.today(), datetime.today() and a bare
datetime.now(), so a new one can't slip back onto the server's clock.
"""

import functools
import sqlite3
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, available_timezones

# The settings row, and the zone it means when unset or unreadable.
TIMEZONE_KEY = "profile.timezone"
DEFAULT_ZONE = "UTC"

# Every key under it is the profile's - /api/profile's alone.
PROFILE_PREFIX = "profile."

# Names zoneinfo can list that aren't a place's zone: localtime is the
# machine's own (the clock this module is here to get away from) and
# Factory a placeholder. As the Admin's schedule leaves them out.
NOT_ZONES = frozenset({"localtime", "Factory"})

# The bad stored values already logged, so each is said once.
_logged = set()


def now_utc():
    """The current moment, as an aware UTC datetime."""
    return datetime.now(timezone.utc)


@functools.lru_cache(maxsize=1)
def zones():
    """The timezones the setting can name, sorted: the IANA database's
    (the tzdata package's, and the system's), less NOT_ZONES. Read once."""
    return tuple(sorted(available_timezones() - NOT_ZONES))


def is_zone(name):
    """Whether `name` is one of zones()."""
    return isinstance(name, str) and name in _zone_set()


@functools.lru_cache(maxsize=1)
def _zone_set():
    return frozenset(zones())


def stored_zone(conn):
    """The timezone the user has saved - an IANA name in zones() - or None
    when there's none, or what's stored isn't one (logged, once a value).
    Never raises over the setting: a database that can't be read counts
    as none."""
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (TIMEZONE_KEY,)
        ).fetchone()
    except sqlite3.Error as e:
        _log_once(("error", str(e)), f"couldn't read {TIMEZONE_KEY} ({e}), so using {DEFAULT_ZONE}")
        return None
    if row is None or row[0] is None:
        return None
    value = row[0]
    if not is_zone(value):
        _log_once(("value", value), f"{TIMEZONE_KEY} is {value!r}, which isn't a timezone, "
                                    f"so using {DEFAULT_ZONE}")
        return None
    return value


def user_zone(conn):
    """The user's timezone, as a ZoneInfo: the saved one, else UTC."""
    return ZoneInfo(stored_zone(conn) or DEFAULT_ZONE)


def today_in(zone):
    """Today's date in `zone` (a tzinfo)."""
    return now_utc().astimezone(zone).date()


def user_today(conn):
    """Today's date where the user is: their timezone setting, UTC if unset."""
    return today_in(user_zone(conn))


def local_midnight(d, zone):
    """The moment day `d` (a date) begins in `zone`, as an aware UTC datetime.

    Midnight in that zone - its first occurrence, where a clock going back
    repeats it - or, where a clock change skips midnight (or the whole
    day), the first moment that exists after it: the change itself.
    """
    wall = datetime.combine(d, time())
    # fold=0 is the earlier reading of a repeated time; in a gap it reads
    # the time with the offset before the change, which lands after it.
    first = wall.replace(tzinfo=zone).astimezone(timezone.utc)
    if first.astimezone(zone).replace(tzinfo=None) == wall:
        return first
    # Midnight doesn't exist. The change lies between the gap's two
    # readings - fold=1 reads it with the offset after, landing before
    # the change - and is on a whole second: the first one whose wall
    # time is midnight or later.
    lo = wall.replace(tzinfo=zone, fold=1).astimezone(timezone.utc)
    hi = first
    while hi - lo > timedelta(seconds=1):
        mid = lo + timedelta(seconds=(hi - lo) // timedelta(seconds=2))
        if mid.astimezone(zone).replace(tzinfo=None) >= wall:
            hi = mid
        else:
            lo = mid
    return hi


def _log_once(key, message):
    if key in _logged:
        return
    _logged.add(key)
    print(f"[clock] {message}")
