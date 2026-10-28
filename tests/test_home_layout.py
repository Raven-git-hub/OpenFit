"""
The Home layout: GET/PUT /api/home-layout and the home_layout settings row.

{"tabs": [{id, name, mode: "flow", subtitle, items}], "default_tab"}:
each tab a flow layout, its items an ordered list of instances -
{instance_id, app_id, size: {w, h}, config} - the order the layout, each
size one the app declares, and the same app placeable more than once.
The save stores what it's given once its shape is checked (an instance
sent without an id minted one), and nothing more; every read squares the
stored instances with the apps installed now: an uninstalled app's
instance dropped, a size its app no longer offers back to the default,
and any app with no instance on any tab given one, at the end of the
default tab. A row from before tabs - {mode, subtitle, items: [{app_id,
size}]} - reads as one tab, "home", showing just what it showed.

Every test gets its own temp apps folder; nothing reads /data/apps.
"""

import json
from pathlib import Path

import pytest

import apps
import home_layout

REAL_APPS = Path(__file__).parent / "fixtures" / "apps"


@pytest.fixture
def apps_dir(tmp_path, monkeypatch):
    """An empty apps folder, which the app is pointed at."""
    import main

    path = tmp_path / "apps"
    path.mkdir()
    monkeypatch.setitem(main.app.config, "APPS_DIR", str(path))
    return path


def install(client, app_id, sizes="[{w: 1, h: 1}]", name=None):
    manifest = f"id: {app_id}\nsizes: {sizes}\n" + (f"name: {name}\n" if name else "")
    resp = client.post("/api/apps", json={"files": {"app.yaml": manifest}})
    assert resp.status_code == 201, resp.get_json()


def uninstall(client, app_id):
    assert client.delete(f"/api/apps/{app_id}").status_code == 200


def layout(client):
    resp = client.get("/api/home-layout")
    assert resp.status_code == 200
    return resp.get_json()


def save(client, body):
    return client.put("/api/home-layout", json=body)


def store(client, value):
    """Write the row as /api/settings can: anything at all."""
    value = value if isinstance(value, str) else json.dumps(value)
    resp = client.put(f"/api/settings/{home_layout.LAYOUT_KEY}", json={"value": value})
    assert resp.status_code == 200


def stored_row(conn):
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (home_layout.LAYOUT_KEY,)
    ).fetchone()
    return row[0] if row else None


def item(app_id, w=1, h=1):
    """An item of a layout from before tabs: an app and its size."""
    return {"app_id": app_id, "size": {"w": w, "h": h}}


def inst(instance_id, app_id, w=1, h=1, config=None):
    """An instance, as a tab holds it."""
    return {
        "instance_id": instance_id,
        "app_id": app_id,
        "size": {"w": w, "h": h},
        "config": {} if config is None else config,
    }


def tab(tab_id, *items, name=None, subtitle=""):
    return {
        "id": tab_id,
        "name": name or tab_id.title(),
        "mode": "flow",
        "subtitle": subtitle,
        "items": list(items),
    }


def tabs(*all_tabs, default=None):
    return {"tabs": list(all_tabs), "default_tab": default or all_tabs[0]["id"]}


def placed(items):
    """Instances as what Home draws of them: each app and its size."""
    return [item(i["app_id"], **i["size"]) for i in items]


def minted(read):
    """Every instance id in a layout, checked a token and used once."""
    ids = [i["instance_id"] for t in read["tabs"] for i in t["items"]]
    assert all(home_layout.INSTANCE_ID.match(i) for i in ids), ids
    assert len(set(ids)) == len(ids), ids
    return ids


def as_before(read):
    """A layout read as one tab, as the layout from before tabs it shows -
    {mode, subtitle, items: [{app_id, size}]} - once it's checked to be
    just that: the one tab "home", the default, every instance an id of
    its own and config {}."""
    assert read["default_tab"] == "home"
    [only] = read["tabs"]
    assert (only["id"], only["name"], only["mode"]) == ("home", "Home", "flow")
    minted(read)
    assert all(i["config"] == {} for i in only["items"])
    return {"mode": only["mode"], "subtitle": only["subtitle"], "items": placed(only["items"])}


