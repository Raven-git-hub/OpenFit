"""
Google Health's intraday series (schema v2, PR 3b), with the network stubbed.

Beside its daily metrics, Google files two series as samples - the grain
Garmin already fills - at one value a minute (SERIES_WINDOW_SECONDS):

  - heart_rate from `heart-rate`, which Google keeps at about one reading
    a second. The v4 discovery doc gives it a windowed rollUp (a
    RollUpDataPointsRequest with a `windowSize`, at most 14 days a
    request), so the plugin asks for 60-second buckets and Google averages
    each minute server-side (HeartRateRollupValue.beatsPerMinuteAvg) -
    the per-second points never cross the wire.
  - spo2 from `oxygen-saturation`, already about one a minute and only
    for valid minutes asleep: listed, filtered on its sample's physical
    time, and stored as it comes.

The fixtures are in the discovery doc's shapes. These pin what is asked
for and over which span; that each lands as a whole-number sample at its
UTC time; that a re-sync upserts; that one series' trouble costs only that
series; and that GET /api/series lists Garmin's and Google's heart rate
side by side, each tagged - no stitch yet.
"""

import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone

import pytest

from crypto import encrypt
from metrics import HEART_RATE, RESTING_HR_BPM, SPO2, STEPS
from plugins.base import write_sample
from plugins.google_health.plugin import MAX_PAGES, SERIES_WINDOW_SECONDS

TODAY = date(2026, 6, 30)
MINUTE = timedelta(seconds=SERIES_WINDOW_SECONDS)


class FrozenDate(date):
    """date, with today pinned to TODAY."""

    @classmethod
    def today(cls):
        return cls(TODAY.year, TODAY.month, TODAY.day)


@pytest.fixture(autouse=True)
def local_tz():
    """The server's local zone, UTC unless a test sets another: series
    days run local midnight to local midnight. Restored afterwards."""
    saved = os.environ.get("TZ")

    def set_tz(name):
        os.environ["TZ"] = name
        time.tzset()

    set_tz("UTC")
    yield set_tz
    if saved is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = saved
    time.tzset()


def at(text):
    """An RFC 3339 UTC timestamp as an aware datetime."""
    return datetime.fromisoformat(text)


def rfc(moment):
    """As Google writes a time: UTC, with milliseconds if it has any."""
    moment = moment.astimezone(timezone.utc)
    millis = f".{moment.microsecond // 1000:03d}" if moment.microsecond else ""
    return moment.strftime("%Y-%m-%dT%H:%M:%S") + millis + "Z"


