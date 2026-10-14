"""
Google Health's daily metrics (schema v2, PR 3), with the network stubbed.

Beside steps and resting HR, Google now files the daily figures a Pixel
Watch gives. Aggregatable ones come from dailyRollUp - Google's own
per-day sums: distance, floors, altitude gain, active and total energy,
Active Zone Minutes and sedentary time. One-a-day summaries come from
the list endpoint, filtered on their `.date`: HRV, VO2 max, SpO2 and
respiratory rate.

The data types, the keys each point carries its value under, the fields
read and their units are the v4 discovery document's
(https://health.googleapis.com/$discovery/rest?version=v4); the fixtures
below are in those shapes, int64s as JSON strings as Google sends them.
These pin that each lands under its canonical key in the canonical unit;
that every daily fetch asks for, and files, the same window; and that a
type that fails, comes back empty or in an unexpected shape costs only
its own metric and says so in the logs.
"""

import json
from datetime import date, timedelta

import pytest

from crypto import encrypt
from metrics import (
    ACTIVE_KCAL,
    ACTIVE_MINUTES,
    DISTANCE_M,
    ELEVATION_GAIN_M,
    FLOORS,
    HRV_MS,
    INTENSITY_MODERATE_MIN,
    INTENSITY_VIGOROUS_MIN,
    RESPIRATORY_RATE_BRPM,
    RESTING_HR_BPM,
    SEDENTARY_MIN,
    SPO2_PCT,
    STEPS,
    TOTAL_KCAL,
    VO2_MAX,
    unit_for,
)

TODAY = date(2026, 6, 30)


class FrozenDate(date):
    """date, with today pinned to TODAY."""

    @classmethod
    def today(cls):
        return cls(TODAY.year, TODAY.month, TODAY.day)


def civil(d):
    return {"year": d.year, "month": d.month, "day": d.day}


def from_civil(c):
    return date(c["year"], c["month"], c["day"])


# ---------- each type, as the discovery doc has it ----------

# dailyRollUp types: (data type, the key a rollup point carries its value
# under, one day's value, the metric it files as, the value filed).
ROLLUPS = [
    ("steps", "steps", {"countSum": "8123"}, STEPS, 8123),
    # millimetres -> whole metres
    ("distance", "distance", {"millimetersSum": "6543210"}, DISTANCE_M, 6543),
    ("floors", "floors", {"countSum": "12"}, FLOORS, 12),
    # millimetres of gain -> whole metres
    ("altitude", "altitude", {"gainMillimetersSum": "36576"}, ELEVATION_GAIN_M, 37),
    # kcalSum is kilocalories already - no kJ conversion
    ("active-energy-burned", "activeEnergyBurned", {"kcalSum": 612.4}, ACTIVE_KCAL, 612),
    ("total-calories", "totalCalories", {"kcalSum": 2398.7}, TOTAL_KCAL, 2399),
    # per zone, summed to the one AZM total
    ("active-zone-minutes", "activeZoneMinutes",
     {"sumInFatBurnHeartZone": "20", "sumInCardioHeartZone": "14", "sumInPeakHeartZone": "4"},
     ACTIVE_MINUTES, 38),
    # a duration in seconds -> whole minutes
    ("sedentary-period", "sedentaryPeriod", {"durationSum": "30600s"}, SEDENTARY_MIN, 510),
]

