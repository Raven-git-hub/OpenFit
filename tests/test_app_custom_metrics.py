"""
Apps' own metrics (B0): a metric an app invents.

Declare what you invent, write what exists. An app lists the metrics it
invents in its app.yaml's `metrics` block - {key, label, unit, category,
grain, range?} each - and reads and writes them by that short key; a
built-in metric it just writes. The host stores an app's metric as
app:<id>:<key>, under the app's source, app:<id>, so the app's code says
caffeine_mg and two apps' keys never meet. The effective vocabulary -
the catalog, /api/coverage and /api/metric/<key> - is the canonical
metrics plus every installed app's own; an uninstalled app's drops out,
its readings kept until they're removed (DELETE /api/app-sources/<id>).

The caffeine app, committed in tests/fixtures/apps/caffeine/, is the
end-to-end test: declare -> listed in the catalog -> write -> stored
under app:caffeine:caffeine_mg -> read back.

Every test gets its own temp apps folder; nothing reads /data/apps.
"""

import shutil
import textwrap
from datetime import date
from pathlib import Path

import pytest

import apps
import host
from derived import recompute_derived
from metrics import CATEGORIES, METRICS, STEPS, WEIGHT_KG

REAL_APPS = Path(__file__).parent / "fixtures" / "apps"

SIZE = "sizes: [{w: 1, h: 1}]\n"

TODAY = date.today().isoformat()

CAFFEINE = "app:caffeine:caffeine_mg"


@pytest.fixture
def apps_dir(tmp_path, monkeypatch):
    """An empty apps folder, which the app is pointed at."""
    import main

    path = tmp_path / "apps"
    path.mkdir()
    monkeypatch.setitem(main.app.config, "APPS_DIR", str(path))
    return path


def make_app(root, app_id, metrics="", code=None):
    """An app folder: app.yaml naming `app_id` with a `metrics` block, app.py from `code`."""
    path = root / app_id
    path.mkdir()
    (path / apps.MANIFEST).write_text(f"id: {app_id}\nname: {app_id}\n{SIZE}" + textwrap.dedent(metrics))
    if code is not None:
        (path / apps.MODULE).write_text(textwrap.dedent(code))
    return path


# An app that writes whatever it's told to - {metric, value, date?} - or
# says what ctx.metric() reads of one ("read"); its render shows how many
# points ctx.metric() has of the key its cfg names.
WRITER = """
    def render(ctx, cfg):
        return ctx.number(len(ctx.metric(cfg.get("read", "weight_kg"))))

    def handle(ctx, action, payload):
        if action == "read":
            raise ValueError(repr([(p.date, p.value, p.source) for p in ctx.metric(payload["metric"])]))
        ctx.write_metric(payload["metric"], payload["value"], date=payload.get("date"))
"""

# caffeine_mg, 0-2000 mg a day; and an instant with no range.
COFFEE_METRICS = """
    metrics:
      - {key: caffeine_mg, label: Caffeine, unit: mg, category: "Nutrition & hydration", grain: daily, range: [0, 2000]}
      - {key: cups, label: Cups, unit: cups, category: Other, grain: instant}
"""


def coffee(apps_dir, app_id="coffee", metrics=COFFEE_METRICS):
    return make_app(apps_dir, app_id, metrics, WRITER)


def write(client, app_id, metric, value, day=None):
    payload = {"metric": metric, "value": value}
    if day is not None:
        payload["date"] = day
    return client.post(f"/api/app/{app_id}/action", json={"action": "write", "payload": payload})


def rows(conn):
    """Every reading in metrics, (date, source, metric, value, unit)."""
    return [
        tuple(r) for r in conn.execute(
            "SELECT date, source, metric, value, unit FROM metrics ORDER BY date, source, metric"
        )
    ]


def derived(conn):
    """Every derived pick, (date, metric, value, unit, source)."""
    return [
        tuple(r) for r in conn.execute(
            "SELECT date, metric, value, unit, source FROM derived_metrics ORDER BY date, metric"
        )
    ]


def assert_refused(resp, status=400):
    assert resp.status_code == status
    body = resp.get_json()
    assert body["ok"] is False
    assert body["error"]
    return body["error"]


