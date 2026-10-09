"""
The derived value: recompute_derived() picks one reading per (date,
metric) into derived_metrics, a sync re-derives the window it wrote, the
first boot after 008 backfills once, and /api/activity reads what's
stored.
"""

import sqlite3
from datetime import date, timedelta

import main
from derived import DEFAULT_PRIORITY, backfill_derived, recompute_derived
from metrics import METRICS, SLEEP_MINUTES, STEPS
from plugins.base import SyncPlugin, write_metric


def insert(conn, date, source, synced_at="2026-01-01 06:00:00", **readings):
    """Seed one source's readings for a day, keyed by canonical metric."""
    for metric, value in readings.items():
        conn.execute(
            "INSERT INTO metrics (date, source, metric, value, unit, synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (date, source, metric, value, METRICS[metric], synced_at),
        )
    conn.commit()


def derived(conn):
    """Every derived row, as (date, metric, value, unit, source, synced_at)."""
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT date, metric, value, unit, source, synced_at "
            "FROM derived_metrics ORDER BY date, metric"
        )
    ]


def picked(conn, date, metric):
    """(value, source) derived for one (date, metric), or None."""
    row = conn.execute(
        "SELECT value, source FROM derived_metrics WHERE date = ? AND metric = ?",
        (date, metric),
    ).fetchone()
    return tuple(row) if row else None


def days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


# ---------- the pick ----------


def test_the_default_priority_is_garmin_then_google_health():
    assert DEFAULT_PRIORITY == ["garmin", "google_health"]


def test_garmin_wins_over_google_health(conn):
    insert(conn, "2026-01-01", "google_health", synced_at="g", steps=8000)
    insert(conn, "2026-01-01", "garmin", synced_at="x", steps=9000)

    recompute_derived(conn)

    # The winner's value, unit and synced_at, tagged with its source.
    assert derived(conn) == [("2026-01-01", "steps", 9000, "count", "garmin", "x")]


def test_the_pick_is_per_metric(conn):
    # Garmin has steps but no sleep; Google Health has both. Each metric
    # gets its own winner.
    insert(conn, "2026-01-01", "garmin", steps=9000)
    insert(conn, "2026-01-01", "google_health", steps=8000, sleep_minutes=390)

    recompute_derived(conn)

    assert picked(conn, "2026-01-01", STEPS) == (9000, "garmin")
    assert picked(conn, "2026-01-01", SLEEP_MINUTES) == (390, "google_health")


def test_falls_back_to_the_next_source_on_a_day_the_top_one_missed(conn):
    insert(conn, "2026-01-01", "garmin", steps=9000)
    insert(conn, "2026-01-01", "google_health", steps=8000)
    insert(conn, "2026-01-02", "google_health", steps=7000)

    recompute_derived(conn)

    assert picked(conn, "2026-01-01", STEPS) == (9000, "garmin")
    assert picked(conn, "2026-01-02", STEPS) == (7000, "google_health")


def test_unlisted_sources_rank_last_then_by_name(conn):
    insert(conn, "2026-01-01", "zeta", steps=1)
    insert(conn, "2026-01-01", "unknown", steps=2)
    insert(conn, "2026-01-01", "aardvark", steps=3)
    recompute_derived(conn)
    assert picked(conn, "2026-01-01", STEPS) == (3, "aardvark")

    insert(conn, "2026-01-01", "google_health", steps=4)
    recompute_derived(conn)
    assert picked(conn, "2026-01-01", STEPS) == (4, "google_health")


def test_a_single_source_yields_that_source_for_every_metric(conn):
    # Weight included: every metric is derived, not only the activity ones.
    insert(conn, "2026-01-01", "manual", synced_at=None, weight_kg=82.5)
    insert(conn, "2026-01-01", "google_health", synced_at="g",
           steps=8000, resting_hr_bpm=61, sleep_minutes=390)

    recompute_derived(conn)

    assert derived(conn) == [
        ("2026-01-01", "resting_hr_bpm", 61, "bpm", "google_health", "g"),
        ("2026-01-01", "sleep_minutes", 390, "min", "google_health", "g"),
        ("2026-01-01", "steps", 8000, "count", "google_health", "g"),
        ("2026-01-01", "weight_kg", 82.5, "kg", "manual", None),
    ]


