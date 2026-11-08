"""
App-wide settings (C4): the user's settings for a whole app, set once.

- Declared in the manifest's `settings:` - a list of fields, in display
  order, each {key, label, help?, type, default} and its type's own
  (text max_length; number min, max, integer; select options) - checked
  strictly: a field that breaks a rule is an AppError and the app
  doesn't load. InstalledApp.settings, and summary() lists them.
- Stored as one record in OpenFit's own collection: app_records (app,
  "_settings", "values"), {key: value}. Written only by the settings
  route; an app's put_record() still refuses every _ collection.
- Read by the app: ctx.settings() -> every declared key, the stored value
  where it's still valid under the manifest now, else the default (a
  stale one passed over, never an error); ctx.setting(key), KeyError for
  an undeclared key.
- Set by the user: GET /api/app/<id>/settings -> {fields, values}; PUT
  {values} -> {ok, values}, every key declared and every value valid
  (else 400 naming the field, nothing saved), a key left out kept.
- App level 7.

Every test gets its own temp database and apps folder.
"""

import json
import sqlite3
import textwrap

import pytest

import apps
import host
import main

SIZE = "sizes: [{w: 1, h: 1}]\n"

SETTINGS = """\
requires: 7
settings:
  - key: home_base
    label: Home base
    help: The airport you fly from.
    type: text
    default: ""
    max_length: 4
  - key: rest_target
    label: Rest target (hours)
    type: number
    default: 10
    min: 0
    max: 24
  - key: sectors
    label: Sectors
    type: number
    default: 2
    integer: true
  - key: units
    label: Units
    type: select
    options: [metric, imperial]
    default: metric
  - key: show_wocl
    label: Show the low-alertness window
    type: toggle
    default: true
"""

DEFAULTS = {"home_base": "", "rest_target": 10, "sectors": 2, "units": "metric", "show_wocl": True}

# Draws its settings, so a render shows what it read.
CODE = """
import json

def render(ctx, cfg):
    return ctx.empty(json.dumps(ctx.settings()))

def handle(ctx, action, payload):
    if action == "write_settings":
        ctx.put_record("_settings", "values", {"units": "imperial"})
    elif action == "one":
        return None
"""


@pytest.fixture
def apps_dir(tmp_path, monkeypatch):
    path = tmp_path / "apps"
    path.mkdir()
    monkeypatch.setitem(main.app.config, "APPS_DIR", str(path))
    return path


def make_app(root, app_id, manifest=SETTINGS, code=CODE):
    path = root / app_id
    path.mkdir()
    own = app_id.split(".", 1)[1] if "." in app_id else app_id
    (path / apps.MANIFEST).write_text(f"id: {own}\nname: Crew\n{SIZE}{manifest}")
    if code is not None:
        (path / apps.MODULE).write_text(textwrap.dedent(code))
    return path


@pytest.fixture
def crew(apps_dir):
    make_app(apps_dir, "crew")
    return apps_dir / "crew"


def load(tmp_path, settings_yaml, requires=7):
    """The app a manifest with these settings loads as (AppError if not)."""
    folder = tmp_path / "a"
    folder.mkdir(exist_ok=True)
    (folder / apps.MANIFEST).write_text(f"id: a\n{SIZE}requires: {requires}\n" + settings_yaml)
    return apps.load_app(str(folder))


def field_yaml(**fields):
    """A manifest's settings: one field, as YAML (JSON is YAML)."""
    return "settings: " + json.dumps([fields]) + "\n"


TEXT = {"key": "k", "label": "K", "type": "text", "default": ""}


def stored(conn, app="crew"):
    return [
        tuple(r) for r in conn.execute(
            "SELECT collection, key, value FROM app_records WHERE app = ? ORDER BY collection, key", (app,)
        )
    ]


def put(client, values, app_id="crew"):
    return client.put(f"/api/app/{app_id}/settings", json={"values": values})


def rendered(client, app_id="crew"):
    spec = client.post(f"/api/app/{app_id}/render", json={}).get_json()
    assert spec["type"] == "empty", spec
    return json.loads(spec["message"])


