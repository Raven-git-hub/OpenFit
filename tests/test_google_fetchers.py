"""
What the Google Health fetchers ask for and read back, against the v4
discovery document (https://health.googleapis.com/$discovery/rest?version=v4),
with the network stubbed.

On a real account steps came through but resting HR never did: its list
request filtered on a field the endpoint doesn't take, and the parser
looked for the value under names Google doesn't use. These pin the
documented ones - a `daily_resting_heart_rate.date` filter, and the
reading in dailyRestingHeartRate.beatsPerMinute, an int64 that arrives as
a JSON string. (Sleep had the same filter problem; its test sits beside
the realistic sleep fixtures in test_sleep_sessions.py.)

Steps have a limit of their own: dailyRollUp takes a range of at most 90
days and refuses a longer one outright, so a longer backfill lost every
step. These pin that such a backfill is asked for in back-to-back
windows of at most 90 days covering the whole range, each read to its
last page, and merged - and that a sync of 90 days or fewer is still a
single request.
"""

import json
from datetime import date, timedelta

import pytest

from crypto import encrypt
from metrics import RESTING_HR_BPM, STEPS
from plugins.google_health.plugin import MAX_ROLLUP_DAYS

TODAY = date(2026, 6, 30)


class FrozenDate(date):
    """date, with today pinned to TODAY."""

    @classmethod
    def today(cls):
        return cls(TODAY.year, TODAY.month, TODAY.day)


def days_back(n):
    """The n days up to and including TODAY, newest first."""
    return [TODAY - timedelta(days=i) for i in range(n)]


def civil(d):
    return {"year": d.year, "month": d.month, "day": d.day}


def from_civil(c):
    return date(c["year"], c["month"], c["day"])


def steps_on(d):
    return 5000 + d.toordinal() % 1000


# ---------- a stand-in for Google ----------


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self.payload = payload
        self.status = status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(f"{self.status} Client Error")


class FakeGoogle:
    """Google Health's data endpoints, recording every data request as
    (data type, query params or body).

    dailyRollUp answers from `steps` (date -> count) with only the days
    inside the request's range, newest first, `page_size` to a page - and,
    like Google, refuses a range over 90 days. A range whose first day is
    in `failing` gets a 500. The resting-HR list answers with
    `hr_points` as given; every other list is empty.
    """

    def __init__(self):
        self.steps = {}
        self.page_size = 1440
        self.failing = set()
        self.hr_points = []
        self.requests = []

    def requests_for(self, data_type):
        return [fields for t, fields in self.requests if t == data_type]

    def rollup_ranges(self):
        """Each steps window asked for, as (first day, exclusive end), in
        the order asked - counting a window once however many pages it
        took."""
        return [
            (from_civil(body["range"]["start"]["date"]), from_civil(body["range"]["end"]["date"]))
            for body in self.requests_for("steps") if "pageToken" not in body
        ]

    def post(self, url, data=None, json=None, headers=None, timeout=None):
        if "oauth2" in url:
            return FakeResponse({"access_token": "access-xyz"})
        self.requests.append((data_type_of(url), json))
        first = from_civil(json["range"]["start"]["date"])
        end = from_civil(json["range"]["end"]["date"])
        if (end - first).days > 90:
            return FakeResponse(status=400)
        if first in self.failing:
            return FakeResponse(status=500)
        points = [
            {"civilStartTime": {"date": civil(d)}, "steps": {"countSum": str(count)}}
            for d, count in sorted(self.steps.items(), reverse=True) if first <= d < end
        ]
        offset = int(json.get("pageToken") or 0)
        page = {"rollupDataPoints": points[offset:offset + self.page_size]}
        if offset + self.page_size < len(points):
            page["nextPageToken"] = str(offset + self.page_size)
        return FakeResponse(page)

    def get(self, url, headers=None, params=None, timeout=None):
        data_type = data_type_of(url)
        self.requests.append((data_type, params))
        if data_type == "daily-resting-heart-rate":
            return FakeResponse({"dataPoints": self.hr_points})
        return FakeResponse({"dataPoints": []})


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


def expected_steps(days):
    return sorted((d.isoformat(), steps_on(d)) for d in days)


# ---------- resting heart rate ----------


def hr_point(d, bpm):
    """A daily-resting-heart-rate list point, in the documented shape."""
    return {"dailyRestingHeartRate": {
        "date": civil(d),
        "beatsPerMinute": bpm,
        "dailyRestingHeartRateMetadata": {"calculationMethod": "WITH_SLEEP"},
    }}