def skipped(apps_dir, capsys):
    """Whether the apps folder loads nothing, and the reason logged."""
    loaded = apps.scan_apps(str(apps_dir))
    return loaded == [], capsys.readouterr().out


def an_app(app_id, *metrics):
    """An InstalledApp to make a ctx for, declaring `metrics` - no folder."""
    return apps.InstalledApp(
        id=app_id, name=app_id, sizes=[{"w": 1, "h": 1}], default_size={"w": 1, "h": 1},
        reads=[], config=[], path="/nowhere", render=None, metrics=list(metrics),
    )


def declared(key, unit="mg", category="Other", grain="daily", range=None, label=None):
    return {"key": key, "label": label or key, "unit": unit, "category": category,
            "grain": grain, "range": range}


# ---------- the manifest's metrics block ----------


def test_a_metrics_block_loads_and_is_listed(client, apps_dir):
    coffee(apps_dir)

    [app] = client.get("/api/apps").get_json()
    # In the manifest's order, by short key; no range is null.
    assert app["metrics"] == [
        {"key": "caffeine_mg", "label": "Caffeine", "unit": "mg",
         "category": "Nutrition & hydration", "grain": "daily", "range": [0, 2000]},
        {"key": "cups", "label": "Cups", "unit": "cups",
         "category": "Other", "grain": "instant", "range": None},
    ]


@pytest.mark.parametrize("block", ["", "metrics:\n", "metrics: []\n"])
def test_an_app_that_invents_nothing_declares_no_metrics(apps_dir, block):
    make_app(apps_dir, "plain", block)

    [app] = apps.scan_apps(str(apps_dir))
    assert app.metrics == [] and app.summary()["metrics"] == []


def test_each_category_will_do(apps_dir):
    make_app(apps_dir, "every", "metrics:\n" + "".join(
        f'  - {{key: m{i}, label: M, unit: u, category: "{category}", grain: daily}}\n'
        for i, category in enumerate(CATEGORIES)
    ))

    [app] = apps.scan_apps(str(apps_dir))
    assert [m["category"] for m in app.metrics] == list(CATEGORIES)


def test_label_and_unit_are_trimmed_and_a_range_kept_as_written(apps_dir):
    make_app(apps_dir, "tidy", """
        metrics:
          - {key: score, label: "  Focus score ", unit: " pts", category: Other, grain: instant, range: [-1.5, 10]}
    """)

    [app] = apps.scan_apps(str(apps_dir))
    assert app.metrics == [declared("score", "pts", "Other", "instant", [-1.5, 10], "Focus score")]


@pytest.mark.parametrize("key", [STEPS, WEIGHT_KG, "calories_in", "heart_rate", "spo2", "sleep", "workout"])
def test_a_custom_key_cant_shadow_a_canonical_name(apps_dir, capsys, key):
    # A built-in metric is written, never declared - and a series metric
    # or session kind's name is just as taken.
    make_app(apps_dir, "shadow", f"""
        metrics:
          - {{key: {key}, label: Mine, unit: u, category: Other, grain: daily}}
    """)

    none, logged = skipped(apps_dir, capsys)
    assert none
    assert f"metric {key!r} is one of OpenFit's own keys: declare only a metric the app invents" in logged