def minutes(start, values):
    """{minute: value}, one minute apart from `start` (an RFC 3339 string)."""
    return {at(start) + i * MINUTE: v for i, v in enumerate(values)}


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
    """Google Health's endpoints, recording every data request as (data
    type, endpoint, query params or body).

    heart-rate's rollUp answers a bucket per minute of `hr` ({minute:
    average bpm}) inside the request's range, a None average as a bucket
    with no heartRate (non-wear), pageSize to a page - and, like Google,
    refuses a range over 14 days. oxygen-saturation's list answers each
    point of `spo2` ({time: percentage}) its filter takes, newest first.
    `raw[data_type]` serves those points verbatim instead, whatever was
    asked; a type in `failing` gets a 500, one in `endless` a token on
    every page. Steps and resting HR answer TODAY's figure, so the daily
    metrics can be seen to survive; every other type is empty.
    """

    def __init__(self):
        self.hr = {}
        self.spo2 = {}
        self.raw = {}
        self.failing = set()
        self.endless = set()
        self.requests = []

    def requests_for(self, data_type):
        return [fields for t, _endpoint, fields in self.requests if t == data_type]

    def rollup_ranges(self):
        """Each heart-rate window asked for, as (start, end) - once
        however many pages it took."""
        return [
            (body["range"]["startTime"], body["range"]["endTime"])
            for body in self.requests_for("heart-rate") if "pageToken" not in body
        ]

    def post(self, url, data=None, json=None, headers=None, timeout=None):
        if "oauth2" in url:
            return FakeResponse({"access_token": "access-xyz"})
        data_type = data_type_of(url)
        endpoint = url.rsplit(":", 1)[1]
        self.requests.append((data_type, endpoint, json))
        if data_type in self.failing:
            return FakeResponse(status=500)
        if endpoint == "dailyRollUp":
            if data_type != "steps":
                return FakeResponse({"rollupDataPoints": []})
            return FakeResponse({"rollupDataPoints": [{
                "civilStartTime": {"date": civil(TODAY)},
                "steps": {"countSum": "8123"},
            }]})
        assert endpoint == "rollUp" and data_type == "heart-rate"
        start, end = at(json["range"]["startTime"]), at(json["range"]["endTime"])
        if end - start > timedelta(days=14):
            return FakeResponse(status=400)
        offset = int(json.get("pageToken") or 0)
        if data_type in self.endless:
            bucket = start + offset * MINUTE
            return FakeResponse({
                "rollupDataPoints": [hr_bucket(bucket, 70.0)],
                "nextPageToken": str(offset + 1),
            })
        if data_type in self.raw:
            return FakeResponse({"rollupDataPoints": self.raw[data_type]})
        points = [hr_bucket(m, bpm) for m, bpm in sorted(self.hr.items()) if start <= m < end]
        size = json.get("pageSize", 1440)
        page = {"rollupDataPoints": points[offset:offset + size]}
        if offset + size < len(points):
            page["nextPageToken"] = str(offset + size)
        return FakeResponse(page)

    def get(self, url, headers=None, params=None, timeout=None):
        data_type = data_type_of(url)
        self.requests.append((data_type, "list", params))
        if data_type in self.failing:
            return FakeResponse(status=500)
        if data_type in self.raw:
            return FakeResponse({"dataPoints": self.raw[data_type]})
        if data_type == "daily-resting-heart-rate":
            return FakeResponse({"dataPoints": [{"dailyRestingHeartRate": {
                "date": civil(TODAY), "beatsPerMinute": "58",
            }}]})
        if data_type != "oxygen-saturation":
            return FakeResponse({"dataPoints": []})
        low, high = re.findall(r'"([^"]+)"', params["filter"])
        return FakeResponse({"dataPoints": [
            spo2_point(t, pct)
            for t, pct in sorted(self.spo2.items(), reverse=True)
            if at(low) <= t < at(high)
        ]})


def hr_bucket(minute, bpm):
    """One rollUp point: a bucket's window and, if there was a reading in
    it, its average, max and min."""
    point = {"startTime": rfc(minute), "endTime": rfc(minute + MINUTE)}
    if bpm is not None:
        point["heartRate"] = {"beatsPerMinuteAvg": bpm}
        if isinstance(bpm, float):
            point["heartRate"].update(beatsPerMinuteMax=bpm + 3, beatsPerMinuteMin=bpm - 2)
    return point


def spo2_point(moment, pct):
    return {"oxygenSaturation": {
        "sampleTime": {"physicalTime": rfc(moment), "utcOffset": "3600s"},
        "percentage": pct,
    }}


def civil(d):
    return {"year": d.year, "month": d.month, "day": d.day}


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


def samples(conn, metric, source="google_health"):
    return [
        tuple(r) for r in conn.execute(
            "SELECT ts, value, unit FROM samples WHERE source = ? AND metric = ? ORDER BY ts",
            (source, metric),
        )
    ]


def daily(conn):
    return {
        r[0]: r[1] for r in conn.execute(
            "SELECT metric, value FROM metrics WHERE source = 'google_health'"
        )
    }


# A morning's heart rate, Google's per-minute averages as doubles, and the
# whole bpm each files as.
HR = minutes("2026-06-30T07:00:00Z", [61.4, 62.6, 70.2])
HR_SAMPLES = [
    ("2026-06-30T07:00:00Z", 61.0, "bpm"),
    ("2026-06-30T07:01:00Z", 63.0, "bpm"),
    ("2026-06-30T07:02:00Z", 70.0, "bpm"),
]

