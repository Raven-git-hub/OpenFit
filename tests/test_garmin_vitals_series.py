"""
Garmin's intraday respiration and SpO2 series, with the network stubbed.

get_respiration_data and get_spo2_data are already fetched a day at a
time for the daily vitals (test_garmin_daily_vitals.py), and the same
responses carry the day's series: respirationValuesArray, [epoch_ms,
brpm] rows about every two minutes with negative sentinels (-1, -2) for
no reading, and spO2HourlyAverages, [epoch_ms, %] rows an hour apart.
The plugin reads both grains from the one response - no extra request -
filing each real reading as a `respiration` or `spo2` sample: series
keys, distinct from the daily respiratory_rate_brpm and spo2_pct.

The converter, _series_samples, is tested directly first: it is pure,
and it is where a shape nobody expected has to be survived. Then the
sync path, through a fake client as in test_heart_rate_samples.py, and
the samples back out through GET /api/series/<metric>.

No network: the Garmin client is a fake handed to sync().
"""

from datetime import date

import pytest

from plugins.garmin.plugin import _series_samples

DAY = "2026-01-02"


class FrozenDate(date):
    """date, with today pinned to the fixture day."""

    @classmethod
    def today(cls):
        return cls(2026, 1, 2)


# 2026-01-02 for a watch on UTC+1, as in test_heart_rate_samples.py: the
# calendar day opens at local midnight, 2026-01-01T23:00:00Z.
DAY_START_MS = 1767308400000
TWO_MINUTES_MS = 120_000
HOUR_MS = 3_600_000


def rows(start_ms, step_ms, values):
    """[epoch_ms, value] rows, one per value, step_ms apart from start_ms."""
    return [[start_ms + i * step_ms, value] for i, value in enumerate(values)]


# get_respiration_data() for the day, trimmed: the waking average is the
# daily figure, the values array the series - opening on a sentinel, as
# the watch does before it has a reading, with another mid-stretch.
GARMIN_RESPIRATION = {
    "userProfilePK": 12345678,
    "calendarDate": DAY,
    "lowestRespirationValue": 8.0,
    "highestRespirationValue": 22.0,
    "avgWakingRespirationValue": 14.0,
    "avgSleepRespirationValue": 12.0,
    "respirationValueDescriptorsDTOList": [
        {"key": "timestamp", "index": 0},
        {"key": "respiration", "index": 1},
    ],
    "respirationValuesArray": rows(
        DAY_START_MS, TWO_MINUTES_MS, [-1.0, 13.0, 12.5, -2.0, 14.0]
    ),
}
RESPIRATION_SAMPLES = [
    ("2026-01-01T23:02:00Z", 13.0),
    ("2026-01-01T23:04:00Z", 12.5),
    ("2026-01-01T23:08:00Z", 14.0),
]

# get_spo2_data() for the day, trimmed: averageSpO2 the daily figure, the
# hourly averages the series. spO2SingleValues is null, as it is on the
# device these shapes came from.
GARMIN_SPO2 = {
    "userProfilePK": 12345678,
    "calendarDate": DAY,
    "averageSpO2": 95.0,
    "lowestSpO2": 88,
    "latestSpO2": 97,
    "spO2ValueDescriptorsDTOList": [
        {"key": "timestamp", "index": 0},
        {"key": "spo2Reading", "index": 1},
    ],
    "spO2HourlyAverages": rows(DAY_START_MS, HOUR_MS, [94, 96, 95]),
    "spO2SingleValues": None,
}
SPO2_SAMPLES = [
    ("2026-01-01T23:00:00Z", 94),
    ("2026-01-02T00:00:00Z", 96),
    ("2026-01-02T01:00:00Z", 95),
]

# get_hrv_data() for the day: a daily figure, and readings that are not
# a series here.
GARMIN_HRV = {
    "hrvSummary": {"calendarDate": DAY, "lastNightAvg": 45},
    "hrvReadings": [{"hrvValue": 42, "readingTimeGMT": "2026-01-02T03:01:17.0"}],
}

ALL_VITALS = {"spo2": GARMIN_SPO2, "respiration": GARMIN_RESPIRATION, "hrv": GARMIN_HRV}

