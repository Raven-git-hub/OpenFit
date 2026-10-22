"""
Sleep sessions from the real plugins, with the network stubbed.

Each plugin's sleep response carries the night as an interval with its
stage breakdown, which it writes as a `sleep` session - and only that:
since schema v2 there is no daily sleep_minutes metric (the night's
total is the session's asleep_minutes). These tests feed each plugin a
response in its provider's documented shape and pin the session, and
that no sleep_minutes reading is filed beside it.

The shapes are what Garmin's dailySleepDTO and the Google Health API v4
Sleep resource look like on paper. They still need checking against a
real account - which is why the defensive cases matter as much as the
happy path: a field that isn't where it's expected must cost the
session at most, never the sync.

No network: the Garmin client is a fake handed to sync(), and Google's
HTTP calls are stubbed the way test_oauth_connector.py stubs the token
exchange.
"""

import copy
import json
from datetime import date

import pytest

from crypto import encrypt

# The retired daily sleep metric: no longer in the vocabulary, spelled
# out here only to check nothing files it.
SLEEP_MINUTES = "sleep_minutes"


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
    and no vitals, activities or heart rate (test_garmin_daily_vitals.py's,
    test_workout_sessions.py's and test_heart_rate_samples.py's)."""

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

    def get_heart_rates(self, cdate):
        return {"calendarDate": cdate, "heartRateValues": None}

    def get_spo2_data(self, cdate):
        return {}

    def get_respiration_data(self, cdate):
        return {}

    def get_hrv_data(self, cdate):
        return {}

    def get_training_status(self, cdate):
        return {}

    def get_hydration_data(self, cdate):
        return {}

    def get_body_composition(self, startdate, enddate=None):
        return {}

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


def test_garmin_writes_the_night_as_a_session_only(conn, garmin):
    stats = {"2026-01-02": {"totalSteps": 9000, "restingHeartRate": 52}}

    assert garmin(conn, garmin_night(), stats=stats) == 1

    # No daily sleep metric: the day's readings are its stats alone...
    assert sleep_readings(conn, "garmin") == []
    assert [tuple(r) for r in conn.execute(
        "SELECT metric, value FROM metrics WHERE source = 'garmin' ORDER BY metric"
    )] == [("resting_hr_bpm", 52), ("steps", 9000)]
    # ...and the night is its session, from the GMT timestamps and stage
    # seconds, seconds asleep in whole minutes.
    assert sleep_sessions(conn, "garmin") == [
        (f"garmin:sleep:{GARMIN_START}", GARMIN_START, GARMIN_END, GARMIN_SUMMARY)
    ]


def test_garmin_night_alone_still_lands_its_session(conn, garmin):
    """A day with nothing but sleep is still a day written."""
    assert garmin(conn, garmin_night()) == 1

    assert conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 0
    assert sleep_sessions(conn, "garmin") == [
        (f"garmin:sleep:{GARMIN_START}", GARMIN_START, GARMIN_END, GARMIN_SUMMARY)
    ]


def test_garmin_resync_of_the_same_night_upserts(conn, garmin):
    garmin(conn, garmin_night(sleepTimeSeconds=25000, deepSleepSeconds=4000))
    garmin(conn, garmin_night())

    assert sleep_readings(conn, "garmin") == []
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

    assert sleep_sessions(conn, "garmin") == [
        (f"garmin:sleep:{GARMIN_START}", GARMIN_START, GARMIN_END, {"asleep_minutes": 443})
    ]
    assert "no stage fields" in capsys.readouterr().out


@pytest.mark.parametrize("start", [..., None, "2026-01-01T22:30:00Z"])
def test_garmin_without_a_usable_start_writes_no_sleep(conn, garmin, capsys, start):
    """No interval, no session - and no daily total in its place."""
    assert garmin(conn, garmin_night(sleepStartTimestampGMT=start)) == 0

    assert sleep_readings(conn, "garmin") == []
    assert sleep_sessions(conn, "garmin") == []
    assert "no session written" in capsys.readouterr().out


def test_garmin_without_an_end_still_writes_the_session(conn, garmin):
    garmin(conn, garmin_night(sleepEndTimestampGMT=None))

    assert sleep_sessions(conn, "garmin") == [
        (f"garmin:sleep:{GARMIN_START}", GARMIN_START, None, GARMIN_SUMMARY)
    ]


def test_garmin_night_with_no_sleep_writes_no_session(conn, garmin):
    """Nothing recorded (the 1st here): no session for it."""
    assert garmin(conn, garmin_night(), days=2) == 1

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
# GOOGLE_NIGHT's keys beyond the stage minutes: its metadata.mainSleep,
# its interval.startUtcOffset in minutes, and its two-segment timeline.
NIGHT_EXTRAS = {
    "main_sleep": True,
    "utc_offset_minutes": 0,
    "stages": [
        {"start": "2026-01-01T22:41:00Z", "end": "2026-01-01T22:50:00Z", "stage": "awake"},
        {"start": "2026-01-01T22:50:00Z", "end": "2026-01-01T23:30:00Z", "stage": "light"},
    ],
}
NIGHT_SUMMARY = {
    "asleep_minutes": 421,
    "light_minutes": 243,
    "deep_minutes": 84,
    "rem_minutes": 94,
    "awake_minutes": 53,
    **NIGHT_EXTRAS,
}
NAP_START = "2026-01-01T14:05:00Z"
NAP_SESSION = (
    f"google_health:sleep:{NAP_START}", NAP_START, "2026-01-01T14:52:00Z",
    {"asleep_minutes": 44, "awake_minutes": 3, "main_sleep": False, "utc_offset_minutes": 0},
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


def test_google_writes_each_sleep_as_a_session_only(conn, google):
    # The day with sleep counts as written...
    assert google(GOOGLE_NIGHT, GOOGLE_NAP) == 1

    # ...but no daily sleep metric is filed for it...
    assert sleep_readings(conn, "google_health") == []
    assert conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 0
    # ...each sleep is its own session, stages mapped to minutes.
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
    # Asked the documented way, the same realistic answer still files
    # both sessions.
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
    day. What began before it is dropped; every day inside keeps all its
    sessions."""
    # Two days written - the 1st and the 2nd - not the 31st.
    assert google(
        TODAY_NAP, GOOGLE_NIGHT, GOOGLE_NAP, PRE_WINDOW_NIGHT, PRE_WINDOW_NAP, days=2,
    ) == 2

    assert google.sleep_requests == [
        {"filter": 'sleep.interval.civil_end_time >= "2026-01-01"'}
    ]
    # No session for the 31st.
    assert sleep_sessions(conn, "google_health") == [
        NAP_SESSION,
        (f"google_health:sleep:{NIGHT_START}", NIGHT_START, NIGHT_END, NIGHT_SUMMARY),
        session_of(TODAY_NAP),
    ]