# A night's SpO2, a reading a minute while asleep.
SPO2_POINTS = minutes("2026-06-30T03:12:00Z", [95.6, 94.2, 96.0])
SPO2_SAMPLES = [
    ("2026-06-30T03:12:00Z", 96.0, "%"),
    ("2026-06-30T03:13:00Z", 94.0, "%"),
    ("2026-06-30T03:14:00Z", 96.0, "%"),
]


def seed(google):
    google.hr.update(HR)
    google.spo2.update(SPO2_POINTS)


# ---------- heart rate: a minute at a time, server-side ----------


def test_heart_rate_is_asked_for_as_one_minute_rollup_buckets(google):
    """The windowed rollUp, not the list: Google averages each minute, so
    the per-second readings never come over the wire."""
    google.sync(days=1)

    assert google.requests_for("heart-rate") == [{
        "range": {"startTime": "2026-06-30T00:00:00Z", "endTime": "2026-07-01T00:00:00Z"},
        "windowSize": "60s",
        "pageSize": 1440,
    }]
    assert [e for t, e, _ in google.requests if t == "heart-rate"] == ["rollUp"]


def test_heart_rate_files_a_whole_number_sample_per_minute(conn, google):
    seed(google)

    google.sync()

    assert samples(conn, HEART_RATE) == HR_SAMPLES


def test_a_full_day_is_one_sample_a_minute(conn, google):
    """Two days of buckets, a day to a page: every minute once, each on
    the minute."""
    google.hr.update(minutes("2026-06-29T00:00:00Z", [60 + i % 30 for i in range(2 * 1440)]))

    google.sync(days=2)

    filed = samples(conn, HEART_RATE)
    assert len(filed) == 2 * 1440
    assert len({ts for ts, *_ in filed}) == 2 * 1440
    assert all(ts.endswith(":00Z") for ts, *_ in filed)
    assert (filed[0][0], filed[-1][0]) == ("2026-06-29T00:00:00Z", "2026-06-30T23:59:00Z")
    assert len(google.requests_for("heart-rate")) == 2


def test_heart_rate_is_asked_for_14_days_at_a_time(conn, google):
    """rollUp takes at most 14 days of heart-rate: a 30-day sync asks in
    back-to-back windows from the first day's midnight on."""
    google.hr.update({at("2026-06-01T12:00:00Z"): 64.0, at("2026-06-20T12:00:00Z"): 66.0})

    google.sync(days=30)

    assert google.rollup_ranges() == [
        ("2026-06-01T00:00:00Z", "2026-06-15T00:00:00Z"),
        ("2026-06-15T00:00:00Z", "2026-06-29T00:00:00Z"),
        ("2026-06-29T00:00:00Z", "2026-07-01T00:00:00Z"),
    ]
    assert [ts for ts, *_ in samples(conn, HEART_RATE)] == [
        "2026-06-01T12:00:00Z", "2026-06-20T12:00:00Z",
    ]


def test_a_minute_with_no_reading_files_nothing(conn, google, capsys):
    """Non-wear: a bucket with no heartRate is no sample, and no error."""
    google.hr.update(minutes("2026-06-30T07:00:00Z", [61.4, None, 70.2]))

    google.sync()

    assert [ts for ts, *_ in samples(conn, HEART_RATE)] == [
        "2026-06-30T07:00:00Z", "2026-06-30T07:02:00Z",
    ]
    assert "not filed" not in capsys.readouterr().out


@pytest.mark.parametrize("bad", [0, 0.3, -1, "NaN", "Infinity", True, [61], {"avg": 61}])
def test_a_heart_rate_that_is_not_a_positive_number_is_dropped(conn, google, capsys, bad):
    google.hr.update(minutes("2026-06-30T07:00:00Z", [61.4, bad, 70.2]))

    google.sync()

    assert samples(conn, HEART_RATE) == [HR_SAMPLES[0], HR_SAMPLES[2]]
    assert "heart-rate: 1 not filed as heart_rate" in capsys.readouterr().out


