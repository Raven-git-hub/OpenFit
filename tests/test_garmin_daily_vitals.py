"""
Garmin's daily vitals - SpO2, respiration rate and HRV - with the network
stubbed.

Each is its own request a day (get_spo2_data, get_respiration_data,
get_hrv_data), and each files one canonical daily metric (app/metrics.py)
from one field of its response: averageSpO2 -> spo2_pct,
avgWakingRespirationValue -> respiratory_rate_brpm, and the nested
hrvSummary.lastNightAvg -> hrv_ms. Daily values only here: the same
SpO2 and respiration responses also carry the intraday series
(spO2HourlyAverages, respirationValuesArray), read from the one fetch and
tested in test_garmin_vitals_series.py. The payloads below keep those
arrays in, so every test here is also a guard that reading the series
leaves the daily figures exactly as they were.

A device without HRV answers get_hrv_data with {} - a normal day, not a
wrong field name - so that writes nothing and logs nothing. A failed
fetch costs its one metric; everything else about the day still lands.

No network: the Garmin client is a fake handed to sync(), as in
test_garmin_daily_metrics.py.
"""

from datetime import date

import pytest

from derived import recompute_derived
from metrics import HRV_MS, RESPIRATORY_RATE_BRPM, SPO2_PCT

DAY = "2026-01-02"


class FrozenDate(date):
    """date, with today pinned to the fixture day."""

    @classmethod
    def today(cls):
        return cls(2026, 1, 2)


# get_spo2_data() for the day, trimmed. averageSpO2 is the day's figure;
# the lowest/latest/seven-day/sleep values beside it are distinct so a
# mix-up shows, and the hourly array is the series'.
GARMIN_SPO2 = {
    "calendarDate": DAY,
    "averageSpO2": 95.0,
    "lowestSpO2": 88,
    "latestSpO2": 97,
    "lastSevenDaysAvgSpO2": 94.71,
    "avgSleepSpO2": 93.0,
    "spO2HourlyAverages": [[1767312000000, 94], [1767315600000, 96]],
}

# get_respiration_data() for the day, trimmed. The values array is the
# series', with a negative sentinel for no reading.
GARMIN_RESPIRATION = {
    "calendarDate": DAY,
    "lowestRespirationValue": 8.0,
    "highestRespirationValue": 22.0,
    "avgWakingRespirationValue": 14.0,
    "avgSleepRespirationValue": 12.0,
    "respirationValueDescriptorsDTOList": [
        {"key": "timestamp", "index": 0},
        {"key": "respiration", "index": 1},
    ],
    "respirationValuesArray": [[1767312000000, 13.0], [1767312120000, -1.0]],
}

# get_hrv_data() for the day on a watch that measures it, trimmed.
GARMIN_HRV = {
    "hrvSummary": {
        "calendarDate": DAY,
        "weeklyAvg": 48,
        "lastNightAvg": 45,
        "lastNight5MinHigh": 68,
        "status": "BALANCED",
    },
    "hrvReadings": [{"hrvValue": 42, "readingTimeGMT": "2026-01-02T03:01:17.0"}],
}

ALL_VITALS = {"spo2": GARMIN_SPO2, "respiration": GARMIN_RESPIRATION, "hrv": GARMIN_HRV}

# What ALL_VITALS files: (metric, value, unit), by metric.
VITAL_READINGS = [
    ("hrv_ms", 45, "ms"),
    ("respiratory_rate_brpm", 14.0, "brpm"),
    ("spo2_pct", 95.0, "%"),
]

# Each vital's metric, and the line a failed fetch of it logs.
VITAL_METRICS = {"spo2": SPO2_PCT, "respiration": RESPIRATORY_RATE_BRPM, "hrv": HRV_MS}
FAILED_FETCH_LOG = {
    "spo2": f"SpO2 fetch failed for {DAY}",
    "respiration": f"respiration fetch failed for {DAY}",
    "hrv": f"HRV fetch failed for {DAY}",
}

# Steps and resting HR from get_stats, for a day with more than vitals.
STATS = {"totalSteps": 9000, "restingHeartRate": 52}
STATS_READINGS = [("resting_hr_bpm", 52, "bpm"), ("steps", 9000, "count")]