# list types, one point a day: (data type, the point's key, one day's
# value beside its date, the metric it files as, the value filed).
SUMMARIES = [
    ("daily-resting-heart-rate", "dailyRestingHeartRate",
     {"beatsPerMinute": "58", "dailyRestingHeartRateMetadata": {"calculationMethod": "WITH_SLEEP"}},
     RESTING_HR_BPM, 58),
    ("daily-heart-rate-variability", "dailyHeartRateVariability",
     {"averageHeartRateVariabilityMilliseconds": 41.7,
      "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds": 55.2,
      "nonRemHeartRateBeatsPerMinute": "52", "entropy": 2.9},
     HRV_MS, 41.7),
    ("daily-vo2-max", "dailyVo2Max",
     {"vo2Max": 44.3, "cardioFitnessLevel": "GOOD", "vo2MaxCovariance": 1.2},
     VO2_MAX, 44.3),
    ("daily-oxygen-saturation", "dailyOxygenSaturation",
     {"averagePercentage": 95.6, "lowerBoundPercentage": 93.1, "upperBoundPercentage": 98.0},
     SPO2_PCT, 95.6),
    ("daily-respiratory-rate", "dailyRespiratoryRate", {"breathsPerMinute": 14.2},
     RESPIRATORY_RATE_BRPM, 14.2),
]

ROLLUP_KEYS = {data_type: key for data_type, key, *_ in ROLLUPS}
SUMMARY_KEYS = {data_type: key for data_type, key, *_ in SUMMARIES}
ALL_TYPES = ROLLUPS + SUMMARIES
NEW_TYPES = [t for t in ALL_TYPES if t[3] not in (STEPS, RESTING_HR_BPM)]


def type_id(case):
    return case[0]


# ---------- a stand-in for Google ----------


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
    """Google Health's data endpoints, recording every data request as
    (data type, query params or body).

    data[data_type] maps a date to that day's value. dailyRollUp answers
    with a point a day inside the request's range, newest first, the value
    under the type's rollup key - and, like Google, refuses a range over
    90 days, or over 14 for total-calories. The list endpoint answers with
    every day it holds, whatever the filter, as a Google that ignored it
    would. A data type in `failing` gets a 500; sleep, exercise and the
    intraday series (test_google_series.py) are empty. `raw[data_type]`
    serves those points verbatim instead.
    """

    def __init__(self):
        self.data = {}
        self.raw = {}
        self.failing = set()
        self.requests = []

    def seed(self, cases, d=TODAY):
        for data_type, _key, value, _metric, _filed in cases:
            self.data.setdefault(data_type, {})[d] = value

    def requests_for(self, data_type):
        return [fields for t, fields in self.requests if t == data_type]

    def post(self, url, data=None, json=None, headers=None, timeout=None):
        if "oauth2" in url:
            return FakeResponse({"access_token": "access-xyz"})
        data_type = data_type_of(url)
        self.requests.append((data_type, json))
        if url.endswith(":rollUp"):
            return FakeResponse({"rollupDataPoints": []})
        assert url.endswith(":dailyRollUp")
        first = from_civil(json["range"]["start"]["date"])
        end = from_civil(json["range"]["end"]["date"])
        if (end - first).days > (14 if data_type == "total-calories" else 90):
            return FakeResponse(status=400)
        if data_type in self.failing:
            return FakeResponse(status=500)
        if data_type in self.raw:
            return FakeResponse({"rollupDataPoints": self.raw[data_type]})
        key = ROLLUP_KEYS[data_type]
        points = [
            {"civilStartTime": {"date": civil(d)},
             "civilEndTime": {"date": civil(d + timedelta(days=1))},
             key: value}
            for d, value in sorted(self.data.get(data_type, {}).items(), reverse=True)
            if first <= d < end
        ]
        return FakeResponse({"rollupDataPoints": points})

    def get(self, url, headers=None, params=None, timeout=None):
        data_type = data_type_of(url)
        self.requests.append((data_type, params))
        if data_type in self.failing:
            return FakeResponse(status=500)
        if data_type in self.raw:
            return FakeResponse({"dataPoints": self.raw[data_type]})
        if data_type not in SUMMARY_KEYS:
            return FakeResponse({"dataPoints": []})
        key = SUMMARY_KEYS[data_type]
        return FakeResponse({"dataPoints": [
            {key: {"date": civil(d), **value}}
            for d, value in sorted(self.data.get(data_type, {}).items(), reverse=True)
        ]})


def data_type_of(url):
    return url.split("/dataTypes/")[1].split("/")[0]


