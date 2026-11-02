"""
Report files: documents an app makes, for the user to download (C3).

An app's handle() makes a report as HTML text and saves it under a name,
in the app_files table (migration 012); render() lists them and offers
each as a download. OpenFit never shows one: its route always hands the
file over as an attachment, so a report's script never runs inside
OpenFit's pages.

- On the WriteCtx alone (handle()): ctx.save_file(name, html) upserts,
  keeping created_at; ctx.delete_file(name) -> whether one went. Inside
  the action's one transaction: nothing committed by either.
- On every ctx (render() and handle()): ctx.files() -> [{name, size,
  updated_at}] by name.
- Only the calling app's own: every query names the ctx's app.
- The rules: a name [a-z0-9][a-z0-9._-]{0,94}.html; html a str, stored
  as UTF-8 exactly. ValueError, the field named, for anything else.
- The limits: 10 MB a file, 200 MB and 1000 files an app, counting a
  save's net change - a shrink at the total still goes in.
- The spec: ctx.download(name, label=None); ctx.input(..., kind="button").
- The route: GET /api/app/<id>/files/<name>, always a download (five
  headers); GET /api/app/<id>/files lists; DELETE .../<name> deletes an
  uninstalled app's.
- Your data: GET /api/app-sources lists each app's files {count, bytes};
  Delete data takes them; uninstall keeps them.
- Migration 012 on a database as 6c77785 left it; app level 6.

Every test gets its own temp database and apps folder.
"""

import json
import sqlite3
import textwrap
from datetime import datetime, timezone

import pytest

import apps
import host
import main
from migrations import run_migrations

SIZE = "sizes: [{w: 1, h: 1}]\n"
NONE = {"count": 0, "bytes": 0}
MB = 1024 * 1024
HTML = "<!doctype html><title>Report</title><h1>October</h1><script>alert('hi')</script>\n"


@pytest.fixture
def apps_dir(tmp_path, monkeypatch):
    """An empty apps folder, which the app is pointed at."""
    path = tmp_path / "apps"
    path.mkdir()
    monkeypatch.setitem(main.app.config, "APPS_DIR", str(path))
    return path


def an_app(app_id):
    """An InstalledApp to make a ctx for - no folder."""
    return apps.InstalledApp(
        id=app_id, name=app_id, sizes=[{"w": 1, "h": 1}], default_size={"w": 1, "h": 1},
        reads=[], config=[], path="/nowhere", render=None,
    )


def write_ctx(conn, app_id="reports"):
    return host.WriteCtx(conn, an_app(app_id))


def read_ctx(conn, app_id="reports"):
    return host.Ctx(conn, app=an_app(app_id))


def stored(conn, app=None):
    """Every app_files row - or `app`'s - as (app, name, content, size,
    created_at, updated_at), by app and name."""
    sql = "SELECT app, name, content, size, created_at, updated_at FROM app_files"
    args = ()
    if app is not None:
        sql, args = sql + " WHERE app = ?", (app,)
    return [tuple(r) for r in conn.execute(sql + " ORDER BY app, name", args)]


def fresh_rows(db_path):
    """app_files as a connection of its own sees them: committed only."""
    other = sqlite3.connect(db_path)
    try:
        return [tuple(r) for r in other.execute("SELECT app, name, content FROM app_files ORDER BY app, name")]
    finally:
        other.close()


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


# ---------- 1. save, list, overwrite, delete ----------

CONTENTS = [
    HTML,
    "",
    "plain text, not HTML at all - stored as given",
    "ünïcödé, 日本語 and an emoji 🛫\r\nwith CRLF\x00and a NUL",
    "<p>" + "x" * 100_000 + "</p>",
]


@pytest.mark.parametrize("html", CONTENTS, ids=[f"content-{i}" for i in range(len(CONTENTS))])
def test_a_files_bytes_round_trip_exactly(conn, html):
    ctx = write_ctx(conn)

    ctx.save_file("report.html", html)

    [(app, name, content, size, _, _)] = stored(conn)
    assert (app, name) == ("reports", "report.html")
    assert isinstance(content, bytes) and content == html.encode("utf-8")
    assert size == len(html.encode("utf-8"))
    assert ctx.files() == read_ctx(conn).files() == [
        {"name": "report.html", "size": size, "updated_at": stored(conn)[0][5]}]