@pytest.mark.parametrize(
    "block, says",
    [
        # The block, and each entry's shape.
        ("metrics: {key: caffeine_mg}", "metrics must be a list of the metrics the app invents"),
        ("metrics: caffeine_mg", "metrics must be a list of the metrics the app invents"),
        ("metrics: [caffeine_mg]", "each metric must be a mapping of key, label, unit, category, grain, range"),
        # The key: there, an app id's shape, once.
        ("metrics: [{label: C, unit: mg, category: Other, grain: daily}]", "each metric needs a key"),
        ("metrics: [{key: '', label: C, unit: mg, category: Other, grain: daily}]", "each metric needs a key"),
        ("metrics: [{key: 3, label: C, unit: mg, category: Other, grain: daily}]", "each metric needs a key"),
        ("metrics: [{key: caffeine mg, label: C, unit: mg, category: Other, grain: daily}]",
         "each metric needs a key: letters, digits, '-' and '_'"),
        ("metrics: [{key: 'app:x:y', label: C, unit: mg, category: Other, grain: daily}]",
         "each metric needs a key: letters, digits, '-' and '_'"),
        ("metrics: [{key: -mg, label: C, unit: mg, category: Other, grain: daily}]",
         "each metric needs a key: letters, digits, '-' and '_'"),
        ("metrics: [{key: '../mg', label: C, unit: mg, category: Other, grain: daily}]",
         "each metric needs a key: letters, digits, '-' and '_'"),
        ("metrics: [{key: mg, label: C, unit: mg, category: Other, grain: daily},"
         " {key: mg, label: D, unit: g, category: Other, grain: daily}]",
         "metric 'mg' is listed twice"),
        # label and unit: text.
        ("metrics: [{key: mg, unit: mg, category: Other, grain: daily}]", "metric 'mg' needs a label, as text"),
        ("metrics: [{key: mg, label: '  ', unit: mg, category: Other, grain: daily}]", "metric 'mg' needs a label, as text"),
        ("metrics: [{key: mg, label: C, category: Other, grain: daily}]", "metric 'mg' needs a unit, as text"),
        ("metrics: [{key: mg, label: C, unit: '', category: Other, grain: daily}]", "metric 'mg' needs a unit, as text"),
        ("metrics: [{key: mg, label: C, unit: 5, category: Other, grain: daily}]", "metric 'mg' needs a unit, as text"),
        # category: one of CATEGORIES, as written.
        ("metrics: [{key: mg, label: C, unit: mg, category: Stimulants, grain: daily}]",
         "metric 'mg': category must be one of Activity, Vitals,"),
        ("metrics: [{key: mg, label: C, unit: mg, category: other, grain: daily}]",
         "metric 'mg': category must be one of"),
        ("metrics: [{key: mg, label: C, unit: mg, grain: daily}]", "metric 'mg': category must be one of"),
        # grain: daily or instant.
        ("metrics: [{key: mg, label: C, unit: mg, category: Other, grain: session}]",
         "metric 'mg': an app's own metric is daily or instant, not session"),
        ("metrics: [{key: mg, label: C, unit: mg, category: Other, grain: series}]",
         "metric 'mg': an app's own metric is daily or instant, not series"),
        ("metrics: [{key: mg, label: C, unit: mg, category: Other, grain: weekly}]",
         "metric 'mg': grain must be daily or instant: 'weekly'"),
        ("metrics: [{key: mg, label: C, unit: mg, category: Other}]", "metric 'mg': grain must be daily or instant: None"),
        # range: [low, high], finite numbers, low first.
        ("metrics: [{key: mg, label: C, unit: mg, category: Other, grain: daily, range: [10]}]",
         "metric 'mg': range must be [low, high], two numbers"),
        ("metrics: [{key: mg, label: C, unit: mg, category: Other, grain: daily, range: [0, 1, 2]}]",
         "metric 'mg': range must be [low, high], two numbers"),
        ("metrics: [{key: mg, label: C, unit: mg, category: Other, grain: daily, range: ['0', 10]}]",
         "metric 'mg': range must be [low, high], two numbers"),
        ("metrics: [{key: mg, label: C, unit: mg, category: Other, grain: daily, range: [true, 10]}]",
         "metric 'mg': range must be [low, high], two numbers"),
        ("metrics: [{key: mg, label: C, unit: mg, category: Other, grain: daily, range: [0, .inf]}]",
         "metric 'mg': range must be [low, high], two numbers"),
        ("metrics: [{key: mg, label: C, unit: mg, category: Other, grain: daily, range: {low: 0, high: 9}}]",
         "metric 'mg': range must be [low, high], two numbers"),
        ("metrics: [{key: mg, label: C, unit: mg, category: Other, grain: daily, range: [10, 0]}]",
         "metric 'mg': range is [low, high], low first: [10, 0]"),
        # Nothing else: a misspelt range isn't quietly no range.
        ("metrics: [{key: mg, label: C, unit: mg, category: Other, grain: daily, ragne: [0, 9]}]",
         "metric 'mg' has a field a metric doesn't: 'ragne'"),
    ],
)
def test_a_metrics_block_that_wont_do_means_the_app_doesnt_load(apps_dir, capsys, block, says):
    # As a bad config block does: logged, skipped, the others unharmed.
    make_app(apps_dir, "bad", block + "\n")
    make_app(apps_dir, "good", COFFEE_METRICS)

    [good] = apps.scan_apps(str(apps_dir))
    assert good.id == "good"
    logged = capsys.readouterr().out
    assert "[apps] skipping" in logged and "/bad: " in logged
    assert says in logged