# ---------- 1. the manifest ----------

def test_a_valid_manifest_loads_and_its_summary_carries_the_settings(tmp_path):
    app = load(tmp_path, SETTINGS.split("\n", 1)[1])
    assert [f["key"] for f in app.settings] == list(DEFAULTS)
    assert app.settings[0] == {
        "key": "home_base", "label": "Home base", "help": "The airport you fly from.",
        "type": "text", "max_length": 4, "default": "",
    }
    assert app.settings[1] == {
        "key": "rest_target", "label": "Rest target (hours)", "help": None, "type": "number",
        "min": 0, "max": 24, "integer": False, "default": 10,
    }
    assert app.settings[3]["options"] == ["metric", "imperial"]
    summary = app.summary()["settings"]
    assert summary == app.settings
    # A copy: the listing can't change the app.
    summary[3]["options"].append("x")
    assert app.settings[3]["options"] == ["metric", "imperial"]


def test_no_settings_is_none_and_a_text_field_gets_its_default_length(tmp_path):
    assert load(tmp_path, "").settings == []
    assert load(tmp_path, field_yaml(**TEXT)).settings[0]["max_length"] == 200


@pytest.mark.parametrize("fields, why", [
    ({**TEXT, "key": "Home"}, "needs a key"),
    ({**TEXT, "key": "1st"}, "needs a key"),
    ({**TEXT, "key": "a-b"}, "needs a key"),
    ({**TEXT, "key": "k" * 41}, "needs a key"),
    ({**TEXT, "type": "colour"}, "type must be one of"),
    ({k: v for k, v in TEXT.items() if k != "default"}, "needs a default"),
    ({**TEXT, "label": ""}, "label must be text of 1 to 80"),
    ({**TEXT, "label": "x" * 81}, "label must be text of 1 to 80"),
    ({**TEXT, "help": "x" * 201}, "help must be text of 1 to 200"),
    ({**TEXT, "default": 3}, "its default must be text"),
    ({**TEXT, "max_length": 0}, "max_length must be a whole number from 1 to 500"),
    ({**TEXT, "max_length": 501}, "max_length must be a whole number from 1 to 500"),
    ({**TEXT, "max_length": True}, "max_length must be a whole number"),
    ({**TEXT, "max_length": 2, "default": "abc"}, "its default must be at most 2 characters"),
    ({**TEXT, "maxlength": 2}, "has a field a text setting doesn't"),
    ({**TEXT, "options": ["a"]}, "has a field a text setting doesn't"),
    ({"key": "n", "label": "N", "type": "number", "default": "10"}, "its default must be a number"),
    ({"key": "n", "label": "N", "type": "number", "default": True}, "its default must be a number"),
    ({"key": "n", "label": "N", "type": "number", "default": 1, "min": 5, "max": 2}, "min must not be more than max"),
    ({"key": "n", "label": "N", "type": "number", "default": 30, "min": 0, "max": 24}, "its default must be at most 24"),
    ({"key": "n", "label": "N", "type": "number", "default": -1, "min": 0}, "its default must be at least 0"),
    ({"key": "n", "label": "N", "type": "number", "default": 1.5, "integer": True}, "its default must be a whole number"),
    ({"key": "n", "label": "N", "type": "number", "default": 1, "integer": "yes"}, "integer must be true or false"),
    ({"key": "n", "label": "N", "type": "number", "default": 1, "min": "0"}, "min must be a finite number"),
    ({"key": "s", "label": "S", "type": "select", "options": [], "default": "a"}, "options must be a list of 1 to 50"),
    ({"key": "s", "label": "S", "type": "select", "default": "a"}, "options must be a list of 1 to 50"),
    ({"key": "s", "label": "S", "type": "select", "options": ["a", "a"], "default": "a"}, "listed twice"),
    ({"key": "s", "label": "S", "type": "select", "options": ["a", ""], "default": "a"}, "each option must be text"),
    ({"key": "s", "label": "S", "type": "select", "options": ["a", 2], "default": "a"}, "each option must be text"),
    ({"key": "s", "label": "S", "type": "select", "options": [str(i) for i in range(51)], "default": "0"},
     "options must be a list of 1 to 50"),
    ({"key": "s", "label": "S", "type": "select", "options": ["a", "b"], "default": "c"}, "its default must be one of a, b"),
    ({"key": "t", "label": "T", "type": "toggle", "default": "true"}, "its default must be true or false"),
    ({"key": "t", "label": "T", "type": "toggle", "default": 1}, "its default must be true or false"),
])
def test_a_field_that_breaks_a_rule_is_refused(tmp_path, fields, why):
    with pytest.raises(apps.AppError) as e:
        load(tmp_path, field_yaml(**fields))
    assert why in str(e.value)