def test_an_overwrite_keeps_created_at_and_moves_updated_at(conn, pin_clock):
    ctx = write_ctx(conn)
    pin_clock(utc(2026, 11, 2, 8, 30, 15, 999999))
    ctx.save_file("report.html", "<p>first</p>")
    pin_clock(utc(2026, 11, 3, 9, 0, 0))
    ctx.save_file("report.html", "<p>second, longer</p>")

    [(_, _, content, size, created, updated)] = stored(conn)
    assert content == b"<p>second, longer</p>" and size == len(content)
    assert (created, updated) == ("2026-11-02T08:30:15Z", "2026-11-03T09:00:00Z")
    assert ctx.files() == [{"name": "report.html", "size": size, "updated_at": updated}]


def test_files_are_listed_by_name(conn):
    ctx = write_ctx(conn)
    for name in ("report-2026-10.html", "a.html", "report-2026-09.html", "0.html", "z_last.html"):
        ctx.save_file(name, name)

    assert [f["name"] for f in ctx.files()] == [
        "0.html", "a.html", "report-2026-09.html", "report-2026-10.html", "z_last.html"]
    assert read_ctx(conn).files() == ctx.files()
    assert read_ctx(conn, "other").files() == []


def test_delete_says_whether_a_file_went(conn):
    ctx = write_ctx(conn)
    ctx.save_file("a.html", "a")
    ctx.save_file("b.html", "b")

    assert ctx.delete_file("a.html") is True
    assert ctx.delete_file("a.html") is False
    assert ctx.delete_file("never.html") is False
    assert [f["name"] for f in ctx.files()] == ["b.html"]


def test_files_note_no_day_for_the_route_to_re_derive(conn):
    ctx = write_ctx(conn)
    ctx.save_file("a.html", "a")
    ctx.delete_file("a.html")
    assert ctx.written == []


# ---------- 2. isolation ----------


def test_two_apps_with_the_same_name_each_see_only_their_own(conn):
    one, two = write_ctx(conn, "one"), write_ctx(conn, "two")
    one.save_file("report.html", "<p>one's</p>")
    two.save_file("report.html", "<p>two's</p>")

    assert [f["size"] for f in one.files()] == [len("<p>one's</p>")]
    assert [f["size"] for f in two.files()] == [len("<p>two's</p>")]
    assert two.delete_file("report.html") is True
    assert [r[:3] for r in stored(conn)] == [("one", "report.html", b"<p>one's</p>")]
    assert two.files() == []


def test_no_file_method_takes_an_app():
    import inspect

    for method, params in (("save_file", ["self", "name", "html"]), ("delete_file", ["self", "name"]),
                           ("files", ["self"])):
        owner = host.WriteCtx if method != "files" else host.Ctx
        assert list(inspect.signature(getattr(owner, method)).parameters) == params


def test_a_ctx_for_no_app_has_no_files(conn):
    ctx = host.Ctx(conn)
    with pytest.raises(ValueError, match="files belong to an app"):
        ctx.files()


def test_the_route_serves_each_app_its_own(client, conn, apps_dir):
    write_ctx(conn, "one").save_file("report.html", "<p>one's</p>")
    write_ctx(conn, "two").save_file("report.html", "<p>two's</p>")
    write_ctx(conn, "two").save_file("only-two.html", "<p>two alone</p>")
    conn.commit()

    assert client.get("/api/app/one/files/report.html").data == b"<p>one's</p>"
    assert client.get("/api/app/two/files/report.html").data == b"<p>two's</p>"
    assert client.get("/api/app/one/files/only-two.html").status_code == 404
    assert client.get("/api/app/three/files/report.html").status_code == 404
    assert [f["name"] for f in client.get("/api/app/one/files").get_json()["files"]] == ["report.html"]
    assert [f["name"] for f in client.get("/api/app/two/files").get_json()["files"]] == [
        "only-two.html", "report.html"]


# ---------- 3. validation ----------

GOOD_NAMES = ["a.html", "0.html", "fatigue-2026-10.html", "a.b_c-d.html", "x..html",
              "a" * 95 + ".html"]
BAD_NAMES = [
    ("Report.html", "uppercase"),
    ("report.HTML", "uppercase extension"),
    ("report.htm", ".htm"),
    ("report", "no extension"),
    ("report.html.txt", "another extension"),
    ("../x.html", "a parent folder"),
    ("a/b.html", "a slash"),
    ("a\\b.html", "a backslash"),
    (".x.html", "a leading dot"),
    ("-x.html", "a leading dash"),
    ("_x.html", "a leading underscore"),
    (".html", "an empty stem"),
    ("a" * 96 + ".html", "101 characters"),
    ("a b.html", "a space"),
    ("a\".html", "a quote"),
    ("é.html", "not ASCII"),
    ("a.html\n", "a trailing newline"),
    ("", "empty"),
    (None, "None"),
    (5, "a number"),
    (b"a.html", "bytes"),
]