# ---------- reading, with nothing saved ----------


def test_with_no_apps_and_nothing_saved_home_is_one_empty_tab(client, apps_dir):
    assert layout(client) == {
        "tabs": [{"id": "home", "name": "Home", "mode": "flow", "subtitle": "", "items": []}],
        "default_tab": "home",
    }


def test_with_nothing_saved_every_app_has_an_instance_at_its_default_in_folder_order(client, apps_dir):
    install(client, "zeta", "[{w: 2, h: 1}, {w: 1, h: 1}]")
    install(client, "alpha", "[{w: 3, h: 2}]")
    install(client, "mid")

    assert as_before(layout(client)) == {
        "mode": "flow",
        "subtitle": "",
        "items": [item("alpha", 3, 2), item("mid"), item("zeta", 2, 1)],
    }


def test_the_real_apps_are_placed_at_their_one_size(client, apps_dir):
    for folder in ("steps", "steps-metres"):
        files = {p.name: p.read_text(encoding="utf-8") for p in (REAL_APPS / folder).glob("app.*")}
        assert client.post("/api/apps", json={"files": files}).status_code == 201

    assert as_before(layout(client))["items"] == [item("steps"), item("steps-metres")]


def test_an_unsaved_instance_has_an_id_only_for_that_read(client, conn, apps_dir):
    # A read never writes, so the ids it mints aren't kept: the next read
    # mints its own. A save keeps whichever it's sent.
    install(client, "a")
    first, second = layout(client), layout(client)

    assert as_before(first) == as_before(second)
    assert minted(first) != minted(second)
    assert stored_row(conn) is None


# ---------- the upgrade: a layout from before tabs ----------


def test_a_layout_from_before_tabs_reads_as_one_home_tab_showing_the_same(client, conn, apps_dir):
    install(client, "a", "[{w: 1, h: 1}, {w: 2, h: 2}]")
    install(client, "b", "[{w: 3, h: 1}, {w: 1, h: 1}]")
    install(client, "c", "[{w: 2, h: 1}, {w: 1, h: 1}]")
    install(client, "d")
    install(client, "e", "[{w: 1, h: 2}]")
    before = {
        "mode": "flow",
        "subtitle": "My week",
        "items": [
            item("c", 1, 1),       # its second size: kept
            item("gone", 2, 2),    # not installed: dropped
            item("a", 2, 2),
            item("b", 6, 6),       # not one of b's sizes: its default
            item("c", 2, 1),       # c again: the first one stands
            item("d"),
        ],
    }
    store(client, before)
    saved = stored_row(conn)

    read = layout(client)
    # The one home tab, holding just what that layout showed: its apps in
    # its order at its sizes, then the app it didn't place (e) - each an
    # instance with an id of its own and config {}.
    assert as_before(read) == {
        "mode": "flow",
        "subtitle": "My week",
        "items": [item("c"), item("a", 2, 2), item("b", 3, 1), item("d"), item("e", 1, 2)],
    }
    # A read never rewrites what's saved: the next save stores the new shape.
    assert stored_row(conn) == saved


def test_saving_an_upgraded_layout_stores_the_new_shape_and_keeps_its_ids(client, conn, apps_dir):
    install(client, "a", "[{w: 1, h: 1}, {w: 2, h: 2}]")
    install(client, "b")
    store(client, {"mode": "flow", "subtitle": "Mine", "items": [item("b"), item("a", 2, 2)]})
    read = layout(client)

    # What Configure sends back, unchanged.
    assert save(client, read).status_code == 200
    assert json.loads(stored_row(conn)) == read
    # Saved, the ids stay put from read to read.
    assert layout(client) == read
    assert layout(client) == read


