"""
Sleep sessions from the real plugins, with the network stubbed.

Both plugins already fetch sleep to file the daily sleep_minutes reading;
the same response carries the night as an interval with its stage
breakdown, which they now also write as a `sleep` session. These tests
feed each plugin a response in its provider's documented shape and pin
both halves of the dual track: the daily total exactly as before, and
the session beside it.

The shapes are what Garmin's dailySleepDTO and the Google Health API v4
Sleep resource look like on paper. They still need checking against a
real account - which is why the defensive cases matter as much as the
happy path: a field that isn't where it's expected must cost the
session at most, never the metric or the sync.

No network: the Garmin client is a fake handed to sync(), and Google's
HTTP calls are stubbed the way test_oauth_connector.py stubs the token
exchange.
"""

import copy
import json
from datetime import date

import pytest

from crypto import encrypt
from metrics import SLEEP_MINUTES


class FrozenDate(date):
    """date, with today pinned to the morning after the fixture night."""

    @classmethod
    def today(cls):
        return cls(2026, 1, 2)


def sleep_readings(conn, source):
    return [
        tuple(r) for r in conn.execute(
            "SELECT date, value FROM metrics WHERE source = ? AND metric = ? ORDER BY date",
            (source, SLEEP_MINUTES),
        )
    ]


def sleep_sessions(conn, source):
    return [
        (r[0], r[1], r[2], json.loads(r[3]))
        for r in conn.execute(
            'SELECT id, "start", "end", summary_json FROM sessions '
            "WHERE source = ? AND kind = 'sleep' ORDER BY \"start\"",
            (source,),
        )
    ]


# ---------- Garmin ----------


# get_sleep_data() for 2026-01-02: the night of the 1st into the 2nd,
# trimmed of the per-minute arrays. Timestamps are epoch milliseconds;
# the *GMT pair is real UTC, the *Local pair is shifted (here to UTC+1).
GARMIN_NIGHT = {
    "dailySleepDTO": {
        "id": 1767306600000,
        "userProfilePK": 12345678,
        "calendarDate": "2026-01-02",
        "sleepTimeSeconds": 26580,          # 443 min asleep
        "napTimeSeconds": 0,
        "sleepWindowConfirmed": True,
        "sleepWindowConfirmationType": "enhanced_confirmed_final",
        "sleepStartTimestampGMT": 1767306600000,    # 2026-01-01T22:30:00Z
        "sleepEndTimestampGMT": 1767334800000,      # 2026-01-02T06:20:00Z
        "sleepStartTimestampLocal": 1767310200000,
        "sleepEndTimestampLocal": 1767338400000,
        "unmeasurableSleepSeconds": 0,
        "deepSleepSeconds": 5460,           # 91 min
        "lightSleepSeconds": 15300,         # 255 min
        "remSleepSeconds": 5820,            # 97 min
        "awakeSleepSeconds": 1620,          # 27 min
        "deviceRemCapable": True,
        "retro": False,
        "sleepFromDevice": True,
        "averageRespirationValue": 14.0,
        "awakeCount": 2,
        "sleepScores": {"overall": {"value": 82, "qualifierKey": "GOOD"}},
    },
    "sleepMovement": [],
    "remSleepData": True,
    "sleepLevels": [],
    "restlessMomentsCount": 31,
}

GARMIN_START = "2026-01-01T22:30:00Z"
GARMIN_END = "2026-01-02T06:20:00Z"
GARMIN_SUMMARY = {
    "asleep_minutes": 443,
    "light_minutes": 255,
    "deep_minutes": 91,
    "rem_minutes": 97,
    "awake_minutes": 27,
}


class FakeGarmin:
    """Stands in for garminconnect.Garmin: canned stats and sleep by date,
    and no activities (workouts are test_workout_sessions.py's)."""

    def __init__(self, sleep_by_date, stats_by_date=None):
        self.sleep_by_date = sleep_by_date
        self.stats_by_date = stats_by_date or {}

    def get_stats(self, cdate):
        return self.stats_by_date.get(cdate, {})

    def get_sleep_data(self, cdate):
        # A night Garmin has nothing for still comes back as a DTO, with
        # the sleep fields empty.
        return self.sleep_by_date.get(
            cdate, {"dailySleepDTO": {"calendarDate": cdate, "sleepTimeSeconds": None}}
        )

    def get_activities_by_date(self, startdate, enddate=None):
        return []


