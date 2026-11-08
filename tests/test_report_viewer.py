"""
The report viewer (app level 8): a rail's action makes each card a button
sending {card: id} to the app's handle(), and ctx.show_file() asks the page
to show one of the app's own report files in the viewer - a popup over the
tile with the file in a sealed frame, served by GET
/api/app/<id>/files/<name>/view with a CSP sandbox and nothing loaded from
anywhere.
"""

import pytest

import host
from test_app_files import HTML, SIZE, apps_dir, make_app, write_ctx  # noqa: F401 (apps_dir, a fixture)

VIEWER = """
def render(ctx, cfg):
    cards = [{"id": "2026-10-29T07:00:00Z", "start": "2026-10-29T07:00:00Z", "end": "2026-10-31T01:00:00Z",
              "out": "CX623", "in": "CX624", "port": "BLR"}]
    return ctx.rail(cards, "report", title="Report", action="report")

def handle(ctx, action, payload):
    if action == "report":
        name = "trip-" + payload["card"][:10] + ".html"
        ctx.save_file(name, "<!doctype html><title>Trip</title><p>" + payload["card"] + "</p>")
        ctx.show_file(name)
    elif action == "show_missing":
        ctx.show_file("not-saved.html")
    elif action == "show_then_fail":
        ctx.save_file("kept.html", "x")
        ctx.show_file("kept.html")
        raise ValueError("refused after the show")
    elif action == "save_only":
        ctx.save_file("plain.html", "x")
    elif action == "show_twice":
        ctx.save_file("a.html", "a")
        ctx.save_file("b.html", "b")
        ctx.show_file("a.html")
        ctx.show_file("b.html")
"""


@pytest.fixture
def viewer_app(apps_dir):
    return make_app(apps_dir, "viewer", VIEWER, extra="requires: 8\n")


def act(client, action, **payload):
    return client.post("/api/app/viewer/action", json={"action": action, "payload": payload})


def test_a_rail_carries_its_action_only_when_given():
    card = {"id": "a", "start": "2026-10-29T07:00:00Z", "end": None, "out": "CX623", "in": None, "port": "BLR"}
    assert "action" not in host.rail([card], "report")
    assert host.rail([card], "report", action="report")["action"] == "report"
    for bad in ("", 3, True):
        with pytest.raises(TypeError, match="a rail's action must be non-empty text"):
            host.rail([card], "report", action=bad)


def test_a_card_pressed_makes_its_report_and_the_reply_names_it_to_show(client, db_path, viewer_app):
    spec = client.post("/api/app/viewer/render", json={"cfg": {}}).get_json()
    assert (spec["type"], spec["action"]) == ("rail", "report")

    resp = act(client, "report", card="2026-10-29T07:00:00Z")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["show"] == "trip-2026-10-29.html" and body["spec"]["type"] == "rail"


def test_an_action_that_shows_nothing_has_no_show_and_the_last_show_wins(client, viewer_app):
    assert "show" not in act(client, "save_only").get_json()
    assert act(client, "show_twice").get_json()["show"] == "b.html"


def test_show_file_needs_a_file_that_is_there_and_a_failed_action_shows_nothing(client, viewer_app):
    resp = act(client, "show_missing")
    assert resp.status_code == 400
    assert resp.get_json() == {"ok": False, "error": "name: no report file 'not-saved.html' to show - save it first (ctx.save_file())"}
    resp = act(client, "show_then_fail")
    assert (resp.status_code, resp.get_json()) == (400, {"ok": False, "error": "refused after the show"})


def test_show_file_refuses_a_name_no_file_could_have(conn, apps_dir):
    ctx = write_ctx(conn)
    with pytest.raises(ValueError, match="name"):
        ctx.show_file("Report.HTML")
    assert ctx.shown is None


def test_the_view_route_shows_the_file_sealed(client, conn, apps_dir):
    write_ctx(conn).save_file("fatigue-2026-10.html", HTML)
    conn.commit()

    resp = client.get("/api/app/reports/files/fatigue-2026-10.html/view")

    assert resp.status_code == 200 and resp.data == HTML.encode("utf-8")
    assert resp.headers["Content-Type"] == "text/html; charset=utf-8"
    assert resp.headers["Content-Disposition"] == 'inline; filename="fatigue-2026-10.html"'
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Referrer-Policy"] == "no-referrer"
    assert resp.headers["Cache-Control"] == "no-store"
    csp = resp.headers["Content-Security-Policy"]
    # Sealed: its own origin (sandbox without allow-same-origin), its scripts
    # and print() allowed, nothing fetched, framed only by OpenFit's page.
    assert csp.startswith("sandbox allow-scripts allow-modals;")
    assert "allow-same-origin" not in csp and "default-src 'none'" in csp
    assert "frame-ancestors 'self'" in csp and "connect-src" not in csp


def test_the_view_route_is_404_for_what_isnt_there(client, apps_dir):
    for path in ("/api/app/reports/files/none.html/view", "/api/app/reports/files/Bad.HTML/view",
                 "/api/app/Bad Id/files/x.html/view"):
        assert client.get(path).status_code == 404