# What ALL_VITALS files as daily metrics - exactly what it did before the
# series were read: (metric, value, unit), by metric.
VITAL_READINGS = [
    ("hrv_ms", 45, "ms"),
    ("respiratory_rate_brpm", 14.0, "brpm"),
    ("spo2_pct", 95.0, "%"),
]


# ---------- the converter ----------


def test_converter_reads_the_rows_as_utc_samples_in_order():
    assert _series_samples(GARMIN_SPO2["spO2HourlyAverages"]) == SPO2_SAMPLES


def test_converter_drops_the_negative_sentinels():
    """-1 and -2 are "no reading", not a breathing rate - so no samples."""
    assert _series_samples(GARMIN_RESPIRATION["respirationValuesArray"]) == RESPIRATION_SAMPLES


def test_converter_drops_nulls_and_zero():
    readings = rows(DAY_START_MS, TWO_MINUTES_MS, [None, 13.0, 0, 0.0, None, 15.0])

    assert _series_samples(readings) == [
        ("2026-01-01T23:02:00Z", 13.0),
        ("2026-01-01T23:10:00Z", 15.0),
    ]


def test_converter_keeps_non_positive_values_when_asked():
    readings = rows(DAY_START_MS, TWO_MINUTES_MS, [-1, 0, 13.0, None])

    assert _series_samples(readings, drop_non_positive=False) == [
        ("2026-01-01T23:00:00Z", -1),
        ("2026-01-01T23:02:00Z", 0),
        ("2026-01-01T23:04:00Z", 13.0),
    ]


def test_converter_on_rows_of_nothing_but_sentinels_is_empty():
    assert _series_samples(rows(DAY_START_MS, TWO_MINUTES_MS, [-1.0, -2.0, -1.0])) == []


@pytest.mark.parametrize("junk", [
    None,
    "not rows",
    "[[1767308400000, 13.0]]",
    {},
    {"respirationValuesArray": [[DAY_START_MS, 13.0]]},
    [],
    42,
])
def test_converter_survives_input_it_cannot_read(junk):
    assert _series_samples(junk) == []


def test_converter_skips_rows_it_cannot_read_and_keeps_the_rest():
    readings = GARMIN_RESPIRATION["respirationValuesArray"][:]
    readings[2:2] = [
        "1767308500000,13.0",               # not a row
        [1767308500000],                    # too short
        [],
        None,
        {"timestamp": 1767308500000, "respiration": 13.0},
        ["1767308500000", 13.0],            # timestamp not a number
        [None, 13.0],
        [True, 13.0],
        [float("nan"), 13.0],
        [1767308500000, "13.0"],            # value not a number
        [1767308500000, True],
        [1767308500000, float("nan")],
        [1767308500000, float("inf")],
        [10 ** 20, 13.0],                   # timestamp out of range
    ]

    assert _series_samples(readings) == RESPIRATION_SAMPLES


def test_converter_takes_the_value_at_index_one_of_a_longer_row():
    assert _series_samples([(DAY_START_MS, 94, "extra")]) == [("2026-01-01T23:00:00Z", 94)]


# ---------- through sync() ----------


def samples(conn, source="garmin"):
    return [
        tuple(r) for r in conn.execute(
            "SELECT metric, ts, value, unit FROM samples WHERE source = ? ORDER BY metric, ts",
            (source,),
        )
    ]


def as_rows(metric, unit, pairs):
    return [(metric, ts, float(value), unit) for ts, value in pairs]


RESPIRATION_ROWS = as_rows("respiration", "brpm", RESPIRATION_SAMPLES)
SPO2_ROWS = as_rows("spo2", "%", SPO2_SAMPLES)


def readings(conn, source="garmin"):
    return [
        tuple(r) for r in conn.execute(
            "SELECT metric, value, unit FROM metrics WHERE source = ? ORDER BY metric",
            (source,),
        )
    ]


