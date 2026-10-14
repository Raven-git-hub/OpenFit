"""
Per-source sync cadence.

Every source syncs at the baseline, SYNC_INTERVAL_HOURS (6h by default),
unless its Settings popup overrides it with one of the presets - 1, 3, 12
or 24 hours. The overrides are the sync_cadences settings row, JSON
{plugin_id: hours}, and only overrides are stored: "Default" clears the
entry, never stores a 6. The scheduler runs one interval job per
connected plugin at its effective cadence; connecting a source adds its
job, changing the cadence moves it and removing the source drops it.

No live scheduler here: a FakeScheduler records the jobs it is given,
and nothing fires. No network either.
"""

import json
import sqlite3

import pytest
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.base import ConflictingIdError, JobLookupError

import main
from main import (
    ALLOWED_CADENCE_HOURS,
    SYNC_CADENCES_KEY,
    effective_cadence,
    load_sync_cadences,
    set_sync_cadence,
)
from metrics import STEPS
from plugins.base import SyncPlugin, write_metric

GARMIN = {"email": "me@example.com", "password": "hunter2"}


@pytest.fixture(autouse=True)
def known_state(monkeypatch):
    """A 6h baseline, no device env and no scheduler, whatever the dev's shell holds."""
    for var in (
        "GARMIN_EMAIL",
        "GARMIN_PASSWORD",
        "GOOGLE_HEALTH_CLIENT_ID",
        "GOOGLE_HEALTH_CLIENT_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(main, "SYNC_INTERVAL_HOURS", 6)
    monkeypatch.setattr(main, "_scheduler", None)


def store_cadences(conn, value):
    """Write the sync_cadences settings row as-is (a string, or a dict as JSON)."""
    if not isinstance(value, str):
        value = json.dumps(value)
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (SYNC_CADENCES_KEY, value),
    )
    conn.commit()


def stored_cadences(db_path):
    """The sync_cadences settings row, parsed, or None if there isn't one."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (SYNC_CADENCES_KEY,)
        ).fetchone()
    finally:
        conn.close()
    return json.loads(row[0]) if row else None


def add_garmin(client):
    assert client.post("/api/connectors/garmin", json=GARMIN).status_code == 200


def put_cadence(client, body, plugin_id="garmin"):
    return client.put(f"/api/connectors/{plugin_id}/cadence", json=body)


def detail(client, plugin_id="garmin"):
    return client.get(f"/api/connectors/{plugin_id}/detail").get_json()


# ---------- the store ----------


def test_the_presets():
    # 6 is the baseline, expressed by clearing - never an override.
    assert ALLOWED_CADENCE_HOURS == (1, 3, 12, 24)


def test_no_overrides_on_a_fresh_install(conn):
    assert load_sync_cadences(conn) == {}
    assert effective_cadence(conn, "garmin") == 6
    assert effective_cadence(conn, "google_health") == 6


@pytest.mark.parametrize("hours", ALLOWED_CADENCE_HOURS)
def test_an_override_is_the_effective_cadence(conn, hours):
    set_sync_cadence(conn, "garmin", hours)

    assert effective_cadence(conn, "garmin") == hours
    # Only that source's.
    assert effective_cadence(conn, "google_health") == 6


def test_without_an_override_the_baseline_is_whatever_the_env_set(conn, monkeypatch):
    monkeypatch.setattr(main, "SYNC_INTERVAL_HOURS", 4)

    assert effective_cadence(conn, "garmin") == 4
    set_sync_cadence(conn, "garmin", 12)
    assert effective_cadence(conn, "garmin") == 12


def test_set_sync_cadence_stores_sets_and_clears(conn, db_path):
    set_sync_cadence(conn, "garmin", 1)
    set_sync_cadence(conn, "google_health", 24)
    assert stored_cadences(db_path) == {"garmin": 1, "google_health": 24}

    set_sync_cadence(conn, "garmin", 3)
    assert load_sync_cadences(conn) == {"garmin": 3, "google_health": 24}

    # Clearing removes the entry rather than storing the baseline.
    set_sync_cadence(conn, "garmin", None)
    assert stored_cadences(db_path) == {"google_health": 24}
    assert effective_cadence(conn, "garmin") == 6

    # Clearing what isn't there is fine.
    set_sync_cadence(conn, "garmin", None)
    assert stored_cadences(db_path) == {"google_health": 24}


@pytest.mark.parametrize("hours", [6, 0, -1, 2, 48, 1.0, "3", True, [1]])
def test_set_sync_cadence_refuses_anything_but_a_preset(conn, db_path, hours):
    with pytest.raises(ValueError):
        set_sync_cadence(conn, "garmin", hours)

    assert stored_cadences(db_path) is None


@pytest.mark.parametrize("raw", ["not json", "[1, 3]", '"garmin"', "3", "null", ""])
def test_a_malformed_value_reads_as_no_overrides(conn, raw):
    """Written through /api/settings, it mustn't break scheduling - just fall back."""
    store_cadences(conn, raw)

    assert load_sync_cadences(conn) == {}
    assert effective_cadence(conn, "garmin") == 6


