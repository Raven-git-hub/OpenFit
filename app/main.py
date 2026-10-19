import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
import traceback
import urllib.parse
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from flask import Flask, jsonify, request, send_from_directory
from apscheduler.executors.pool import ThreadPoolExecutor
from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler

from migrations import connect, run_migrations
from plugins import PLUGINS
from plugins.base import OAUTH_REDIRECT_URI, write_metric
from apps import (
    APP_ID,
    LibraryError,
    app_present,
    find_app,
    install_app,
    read_app_file,
    scan_apps,
    uninstall_app,
    update_app,
)
from crypto import encrypt
from home_layout import LayoutError, load_layout, parse_layout, save_layout
from derived import (
    backfill_derived,
    clear_source_roles,
    derived_points,
    load_source_roles,
    recompute_derived,
    session_rows,
    set_source_role,
    source_rank,
)
from metrics import (
    METRICS,
    RESTING_HR_BPM,
    SERIES_METRICS,
    SESSION_KINDS,
    STEPS,
    category_for,
    grain_for,
    plausible_value,
)
from host import (
    APP_SOURCE_PREFIX,
    FileUpload,
    WriteCtx,
    app_source,
    custom_metric_defs,
    render_app,
    trailing_window,
)

DB_PATH = os.getenv("DB_PATH", "/data/tracker.db")
AUDIO_DIR = os.getenv("AUDIO_DIR", "/app/audio")
APPS_DIR = os.getenv("APPS_DIR", "/data/apps")
SYNC_INTERVAL_HOURS = int(os.getenv("SYNC_INTERVAL_HOURS", "6"))
# The most a request may carry: a tile's file input sends its file in
# one, and nothing else comes near it - an app's install is a few small
# text files.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

app = Flask(__name__, static_folder=None)

# The database path lives in app.config so tests can point the app at a
# temp file. It still defaults to $DB_PATH, so nothing changes in the
# container.
app.config["DB_PATH"] = DB_PATH
# Likewise the console's voice clips, and the installed apps.
app.config["AUDIO_DIR"] = AUDIO_DIR
app.config["APPS_DIR"] = APPS_DIR
# Flask turns a body over it away with a 413 before reading any of it.
app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


def get_conn():
    # connect() sets the busy timeout and WAL on every connection,
    # before migrate() runs too.
    conn = connect(app.config["DB_PATH"])
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


@app.errorhandler(413)
def too_large(e):
    # Said as every other refusal is, so the page can show why.
    return jsonify({"ok": False, "error": f"Request too large (max {MAX_UPLOAD_BYTES // (1024 * 1024)} MB)."}), 413


# ---------- static ----------

@app.route("/")
def index():
    return send_from_directory("templates", "index.html")


# The console's spoken explanations (its voice buttons). The clips ship
# in app/audio/ and are baked into the image (/app/audio), so they deploy
# with the code. A missing clip 404s and the button just resets.
@app.route("/audio/<name>")
def audio(name):
    return send_from_directory(app.config["AUDIO_DIR"], name)


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
# turn the stored value back into what the field used to hold. Its old
# sleep_hours field is gone: sleep has no daily metric in schema v2 (it
# is sessions only - /api/sessions/sleep).


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

# The stable read API apps build on: discovery (/api/catalog) and which
# days hold data (/api/coverage), then range queries - one metric's series
# (/api/metric/<metric>), one kind of session (/api/sessions/<kind>) or one
# series metric's intraday samples (/api/series/<metric>). Read-only, on the
# same trusted-network model as everything else.
#
# Metrics are served from derived_metrics - the one value per metric per
# day picked in derived.py, each point tagged with the source it came
# from - never the raw per-source rows in metrics, so an app gets one
# trusted answer and never reconciles sources itself. Sessions and
# samples have no derived pick yet: every source's session or sample is
# served, each tagged with its source.
#
# Unlike /api/activity, these shapes were made for apps,
# not kept for the interim UI: keys and units are the canonical ones from
# metrics.py, and a value is the number as stored.
#
# The metrics are the effective vocabulary: the canonical ones, then every
# installed app's own (custom_metric_defs() in host.py), each under its
# full key, app:<id>:<key>, in the unit its app declares. An app's metric
# is served while its app is installed; uninstalled, it drops out of the
# catalog and its reads 404, its readings kept under its source until
# they're removed (DELETE /api/app-sources/<id>, or just that metric's
# with DELETE /api/app-sources/<id>/app:<id>:<key>).


def _date_range(args):
    """((from, to), None) for a contract read's query args, or (None, what's wrong).

    ?from= and ?to= are optional YYYY-MM-DD dates, both inclusive; one
    that is missing or empty is None, leaving that end open. ?days=N
    instead keeps the trailing N days - today (the server's local date)
    and the N - 1 before it, as a sync's days count - and can't be
    combined with them.
    """
    raw_days = args.get("days", "")
    if raw_days:
        if args.get("from") or args.get("to"):
            return None, "days can't be combined with from or to"
        # Nine digits at most: more is past the calendar, and int() of a
        # few thousand digits raises rather than returning.
        if not re.fullmatch(r"[0-9]{1,9}", raw_days) or int(raw_days) < 1:
            return None, "days must be a whole number from 1 to 999999999"
        return trailing_window(int(raw_days)), None

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
    """What this install has: every metric, session kind and series with any data.

    {"metrics": [...], "sessions": [...], "series": [...]}. A metric
    entry is {metric, unit, sources, first, last, count, last_value,
    last_date}: `sources` are those that have won any day, in the order
    the pick tries them; first/last bound its derived days and `count` is
    how many there are; last_value is the value on last_date, its latest
    day. A session entry is {kind, count, first, last}, first/last being
    the earliest and latest start. A series entry is {metric, unit,
    sources, first, last, count}: `sources` are every source with a
    sample of it, in default precedence (there is no pick yet), and
    first/last the earliest and latest sample ts. Every entry also has
    `grain` (grain_for() in metrics.py: "daily" or "instant" for a
    metric, "session", "series") and `category` (category_for()).
    Metrics and series are in vocabulary order, kinds by name; one with
    no data is left out, so an empty install has three empty lists -
    unless ?include_empty=1, which lists every key in the vocabulary, one
    with no data as count 0, sources [] and null first/last (and
    last_value/last_date).

    After the canonical metrics come the installed apps' own, in folder
    then manifest order: `metric` is the full key, app:<id>:<key>, its
    unit, grain and category the ones its app declares, and two more
    fields mark it - `app`, the id of the app that defines it, and
    `label`, its declared name. An uninstalled app's are left out, data
    or not.
    """
    include_empty = request.args.get("include_empty", "").lower() in ("1", "true", "yes")
    customs = custom_metric_defs(app.config["APPS_DIR"])
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
    sampled = {
        row["metric"]: row
        for row in conn.execute(
            "SELECT metric, MIN(ts) AS first, MAX(ts) AS last, COUNT(*) AS count, "
            "json_group_array(DISTINCT source) AS sources "
            "FROM samples GROUP BY metric"
        )
    }
    conn.close()

    # What a key with no data reads as: with ?include_empty, an entry with
    # no sources and nothing to bound; without it, None - left out.
    nothing = (
        {"sources": "[]", "first": None, "last": None, "count": 0, "last_value": None}
        if include_empty else None
    )

    # (key, unit, grain, category, the app's CustomMetric or None)
    vocabulary = [
        (metric, unit, grain_for(metric), category_for(metric), None)
        for metric, unit in METRICS.items()
    ] + [
        (metric, custom.unit, custom.grain, custom.category, custom)
        for metric, custom in customs.items()
    ]
    metrics = []
    for metric, unit, grain, category, custom in vocabulary:
        row = coverage.get(metric, nothing)
        if row is None:
            continue
        entry = {
            "metric": metric,
            "unit": unit,
            "grain": grain,
            "category": category,
            "sources": sorted(
                json.loads(row["sources"]),
                key=lambda s: source_rank(s, roles.get(metric)),
            ),
            "first": row["first"],
            "last": row["last"],
            "count": row["count"],
            "last_value": row["last_value"],
            "last_date": row["last"],
        }
        if custom is not None:
            entry["app"] = custom.app_id
            entry["label"] = custom.label
        metrics.append(entry)
    sessions = [
        {"kind": kind, "grain": grain_for(kind), "category": category_for(kind),
         "count": row["count"], "first": row["first"], "last": row["last"]}
        for kind in sorted(SESSION_KINDS)
        if (row := kinds.get(kind, nothing)) is not None
    ]
    series = [
        {
            "metric": metric,
            "unit": unit,
            "grain": grain_for(metric),
            "category": category_for(metric),
            "sources": sorted(json.loads(row["sources"]), key=source_rank),
            "first": row["first"],
            "last": row["last"],
            "count": row["count"],
        }
        for metric, unit in SERIES_METRICS.items()
        if (row := sampled.get(metric, nothing)) is not None
    ]
    return jsonify({"metrics": metrics, "sessions": sessions, "series": series})


