"""
Migration runner: an old-shape database must adopt cleanly, keep its
rows, and re-running must be a no-op.
"""

import sqlite3

import pytest

from migrations import (
    current_version,
    discover_migrations,
    run_migrations,
)

def all_versions():
    """Every migration version on disk, in order.

    Derived rather than hardcoded so adding a migration doesn't require
    editing every assertion in this file.
    """
    return [v for v, _, _ in discover_migrations()]


OLD_ACTIVITY_DDL = """
CREATE TABLE activity (
    date TEXT PRIMARY KEY,
    steps INTEGER,
    resting_hr INTEGER,
    sleep_hours REAL,
    source TEXT,
    synced_at TEXT
)
"""


def old_shape_db(path, rows=()):
    """A database as the pre-migrations init_db() would have left it."""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE weights (date TEXT PRIMARY KEY, weight REAL)")
    conn.execute(
        "CREATE TABLE workouts (week INTEGER, idx INTEGER, done INTEGER, "
        "PRIMARY KEY (week, idx))"
    )
    conn.execute(OLD_ACTIVITY_DDL)
    conn.executemany(
        "INSERT INTO activity (date, steps, resting_hr, sleep_hours, source, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()


def table_sql(conn, name):
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row[0] if row else None


def column_names(conn, table):
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def primary_key_columns(conn, table):
    return [
        r[1] for r in
        sorted(conn.execute(f"PRAGMA table_info({table})").fetchall(), key=lambda r: r[5])
        if r[5]  # pk position, 0 means not part of the key
    ]


def test_migrations_are_discovered_in_order():
    versions = [v for v, _, _ in discover_migrations()]
    assert versions == sorted(versions)
    assert versions[:2] == [1, 2]
    assert len(versions) == len(set(versions))


def test_fresh_database_gets_all_tables(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "fresh.db"))
    applied = run_migrations(conn)

    assert applied == all_versions()
    names = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "weights", "workouts", "metrics", "settings", "accounts", "schema_migrations"
    } <= names
    assert "activity" not in names  # unpivoted into metrics by 005
    assert primary_key_columns(conn, "metrics") == ["date", "source", "metric"]
    conn.close()


def test_old_shape_database_adopts_and_keeps_rows(tmp_path):
    path = str(tmp_path / "old.db")
    old_shape_db(
        path,
        rows=[
            ("2026-01-01", 9000, 52, 7.5, "garmin", "2026-01-01T06:00:00"),
            ("2026-01-02", 8000, 61, 6.5, "google_health", "2026-01-02T06:00:00"),
        ],
    )

    conn = sqlite3.connect(path)
    assert run_migrations(conn) == all_versions()

    rows = conn.execute(
        "SELECT date, source, metric, value, unit, synced_at "
        "FROM metrics ORDER BY date, metric"
    ).fetchall()
    assert rows == [
        ("2026-01-01", "garmin", "resting_hr_bpm", 52, "bpm", "2026-01-01T06:00:00"),
        ("2026-01-01", "garmin", "sleep_minutes", 450, "min", "2026-01-01T06:00:00"),
        ("2026-01-01", "garmin", "steps", 9000, "count", "2026-01-01T06:00:00"),
        ("2026-01-02", "google_health", "resting_hr_bpm", 61, "bpm", "2026-01-02T06:00:00"),
        ("2026-01-02", "google_health", "sleep_minutes", 390, "min", "2026-01-02T06:00:00"),
        ("2026-01-02", "google_health", "steps", 8000, "count", "2026-01-02T06:00:00"),
    ]
    conn.close()


@pytest.mark.parametrize("source", [None, "", "   "])
def test_rows_without_a_source_become_unknown_not_dropped(tmp_path, source):
    path = str(tmp_path / "old.db")
    old_shape_db(path, rows=[("2026-01-01", 9000, None, None, source, None)])

    conn = sqlite3.connect(path)
    run_migrations(conn)

    assert conn.execute("SELECT date, source, metric FROM metrics").fetchall() == [
        ("2026-01-01", "unknown", "steps")
    ]
    conn.close()