def test_a_bad_entry_is_dropped_and_the_good_ones_kept(conn):
    store_cadences(conn, {
        "garmin": 6,           # the baseline, never stored: reads as default
        "google_health": 3,
        "a": "12", "b": True, "c": 2.0, "d": 0, "e": None, "f": 5,
    })

    assert load_sync_cadences(conn) == {"google_health": 3}
    assert effective_cadence(conn, "garmin") == 6


def test_setting_over_a_malformed_value_replaces_it(conn, db_path):
    store_cadences(conn, "not json")

    set_sync_cadence(conn, "garmin", 3)

    assert stored_cadences(db_path) == {"garmin": 3}


def test_a_cadence_is_kept_beside_source_roles(conn, db_path):
    """Its own settings row: setting one doesn't touch the other."""
    from derived import load_source_roles, set_source_role

    set_source_role(conn, STEPS, "google_health")
    set_sync_cadence(conn, "garmin", 12)

    assert load_source_roles(conn) == {STEPS: "google_health"}
    assert load_sync_cadences(conn) == {"garmin": 12}


# ---------- PUT /api/connectors/<id>/cadence ----------


@pytest.mark.parametrize("hours", ALLOWED_CADENCE_HOURS)
def test_put_sets_each_preset(client, db_path, hours):
    add_garmin(client)

    resp = put_cadence(client, {"hours": hours})

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    assert stored_cadences(db_path) == {"garmin": hours}


@pytest.mark.parametrize("body", [{"hours": None}, {}])
def test_put_null_or_no_hours_clears_back_to_default(client, db_path, body):
    add_garmin(client)
    put_cadence(client, {"hours": 3})

    resp = put_cadence(client, body)

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    assert stored_cadences(db_path) == {}
    assert detail(client)["sync_is_default"] is True


def test_put_6_is_a_400(client, db_path):
    """Default is null, not the number: 6 is never stored."""
    add_garmin(client)
    put_cadence(client, {"hours": 3})

    resp = put_cadence(client, {"hours": 6})

    assert resp.status_code == 400
    body = resp.get_json()
    assert body["ok"] is False
    assert "null" in body["error"]
    # The override it had stays.
    assert stored_cadences(db_path) == {"garmin": 3}


@pytest.mark.parametrize("hours", ["3", "default", 3.0, 2, 0, -1, 48, True, False, [3], {"h": 3}])
def test_put_junk_hours_is_a_400(client, db_path, hours):
    resp = put_cadence(client, {"hours": hours})

    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False
    assert stored_cadences(db_path) is None


@pytest.mark.parametrize("body", ["not json", "[3]", "3", ""])
def test_put_a_body_that_isnt_an_object_is_a_400(client, db_path, body):
    resp = client.put(
        "/api/connectors/garmin/cadence", data=body, content_type="application/json"
    )

    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False
    assert stored_cadences(db_path) is None


