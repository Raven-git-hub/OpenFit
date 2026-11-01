"""
TZ1: the user's timezone - which day is today, wherever the server is.

OpenFit took "today" from the container's clock, which is UTC, so for
someone in Hong Kong it was still yesterday until 08:00. Every today in
app/ now comes from clock.py, in the user's timezone (the settings row
profile.timezone, an IANA name, set through PUT /api/profile -
test_profile.py), UTC while none is set:

  1. the clock: user_today() across zones at an instant Hong Kong and
     UTC disagree on, the UTC fallback, and local_midnight() in a zone
     and across clock changes;
  2. every today following it - the webhook's default date, a sync's
     re-derived window, ?days=, ctx.today(), Garmin's first day, and
     Google's sync day and local midnight - all at one pinned instant
     (the conftest's pin_clock, which holds clock.now_utc());
  3. the source test: nothing in app/ outside clock.py asks the system
     for a local date or time, so a new one can't slip back in;
  4. the ctx's timezone(), and APP_LEVEL 2.
"""

import json
import os
import re
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest

import apps
import clock
import host
from crypto import encrypt

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")

# 20:00 UTC on 1 November is 04:00 on 2 November in Hong Kong.
INSTANT = datetime(2026, 11, 1, 20, 0, tzinfo=timezone.utc)
HONG_KONG = "Asia/Hong_Kong"


def set_zone(conn, value):
    """The settings row as PUT /api/profile writes it - or anything else,
    to see what a bad one does."""
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (clock.TIMEZONE_KEY, value),
    )
    conn.commit()


@pytest.fixture(autouse=True)
def fresh_log(monkeypatch):
    """Each test starts with nothing logged yet."""
    monkeypatch.setattr(clock, "_logged", set())


# ---------- 1. the clock ----------


@pytest.mark.parametrize("zone, today", [
    ("Asia/Hong_Kong", date(2026, 11, 2)),
    ("UTC", date(2026, 11, 1)),
    ("America/Los_Angeles", date(2026, 11, 1)),
    ("Pacific/Kiritimati", date(2026, 11, 2)),   # UTC+14: 10:00 on the 2nd
    ("Pacific/Pago_Pago", date(2026, 11, 1)),    # UTC-11: 09:00 on the 1st
])
def test_user_today_is_the_date_in_the_users_zone(conn, pin_clock, zone, today):
    pin_clock(INSTANT)
    set_zone(conn, zone)

    assert clock.user_zone(conn) == ZoneInfo(zone)
    assert clock.user_today(conn) == today


def test_unset_is_utc_and_says_nothing(conn, pin_clock, capsys):
    pin_clock(INSTANT)

    assert clock.stored_zone(conn) is None
    assert clock.user_zone(conn) == ZoneInfo("UTC")
    assert clock.user_today(conn) == date(2026, 11, 1)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("bad", ["Mars/Olympus_Mons", "", "  ", "asia/hong_kong", "localtime",
                                 "Factory", "../../etc/passwd", "Asia/Hong_Kong ", '"Asia/Hong_Kong"'])
def test_an_unreadable_zone_is_utc_logged_once(conn, pin_clock, capsys, bad):
    pin_clock(INSTANT)
    set_zone(conn, bad)

    assert clock.user_zone(conn) == ZoneInfo("UTC")
    assert clock.user_today(conn) == date(2026, 11, 1)
    assert clock.user_today(conn) == date(2026, 11, 1)
    out = capsys.readouterr().out
    assert out == f"[clock] profile.timezone is {bad!r}, which isn't a timezone, so using UTC\n"


def test_a_database_that_cant_be_read_is_utc_too(tmp_path, pin_clock, capsys):
    """No settings table at all: still a today, never an error."""
    import sqlite3

    pin_clock(INSTANT)
    bare = sqlite3.connect(tmp_path / "bare.db")

    assert clock.user_today(bare) == date(2026, 11, 1)
    assert clock.user_today(bare) == date(2026, 11, 1)
    assert capsys.readouterr().out.count("[clock] couldn't read profile.timezone") == 1
    bare.close()


