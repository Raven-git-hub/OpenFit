"""
Database locking: every connection waits on a lock (busy_timeout) and
runs in WAL, set before migrations at startup too; and one sync runs at
a time - a manual sync that finds one running gets a 409, a scheduled
tick that does (the startup catch-up, or one plugin's own job) skips,
and the lock is released however a sync ends.
"""

import sqlite3
import threading
from datetime import date

import pytest
from conftest import signed_in

import main
import migrations
from metrics import STEPS
from plugins.base import SyncPlugin, write_metric

# Upper bound on any wait for another thread, so a regression fails the
# test instead of hanging the suite.
WAIT = 10

SYNC_IN_PROGRESS = {"ok": False, "error": "a sync is already in progress"}


def pragmas(conn):
    """(busy_timeout in ms, journal_mode) as the connection reports them."""
    return (
        conn.execute("PRAGMA busy_timeout").fetchone()[0],
        conn.execute("PRAGMA journal_mode").fetchone()[0],
    )


# ---------- connections ----------


def test_an_app_connection_waits_for_locks_and_runs_in_wal(client):
    conn = main.get_conn()
    try:
        assert pragmas(conn) == (30000, "wal")
    finally:
        conn.close()


def test_startup_migrations_run_on_a_hardened_connection(tmp_path, monkeypatch):
    path = str(tmp_path / "new.db")
    monkeypatch.setitem(main.app.config, "DB_PATH", path)
    seen = []
    real = main.run_migrations

    def spy(conn, **kwargs):
        seen.append(pragmas(conn))
        return real(conn, **kwargs)

    monkeypatch.setattr(main, "run_migrations", spy)

    main.migrate()

    assert seen == [(30000, "wal")]
    # WAL is stored in the database file, so it outlives the connection
    # that set it: even a plain connection opens it in WAL now.
    conn = sqlite3.connect(path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        conn.close()


def test_the_migrations_cli_runs_on_a_hardened_connection(tmp_path, monkeypatch):
    seen = []
    real = migrations.run_migrations

    def spy(conn, **kwargs):
        seen.append(pragmas(conn))
        return real(conn, **kwargs)

    monkeypatch.setattr(migrations, "run_migrations", spy)

    migrations.migrate_path(str(tmp_path / "copy.db"), verbose=False)

    assert seen == [(30000, "wal")]


# ---------- one sync at a time ----------


class BlockingPlugin(SyncPlugin):
    """A SyncPlugin that writes a reading, then holds its transaction open
    until the test lets it finish - a sync caught mid-run."""

    name = "Blocking source"
    required_env = []

    def __init__(self, plugin_id):
        self.id = plugin_id
        self.started = threading.Event()
        self.finish = threading.Event()
        self.runs = 0

    def sync(self, conn, days: int) -> int:
        self.runs += 1
        write_metric(conn, date.today().isoformat(), self.id, STEPS, 9000)
        self.started.set()
        self.finish.wait(WAIT)
        conn.commit()
        return 1


class FailingPlugin(SyncPlugin):
    name = "Failing source"
    required_env = []

    def __init__(self, plugin_id):
        self.id = plugin_id

    def sync(self, conn, days: int) -> int:
        raise RuntimeError("device API is down")


@pytest.fixture
def garmin(client, monkeypatch):
    plugin = BlockingPlugin("garmin")
    monkeypatch.setattr(main, "PLUGINS", {"garmin": plugin})
    return plugin


@pytest.fixture
def background(garmin):
    """start(fn): run fn on its own thread, as the scheduler and each
    request do. Returns the thread and a dict its result lands in."""
    threads = []

    def start(fn):
        result = {}
        thread = threading.Thread(target=lambda: result.update(value=fn()), daemon=True)
        thread.start()
        threads.append(thread)
        return thread, result

    yield start
    # Let any sync still blocked finish, even when the test failed, so
    # it releases the lock before the next test.
    garmin.finish.set()
    for thread in threads:
        thread.join(WAIT)


def post_sync():
    """POST /api/sync/garmin from the calling thread, as a request would."""
    resp = signed_in(main.app.test_client()).post("/api/sync/garmin")
    return resp.status_code, resp.get_json()


def test_a_manual_sync_during_the_scheduled_one_gets_409(client, garmin, background):
    # The startup sync is mid-run when a manual one is asked for.
    scheduled, _ = background(main.scheduled_sync)
    assert garmin.started.wait(WAIT)

    resp = client.post("/api/sync/garmin")

    assert resp.status_code == 409
    assert resp.get_json() == SYNC_IN_PROGRESS
    assert garmin.runs == 1  # no second sync started

    garmin.finish.set()
    scheduled.join(WAIT)
    assert client.post("/api/sync/garmin").get_json() == {"ok": True, "days_written": 1}
    assert garmin.runs == 2


def test_a_second_manual_sync_gets_409_and_the_first_completes(client, garmin, background):
    first, result = background(post_sync)
    assert garmin.started.wait(WAIT)

    resp = client.post("/api/sync/garmin")

    assert resp.status_code == 409
    assert resp.get_json() == SYNC_IN_PROGRESS

    garmin.finish.set()
    first.join(WAIT)
    assert result["value"] == (200, {"ok": True, "days_written": 1})
    assert garmin.runs == 1


def test_a_scheduled_tick_skips_while_a_manual_sync_runs(client, garmin, background, capsys):
    manual, result = background(post_sync)
    assert garmin.started.wait(WAIT)

    main.scheduled_sync()

    assert garmin.runs == 1
    assert "[scheduler] sync already running, skipping" in capsys.readouterr().out

    garmin.finish.set()
    manual.join(WAIT)
    assert result["value"] == (200, {"ok": True, "days_written": 1})

    # With nothing running, the next tick syncs as usual.
    main.scheduled_sync()

    assert garmin.runs == 2
    assert "[scheduler] garmin wrote 1 day(s)" in capsys.readouterr().out


def test_a_plugins_tick_skips_while_a_manual_sync_runs(client, garmin, background, capsys):
    manual, result = background(post_sync)
    assert garmin.started.wait(WAIT)

    main.sync_one("garmin")

    assert garmin.runs == 1
    assert "[scheduler] sync already running, skipping garmin" in capsys.readouterr().out

    garmin.finish.set()
    manual.join(WAIT)
    assert result["value"] == (200, {"ok": True, "days_written": 1})

    # Its next tick, with nothing running, syncs.
    main.sync_one("garmin")

    assert garmin.runs == 2
    assert "[scheduler] garmin wrote 1 day(s)" in capsys.readouterr().out


def test_a_manual_sync_during_a_plugins_tick_gets_409(client, garmin, background):
    tick, _ = background(lambda: main.sync_one("garmin"))
    assert garmin.started.wait(WAIT)

    resp = client.post("/api/sync/garmin")

    assert resp.status_code == 409
    assert resp.get_json() == SYNC_IN_PROGRESS
    assert garmin.runs == 1

    garmin.finish.set()
    tick.join(WAIT)
    assert not main._sync_lock.locked()


def test_one_plugins_tick_skips_while_anothers_runs(client, garmin, background, monkeypatch, capsys):
    """Every source shares the one lock: two never sync side by side."""
    other = BlockingPlugin("google_health")
    other.finish.set()
    monkeypatch.setattr(main, "PLUGINS", {"garmin": garmin, "google_health": other})
    tick, _ = background(lambda: main.sync_one("garmin"))
    assert garmin.started.wait(WAIT)

    main.sync_one("google_health")

    assert other.runs == 0
    assert "skipping google_health" in capsys.readouterr().out

    garmin.finish.set()
    tick.join(WAIT)


def test_a_failed_sync_releases_the_lock(client, monkeypatch):
    monkeypatch.setattr(main, "PLUGINS", {"garmin": FailingPlugin("garmin")})

    resp = client.post("/api/sync/garmin")
    assert resp.status_code == 500
    assert not main._sync_lock.locked()

    main.scheduled_sync()
    assert not main._sync_lock.locked()

    main.sync_one("garmin")
    assert not main._sync_lock.locked()

    # Not a 409: the next sync runs, and fails on its own terms.
    assert client.post("/api/sync/garmin").status_code == 500
