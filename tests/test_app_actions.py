"""
Apps as sources: an app writing into the dataset, and the weight app.

An app's app.py may define handle(ctx, action, payload) beside render();
POST /api/app/<id>/action runs it with a WriteCtx whose source is fixed
to the app's own, app:<id> - no writer takes a source, so an app can
only ever write its own rows. A handle() that raises is rolled back and
answered {ok: false, error}; one that succeeds is committed, its days
re-derived, and the app rendered afresh for its tile.

The weight app, committed in tests/fixtures/apps/weight/ as deployed in
/data/apps, is the first writer and the end-to-end test. It replaced
/api/weights, whose 'manual' weigh-ins are kept as history: still read,
never written to again.

A file input's action comes multipart, the file handed to handle() as a
FileUpload in a temp file that's gone once the action is: the import
app, which parses a file of past weigh-ins itself, is its end-to-end
test.

handle() can file a session too, ctx.write_session(), under the same
source and in the same transaction: the workout-log app, which logs a
workout just finished, is its end-to-end test.

Every test gets its own temp apps folder; nothing reads /data/apps.
"""

import json
import os
import re
import shutil
import sqlite3
import tempfile
import textwrap
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pytest

import apps
import host
from derived import backfill_derived, recompute_derived, set_source_role
from metrics import METRICS, PLAUSIBLE_RANGES, STEPS, WEIGHT_KG
from migrations import run_migrations

REAL_APPS = Path(__file__).parent / "fixtures" / "apps"

SIZE = "sizes: [{w: 1, h: 1}]\n"

TODAY = date.today().isoformat()


@pytest.fixture
def apps_dir(tmp_path, monkeypatch):
    """An empty apps folder, which the app is pointed at."""
    import main

    path = tmp_path / "apps"
    path.mkdir()
    monkeypatch.setitem(main.app.config, "APPS_DIR", str(path))
    return path


def make_app(root, app_id, code=None, manifest=None):
    """An app folder: app.yaml naming `app_id`, app.py from `code`."""
    path = root / app_id
    path.mkdir()
    (path / apps.MANIFEST).write_text(manifest or f"id: {app_id}\nname: {app_id}\n{SIZE}")
    if code is not None:
        (path / apps.MODULE).write_text(textwrap.dedent(code))
    return path


# An app that writes whatever it's told to: {metric, value, date?}, and
# renders what it's given, so a test can see the cfg its render got.
WRITER = """
    def render(ctx, cfg):
        return ctx.number(len(ctx.metric("weight_kg")), label="cfg " + repr(sorted(cfg.items())))

    def handle(ctx, action, payload):
        if action == "write":
            ctx.write_metric(payload["metric"], payload["value"], date=payload.get("date"))
        elif action == "write-then-fail":
            ctx.write_metric(payload["metric"], payload["value"])
            raise RuntimeError("it broke after writing")
        else:
            raise ValueError("no such action: " + action)
"""


def act(client, app_id, action="write", payload=None, cfg=None):
    body = {"action": action}
    if payload is not None:
        body["payload"] = payload
    if cfg is not None:
        body["cfg"] = cfg
    return client.post(f"/api/app/{app_id}/action", json=body)


def write(client, app_id, metric=WEIGHT_KG, value=81.2, day=None):
    payload = {"metric": metric, "value": value}
    if day is not None:
        payload["date"] = day
    return act(client, app_id, payload=payload)


def rows(conn):
    """Every reading in metrics, (date, source, metric, value)."""
    return [
        tuple(r) for r in conn.execute(
            "SELECT date, source, metric, value FROM metrics ORDER BY date, source, metric"
        )
    ]


def picked(conn, day, metric):
    """(value, source) derived for one (date, metric), or None."""
    row = conn.execute(
        "SELECT value, source FROM derived_metrics WHERE date = ? AND metric = ?",
        (day, metric),
    ).fetchone()
    return tuple(row) if row else None


def insert(conn, day, source, **values):
    for metric, value in values.items():
        conn.execute(
            "INSERT INTO metrics (date, source, metric, value, unit, synced_at) "
            "VALUES (?, ?, ?, ?, ?, '2026-01-01 06:00:00')",
            (day, source, metric, value, METRICS[metric]),
        )
    conn.commit()


# What the ctx says of a key that's neither canonical nor the app's own.
DECLARE_IT = "declare it in app.yaml's metrics to write it"


def an_app(app_id, metrics=()):
    """An InstalledApp to make a ctx for, declaring `metrics` - no folder."""
    return apps.InstalledApp(
        id=app_id, name=app_id, sizes=[{"w": 1, "h": 1}], default_size={"w": 1, "h": 1},
        reads=[], config=[], path="/nowhere", render=None, metrics=list(metrics),
    )


def assert_refused(resp, status=400):
    assert resp.status_code == status
    body = resp.get_json()
    assert body["ok"] is False
    assert body["error"]
    return body["error"]


# ---------- the route ----------


def test_an_action_writes_under_the_apps_own_source_and_nothing_else(client, conn, apps_dir):
    make_app(apps_dir, "scale", WRITER)

    resp = write(client, "scale", value=81.2, day="2026-01-02")

    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert rows(conn) == [("2026-01-02", "app:scale", "weight_kg", 81.2)]
    [unit] = conn.execute("SELECT unit FROM metrics").fetchone()
    assert unit == "kg"


def test_an_app_cant_name_a_source(client, conn, apps_dir):
    # No writer takes one: an app that tries is refused, and nothing is
    # written - least of all under the device's name.
    make_app(apps_dir, "forger", """
        def render(ctx, cfg):
            return ctx.empty()

        def handle(ctx, action, payload):
            ctx.write_metric("weight_kg", 80.0, source="garmin")
    """)
    insert(conn, "2026-01-02", "garmin", weight_kg=75.0)

    assert_refused(act(client, "forger", payload={}))
    assert rows(conn) == [("2026-01-02", "garmin", "weight_kg", 75.0)]


def test_an_app_named_after_a_device_still_writes_only_its_own_rows(client, conn, apps_dir):
    # The source is injected, never the app's to choose: an app whose id
    # is garmin's writes under app:garmin, beside Garmin's reading - the
    # device's row untouched, and its pick with it.
    make_app(apps_dir, "garmin", WRITER)
    insert(conn, TODAY, "garmin", weight_kg=75.0)
    recompute_derived(conn)

    assert write(client, "garmin", value=90.0).status_code == 200

    assert rows(conn) == [
        (TODAY, "app:garmin", "weight_kg", 90.0),
        (TODAY, "garmin", "weight_kg", 75.0),
    ]
    assert picked(conn, TODAY, WEIGHT_KG) == (75.0, "garmin")


def test_two_apps_never_touch_each_others_rows(client, conn, apps_dir):
    make_app(apps_dir, "one", WRITER)
    make_app(apps_dir, "two", WRITER)

    write(client, "one", value=80.0, day="2026-01-02")
    write(client, "two", value=82.0, day="2026-01-02")
    write(client, "one", value=81.0, day="2026-01-02")

    assert rows(conn) == [
        ("2026-01-02", "app:one", "weight_kg", 81.0),
        ("2026-01-02", "app:two", "weight_kg", 82.0),
    ]


def test_an_action_re_derives_the_day_it_wrote(client, conn, apps_dir):
    make_app(apps_dir, "scale", WRITER)

    write(client, "scale", value=81.2, day="2026-01-02")

    assert picked(conn, "2026-01-02", WEIGHT_KG) == (81.2, "app:scale")
    assert client.get("/api/metric/weight_kg").get_json()["points"] == [
        {"date": "2026-01-02", "value": 81.2, "source": "app:scale"}
    ]
    [weight] = client.get("/api/catalog").get_json()["metrics"]
    assert (weight["metric"], weight["last_value"], weight["sources"]) == ("weight_kg", 81.2, ["app:scale"])


def test_an_apps_write_still_goes_through_the_pick(client, conn, apps_dir):
    # No tier and no outranking: Garmin is in DEFAULT_PRIORITY and the
    # app isn't, so Garmin's weigh-in stays the pick...
    make_app(apps_dir, "scale", WRITER)
    insert(conn, "2026-01-02", "garmin", weight_kg=83.0)
    recompute_derived(conn)

    write(client, "scale", value=82.5, day="2026-01-02")
    assert picked(conn, "2026-01-02", WEIGHT_KG) == (83.0, "garmin")

    # ... unless the user makes the app the metric's primary.
    set_source_role(conn, WEIGHT_KG, "app:scale")
    write(client, "scale", value=82.4, day="2026-01-02")
    assert picked(conn, "2026-01-02", WEIGHT_KG) == (82.4, "app:scale")


def test_an_action_re_derives_only_the_days_it_wrote(client, conn, apps_dir):
    # Forward-only: a role set since the last derive mustn't reach a day
    # between two the app wrote - each is re-derived on its own.
    make_app(apps_dir, "multi", """
        def render(ctx, cfg):
            return ctx.empty()

        def handle(ctx, action, payload):
            ctx.write_metric("weight_kg", 82.0, date="2026-01-01")
            ctx.write_metric("weight_kg", 81.5, date="2026-01-03")
    """)
    insert(conn, "2026-01-02", "garmin", steps=9000)
    insert(conn, "2026-01-02", "google_health", steps=8000)
    recompute_derived(conn)
    set_source_role(conn, STEPS, "google_health")

    assert act(client, "multi", "log", {}).status_code == 200

    assert picked(conn, "2026-01-02", STEPS) == (9000, "garmin")
    assert picked(conn, "2026-01-01", WEIGHT_KG) == (82.0, "app:multi")
    assert picked(conn, "2026-01-03", WEIGHT_KG) == (81.5, "app:multi")


def test_an_action_replies_with_the_app_rendered_afresh_with_its_cfg(client, conn, apps_dir):
    make_app(apps_dir, "scale", WRITER)

    resp = act(client, "scale", payload={"metric": WEIGHT_KG, "value": 80.0}, cfg={"k": 1})

    # The render after the write: it sees the new reading, and the cfg.
    assert resp.get_json() == {"ok": True, "spec": {
        "type": "number", "value": 1, "label": "cfg [('k', 1)]", "caption": None, "unit": None,
    }}