@pytest.mark.parametrize(
    "value",
    [
        "not json",
        "",
        "null",
        "[1, 2]",
        '"flow"',
        '{"mode": "grid"}',
        '{"items": "a"}',
        '{"items": {"a": 1}}',
        '{"subtitle": 5, "items": [5, "a", null, {"app_id": ["a"]}, {"app_id": "nope"}]}',
        '{"tabs": "home"}',
        '{"tabs": {"home": {}}}',
        '{"tabs": []}',
        '{"tabs": [5, null, "home", {"id": "Home"}, {"id": ""}, {"name": "Home"}]}',
    ],
)
def test_a_stored_row_that_isnt_a_layout_reads_as_none_saved(client, apps_dir, value):
    # The row can be written through /api/settings too: whatever's there,
    # the read never fails - one home tab, every app at its default, in
    # folder order.
    install(client, "a", "[{w: 2, h: 2}]")
    install(client, "b")
    store(client, value)

    assert as_before(layout(client)) == {"mode": "flow", "subtitle": "", "items": [item("a", 2, 2), item("b")]}


def test_a_stored_row_with_some_good_items_keeps_them(client, apps_dir):
    install(client, "a", "[{w: 1, h: 1}, {w: 2, h: 2}]")
    install(client, "b")
    install(client, "c")
    store(client, {
        "mode": "flow",
        "subtitle": "Kept",
        "items": [
            "junk",
            {"app_id": "c", "size": {"w": 9, "h": 1}},     # not a size: the default
            {"app_id": "a", "size": {"w": 2, "h": 2}},
            {"app_id": "c", "size": {"w": 1, "h": 1}},     # c again: the first one stands
            {"app_id": "a"},
        ],
    })

    assert as_before(layout(client)) == {
        "mode": "flow",
        "subtitle": "Kept",
        "items": [item("c"), item("a", 2, 2), item("b")],
    }


# ---------- saving: a round trip ----------


def test_a_saved_layout_reads_back_as_saved(client, conn, apps_dir):
    install(client, "steps", "[{w: 1, h: 1}, {w: 2, h: 2}]")
    install(client, "week", "[{w: 2, h: 1}, {w: 3, h: 2}]")
    install(client, "notes")
    body = tabs(
        tab("home", inst("w1", "week", 3, 2), inst("n1", "notes", config={"note": "<b>hi</b>"}),
            name="Home", subtitle="My week <b>&</b> ‘more’"),
        tab("run-club", inst("s1", "steps", 2, 2, config={"goal": 10000, "units": ["km"]}),
            name="Run club ☕", subtitle=""),
        default="run-club",
    )

    resp = save(client, body)
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    # Tabs, order, sizes, ids, configs and subtitles as saved - the text
    # exactly as given.
    assert layout(client) == body
    assert json.loads(stored_row(conn)) == body


def test_saving_again_replaces_the_layout(client, apps_dir):
    install(client, "a")
    install(client, "b")
    save(client, tabs(tab("home", inst("1", "b"), inst("2", "a"), subtitle="one")))
    second = tabs(tab("home", inst("3", "a"), inst("4", "b"), subtitle="two"))
    save(client, second)

    assert layout(client) == second


def test_the_save_keeps_only_the_layout(client, conn, apps_dir):
    # Anything else in the body, a tab or an item is dropped: the row
    # holds the shape the reads expect, and nothing else.
    install(client, "a")
    resp = save(client, {
        "default_tab": "home", "mode": "flow", "extra": 1,
        "tabs": [{
            "id": "home", "name": "Home", "mode": "flow", "subtitle": "", "icon": "star",
            "items": [{"instance_id": "i1", "app_id": "a", "size": {"w": 1, "h": 1, "d": 3},
                       "config": {}, "x": 0, "y": 0, "name": "A"}],
        }],
    })

    assert resp.status_code == 200
    assert json.loads(stored_row(conn)) == tabs(tab("home", inst("i1", "a")))


def test_the_save_doesnt_check_the_apps(client, conn, apps_dir):
    # The server stores the layout; it doesn't need to understand it. An
    # instance of an app that isn't installed, or at a size it doesn't
    # offer, is stored as given - and squared on the way back out.
    install(client, "a")
    body = tabs(tab("home", inst("g", "gone", 2, 2), inst("i", "a", 6, 6)))

    assert save(client, body).status_code == 200
    assert json.loads(stored_row(conn)) == body
    assert layout(client)["tabs"][0]["items"] == [inst("i", "a")]