def test_a_day_that_leaves_the_window_keeps_its_sessions(conn, google):
    """A day's sleep, written whole while it was inside the window, is
    left as it is once the window moves past it."""
    # A 7-day sync has all of the 1st: its night and its afternoon nap.
    google(GOOGLE_NIGHT, GOOGLE_NAP, days=7)
    # A 1-day sync starts on the 2nd. Google sends the night that began
    # on the 1st, which ended on the 2nd, but not the nap, which didn't.
    google(GOOGLE_NIGHT, days=1)

    assert sleep_sessions(conn, "google_health") == [
        NAP_SESSION,
        (f"google_health:sleep:{NIGHT_START}", NIGHT_START, NIGHT_END, NIGHT_SUMMARY),
    ]


def test_google_resync_of_the_same_sleep_upserts(conn, google):
    google(google_night(minutesAsleep="400"), GOOGLE_NAP)
    google(GOOGLE_NIGHT, GOOGLE_NAP)

    assert sleep_readings(conn, "google_health") == []
    sessions = sleep_sessions(conn, "google_health")
    assert len(sessions) == 2
    assert sessions[1][3] == NIGHT_SUMMARY


def test_google_falls_back_to_minutes_awake_without_stages(conn, google):
    google(google_night(stagesSummary=...))

    assert sleep_sessions(conn, "google_health")[0][3] == {
        "asleep_minutes": 421, "awake_minutes": 53, **NIGHT_EXTRAS,
    }


