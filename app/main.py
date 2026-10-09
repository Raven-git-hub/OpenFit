import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import time
import urllib.parse
from datetime import date, datetime, timedelta, timezone

from flask import Flask, jsonify, request, send_from_directory
from apscheduler.schedulers.background import BackgroundScheduler

from migrations import run_migrations
from plugins import PLUGINS
from plugins.base import OAUTH_REDIRECT_URI, write_metric
from crypto import encrypt
from derived import (
    backfill_derived,
    load_source_roles,
    recompute_derived,
    set_source_role,
    source_rank,
)
from metrics import (
    METRICS,
    PLAUSIBLE_RANGES,
    RESTING_HR_BPM,
    SESSION_KINDS,
    SLEEP_MINUTES,
    STEPS,
    WEIGHT_KG,
)

DB_PATH = os.getenv("DB_PATH", "/data/tracker.db")
SYNC_INTERVAL_HOURS = int(os.getenv("SYNC_INTERVAL_HOURS", "6"))

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


def backfill():
    """Derive the existing readings, on the first boot after 008 only.

    derived_metrics starts out empty on an upgraded database, and syncs
    only refresh their own recent window - so without this, history
    would never be derived. A no-op once anything has been derived: see
    backfill_derived() for why this is never a re-derive on every start.
    """
    conn = get_conn()
    try:
        if backfill_derived(conn):
            print("[derived] backfilled derived_metrics from the existing readings")
    finally:
        conn.close()


# ---------- static ----------

@app.route("/")
def index():
    return send_from_directory("templates", "index.html")


# ---------- weights ----------

# /api/weights predates the metrics table and keeps its shape for the
# interim UI: [{date, weight}] in kilograms. Weight is now the weight_kg
# metric, and these routes read and write the hand-entered readings
# only - source 'manual'. Any other source's weight_kg readings (a
# scale plugin, one day) sit beside them and are never touched here.
MANUAL_SOURCE = "manual"


@app.route("/api/weights", methods=["GET"])
def get_weights():
    conn = get_conn()
    # The value comes back as stored - a REAL, as the old weights.weight
    # column was - so 82 still reads as 82.0. No whole-number cast:
    # unlike steps, weight was never an INTEGER field.
    rows = conn.execute(
        "SELECT date, value AS weight FROM metrics "
        "WHERE metric = ? AND source = ? ORDER BY date",
        (WEIGHT_KG, MANUAL_SOURCE),
    ).fetchall()
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
    write_metric(conn, d, MANUAL_SOURCE, WEIGHT_KG, w)
    # Re-derive the day, as the webhook does, so the weigh-in is in the
    # derived profile - and the access contract - at once rather than
    # after the next sync. The commit in recompute_derived() lands the
    # reading and its pick together.
    recompute_derived(conn, since=d, until=d)
    conn.close()
    return jsonify({"ok": True})


@app.route("/api/weights", methods=["DELETE"])
def clear_weights():
    conn = get_conn()
    # RETURNING, so the days re-derived are exactly the rows deleted -
    # a weigh-in posted in between can't be cleared without its day
    # being re-derived too.
    cleared = conn.execute(
        "DELETE FROM metrics WHERE metric = ? AND source = ? RETURNING date",
        (WEIGHT_KG, MANUAL_SOURCE),
    ).fetchall()
    # Each cleared day re-derived on its own, never one span over all of
    # them: that would re-pick every other metric in between under the
    # current source roles, which are forward-only. The first commits the
    # delete along with its day's pick.
    for row in cleared:
        recompute_derived(conn, since=row["date"], until=row["date"])
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
# By default each field is the stored derived value (derived_metrics -
# see derived.py); ?by_source=1 lists every source's readings instead.
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

# Canonical metric -> (field, convert), for pivoting readings back.
_ACTIVITY_BY_METRIC = {
    metric: (field, convert) for field, (metric, convert) in ACTIVITY_FIELDS.items()
}


def _recent_activity(conn, table, days):
    """The activity readings in `table` for its `days` latest activity dates.

    `table` is metrics (every source's reading) or derived_metrics (the
    one picked per metric) - both have date, source, metric, value and
    synced_at - and is always one of those two names, never request
    input. Newest date first, then by source.

    Only the metrics this endpoint has fields for: a date that holds
    nothing but some other metric is not an activity day, and must not
    use up one of the `days` asked for.
    """
    keys = [metric for metric, _ in ACTIVITY_FIELDS.values()]
    marks = ", ".join("?" * len(keys))
    # LIMIT applies to distinct dates, not rows: with several sources and
    # metrics per day, limiting rows would silently return fewer days
    # than asked for.
    return conn.execute(
        f"SELECT date, source, metric, value, synced_at FROM {table} "
        f"WHERE metric IN ({marks}) AND date IN ("
        f"SELECT DISTINCT date FROM {table} WHERE metric IN ({marks}) "
        "ORDER BY date DESC LIMIT ?"
        ") "
        "ORDER BY date DESC, source",
        (*keys, *keys, days),
    ).fetchall()


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