@app.route("/api/coverage", methods=["GET"])
def get_coverage():
    """Which days hold data: {"metrics": {...}, "series": {...}, "sessions": {...}}.

    Each maps a metric, series metric or session kind to its dates with
    any data - ["YYYY-MM-DD", ...], ascending, each once - whichever
    source supplied it: a metric's derived days, the dates a series has a
    sample on, and the dates a kind has a session starting on. Only keys
    with at least one date are listed (?include_empty on the catalog
    names the rest), so an empty install has three empty maps. ?from= and
    ?to= (YYYY-MM-DD, inclusive) narrow the dates. Metrics are the
    catalog's: the canonical ones, then the installed apps' own.
    """
    bounds, error = _date_range(request.args)
    if error:
        return jsonify({"ok": False, "error": error}), 400
    metrics = [*METRICS, *custom_metric_defs(app.config["APPS_DIR"])]

    since, until = bounds
    daily = _in_range("date", since, until)
    # As /api/series bounds samples: on ts itself, which the (metric, ts)
    # index holds, rather than on date(ts).
    sampled = _in_range(
        "ts",
        f"{since}T00:00:00Z" if since else None,
        f"{until}T23:59:59Z" if until else None,
    )
    started = _in_range('date("start")', since, until)
    conn = get_conn()
    coverage = {}
    # Series and session dates are UTC-bucketed; a user-local day boundary is
    # a later refinement (docs/architecture/data-consolidation-model.md).
    for grain, vocabulary, select, (in_range, params) in (
        ("metrics", metrics, "SELECT metric, date FROM derived_metrics", daily),
        ("series", SERIES_METRICS, "SELECT metric, date(ts) FROM samples", sampled),
        ("sessions", sorted(SESSION_KINDS), 'SELECT kind, date("start") FROM sessions', started),
    ):
        dates = {}
        for key, day in conn.execute(
            f"{select} WHERE {in_range} GROUP BY 1, 2 ORDER BY 1, 2", params
        ):
            dates.setdefault(key, []).append(day)
        # Only the vocabulary's keys, as the catalog lists them.
        coverage[grain] = {key: dates[key] for key in vocabulary if key in dates}
    conn.close()
    return jsonify(coverage)


@app.route("/api/metric/<metric>", methods=["GET"])
def get_metric_series(metric):
    """One metric's derived series: {metric, unit, points: [{date, value, source}]}.

    One point per day with a derived value, oldest first, `source` naming
    the source it was picked from. ?from= and ?to= (YYYY-MM-DD, inclusive)
    narrow it, or ?days=N to the trailing N days. `metric` is a canonical
    key or an installed app's own, by its full key (app:<id>:<key>, its
    unit the declared one). 404 for anything else - an uninstalled app's
    metric among them; a known one with no data in range has no points.
    """
    unit = METRICS.get(metric)
    if unit is None:
        custom = custom_metric_defs(app.config["APPS_DIR"]).get(metric)
        if custom is None:
            return jsonify({"ok": False, "error": f"unknown metric: {metric}"}), 404
        unit = custom.unit
    bounds, error = _date_range(request.args)
    if error:
        return jsonify({"ok": False, "error": error}), 400

    conn = get_conn()
    points = derived_points(conn, metric, *bounds)
    conn.close()
    return jsonify({"metric": metric, "unit": unit, "points": points})


@app.route("/api/sessions/<kind>", methods=["GET"])
def get_sessions(kind):
    """One kind's sessions: [{start, end, source, summary}], by source priority.

    start/end are ISO 8601 UTC (end may be null) and `summary` the kind's
    breakdown (see SESSION_KINDS in metrics.py) - {} when the source gave
    none. ?from= and ?to= (YYYY-MM-DD, inclusive) match the start's UTC
    date. Every source's sessions are listed, so a night two devices both
    tracked appears twice, each with its source - a priority-ordered
    passthrough: nothing is picked, filtered or merged, but the sessions
    of the source that would be picked come first, then the rest's, in
    the order the derived pick ranks sources (source_rank(): the kind's
    configured primary in the source roles, if it has one, then
    DEFAULT_PRIORITY, then by name). Each source's sessions are by start.
    404 for an unknown kind.
    """
    if kind not in SESSION_KINDS:
        return jsonify({"ok": False, "error": f"unknown session kind: {kind}"}), 404
    bounds, error = _date_range(request.args)
    if error:
        return jsonify({"ok": False, "error": error}), 400

    conn = get_conn()
    # The query and its order are derived.session_rows()'s, shared with an
    # app's ctx.sessions(), so the two can't drift apart.
    rows = session_rows(conn, kind, *bounds)
    conn.close()
    return jsonify(rows)