# ---------- SpO2: listed, as it comes ----------


def test_spo2_is_listed_by_its_sample_time_over_the_window(google):
    google.sync()

    field = "oxygen_saturation.sample_time.physical_time"
    assert google.requests_for("oxygen-saturation") == [{
        "filter": f'{field} >= "2026-06-24T00:00:00Z" AND {field} < "2026-07-01T00:00:00Z"',
        "pageSize": 1440,
    }]


def test_spo2_files_each_point_as_a_whole_number_at_its_own_time(conn, google):
    seed(google)
    # Not on the minute: stored at its own time (to the second), not
    # moved into a bucket.
    google.spo2[at("2026-06-30T03:20:30.250000+00:00")] = 93.4

    google.sync()

    assert samples(conn, SPO2) == SPO2_SAMPLES + [("2026-06-30T03:20:30Z", 93.0, "%")]


@pytest.mark.parametrize("bad", [0, -1, -2, 0.2, "NaN", True, "high"])
def test_a_non_positive_or_unreadable_spo2_minute_is_dropped(conn, google, capsys, bad):
    seed(google)
    google.spo2[at("2026-06-30T03:15:00Z")] = bad

    google.sync()

    assert samples(conn, SPO2) == SPO2_SAMPLES
    assert "oxygen-saturation: 1 not filed as spo2" in capsys.readouterr().out


def test_a_null_spo2_is_no_reading(conn, google, capsys):
    seed(google)
    google.spo2[at("2026-06-30T03:15:00Z")] = None

    google.sync()

    assert samples(conn, SPO2) == SPO2_SAMPLES
    assert "not filed" not in capsys.readouterr().out


def test_a_spo2_point_with_no_usable_time_is_dropped(conn, google, capsys):
    google.raw["oxygen-saturation"] = [
        spo2_point(at("2026-06-30T03:12:00Z"), 95.6),
        {"oxygenSaturation": {"percentage": 94.0}},
        {"oxygenSaturation": {"sampleTime": "03:13", "percentage": 94.0}},
        {"oxygenSaturation": {"sampleTime": {"physicalTime": "soon"}, "percentage": 94.0}},
    ]

    google.sync()

    assert samples(conn, SPO2) == [SPO2_SAMPLES[0]]
    assert "oxygen-saturation: 3 not filed as spo2" in capsys.readouterr().out


# ---------- the window ----------

# US Eastern as a POSIX rule, so no tz database is needed: UTC-5, UTC-4
# from the second Sunday in March to the first in November.
EASTERN = "EST+5EDT,M3.2.0,M11.1.0"


def test_series_days_run_local_midnight_to_local_midnight(google, local_tz):
    """The sync's days in the server's zone - the one date.today() reads -
    as UTC bounds: New York's 30 June is 04:00Z to 04:00Z in summer."""
    local_tz(EASTERN)

    google.sync(days=1)

    field = "oxygen_saturation.sample_time.physical_time"
    assert google.rollup_ranges() == [("2026-06-30T04:00:00Z", "2026-07-01T04:00:00Z")]
    assert google.requests_for("oxygen-saturation")[0]["filter"] == (
        f'{field} >= "2026-06-30T04:00:00Z" AND {field} < "2026-07-01T04:00:00Z"'
    )


def test_the_window_stops_at_the_current_minute(google, monkeypatch):
    """Not the rest of today, which holds nothing yet; the minute under
    way is the next sync's."""
    monkeypatch.setattr("plugins.google_health.plugin._now",
                        lambda: at("2026-06-30T18:30:45.5+00:00"))

    google.sync(days=1)

    field = "oxygen_saturation.sample_time.physical_time"
    assert google.rollup_ranges() == [("2026-06-30T00:00:00Z", "2026-06-30T18:30:00Z")]
    assert google.requests_for("oxygen-saturation")[0]["filter"] == (
        f'{field} >= "2026-06-30T00:00:00Z" AND {field} < "2026-06-30T18:30:00Z"'
    )


