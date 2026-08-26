import os
import sqlite3
from datetime import date

from flask import Flask, jsonify, request, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler

from migrations import run_migrations
from plugins import PLUGINS

DB_PATH = os.getenv("DB_PATH", "/data/tracker.db")
SYNC_INTERVAL_HOURS = int(os.getenv("SYNC_INTERVAL_HOURS", "6"))

# Order of trust when two sources report the same metric for the same day.
# Earlier wins. Sources not listed here (e.g. 'unknown' rows migrated from
# before the source column was populated) rank last, ordered by name so the
# result is deterministic.
SOURCE_PRIORITY = ["garmin", "google_health"]

app = Flask(__name__, static_folder=None)

# The database path lives in app.config so tests can point the app at a
# temp file. It still defaults to $DB_PATH, so nothing changes in the
# container.
app.config["DB_PATH"] = DB_PATH


def get_conn():
    conn = sqlite3.connect(app.config["DB_PATH"])
    conn.row_factory = sqlite3.Row
    return conn


def migrate():
    """Bring the database up to the latest schema version."""
    conn = get_conn()
    try:
        return run_migrations(conn, verbose=True)
    finally:
        conn.close()


# ---------- static ----------

@app.route("/")
def index():
    return send_from_directory("templates", "index.html")


# ---------- weights ----------

@app.route("/api/weights", methods=["GET"])
def get_weights():
    conn = get_conn()
    rows = conn.execute("SELECT date, weight FROM weights ORDER BY date").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/weights", methods=["POST"])
def add_weight():
    body = request.get_json(force=True)
    d = body.get("date") or date.today().isoformat()
    w = body.get("weight")
    if w is None:
        return jsonify({"error": "weight required"}), 400
    conn = get_conn()
    conn.execute(
        "INSERT INTO weights (date, weight) VALUES (?, ?) "
        "ON CONFLICT(date) DO UPDATE SET weight=excluded.weight",
        (d, w),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/weights", methods=["DELETE"])
def clear_weights():
    conn = get_conn()
    conn.execute("DELETE FROM weights")
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---------- workouts ----------

@app.route("/api/workouts", methods=["GET"])
def get_workouts():
    conn = get_conn()
    rows = conn.execute("SELECT week, idx, done FROM workouts").fetchall()
    conn.close()
    out = {}
    for r in rows:
        out.setdefault(str(r["week"]), {})[str(r["idx"])] = bool(r["done"])
    return jsonify(out)


@app.route("/api/workouts", methods=["POST"])
def set_workout():
    body = request.get_json(force=True)
    week, idx, done = body.get("week"), body.get("idx"), body.get("done")
    if week is None or idx is None:
        return jsonify({"error": "week and idx required"}), 400
    conn = get_conn()
    conn.execute(
        "INSERT INTO workouts (week, idx, done) VALUES (?, ?, ?) "
        "ON CONFLICT(week, idx) DO UPDATE SET done=excluded.done",
        (week, idx, int(bool(done))),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---------- activity ----------

ACTIVITY_METRICS = ("steps", "resting_hr", "sleep_hours")


def _source_rank(source):
    """Sort key for a source: priority order first, then name."""
    try:
        return (SOURCE_PRIORITY.index(source), "")
    except ValueError:
        return (len(SOURCE_PRIORITY), source or "")


def _merge_by_date(rows):
    """Collapse per-source rows into one flat row per date.

    Each metric is taken from the highest-priority source that actually
    reported a value for that day, so a day with steps from Garmin and
    sleep from Google Health keeps both. `source` names the
    highest-priority source that contributed anything to the merged row -
    per-metric provenance is available via ?by_source=1.
    """
    by_date = {}
    for row in rows:
        by_date.setdefault(row["date"], []).append(row)

    merged = []
    for d in sorted(by_date, reverse=True):
        candidates = sorted(by_date[d], key=lambda r: _source_rank(r["source"]))
        out = {"date": d}
        for metric in ACTIVITY_METRICS:
            out[metric] = next(
                (c[metric] for c in candidates if c[metric] is not None), None
            )
        # `source` is the best-ranked source that contributed any value,
        # falling back to the best-ranked row for an all-empty day.
        out["source"] = next(
            (
                c["source"]
                for c in candidates
                if any(c[m] is not None for m in ACTIVITY_METRICS)
            ),
            candidates[0]["source"],
        )
        merged.append(out)
    return merged


@app.route("/api/activity", methods=["GET"])
def get_activity():
    days = int(request.args.get("days", 30))
    by_source = request.args.get("by_source", "").lower() in ("1", "true", "yes")

    conn = get_conn()
    # LIMIT applies to distinct dates, not rows: with several sources per
    # day, limiting rows would silently return fewer days than asked for.
    rows = conn.execute(
        "SELECT date, steps, resting_hr, sleep_hours, source, synced_at FROM activity "
        "WHERE date IN (SELECT DISTINCT date FROM activity ORDER BY date DESC LIMIT ?) "
        "ORDER BY date DESC, source",
        (days,),
    ).fetchall()
    conn.close()

    if by_source:
        # Additive: raw per-source rows, for anything that wants to see
        # which device said what.
        return jsonify([dict(r) for r in rows])

    # Default stays the flat one-row-per-date shape the frontend expects.
    return jsonify(_merge_by_date(rows))


# ---------- plugins ----------

@app.route("/api/plugins", methods=["GET"])
def list_plugins():
    return jsonify(
        [
            {"id": p.id, "name": p.name, **p.status()}
            for p in PLUGINS.values()
        ]
    )


@app.route("/api/sync/<plugin_id>", methods=["POST"])
def trigger_sync(plugin_id):
    plugin = PLUGINS.get(plugin_id)
    if not plugin:
        return jsonify({"ok": False, "error": f"no such plugin: {plugin_id}"}), 404
    try:
        conn = get_conn()
        n = plugin.sync(conn, int(request.args.get("days", 7)))
        conn.close()
        return jsonify({"ok": True, "days_written": n})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def scheduled_sync():
    for plugin in PLUGINS.values():
        if not plugin.status()["configured"]:
            continue
        try:
            conn = get_conn()
            n = plugin.sync(conn, 7)
            conn.close()
            print(f"[scheduler] {plugin.id} wrote {n} day(s)")
        except Exception as e:
            print(f"[scheduler] {plugin.id} failed: {e}")


def start_scheduler():
    """Start the background sync jobs.

    Called from serve() only - importing main.py (as the tests do) must
    not start a scheduler or fire off syncs.
    """
    scheduler = BackgroundScheduler()
    scheduler.add_job(scheduled_sync, "date")  # run once shortly after startup
    scheduler.add_job(scheduled_sync, "interval", hours=SYNC_INTERVAL_HOURS)
    scheduler.start()
    return scheduler


def serve():
    migrate()
    start_scheduler()
    app.run(host="0.0.0.0", port=80)


if __name__ == "__main__":
    serve()
