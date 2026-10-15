"""
The app host: installed apps, their render() and the Home tab's routes.

An app is a folder in APPS_DIR holding app.yaml (its manifest) and
app.py (its render(ctx, cfg)). scan_apps() lists the ones that load and
logs and skips the rest; GET /api/apps lists them, and GET
/api/app/<id>/render runs one's render() against a fresh ctx and returns
its display spec - or an error spec, never a 500. ctx.metric() is an
app's one data path: a metric's derived points as a list of Points,
trailing `days` days.
Every test gets its own temp apps folder; nothing reads /data/apps.
"""

import math
import os
import textwrap
from datetime import date, timedelta

import pytest

import apps
import host
from derived import recompute_derived
from metrics import DISTANCE_M, METRICS, STEPS


# The two apps Home was first built for, as they use the host: ctx.metric()
# read as a list of points (data[-1] the latest day, .date/.value on each),
# (date, value) pairs for the line, and config as an ordered list of fields.
# steps: the latest day as a number over a week's sparkline.
STEPS_MANIFEST = """
id: steps
name: Steps
default_size: {w: 2, h: 2}
reads: [steps]
config:
  - {key: days, label: Days shown, type: int, default: 7}
"""

STEPS_APP = """
def render(ctx, cfg):
    data = ctx.metric("steps", cfg.get("days", 7))
    if not data:
        return ctx.empty("No steps yet")
    latest = data[-1]
    caption = "so far today" if latest.date == ctx.today() else latest.date
    return ctx.panel([
        ctx.number(latest.value, label="steps", caption=caption),
        ctx.line([(p.date, p.value) for p in data]),
    ])
"""

# steps-metres: today's steps as metres walked, one number.
METRES_MANIFEST = """
id: steps-metres
name: Distance
default_size: {w: 2, h: 2}
reads: [steps]
config:
  - {key: stride_m, label: Stride length (m), type: float, default: 0.75}
  - {key: label, label: Label, type: text, default: metres today}
"""

METRES_APP = """
def render(ctx, cfg):
    data = ctx.metric("steps", 1)
    if not data:
        return ctx.empty("No steps today")
    metres = round(data[-1].value * cfg.get("stride_m", 0.75))
    return ctx.number(metres, label=cfg.get("label", "metres today"), unit="m")
"""


def make_app(root, folder, manifest="", code=None):
    """An app folder under `root`: app.yaml from `manifest`, app.py from `code`."""
    path = root / folder
    path.mkdir(parents=True)
    if manifest is not None:
        (path / apps.MANIFEST).write_text(textwrap.dedent(manifest))
    if code is not None:
        (path / apps.MODULE).write_text(textwrap.dedent(code))
    return path


@pytest.fixture
def apps_dir(tmp_path, monkeypatch):
    """An empty apps folder, which the app is pointed at."""
    import main

    path = tmp_path / "apps"
    path.mkdir()
    monkeypatch.setitem(main.app.config, "APPS_DIR", str(path))
    return path


def today_minus(n):
    return (date.today() - timedelta(days=n)).isoformat()


def insert(conn, day, source, **values):
    for metric, value in values.items():
        conn.execute(
            "INSERT INTO metrics (date, source, metric, value, unit, synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (day, source, metric, value, METRICS[metric], "2026-01-01 06:00:00"),
        )
    conn.commit()


def ids(found):
    return [a.id for a in found]


# ---------- the scanner ----------


def test_scan_loads_a_valid_app_and_skips_a_malformed_one(apps_dir, capsys):
    make_app(apps_dir, "steps", "id: steps\nname: Steps\n", STEPS_APP)
    make_app(apps_dir, "broken", "id: broken\nname: [unclosed\n", STEPS_APP)

    assert ids(apps.scan_apps(str(apps_dir))) == ["steps"]
    log = capsys.readouterr().out
    assert "[apps] skipping" in log and "broken" in log