@pytest.fixture
def garmin(monkeypatch):
    """The real Garmin plugin, syncing from a FakeGarmin on 2026-01-02."""
    from plugins import PLUGINS

    plugin = PLUGINS["garmin"]
    monkeypatch.setattr("plugins.garmin.plugin.date", FrozenDate)

    def sync_with(conn, sleep, days=1, stats=None):
        monkeypatch.setattr(plugin, "_get_client", lambda conn=None: FakeGarmin(sleep, stats))
        return plugin.sync(conn, days)

    return sync_with


def garmin_night(**dto_changes):
    """GARMIN_NIGHT for 2026-01-02 with some dailySleepDTO fields replaced
    (a value of ... removes the field)."""
    night = copy.deepcopy(GARMIN_NIGHT)
    for key, value in dto_changes.items():
        if value is ...:
            night["dailySleepDTO"].pop(key)
        else:
            night["dailySleepDTO"][key] = value
    return {"2026-01-02": night}


def test_garmin_writes_the_night_as_a_session_beside_sleep_minutes(conn, garmin):
    stats = {"2026-01-02": {"totalSteps": 9000, "restingHeartRate": 52}}

    assert garmin(conn, garmin_night(), stats=stats) == 1

    # The daily track, exactly as before: seconds asleep in whole minutes.
    assert sleep_readings(conn, "garmin") == [("2026-01-02", 443)]
    assert [tuple(r) for r in conn.execute(
        "SELECT metric, value FROM metrics WHERE source = 'garmin' ORDER BY metric"
    )] == [("resting_hr_bpm", 52), ("sleep_minutes", 443), ("steps", 9000)]
    # ...and the night itself, from the GMT timestamps and stage seconds.
    assert sleep_sessions(conn, "garmin") == [
        (f"garmin:sleep:{GARMIN_START}", GARMIN_START, GARMIN_END, GARMIN_SUMMARY)
    ]


def test_garmin_resync_of_the_same_night_upserts(conn, garmin):
    garmin(conn, garmin_night(sleepTimeSeconds=25000, deepSleepSeconds=4000))
    garmin(conn, garmin_night())

    assert sleep_readings(conn, "garmin") == [("2026-01-02", 443)]
    assert sleep_sessions(conn, "garmin") == [
        (f"garmin:sleep:{GARMIN_START}", GARMIN_START, GARMIN_END, GARMIN_SUMMARY)
    ]


def test_garmin_night_without_rem_leaves_the_stage_out(conn, garmin):
    """A watch without REM tracking reports no remSleepSeconds."""
    garmin(conn, garmin_night(remSleepSeconds=None, deviceRemCapable=False))

    summary = sleep_sessions(conn, "garmin")[0][3]
    assert "rem_minutes" not in summary
    assert summary["deep_minutes"] == 91


def test_garmin_without_stage_fields_still_writes_the_session(conn, garmin, capsys):
    garmin(conn, garmin_night(
        deepSleepSeconds=..., lightSleepSeconds=..., remSleepSeconds=..., awakeSleepSeconds=...,
    ))

    assert sleep_readings(conn, "garmin") == [("2026-01-02", 443)]
    assert sleep_sessions(conn, "garmin") == [
        (f"garmin:sleep:{GARMIN_START}", GARMIN_START, GARMIN_END, {"asleep_minutes": 443})
    ]
    assert "no stage fields" in capsys.readouterr().out


@pytest.mark.parametrize("start", [..., None, "2026-01-01T22:30:00Z"])
def test_garmin_without_a_usable_start_keeps_the_metric(conn, garmin, capsys, start):
    """No interval, no session - but the daily total is still filed."""
    garmin(conn, garmin_night(sleepStartTimestampGMT=start))

    assert sleep_readings(conn, "garmin") == [("2026-01-02", 443)]
    assert sleep_sessions(conn, "garmin") == []
    assert "no session written" in capsys.readouterr().out


def test_garmin_without_an_end_still_writes_the_session(conn, garmin):
    garmin(conn, garmin_night(sleepEndTimestampGMT=None))

    assert sleep_sessions(conn, "garmin") == [
        (f"garmin:sleep:{GARMIN_START}", GARMIN_START, None, GARMIN_SUMMARY)
    ]


def test_garmin_night_with_no_sleep_writes_neither(conn, garmin):
    """Nothing recorded (the 1st here): no reading and no session."""
    garmin(conn, garmin_night(), days=2)

    assert sleep_readings(conn, "garmin") == [("2026-01-02", 443)]
    assert len(sleep_sessions(conn, "garmin")) == 1


