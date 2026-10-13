"""
Intraday heart rate from the real Garmin plugin, with the network stubbed.

Garmin's get_heart_rates(date) carries the day's heart rate as
heartRateValues: [epoch_ms, bpm] pairs on a steady ~2-minute cadence,
where a bpm of null is non-wear (and the first pair often is one). The
plugin writes each real reading as a `heart_rate` sample - the series
grain, beside the daily metrics and not in place of any of them.

The parser is tested directly first: it is pure, and it is where a shape
nobody expected has to be survived - a payload that isn't what's on paper
costs the samples, never the sync. Then the sync path, through a fake
client like the ones in test_sleep_sessions.py and test_workout_sessions.py.

No network: the Garmin client is a fake handed to sync().
"""

import copy
from datetime import date

import pytest

from plugins.garmin.plugin import _heart_rate_samples


class FrozenDate(date):
    """date, with today pinned to the fixture day."""

    @classmethod
    def today(cls):
        return cls(2026, 1, 2)


def hr_payload(start_ms, bpms, cdate="2026-01-02"):
    """A get_heart_rates() response: one pair per bpm, 120 s apart from
    start_ms, trimmed of the fields nothing reads."""
    return {
        "userProfilePK": 12345678,
        "calendarDate": cdate,
        "maxHeartRate": 142,
        "minHeartRate": 48,
        "restingHeartRate": 52,
        "lastSevenDaysAvgRestingHeartRate": 53,
        "heartRateValueDescriptors": [
            {"key": "timestamp", "index": 0},
            {"key": "heartrate", "index": 1},
        ],
        "heartRateValues": [
            [start_ms + i * 120_000, bpm] for i, bpm in enumerate(bpms)
        ],
    }


# 2026-01-02 for a watch on UTC+1: the calendar day opens at local
# midnight, 2026-01-01T23:00:00Z, with the usual leading null.
DAY_START_MS = 1767308400000
GARMIN_DAY = hr_payload(DAY_START_MS, [None, 58, 57, 59, 61])
GARMIN_SAMPLES = [
    ("2026-01-01T23:02:00Z", 58),
    ("2026-01-01T23:04:00Z", 57),
    ("2026-01-01T23:06:00Z", 59),
    ("2026-01-01T23:08:00Z", 61),
]


# ---------- the parser ----------


def test_parser_reads_the_real_readings_as_utc_samples_in_order():
    assert _heart_rate_samples(GARMIN_DAY) == GARMIN_SAMPLES


def test_parser_drops_non_wear_nulls():
    """A gap mid-day (the watch off for a while) is no samples, not zeros."""
    payload = hr_payload(DAY_START_MS, [None, 58, None, None, 62, None])

    assert _heart_rate_samples(payload) == [
        ("2026-01-01T23:02:00Z", 58),
        ("2026-01-01T23:08:00Z", 62),
    ]


def test_parser_on_a_day_of_nothing_but_nulls_is_empty():
    assert _heart_rate_samples(hr_payload(DAY_START_MS, [None, None, None])) == []


@pytest.mark.parametrize("payload", [
    None,
    "not a payload",
    [[DAY_START_MS, 58]],
    {},
    {"heartRateValues": None},
    {"heartRateValues": "[[1767308400000, 58]]"},
    {"heartRateValues": {"1767308400000": 58}},
])
def test_parser_survives_a_payload_it_cannot_read(payload):
    assert _heart_rate_samples(payload) == []


def test_parser_skips_pairs_it_cannot_read_and_keeps_the_rest():
    payload = copy.deepcopy(GARMIN_DAY)
    payload["heartRateValues"][2:2] = [
        "1767308500000,60",                 # not a pair
        [1767308500000],                    # too short
        [],
        None,
        {"timestamp": 1767308500000, "heartrate": 60},
        ["1767308500000", 60],              # timestamp not a number
        [None, 60],
        ["2026-01-01T23:03:00Z", 60],
        [1767308500000, "60"],              # bpm not a number
        [10 ** 20, 60],                     # timestamp out of range
    ]

    assert _heart_rate_samples(payload) == GARMIN_SAMPLES


# ---------- through sync() ----------


def heart_rate_samples(conn, source="garmin"):
    return [
        tuple(r) for r in conn.execute(
            "SELECT metric, ts, value, unit FROM samples WHERE source = ? ORDER BY ts",
            (source,),
        )
    ]


def as_rows(samples):
    return [("heart_rate", ts, float(bpm), "bpm") for ts, bpm in samples]