# ---------- the catalog: canonical, then the installed apps' own ----------


def test_a_custom_metric_is_in_the_catalog(client, conn, apps_dir):
    coffee(apps_dir)
    write(client, "coffee", "caffeine_mg", 95, day="2026-01-02")
    write(client, "coffee", "caffeine_mg", 180, day="2026-01-04")

    metrics = client.get("/api/catalog").get_json()["metrics"]

    # Its full key, with its declared unit, grain and category, marked as
    # its app's - and only the one with data (cups has none).
    assert metrics == [{
        "metric": "app:coffee:caffeine_mg", "unit": "mg", "grain": "daily",
        "category": "Nutrition & hydration", "sources": ["app:coffee"],
        "first": "2026-01-02", "last": "2026-01-04", "count": 2,
        "last_value": 180.0, "last_date": "2026-01-04",
        "app": "coffee", "label": "Caffeine",
    }]


def test_with_include_empty_an_apps_metrics_follow_the_canonical_ones(client, apps_dir):
    coffee(apps_dir)
    coffee(apps_dir, "tea", "metrics: [{key: theanine_mg, label: L-theanine, unit: mg, category: Other, grain: daily}]\n")

    metrics = client.get("/api/catalog?include_empty=1").get_json()["metrics"]

    # Every canonical key first, as ever, none of them marked; then each
    # app's in folder then manifest order, declared but not yet reporting.
    assert [m["metric"] for m in metrics] == [
        *METRICS, "app:coffee:caffeine_mg", "app:coffee:cups", "app:tea:theanine_mg",
    ]
    assert not any("app" in m or "label" in m for m in metrics[:len(METRICS)])
    assert metrics[-2] == {
        "metric": "app:coffee:cups", "unit": "cups", "grain": "instant", "category": "Other",
        "sources": [], "first": None, "last": None, "count": 0, "last_value": None, "last_date": None,
        "app": "coffee", "label": "Cups",
    }
    assert (metrics[-1]["app"], metrics[-1]["label"]) == ("tea", "L-theanine")


def test_uninstalling_the_app_drops_its_metric_but_keeps_its_data(client, conn, apps_dir):
    coffee(apps_dir)
    write(client, "coffee", "caffeine_mg", 95, day="2026-01-02")

    assert client.delete("/api/apps/coffee").status_code == 200

    # Gone from the vocabulary - the catalog, coverage, its reads...
    catalog = client.get("/api/catalog?include_empty=1").get_json()["metrics"]
    assert [m["metric"] for m in catalog] == list(METRICS)
    assert client.get("/api/coverage").get_json()["metrics"] == {}
    assert_refused(client.get("/api/metric/app:coffee:caffeine_mg"), 404)
    # ... but its readings, and their pick, stay - orphaned with their source.
    assert rows(conn) == [("2026-01-02", "app:coffee", "app:coffee:caffeine_mg", 95.0, "mg")]
    assert derived(conn) == [("2026-01-02", "app:coffee:caffeine_mg", 95.0, "mg", "app:coffee")]
    [orphan] = client.get("/api/app-sources").get_json()
    assert (orphan["id"], orphan["orphaned"], orphan["metrics"]) == ("coffee", True, ["app:coffee:caffeine_mg"])

    # Installed again, it's back, data and all.
    coffee(apps_dir)
    [entry] = client.get("/api/catalog").get_json()["metrics"]
    assert (entry["metric"], entry["count"]) == ("app:coffee:caffeine_mg", 1)


def test_removing_an_uninstalled_apps_data_takes_its_metrics_readings(client, conn, apps_dir):
    # Data lifecycle (A), unchanged: everything app:<id> wrote goes, its
    # own metrics' readings and picks among it.
    coffee(apps_dir)
    write(client, "coffee", "caffeine_mg", 95, day="2026-01-02")
    write(client, "coffee", WEIGHT_KG, 80, day="2026-01-03")
    client.delete("/api/apps/coffee")

    resp = client.delete("/api/app-sources/coffee")

    assert resp.get_json() == {"ok": True, "removed": {"metrics": 2, "sessions": 0, "samples": 0}}
    assert rows(conn) == [] and derived(conn) == []


