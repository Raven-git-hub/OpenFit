"""
The Home layout: GET/PUT /api/home-layout and the home_layout settings row.

Flow mode, the one mode so far: {"mode": "flow", "subtitle", "items"},
items an ordered list of {app_id, size: {w, h}} - the order is the
layout, and each size one the app declares. The save stores what it's
given once its shape is checked, and nothing more; every read squares
the stored items with the apps installed now: an uninstalled app's item
dropped, a size its app no longer offers back to the default, and any
app the layout doesn't place appended at its default size.

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


def item(app_id, w=1, h=1):
    return {"app_id": app_id, "size": {"w": w, "h": h}}


def stored_row(conn):
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (home_layout.LAYOUT_KEY,)
    ).fetchone()
    return row[0] if row else None


# ---------- reading ----------


def test_with_no_apps_and_nothing_saved_the_layout_is_empty(client, apps_dir):
    assert layout(client) == {"mode": "flow", "subtitle": "", "items": []}


def test_with_nothing_saved_every_app_is_placed_at_its_default_in_folder_order(client, apps_dir):
    install(client, "zeta", "[{w: 2, h: 1}, {w: 1, h: 1}]")
    install(client, "alpha", "[{w: 3, h: 2}]")
    install(client, "mid")

    assert layout(client) == {
        "mode": "flow",
        "subtitle": "",
        "items": [item("alpha", 3, 2), item("mid"), item("zeta", 2, 1)],
    }


def test_the_real_apps_are_placed_at_their_one_size(client, apps_dir):
    for folder in ("steps", "steps-metres"):
        files = {p.name: p.read_text(encoding="utf-8") for p in (REAL_APPS / folder).glob("app.*")}
        assert client.post("/api/apps", json={"files": files}).status_code == 201

    assert layout(client)["items"] == [item("steps"), item("steps-metres")]


# ---------- saving: a round trip ----------


def test_a_saved_layout_reads_back_as_saved(client, conn, apps_dir):
    install(client, "steps", "[{w: 1, h: 1}, {w: 2, h: 2}]")
    install(client, "week", "[{w: 2, h: 1}, {w: 3, h: 2}]")
    install(client, "notes")
    body = {
        "mode": "flow",
        "subtitle": "My week <b>&</b> ‘more’",
        "items": [item("week", 3, 2), item("notes"), item("steps", 2, 2)],
    }

    resp = save(client, body)
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    # Order, sizes and subtitle as saved - the subtitle exactly as given.
    assert layout(client) == body
    assert json.loads(stored_row(conn)) == body


def test_saving_again_replaces_the_layout(client, apps_dir):
    install(client, "a")
    install(client, "b")
    save(client, {"mode": "flow", "subtitle": "one", "items": [item("b"), item("a")]})
    save(client, {"mode": "flow", "subtitle": "two", "items": [item("a"), item("b")]})

    assert layout(client) == {"mode": "flow", "subtitle": "two", "items": [item("a"), item("b")]}


def test_the_save_keeps_only_the_layout(client, conn, apps_dir):
    # Anything else in the body or an item is dropped: the row holds the
    # shape the reads expect, and nothing else.
    install(client, "a")
    resp = save(client, {
        "mode": "flow", "subtitle": "", "extra": 1,
        "items": [{"app_id": "a", "size": {"w": 1, "h": 1, "d": 3}, "x": 0, "y": 0, "name": "A"}],
    })

    assert resp.status_code == 200
    assert json.loads(stored_row(conn)) == {"mode": "flow", "subtitle": "", "items": [item("a")]}


def test_the_save_doesnt_check_the_apps(client, conn, apps_dir):
    # The server stores the layout; it doesn't need to understand it. An
    # item for an app that isn't installed, or at a size it doesn't offer,
    # is stored as given - and squared on the way back out.
    install(client, "a")
    body = {"mode": "flow", "subtitle": "", "items": [item("gone", 2, 2), item("a", 6, 6)]}

    assert save(client, body).status_code == 200
    assert json.loads(stored_row(conn)) == body
    assert layout(client)["items"] == [item("a")]


@pytest.mark.parametrize("subtitle", ["", "x" * home_layout.MAX_SUBTITLE, "  spaced  ", "ünï ☕"])
def test_a_subtitle_up_to_the_limit_is_saved_as_given(client, apps_dir, subtitle):
    assert save(client, {"mode": "flow", "subtitle": subtitle, "items": []}).status_code == 200
    assert layout(client)["subtitle"] == subtitle


# ---------- saving: what's refused, storing nothing ----------


GOOD = {"mode": "flow", "subtitle": "Mine", "items": [item("a")]}


@pytest.mark.parametrize(
    "body, says",
    [
        (None, "expected {"),
        ([], "expected {"),
        ("flow", "expected {"),
        ({**GOOD, "mode": None}, "mode must be 'flow'"),
        ({"subtitle": "", "items": []}, "mode must be 'flow'"),
        ({**GOOD, "mode": "grid"}, "mode must be 'flow'"),
        ({**GOOD, "mode": "FLOW"}, "mode must be 'flow'"),
        ({"mode": "flow", "items": []}, "subtitle must be text"),
        ({**GOOD, "subtitle": None}, "subtitle must be text"),
        ({**GOOD, "subtitle": 5}, "subtitle must be text"),
        ({**GOOD, "subtitle": "x" * (home_layout.MAX_SUBTITLE + 1)}, "subtitle must be at most 120 characters"),
        ({"mode": "flow", "subtitle": ""}, "items must be a list"),
        ({**GOOD, "items": None}, "items must be a list"),
        ({**GOOD, "items": {"a": {"w": 1, "h": 1}}}, "items must be a list"),
        ({**GOOD, "items": ["a"]}, "each item needs the app_id of an app"),
        ({**GOOD, "items": [{"size": {"w": 1, "h": 1}}]}, "each item needs the app_id of an app"),
        ({**GOOD, "items": [item("")]}, "each item needs the app_id of an app"),
        ({**GOOD, "items": [item("../x")]}, "each item needs the app_id of an app"),
        ({**GOOD, "items": [item(".hidden")]}, "each item needs the app_id of an app"),
        ({**GOOD, "items": [{"app_id": 5, "size": {"w": 1, "h": 1}}]}, "each item needs the app_id of an app"),
        ({**GOOD, "items": [item("a"), item("b"), item("a", 2, 2)]}, "'a' is listed twice"),
        ({**GOOD, "items": [{"app_id": "a"}]}, "'a''s size must be {w, h}"),
        ({**GOOD, "items": [{"app_id": "a", "size": [1, 1]}]}, "'a''s size must be {w, h}"),
        ({**GOOD, "items": [item("a", 0, 1)]}, "'a''s size must be {w, h}"),
        ({**GOOD, "items": [item("a", 7, 1)]}, "'a''s size must be {w, h}, each a whole number of cells from 1 to 6"),
        ({**GOOD, "items": [item("a", 1, 7)]}, "'a''s size must be {w, h}"),
        ({**GOOD, "items": [item("a", 1.5, 1)]}, "'a''s size must be {w, h}"),
        ({**GOOD, "items": [item("a", "1", 1)]}, "'a''s size must be {w, h}"),
        ({**GOOD, "items": [item("a", True, 1)]}, "'a''s size must be {w, h}"),
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
    resp = client.put("/api/home-layout", data="mode=flow", content_type="text/plain")

    assert resp.status_code == 400
    assert stored_row(conn) is None


# ---------- reconciling: apps come and go out of band ----------


def test_an_app_installed_after_the_save_is_added_at_the_end_at_its_default(client, conn, apps_dir):
    install(client, "b")
    install(client, "d")
    save(client, {"mode": "flow", "subtitle": "Mine", "items": [item("d"), item("b")]})
    saved = stored_row(conn)

    # Two new apps, in folder order after the saved ones; the saved order
    # holds, whatever the new apps' names.
    install(client, "c", "[{w: 2, h: 2}, {w: 1, h: 1}]")
    install(client, "a", "[{w: 3, h: 1}]")

    assert layout(client) == {
        "mode": "flow",
        "subtitle": "Mine",
        "items": [item("d"), item("b"), item("a", 3, 1), item("c", 2, 2)],
    }
    # A read never rewrites what's saved.
    assert stored_row(conn) == saved


def test_an_uninstalled_app_is_dropped_and_the_rest_keep_their_places(client, conn, apps_dir):
    for app_id in ("a", "b", "c"):
        install(client, app_id, "[{w: 1, h: 1}, {w: 2, h: 1}]")
    save(client, {"mode": "flow", "subtitle": "", "items": [item("c", 2, 1), item("b"), item("a", 2, 1)]})
    saved = stored_row(conn)

    uninstall(client, "b")

    assert layout(client)["items"] == [item("c", 2, 1), item("a", 2, 1)]
    # The saved layout is untouched, and still reads as a layout.
    assert stored_row(conn) == saved
    assert json.loads(saved)["items"][1] == item("b")


def test_an_app_removed_and_added_again_before_a_save_comes_back_where_it_was(client, apps_dir):
    # Uninstall then add is the update path: the tile keeps its place.
    for app_id in ("a", "b", "c"):
        install(client, app_id, "[{w: 1, h: 1}, {w: 2, h: 2}]")
    save(client, {"mode": "flow", "subtitle": "", "items": [item("b", 2, 2), item("c"), item("a")]})

    uninstall(client, "b")
    assert layout(client)["items"] == [item("c"), item("a")]
    install(client, "b", "[{w: 1, h: 1}, {w: 2, h: 2}]")

    assert layout(client)["items"] == [item("b", 2, 2), item("c"), item("a")]


def test_a_size_the_app_no_longer_offers_falls_back_to_its_default(client, apps_dir):
    install(client, "a", "[{w: 1, h: 1}, {w: 2, h: 2}]")
    install(client, "b", "[{w: 1, h: 1}, {w: 3, h: 1}]")
    save(client, {"mode": "flow", "subtitle": "", "items": [item("b", 3, 1), item("a", 2, 2)]})

    # a's new version drops 2x2 and leads with 2x1: its tile falls back to
    # that default. b still offers 3x1, so it keeps it.
    uninstall(client, "a")
    install(client, "a", "[{w: 2, h: 1}, {w: 1, h: 1}]")

    assert layout(client)["items"] == [item("b", 3, 1), item("a", 2, 1)]


def test_an_app_with_one_size_is_always_at_it(client, apps_dir):
    install(client, "fixed", "[{w: 2, h: 1}]")
    save(client, {"mode": "flow", "subtitle": "", "items": [item("fixed", 1, 1)]})

    assert layout(client)["items"] == [item("fixed", 2, 1)]


def test_an_app_that_stops_loading_leaves_the_layout(client, apps_dir):
    install(client, "a")
    install(client, "b")
    save(client, {"mode": "flow", "subtitle": "", "items": [item("b"), item("a")]})

    # Edited by hand to drop its size: the scanner skips it, so Home does.
    (apps_dir / "b" / "app.yaml").write_text("id: b\n")

    assert layout(client)["items"] == [item("a")]


# ---------- a stored row that isn't a layout ----------


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
    ],
)
def test_a_stored_row_that_isnt_a_layout_reads_as_none_saved(client, apps_dir, value):
    # The row can be written through /api/settings too: whatever's there,
    # the read never fails - every app at its default, in folder order.
    install(client, "a", "[{w: 2, h: 2}]")
    install(client, "b")
    assert client.put(f"/api/settings/{home_layout.LAYOUT_KEY}", json={"value": value}).status_code == 200

    assert layout(client) == {"mode": "flow", "subtitle": "", "items": [item("a", 2, 2), item("b")]}


def test_a_stored_row_with_some_good_items_keeps_them(client, apps_dir):
    install(client, "a", "[{w: 1, h: 1}, {w: 2, h: 2}]")
    install(client, "b")
    install(client, "c")
    stored = {
        "mode": "flow",
        "subtitle": "Kept",
        "items": [
            "junk",
            {"app_id": "c", "size": {"w": 9, "h": 1}},     # not a size: the default
            {"app_id": "a", "size": {"w": 2, "h": 2}},
            {"app_id": "c", "size": {"w": 1, "h": 1}},     # c again: the first one stands
            {"app_id": "a"},
        ],
    }
    client.put(f"/api/settings/{home_layout.LAYOUT_KEY}", json={"value": json.dumps(stored)})

    assert layout(client) == {
        "mode": "flow",
        "subtitle": "Kept",
        "items": [item("c"), item("a", 2, 2), item("b")],
    }


# ---------- reconcile() on its own ----------


def app(app_id, *sizes):
    sizes = [{"w": w, "h": h} for w, h in sizes]
    return apps.InstalledApp(
        id=app_id, name=app_id, sizes=sizes, default_size=sizes[0],
        reads=[], config=[], path="/nowhere", render=None,
    )


def test_reconcile_places_every_installed_app_exactly_once():
    installed = [app("a", (1, 1)), app("b", (2, 1), (1, 1)), app("c", (3, 3))]
    stored = {"items": [item("b", 1, 1), item("x"), item("b", 2, 1), item("a", 1, 1)]}

    result = home_layout.reconcile(stored, installed)
    assert result == {"mode": "flow", "subtitle": "", "items": [item("b"), item("a"), item("c", 3, 3)]}
    assert sorted(i["app_id"] for i in result["items"]) == ["a", "b", "c"]


def test_reconcile_never_hands_out_a_stored_size_object():
    # Each size in the result is its own dict: changing one changes nothing else.
    installed = [app("a", (1, 1), (2, 2))]
    stored = {"items": [item("a", 2, 2)]}

    result = home_layout.reconcile(stored, installed)
    result["items"][0]["size"]["w"] = 6
    assert stored["items"][0]["size"] == {"w": 2, "h": 2}
    assert installed[0].sizes == [{"w": 1, "h": 1}, {"w": 2, "h": 2}]
