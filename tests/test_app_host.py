"""
The app host: installed apps, their render() and the Home tab's routes.

An app is a folder in APPS_DIR holding app.yaml (its manifest) and,
usually, app.py (its render(ctx, cfg)). scan_apps() lists the ones that
load and logs and skips the rest; GET /api/apps lists them, and POST
/api/app/<id>/render runs one's render() against a fresh ctx and the
cfg it's sent, and returns its display spec - or an error spec, never a
500. The ctx is an
app's one data path: ctx.metric() a metric's derived points as a list of
Points, trailing `days` days, and ctx.sessions() a session kind's
sessions, as GET /api/sessions/<kind> serves them. An app's handle()
writes through a WriteCtx; its
write_session() - a session under the app's own source - is tested here,
its route round trip in test_app_actions.py.
Every test gets its own temp apps folder; nothing reads /data/apps.
"""

import json
import math
import os
import re
import shutil
import textwrap
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import apps
import host
from derived import recompute_derived, set_source_role
from metrics import DISTANCE_M, DUTIES_COMPLETE, METRICS, SLEEP, STEPS, WORKOUT
from plugins.base import write_session


# The two real apps Home was built for, steps and steps-metres, committed
# in tests/fixtures/apps/ as they're deployed in /data/apps, so every run
# checks the host against the genuine apps. They are correct per the
# contract: when one fails here, fix the host, never these files. Their
# app.py is verbatim; their app.yaml declares sizes: [{ w: 1, h: 1 }], the
# canonical form, where the deployed copies still have the older
# default_size: { w: 2, h: 2 } - which the host keeps loading
# (test_the_deployed_manifests_still_load).
REAL_APPS = Path(__file__).parent / "fixtures" / "apps"


def real(folder, name):
    return (REAL_APPS / folder / name).read_text(encoding="utf-8")


STEPS_APP = real("steps", apps.MODULE)
METRES_APP = real("steps-metres", apps.MODULE)


def install_real_apps(apps_dir):
    """Copy both real app folders into `apps_dir`, byte for byte."""
    for folder in ("steps", "steps-metres"):
        shutil.copytree(
            REAL_APPS / folder, apps_dir / folder,
            ignore=shutil.ignore_patterns("__pycache__"),
        )


# Every app declares a size - one with none doesn't load - so the tests
# that aren't about size give their apps this one.
SIZE = "sizes: [{w: 1, h: 1}]\n"


def make_app(root, folder, manifest=SIZE, code=None):
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
    make_app(apps_dir, "steps", "id: steps\nname: Steps\n" + SIZE, STEPS_APP)
    make_app(apps_dir, "broken", SIZE + "id: broken\nname: [unclosed\n", STEPS_APP)

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
    # default_size alone is the older form of sizes: a list of one.
    assert app.sizes == [{"w": 3, "h": 2}]
    assert app.reads == ["steps"]
    # In the manifest's order: it's the order the config form will use.
    assert app.config == [
        {"key": "days", "label": "Days shown", "type": "int", "default": 7},
        {"key": "goal", "label": "Daily goal", "type": "int", "default": 10000},
    ]
    assert app.path == str(apps_dir / "steps")
    assert callable(app.render)


def test_a_manifest_can_leave_everything_but_its_size_to_the_defaults(apps_dir):
    # The id defaults to the folder's name, the name to the id. The size
    # has no default: the host never makes one up.
    make_app(apps_dir, "steps", SIZE, STEPS_APP)

    [app] = apps.scan_apps(str(apps_dir))
    assert (app.id, app.name, app.default_size) == ("steps", "steps", {"w": 1, "h": 1})
    assert app.sizes == [{"w": 1, "h": 1}]
    assert (app.reads, app.config) == ([], [])


MALFORMED = {
    "unparseable yaml": (SIZE + "name: [unclosed\n", STEPS_APP),
    "manifest not a mapping": ("- a\n- list\n", STEPS_APP),
    "app.py syntax error": (SIZE + "name: Steps\n", "def render(ctx, cfg)\n    return 1\n"),
    "app.py raises on import": (SIZE + "name: Steps\n", "raise RuntimeError('no')\n"),
    "no render": (SIZE + "name: Steps\n", "def draw(ctx, cfg):\n    return None\n"),
    "render not callable": (SIZE + "name: Steps\n", "render = 'a tile'\n"),
    "id with a slash": (SIZE + "id: a/b\n", STEPS_APP),
    "id with a space": (SIZE + "id: my app\n", STEPS_APP),
    "id not a string": (SIZE + "id: 5\n", STEPS_APP),
    "empty name": (SIZE + "name: ''\n", STEPS_APP),
    "name not a string": (SIZE + "name: [a]\n", STEPS_APP),
    "reads not a list": (SIZE + "reads: steps\n", STEPS_APP),
    "reads not strings": (SIZE + "reads: [1, 2]\n", STEPS_APP),
    "config a mapping": (SIZE + "config: {days: 7}\n", STEPS_APP),
    "config a string": (SIZE + "config: days\n", STEPS_APP),
    "config field not a mapping": (SIZE + "config: [days]\n", STEPS_APP),
    "config field with no key": (SIZE + "config: [{label: Days}]\n", STEPS_APP),
    "config field key not a string": (SIZE + "config: [{key: 7}]\n", STEPS_APP),
    "config field listed twice": (SIZE + "config: [{key: days}, {key: days}]\n", STEPS_APP),
    # A field is sent to the page as written (GET /api/apps), so it must
    # be plain JSON: one that isn't would fail the whole listing.
    "config default a YAML date": (SIZE + "config: [{key: start, default: 2026-01-01}]\n", STEPS_APP),
    "config default not a number": (SIZE + "config: [{key: stride, type: number, default: .nan}]\n", STEPS_APP),
    "config default binary": (SIZE + "config: [{key: blob, default: !!binary aGk=}]\n", STEPS_APP),
}

# Manifests whose size won't do, and why: each app is skipped with the reason.
BAD_SIZES = {
    "no size at all": ("name: Steps\n", "no tile size: an app declares the sizes it's drawn at"),
    "an empty manifest": ("", "no tile size"),
    "sizes empty": ("sizes: []\n", "sizes must be a list of one or more {w, h}"),
    "sizes a mapping": ("sizes: {w: 1, h: 1}\n", "sizes must be a list of one or more {w, h}"),
    "sizes a number": ("sizes: 2\n", "sizes must be a list of one or more {w, h}"),
    "a size not a mapping": ("sizes: [{w: 1, h: 1}, 2]\n", "sizes[1] must be a mapping of w and h"),
    "a size missing h": ("sizes: [{w: 2}]\n", "sizes[0].h must be a whole number from 1 to 6"),
    "a size zero": ("sizes: [{w: 0, h: 1}]\n", "sizes[0].w must be a whole number from 1 to 6"),
    "a size wider than the grid": ("sizes: [{w: 7, h: 1}]\n", "sizes[0].w must be a whole number from 1 to 6"),
    "a size taller than six": ("sizes: [{w: 1, h: 1}, {w: 1, h: 7}]\n", "sizes[1].h must be a whole number from 1 to 6"),
    "a size a string": ("sizes: [{w: '2', h: 2}]\n", "sizes[0].w must be a whole number"),
    "a size a bool": ("sizes: [{w: true, h: 2}]\n", "sizes[0].w must be a whole number"),
    "a size a float": ("sizes: [{w: 2.5, h: 2}]\n", "sizes[0].w must be a whole number"),
    "a size listed twice": ("sizes: [{w: 1, h: 1}, {w: 2, h: 1}, {w: 1, h: 1}]\n", "sizes lists 1×1 twice"),
    "YAML with no space after a colon": ("sizes: [{w:1,h:1}]\n", "sizes[0] needs a space after each colon"),
    "default_size not a mapping": ("default_size: 2\n", "default_size must be a mapping of w and h"),
    "default_size missing h": ("default_size: {w: 2}\n", "default_size.h must be a whole number from 1 to 6"),
    "default_size zero": ("default_size: {w: 0, h: 2}\n", "default_size.w must be a whole number"),
    "default_size too big": ("default_size: {w: 2, h: 7}\n", "default_size.h must be a whole number from 1 to 6"),
    "default_size twelve wide": ("default_size: {w: 12, h: 1}\n", "default_size.w must be a whole number from 1 to 6"),
    "default_size a bool": ("default_size: {w: true, h: 2}\n", "default_size.w must be a whole number"),
    "default_size not the first of sizes": (
        "sizes: [{w: 1, h: 1}, {w: 2, h: 2}]\ndefault_size: {w: 2, h: 2}\n",
        "default_size must be the first of sizes",
    ),
}


@pytest.mark.parametrize("manifest, code", MALFORMED.values(), ids=MALFORMED.keys())
def test_a_malformed_app_is_logged_and_skipped(apps_dir, capsys, manifest, code):
    make_app(apps_dir, "bad", manifest, code)
    make_app(apps_dir, "good", "name: Good\n" + SIZE, STEPS_APP)

    assert ids(apps.scan_apps(str(apps_dir))) == ["good"]
    assert f"[apps] skipping {apps_dir / 'bad'}" in capsys.readouterr().out


@pytest.mark.parametrize("manifest, says", BAD_SIZES.values(), ids=BAD_SIZES.keys())
def test_an_app_without_a_size_it_can_have_is_skipped(apps_dir, capsys, manifest, says):
    # Won't deploy without a size: the scanner leaves it out, saying why.
    make_app(apps_dir, "bad", manifest, STEPS_APP)
    make_app(apps_dir, "good", "name: Good\n" + SIZE, STEPS_APP)

    assert ids(apps.scan_apps(str(apps_dir))) == ["good"]
    log = capsys.readouterr().out
    assert f"[apps] skipping {apps_dir / 'bad'}: {says}" in log, log