def test_a_failed_render_after_a_good_action_is_an_error_spec_not_a_failure(client, conn, apps_dir):
    # The write stands: it's the render that failed, as a render does.
    make_app(apps_dir, "scale", """
        def render(ctx, cfg):
            raise RuntimeError("can't draw")

        def handle(ctx, action, payload):
            ctx.write_metric("weight_kg", 80.0)
    """)

    resp = act(client, "scale", "log", {})

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "spec": {"type": "error", "message": "RuntimeError: can't draw"}}
    assert rows(conn) == [(TODAY, "app:scale", "weight_kg", 80.0)]


def test_handle_is_given_the_action_and_payload_as_sent_and_a_ctx_that_reads(client, conn, apps_dir):
    # What it's given comes back as its refusal, so the test can see it;
    # the ctx reads as render's does - two days of steps.
    make_app(apps_dir, "echo", """
        def render(ctx, cfg):
            return ctx.empty()

        def handle(ctx, action, payload):
            seen = (action, payload, ctx.today(), ctx.source, len(ctx.metric("steps")))
            raise ValueError(repr(seen))
    """)
    insert(conn, "2026-01-01", "garmin", steps=9000)
    insert(conn, "2026-01-02", "garmin", steps=9500)
    recompute_derived(conn)

    error = assert_refused(act(client, "echo", "go", {"a": [1, "two"], "b": None}))

    assert error == repr(("go", {"a": [1, "two"], "b": None}, TODAY, "app:echo", 2))


def test_a_handle_that_raises_is_rolled_back_and_says_why(client, conn, apps_dir, capsys):
    # A write fails visibly - never a 500 - and whatever it wrote before
    # raising goes with it, its derived pick never made.
    make_app(apps_dir, "scale", WRITER)

    error = assert_refused(act(client, "scale", "write-then-fail", {"metric": WEIGHT_KG, "value": 80.0}))

    assert error == "RuntimeError: it broke after writing"
    assert rows(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM derived_metrics").fetchone()[0] == 0
    # A bug is logged with its traceback, for the app's author.
    logged = capsys.readouterr()
    assert "[apps] scale: action 'write-then-fail' failed" in logged.out
    assert "Traceback" in logged.err and "RuntimeError: it broke after writing" in logged.err


def test_a_value_error_is_the_apps_refusal_said_as_written(client, conn, apps_dir, capsys):
    make_app(apps_dir, "scale", WRITER)

    error = assert_refused(act(client, "scale", "nope", {}))

    assert error == "no such action: nope"
    assert "[apps] scale: action 'nope' refused: no such action: nope" in capsys.readouterr().out
    assert rows(conn) == []


@pytest.mark.parametrize(
    "payload, says",
    [
        ({"metric": "weight_lbs", "value": 180}, f"unknown metric: 'weight_lbs' - {DECLARE_IT}"),
        ({"metric": "heart_rate", "value": 60}, f"unknown metric: 'heart_rate' - {DECLARE_IT}"),
        ({"metric": WEIGHT_KG, "value": True}, "value must be a number"),
        ({"metric": WEIGHT_KG, "value": "81.2"}, "value must be a number"),
        ({"metric": WEIGHT_KG, "value": None}, "value must be a number"),
        ({"metric": WEIGHT_KG, "value": 5000}, "weight_kg must be between 20 and 400 kg, got 5000"),
        ({"metric": WEIGHT_KG, "value": 81, "date": "02/01/2026"}, "date must be YYYY-MM-DD: '02/01/2026'"),
        ({"metric": WEIGHT_KG, "value": 81, "date": 20260102}, "date must be YYYY-MM-DD: 20260102"),
    ],
)
def test_the_ctx_refuses_a_reading_that_wont_do(client, conn, apps_dir, payload, says):
    # The webhook's rule: a canonical key (or one the app declares - this
    # one declares none), a finite number within its plausible range, a
    # YYYY-MM-DD date.
    make_app(apps_dir, "scale", WRITER)

    assert assert_refused(act(client, "scale", payload=payload)) == says
    assert rows(conn) == []


def test_an_unknown_app_is_404(client, apps_dir):
    assert_refused(write(client, "nope"), 404)


def test_an_app_that_doesnt_load_is_404(client, apps_dir):
    make_app(apps_dir, "broken", "def handle(ctx, action, payload):\n    pass\n")   # no render

    assert_refused(write(client, "broken"), 404)


def test_an_app_with_no_handle_is_400(client, conn, apps_dir):
    shutil.copytree(REAL_APPS / "steps", apps_dir / "steps", ignore=shutil.ignore_patterns("__pycache__"))
    make_app(apps_dir, "bare")   # no app.py at all

    for app_id in ("steps", "bare"):
        error = assert_refused(write(client, app_id))
        assert error == f"{app_id} takes no actions: its app.py has no handle(ctx, action, payload)"
    assert rows(conn) == []


@pytest.mark.parametrize(
    "body, says",
    [
        (None, 'expected {"action": "<name>", "payload": {...}}'),
        ([], 'expected {"action": "<name>", "payload": {...}}'),
        ({}, "action must name one, as text"),
        ({"action": ""}, "action must name one, as text"),
        ({"action": 3}, "action must name one, as text"),
        ({"action": "write", "payload": [81.2]}, 'payload must be a mapping, such as {"weight_kg": 81.2}'),
        ({"action": "write", "payload": "81.2"}, 'payload must be a mapping, such as {"weight_kg": 81.2}'),
    ],
)
def test_a_body_that_isnt_an_action_runs_nothing(client, conn, apps_dir, body, says):
    make_app(apps_dir, "scale", """
        def render(ctx, cfg):
            return ctx.empty()

        def handle(ctx, action, payload):
            ctx.write_metric("weight_kg", 80.0)
    """)

    resp = client.post("/api/app/scale/action", json=body) if body is not None else client.post(
        "/api/app/scale/action", data="not json", content_type="application/json"
    )

    assert assert_refused(resp) == says
    assert rows(conn) == []


def test_a_payload_left_out_is_an_empty_one_and_a_bad_cfg_is_ignored(client, conn, apps_dir):
    make_app(apps_dir, "scale", """
        def render(ctx, cfg):
            return ctx.empty(repr(cfg))

        def handle(ctx, action, payload):
            ctx.write_metric("mood", len(payload) + 1)
    """)

    resp = client.post("/api/app/scale/action", json={"action": "log", "cfg": ["not", "a", "mapping"]})

    assert resp.get_json() == {"ok": True, "spec": {"type": "empty", "message": "{}"}}
    assert rows(conn) == [(TODAY, "app:scale", "mood", 1.0)]


def test_an_action_that_writes_nothing_is_fine(client, conn, apps_dir):
    make_app(apps_dir, "idle", """
        def render(ctx, cfg):
            return ctx.empty("idle")

        def handle(ctx, action, payload):
            pass
    """)

    assert act(client, "idle", "log", {}).get_json() == {"ok": True, "spec": {"type": "empty", "message": "idle"}}
    assert rows(conn) == []


def test_an_action_is_a_post(client, apps_dir):
    make_app(apps_dir, "scale", WRITER)

    assert client.get("/api/app/scale/action").status_code == 405


def test_render_cant_write(client, conn, apps_dir):
    # The read path's ctx has no writer: an app writes only from handle(),
    # through the action route.
    make_app(apps_dir, "sneaky", """
        def render(ctx, cfg):
            ctx.write_metric("weight_kg", 80.0)
            return ctx.empty()
    """)

    spec = client.post("/api/app/sneaky/render").get_json()

    assert spec["type"] == "error" and "write_metric" in spec["message"]
    assert rows(conn) == []
    assert not hasattr(host.Ctx(conn), "write_metric")


def test_uninstalling_an_app_keeps_what_it_wrote(client, conn, apps_dir):
    # As a disconnected source's data stays - and the app installed again
    # under its id carries on under the same source.
    make_app(apps_dir, "scale", WRITER)
    write(client, "scale", value=80.0, day="2026-01-02")

    assert client.delete("/api/apps/scale").status_code == 200
    assert rows(conn) == [("2026-01-02", "app:scale", "weight_kg", 80.0)]
    assert picked(conn, "2026-01-02", WEIGHT_KG) == (80.0, "app:scale")

    make_app(apps_dir, "scale", WRITER)
    write(client, "scale", value=79.5, day="2026-01-03")
    assert [r[1] for r in rows(conn)] == ["app:scale", "app:scale"]


# ---------- the writing ctx ----------


def test_a_write_ctx_writes_under_app_and_its_id(conn):
    ctx = host.WriteCtx(conn, an_app("scale"), today=date(2026, 1, 5))

    assert ctx.source == "app:scale" == host.app_source("scale")
    ctx.write_metric(WEIGHT_KG, 81)
    ctx.write_metric(WEIGHT_KG, 80.5, date="2026-01-03")
    ctx.write_metric(STEPS, 9000, date=date(2026, 1, 4))
    # Not committed: that's the route's.
    conn.commit()

    assert rows(conn) == [
        ("2026-01-03", "app:scale", "weight_kg", 80.5),
        ("2026-01-04", "app:scale", "steps", 9000.0),
        ("2026-01-05", "app:scale", "weight_kg", 81.0),
    ]
    # The days written, oldest first, for the route to re-derive.
    assert ctx.written == ["2026-01-03", "2026-01-04", "2026-01-05"]


def test_a_write_ctx_takes_a_day_as_a_date_or_iso_text(conn):
    ctx = host.WriteCtx(conn, an_app("scale"))
    ctx.write_metric(WEIGHT_KG, 81, date="2026-1-5")   # as the webhook does: normalised

    assert ctx.written == ["2026-01-05"]
    for bad in ("2026-13-01", "20260105", "", "today", 20260105):
        with pytest.raises(ValueError, match="date must be YYYY-MM-DD"):
            ctx.write_metric(WEIGHT_KG, 81, date=bad)
    with pytest.raises(ValueError, match="date must be YYYY-MM-DD"):
        # A datetime is a date to Python, but it's a moment, not a day.
        ctx.write_metric(WEIGHT_KG, 81, date=__import__("datetime").datetime(2026, 1, 5, 7, 30))
    assert ctx.written == ["2026-01-05"]


@pytest.mark.parametrize("metric", sorted(PLAUSIBLE_RANGES))
def test_a_write_ctx_holds_each_metric_to_its_plausible_range(conn, metric):
    low, high = PLAUSIBLE_RANGES[metric]
    ctx = host.WriteCtx(conn, an_app("scale"), today=date(2026, 1, 5))

    for value in (low - 0.01, high + 0.01, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            ctx.write_metric(metric, value)
    ctx.write_metric(metric, low)
    ctx.write_metric(metric, high, date="2026-01-04")
    assert len(rows(conn)) == 2


def test_the_webhook_refuses_an_apps_source(client, conn):
    # The other way round: nothing but the app itself writes under app:<id>.
    token = client.get("/api/webhook-token").get_json()["token"]

    for source in ("app:weight", "app:"):
        resp = client.post(f"/api/webhook/{token}", json={"metric": WEIGHT_KG, "value": 80, "source": source})
        assert assert_refused(resp) == f"source {source!r} is an app's - name your device instead"
    assert rows(conn) == []


# ---------- loading handle() ----------


def test_an_app_py_with_a_handle_loads_it_beside_render(apps_dir):
    make_app(apps_dir, "scale", WRITER)
    make_app(apps_dir, "steps", "def render(ctx, cfg):\n    return ctx.empty()\n")
    make_app(apps_dir, "bare")

    bare, scale, steps = apps.scan_apps(str(apps_dir))   # folder order
    assert (scale.id, callable(scale.handle)) == ("scale", True)
    assert scale.handle.__module__ == scale.render.__module__   # one import
    assert steps.handle is None and bare.handle is None


def test_a_handle_that_isnt_a_function_doesnt_load(apps_dir, capsys):
    make_app(apps_dir, "odd", "def render(ctx, cfg):\n    return ctx.empty()\n\nhandle = 3\n")

    assert apps.scan_apps(str(apps_dir)) == []
    assert "app.py's handle must be a function: handle(ctx, action, payload)" in capsys.readouterr().out


# ---------- the weight app ----------


def install_weight(apps_dir):
    shutil.copytree(REAL_APPS / "weight", apps_dir / "weight", ignore=shutil.ignore_patterns("__pycache__"))


def log(client, weight):
    return act(client, "weight", "log", {"weight_kg": weight})


LOG_FIELD = {
    "type": "input", "key": "weight_kg", "action": "log",
    "label": "Log weight", "kind": "number", "unit": "kg", "options": None, "accept": None,
}


def test_the_weight_app_loads_as_written(client, apps_dir):
    install_weight(apps_dir)

    assert client.get("/api/apps").get_json() == [
        {"id": "weight", "name": "Weight", "default_size": {"w": 1, "h": 1},
         "sizes": [{"w": 1, "h": 1}, {"w": 2, "h": 1}], "reads": ["weight_kg"], "config": [], "metrics": [],
         "files": ["app.py", "app.yaml"]},
    ]


def test_the_weight_app_with_no_weigh_ins_says_so_over_its_field(client, apps_dir):
    install_weight(apps_dir)

    assert client.post("/api/app/weight/render").get_json() == {
        "type": "panel", "children": [{"type": "empty", "message": "No weigh-ins yet"}, LOG_FIELD],
    }


def test_the_weight_app_shows_the_latest_weight_over_its_field(client, conn, apps_dir):
    install_weight(apps_dir)
    insert(conn, "2026-01-01", "manual", weight_kg=82.0)
    insert(conn, "2026-01-03", "garmin", weight_kg=81.4)
    recompute_derived(conn)

    assert client.post("/api/app/weight/render").get_json() == {
        "type": "panel", "children": [
            {"type": "number", "value": 81.4, "label": None, "caption": "2026-01-03 · via garmin", "unit": "kg"},
            LOG_FIELD,
        ],
    }


def test_logging_a_weight_files_it_under_the_app_and_redraws_the_tile(client, conn, apps_dir):
    install_weight(apps_dir)

    resp = log(client, 81.2)

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "spec": {
        "type": "panel", "children": [
            {"type": "number", "value": 81.2, "label": None, "caption": f"{TODAY} · via app:weight", "unit": "kg"},
            LOG_FIELD,
        ],
    }}
    assert rows(conn) == [(TODAY, "app:weight", "weight_kg", 81.2)]
    assert client.get("/api/metric/weight_kg").get_json() == {
        "metric": "weight_kg", "unit": "kg",
        "points": [{"date": TODAY, "value": 81.2, "source": "app:weight"}],
    }


def test_a_second_weigh_in_the_same_day_replaces_the_first(client, conn, apps_dir):
    install_weight(apps_dir)

    log(client, 81.2)
    log(client, 80.9)

    assert rows(conn) == [(TODAY, "app:weight", "weight_kg", 80.9)]
    assert client.get("/api/metric/weight_kg").get_json()["points"] == [
        {"date": TODAY, "value": 80.9, "source": "app:weight"}
    ]


def test_the_weight_app_takes_typed_text_that_is_a_number(client, conn, apps_dir):
    install_weight(apps_dir)

    assert log(client, "81.2").status_code == 200
    assert rows(conn) == [(TODAY, "app:weight", "weight_kg", 81.2)]


@pytest.mark.parametrize("junk", ["abc", "", None, True, 0, -80, "nan", "inf", [81], {"kg": 81}])
def test_the_weight_app_refuses_junk(client, conn, apps_dir, junk):
    install_weight(apps_dir)

    assert assert_refused(log(client, junk)) == "Enter your weight in kg, as a positive number"
    assert rows(conn) == []


def test_the_weight_app_refuses_an_implausible_weight(client, conn, apps_dir):
    # Positive, but no one's weight: the ctx's plausible range catches it.
    install_weight(apps_dir)

    assert assert_refused(log(client, 8000)) == "weight_kg must be between 20 and 400 kg, got 8000.0"
    assert rows(conn) == []


def test_the_weight_app_has_one_action(client, conn, apps_dir):
    install_weight(apps_dir)

    assert assert_refused(act(client, "weight", "clear", {})) == "unknown action: 'clear'"


# ---------- the mood app: a select ----------
#
# The weight app's shape with a dropdown in place of a number field:
# committed in tests/fixtures/apps/mood/, it offers the canonical mood
# scale as the options "1"-"5", and its handle() - not the host - checks
# the choice sent is one of them before filing it as a number.


MOOD_FIELD = {
    "type": "input", "key": "mood", "action": "log", "label": "Mood today",
    "kind": "select", "unit": None, "options": ["1", "2", "3", "4", "5"], "accept": None,
}


def install_mood(client):
    """Install the mood app through the Library, from its committed files."""
    folder = REAL_APPS / "mood"
    files = {name: (folder / name).read_text() for name in ("app.yaml", "app.py")}
    resp = client.post("/api/apps", json={"files": files})
    assert resp.status_code == 201, resp.get_json()


def log_mood(client, mood):
    return act(client, "mood", "log", {"mood": mood})


def test_the_mood_app_installs_and_is_listed(client, apps_dir):
    install_mood(client)

    assert client.get("/api/apps").get_json() == [
        {"id": "mood", "name": "Mood", "default_size": {"w": 1, "h": 1},
         "sizes": [{"w": 1, "h": 1}, {"w": 2, "h": 1}], "reads": ["mood"], "config": [], "metrics": [],
         "files": ["app.py", "app.yaml"]},
    ]


def test_the_mood_app_with_no_mood_says_so_over_its_dropdown(client, apps_dir):
    install_mood(client)

    assert client.post("/api/app/mood/render").get_json() == {
        "type": "panel", "children": [{"type": "empty", "message": "No mood logged yet"}, MOOD_FIELD],
    }


def test_logging_a_mood_files_the_choice_as_a_number_and_redraws_the_tile(client, conn, apps_dir):
    install_mood(client)

    resp = log_mood(client, "3")

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "spec": {
        "type": "panel", "children": [
            {"type": "number", "value": 3, "label": None, "caption": "today", "unit": None},
            MOOD_FIELD,
        ],
    }}
    # The canonical key, under the app's own source, and derived for today.
    assert rows(conn) == [(TODAY, "app:mood", "mood", 3.0)]
    assert picked(conn, TODAY, "mood") == (3.0, "app:mood")
    assert client.get("/api/metric/mood").get_json() == {
        "metric": "mood", "unit": "scale",
        "points": [{"date": TODAY, "value": 3.0, "source": "app:mood"}],
    }