def test_a_loaded_app_carries_its_manifest_and_render(apps_dir):
    make_app(
        apps_dir,
        "steps",
        """
        id: steps
        name: Steps
        default_size: {w: 3, h: 2}
        reads: [steps]
        config:
          - {key: days, label: Days shown, type: int, default: 7}
          - {key: goal, label: Daily goal, type: int, default: 10000}
        """,
        STEPS_APP,
    )

    [app] = apps.scan_apps(str(apps_dir))
    assert (app.id, app.name, app.default_size) == ("steps", "Steps", {"w": 3, "h": 2})
    assert app.reads == ["steps"]
    # In the manifest's order: it's the order the config form will use.
    assert app.config == [
        {"key": "days", "label": "Days shown", "type": "int", "default": 7},
        {"key": "goal", "label": "Daily goal", "type": "int", "default": 10000},
    ]
    assert app.path == str(apps_dir / "steps")
    assert callable(app.render)


def test_a_manifest_can_leave_everything_but_render_to_the_defaults(apps_dir):
    # The id defaults to the folder's name, the name to the id.
    make_app(apps_dir, "steps", "", STEPS_APP)

    [app] = apps.scan_apps(str(apps_dir))
    assert (app.id, app.name, app.default_size) == ("steps", "steps", {"w": 2, "h": 2})
    assert (app.reads, app.config) == ([], [])


MALFORMED = {
    "unparseable yaml": ("name: [unclosed\n", STEPS_APP),
    "manifest not a mapping": ("- a\n- list\n", STEPS_APP),
    "no app.py": ("name: Steps\n", None),
    "app.py syntax error": ("name: Steps\n", "def render(ctx, cfg)\n    return 1\n"),
    "app.py raises on import": ("name: Steps\n", "raise RuntimeError('no')\n"),
    "no render": ("name: Steps\n", "def draw(ctx, cfg):\n    return None\n"),
    "render not callable": ("name: Steps\n", "render = 'a tile'\n"),
    "id with a slash": ("id: a/b\n", STEPS_APP),
    "id with a space": ("id: my app\n", STEPS_APP),
    "id not a string": ("id: 5\n", STEPS_APP),
    "empty name": ("name: ''\n", STEPS_APP),
    "name not a string": ("name: [a]\n", STEPS_APP),
    "default_size not a mapping": ("default_size: 2\n", STEPS_APP),
    "default_size missing h": ("default_size: {w: 2}\n", STEPS_APP),
    "default_size zero": ("default_size: {w: 0, h: 2}\n", STEPS_APP),
    "default_size too big": ("default_size: {w: 2, h: 13}\n", STEPS_APP),
    "default_size a string": ("default_size: {w: '2', h: 2}\n", STEPS_APP),
    "default_size a bool": ("default_size: {w: true, h: 2}\n", STEPS_APP),
    "default_size a float": ("default_size: {w: 2.5, h: 2}\n", STEPS_APP),
    "reads not a list": ("reads: steps\n", STEPS_APP),
    "reads not strings": ("reads: [1, 2]\n", STEPS_APP),
    "config a mapping": ("config: {days: 7}\n", STEPS_APP),
    "config a string": ("config: days\n", STEPS_APP),
    "config field not a mapping": ("config: [days]\n", STEPS_APP),
    "config field with no key": ("config: [{label: Days}]\n", STEPS_APP),
    "config field key not a string": ("config: [{key: 7}]\n", STEPS_APP),
    "config field listed twice": ("config: [{key: days}, {key: days}]\n", STEPS_APP),
}


@pytest.mark.parametrize("manifest, code", MALFORMED.values(), ids=MALFORMED.keys())
def test_a_malformed_app_is_logged_and_skipped(apps_dir, capsys, manifest, code):
    make_app(apps_dir, "bad", manifest, code)
    make_app(apps_dir, "good", "name: Good\n", STEPS_APP)

    assert ids(apps.scan_apps(str(apps_dir))) == ["good"]
    assert f"[apps] skipping {apps_dir / 'bad'}" in capsys.readouterr().out


def test_a_folder_without_a_manifest_is_not_an_app(apps_dir, capsys):
    make_app(apps_dir, "notes", None, STEPS_APP)
    make_app(apps_dir, ".hidden", "name: Hidden\n", STEPS_APP)
    (apps_dir / "README.txt").write_text("not an app")

    assert apps.scan_apps(str(apps_dir)) == []
    # Not an app, so nothing to warn about.
    assert "[apps]" not in capsys.readouterr().out