# ---------- Google Health ----------


# GET dataTypes/sleep/dataPoints: one night with stages and an afternoon
# nap (CLASSIC sleep, no stages) on the same civil day. int64 fields
# arrive as JSON strings, as Google sends them.
GOOGLE_NIGHT = {
    "sleep": {
        "interval": {
            "startTime": "2026-01-01T22:41:00Z",
            "startUtcOffset": "0s",
            "endTime": "2026-01-02T06:35:00.000Z",
            "endUtcOffset": "0s",
            "civilStartTime": {
                "date": {"year": 2026, "month": 1, "day": 1},
                "time": {"hours": 22, "minutes": 41},
            },
            "civilEndTime": {
                "date": {"year": 2026, "month": 1, "day": 2},
                "time": {"hours": 6, "minutes": 35},
            },
        },
        "type": "STAGES",
        "stages": [
            {"type": "AWAKE", "startTime": "2026-01-01T22:41:00Z", "endTime": "2026-01-01T22:50:00Z"},
            {"type": "LIGHT", "startTime": "2026-01-01T22:50:00Z", "endTime": "2026-01-01T23:30:00Z"},
        ],
        "metadata": {
            "processed": True,
            "mainSleep": True,
            "nap": False,
            "manuallyEdited": False,
            "stagesStatus": "SUCCEEDED",
        },
        "summary": {
            "minutesInSleepPeriod": "474",
            "minutesAsleep": "421",
            "minutesAwake": "53",
            "minutesToFallAsleep": "0",
            "minutesAfterWakeUp": "0",
            "stagesSummary": [
                {"type": "AWAKE", "minutes": "53", "count": "21"},
                {"type": "LIGHT", "minutes": "243", "count": "24"},
                {"type": "DEEP", "minutes": "84", "count": "5"},
                {"type": "REM", "minutes": "94", "count": "6"},
            ],
        },
    },
}

GOOGLE_NAP = {
    "sleep": {
        "interval": {
            "startTime": "2026-01-01T14:05:00Z",
            "startUtcOffset": "0s",
            "endTime": "2026-01-01T14:52:00Z",
            "endUtcOffset": "0s",
            "civilStartTime": {"date": {"year": 2026, "month": 1, "day": 1}},
            "civilEndTime": {"date": {"year": 2026, "month": 1, "day": 1}},
        },
        "type": "CLASSIC",
        "metadata": {"processed": True, "mainSleep": False, "nap": True},
        "summary": {
            "minutesInSleepPeriod": "47",
            "minutesAsleep": "44",
            "minutesAwake": "3",
            "stagesSummary": [
                {"type": "ASLEEP", "minutes": "44", "count": "1"},
                {"type": "AWAKE", "minutes": "3", "count": "1"},
            ],
        },
    },
}

NIGHT_START = "2026-01-01T22:41:00Z"
NIGHT_END = "2026-01-02T06:35:00Z"
NIGHT_SUMMARY = {
    "asleep_minutes": 421,
    "light_minutes": 243,
    "deep_minutes": 84,
    "rem_minutes": 94,
    "awake_minutes": 53,
}
NAP_START = "2026-01-01T14:05:00Z"
NAP_SESSION = (
    f"google_health:sleep:{NAP_START}", NAP_START, "2026-01-01T14:52:00Z",
    {"asleep_minutes": 44, "awake_minutes": 3},
)


@pytest.fixture
def google(conn, monkeypatch):
    """The real Google Health plugin, its HTTP calls stubbed, syncing on
    2026-01-02.

    The token refresh answers with an access token; the sleep list
    answers with the data points the test passes, and records the query
    params it was asked with in google.sleep_requests; steps and resting
    HR come back empty, so sleep is all that's written.
    """
    from plugins import PLUGINS

    monkeypatch.setattr("plugins.google_health.plugin.date", FrozenDate)
    for var in ("GOOGLE_HEALTH_CLIENT_ID", "GOOGLE_HEALTH_CLIENT_SECRET"):
        monkeypatch.delenv(var, raising=False)
    conn.execute(
        "INSERT INTO accounts (plugin_id, credentials, created_at) VALUES (?, ?, ?)",
        ("google_health", encrypt(json.dumps({
            "client_id": "cid", "client_secret": "secret", "refresh_token": "refresh-abc",
        })), "2026-01-01T00:00:00Z"),
    )
    conn.commit()

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            pass

    sleep_points = []
    sleep_requests = []

    def fake_post(url, data=None, json=None, headers=None, timeout=None):
        if "oauth2" in url:
            return FakeResponse({"access_token": "access-xyz"})
        return FakeResponse({"rollupDataPoints": []})

    def fake_get(url, headers=None, params=None, timeout=None):
        if "/dataTypes/sleep/" in url:
            sleep_requests.append(params)
            return FakeResponse({"dataPoints": sleep_points})
        return FakeResponse({"dataPoints": []})

    monkeypatch.setattr("plugins.google_health.plugin.requests.post", fake_post)
    monkeypatch.setattr("plugins.google_health.plugin.requests.get", fake_get)

    def sync_with(*points, days=7):
        sleep_points[:] = points
        return PLUGINS["google_health"].sync(conn, days)

    sync_with.sleep_requests = sleep_requests
    return sync_with