@app.route("/api/series/<metric>", methods=["GET"])
def get_samples(metric):
    """One series metric's samples: {metric, unit, points: [{ts, value, source}]}.

    Every source's samples, by ts then source, each tagged with its
    source: there is no pick between sources yet, so where two devices
    both sampled a stretch, both appear. ts is ISO 8601 UTC; ?from= and
    ?to= (YYYY-MM-DD, inclusive) match its UTC date. 404 for a metric
    outside SERIES_METRICS - a daily key included; a known one with no
    samples in range has no points.
    """
    if metric not in SERIES_METRICS:
        return jsonify({"ok": False, "error": f"unknown series metric: {metric}"}), 404
    bounds, error = _date_range(request.args)
    if error:
        return jsonify({"ok": False, "error": error}), 400

    # ts is YYYY-MM-DDTHH:MM:SSZ (iso_utc()), whole seconds, so these
    # bounds take in exactly the samples on those UTC dates - and, unlike
    # sessions' substr(), a range on ts itself can use the (metric, ts)
    # index.
    since, until = bounds
    in_range, params = _in_range(
        "ts",
        f"{since}T00:00:00Z" if since else None,
        f"{until}T23:59:59Z" if until else None,
    )
    conn = get_conn()
    rows = conn.execute(
        "SELECT ts, value, source FROM samples "
        f"WHERE metric = ? AND {in_range} ORDER BY ts, source",
        (metric, *params),
    ).fetchall()
    conn.close()
    # value is a REAL column, so a whole bpm comes back as 77.0: served
    # as the 77 the device recorded.
    points = [
        {"ts": row["ts"], "value": _whole(row["value"]), "source": row["source"]}
        for row in rows
    ]
    return jsonify({"metric": metric, "unit": SERIES_METRICS[metric], "points": points})


# ---------- the app host ----------

# The Home tab's tiles. Each installed app is a folder in APPS_DIR
# (apps.py) whose render(ctx, cfg) returns a display spec (host.py); the
# page lists the apps, renders each and draws the specs - and an app
# with a handle(ctx, action, payload) can be acted on from its tile's
# input, writing under its own source (POST /api/app/<id>/action). The
# folder is scanned on every request, so an app added, edited or removed
# shows on the next load, with no restart. An app is added by hand, or installed
# from the page through POST /api/apps, its files replaced through PUT
# /api/apps/<id>, and removed through DELETE /api/apps/<id>.


@app.route("/api/apps", methods=["GET"])
def list_apps():
    """The installed apps, in folder order: [{id, name, default_size:
    {w, h}, sizes: [{w, h}], reads: [key], config: [{key, label, type,
    default}], metrics: [{key, label, unit, category, grain, range}],
    files: [name]}].

    Only apps that load: a malformed one - one with no size among them -
    is logged and left out. sizes are the tile sizes the app declares,
    in cells, and default_size the first of them, the size a tile is
    first placed at; reads is the manifest's (declared, not enforced);
    config is the manifest's config fields, in order, as written (only
    key is checked) - the schema of the settings form a tile's gear
    opens in Configure, whose values an instance's config holds; metrics
    are the ones it invents, in order, by their short key (stored and
    served as app:<id>:<key>), range [low, high] or null; files names the
    plain files in the app's folder, each one GET /api/app/<id>/file
    serves.
    """
    return jsonify([a.summary() for a in scan_apps(app.config["APPS_DIR"])])


@app.route("/api/apps", methods=["POST"])
def add_app():
    """Install an app from its files: {"files": {name: text}} -> APPS_DIR/<id>/.

    The one route that writes to the apps folder, and the Applications
    panel's Add Application popup is its caller. The id comes from the
    app.yaml among the files. Every check is install_app()'s - the
    page's own are a convenience - and a refusal writes nothing: 400
    for a request, app.yaml, id or file name that won't do, 409 for an
    id that's taken (an install only ever creates: PUT /api/apps/<id>
    replaces an installed app's files). 201 {"ok": true, "id",
    "has_app_py"} once it's there; nothing in it has been run - the next
    scan loads it.
    """
    body = request.get_json(force=True, silent=True)
    files = body.get("files") if isinstance(body, dict) else None
    try:
        installed = install_app(app.config["APPS_DIR"], files)
    except LibraryError as e:
        return jsonify({"ok": False, "error": str(e)}), e.status
    return jsonify({"ok": True, **installed}), 201


@app.route("/api/apps/<app_id>", methods=["PUT"])
def update_installed_app(app_id):
    """Update an app: {"files": {name: text}} replace the files in its folder.

    The Applications list's Update is its caller: install's popup,
    prefilled with the app's files, confirmed. Every check is
    update_app()'s, and they all run before the app is touched - the
    new files staged and loaded first - so a refusal leaves it running
    on its old files: 400 for an id no app could have, or a request,
    app.yaml, id (it must be this app's own), file name or app that
    won't do; 404 for an app that isn't installed; 500 if the files
    can't be written or swapped in. 200 {"ok": true, "id",
    "has_app_py"} once the new folder is in place - all of it: a file
    left out is gone. Nothing in the database changes: what the app
    wrote under app:<id> stays its own.
    """
    body = request.get_json(force=True, silent=True)
    files = body.get("files") if isinstance(body, dict) else None
    try:
        updated = update_app(app.config["APPS_DIR"], app_id, files)
    except LibraryError as e:
        return jsonify({"ok": False, "error": str(e)}), e.status
    return jsonify({"ok": True, **updated})


@app.route("/api/apps/<app_id>", methods=["DELETE"])
def remove_app(app_id):
    """Uninstall an app: its folder in APPS_DIR goes, files and all.

    The Applications list's Uninstall is its caller. The id is never
    used as a path: uninstall_app() checks it, finds the app by scanning
    and removes the folder it loaded from. 200 {"ok": true, "id"}; 400
    for an id no app could have, 404 for one no app loads under - either
    way nothing is removed. Its data stays: what it wrote under app:<id>
    is kept, as a disconnected source's is, until DELETE
    /api/app-sources/<id> removes it.
    """
    try:
        removed = uninstall_app(app.config["APPS_DIR"], app_id)
    except LibraryError as e:
        return jsonify({"ok": False, "error": str(e)}), e.status
    return jsonify({"ok": True, **removed})


@app.route("/api/app/<app_id>/file", methods=["GET"])
def app_file(app_id):
    """One file in an app's folder, as text: ?name=<file name>.

    For the Applications list's read-only viewer. -> {id, name, text}.
    The name must be a plain file name the folder holds - one /api/apps
    lists - and nothing outside the app's folder is ever read (see
    read_app_file()): 400 for a name that isn't plain, 404 for an
    unknown app or file, 413 for a file too big to show, 415 for one
    that isn't text.
    """
    installed = find_app(app.config["APPS_DIR"], app_id)
    if installed is None:
        return jsonify({"ok": False, "error": f"unknown app: {app_id}"}), 404
    name = request.args.get("name", "")
    try:
        text = read_app_file(installed, name)
    except LibraryError as e:
        return jsonify({"ok": False, "error": str(e)}), e.status
    return jsonify({"id": installed.id, "name": name, "text": text})


