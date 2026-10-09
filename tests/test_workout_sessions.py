"""
Workout sessions from the real plugins, with the network stubbed.

A workout is an interval and nothing else - there is no daily workout
metric - so each plugin fetches its activities and writes one `workout`
session apiece: start and end in UTC, and a summary in canonical units
(type, duration_minutes, distance_m, avg_hr_bpm, calories_kcal) holding
whichever of those the source gives.

The shapes are what Garmin's activity search (get_activities_by_date in
python-garminconnect) and the Google Health API v4 Exercise resource look
like on paper. Like the sleep shapes in test_sleep_sessions.py they still
need checking against a real account, so the defensive cases matter as
much as the happy path: a field that isn't where it's expected costs that
field or that one workout, never the sync.

No network: the Garmin client is a fake handed to sync(), and Google's
HTTP calls go to a stand-in that pages the way the real API does.
"""

import copy
import json
from datetime import date

import pytest

from crypto import encrypt


class FrozenDate(date):
    """date, with today pinned to the fixture run's day."""

    @classmethod
    def today(cls):
        return cls(2026, 1, 2)


def workout_sessions(conn, source):
    return [
        (r[0], r[1], r[2], json.loads(r[3]))
        for r in conn.execute(
            'SELECT id, "start", "end", summary_json FROM sessions '
            "WHERE source = ? AND kind = 'workout' ORDER BY \"start\"",
            (source,),
        )
    ]


def metric_count(conn):
    return conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0]


# ---------- Garmin ----------


# get_activities_by_date() entries, trimmed of the fields nothing reads.
# startTimeGMT is real UTC; startTimeLocal is wall-clock (here UTC+1).
# duration is the timer time and elapsedDuration the wall-clock time,
# pauses included, both in seconds; distance is metres.
GARMIN_RUN = {
    "activityId": 21345678901,
    "activityName": "Morning Run",
    "startTimeLocal": "2026-01-02 08:15:04",
    "startTimeGMT": "2026-01-02 07:15:04",
    "activityType": {"typeId": 1, "typeKey": "running", "parentTypeId": 17, "isHidden": False},
    "eventType": {"typeId": 9, "typeKey": "uncategorized", "sortOrder": 10},
    "distance": 10234.56,
    "duration": 3065.4,             # 51 min on the timer
    "elapsedDuration": 3190.2,      # 53 min 10 s start to finish
    "movingDuration": 3010.0,
    "elevationGain": 87.0,
    "averageSpeed": 3.339,
    "calories": 712.0,
    "bmrCalories": 61.0,
    "averageHR": 151.0,
    "maxHR": 176.0,
    "averageRunningCadenceInStepsPerMinute": 168.4,
    "steps": 8612,
    "beginTimestamp": 1767338104000,
    "manualActivity": False,
}

# A strength session the evening before: no distance, and here no
# heart-rate sensor either.
GARMIN_STRENGTH = {
    "activityId": 21345678777,
    "activityName": "Strength",
    "startTimeLocal": "2026-01-01 19:02:40",
    "startTimeGMT": "2026-01-01 18:02:40",
    "activityType": {"typeId": 13, "typeKey": "strength_training", "parentTypeId": 29},
    "duration": 2701.6,             # 45 min
    "elapsedDuration": 2733.9,
    "calories": 248.0,
    "totalSets": 12,
    "activeSets": 12,
    "totalReps": 96,
    "beginTimestamp": 1767290560000,
    "manualActivity": False,
}

RUN_START = "2026-01-02T07:15:04Z"
RUN_SESSION = (
    f"garmin:workout:{RUN_START}", RUN_START, "2026-01-02T08:08:14Z",
    {"type": "running", "duration_minutes": 51, "distance_m": 10234.56,
     "avg_hr_bpm": 151, "calories_kcal": 712},
)
STRENGTH_START = "2026-01-01T18:02:40Z"
STRENGTH_SESSION = (
    f"garmin:workout:{STRENGTH_START}", STRENGTH_START, "2026-01-01T18:48:13Z",
    {"type": "strength_training", "duration_minutes": 45, "calories_kcal": 248},
)