def google_night(**summary_changes):
    """GOOGLE_NIGHT with some summary fields replaced (... removes one)."""
    night = copy.deepcopy(GOOGLE_NIGHT)
    for key, value in summary_changes.items():
        if value is ...:
            night["sleep"]["summary"].pop(key)
        else:
            night["sleep"]["summary"][key] = value
    return night


def test_google_writes_each_sleep_as_a_session_beside_sleep_minutes(conn, google):
    assert google(GOOGLE_NIGHT, GOOGLE_NAP) == 1

    # The daily track, exactly as before: minutesAsleep summed per civil
    # start day, night and nap together.
    assert sleep_readings(conn, "google_health") == [("2026-01-01", 421 + 44)]
    # ...and each sleep as its own session, stages mapped to minutes.
    assert sleep_sessions(conn, "google_health") == [
        NAP_SESSION,
        (f"google_health:sleep:{NIGHT_START}", NIGHT_START, NIGHT_END, NIGHT_SUMMARY),
    ]


def test_google_asks_for_sleep_by_its_civil_end_date(conn, google):
    """The discovery doc rules sleep out of interval.civil_start_time -
    the filter this used to send, which got nothing back from a real
    account - and lists interval.civil_end_time for it instead."""
    google(GOOGLE_NIGHT, GOOGLE_NAP, days=7)

    # A 7-day sync on 2026-01-02 starts on 2025-12-27.
    assert google.sleep_requests == [
        {"filter": 'sleep.interval.civil_end_time >= "2025-12-27"'}
    ]
    # Asked the documented way, the same realistic answer still files the
    # daily total and both sessions.
    assert sleep_readings(conn, "google_health") == [("2026-01-01", 421 + 44)]
    assert sleep_sessions(conn, "google_health") == [
        NAP_SESSION,
        (f"google_health:sleep:{NIGHT_START}", NIGHT_START, NIGHT_END, NIGHT_SUMMARY),
    ]


def google_sleep(start, end, asleep):
    """A bare sleep data point from start to end - RFC 3339 UTC, with the
    same civil times - and `asleep` minutes asleep."""
    def civil(stamp):
        d = date.fromisoformat(stamp[:10])
        return {"date": {"year": d.year, "month": d.month, "day": d.day}}

    return {"sleep": {
        "interval": {
            "startTime": start, "endTime": end,
            "civilStartTime": civil(start), "civilEndTime": civil(end),
        },
        "type": "CLASSIC",
        "summary": {"minutesAsleep": str(asleep)},
    }}


def session_of(point):
    interval = point["sleep"]["interval"]
    asleep = int(point["sleep"]["summary"]["minutesAsleep"])
    return (f"google_health:sleep:{interval['startTime']}", interval["startTime"],
            interval["endTime"], {"asleep_minutes": asleep})


# The day before a 2-day sync on 2026-01-02, which starts on the 1st: the
# night that began that evening ends inside the window, so asking by end
# date brings it back; the nap that afternoon stands in for anything else
# from that day a looser answer might carry.
PRE_WINDOW_NIGHT = google_sleep("2025-12-31T23:10:00Z", "2026-01-01T07:02:00Z", 452)
PRE_WINDOW_NAP = google_sleep("2025-12-31T15:20:00Z", "2025-12-31T15:58:00Z", 35)
# ...and a nap on the window's second day, today.
TODAY_NAP = google_sleep("2026-01-02T13:10:00Z", "2026-01-02T13:40:00Z", 28)