def _pivot_by_date(derived):
    """Fold derived readings - one per (date, metric) - into one flat row per date.

    Each field is the stored derived value: the source for each metric
    was picked in derived.py, not here, so a day with steps from Garmin
    and sleep from Google Health keeps both. A metric nothing reported
    is None. `source` names the highest-ranked source behind any of the
    day's fields, as the merged row always has - per-metric provenance
    is available via ?by_source=1. Rows come out in the order the
    readings arrive in.
    """
    flat = {}
    for reading in derived:
        row = flat.get(reading["date"])
        if row is None:
            row = flat[reading["date"]] = {
                "date": reading["date"],
                **{field: None for field in ACTIVITY_FIELDS},
                "source": reading["source"],
            }
        field, convert = _ACTIVITY_BY_METRIC[reading["metric"]]
        row[field] = convert(reading["value"])
        if source_rank(reading["source"]) < source_rank(row["source"]):
            row["source"] = reading["source"]
    return list(flat.values())


@app.route("/api/activity", methods=["GET"])
def get_activity():
    days = int(request.args.get("days", 30))
    by_source = request.args.get("by_source", "").lower() in ("1", "true", "yes")

    conn = get_conn()
    if by_source:
        # Additive: raw per-source rows, for anything that wants to see
        # which device said what.
        rows = _pivot_by_source(_recent_activity(conn, "metrics", days))
    else:
        # Default stays the flat one-row-per-date shape the frontend
        # expects, read from the stored derived values.
        rows = _pivot_by_date(_recent_activity(conn, "derived_metrics", days))
    conn.close()
    return jsonify(rows)


# ---------- the access contract ----------

# The stable read API apps build on: discovery (/api/catalog), then range
# queries - one metric's series (/api/metric/<metric>) or one kind of
# session (/api/sessions/<kind>). Read-only, on the same trusted-network
# model as everything else.
#
# Metrics are served from derived_metrics - the one value per metric per
# day picked in derived.py, each point tagged with the source it came
# from - never the raw per-source rows in metrics, so an app gets one
# trusted answer and never reconciles sources itself. Sessions have no
# derived pick yet: every source's session is served, each tagged with
# its source.
#
# Unlike /api/activity and /api/weights, these shapes were made for apps,
# not kept for the interim UI: keys and units are the canonical ones from
# metrics.py, and a value is the number as stored.


def _date_range(args):
    """((from, to), None) for a contract read's query args, or (None, what's wrong).

    ?from= and ?to= are optional YYYY-MM-DD dates, both inclusive; one
    that is missing or empty is None, leaving that end open.
    """
    bounds = []
    for name in ("from", "to"):
        raw = args.get(name, "")
        if not raw:
            bounds.append(None)
            continue
        try:
            # As the webhook reads its date: strptime rather than
            # date.fromisoformat, which also takes '20260101' and week
            # dates. Normalised, so it compares with stored dates as text.
            bounds.append(datetime.strptime(raw, "%Y-%m-%d").date().isoformat())
        except ValueError:
            return None, f"{name} must be YYYY-MM-DD"
    since, until = bounds
    if since and until and since > until:
        return None, "from must not be after to"
    return (since, until), None


def _in_range(column, since, until):
    """SQL for `since <= column <= until`, an end of None left open, and its params.

    `column` is always an expression written here, never request input.
    """
    bounds = [(f"{column} >= ?", since), (f"{column} <= ?", until)]
    clause = " AND ".join(c for c, d in bounds if d is not None) or "1"
    return clause, [d for _, d in bounds if d is not None]