@pytest.mark.parametrize(
    "manifest, sizes",
    [
        # sizes, the canonical form: one, or several, the first the default.
        ("sizes: [{w: 1, h: 1}]\n", [{"w": 1, "h": 1}]),
        ("sizes: [{w: 1, h: 1}, {w: 2, h: 2}]\n", [{"w": 1, "h": 1}, {"w": 2, "h": 2}]),
        ("sizes: [{w: 6, h: 1}, {w: 3, h: 6}, {w: 1, h: 1}]\n",
         [{"w": 6, "h": 1}, {"w": 3, "h": 6}, {"w": 1, "h": 1}]),
        ("sizes:\n  - {w: 2, h: 1}\n  - w: 4\n    h: 2\n", [{"w": 2, "h": 1}, {"w": 4, "h": 2}]),
        ('sizes: [{"w": 1, "h": 1}]\n', [{"w": 1, "h": 1}]),
        # default_size, the older form: a list of one.
        ("default_size: {w: 2, h: 2}\n", [{"w": 2, "h": 2}]),
        # Both, for hosts either side of sizes: default_size is the first.
        ("default_size: {w: 2, h: 2}\nsizes: [{w: 2, h: 2}, {w: 1, h: 1}]\n",
         [{"w": 2, "h": 2}, {"w": 1, "h": 1}]),
        # A size's other keys aren't read.
        ("sizes: [{w: 1, h: 1, label: small}]\n", [{"w": 1, "h": 1}]),
    ],
)
def test_a_manifest_declares_its_sizes(apps_dir, manifest, sizes):
    make_app(apps_dir, "steps", manifest, STEPS_APP)

    [app] = apps.scan_apps(str(apps_dir))
    assert app.sizes == sizes
    assert app.default_size == sizes[0]


def test_the_deployed_manifests_still_load(apps_dir):
    # The real apps as deployed declare default_size: { w: 2, h: 2 }, the
    # form before sizes; they keep loading, at that one size.
    for folder in ("steps", "steps-metres"):
        manifest = real(folder, apps.MANIFEST)
        assert "sizes: [{ w: 1, h: 1 }]" in manifest
        deployed = manifest.replace("sizes: [{ w: 1, h: 1 }]", "default_size: { w: 2, h: 2 }")
        make_app(apps_dir, folder, deployed, real(folder, apps.MODULE))

    steps, metres = apps.scan_apps(str(apps_dir))
    for app in (steps, metres):
        assert (app.sizes, app.default_size) == ([{"w": 2, "h": 2}], {"w": 2, "h": 2})
    assert steps.summary()["sizes"] == [{"w": 2, "h": 2}]


def test_an_app_with_no_app_py_loads_with_nothing_to_draw(client, apps_dir, capsys):
    # app.py is optional: a manifest alone is an app, listed like any
    # other, whose tile says it has nothing to draw - not an error.
    make_app(apps_dir, "notes", "name: Notes\n" + SIZE, None)

    [app] = apps.scan_apps(str(apps_dir))
    assert (app.id, app.name) == ("notes", "Notes")
    assert "[apps]" not in capsys.readouterr().out
    assert client.get("/api/apps").get_json() == [
        {"id": "notes", "name": "Notes", "default_size": {"w": 1, "h": 1},
         "sizes": [{"w": 1, "h": 1}], "reads": [], "config": [], "metrics": [], "files": ["app.yaml"]}
    ]
    assert client.post("/api/app/notes/render").get_json() == {
        "type": "empty",
        "message": "Nothing to draw yet: this app has no app.py",
    }


def test_an_app_py_added_later_is_picked_up(apps_dir):
    folder = make_app(apps_dir, "steps", "name: Steps\n" + SIZE, None)
    [app] = apps.scan_apps(str(apps_dir))
    assert app.render(None, {})["type"] == "empty"

    (folder / apps.MODULE).write_text(STEPS_APP)
    [app] = apps.scan_apps(str(apps_dir))
    assert app.render.__module__ == "openfit_app_steps"


def test_a_folder_without_a_manifest_is_not_an_app(apps_dir, capsys):
    make_app(apps_dir, "notes", None, STEPS_APP)
    make_app(apps_dir, ".hidden", "name: Hidden\n" + SIZE, STEPS_APP)
    (apps_dir / "README.txt").write_text("not an app")

    assert apps.scan_apps(str(apps_dir)) == []
    # Not an app, so nothing to warn about.
    assert "[apps]" not in capsys.readouterr().out


def test_a_missing_apps_folder_has_no_apps(tmp_path):
    assert apps.scan_apps(str(tmp_path / "nowhere")) == []


def test_apps_are_listed_in_folder_order(apps_dir):
    for folder in ("zeta", "alpha", "mid"):
        make_app(apps_dir, folder, SIZE, STEPS_APP)

    assert ids(apps.scan_apps(str(apps_dir))) == ["alpha", "mid", "zeta"]


def test_two_apps_claiming_one_id_keep_the_first_folder(apps_dir, capsys):
    make_app(apps_dir, "a-steps", "id: steps\nname: First\n" + SIZE, STEPS_APP)
    make_app(apps_dir, "b-steps", "id: steps\nname: Second\n" + SIZE, STEPS_APP)

    [app] = apps.scan_apps(str(apps_dir))
    assert app.name == "First"
    assert "'steps' is already taken" in capsys.readouterr().out


def test_an_edited_app_is_reloaded_and_a_broken_one_logged_once(apps_dir, capsys):
    folder = make_app(apps_dir, "steps", "name: Steps\n" + SIZE, "def render(ctx, cfg)\n")
    assert apps.scan_apps(str(apps_dir)) == []
    assert apps.scan_apps(str(apps_dir)) == []
    # Logged when it was loaded - not again on the second scan.
    assert capsys.readouterr().out.count("[apps] skipping") == 1

    (folder / apps.MODULE).write_text(STEPS_APP)
    assert ids(apps.scan_apps(str(apps_dir))) == ["steps"]

    (folder / apps.MANIFEST).write_text("name: Steps, renamed\n" + SIZE)
    assert apps.scan_apps(str(apps_dir))[0].name == "Steps, renamed"


def test_find_app_by_id(apps_dir):
    make_app(apps_dir, "steps", SIZE, STEPS_APP)

    assert apps.find_app(str(apps_dir), "steps").id == "steps"
    assert apps.find_app(str(apps_dir), "nope") is None


# ---------- GET /api/apps ----------


def test_api_apps_lists_the_installed_apps(client, apps_dir):
    make_app(apps_dir, "steps", "id: steps\nname: Steps\nsizes: [{w: 1, h: 1}, {w: 2, h: 2}]\n", STEPS_APP)
    make_app(apps_dir, "steps-metres", "name: Distance\ndefault_size: {w: 2, h: 1}\n", METRES_APP)
    make_app(apps_dir, "broken", "name: Broken\n" + SIZE, "def render(ctx, cfg)\n")
    make_app(apps_dir, "sizeless", "name: Sizeless\n", METRES_APP)

    resp = client.get("/api/apps")
    assert resp.status_code == 200
    # Each app's sizes, the default first, and default_size that first one
    # - one size for a manifest with the older default_size alone.
    assert resp.get_json() == [
        {"id": "steps", "name": "Steps", "default_size": {"w": 1, "h": 1},
         "sizes": [{"w": 1, "h": 1}, {"w": 2, "h": 2}],
         "reads": [], "config": [], "metrics": [], "files": ["app.py", "app.yaml"]},
        {"id": "steps-metres", "name": "Distance", "default_size": {"w": 2, "h": 1},
         "sizes": [{"w": 2, "h": 1}], "reads": [], "config": [], "metrics": [], "files": ["app.py", "app.yaml"]},
    ]


def test_api_apps_with_none_installed(client, apps_dir):
    assert client.get("/api/apps").get_json() == []


# ---------- POST /api/app/<id>/render ----------


def test_the_real_apps_are_listed_and_both_render_specs(client, conn, apps_dir):
    # Home's acceptance, on the two real apps verbatim: both listed, and both
    # rendering their specs over live data - neither an error tile. steps is
    # a number over a titled line.
    install_real_apps(apps_dir)
    insert(conn, today_minus(1), "garmin", steps=8000)
    insert(conn, today_minus(0), "garmin", steps=4000)
    recompute_derived(conn)
    today = today_minus(0)

    assert client.get("/api/apps").get_json() == [
        {"id": "steps", "name": "Steps", "default_size": {"w": 1, "h": 1},
         "sizes": [{"w": 1, "h": 1}], "reads": ["steps"], "config": [], "metrics": [], "files": ["app.py", "app.yaml"]},
        # steps-metres' config schema, as the manifest gives it: the
        # tile's settings form is built from it.
        {"id": "steps-metres", "name": "Distance (from steps)", "default_size": {"w": 1, "h": 1},
         "sizes": [{"w": 1, "h": 1}], "reads": ["steps"],
         "config": [{"key": "stride_m", "label": "Stride length (m)", "type": "number", "default": 0.75}],
         "metrics": [], "files": ["app.py", "app.yaml"]},
    ]
    assert client.post("/api/app/steps/render").get_json() == {
        "type": "panel",
        "children": [
            {"type": "number", "value": 4000, "label": "steps",
             "caption": f"{today} · so far today · via garmin", "unit": None},
            {"type": "line", "title": "last 7 days",
             "points": [[today_minus(1), 8000], [today, 4000]]},
        ],
    }
    assert client.post("/api/app/steps-metres/render").get_json() == {
        "type": "number", "value": 3000, "label": "metres",
        "caption": "4000 steps × 0.75 m · so far today", "unit": None,
    }


