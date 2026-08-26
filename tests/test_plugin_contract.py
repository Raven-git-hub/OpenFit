"""
Plugin sync() contract, exercised with a fake in-process plugin.

Nothing here talks to Garmin or Google - the point is the database
contract every SyncPlugin has to honour, not any particular vendor API.
"""

import pytest

from plugins.base import SyncPlugin


class FakePlugin(SyncPlugin):
    """A minimal SyncPlugin that writes whatever rows it's handed."""

    name = "Fake source"
    required_env = []

    def __init__(self, plugin_id, rows):
        self.id = plugin_id
        self.rows = rows

    def sync(self, conn, days: int) -> int:
        written = 0
        for row in self.rows[:days] if days else self.rows:
            conn.execute(
                """
                INSERT INTO activity (date, steps, resting_hr, sleep_hours, source, synced_at)
                VALUES (?, ?, ?, ?, ?, datetime('now'))
                ON CONFLICT(date, source) DO UPDATE SET
                    steps=COALESCE(excluded.steps, activity.steps),
                    resting_hr=COALESCE(excluded.resting_hr, activity.resting_hr),
                    sleep_hours=COALESCE(excluded.sleep_hours, activity.sleep_hours),
                    synced_at=datetime('now')
                """,
                (
                    row["date"],
                    row.get("steps"),
                    row.get("resting_hr"),
                    row.get("sleep_hours"),
                    self.id,
                ),
            )
            written += 1
        conn.commit()
        return written


def rows_for(conn, source):
    return conn.execute(
        "SELECT date, steps, resting_hr, sleep_hours FROM activity "
        "WHERE source = ? ORDER BY date",
        (source,),
    ).fetchall()


def test_sync_writes_rows_and_returns_count(conn):
    plugin = FakePlugin("fake", [{"date": "2026-01-01", "steps": 9000}])

    assert plugin.sync(conn, 7) == 1
    assert [tuple(r) for r in rows_for(conn, "fake")] == [("2026-01-01", 9000, None, None)]


def test_resync_upserts_rather_than_duplicating(conn):
    FakePlugin("fake", [{"date": "2026-01-01", "steps": 9000}]).sync(conn, 7)
    FakePlugin("fake", [{"date": "2026-01-01", "steps": 9500}]).sync(conn, 7)

    assert [tuple(r) for r in rows_for(conn, "fake")] == [("2026-01-01", 9500, None, None)]


def test_upsert_coalesces_missing_fields(conn):
    """A later partial sync must not blank out fields an earlier one filled."""
    FakePlugin("fake", [{"date": "2026-01-01", "steps": 9000, "sleep_hours": 7.5}]).sync(conn, 7)
    FakePlugin("fake", [{"date": "2026-01-01", "resting_hr": 52}]).sync(conn, 7)

    assert [tuple(r) for r in rows_for(conn, "fake")] == [("2026-01-01", 9000, 52, 7.5)]


def test_two_sources_same_date_do_not_contend(conn):
    """The regression the (date, source) reshape fixes.

    Under the old date-only primary key the second sync overwrote the
    first source's row for the day; now each source keeps its own.
    """
    day = "2026-01-01"
    FakePlugin("garmin", [{"date": day, "steps": 9000, "resting_hr": 52}]).sync(conn, 7)
    FakePlugin("google_health", [{"date": day, "steps": 8000, "sleep_hours": 6.5}]).sync(conn, 7)

    assert [tuple(r) for r in rows_for(conn, "garmin")] == [(day, 9000, 52, None)]
    assert [tuple(r) for r in rows_for(conn, "google_health")] == [(day, 8000, None, 6.5)]

    total = conn.execute("SELECT COUNT(*) FROM activity WHERE date = ?", (day,)).fetchone()[0]
    assert total == 2


def test_sources_stay_independent_across_repeated_syncs(conn):
    day = "2026-01-01"
    garmin = FakePlugin("garmin", [{"date": day, "steps": 9000}])
    google = FakePlugin("google_health", [{"date": day, "steps": 8000}])

    for _ in range(3):
        garmin.sync(conn, 7)
        google.sync(conn, 7)

    assert conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0] == 2
    assert [tuple(r) for r in rows_for(conn, "garmin")] == [(day, 9000, None, None)]


def test_real_plugins_implement_the_interface():
    """The registry's plugins must still be SyncPlugins (no network here)."""
    from plugins import PLUGINS

    assert PLUGINS
    for plugin_id, plugin in PLUGINS.items():
        assert isinstance(plugin, SyncPlugin)
        assert plugin.id == plugin_id
        assert plugin.name
        status = plugin.status()
        assert set(status) >= {"configured", "missing_env"}


def test_sync_plugin_cannot_be_instantiated_without_sync():
    class Incomplete(SyncPlugin):
        id = "incomplete"
        name = "Incomplete"

    with pytest.raises(TypeError):
        Incomplete()