def test_a_key_twice_too_many_fields_or_not_a_list_is_refused(tmp_path):
    with pytest.raises(apps.AppError, match="'k' is listed twice"):
        load(tmp_path, "settings: " + json.dumps([TEXT, TEXT]) + "\n")
    many = [{**TEXT, "key": f"k{i}"} for i in range(31)]
    with pytest.raises(apps.AppError, match="at most 30 fields"):
        load(tmp_path, "settings: " + json.dumps(many) + "\n")
    assert len(load(tmp_path, "settings: " + json.dumps(many[:30]) + "\n").settings) == 30
    with pytest.raises(apps.AppError, match="settings must be a list"):
        load(tmp_path, "settings: {k: 1}\n")
    with pytest.raises(apps.AppError, match="each setting must be a mapping"):
        load(tmp_path, "settings: [k]\n")


def test_an_app_with_bad_settings_doesnt_load_and_isnt_listed(client, apps_dir):
    make_app(apps_dir, "crew", manifest="requires: 7\n" + field_yaml(**{**TEXT, "default": 3}))
    assert apps.find_app(str(apps_dir), "crew") is None
    assert client.get("/api/apps").get_json() == []


# ---------- 2. reads ----------

@pytest.fixture
def app(crew):
    return apps.find_app(str(crew.parent), "crew")


def set_stored(conn, values, app_id="crew"):
    conn.execute(
        "INSERT INTO app_records (app, collection, key, value, size, created_at, updated_at) "
        "VALUES (?, '_settings', 'values', ?, 1, 'x', 'x') "
        "ON CONFLICT (app, collection, key) DO UPDATE SET value = excluded.value",
        (app_id, json.dumps(values)),
    )
    conn.commit()


def test_defaults_with_nothing_stored(conn, app):
    ctx = host.Ctx(conn, app=app)
    assert ctx.settings() == DEFAULTS
    assert list(ctx.settings()) == list(DEFAULTS)
    assert ctx.setting("units") == "metric"


def test_a_stored_value_wins(conn, app):
    set_stored(conn, {"units": "imperial", "rest_target": 8.5})
    ctx = host.Ctx(conn, app=app)
    assert ctx.settings() == {**DEFAULTS, "units": "imperial", "rest_target": 8.5}
    assert ctx.setting("rest_target") == 8.5
    # Read afresh each call.
    set_stored(conn, {"units": "metric"})
    assert ctx.setting("units") == "metric"


@pytest.mark.parametrize("stale", [
    {"gone": "x"},                       # a key no longer declared
    {"units": "nautical"},               # an option that's gone
    {"rest_target": 30},                 # out of range now
    {"rest_target": "8"},                # the wrong type
    {"sectors": 2.5},                    # no longer whole
    {"show_wocl": 1},                    # 1 isn't a bool
    {"home_base": "TOOLONG"},            # over max_length
    {"home_base": "a\tb"},               # not printable
])
def test_a_stale_stored_value_reads_as_the_default_never_an_error(conn, app, stale):
    set_stored(conn, {**stale, "units": stale.get("units", "imperial")})
    got = host.Ctx(conn, app=app).settings()
    expected = {**DEFAULTS}
    if "units" not in stale:
        expected["units"] = "imperial"
    assert got == expected