@pytest.mark.parametrize("name", GOOD_NAMES)
def test_a_good_name_is_taken(conn, name):
    ctx = write_ctx(conn)
    ctx.save_file(name, "x")
    assert [f["name"] for f in ctx.files()] == [name]
    assert ctx.download(name)["name"] == name
    assert ctx.delete_file(name) is True


@pytest.mark.parametrize("name, why", BAD_NAMES, ids=[why for _, why in BAD_NAMES])
def test_a_bad_name_is_refused_everywhere_with_nothing_written(conn, name, why):
    ctx = write_ctx(conn)
    with pytest.raises(ValueError, match="^name must be"):
        ctx.save_file(name, "x")
    with pytest.raises(ValueError, match="^name must be"):
        ctx.delete_file(name)
    with pytest.raises(ValueError, match="^name must be"):
        ctx.download(name)
    assert not host.is_file_name(name)
    assert stored(conn) == []


@pytest.mark.parametrize("html", [b"<p>bytes</p>", None, 5, ["<p>"], {"html": "x"}, bytearray(b"x")],
                         ids=["bytes", "None", "int", "list", "dict", "bytearray"])
def test_content_that_isnt_a_str_is_refused(conn, html):
    with pytest.raises(ValueError, match="^html must be text"):
        write_ctx(conn).save_file("report.html", html)
    assert stored(conn) == []


def test_text_utf8_cant_hold_is_refused(conn):
    with pytest.raises(ValueError, match="^html must be text UTF-8 can hold"):
        write_ctx(conn).save_file("report.html", "a lone \ud800 surrogate")
    assert stored(conn) == []


def test_nothing_is_injected_into_a_file(client, conn, apps_dir):
    write_ctx(conn).save_file("report.html", HTML)
    conn.commit()
    assert client.get("/api/app/reports/files/report.html").data == HTML.encode()


# ---------- 4. limits ----------


def test_a_file_is_at_most_10_mb(conn):
    ctx = write_ctx(conn)
    with pytest.raises(ValueError) as e:
        ctx.save_file("big.html", "x" * (10 * MB + 1))
    assert str(e.value) == "html is 10,485,761 bytes; a file holds at most 10 MB (10,485,760 bytes)"
    assert stored(conn) == []
    # Its UTF-8 bytes, not its characters: 2-byte characters past the limit.
    with pytest.raises(ValueError, match="10,485,762 bytes"):
        ctx.save_file("big.html", "é" * (5 * MB + 1))
    ctx.save_file("big.html", "x" * (10 * MB))
    assert ctx.files()[0]["size"] == 10 * MB


def fake(conn, app, name, size):
    """A row claiming `size` bytes - the limits read only the size column."""
    conn.execute(
        "INSERT INTO app_files (app, name, content, size, created_at, updated_at) VALUES (?, ?, ?, ?, 't', 't')",
        (app, name, b"x", size),
    )


def test_an_app_keeps_at_most_200_mb_and_a_shrink_at_the_total_still_goes_in(conn):
    ctx = write_ctx(conn)
    for i in range(19):
        fake(conn, "reports", f"filler-{i:02}.html", 10 * MB)
    fake(conn, "reports", "filler-19.html", 5 * MB)
    fake(conn, "reports", "a.html", 5 * MB)  # 200 MB exactly

    # A new file of a byte, or a's own grown by one, goes over.
    with pytest.raises(ValueError) as e:
        ctx.save_file("b.html", "x")
    assert str(e.value) == (
        "html would take the app's files to 209,715,201 bytes; an app keeps at most 200 MB of files "
        "(209,715,200 bytes) - delete some first")
    with pytest.raises(ValueError, match="209,715,201 bytes"):
        ctx.save_file("a.html", "x" * (5 * MB + 1))
    # The same size, or smaller, goes in at the total.
    ctx.save_file("a.html", "y" * 10)
    assert dict((r[1], r[3]) for r in stored(conn))["a.html"] == 10
    # And now there's room for the rest.
    ctx.save_file("b.html", "z" * (5 * MB - 10))
    assert conn.execute("SELECT SUM(size) FROM app_files").fetchone()[0] == 200 * MB