class FakeGarmin:
    """Stands in for garminconnect.Garmin: the given SpO2, respiration and
    HRV responses by date - {} for a vital or day not given, as a device
    without that sensor answers - with an exception given in place of a
    response raised instead. No stats, sleep, heart rate or activities.
    Records every vitals call as (method, date)."""

    def __init__(self, vitals_by_date):
        self.vitals_by_date = vitals_by_date
        self.calls = []

    def _vital(self, name, method, cdate):
        self.calls.append((method, cdate))
        payload = self.vitals_by_date.get(cdate, {}).get(name, {})
        if isinstance(payload, Exception):
            raise payload
        return payload

    def get_stats(self, cdate):
        return {}

    def get_sleep_data(self, cdate):
        return {"dailySleepDTO": {"calendarDate": cdate, "sleepTimeSeconds": None}}

    def get_spo2_data(self, cdate):
        return self._vital("spo2", "get_spo2_data", cdate)

    def get_respiration_data(self, cdate):
        return self._vital("respiration", "get_respiration_data", cdate)

    def get_hrv_data(self, cdate):
        return self._vital("hrv", "get_hrv_data", cdate)

    def get_training_status(self, cdate):
        return {}

    def get_hydration_data(self, cdate):
        return {}

    def get_body_composition(self, startdate, enddate=None):
        return {}

    def get_heart_rates(self, cdate):
        return {"calendarDate": cdate, "heartRateValues": None}

    def get_activities_by_date(self, startdate, enddate=None):
        return []


@pytest.fixture
def garmin(monkeypatch):
    """The real Garmin plugin, syncing from a FakeGarmin on 2026-01-02.
    `vitals` is the fixture day's responses, or {date: responses} with
    by_date=True. The fake from the latest sync is garmin.client."""
    from plugins import PLUGINS

    plugin = PLUGINS["garmin"]
    monkeypatch.setattr("plugins.garmin.plugin.date", FrozenDate)

    def sync_with(conn, vitals, days=1, by_date=False):
        sync_with.client = FakeGarmin(vitals if by_date else {DAY: vitals})
        monkeypatch.setattr(plugin, "_get_client", lambda conn=None: sync_with.client)
        return plugin.sync(conn, days)

    return sync_with


def test_garmin_writes_the_respiration_and_spo2_series_as_samples(conn, garmin):
    """Sentinels gone, each in its series unit - and nothing from HRV."""
    garmin(conn, ALL_VITALS)

    assert samples(conn) == RESPIRATION_ROWS + SPO2_ROWS


def test_the_daily_vitals_are_exactly_as_before(conn, garmin):
    """The regression guard: reading the series from a response leaves the
    daily figure filed from it untouched."""
    assert garmin(conn, ALL_VITALS) == 1

    assert readings(conn) == VITAL_READINGS


def test_both_grains_come_from_the_one_fetch(conn, garmin):
    """No new request: each vitals method once per day of the window."""
    garmin(conn, ALL_VITALS, days=2)

    assert sorted(garmin.client.calls) == sorted(
        (method, d)
        for method in ("get_spo2_data", "get_respiration_data", "get_hrv_data")
        for d in (DAY, "2026-01-01")
    )


def test_each_day_of_the_window_files_its_own_series(conn, garmin):
    the_first = {
        "respiration": {
            "respirationValuesArray": rows(DAY_START_MS - 86_400_000, TWO_MINUTES_MS, [-1.0, 11.0]),
        },
    }

    garmin(conn, {DAY: ALL_VITALS, "2026-01-01": the_first}, days=2, by_date=True)

    assert samples(conn) == (
        as_rows("respiration", "brpm", [("2025-12-31T23:02:00Z", 11.0)])
        + RESPIRATION_ROWS
        + SPO2_ROWS
    )


def test_a_day_with_series_but_no_daily_figures_keeps_its_samples(conn, garmin):
    """Not dropped by the no-daily-summary skip, as heart rate isn't."""
    vitals = {
        "spo2": {"spO2HourlyAverages": GARMIN_SPO2["spO2HourlyAverages"]},
        "respiration": {
            "respirationValuesArray": GARMIN_RESPIRATION["respirationValuesArray"],
        },
    }

    # written still counts days with daily metrics, not samples.
    assert garmin(conn, vitals) == 0

    assert samples(conn) == RESPIRATION_ROWS + SPO2_ROWS
    assert readings(conn) == []