def test_a_stored_record_that_isnt_an_object_reads_as_defaults(conn, app):
    set_stored(conn, ["units", "imperial"])
    assert host.Ctx(conn, app=app).settings() == DEFAULTS


def test_an_undeclared_key_is_a_key_error(conn, app):
    ctx = host.Ctx(conn, app=app)
    with pytest.raises(KeyError):
        ctx.setting("nope")
    with pytest.raises(KeyError):
        ctx.setting(3)


def test_a_ctx_for_no_app_has_no_settings(conn):
    ctx = host.Ctx(conn)
    with pytest.raises(ValueError, match="settings belong to an app"):
        ctx.settings()
    with pytest.raises(ValueError, match="settings belong to an app"):
        ctx.setting("units")


def test_an_app_with_no_settings_reads_none(conn, apps_dir):
    make_app(apps_dir, "plain", manifest="")
    assert host.Ctx(conn, app=apps.find_app(str(apps_dir), "plain")).settings() == {}


# ---------- 3. the route ----------

def test_get_gives_the_fields_and_the_values(client, crew):
    resp = client.get("/api/app/crew/settings")
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["values"] == DEFAULTS
    assert body["fields"] == apps.find_app(str(crew.parent), "crew").summary()["settings"]
    assert [f["key"] for f in body["fields"]] == list(DEFAULTS)


def test_put_saves_and_a_partial_put_keeps_the_rest(client, conn, crew):
    resp = put(client, {"units": "imperial", "home_base": "HKG", "sectors": 4.0})
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "values": {**DEFAULTS, "units": "imperial", "home_base": "HKG", "sectors": 4}}

    resp = put(client, {"show_wocl": False})
    assert resp.get_json()["values"] == {
        **DEFAULTS, "units": "imperial", "home_base": "HKG", "sectors": 4, "show_wocl": False,
    }
    assert client.get("/api/app/crew/settings").get_json()["values"] == resp.get_json()["values"]
    # To reset a key, the page sends its default.
    assert put(client, {"units": "metric"}).get_json()["values"]["units"] == "metric"
    assert put(client, {}).status_code == 200


def test_a_put_moves_updated_at(client, conn, crew, pin_clock):
    from datetime import datetime, timezone

    pin_clock(datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc))
    put(client, {"units": "imperial"})
    pin_clock(datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc))
    put(client, {"units": "metric"})
    row = conn.execute(
        "SELECT created_at, updated_at FROM app_records WHERE app = 'crew' AND collection = '_settings'"
    ).fetchone()
    assert tuple(row) == ("2026-11-02T09:00:00Z", "2026-11-02T10:00:00Z")


@pytest.mark.parametrize("values, field", [
    ({"home_base": 5}, "home_base"),
    ({"home_base": "TOOLONG"}, "home_base"),
    ({"home_base": "a\nb"}, "home_base"),
    ({"rest_target": 25}, "rest_target"),
    ({"rest_target": -1}, "rest_target"),
    ({"rest_target": "8"}, "rest_target"),
    ({"rest_target": True}, "rest_target"),
    ({"rest_target": None}, "rest_target"),
    ({"sectors": 2.5}, "sectors"),
    ({"units": "nautical"}, "units"),
    ({"units": None}, "units"),
    ({"show_wocl": "true"}, "show_wocl"),
    ({"show_wocl": 0}, "show_wocl"),
    # One bad value among good ones: none of them saved.
    ({"units": "imperial", "rest_target": 99}, "rest_target"),
])
def test_an_invalid_value_is_a_400_naming_the_field_with_nothing_saved(client, conn, crew, values, field):
    assert put(client, {"home_base": "LHR"}).status_code == 200
    before = stored(conn)
    resp = put(client, values)
    assert resp.status_code == 400
    body = resp.get_json()
    assert body["ok"] is False and body["field"] == field
    assert f"({field})" in body["error"]
    assert stored(conn) == before


