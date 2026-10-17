"""
The app host: installed apps, their render() and the Home tab's routes.

An app is a folder in APPS_DIR holding app.yaml (its manifest) and,
usually, app.py (its render(ctx, cfg)). scan_apps() lists the ones that
load and logs and skips the rest; GET /api/apps lists them, and POST
/api/app/<id>/render runs one's render() against a fresh ctx and the
cfg it's sent, and returns its display spec - or an error spec, never a
500. ctx.metric() is an
app's one data path: a metric's derived points as a list of Points,
trailing `days` days.
Every test gets its own temp apps folder; nothing reads /data/apps.
"""

import math
import os
import shutil
import textwrap
from datetime import date, timedelta
from pathlib import Path

import pytest

import apps
import host
from derived import recompute_derived
from metrics import DISTANCE_M, METRICS, STEPS


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
        "label": "Log weight", "kind": "number", "unit": "kg",
    }
    # A number by default; label and unit may be left out.
    assert host.input("note", "save", kind="text") == {
        "type": "input", "key": "note", "action": "save", "label": None, "kind": "text", "unit": None,
    }


@pytest.mark.parametrize(
    "args, kwargs, error",
    [
        (("", "log"), {}, TypeError),
        ((None, "log"), {}, TypeError),
        (("weight_kg", ""), {}, TypeError),
        (("weight_kg", 3), {}, TypeError),
        (("weight_kg", "log"), {"kind": "date"}, ValueError),
        (("weight_kg", "log"), {"label": 3}, TypeError),
        (("weight_kg", "log"), {"unit": ["kg"]}, TypeError),
    ],
)
def test_input_spec_rejects_what_it_cant_draw(args, kwargs, error):
    with pytest.raises(error):
        host.input(*args, **kwargs)


def test_the_ctx_carries_the_builders(conn):
    ctx = host.Ctx(conn)
    assert ctx.panel(ctx.number(1), ctx.line([1]), ctx.input("k", "a"), ctx.empty())["type"] == "panel"


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
