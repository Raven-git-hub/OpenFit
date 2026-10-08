"""
The metrics reshape must stay invisible to the frontend: GET /api/activity
still returns one flat row per date in the old field names and units,
with garmin winning over google_health per metric.
"""

import sqlite3

import pytest

from metrics import METRICS
from migrations import run_migrations


def insert(conn, date, source, synced_at=None, **readings):
    """Seed one source's readings for a day, keyed by canonical metric."""
    for metric, value in readings.items():
        conn.execute(
            "INSERT INTO metrics (date, source, metric, value, unit, synced_at) "
            "VALUES (?, ?, ?, ?, ?, COALESCE(?, datetime('now')))",
            (date, source, metric, value, METRICS[metric], synced_at),
        )
    conn.commit()


def test_garmin_wins_over_google_health(client, conn):
    insert(conn, "2026-01-01", "google_health", steps=8000, resting_hr_bpm=60, sleep_minutes=360)
    insert(conn, "2026-01-01", "garmin", steps=9000, resting_hr_bpm=52, sleep_minutes=450)

    row = client.get("/api/activity").get_json()[0]
    assert row == {
        "date": "2026-01-01",
        "steps": 9000,
        "resting_hr": 52,
        "sleep_hours": 7.5,
        "source": "garmin",
    }


def test_merge_is_per_metric_not_per_row(client, conn):
    # Garmin has steps but no sleep; Google Health has the sleep. The
    # merged day should carry both - this is the whole point of the
    # reshape.
    insert(conn, "2026-01-01", "garmin", steps=9000)
    insert(conn, "2026-01-01", "google_health", steps=8000, sleep_minutes=390, resting_hr_bpm=61)

    row = client.get("/api/activity").get_json()[0]
    assert row["steps"] == 9000
    assert row["sleep_hours"] == 6.5
    assert row["resting_hr"] == 61


def test_unknown_source_ranks_below_known_sources(client, conn):
    insert(conn, "2026-01-01", "unknown", steps=1, resting_hr_bpm=99)
    insert(conn, "2026-01-01", "google_health", steps=8000)

    row = client.get("/api/activity").get_json()[0]
    assert row["steps"] == 8000
    assert row["resting_hr"] == 99  # only 'unknown' had it
    assert row["source"] == "google_health"


def test_a_source_with_one_metric_leaves_the_rest_empty(client, conn):
    # A day is only as full as its readings: there is no row for a
    # metric nobody reported, and the field comes back null as before.
    insert(conn, "2026-01-01", "garmin", resting_hr_bpm=52)

    assert client.get("/api/activity").get_json() == [
        {
            "date": "2026-01-01",
            "steps": None,
            "resting_hr": 52,
            "sleep_hours": None,
            "source": "garmin",
        }
    ]


def test_values_come_back_in_the_old_types_and_units(client, conn):
    # Stored as REAL minutes; read back as the old INTEGER columns and
    # one-decimal hours. 9000 == 9000.0 in Python, so check the types:
    # the JSON must say 9000, not 9000.0.
    insert(conn, "2026-01-01", "garmin", steps=9000, resting_hr_bpm=52, sleep_minutes=437)

    for row in (
        client.get("/api/activity").get_json()[0],
        client.get("/api/activity?by_source=1").get_json()[0],
    ):
        assert (row["steps"], row["resting_hr"], row["sleep_hours"]) == (9000, 52, 7.3)
        assert type(row["steps"]) is int
        assert type(row["resting_hr"]) is int


def test_dates_descending(client, conn):
    for d in ("2026-01-01", "2026-01-03", "2026-01-02"):
        insert(conn, d, "garmin", steps=1)

    rows = client.get("/api/activity").get_json()
    assert [r["date"] for r in rows] == ["2026-01-03", "2026-01-02", "2026-01-01"]


def test_metrics_outside_the_activity_fields_are_ignored(client, conn):
    # A future metric (weight, say) lives in the same table. It has no
    # field here, so it must neither show up nor use up a day.
    insert(conn, "2026-01-01", "garmin", steps=9000)
    conn.execute(
        "INSERT INTO metrics (date, source, metric, value, unit, synced_at) "
        "VALUES ('2026-01-02', 'garmin', 'weight_kg', 82.0, 'kg', datetime('now'))"
    )
    conn.commit()

    assert [r["date"] for r in client.get("/api/activity?days=1").get_json()] == ["2026-01-01"]
    by_source = client.get("/api/activity?by_source=1").get_json()
    assert [(r["date"], r["source"]) for r in by_source] == [("2026-01-01", "garmin")]