def test_google_without_any_stage_fields_still_writes_the_session(conn, google, capsys):
    google(google_night(stagesSummary=..., minutesAwake=...))

    assert sleep_sessions(conn, "google_health") == [
        (f"google_health:sleep:{NIGHT_START}", NIGHT_START, NIGHT_END,
         {"asleep_minutes": 421, **NIGHT_EXTRAS})
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
        "asleep_minutes": 421, "light_minutes": 243, "awake_minutes": 53, **NIGHT_EXTRAS,
    }


def test_google_without_a_start_time_costs_only_that_session(conn, google, capsys):
    night = copy.deepcopy(GOOGLE_NIGHT)
    del night["sleep"]["interval"]["startTime"]

    google(night, GOOGLE_NAP)

    assert sleep_readings(conn, "google_health") == []
    assert sleep_sessions(conn, "google_health") == [NAP_SESSION]
    assert "no session written" in capsys.readouterr().out


def test_an_unreadable_session_never_breaks_the_sync(conn, google, capsys):
    """A summary in a shape nobody expected costs the session, not the day."""
    assert google(google_night(stagesSummary={"LIGHT": "243"}), GOOGLE_NAP) == 1

    assert sleep_sessions(conn, "google_health") == [NAP_SESSION]
    assert "could not read a sleep session" in capsys.readouterr().out


# ---- the stage timeline (stages, brief_wakes, main_sleep, utc_offset_minutes) ----
#
# A real night from a Pixel Watch 4, as the Google Health API v4 sent it
# (2026-10-22, Hong Kong, UTC+8; the account id in `name` replaced). The
# stages tile the interval exactly and sum to stagesSummary; the
# shortAwakenings overlay them, typed LIGHT, and aren't in the AWAKE
# minutes; metadata has mainSleep but no nap; the interval carries no
# civil times, so the point is kept but counts no day.


def google_segment(start, end, kind=None, stamps=True):
    """One entry of a sleep point's stages/shortAwakenings, as Google sends it."""
    segment = {"startTime": start, "startUtcOffset": "28800s",
               "endTime": end, "endUtcOffset": "28800s"}
    if kind is not None:
        segment["type"] = kind
    if stamps:
        segment["createTime"] = "2026-10-22T01:25:02.648335Z"
        segment["updateTime"] = "2026-10-22T01:25:02.648335Z"
    return segment


REAL_STAGES = [
    ("2026-10-21T19:31:00Z", "2026-10-21T19:34:00Z", "AWAKE"),
    ("2026-10-21T19:34:00Z", "2026-10-21T19:47:00Z", "LIGHT"),
    ("2026-10-21T19:47:00Z", "2026-10-21T20:29:00Z", "DEEP"),
    ("2026-10-21T20:29:00Z", "2026-10-21T20:32:00Z", "LIGHT"),
    ("2026-10-21T20:32:00Z", "2026-10-21T20:49:00Z", "REM"),
    ("2026-10-21T20:49:00Z", "2026-10-21T21:10:00Z", "LIGHT"),
    ("2026-10-21T21:10:00Z", "2026-10-21T21:13:00Z", "DEEP"),
    ("2026-10-21T21:13:00Z", "2026-10-21T21:27:00Z", "LIGHT"),
    ("2026-10-21T21:27:00Z", "2026-10-21T21:50:00Z", "DEEP"),
    ("2026-10-21T21:50:00Z", "2026-10-21T22:09:00Z", "LIGHT"),
    ("2026-10-21T22:09:00Z", "2026-10-21T22:41:00Z", "REM"),
    ("2026-10-21T22:41:00Z", "2026-10-21T23:43:00Z", "LIGHT"),
    ("2026-10-21T23:43:00Z", "2026-10-22T00:24:00Z", "REM"),
    ("2026-10-22T00:24:00Z", "2026-10-22T01:05:00Z", "LIGHT"),
    ("2026-10-22T01:05:00Z", "2026-10-22T01:07:00Z", "AWAKE"),
]
REAL_WAKES = [
    ("2026-10-21T20:49:00Z", "2026-10-21T20:50:00Z"),
    ("2026-10-21T21:13:00Z", "2026-10-21T21:14:00Z"),
    ("2026-10-21T21:16:00Z", "2026-10-21T21:18:00Z"),
    ("2026-10-21T21:52:00Z", "2026-10-21T21:54:00Z"),
    ("2026-10-21T21:55:00Z", "2026-10-21T21:56:00Z"),
    ("2026-10-21T21:57:00Z", "2026-10-21T21:58:00Z"),
    ("2026-10-21T22:00:00Z", "2026-10-21T22:01:00Z"),
    ("2026-10-21T23:03:00Z", "2026-10-21T23:06:00Z"),
    ("2026-10-22T00:32:00Z", "2026-10-22T00:33:00Z"),
]

