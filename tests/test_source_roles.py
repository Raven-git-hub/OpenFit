"""
Source roles: the per-metric primary source the user can set.

The source_roles settings row maps a metric to its configured primary;
recompute_derived() puts that source first for the metric and keeps the
default order for the rest, and a metric with no entry is picked exactly
as before. /api/source-roles reads and sets it - forward-only: a change
re-derives nothing until the next sync.
"""

import json
from datetime import date, timedelta

import pytest

import main
from derived import (
    SOURCE_ROLES_KEY,
    load_source_roles,
    recompute_derived,
    set_source_role,
    source_rank,
)
from metrics import METRICS, RESTING_HR_BPM, SLEEP_MINUTES, STEPS, WEIGHT_KG
from plugins.base import SyncPlugin, write_metric


def insert(conn, date, source, **readings):
    """Seed one source's readings for a day, keyed by canonical metric."""
    for metric, value in readings.items():
        conn.execute(
            "INSERT INTO metrics (date, source, metric, value, unit, synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (date, source, metric, value, METRICS[metric], "2026-01-01 06:00:00"),
        )
    conn.commit()


def store_roles(conn, value):
    """Write the source_roles settings row as-is (a string, or a dict as JSON)."""
    if not isinstance(value, str):
        value = json.dumps(value)
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (SOURCE_ROLES_KEY, value),
    )
    conn.commit()


def stored_roles(conn):
    """The source_roles settings row, parsed, or None if there isn't one."""
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (SOURCE_ROLES_KEY,)
    ).fetchone()
    return json.loads(row[0]) if row else None


def picked(conn, date, metric):
    """(value, source) derived for one (date, metric), or None."""
    row = conn.execute(
        "SELECT value, source FROM derived_metrics WHERE date = ? AND metric = ?",
        (date, metric),
    ).fetchone()
    return tuple(row) if row else None


def days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


# ---------- the rank ----------


def test_a_configured_primary_outranks_every_other_source():
    assert source_rank("google_health", "google_health") < source_rank("garmin", "google_health")
    assert source_rank("manual", "manual") < source_rank("garmin", "manual")
    # The rest keep the default order.
    assert source_rank("garmin", "manual") < source_rank("google_health", "manual")
    assert source_rank("google_health", "manual") < source_rank("aardvark", "manual")


def test_no_primary_is_the_default_rank():
    for source in ("garmin", "google_health", "manual", None):
        assert source_rank(source, None) == source_rank(source)


# ---------- the pick honours the config ----------


def test_a_configured_primary_wins_its_metric(conn):
    insert(conn, "2026-01-01", "garmin", steps=9000, sleep_minutes=450)
    insert(conn, "2026-01-01", "google_health", steps=8000, sleep_minutes=390)
    store_roles(conn, {STEPS: "google_health"})

    recompute_derived(conn)

    assert picked(conn, "2026-01-01", STEPS) == (8000, "google_health")
    # sleep has no override: the default still picks Garmin.
    assert picked(conn, "2026-01-01", SLEEP_MINUTES) == (450, "garmin")


def test_a_primary_that_missed_a_day_falls_back_to_the_default_order(conn):
    store_roles(conn, {STEPS: "google_health"})
    insert(conn, "2026-01-01", "google_health", steps=8000)
    insert(conn, "2026-01-01", "garmin", steps=9000)
    # No Google reading on the 2nd: the default order picks, Garmin
    # ahead of the unlisted 'manual' (not name order).
    insert(conn, "2026-01-02", "manual", steps=1)
    insert(conn, "2026-01-02", "garmin", steps=7000)

    recompute_derived(conn)

    assert picked(conn, "2026-01-01", STEPS) == (8000, "google_health")
    assert picked(conn, "2026-01-02", STEPS) == (7000, "garmin")


def test_any_source_can_be_primary(conn):
    # Not only the ones in DEFAULT_PRIORITY: a hand-entered weight can
    # outrank a device's.
    insert(conn, "2026-01-01", "garmin", weight_kg=83.0)
    insert(conn, "2026-01-01", "manual", weight_kg=82.5)
    store_roles(conn, {WEIGHT_KG: "manual"})

    recompute_derived(conn)

    assert picked(conn, "2026-01-01", WEIGHT_KG) == (82.5, "manual")