def test_zones_are_the_iana_names_less_the_machines_own():
    zones = clock.zones()
    assert list(zones) == sorted(zones)
    assert {"UTC", "Asia/Hong_Kong", "Europe/London", "America/New_York"} <= set(zones)
    assert "localtime" not in zones and "Factory" not in zones
    assert all(clock.is_zone(z) for z in zones)
    assert not clock.is_zone(None) and not clock.is_zone(8) and not clock.is_zone(["UTC"])


@pytest.mark.parametrize("zone, day, midnight", [
    # Hong Kong, UTC+8 all year: the 2nd begins at 16:00Z on the 1st.
    ("Asia/Hong_Kong", date(2026, 11, 2), "2026-11-01T16:00:00+00:00"),
    ("UTC", date(2026, 11, 2), "2026-11-02T00:00:00+00:00"),
    # London goes forward at 01:00 on 29 March: that day begins in GMT,
    # the next in BST.
    ("Europe/London", date(2026, 3, 29), "2026-03-29T00:00:00+00:00"),
    ("Europe/London", date(2026, 3, 30), "2026-03-29T23:00:00+00:00"),
    # Santiago goes forward at midnight on 6 September: 00:00 doesn't
    # exist, and the day begins at the change - 01:00 there, 04:00Z.
    ("America/Santiago", date(2026, 9, 6), "2026-09-06T04:00:00+00:00"),
    # Havana goes back at 01:00 on 1 November, so midnight comes twice:
    # the first, in daylight time.
    ("America/Havana", date(2026, 11, 1), "2026-11-01T04:00:00+00:00"),
    # Samoa skipped 30 December 2011 whole: the first moment there is on
    # or after it is midnight on the 31st, the change itself.
    ("Pacific/Apia", date(2011, 12, 30), "2011-12-30T10:00:00+00:00"),
])
def test_local_midnight_is_the_moment_the_day_begins_there(zone, day, midnight):
    got = clock.local_midnight(day, ZoneInfo(zone))
    assert got == datetime.fromisoformat(midnight)
    assert got.tzinfo == timezone.utc


# ---------- 2. every today follows it ----------
#
# Hong Kong, at INSTANT: 2 November there, still 1 November in UTC.

TODAY_HK = "2026-11-02"


@pytest.fixture
def in_hong_kong(conn, pin_clock):
    pin_clock(INSTANT)
    set_zone(conn, HONG_KONG)
    return conn


def test_the_webhooks_default_date(client, in_hong_kong):
    token = client.get("/api/webhook-token").get_json()["token"]

    resp = client.post(f"/api/webhook/{token}", json={"metric": "steps", "value": 4000})

    assert resp.status_code == 200
    assert resp.get_json()["stored"]["date"] == TODAY_HK
    assert [tuple(r) for r in in_hong_kong.execute("SELECT date, metric FROM derived_metrics")] == [
        (TODAY_HK, "steps"),
    ]


def test_a_syncs_rederived_window_starts_on_the_users_today(in_hong_kong, monkeypatch):
    import main

    seen = []
    monkeypatch.setattr(main, "recompute_derived", lambda conn, since=None, until=None: seen.append(since))

    class Plugin:
        def sync(self, conn, days):
            return 0

    main._sync(Plugin(), in_hong_kong, 1)
    main._sync(Plugin(), in_hong_kong, 3)

    assert seen == [TODAY_HK, "2026-10-31"]


def test_days_on_a_contract_read_is_the_users_trailing_days(client, in_hong_kong):
    for day, value in (("2026-11-02", 3), ("2026-11-01", 2), ("2026-10-31", 1)):
        in_hong_kong.execute(
            "INSERT INTO derived_metrics (date, metric, value, unit, source) VALUES (?, 'steps', ?, 'count', 'garmin')",
            (day, value),
        )
    in_hong_kong.commit()

    points = client.get("/api/metric/steps?days=1").get_json()["points"]
    assert [p["date"] for p in points] == [TODAY_HK]
    points = client.get("/api/metric/steps?days=2").get_json()["points"]
    assert [p["date"] for p in points] == ["2026-11-01", TODAY_HK]
    # trailing_window() itself is told the day: it has no clock of its own.
    assert host.trailing_window(2, date(2026, 11, 2)) == ("2026-11-01", TODAY_HK)