def test_no_window_is_longer_than_14_days_across_a_clock_change(google, local_tz):
    """14 days of local midnights over the March change is 14 days less
    an hour; over November's, an hour more - which Google would refuse.
    Windows are measured in elapsed time, so none is."""
    local_tz(EASTERN)

    google.sync(days=120)

    ranges = [(at(start), at(end)) for start, end in google.rollup_ranges()]
    assert ranges[0][0] == at("2026-03-03T05:00:00Z")
    assert ranges[-1][1] == at("2026-07-01T04:00:00Z")
    assert all(end == nxt for (_, end), (nxt, _) in zip(ranges, ranges[1:]))
    assert all(end - start <= timedelta(days=14) for start, end in ranges)


def test_a_point_timed_outside_the_window_is_dropped_and_logged(conn, google, capsys):
    """A Google that ignored the filter: what lies outside the sync's days
    isn't filed."""
    google.raw["oxygen-saturation"] = [
        spo2_point(at("2026-06-30T03:12:00Z"), 95.6),
        spo2_point(at("2026-06-23T23:59:00Z"), 94.0),
    ]

    google.sync()

    assert samples(conn, SPO2) == [SPO2_SAMPLES[0]]
    assert ("oxygen-saturation: dropped 1 points timed outside 2026-06-24T00:00:00Z to "
            "2026-07-01T00:00:00Z") in capsys.readouterr().out


def test_a_token_that_never_runs_out_stops_at_the_page_cap(conn, google, capsys):
    google.endless = {"heart-rate"}

    google.sync(days=1)

    assert len(google.requests_for("heart-rate")) == MAX_PAGES
    # Each page's bucket was kept on the way.
    assert len(samples(conn, HEART_RATE)) == MAX_PAGES
    assert f"heart-rate: stopped after {MAX_PAGES} pages" in capsys.readouterr().out


# ---------- re-syncing ----------


def test_a_resync_of_the_same_window_upserts(conn, google):
    seed(google)
    google.sync()

    google.hr[at("2026-06-30T07:00:00Z")] = 65.2
    google.spo2[at("2026-06-30T03:12:00Z")] = 97.1
    google.sync()

    assert samples(conn, HEART_RATE) == [("2026-06-30T07:00:00Z", 65.0, "bpm")] + HR_SAMPLES[1:]
    assert samples(conn, SPO2) == [("2026-06-30T03:12:00Z", 97.0, "%")] + SPO2_SAMPLES[1:]


# ---------- one series' trouble costs only that series ----------


SERIES = [("heart-rate", HEART_RATE, HR_SAMPLES), ("oxygen-saturation", SPO2, SPO2_SAMPLES)]


@pytest.mark.parametrize("data_type, metric, _filed", SERIES, ids=[s[0] for s in SERIES])
def test_a_failed_series_fetch_costs_only_that_series(conn, google, capsys,
                                                      data_type, metric, _filed):
    seed(google)
    google.failing = {data_type}

    assert google.sync() == 1

    assert samples(conn, metric) == []
    for _type, other, filed in SERIES:
        if other != metric:
            assert samples(conn, other) == filed
    assert daily(conn) == {STEPS: 8123, RESTING_HR_BPM: 58}
    assert f"[google_health] {data_type} fetch failed for 2026-06-24T00:00:00Z" in (
        capsys.readouterr().out
    )


@pytest.mark.parametrize("data_type, metric, _filed", SERIES, ids=[s[0] for s in SERIES])
def test_an_empty_series_writes_nothing_and_says_so(conn, google, capsys,
                                                    data_type, metric, _filed):
    seed(google)
    if metric == HEART_RATE:
        google.hr.clear()
    else:
        google.spo2.clear()

    assert google.sync() == 1

    assert samples(conn, metric) == []
    for _type, other, filed in SERIES:
        if other != metric:
            assert samples(conn, other) == filed
    assert daily(conn) == {STEPS: 8123, RESTING_HR_BPM: 58}
    assert (f"[google_health] {data_type}: no data from 2026-06-24T00:00:00Z to "
            "2026-07-01T00:00:00Z") in capsys.readouterr().out


