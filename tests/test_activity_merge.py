"""
The per-source reshape must stay invisible to the frontend: GET
/api/activity still returns one flat row per date, with garmin winning
over google_health per metric.
"""

import pytest


def insert(conn, date, source, steps=None, resting_hr=None, sleep_hours=None):
    conn.execute(
        "INSERT INTO activity (date, steps, resting_hr, sleep_hours, source, synced_at) "
        "VALUES (?, ?, ?, ?, ?, datetime('now'))",
        (date, steps, resting_hr, sleep_hours, source),
    )
    conn.commit()


def test_garmin_wins_over_google_health(client, conn):
    insert(conn, "2026-01-01", "google_health", steps=8000, resting_hr=60, sleep_hours=6.0)
    insert(conn, "2026-01-01", "garmin", steps=9000, resting_hr=52, sleep_hours=7.5)

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
    insert(conn, "2026-01-01", "google_health", steps=8000, sleep_hours=6.5, resting_hr=61)

    row = client.get("/api/activity").get_json()[0]
    assert row["steps"] == 9000
    assert row["sleep_hours"] == 6.5
    assert row["resting_hr"] == 61


def test_unknown_source_ranks_below_known_sources(client, conn):
    insert(conn, "2026-01-01", "unknown", steps=1, resting_hr=99)
    insert(conn, "2026-01-01", "google_health", steps=8000)

    row = client.get("/api/activity").get_json()[0]
    assert row["steps"] == 8000
    assert row["resting_hr"] == 99  # only 'unknown' had it
    assert row["source"] == "google_health"


def test_all_null_day_still_returns_a_row(client, conn):
    insert(conn, "2026-01-01", "garmin")

    assert client.get("/api/activity").get_json() == [
        {
            "date": "2026-01-01",
            "steps": None,
            "resting_hr": None,
            "sleep_hours": None,
            "source": "garmin",
        }
    ]


def test_dates_descending(client, conn):
    for d in ("2026-01-01", "2026-01-03", "2026-01-02"):
        insert(conn, d, "garmin", steps=1)

    rows = client.get("/api/activity").get_json()
    assert [r["date"] for r in rows] == ["2026-01-03", "2026-01-02", "2026-01-01"]


@pytest.mark.parametrize("flag", ["1", "true", "yes", "TRUE"])
def test_by_source_returns_raw_rows(client, conn, flag):
    insert(conn, "2026-01-01", "garmin", steps=9000)
    insert(conn, "2026-01-01", "google_health", steps=8000)

    rows = client.get(f"/api/activity?by_source={flag}").get_json()
    assert len(rows) == 2
    assert {r["source"] for r in rows} == {"garmin", "google_health"}
    assert {r["steps"] for r in rows} == {9000, 8000}
    assert "synced_at" in rows[0]


def test_by_source_off_by_default(client, conn):
    insert(conn, "2026-01-01", "garmin", steps=9000)
    insert(conn, "2026-01-01", "google_health", steps=8000)

    assert len(client.get("/api/activity").get_json()) == 1
    assert len(client.get("/api/activity?by_source=0").get_json()) == 1