def test_a_reading_that_goes_is_re_picked_and_the_last_one_drops_the_row(conn):
    insert(conn, "2026-01-01", "garmin", steps=9000)
    insert(conn, "2026-01-01", "google_health", steps=8000)
    insert(conn, "2026-01-02", "garmin", steps=7000)
    recompute_derived(conn)

    conn.execute("DELETE FROM metrics WHERE date = '2026-01-01' AND source = 'garmin'")
    conn.commit()
    recompute_derived(conn)
    assert picked(conn, "2026-01-01", STEPS) == (8000, "google_health")

    conn.execute("DELETE FROM metrics WHERE date = '2026-01-01'")
    conn.commit()
    recompute_derived(conn)
    assert picked(conn, "2026-01-01", STEPS) is None
    assert picked(conn, "2026-01-02", STEPS) == (7000, "garmin")


def test_a_newer_reading_replaces_the_derived_value(conn):
    insert(conn, "2026-01-01", "garmin", synced_at="x", steps=9000)
    recompute_derived(conn)

    conn.execute(
        "UPDATE metrics SET value = 9500, synced_at = 'later' WHERE date = '2026-01-01'"
    )
    conn.commit()
    recompute_derived(conn)

    assert derived(conn) == [("2026-01-01", "steps", 9500, "count", "garmin", "later")]


def test_since_leaves_earlier_dates_alone(conn):
    insert(conn, "2026-01-01", "garmin", steps=1)
    insert(conn, "2026-01-03", "garmin", steps=1)
    recompute_derived(conn)

    # Both days' readings change, and the earlier one's go entirely.
    conn.execute("UPDATE metrics SET value = 2 WHERE date = '2026-01-03'")
    conn.execute("DELETE FROM metrics WHERE date = '2026-01-01'")
    conn.commit()
    recompute_derived(conn, since="2026-01-02")

    assert picked(conn, "2026-01-03", STEPS) == (2, "garmin")
    # Before `since`: neither updated nor deleted.
    assert picked(conn, "2026-01-01", STEPS) == (1, "garmin")


def test_until_leaves_later_dates_alone(conn):
    for d in ("2026-01-01", "2026-01-02", "2026-01-03"):
        insert(conn, d, "garmin", steps=1)
    recompute_derived(conn)

    conn.execute("UPDATE metrics SET value = 2")
    conn.execute("DELETE FROM metrics WHERE date = '2026-01-03'")
    conn.commit()
    # One day, as a webhook push re-derives.
    recompute_derived(conn, since="2026-01-02", until="2026-01-02")

    assert picked(conn, "2026-01-02", STEPS) == (2, "garmin")
    # Outside the range on either side: neither updated nor deleted.
    assert picked(conn, "2026-01-01", STEPS) == (1, "garmin")
    assert picked(conn, "2026-01-03", STEPS) == (1, "garmin")


def test_recompute_is_stable_and_counts_what_it_wrote(conn):
    insert(conn, "2026-01-01", "garmin", steps=9000, sleep_minutes=450)
    insert(conn, "2026-01-01", "google_health", steps=8000)

    assert recompute_derived(conn) == 2
    first = derived(conn)
    assert recompute_derived(conn) == 2
    assert derived(conn) == first


def test_recompute_with_no_readings_derives_nothing(conn):
    assert recompute_derived(conn) == 0
    assert derived(conn) == []


# ---------- the first-boot backfill ----------


def test_backfill_derives_existing_readings_into_an_empty_table(conn):
    insert(conn, "2026-01-01", "garmin", steps=9000)
    insert(conn, "2026-01-01", "google_health", steps=8000, sleep_minutes=390)
    insert(conn, "2026-01-02", "manual", weight_kg=82.0)

    assert backfill_derived(conn) is True

    filled = derived(conn)
    assert [(d, m, s) for d, m, _, _, s, _ in filled] == [
        ("2026-01-01", "sleep_minutes", "google_health"),
        ("2026-01-01", "steps", "garmin"),
        ("2026-01-02", "weight_kg", "manual"),
    ]
    # A second call is stable: the table isn't empty any more.
    assert backfill_derived(conn) is False
    assert derived(conn) == filled


def test_backfill_never_rewrites_values_already_derived(conn):
    insert(conn, "2026-01-01", "garmin", steps=9000)
    backfill_derived(conn)

    conn.execute("UPDATE metrics SET value = 1")
    conn.commit()

    assert backfill_derived(conn) is False
    assert picked(conn, "2026-01-01", STEPS) == (9000, "garmin")


def test_backfill_of_an_empty_database_does_nothing(conn):
    assert backfill_derived(conn) is False
    assert derived(conn) == []


def stub_startup(monkeypatch, db_path):
    """Run serve() against db_path without a scheduler or a server.

    Returns the calls it made, in order, each with the number of derived
    rows there were at the time.
    """
    calls = []

    def count():
        c = sqlite3.connect(db_path)
        try:
            return c.execute("SELECT COUNT(*) FROM derived_metrics").fetchone()[0]
        finally:
            c.close()

    monkeypatch.setitem(main.app.config, "DB_PATH", db_path)
    monkeypatch.setattr(main, "start_scheduler", lambda: calls.append(("scheduler", count())))
    monkeypatch.setattr(main.app, "run", lambda **kwargs: calls.append(("run", count())))
    return calls