def test_baseline_does_not_disturb_existing_data(tmp_path):
    """001 uses CREATE TABLE IF NOT EXISTS, so populated tables survive."""
    path = str(tmp_path / "old.db")
    old_shape_db(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO weights VALUES ('2026-01-01', 82.0)")
    conn.execute("INSERT INTO workouts VALUES (1, 0, 1)")
    conn.commit()

    run_migrations(conn)

    assert conn.execute("SELECT * FROM weights").fetchall() == [("2026-01-01", 82.0)]
    assert conn.execute("SELECT * FROM workouts").fetchall() == [(1, 0, 1)]
    conn.close()


def test_rerunning_is_a_no_op(tmp_path):
    path = str(tmp_path / "old.db")
    old_shape_db(path, rows=[("2026-01-01", 9000, 52, 7.5, "garmin", "x")])

    conn = sqlite3.connect(path)
    assert run_migrations(conn) == all_versions()

    schema_before = table_sql(conn, "metrics")
    rows_before = conn.execute("SELECT * FROM metrics").fetchall()
    stamps_before = conn.execute(
        "SELECT version, applied_at FROM schema_migrations ORDER BY version"
    ).fetchall()

    assert run_migrations(conn) == []
    assert run_migrations(conn) == []

    assert table_sql(conn, "metrics") == schema_before
    assert conn.execute("SELECT * FROM metrics").fetchall() == rows_before
    # No duplicate or rewritten version records.
    assert conn.execute(
        "SELECT version, applied_at FROM schema_migrations ORDER BY version"
    ).fetchall() == stamps_before
    conn.close()


def test_version_is_recorded(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "fresh.db"))
    assert current_version(conn) == 0
    run_migrations(conn)
    assert current_version(conn) == max(v for v, _, _ in discover_migrations())
    conn.close()


def test_partially_migrated_database_only_applies_the_rest(tmp_path):
    """A DB already at version 1 gets every later migration and nothing else."""
    path = str(tmp_path / "old.db")
    old_shape_db(path, rows=[("2026-01-01", 9000, 52, 7.5, "garmin", "x")])

    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT)"
    )
    conn.execute("INSERT INTO schema_migrations VALUES (1, 'earlier')")
    conn.commit()

    assert run_migrations(conn) == all_versions()[1:]
    assert primary_key_columns(conn, "metrics") == ["date", "source", "metric"]
    conn.close()


def test_failed_migration_rolls_back_and_is_not_recorded(tmp_path):
    """A migration and its version record land together, or not at all."""
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "001_ok.sql").write_text("CREATE TABLE ok (id INTEGER);")
    (migrations_dir / "002_broken.sql").write_text(
        "CREATE TABLE half (id INTEGER);\nTHIS IS NOT SQL;"
    )

    conn = sqlite3.connect(str(tmp_path / "broken.db"))
    with pytest.raises(sqlite3.Error):
        run_migrations(conn, migrations_dir=str(migrations_dir))

    names = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert "ok" in names          # 001 committed
    assert "half" not in names    # 002 rolled back entirely
    assert current_version(conn) == 1
    conn.close()


def test_non_migration_files_are_ignored(tmp_path):
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "001_ok.sql").write_text("CREATE TABLE ok (id INTEGER);")
    (migrations_dir / "README.md").write_text("not a migration")
    (migrations_dir / "001_ok.sql.bak").write_text("DROP TABLE ok;")

    conn = sqlite3.connect(str(tmp_path / "db.db"))
    assert run_migrations(conn, migrations_dir=str(migrations_dir)) == [1]
    conn.close()


def test_settings_table_is_added_without_disturbing_existing_data(tmp_path):
    """003 must be safe against a populated production database."""
    path = str(tmp_path / "populated.db")
    old_shape_db(path, rows=[("2026-01-01", 9000, 52, 7.5, "garmin", "x")])
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO weights VALUES ('2026-01-01', 82.0)")
    conn.execute("INSERT INTO workouts VALUES (1, 0, 1)")
    conn.commit()

    run_migrations(conn)

    # The new table exists and starts empty...
    assert conn.execute("SELECT COUNT(*) FROM settings").fetchone()[0] == 0
    assert primary_key_columns(conn, "settings") == ["key"]
    # ...and nothing that was already there was touched.
    assert conn.execute("SELECT * FROM weights").fetchall() == [("2026-01-01", 82.0)]
    assert conn.execute("SELECT * FROM workouts").fetchall() == [(1, 0, 1)]
    assert conn.execute(
        "SELECT date, value FROM metrics WHERE metric = 'steps'"
    ).fetchall() == [("2026-01-01", 9000)]
    conn.close()