def test_an_instance_sent_without_an_id_is_given_one(client, conn, apps_dir):
    install(client, "a")
    install(client, "b", "[{w: 2, h: 1}]")
    body = tabs(tab("home", item("b", 2, 1), inst("kept", "a"), {**item("a"), "config": {"k": 1}}))

    assert save(client, body).status_code == 200
    stored = json.loads(stored_row(conn))
    first, kept, second = stored["tabs"][0]["items"]
    # The ones sent without an id have a fresh one each; the one sent with
    # one keeps it. A missing config is {}.
    assert kept == inst("kept", "a")
    assert first == inst(first["instance_id"], "b", 2, 1)
    assert second == inst(second["instance_id"], "a", config={"k": 1})
    assert minted(stored) == [first["instance_id"], "kept", second["instance_id"]]
    # The ids minted are stored: every read hands out the same ones.
    assert layout(client) == stored


def test_a_minted_id_never_takes_one_given_further_on(monkeypatch):
    # The first token minted is the id a later instance is given: it's
    # passed over for the next.
    tokens = iter(["clash", "fresh"])
    monkeypatch.setattr(home_layout.secrets, "token_hex", lambda n: next(tokens))
    body = tabs(tab("home", item("a")), tab("two", inst("clash", "b")))

    parsed = home_layout.parse_layout(body)
    assert parsed["tabs"][0]["items"] == [inst("fresh", "a")]
    assert parsed["tabs"][1]["items"] == [inst("clash", "b")]


def test_the_same_app_can_be_placed_more_than_once(client, conn, apps_dir):
    # An instance is a placement, not an app: one app on two tabs, and
    # twice on one, each with its own size and config - all kept.
    install(client, "steps", "[{w: 1, h: 1}, {w: 2, h: 2}]")
    install(client, "notes")
    body = tabs(
        tab("home", inst("s-small", "steps"), inst("n", "notes"), inst("s-big", "steps", 2, 2)),
        tab("week", inst("s-week", "steps", 2, 2, config={"days": 7})),
    )

    assert save(client, body).status_code == 200
    assert layout(client) == body


@pytest.mark.parametrize("default", [None, "nope", 5, ["two"], {"id": "two"}, "Two"])
def test_a_default_tab_that_isnt_a_tab_is_the_first(client, conn, apps_dir, default):
    body = {"tabs": [tab("one"), tab("two")]}
    if default is not None:
        body["default_tab"] = default

    assert save(client, body).status_code == 200
    assert json.loads(stored_row(conn))["default_tab"] == "one"
    assert layout(client)["default_tab"] == "one"


@pytest.mark.parametrize("subtitle", ["", "x" * home_layout.MAX_SUBTITLE, "  spaced  ", "ünï ☕"])
def test_a_subtitle_up_to_the_limit_is_saved_as_given(client, apps_dir, subtitle):
    assert save(client, tabs(tab("home", subtitle=subtitle))).status_code == 200
    assert layout(client)["tabs"][0]["subtitle"] == subtitle


@pytest.mark.parametrize("name", ["H", "x" * home_layout.MAX_TAB_NAME, " Runs ", "ünï ☕", "<b>"])
def test_a_tab_name_up_to_the_limit_is_saved_as_given(client, apps_dir, name):
    assert save(client, tabs(tab("home", name=name))).status_code == 200
    assert layout(client)["tabs"][0]["name"] == name


@pytest.mark.parametrize("tab_id", ["home", "a", "0", "run-club", "a-", "x" * 32])
def test_a_tab_id_is_a_short_slug(client, apps_dir, tab_id):
    assert save(client, tabs(tab(tab_id, name="T"))).status_code == 200
    assert layout(client)["tabs"][0]["id"] == tab_id


# ---------- saving: what's refused, storing nothing ----------


GOOD_TAB = tab("home", inst("i1", "a"), name="Home", subtitle="Mine")
GOOD = tabs(GOOD_TAB)


def with_tab(**changes):
    """GOOD with its tab changed - a value of None removes that key."""
    changed = {k: v for k, v in {**GOOD_TAB, **changes}.items() if v is not None}
    return {"tabs": [changed], "default_tab": "home"}