@app.route("/api/catalog", methods=["GET"])
def get_catalog():
    """What this install has: every metric and session kind with any data.

    {"metrics": [...], "sessions": [...]}. A metric entry is {metric,
    unit, sources, first, last, count, last_value, last_date}: `sources`
    are those that have won any day, in the order the pick tries them;
    first/last bound its derived days and `count` is how many there are;
    last_value is the value on last_date, its latest day. A session entry
    is {kind, count, first, last}, first/last being the earliest and
    latest start. Metrics are in vocabulary order, kinds by name; one with
    no data is left out, so an empty install has two empty lists.
    """
    conn = get_conn()
    roles = load_source_roles(conn)
    # One statement, so the coverage and the last value are one snapshot
    # even if a sync commits while this runs.
    coverage = {
        row["metric"]: row
        for row in conn.execute(
            "SELECT c.metric, c.first, c.last, c.count, c.sources, d.value AS last_value "
            "FROM ("
            "SELECT metric, MIN(date) AS first, MAX(date) AS last, COUNT(*) AS count, "
            # JSON rather than GROUP_CONCAT: a pushed source can be any
            # string, commas included.
            "json_group_array(DISTINCT source) AS sources "
            "FROM derived_metrics GROUP BY metric"
            ") c "
            "JOIN derived_metrics d ON d.metric = c.metric AND d.date = c.last"
        )
    }
    kinds = {
        row["kind"]: row
        for row in conn.execute(
            'SELECT kind, COUNT(*) AS count, MIN("start") AS first, MAX("start") AS last '
            "FROM sessions GROUP BY kind"
        )
    }
    conn.close()

    metrics = []
    for metric, unit in METRICS.items():
        row = coverage.get(metric)
        if row is None:
            continue
        metrics.append({
            "metric": metric,
            "unit": unit,
            "sources": sorted(
                json.loads(row["sources"]),
                key=lambda s: source_rank(s, roles.get(metric)),
            ),
            "first": row["first"],
            "last": row["last"],
            "count": row["count"],
            "last_value": row["last_value"],
            "last_date": row["last"],
        })
    sessions = [
        {"kind": kind, "count": kinds[kind]["count"],
         "first": kinds[kind]["first"], "last": kinds[kind]["last"]}
        for kind in sorted(SESSION_KINDS)
        if kind in kinds
    ]
    return jsonify({"metrics": metrics, "sessions": sessions})


@app.route("/api/metric/<metric>", methods=["GET"])
def get_metric_series(metric):
    """One metric's derived series: {metric, unit, points: [{date, value, source}]}.

    One point per day with a derived value, oldest first, `source` naming
    the source it was picked from. ?from= and ?to= (YYYY-MM-DD, inclusive)
    narrow it. 404 for a metric outside the vocabulary; a known one with
    no data in range has no points.
    """
    if metric not in METRICS:
        return jsonify({"ok": False, "error": f"unknown metric: {metric}"}), 404
    bounds, error = _date_range(request.args)
    if error:
        return jsonify({"ok": False, "error": error}), 400

    in_range, params = _in_range("date", *bounds)
    conn = get_conn()
    points = [
        dict(row)
        for row in conn.execute(
            "SELECT date, value, source FROM derived_metrics "
            f"WHERE metric = ? AND {in_range} ORDER BY date",
            (metric, *params),
        )
    ]
    conn.close()
    return jsonify({"metric": metric, "unit": METRICS[metric], "points": points})


@app.route("/api/sessions/<kind>", methods=["GET"])
def get_sessions(kind):
    """One kind's sessions: [{start, end, source, summary}], by start.

    start/end are ISO 8601 UTC (end may be null) and `summary` the kind's
    breakdown (see SESSION_KINDS in metrics.py) - {} when the source gave
    none. ?from= and ?to= (YYYY-MM-DD, inclusive) match the start's UTC
    date. Every source's sessions are listed, so a night two devices both
    tracked appears twice, each with its source. 404 for an unknown kind.
    """
    if kind not in SESSION_KINDS:
        return jsonify({"ok": False, "error": f"unknown session kind: {kind}"}), 404
    bounds, error = _date_range(request.args)
    if error:
        return jsonify({"ok": False, "error": error}), 400

    # start is YYYY-MM-DDTHH:MM:SSZ (iso_utc()), so its first ten
    # characters are its UTC date.
    in_range, params = _in_range('substr("start", 1, 10)', *bounds)
    conn = get_conn()
    rows = conn.execute(
        'SELECT "start", "end", source, summary_json FROM sessions '
        f'WHERE kind = ? AND {in_range} ORDER BY "start", source',
        (kind, *params),
    ).fetchall()
    conn.close()
    return jsonify([
        {
            "start": row["start"],
            "end": row["end"],
            "source": row["source"],
            "summary": json.loads(row["summary_json"]) if row["summary_json"] else {},
        }
        for row in rows
    ])


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


# ---------- source roles ----------

# Which source is primary for each metric - the engine's config for the
# pick in derived.py. By default every metric follows DEFAULT_PRIORITY;
# setting a metric's primary puts that source first for it, and the rest
# keep the default order (so on a day the primary didn't report, the
# default picks). Stored as the source_roles settings row.
#
# Forward-only: changing a role re-derives nothing. Values already in
# derived_metrics keep the source they were picked with; the new primary
# applies from the next sync, over the window that sync re-derives.