def test_settings_migration_adopts_a_preexisting_table(tmp_path):
    """CREATE TABLE IF NOT EXISTS: an existing settings table survives."""
    path = str(tmp_path / "has-settings.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT INTO settings VALUES ('home_tiles', '{}')")
    conn.commit()

    run_migrations(conn)

    assert conn.execute("SELECT key, value FROM settings").fetchall() == [
        ("home_tiles", "{}")
    ]
    conn.close()


def test_accounts_table_is_added_without_disturbing_existing_data(tmp_path):
    """004 must be safe against a populated production database."""
    path = str(tmp_path / "populated.db")
    old_shape_db(path, rows=[("2026-01-01", 9000, 52, 7.5, "garmin", "x")])
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO weights VALUES ('2026-01-01', 82.0)")
    conn.execute("INSERT INTO workouts VALUES (1, 0, 1)")
    conn.commit()

    run_migrations(conn)

    # The new table exists and starts empty...
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    assert primary_key_columns(conn, "accounts") == ["plugin_id"]
    assert column_names(conn, "accounts") == ["plugin_id", "credentials", "created_at"]
    # ...and nothing that was already there was touched.
    assert conn.execute("SELECT * FROM weights").fetchall() == [("2026-01-01", 82.0)]
    assert conn.execute("SELECT * FROM workouts").fetchall() == [(1, 0, 1)]
    assert conn.execute(
        "SELECT date, value FROM metrics WHERE metric = 'steps'"
    ).fetchall() == [("2026-01-01", 9000)]
    conn.close()


def test_accounts_migration_adopts_a_preexisting_table(tmp_path):
    """CREATE TABLE IF NOT EXISTS: an existing accounts table survives."""
    path = str(tmp_path / "has-accounts.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE accounts (plugin_id TEXT PRIMARY KEY, credentials TEXT, created_at TEXT)"
    )
    conn.execute("INSERT INTO accounts VALUES ('garmin', 'already-encrypted', '2026-01-01')")
    conn.commit()

    run_migrations(conn)

    assert conn.execute("SELECT plugin_id, credentials FROM accounts").fetchall() == [
        ("garmin", "already-encrypted")
    ]
    conn.close()


def test_one_account_per_plugin(tmp_path):
    """plugin_id is the primary key - re-adding a device replaces the row."""
    conn = sqlite3.connect(str(tmp_path / "fresh.db"))
    run_migrations(conn)

    conn.execute("INSERT INTO accounts VALUES ('garmin', 'blob-1', 'then')")
    conn.execute(
        "INSERT INTO accounts VALUES ('garmin', 'blob-2', 'now') "
        "ON CONFLICT(plugin_id) DO UPDATE SET credentials=excluded.credentials"
    )

    assert conn.execute("SELECT credentials, created_at FROM accounts").fetchall() == [
        ("blob-2", "then")
    ]
    conn.close()


# ---------- 005: activity -> metrics ----------