def test_coverage_lists_an_installed_apps_metric(client, apps_dir):
    coffee(apps_dir)
    write(client, "coffee", "caffeine_mg", 95, day="2026-01-02")
    write(client, "coffee", "caffeine_mg", 180, day="2026-01-04")

    assert client.get("/api/coverage").get_json()["metrics"] == {
        "app:coffee:caffeine_mg": ["2026-01-02", "2026-01-04"],
    }
    assert client.get("/api/coverage?from=2026-01-03").get_json()["metrics"] == {
        "app:coffee:caffeine_mg": ["2026-01-04"],
    }


# ---------- writing ----------


def test_a_custom_metric_is_stored_as_app_id_key(client, conn, apps_dir):
    coffee(apps_dir)

    resp = write(client, "coffee", "caffeine_mg", 95, day="2026-01-02")

    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    # The full key, in the declared unit, under the app's own source - and
    # the day re-derived, as any write's is.
    assert rows(conn) == [("2026-01-02", "app:coffee", "app:coffee:caffeine_mg", 95.0, "mg")]
    assert derived(conn) == [("2026-01-02", "app:coffee:caffeine_mg", 95.0, "mg", "app:coffee")]


def test_an_undeclared_key_is_refused(client, conn, apps_dir):
    coffee(apps_dir)

    for key in ("theanine_mg", "Caffeine_mg", "caffeine"):
        error = assert_refused(write(client, "coffee", key, 95))
        assert error == f"unknown metric: {key!r} - declare it in app.yaml's metrics to write it"
    assert rows(conn) == []


def test_an_app_cant_write_another_apps_custom_key(client, conn, apps_dir):
    coffee(apps_dir)
    coffee(apps_dir, "tea", "")   # declares nothing

    # Not by its short key - it's coffee's declaration, not tea's - nor
    # by its full one: an app's code says only its own short keys.
    for key in ("caffeine_mg", "app:coffee:caffeine_mg"):
        assert "unknown metric" in assert_refused(write(client, "tea", key, 95))
    assert "unknown metric" in assert_refused(write(client, "coffee", "app:coffee:caffeine_mg", 95))
    assert rows(conn) == []


def test_two_apps_declaring_one_key_keep_two_metrics(client, conn, apps_dir):
    coffee(apps_dir)
    coffee(apps_dir, "energy", "metrics: [{key: caffeine_mg, label: Caffeine, unit: g, category: Other, grain: daily}]\n")

    write(client, "coffee", "caffeine_mg", 95, day="2026-01-02")
    write(client, "energy", "caffeine_mg", 0.2, day="2026-01-02")

    # Each under its own app's key, in its own unit; neither picked over the other.
    assert rows(conn) == [
        ("2026-01-02", "app:coffee", "app:coffee:caffeine_mg", 95.0, "mg"),
        ("2026-01-02", "app:energy", "app:energy:caffeine_mg", 0.2, "g"),
    ]
    assert derived(conn) == [
        ("2026-01-02", "app:coffee:caffeine_mg", 95.0, "mg", "app:coffee"),
        ("2026-01-02", "app:energy:caffeine_mg", 0.2, "g", "app:energy"),
    ]
    catalog = client.get("/api/catalog").get_json()["metrics"]
    assert [(m["metric"], m["unit"], m["category"]) for m in catalog] == [
        ("app:coffee:caffeine_mg", "mg", "Nutrition & hydration"),
        ("app:energy:caffeine_mg", "g", "Other"),
    ]