def test_put_for_an_unknown_connector_is_a_404(client, db_path):
    resp = put_cadence(client, {"hours": 3}, plugin_id="nope")

    assert resp.status_code == 404
    assert resp.get_json()["ok"] is False
    assert stored_cadences(db_path) is None


def test_put_leaves_the_other_sources_override_alone(client, db_path):
    put_cadence(client, {"hours": 24}, plugin_id="google_health")
    put_cadence(client, {"hours": 1})
    put_cadence(client, {"hours": None})

    assert stored_cadences(db_path) == {"google_health": 24}


# ---------- /detail ----------


def test_detail_reports_the_baseline_then_the_override(client):
    add_garmin(client)
    assert detail(client)["sync_hours"] == 6
    assert detail(client)["sync_is_default"] is True

    put_cadence(client, {"hours": 12})
    assert detail(client)["sync_hours"] == 12
    assert detail(client)["sync_is_default"] is False

    put_cadence(client, {"hours": None})
    assert detail(client)["sync_hours"] == 6
    assert detail(client)["sync_is_default"] is True


def test_detail_reports_the_cadence_of_a_source_that_isnt_connected(client):
    assert detail(client, "google_health")["sync_hours"] == 6
    assert detail(client, "google_health")["sync_is_default"] is True

    put_cadence(client, {"hours": 3}, plugin_id="google_health")

    assert detail(client, "google_health")["sync_hours"] == 3
    assert detail(client, "google_health")["sync_is_default"] is False
    # Still not connected: a cadence isn't an account.
    assert detail(client, "google_health")["added"] is None


def test_detail_reports_the_env_baseline(client, monkeypatch):
    monkeypatch.setattr(main, "SYNC_INTERVAL_HOURS", 4)

    assert detail(client)["sync_hours"] == 4
    assert detail(client)["sync_is_default"] is True


def test_detail_reads_a_malformed_value_as_default(client, conn):
    store_cadences(conn, {"garmin": 6})

    assert detail(client)["sync_hours"] == 6
    assert detail(client)["sync_is_default"] is True


# ---------- the scheduler's jobs ----------


class FakeScheduler:
    """Stands in for BackgroundScheduler: records its jobs, runs nothing."""

    def __init__(self, **config):
        self.config = config
        self.jobs = {}
        self.started = False

    def add_job(self, func, trigger, args=(), id=None, replace_existing=False, **trigger_args):
        job_id = id or f"anonymous-{len(self.jobs)}"
        if job_id in self.jobs and not replace_existing:
            raise ConflictingIdError(job_id)
        self.jobs[job_id] = {"func": func, "trigger": trigger, "args": list(args), **trigger_args}

    def remove_job(self, job_id):
        if job_id not in self.jobs:
            raise JobLookupError(job_id)
        del self.jobs[job_id]

    def start(self):
        self.started = True


def intervals(scheduler):
    """{job id: hours} for each per-plugin job, checking each is sync_one(<its plugin>)."""
    out = {}
    for job_id, job in scheduler.jobs.items():
        if job["trigger"] != "interval":
            continue
        assert job["func"] is main.sync_one
        assert job_id == f"sync:{job['args'][0]}"
        out[job_id] = job["hours"]
    return out


@pytest.fixture
def running(client, monkeypatch):
    """A FakeScheduler standing in as the running one."""
    scheduler = FakeScheduler()
    monkeypatch.setattr(main, "_scheduler", scheduler)
    return scheduler


@pytest.fixture
def start_scheduler(client, monkeypatch):
    """Run start_scheduler() against a FakeScheduler, and return it."""
    monkeypatch.setattr(main, "BackgroundScheduler", FakeScheduler)

    def start():
        scheduler = main.start_scheduler()
        assert main._scheduler is scheduler
        assert scheduler.started
        return scheduler

    return start