@pytest.mark.parametrize("flag", ["1", "true", "yes", "TRUE"])
def test_by_source_returns_raw_rows(client, conn, flag):
    insert(conn, "2026-01-01", "garmin", steps=9000)
    insert(conn, "2026-01-01", "google_health", steps=8000)

    rows = client.get(f"/api/activity?by_source={flag}").get_json()
    assert len(rows) == 2
    assert {r["source"] for r in rows} == {"garmin", "google_health"}
    assert {r["steps"] for r in rows} == {9000, 8000}
    assert "synced_at" in rows[0]


def test_by_source_rows_keep_the_wide_shape(client, conn):
    # One row per (date, source) with every field; synced_at is the
    # latest write to any of its metrics, as the old row's would be.
    insert(conn, "2026-01-01", "garmin", synced_at="2026-01-01 06:00:00", steps=9000)
    insert(conn, "2026-01-01", "garmin", synced_at="2026-01-01 07:00:00", sleep_minutes=450)
    insert(conn, "2026-01-01", "google_health", synced_at="2026-01-01 05:00:00", resting_hr_bpm=61)
    insert(conn, "2026-01-02", "garmin", synced_at="2026-01-02 06:00:00", steps=8000)

    assert client.get("/api/activity?by_source=1").get_json() == [
        {
            "date": "2026-01-02",
            "steps": 8000,
            "resting_hr": None,
            "sleep_hours": None,
            "source": "garmin",
            "synced_at": "2026-01-02 06:00:00",
        },
        {
            "date": "2026-01-01",
            "steps": 9000,
            "resting_hr": None,
            "sleep_hours": 7.5,
            "source": "garmin",
            "synced_at": "2026-01-01 07:00:00",
        },
        {
            "date": "2026-01-01",
            "steps": None,
            "resting_hr": 61,
            "sleep_hours": None,
            "source": "google_health",
            "synced_at": "2026-01-01 05:00:00",
        },
    ]


def test_by_source_off_by_default(client, conn):
    insert(conn, "2026-01-01", "garmin", steps=9000)
    insert(conn, "2026-01-01", "google_health", steps=8000)

    assert len(client.get("/api/activity").get_json()) == 1
    assert len(client.get("/api/activity?by_source=0").get_json()) == 1


def test_activity_migrated_by_005_reads_back_unchanged(db_at_version, monkeypatch):
    """Data written before 005 comes out of the API as it went in.

    Seeded at version 4 - the shape a production database is in - with
    the values the old plugins wrote, then migrated and read back.
    """
    import main

    path = db_at_version(4)
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO activity (date, steps, resting_hr, sleep_hours, source, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            ("2026-01-01", 9000, 52, 7.5, "garmin", "2026-01-01 06:00:00"),
            ("2026-01-01", 8000, 60, 6.0, "google_health", "2026-01-01 07:00:00"),
            ("2026-01-02", 7000, None, None, "garmin", "2026-01-02 06:00:00"),
            # Google summed per-session hours, so float noise like this
            # is real. It now reads back as the one-decimal value it
            # meant - the one place the output differs, for the better.
            ("2026-01-02", None, 61, 6.6000000000000005, "google_health", "2026-01-02 07:00:00"),
        ],
    )
    conn.commit()
    assert 5 in run_migrations(conn)
    conn.close()

    monkeypatch.setitem(main.app.config, "DB_PATH", path)
    client = main.app.test_client()

    assert client.get("/api/activity").get_json() == [
        {"date": "2026-01-02", "steps": 7000, "resting_hr": 61, "sleep_hours": 6.6,
         "source": "garmin"},
        {"date": "2026-01-01", "steps": 9000, "resting_hr": 52, "sleep_hours": 7.5,
         "source": "garmin"},
    ]
    assert client.get("/api/activity?by_source=1").get_json() == [
        {"date": "2026-01-02", "steps": 7000, "resting_hr": None, "sleep_hours": None,
         "source": "garmin", "synced_at": "2026-01-02 06:00:00"},
        {"date": "2026-01-02", "steps": None, "resting_hr": 61, "sleep_hours": 6.6,
         "source": "google_health", "synced_at": "2026-01-02 07:00:00"},
        {"date": "2026-01-01", "steps": 9000, "resting_hr": 52, "sleep_hours": 7.5,
         "source": "garmin", "synced_at": "2026-01-01 06:00:00"},
        {"date": "2026-01-01", "steps": 8000, "resting_hr": 60, "sleep_hours": 6.0,
         "source": "google_health", "synced_at": "2026-01-01 07:00:00"},
    ]