def test_a_canonical_key_still_writes_as_itself(client, conn, apps_dir):
    # Unchanged: an app with metrics of its own still files a built-in one
    # undeclared, under the built-in's key and unit, competing in its pick.
    coffee(apps_dir)

    assert write(client, "coffee", WEIGHT_KG, 80.5, day="2026-01-02").status_code == 200
    assert write(client, "coffee", "calories_in", 2100, day="2026-01-02").status_code == 200

    assert rows(conn) == [
        ("2026-01-02", "app:coffee", "calories_in", 2100.0, "kcal"),
        ("2026-01-02", "app:coffee", "weight_kg", 80.5, "kg"),
    ]
    # And its canonical range still holds.
    assert assert_refused(write(client, "coffee", WEIGHT_KG, 5000)) == "weight_kg must be between 20 and 400 kg, got 5000"


def test_writing_a_custom_day_again_replaces_it(client, conn, apps_dir):
    coffee(apps_dir)

    write(client, "coffee", "cups", 2, day="2026-01-02")
    write(client, "coffee", "cups", 3, day="2026-01-02")

    assert rows(conn) == [("2026-01-02", "app:coffee", "app:coffee:cups", 3.0, "cups")]


def test_a_write_ctx_files_its_apps_metric_and_notes_the_day(conn):
    ctx = host.WriteCtx(conn, an_app("coffee", declared("caffeine_mg")), today=date(2026, 1, 5))

    ctx.write_metric("caffeine_mg", 95)
    ctx.write_metric("caffeine_mg", 60, date="2026-01-03")
    conn.commit()

    assert ctx.written == ["2026-01-03", "2026-01-05"]
    assert rows(conn) == [
        ("2026-01-03", "app:coffee", "app:coffee:caffeine_mg", 60.0, "mg"),
        ("2026-01-05", "app:coffee", "app:coffee:caffeine_mg", 95.0, "mg"),
    ]


# ---------- the range ----------


def test_a_declared_range_refuses_a_value_outside_it(client, conn, apps_dir):
    coffee(apps_dir)

    for value in (-1, -0.01, 2000.01, 1e9):
        assert assert_refused(write(client, "coffee", "caffeine_mg", value)) == (
            f"caffeine_mg must be between 0 and 2000 mg, got {value}"
        )
    assert rows(conn) == []
    # Both ends included.
    write(client, "coffee", "caffeine_mg", 0, day="2026-01-01")
    write(client, "coffee", "caffeine_mg", 2000, day="2026-01-02")
    assert [r[3] for r in rows(conn)] == [0.0, 2000.0]


def test_no_range_takes_any_finite_number(client, conn, apps_dir):
    coffee(apps_dir)

    for day, value in (("2026-01-01", -1e9), ("2026-01-02", 0), ("2026-01-03", 1e12), ("2026-01-04", 2.5)):
        assert write(client, "coffee", "cups", value, day=day).status_code == 200
    assert [r[3] for r in rows(conn)] == [-1e9, 0.0, 1e12, 2.5]


@pytest.mark.parametrize("value", [True, False, "95", None, [95], {"mg": 95}])
def test_a_custom_value_that_isnt_a_number_is_refused(client, conn, apps_dir, value):
    coffee(apps_dir)

    for key in ("caffeine_mg", "cups"):   # with a range, and without
        assert assert_refused(write(client, "coffee", key, value)) == "value must be a number"
    assert rows(conn) == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), 10**400])
def test_a_custom_value_that_isnt_finite_is_refused(conn, value):
    ctx = host.WriteCtx(conn, an_app("coffee", declared("cups")))

    with pytest.raises(ValueError, match="value must be a number"):
        ctx.write_metric("cups", value)
    assert ctx.written == [] and rows(conn) == []


# ---------- reading ----------


def test_ctx_metric_reads_the_apps_own_metric_and_canonical_ones(client, conn, apps_dir):
    coffee(apps_dir)
    write(client, "coffee", "caffeine_mg", 95, day="2026-01-02")
    write(client, "coffee", "caffeine_mg", 180, day="2026-01-04")
    write(client, "coffee", STEPS, 9000, day="2026-01-04")

    def read(metric):
        return client.post("/api/app/coffee/action", json={"action": "read", "payload": {"metric": metric}}).get_json()["error"]

    # By its short key, from app:coffee:caffeine_mg; a canonical key as ever.
    assert read("caffeine_mg") == repr([("2026-01-02", 95.0, "app:coffee"), ("2026-01-04", 180.0, "app:coffee")])
    assert read(STEPS) == repr([("2026-01-04", 9000.0, "app:coffee")])
    # render()'s ctx is the app's too.
    spec = client.post("/api/app/coffee/render", json={"cfg": {"read": "caffeine_mg"}}).get_json()
    assert spec["value"] == 2