def test_the_real_apps_manifests_load_as_written(apps_dir):
    install_real_apps(apps_dir)

    steps, metres = apps.scan_apps(str(apps_dir))
    assert (steps.reads, steps.config) == (["steps"], [])
    # The canonical form: one size, a single cell.
    assert steps.sizes == metres.sizes == [{"w": 1, "h": 1}]
    assert metres.config == [
        {"key": "stride_m", "label": "Stride length (m)", "type": "number", "default": 0.75},
    ]


def test_render_returns_the_apps_spec_over_live_data(client, conn, apps_dir):
    # The week's derived points: the pick (garmin over google_health), and
    # nothing from before the trailing seven days.
    make_app(apps_dir, "steps", "name: Steps\n" + SIZE, STEPS_APP)
    insert(conn, today_minus(8), "garmin", steps=1000)   # outside the week
    insert(conn, today_minus(2), "garmin", steps=8000)
    insert(conn, today_minus(1), "google_health", steps=4500.4)
    insert(conn, today_minus(1), "garmin", steps=4499.6)
    recompute_derived(conn)

    resp = client.post("/api/app/steps/render")
    assert resp.status_code == 200
    assert resp.get_json() == {
        "type": "panel",
        "children": [
            # The latest day isn't today, so no "so far today".
            {"type": "number", "value": 4500, "label": "steps",
             "caption": f"{today_minus(1)} · via garmin", "unit": None},
            {"type": "line", "title": "last 7 days", "points": [
                [today_minus(2), 8000],
                [today_minus(1), 4499.6],
            ]},
        ],
    }


def test_render_with_no_data_is_the_apps_empty_spec(client, apps_dir):
    install_real_apps(apps_dir)

    for app_id in ("steps", "steps-metres"):
        assert client.post(f"/api/app/{app_id}/render").get_json() == {
            "type": "empty", "message": "No step data yet",
        }


def test_render_of_an_unknown_app_is_404(client, apps_dir):
    resp = client.post("/api/app/nope/render")
    assert resp.status_code == 404
    assert resp.get_json() == {"ok": False, "error": "unknown app: nope"}


def test_render_of_a_malformed_app_is_404(client, apps_dir):
    make_app(apps_dir, "broken", SIZE, "def render(ctx, cfg)\n")

    assert client.post("/api/app/broken/render").status_code == 404


def test_a_render_that_raises_is_an_error_spec_not_a_500(client, apps_dir, capsys):
    make_app(apps_dir, "raises", SIZE, "def render(ctx, cfg):\n    raise KeyError('series')\n")

    resp = client.post("/api/app/raises/render")
    assert resp.status_code == 200
    assert resp.get_json() == {"type": "error", "message": "KeyError: 'series'"}
    log = capsys.readouterr()
    assert "[apps] raises: render failed" in log.out
    # With the traceback, pointing into the app.
    assert "app.py" in log.err