REAL_NIGHT = {
    "name": "users/0000/dataTypes/sleep/dataPoints/3487078624060513096",
    "dataSource": {"recordingMethod": "DERIVED",
                   "device": {"displayName": "Pixel Watch 4"}, "platform": "FITBIT"},
    "sleep": {
        "interval": {
            "startTime": "2026-10-21T19:31:00Z", "startUtcOffset": "28800s",
            "endTime": "2026-10-22T01:07:00Z", "endUtcOffset": "28800s",
        },
        "type": "STAGES",
        "stages": [google_segment(a, b, kind) for a, b, kind in REAL_STAGES],
        "metadata": {"stagesStatus": "SUCCEEDED", "processed": True, "mainSleep": True},
        "summary": {
            "minutesInSleepPeriod": "336", "minutesAfterWakeUp": "0",
            "minutesToFallAsleep": "0", "minutesAsleep": "331", "minutesAwake": "5",
            "stagesSummary": [
                {"type": "AWAKE", "minutes": "5", "count": "2"},
                {"type": "LIGHT", "minutes": "173", "count": "7"},
                {"type": "DEEP", "minutes": "68", "count": "3"},
                {"type": "REM", "minutes": "90", "count": "3"},
            ],
        },
        "createTime": "2026-10-22T01:25:00.456031Z",
        "updateTime": "2026-10-22T06:48:10.365039Z",
        "shortAwakenings": [google_segment(a, b, "LIGHT", stamps=False) for a, b in REAL_WAKES],
    },
}

REAL_START = "2026-10-21T19:31:00Z"
REAL_SUMMARY = {
    "asleep_minutes": 331,
    "light_minutes": 173,
    "deep_minutes": 68,
    "rem_minutes": 90,
    "awake_minutes": 5,
    "main_sleep": True,
    "utc_offset_minutes": 480,
    "stages": [{"start": a, "end": b, "stage": kind.lower()} for a, b, kind in REAL_STAGES],
    "brief_wakes": [{"start": a, "end": b} for a, b in REAL_WAKES],
}


def real_night(**sleep_changes):
    """REAL_NIGHT with some `sleep` fields replaced (... removes one)."""
    night = copy.deepcopy(REAL_NIGHT)
    for key, value in sleep_changes.items():
        if value is ...:
            night["sleep"].pop(key)
        else:
            night["sleep"][key] = value
    return night


def only_summary(conn):
    (session,) = sleep_sessions(conn, "google_health")
    return session[3]


def test_google_files_a_real_night_in_the_openfit_shape(conn, google):
    google(REAL_NIGHT)

    assert sleep_sessions(conn, "google_health") == [
        (f"google_health:sleep:{REAL_START}", REAL_START, "2026-10-22T01:07:00Z", REAL_SUMMARY),
    ]


def test_brief_wakes_overlay_the_timeline_and_are_not_awake_time(conn, google):
    google(REAL_NIGHT)
    summary = only_summary(conn)

    # The totals are Google's, never recomputed: nine brief wakes, still 5.
    assert summary["awake_minutes"] == 5
    # The timeline tiles the session; the brief wakes stay out of it.
    stages = summary["stages"]
    assert stages[0]["start"] == REAL_START and stages[-1]["end"] == "2026-10-22T01:07:00Z"
    assert all(a["end"] == b["start"] for a, b in zip(stages, stages[1:]))
    assert len(summary["brief_wakes"]) == 9
    assert all(set(wake) == {"start", "end"} for wake in summary["brief_wakes"])


def test_google_keeps_the_timeline_in_time_order(conn, google):
    shuffled = copy.deepcopy(REAL_NIGHT["sleep"]["stages"])
    shuffled.reverse()
    wakes = copy.deepcopy(REAL_NIGHT["sleep"]["shortAwakenings"])
    wakes.reverse()

    google(real_night(stages=shuffled, shortAwakenings=wakes))

    assert only_summary(conn) == REAL_SUMMARY