def with_items(*items):
    return with_tab(items=list(items))


@pytest.mark.parametrize(
    "body, says",
    [
        (None, "expected {"),
        ([], "expected {"),
        ("flow", "expected {"),
        # The shape from before tabs is a read's to upgrade, not a save's.
        ({"mode": "flow", "subtitle": "", "items": [item("a")]}, "tabs must be a non-empty list"),
        ({"tabs": []}, "tabs must be a non-empty list"),
        ({"tabs": None}, "tabs must be a non-empty list"),
        ({"tabs": {"home": GOOD_TAB}}, "tabs must be a non-empty list"),
        ({"tabs": ["home"]}, "each tab must be {id, name, mode, subtitle, items}"),
        ({"tabs": [None]}, "each tab must be {id, name, mode, subtitle, items}"),
        (with_tab(id=None), "each tab needs an id"),
        (with_tab(id=""), "each tab needs an id"),
        (with_tab(id="Home"), "each tab needs an id"),
        (with_tab(id="-home"), "each tab needs an id"),
        (with_tab(id="my home"), "each tab needs an id"),
        (with_tab(id="home\n"), "each tab needs an id"),
        (with_tab(id="../x"), "each tab needs an id"),
        (with_tab(id="x" * 33), "each tab needs an id of 1 to 32 lowercase letters"),
        (with_tab(id=5), "each tab needs an id"),
        ({"tabs": [GOOD_TAB, {**GOOD_TAB, "items": []}]}, "tab 'home' is listed twice"),
        (with_tab(name=None), "tab 'home': name must be text, not blank"),
        (with_tab(name=""), "tab 'home': name must be text, not blank"),
        (with_tab(name="   "), "tab 'home': name must be text, not blank"),
        (with_tab(name=5), "tab 'home': name must be text, not blank"),
        (with_tab(name="x" * (home_layout.MAX_TAB_NAME + 1)), "tab 'home': name must be at most 40 characters"),
        (with_tab(mode=None), "tab 'home': mode must be 'flow'"),
        (with_tab(mode="grid"), "tab 'home': mode must be 'flow'"),
        (with_tab(mode="FLOW"), "tab 'home': mode must be 'flow'"),
        (with_tab(subtitle=None), "tab 'home': subtitle must be text"),
        (with_tab(subtitle=5), "tab 'home': subtitle must be text"),
        (with_tab(subtitle="x" * (home_layout.MAX_SUBTITLE + 1)), "tab 'home': subtitle must be at most 120 characters"),
        (with_tab(items=None), "tab 'home': items must be a list"),
        (with_tab(items={"a": {"w": 1, "h": 1}}), "tab 'home': items must be a list"),
        (with_items("a"), "tab 'home': each item needs the app_id of an app"),
        (with_items({"size": {"w": 1, "h": 1}}), "tab 'home': each item needs the app_id of an app"),
        (with_items(inst("i", "")), "tab 'home': each item needs the app_id of an app"),
        (with_items(inst("i", "../x")), "tab 'home': each item needs the app_id of an app"),
        (with_items(inst("i", ".hidden")), "tab 'home': each item needs the app_id of an app"),
        (with_items({**inst("i", "a"), "app_id": 5}), "tab 'home': each item needs the app_id of an app"),
        (with_items({"instance_id": "i", "app_id": "a"}), "tab 'home': 'a''s size must be {w, h}"),
        (with_items({**inst("i", "a"), "size": [1, 1]}), "tab 'home': 'a''s size must be {w, h}"),
        (with_items(inst("i", "a", 0, 1)), "tab 'home': 'a''s size must be {w, h}"),
        (with_items(inst("i", "a", 7, 1)), "tab 'home': 'a''s size must be {w, h}, each a whole number of cells from 1 to 6"),
        (with_items(inst("i", "a", 1, 7)), "tab 'home': 'a''s size must be {w, h}"),
        (with_items(inst("i", "a", 1.5, 1)), "tab 'home': 'a''s size must be {w, h}"),
        (with_items(inst("i", "a", "1", 1)), "tab 'home': 'a''s size must be {w, h}"),
        (with_items(inst("i", "a", True, 1)), "tab 'home': 'a''s size must be {w, h}"),
        (with_items(inst(None, "a")), "tab 'home': 'a''s instance_id must be 1 to 64 letters"),
        (with_items(inst("", "a")), "tab 'home': 'a''s instance_id must be"),
        (with_items(inst("a b", "a")), "tab 'home': 'a''s instance_id must be"),
        (with_items(inst("a/b", "a")), "tab 'home': 'a''s instance_id must be"),
        (with_items(inst("é", "a")), "tab 'home': 'a''s instance_id must be"),
        (with_items(inst("x\n", "a")), "tab 'home': 'a''s instance_id must be"),
        (with_items(inst("x" * 65, "a")), "tab 'home': 'a''s instance_id must be"),
        (with_items(inst(5, "a")), "tab 'home': 'a''s instance_id must be"),
        (with_items(inst("i", "a"), inst("i", "b")), "tab 'home': instance_id 'i' is used twice"),
        ({"tabs": [GOOD_TAB, tab("two", inst("i1", "b"))]}, "tab 'two': instance_id 'i1' is used twice"),
        (with_items({**inst("i", "a"), "config": None}), "tab 'home': 'a''s config must be an object"),
        (with_items({**inst("i", "a"), "config": []}), "tab 'home': 'a''s config must be an object"),
        (with_items({**inst("i", "a"), "config": "x"}), "tab 'home': 'a''s config must be an object"),
        (with_items({**inst("i", "a"), "config": 5}), "tab 'home': 'a''s config must be an object"),
    ],
)
def test_a_body_that_isnt_a_layout_is_refused_and_nothing_stored(client, conn, apps_dir, body, says):
    install(client, "a")
    assert save(client, GOOD).status_code == 200
    before = stored_row(conn)

    resp = save(client, body)
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False
    assert resp.get_json()["error"].startswith(says), resp.get_json()["error"]
    assert stored_row(conn) == before


