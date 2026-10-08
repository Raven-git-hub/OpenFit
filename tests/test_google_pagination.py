"""
Google Health pagination, with the network stubbed.

Google answers its list and rollup calls a page at a time, newest first,
with a nextPageToken while more remain - and a sleep page holds at most
25 points. Reading only the first page kept the newest month and silently
dropped the rest of a backfill, from the daily metrics and the sleep
sessions alike. These tests serve each fetcher its data across pages and
pin that sync() reads them all, keeps what it has when a later page
fails, and stops when a provider never runs out of tokens.

Per the v4 discovery document the list GETs take the token as a
`pageToken` query param, and dailyRollUp takes it as `pageToken` in the
request body with every other field repeated unchanged; the tests check
each token goes where its endpoint wants it.
"""

import json
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Callable

import pytest
import requests

from crypto import encrypt
from metrics import RESTING_HR_BPM, SLEEP_MINUTES, STEPS
from plugins.google_health.plugin import MAX_PAGES

# The newest day in every fixture; the points run back from it, newest
# first, as Google sends them. Syncs run on this day too.
NEWEST = date(2026, 3, 1)

# How many days a sync covers unless a test says otherwise.
SYNC_DAYS = 60


class FrozenDate(date):
    """date, with today pinned to NEWEST - sleep is only kept from the
    sync window's first day on, so the window has to be where the points
    are."""

    @classmethod
    def today(cls):
        return cls(NEWEST.year, NEWEST.month, NEWEST.day)


def days_back(n, skip=0):
    """n consecutive days, newest first, starting `skip` days before NEWEST."""
    return [NEWEST - timedelta(days=skip + i) for i in range(n)]


def civil(d):
    return {"year": d.year, "month": d.month, "day": d.day}


# ---------- one data point per day, for each endpoint ----------


def steps_point(d):
    return {"civilStartTime": {"date": civil(d)}, "steps": {"countSum": str(steps_on(d))}}


def steps_on(d):
    return 5000 + d.toordinal() % 1000


def sleep_point(d):
    """A night starting on d, as the sleep list sends it."""
    nxt = d + timedelta(days=1)
    return {"sleep": {
        "interval": {
            "startTime": f"{d.isoformat()}T22:30:00Z",
            "endTime": f"{nxt.isoformat()}T06:30:00Z",
            "civilStartTime": {"date": civil(d)},
        },
        "type": "STAGES",
        "summary": {
            "minutesAsleep": str(sleep_on(d)),
            "stagesSummary": [{"type": "DEEP", "minutes": "80"}],
        },
    }}


def sleep_on(d):
    return 400 + d.day


def hr_point(d):
    # beatsPerMinute is an int64, which Google sends as a JSON string.
    return {"dailyRestingHeartRate": {"date": civil(d), "beatsPerMinute": str(hr_on(d))}}


def hr_on(d):
    return 50 + d.day % 10


@dataclass
class Fetcher:
    data_type: str      # the path segment under dataTypes/
    items_key: str      # where each page lists its points
    token_in: str       # where the next page's token goes: "query" or "body"
    metric: str         # what sync() files the points as
    point: Callable     # date -> one data point for that day
    value: Callable     # date -> the reading that point should file


FETCHERS = {
    "steps": Fetcher("steps", "rollupDataPoints", "body", STEPS, steps_point, steps_on),
    "sleep": Fetcher("sleep", "dataPoints", "query", SLEEP_MINUTES, sleep_point, sleep_on),
    "resting_hr": Fetcher(
        "daily-resting-heart-rate", "dataPoints", "query", RESTING_HR_BPM, hr_point, hr_on,
    ),
}


@pytest.fixture(params=list(FETCHERS))
def fetcher(request):
    return FETCHERS[request.param]


# ---------- a paging stand-in for Google ----------


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self.payload = payload
        self.status = status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status >= 400:
            raise requests.HTTPError(f"{self.status} Server Error")


class EndlessPages:
    """Pages that never run out: every one carries a nextPageToken."""

    def __init__(self, fetcher):
        self.fetcher = fetcher

    def __getitem__(self, index):
        return {
            self.fetcher.items_key: [self.fetcher.point(NEWEST - timedelta(days=index))],
            "nextPageToken": f"page-{index + 1}",
        }


class FakeGoogle:
    """Google Health's endpoints, serving canned pages by data type.

    pages[data_type] is a sequence of page payloads; the request with no
    token gets the first and pageToken "page-N" gets page N. A status
    code in place of a payload fails that page, an exception is raised
    by it. A data type with no pages answers empty. Every data request is
    recorded as (data_type, where the fields went, the fields).
    """

    def __init__(self):
        self.pages = {}
        self.requests = []

    def serve(self, fetcher, *pages):
        """Chain pages of points for one endpoint: each page but the last
        carries the token of the one after. A status code or exception in
        place of a list of points fails that page."""
        chained = []
        for i, page in enumerate(pages):
            if isinstance(page, list):
                page = {fetcher.items_key: page}
                if i < len(pages) - 1:
                    page["nextPageToken"] = f"page-{i + 1}"
            chained.append(page)
        self.pages[fetcher.data_type] = chained

    def requests_for(self, fetcher):
        return [(where, fields) for t, where, fields in self.requests if t == fetcher.data_type]

    def _respond(self, url, where, fields):
        data_type = url.split("/dataTypes/")[1].split("/")[0]
        self.requests.append((data_type, where, dict(fields or {})))
        token = (fields or {}).get("pageToken")
        page = self.pages.get(data_type, [{}])[int(token.split("-")[1]) if token else 0]
        if isinstance(page, Exception):
            raise page
        if isinstance(page, int):
            return FakeResponse(status=page)
        return FakeResponse(page)

    def get(self, url, headers=None, params=None, timeout=None):
        return self._respond(url, "query", params)

    def post(self, url, data=None, json=None, headers=None, timeout=None):
        if "oauth2" in url:
            return FakeResponse({"access_token": "access-xyz"})
        return self._respond(url, "body", json)