@app.route("/api/app/<app_id>/render", methods=["POST"])
def render_installed_app(app_id):
    """One app's display spec, rendered now: {"cfg": {key: value}} -> the spec.

    cfg is the instance's config - the values its tile's settings form
    set, stored on the instance in the Home layout - and is passed to
    the app's render(ctx, cfg) as given: the app is trusted and owns its
    fallbacks (cfg.get("stride_m", 0.75)), so nothing in it is checked.
    A body with no cfg, or one that isn't a mapping, renders with {}.
    POST, not GET, only to carry the cfg: nothing is written.

    Always a 200 for an installed app: a render that raises, or returns
    something that isn't a spec, comes back as {"type": "error",
    "message"} so the page shows one error tile, not a failed load. 404
    for an id with no app that loads.
    """
    installed = find_app(app.config["APPS_DIR"], app_id)
    if installed is None:
        return jsonify({"ok": False, "error": f"unknown app: {app_id}"}), 404
    body = request.get_json(force=True, silent=True)
    cfg = body.get("cfg") if isinstance(body, dict) else None
    if not isinstance(cfg, dict):
        cfg = {}
    conn = get_conn()
    try:
        spec = render_app(installed, conn, cfg)
    finally:
        conn.close()
    return jsonify(spec)


def _form_json(raw):
    """A multipart form field that holds JSON, parsed - or, if it isn't
    JSON, the text as sent, which is no mapping."""
    try:
        return json.loads(raw)
    except ValueError:
        return raw


@contextmanager
def _uploaded(files):
    """A request's files as FileUploads, {field: FileUpload}, for an action.

    Each is written to a temp file the host names (never after the
    file's own name), and every one is deleted on the way out - whatever
    happened in between: nothing uploaded is kept.
    """
    paths = []
    try:
        uploads = {}
        for field, storage in files.items():
            fd, path = tempfile.mkstemp(prefix="openfit-upload-")
            paths.append(path)
            with os.fdopen(fd, "wb") as f:
                storage.save(f)
            uploads[field] = FileUpload(path, storage.filename or "", storage.content_type or "")
        yield uploads
    finally:
        for path in paths:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass  # the app removed it itself
            except OSError as e:
                print(f"[apps] couldn't delete the upload {path}: {e}")


@app.route("/api/app/<app_id>/action", methods=["POST"])
def act_on_app(app_id):
    """Act on an app: {"action", "payload", "cfg"} -> {"ok": true, "spec"}.

    What a tile's input element sends when its Save is pressed, and the
    one place an app writes. The app's handle(ctx, action, payload) runs
    with a WriteCtx (host.py) whose source is the app's own, app:<id>:
    the ctx injects it, and no writer takes one, so an app can only ever
    create or overwrite its own rows - never a device's, nor another
    app's. It writes canonical metrics and the ones its manifest declares
    (as app:<id>:<key>), never another app's, and sessions of OpenFit's
    kinds (ctx.write_session()). payload is the app's to
    interpret: a mapping, {key: value}
    from the input, {} if absent. cfg is the instance's config, for the
    render after; one that isn't a mapping is {}.

    A file input's action comes as multipart/form-data instead: action
    as a form field, payload and cfg as form fields holding their JSON
    (either may be left out), and each file under the input's key. A
    file is put in payload under its field as a FileUpload (host.py) -
    in a temp file the host names, deleted once the action is done,
    whatever handle() did. The host carries the file and never reads
    it: handle() decides what it is. A body over MAX_UPLOAD_BYTES is
    turned away with a 413 before any of it is read.

    One transaction. If handle() raises - input it refused, a write the
    ctx refused, a bug - everything it wrote is rolled back and the reply
    is 400 {"ok": false, "error"}: unlike a render, a write fails
    visibly, and a ValueError's message is put as the app wrote it.
    Otherwise each day it wrote a reading to is re-derived, landing its
    writes and their pick together (a session is derived into nothing, and
    is committed all the same), and the app is rendered afresh (render_app(),
    which never raises) for the tile: 200 {"ok": true, "spec"}.

    404 for an id with no app that loads; 400 for an app with no
    handle(), or a body whose action isn't text or whose payload isn't a
    mapping - with nothing run.
    """
    installed = find_app(app.config["APPS_DIR"], app_id)
    if installed is None:
        return jsonify({"ok": False, "error": f"unknown app: {app_id}"}), 404
    if installed.handle is None:
        return jsonify({
            "ok": False,
            "error": f"{app_id} takes no actions: its app.py has no handle(ctx, action, payload)",
        }), 400
    files = {}
    if request.mimetype == "multipart/form-data":
        # A file input's: the same body, as form fields beside the files.
        body = {"action": request.form.get("action")}
        for field in ("payload", "cfg"):
            if field in request.form:
                body[field] = _form_json(request.form[field])
        files = request.files
    else:
        body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": 'expected {"action": "<name>", "payload": {...}}'}), 400
    action = body.get("action")
    if not isinstance(action, str) or not action:
        return jsonify({"ok": False, "error": "action must name one, as text"}), 400
    payload = body.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "payload must be a mapping, such as {\"weight_kg\": 81.2}"}), 400
    cfg = body.get("cfg")
    if not isinstance(cfg, dict):
        cfg = {}

    with _uploaded(files) as uploads:
        payload.update(uploads)
        conn = get_conn()
        try:
            ctx = WriteCtx(conn, installed)
            try:
                installed.handle(ctx, action, payload)
            except Exception as e:
                conn.rollback()
                if isinstance(e, ValueError) and str(e):
                    # A refusal - the app's, or the ctx's: said as it was put.
                    print(f"[apps] {installed.id}: action {action!r} refused: {e}")
                    error = str(e)
                else:
                    print(f"[apps] {installed.id}: action {action!r} failed")
                    traceback.print_exc()
                    error = f"{type(e).__name__}: {e}"
                return jsonify({"ok": False, "error": error}), 400
            # Each day re-derived on its own, never one span over them all,
            # which would re-pick every other metric in between under the
            # current source roles (forward-only). The first commits the
            # writes along with its day's pick.
            for day in ctx.written:
                recompute_derived(conn, since=day, until=day)
            conn.commit()
            spec = render_app(installed, conn, cfg)
        finally:
            conn.close()
    return jsonify({"ok": True, "spec": spec})


# ---------- removing a source's data ----------

# A source's data outlives it: disconnecting a device keeps what it
# synced, and uninstalling an app keeps what it wrote - hand-entered
# data can't be re-synced. Removing that is its own explicit step: a
# device's with DELETE /api/connectors/<id>?wipe=1, an uninstalled app's
# with DELETE /api/app-sources/<id> - or one metric of it with DELETE
# /api/app-sources/<id>/<metric>. All are purge_source().