def test_a_missing_apps_folder_has_no_apps(tmp_path):
    assert apps.scan_apps(str(tmp_path / "nowhere")) == []


def test_apps_are_listed_in_folder_order(apps_dir):
    for folder in ("zeta", "alpha", "mid"):
        make_app(apps_dir, folder, "", STEPS_APP)

    assert ids(apps.scan_apps(str(apps_dir))) == ["alpha", "mid", "zeta"]


def test_two_apps_claiming_one_id_keep_the_first_folder(apps_dir, capsys):
    make_app(apps_dir, "a-steps", "id: steps\nname: First\n", STEPS_APP)
    make_app(apps_dir, "b-steps", "id: steps\nname: Second\n", STEPS_APP)

    [app] = apps.scan_apps(str(apps_dir))
    assert app.name == "First"
    assert "'steps' is already taken" in capsys.readouterr().out


def test_an_edited_app_is_reloaded_and_a_broken_one_logged_once(apps_dir, capsys):
    folder = make_app(apps_dir, "steps", "name: Steps\n", "def render(ctx, cfg)\n")
    assert apps.scan_apps(str(apps_dir)) == []
    assert apps.scan_apps(str(apps_dir)) == []
    # Logged when it was loaded - not again on the second scan.
    assert capsys.readouterr().out.count("[apps] skipping") == 1

    (folder / apps.MODULE).write_text(STEPS_APP)
    assert ids(apps.scan_apps(str(apps_dir))) == ["steps"]

    (folder / apps.MANIFEST).write_text("name: Steps, renamed\n")
    assert apps.scan_apps(str(apps_dir))[0].name == "Steps, renamed"


def test_find_app_by_id(apps_dir):
    make_app(apps_dir, "steps", "", STEPS_APP)

    assert apps.find_app(str(apps_dir), "steps").id == "steps"
    assert apps.find_app(str(apps_dir), "nope") is None


# ---------- GET /api/apps ----------


def test_api_apps_lists_the_installed_apps(client, apps_dir):
    make_app(apps_dir, "steps", "id: steps\nname: Steps\ndefault_size: {w: 2, h: 2}\n", STEPS_APP)
    make_app(apps_dir, "steps-metres", "name: Distance\ndefault_size: {w: 2, h: 1}\n", METRES_APP)
    make_app(apps_dir, "broken", "name: Broken\n", None)

    resp = client.get("/api/apps")
    assert resp.status_code == 200
    assert resp.get_json() == [
        {"id": "steps", "name": "Steps", "default_size": {"w": 2, "h": 2}},
        {"id": "steps-metres", "name": "Distance", "default_size": {"w": 2, "h": 1}},
    ]


def test_api_apps_with_none_installed(client, apps_dir):
    assert client.get("/api/apps").get_json() == []


# ---------- GET /api/app/<id>/render ----------


def test_the_first_two_apps_are_listed_and_both_render_specs(client, conn, apps_dir):
    # Home's acceptance: steps and steps-metres both listed, and both
    # rendering their specs over live data - neither an error tile.
    make_app(apps_dir, "steps", STEPS_MANIFEST, STEPS_APP)
    make_app(apps_dir, "steps-metres", METRES_MANIFEST, METRES_APP)
    insert(conn, today_minus(1), "garmin", steps=8000)
    insert(conn, today_minus(0), "garmin", steps=4000)
    recompute_derived(conn)

    assert client.get("/api/apps").get_json() == [
        {"id": "steps", "name": "Steps", "default_size": {"w": 2, "h": 2}},
        {"id": "steps-metres", "name": "Distance", "default_size": {"w": 2, "h": 2}},
    ]
    steps = client.get("/api/app/steps/render").get_json()
    assert [child["type"] for child in steps["children"]] == ["number", "line"]
    assert client.get("/api/app/steps-metres/render").get_json() == {
        "type": "number", "value": 3000, "label": "metres today", "caption": None, "unit": "m",
    }