class FakeGarmin:
    """Stands in for garminconnect.Garmin: canned activities, recording
    every get_activities_by_date call; no stats or sleep for any day
    unless given."""

    def __init__(self, activities, stats_by_date=None):
        self.activities = activities
        self.stats_by_date = stats_by_date or {}
        self.activity_calls = []

    def get_stats(self, cdate):
        return self.stats_by_date.get(cdate, {})

    def get_sleep_data(self, cdate):
        return {"dailySleepDTO": {"calendarDate": cdate, "sleepTimeSeconds": None}}

    def get_activities_by_date(self, startdate, enddate=None):
        self.activity_calls.append((startdate, enddate))
        if isinstance(self.activities, Exception):
            raise self.activities
        return self.activities


@pytest.fixture
def garmin(monkeypatch):
    """The real Garmin plugin, syncing from a FakeGarmin on 2026-01-02.
    The fake from the latest sync is garmin.client."""
    from plugins import PLUGINS

    plugin = PLUGINS["garmin"]
    monkeypatch.setattr("plugins.garmin.plugin.date", FrozenDate)

    def sync_with(conn, *activities, days=2, stats=None, fail=None):
        sync_with.client = FakeGarmin(fail or list(activities), stats)
        monkeypatch.setattr(plugin, "_get_client", lambda conn=None: sync_with.client)
        return plugin.sync(conn, days)

    return sync_with


def garmin_run(**changes):
    """GARMIN_RUN with some fields replaced (a value of ... removes one)."""
    run = copy.deepcopy(GARMIN_RUN)
    for key, value in changes.items():
        if value is ...:
            run.pop(key)
        else:
            run[key] = value
    return run


def test_garmin_writes_each_activity_as_a_workout_session(conn, garmin):
    garmin(conn, GARMIN_RUN, GARMIN_STRENGTH)

    assert workout_sessions(conn, "garmin") == [STRENGTH_SESSION, RUN_SESSION]
    # Sessions only: a workout files no daily metric.
    assert metric_count(conn) == 0


def test_garmin_asks_for_the_whole_range_in_one_call(conn, garmin):
    """Not a request per day: one range call, and the library pages it."""
    garmin(conn, GARMIN_RUN, days=7)

    # A 7-day sync on 2026-01-02 starts on 2025-12-27.
    assert garmin.client.activity_calls == [("2025-12-27", "2026-01-02")]


def test_garmin_workouts_sit_beside_the_days_metrics(conn, garmin):
    stats = {"2026-01-02": {"totalSteps": 14200, "restingHeartRate": 51}}

    assert garmin(conn, GARMIN_RUN, days=1, stats=stats) == 1

    assert [tuple(r) for r in conn.execute(
        "SELECT metric, value FROM metrics WHERE source = 'garmin' ORDER BY metric"
    )] == [("resting_hr_bpm", 51), ("steps", 14200)]
    assert workout_sessions(conn, "garmin") == [RUN_SESSION]


def test_garmin_resync_of_the_same_workout_upserts(conn, garmin):
    garmin(conn, garmin_run(calories=650.0, elapsedDuration=3100.0))
    garmin(conn, GARMIN_RUN)

    assert workout_sessions(conn, "garmin") == [RUN_SESSION]


@pytest.mark.parametrize("missing", ["distance", "averageHR", "calories", "duration"])
def test_garmin_leaves_out_a_field_the_activity_lacks(conn, garmin, missing):
    garmin(conn, garmin_run(**{missing: ...}))

    expected = dict(RUN_SESSION[3])
    expected.pop({
        "distance": "distance_m", "averageHR": "avg_hr_bpm",
        "calories": "calories_kcal", "duration": "duration_minutes",
    }[missing])
    assert workout_sessions(conn, "garmin")[0][3] == expected


def test_garmin_leaves_out_a_field_that_is_not_a_number(conn, garmin):
    garmin(conn, garmin_run(averageHR=None, distance="10 km", activityType={"typeId": 1}))

    assert workout_sessions(conn, "garmin")[0][3] == {
        "duration_minutes": 51, "calories_kcal": 712,
    }