@pytest.fixture
def google(conn, monkeypatch):
    """A FakeGoogle behind the real Google Health plugin, syncing on TODAY."""
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
    fake.sync = lambda days=7: PLUGINS["google_health"].sync(conn, days)
    return fake


def readings(conn, metric):
    return [
        tuple(r) for r in conn.execute(
            "SELECT date, value FROM metrics WHERE source = 'google_health' AND metric = ? "
            "ORDER BY date",
            (metric,),
        )
    ]


def filed_metrics(conn):
    return {
        r[0] for r in conn.execute(
            "SELECT DISTINCT metric FROM metrics WHERE source = 'google_health'"
        )
    }


# ---------- each type lands under its key, in its unit ----------


@pytest.mark.parametrize("case", ALL_TYPES, ids=type_id)
def test_each_type_is_filed_under_its_canonical_key_and_unit(conn, google, case):
    data_type, _key, _value, metric, filed = case
    google.seed([case])

    assert google.sync() == 1

    assert readings(conn, metric) == [(TODAY.isoformat(), filed)]
    unit = conn.execute(
        "SELECT unit FROM metrics WHERE source = 'google_health' AND metric = ?", (metric,)
    ).fetchone()[0]
    assert unit == unit_for(metric)


def test_every_type_files_on_the_same_day(conn, google):
    google.seed(ALL_TYPES)

    assert google.sync() == 1

    for _type, _key, _value, metric, filed in ALL_TYPES:
        assert readings(conn, metric) == [(TODAY.isoformat(), filed)]


def test_each_type_is_asked_for_once_by_its_documented_name(google):
    google.sync()

    rollups = [t for t, fields in google.requests if "windowSizeDays" in (fields or {})]
    series_rollups = [t for t, fields in google.requests if "windowSize" in (fields or {})]
    lists = [t for t, fields in google.requests if "filter" in (fields or {})]
    assert sorted(rollups) == sorted(t for t, *_ in ROLLUPS)
    assert series_rollups == ["heart-rate"]
    assert sorted(lists) == sorted(
        [t for t, *_ in SUMMARIES] + ["sleep", "exercise", "oxygen-saturation"]
    )


def test_rollups_ask_for_one_day_windows(google):
    google.sync()

    for data_type, *_ in ROLLUPS:
        (body,) = google.requests_for(data_type)
        assert body["windowSizeDays"] == 1


# ---------- the one window ----------


@pytest.mark.parametrize("days", [1, 7, 30])
def test_every_daily_fetch_asks_for_the_same_window(google, days):
    """Rollups by their range, closed-open; the daily summaries by their
    `.date`, which takes >= and < - so start to the day after today."""
    first = TODAY - timedelta(days=days - 1)
    after = TODAY + timedelta(days=1)

    google.sync(days=days)

    for data_type, *_ in ROLLUPS:
        ranges = [
            (from_civil(b["range"]["start"]["date"]), from_civil(b["range"]["end"]["date"]))
            for b in google.requests_for(data_type)
        ]
        # Back to back from the window's first day to the day after today.
        assert ranges[0][0] == first
        assert ranges[-1][1] == after
        assert all(end == nxt for (_, end), (nxt, _) in zip(ranges, ranges[1:]))
    for data_type, *_ in SUMMARIES:
        field = data_type.replace("-", "_")
        assert google.requests_for(data_type) == [{
            "filter": f'{field}.date >= "{first.isoformat()}" AND '
                      f'{field}.date < "{after.isoformat()}"'
        }]


def test_every_type_files_the_same_span(conn, google):
    """30 days of each type, and 10 older days besides that a list
    endpoint hands back anyway: every metric files exactly the window."""
    for back in range(40):
        google.seed(ALL_TYPES, d=TODAY - timedelta(days=back))

    assert google.sync(days=30) == 30

    window = [(TODAY - timedelta(days=back)).isoformat() for back in range(30)]
    for _type, _key, _value, metric, filed in ALL_TYPES:
        assert readings(conn, metric) == [(d, filed) for d in sorted(window)]