def purge_source(conn, source, metric=None, keep_roles=False):
    """Delete everything `source` wrote, and re-derive what it fed.

    Every metric, sample and session filed under `source` goes, and the
    days its readings covered are re-derived, so each falls to whatever
    source is left or loses its derived row. The metrics themselves
    stay - only the readings go. Every source role naming it as a
    primary is cleared too (clear_source_roles()), so none is left
    pointing at data that's gone - unless `keep_roles`: a disconnected
    device keeps its roles, as it keeps its sync cadence, for when it's
    added back.

    With `metric`, only that metric's readings and samples from `source`
    go, and only its role is cleared, if it names `source`: the
    source's other metrics, and its roles for them, stay. Sessions are
    keyed by kind, not metric, so none goes.

    One transaction with whatever the caller has already done on `conn`:
    recompute_derived() commits it (this does, with no readings to
    re-derive), so a failure part way leaves every row and role as it
    was. Returns {days, metrics, sessions, samples}: how many distinct
    days its readings covered, and the rows deleted from each table.
    """
    where, args = "source = ?", (source,)
    if metric is not None:
        where, args = "source = ? AND metric = ?", (source, metric)
    # RETURNING: the affected range is exactly the readings deleted,
    # even if a sync commits rows between a separate read and this delete.
    dates = [
        row[0]
        for row in conn.execute(f"DELETE FROM metrics WHERE {where} RETURNING date", args)
    ]
    samples = conn.execute(f"DELETE FROM samples WHERE {where}", args).rowcount
    sessions = 0
    if metric is None:
        sessions = conn.execute("DELETE FROM sessions WHERE source = ?", (source,)).rowcount
    if not keep_roles:
        # Inside the transaction the deletes opened, before the re-derive.
        clear_source_roles(conn, source, only=metric)
    if dates:
        # One span rather than a day at a time, so the whole removal
        # stays one transaction (recompute_derived() commits it). Every
        # metric on every day in the span is re-picked under the
        # current source roles, as a re-derived day's other metrics
        # already are. Samples and sessions have no derived pick.
        recompute_derived(conn, since=min(dates), until=max(dates))
    conn.commit()
    return {
        "days": len(set(dates)),
        "metrics": len(dates),
        "sessions": sessions,
        "samples": samples,
    }


def _has_data(conn, source):
    """Whether `source` has a metric, session or sample row at all."""
    return any(
        conn.execute(f"SELECT 1 FROM {table} WHERE source = ? LIMIT 1", (source,)).fetchone()
        for table in ("metrics", "sessions", "samples")
    )


def _has_metric_data(conn, source, metric):
    """Whether `source` has a reading or sample of `metric`. Sessions are
    keyed by kind, not metric, so aren't looked at."""
    return any(
        conn.execute(
            f"SELECT 1 FROM {table} WHERE source = ? AND metric = ? LIMIT 1", (source, metric)
        ).fetchone()
        for table in ("metrics", "samples")
    )


@app.route("/api/app-sources", methods=["GET"])
def list_app_sources():
    """What apps have written: [{id, source, orphaned, metrics, readings,
    sessions, samples}].

    One entry per app source - app:<id> - that has any rows, by id: the
    app's id and its source; `orphaned` once its app is gone from the
    volume (app_present(): its folder is gone, and no other folder's app
    loads under the id) - an app whose folder is there but fails to load
    is broken, not gone, so isn't, nor is an id no app could have (no
    folder to look for); the metrics it has readings of, in vocabulary
    order; and how many readings, sessions and samples it holds. An
    orphan's data can be removed (DELETE /api/app-sources/<id>), all of
    it or one metric's (DELETE /api/app-sources/<id>/<metric>); no
    other's can. Devices, the webhook's sources and manual aren't apps',
    so aren't listed.
    """
    # Every source from "app:" up to "app;" - the prefix, compared as
    # stored (LIKE would ignore case), and a range the samples table's
    # (source, metric, ts) key can serve.
    low = APP_SOURCE_PREFIX
    high = low[:-1] + chr(ord(low[-1]) + 1)
    found = {}

    def entry(source):
        return found.setdefault(
            source, {"metrics": set(), "readings": 0, "sessions": 0, "samples": 0}
        )

    conn = get_conn()
    try:
        for row in conn.execute(
            "SELECT source, metric, COUNT(*) AS n FROM metrics "
            "WHERE source >= ? AND source < ? GROUP BY source, metric",
            (low, high),
        ):
            got = entry(row["source"])
            got["metrics"].add(row["metric"])
            got["readings"] += row["n"]
        for table in ("sessions", "samples"):
            for row in conn.execute(
                f"SELECT source, COUNT(*) AS n FROM {table} "
                "WHERE source >= ? AND source < ? GROUP BY source",
                (low, high),
            ):
                entry(row["source"])[table] = row["n"]
    finally:
        conn.close()

    apps_dir = app.config["APPS_DIR"]
    order = {metric: i for i, metric in enumerate(METRICS)}
    out = []
    for source in sorted(found):
        app_id = source[len(APP_SOURCE_PREFIX):]
        got = found[source]
        out.append({
            "id": app_id,
            "source": source,
            "orphaned": bool(APP_ID.match(app_id)) and not app_present(apps_dir, app_id),
            "metrics": sorted(got["metrics"], key=lambda m: (order.get(m, len(order)), m)),
            "readings": got["readings"],
            "sessions": got["sessions"],
            "samples": got["samples"],
        })
    return jsonify(out)


@app.route("/api/app-sources/<app_id>", methods=["DELETE"])
def remove_app_source(app_id):
    """Remove an uninstalled app's data: everything app:<app_id> wrote.

    The explicit step after Uninstall, which keeps it. Every metric,
    session and sample filed under app:<app_id> is deleted, the days its
    readings covered re-derived, and every source role naming it cleared
    - purge_source(), one transaction. The metrics stay: weight_kg
    without app:weight's readings is still weight_kg, empty or fed by
    another source. 200 {"ok": true, "removed": {metrics, sessions,
    samples}}, the rows deleted from each.

    Only ever an app's source: the id is always read as app:<id>, so
    this can't reach a device's data (DELETE /api/connectors/<id>?wipe=1
    is that), a webhook source's, or manual's. 404 for an id no app
    could have (the scanner's APP_ID); 400 while the app is still on the
    volume (app_present()) - its folder there, even if what's in it
    fails to load: a live or broken app's data is never removed - and
    404 when app:<id> has no rows. None of those changes anything.
    """
    if not APP_ID.match(app_id):
        return jsonify({"ok": False, "error": f"'{app_id}' isn't an app id: no app source to remove"}), 404
    if app_present(app.config["APPS_DIR"], app_id):
        return jsonify({
            "ok": False,
            "error": f"'{app_id}' is present — fix or uninstall it first; an installed app's data is never removed",
        }), 400
    source = app_source(app_id)
    conn = get_conn()
    try:
        if not _has_data(conn, source):
            return jsonify({"ok": False, "error": f"no data from {source} to remove"}), 404
        removed = purge_source(conn, source)
    finally:
        conn.close()
    return jsonify({
        "ok": True,
        "removed": {k: removed[k] for k in ("metrics", "sessions", "samples")},
    })