def test_ctx_metric_refuses_a_key_that_isnt_the_apps(client, conn, apps_dir):
    coffee(apps_dir)
    coffee(apps_dir, "tea", "")
    write(client, "coffee", "caffeine_mg", 95, day="2026-01-02")

    # Reading another app's metric is out of scope: by either key.
    for key in ("caffeine_mg", "app:coffee:caffeine_mg"):
        spec = client.post("/api/app/tea/render", json={"cfg": {"read": key}}).get_json()
        assert spec == {"type": "error", "message": f"ValueError: unknown metric: {key!r}"}
    # A ctx made for no app reads the canonical metrics only.
    with pytest.raises(ValueError, match="unknown metric"):
        host.Ctx(conn).metric("caffeine_mg")


def test_ctx_metric_keeps_the_trailing_days_of_a_custom_metric(conn):
    ctx = host.WriteCtx(conn, an_app("coffee", declared("caffeine_mg")), today=date(2026, 1, 5))
    for day in ("2026-01-01", "2026-01-04", "2026-01-05"):
        ctx.write_metric("caffeine_mg", 100, date=day)
    recompute_derived(conn)

    assert [p.date for p in ctx.metric("caffeine_mg", days=2)] == ["2026-01-04", "2026-01-05"]


def test_api_metric_serves_a_custom_metric(client, apps_dir):
    coffee(apps_dir)
    write(client, "coffee", "caffeine_mg", 95, day="2026-01-02")
    write(client, "coffee", "caffeine_mg", 180, day="2026-01-04")

    assert client.get("/api/metric/app:coffee:caffeine_mg").get_json() == {
        "metric": "app:coffee:caffeine_mg", "unit": "mg", "points": [
            {"date": "2026-01-02", "value": 95.0, "source": "app:coffee"},
            {"date": "2026-01-04", "value": 180.0, "source": "app:coffee"},
        ],
    }
    # Escaped as the page sends it, and narrowed as any metric's read is.
    assert client.get("/api/metric/app%3Acoffee%3Acaffeine_mg?from=2026-01-03").get_json()["points"] == [
        {"date": "2026-01-04", "value": 180.0, "source": "app:coffee"},
    ]
    # Declared but not yet reporting: known, no points.
    assert client.get("/api/metric/app:coffee:cups").get_json() == {
        "metric": "app:coffee:cups", "unit": "cups", "points": [],
    }
    assert_refused(client.get("/api/metric/app:coffee:caffeine_mg?from=junk"))


@pytest.mark.parametrize("metric", [
    "caffeine_mg",                 # the short key is the app's to say, not the contract's
    "app:coffee:theanine_mg",      # not declared
    "app:tea:caffeine_mg",         # no such app
    "app:coffee",                  # a source, not a metric
    "app:coffee:caffeine_mg:x",
])
def test_api_metric_404s_a_key_that_isnt_in_the_vocabulary(client, apps_dir, metric):
    coffee(apps_dir)

    assert assert_refused(client.get(f"/api/metric/{metric}"), 404) == f"unknown metric: {metric}"


def test_the_webhook_cant_write_an_apps_metric(client, conn, apps_dir):
    # Only the app writes its own metric: the webhook takes canonical keys.
    coffee(apps_dir)
    token = client.get("/api/webhook-token").get_json()["token"]

    resp = client.post(f"/api/webhook/{token}", json={"metric": "app:coffee:caffeine_mg", "value": 95})

    assert "unknown metric" in assert_refused(resp)
    assert rows(conn) == []


# ---------- the caffeine app ----------


CAFFEINE_FIELD = {
    "type": "input", "key": "caffeine_mg", "action": "log",
    "label": "Caffeine today", "kind": "number", "unit": "mg", "options": None, "accept": None,
}


def install_caffeine(client):
    """Install the caffeine app through the Library, from its committed files."""
    folder = REAL_APPS / "caffeine"
    files = {name: (folder / name).read_text() for name in ("app.yaml", "app.py")}
    resp = client.post("/api/apps", json={"files": files})
    assert resp.status_code == 201, resp.get_json()