def test_with_no_config_the_pick_is_the_default(conn):
    insert(conn, "2026-01-01", "garmin", steps=9000)
    insert(conn, "2026-01-01", "google_health", steps=8000, resting_hr_bpm=61)

    recompute_derived(conn)

    assert picked(conn, "2026-01-01", STEPS) == (9000, "garmin")
    assert picked(conn, "2026-01-01", RESTING_HR_BPM) == (61, "google_health")


def test_recompute_reads_the_config_from_settings_each_time(conn):
    insert(conn, "2026-01-01", "garmin", steps=9000)
    insert(conn, "2026-01-01", "google_health", steps=8000)

    store_roles(conn, {STEPS: "google_health"})
    recompute_derived(conn)
    assert picked(conn, "2026-01-01", STEPS) == (8000, "google_health")

    store_roles(conn, {})
    recompute_derived(conn)
    assert picked(conn, "2026-01-01", STEPS) == (9000, "garmin")


def test_a_bad_config_falls_back_to_the_default(conn):
    insert(conn, "2026-01-01", "garmin", steps=9000)
    insert(conn, "2026-01-01", "google_health", steps=8000)

    for bad in ("not json", "", '["google_health"]', {STEPS: 5}, {STEPS: ""}):
        store_roles(conn, bad)
        assert load_source_roles(conn) == {}, bad
        recompute_derived(conn)
        assert picked(conn, "2026-01-01", STEPS) == (9000, "garmin"), bad


def test_set_source_role_stores_sets_and_clears(conn):
    assert load_source_roles(conn) == {}

    set_source_role(conn, STEPS, "google_health")
    set_source_role(conn, WEIGHT_KG, "manual")
    assert stored_roles(conn) == {STEPS: "google_health", WEIGHT_KG: "manual"}

    set_source_role(conn, STEPS, "garmin")
    set_source_role(conn, WEIGHT_KG, None)
    assert stored_roles(conn) == {STEPS: "garmin"}
    assert load_source_roles(conn) == {STEPS: "garmin"}


def test_set_source_role_refuses_an_unknown_metric(conn):
    with pytest.raises(ValueError):
        set_source_role(conn, "stepz", "garmin")
    assert stored_roles(conn) is None


# ---------- GET /api/source-roles ----------


def test_get_on_an_empty_database_lists_every_metric_unconfigured(client):
    assert client.get("/api/source-roles").get_json() == {
        metric: {"primary": None, "configured": False, "sources": []}
        for metric in METRICS
    }


def test_get_shows_the_default_primary_and_the_sources_that_reported(client, conn):
    insert(conn, "2026-01-01", "manual", steps=1, weight_kg=82.0)
    insert(conn, "2026-01-01", "google_health", steps=8000, sleep_minutes=390)
    insert(conn, "2026-01-02", "garmin", steps=9000)

    roles = client.get("/api/source-roles").get_json()

    # Sources in the order the pick tries them; the primary is the first.
    assert roles[STEPS] == {
        "primary": "garmin",
        "configured": False,
        "sources": ["garmin", "google_health", "manual"],
    }
    assert roles[SLEEP_MINUTES] == {
        "primary": "google_health", "configured": False, "sources": ["google_health"],
    }
    assert roles[WEIGHT_KG] == {"primary": "manual", "configured": False, "sources": ["manual"]}
    assert roles[RESTING_HR_BPM] == {"primary": None, "configured": False, "sources": []}


def test_get_shows_a_configured_primary_first(client, conn):
    insert(conn, "2026-01-01", "garmin", steps=9000)
    insert(conn, "2026-01-01", "google_health", steps=8000)
    insert(conn, "2026-01-01", "manual", steps=1)
    store_roles(conn, {STEPS: "manual", SLEEP_MINUTES: "oura"})

    roles = client.get("/api/source-roles").get_json()

    assert roles[STEPS] == {
        "primary": "manual",
        "configured": True,
        "sources": ["manual", "garmin", "google_health"],
    }
    # A primary that hasn't reported yet is still the primary.
    assert roles[SLEEP_MINUTES] == {"primary": "oura", "configured": True, "sources": []}