def test_the_helpers_do_nothing_without_a_scheduler(client, monkeypatch):
    """Importing main never starts one - so nothing may need one, or touch the database."""
    def no_database():
        raise AssertionError("opened a connection with no scheduler to change")

    monkeypatch.setattr(main, "get_conn", no_database)

    main.reschedule_plugin("garmin")
    main.remove_plugin_job("garmin")


def test_the_routes_work_without_a_scheduler(client, db_path):
    add_garmin(client)
    assert put_cadence(client, {"hours": 1}).status_code == 200
    assert client.delete("/api/connectors/garmin").status_code == 200
    assert stored_cadences(db_path) == {"garmin": 1}


def test_a_fresh_install_starts_with_only_the_catch_up(start_scheduler):
    scheduler = start_scheduler()

    assert list(scheduler.jobs.values()) == [
        {"func": main.scheduled_sync, "trigger": "date", "args": []}
    ]


def test_start_adds_a_job_per_connected_plugin_at_its_cadence(client, conn, start_scheduler):
    add_garmin(client)
    set_sync_cadence(conn, "garmin", 3)
    # google_health has an override but no account: no job.
    set_sync_cadence(conn, "google_health", 1)

    scheduler = start_scheduler()

    assert intervals(scheduler) == {"sync:garmin": 3}
    # The startup catch-up of every plugin is still there.
    assert [j["func"] for j in scheduler.jobs.values() if j["trigger"] == "date"] == [
        main.scheduled_sync
    ]


def test_start_gives_a_connected_plugin_with_no_override_the_baseline(client, start_scheduler, monkeypatch):
    monkeypatch.setattr(main, "SYNC_INTERVAL_HOURS", 4)
    add_garmin(client)

    assert intervals(start_scheduler()) == {"sync:garmin": 4}


def test_an_env_configured_install_keeps_its_job(client, start_scheduler, monkeypatch):
    """A pre-UI install has no account row, but the old global job synced it."""
    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")

    assert intervals(start_scheduler()) == {"sync:garmin": 6}


def test_jobs_due_together_queue_rather_than_skip(start_scheduler):
    """One worker, and no misfire cutoff: two sources ticking in step both sync.

    With the default pool they would race for _sync_lock and one would
    skip, every tick; queued, a job that starts late must not be dropped.
    """
    config = start_scheduler().config

    executor = config["executors"]["default"]
    assert isinstance(executor, ThreadPoolExecutor)
    assert executor._pool._max_workers == 1
    assert config["job_defaults"]["misfire_grace_time"] is None


def test_connecting_a_source_adds_its_job(client, running):
    add_garmin(client)

    assert intervals(running) == {"sync:garmin": 6}


def test_connecting_a_source_with_an_override_uses_it(client, running):
    """An override set while it wasn't connected - or kept from before - applies."""
    put_cadence(client, {"hours": 12})
    assert intervals(running) == {}

    add_garmin(client)

    assert intervals(running) == {"sync:garmin": 12}


def test_finishing_an_oauth_add_adds_its_job(client, running, monkeypatch):
    class TokenResponse:
        ok = True

        def json(self):
            return {"refresh_token": "refresh-abc", "access_token": "access-xyz"}

        def raise_for_status(self):
            pass

    monkeypatch.setattr(
        "plugins.google_health.plugin.requests.post", lambda *a, **k: TokenResponse()
    )
    main._oauth_pending.clear()
    try:
        client.post(
            "/api/connectors/google_health/oauth/start",
            json={"client_id": "cid.apps.googleusercontent.com", "client_secret": "shh"},
        )
        resp = client.post(
            "/api/connectors/google_health/oauth/finish", json={"code": "4/abc"}
        )
    finally:
        main._oauth_pending.clear()

    assert resp.get_json() == {"ok": True}
    assert intervals(running) == {"sync:google_health": 6}