def test_an_app_over_its_total_may_still_shrink(conn):
    ctx = write_ctx(conn)
    fake(conn, "reports", "a.html", 205 * MB)  # over, as an older limit might leave it
    fake(conn, "reports", "b.html", 1000)

    ctx.save_file("b.html", "smaller")
    ctx.save_file("b.html", "smaller")  # the same size goes in too
    with pytest.raises(ValueError, match="delete some first"):
        ctx.save_file("c.html", "x")


def test_the_total_counts_only_that_apps_files(conn):
    fake(conn, "other", "a.html", 200 * MB)
    write_ctx(conn).save_file("a.html", "x" * 1000)
    assert [f["name"] for f in write_ctx(conn).files()] == ["a.html"]


def test_an_app_keeps_at_most_1000_files(conn):
    ctx = write_ctx(conn)
    for i in range(1000):
        fake(conn, "reports", f"f{i:04}.html", 1)
    fake(conn, "other", "more.html", 1)

    with pytest.raises(ValueError) as e:
        ctx.save_file("one-more.html", "")
    assert str(e.value) == (
        "name 'one-more.html' would be the app's 1,001th file; an app keeps at most 1,000 files - delete some first")
    # Overwriting one of the thousand is no new file.
    ctx.save_file("f0000.html", "replaced")
    assert ctx.delete_file("f0001.html") is True
    ctx.save_file("one-more.html", "")
    assert conn.execute("SELECT COUNT(*) FROM app_files WHERE app = 'reports'").fetchone()[0] == 1000


# ---------- 5. the transaction, through the action route ----------

REPORTS = """
import json

def render(ctx, cfg):
    return ctx.panel(
        [ctx.input("make", "make", label="Make report", kind="button")]
        + [ctx.download(f["name"]) for f in ctx.files()]
    )

def handle(ctx, action, payload):
    if action == "make":
        if payload != {"make": True}:
            raise ValueError(f"unexpected payload {payload!r}")
        ctx.save_file("report.html", "<h1>Report</h1>")
    elif action == "save":
        ctx.save_file(payload["name"], payload["html"])
    elif action == "save_then_fail":
        ctx.save_file(payload["name"], payload["html"])
        ctx.save_file("another.html", "x")
        raise ValueError("refused after the save")
    elif action == "save_then_crash":
        ctx.save_file(payload["name"], payload["html"])
        1 / 0
    elif action == "delete":
        ctx.delete_file(payload["name"])
    elif action == "delete_then_fail":
        assert ctx.delete_file(payload["name"]) is True
        raise ValueError("refused after the delete")
    elif action == "bad_name":
        ctx.save_file("Report.HTML", "x")
    else:
        raise ValueError(f"unknown action {action}")
"""


def make_app(root, app_id, code, extra=""):
    path = root / app_id
    path.mkdir()
    (path / apps.MANIFEST).write_text(f"id: {app_id}\nname: {app_id}\n{extra}{SIZE}")
    (path / apps.MODULE).write_text(textwrap.dedent(code))
    return path


@pytest.fixture
def reports_app(apps_dir):
    return make_app(apps_dir, "reports", REPORTS, extra="requires: 6\n")


def act(client, action, app_id="reports", **payload):
    return client.post(f"/api/app/{app_id}/action", json={"action": action, "payload": payload})


def test_a_save_then_a_raise_leaves_no_file(client, db_path, reports_app):
    resp = act(client, "save_then_fail", name="report.html", html="<p>x</p>")

    assert (resp.status_code, resp.get_json()) == (400, {"ok": False, "error": "refused after the save"})
    assert fresh_rows(db_path) == []
    resp = act(client, "save_then_crash", name="report.html", html="<p>x</p>")
    assert resp.status_code == 400 and resp.get_json()["error"].startswith("ZeroDivisionError")
    assert fresh_rows(db_path) == []


def test_a_successful_save_is_committed_and_the_tile_offers_it(client, db_path, reports_app):
    resp = act(client, "save", name="report.html", html="<p>x</p>")

    assert resp.status_code == 200
    [button, download] = resp.get_json()["spec"]["children"]
    assert button == {"type": "input", "key": "make", "action": "make", "label": "Make report",
                      "kind": "button", "unit": None, "options": None, "accept": None}
    assert download == {"type": "download", "name": "report.html", "label": "report.html"}
    assert fresh_rows(db_path) == [("reports", "report.html", b"<p>x</p>")]


