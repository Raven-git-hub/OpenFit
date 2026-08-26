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

    assert applied == [1, 2]
    names = {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {"weights", "workouts", "activity", "schema_migrations"} <= names
    assert primary_key_columns(conn, "activity") == ["date", "source"]
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
    assert run_migrations(conn) == [1, 2]

    assert primary_key_columns(conn, "activity") == ["date", "source"]
    rows = conn.execute(
        "SELECT date, steps, resting_hr, sleep_hours, source, synced_at "
        "FROM activity ORDER BY date"
    ).fetchall()
    assert rows == [
        ("2026-01-01", 9000, 52, 7.5, "garmin", "2026-01-01T06:00:00"),
        ("2026-01-02", 8000, 61, 6.5, "google_health", "2026-01-02T06:00:00"),
    ]
    conn.close()


@pytest.mark.parametrize("source", [None, "", "   "])
def test_rows_without_a_source_become_unknown_not_dropped(tmp_path, source):
    path = str(tmp_path / "old.db")
    old_shape_db(path, rows=[("2026-01-01", 9000, None, None, source, None)])

    conn = sqlite3.connect(path)
    run_migrations(conn)

    assert conn.execute("SELECT date, source FROM activity").fetchall() == [
        ("2026-01-01", "unknown")
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
    assert run_migrations(conn) == [1, 2]

    schema_before = table_sql(conn, "activity")
    rows_before = conn.execute("SELECT * FROM activity").fetchall()
    stamps_before = conn.execute(
        "SELECT version, applied_at FROM schema_migrations ORDER BY version"
    ).fetchall()

    assert run_migrations(conn) == []
    assert run_migrations(conn) == []

    assert table_sql(conn, "activity") == schema_before
    assert conn.execute("SELECT * FROM activity").fetchall() == rows_before
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
    """A DB already at version 1 gets 002 and nothing else."""
    path = str(tmp_path / "old.db")
    old_shape_db(path, rows=[("2026-01-01", 9000, 52, 7.5, "garmin", "x")])

    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT)"
    )
    conn.execute("INSERT INTO schema_migrations VALUES (1, 'earlier')")
    conn.commit()

    assert run_migrations(conn) == [2]
    assert primary_key_columns(conn, "activity") == ["date", "source"]
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