@pytest.fixture
def google(conn, monkeypatch):
    """A FakeGoogle behind the real Google Health plugin, and its sync,
    run on NEWEST."""
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
    fake.sync = lambda days=SYNC_DAYS: PLUGINS["google_health"].sync(conn, days)
    return fake


def readings(conn, metric):
    return [
        tuple(r) for r in conn.execute(
            "SELECT date, value FROM metrics WHERE source = 'google_health' AND metric = ? "
            "ORDER BY date",
            (metric,),
        )
    ]


def expected(fetcher, days):
    return sorted((d.isoformat(), fetcher.value(d)) for d in days)


# ---------- every fetcher ----------


def test_every_page_is_read(conn, google, fetcher):
    page1, page2 = days_back(25), days_back(10, skip=25)
    google.serve(fetcher, [fetcher.point(d) for d in page1], [fetcher.point(d) for d in page2])

    google.sync()

    # The older days live on page 2 - exactly what a first-page-only
    # fetch dropped.
    assert readings(conn, fetcher.metric) == expected(fetcher, page1 + page2)


def test_the_page_token_goes_where_the_endpoint_wants_it(google, fetcher):
    google.serve(fetcher, [fetcher.point(d) for d in days_back(2)],
                 [fetcher.point(d) for d in days_back(2, skip=2)])

    google.sync()

    (where1, first), (where2, second) = google.requests_for(fetcher)
    assert where1 == where2 == fetcher.token_in
    assert "pageToken" not in first
    # The rest of the request is repeated unchanged, as both kinds of
    # endpoint require.
    assert second == {**first, "pageToken": "page-1"}


def test_a_single_page_still_works(conn, google, fetcher):
    days = days_back(7)
    google.serve(fetcher, [fetcher.point(d) for d in days])

    google.sync()

    assert readings(conn, fetcher.metric) == expected(fetcher, days)
    assert len(google.requests_for(fetcher)) == 1


@pytest.mark.parametrize("failure", [500, requests.ConnectionError("connection reset")])
def test_a_failed_later_page_keeps_the_earlier_ones(conn, google, fetcher, capsys, failure):
    """A partial backfill beats none - and never raises into sync()."""
    page1 = days_back(25)
    google.serve(fetcher, [fetcher.point(d) for d in page1], failure)

    assert google.sync() == 25

    assert readings(conn, fetcher.metric) == expected(fetcher, page1)
    assert "page 2 failed, keeping the 25 points" in capsys.readouterr().out


def test_a_failed_first_page_writes_nothing_for_that_fetcher(conn, google, fetcher, capsys):
    google.serve(fetcher, 503)

    assert google.sync() == 0

    assert readings(conn, fetcher.metric) == []
    assert "fetch failed" in capsys.readouterr().out


def test_a_token_that_never_runs_out_stops_at_the_page_cap(conn, google, fetcher, capsys):
    google.pages[fetcher.data_type] = EndlessPages(fetcher)

    google.sync()

    assert len(google.requests_for(fetcher)) == MAX_PAGES
    # Each page's day was kept on the way - for sleep, only those inside
    # the sync window. This fake pages out 100 days of nights whatever the
    # filter; a real account's would stop at the window, and the plugin
    # drops any night that began before it.
    kept = days_back(SYNC_DAYS) if fetcher is FETCHERS["sleep"] else days_back(MAX_PAGES)
    assert readings(conn, fetcher.metric) == expected(fetcher, kept)
    assert f"stopped after {MAX_PAGES} pages" in capsys.readouterr().out


# ---------- the backfill, end to end ----------


def sleep_sessions(conn):
    return [
        (r[0], json.loads(r[1])) for r in conn.execute(
            "SELECT \"start\", summary_json FROM sessions "
            "WHERE source = 'google_health' AND kind = 'sleep' ORDER BY \"start\""
        )
    ]


def test_a_backfill_longer_than_a_sleep_page_keeps_every_night(conn, google):
    """30 days: steps in one rollup page, sleep across a full page of 25
    and a second of 5, resting HR in one list page."""
    days = days_back(30)
    google.serve(FETCHERS["steps"], [steps_point(d) for d in days])
    google.serve(FETCHERS["sleep"], [sleep_point(d) for d in days[:25]],
                 [sleep_point(d) for d in days[25:]])
    google.serve(FETCHERS["resting_hr"], [hr_point(d) for d in days])

    assert google.sync(days=30) == 30

    for fetcher in FETCHERS.values():
        assert readings(conn, fetcher.metric) == expected(fetcher, days)
    # Sleep sessions are read from the same pages, so the oldest five
    # nights are back too.
    sessions = sleep_sessions(conn)
    assert len(sessions) == 30
    assert sessions[0] == (
        f"{days[-1].isoformat()}T22:30:00Z",
        {"asleep_minutes": sleep_on(days[-1]), "deep_minutes": 80},
    )