def test_an_app_asking_for_an_unknown_metric_is_an_error_spec(client, apps_dir):
    make_app(apps_dir, "typo", SIZE, "def render(ctx, cfg):\n    return ctx.metric('stepz')\n")

    assert client.post("/api/app/typo/render").get_json() == {
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
    make_app(apps_dir, "odd", SIZE, f"def render(ctx, cfg):\n    return {returned}\n")

    resp = client.post("/api/app/odd/render")
    assert resp.status_code == 200
    spec = resp.get_json()
    assert spec["type"] == "error"
    assert spec["message"].startswith(message)


ECHO_CFG = "def render(ctx, cfg):\n    return ctx.number(len(cfg), label=repr(cfg))\n"


def test_render_is_given_the_cfg_its_sent(client, apps_dir):
    # The instance's config, as given: the app is trusted and owns its
    # fallbacks, so nothing in it is checked - not even against the
    # app's own config fields.
    make_app(apps_dir, "cfg", "config: [{key: days, default: 7}]\n" + SIZE, ECHO_CFG)

    resp = client.post("/api/app/cfg/render", json={"cfg": {"days": 14, "note": "a <b>", "on": True}})
    assert resp.status_code == 200
    assert resp.get_json()["label"] == "{'days': 14, 'note': 'a <b>', 'on': True}"


@pytest.mark.parametrize(
    "body",
    [
        {},                                                     # no body at all
        {"data": "", "content_type": "application/json"},
        {"data": "{not json", "content_type": "application/json"},
        {"json": {}},
        {"json": {"cfg": None}},
        {"json": {"cfg": [1, 2]}},
        {"json": {"cfg": "days=14"}},
        {"json": {"cfg": 5}},
        {"json": [{"cfg": {"days": 14}}]},
    ],
    ids=["no body", "empty", "not JSON", "no cfg", "cfg null", "cfg a list", "cfg a string",
         "cfg a number", "a list body"],
)
def test_render_with_no_cfg_or_one_thats_not_a_mapping_is_given_an_empty_one(client, apps_dir, body):
    make_app(apps_dir, "cfg", "config: [{key: days, default: 7}]\n" + SIZE, ECHO_CFG)

    resp = client.post("/api/app/cfg/render", **body)
    assert resp.status_code == 200
    assert resp.get_json() == {"type": "number", "value": 0, "label": "{}", "caption": None, "unit": None}


def test_the_real_metres_app_renders_with_the_stride_its_sent(client, conn, apps_dir):
    # steps-metres declares stride_m (default 0.75): its tile's settings
    # set it, and the spec follows.
    install_real_apps(apps_dir)
    insert(conn, today_minus(0), "garmin", steps=4000)
    recompute_derived(conn)
    url = "/api/app/steps-metres/render"

    assert client.post(url, json={"cfg": {"stride_m": 1.0}}).get_json() == {
        "type": "number", "value": 4000, "label": "metres",
        "caption": "4000 steps × 1.0 m · so far today", "unit": None,
    }
    # With none set, the app's own fallback.
    assert client.post(url, json={"cfg": {}}).get_json()["value"] == 3000
    assert client.post(url).get_json()["value"] == 3000
    # An app that declares no config is sent one all the same, and draws
    # as ever.
    assert client.post("/api/app/steps/render", json={"cfg": {"stride_m": 1.0}}).get_json() == \
        client.post("/api/app/steps/render").get_json()


def test_render_is_a_post_a_get_is_refused(client, apps_dir):
    # POST only to carry the cfg: a GET, the route's old method, no longer
    # renders anything.
    install_real_apps(apps_dir)

    assert client.get("/api/app/steps/render").status_code == 405
    assert client.post("/api/app/steps/render").status_code == 200


def test_render_of_an_unknown_app_with_a_cfg_is_404(client, apps_dir):
    resp = client.post("/api/app/nope/render", json={"cfg": {"stride_m": 1.0}})
    assert resp.status_code == 404
    assert resp.get_json() == {"ok": False, "error": "unknown app: nope"}


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


def test_line_spec_takes_date_value_pairs_and_a_title():
    assert host.line([
        ("2026-10-14", 9000.0),
        ("2026-10-15", None),
        (date(2026, 10, 16), math.nan),
        ["2026-10-17", 7000],
    ], title="last 7 days") == {"type": "line", "title": "last 7 days", "points": [
        ["2026-10-14", 9000.0],
        ["2026-10-15", None],
        ["2026-10-16", None],
        ["2026-10-17", 7000],
    ]}
    # The title is optional: no title is null.
    assert host.line([]) == {"type": "line", "points": [], "title": None}


def test_line_spec_takes_points_dicts_or_bare_numbers():
    # ctx.metric()'s points go straight in; their source is dropped.
    assert host.line([host.Point("2026-10-14", 9000.0, "garmin")])["points"] == [
        ["2026-10-14", 9000.0],
    ]
    assert host.line([
        {"date": "2026-10-14", "value": 9000, "source": "garmin"},
        {"value": None},
    ])["points"] == [["2026-10-14", 9000], [None, None]]
    # An undated value is a pair with a null date.
    assert host.line([1, None, 2.5, math.nan])["points"] == [
        [None, 1], [None, None], [None, 2.5], [None, None],
    ]


@pytest.mark.parametrize("title", [5, ["last 7 days"], True])
def test_line_spec_title_is_text(title):
    with pytest.raises(TypeError, match="title"):
        host.line([], title=title)


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


def test_input_spec():
    assert host.input("weight_kg", "log", label="Log weight", unit="kg") == {
        "type": "input", "key": "weight_kg", "action": "log",
        "label": "Log weight", "kind": "number", "unit": "kg", "options": None, "accept": None,
    }
    # A number by default; label and unit may be left out. Only a select
    # has options, and only a file an accept.
    assert host.input("note", "save", kind="text") == {
        "type": "input", "key": "note", "action": "save", "label": None, "kind": "text", "unit": None,
        "options": None, "accept": None,
    }


def test_a_select_input_spec_carries_its_options():
    assert host.input("mood", "log", kind="select", options=["1", "2"]) == {
        "type": "input", "key": "mood", "action": "log",
        "label": None, "kind": "select", "unit": None, "options": ["1", "2"], "accept": None,
    }
    # Label and unit as for any input; options kept in order, as written -
    # a tuple of them a plain list in the spec.
    assert host.input("tag", "add", label="Tag", unit="", kind="select", options=("b", " a ", "b ")) == {
        "type": "input", "key": "tag", "action": "add",
        "label": "Tag", "kind": "select", "unit": "", "options": ["b", " a ", "b "], "accept": None,
    }


@pytest.mark.parametrize("options", [None, [], ()])
def test_a_select_needs_options(options):
    with pytest.raises(ValueError, match="a select's options must be a non-empty list of strings"):
        host.input("mood", "log", kind="select", options=options)
    if options is None:
        with pytest.raises(ValueError):
            host.input("mood", "log", kind="select")


@pytest.mark.parametrize(
    "options", ["12345", {"1": "one"}, {"1", "2"}, 3, ["1", 2], ["1", None], ["1", ""], [["1"]], [b"1"]],
)
def test_a_selects_options_must_be_a_list_of_non_empty_strings(options):
    with pytest.raises(TypeError, match="a select's options must be a non-empty list of strings"):
        host.input("mood", "log", kind="select", options=options)


@pytest.mark.parametrize("kind", ["number", "text"])
@pytest.mark.parametrize("options", [["1", "2"], [], ()])
def test_options_are_only_for_a_select(kind, options):
    with pytest.raises(ValueError, match="options is only for a select"):
        host.input("mood", "log", kind=kind, options=options)


def test_a_file_input_spec_carries_its_accept():
    assert host.input("file", "import", kind="file", accept=".txt,.csv", label="Import weigh-ins") == {
        "type": "input", "key": "file", "action": "import",
        "label": "Import weigh-ins", "kind": "file", "unit": None, "options": None, "accept": ".txt,.csv",
    }
    # accept is optional - any file, then - and kept as written; label and
    # unit as for any input.
    assert host.input("roster", "load", kind="file", unit="ics") == {
        "type": "input", "key": "roster", "action": "load",
        "label": None, "kind": "file", "unit": "ics", "options": None, "accept": None,
    }
    assert host.input("db", "load", kind="file", accept=" application/x-sqlite3, .db ")["accept"] == (
        " application/x-sqlite3, .db "
    )


@pytest.mark.parametrize("accept", [3, [".csv"], (".csv",), {".csv"}, b".csv", True])
def test_a_file_inputs_accept_must_be_text(accept):
    with pytest.raises(TypeError, match="a file input's accept must be non-empty text"):
        host.input("file", "import", kind="file", accept=accept)


def test_a_file_inputs_accept_cant_be_empty():
    with pytest.raises(ValueError, match="a file input's accept must be non-empty text"):
        host.input("file", "import", kind="file", accept="")


@pytest.mark.parametrize("kind", ["number", "text", "select"])
@pytest.mark.parametrize("accept", [".csv", "", 3])
def test_accept_is_only_for_a_file(kind, accept):
    options = ["1"] if kind == "select" else None
    with pytest.raises(ValueError, match="accept is only for a file"):
        host.input("k", "a", kind=kind, options=options, accept=accept)


@pytest.mark.parametrize("options", [["1", "2"], [], ()])
def test_a_file_takes_no_options(options):
    with pytest.raises(ValueError, match="options is only for a select, not a file"):
        host.input("file", "import", kind="file", options=options)


def test_a_file_input_carries_its_toggle():
    toggle = {"key": "backfill", "label": "Backfill", "confirm": "Backfill <b>past</b> duties?"}
    assert host.input("file", "upload", kind="file", accept=".ics", toggle=toggle) == {
        "type": "input", "key": "file", "action": "upload", "label": None, "kind": "file", "unit": None,
        "options": None, "accept": ".ics",
        "toggle": {"key": "backfill", "label": "Backfill", "confirm": "Backfill <b>past</b> duties?"},
    }
    # confirm is optional: none, a null in the spec. The spec's toggle is
    # its own copy, not the mapping given.
    bare = {"key": "replace", "label": "Replace"}
    spec = host.input("file", "upload", kind="file", toggle=bare)
    assert spec["toggle"] == {"key": "replace", "label": "Replace", "confirm": None}
    assert spec["toggle"] is not bare
    assert host.input("file", "upload", kind="file", toggle=dict(bare, confirm=None))["toggle"] == spec["toggle"]
    # ctx.input is the same builder.
    assert host.Ctx(None).input("file", "upload", kind="file", toggle=bare) == spec


def test_an_input_with_no_toggle_carries_no_toggle_key():
    # The spec from before toggles, exactly: no key, not a null one.
    for kwargs in ({"kind": "file", "accept": ".ics"}, {"kind": "file", "toggle": None}, {}, {"kind": "text"},
                   {"kind": "select", "options": ["1"]}):
        assert "toggle" not in host.input("k", "a", **kwargs), kwargs


@pytest.mark.parametrize("kind", ["number", "text", "select"])
@pytest.mark.parametrize("toggle", [{"key": "backfill", "label": "Backfill"}, {}, "backfill"])
def test_a_toggle_is_only_for_a_file(kind, toggle):
    options = ["1"] if kind == "select" else None
    with pytest.raises(ValueError, match=f"a toggle is only for a file, not a {kind}"):
        host.input("k", "a", kind=kind, options=options, toggle=toggle)


@pytest.mark.parametrize(
    "toggle, error, says",
    [
        # Not a mapping.
        ("backfill", TypeError, "a file input's toggle must be a mapping"),
        (["backfill", "Backfill"], TypeError, "a file input's toggle must be a mapping"),
        (True, TypeError, "a file input's toggle must be a mapping"),
        # A key and a label, each non-empty text.
        ({"label": "Backfill"}, TypeError, "a file input's toggle's key must be non-empty text"),
        ({"key": "", "label": "Backfill"}, ValueError, "a file input's toggle's key must be non-empty text"),
        ({"key": 3, "label": "Backfill"}, TypeError, "a file input's toggle's key must be non-empty text"),
        ({"key": "backfill"}, TypeError, "a file input's toggle's label must be non-empty text"),
        ({"key": "backfill", "label": ""}, ValueError, "a file input's toggle's label must be non-empty text"),
        ({"key": "backfill", "label": None}, TypeError, "a file input's toggle's label must be non-empty text"),
        # Never the file's own key: the file is sent under it.
        ({"key": "file", "label": "Backfill"}, ValueError, "a file input's toggle's key can't be the input's own"),
        # confirm, if given, is text - and not empty.
        ({"key": "backfill", "label": "Backfill", "confirm": 1}, TypeError, "a file input's toggle's confirm must be text"),
        ({"key": "backfill", "label": "Backfill", "confirm": ""}, ValueError,
         "a file input's toggle's confirm must be non-empty text"),
        # Nothing else: a misspelt field is refused, not quietly ignored.
        ({"key": "backfill", "label": "Backfill", "confrim": "Sure?"}, ValueError,
         "a file input's toggle takes key, label, confirm - not 'confrim'"),
    ],
)
def test_a_malformed_toggle_is_refused(toggle, error, says):
    with pytest.raises(error, match=re.escape(says)):
        host.input("file", "upload", kind="file", toggle=toggle)


def test_a_file_upload_is_the_file_for_the_app_to_read(tmp_path):
    path = tmp_path / "upload"
    path.write_bytes("2026-01-03 81,2 €\n".encode("utf-8"))
    f = host.FileUpload(str(path), "w.txt", "text/plain")

    assert (f.name, f.content_type, f.path) == ("w.txt", "text/plain", str(path))
    assert f.bytes == "2026-01-03 81,2 €\n".encode("utf-8")
    assert f.text() == "2026-01-03 81,2 €\n"
    # Decoded as the app says; one that isn't text in it is the app's to
    # catch - the host reads nothing into it.
    assert f.text("latin-1") == "2026-01-03 81,2 â\x82¬\n"
    with pytest.raises(UnicodeDecodeError):
        f.text("ascii")
    # Read once: the bytes are the file as it was when first asked for.
    path.write_bytes(b"changed")
    assert f.bytes == "2026-01-03 81,2 €\n".encode("utf-8")
    # The name and type default to empty: a browser needn't send either.
    bare = host.FileUpload(str(path))
    assert (bare.name, bare.content_type, bare.bytes) == ("", "", b"changed")


@pytest.mark.parametrize(
    "args, kwargs, error",
    [
        (("", "log"), {}, TypeError),
        ((None, "log"), {}, TypeError),
        (("weight_kg", ""), {}, TypeError),
        (("weight_kg", 3), {}, TypeError),
        (("weight_kg", "log"), {"kind": "date"}, ValueError),
        (("weight_kg", "log"), {"kind": "files"}, ValueError),
        (("weight_kg", "log"), {"label": 3}, TypeError),
        (("weight_kg", "log"), {"unit": ["kg"]}, TypeError),
    ],
)
def test_input_spec_rejects_what_it_cant_draw(args, kwargs, error):
    with pytest.raises(error):
        host.input(*args, **kwargs)


HKT = timezone(timedelta(hours=8))


def block(start, end, label=None):
    return {"start": start, "end": end, "label": label}


def test_timeline_spec_with_two_lanes():
    spec = host.timeline(
        [
            {"label": "Rostered", "intervals": [
                # ISO text in any zone, or a datetime - aware, or naive
                # and so UTC - each stored as UTC to the second.
                {"start": "2026-10-19T09:10:00+08:00", "end": datetime(2026, 10, 19, 14, 10, 30, 999, tzinfo=timezone.utc),
                 "label": "CX105"},
                {"start": datetime(2026, 10, 21, 22, 0), "end": "2026-10-22T10:00:00Z", "label": "R1E"},
            ]},
            {"label": "Flown", "grey_after_now": True, "intervals": [
                {"start": "2026-10-17T01:00:00Z", "end": "2026-10-17T01:00:00Z", "label": "A1534"},   # no length: fine
                {"start": datetime(2026, 10, 18, 9, 0, tzinfo=HKT), "end": "2026-10-18T05:00:00Z"},   # no label
            ]},
        ],
        now=datetime(2026, 10, 19, 8, 0, tzinfo=HKT),
        title="Duties",
    )

    assert spec == {"type": "timeline", "title": "Duties", "now": "2026-10-19T00:00:00Z", "lanes": [
        {"label": "Rostered", "grey_after_now": False, "intervals": [
            block("2026-10-19T01:10:00Z", "2026-10-19T14:10:30Z", "CX105"),
            block("2026-10-21T22:00:00Z", "2026-10-22T10:00:00Z", "R1E"),
        ]},
        {"label": "Flown", "grey_after_now": True, "intervals": [
            block("2026-10-17T01:00:00Z", "2026-10-17T01:00:00Z", "A1534"),
            block("2026-10-18T01:00:00Z", "2026-10-18T05:00:00Z"),
        ]},
    ]}
    # Plain JSON, as a render's spec must be.
    assert json.loads(json.dumps(spec, allow_nan=False)) == spec


def test_timeline_spec_with_one_lane_and_nothing_optional():
    # A tuple of lanes, a tuple of intervals; no label, no now, no title -
    # each null - and the lane not greyed.
    assert host.timeline(({"intervals": ({"start": "2026-10-19T01:00:00Z", "end": "2026-10-19T02:00:00Z"},)},)) == {
        "type": "timeline", "title": None, "now": None, "lanes": [
            {"label": None, "grey_after_now": False, "intervals": [block("2026-10-19T01:00:00Z", "2026-10-19T02:00:00Z")]},
        ],
    }
    # A lane may be empty: nothing flown yet is still a lane. Labels are
    # kept as written, an empty one included.
    assert host.timeline([{"label": "", "intervals": []}], now="2026-10-19T00:00:00Z")["lanes"] == [
        {"label": "", "grey_after_now": False, "intervals": []},
    ]


@pytest.mark.parametrize("count", [0, 3, 4])
def test_a_timeline_has_one_lane_or_two(count):
    with pytest.raises(ValueError, match=f"a timeline has one lane or two, not {count}"):
        host.timeline([{"intervals": []}] * count)


@pytest.mark.parametrize("lanes", [None, {"intervals": []}, "lanes", 2])
def test_a_timelines_lanes_are_a_list(lanes):
    with pytest.raises(TypeError, match="a timeline's lanes must be a list"):
        host.timeline(lanes)


def test_a_timeline_intervals_end_cant_be_before_its_start():
    with pytest.raises(ValueError, match="a timeline interval's end is before its start"):
        host.timeline([{"intervals": [{"start": "2026-10-19T02:00:00Z", "end": "2026-10-19T01:59:59Z"}]}])
    # Compared once normalised: 09:00 in Hong Kong is 01:00 UTC, before 01:30.
    with pytest.raises(ValueError, match="end is before its start"):
        host.timeline([{"intervals": [{"start": "2026-10-19T01:30:00Z", "end": "2026-10-19T09:00:00+08:00"}]}])


@pytest.mark.parametrize(
    "interval",
    [
        {"start": "2026-10-19T01:00:00Z"},                   # an end is required: no open blocks
        {"start": "2026-10-19T01:00:00Z", "end": None},
        {"end": "2026-10-19T01:00:00Z"},
        {"start": None, "end": "2026-10-19T01:00:00Z"},
    ],
)
def test_a_timeline_interval_needs_both_ends(interval):
    with pytest.raises(ValueError, match="a timeline interval needs a start and an end"):
        host.timeline([{"intervals": [interval]}])


@pytest.mark.parametrize(
    "start, says",
    [
        ("tomorrow", "a timeline interval's start must be a datetime or ISO-8601 timestamp"),
        (date(2026, 10, 19), "a timeline interval's start must be a datetime or ISO-8601 timestamp"),
        (1760835600, "a timeline interval's start must be a datetime or ISO-8601 timestamp"),
    ],
)
def test_a_timeline_intervals_times_are_times(start, says):
    with pytest.raises(ValueError, match=says):
        host.timeline([{"intervals": [{"start": start, "end": "2026-10-19T01:00:00Z"}]}])


@pytest.mark.parametrize("now", ["now", date(2026, 10, 19), 0])
def test_a_timelines_now_is_a_time_or_none(now):
    with pytest.raises(ValueError, match="a timeline's now must be a datetime or ISO-8601 timestamp"):
        host.timeline([{"intervals": []}], now=now)


@pytest.mark.parametrize(
    "lane, error, says",
    [
        ("Rostered", TypeError, "a timeline lane must be a mapping"),
        ([], TypeError, "a timeline lane must be a mapping"),
        ({"label": "Rostered"}, TypeError, "a timeline lane's intervals must be a list"),
        ({"intervals": "CX105"}, TypeError, "a timeline lane's intervals must be a list"),
        ({"intervals": [], "label": 3}, TypeError, "a timeline lane's label must be text"),
        ({"intervals": [], "grey_after_now": 1}, TypeError, "grey_after_now must be True or False"),
        ({"intervals": [], "grey_after_now": "yes"}, TypeError, "grey_after_now must be True or False"),
        # A misspelt field is refused, not ignored.
        ({"intervals": [], "gray_after_now": True}, ValueError,
         "a timeline lane takes label, grey_after_now, intervals, bands - not 'gray_after_now'"),
        ({"intervals": ["CX105"]}, TypeError, "a timeline interval must be a mapping"),
        ({"intervals": [("2026-10-19T01:00:00Z", "2026-10-19T02:00:00Z")]}, TypeError, "a timeline interval must be a mapping"),
        ({"intervals": [{"start": "2026-10-19T01:00:00Z", "end": "2026-10-19T02:00:00Z", "label": 105}]},
         TypeError, "a timeline interval's label must be text"),
        ({"intervals": [{"start": "2026-10-19T01:00:00Z", "end": "2026-10-19T02:00:00Z", "code": "CX105"}]},
         ValueError, "a timeline interval takes start, end, label - not 'code'"),
    ],
)
def test_a_timeline_refuses_a_lane_or_interval_it_cant_draw(lane, error, says):
    with pytest.raises(error, match=says):
        host.timeline([lane])


def test_a_timeline_lanes_bands_are_spans_shaded_behind_its_blocks():
    flights = [block("2026-10-19T01:10:00Z", "2026-10-19T14:10:00Z", "CX105"),
               block("2026-10-21T02:00:00Z", "2026-10-21T13:00:00Z", "CX104")]
    spec = host.timeline([
        {"label": "Rostered", "intervals": flights, "bands": [
            # Its ends as an interval's: any zone or a datetime, each UTC
            # to the second; a label the port, or none.
            {"start": "2026-10-19T09:10:00+08:00", "end": datetime(2026, 10, 21, 13, 0, 30, 999, tzinfo=timezone.utc),
             "label": "MEL"},
            {"start": datetime(2026, 10, 25, 1, 0), "end": "2026-10-25T01:00:00Z"},   # no length, no label: fine
        ]},
        # A tuple of bands, as of intervals.
        {"label": "Flown", "grey_after_now": True, "intervals": flights,
         "bands": ({"start": "2026-10-19T01:10:00Z", "end": "2026-10-21T13:00:00Z", "label": "MEL"},)},
    ])

    assert spec["lanes"] == [
        {"label": "Rostered", "grey_after_now": False, "intervals": flights, "bands": [
            block("2026-10-19T01:10:00Z", "2026-10-21T13:00:30Z", "MEL"),
            block("2026-10-25T01:00:00Z", "2026-10-25T01:00:00Z"),
        ]},
        {"label": "Flown", "grey_after_now": True, "intervals": flights, "bands": [
            block("2026-10-19T01:10:00Z", "2026-10-21T13:00:00Z", "MEL"),
        ]},
    ]
    assert json.loads(json.dumps(spec, allow_nan=False)) == spec


@pytest.mark.parametrize("none", [{}, {"bands": None}, {"bands": []}, {"bands": ()}])
def test_a_lane_without_bands_is_the_lane_from_before_them(none):
    lane = {"label": "Flown", "intervals": [block("2026-10-19T01:00:00Z", "2026-10-19T02:00:00Z")], **none}

    # No "bands" key at all - not an empty list - so a timeline without
    # any is exactly the spec from before them.
    assert host.timeline([lane])["lanes"] == [
        {"label": "Flown", "grey_after_now": False, "intervals": [block("2026-10-19T01:00:00Z", "2026-10-19T02:00:00Z")]},
    ]


@pytest.mark.parametrize(
    "bands, error, says",
    [
        ("MEL", TypeError, "a timeline lane's bands must be a list"),
        ({"start": "2026-10-19T01:00:00Z", "end": "2026-10-19T02:00:00Z"}, TypeError, "a timeline lane's bands must be a list"),
        (["MEL"], TypeError, "a timeline band must be a mapping"),
        ([{"start": "2026-10-19T01:00:00Z"}], ValueError, "a timeline band needs a start and an end"),
        ([{"start": None, "end": "2026-10-19T01:00:00Z"}], ValueError, "a timeline band needs a start and an end"),
        ([{"start": "2026-10-21T00:00:00Z", "end": "2026-10-20T23:59:59Z"}], ValueError,
         "a timeline band's end is before its start"),
        # Compared once normalised: 09:00 in Hong Kong is 01:00 UTC, before 01:30.
        ([{"start": "2026-10-19T01:30:00Z", "end": "2026-10-19T09:00:00+08:00"}], ValueError,
         "a timeline band's end is before its start"),
        ([{"start": "tomorrow", "end": "2026-10-19T01:00:00Z"}], ValueError,
         "a timeline band's start must be a datetime or ISO-8601 timestamp"),
        ([{"start": "2026-10-19T01:00:00Z", "end": date(2026, 10, 20)}], ValueError,
         "a timeline band's end must be a datetime or ISO-8601 timestamp"),
        ([{"start": "2026-10-19T01:00:00Z", "end": "2026-10-20T01:00:00Z", "label": 5}], TypeError,
         "a timeline band's label must be text"),
        # A misspelt field is refused, not ignored.
        ([{"start": "2026-10-19T01:00:00Z", "end": "2026-10-20T01:00:00Z", "port": "MEL"}], ValueError,
         "a timeline band takes start, end, label - not 'port'"),
    ],
)
def test_a_timeline_refuses_a_band_it_cant_draw(bands, error, says):
    with pytest.raises(error, match=says):
        host.timeline([{"intervals": [], "bands": bands}])


@pytest.mark.parametrize("title", [3, ["Duties"], True])
def test_a_timelines_title_is_text(title):
    with pytest.raises(TypeError, match="a timeline's title must be text"):
        host.timeline([{"intervals": []}], title=title)


def test_a_timeline_joins_a_focus_group_by_name():
    lanes = [{"label": "Flown", "grey_after_now": True, "intervals": [
        {"start": "2026-10-19T01:00:00Z", "end": "2026-10-19T02:00:00Z", "label": "CX105"},
    ]}]
    plain = host.timeline(lanes, now="2026-10-19T00:00:00Z", title="Duties")
    synced = host.timeline(lanes, now="2026-10-19T00:00:00Z", title="Duties", sync="frms")

    # Named, the spec carries the group's name - and is otherwise the
    # same spec: the host carries it, the page keeps the focus clock.
    assert synced == {**plain, "sync": "frms"}
    assert json.loads(json.dumps(synced, allow_nan=False)) == synced
    # Not named - no sync, None or an empty name - the spec is exactly
    # what it was before there was a sync: no key, the same JSON.
    before = (
        '{"type": "timeline", "title": "Duties", "now": "2026-10-19T00:00:00Z", "lanes": '
        '[{"label": "Flown", "grey_after_now": true, "intervals": '
        '[{"start": "2026-10-19T01:00:00Z", "end": "2026-10-19T02:00:00Z", "label": "CX105"}]}]}'
    )
    assert json.dumps(plain) == before
    for unnamed in (None, ""):
        assert json.dumps(host.timeline(lanes, now="2026-10-19T00:00:00Z", title="Duties", sync=unnamed)) == before
    # On the ctx, as the other builders are.
    assert host.Ctx.timeline(lanes, sync="frms")["sync"] == "frms"


@pytest.mark.parametrize("sync", [3, True, ["frms"], {"group": "frms"}])
def test_a_timelines_sync_is_a_groups_name(sync):
    with pytest.raises(TypeError, match="a timeline's sync must be text, a focus group's name"):
        host.timeline([{"intervals": []}], sync=sync)


def test_the_ctx_carries_the_builders(conn):
    ctx = host.Ctx(conn)
    assert ctx.panel(ctx.number(1), ctx.line([1]), ctx.input("k", "a"), ctx.empty())["type"] == "panel"
    assert ctx.input("f", "a", kind="file", accept=".csv")["accept"] == ".csv"
    assert ctx.panel(ctx.timeline([{"intervals": []}]))["children"][0]["type"] == "timeline"


def test_render_app_passes_today_and_a_copy_of_cfg(conn, apps_dir):
    make_app(apps_dir, "echo", SIZE, """
        def render(ctx, cfg):
            cfg["seen"] = True
            return ctx.number(None, label=ctx.today(), caption=str(sorted(cfg)))
    """)
    [app] = apps.scan_apps(str(apps_dir))
    cfg = {"days": 7}

    spec = host.render_app(app, conn, cfg=cfg, today=TODAY)
    assert (spec["label"], spec["caption"]) == ("2026-10-15", "['days', 'seen']")
    assert cfg == {"days": 7}


# ---------- the writing ctx: sessions ----------
#
# handle()'s WriteCtx files a session - an interval - as a sync does,
# through base.write_session(), under the app's own source, app:<id>: a
# kind OpenFit has, a start and an end stored as iso_utc() stores every
# session's, a summary of plain JSON. Nothing is derived from it.


def an_app(app_id):
    """An InstalledApp to make a ctx for - no folder."""
    return apps.InstalledApp(
        id=app_id, name=app_id, sizes=[{"w": 1, "h": 1}], default_size={"w": 1, "h": 1},
        reads=[], config=[], path="/nowhere", render=None,
    )


def sessions(conn, source, kind=WORKOUT):
    """(id, start, end, summary) of each of `source`'s `kind` sessions, by start."""
    return [
        (r[0], r[1], r[2], None if r[3] is None else json.loads(r[3]))
        for r in conn.execute(
            'SELECT id, "start", "end", summary_json FROM sessions '
            'WHERE source = ? AND kind = ? ORDER BY "start"',
            (source, kind),
        )
    ]


def session_count(conn):
    return conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


RUN = {"type": "running", "duration_minutes": 45, "distance_m": 8012.5, "avg_hr_bpm": 151}


def test_write_session_files_under_the_apps_own_source(conn):
    ctx = host.WriteCtx(conn, an_app("log"), today=TODAY)

    assert ctx.write_session(WORKOUT, utc(2026, 10, 14, 7, 0), utc(2026, 10, 14, 7, 45), RUN) is None
    # Not committed: that's the route's.
    conn.commit()

    assert sessions(conn, "app:log") == [
        ("app:log:workout:2026-10-14T07:00:00Z", "2026-10-14T07:00:00Z", "2026-10-14T07:45:00Z", RUN),
    ]
    # No writer takes a source: an app named after a device still files
    # under app:<its id>, never the device's.
    host.WriteCtx(conn, an_app("garmin")).write_session(SLEEP, utc(2026, 10, 14, 23, 0))
    with pytest.raises(TypeError):
        ctx.write_session(WORKOUT, utc(2026, 10, 15, 7, 0), source="garmin")
    conn.commit()
    assert [tuple(r) for r in conn.execute("SELECT id, source, kind FROM sessions ORDER BY id")] == [
        ("app:garmin:sleep:2026-10-14T23:00:00Z", "app:garmin", SLEEP),
        ("app:log:workout:2026-10-14T07:00:00Z", "app:log", WORKOUT),
    ]


def test_write_session_rejects_an_unknown_kind(conn):
    ctx = host.WriteCtx(conn, an_app("log"), today=TODAY)

    for kind in ("run", "Workout", "heart_rate", "steps", "", None, 3, ["workout"]):
        with pytest.raises(ValueError) as refused:
            ctx.write_session(kind, utc(2026, 10, 14, 7, 0), utc(2026, 10, 14, 7, 45))
        assert str(refused.value) == f"unknown session kind: {kind!r}"
    # The kind is checked first, the app's word for it - before the start.
    with pytest.raises(ValueError) as refused:
        ctx.write_session("nap", None)
    assert str(refused.value) == "unknown session kind: 'nap'"
    assert session_count(conn) == 0


def test_write_session_requires_a_start(conn):
    # Where a sync's None start writes nothing, an app's is said.
    ctx = host.WriteCtx(conn, an_app("log"), today=TODAY)

    with pytest.raises(ValueError) as refused:
        ctx.write_session(WORKOUT, None, utc(2026, 10, 14, 7, 45), RUN)
    assert str(refused.value) == "a session needs a start"
    assert session_count(conn) == 0


def test_write_session_normalises_datetimes_and_strings_to_utc(conn):
    # One instant, 06:30 UTC, said six ways: each stored as iso_utc()
    # stores every session's start - UTC, whole seconds, a Z.
    same = {
        "aware": datetime(2026, 10, 14, 8, 30, tzinfo=timezone(timedelta(hours=2))),
        "naive": datetime(2026, 10, 14, 6, 30),          # taken to be UTC
        "fraction": utc(2026, 10, 14, 6, 30, 0, 999999),  # cut to the second
        "z": "2026-10-14T06:30:00Z",
        "offset": "2026-10-14T01:30:00-05:00",
        "micro": "2026-10-14T06:30:00.250000+00:00",
    }
    for app_id, start in same.items():
        host.WriteCtx(conn, an_app(app_id)).write_session(WORKOUT, start)
    conn.commit()

    stored = conn.execute('SELECT DISTINCT "start", "end", summary_json FROM sessions').fetchall()
    assert [tuple(r) for r in stored] == [("2026-10-14T06:30:00Z", None, None)]   # end None: SQL NULL
    assert session_count(conn) == len(same)

    # An end the same way.
    ctx = host.WriteCtx(conn, an_app("log"))
    ctx.write_session(WORKOUT, "2026-10-14T08:00:00+02:00", "2026-10-14T09:45:30.5+02:00")
    assert sessions(conn, "app:log") == [
        ("app:log:workout:2026-10-14T06:00:00Z", "2026-10-14T06:00:00Z", "2026-10-14T07:45:30Z", None),
    ]

    # And what isn't a time is refused, start or end.
    for bad in (20261014, date(2026, 10, 14), "yesterday", "", "2026-10-14T25:00", 1.5,
                ["2026-10-14T06:30:00Z"]):
        with pytest.raises(ValueError) as refused:
            ctx.write_session(WORKOUT, bad)
        assert str(refused.value) == f"start must be a datetime or ISO-8601 timestamp: {bad!r}"
        with pytest.raises(ValueError) as refused:
            ctx.write_session(WORKOUT, utc(2026, 10, 15, 6, 0), bad)
        assert str(refused.value) == f"end must be a datetime or ISO-8601 timestamp: {bad!r}"
    assert session_count(conn) == len(same) + 1


def test_write_session_rejects_end_before_start(conn):
    ctx = host.WriteCtx(conn, an_app("log"), today=TODAY)
    start = utc(2026, 10, 14, 7, 0)

    # Compared as instants: 08:30+02:00 reads later, and is half an hour before.
    for end in (utc(2026, 10, 14, 6, 59, 59), "2026-10-14T08:30:00+02:00", utc(2026, 10, 13, 7, 0)):
        with pytest.raises(ValueError) as refused:
            ctx.write_session(WORKOUT, start, end)
        assert str(refused.value) == "a session's end is before its start"
    assert session_count(conn) == 0

    # Zero-length is a session: an end at the start, or in its second.
    ctx.write_session(WORKOUT, start, start)
    ctx.write_session(SLEEP, datetime(2026, 10, 14, 7, 0, 0, 900000), datetime(2026, 10, 14, 7, 0, 0, 100000))
    conn.commit()
    assert sessions(conn, "app:log") == [
        ("app:log:workout:2026-10-14T07:00:00Z", "2026-10-14T07:00:00Z", "2026-10-14T07:00:00Z", None),
    ]
    assert sessions(conn, "app:log", SLEEP)[0][1:3] == ("2026-10-14T07:00:00Z", "2026-10-14T07:00:00Z")


def test_write_session_rejects_a_non_mapping_or_unserialisable_summary(conn):
    ctx = host.WriteCtx(conn, an_app("log"), today=TODAY)
    start, end = utc(2026, 10, 14, 7, 0), utc(2026, 10, 14, 7, 45)

    for summary in ([("type", "running")], ["running"], "running", 45, (("a", 1),)):
        with pytest.raises(ValueError) as refused:
            ctx.write_session(WORKOUT, start, end, summary)
        assert str(refused.value) == "a session's summary must be a mapping or None"
    looped = {}
    looped["self"] = looped
    for summary in ({"at": start}, {"stages": {"deep", "rem"}}, {"distance_m": float("nan")},
                    {"pace": float("inf")}, {("a", 1): 2}, looped):
        with pytest.raises(ValueError) as refused:
            ctx.write_session(WORKOUT, start, end, summary)
        assert str(refused.value) == "a session's summary must be JSON"
    assert session_count(conn) == 0

    # An empty one is a summary, stored as such.
    ctx.write_session(WORKOUT, start, end, {})
    assert sessions(conn, "app:log")[0][3] == {}


def test_write_session_rewriting_the_same_start_replaces_the_row(conn):
    ctx = host.WriteCtx(conn, an_app("log"), today=TODAY)

    ctx.write_session(WORKOUT, utc(2026, 10, 14, 7, 0), utc(2026, 10, 14, 7, 45), RUN)
    # The same kind and start - said another way - is the same session.
    ctx.write_session(WORKOUT, "2026-10-14T09:00:00+02:00", "2026-10-14T08:10:00Z", {"type": "running"})
    conn.commit()

    assert sessions(conn, "app:log") == [
        ("app:log:workout:2026-10-14T07:00:00Z", "2026-10-14T07:00:00Z", "2026-10-14T08:10:00Z",
         {"type": "running"}),
    ]
    # Its end and summary can go back to none.
    ctx.write_session(WORKOUT, utc(2026, 10, 14, 7, 0))
    assert sessions(conn, "app:log")[0][2:] == (None, None)
    # Another start, or another kind at that start, is another session.
    ctx.write_session(WORKOUT, utc(2026, 10, 14, 18, 0), utc(2026, 10, 14, 18, 30))
    ctx.write_session(SLEEP, utc(2026, 10, 14, 7, 0), utc(2026, 10, 14, 7, 20))
    assert session_count(conn) == 3


def test_write_session_does_not_rederive(client, conn, apps_dir):
    # A session goes into no day's pick, so the ctx notes no day ...
    ctx = host.WriteCtx(conn, an_app("rest"), today=TODAY)
    ctx.write_session(SLEEP, datetime(2026, 10, 15, 1, 0), datetime(2026, 10, 15, 7, 0), {"asleep_minutes": 330})
    assert ctx.written == []
    conn.rollback()

    # ... and an action that writes only a session re-derives nothing:
    # with a role set since today was picked, a re-derive would flip
    # today's steps to google_health (forward-only).
    make_app(apps_dir, "rest", SIZE, """
        from datetime import datetime

        def render(ctx, cfg):
            return ctx.empty("rested")

        def handle(ctx, action, payload):
            day = datetime.fromisoformat(ctx.today())
            ctx.write_session("sleep", day.replace(hour=1), day.replace(hour=7), {"asleep_minutes": 330})
    """)
    today = today_minus(0)
    insert(conn, today, "garmin", steps=9000)
    insert(conn, today, "google_health", steps=8000)
    recompute_derived(conn)
    set_source_role(conn, STEPS, "google_health")
    derived = [tuple(r) for r in conn.execute("SELECT * FROM derived_metrics")]

    resp = client.post("/api/app/rest/action", json={"action": "log"})

    assert resp.get_json() == {"ok": True, "spec": {"type": "empty", "message": "rested"}}
    assert [tuple(r) for r in conn.execute("SELECT * FROM derived_metrics")] == derived
    assert derived == [(today, STEPS, 9000, "count", "garmin", "2026-01-01 06:00:00")]
    # The session itself was committed, with nothing to re-derive.
    assert [s[1:] for s in sessions(conn, "app:rest", SLEEP)] == [
        (f"{today}T01:00:00Z", f"{today}T07:00:00Z", {"asleep_minutes": 330}),
    ]


# ---------- the ctx: reading sessions ----------
#
# ctx.sessions(kind, since, until) is GET /api/sessions/<kind> in
# process: every source's sessions of one kind, as Sessions (start, end,
# source, summary), the source the pick would rank first leading, each
# source's by start. Both read derived.session_rows(), so they can't
# drift - test_ctx_sessions_matches_the_http_endpoint holds them to it.


def filed(conn, source, kind, start, end=None, summary=None):
    """One session, filed as a sync files it, committed."""
    write_session(conn, source, kind, start, end, summary)
    conn.commit()


def starts(found):
    return [(s.source, s.start) for s in found]


def kind_primary(conn, kind, source):
    # set_source_role() takes metrics only: a kind's role is written
    # through /api/settings, as this does.
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('source_roles', ?)",
        (json.dumps({kind: source}),),
    )
    conn.commit()