def test_resting_hr_filters_on_the_documented_date_field(google):
    google.sync(days=7)

    # A 7-day sync on 2026-06-30 starts on 2026-06-24.
    assert google.requests_for("daily-resting-heart-rate") == [
        {"filter": 'daily_resting_heart_rate.date >= "2026-06-24"'}
    ]


def test_resting_hr_reads_beats_per_minute(conn, google):
    google.hr_points = [hr_point(TODAY, "58"), hr_point(TODAY - timedelta(days=1), "61")]

    assert google.sync() == 2

    assert readings(conn, RESTING_HR_BPM) == [("2026-06-29", 61), ("2026-06-30", 58)]


def test_a_resting_hr_point_without_a_reading_is_skipped(conn, google):
    no_reading = hr_point(TODAY - timedelta(days=1), "61")
    del no_reading["dailyRestingHeartRate"]["beatsPerMinute"]
    google.hr_points = [hr_point(TODAY, "58"), no_reading]

    google.sync()

    assert readings(conn, RESTING_HR_BPM) == [("2026-06-30", 58)]


def test_resting_hr_in_an_unexpected_shape_says_what_it_got(conn, google, capsys):
    """Points that came back but held no reading where the doc puts it:
    nothing filed, and one sample logged to show what Google sent."""
    google.hr_points = [{"dailyRestingHeartRate": {"date": civil(TODAY), "bpm": 58}}]

    assert google.sync() == 0

    assert readings(conn, RESTING_HR_BPM) == []
    out = capsys.readouterr().out
    assert "no date and beatsPerMinute" in out
    assert "'bpm': 58" in out


# ---------- steps, a window of at most 90 days at a time ----------


@pytest.mark.parametrize("days", [1, 7, 90])
def test_a_sync_of_up_to_90_days_is_one_rollup_request(conn, google, days):
    span = days_back(days)
    google.steps = {d: steps_on(d) for d in span}

    google.sync(days=days)

    assert google.rollup_ranges() == [(span[-1], TODAY + timedelta(days=1))]
    assert readings(conn, STEPS) == expected_steps(span)


@pytest.mark.parametrize("days, windows", [(91, 2), (180, 2), (181, 3), (365, 5)])
def test_a_longer_backfill_is_asked_for_in_90_day_windows(conn, google, days, windows):
    span = days_back(days)
    google.steps = {d: steps_on(d) for d in span}

    google.sync(days=days)

    ranges = google.rollup_ranges()
    assert len(ranges) == windows
    # Back to back from the first day of the backfill through today...
    assert ranges[0][0] == span[-1]
    assert ranges[-1][1] == TODAY + timedelta(days=1)
    assert all(end == next_first for (_, end), (next_first, _) in zip(ranges, ranges[1:]))
    # ...none over the limit...
    assert all((end - first).days <= MAX_ROLLUP_DAYS for first, end in ranges)
    # ...and every day's steps merged into the one sync.
    assert readings(conn, STEPS) == expected_steps(span)


def test_the_windows_start_from_the_oldest_day(google):
    google.sync(days=200)

    assert google.rollup_ranges() == [
        (TODAY - timedelta(days=199), TODAY - timedelta(days=109)),
        (TODAY - timedelta(days=109), TODAY - timedelta(days=19)),
        (TODAY - timedelta(days=19), TODAY + timedelta(days=1)),
    ]


def test_each_window_is_read_to_its_last_page(conn, google):
    span = days_back(200)
    google.steps = {d: steps_on(d) for d in span}
    google.page_size = 40

    google.sync(days=200)

    # 90 + 90 + 20 days at 40 a page: 3 + 3 + 1 requests.
    assert len(google.requests_for("steps")) == 7
    assert readings(conn, STEPS) == expected_steps(span)


def test_a_failed_window_costs_only_its_own_days(conn, google, capsys):
    span = days_back(200)
    google.steps = {d: steps_on(d) for d in span}
    oldest_first = TODAY - timedelta(days=199)
    google.failing = {oldest_first}

    google.sync(days=200)

    # The oldest 90 days are lost; the 110 after them are filed.
    assert readings(conn, STEPS) == expected_steps(span[:110])
    assert f"steps fetch failed for {oldest_first}" in capsys.readouterr().out