@app.route("/api/source-roles", methods=["GET"])
def get_source_roles():
    """Every canonical metric's primary, and the sources it could be.

    {metric: {"primary", "configured", "sources"}}: `primary` is the
    configured source if `configured`, otherwise the default's pick -
    the highest-ranked source that has reported the metric, or None if
    none has yet. `sources` is every source with a reading of the metric,
    in the order the pick tries them.
    """
    conn = get_conn()
    roles = load_source_roles(conn)
    reported = {}
    for row in conn.execute("SELECT DISTINCT metric, source FROM metrics"):
        reported.setdefault(row["metric"], []).append(row["source"])
    conn.close()

    out = {}
    for metric in METRICS:
        configured = roles.get(metric)
        sources = sorted(
            reported.get(metric, []), key=lambda s: source_rank(s, configured)
        )
        out[metric] = {
            "primary": configured or (sources[0] if sources else None),
            "configured": configured is not None,
            "sources": sources,
        }
    return jsonify(out)


@app.route("/api/source-roles/<metric>", methods=["PUT"])
def set_source_role_route(metric):
    """Set a metric's primary source: {"primary": "<source>"}.

    A null or empty primary clears the override, and the metric reverts
    to the default. The source isn't checked against what has reported:
    a device can be made primary before its first sync.

    Stores the config only - see above: nothing already derived changes
    until the next sync re-derives its window.
    """
    if metric not in METRICS:
        return jsonify({"ok": False, "error": f"unknown metric: {metric}"}), 400
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict) or "primary" not in body:
        return jsonify({"ok": False, "error": 'expected {"primary": "<source>"}'}), 400
    primary = body["primary"]
    if primary is not None and not isinstance(primary, str):
        return jsonify({"ok": False, "error": "primary must be a source id, or null"}), 400

    conn = get_conn()
    set_source_role(conn, metric, (primary or "").strip() or None)
    conn.close()
    return jsonify({"ok": True})


# ---------- webhook ----------

# The push input, beside pull plugins and manual entry: an automation
# (Home Assistant on a smart-scale weigh-in, a Shortcut, a script) POSTs
# one reading to /api/webhook/<token> the moment it happens. One URL for
# every metric - the body names which.
#
# The token in the URL is the auth, and this is the one route that has
# any: everything else stays on the trusted-network model. The token is
# the webhook_token settings row, made on first ask and regenerated to
# revoke a leaked one.

WEBHOOK_TOKEN_KEY = "webhook_token"

# The source a pushed reading is filed under when the body names none.
# An automation can name its device instead (e.g. "eufy"), which then
# takes part in source roles and the derived pick like any other source.
WEBHOOK_SOURCE = "webhook"


def _stored_webhook_token(conn):
    """The stored token, or None if there isn't one yet."""
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (WEBHOOK_TOKEN_KEY,)
    ).fetchone()
    return row["value"] if row and row["value"] else None


def _webhook_token(conn):
    """The current token, making and storing one if there isn't one yet."""
    token = _stored_webhook_token(conn)
    if token:
        return token
    # Only fills an empty slot, never replaces a token: two first asks at
    # once both read back whichever landed first, not one that was then
    # overwritten.
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value "
        "WHERE settings.value IS NULL OR settings.value = ''",
        (WEBHOOK_TOKEN_KEY, secrets.token_urlsafe(32)),
    )
    conn.commit()
    return _stored_webhook_token(conn)


def _webhook_address(token):
    """The token, and where to POST with it - for pasting into an automation."""
    path = f"/api/webhook/{token}"
    return {"token": token, "path": path, "url": request.host_url.rstrip("/") + path}


@app.route("/api/webhook-token", methods=["GET"])
def get_webhook_token():
    """The webhook's token and URL; the token is made on the first ask."""
    conn = get_conn()
    token = _webhook_token(conn)
    conn.close()
    return jsonify(_webhook_address(token))


@app.route("/api/webhook-token/regenerate", methods=["POST"])
def regenerate_webhook_token():
    """Replace the token: the old one stops working at once."""
    token = secrets.token_urlsafe(32)
    conn = get_conn()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (WEBHOOK_TOKEN_KEY, token),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, **_webhook_address(token)})