def test_ctx_sessions_reads_a_kinds_sessions(conn):
    run = {"type": "running", "duration_minutes": 45, "distance_m": 8012.5}
    filed(conn, "garmin", WORKOUT, "2026-10-14T07:00:00Z", "2026-10-14T07:45:00Z", run)
    filed(conn, "app:log", WORKOUT, "2026-10-14T18:00:00Z", None, None)
    # Another kind's isn't this kind's.
    filed(conn, "garmin", SLEEP, "2026-10-13T22:00:00Z", "2026-10-14T06:00:00Z", {"asleep_minutes": 420})

    data = host.Ctx(conn, today=TODAY).sessions(WORKOUT)

    assert isinstance(data, list)
    assert data == [
        host.Session("2026-10-14T07:00:00Z", "2026-10-14T07:45:00Z", "garmin", run),
        host.Session("2026-10-14T18:00:00Z", None, "app:log", {}),
    ]
    first, second = data
    assert (first.start, first.end, first.source) == ("2026-10-14T07:00:00Z", "2026-10-14T07:45:00Z", "garmin")
    assert isinstance(first.summary, dict) and first.summary["distance_m"] == 8012.5
    # No end yet stays no end - None, never a made-up time.
    assert second.end is None
    # A ctx made for an app, and handle()'s WriteCtx, read the same: any
    # source's sessions, not only the app's own.
    assert host.Ctx(conn, app=an_app("log")).sessions(WORKOUT) == data
    assert host.WriteCtx(conn, an_app("log")).sessions(WORKOUT) == data