def activity_at_version_4(db_at_version, rows):
    """A version-4 database - where production sits before 005 - holding rows."""
    path = db_at_version(4)
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO activity (date, steps, resting_hr, sleep_hours, source, synced_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    return conn


def test_metrics_migration_unpivots_activity(db_at_version):
    """005 turns each wide activity row into one row per non-null metric."""
    conn = activity_at_version_4(db_at_version, [
        # Two sources on one day, as 002 allows.
        ("2026-01-01", 9000, 52, 7.5, "garmin", "2026-01-01 06:00:00"),
        ("2026-01-01", 8000, None, 6.2, "google_health", "2026-01-01 07:00:00"),
        # Nothing but NULLs: no rows at all, rather than empty ones.
        ("2026-01-02", None, None, None, "garmin", "2026-01-02 06:00:00"),
        # One metric, no synced_at, the 'unknown' source 002 assigns.
        ("2026-01-03", None, 61, None, "unknown", None),
        # Hours with float noise from summing sessions still land on a
        # whole number of minutes.
        ("2026-01-04", None, None, 6.6000000000000005, "google_health", "x"),
    ])

    assert run_migrations(conn) == [5]

    rows = conn.execute(
        "SELECT date, source, metric, value, unit, synced_at "
        "FROM metrics ORDER BY date, source, metric"
    ).fetchall()
    assert rows == [
        ("2026-01-01", "garmin", "resting_hr_bpm", 52, "bpm", "2026-01-01 06:00:00"),
        ("2026-01-01", "garmin", "sleep_minutes", 450, "min", "2026-01-01 06:00:00"),
        ("2026-01-01", "garmin", "steps", 9000, "count", "2026-01-01 06:00:00"),
        ("2026-01-01", "google_health", "sleep_minutes", 372, "min", "2026-01-01 07:00:00"),
        ("2026-01-01", "google_health", "steps", 8000, "count", "2026-01-01 07:00:00"),
        ("2026-01-03", "unknown", "resting_hr_bpm", 61, "bpm", None),
        ("2026-01-04", "google_health", "sleep_minutes", 396, "min", "x"),
    ]
    # The minutes are exact, not 371.99999...
    assert all(
        value == int(value)
        for (value,) in conn.execute("SELECT value FROM metrics WHERE metric = 'sleep_minutes'")
    )
    conn.close()


def test_metrics_migration_drops_activity_and_indexes_by_date(db_at_version):
    conn = activity_at_version_4(db_at_version, [
        ("2026-01-01", 9000, 52, 7.5, "garmin", "x"),
    ])

    run_migrations(conn)

    assert table_sql(conn, "activity") is None
    indexes = {
        r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='metrics'"
        )
    }
    assert "idx_metrics_date" in indexes
    assert column_names(conn, "metrics") == [
        "date", "source", "metric", "value", "unit", "synced_at"
    ]
    assert primary_key_columns(conn, "metrics") == ["date", "source", "metric"]
    conn.close()


def test_metrics_migration_is_idempotent(db_at_version):
    """Re-running after 005 changes nothing - and can't re-read activity."""
    conn = activity_at_version_4(db_at_version, [
        ("2026-01-01", 9000, 52, 7.5, "garmin", "x"),
        ("2026-01-01", 8000, None, None, "google_health", "y"),
    ])
    assert run_migrations(conn) == [5]
    rows_before = conn.execute("SELECT * FROM metrics ORDER BY date, source, metric").fetchall()

    assert run_migrations(conn) == []
    assert run_migrations(conn) == []

    assert conn.execute(
        "SELECT * FROM metrics ORDER BY date, source, metric"
    ).fetchall() == rows_before
    assert len(rows_before) == 4
    assert conn.execute(
        "SELECT COUNT(*) FROM schema_migrations WHERE version = 5"
    ).fetchone()[0] == 1
    conn.close()


def test_metrics_migration_leaves_other_tables_alone(db_at_version):
    """005 rewrites activity only - weights, workouts and the rest survive."""
    conn = activity_at_version_4(db_at_version, [
        ("2026-01-01", 9000, None, None, "garmin", "x"),
    ])
    conn.execute("INSERT INTO weights VALUES ('2026-01-01', 82.0)")
    conn.execute("INSERT INTO workouts VALUES (1, 0, 1)")
    conn.execute("INSERT INTO settings VALUES ('home_tiles', '{}')")
    conn.execute("INSERT INTO accounts VALUES ('garmin', 'blob', 'then')")
    conn.commit()

    run_migrations(conn)

    assert conn.execute("SELECT * FROM weights").fetchall() == [("2026-01-01", 82.0)]
    assert conn.execute("SELECT * FROM workouts").fetchall() == [(1, 0, 1)]
    assert conn.execute("SELECT * FROM settings").fetchall() == [("home_tiles", "{}")]
    assert conn.execute("SELECT * FROM accounts").fetchall() == [("garmin", "blob", "then")]
    conn.close()


def test_a_metric_reading_cannot_be_null(conn):
    """No empty metric rows: a missing reading is no row at all."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO metrics (date, source, metric, value, unit) "
            "VALUES ('2026-01-01', 'garmin', 'steps', NULL, 'count')"
        )