def test_a_second_mood_the_same_day_replaces_the_first(client, conn, apps_dir):
    install_mood(client)

    log_mood(client, "2")
    log_mood(client, "5")

    assert rows(conn) == [(TODAY, "app:mood", "mood", 5.0)]


def test_the_mood_app_shows_an_earlier_days_date(client, conn, apps_dir):
    install_mood(client)
    insert(conn, "2026-01-03", "webhook", mood=4)
    recompute_derived(conn)

    spec = client.post("/api/app/mood/render").get_json()

    assert spec["children"][0] == {"type": "number", "value": 4, "label": None, "caption": "2026-01-03", "unit": None}


@pytest.mark.parametrize("choice", ["", "0", "6", "3.0", " 3", "three", 3, None, True])
def test_the_mood_app_refuses_a_choice_it_didnt_offer(client, conn, apps_dir, choice):
    # The host doesn't hold a select's value to its options - the action
    # route keeps no memory of a render - so handle() does, and its
    # refusal is the action's error. Nothing is written.
    install_mood(client)

    assert assert_refused(log_mood(client, choice)) == "Choose a mood from 1 to 5"
    assert rows(conn) == []
    assert picked(conn, TODAY, "mood") is None


def test_the_mood_app_has_one_action(client, conn, apps_dir):
    install_mood(client)

    assert assert_refused(act(client, "mood", "clear", {"mood": "3"})) == "unknown action: 'clear'"
    assert rows(conn) == []


# ---------- a file input: multipart ----------
#
# A file input's Save posts multipart/form-data to the same route: the
# action and cfg (and, if it likes, payload) as form fields, each file
# under its input's key. The file reaches handle() as a FileUpload in
# payload, in a temp file the host named, deleted once the action is
# done - whatever handle() did. The host never reads it.