def test_render_returns_the_apps_spec_over_live_data(client, conn, apps_dir):
    make_app(apps_dir, "steps", "name: Steps\n", STEPS_APP)
    insert(conn, today_minus(8), "garmin", steps=1000)   # outside the week
    insert(conn, today_minus(2), "garmin", steps=8000)
    insert(conn, today_minus(0), "garmin", steps=4500)
    insert(conn, today_minus(0), "google_health", steps=4400)
    recompute_derived(conn)

    resp = client.get("/api/app/steps/render")
    assert resp.status_code == 200
    assert resp.get_json() == {
        "type": "panel",
        "children": [
            {"type": "number", "value": 4500, "label": "steps",
             "caption": "so far today", "unit": None},
            {"type": "line", "points": [
                {"date": today_minus(2), "value": 8000},
                {"date": today_minus(0), "value": 4500},
            ]},
        ],
    }


def test_render_with_no_data_is_the_apps_empty_spec(client, apps_dir):
    make_app(apps_dir, "steps", "", STEPS_APP)

    assert client.get("/api/app/steps/render").get_json() == {
        "type": "empty", "message": "No steps yet",
    }


def test_render_of_an_unknown_app_is_404(client, apps_dir):
    resp = client.get("/api/app/nope/render")
    assert resp.status_code == 404
    assert resp.get_json() == {"ok": False, "error": "unknown app: nope"}


def test_render_of_a_malformed_app_is_404(client, apps_dir):
    make_app(apps_dir, "broken", "", "def render(ctx, cfg)\n")

    assert client.get("/api/app/broken/render").status_code == 404


def test_a_render_that_raises_is_an_error_spec_not_a_500(client, apps_dir, capsys):
    make_app(apps_dir, "raises", "", "def render(ctx, cfg):\n    raise KeyError('series')\n")

    resp = client.get("/api/app/raises/render")
    assert resp.status_code == 200
    assert resp.get_json() == {"type": "error", "message": "KeyError: 'series'"}
    log = capsys.readouterr()
    assert "[apps] raises: render failed" in log.out
    # With the traceback, pointing into the app.
    assert "app.py" in log.err


def test_an_app_asking_for_an_unknown_metric_is_an_error_spec(client, apps_dir):
    make_app(apps_dir, "typo", "", "def render(ctx, cfg):\n    return ctx.metric('stepz')\n")

    assert client.get("/api/app/typo/render").get_json() == {
        "type": "error", "message": "ValueError: unknown metric: 'stepz'",
    }


@pytest.mark.parametrize(
    "returned, message",
    [
        ("None", "render returned NoneType, not a spec"),
        ("[1, 2]", "render returned list, not a spec"),
        ("{'value': 1}", "render returned dict, not a spec"),
        ("{'type': 5}", "render returned dict, not a spec"),
        ("{'type': 'number', 'value': float('nan')}", "render returned a spec that isn't JSON"),
        ("{'type': 'number', 'value': {1, 2}}", "render returned a spec that isn't JSON"),
    ],
)
def test_a_render_that_returns_no_spec_is_an_error_spec(client, apps_dir, returned, message):
    make_app(apps_dir, "odd", "", f"def render(ctx, cfg):\n    return {returned}\n")

    resp = client.get("/api/app/odd/render")
    assert resp.status_code == 200
    spec = resp.get_json()
    assert spec["type"] == "error"
    assert spec["message"].startswith(message)


def test_render_is_given_an_empty_cfg(client, apps_dir):
    make_app(apps_dir, "cfg", "config: [{key: days, default: 7}]\n",
             "def render(ctx, cfg):\n    return ctx.number(len(cfg), label=repr(cfg))\n")

    assert client.get("/api/app/cfg/render").get_json()["label"] == "{}"


# ---------- ctx ----------


TODAY = date(2026, 10, 15)


def test_ctx_metric_is_a_list_of_the_derived_points_oldest_first(conn):
    insert(conn, "2026-10-14", "garmin", steps=9000)
    insert(conn, "2026-10-14", "google_health", steps=8000)
    insert(conn, "2026-10-12", "google_health", steps=7000)
    recompute_derived(conn)

    data = host.Ctx(conn, today=TODAY).metric(STEPS)
    assert isinstance(data, list)
    assert data == [
        host.Point("2026-10-12", 7000, "google_health"),
        host.Point("2026-10-14", 9000, "garmin"),
    ]
    # The latest day is the last, read by attribute.
    latest = data[-1]
    assert (latest.date, latest.value, latest.source) == ("2026-10-14", 9000, "garmin")