def test_a_delete_then_a_raise_rolls_back_too(client, db_path, reports_app):
    assert act(client, "save", name="kept.html", html="kept").status_code == 200

    resp = act(client, "delete_then_fail", name="kept.html")

    assert (resp.status_code, resp.get_json()) == (400, {"ok": False, "error": "refused after the delete"})
    assert fresh_rows(db_path) == [("reports", "kept.html", b"kept")]
    assert act(client, "delete", name="kept.html").status_code == 200
    assert fresh_rows(db_path) == []


def test_a_refusal_is_said_as_the_ctx_put_it(client, db_path, reports_app):
    resp = act(client, "bad_name")
    assert resp.status_code == 400 and resp.get_json()["error"].startswith("name must be")
    assert fresh_rows(db_path) == []


# ---------- 6. the download route ----------


def test_a_download_is_always_an_attachment_with_the_five_headers(client, conn, apps_dir):
    write_ctx(conn).save_file("fatigue-2026-10.html", HTML)
    conn.commit()

    resp = client.get("/api/app/reports/files/fatigue-2026-10.html")

    assert resp.status_code == 200
    assert resp.data == HTML.encode("utf-8")
    assert resp.headers["Content-Type"] == "application/octet-stream"
    assert resp.headers["Content-Disposition"] == 'attachment; filename="fatigue-2026-10.html"'
    assert resp.headers["X-Content-Type-Options"] == "nosniff"
    assert resp.headers["Content-Security-Policy"] == "sandbox"
    assert resp.headers["Cache-Control"] == "no-store"
    # A HEAD (the page's check before it downloads) has them too, no body.
    head = client.head("/api/app/reports/files/fatigue-2026-10.html")
    assert head.status_code == 200 and head.data == b""
    assert head.headers["Content-Disposition"] == 'attachment; filename="fatigue-2026-10.html"'


def test_a_download_changes_nothing(client, conn, apps_dir, db_path):
    write_ctx(conn).save_file("a.html", "a")
    conn.commit()
    before = fresh_rows(db_path), stored(conn)
    for _ in range(2):
        client.get("/api/app/reports/files/a.html")
    assert (fresh_rows(db_path), stored(conn)) == before


@pytest.mark.parametrize("path", [
    "/api/app/reports/files/missing.html",
    "/api/app/reports/files/Report.html",
    "/api/app/reports/files/report.htm",
    "/api/app/reports/files/.html",
    "/api/app/reports/files/..%2Fx.html",
    "/api/app/Bad%20Id/files/a.html",
    "/api/app/..reports/files/a.html",
    "/api/app/-x/files/a.html",
], ids=["missing", "uppercase", "htm", "empty-stem", "parent", "bad-id-space", "bad-id-dots", "bad-id-dash"])
def test_404_for_a_missing_file_a_bad_name_or_a_bad_app_id(client, conn, apps_dir, path):
    write_ctx(conn).save_file("a.html", "a")
    conn.commit()
    resp = client.get(path)
    assert resp.status_code == 404
    assert resp.headers.get("Content-Disposition") is None


def test_a_signed_out_download_is_401(client, conn, apps_dir):
    write_ctx(conn).save_file("a.html", "a")
    conn.commit()
    with client.session_transaction() as sess:
        sess.clear()
    for method in ("get", "head", "delete"):
        resp = getattr(client, method)("/api/app/reports/files/a.html")
        assert resp.status_code == 401
    assert client.get("/api/app/reports/files").status_code == 401


def test_an_uninstalled_apps_file_still_downloads(client, conn, reports_app):
    assert act(client, "make", make=True).status_code == 200
    assert client.delete("/api/apps/reports").status_code == 200

    resp = client.get("/api/app/reports/files/report.html")

    assert resp.status_code == 200 and resp.data == b"<h1>Report</h1>"
    assert resp.headers["Content-Disposition"] == 'attachment; filename="report.html"'


def test_the_listing_route(client, conn, apps_dir, pin_clock):
    pin_clock(utc(2026, 11, 2, 10, 0, 0))
    write_ctx(conn).save_file("b.html", "bb")
    write_ctx(conn).save_file("a.html", "a")
    conn.commit()

    assert client.get("/api/app/reports/files").get_json() == {"id": "reports", "files": [
        {"name": "a.html", "size": 1, "updated_at": "2026-11-02T10:00:00Z"},
        {"name": "b.html", "size": 2, "updated_at": "2026-11-02T10:00:00Z"},
    ]}
    assert client.get("/api/app/nothing/files").get_json() == {"id": "nothing", "files": []}
    assert client.get("/api/app/Bad%20Id/files").status_code == 404