def test_ctx_sessions_in_handle_sees_what_the_action_has_written(conn):
    # One connection, one transaction: a session handle() has just filed
    # is read back before the route commits - and gone if it rolls back.
    ctx = host.WriteCtx(conn, an_app("log"), today=TODAY)
    ctx.write_session(WORKOUT, utc(2026, 10, 14, 7, 0), utc(2026, 10, 14, 7, 45), {"type": "running"})

    assert ctx.sessions(WORKOUT) == [
        host.Session("2026-10-14T07:00:00Z", "2026-10-14T07:45:00Z", "app:log", {"type": "running"}),
    ]
    conn.rollback()
    assert ctx.sessions(WORKOUT) == []


def test_ctx_sessions_rejects_an_unknown_kind(conn):
    filed(conn, "garmin", WORKOUT, "2026-10-14T07:00:00Z")
    ctx = host.Ctx(conn, today=TODAY)

    # A metric, a series metric, a misspelling, a key in another case:
    # none is a session kind.
    for kind in ("nap", "Workout", "steps", "heart_rate", "sleep_minutes", "", None, 3, ["workout"]):
        with pytest.raises(ValueError) as refused:
            ctx.sessions(kind)
        assert str(refused.value) == f"unknown session kind: {kind!r}"


def test_ctx_sessions_is_empty_for_a_kind_with_none(conn):
    filed(conn, "garmin", SLEEP, "2026-10-13T22:00:00Z", "2026-10-14T06:00:00Z")
    ctx = host.Ctx(conn, today=TODAY)

    assert ctx.sessions(WORKOUT) == []
    assert ctx.sessions(DUTIES_COMPLETE) == []
    # And a kind with sessions, none of them in range.
    assert ctx.sessions(SLEEP, since="2026-10-14") == []