def test_ctx_metric_with_no_data_is_an_empty_list(conn):
    assert host.Ctx(conn).metric(STEPS, 7) == []


@pytest.mark.parametrize(
    "days, dates",
    [
        (1, ["2026-10-15"]),
        (2, ["2026-10-14", "2026-10-15"]),
        (4, ["2026-10-12", "2026-10-14", "2026-10-15"]),
        (None, ["2026-10-01", "2026-10-12", "2026-10-14", "2026-10-15", "2026-10-16"]),
    ],
)
def test_ctx_metric_keeps_the_trailing_days(conn, days, dates):
    # A day with no reading (10-13) is missing, not a zero, and a reading
    # dated after today is outside any trailing window.
    for day in ("2026-10-01", "2026-10-12", "2026-10-14", "2026-10-15", "2026-10-16"):
        insert(conn, day, "garmin", steps=1)
    recompute_derived(conn)

    assert [p.date for p in host.Ctx(conn, today=TODAY).metric(STEPS, days)] == dates


@pytest.mark.parametrize("days", [0, -1, True, 2.0, "7"])
def test_ctx_metric_rejects_days_that_arent_a_count(conn, days):
    with pytest.raises(ValueError, match="days"):
        host.Ctx(conn).metric(STEPS, days)


def test_ctx_metric_rejects_a_key_outside_the_vocabulary(conn):
    with pytest.raises(ValueError, match="unknown metric"):
        host.Ctx(conn).metric("heart_rate")


def test_ctx_today_is_the_iso_date(conn):
    assert host.Ctx(conn, today=TODAY).today() == "2026-10-15"
    assert host.Ctx(conn).today() == date.today().isoformat()


def test_number_spec():
    assert host.number(8432, label="steps", caption="so far today", unit="count") == {
        "type": "number", "value": 8432, "label": "steps",
        "caption": "so far today", "unit": "count",
    }
    assert host.number(None) == {
        "type": "number", "value": None, "label": None, "caption": None, "unit": None,
    }
    assert host.number(math.inf)["value"] is None


def test_line_spec_takes_date_value_pairs_and_keeps_gaps():
    assert host.line([
        ("2026-10-14", 9000.0),
        ("2026-10-15", None),
        (date(2026, 10, 16), math.nan),
        ["2026-10-17", 7000],
    ]) == {"type": "line", "points": [
        {"date": "2026-10-14", "value": 9000.0},
        {"date": "2026-10-15", "value": None},
        {"date": "2026-10-16", "value": None},
        {"date": "2026-10-17", "value": 7000},
    ]}
    assert host.line([]) == {"type": "line", "points": []}


def test_line_spec_takes_points_dicts_or_bare_numbers():
    # ctx.metric()'s points go straight in; their source is dropped.
    assert host.line([host.Point("2026-10-14", 9000.0, "garmin")]) == {
        "type": "line", "points": [{"date": "2026-10-14", "value": 9000.0}],
    }
    assert host.line([
        {"date": "2026-10-14", "value": 9000, "source": "garmin"},
        {"value": None},
    ]) == {"type": "line", "points": [{"date": "2026-10-14", "value": 9000}, {"value": None}]}
    assert host.line([1, None, 2.5, math.nan]) == {"type": "line", "points": [1, None, 2.5, None]}


@pytest.mark.parametrize(
    "bad",
    [
        True,
        "9000",
        {"date": "2026-10-14"},
        [1, 2],                          # a pair's date is YYYY-MM-DD
        ("2026-10-14",),                 # a pair is two
        ("2026-10-14", 9000, "garmin"),
        ("2026-10-14", "9000"),
    ],
)
def test_line_spec_rejects_what_isnt_a_value(bad):
    with pytest.raises(TypeError):
        host.line([bad])


def test_panel_spec_stacks_its_children_in_order():
    a, b = host.number(1), host.line([1, 2])
    expected = {"type": "panel", "children": [a, b]}
    assert host.panel(a, b) == expected
    assert host.panel([a, b]) == expected
    with pytest.raises(TypeError):
        host.panel(a, "not a spec")


def test_empty_spec():
    assert host.empty("No steps yet") == {"type": "empty", "message": "No steps yet"}
    assert host.empty() == {"type": "empty", "message": "No data yet"}