def test_an_out_of_range_number_says_its_bound(client, crew):
    assert put(client, {"rest_target": 25}).get_json()["error"] == "Rest target (hours) (rest_target) must be at most 24"


def test_an_unknown_key_is_a_400(client, conn, crew):
    resp = put(client, {"units": "imperial", "nope": 1})
    assert resp.status_code == 400
    assert resp.get_json()["field"] == "nope"
    assert stored(conn) == []


@pytest.mark.parametrize("body", [None, [], {"values": []}, {"values": "x"}, {}, {"units": "imperial"}])
def test_a_body_without_an_object_of_values_is_a_400(client, conn, crew, body):
    resp = client.put("/api/app/crew/settings", data=json.dumps(body), content_type="application/json")
    assert resp.status_code == 400
    assert stored(conn) == []


def test_an_app_not_installed_or_with_no_settings_is_a_404(client, apps_dir):
    assert client.get("/api/app/crew/settings").status_code == 404
    assert put(client, {}).status_code == 404
    make_app(apps_dir, "plain", manifest="")
    assert client.get("/api/app/plain/settings").status_code == 404
    assert put(client, {}, app_id="plain").status_code == 404


def test_signed_out_is_a_401(guest, crew):
    assert guest.get("/api/app/crew/settings").status_code == 401
    resp = guest.put("/api/app/crew/settings", json={"values": {}}, headers={"X-OpenFit": "1"})
    assert resp.status_code == 401


@pytest.fixture
def guest(db_path, monkeypatch):
    monkeypatch.setitem(main.app.config, "DB_PATH", db_path)
    return main.app.test_client()


def test_a_put_without_the_csrf_header_is_a_403(client, conn, crew):
    del client.environ_base["HTTP_X_OPENFIT"]
    for headers in ({}, {"X-OpenFit": "0"}):
        resp = client.put("/api/app/crew/settings", json={"values": {"units": "imperial"}}, headers=headers)
        assert resp.status_code == 403
    assert stored(conn) == []
    # A read needs none.
    assert client.get("/api/app/crew/settings").status_code == 200


def test_a_library_apps_settings_are_the_users(client, conn, apps_dir):
    make_app(apps_dir, "openfitavi.crew")
    assert client.get("/api/app/openfitavi.crew/settings").get_json()["values"] == DEFAULTS
    resp = put(client, {"units": "imperial"}, app_id="openfitavi.crew")
    assert resp.status_code == 200
    assert stored(conn, "openfitavi.crew") == [("_settings", "values", '{"units":"imperial"}')]
    assert rendered(client, "openfitavi.crew")["units"] == "imperial"


# ---------- 4. storage ----------

def test_the_record_is_settings_values_holding_what_was_set(client, conn, crew):
    put(client, {"units": "imperial", "rest_target": 8})
    assert stored(conn) == [("_settings", "values", '{"rest_target":8,"units":"imperial"}')]


def test_a_put_drops_a_stale_stored_value(client, conn, crew):
    set_stored(conn, {"units": "nautical", "gone": 1, "home_base": "HKG"})
    put(client, {"rest_target": 8})
    assert json.loads(stored(conn)[0][2]) == {"home_base": "HKG", "rest_target": 8}


def test_an_app_still_cant_write_settings_through_put_record(client, conn, crew):
    resp = client.post("/api/app/crew/action", json={"action": "write_settings", "payload": {}})
    assert resp.status_code == 400
    assert "reserved" in resp.get_json()["error"]
    assert stored(conn) == []
    with pytest.raises(ValueError, match="reserved"):
        host.WriteCtx(conn, apps.find_app(str(crew.parent), "crew")).put_record("_settings", "values", {})
    # Nor read them as a record.
    with pytest.raises(ValueError, match="reserved"):
        host.Ctx(conn, app=apps.find_app(str(crew.parent), "crew")).record("_settings", "values")