def test_ctx_sessions_filters_by_start_date_range(conn):
    # Matched on the day each starts, in UTC: a night that starts on the
    # 12th and ends on the 13th is the 12th's.
    for start in ("2026-10-12T23:30:00Z", "2026-10-13T00:00:00Z", "2026-10-14T23:59:59Z",
                  "2026-10-15T06:00:00Z"):
        filed(conn, "garmin", SLEEP, start, "2026-10-16T00:00:00Z")
    ctx = host.Ctx(conn, today=TODAY)

    def days(since=None, until=None):
        return [s.start for s in ctx.sessions(SLEEP, since, until)]

    # Both ends included.
    assert days("2026-10-13", "2026-10-14") == ["2026-10-13T00:00:00Z", "2026-10-14T23:59:59Z"]
    assert days("2026-10-14", "2026-10-14") == ["2026-10-14T23:59:59Z"]
    # Either end left open, or both.
    assert days(since="2026-10-14") == ["2026-10-14T23:59:59Z", "2026-10-15T06:00:00Z"]
    assert days(until="2026-10-12") == ["2026-10-12T23:30:00Z"]
    assert len(days()) == 4
    # By keyword too, and a date will do for a day.
    assert ctx.sessions(SLEEP, until=date(2026, 10, 13), since=date(2026, 10, 13)) == ctx.sessions(
        SLEEP, "2026-10-13", "2026-10-13"
    )