# ---------- 7. the button input ----------


def test_a_button_input(conn):
    spec = host.input("make", "report", label="Make October's report", kind="button")
    assert spec == {"type": "input", "key": "make", "action": "report", "label": "Make October's report",
                    "kind": "button", "unit": None, "options": None, "accept": None}
    assert read_ctx(conn).input("make", "report", label="Go", kind="button")["kind"] == "button"


@pytest.mark.parametrize("kwargs, error, match", [
    ({}, ValueError, "a button needs a label"),
    ({"label": None}, ValueError, "a button needs a label"),
    ({"label": ""}, ValueError, "non-empty text"),
    ({"label": 5}, TypeError, "non-empty text"),
    ({"label": "Go", "unit": "kg"}, ValueError, "a unit isn't for a button"),
    ({"label": "Go", "options": ["a"]}, ValueError, "options is only for a select"),
    ({"label": "Go", "accept": ".ics"}, ValueError, "accept is only for a file"),
    ({"label": "Go", "toggle": {"key": "b", "label": "B"}}, ValueError, "a toggle is only for a file"),
])
def test_a_button_needs_a_label_and_nothing_else(kwargs, error, match):
    with pytest.raises(error, match=match):
        host.input("make", "report", kind="button", **kwargs)


def test_pressing_the_button_sends_key_true_and_the_tile_offers_the_new_file(client, db_path, reports_app):
    spec = client.post("/api/app/reports/render", json={}).get_json()
    assert [c["type"] for c in spec["children"]] == ["input"]

    resp = act(client, "make", make=True)

    assert resp.status_code == 200
    assert resp.get_json()["spec"]["children"][1] == {
        "type": "download", "name": "report.html", "label": "report.html"}
    assert fresh_rows(db_path) == [("reports", "report.html", b"<h1>Report</h1>")]
    # The app itself held the payload to {make: true}.
    assert act(client, "make", make="yes").status_code == 400


# ---------- 8. the download spec ----------


def test_the_download_builder(conn):
    assert host.download("a.html") == {"type": "download", "name": "a.html", "label": "a.html"}
    assert read_ctx(conn).download("a.html", "October <b>report</b>") == {
        "type": "download", "name": "a.html", "label": "October <b>report</b>"}
    with pytest.raises(TypeError):
        host.download("a.html", 5)
    with pytest.raises(ValueError):
        host.download("a.html", "")
    with pytest.raises(ValueError, match="^name must be"):
        host.download("../a.html")


def test_a_download_of_a_file_not_there_is_left_as_it_is(client, apps_dir):
    make_app(apps_dir, "ghost", """
        def render(ctx, cfg):
            return ctx.download("gone.html", "Gone")
    """)
    spec = client.post("/api/app/ghost/render", json={}).get_json()
    assert spec == {"type": "download", "name": "gone.html", "label": "Gone"}
    assert client.get("/api/app/ghost/files/gone.html").status_code == 404


# ---------- 9. Your data ----------


def listing(client, app_id):
    [entry] = [e for e in client.get("/api/app-sources").get_json() if e["id"] == app_id]
    return entry


def test_the_listing_counts_each_apps_files_and_their_bytes(client, conn, apps_dir):
    make_app(apps_dir, "reports", REPORTS)
    write_ctx(conn, "reports").save_file("a.html", "12345")
    write_ctx(conn, "reports").save_file("b.html", "ü")
    write_ctx(conn, "solo").save_file("c.html", "x")
    write_ctx(conn, "solo").put_record("c", "k", 1)
    conn.commit()

    reports = listing(client, "reports")
    assert reports["files"] == {"count": 2, "bytes": 7}
    assert reports["orphaned"] is False
    # An app with only files and records is listed, uninstalled as it is.
    solo = listing(client, "solo")
    assert solo == {"id": "solo", "source": "app:solo", "orphaned": True, "metrics": [], "readings": 0,
                    "sessions": 0, "samples": 0, "records": {"count": 1, "bytes": 1},
                    "files": {"count": 1, "bytes": 1}}


def test_every_entry_says_files_even_with_none(client, conn, apps_dir):
    conn.execute(
        "INSERT INTO metrics (date, source, metric, value, unit, synced_at) "
        "VALUES ('2026-10-02', 'app:old', 'steps', 5, 'count', 'x')"
    )
    conn.commit()
    assert listing(client, "old")["files"] == NONE