def make_app(root, app_id, py, requires=None):
    """An app folder under root, its app.py `py`; found as the host finds it."""
    folder = root / app_id
    folder.mkdir()
    level = f"requires: {requires}\n" if requires else ""
    (folder / "app.yaml").write_text(f"id: {app_id}\n{level}sizes: [{{w: 1, h: 1}}]\n")
    (folder / "app.py").write_text(py)
    return apps.find_app(str(root), app_id)


def test_ctx_today_and_its_trailing_days(in_hong_kong, tmp_path):
    in_hong_kong.execute(
        "INSERT INTO derived_metrics (date, metric, value, unit, source) VALUES "
        "('2026-11-01', 'steps', 1, 'count', 'garmin'), ('2026-11-02', 'steps', 2, 'count', 'garmin')"
    )
    in_hong_kong.commit()

    ctx = host.Ctx(in_hong_kong)
    assert ctx.today() == TODAY_HK
    assert [p.date for p in ctx.metric("steps", days=1)] == [TODAY_HK]

    # And what an app writes with no date lands on it.
    installed = make_app(tmp_path, "logger", "def render(ctx, cfg):\n    return ctx.empty('x')\n")
    write = host.WriteCtx(in_hong_kong, installed)
    write.write_metric("steps", 100)
    assert write.today() == TODAY_HK and write.written == [TODAY_HK]
    assert in_hong_kong.execute(
        "SELECT date FROM metrics WHERE source = 'app:logger'"
    ).fetchone()[0] == TODAY_HK


class RecordingGarmin:
    """Every Garmin call the sync makes, in order, answered with nothing."""

    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def call(*args, **kwargs):
            self.calls.append((name, args))
            return [] if name == "get_activities_by_date" else {}
        return call


def test_garmins_first_day_is_the_users_today(in_hong_kong, monkeypatch):
    from plugins import PLUGINS

    plugin = PLUGINS["garmin"]
    fake = RecordingGarmin()
    monkeypatch.setattr(plugin, "_get_client", lambda conn=None: fake)

    plugin.sync(in_hong_kong, 2)

    days = [args[0] for name, args in fake.calls if name == "get_stats"]
    assert days == [TODAY_HK, "2026-11-01"]


def test_googles_day_and_local_midnight_are_the_users(in_hong_kong, monkeypatch):
    from plugins import PLUGINS
    from test_google_series import FakeGoogle

    for var in ("GOOGLE_HEALTH_CLIENT_ID", "GOOGLE_HEALTH_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    in_hong_kong.execute(
        "INSERT INTO accounts (plugin_id, credentials, created_at) VALUES (?, ?, ?)",
        ("google_health", encrypt(json.dumps({
            "client_id": "cid", "client_secret": "secret", "refresh_token": "refresh-abc",
        })), "2026-01-01T00:00:00Z"),
    )
    in_hong_kong.commit()
    fake = FakeGoogle()
    monkeypatch.setattr("plugins.google_health.plugin.requests.post", fake.post)
    monkeypatch.setattr("plugins.google_health.plugin.requests.get", fake.get)

    PLUGINS["google_health"].sync(in_hong_kong, 1)

    # The daily metrics: Hong Kong's 2 November, the end the day after.
    steps = fake.requests_for("steps")[0]["range"]
    assert steps == {"start": {"date": {"year": 2026, "month": 11, "day": 2}},
                     "end": {"date": {"year": 2026, "month": 11, "day": 3}}}
    assert fake.requests_for("daily-resting-heart-rate")[0]["filter"] == (
        'daily_resting_heart_rate.date >= "2026-11-02" AND daily_resting_heart_rate.date < "2026-11-03"'
    )
    # The series: from midnight in Hong Kong - 16:00Z on the 1st - to now.
    assert fake.rollup_ranges() == [("2026-11-01T16:00:00Z", "2026-11-01T20:00:00Z")]


