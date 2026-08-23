import os
import sqlite3
from datetime import date

from flask import Flask, jsonify, request, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler

from plugins import PLUGINS

DB_PATH = os.getenv("DB_PATH", "/data/tracker.db")
SYNC_INTERVAL_HOURS = int(os.getenv("SYNC_INTERVAL_HOURS", "6"))

app = Flask(__name__, static_folder=None)


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_conn()
    conn.execute(
        "CREATE TABLE IF NOT EXISTS weights (date TEXT PRIMARY KEY, weight REAL)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS workouts (week INTEGER, idx INTEGER, done INTEGER, PRIMARY KEY (week, idx))"
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS activity (
            date TEXT PRIMARY KEY,
            steps INTEGER,
            resting_hr INTEGER,
            sleep_hours REAL,
            source TEXT,
            synced_at TEXT
        )
        """
    )
    conn.commit()
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

@app.route("/api/activity", methods=["GET"])
def get_activity():
    days = int(request.args.get("days", 30))
    conn = get_conn()
    rows = conn.execute(
        "SELECT date, steps, resting_hr, sleep_hours, source FROM activity "
        "ORDER BY date DESC LIMIT ?",
        (days,),
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


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


init_db()

scheduler = BackgroundScheduler()
scheduler.add_job(scheduled_sync, "date")  # run once shortly after startup
scheduler.add_job(scheduled_sync, "interval", hours=SYNC_INTERVAL_HOURS)
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=80)
