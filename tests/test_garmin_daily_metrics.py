"""
Garmin's daily activity metrics from get_stats, with the network stubbed.

The plugin already fetches get_stats each day for steps and resting HR.
The same response carries the day's distance, calories, floors and
intensity minutes, which it now writes too: six canonical daily metrics
(app/metrics.py), each a plain reading in the metrics table. The derived
pick and the access contract read whatever is there, so they take these
up with no change of their own - the last test here pins that.

floorsAscended and the two intensity-minute field names are best-known
guesses that still need checking against a real account, so the
defensive cases matter as much as the happy path: a field that isn't
there, or isn't a number, costs that one metric and nothing else.

No network: the Garmin client is a fake handed to sync(), as in
test_heart_rate_samples.py.
"""

from datetime import date

import pytest

from derived import recompute_derived
from metrics import (
    ACTIVE_KCAL,
    DISTANCE_M,
    FLOORS,
    INTENSITY_MODERATE_MIN,
    INTENSITY_VIGOROUS_MIN,
    TOTAL_KCAL,
)


class FrozenDate(date):
    """date, with today pinned to the fixture day."""

    @classmethod
    def today(cls):
        return cls(2026, 1, 2)


# get_stats() for 2026-01-02, trimmed to the fields read plus a few
# neighbours nothing reads. Garmin sends floors fractional (derived from
# metres climbed) and calories as floats.
GARMIN_STATS = {
    "calendarDate": "2026-01-02",
    "totalSteps": 9000,
    "restingHeartRate": 52,
    "totalDistanceMeters": 7012.6,
    "activeKilocalories": 412.0,
    "totalKilocalories": 2290.0,
    "bmrKilocalories": 1878.0,
    "floorsAscended": 11.98,
    "floorsDescended": 10.5,
    "moderateIntensityMinutes": 22,
    "vigorousIntensityMinutes": 9,
}

# What GARMIN_STATS files: (metric, value, unit), by metric.
GARMIN_READINGS = [
    ("active_kcal", 412, "kcal"),
    ("distance_m", 7013, "m"),
    ("floors", 12, "count"),
    ("intensity_moderate_min", 22, "min"),
    ("intensity_vigorous_min", 9, "min"),
    ("resting_hr_bpm", 52, "bpm"),
    ("steps", 9000, "count"),
    ("total_kcal", 2290, "kcal"),
]

# Each new metric and the get_stats field it comes from.
NEW_FIELDS = {
    "totalDistanceMeters": DISTANCE_M,
    "activeKilocalories": ACTIVE_KCAL,
    "totalKilocalories": TOTAL_KCAL,
    "floorsAscended": FLOORS,
    "moderateIntensityMinutes": INTENSITY_MODERATE_MIN,
    "vigorousIntensityMinutes": INTENSITY_VIGOROUS_MIN,
}


def readings(conn, source="garmin"):
    return [
        tuple(r) for r in conn.execute(
            "SELECT metric, value, unit FROM metrics WHERE source = ? ORDER BY metric",
            (source,),
        )
    ]


def without(rows, *metrics):
    return [row for row in rows if row[0] not in metrics]


class FakeGarmin:
    """Stands in for garminconnect.Garmin: canned stats by date; no
    sleep, vitals, heart rate or activities (those are
    test_sleep_sessions.py's, test_garmin_daily_vitals.py's,
    test_heart_rate_samples.py's and test_workout_sessions.py's)."""

    def __init__(self, stats_by_date):
        self.stats_by_date = stats_by_date

    def get_stats(self, cdate):
        if isinstance(self.stats_by_date, Exception):
            raise self.stats_by_date
        return self.stats_by_date.get(cdate, {})

    def get_sleep_data(self, cdate):
        return {"dailySleepDTO": {"calendarDate": cdate, "sleepTimeSeconds": None}}

    def get_heart_rates(self, cdate):
        return {"calendarDate": cdate, "heartRateValues": None}

    def get_spo2_data(self, cdate):
        return {}

    def get_respiration_data(self, cdate):
        return {}

    def get_hrv_data(self, cdate):
        return {}

    def get_activities_by_date(self, startdate, enddate=None):
        return []


