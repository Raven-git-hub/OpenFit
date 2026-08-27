import json
import os
import sqlite3
from datetime import date, datetime, timezone

from flask import Flask, jsonify, request, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler

from migrations import run_migrations
from plugins import PLUGINS
from crypto import encrypt

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


# ---------- settings ----------

# A tiny key/value store for UI preferences that belong to the install
# rather than to one browser - the home tile layout is the first user.
# Per-browser choices (theme, display unit) deliberately stay in
# localStorage and never come near this table.
#
# The value is an opaque JSON string: the API stores and returns exactly
# what the client PUT, and the client decides what it means. That keeps
# a new preference from needing a migration or a route of its own.


@app.route("/api/settings/<key>", methods=["GET"])
def get_setting(key):
    conn = get_conn()
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    conn.close()
    # An unset key is not an error - it means "no preference saved", and
    # the caller falls back to its own default. 404 would force every
    # caller to special-case a perfectly normal first run.
    return jsonify({"key": key, "value": row["value"] if row else None})


@app.route("/api/settings/<key>", methods=["PUT"])
def set_setting(key):
    body = request.get_json(force=True, silent=True) or {}
    value = body.get("value")
    if not isinstance(value, str):
        return jsonify({"error": "value must be a JSON string"}), 400
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


# ---------- connectors ----------

# A "connector" is a plugin plus whatever account has been attached to it
# through the UI. The manifest (fields / add_flow) lives on the plugin, so
# adding a device to OpenFit stays a one-file job: declare the fields and
# the form, validation and storage all follow.
#
# Credentials are encrypted before they touch the database - see
# app/crypto.py. They are never read back out over the API: the UI shows
# that a device is connected, not what it was connected with.


@app.route("/api/connectors", methods=["GET"])
def list_connectors():
    conn = get_conn()
    connected = {
        row["plugin_id"] for row in conn.execute("SELECT plugin_id FROM accounts")
    }
    out = [
        {
            "id": p.id,
            "name": p.name,
            "fields": p.fields,
            "add_flow": p.add_flow,
            "connected": p.id in connected,
            # Whether it could actually sync right now - true for a
            # pre-UI install whose credentials are still in the env even
            # though no account row exists yet.
            "configured": p.status(conn)["configured"],
        }
        for p in PLUGINS.values()
    ]
    conn.close()
    return jsonify(out)


@app.route("/api/connectors/<plugin_id>", methods=["POST"])
def connect_connector(plugin_id):
    """Attach an account to a plugin from the values its manifest asks for."""
    plugin = PLUGINS.get(plugin_id)
    if not plugin:
        return jsonify({"ok": False, "error": f"no such connector: {plugin_id}"}), 404

    body = request.get_json(force=True, silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "expected a JSON object"}), 400

    # Only manifest fields are stored - anything else in the body is
    # ignored rather than quietly encrypted and kept forever.
    values = {}
    for field in plugin.fields:
        raw = body.get(field["key"])
        if raw is None:
            continue
        value = raw.strip() if isinstance(raw, str) else str(raw)
        if value:
            values[field["key"]] = value

    missing = plugin.missing_fields(values)
    if missing:
        names = ", ".join(f.get("label") or f["key"] for f in missing)
        return jsonify({"ok": False, "error": f"{names} required"}), 400

    conn = get_conn()
    conn.execute(
        "INSERT INTO accounts (plugin_id, credentials, created_at) VALUES (?, ?, ?) "
        # Re-adding a device replaces the credentials but keeps the
        # original created_at - it's the same connection, re-authorised.
        "ON CONFLICT(plugin_id) DO UPDATE SET credentials=excluded.credentials",
        (plugin_id, encrypt(json.dumps(values)), _now()),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/connectors/<plugin_id>", methods=["DELETE"])
def disconnect_connector(plugin_id):
    """Remove the account AND any token the plugin cached on disk."""
    plugin = PLUGINS.get(plugin_id)
    if not plugin:
        return jsonify({"ok": False, "error": f"no such connector: {plugin_id}"}), 404

    conn = get_conn()
    conn.execute("DELETE FROM accounts WHERE plugin_id = ?", (plugin_id,))
    conn.commit()
    conn.close()

    # A leftover session token would mean the device still syncs after
    # you removed it, so this is part of the delete, not a nicety.
    plugin.clear_cached_auth()
    return jsonify({"ok": True})


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- plugins ----------

@app.route("/api/plugins", methods=["GET"])
def list_plugins():
    conn = get_conn()
    out = [
        {"id": p.id, "name": p.name, **p.status(conn)}
        for p in PLUGINS.values()
    ]
    conn.close()
    return jsonify(out)


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
        try:
            conn = get_conn()
            try:
                # Skip anything with no account and no env credentials -
                # it would only fail with a "not connected" error.
                if not plugin.status(conn)["configured"]:
                    continue
                n = plugin.sync(conn, 7)
            finally:
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
