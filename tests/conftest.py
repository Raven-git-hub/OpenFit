"""
Shared test fixtures.

Every test runs against a fresh temp SQLite file - never the real
/data/tracker.db - and nothing here touches the network: Garmin and
Google Health are never contacted, only the fake plugin in
test_plugin_contract.py writes rows.

The same goes for the encryption key: every test gets a throwaway one in
its tmp_path, so nothing can read or create /data/.secret_key.
"""

import os
import sqlite3
import sys

import pytest

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import secrets as openfit_secrets  # noqa: E402  (app/secrets.py, not the stdlib)
from migrations import run_migrations  # noqa: E402


@pytest.fixture(autouse=True)
def secret_key_file(tmp_path, monkeypatch):
    """Point credential encryption at a throwaway key for every test.

    Autouse and unconditional: without it a test that encrypts anything
    would read - or worse, create - the real /data/.secret_key.
    """
    path = tmp_path / ".secret_key"
    monkeypatch.delenv(openfit_secrets.KEY_ENV, raising=False)
    monkeypatch.setenv(openfit_secrets.KEY_PATH_ENV, str(path))
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