@pytest.mark.parametrize("series", [None, [], "nope", {"0": 94}])
def test_an_unreadable_or_empty_series_writes_no_samples_and_logs_nothing(
    conn, garmin, capsys, series
):
    """The daily figure from the same response still lands."""
    vitals = {**ALL_VITALS, "spo2": {**GARMIN_SPO2, "spO2HourlyAverages": series}}

    garmin(conn, vitals)

    assert samples(conn) == RESPIRATION_ROWS
    assert readings(conn) == VITAL_READINGS
    assert capsys.readouterr().out == ""


def test_a_response_without_the_series_key_writes_no_samples(conn, garmin):
    respiration = {
        k: v for k, v in GARMIN_RESPIRATION.items() if k != "respirationValuesArray"
    }

    garmin(conn, {**ALL_VITALS, "respiration": respiration})

    assert samples(conn) == SPO2_ROWS
    assert readings(conn) == VITAL_READINGS


@pytest.mark.parametrize("payload", [None, "95", [], [[DAY_START_MS, 94]]])
def test_a_response_that_is_not_a_dict_writes_nothing_from_it(conn, garmin, payload):
    garmin(conn, {**ALL_VITALS, "spo2": payload})

    assert samples(conn) == RESPIRATION_ROWS
    assert readings(conn) == [r for r in VITAL_READINGS if r[0] != "spo2_pct"]


@pytest.mark.parametrize(
    "vital, kept_rows, lost_metric, log_line",
    [
        ("spo2", RESPIRATION_ROWS, "spo2_pct", f"SpO2 fetch failed for {DAY}"),
        ("respiration", SPO2_ROWS, "respiratory_rate_brpm", f"respiration fetch failed for {DAY}"),
    ],
)
def test_a_failed_fetch_costs_that_vitals_metric_and_series_only(
    conn, garmin, capsys, vital, kept_rows, lost_metric, log_line
):
    garmin(conn, {**ALL_VITALS, vital: RuntimeError("503 Service Unavailable")})

    assert samples(conn) == kept_rows
    assert readings(conn) == [r for r in VITAL_READINGS if r[0] != lost_metric]
    assert log_line in capsys.readouterr().out


def test_a_resync_replaces_the_series_in_place(conn, garmin):
    earlier = {
        **ALL_VITALS,
        "spo2": {
            **GARMIN_SPO2,
            "spO2HourlyAverages": rows(DAY_START_MS, HOUR_MS, [90, 91, 92]),
        },
    }
    garmin(conn, earlier)
    garmin(conn, ALL_VITALS)

    assert samples(conn) == RESPIRATION_ROWS + SPO2_ROWS


# ---------- through the access contract ----------


def test_the_series_come_back_through_the_access_contract(client, conn, garmin):
    garmin(conn, ALL_VITALS)

    assert client.get("/api/series/respiration").get_json() == {
        "metric": "respiration",
        "unit": "brpm",
        "points": [
            {"ts": "2026-01-01T23:02:00Z", "value": 13, "source": "garmin"},
            {"ts": "2026-01-01T23:04:00Z", "value": 12.5, "source": "garmin"},
            {"ts": "2026-01-01T23:08:00Z", "value": 14, "source": "garmin"},
        ],
    }
    assert client.get("/api/series/spo2").get_json() == {
        "metric": "spo2",
        "unit": "%",
        "points": [
            {"ts": ts, "value": value, "source": "garmin"} for ts, value in SPO2_SAMPLES
        ],
    }
    # A day's range is its UTC date: the 23:00Z hour belongs to the 1st.
    assert [
        p["ts"] for p in client.get("/api/series/spo2?from=2026-01-02").get_json()["points"]
    ] == ["2026-01-02T00:00:00Z", "2026-01-02T01:00:00Z"]


def test_the_catalog_lists_both_series(client, conn, garmin):
    garmin(conn, ALL_VITALS)

    assert client.get("/api/catalog").get_json()["series"] == [
        {
            "metric": "respiration",
            "unit": "brpm",
            "sources": ["garmin"],
            "first": "2026-01-01T23:02:00Z",
            "last": "2026-01-01T23:08:00Z",
            "count": 3,
        },
        {
            "metric": "spo2",
            "unit": "%",
            "sources": ["garmin"],
            "first": "2026-01-01T23:00:00Z",
            "last": "2026-01-02T01:00:00Z",
            "count": 3,
        },
    ]