def test_delete_data_removes_the_settings(client, conn, crew):
    put(client, {"units": "imperial"})
    assert client.delete("/api/apps/crew").status_code == 200
    resp = client.delete("/api/app-sources/crew")
    assert resp.get_json()["removed"]["records"] == 1
    assert stored(conn) == []


def test_uninstall_then_reinstall_keeps_them(client, conn, apps_dir):
    files = {"app.yaml": f"id: crew\nname: Crew\n{SIZE}{SETTINGS}", "app.py": textwrap.dedent(CODE)}
    assert client.post("/api/apps", json={"files": files}).status_code == 201
    put(client, {"units": "imperial"})
    assert client.delete("/api/apps/crew").status_code == 200
    # Uninstalled: still stored, listed as the app's records.
    listing = {e["id"]: e for e in client.get("/api/app-sources").get_json()}
    assert listing["crew"]["orphaned"] is True and listing["crew"]["records"]["count"] == 1
    assert client.post("/api/apps", json={"files": files}).status_code == 201
    assert client.get("/api/app/crew/settings").get_json()["values"]["units"] == "imperial"


def test_they_count_in_the_records_total(client, conn, crew, monkeypatch):
    put(client, {"units": "imperial"})
    size = len('{"units":"imperial"}')
    listing = {e["id"]: e for e in client.get("/api/app-sources").get_json()}
    assert listing["crew"]["records"] == {"count": 1, "bytes": size}
    # At the app's total, a settings write that grows is refused, said.
    monkeypatch.setattr(host, "MAX_APP_RECORD_BYTES", size + 5)
    resp = put(client, {"home_base": "HKG"})
    assert resp.status_code == 400 and "50 MB of records" in resp.get_json()["error"]
    assert json.loads(stored(conn)[0][2]) == {"units": "imperial"}
    # And they count against an app's own put_record.
    monkeypatch.setattr(host, "MAX_APP_RECORD_BYTES", size + 3)
    with pytest.raises(ValueError, match="50 MB of records"):
        host.WriteCtx(conn, apps.find_app(str(crew.parent), "crew")).put_record("c", "k", "abcd")


# ---------- 5. render ----------

def test_the_next_render_sees_a_put(client, crew):
    assert rendered(client) == DEFAULTS
    put(client, {"units": "imperial", "show_wocl": False})
    assert rendered(client) == {**DEFAULTS, "units": "imperial", "show_wocl": False}


def test_handle_reads_them_too(client, conn, crew):
    code = """
def render(ctx, cfg):
    return ctx.empty("x")

def handle(ctx, action, payload):
    raise ValueError("units " + ctx.setting("units"))
"""
    (crew / apps.MODULE).write_text(textwrap.dedent(code) + "\n# changed\n")
    put(client, {"units": "imperial"})
    resp = client.post("/api/app/crew/action", json={"action": "x", "payload": {}})
    assert resp.get_json()["error"] == "units imperial"


# ---------- 6. the level ----------

def test_the_app_level_is_7_and_8_waits(tmp_path):
    assert apps.APP_LEVEL == 8
    assert load(tmp_path, "", requires=7).requires == 7
    with pytest.raises(apps.AppError, match="needs OpenFit app level 9; this OpenFit is level 8"):
        load(tmp_path, "", requires=9)


# ---------- 7. the page ----------
#
# The app row's Settings button and its popup, run under node over the
# stand-in DOM the other page tests use, against what the server hands
# out: /api/apps, the settings GET, and the PUT's reply - each the real
# route's.

import shutil  # noqa: E402

from test_routes import PARSING_DOM, _js, _js_function, _node, _page  # noqa: E402

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")

# Labels, help and an option with markup in them: the page escapes each.
MARKED = SETTINGS.replace("label: Home base", 'label: "Home <b>base</b>"').replace(
    "help: The airport you fly from.", 'help: "The airport <i>you</i> fly from & back."'
).replace("options: [metric, imperial]", 'options: [metric, "imp<erial>"]')

PAGE_PARTS = [
    "$", "el", "appsList", "appPanelId", "writesTicker", "openModal",
    "settingField", "openAppSettings", "appPanel",
]