def test_a_body_that_isnt_json_is_refused(client, conn, apps_dir):
    resp = client.put("/api/home-layout", data="tabs=home", content_type="text/plain")

    assert resp.status_code == 400
    assert stored_row(conn) is None


# ---------- reconciling: apps come and go out of band ----------


def test_an_app_installed_after_the_save_gets_an_instance_on_the_default_tab(client, conn, apps_dir):
    install(client, "b")
    install(client, "d")
    save(client, tabs(tab("first", inst("d1", "d")), tab("mine", inst("b1", "b"), subtitle="Mine"), default="mine"))
    saved = stored_row(conn)

    # Two new apps: each placed nowhere, so each gets an instance at the
    # end of the default tab - not the first - at its default size, in
    # folder order whatever their names. The saved ones hold.
    install(client, "c", "[{w: 2, h: 2}, {w: 1, h: 1}]")
    install(client, "a", "[{w: 3, h: 1}]")

    read = layout(client)
    first, mine = read["tabs"]
    assert first == tab("first", inst("d1", "d"))
    assert mine["items"][0] == inst("b1", "b")
    assert placed(mine["items"]) == [item("b"), item("a", 3, 1), item("c", 2, 2)]
    assert [i["config"] for i in mine["items"]] == [{}, {}, {}]
    minted(read)
    assert read["default_tab"] == "mine"
    # A read never rewrites what's saved.
    assert stored_row(conn) == saved


def test_an_app_placed_on_any_tab_gets_no_more_instances(client, apps_dir):
    # At least one instance an app, across every tab: one placed only on
    # a tab that isn't the default isn't added to the default as well.
    install(client, "a")
    install(client, "b")
    body = tabs(tab("home", inst("a1", "a")), tab("two", inst("b1", "b")))
    save(client, body)

    assert layout(client) == body


