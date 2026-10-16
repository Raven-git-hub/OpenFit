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

Every test gets its own temp apps folder; nothing reads /data/apps.
"""

import shutil
import sqlite3
import textwrap
from datetime import date, timedelta
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
        ({"metric": "weight_lbs", "value": 180}, "unknown metric: 'weight_lbs'"),
        ({"metric": "heart_rate", "value": 60}, "unknown metric: 'heart_rate'"),
        ({"metric": WEIGHT_KG, "value": True}, "value must be a number"),
        ({"metric": WEIGHT_KG, "value": "81.2"}, "value must be a number"),
        ({"metric": WEIGHT_KG, "value": None}, "value must be a number"),
        ({"metric": WEIGHT_KG, "value": 5000}, "weight_kg must be between 20 and 400 kg, got 5000"),
        ({"metric": WEIGHT_KG, "value": 81, "date": "02/01/2026"}, "date must be YYYY-MM-DD: '02/01/2026'"),
        ({"metric": WEIGHT_KG, "value": 81, "date": 20260102}, "date must be YYYY-MM-DD: 20260102"),
    ],
)
def test_the_ctx_refuses_a_reading_that_wont_do(client, conn, apps_dir, payload, says):
    # The webhook's rule: a canonical key, a finite number within its
    # plausible range, a YYYY-MM-DD date.
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
    ctx = host.WriteCtx(conn, "scale", today=date(2026, 1, 5))

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
    ctx = host.WriteCtx(conn, "scale")
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
    ctx = host.WriteCtx(conn, "scale", today=date(2026, 1, 5))

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
    "label": "Log weight", "kind": "number", "unit": "kg",
}


def test_the_weight_app_loads_as_written(client, apps_dir):
    install_weight(apps_dir)

    assert client.get("/api/apps").get_json() == [
        {"id": "weight", "name": "Weight", "default_size": {"w": 1, "h": 1},
         "sizes": [{"w": 1, "h": 1}, {"w": 2, "h": 1}], "reads": ["weight_kg"], "config": [],
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