class FakeGarmin:
    """Stands in for garminconnect.Garmin: canned heart rate and stats by
    date, recording every get_heart_rates call; no sleep and no
    activities (those are test_sleep_sessions.py's and
    test_workout_sessions.py's)."""

    def __init__(self, hr_by_date, stats_by_date=None):
        self.hr_by_date = hr_by_date
        self.stats_by_date = stats_by_date or {}
        self.hr_calls = []

    def get_stats(self, cdate):
        return self.stats_by_date.get(cdate, {})

    def get_sleep_data(self, cdate):
        return {"dailySleepDTO": {"calendarDate": cdate, "sleepTimeSeconds": None}}

    def get_heart_rates(self, cdate):
        self.hr_calls.append(cdate)
        if isinstance(self.hr_by_date, Exception):
            raise self.hr_by_date
        return self.hr_by_date.get(cdate, {"calendarDate": cdate, "heartRateValues": None})

    def get_activities_by_date(self, startdate, enddate=None):
        return []


@pytest.fixture
def garmin(monkeypatch):
    """The real Garmin plugin, syncing from a FakeGarmin on 2026-01-02.
    The fake from the latest sync is garmin.client."""
    from plugins import PLUGINS

    plugin = PLUGINS["garmin"]
    monkeypatch.setattr("plugins.garmin.plugin.date", FrozenDate)

    def sync_with(conn, hr, days=1, stats=None):
        sync_with.client = FakeGarmin(hr, stats)
        monkeypatch.setattr(plugin, "_get_client", lambda conn=None: sync_with.client)
        return plugin.sync(conn, days)

    return sync_with


def test_garmin_writes_the_days_heart_rate_as_samples(conn, garmin):
    stats = {"2026-01-02": {"totalSteps": 9000, "restingHeartRate": 52}}

    # written still counts days with daily metrics, not samples.
    assert garmin(conn, {"2026-01-02": GARMIN_DAY}, stats=stats) == 1

    assert heart_rate_samples(conn) == as_rows(GARMIN_SAMPLES)
    # ...beside the day's metrics, which are exactly as before.
    assert [tuple(r) for r in conn.execute(
        "SELECT metric, value FROM metrics WHERE source = 'garmin' ORDER BY metric"
    )] == [("resting_hr_bpm", 52), ("steps", 9000)]


def test_garmin_asks_for_heart_rate_a_day_at_a_time(conn, garmin):
    the_first = hr_payload(DAY_START_MS - 86_400_000, [64, 66], cdate="2026-01-01")

    garmin(conn, {"2026-01-02": GARMIN_DAY, "2026-01-01": the_first}, days=2)

    assert garmin.client.hr_calls == ["2026-01-02", "2026-01-01"]
    assert heart_rate_samples(conn) == as_rows(
        [("2025-12-31T23:00:00Z", 64), ("2025-12-31T23:02:00Z", 66)] + GARMIN_SAMPLES
    )


def test_garmin_keeps_heart_rate_on_a_day_without_a_daily_summary(conn, garmin):
    """No steps, resting HR or sleep for the day - the samples still land."""
    assert garmin(conn, {"2026-01-02": GARMIN_DAY}) == 0

    assert heart_rate_samples(conn) == as_rows(GARMIN_SAMPLES)
    assert conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 0


def test_garmin_resync_of_the_same_samples_upserts(conn, garmin):
    garmin(conn, {"2026-01-02": hr_payload(DAY_START_MS, [None, 70, 70, 70, 70])})
    garmin(conn, {"2026-01-02": GARMIN_DAY})

    assert heart_rate_samples(conn) == as_rows(GARMIN_SAMPLES)


def test_an_unreadable_heart_rate_payload_never_breaks_the_sync(conn, garmin):
    stats = {"2026-01-02": {"totalSteps": 9000}}

    assert garmin(conn, {"2026-01-02": {"heartRateValues": "nope"}}, stats=stats) == 1

    assert heart_rate_samples(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 1


def test_a_failed_heart_rate_fetch_costs_only_the_samples(conn, garmin, capsys):
    stats = {"2026-01-02": {"totalSteps": 9000, "restingHeartRate": 52}}

    assert garmin(conn, RuntimeError("503 Service Unavailable"), stats=stats) == 1

    assert heart_rate_samples(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM metrics").fetchone()[0] == 2
    assert "heart-rate fetch failed for 2026-01-02" in capsys.readouterr().out