PAGE_HARNESS = r"""
    const metricName = k => k;
    const setTimeout = () => 0, clearTimeout = () => {};
    const copy = x => JSON.parse(JSON.stringify(x));
    const APPS_ = APPS, GOT_ = GOT;
    let saveReply = SAVE;
    const reads = [], saves = [], renders = [], repaints = [];
    const api = {
      appSettings: async id => { reads.push(id); return copy(GOT_); },
      saveAppSettings: async (id, values) => { saves.push([id, copy(values)]); return copy(saveReply); },
      renderApp: async (id, cfg) => { renders.push([id, cfg]); return {type:'empty', message:'fresh'}; },
    };
    const tileCfg = id => ({tile: id});
    const repaintTile = (id, spec) => { repaints.push([id, spec]); };
    const r = {};
    const tick = () => new Promise(res => setImmediate(res));
    const row = id => appPanel(APPS_.find(a => a.id===id));
    const acts = p => p.querySelectorAll('.a-acts button').map(b => b.dataset.act);
    const popup = () => {
      const bg = document.body.children[document.body.children.length-1];
      return bg ? {bg, box: bg.children[0], q: s => bg.children[0].querySelector(s)} : null;
    };
"""


def _page_script(page, listed, got, save_reply, scenario):
    import re

    esc = re.search(r"\n  (const esc = .*?\);)\n", page, re.S).group(1)
    harness = (
        PAGE_HARNESS.replace("APPS_ = APPS", f"APPS_ = {json.dumps(listed)}")
        .replace("GOT_ = GOT", f"GOT_ = {json.dumps(got)}")
        .replace("saveReply = SAVE", f"saveReply = {json.dumps(save_reply)}")
    )
    return (
        PARSING_DOM + esc + "\n" + "\n".join(_js(page, n) for n in PAGE_PARTS) + harness
        + "(async () => {\n" + scenario + "\nconsole.log(JSON.stringify(r));\n})()"
        + ".catch(e => { console.error(e); process.exit(1); });"
    )


@pytest.fixture
def page_apps(apps_dir):
    make_app(apps_dir, "crew", manifest=MARKED)
    make_app(apps_dir, "plain", manifest="")
    return apps_dir


def _run_page(client, scenario, save_reply=None):
    listed = client.get("/api/apps").get_json()
    got = client.get("/api/app/crew/settings").get_json()
    return _node(_page_script(_page(client), listed, got, save_reply or {"ok": True}, scenario))


OPEN = """
    const p = row('crew');
    await p.querySelector('[data-act="settings"]').onclick();
    const m = popup();
    r.html = m.box.innerHTML;
    const f = i => m.q('#appSet'+i);
"""


@needs_node
def test_the_settings_button_shows_only_for_an_app_with_settings(client, page_apps):
    r = _run_page(client, """
        r.plain = acts(row('plain'));
        r.crew = acts(row('crew'));
    """)
    assert "settings" not in r["plain"]
    assert r["crew"][:2] == ["refresh", "settings"]
    assert r["plain"][0] == "refresh"