def test_the_per_file_delete_needs_the_header_and_an_uninstalled_app(client, conn, reports_app, db_path):
    assert act(client, "save", name="a.html", html="a").status_code == 200
    assert act(client, "save", name="b.html", html="b").status_code == 200

    # Installed: its files are its own.
    resp = client.delete("/api/app/reports/files/a.html")
    assert resp.status_code == 400 and "is present" in resp.get_json()["error"]
    assert client.delete("/api/apps/reports").status_code == 200

    # No X-OpenFit: refused by the guard, nothing deleted.
    del client.environ_base["HTTP_X_OPENFIT"]
    resp = client.delete("/api/app/reports/files/a.html")
    assert resp.status_code == 403
    resp = client.delete("/api/app/reports/files/a.html", headers={"X-OpenFit": "0"})
    assert resp.status_code == 403
    assert len(fresh_rows(db_path)) == 2
    client.environ_base["HTTP_X_OPENFIT"] = "1"

    resp = client.delete("/api/app/reports/files/a.html")
    assert (resp.status_code, resp.get_json()) == (200, {"ok": True})
    assert fresh_rows(db_path) == [("reports", "b.html", b"b")]
    assert listing(client, "reports")["files"] == {"count": 1, "bytes": 1}
    assert client.delete("/api/app/reports/files/a.html").status_code == 404
    assert client.delete("/api/app/reports/files/A.html").status_code == 404
    assert client.delete("/api/app/Bad%20Id/files/a.html").status_code == 404


def test_delete_data_removes_files(client, conn, reports_app, db_path):
    assert act(client, "make", make=True).status_code == 200
    write_ctx(conn, "neighbour").save_file("report.html", "kept")
    conn.commit()
    assert client.delete("/api/apps/reports").status_code == 200
    assert listing(client, "reports")["files"] == {"count": 1, "bytes": len("<h1>Report</h1>")}

    resp = client.delete("/api/app-sources/reports")

    assert (resp.status_code, resp.get_json()) == (200, {"ok": True, "removed": {
        "metrics": 0, "sessions": 0, "samples": 0, "records": 0, "files": 1}})
    assert fresh_rows(db_path) == [("neighbour", "report.html", b"kept")]
    assert client.get("/api/app/reports/files/report.html").status_code == 404
    assert "reports" not in [e["id"] for e in client.get("/api/app-sources").get_json()]


def test_the_per_metric_delete_leaves_the_files(client, conn, apps_dir, db_path):
    conn.execute(
        "INSERT INTO metrics (date, source, metric, value, unit, synced_at) "
        "VALUES ('2026-10-02', 'app:old', 'steps', 5, 'count', 'x')"
    )
    write_ctx(conn, "old").save_file("a.html", "a")
    conn.commit()

    resp = client.delete("/api/app-sources/old/steps")

    assert resp.get_json()["removed"] == {"metrics": 1, "sessions": 0, "samples": 0, "records": 0, "files": 0}
    assert fresh_rows(db_path) == [("old", "a.html", b"a")]


def test_a_devices_wipe_never_reaches_an_app_of_the_same_id(conn):
    write_ctx(conn, "garmin").save_file("a.html", "a")
    conn.commit()
    assert main.purge_source(conn, "garmin")["files"] == 0
    assert [r[:2] for r in stored(conn)] == [("garmin", "a.html")]


def test_uninstall_then_reinstall_finds_the_files(client, conn, reports_app, db_path):
    assert act(client, "make", make=True).status_code == 200
    files = {"app.yaml": "id: reports\nname: reports\nrequires: 6\n" + SIZE, "app.py": textwrap.dedent(REPORTS)}
    assert client.delete("/api/apps/reports").status_code == 200
    assert listing(client, "reports")["orphaned"] is True

    assert client.post("/api/apps", json={"files": files}).status_code == 201

    spec = client.post("/api/app/reports/render", json={}).get_json()
    assert spec["children"][1] == {"type": "download", "name": "report.html", "label": "report.html"}
    assert listing(client, "reports")["orphaned"] is False


# ---------- 10. render() can't write ----------


def test_the_render_ctx_lists_files_and_cant_write_them(conn):
    for ctx in (host.Ctx(conn), host.Ctx(conn, app=an_app("reports"))):
        assert callable(ctx.files) and callable(ctx.download)
        assert not hasattr(ctx, "save_file")
        assert not hasattr(ctx, "delete_file")
    ctx = write_ctx(conn)
    assert all(callable(getattr(ctx, m)) for m in ("files", "download", "save_file", "delete_file"))