@pytest.fixture
def upload_dir(tmp_path, monkeypatch):
    """Where the host puts an upload while its action runs: an empty folder,
    which every test checks is empty again after."""
    path = tmp_path / "uploads"
    path.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(path))
    return path


# An app that notes what handle() was given - a file as the app sees it:
# its name, type, bytes (as latin-1, so any byte reads back), its path
# and whether the file is there - and renders that note with its cfg.
# Told to, it refuses with the note, or crashes after it.
PEEK = """
    import json, os

    SEEN = {}

    def render(ctx, cfg):
        return ctx.empty(json.dumps({"cfg": cfg, "seen": SEEN}, sort_keys=True))

    def handle(ctx, action, payload):
        SEEN.clear()
        for key, value in payload.items():
            if hasattr(value, "path"):
                value = {"name": value.name, "content_type": value.content_type,
                         "bytes": value.bytes.decode("latin-1"), "text": value.text("latin-1"),
                         "path": value.path, "there": os.path.isfile(value.path)}
            SEEN[key] = value
        if action == "refuse":
            raise ValueError(json.dumps(SEEN, sort_keys=True))
        if action == "crash":
            raise RuntimeError("it broke")
"""


def post_form(client, app_id, fields=None, files=None):
    """A multipart action: `fields` as form fields, `files` {key: (bytes, name[, type])}."""
    data = dict(fields or {})
    for key, (content, *rest) in (files or {}).items():
        data[key] = (BytesIO(content), *rest)
    return client.post(f"/api/app/{app_id}/action", data=data, content_type="multipart/form-data")


def seen(resp):
    """What PEEK's handle() was given, from its reply - rendered, or refused with."""
    body = resp.get_json()
    return json.loads(body["spec"]["message"])["seen"] if body["ok"] else json.loads(body["error"])