def _webhook_reading(body):
    """(reading, None) for a valid webhook body, or (None, what's wrong).

    The reading is {date, source, metric, value}, ready for write_metric().
    """
    if not isinstance(body, dict):
        return None, 'expected a JSON object: {"metric": "<key>", "value": <number>}'

    metric = body.get("metric")
    if not isinstance(metric, str) or metric not in METRICS:
        return None, f"unknown metric: {metric!r} - expected one of {', '.join(METRICS)}"

    raw = body.get("value")
    value = _finite_number(raw)
    if value is None:
        return None, "value must be a number"

    low, high = PLAUSIBLE_RANGES.get(metric, (-math.inf, math.inf))
    if not low <= value <= high:
        return None, (
            f"{metric} must be between {low} and {high} {METRICS[metric]}, got {raw}"
        )

    day = body.get("date") or date.today().isoformat()
    try:
        # strptime rather than date.fromisoformat, which also takes
        # '20260101' and week dates: dates are stored as YYYY-MM-DD.
        day = datetime.strptime(day, "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError):
        return None, "date must be YYYY-MM-DD"

    source = body.get("source") or WEBHOOK_SOURCE
    if not isinstance(source, str) or not source.strip():
        return None, "source must be a source id string"
    source = source.strip()
    if source in PLUGINS:
        # Its syncs own those readings: a pushed one would overwrite the
        # device's own, then be overwritten by the next sync.
        return None, (
            f"source {source!r} is the {PLUGINS[source].name} plugin's - "
            "name your device instead"
        )

    return {"date": day, "source": source, "metric": metric, "value": value}, None


def _finite_number(raw):
    """`raw` as a float, or None if it isn't a finite number.

    bool is an int to Python, but true is not a reading; float() can't
    hold an integer too big to store; and the JSON parser lets NaN and
    Infinity through.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        value = float(raw)
    except OverflowError:
        return None
    return value if math.isfinite(value) else None


# No token in the URL at all is a 401 like a wrong one, not a 404.
@app.route("/api/webhook/", defaults={"token": ""}, methods=["POST"])
@app.route("/api/webhook/<token>", methods=["POST"])
def webhook(token):
    """Take one pushed reading: {"metric", "value", "date"?, "source"?}.

    `metric` is a canonical key and `value` a number in its unit, within
    its plausible range (PLAUSIBLE_RANGES); `date` is YYYY-MM-DD and
    defaults to today; `source` defaults to "webhook". Idempotent: it's
    an upsert, so a repeat of the same (date, source, metric) replaces
    the reading - one value per source per day, the latest push wins.

    401 for a wrong token, or when none has been made yet; 400 for a body
    that fails a check, with nothing written.
    """
    conn = get_conn()
    try:
        stored = _stored_webhook_token(conn)
        # Constant-time, so response timing can't leak how much of a
        # guess was right. As bytes: compare_digest refuses a non-ASCII
        # str, and the URL can hold anything.
        if stored is None or not hmac.compare_digest(token.encode(), stored.encode()):
            return jsonify({"ok": False, "error": "unknown webhook token"}), 401

        reading, error = _webhook_reading(request.get_json(force=True, silent=True))
        if error:
            return jsonify({"ok": False, "error": error}), 400

        write_metric(conn, reading["date"], reading["source"], reading["metric"], reading["value"])
        # Like a sync, re-derive what was written - here its one day - so
        # the derived value is current at once. Same transaction: the
        # commit in recompute_derived() lands the reading and its pick
        # together.
        recompute_derived(conn, since=reading["date"], until=reading["date"])
    finally:
        conn.close()
    return jsonify({"ok": True, "stored": {**reading, "unit": METRICS[reading["metric"]]}})


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


def _sync(plugin, conn, days):
    """Run one plugin's sync, then re-derive the window it wrote.

    A `days`-day sync covers today and the days - 1 before it, as every
    plugin counts it, so that is the window recomputed - nothing older.
    Its start is taken before the sync runs: one that crosses midnight
    then starts its own window a day later, still inside this one.
    """
    since = (date.today() - timedelta(days=days - 1)).isoformat()
    n = plugin.sync(conn, days)
    recompute_derived(conn, since=since)
    return n


@app.route("/api/sync/<plugin_id>", methods=["POST"])
def trigger_sync(plugin_id):
    plugin = PLUGINS.get(plugin_id)
    if not plugin:
        return jsonify({"ok": False, "error": f"no such plugin: {plugin_id}"}), 404
    try:
        conn = get_conn()
        n = _sync(plugin, conn, int(request.args.get("days", 7)))
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
                n = _sync(plugin, conn, 7)
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
    backfill()
    start_scheduler()
    app.run(host="0.0.0.0", port=80)


if __name__ == "__main__":
    serve()