def test_an_uninstalled_app_is_dropped_and_the_rest_keep_their_places(client, conn, apps_dir):
    for app_id in ("a", "b", "c"):
        install(client, app_id, "[{w: 1, h: 1}, {w: 2, h: 1}]")
    save(client, tabs(
        tab("home", inst("c1", "c", 2, 1), inst("b1", "b"), inst("a1", "a", 2, 1)),
        tab("two", inst("b2", "b", 2, 1), inst("a2", "a")),
    ))
    saved = stored_row(conn)

    uninstall(client, "b")

    home, two = layout(client)["tabs"]
    assert home["items"] == [inst("c1", "c", 2, 1), inst("a1", "a", 2, 1)]
    assert two["items"] == [inst("a2", "a")]
    # The saved layout is untouched, and still reads as a layout.
    assert stored_row(conn) == saved
    assert json.loads(saved)["tabs"][0]["items"][1] == inst("b1", "b")


def test_an_app_removed_and_added_again_before_a_save_comes_back_where_it_was(client, apps_dir):
    # Uninstall then add is the update path: each instance keeps its
    # place, its id and its config.
    for app_id in ("a", "b", "c"):
        install(client, app_id, "[{w: 1, h: 1}, {w: 2, h: 2}]")
    body = tabs(
        tab("home", inst("b1", "b", 2, 2, config={"x": 1}), inst("c1", "c"), inst("a1", "a")),
        tab("two", inst("b2", "b", config={"y": 2})),
    )
    save(client, body)

    uninstall(client, "b")
    home, two = layout(client)["tabs"]
    assert home["items"] == [inst("c1", "c"), inst("a1", "a")]
    assert two["items"] == []
    install(client, "b", "[{w: 1, h: 1}, {w: 2, h: 2}]")

    assert layout(client) == body


def test_a_size_the_app_no_longer_offers_falls_back_to_its_default(client, apps_dir):
    install(client, "a", "[{w: 1, h: 1}, {w: 2, h: 2}]")
    install(client, "b", "[{w: 1, h: 1}, {w: 3, h: 1}]")
    save(client, tabs(tab("home", inst("b1", "b", 3, 1), inst("a1", "a", 2, 2)), tab("two", inst("a2", "a", 1, 1))))

    # a's new version drops 2x2 and leads with 2x1: its 2x2 instance falls
    # back to that default, its 1x1 one keeps its size. b still offers
    # 3x1, so it keeps it.
    uninstall(client, "a")
    install(client, "a", "[{w: 2, h: 1}, {w: 1, h: 1}]")

    home, two = layout(client)["tabs"]
    assert home["items"] == [inst("b1", "b", 3, 1), inst("a1", "a", 2, 1)]
    assert two["items"] == [inst("a2", "a")]


def test_an_app_with_one_size_is_always_at_it(client, apps_dir):
    install(client, "fixed", "[{w: 2, h: 1}]")
    save(client, tabs(tab("home", inst("f", "fixed", 1, 1))))

    assert layout(client)["tabs"][0]["items"] == [inst("f", "fixed", 2, 1)]


def test_an_app_that_stops_loading_leaves_the_layout(client, apps_dir):
    install(client, "a")
    install(client, "b")
    save(client, tabs(tab("home", inst("b1", "b"), inst("a1", "a"))))

    # Edited by hand to drop its size: the scanner skips it, so Home does.
    (apps_dir / "b" / "app.yaml").write_text("id: b\n")

    assert layout(client)["tabs"][0]["items"] == [inst("a1", "a")]


# ---------- reconciling: a stored row with bad parts ----------


def test_an_instance_id_used_twice_keeps_the_first(client, apps_dir):
    # Only a row written through /api/settings can hold one. The later
    # instance is dropped - and b, with no instance left, gets a fresh one
    # on the default tab.
    install(client, "a")
    install(client, "b")
    store(client, tabs(tab("home", inst("x", "a")), tab("two", inst("x", "b"), inst("y", "a"))))

    read = layout(client)
    home, two = read["tabs"]
    assert home["items"][0] == inst("x", "a")
    assert placed(home["items"]) == [item("a"), item("b")]
    assert two["items"] == [inst("y", "a")]
    minted(read)