def test_a_file_reaches_handle_as_a_file_upload_and_is_gone_after(client, apps_dir, upload_dir):
    make_app(apps_dir, "peek", PEEK)

    resp = post_form(client, "peek", {"action": "keep"},
                     {"roster": (b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", "rota.ics", "text/calendar")})

    assert resp.status_code == 200
    got = seen(resp)["roster"]
    path = got.pop("path")
    assert got == {
        "name": "rota.ics", "content_type": "text/calendar",
        "bytes": "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", "text": "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n",
        "there": True,
    }
    # In a temp file the host chose - never named after the file - and
    # deleted once the action was done: nothing uploaded is kept.
    assert os.path.dirname(path) == str(upload_dir)
    assert "rota" not in os.path.basename(path)
    assert not os.path.exists(path)
    assert os.listdir(upload_dir) == []


def test_a_file_is_carried_byte_for_byte_whatever_it_holds(client, apps_dir, upload_dir):
    # The host reads nothing into a file: a binary one - a SQLite
    # database, say - comes through as sent, its type as the browser said.
    make_app(apps_dir, "peek", PEEK)
    blob = b"SQLite format 3\x00" + bytes(range(256)) * 4

    got = seen(post_form(client, "peek", {"action": "keep"},
                         {"db": (blob, "export.db", "application/vnd.sqlite3")}))["db"]

    assert got["bytes"].encode("latin-1") == blob
    assert (got["name"], got["content_type"]) == ("export.db", "application/vnd.sqlite3")
    assert os.listdir(upload_dir) == []


def test_a_multipart_action_carries_its_payload_and_cfg_as_form_fields(client, apps_dir, upload_dir):
    make_app(apps_dir, "peek", PEEK)

    resp = post_form(
        client, "peek",
        {"action": "keep", "payload": json.dumps({"note": "hi", "file": "a name"}), "cfg": json.dumps({"days": 7})},
        {"file": (b"x", "x.txt")},
    )

    # The payload's own entries beside the file - which, under the same
    # key, is the input's value - and the tile rendered with its cfg.
    body = json.loads(resp.get_json()["spec"]["message"])
    assert body["cfg"] == {"days": 7}
    assert body["seen"]["note"] == "hi"
    assert body["seen"]["file"]["bytes"] == "x"


def test_a_multipart_action_needs_no_file(client, apps_dir, upload_dir):
    make_app(apps_dir, "peek", PEEK)

    resp = post_form(client, "peek", {"action": "keep", "cfg": "not json"})

    assert resp.get_json() == {"ok": True, "spec": {"type": "empty", "message": json.dumps({"cfg": {}, "seen": {}})}}


def test_two_files_reach_handle_under_their_keys(client, apps_dir, upload_dir):
    make_app(apps_dir, "peek", PEEK)

    got = seen(post_form(client, "peek", {"action": "keep"}, {"a": (b"one", "a.txt"), "b": (b"two", "b.csv")}))

    assert (got["a"]["bytes"], got["b"]["bytes"]) == ("one", "two")
    assert got["a"]["path"] != got["b"]["path"]
    assert os.listdir(upload_dir) == []


@pytest.mark.parametrize("action", ["refuse", "crash"])
def test_the_upload_is_gone_after_a_refused_or_failed_action(client, conn, apps_dir, upload_dir, action):
    make_app(apps_dir, "peek", PEEK)

    resp = post_form(client, "peek", {"action": action}, {"file": (b"1,2,3", "w.csv")})

    assert_refused(resp)
    if action == "refuse":
        # It was there while handle() ran ...
        got = seen(resp)["file"]
        assert got["there"] is True and got["bytes"] == "1,2,3"
        assert not os.path.exists(got["path"])
    # ... and isn't now.
    assert os.listdir(upload_dir) == []
    assert rows(conn) == []


def test_an_upload_the_app_removed_itself_is_fine(client, apps_dir, upload_dir):
    make_app(apps_dir, "tidy", """
        import os

        def render(ctx, cfg):
            return ctx.empty("tidied")

        def handle(ctx, action, payload):
            os.remove(payload["file"].path)
    """)

    resp = post_form(client, "tidy", {"action": "go"}, {"file": (b"x", "x.txt")})

    assert resp.get_json() == {"ok": True, "spec": {"type": "empty", "message": "tidied"}}
    assert os.listdir(upload_dir) == []


@pytest.mark.parametrize(
    "fields, says",
    [
        ({}, "action must name one, as text"),
        ({"action": ""}, "action must name one, as text"),
        ({"action": "keep", "payload": "[81.2]"}, 'payload must be a mapping, such as {"weight_kg": 81.2}'),
        ({"action": "keep", "payload": "81.2"}, 'payload must be a mapping, such as {"weight_kg": 81.2}'),
        ({"action": "keep", "payload": "{not json"}, 'payload must be a mapping, such as {"weight_kg": 81.2}'),
    ],
)
def test_a_multipart_body_that_isnt_an_action_runs_nothing(client, apps_dir, upload_dir, fields, says):
    # The JSON body's checks, on the form fields - before any file is
    # written anywhere.
    make_app(apps_dir, "peek", PEEK)

    assert assert_refused(post_form(client, "peek", fields, {"file": (b"x", "x.txt")})) == says
    assert os.listdir(upload_dir) == []


def test_the_cap_on_a_request_is_25_mb():
    import main

    assert main.MAX_UPLOAD_BYTES == 25 * 1024 * 1024
    assert main.app.config["MAX_CONTENT_LENGTH"] == main.MAX_UPLOAD_BYTES


def test_a_file_over_the_cap_is_turned_away_unread(client, conn, apps_dir, upload_dir, monkeypatch):
    import main

    install_import(client)
    # A cap of 1 KiB here, so the test needn't send 25 MiB to pass it.
    monkeypatch.setitem(main.app.config, "MAX_CONTENT_LENGTH", 1024)

    resp = post_form(client, "import", {"action": "import"}, {"file": (b"2026-01-03 81.2\n" * 100, "w.txt")})

    # 413, said as any refusal is; handle() never ran, nothing was kept.
    assert resp.status_code == 413
    assert resp.get_json() == {"ok": False, "error": "Request too large (max 25 MB)."}
    assert rows(conn) == []
    assert os.listdir(upload_dir) == []
    # Under the cap, the same file goes through.
    assert post_form(client, "import", {"action": "import"}, {"file": (b"2026-01-03 81.2\n", "w.txt")}).status_code == 200


def test_the_cap_holds_for_a_json_body_too(client, conn, apps_dir, monkeypatch):
    import main

    monkeypatch.setitem(main.app.config, "MAX_CONTENT_LENGTH", 1024)
    make_app(apps_dir, "scale", WRITER)

    resp = act(client, "scale", payload={"metric": WEIGHT_KG, "value": 81.2, "pad": "x" * 2000})

    assert resp.status_code == 413
    assert rows(conn) == []


# ---------- the import app: a file, parsed by the app ----------
#
# Committed in tests/fixtures/apps/import/: a file picker for a
# plain-text file of past weigh-ins, a 'YYYY-MM-DD <kg>' a line. The
# host delivers the file; the app decodes and parses it, and files each
# weigh-in on its own day - not just today - under app:import.


FILE_FIELD = {
    "type": "input", "key": "file", "action": "import", "label": "Import weigh-ins",
    "kind": "file", "unit": None, "options": None, "accept": ".txt,.csv",
}


def install_import(client):
    """Install the import app through the Library, from its committed files."""
    folder = REAL_APPS / "import"
    files = {name: (folder / name).read_text() for name in ("app.yaml", "app.py")}
    resp = client.post("/api/apps", json={"files": files})
    assert resp.status_code == 201, resp.get_json()


def import_file(client, content, name="weigh-ins.txt"):
    return post_form(client, "import", {"action": "import", "cfg": "{}"}, {"file": (content, name, "text/plain")})


def test_the_import_app_installs_and_is_listed(client, apps_dir):
    install_import(client)

    assert client.get("/api/apps").get_json() == [
        {"id": "import", "name": "Import weigh-ins", "default_size": {"w": 1, "h": 1},
         "sizes": [{"w": 1, "h": 1}, {"w": 2, "h": 1}], "reads": ["weight_kg"], "config": [], "metrics": [],
         "files": ["app.py", "app.yaml"]},
    ]


def test_the_import_app_with_no_weigh_ins_says_so_over_its_picker(client, apps_dir):
    install_import(client)

    assert client.post("/api/app/import/render").get_json() == {
        "type": "panel", "children": [{"type": "empty", "message": "No weigh-ins yet"}, FILE_FIELD],
    }


def test_importing_a_file_files_each_weigh_in_on_its_day(client, conn, apps_dir, upload_dir):
    install_import(client)

    resp = import_file(client, b"2026-01-03 81.2\n2026-01-05 80.9\n")

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "spec": {
        "type": "panel", "children": [
            {"type": "number", "value": 2, "label": None, "caption": "since 2026-01-03", "unit": "days"},
            FILE_FIELD,
        ],
    }}
    # Each on the day it names, under the app's own source, and derived.
    assert rows(conn) == [
        ("2026-01-03", "app:import", WEIGHT_KG, 81.2),
        ("2026-01-05", "app:import", WEIGHT_KG, 80.9),
    ]
    assert picked(conn, "2026-01-03", WEIGHT_KG) == (81.2, "app:import")
    assert picked(conn, "2026-01-05", WEIGHT_KG) == (80.9, "app:import")
    assert picked(conn, "2026-01-04", WEIGHT_KG) is None
    assert os.listdir(upload_dir) == []


def test_the_import_app_reads_a_spreadsheets_export(client, conn, apps_dir, upload_dir):
    # Its own parser, its own choices: commas or spaces between, blank
    # lines skipped, CRLF line ends and a leading byte-order mark taken.
    install_import(client)

    resp = import_file(client, "\ufeff2026-01-03,81.2\r\n\r\n  2026-01-04 , 80.9 \r\n2026-01-06\t80\r\n".encode("utf-8"),
                       "export.csv")

    assert resp.status_code == 200, resp.get_json()
    assert rows(conn) == [
        ("2026-01-03", "app:import", WEIGHT_KG, 81.2),
        ("2026-01-04", "app:import", WEIGHT_KG, 80.9),
        ("2026-01-06", "app:import", WEIGHT_KG, 80.0),
    ]


def test_an_imported_day_competes_in_the_pick_and_replaces_its_own(client, conn, apps_dir, upload_dir):
    install_import(client)
    insert(conn, "2026-01-03", "garmin", weight_kg=82.0)
    recompute_derived(conn)

    import_file(client, b"2026-01-03 81.2\n2026-01-04 81.0\n")
    import_file(client, b"2026-01-04 80.5\n2026-01-04 80.4\n")

    # Garmin keeps the day it measured; the app's day replaced - the
    # file's last line for a day twice in it.
    assert rows(conn) == [
        ("2026-01-03", "app:import", WEIGHT_KG, 81.2),
        ("2026-01-03", "garmin", WEIGHT_KG, 82.0),
        ("2026-01-04", "app:import", WEIGHT_KG, 80.4),
    ]
    assert picked(conn, "2026-01-03", WEIGHT_KG) == (82.0, "garmin")
    assert picked(conn, "2026-01-04", WEIGHT_KG) == (80.4, "app:import")


@pytest.mark.parametrize(
    "content, says",
    [
        (b"2026-01-03 81.2\n2026-01-04\n", "line 2: expected 'YYYY-MM-DD <kg>'"),
        (b"2026-01-03 81.2\n2026-01-04 80.9 kg\n", "line 2: expected 'YYYY-MM-DD <kg>'"),
        (b"2026-01-03 81.2\n\n04/01/2026 80.9\n", "line 3: expected 'YYYY-MM-DD <kg>'"),
        (b"date,weight\n2026-01-03,81.2\n", "line 1: expected 'YYYY-MM-DD <kg>'"),
        (b"2026-01-03 heavy\n", "line 1: expected 'YYYY-MM-DD <kg>'"),
        # A line that parses, but no weight: the host's range, said with
        # the app's line number - after line 1 was filed, and rolled back.
        (b"2026-01-03 81.2\n2026-01-04 8000\n", "line 2: weight_kg must be between 20 and 400 kg, got 8000.0"),
        (b"2026-01-03 nan\n", "line 1: value must be a number"),
        (b"\n  \n", "No weigh-ins in w.txt: expected 'YYYY-MM-DD <kg>' on each line"),
        (b"\x89PNG\r\n\x1a\n\x00\xff", "w.txt isn't a text file: expected 'YYYY-MM-DD <kg>' on each line"),
    ],
)
def test_the_import_app_refuses_a_file_that_wont_do_and_writes_nothing(client, conn, apps_dir, upload_dir,
                                                                       content, says):
    install_import(client)

    assert assert_refused(import_file(client, content, "w.txt")) == says
    assert rows(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM derived_metrics").fetchone()[0] == 0
    assert os.listdir(upload_dir) == []


def test_the_import_app_needs_a_file(client, conn, apps_dir):
    install_import(client)

    # No file, or a name where a file should be: nothing to read.
    for resp in (act(client, "import", "import", {}), act(client, "import", "import", {"file": "w.txt"})):
        assert assert_refused(resp) == "Choose a file of weigh-ins to import"
    assert rows(conn) == []


def test_the_import_app_has_one_action(client, conn, apps_dir, upload_dir):
    install_import(client)

    resp = post_form(client, "import", {"action": "clear"}, {"file": (b"2026-01-03 81.2\n", "w.txt")})

    assert assert_refused(resp) == "unknown action: 'clear'"
    assert rows(conn) == []


# ---------- sessions: the workout-log app ----------
#
# handle()'s ctx.write_session() files an interval under the app's own
# source, in the action's one transaction - rolled back with the rest
# when handle() raises. Committed in tests/fixtures/apps/workout-log/: a
# field for how many minutes a workout just finished lasted, filed as a
# workout session that ended now. Nothing is derived from a session, and
# the app doesn't read its workouts back (an app can't read sessions).


def sessions(conn, source, kind="workout"):
    """(id, start, end, summary) of each of `source`'s `kind` sessions, by start."""
    return [
        (r[0], r[1], r[2], None if r[3] is None else json.loads(r[3]))
        for r in conn.execute(
            'SELECT id, "start", "end", summary_json FROM sessions '
            'WHERE source = ? AND kind = ? ORDER BY "start"',
            (source, kind),
        )
    ]


@pytest.mark.parametrize(
    "then, says",
    [
        ('raise RuntimeError("it broke after writing")', "RuntimeError: it broke after writing"),
        ('ctx.write_session("nap", "2026-01-03T13:00:00Z")', "unknown session kind: 'nap'"),
    ],
)
def test_an_action_that_raises_after_a_session_write_rolls_it_back(client, conn, apps_dir, then, says):
    # A crash, or a second write the ctx refuses: the first session goes
    # with it, as a reading would.
    make_app(apps_dir, "rest", f"""
        def render(ctx, cfg):
            return ctx.empty()

        def handle(ctx, action, payload):
            ctx.write_session("sleep", "2026-01-02T23:00:00Z", "2026-01-03T07:00:00Z", {{"asleep_minutes": 450}})
            {then}
    """)

    assert assert_refused(act(client, "rest", "log", {})) == says
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


WORKOUT_LOG_SPEC = {
    "type": "panel", "children": [
        {"type": "empty", "message": "Log a workout with Save"},
        {"type": "input", "key": "minutes", "action": "log", "label": "Workout",
         "kind": "number", "unit": "min", "options": None, "accept": None},
    ],
}


def install_workout_log(client):
    """Install the workout-log app through the Library, from its committed files."""
    folder = REAL_APPS / "workout-log"
    files = {name: (folder / name).read_text() for name in ("app.yaml", "app.py")}
    resp = client.post("/api/apps", json={"files": files})
    assert resp.status_code == 201, resp.get_json()


def log_workout(client, minutes):
    return act(client, "workout-log", "log", {"minutes": minutes})


def instant(stamp):
    """A stored session timestamp - iso_utc()'s, to the second - as a datetime."""
    assert len(stamp) == 20 and stamp.endswith("Z"), stamp
    return datetime.fromisoformat(stamp)


def test_the_workout_log_app_installs_and_is_listed(client, apps_dir):
    install_workout_log(client)

    assert client.get("/api/apps").get_json() == [
        {"id": "workout-log", "name": "Workout log", "default_size": {"w": 1, "h": 1},
         "sizes": [{"w": 1, "h": 1}, {"w": 2, "h": 1}], "reads": [], "config": [], "metrics": [],
         "files": ["app.py", "app.yaml"]},
    ]
    assert client.post("/api/app/workout-log/render").get_json() == WORKOUT_LOG_SPEC


def test_the_workout_log_app_writes_a_session(client, conn, apps_dir):
    install_workout_log(client)

    resp = log_workout(client, 30)
    now = datetime.now(timezone.utc)

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "spec": WORKOUT_LOG_SPEC}
    # One workout under the app's own source: ended now, started its
    # minutes before, its summary the app's.
    [(session_id, start, end, summary)] = sessions(conn, "app:workout-log")
    assert session_id == f"app:workout-log:workout:{start}"
    assert summary == {"type": "manual", "duration_minutes": 30}
    assert instant(end) - instant(start) == timedelta(minutes=30)
    assert timedelta(0) <= now - instant(end) < timedelta(minutes=1)
    # Read back through the contract, beside any other source's ...
    assert client.get("/api/sessions/workout").get_json() == [
        {"start": start, "end": end, "source": "app:workout-log", "summary": summary},
    ]
    # ... and nothing else: no reading, no derived day.
    assert rows(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM derived_metrics").fetchone()[0] == 0


def test_two_workouts_logged_are_two_sessions(client, conn, apps_dir):
    install_workout_log(client)

    log_workout(client, 45)
    log_workout(client, "12.5")   # typed text that is a number

    got = sessions(conn, "app:workout-log")
    assert [s[3]["duration_minutes"] for s in got] == [45, 12.5]
    assert [instant(s[2]) - instant(s[1]) for s in got] == [timedelta(minutes=45), timedelta(seconds=750)]


@pytest.mark.parametrize("junk", ["abc", "", None, True, 0, -30, "nan", "inf", [30], {"min": 30}])
def test_the_workout_log_app_refuses_junk(client, conn, apps_dir, junk):
    install_workout_log(client)

    assert assert_refused(log_workout(client, junk)) == "Enter the workout in minutes, as a positive number"
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


def test_the_workout_log_app_has_one_action(client, conn, apps_dir):
    install_workout_log(client)

    assert assert_refused(act(client, "workout-log", "clear", {"minutes": 30})) == "unknown action: 'clear'"
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0


# ---------- the FRMS app: a roster's .ics, filed as duty sessions ----------
#
# Committed in tests/fixtures/apps/frms/: one 6x4 tile whose roster
# panel has a file picker for a duty roster's .ics, which the app parses
# itself. Every timed event is a duty, filed as a session under app:frms
# - duties_complete if it had ended when the roster was uploaded, else
# duties_rostered - and nothing else: no metric, so no day's hours to
# hold to 24. The parsing, labelling and routing on their own are
# test_frms_app.py's; here, what lands.

ROSTERS = Path(__file__).parent / "fixtures" / "rosters"

ROSTER_PICKER = {
    "type": "input", "key": "file", "action": "upload", "label": "Upload roster",
    "kind": "file", "unit": None, "options": None, "accept": ".ics",
    "toggle": {
        "key": "backfill", "label": "Backfill",
        "confirm": "Backfill past duties into your rostered plan? They're also recorded as "
                   "flown, and any plan you already uploaded for those duties is replaced.",
    },
}


def _roster_cell(spec):
    """The roster panel's spec in the FRMS tile's grid: the sub-panel at x 0, y 2."""
    [cell] = [c for c in spec["cells"] if (c["x"], c["y"]) == (0, 2)]
    return cell["spec"]


def install_frms(client):
    """Install the FRMS app through the Library, from its committed files."""
    folder = REAL_APPS / "frms"
    files = {name: (folder / name).read_text()
             for name in ("app.yaml", "app.py", "config.json", "flash-light.txt", "flash-dark.txt")}
    resp = client.post("/api/apps", json={"files": files})
    assert resp.status_code == 201, resp.get_json()


def upload_roster(client, content, name="roster.ics", payload=None):
    """Upload `content` as the page sends it - a payload, the Backfill
    toggle's state, as a form field of its JSON, when given."""
    if isinstance(content, str):
        content = content.encode()
    fields = {"action": "upload", "cfg": "{}"}
    if payload is not None:
        fields["payload"] = json.dumps(payload)
    return post_form(client, "frms", fields, {"file": (content, name, "text/calendar")})


def sample_roster(year=2025):
    """The synthetic roster (fake flights and ports): its duties in 2025,
    long over - or moved to `year`, 2099 still to come."""
    return (ROSTERS / "sample.ics").read_bytes().replace(b"2025", str(year).encode())


def roster_ics(*events):
    """An .ics of `events`, each (summary, DTSTART, DTEND) - UTC times - or
    (summary, day) for an all-day one."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//OpenFit tests//EN"]
    for summary, *when in events:
        if len(when) == 1:
            times = [f"DTSTART;VALUE=DATE:{when[0]}"]
        else:
            times = [f"DTSTART:{when[0]}", f"DTEND:{when[1]}"]
        lines += ["BEGIN:VEVENT", *times, f"SUMMARY:{summary}", "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# Two duties over and one to come.
MIXED_ROSTER = roster_ics(
    ("19-Nov R1E", "20251119T010000Z", "20251119T050000Z"),             # 4h, over
    ("20-Nov CX9101 ZZA-ZZB", "20251120T100000Z", "20251120T230000Z"),  # 13h, over
    ("19-Nov CX9102 ZZB-ZZA", "20991119T100000Z", "20991120T000000Z"),  # 14h, to come
)


def ics_stamp(stamp):
    """A stored session timestamp as an .ics UTC time: 20251105T151000Z."""
    return instant(stamp).strftime("%Y%m%dT%H%M%SZ")


def duty_rows(conn):
    """(kind, start, end, summary) of every session, by kind then start."""
    return [
        (r[0], r[1], r[2], json.loads(r[3]))
        for r in conn.execute('SELECT kind, "start", "end", summary_json FROM sessions ORDER BY kind, "start"')
    ]


def test_the_frms_app_installs_and_is_listed(client, apps_dir):
    install_frms(client)

    # It reads the two duty kinds and invents no metric.
    assert client.get("/api/apps").get_json() == [
        {"id": "frms", "name": "FRMS", "default_size": {"w": 6, "h": 5}, "sizes": [{"w": 6, "h": 5}],
         "reads": ["duties_rostered", "duties_complete", "sleep"], "config": [], "metrics": [],
         "files": ["app.py", "app.yaml", "config.json", "flash-dark.txt", "flash-light.txt"]},
    ]
    # Nothing yet: it says so, over its picker.
    assert _roster_cell(client.post("/api/app/frms/render").get_json()) == {
        "type": "panel", "children": [{"type": "empty", "message": "Upload your roster .ics"}, ROSTER_PICKER],
    }


def test_the_frms_tile_is_drawn_as_four_sub_panels(client, apps_dir):
    install_frms(client)

    spec = client.post("/api/app/frms/render").get_json()

    # One grid spec, from the render route: the circadian panel - before
    # any duty, the ask, as the roster's - the sleep panel, before any
    # sleep with stages its own ask, the roster's panel 2x2 bottom-left,
    # and the rest budget beside it, before any duty the same ask; under
    # them the flash, bottom right, its two images the files installed.
    folder = REAL_APPS / "frms"
    assert spec == {"type": "grid", "cols": 6, "rows": 5, "cells": [
        {"x": 0, "y": 0, "w": 3, "h": 2, "spec": {"type": "empty", "message": "Upload your roster .ics"}},
        {"x": 3, "y": 0, "w": 3, "h": 2, "spec": {"type": "empty",
                                                   "message": "No sleep stages yet - sync a sleep source that gives them"}},
        {"x": 0, "y": 2, "w": 2, "h": 2, "spec": {"type": "panel", "children": [
            {"type": "empty", "message": "Upload your roster .ics"}, ROSTER_PICKER]}},
        {"x": 2, "y": 2, "w": 4, "h": 2, "spec": {"type": "empty", "message": "Upload your roster .ics"}},
        {"x": 2, "y": 4, "w": 4, "h": 1, "spec": {
            "type": "image", "src": (folder / "flash-light.txt").read_text().strip(),
            "dark": (folder / "flash-dark.txt").read_text().strip(),
            "alt": "OpenFit FRMS - Fatigue Risk Management System", "position": "bottom right"}},
    ]}


def test_frms_is_one_six_by_five_tile(client, apps_dir):
    install_frms(client)

    # One size, the whole suite's tile - so no size picker - and its
    # default: the four panels and the flash's row under them.
    [frms] = client.get("/api/apps").get_json()
    assert frms["sizes"] == [{"w": 6, "h": 5}]
    assert frms["default_size"] == {"w": 6, "h": 5}


def test_an_upload_files_its_sessions_under_app_frms(client, conn, apps_dir, upload_dir):
    install_frms(client)

    assert upload_roster(client, MIXED_ROSTER, payload={"backfill": True}).status_code == 200

    # Every session the app's own source's, its id that source, its kind
    # and its start - the past duties' backfilled copies too.
    got = conn.execute('SELECT id, source, kind, "start" FROM sessions ORDER BY kind, "start"').fetchall()
    assert len(got) == 5
    for id_, source, kind, start in got:
        assert source == "app:frms"
        assert id_ == f"app:frms:{kind}:{start}"
    assert conn.execute("SELECT COUNT(*) FROM sessions WHERE id NOT LIKE 'app:frms:%'").fetchone()[0] == 0


@pytest.mark.parametrize("payload", [None, {"backfill": False}, {"backfill": True}])
def test_an_upload_writes_no_metric(client, conn, apps_dir, upload_dir, payload):
    install_frms(client)

    resp = upload_roster(client, MIXED_ROSTER, payload=payload)

    assert resp.status_code == 200, resp.get_json()
    # Its duties filed, as sessions - the two past ones twice, backfilling ...
    backfill = payload == {"backfill": True}
    assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == (5 if backfill else 3)
    # ... and no reading of anything, under the app's source or any
    # other: duty hours are read from the sessions when they're needed.
    assert conn.execute("SELECT COUNT(*) FROM metrics WHERE source LIKE 'app:frms%'").fetchone()[0] == 0
    assert rows(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM derived_metrics").fetchone()[0] == 0


def test_upload_files_sessions_under_the_two_kinds(client, conn, apps_dir, upload_dir):
    install_frms(client)

    # Long over: every duty is complete ...
    assert upload_roster(client, sample_roster(2025)).status_code == 200
    # ... and still to come, rostered.
    assert upload_roster(client, sample_roster(2099)).status_code == 200

    got = duty_rows(conn)
    assert [(kind, start, s["code"]) for kind, start, _, s in got] == [
        ("duties_complete", "2025-11-05T15:10:00Z", "CX9101"),
        ("duties_complete", "2025-11-08T02:00:00Z", "CX9102"),
        ("duties_complete", "2025-11-09T22:00:00Z", "R1E"),
        ("duties_complete", "2025-11-12T01:00:00Z", "A1534"),
        ("duties_rostered", "2099-11-05T15:10:00Z", "CX9101"),
        ("duties_rostered", "2099-11-08T02:00:00Z", "CX9102"),
        ("duties_rostered", "2099-11-09T22:00:00Z", "R1E"),
        ("duties_rostered", "2099-11-12T01:00:00Z", "A1534"),
    ]
    # Every one under the app's own source, its id its kind and start;
    # the O and G days off nowhere.
    assert {source for (source,) in conn.execute("SELECT DISTINCT source FROM sessions")} == {"app:frms"}
    assert [r[0] for r in conn.execute("SELECT id FROM sessions ORDER BY kind, \"start\"")][:1] == [
        "app:frms:duties_complete:2025-11-05T15:10:00Z",
    ]
    # Read back through the contract, as any source's sessions are.
    assert [(s["start"], s["source"], s["summary"]["type"]) for s in client.get("/api/sessions/duties_rostered").get_json()] == [
        ("2099-11-05T15:10:00Z", "app:frms", "flight"),
        ("2099-11-08T02:00:00Z", "app:frms", "flight"),
        ("2099-11-09T22:00:00Z", "app:frms", "reserve"),
        ("2099-11-12T01:00:00Z", "app:frms", "sim"),
    ]
    assert os.listdir(upload_dir) == []


def test_duty_envelope_is_dtstart_dtend_utc(client, conn, apps_dir, upload_dir):
    install_frms(client)
    text = sample_roster(2025).decode()

    upload_roster(client, text)

    for kind, start, end, summary in duty_rows(conn):
        # The session is DTSTART..DTEND, as the file has them in UTC ...
        assert f"DTSTART:{ics_stamp(start)}\r\nDTEND:{ics_stamp(end)}\r\nSUMMARY:{summary['label']}" in text
        # ... the length the roster states, TOTAL hh:mm in its description.
        hours, minutes = re.search(r"TOTAL (\d\d):(\d\d)", summary["raw"]).groups()
        assert instant(end) - instant(start) == timedelta(hours=int(hours), minutes=int(minutes))
    [reserve] = [r for r in duty_rows(conn) if r[3]["type"] == "reserve"]
    assert reserve[1:3] == ("2025-11-09T22:00:00Z", "2025-11-10T10:00:00Z")


def test_unknown_timed_code_is_still_written(client, conn, apps_dir, upload_dir):
    # Capture first: a code the app has never seen, timed, is a duty.
    install_frms(client)

    resp = upload_roster(client, roster_ics(("20-Nov ZQX9 ZZA", "20251120T010000Z", "20251120T070000Z")))

    assert resp.status_code == 200, resp.get_json()
    assert duty_rows(conn) == [(
        "duties_complete", "2025-11-20T01:00:00Z", "2025-11-20T07:00:00Z",
        {"capture": "live", "type": "other", "code": "ZQX9", "label": "20-Nov ZQX9 ZZA", "raw": ""},
    )]


def test_reupload_replaces_same_duty(client, conn, apps_dir, upload_dir):
    install_frms(client)
    upload_roster(client, sample_roster(2025))
    before = duty_rows(conn)

    # The same roster again, one description changed.
    again = sample_roster(2025).replace(b"R1E - Home reserve", b"R1E - Airport reserve")
    assert upload_roster(client, again).status_code == 200

    # Not doubled: the same start, the same session, its summary replaced.
    after = duty_rows(conn)
    assert len(after) == len(before) == 4
    assert [r[:3] for r in after] == [r[:3] for r in before]
    [reserve] = [r[3] for r in after if r[3]["code"] == "R1E"]
    assert reserve["raw"].startswith("R1E - Airport reserve")


# ---------- backfill: past duties into the plan too ----------
#
# The Backfill toggle beside the picker sends {"backfill": true} with the
# file. A duty already over is then filed into the rostered plan as well
# as the flown record, the rostered copy captured "backfill"; a duty
# still to come is filed once, as without it.


def filed(conn):
    """(kind, start, code, capture) of every session, by kind then start."""
    return [(kind, start, s["code"], s["capture"]) for kind, start, _, s in duty_rows(conn)]


def test_backfill_writes_past_duties_to_rostered_too(client, conn, apps_dir, upload_dir):
    install_frms(client)

    resp = upload_roster(client, MIXED_ROSTER, payload={"backfill": True})

    assert resp.status_code == 200, resp.get_json()
    # Each past duty twice - flown, captured live, and in the plan,
    # captured "backfill" - and the duty to come once, rostered, live.
    assert filed(conn) == [
        ("duties_complete", "2025-11-19T01:00:00Z", "R1E", "live"),
        ("duties_complete", "2025-11-20T10:00:00Z", "CX9101", "live"),
        ("duties_rostered", "2025-11-19T01:00:00Z", "R1E", "backfill"),
        ("duties_rostered", "2025-11-20T10:00:00Z", "CX9101", "backfill"),
        ("duties_rostered", "2099-11-19T10:00:00Z", "CX9102", "live"),
    ]
    # The backfilled copy is the flown duty, its envelope and summary,
    # but for its capture.
    by_kind = {(kind, start): (end, summary) for kind, start, end, summary in duty_rows(conn)}
    for start in ("2025-11-19T01:00:00Z", "2025-11-20T10:00:00Z"):
        (end, flown), (rostered_end, backfilled) = by_kind[("duties_complete", start)], by_kind[("duties_rostered", start)]
        assert rostered_end == end
        assert {**backfilled, "capture": "live"} == flown
    # The duty to come: rostered once, never flown.
    assert [r for r in filed(conn) if r[1].startswith("2099")] == [
        ("duties_rostered", "2099-11-19T10:00:00Z", "CX9102", "live"),
    ]
    # Read back through the contract, as any session is.
    assert [(s["start"], s["summary"]["capture"]) for s in client.get("/api/sessions/duties_rostered").get_json()] == [
        ("2025-11-19T01:00:00Z", "backfill"), ("2025-11-20T10:00:00Z", "backfill"), ("2099-11-19T10:00:00Z", "live"),
    ]
    assert os.listdir(upload_dir) == []


@pytest.mark.parametrize("payload", [
    None,                    # no payload field at all: a page from before the toggle
    {"backfill": False},     # the toggle unticked
    {},
    {"backfill": "true"},    # not the checkbox's JSON bool: no backfill
    {"backfill": 1},
])
def test_without_backfill_past_duties_are_complete_only(client, conn, apps_dir, upload_dir, payload):
    install_frms(client)

    resp = upload_roster(client, MIXED_ROSTER, payload=payload)

    assert resp.status_code == 200, resp.get_json()
    # The plain clock split: over is flown, to come rostered - each duty
    # once, captured live.
    assert filed(conn) == [
        ("duties_complete", "2025-11-19T01:00:00Z", "R1E", "live"),
        ("duties_complete", "2025-11-20T10:00:00Z", "CX9101", "live"),
        ("duties_rostered", "2099-11-19T10:00:00Z", "CX9102", "live"),
    ]


def test_backfill_replaces_a_plan_uploaded_before(client, conn, apps_dir, upload_dir):
    install_frms(client)
    # The plan for CX9101 as an earlier upload filed it, while it was
    # still to come: rostered, live.
    plan = {"capture": "live", "type": "flight", "code": "CX9101", "label": "20-Nov CX9101 ZZA-ZZB",
            "raw": "the plan as it stood"}
    conn.execute(
        'INSERT INTO sessions (id, source, kind, "start", "end", summary_json) VALUES (?, ?, ?, ?, ?, ?)',
        ("app:frms:duties_rostered:2025-11-20T10:00:00Z", "app:frms", "duties_rostered",
         "2025-11-20T10:00:00Z", "2025-11-20T20:00:00Z", json.dumps(plan)),
    )
    conn.commit()

    assert upload_roster(client, MIXED_ROSTER, payload={"backfill": True}).status_code == 200

    # What the confirm warns of: the plan of that duty replaced by the
    # one put back from the file - its envelope, and its capture - not
    # kept beside it.
    rostered = [r for r in duty_rows(conn) if r[0] == "duties_rostered" and r[3]["code"] == "CX9101"]
    assert [(start, end, s["capture"], s["raw"]) for _, start, end, s in rostered] == [
        ("2025-11-20T10:00:00Z", "2025-11-20T23:00:00Z", "backfill", ""),
    ]


def test_a_backfilled_duty_draws_in_the_rostered_lane(client, conn, apps_dir, upload_dir):
    install_frms(client)
    start = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=3)
    end = start + timedelta(hours=12)

    spec = _roster_cell(upload_roster(client, roster_ics(("R1E", ics_at(start), ics_at(end))), payload={"backfill": True}).get_json()["spec"])

    # The past duty on both sides: flown, and in the plan beside it.
    [frame, picker] = spec["children"]
    assert picker == ROSTER_PICKER
    timeline = frame["timeline"]
    block = {"start": stamp(start), "end": stamp(end), "label": "R1E"}
    assert [lane["intervals"] for lane in timeline["lanes"]] == [[block], [block]]


def ics_at(moment):
    """A datetime as an .ics UTC time: 20251105T151000Z."""
    return moment.strftime("%Y%m%dT%H%M%SZ")


def stamp(moment):
    """A datetime as a session's stored timestamp: 2025-11-05T15:10:00Z."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def drawn_now(before, after, spec):
    """The timeline's now, checked to be the render's: between the two
    instants either side of the request, as a session's time is stored."""
    assert stamp(before) <= spec["now"] <= stamp(after), (before, spec["now"], after)
    return spec["now"]


def test_frms_shows_rostered_against_flown_on_a_timeline(client, conn, apps_dir, upload_dir):
    install_frms(client)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    hours = timedelta(hours=1)
    duties = [
        # Flown: forty days ago and three; and one over six months ago,
        # filed, but before the tile's window.
        ("CX9102 ZZB-ZZA", now - timedelta(days=200), 11 * hours),
        ("A1534", now - timedelta(days=40), 4.5 * hours),
        ("CX 9101 ZZA-ZZB RP", now - timedelta(days=3), 13 * hours),
        # Rostered: two days ahead.
        ("R1E", now + timedelta(days=2), 12 * hours),
    ]

    before = datetime.now(timezone.utc)
    resp = upload_roster(client, roster_ics(*((t, ics_at(a), ics_at(a + d)) for t, a, d in duties)))
    after = datetime.now(timezone.utc)

    # The upload's reply is the tile, drawn afresh: the timeline -
    # rostered on the left, flown on the right and greyed after now -
    # each duty a block of its start and end, labelled with its code,
    # oldest first - in the FRMS focus group, between its rails, over
    # the picker at the tile's foot. Behind the flown blocks, a band:
    # three days ago out of base (ZZA) to ZZB, and not back - away, on
    # the layover, until now.
    block = lambda i: {"start": stamp(duties[i][1]), "end": stamp(duties[i][1] + duties[i][2]),
                       "label": ["CX9102", "A1534", "CX9101", "R1E"][i]}
    [frame, picker] = _roster_cell(resp.get_json()["spec"])["children"]
    assert picker == ROSTER_PICKER
    timeline = frame["timeline"]
    now_drawn = drawn_now(before, after, timeline)
    assert timeline == {"type": "timeline", "title": None, "now": now_drawn, "sync": "frms", "lanes": [
        {"label": "Roster", "grey_after_now": False, "intervals": [block(3)]},
        {"label": "Complete", "grey_after_now": True, "intervals": [block(1), block(2)],
         "bands": [{"start": stamp(duties[2][1]), "end": now_drawn, "label": "ZZB"}]},
    ]}
    # That trip, not home, is a card on the Plan rail - no flight home
    # yet - and nothing's home yet to report.
    assert frame["left"] == {"type": "rail", "side": "plan", "title": "Plan", "sync": "frms", "cards": [
        {"id": stamp(duties[2][1]), "start": stamp(duties[2][1]), "end": None,
         "out": "CX9101", "in": None, "port": "ZZB"},
    ]}
    assert frame["right"] == {"type": "rail", "side": "report", "title": "Report", "sync": "frms", "cards": []}
    # The same tile from the render route - the band to its own now.
    before = datetime.now(timezone.utc)
    again = _roster_cell(client.post("/api/app/frms/render").get_json())
    after = datetime.now(timezone.utc)
    assert again["children"][1] == ROSTER_PICKER
    now_again = drawn_now(before, after, again["children"][0]["timeline"])
    rostered, flown = timeline["lanes"]
    assert again["children"][0] == {**frame, "timeline": {**timeline, "now": now_again, "lanes": [
        rostered, {**flown, "bands": [{**flown["bands"][0], "end": now_again}]},
    ]}}


def test_frms_with_only_a_roster_to_come_has_flown_nothing(client, conn, apps_dir, upload_dir):
    install_frms(client)

    spec = _roster_cell(upload_roster(client, sample_roster(2099)).get_json()["spec"])

    # Every duty still to come, however far ahead; nothing flown - an
    # empty lane, still drawn, greyed after now, and with no trip.
    [frame, picker] = spec["children"]
    assert picker == ROSTER_PICKER
    timeline = frame["timeline"]
    rostered, flown = timeline["lanes"]
    assert [(i["start"], i["end"], i["label"]) for i in rostered["intervals"]] == [
        ("2099-11-05T15:10:00Z", "2099-11-06T04:10:00Z", "CX9101"),
        ("2099-11-08T02:00:00Z", "2099-11-08T13:00:00Z", "CX9102"),
        ("2099-11-09T22:00:00Z", "2099-11-10T10:00:00Z", "R1E"),
        ("2099-11-12T01:00:00Z", "2099-11-12T05:30:00Z", "A1534"),
    ]
    assert (rostered["label"], rostered["grey_after_now"]) == ("Roster", False)
    # The roster's one trip, shaded behind its blocks: out of base (ZZA)
    # on CX9101 to ZZB, home on CX9102 - the reserve and the sim after it
    # at home, no part of it.
    assert rostered["bands"] == [{"start": "2099-11-05T15:10:00Z", "end": "2099-11-08T13:00:00Z", "label": "ZZB"}]
    assert flown == {"label": "Complete", "grey_after_now": True, "intervals": []}
    # The same trip, a card to plan for; none to report.
    assert [(c["out"], c["in"], c["port"]) for c in frame["left"]["cards"]] == [("CX9101", "CX9102", "ZZB")]
    assert frame["right"]["cards"] == []


def test_a_roster_flown_over_six_months_ago_draws_as_the_empty_state(client, conn, apps_dir, upload_dir):
    install_frms(client)

    resp = upload_roster(client, sample_roster(2020))

    # Filed - every duty - but all of it before the tile's window, about
    # six months back: nothing to draw, so the tile asks for a roster, as
    # before the first.
    assert len(duty_rows(conn)) == 4
    assert _roster_cell(resp.get_json()["spec"]) == {"type": "panel", "children": [
        {"type": "empty", "message": "Upload your roster .ics"}, ROSTER_PICKER,
    ]}


@pytest.mark.parametrize(
    "content, says",
    [
        # Only days off: nothing timed, so no duties.
        (roster_ics(("03-Nov O", "20251103"), ("04-Nov G", "20251104")), "No duties found in roster.ics"),
        (b"2026-01-03 81.2\n2026-01-04 80.9\n", "No duties found in roster.ics"),
        (b"", "No duties found in roster.ics"),
        (b"\x89PNG\r\n\x1a\n\x00\xff", "roster.ics isn't a text roster"),
        # A time not in UTC refuses the whole file - the duty before it too.
        (roster_ics(("19-Nov R1E", "20251119T010000Z", "20251119T050000Z"))
         .replace("END:VCALENDAR", "BEGIN:VEVENT\r\nDTSTART;TZID=Asia/Hong_Kong:20251120T090000\r\n"
                                   "DTEND;TZID=Asia/Hong_Kong:20251120T170000\r\nSUMMARY:20-Nov R2E\r\n"
                                   "END:VEVENT\r\nEND:VCALENDAR"),
         "roster has a non-UTC time, which isn't supported yet: DTSTART;TZID=Asia/Hong_Kong:20251120T090000"),
    ],
)
def test_a_file_with_no_duties_is_refused(client, conn, apps_dir, upload_dir, content, says):
    install_frms(client)

    assert assert_refused(upload_roster(client, content)) == says
    assert duty_rows(conn) == []
    assert rows(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM derived_metrics").fetchone()[0] == 0
    assert os.listdir(upload_dir) == []


def test_a_day_over_24_hours_of_duty_is_accepted(client, conn, apps_dir, upload_dir):
    install_frms(client)
    # 25 hours of duty starting on one UTC day: a reserve, then a flight
    # out past midnight. Refused when the app filed a day's hours, held
    # to 0-24; with no metric to hold, it's two duties like any.
    content = roster_ics(("19-Nov R1E", "20251119T000000Z", "20251119T120000Z"),
                         ("19-Nov CX9101 ZZA-ZZB", "20251119T120000Z", "20251120T010000Z"))

    resp = upload_roster(client, content)

    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["ok"] is True
    assert [(kind, start, end, s["code"]) for kind, start, end, s in duty_rows(conn)] == [
        ("duties_complete", "2025-11-19T00:00:00Z", "2025-11-19T12:00:00Z", "R1E"),
        ("duties_complete", "2025-11-19T12:00:00Z", "2025-11-20T01:00:00Z", "CX9101"),
    ]
    assert rows(conn) == []


def test_the_frms_app_needs_a_file_and_has_one_action(client, conn, apps_dir, upload_dir):
    install_frms(client)

    for resp in (act(client, "frms", "upload", {}), act(client, "frms", "upload", {"file": "roster.ics"})):
        assert assert_refused(resp) == "Choose a roster .ics to upload"
    resp = post_form(client, "frms", {"action": "clear"}, {"file": (sample_roster(), "roster.ics")})
    assert assert_refused(resp) == "unknown action: 'clear'"
    assert duty_rows(conn) == []


def test_the_duties_are_listed_under_roster(client, conn, apps_dir, upload_dir):
    install_frms(client)
    upload_roster(client, sample_roster(2025))

    catalog = client.get("/api/catalog?include_empty=1").get_json()
    sessions_listed = {s["kind"]: s for s in catalog["sessions"]}
    assert (sessions_listed["duties_complete"]["count"], sessions_listed["duties_rostered"]["count"]) == (4, 0)
    assert {sessions_listed[k]["category"] for k in ("duties_complete", "duties_rostered")} == {"Roster"}
    # The category is the data's: the app adds no metric to it.
    assert [m["metric"] for m in catalog["metrics"] if m["metric"].startswith("app:")] == []
    assert client.get("/api/coverage").get_json()["sessions"]["duties_complete"] == [
        "2025-11-05", "2025-11-08", "2025-11-09", "2025-11-12",
    ]


# ---------- /api/weights retired; 'manual' kept as history ----------


def test_the_weights_routes_are_gone(client):
    import main

    for method in ("get", "post", "delete"):
        assert getattr(client, method)("/api/weights", json={"weight": 80}).status_code == 404, method
    assert not hasattr(main, "MANUAL_SOURCE")


def test_the_page_has_no_weight_tile_and_no_weights_route(client):
    resp = client.get("/")
    page = resp.get_data(as_text=True)
    resp.close()

    assert "/api/weights" not in page
    assert "addWeight" not in page and "weightForm" not in page


def test_manual_weigh_ins_still_read_back_beside_the_apps(client, conn, apps_dir):
    # Frozen, not migrated: the old weigh-ins keep their source - it's
    # where they came from - and the timeline runs on from the app's.
    install_weight(apps_dir)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    insert(conn, "2026-01-01", "manual", weight_kg=82.0)
    insert(conn, yesterday, "manual", weight_kg=81.6)
    recompute_derived(conn)

    log(client, 81.2)

    assert client.get("/api/metric/weight_kg").get_json()["points"] == [
        {"date": "2026-01-01", "value": 82.0, "source": "manual"},
        {"date": yesterday, "value": 81.6, "source": "manual"},
        {"date": TODAY, "value": 81.2, "source": "app:weight"},
    ]
    assert rows(conn)[:2] == [
        ("2026-01-01", "manual", "weight_kg", 82.0),
        (yesterday, "manual", "weight_kg", 81.6),
    ]


def test_weigh_ins_migrated_by_006_still_read_back(db_at_version, monkeypatch):
    """Weigh-ins from before the metrics table, migrated to 'manual' by 006
    and derived by the first boot's backfill, are served as they were."""
    import main

    path = db_at_version(5)
    conn = sqlite3.connect(path)
    conn.executemany(
        "INSERT INTO weights (date, weight) VALUES (?, ?)",
        [("2026-01-02", 81.5), ("2026-01-01", 82), ("2026-01-03", 80.25)],
    )
    conn.commit()
    run_migrations(conn)
    assert backfill_derived(conn)
    conn.close()

    monkeypatch.setitem(main.app.config, "DB_PATH", path)
    assert main.app.test_client().get("/api/metric/weight_kg").get_json()["points"] == [
        {"date": "2026-01-01", "value": 82.0, "source": "manual"},
        {"date": "2026-01-02", "value": 81.5, "source": "manual"},
        {"date": "2026-01-03", "value": 80.25, "source": "manual"},
    ]


def test_nothing_writes_to_manual_any_more(client, conn):
    # Its one writer gone, and the webhook refuses it: history, never
    # added to.
    token = client.get("/api/webhook-token").get_json()["token"]

    resp = client.post(f"/api/webhook/{token}", json={"metric": WEIGHT_KG, "value": 80, "source": "manual"})

    assert assert_refused(resp) == (
        "source 'manual' is kept as history and takes no new readings - name your device instead"
    )
    assert rows(conn) == []