def test_garmin_without_elapsed_duration_ends_after_the_timer_time(conn, garmin):
    garmin(conn, garmin_run(elapsedDuration=...))

    # 07:15:04 plus duration's 3065.4 s.
    assert workout_sessions(conn, "garmin")[0][2] == "2026-01-02T08:06:09Z"


def test_garmin_without_any_duration_has_no_end(conn, garmin):
    garmin(conn, garmin_run(elapsedDuration=..., duration=...))

    session = workout_sessions(conn, "garmin")[0]
    assert session[1:3] == (RUN_START, None)
    assert "duration_minutes" not in session[3]


@pytest.mark.parametrize("start", [..., None, "", "07:15 on the 2nd", 1767338104000])
def test_garmin_skips_an_activity_without_a_usable_start(conn, garmin, capsys, start):
    garmin(conn, garmin_run(startTimeGMT=start), GARMIN_STRENGTH)

    assert workout_sessions(conn, "garmin") == [STRENGTH_SESSION]
    assert "no session written" in capsys.readouterr().out


def test_an_unreadable_activity_never_breaks_the_sync(conn, garmin, capsys):
    """An activity in a shape nobody expected costs that workout only."""
    garmin(conn, garmin_run(activityType="running"), "not an activity", GARMIN_STRENGTH)

    assert workout_sessions(conn, "garmin") == [STRENGTH_SESSION]
    assert "could not read a workout" in capsys.readouterr().out


def test_a_failed_activities_fetch_costs_only_the_workouts(conn, garmin, capsys):
    stats = {"2026-01-02": {"totalSteps": 14200}}

    assert garmin(conn, days=1, stats=stats, fail=RuntimeError("503 Service Unavailable")) == 1

    assert metric_count(conn) == 1
    assert workout_sessions(conn, "garmin") == []
    assert "activities fetch failed for 2026-01-02 to 2026-01-02" in capsys.readouterr().out


# ---------- Google Health ----------


# GET dataTypes/exercise/dataPoints, in the v4 Exercise shape: a run and
# a yoga session. activeDuration is a duration string, distance is in
# millimetres, and int64 fields arrive as JSON strings.
GOOGLE_RUN = {
    "exercise": {
        "interval": {
            "startTime": "2026-01-02T07:15:04.250Z",
            "startUtcOffset": "3600s",
            "endTime": "2026-01-02T08:08:14Z",
            "endUtcOffset": "3600s",
            "civilStartTime": {
                "date": {"year": 2026, "month": 1, "day": 2},
                "time": {"hours": 8, "minutes": 15, "seconds": 4},
            },
            "civilEndTime": {
                "date": {"year": 2026, "month": 1, "day": 2},
                "time": {"hours": 9, "minutes": 8, "seconds": 14},
            },
        },
        "exerciseType": "RUNNING",
        "displayName": "Run",
        "activeDuration": "3065.400s",
        "metricsSummary": {
            "caloriesKcal": 712.4,
            "distanceMillimeters": 10234560,
            "steps": "8612",
            "averageHeartRateBeatsPerMinute": "151",
            "averagePaceSecondsPerMeter": 0.2995,
            "averageSpeedMillimetersPerSecond": 3339.0,
            "elevationGainMillimeters": 87000,
            "activeZoneMinutes": "38",
            "heartRateZoneDurations": {
                "lightTime": "600s", "moderateTime": "1500s",
                "vigorousTime": "900s", "peakTime": "65s",
            },
        },
        "exerciseMetadata": {"hasGps": True},
        "exerciseEvents": [
            {"exerciseEventType": "START", "eventTime": "2026-01-02T07:15:04Z",
             "eventUtcOffset": "3600s"},
            {"exerciseEventType": "STOP", "eventTime": "2026-01-02T08:08:14Z",
             "eventUtcOffset": "3600s"},
        ],
        "createTime": "2026-01-02T08:09:01Z",
        "updateTime": "2026-01-02T08:09:01Z",
    },
}