@app.route("/api/app-sources/<app_id>/<metric>", methods=["DELETE"])
def remove_app_source_metric(app_id, metric):
    """Remove one metric of an uninstalled app's data: every reading (and
    sample) of `metric` that app:<app_id> wrote.

    DELETE /api/app-sources/<id>, narrowed to one metric: its readings
    under app:<app_id> are deleted, the days they covered re-derived -
    each falls to whatever source is left, or loses its derived row -
    and its source role cleared if it names the app; the app's other
    metrics, its sessions and its roles for anything else stay -
    purge_source(metric=...), one transaction. `metric` is the key as
    stored: a canonical one stays in the vocabulary, only the readings
    go; an app's own (app:<id>:<key>) already left it at uninstall. 200
    {"ok": true, "removed": {metrics, sessions, samples}}, sessions
    always 0.

    The guards are the whole-app route's: 404 for an id no app could
    have (APP_ID); 400 while the app is still on the volume
    (app_present()), loading or not; 404 when app:<id> has no reading or
    sample of `metric`. None of those changes anything.
    """
    if not APP_ID.match(app_id):
        return jsonify({"ok": False, "error": f"'{app_id}' isn't an app id: no app source to remove"}), 404
    if app_present(app.config["APPS_DIR"], app_id):
        return jsonify({
            "ok": False,
            "error": f"'{app_id}' is present — fix or uninstall it first; an installed app's data is never removed",
        }), 400
    source = app_source(app_id)
    conn = get_conn()
    try:
        if not _has_metric_data(conn, source, metric):
            return jsonify({"ok": False, "error": f"no {metric} data from {source} to remove"}), 404
        removed = purge_source(conn, source, metric=metric)
    finally:
        conn.close()
    return jsonify({
        "ok": True,
        "removed": {k: removed[k] for k in ("metrics", "sessions", "samples")},
    })


# ---------- the Home layout ----------

# Home's tabs, and on each which tiles it shows in what order, at what
# size, and the line of text over them: the home_layout settings row
# (home_layout.py). A tile is an instance - a placement of an app, with
# its own id, size and config - so one app can be placed more than once.
# The page owns the flow - it lays a tab's items into its grid in order -
# so the server only keeps the lists, and squares them with the apps
# installed now on every read.


@app.route("/api/home-layout", methods=["GET"])
def get_home_layout():
    """The Home layout: {"tabs": [{id, name, mode: "flow", subtitle,
    items: [{instance_id, app_id, size: {w, h}, config}]}], "default_tab"},
    reconciled with the installed apps.

    Each tab's saved instances in their order (an uninstalled app's
    dropped, a size its app no longer offers back to its default); then
    every installed app with no instance on any tab gets one, a fresh id
    at its default size, at the end of the default tab, in folder order -
    so every installed app has at least one. A layout saved before tabs
    (or nothing saved yet) reads as one tab, "home", showing what it
    showed: nothing saved, every app in folder order at its default size,
    under an empty subtitle. A read never rewrites the saved layout.
    """
    installed = scan_apps(app.config["APPS_DIR"])
    conn = get_conn()
    try:
        layout = load_layout(conn, installed)
    finally:
        conn.close()
    return jsonify(layout)


@app.route("/api/home-layout", methods=["PUT"])
def put_home_layout():
    """Save the Home layout: {"tabs": [...], "default_tab"}, every tab.

    Configure's save, as it's switched off. Stored as given once its
    shape is checked (parse_layout()), an instance sent without an id
    given a fresh one: the server doesn't look at which apps are
    installed or what sizes they offer - the next read squares that -
    and it never works out a position. 200 {"ok": true}; 400 for a body
    that isn't a layout, with nothing stored.
    """
    body = request.get_json(force=True, silent=True)
    try:
        layout = parse_layout(body)
    except LayoutError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    conn = get_conn()
    try:
        save_layout(conn, layout)
    finally:
        conn.close()
    return jsonify({"ok": True})


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

# The push input, beside pull plugins and apps: an automation
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

