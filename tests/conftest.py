"""
Shared test fixtures.

Every test runs against a fresh temp SQLite file - never the real
/data/tracker.db - and nothing here touches the network: Garmin and
Google Health are never contacted. Rows are written by the fake plugin
in test_plugin_contract.py, or by the real plugins with their network
calls stubbed (test_sleep_sessions.py).

The same goes for the encryption key: every test gets a throwaway one in
its tmp_path, so nothing can read or create /data/.secret_key.
"""

import os
import shutil
import sqlite3
import sys

import pytest

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import crypto as openfit_crypto  # noqa: E402
from migrations import discover_migrations, run_migrations  # noqa: E402


@pytest.fixture(autouse=True)
def secret_key_file(tmp_path, monkeypatch):
    """Point credential encryption at a throwaway key for every test.

    Autouse and unconditional: without it a test that encrypts anything
    would read - or worse, create - the real /data/.secret_key.
    """
    path = tmp_path / ".secret_key"
    monkeypatch.delenv(openfit_crypto.KEY_ENV, raising=False)
    monkeypatch.setenv(openfit_crypto.KEY_PATH_ENV, str(path))
    return path


@pytest.fixture
def db_path(tmp_path):
    """Path to an empty, fully migrated database."""
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    run_migrations(conn)
    conn.close()
    return path


@pytest.fixture
def db_at_version(tmp_path):
    """Factory: path to a database migrated up to `version` and no further.

    For testing a migration against the schema it really runs on - a
    production database sits at the previous version, holding data in
    the shape that version left it.
    """

    def make(version, name="at-version.db"):
        partial = tmp_path / f"migrations-upto-{version}"
        partial.mkdir()
        for v, filename, path in discover_migrations():
            if v <= version:
                shutil.copy(path, partial / filename)
        path = str(tmp_path / name)
        conn = sqlite3.connect(path)
        run_migrations(conn, migrations_dir=str(partial))
        conn.close()
        return path

    return make


@pytest.fixture
def conn(db_path):
    """Direct connection to the migrated test database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def client(db_path):
    """Flask test client pointed at the temp database.

    Importing main must not start the scheduler or create tables; if that
    regresses, these tests are the first thing to notice.
    """
    import main

    previous = main.app.config["DB_PATH"]
    main.app.config["DB_PATH"] = db_path
    main.app.config["TESTING"] = True
    with main.app.test_client() as client:
        yield client
    main.app.config["DB_PATH"] = previous
