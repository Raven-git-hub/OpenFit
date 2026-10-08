import base64
import hashlib
import json
import os
import secrets
import sqlite3
import time
import urllib.parse
from datetime import date, datetime, timezone

from flask import Flask, jsonify, request, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler

from migrations import run_migrations
from plugins import PLUGINS
from plugins.base import OAUTH_REDIRECT_URI
from crypto import encrypt
from metrics import RESTING_HR_BPM, SLEEP_MINUTES, STEPS

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

# /api/activity predates the metrics table and keeps its flat shape for
# the interim UI: one field per metric, in the old field names and units.
# Each field maps to the canonical metric it is now read from, and how to
# turn the stored value back into what the field used to hold.


def _whole(value):
    """A stored REAL as the old INTEGER column returned it.

    SQLite's INTEGER affinity kept a whole number as an int and anything
    else as it was, so 9000.0 reads as 9000 again, as it did before.
    """
    return int(value) if float(value).is_integer() else value


ACTIVITY_FIELDS = {
    # field: (canonical metric, stored value -> field value)
    "steps": (STEPS, _whole),
    "resting_hr": (RESTING_HR_BPM, _whole),
    "sleep_hours": (SLEEP_MINUTES, lambda minutes: round(minutes / 60, 1)),
}

ACTIVITY_METRICS = tuple(ACTIVITY_FIELDS)

# Canonical metric -> (field, convert), for pivoting readings back.
_ACTIVITY_BY_METRIC = {
    metric: (field, convert) for field, (metric, convert) in ACTIVITY_FIELDS.items()
}


def _source_rank(source):
    """Sort key for a source: priority order first, then name."""
    try:
        return (SOURCE_PRIORITY.index(source), "")
    except ValueError:
        return (len(SOURCE_PRIORITY), source or "")


def _pivot_by_source(readings):
    """Fold tidy metric readings back into one wide row per (date, source).

    The rows come out in the order the readings arrive in, with the old
    activity columns: a metric the source didn't report is None, and
    synced_at is the most recent write to any of the row's metrics -
    what the old row's synced_at, bumped by every upsert, would hold.
    """
    wide = {}
    for reading in readings:
        key = (reading["date"], reading["source"])
        row = wide.get(key)
        if row is None:
            row = wide[key] = {
                "date": reading["date"],
                **{field: None for field in ACTIVITY_FIELDS},
                "source": reading["source"],
                "synced_at": None,
            }
        field, convert = _ACTIVITY_BY_METRIC[reading["metric"]]
        row[field] = convert(reading["value"])
        stamp = reading["synced_at"]
        if stamp is not None and (row["synced_at"] is None or stamp > row["synced_at"]):
            row["synced_at"] = stamp
    return list(wide.values())


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

    # Only the metrics this endpoint has fields for: a date that holds
    # nothing but some other metric is not an activity day, and must not
    # use up one of the `days` asked for.
    keys = [metric for metric, _ in ACTIVITY_FIELDS.values()]
    marks = ", ".join("?" * len(keys))

    conn = get_conn()
    # LIMIT applies to distinct dates, not rows: with several sources and
    # metrics per day, limiting rows would silently return fewer days
    # than asked for.
    readings = conn.execute(
        "SELECT date, source, metric, value, synced_at FROM metrics "
        f"WHERE metric IN ({marks}) AND date IN ("
        f"SELECT DISTINCT date FROM metrics WHERE metric IN ({marks}) "
        "ORDER BY date DESC LIMIT ?"
        ") "
        "ORDER BY date DESC, source",
        (*keys, *keys, days),
    ).fetchall()
    conn.close()

    rows = _pivot_by_source(readings)

    if by_source:
        # Additive: raw per-source rows, for anything that wants to see
        # which device said what.
        return jsonify(rows)

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
            "add_note": p.add_note,
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


def _manifest_values(plugin, body):
    """The manifest fields present in a request body, trimmed.

    Only manifest fields are kept - anything else in the body is ignored
    rather than quietly encrypted and kept forever.
    """
    values = {}
    for field in plugin.fields:
        raw = body.get(field["key"])
        if raw is None:
            continue
        value = raw.strip() if isinstance(raw, str) else str(raw)
        if value:
            values[field["key"]] = value
    return values


def _store_account(plugin_id, credentials):
    """Encrypt and upsert one plugin's credentials blob."""
    conn = get_conn()
    conn.execute(
        "INSERT INTO accounts (plugin_id, credentials, created_at) VALUES (?, ?, ?) "
        # Re-adding a device replaces the credentials but keeps the
        # original created_at - it's the same connection, re-authorised.
        "ON CONFLICT(plugin_id) DO UPDATE SET credentials=excluded.credentials",
        (plugin_id, encrypt(json.dumps(credentials)), _now()),
    )
    conn.commit()
    conn.close()


@app.route("/api/connectors/<plugin_id>", methods=["POST"])
def connect_connector(plugin_id):
    """Attach an account to a plugin from the values its manifest asks for."""
    plugin = PLUGINS.get(plugin_id)
    if not plugin:
        return jsonify({"ok": False, "error": f"no such connector: {plugin_id}"}), 404

    body = request.get_json(force=True, silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "expected a JSON object"}), 400

    if plugin.add_flow == "oauth":
        # An oauth connector needs the consent round trip; storing the
        # client details alone would look connected and never sync.
        return jsonify({
            "ok": False,
            "error": (
                f"{plugin.name} is added by authorization - "
                f"use /api/connectors/{plugin_id}/oauth/start then .../oauth/finish"
            ),
        }), 400

    values = _manifest_values(plugin, body)
    missing = plugin.missing_fields(values)
    if missing:
        names = ", ".join(f.get("label") or f["key"] for f in missing)
        return jsonify({"ok": False, "error": f"{names} required"}), 400

    _store_account(plugin_id, values)
    return jsonify({"ok": True})