GOOGLE_YOGA = {
    "exercise": {
        "interval": {
            "startTime": "2026-01-01T18:00:00Z",
            "startUtcOffset": "3600s",
            "endTime": "2026-01-01T18:31:00Z",
            "endUtcOffset": "3600s",
            "civilStartTime": {"date": {"year": 2026, "month": 1, "day": 1}},
            "civilEndTime": {"date": {"year": 2026, "month": 1, "day": 1}},
        },
        "exerciseType": "YOGA",
        "displayName": "Yoga",
        "activeDuration": "1800s",
        "metricsSummary": {"caloriesKcal": 95.0, "averageHeartRateBeatsPerMinute": "88"},
    },
}

G_RUN_START = "2026-01-02T07:15:04Z"
G_RUN_SESSION = (
    f"google_health:workout:{G_RUN_START}", G_RUN_START, "2026-01-02T08:08:14Z",
    {"type": "running", "duration_minutes": 51, "distance_m": 10234.56,
     "avg_hr_bpm": 151, "calories_kcal": 712},
)
G_YOGA_START = "2026-01-01T18:00:00Z"
G_YOGA_SESSION = (
    f"google_health:workout:{G_YOGA_START}", G_YOGA_START, "2026-01-01T18:31:00Z",
    {"type": "yoga", "duration_minutes": 30, "avg_hr_bpm": 88, "calories_kcal": 95},
)


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self.payload = payload
        self.status = status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"{self.status} Server Error")


class FakeGoogle:
    """Google Health's endpoints: the exercise list serves `exercise_pages`
    in order, each but the last with a nextPageToken ("page-N"), and a
    status code in place of a page fails it; every other data type
    answers empty. Each exercise request's query params are recorded."""

    def __init__(self):
        self.exercise_pages = [[]]
        self.exercise_requests = []

    def post(self, url, data=None, json=None, headers=None, timeout=None):
        if "oauth2" in url:
            return FakeResponse({"access_token": "access-xyz"})
        return FakeResponse({"rollupDataPoints": []})

    def get(self, url, headers=None, params=None, timeout=None):
        if "/dataTypes/exercise/" not in url:
            return FakeResponse({"dataPoints": []})
        self.exercise_requests.append(dict(params))
        token = params.get("pageToken")
        index = int(token.split("-")[1]) if token else 0
        page = self.exercise_pages[index]
        if isinstance(page, int):
            return FakeResponse(status=page)
        payload = {"dataPoints": page}
        if index < len(self.exercise_pages) - 1:
            payload["nextPageToken"] = f"page-{index + 1}"
        return FakeResponse(payload)


@pytest.fixture
def google(conn, monkeypatch):
    """A FakeGoogle behind the real Google Health plugin, syncing on
    2026-01-02. google(*pages, days=7) serves each argument as one page of
    exercise points and syncs."""
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

    fake = FakeGoogle()
    monkeypatch.setattr("plugins.google_health.plugin.requests.post", fake.post)
    monkeypatch.setattr("plugins.google_health.plugin.requests.get", fake.get)

    def sync_with(*pages, days=7):
        fake.exercise_pages = list(pages) or [[]]
        fake.exercise_requests.clear()
        return PLUGINS["google_health"].sync(conn, days)

    sync_with.fake = fake
    return sync_with


def google_exercise(point, **changes):
    """A copy of an exercise point with some `exercise` fields replaced
    (a value of ... removes one)."""
    point = copy.deepcopy(point)
    for key, value in changes.items():
        if value is ...:
            point["exercise"].pop(key)
        else:
            point["exercise"][key] = value
    return point


def test_google_writes_each_exercise_as_a_workout_session(conn, google):
    google([GOOGLE_RUN, GOOGLE_YOGA])

    assert workout_sessions(conn, "google_health") == [G_YOGA_SESSION, G_RUN_SESSION]
    # Sessions only: a workout files no daily metric.
    assert metric_count(conn) == 0


def test_google_asks_for_exercise_by_its_civil_start_date(conn, google):
    """The discovery doc's session filter, which exercise - unlike sleep -
    supports."""
    google([GOOGLE_RUN], days=7)

    # A 7-day sync on 2026-01-02 starts on 2025-12-27.
    assert google.fake.exercise_requests == [
        {"filter": 'exercise.interval.civil_start_time >= "2025-12-27"'}
    ]