def test_failed_daily_fetches_leave_the_series_alone(conn, google):
    """And, as with Garmin, a day holding only samples keeps them though
    it counts as no day written - those are days of daily figures or
    sleep."""
    seed(google)
    google.failing = {"steps", "daily-resting-heart-rate"}

    assert google.sync() == 0

    assert daily(conn) == {}
    assert samples(conn, HEART_RATE) == HR_SAMPLES
    assert samples(conn, SPO2) == SPO2_SAMPLES


# ---------- the logs ----------


def test_a_value_under_an_unexpected_field_logs_the_point_and_its_keys(conn, google, capsys):
    google.raw["heart-rate"] = [{
        "startTime": "2026-06-30T07:00:00Z",
        "endTime": "2026-06-30T07:01:00Z",
        "heartRate": {"bpmAverage": 61.4},
    }]

    google.sync()

    assert samples(conn, HEART_RATE) == []
    out = capsys.readouterr().out
    assert "[google_health] heart-rate: got data but no beatsPerMinuteAvg in heartRate" in out
    assert "'bpmAverage': 61.4" in out
    assert "keys: ['endTime', 'heartRate', 'startTime']" in out


def test_each_series_logs_what_it_filed(google, capsys):
    """One line a series: how many samples over what span, and the latest
    raw value beside the one filed - so the unit can be checked against
    a real sync."""
    seed(google)

    google.sync()

    out = capsys.readouterr().out
    assert ("[google_health] heart-rate -> heart_rate: 3 samples from 2026-06-30T07:00:00Z "
            "to 2026-06-30T07:02:00Z, latest: {'beatsPerMinuteAvg': 70.2} -> 70") in out
    assert ("[google_health] oxygen-saturation -> spo2: 3 samples from 2026-06-30T03:12:00Z "
            "to 2026-06-30T03:14:00Z, latest: {'percentage': 96.0} -> 96") in out


# ---------- through the access contract: a passthrough, both sources ----------


def test_api_series_lists_garmin_and_google_heart_rate_each_tagged(client, conn, google):
    """Google is the second heart_rate source. No stitch yet: every
    source's samples, by time then source, each tagged - an overlapping
    minute listed once per source."""
    for ts, bpm in [("2026-06-30T07:00:00Z", 60), ("2026-06-30T07:02:00Z", 71)]:
        write_sample(conn, "garmin", HEART_RATE, ts, bpm)
    conn.commit()
    seed(google)

    google.sync()

    assert client.get("/api/series/heart_rate").get_json() == {
        "metric": "heart_rate",
        "unit": "bpm",
        "points": [
            {"ts": "2026-06-30T07:00:00Z", "value": 60, "source": "garmin"},
            {"ts": "2026-06-30T07:00:00Z", "value": 61, "source": "google_health"},
            {"ts": "2026-06-30T07:01:00Z", "value": 63, "source": "google_health"},
            {"ts": "2026-06-30T07:02:00Z", "value": 71, "source": "garmin"},
            {"ts": "2026-06-30T07:02:00Z", "value": 70, "source": "google_health"},
        ],
    }
    hr, spo2 = client.get("/api/catalog").get_json()["series"]
    assert (hr["metric"], hr["sources"]) == (HEART_RATE, ["garmin", "google_health"])
    assert (spo2["metric"], spo2["sources"]) == (SPO2, ["google_health"])


def test_api_series_spo2_lists_googles_samples(client, google):
    seed(google)

    google.sync()

    assert client.get("/api/series/spo2").get_json()["points"] == [
        {"ts": ts, "value": int(value), "source": "google_health"}
        for ts, value, _unit in SPO2_SAMPLES
    ]