def test_reconnecting_keeps_one_job(client, running):
    add_garmin(client)
    add_garmin(client)

    assert intervals(running) == {"sync:garmin": 6}


def test_changing_the_cadence_moves_the_job(client, running):
    add_garmin(client)

    for hours in ALLOWED_CADENCE_HOURS:
        put_cadence(client, {"hours": hours})
        assert intervals(running) == {"sync:garmin": hours}

    put_cadence(client, {"hours": None})
    assert intervals(running) == {"sync:garmin": 6}


def test_a_rejected_cadence_leaves_the_job_alone(client, running):
    add_garmin(client)
    put_cadence(client, {"hours": 3})

    put_cadence(client, {"hours": 6})

    assert intervals(running) == {"sync:garmin": 3}


def test_a_cadence_for_a_source_that_isnt_connected_adds_no_job(client, running):
    put_cadence(client, {"hours": 1}, plugin_id="google_health")

    assert intervals(running) == {}


def test_removing_a_source_drops_its_job(client, running):
    add_garmin(client)
    put_cadence(client, {"hours": 24}, plugin_id="google_health")

    assert client.delete("/api/connectors/garmin").status_code == 200

    assert intervals(running) == {}
    # And removing it again - no job left - is still fine.
    assert client.delete("/api/connectors/garmin").status_code == 200


def test_a_cadence_outlives_a_disconnect_and_applies_on_re_add(client, running, db_path):
    """Like a source role: removing the source keeps its config."""
    add_garmin(client)
    put_cadence(client, {"hours": 12})

    client.delete("/api/connectors/garmin?wipe=1")
    assert stored_cadences(db_path) == {"garmin": 12}
    assert intervals(running) == {}

    add_garmin(client)
    assert intervals(running) == {"sync:garmin": 12}


def test_removing_a_source_with_a_wipe_drops_its_job(client, running):
    add_garmin(client)

    assert client.delete("/api/connectors/garmin?wipe=1").status_code == 200

    assert intervals(running) == {}


def test_removing_one_source_leaves_the_others_job(client, running, monkeypatch):
    add_garmin(client)
    # A second job as start_scheduler() would add it for another plugin.
    main._add_sync_job(running, "google_health", 3)

    client.delete("/api/connectors/garmin")

    assert intervals(running) == {"sync:google_health": 3}


# ---------- sync_one: a plugin's own tick ----------


class RecordingPlugin(SyncPlugin):
    """Writes one reading per sync and counts its runs."""

    name = "Recording source"

    def __init__(self, plugin_id, configured=True):
        self.id = plugin_id
        self.required_env = [] if configured else ["NEVER_SET_IN_TESTS"]
        self.runs = 0

    def sync(self, conn, days: int) -> int:
        self.runs += 1
        write_metric(conn, "2026-10-01", self.id, STEPS, 9000)
        conn.commit()
        return 1


def test_a_tick_syncs_only_its_own_plugin(client, monkeypatch, capsys):
    a, b = RecordingPlugin("a"), RecordingPlugin("b")
    monkeypatch.setattr(main, "PLUGINS", {"a": a, "b": b})

    main.sync_one("a")

    assert (a.runs, b.runs) == (1, 0)
    assert "[scheduler] a wrote 1 day(s)" in capsys.readouterr().out
    assert not main._sync_lock.locked()


def test_a_tick_for_an_unconfigured_plugin_syncs_nothing(client, monkeypatch, capsys):
    plugin = RecordingPlugin("a", configured=False)
    monkeypatch.setattr(main, "PLUGINS", {"a": plugin})

    main.sync_one("a")

    assert plugin.runs == 0
    assert capsys.readouterr().out == ""
    assert not main._sync_lock.locked()


def test_a_tick_for_a_plugin_that_has_gone_is_harmless(client, monkeypatch):
    monkeypatch.setattr(main, "PLUGINS", {})

    main.sync_one("garmin")

    assert not main._sync_lock.locked()