def test_a_default_tab_that_isnt_one_reads_as_the_first(client, apps_dir):
    install(client, "a")
    install(client, "b")
    store(client, tabs(tab("one", inst("b1", "b")), tab("two"), default="gone"))

    read = layout(client)
    assert read["default_tab"] == "one"
    # ... which is where an app placed nowhere goes.
    assert placed(read["tabs"][0]["items"]) == [item("b"), item("a")]
    assert read["tabs"][1]["items"] == []


def test_a_stored_tabs_bad_parts_are_read_round(client, apps_dir):
    install(client, "a", "[{w: 1, h: 1}, {w: 2, h: 2}]")
    install(client, "b")
    install(client, "c")
    store(client, {
        "tabs": [
            "junk",
            {"id": "Bad", "name": "Bad", "items": [inst("lost", "c")]},   # not a tab id: the tab's dropped
            {
                "id": "home", "name": "   ", "mode": "grid", "subtitle": 5, "extra": 1,
                "items": [
                    None,
                    inst("a1", "a", 2, 2, config=["not", "an", "object"]),
                    inst("bad id", "b"),                                    # not an id: dropped
                    {"app_id": "b", "size": {"w": 1, "h": 1}},              # no id: dropped
                    inst("a2", "a", 9, 9),
                ],
            },
            {"id": "home", "name": "Again", "items": [inst("c1", "c")]},  # a repeat: dropped
            {"id": "two", "name": "x" * (home_layout.MAX_TAB_NAME + 1), "subtitle": "Kept", "items": "junk"},
        ],
        "default_tab": "two",
    })

    read = layout(client)
    home, two = read["tabs"]
    # A name a save wouldn't take reads as the id; any mode as flow; a
    # subtitle that isn't text as none; a config that isn't an object as {}.
    assert home == tab("home", inst("a1", "a", 2, 2), inst("a2", "a"), name="home")
    # b and c have no instance left: each gets one, on the default tab.
    assert {k: v for k, v in two.items() if k != "items"} == {
        "id": "two", "name": "two", "mode": "flow", "subtitle": "Kept",
    }
    assert placed(two["items"]) == [item("b"), item("c")]
    minted(read)
    assert read["default_tab"] == "two"


def test_a_read_layout_always_saves(client, apps_dir):
    # Whatever's stored, what a read hands out is a layout a save takes
    # back - Configure sends it as it is.
    install(client, "a")
    store(client, {"tabs": [{"id": "x" * 32, "name": 5, "items": [inst("i", "a", config=[])]}]})
    read = layout(client)

    assert save(client, read).status_code == 200
    assert layout(client) == read


# ---------- reconcile() on its own ----------


def app(app_id, *sizes):
    sizes = [{"w": w, "h": h} for w, h in sizes]
    return apps.InstalledApp(
        id=app_id, name=app_id, sizes=sizes, default_size=sizes[0],
        reads=[], config=[], path="/nowhere", render=None,
    )


def test_reconcile_gives_every_installed_app_at_least_one_instance():
    installed = [app("a", (1, 1)), app("b", (2, 1), (1, 1)), app("c", (3, 3)), app("d", (1, 1))]
    stored = tabs(
        tab("one", inst("p", "b", 1, 1), inst("q", "x"), inst("r", "b", 2, 1)),
        tab("two", inst("s", "a")),
        default="two",
    )

    result = home_layout.reconcile(stored, installed)
    one, two = result["tabs"]
    assert one["items"] == [inst("p", "b"), inst("r", "b", 2, 1)]
    assert two["items"][0] == inst("s", "a")
    assert placed(two["items"][1:]) == [item("c", 3, 3), item("d")]
    assert result["default_tab"] == "two"
    minted(result)


def test_reconcile_never_hands_out_a_stored_size_object():
    # Each size in the result is its own dict: changing one changes nothing else.
    installed = [app("a", (1, 1), (2, 2))]
    stored = tabs(tab("home", inst("i", "a", 2, 2)))

    result = home_layout.reconcile(stored, installed)
    result["tabs"][0]["items"][0]["size"]["w"] = 6
    assert stored["tabs"][0]["items"][0]["size"] == {"w": 2, "h": 2}
    assert installed[0].sizes == [{"w": 1, "h": 1}, {"w": 2, "h": 2}]