def test_total_calories_is_asked_for_14_days_at_a_time(conn, google):
    """Google takes at most 14 days of total-calories a request; the rest
    take 90, so a 30-day sync asks for them in one."""
    case = next(t for t in ROLLUPS if t[0] == "total-calories")
    for back in range(30):
        google.seed([case], d=TODAY - timedelta(days=back))

    google.sync(days=30)

    ranges = [
        (from_civil(b["range"]["start"]["date"]), from_civil(b["range"]["end"]["date"]))
        for b in google.requests_for("total-calories")
    ]
    assert ranges == [
        (TODAY - timedelta(days=29), TODAY - timedelta(days=15)),
        (TODAY - timedelta(days=15), TODAY - timedelta(days=1)),
        (TODAY - timedelta(days=1), TODAY + timedelta(days=1)),
    ]
    assert len(readings(conn, TOTAL_KCAL)) == 30
    assert len(google.requests_for("distance")) == 1


def test_a_point_dated_outside_the_window_is_dropped_and_logged(conn, google, capsys):
    case = next(t for t in SUMMARIES if t[0] == "daily-vo2-max")
    google.seed([case])
    google.seed([case], d=TODAY - timedelta(days=10))

    google.sync(days=7)

    assert readings(conn, VO2_MAX) == [(TODAY.isoformat(), 44.3)]
    assert "daily-vo2-max: dropped 1 points dated outside 2026-06-24 to 2026-06-30" in (
        capsys.readouterr().out
    )


# ---------- one type's trouble costs only that type ----------


@pytest.mark.parametrize("case", ALL_TYPES, ids=type_id)
def test_a_failed_fetch_costs_only_its_own_metric(conn, google, capsys, case):
    google.seed(ALL_TYPES)
    google.failing = {case[0]}

    assert google.sync() == 1

    assert readings(conn, case[3]) == []
    for _type, _key, _value, metric, filed in ALL_TYPES:
        if metric != case[3]:
            assert readings(conn, metric) == [(TODAY.isoformat(), filed)]
    assert f"[google_health] {case[0]} fetch failed" in capsys.readouterr().out


@pytest.mark.parametrize("case", ALL_TYPES, ids=type_id)
def test_an_empty_fetch_writes_nothing_and_says_so(conn, google, capsys, case):
    google.seed([t for t in ALL_TYPES if t is not case])

    assert google.sync() == 1

    assert case[3] not in filed_metrics(conn)
    assert len(filed_metrics(conn)) == len(ALL_TYPES) - 1
    assert f"[google_health] {case[0]}: no data from 2026-06-24 to 2026-06-30" in (
        capsys.readouterr().out
    )


def test_a_value_under_an_unexpected_field_logs_the_point_and_its_keys(conn, google, capsys):
    """Points came back but none held the documented field: nothing filed,
    and a sample point and its keys logged - the mark of a wrong name."""
    google.seed([t for t in ALL_TYPES if t[0] != "distance"])
    google.raw["distance"] = [{
        "civilStartTime": {"date": civil(TODAY)},
        "distance": {"metersSum": "6543"},
    }]

    assert google.sync() == 1

    assert readings(conn, DISTANCE_M) == []
    assert readings(conn, STEPS) == [(TODAY.isoformat(), 8123)]
    out = capsys.readouterr().out
    assert "[google_health] distance: got data but no millimetersSum in distance" in out
    assert "'metersSum': '6543'" in out
    assert "keys: ['civilStartTime', 'distance']" in out