# Sources nothing writes to any more, kept as they are: "manual" is the
# weigh-ins typed into the console before the weight-entry app replaced
# it (POST /api/weights, retired, and migration 006's). History, read
# and picked like any source's, but never added to.
FROZEN_SOURCES = ("manual",)


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

    try:
        value = plausible_value(metric, body.get("value"))
    except ValueError as e:
        return None, str(e)

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
    if source.startswith(APP_SOURCE_PREFIX):
        # An app's own: written by that app alone, through its actions
        # (POST /api/app/<id>/action), so its rows are only ever its own.
        return None, f"source {source!r} is an app's - name your device instead"
    if source in FROZEN_SOURCES:
        return None, (
            f"source {source!r} is kept as history and takes no new readings - "
            "name your device instead"
        )

    return {"date": day, "source": source, "metric": metric, "value": value}, None


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
# app/crypto.py. The secrets among them never come back out over the API:
# the one thing read back is the account's non-password identifier (a
# Garmin email), for the source's Settings popup - see connector_detail().


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
        # By display name, not registration order: the console's source
        # list and its Add-source menu both render this list as-is.
        for p in sorted(PLUGINS.values(), key=lambda p: p.name.lower())
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
    """Encrypt and upsert one plugin's credentials blob, and schedule its syncs.

    Both add flows - the credentials POST and the oauth finish - come
    through here, so this is where a new source gets its sync job (and a
    re-added one has its job refreshed).
    """
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
    reschedule_plugin(plugin_id)


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
    """Step one: take the client details, hand back a consent URL.

    A manifest field the body leaves out is filled from the plugin's
    stored credentials (get_credentials(): the account, else the env), so
    a connected source re-authorises with an empty body - its client
    details aren't typed in again, and its secret couldn't be: it never
    comes back over the API. The body wins where it has a value, so a
    rotated secret can still be entered.
    """
    plugin, error = _oauth_plugin(plugin_id)
    if error:
        return error

    body = request.get_json(force=True, silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": "expected a JSON object"}), 400

    conn = get_conn()
    try:
        stored = plugin.get_credentials(conn)
    finally:
        conn.close()
    # Manifest fields only, from either side: the old refresh token stays
    # behind - the exchange gets a fresh one for the new code. The secret
    # stays server-side all the same: oauth_auth_url() puts only the
    # client id in the URL the browser sees, and the secret is used only
    # in finish's server-side token exchange.
    values = {**_manifest_values(plugin, stored), **_manifest_values(plugin, body)}
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


def _login(plugin, credentials):
    """The account's non-secret identifier, or None if it has none.

    Built from the manifest, never from the stored blob's own keys: only
    fields declared with a type other than "password" are read, so a
    password can't come back, and neither can anything stored beside the
    manifest fields - an oauth connector's refresh token. Garmin's is its
    email. Several such fields are joined in manifest order.
    """
    values = [
        credentials.get(f["key"])
        for f in plugin.fields
        if f.get("type") != "password"
    ]
    return ", ".join(v for v in values if isinstance(v, str) and v) or None


@app.route("/api/connectors/<plugin_id>/detail", methods=["GET"])
def connector_detail(plugin_id):
    """One source's account, for its Settings popup.

    {connected_via, login, added, last_sync, sync_hours, sync_is_default}:
    `connected_via` is the plugin's add_flow ("credentials" or "oauth");
    `login` the account's non-secret identifier (_login()); `added` when
    the account was stored (accounts.created_at); `last_sync` the latest
    synced_at across the source's metrics, samples and sessions, as
    stored (UTC), null before its first sync. With no account - not
    connected - login, added and last_sync are all null.

    `sync_hours` is how often it syncs (effective_cadence()), and
    `sync_is_default` whether that's the baseline rather than an
    override - reported whether or not it is connected.
    """
    plugin = PLUGINS.get(plugin_id)
    if not plugin:
        return jsonify({"ok": False, "error": f"no such connector: {plugin_id}"}), 404

    login = added = last_sync = None
    conn = get_conn()
    try:
        override = load_sync_cadences(conn).get(plugin_id)
        account = conn.execute(
            "SELECT created_at FROM accounts WHERE plugin_id = ?", (plugin_id,)
        ).fetchone()
        if account is not None:
            added = account["created_at"]
            login = _login(plugin, plugin.get_credentials(conn))
            # MAX skips NULLs - a migrated reading has no synced_at - and
            # every stamp is datetime('now')'s, so they compare as text.
            last_sync = conn.execute(
                "SELECT MAX(stamp) FROM ("
                "SELECT MAX(synced_at) AS stamp FROM metrics WHERE source = ? "
                "UNION ALL SELECT MAX(synced_at) FROM samples WHERE source = ? "
                "UNION ALL SELECT MAX(synced_at) FROM sessions WHERE source = ?"
                ")",
                (plugin_id, plugin_id, plugin_id),
            ).fetchone()[0]
    finally:
        conn.close()
    return jsonify({
        "connected_via": plugin.add_flow,
        "login": login,
        "added": added,
        "last_sync": last_sync,
        "sync_hours": override or SYNC_INTERVAL_HOURS,
        "sync_is_default": override is None,
    })


@app.route("/api/connectors/<plugin_id>/cadence", methods=["PUT"])
def set_connector_cadence(plugin_id):
    """Set how often a source syncs: {"hours": <1, 3, 12 or 24 | null>}.

    A null or absent `hours` clears the override, back to the baseline
    (SYNC_INTERVAL_HOURS). The baseline itself is never stored, so 6 -
    or any value outside ALLOWED_CADENCE_HOURS - is a 400: "default" is
    null, not a number. Takes effect at once: the source's sync job is
    rescheduled at its new cadence, its next sync that far from now.
    """
    if plugin_id not in PLUGINS:
        return jsonify({"ok": False, "error": f"no such connector: {plugin_id}"}), 404
    body = request.get_json(force=True, silent=True)
    if not isinstance(body, dict):
        return jsonify({"ok": False, "error": 'expected {"hours": <hours>|null}'}), 400
    hours = body.get("hours")
    if hours is not None and not _allowed_cadence(hours):
        allowed = ", ".join(str(h) for h in ALLOWED_CADENCE_HOURS)
        return jsonify({
            "ok": False,
            "error": f"hours must be one of {allowed}, or null for the default",
        }), 400

    conn = get_conn()
    try:
        set_sync_cadence(conn, plugin_id, hours)
    finally:
        conn.close()
    reschedule_plugin(plugin_id)
    return jsonify({"ok": True})


@app.route("/api/connectors/<plugin_id>", methods=["DELETE"])
def disconnect_connector(plugin_id):
    """Remove the account AND any token the plugin cached on disk.

    The source's data stays: disconnecting stops future syncs, nothing
    more. ?wipe=1 (also true/yes) deletes that too - every metric, sample
    and session it wrote - and re-derives the days its readings covered,
    so each falls to whatever source is left or loses its derived row
    (purge_source()). Its source roles stay either way, as its cadence
    does. The wipe, the re-derive and the account delete are one
    transaction.
    """
    plugin = PLUGINS.get(plugin_id)
    if not plugin:
        return jsonify({"ok": False, "error": f"no such connector: {plugin_id}"}), 404
    wipe = request.args.get("wipe", "").lower() in ("1", "true", "yes")

    conn = get_conn()
    try:
        conn.execute("DELETE FROM accounts WHERE plugin_id = ?", (plugin_id,))
        if wipe:
            # Commits the account delete along with it. Its roles are
            # kept: a device added back is primary again where it was.
            purge_source(conn, plugin_id, keep_roles=True)
        conn.commit()
    finally:
        conn.close()

    # A leftover session token would mean the device still syncs after
    # you removed it, so this is part of the delete, not a nicety.
    plugin.clear_cached_auth()
    # And its job goes, so a removed source stops ticking at all.
    remove_plugin_job(plugin_id)
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


# One sync at a time, process-wide. The scheduler's thread and a request
# thread are both in this one process, and a sync holds its write
# transaction for the whole run, so two side by side would only wait on
# (or fail on) each other's lock - and pull the same days twice. Taken
# without blocking: a sync that finds one already running backs off.
_sync_lock = threading.Lock()

SYNC_IN_PROGRESS = "a sync is already in progress"


@app.route("/api/sync/<plugin_id>", methods=["POST"])
def trigger_sync(plugin_id):
    plugin = PLUGINS.get(plugin_id)
    if not plugin:
        return jsonify({"ok": False, "error": f"no such plugin: {plugin_id}"}), 404
    if not _sync_lock.acquire(blocking=False):
        return jsonify({"ok": False, "error": SYNC_IN_PROGRESS}), 409
    try:
        conn = get_conn()
        try:
            n = _sync(plugin, conn, int(request.args.get("days", 7)))
        finally:
            conn.close()
        return jsonify({"ok": True, "days_written": n})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500
    finally:
        _sync_lock.release()


def _run_scheduled(plugin):
    """One plugin's scheduled sync, logged. The caller holds _sync_lock.

    A failure is logged, not raised: one source's bad sync must not stop
    the others in scheduled_sync()'s loop, or take its job down.
    """
    try:
        conn = get_conn()
        try:
            # Skip anything with no account and no env credentials -
            # it would only fail with a "not connected" error.
            if not plugin.status(conn)["configured"]:
                return
            n = _sync(plugin, conn, 7)
        finally:
            conn.close()
        print(f"[scheduler] {plugin.id} wrote {n} day(s)")
    except Exception as e:
        print(f"[scheduler] {plugin.id} failed: {e}")


def scheduled_sync():
    """Sync every configured plugin, one after another: the startup catch-up."""
    if not _sync_lock.acquire(blocking=False):
        # Never queued behind it: a manual sync is mid-pull, and the
        # next tick catches up on anything it didn't cover.
        print("[scheduler] sync already running, skipping")
        return
    try:
        for plugin in PLUGINS.values():
            _run_scheduled(plugin)
    finally:
        _sync_lock.release()


def sync_one(plugin_id):
    """One plugin's scheduled tick: its own sync job, at its own cadence.

    Takes the same lock as every other sync, so sources still sync one
    at a time, and backs off the same way when one is already running.
    """
    if not _sync_lock.acquire(blocking=False):
        print(f"[scheduler] sync already running, skipping {plugin_id}")
        return
    try:
        plugin = PLUGINS.get(plugin_id)
        if plugin is not None:
            _run_scheduled(plugin)
    finally:
        _sync_lock.release()


# ---------- sync cadence ----------

# How often each source syncs. Every source runs at the baseline,
# SYNC_INTERVAL_HOURS ($SYNC_INTERVAL_HOURS, default 6), unless its
# Settings popup picks one of the override presets. Kept as the
# sync_cadences settings row, the way source_roles keeps each metric's
# primary: JSON {plugin_id: hours} holding the overrides only, so a
# source absent from it runs at the baseline, and choosing Default
# clears its entry. No migration - it's a settings key.

SYNC_CADENCES_KEY = "sync_cadences"

# The override presets, in hours. The baseline isn't one of them: it's
# expressed by clearing the override, never stored as a number.
ALLOWED_CADENCE_HOURS = (1, 3, 12, 24)


def _allowed_cadence(hours):
    """Whether `hours` is an override preset. bool is an int to Python, but true isn't 1h."""
    return (
        isinstance(hours, int)
        and not isinstance(hours, bool)
        and hours in ALLOWED_CADENCE_HOURS
    )


def load_sync_cadences(conn):
    """The cadence overrides: {plugin_id: hours}.

    Read from the SYNC_CADENCES_KEY settings row; no row means none. A
    value that isn't a JSON object, or an entry that isn't one of
    ALLOWED_CADENCE_HOURS (a stored 6 included), is ignored rather than
    raised, as load_source_roles() ignores a bad role: the row can also
    be written through /api/settings, and a bad value there must not
    stop the scheduler - that source just runs at the baseline.
    """
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (SYNC_CADENCES_KEY,)
    ).fetchone()
    try:
        cadences = json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        return {}
    if not isinstance(cadences, dict):
        return {}
    return {
        plugin_id: hours
        for plugin_id, hours in cadences.items()
        if _allowed_cadence(hours)
    }