def readings(conn, source="garmin"):
    return [
        tuple(r) for r in conn.execute(
            "SELECT metric, value, unit FROM metrics WHERE source = ? ORDER BY metric",
            (source,),
        )
    ]


def without(rows, *metrics):
    return [row for row in rows if row[0] not in metrics]


def heart_rate_sample_count(conn):
    return conn.execute(
        "SELECT COUNT(*) FROM samples WHERE metric = 'heart_rate'"
    ).fetchone()[0]


class FakeGarmin:
    """Stands in for garminconnect.Garmin: the given SpO2, respiration
    and HRV responses for the fixture day - {} for a vital not given, or
    for any other day, as a device without that sensor answers - with an
    exception given in place of a response raised instead. Stats and
    heart rate if given; no sleep or activities. Records every vitals
    call as (vital, date)."""

    def __init__(self, vitals, stats=None, hr=None):
        self.vitals = vitals
        self.stats = stats or {}
        self.hr = hr
        self.vital_calls = []

    def _vital(self, name, cdate):
        self.vital_calls.append((name, cdate))
        payload = self.vitals.get(name, {}) if cdate == DAY else {}
        if isinstance(payload, Exception):
            raise payload
        return payload

    def get_stats(self, cdate):
        return self.stats if cdate == DAY else {}

    def get_sleep_data(self, cdate):
        return {"dailySleepDTO": {"calendarDate": cdate, "sleepTimeSeconds": None}}

    def get_spo2_data(self, cdate):
        return self._vital("spo2", cdate)

    def get_respiration_data(self, cdate):
        return self._vital("respiration", cdate)

    def get_hrv_data(self, cdate):
        return self._vital("hrv", cdate)

    def get_heart_rates(self, cdate):
        if self.hr is not None and cdate == DAY:
            return self.hr
        return {"calendarDate": cdate, "heartRateValues": None}

    def get_activities_by_date(self, startdate, enddate=None):
        return []


@pytest.fixture
def garmin(monkeypatch):
    """The real Garmin plugin, syncing from a FakeGarmin on 2026-01-02.
    The fake from the latest sync is garmin.client."""
    from plugins import PLUGINS

    plugin = PLUGINS["garmin"]
    monkeypatch.setattr("plugins.garmin.plugin.date", FrozenDate)

    def sync_with(conn, vitals, days=1, stats=None, hr=None):
        sync_with.client = FakeGarmin(vitals, stats, hr)
        monkeypatch.setattr(plugin, "_get_client", lambda conn=None: sync_with.client)
        return plugin.sync(conn, days)

    return sync_with


def test_garmin_writes_the_days_vitals_as_canonical_metrics(conn, garmin):
    assert garmin(conn, ALL_VITALS, stats=STATS) == 1

    assert readings(conn) == sorted(STATS_READINGS + VITAL_READINGS)


@pytest.mark.parametrize(
    "vital, payload, reading",
    [
        ("spo2", GARMIN_SPO2, ("spo2_pct", 95.0, "%")),
        ("respiration", GARMIN_RESPIRATION, ("respiratory_rate_brpm", 14.0, "brpm")),
        ("hrv", GARMIN_HRV, ("hrv_ms", 45, "ms")),
    ],
)
def test_each_vital_is_read_from_its_daily_field(conn, garmin, vital, payload, reading):
    """averageSpO2, avgWakingRespirationValue, hrvSummary.lastNightAvg -
    not a neighbour, and stored as reported."""
    garmin(conn, {vital: payload}, stats=STATS)

    assert readings(conn) == sorted(STATS_READINGS + [reading])


def test_a_fractional_vital_is_stored_as_reported(conn, garmin):
    garmin(conn, {"respiration": {"avgWakingRespirationValue": 12.5}})

    assert readings(conn) == [("respiratory_rate_brpm", 12.5, "brpm")]