# ---------- PUT /api/source-roles/<metric> ----------


def test_put_stores_an_override(client, conn):
    resp = client.put(f"/api/source-roles/{STEPS}", json={"primary": "google_health"})

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    assert stored_roles(conn) == {STEPS: "google_health"}
    assert client.get("/api/source-roles").get_json()[STEPS] == {
        "primary": "google_health", "configured": True, "sources": [],
    }


def test_put_keeps_the_other_metrics_overrides(client, conn):
    client.put(f"/api/source-roles/{STEPS}", json={"primary": "google_health"})
    client.put(f"/api/source-roles/{WEIGHT_KG}", json={"primary": "manual"})
    client.put(f"/api/source-roles/{STEPS}", json={"primary": " garmin "})

    assert stored_roles(conn) == {STEPS: "garmin", WEIGHT_KG: "manual"}


def test_put_with_an_unknown_metric_is_a_400(client, conn):
    resp = client.put("/api/source-roles/stepz", json={"primary": "garmin"})

    assert resp.status_code == 400
    assert "error" in resp.get_json()
    assert stored_roles(conn) is None


def test_put_with_an_invalid_body_is_a_400(client, conn):
    for body in ({}, {"source": "garmin"}, {"primary": 5}, {"primary": ["garmin"]},
                 {"primary": {"id": "garmin"}}, {"primary": True}, ["garmin"], "garmin"):
        resp = client.put(f"/api/source-roles/{STEPS}", json=body)
        assert resp.status_code == 400, body
        assert "error" in resp.get_json()

    resp = client.put(f"/api/source-roles/{STEPS}", data="not json",
                      content_type="application/json")
    assert resp.status_code == 400

    # Nothing was stored by any of the rejected requests.
    assert stored_roles(conn) is None


def test_clearing_a_primary_reverts_to_the_default(client, conn):
    insert(conn, "2026-01-01", "garmin", steps=9000)
    insert(conn, "2026-01-01", "google_health", steps=8000)

    for cleared in (None, "", "   "):
        client.put(f"/api/source-roles/{STEPS}", json={"primary": "google_health"})
        resp = client.put(f"/api/source-roles/{STEPS}", json={"primary": cleared})

        assert resp.status_code == 200, cleared
        assert stored_roles(conn) == {}, cleared
        assert client.get("/api/source-roles").get_json()[STEPS] == {
            "primary": "garmin", "configured": False, "sources": ["garmin", "google_health"],
        }

    recompute_derived(conn)
    assert picked(conn, "2026-01-01", STEPS) == (9000, "garmin")


# ---------- forward-only ----------


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


def test_a_change_rewrites_nothing_until_a_sync_re_derives_its_window(client, conn, monkeypatch):
    for d in (days_ago(10), days_ago(0)):
        insert(conn, d, "garmin", steps=9000)
        insert(conn, d, "google_health", steps=8000)
    recompute_derived(conn)
    before = [tuple(r) for r in conn.execute("SELECT * FROM derived_metrics ORDER BY date")]

    assert client.put(f"/api/source-roles/{STEPS}", json={"primary": "google_health"}).status_code == 200

    # Stored, not applied: every derived row is exactly as it was.
    assert [tuple(r) for r in conn.execute("SELECT * FROM derived_metrics ORDER BY date")] == before
    assert client.get("/api/activity").get_json()[0]["steps"] == 9000

    # The next sync re-derives its own window under the new role - and
    # only that window: the older day keeps the source it was picked with.
    monkeypatch.setattr(main, "PLUGINS", {"garmin": FakePlugin("garmin")})
    assert client.post("/api/sync/garmin?days=7").get_json()["ok"] is True

    assert picked(conn, days_ago(0), STEPS) == (8000, "google_health")
    assert picked(conn, days_ago(10), STEPS) == (9000, "garmin")
    assert client.get("/api/activity").get_json()[0]["steps"] == 8000
