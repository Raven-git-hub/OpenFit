"""
CLI entrypoint: python -m migrations [DB_PATH]

Run by hand against a copy of the production database before deploying,
so the migration can be verified before it touches real data:

    docker compose cp tracker:/data/tracker.db ./tracker-copy.db
    python -m migrations ./tracker-copy.db

The database runs in WAL mode: if /data/tracker.db-wal exists, recent
writes are still in it, so copy it too, as ./tracker-copy.db-wal.

With no argument it uses $DB_PATH, falling back to the container default
/data/tracker.db.
"""

import os
import sys

from . import migrate_path


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv

    if argv and argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0

    db_path = argv[0] if argv else os.getenv("DB_PATH", "/data/tracker.db")
    print(f"[migrations] database: {db_path}")

    try:
        applied = migrate_path(db_path)
    except Exception as e:
        print(f"[migrations] FAILED: {e}", file=sys.stderr)
        return 1

    if applied:
        print(f"[migrations] applied version(s): {', '.join(str(v) for v in applied)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