def test_a_render_that_tries_to_save_a_file_is_an_error_and_writes_nothing(client, conn, apps_dir):
    make_app(apps_dir, "sneak", """
        def render(ctx, cfg):
            ctx.save_file("a.html", "x")
            return ctx.empty("wrote")
    """)
    spec = client.post("/api/app/sneak/render", json={}).get_json()
    assert spec["type"] == "error" and "save_file" in spec["message"]
    assert stored(conn) == []


# ---------- 11. migration 012 and app level 6 ----------

FILE_COLUMNS = ["app", "name", "content", "size", "created_at", "updated_at"]


def snapshot(conn):
    """Every table but schema_migrations: its schema and rows."""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name != 'schema_migrations' ORDER BY name")]
    return {
        t: (conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (t,)).fetchone()[0],
            sorted(conn.execute(f"SELECT * FROM {t}").fetchall(), key=repr))
        for t in tables
    }


def test_012_applies_on_a_database_as_6c77785_left_it(db_at_version):
    """6c77785's newest migration is 011: a database there, holding data,
    gets 012 alone - a table and its index - and nothing else changes."""
    conn = sqlite3.connect(db_at_version(11))
    conn.execute(
        "INSERT INTO metrics (date, source, metric, value, unit, synced_at) "
        "VALUES ('2026-10-02', 'app:reports', 'steps', 5, 'count', 'x')"
    )
    conn.execute("INSERT INTO app_records VALUES ('reports', 'c', 'k', '1', 1, 't', 't')")
    conn.execute("INSERT INTO settings VALUES ('home_layout', '{}')")
    conn.commit()
    assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 11
    before = snapshot(conn)
    assert "app_files" not in before

    assert run_migrations(conn) == [12]

    columns = conn.execute("PRAGMA table_info(app_files)").fetchall()
    assert [c[1] for c in columns] == FILE_COLUMNS
    assert [c[2] for c in columns] == ["TEXT", "TEXT", "BLOB", "INTEGER", "TEXT", "TEXT"]
    assert all(c[3] for c in columns)  # every column NOT NULL
    assert [c[1] for c in sorted(columns, key=lambda c: c[5]) if c[5]] == ["app", "name"]
    assert [r[2] for r in conn.execute("PRAGMA index_info(idx_app_files_size)")] == ["app", "size"]
    assert conn.execute("SELECT COUNT(*) FROM app_files").fetchone()[0] == 0
    after = snapshot(conn)
    assert {t: after[t] for t in before} == before
    assert run_migrations(conn) == []
    conn.close()


def test_one_file_per_app_and_name(conn):
    conn.execute("INSERT INTO app_files VALUES ('reports', 'a.html', x'00', 1, 't', 't')")
    conn.execute("INSERT INTO app_files VALUES ('other', 'a.html', x'00', 1, 't', 't')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO app_files VALUES ('reports', 'a.html', x'01', 1, 't', 't')")


def test_the_totals_are_read_from_the_index_alone(conn):
    plan = " ".join(r[3] for r in conn.execute(
        "EXPLAIN QUERY PLAN SELECT COUNT(*), COALESCE(SUM(size), 0) FROM app_files WHERE app = ?", ("x",)))
    assert "COVERING INDEX idx_app_files_size" in plan


def test_this_openfit_is_app_level_six(client, apps_dir, capsys):
    assert apps.APP_LEVEL == 6
    assert apps.level_problem(6) is None
    seven = "needs OpenFit app level 7; this OpenFit is level 6"
    assert apps.level_problem(7) == seven
    make_app(apps_dir, "reports", REPORTS, extra="requires: 6\n")
    make_app(apps_dir, "newer", REPORTS, extra="requires: 7\n")

    # requires: 6 loads, and its files work; requires: 7 is skipped ...
    assert [(a.id, a.requires) for a in apps.scan_apps(apps_dir)] == [("reports", 6)]
    assert f"[apps] skipping {apps_dir / 'newer'}: {seven}" in capsys.readouterr().out
    assert act(client, "make", make=True).status_code == 200
    # ... and refused at install.
    resp = client.post("/api/apps", json={"files": {"app.yaml": f"id: newest\nrequires: 7\n{SIZE}"}})
    assert (resp.status_code, resp.get_json()) == (400, {"ok": False, "error": seven})
    resp = client.post("/api/apps", json={"files": {"app.yaml": f"id: fine\nrequires: 6\n{SIZE}"}})
    assert resp.status_code == 201