@pytest.mark.parametrize(
    "payload",
    [{}, None, {"hrvSummary": None}, {"hrvSummary": {}}, {"hrvSummary": {"lastNightAvg": None}}],
)
def test_no_hrv_writes_nothing_and_logs_nothing(conn, garmin, capsys, payload):
    """{} is what a device without HRV sends: a normal day, not an error."""
    vitals = {**ALL_VITALS, "hrv": payload}

    assert garmin(conn, vitals) == 1

    assert readings(conn) == without(VITAL_READINGS, HRV_MS)
    assert capsys.readouterr().out == ""


def test_a_day_with_no_vitals_at_all_logs_nothing(conn, garmin, capsys):
    """Each fetch comes back {}: a day of nothing, skipped quietly."""
    assert garmin(conn, {}) == 0

    assert readings(conn) == []
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("value", [None, "95", True, float("nan"), float("inf"), [95], {"v": 95}])
def test_a_vital_that_is_not_a_number_is_skipped(conn, garmin, value):
    vitals = {**ALL_VITALS, "spo2": {**GARMIN_SPO2, "averageSpO2": value}}

    garmin(conn, vitals)

    assert readings(conn) == without(VITAL_READINGS, SPO2_PCT)


@pytest.mark.parametrize("payload", ["95", [], [{"lastNightAvg": 45}], {"hrvSummary": [45]}])
def test_a_response_of_the_wrong_shape_writes_nothing(conn, garmin, payload):
    vitals = {**ALL_VITALS, "hrv": payload}

    garmin(conn, vitals)

    assert readings(conn) == without(VITAL_READINGS, HRV_MS)


@pytest.mark.parametrize("vital", sorted(VITAL_METRICS))
def test_a_failed_vital_fetch_costs_only_that_metric(conn, garmin, capsys, vital):
    hr = {"calendarDate": DAY, "heartRateValues": [[1767312000000, 61]]}
    vitals = {**ALL_VITALS, vital: RuntimeError("503 Service Unavailable")}

    assert garmin(conn, vitals, stats=STATS, hr=hr) == 1

    assert readings(conn) == sorted(
        STATS_READINGS + without(VITAL_READINGS, VITAL_METRICS[vital])
    )
    assert heart_rate_sample_count(conn) == 1
    assert FAILED_FETCH_LOG[vital] in capsys.readouterr().out


@pytest.mark.parametrize("vital", sorted(VITAL_METRICS))
def test_a_day_whose_only_data_is_one_vital_still_lands(conn, garmin, vital):
    """Not dropped by the no-steps/resting-HR/sleep/stats early skip."""
    stats = {"totalSteps": None, "restingHeartRate": None}

    assert garmin(conn, {vital: ALL_VITALS[vital]}, stats=stats) == 1

    assert [m for m, _, _ in readings(conn)] == [VITAL_METRICS[vital]]


def test_a_day_whose_vital_fetches_all_fail_is_still_skipped(conn, garmin, capsys):
    error = RuntimeError("503 Service Unavailable")

    assert garmin(conn, {vital: error for vital in VITAL_METRICS}) == 0

    assert readings(conn) == []
    out = capsys.readouterr().out
    for line in FAILED_FETCH_LOG.values():
        assert line in out


def test_each_vital_is_fetched_for_each_day_of_the_window(conn, garmin):
    garmin(conn, ALL_VITALS, days=2)

    assert sorted(garmin.client.vital_calls) == sorted(
        (vital, d) for vital in VITAL_METRICS for d in (DAY, "2026-01-01")
    )


def test_a_resync_replaces_the_days_vitals(conn, garmin):
    garmin(conn, {**ALL_VITALS, "spo2": {**GARMIN_SPO2, "averageSpO2": 91.0}})
    garmin(conn, ALL_VITALS)

    assert readings(conn) == VITAL_READINGS


def test_the_vitals_flow_through_derive_and_the_access_contract(client, conn, garmin):
    garmin(conn, ALL_VITALS)
    recompute_derived(conn)

    catalog = {m["metric"]: m for m in client.get("/api/catalog").get_json()["metrics"]}
    for metric in VITAL_METRICS.values():
        assert catalog[metric]["sources"] == ["garmin"]

    assert client.get("/api/metric/spo2_pct").get_json() == {
        "metric": "spo2_pct",
        "unit": "%",
        "points": [{"date": DAY, "value": 95.0, "source": "garmin"}],
    }
