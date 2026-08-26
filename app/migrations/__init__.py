"""
Minimal migration runner.

Replaces the ad hoc CREATE TABLE IF NOT EXISTS calls that used to live in
main.py. Deliberately tiny - no Alembic, no SQLAlchemy, no ORM. It is
about 100 lines of stdlib sqlite3 and that is the whole point: OpenFit
ships as a single small container and the schema story should not be
heavier than the app.

How it works
------------
Migrations are plain .sql files in this folder named NNN_name.sql, e.g.
001_baseline.sql. They are applied in filename (version number) order.
Applied versions are recorded in schema_migrations, so each migration
runs exactly once per database.

Each migration is wrapped in a single transaction together with the
INSERT that records its version, so a migration either fully applies and
is marked applied, or leaves the database untouched. Migration files must
therefore NOT contain their own BEGIN/COMMIT.

Usage
-----
From Python (main.py does this at startup):

    conn = sqlite3.connect(db_path)
    run_migrations(conn)

From the shell, e.g. against a copy of the production DB before deploy:

    python -m migrations /path/to/tracker.db
"""

import os
import re
import sqlite3

MIGRATIONS_DIR = os.path.dirname(os.path.abspath(__file__))

FILENAME_RE = re.compile(r"^(\d+)_[\w-]+\.sql$")

SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT
)
"""


def discover_migrations(migrations_dir=None):
    """Return [(version, name, path), ...] sorted by version.

    Files that don't match NNN_name.sql are ignored, so a README or a
    stray editor backup in this folder is harmless.
    """
    migrations_dir = migrations_dir or MIGRATIONS_DIR
    found = []
    for filename in os.listdir(migrations_dir):
        match = FILENAME_RE.match(filename)
        if not match:
            continue
        found.append((int(match.group(1)), filename, os.path.join(migrations_dir, filename)))

    found.sort(key=lambda item: item[0])

    versions = [v for v, _, _ in found]
    duplicates = {v for v in versions if versions.count(v) > 1}
    if duplicates:
        raise RuntimeError(f"duplicate migration version(s): {sorted(duplicates)}")

    return found


def applied_versions(conn):
    """Versions already applied to this database (empty set on a fresh DB)."""
    conn.execute(SCHEMA_MIGRATIONS_DDL)
    conn.commit()
    rows = conn.execute("SELECT version FROM schema_migrations").fetchall()
    return {row[0] for row in rows}


def _apply(conn, version, sql):
    """Apply one migration + its version record as a single transaction."""
    script = (
        "BEGIN;\n"
        f"{sql}\n"
        f"INSERT INTO schema_migrations (version, applied_at) "
        f"VALUES ({version}, datetime('now'));\n"
        "COMMIT;\n"
    )
    # Manual transaction control: we drive BEGIN/COMMIT from inside the
    # script so behaviour is identical across Python versions (3.11 and
    # earlier implicitly commit a pending transaction before running
    # executescript, 3.12+ does not).
    previous_isolation = conn.isolation_level
    conn.isolation_level = None
    try:
        conn.executescript(script)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.isolation_level = previous_isolation


def run_migrations(conn, migrations_dir=None, verbose=False):
    """Apply every pending migration in order. Returns versions applied now.

    Safe to call on every startup: already-applied migrations are skipped,
    so this is a no-op once the database is up to date.
    """
    already = applied_versions(conn)
    newly_applied = []

    for version, name, path in discover_migrations(migrations_dir):
        if version in already:
            continue
        with open(path) as f:
            sql = f.read()
        _apply(conn, version, sql)
        newly_applied.append(version)
        if verbose:
            print(f"[migrations] applied {name}")

    if verbose and not newly_applied:
        print("[migrations] database already up to date")

    return newly_applied


def current_version(conn):
    """Highest applied migration version, or 0 if none."""
    versions = applied_versions(conn)
    return max(versions) if versions else 0


def migrate_path(db_path, verbose=True):
    """Convenience wrapper: open db_path, migrate it, close."""
    conn = sqlite3.connect(db_path)
    try:
        return run_migrations(conn, verbose=verbose)
    finally:
        conn.close()