@needs_node
def test_the_form_builds_each_type_and_escapes_every_label(client, page_apps):
    r = _run_page(client, OPEN + """
        r.reads = reads;
        r.title = m.q('h4').textContent;
        r.hint = m.q('.hint').textContent;
        r.values = [0, 1, 2, 3].map(i => f(i).value);
        r.checked = f(4).checked;
        r.types = [0, 1, 2, 3, 4].map(i => [f(i).tag, f(i).attrs.type || null]);
        r.options = m.box.querySelectorAll('#appSet3 option').map(o => [o.attrs.value, o.textContent]);
    """)
    html = r["html"]
    assert r["reads"] == ["crew"]
    assert r["title"] == "Crew settings"
    assert r["hint"] == "Applies to this app everywhere it appears, for you. A tile's own settings are separate."
    # Each type its input, with the field's limits as attributes.
    assert 'id="appSet0" type="text" maxlength="4"' in html
    assert 'id="appSet1" type="number" step="any" min="0" max="24"' in html
    assert 'id="appSet2" type="number" step="1" autocomplete' in html
    assert '<select class="input" id="appSet3">' in html
    assert 'id="appSet4" type="checkbox"' in html
    assert r["types"] == [["input", "text"], ["input", "number"], ["input", "number"], ["select", None],
                          ["input", "checkbox"]]
    # Labels, help and options escaped: text, never markup.
    assert '<label for="appSet0">Home &lt;b&gt;base&lt;/b&gt;</label>' in html
    assert '<p class="hint as-help">The airport &lt;i&gt;you&lt;/i&gt; fly from &amp; back.</p>' in html
    assert r["options"] == [["metric", "metric"], ["imp<erial>", "imp<erial>"]]
    assert "<b>" not in html and "<i>" not in html and "<erial>" not in html
    # Values from the server's reply, set as .value / .checked.
    assert r["values"] == ["", "10", "2", "metric"] and r["checked"] is True
    assert "value=\"10\"" not in html


@needs_node
def test_save_puts_the_values_and_redraws_the_apps_tile(client, conn, page_apps):
    r = _run_page(client, OPEN + """
        f(0).value = 'HKG'; f(1).value = '8.5'; f(2).value = '3'; f(3).value = 'imp<erial>';
        f(4).checked = false;
        await m.q('[data-act="save"]').onclick();
        await tick();
        r.saves = saves; r.closed = !m.bg.parent;
        r.renders = renders; r.repaints = repaints;
        r.msg = p.querySelector('.a-msg').textContent;
    """)
    [[app_id, values]] = r["saves"]
    assert app_id == "crew"
    assert values == {"home_base": "HKG", "rest_target": 8.5, "sectors": 3, "units": "imp<erial>",
                      "show_wocl": False}
    assert r["closed"] is True
    # The app's tile drawn again, with its tile's config, from the fresh spec.
    assert r["renders"] == [["crew", {"tile": "crew"}]]
    assert r["repaints"] == [["crew", {"type": "empty", "message": "fresh"}]]
    assert r["msg"] == "✓ Settings saved"
    # What the page sent, the server takes.
    resp = put(client, values)
    assert resp.status_code == 200 and resp.get_json()["values"] == values


@needs_node
def test_a_400_keeps_the_popup_open_with_the_servers_message(client, conn, page_apps):
    refused = put(client, {"rest_target": 99})
    assert refused.status_code == 400
    reply = {"ok": False, **refused.get_json()}
    r = _run_page(client, OPEN + """
        f(1).value = '99';
        await m.q('[data-act="save"]').onclick();
        r.open = !!m.bg.parent;
        r.msg = [m.q('.as-msg').className, m.q('.as-msg').textContent];
        r.saveOn = !m.q('[data-act="save"]').disabled;
        r.renders = renders.length;
        // A number field left blank is said before anything is sent.
        const before = saves.length;
        f(1).value = '  ';
        await m.q('[data-act="save"]').onclick();
        r.blank = [saves.length - before, m.q('.as-msg').textContent];
    """, save_reply=reply)
    assert r["open"] is True and r["saveOn"] is True and r["renders"] == 0
    assert r["msg"] == ["form-msg as-msg bad", "Rest target (hours) (rest_target) must be at most 24"]
    assert r["blank"] == [0, "Enter a number for Rest target (hours)."]
    assert stored(conn) == []


def test_the_page_reads_and_writes_settings_only_through_its_api(client):
    page = _page(client)
    assert "appSettings:  id             => fetchJSON('/api/app/'+enc(id)+'/settings')," in page
    assert "saveAppSettings: (id, values) => send('/api/app/'+enc(id)+'/settings', 'PUT', {values})," in page
    form = _js_function(page, "openAppSettings")
    assert "${esc(name)} settings" in form
    assert "innerHTML" not in form and "${v" not in form
    # The tile config popup is as it was.
    assert "Saved with the layout as you switch Configure off." in _js_function(page, "openTileConfig")