# ---------- 3. the source test ----------
#
# The system's local date and time are the server's, never the user's:
# date.today(), datetime.today() and a bare datetime.now() (no tz) read
# them. clock.py alone asks the system for the time, as UTC; everything
# else asks it.

LOCAL_CLOCK = re.compile(r"\bdate\.today\(|\bdatetime\.today\(|\bdatetime\.now\(\s*\)")


def app_sources():
    for folder, dirs, files in os.walk(APP_DIR):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            if name.endswith(".py"):
                yield os.path.join(folder, name)


def test_nothing_in_app_reads_the_servers_local_clock_but_clock_py():
    found = []
    for path in app_sources():
        if os.path.relpath(path, APP_DIR) == "clock.py":
            continue
        with open(path, encoding="utf-8") as f:
            for n, line in enumerate(f, 1):
                if LOCAL_CLOCK.search(line):
                    found.append(f"{os.path.relpath(path, APP_DIR)}:{n}: {line.strip()}")
    assert found == []


@pytest.mark.parametrize("line, caught", [
    ("today = date.today()", True),
    ("x = datetime.today()", True),
    ("now = datetime.now()", True),
    ("now = datetime.datetime.now( )", True),
    ("now = datetime.now(timezone.utc)", False),
    ("now = clock.now_utc()", False),
    ("today = user_today(conn)", False),
])
def test_the_source_test_catches_each_form(line, caught):
    assert bool(LOCAL_CLOCK.search(line)) is caught


def test_the_source_test_reads_every_module():
    names = {os.path.relpath(p, APP_DIR) for p in app_sources()}
    assert {"main.py", "host.py", "clock.py", os.path.join("plugins", "garmin", "plugin.py"),
            os.path.join("plugins", "google_health", "plugin.py")} <= names


# ---------- 4. apps: ctx.timezone(), and app level 2 ----------


def test_ctx_timezone_is_the_users_iana_name(conn):
    assert host.Ctx(conn).timezone() == "UTC"
    set_zone(conn, HONG_KONG)
    assert host.Ctx(conn).timezone() == HONG_KONG
    set_zone(conn, "not a zone")
    assert host.Ctx(conn).timezone() == "UTC"


def test_ctx_reads_the_setting_once_and_only_when_asked(conn):
    """A render that never asks the date never reads the setting; one
    that does reads it once."""
    ctx = host.Ctx(None)
    assert ctx.number(1)["value"] == 1

    ctx = host.Ctx(conn)
    set_zone(conn, HONG_KONG)
    assert ctx.timezone() == HONG_KONG
    set_zone(conn, "Europe/London")
    assert ctx.timezone() == HONG_KONG


def test_ctx_today_given_by_the_caller_stands(conn):
    set_zone(conn, HONG_KONG)
    assert host.Ctx(conn, today=date(2026, 1, 5)).today() == "2026-01-05"


RENDER = "def render(ctx, cfg):\n    return ctx.empty('x')\n"


def test_this_openfit_is_app_level_two_or_more_and_an_app_saying_nothing_still_loads(tmp_path):
    assert apps.APP_LEVEL >= 2
    plain = make_app(tmp_path, "plain", RENDER)
    needs = make_app(tmp_path, "needs", RENDER, requires=2)

    assert [a.id for a in apps.scan_apps(str(tmp_path))] == ["needs", "plain"]
    assert (plain.requires, needs.requires) == (1, 2)
    assert apps.level_problem(2) is None
    assert apps.level_problem(5) == "needs OpenFit app level 5; this OpenFit is level 4"


def test_an_app_reads_the_timezone_through_its_ctx(conn, tmp_path, pin_clock):
    pin_clock(INSTANT)
    set_zone(conn, HONG_KONG)
    installed = make_app(tmp_path, "zone", "def render(ctx, cfg):\n"
                                           "    return ctx.empty(ctx.timezone() + ' ' + ctx.today())\n",
                         requires=2)

    spec = host.render_app(installed, conn)

    assert spec == {"type": "empty", "message": "Asia/Hong_Kong 2026-11-02"}