def test_serve_backfills_an_upgraded_database_before_syncing(db_at_version, monkeypatch):
    """The first boot after 008: readings from before it are derived
    once, before the scheduler can sync anything."""
    path = db_at_version(7)
    conn = sqlite3.connect(path)
    insert(conn, "2026-01-01", "garmin", steps=9000)
    insert(conn, "2026-01-01", "google_health", steps=8000, sleep_minutes=390)
    conn.close()
    calls = stub_startup(monkeypatch, path)

    main.serve()

    assert calls == [("scheduler", 2), ("run", 2)]
    conn = sqlite3.connect(path)
    assert picked(conn, "2026-01-01", STEPS) == (9000, "garmin")
    assert picked(conn, "2026-01-01", SLEEP_MINUTES) == (390, "google_health")
    conn.close()


def test_serve_does_not_re_derive_on_later_boots(db_path, conn, monkeypatch):
    insert(conn, "2026-01-01", "garmin", steps=9000)
    recompute_derived(conn)
    conn.execute("UPDATE metrics SET value = 1")
    conn.commit()
    stub_startup(monkeypatch, db_path)

    main.serve()

    assert picked(conn, "2026-01-01", STEPS) == (9000, "garmin")


# ---------- syncs re-derive their window ----------


class FakePlugin(SyncPlugin):
    """A SyncPlugin that writes the (date, metric, value) readings it's handed."""

    name = "Fake source"
    required_env = []

    def __init__(self, plugin_id, readings=()):
        self.id = plugin_id
        self.readings = list(readings)

    def sync(self, conn, days: int) -> int:
        for d, metric, value in self.readings:
            write_metric(conn, d, self.id, metric, value)
        conn.commit()
        return len(self.readings)


def test_a_manual_sync_refreshes_the_derived_values(client, conn, monkeypatch):
    today = days_ago(0)
    insert(conn, today, "google_health", steps=8000)
    recompute_derived(conn)
    monkeypatch.setattr(main, "PLUGINS", {"garmin": FakePlugin("garmin", [(today, STEPS, 9000)])})

    assert client.post("/api/sync/garmin").get_json() == {"ok": True, "days_written": 1}

    assert picked(conn, today, STEPS) == (9000, "garmin")
    assert client.get("/api/activity").get_json()[0]["steps"] == 9000


def test_a_scheduled_sync_refreshes_the_derived_values(client, conn, monkeypatch):
    today = days_ago(0)
    monkeypatch.setattr(main, "PLUGINS", {
        "google_health": FakePlugin("google_health", [(today, SLEEP_MINUTES, 390)]),
    })

    main.scheduled_sync()

    assert picked(conn, today, SLEEP_MINUTES) == (390, "google_health")


def test_a_sync_re_derives_its_own_window_and_nothing_older(client, conn, monkeypatch):
    # A 3-day sync covers today and the two days before it.
    insert(conn, days_ago(3), "garmin", steps=1)
    insert(conn, days_ago(2), "garmin", steps=1)
    recompute_derived(conn)
    conn.execute("UPDATE metrics SET value = 2")
    conn.commit()
    monkeypatch.setattr(main, "PLUGINS", {"garmin": FakePlugin("garmin")})

    assert client.post("/api/sync/garmin?days=3").get_json()["ok"] is True

    assert picked(conn, days_ago(2), STEPS) == (2, "garmin")
    assert picked(conn, days_ago(3), STEPS) == (1, "garmin")


# ---------- /api/activity reads the stored value ----------


def test_activity_reads_the_stored_derived_value(client, conn):
    insert(conn, "2026-01-01", "garmin", steps=9000)
    insert(conn, "2026-01-02", "garmin", steps=7000)
    recompute_derived(conn)
    # Change what's stored, not the readings: the default view follows
    # derived_metrics, ?by_source=1 still shows the raw readings.
    conn.execute("UPDATE derived_metrics SET value = 1234 WHERE date = '2026-01-01'")
    conn.execute("DELETE FROM derived_metrics WHERE date = '2026-01-02'")
    conn.commit()

    assert client.get("/api/activity").get_json() == [
        {"date": "2026-01-01", "steps": 1234, "resting_hr": None, "sleep_hours": None,
         "source": "garmin"},
    ]
    assert [r["steps"] for r in client.get("/api/activity?by_source=1").get_json()] == [7000, 9000]