# ---------- connectors: the oauth add flow ----------

# Adding an OAuth device is two calls, because there is nowhere for the
# provider to redirect back to: OpenFit is self-hosted, on whatever
# address this container happens to have, with no domain and no hosted
# callback. So consent lands on a loopback URL the app doesn't serve
# (OAUTH_REDIRECT_URI - see plugins/base.py), the browser shows a
# "can't connect" page with ?code=... in the address bar, and the human
# pastes that back. No inbound callback, nothing registered with the
# provider beyond a desktop-app client.
#
# Between the two calls sits the PKCE verifier, which must not go near
# the browser. It waits here, in memory, keyed by plugin:

OAUTH_PENDING_TTL_SECONDS = 600

# {plugin_id: {"code_verifier": str, "credentials": dict, "expires_at": float}}
#
# Deliberately not a table: this is a few minutes of half-finished login,
# not state worth persisting. A restart mid-flow just means starting the
# add over, and nothing sensitive outlives the process.
_oauth_pending = {}


def _pkce_pair():
    """A PKCE verifier and its S256 challenge, per RFC 7636."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(40)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


def _oauth_plugin(plugin_id):
    """(plugin, error_response) for an oauth-flow plugin id."""
    plugin = PLUGINS.get(plugin_id)
    if not plugin:
        return None, (jsonify({"ok": False, "error": f"no such connector: {plugin_id}"}), 404)
    if plugin.add_flow != "oauth":
        return None, (jsonify({
            "ok": False,
            "error": f"{plugin.name} is not an OAuth connector - POST /api/connectors/{plugin_id}",
        }), 400)
    return plugin, None


def _take_pending(plugin_id):
    """Pop this plugin's pending authorization, if it hasn't expired."""
    now = time.monotonic()
    for key, pending in list(_oauth_pending.items()):
        if pending["expires_at"] <= now:
            del _oauth_pending[key]
    return _oauth_pending.pop(plugin_id, None)


def _authorization_code(raw):
    """The code out of whatever got pasted in.

    Accepts the bare code, the full redirect URL the browser failed to
    load, or just its query string - people paste all three, and telling
    them off for it would be a strange way to end a login.
    """
    text = (raw or "").strip()
    if not text:
        return None
    if "code=" in text:
        query = urllib.parse.urlsplit(text).query or text.split("?", 1)[-1]
        found = urllib.parse.parse_qs(query).get("code")
        return found[0] if found else None
    if "://" in text or "?" in text:
        # A URL with no code in it - a denied consent, most likely.
        return None
    return text


@app.route("/api/connectors/<plugin_id>/oauth/start", methods=["POST"])
def start_oauth(plugin_id):
    """Step one: take the client details, hand back a consent URL."""
    plugin, error = _oauth_plugin(plugin_id)
    if error:
        return error

    body = request.get_json(force=True, silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "expected a JSON object"}), 400

    values = _manifest_values(plugin, body)
    missing = plugin.missing_fields(values)
    if missing:
        names = ", ".join(f.get("label") or f["key"] for f in missing)
        return jsonify({"ok": False, "error": f"{names} required"}), 400

    verifier, challenge = _pkce_pair()
    try:
        auth_url = plugin.oauth_auth_url(values, OAUTH_REDIRECT_URI, challenge)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

    # Nothing is written to the database yet: an abandoned consent screen
    # must not leave a half-connected device behind.
    _oauth_pending[plugin_id] = {
        "code_verifier": verifier,
        "credentials": values,
        "expires_at": time.monotonic() + OAUTH_PENDING_TTL_SECONDS,
    }
    return jsonify({"ok": True, "auth_url": auth_url})


@app.route("/api/connectors/<plugin_id>/oauth/finish", methods=["POST"])
def finish_oauth(plugin_id):
    """Step two: trade the pasted code for the credentials to store."""
    plugin, error = _oauth_plugin(plugin_id)
    if error:
        return error

    body = request.get_json(force=True, silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "expected a JSON object"}), 400

    code = _authorization_code(body.get("code"))
    if not code:
        return jsonify({
            "ok": False,
            "error": "No authorization code found - paste the address the consent "
                     "screen sent you to, or the code itself",
        }), 400

    pending = _take_pending(plugin_id)
    if not pending:
        return jsonify({
            "ok": False,
            "error": "This authorization expired or was never started - "
                     "get a new authorization link and try again",
        }), 400

    try:
        credentials = plugin.oauth_exchange(
            pending["credentials"], code, pending["code_verifier"], OAUTH_REDIRECT_URI
        )
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    if not isinstance(credentials, dict) or not credentials.get("refresh_token"):
        # Without a refresh token the device would sync for an hour and
        # then quietly stop, so this is a failed add, not a warning.
        return jsonify({
            "ok": False,
            "error": plugin.oauth_refresh_help or (
                f"{plugin.name} didn't return a refresh token - revoke this app's "
                "access with the provider and try again"
            ),
        }), 400

    _store_account(plugin_id, credentials)
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