def test_google_reads_every_page_of_exercise(conn, google):
    """An exercise page holds at most 25 points, so a backfill spans
    several; each token goes back as the pageToken query param."""
    google([GOOGLE_RUN], [GOOGLE_YOGA], [])

    flt = 'exercise.interval.civil_start_time >= "2025-12-27"'
    assert google.fake.exercise_requests == [
        {"filter": flt},
        {"filter": flt, "pageToken": "page-1"},
        {"filter": flt, "pageToken": "page-2"},
    ]
    assert workout_sessions(conn, "google_health") == [G_YOGA_SESSION, G_RUN_SESSION]


def test_google_keeps_earlier_pages_when_a_later_one_fails(conn, google, capsys):
    google([GOOGLE_RUN], 500)

    assert workout_sessions(conn, "google_health") == [G_RUN_SESSION]
    assert "exercise: page 2 failed" in capsys.readouterr().out


def test_google_resync_of_the_same_workout_upserts(conn, google):
    google([google_exercise(GOOGLE_RUN, activeDuration="2900s")])
    google([GOOGLE_RUN])

    assert workout_sessions(conn, "google_health") == [G_RUN_SESSION]


def test_google_leaves_out_what_the_exercise_lacks(conn, google):
    """Yoga has no distance; a type Google left unspecified is no type."""
    google([google_exercise(GOOGLE_YOGA, exerciseType="EXERCISE_TYPE_UNSPECIFIED")])

    assert workout_sessions(conn, "google_health")[0][3] == {
        "duration_minutes": 30, "avg_hr_bpm": 88, "calories_kcal": 95,
    }


def test_google_skips_a_field_it_cannot_read(conn, google):
    google([google_exercise(GOOGLE_RUN, activeDuration="51 minutes", metricsSummary={
        "distanceMillimeters": "far", "averageHeartRateBeatsPerMinute": "unknown",
        "caloriesKcal": 712.4,
    })])

    assert workout_sessions(conn, "google_health")[0][3] == {
        "type": "running", "calories_kcal": 712,
    }


def test_google_exercise_with_no_numbers_says_what_it_got(conn, google, capsys):
    """Sessions came back but none had a metric where the doc puts them:
    each is still written, and one sample is logged to show the shape."""
    google([google_exercise(GOOGLE_RUN, activeDuration=..., metricsSummary=...)])

    assert workout_sessions(conn, "google_health") == [
        (G_RUN_SESSION[0], G_RUN_START, "2026-01-02T08:08:14Z", {"type": "running"})
    ]
    out = capsys.readouterr().out
    assert "no duration, distance, heart rate or calories" in out
    assert "'exerciseType': 'RUNNING'" in out


def test_google_skips_an_exercise_without_a_start_time(conn, google, capsys):
    no_start = copy.deepcopy(GOOGLE_RUN)
    del no_start["exercise"]["interval"]["startTime"]

    google([no_start, GOOGLE_YOGA])

    assert workout_sessions(conn, "google_health") == [G_YOGA_SESSION]
    assert "no session written" in capsys.readouterr().out


def test_google_without_an_end_time_still_writes_the_session(conn, google):
    no_end = copy.deepcopy(GOOGLE_RUN)
    del no_end["exercise"]["interval"]["endTime"]

    google([no_end])

    assert workout_sessions(conn, "google_health") == [
        (G_RUN_SESSION[0], G_RUN_START, None, G_RUN_SESSION[3])
    ]


def test_an_unreadable_exercise_never_breaks_the_sync(conn, google, capsys):
    """A point in a shape nobody expected costs that workout only."""
    google([google_exercise(GOOGLE_RUN, metricsSummary=["151"]), "not a point", GOOGLE_YOGA])

    assert workout_sessions(conn, "google_health") == [G_YOGA_SESSION]
    assert "could not read an exercise session" in capsys.readouterr().out


def test_a_failed_exercise_fetch_never_breaks_the_sync(conn, google, capsys):
    google(500)

    assert workout_sessions(conn, "google_health") == []
    assert "exercise fetch failed" in capsys.readouterr().out