def test_google_keeps_a_classic_naps_own_stage_names(conn, google):
    """A CLASSIC nap's ASLEEP isn't light sleep and RESTLESS has no
    equivalent: both are kept under their own canonical names."""
    nap = copy.deepcopy(GOOGLE_NAP)
    nap["sleep"]["stages"] = [
        google_segment("2026-01-01T14:05:00Z", "2026-01-01T14:20:00Z", "ASLEEP"),
        google_segment("2026-01-01T14:20:00Z", "2026-01-01T14:23:00Z", "RESTLESS"),
        google_segment("2026-01-01T14:23:00Z", "2026-01-01T14:49:00Z", "ASLEEP"),
        google_segment("2026-01-01T14:49:00Z", "2026-01-01T14:52:00Z", "AWAKE"),
    ]

    google(nap)

    assert [s["stage"] for s in only_summary(conn)["stages"]] == [
        "asleep", "restless", "asleep", "awake",
    ]


def test_google_files_an_unheard_of_stage_as_unknown(conn, google, capsys):
    stages = copy.deepcopy(REAL_NIGHT["sleep"]["stages"])
    stages[2]["type"] = "OUT_OF_BED_MAYBE"

    google(real_night(stages=stages))

    assert only_summary(conn)["stages"][2] == {
        "start": "2026-10-21T19:47:00Z", "end": "2026-10-21T20:29:00Z", "stage": "unknown",
    }
    assert "OUT_OF_BED_MAYBE" in capsys.readouterr().out


def test_google_skips_a_segment_it_cannot_read(conn, google, capsys):
    stages = copy.deepcopy(REAL_NIGHT["sleep"]["stages"])
    del stages[1]["endTime"]                                   # no end
    stages[2]["endTime"] = "2026-10-21T19:00:00Z"              # ends before it starts
    stages[3] = "LIGHT"                                        # not a segment at all

    google(real_night(stages=stages))

    kept = only_summary(conn)["stages"]
    assert len(kept) == len(REAL_STAGES) - 3
    assert kept[1]["start"] == "2026-10-21T20:32:00Z"
    assert "skipped 3 sleep.stages segment(s)" in capsys.readouterr().out


def test_an_unreadable_timeline_costs_only_the_timeline(conn, google, capsys):
    google(real_night(stages={"LIGHT": "173"}))

    summary = only_summary(conn)
    assert "stages" not in summary
    # Everything else is still filed.
    assert summary == {k: v for k, v in REAL_SUMMARY.items() if k != "stages"}
    assert "could not read sleep.stages" in capsys.readouterr().out


@pytest.mark.parametrize("offset, minutes", [
    ("28800s", 480), ("-18000s", -300), ("19800s", 330), ("0s", 0),
    ("3601s", None), ("28800.5s", None), ("+08:00", None), (28800, None), (None, None),
])
def test_google_reads_the_utc_offset_in_whole_minutes(conn, google, offset, minutes):
    night = copy.deepcopy(REAL_NIGHT)
    if offset is None:
        del night["sleep"]["interval"]["startUtcOffset"]
    else:
        night["sleep"]["interval"]["startUtcOffset"] = offset

    google(night)

    assert only_summary(conn).get("utc_offset_minutes") == minutes


@pytest.mark.parametrize("metadata, main_sleep", [
    ({"mainSleep": True}, True), ({"mainSleep": False}, False),
    ({"mainSleep": "true"}, None), ({}, None), (None, None),
])
def test_google_keeps_main_sleep_only_as_a_bool(conn, google, metadata, main_sleep):
    if metadata is None:
        google(real_night(metadata=...))
    else:
        google(real_night(metadata=metadata))

    assert only_summary(conn).get("main_sleep") == main_sleep


def test_google_without_a_stage_timeline_says_so(conn, google, capsys):
    google(real_night(stages=..., shortAwakenings=...))

    summary = only_summary(conn)
    assert "stages" not in summary and "brief_wakes" not in summary
    assert summary["light_minutes"] == 173
    assert "no stage timeline" in capsys.readouterr().out


def test_google_stage_names_are_all_canonical():
    from metrics import SLEEP_STAGES
    from plugins.google_health.plugin import SLEEP_STAGE_NAMES

    assert set(SLEEP_STAGE_NAMES.values()) <= set(SLEEP_STAGES)
    assert "unknown" in SLEEP_STAGES