def log(client, mg):
    return client.post("/api/app/caffeine/action", json={"action": "log", "payload": {"caffeine_mg": mg}})


def test_the_caffeine_app_declares_its_metric(client, apps_dir):
    install_caffeine(client)

    assert client.get("/api/apps").get_json() == [
        {"id": "caffeine", "name": "Caffeine", "library": None, "default_size": {"w": 1, "h": 1},
         "sizes": [{"w": 1, "h": 1}, {"w": 2, "h": 1}], "reads": ["caffeine_mg"], "config": [],
         "metrics": [{"key": "caffeine_mg", "label": "Caffeine", "unit": "mg",
                      "category": "Nutrition & hydration", "grain": "daily", "range": [0, 2000]}],
         "files": ["app.py", "app.yaml"]},
    ]
    # Declared, so in the catalog - in its category - before any data.
    [entry] = [m for m in client.get("/api/catalog?include_empty=1").get_json()["metrics"] if m["metric"] == CAFFEINE]
    assert (entry["category"], entry["grain"], entry["unit"], entry["count"], entry["app"]) == (
        "Nutrition & hydration", "daily", "mg", 0, "caffeine",
    )


def test_the_caffeine_app_logs_and_reads_back_its_own_metric(client, conn, apps_dir):
    # The loop: declare -> listed -> write -> stored as app:<id>:<key> -> read back.
    install_caffeine(client)
    assert client.post("/api/app/caffeine/render").get_json() == {
        "type": "panel", "children": [{"type": "empty", "message": "No caffeine logged yet"}, CAFFEINE_FIELD],
    }

    resp = log(client, "95")

    assert resp.get_json() == {"ok": True, "spec": {
        "type": "panel", "children": [
            {"type": "number", "value": 95.0, "label": None, "caption": "today", "unit": "mg"},
            CAFFEINE_FIELD,
        ],
    }}
    assert rows(conn) == [(TODAY, "app:caffeine", CAFFEINE, 95.0, "mg")]
    assert client.get(f"/api/metric/{CAFFEINE}").get_json() == {
        "metric": CAFFEINE, "unit": "mg", "points": [{"date": TODAY, "value": 95.0, "source": "app:caffeine"}],
    }
    [entry] = client.get("/api/catalog").get_json()["metrics"]
    assert (entry["metric"], entry["last_value"], entry["label"]) == (CAFFEINE, 95.0, "Caffeine")

    # One figure a day: a second log replaces the first.
    log(client, 140)
    assert rows(conn) == [(TODAY, "app:caffeine", CAFFEINE, 140.0, "mg")]


def test_the_caffeine_app_shows_an_earlier_days_date(client, conn, apps_dir):
    install_caffeine(client)
    host.WriteCtx(conn, apps.find_app(str(apps_dir), "caffeine")).write_metric("caffeine_mg", 60, date="2026-01-02")
    recompute_derived(conn)

    [number, _] = client.post("/api/app/caffeine/render").get_json()["children"]
    assert (number["value"], number["caption"]) == (60.0, "2026-01-02")


def test_the_caffeine_app_is_held_to_its_declared_range(client, conn, apps_dir):
    install_caffeine(client)

    assert assert_refused(log(client, 5000)) == "caffeine_mg must be between 0 and 2000 mg, got 5000.0"
    assert rows(conn) == []


@pytest.mark.parametrize("junk", ["abc", "", None, True, -5, "nan", "inf", [95]])
def test_the_caffeine_app_refuses_junk(client, conn, apps_dir, junk):
    install_caffeine(client)

    assert assert_refused(log(client, junk)) == "Enter today's caffeine in mg, as a number"
    assert rows(conn) == []


def test_the_caffeine_fixture_runs_verbatim_from_the_folder(client, conn, apps_dir):
    # As deployed by copying onto the volume, not just through the Library.
    shutil.copytree(REAL_APPS / "caffeine", apps_dir / "caffeine", ignore=shutil.ignore_patterns("__pycache__"))

    assert log(client, 0).status_code == 200
    assert rows(conn) == [(TODAY, "app:caffeine", CAFFEINE, 0.0, "mg")]