def test_ctx_sessions_rejects_a_range_that_isnt_one(conn):
    # As ?from=/?to= are refused with a 400: a day that isn't YYYY-MM-DD,
    # or a since after the until.
    ctx = host.Ctx(conn, today=TODAY)

    for bad in ("2026-10-32", "yesterday", "", "20261014", 20261014, True, datetime(2026, 10, 14, 7, 0)):
        with pytest.raises(ValueError) as refused:
            ctx.sessions(SLEEP, since=bad)
        assert str(refused.value) == f"since must be YYYY-MM-DD: {bad!r}"
        with pytest.raises(ValueError) as refused:
            ctx.sessions(SLEEP, until=bad)
        assert str(refused.value) == f"until must be YYYY-MM-DD: {bad!r}"
    with pytest.raises(ValueError) as refused:
        ctx.sessions(SLEEP, "2026-10-15", "2026-10-14")
    assert str(refused.value) == "since must not be after until: 2026-10-15 > 2026-10-14"


def test_ctx_sessions_orders_by_source_priority_then_start(conn):
    # Filed out of order: by start alone, app:x's (the 10th) would lead.
    for source, start in (
        ("zepp", "2026-10-11T06:00:00Z"),
        ("google_health", "2026-10-12T06:00:00Z"),
        ("garmin", "2026-10-14T06:00:00Z"),
        ("app:x", "2026-10-10T06:00:00Z"),
        ("garmin", "2026-10-13T06:00:00Z"),
        ("app:a", "2026-10-15T06:00:00Z"),
        ("app:x", "2026-10-09T06:00:00Z"),
        ("google_health", "2026-10-11T06:00:00Z"),
    ):
        filed(conn, source, WORKOUT, start)
    ctx = host.Ctx(conn, today=TODAY)

    # The default precedence, garmin then google_health, then the rest by
    # name; each source's by start.
    assert starts(ctx.sessions(WORKOUT)) == [
        ("garmin", "2026-10-13T06:00:00Z"),
        ("garmin", "2026-10-14T06:00:00Z"),
        ("google_health", "2026-10-11T06:00:00Z"),
        ("google_health", "2026-10-12T06:00:00Z"),
        ("app:a", "2026-10-15T06:00:00Z"),
        ("app:x", "2026-10-09T06:00:00Z"),
        ("app:x", "2026-10-10T06:00:00Z"),
        ("zepp", "2026-10-11T06:00:00Z"),
    ]

    # The kind's configured primary leads, the rest keep their order.
    kind_primary(conn, WORKOUT, "app:x")
    assert starts(ctx.sessions(WORKOUT))[:4] == [
        ("app:x", "2026-10-09T06:00:00Z"),
        ("app:x", "2026-10-10T06:00:00Z"),
        ("garmin", "2026-10-13T06:00:00Z"),
        ("garmin", "2026-10-14T06:00:00Z"),
    ]
    # Only this kind's: another kind's primary changes nothing here.
    kind_primary(conn, SLEEP, "zepp")
    assert starts(ctx.sessions(WORKOUT))[0] == ("garmin", "2026-10-13T06:00:00Z")


def test_ctx_sessions_summary_is_a_parsed_dict(conn):
    nested = {"type": "flight", "legs": [{"dep": "AAA", "arr": "BBB"}], "duration_minutes": 95.5}
    filed(conn, "app:roster", DUTIES_COMPLETE, "2026-10-14T05:00:00Z", "2026-10-14T07:00:00Z", nested)
    filed(conn, "app:roster", DUTIES_COMPLETE, "2026-10-15T05:00:00Z", "2026-10-15T07:00:00Z", None)
    filed(conn, "app:roster", DUTIES_COMPLETE, "2026-10-16T05:00:00Z", "2026-10-16T07:00:00Z", {})
    ctx = host.Ctx(conn, today=TODAY)

    with_summary, without, emptied = ctx.sessions(DUTIES_COMPLETE)
    # Parsed, never the JSON text it's stored as.
    assert with_summary.summary == nested
    assert with_summary.summary["legs"][0]["arr"] == "BBB"
    # NULL in summary_json reads as {}, as an empty one does.
    assert conn.execute(
        "SELECT summary_json FROM sessions WHERE \"start\" = '2026-10-15T05:00:00Z'"
    ).fetchone()[0] is None
    assert without.summary == {} and emptied.summary == {}
    assert all(isinstance(s.summary, dict) for s in (with_summary, without, emptied))
    # Each read is the app's own copy: changing it changes nothing stored.
    with_summary.summary["legs"].clear()
    assert ctx.sessions(DUTIES_COMPLETE)[0].summary == nested


def test_ctx_sessions_matches_the_http_endpoint(client, conn):
    filed(conn, "zepp", SLEEP, "2026-10-12T23:30:00Z", "2026-10-13T06:00:00Z", {"asleep_minutes": 300})
    filed(conn, "garmin", SLEEP, "2026-10-13T22:00:00Z", None, None)
    filed(conn, "app:rest", SLEEP, "2026-10-11T01:00:00Z", "2026-10-11T02:00:00Z", {"stages": [1, {"deep": 2.5}]})
    filed(conn, "google_health", SLEEP, "2026-10-13T22:00:00Z", "2026-10-14T06:00:00Z", {})
    filed(conn, "garmin", SLEEP, "2026-10-10T22:00:00Z", "2026-10-11T06:00:00Z", {"deep_minutes": 60})
    kind_primary(conn, SLEEP, "zepp")
    ctx = host.Ctx(conn, today=TODAY)

    for since, until in ((None, None), ("2026-10-11", None), (None, "2026-10-12"),
                         ("2026-10-11", "2026-10-13"), ("2026-10-13", "2026-10-13"),
                         ("2026-10-20", None)):
        query = "&".join(f"{name}={day}" for name, day in (("from", since), ("to", until)) if day)
        resp = client.get(f"/api/sessions/{SLEEP}?{query}")
        assert resp.status_code == 200
        # The same sessions, field for field, in the same order.
        assert [s._asdict() for s in ctx.sessions(SLEEP, since, until)] == resp.get_json()

    # And that shared answer is the documented order: the kind's primary
    # first, then garmin, google_health, the rest by name; each by start.
    assert starts(ctx.sessions(SLEEP)) == [
        ("zepp", "2026-10-12T23:30:00Z"),
        ("garmin", "2026-10-10T22:00:00Z"),
        ("garmin", "2026-10-13T22:00:00Z"),
        ("google_health", "2026-10-13T22:00:00Z"),
        ("app:rest", "2026-10-11T01:00:00Z"),
    ]


def test_a_render_can_read_back_the_sessions_an_app_wrote(client, conn, apps_dir):
    # A watch's workout, there before the app's: ctx.sessions() is every
    # source's.
    filed(conn, "garmin", WORKOUT, "2026-10-13T17:00:00Z", "2026-10-13T18:00:00Z", {"duration_minutes": 60})
    make_app(apps_dir, "gym", SIZE, """
        def render(ctx, cfg):
            workouts = ctx.sessions("workout")
            if not workouts:
                return ctx.empty("No workouts yet")
            latest = max(workouts, key=lambda s: s.start)
            return ctx.number(
                len(workouts), label="workouts",
                caption=f"{latest.start} · {latest.summary['duration_minutes']} min · via {latest.source}",
            )

        def handle(ctx, action, payload):
            # handle() reads too - its own app's sessions, by day.
            day = payload["start"][:10]
            if any(s.source == ctx.source for s in ctx.sessions("workout", day, day)):
                raise ValueError(f"Already logged a workout on {day}")
            ctx.write_session("workout", payload["start"], payload["end"],
                              {"type": "manual", "duration_minutes": payload["minutes"]})
    """)

    resp = client.post("/api/app/gym/action", json={"action": "log", "payload": {
        "start": "2026-10-14T07:00:00Z", "end": "2026-10-14T07:45:00Z", "minutes": 45,
    }})

    # The render after the write reads back what the action wrote.
    drawn = {
        "type": "number", "value": 2, "label": "workouts", "unit": None,
        "caption": "2026-10-14T07:00:00Z · 45 min · via app:gym",
    }
    assert resp.get_json() == {"ok": True, "spec": drawn}
    assert starts(host.Ctx(conn).sessions(WORKOUT)) == [
        ("garmin", "2026-10-13T17:00:00Z"),
        ("app:gym", "2026-10-14T07:00:00Z"),
    ]

    # handle() sees the app's own session from that day, and refuses a
    # second - writing nothing.
    resp = client.post("/api/app/gym/action", json={"action": "log", "payload": {
        "start": "2026-10-14T18:00:00Z", "end": "2026-10-14T18:30:00Z", "minutes": 30,
    }})
    assert resp.status_code == 400
    assert resp.get_json() == {"ok": False, "error": "Already logged a workout on 2026-10-14"}
    assert len(host.Ctx(conn).sessions(WORKOUT)) == 2

    # And a later render, a request of its own, reads the same back.
    assert client.post("/api/app/gym/render", json={}).get_json() == drawn


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