@pytest.fixture
def garmin(monkeypatch):
    """The real Garmin plugin, syncing from a FakeGarmin on 2026-01-02."""
    from plugins import PLUGINS

    plugin = PLUGINS["garmin"]
    monkeypatch.setattr("plugins.garmin.plugin.date", FrozenDate)

    def sync_with(conn, stats, days=1):
        client = FakeGarmin(stats)
        monkeypatch.setattr(plugin, "_get_client", lambda conn=None: client)
        return plugin.sync(conn, days)

    return sync_with


def test_garmin_writes_the_days_stats_as_canonical_metrics(conn, garmin):
    assert garmin(conn, {"2026-01-02": GARMIN_STATS}) == 1

    assert readings(conn) == GARMIN_READINGS


def test_steps_and_resting_hr_write_exactly_as_before(conn, garmin):
    """A response with nothing but steps and resting HR files just those."""
    stats = {"totalSteps": 9000, "restingHeartRate": 52}

    assert garmin(conn, {"2026-01-02": stats}) == 1

    assert readings(conn) == [("resting_hr_bpm", 52, "bpm"), ("steps", 9000, "count")]


@pytest.mark.parametrize("field", sorted(NEW_FIELDS))
def test_a_field_the_response_leaves_out_is_skipped(conn, garmin, field):
    stats = {k: v for k, v in GARMIN_STATS.items() if k != field}

    assert garmin(conn, {"2026-01-02": stats}) == 1

    assert readings(conn) == without(GARMIN_READINGS, NEW_FIELDS[field])


@pytest.mark.parametrize("value", [None, "12", True, float("nan"), float("inf"), [12], {"v": 12}])
def test_a_field_that_is_not_a_number_is_skipped(conn, garmin, value):
    stats = {**GARMIN_STATS, "floorsAscended": value}

    assert garmin(conn, {"2026-01-02": stats}) == 1

    assert readings(conn) == without(GARMIN_READINGS, FLOORS)


def test_a_missing_field_is_logged_with_the_keys_there(conn, garmin, capsys):
    """Absent outright is most likely a wrong field name - say so."""
    stats = {k: v for k, v in GARMIN_STATS.items() if k != "floorsAscended"}

    garmin(conn, {"2026-01-02": stats})

    out = capsys.readouterr().out
    assert "stats for 2026-01-02 have no floorsAscended" in out
    assert "'totalKilocalories'" in out


def test_a_null_field_is_a_day_without_it_not_a_log_line(conn, garmin, capsys):
    stats = {**GARMIN_STATS, "floorsAscended": None, "vigorousIntensityMinutes": None}

    garmin(conn, {"2026-01-02": stats})

    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("field", sorted(NEW_FIELDS))
def test_a_day_with_only_a_new_metric_still_lands(conn, garmin, field):
    """Not dropped by the no-steps/resting-HR/sleep early skip."""
    stats = {"totalSteps": None, "restingHeartRate": None, field: 30}

    assert garmin(conn, {"2026-01-02": stats}) == 1

    assert [(m, v) for m, v, _ in readings(conn)] == [(NEW_FIELDS[field], 30)]


def test_a_day_of_nothing_is_still_skipped(conn, garmin):
    stats = {field: None for field in GARMIN_STATS}

    assert garmin(conn, {"2026-01-02": stats}) == 0

    assert readings(conn) == []


def test_a_failed_stats_fetch_writes_nothing(conn, garmin, capsys):
    assert garmin(conn, RuntimeError("503 Service Unavailable")) == 0

    assert readings(conn) == []
    assert "stats fetch failed for 2026-01-02" in capsys.readouterr().out


def test_a_resync_replaces_the_days_readings(conn, garmin):
    garmin(conn, {"2026-01-02": {**GARMIN_STATS, "totalKilocalories": 1900.0}})
    garmin(conn, {"2026-01-02": GARMIN_STATS})

    assert readings(conn) == GARMIN_READINGS


def test_the_new_metrics_flow_through_derive_and_the_access_contract(client, conn, garmin):
    garmin(conn, {"2026-01-02": GARMIN_STATS})
    recompute_derived(conn)

    catalog = {m["metric"]: m for m in client.get("/api/catalog").get_json()["metrics"]}
    for metric in NEW_FIELDS.values():
        assert catalog[metric]["sources"] == ["garmin"]

    assert client.get("/api/metric/distance_m").get_json() == {
        "metric": "distance_m",
        "unit": "m",
        "points": [{"date": "2026-01-02", "value": 7013.0, "source": "garmin"}],
    }