def set_sync_cadence(conn, plugin_id, hours):
    """Override `plugin_id`'s cadence with `hours`; None clears it, back to the baseline.

    Raises ValueError for anything but None or one of
    ALLOWED_CADENCE_HOURS. Only stores the config - moving the plugin's
    job is reschedule_plugin()'s.

    Commits.
    """
    if hours is not None and not _allowed_cadence(hours):
        raise ValueError(f"not a cadence preset: {hours!r}")

    # The insert goes first, as in set_source_role(): it opens the write
    # transaction, so two changes at once can't both read the old map
    # and one lose the other's entry.
    conn.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES (?, '{}')",
        (SYNC_CADENCES_KEY,),
    )
    cadences = load_sync_cadences(conn)
    if hours is None:
        cadences.pop(plugin_id, None)
    else:
        cadences[plugin_id] = hours
    conn.execute(
        "UPDATE settings SET value = ? WHERE key = ?",
        (json.dumps(cadences, sort_keys=True), SYNC_CADENCES_KEY),
    )
    conn.commit()


def effective_cadence(conn, plugin_id):
    """How often `plugin_id` syncs, in hours: its override, else the baseline."""
    return load_sync_cadences(conn).get(plugin_id, SYNC_INTERVAL_HOURS)


# ---------- the scheduler ----------

# One interval job per connected plugin, `sync:<plugin_id>`, ticking
# sync_one() at that plugin's effective cadence, plus a one-off
# scheduled_sync() of every plugin at startup. Connecting a source adds
# its job (_store_account()), changing its cadence moves it (PUT
# .../cadence) and removing it drops it (disconnect_connector()).
#
# The running scheduler, once serve() has started it. None until then -
# and always in the tests, which import main without starting one - so
# the helpers below change nothing until there is a scheduler to change.
_scheduler = None


def _job_id(plugin_id):
    return f"sync:{plugin_id}"


def _wants_job(conn, plugin):
    """Whether a plugin gets a sync job: it has an account, or env credentials.

    The env is the pre-UI install's way in (SyncPlugin.get_credentials()),
    and it kept syncing on the old global job, so it keeps a job here.
    """
    has_account = conn.execute(
        "SELECT 1 FROM accounts WHERE plugin_id = ?", (plugin.id,)
    ).fetchone()
    return has_account is not None or plugin.status(conn)["configured"]


def _add_sync_job(scheduler, plugin_id, hours):
    """Add `plugin_id`'s job, replacing any it has: its interval restarts from now."""
    scheduler.add_job(
        sync_one, "interval", hours=hours, args=[plugin_id],
        id=_job_id(plugin_id), replace_existing=True,
    )


def reschedule_plugin(plugin_id):
    """Bring one plugin's sync job in line with its account and cadence.

    A plugin that wants a job (_wants_job()) gets one at its effective
    cadence, its next tick that far from now; one that doesn't has none.
    Safe to repeat. A no-op until the scheduler is running.
    """
    if _scheduler is None:
        return
    conn = get_conn()
    try:
        wanted = _wants_job(conn, PLUGINS[plugin_id])
        hours = effective_cadence(conn, plugin_id)
    finally:
        conn.close()
    if wanted:
        _add_sync_job(_scheduler, plugin_id, hours)
    else:
        remove_plugin_job(plugin_id)


def remove_plugin_job(plugin_id):
    """Drop one plugin's sync job, if it has one. A no-op until the scheduler is running."""
    if _scheduler is None:
        return
    try:
        _scheduler.remove_job(_job_id(plugin_id))
    except JobLookupError:
        pass


def start_scheduler():
    """Start the background sync jobs: the startup catch-up, then a job per plugin.

    Called from serve() only - importing main.py (as the tests do) must
    not start a scheduler or fire off syncs.
    """
    global _scheduler
    scheduler = BackgroundScheduler(
        # One worker, so jobs that come due together - two sources on the
        # same cadence, started together, tick in step for good - queue
        # and run one after the other. With the default pool they would
        # race for _sync_lock and all but one skip, every tick. Queued, a
        # job can start late, so it must never be dropped as missed: no
        # misfire cutoff (APScheduler's default is one second).
        executors={"default": ThreadPoolExecutor(1)},
        job_defaults={"misfire_grace_time": None},
    )
    scheduler.add_job(scheduled_sync, "date")  # run once shortly after startup
    conn = get_conn()
    try:
        for plugin in PLUGINS.values():
            if _wants_job(conn, plugin):
                _add_sync_job(scheduler, plugin.id, effective_cadence(conn, plugin.id))
    finally:
        conn.close()
    scheduler.start()
    _scheduler = scheduler
    return scheduler


def serve():
    migrate()
    backfill()
    start_scheduler()
    app.run(host="0.0.0.0", port=80)


if __name__ == "__main__":
    serve()