def test_the_ctx_carries_the_builders(conn):
    ctx = host.Ctx(conn)
    assert ctx.panel(ctx.number(1), ctx.line([1]), ctx.empty())["type"] == "panel"


def test_render_app_passes_today_and_a_copy_of_cfg(conn, apps_dir):
    make_app(apps_dir, "echo", "", """
        def render(ctx, cfg):
            cfg["seen"] = True
            return ctx.number(None, label=ctx.today(), caption=str(sorted(cfg)))
    """)
    [app] = apps.scan_apps(str(apps_dir))
    cfg = {"days": 7}

    spec = host.render_app(app, conn, cfg=cfg, today=TODAY)
    assert (spec["label"], spec["caption"]) == ("2026-10-15", "['days', 'seen']")
    assert cfg == {"days": 7}


# ---------- ?days= on the access contract ----------


def seed_trailing(conn, n=10):
    for back in range(n):
        insert(conn, today_minus(back), "garmin", steps=1000 + back, distance_m=500 + back)
    recompute_derived(conn)


@pytest.mark.parametrize("days", [1, 3, 7])
def test_metric_days_returns_the_trailing_days(client, conn, days):
    seed_trailing(conn)

    resp = client.get(f"/api/metric/{STEPS}?days={days}")
    assert resp.status_code == 200
    assert [p["date"] for p in resp.get_json()["points"]] == [
        today_minus(back) for back in reversed(range(days))
    ]


def test_metric_days_counts_days_not_points(client, conn):
    # A missing day still counts towards the window: fewer points, never
    # an older one pulled in to make up the number.
    for back in (0, 1, 4, 6, 7, 9):
        insert(conn, today_minus(back), "garmin", steps=1)
    insert(conn, (date.today() + timedelta(days=1)).isoformat(), "garmin", steps=1)
    recompute_derived(conn)

    points = client.get(f"/api/metric/{STEPS}?days=7").get_json()["points"]
    assert [p["date"] for p in points] == [today_minus(b) for b in (6, 4, 1, 0)]


def test_metric_days_larger_than_the_history_is_all_of_it(client, conn):
    seed_trailing(conn, n=3)

    for days in (365, 999999999):
        points = client.get(f"/api/metric/{DISTANCE_M}?days={days}").get_json()["points"]
        assert len(points) == 3


def test_an_empty_days_is_the_same_as_none(client, conn):
    seed_trailing(conn)

    assert len(client.get(f"/api/metric/{STEPS}?days=").get_json()["points"]) == 10


@pytest.mark.parametrize("days", ["0", "-1", "x", "1.5", "7d", "1000000000", "٣"])
def test_metric_days_must_be_a_positive_count(client, days):
    resp = client.get(f"/api/metric/{STEPS}?days={days}")
    assert resp.status_code == 400
    assert resp.get_json() == {
        "ok": False, "error": "days must be a whole number from 1 to 999999999",
    }


@pytest.mark.parametrize("query", ["?days=7&from=2026-01-01", "?days=7&to=2026-01-01"])
def test_metric_days_cant_be_combined_with_from_or_to(client, query):
    resp = client.get(f"/api/metric/{STEPS}{query}")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == "days can't be combined with from or to"


def test_days_narrows_every_contract_read(client, conn):
    # One parser for every read's range, so ?days= means the same on each.
    seed_trailing(conn, n=5)

    coverage = client.get("/api/coverage?days=2").get_json()
    assert coverage["metrics"][STEPS] == [today_minus(1), today_minus(0)]
    for route in ("/api/sessions/sleep?days=x", "/api/series/heart_rate?days=0"):
        assert client.get(route).status_code == 400


# ---------- the image ----------


def test_the_image_carries_every_module():
    # app/Dockerfile copies the modules by name: one left off the list
    # imports fine here and fails only when the container starts.
    app_dir = os.path.dirname(os.path.abspath(apps.__file__))
    with open(os.path.join(app_dir, "Dockerfile")) as f:
        copied = next(line for line in f if line.startswith("COPY main.py")).split()[1:-1]
    modules = sorted(name for name in os.listdir(app_dir) if name.endswith(".py"))
    assert sorted(copied) == modules