def test_google_drops_sleep_that_began_before_the_window(conn, google):
    """Filtered by end date, the answer runs back past the window's first
    day. What began before it is dropped from the daily total and the
    sessions both; every day inside keeps its full total and sessions."""
    google(TODAY_NAP, GOOGLE_NIGHT, GOOGLE_NAP, PRE_WINDOW_NIGHT, PRE_WINDOW_NAP, days=2)

    assert google.sleep_requests == [
        {"filter": 'sleep.interval.civil_end_time >= "2026-01-01"'}
    ]
    # Nothing for the 31st - neither a total nor a session...
    assert sleep_readings(conn, "google_health") == [
        ("2026-01-01", 421 + 44), ("2026-01-02", 28),
    ]
    assert sleep_sessions(conn, "google_health") == [
        NAP_SESSION,
        (f"google_health:sleep:{NIGHT_START}", NIGHT_START, NIGHT_END, NIGHT_SUMMARY),
        session_of(TODAY_NAP),
    ]


def test_a_day_that_leaves_the_window_keeps_its_full_total(conn, google):
    """The daily sync this guards: a day's total, written whole while it
    was inside the window, isn't cut back to its night alone once the
    window moves past it."""
    # A 7-day sync has all of the 1st: its night and its afternoon nap.
    google(GOOGLE_NIGHT, GOOGLE_NAP, days=7)
    # A 1-day sync starts on the 2nd. Google sends the night that began
    # on the 1st, which ended on the 2nd, but not the nap, which didn't.
    google(GOOGLE_NIGHT, days=1)

    assert sleep_readings(conn, "google_health") == [("2026-01-01", 421 + 44)]
    assert sleep_sessions(conn, "google_health") == [
        NAP_SESSION,
        (f"google_health:sleep:{NIGHT_START}", NIGHT_START, NIGHT_END, NIGHT_SUMMARY),
    ]


def test_google_resync_of_the_same_sleep_upserts(conn, google):
    google(google_night(minutesAsleep="400"), GOOGLE_NAP)
    google(GOOGLE_NIGHT, GOOGLE_NAP)

    assert sleep_readings(conn, "google_health") == [("2026-01-01", 465)]
    sessions = sleep_sessions(conn, "google_health")
    assert len(sessions) == 2
    assert sessions[1][3] == NIGHT_SUMMARY


def test_google_falls_back_to_minutes_awake_without_stages(conn, google):
    google(google_night(stagesSummary=...))

    assert sleep_sessions(conn, "google_health")[0][3] == {
        "asleep_minutes": 421, "awake_minutes": 53,
    }


def test_google_without_any_stage_fields_still_writes_the_session(conn, google, capsys):
    google(google_night(stagesSummary=..., minutesAwake=...))

    assert sleep_readings(conn, "google_health") == [("2026-01-01", 421)]
    assert sleep_sessions(conn, "google_health") == [
        (f"google_health:sleep:{NIGHT_START}", NIGHT_START, NIGHT_END, {"asleep_minutes": 421})
    ]
    # Shape mismatch: say so once, with a sample, like the resting-HR fetcher.
    assert "no stage breakdown" in capsys.readouterr().out


def test_google_skips_a_stage_it_cannot_read(conn, google):
    google(google_night(stagesSummary=[
        {"type": "LIGHT", "minutes": "243"},
        {"type": "DEEP", "minutes": "eighty-four"},
        {"type": "REM"},
        {"type": "UNHEARD_OF", "minutes": "5"},
    ]))

    assert sleep_sessions(conn, "google_health")[0][3] == {
        "asleep_minutes": 421, "light_minutes": 243, "awake_minutes": 53,
    }


def test_google_without_a_start_time_keeps_the_metric(conn, google, capsys):
    night = copy.deepcopy(GOOGLE_NIGHT)
    del night["sleep"]["interval"]["startTime"]

    google(night, GOOGLE_NAP)

    assert sleep_readings(conn, "google_health") == [("2026-01-01", 465)]
    assert sleep_sessions(conn, "google_health") == [NAP_SESSION]
    assert "no session written" in capsys.readouterr().out


def test_an_unreadable_session_never_breaks_the_sync(conn, google, capsys):
    """A summary in a shape nobody expected costs the session, not the day."""
    google(google_night(stagesSummary={"LIGHT": "243"}), GOOGLE_NAP)

    assert sleep_readings(conn, "google_health") == [("2026-01-01", 465)]
    assert sleep_sessions(conn, "google_health") == [NAP_SESSION]
    assert "could not read a sleep session" in capsys.readouterr().out