def test_each_type_logs_what_it_filed(google, capsys):
    """One line a type: days filed, and the latest raw value beside the
    converted one - so a unit can be checked against a real sync."""
    google.seed(ALL_TYPES)

    google.sync()

    out = capsys.readouterr().out
    assert ("[google_health] distance -> distance_m: 1 days from 2026-06-24 to 2026-06-30, "
            "latest 2026-06-30: {'millimetersSum': '6543210'} -> 6543") in out
    assert ("[google_health] sedentary-period -> sedentary_min: 1 days from 2026-06-24 to "
            "2026-06-30, latest 2026-06-30: {'durationSum': '30600s'} -> 510") in out
    for data_type, _key, _value, metric, _filed in ALL_TYPES:
        assert f"[google_health] {data_type} -> {metric}: 1 days" in out


# ---------- a day with one new metric ----------


@pytest.mark.parametrize("case", NEW_TYPES, ids=type_id)
def test_a_day_holding_only_one_new_metric_still_lands(conn, google, case):
    google.seed([case], d=TODAY - timedelta(days=2))

    assert google.sync() == 1

    assert readings(conn, case[3]) == [((TODAY - timedelta(days=2)).isoformat(), case[4])]
    assert filed_metrics(conn) == {case[3]}


# ---------- only finite, plausible numbers ----------


@pytest.mark.parametrize("bad", ["", "abc", "NaN", "Infinity", True, [44.3], {"value": 44}])
def test_a_value_that_is_not_a_finite_number_writes_nothing(conn, google, capsys, bad):
    google.data["daily-vo2-max"] = {TODAY: {"vo2Max": bad}}
    google.seed([t for t in ROLLUPS if t[0] == "steps"])

    assert google.sync() == 1

    assert readings(conn, VO2_MAX) == []
    assert readings(conn, STEPS) == [(TODAY.isoformat(), 8123)]
    assert "daily-vo2-max: 1 not filed as vo2_max" in capsys.readouterr().out


def test_a_bool_is_not_a_count(conn, google):
    """True would pass for 1 floor if taken as a number."""
    google.data["floors"] = {TODAY: {"countSum": True}}

    assert google.sync() == 0

    assert readings(conn, FLOORS) == []


def test_a_null_value_is_no_reading(conn, google, capsys):
    google.data["daily-oxygen-saturation"] = {TODAY: {"averagePercentage": None}}

    assert google.sync() == 0

    assert readings(conn, SPO2_PCT) == []
    assert "not filed" not in capsys.readouterr().out


@pytest.mark.parametrize("data_type, value, metric", [
    # a net altitude change that went down, not a gain
    ("altitude", {"gainMillimetersSum": "-5000"}, ELEVATION_GAIN_M),
    # more sedentary minutes than a day holds
    ("sedentary-period", {"durationSum": "90000s"}, SEDENTARY_MIN),
    ("daily-oxygen-saturation", {"averagePercentage": 12.0}, SPO2_PCT),
])
def test_a_value_outside_the_plausible_range_is_logged_not_filed(conn, google, capsys,
                                                                  data_type, value, metric):
    google.data[data_type] = {TODAY: value}

    assert google.sync() == 0

    assert readings(conn, metric) == []
    out = capsys.readouterr().out
    assert f"{data_type}: 1 not filed as {metric}" in out
    # The raw value, so the unit or field can be checked.
    assert str(value) in out


# ---------- Active Zone Minutes ----------


def test_azm_sums_only_the_zones_present(conn, google):
    """Google leaves a zero out, so a day with fat-burn minutes alone has
    no cardio or peak field at all."""
    google.data["active-zone-minutes"] = {TODAY: {"sumInFatBurnHeartZone": "23"}}

    google.sync()

    assert readings(conn, ACTIVE_MINUTES) == [(TODAY.isoformat(), 23)]


def test_azm_is_never_split_into_moderate_and_vigorous_minutes(conn, google):
    google.seed([t for t in ROLLUPS if t[0] == "active-zone-minutes"])

    google.sync()

    assert readings(conn, INTENSITY_MODERATE_MIN) == []
    assert readings(conn, INTENSITY_VIGOROUS_MIN) == []
    assert filed_metrics(conn) == {ACTIVE_MINUTES}
