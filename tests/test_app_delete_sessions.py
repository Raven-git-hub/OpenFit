"""
An app removing its own sessions: ctx.delete_sessions() (C1).

write_session() upserts on app:<id>:<kind>:<start>, so an app can add a
session or replace one, but a session the app no longer has - a flight
taken off a re-uploaded roster - keeps its row. delete_sessions(kind,
since, until) removes the app's own sessions of `kind` whose start is in
[since, until), and says how many: a roster app clears the roster's span,
then writes what the new roster has, in one action.

- Own data only: the DELETE is scoped to the ctx's source, app:<id>, and
  no argument widens it - a device's sessions, or another app's, are out
  of reach by construction.
- On the WriteCtx alone, so handle() has it and render() hasn't.
- since and until are instants, not days (a roster's span isn't whole
  days), read as write_session() reads a start: a naive time is UTC.
  Both required; until after since.
- Nothing committed: the action route's one transaction holds it, so an
  action that deletes and then raises removes nothing.
- Nothing re-derived: sessions feed no derived value.

And app level 4, which an app using it declares.

Every test gets its own temp database and apps folder.
"""

import sqlite3
import textwrap
from datetime import date, datetime, timedelta, timezone

import pytest

import apps
import host
from metrics import DUTIES_COMPLETE, DUTIES_ROSTERED, SLEEP, WORKOUT
from plugins.base import write_session

SIZE = "sizes: [{w: 1, h: 1}]\n"


@pytest.fixture
def apps_dir(tmp_path, monkeypatch):
    """An empty apps folder, which the app is pointed at."""
    import main

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


def make_app(root, app_id, code, extra=""):
    """An app folder: app.yaml naming `app_id`, app.py from `code`."""
    path = root / app_id
    path.mkdir()
    (path / apps.MANIFEST).write_text(f"id: {app_id}\nname: {app_id}\n{extra}{SIZE}")
    (path / apps.MODULE).write_text(textwrap.dedent(code))
    return path


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def stamp(moment):
    """A UTC datetime as a session's start is stored."""
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def rows(conn, source=None, kind=None):
    """(source, kind, start, end) of every session - or `source`'s, of
    `kind` - by source, kind and start."""
    where, args = [], []
    if source is not None:
        where.append("source = ?")
        args.append(source)
    if kind is not None:
        where.append("kind = ?")
        args.append(kind)
    sql = 'SELECT source, kind, "start", "end" FROM sessions'
    if where:
        sql += " WHERE " + " AND ".join(where)
    return [tuple(r) for r in conn.execute(sql + ' ORDER BY source, kind, "start"', args)]


def starts(conn, source, kind=DUTIES_ROSTERED):
    return [r[2] for r in rows(conn, source, kind)]


SINCE = utc(2026, 11, 3, 6, 0)
UNTIL = utc(2026, 11, 10, 6, 0)


# ---------- the range: [since, until), on the start ----------


def test_the_range_is_half_open_on_the_start(conn):
    ctx = host.WriteCtx(conn, an_app("roster"))
    second = timedelta(seconds=1)
    # A second before since, ending well inside the range: kept - it's
    # the start that's matched, never the end.
    ctx.write_session(DUTIES_ROSTERED, SINCE - second, SINCE + timedelta(hours=9))
    ctx.write_session(DUTIES_ROSTERED, SINCE, SINCE + timedelta(hours=10))          # at since: gone
    ctx.write_session(DUTIES_ROSTERED, SINCE + second, SINCE + timedelta(hours=3))  # inside: gone
    ctx.write_session(DUTIES_ROSTERED, utc(2026, 11, 6, 14, 30), None)              # no end: gone
    ctx.write_session(DUTIES_ROSTERED, UNTIL - second, UNTIL + timedelta(hours=8))  # last second: gone
    ctx.write_session(DUTIES_ROSTERED, UNTIL, UNTIL + timedelta(hours=2))           # at until: kept
    ctx.write_session(DUTIES_ROSTERED, UNTIL + second, UNTIL + timedelta(hours=2))  # after: kept

    removed = ctx.delete_sessions(DUTIES_ROSTERED, SINCE, UNTIL)

    assert removed == 4
    assert starts(conn, "app:roster") == [stamp(SINCE - second), stamp(UNTIL), stamp(UNTIL + second)]
    # Nothing is left in the range, so a second go removes none.
    assert ctx.delete_sessions(DUTIES_ROSTERED, SINCE, UNTIL) == 0
    # Sessions feed no derived value: no day noted for the route to re-derive.
    assert ctx.written == []


def test_the_count_is_the_rows_removed(conn):
    ctx = host.WriteCtx(conn, an_app("roster"))
    assert ctx.delete_sessions(DUTIES_ROSTERED, SINCE, UNTIL) == 0
    for day in range(3, 10):
        ctx.write_session(DUTIES_ROSTERED, utc(2026, 11, day, 6, 0), utc(2026, 11, day, 16, 0))

    assert ctx.delete_sessions(DUTIES_ROSTERED, utc(2026, 11, 5), utc(2026, 11, 8)) == 3
    assert ctx.delete_sessions(DUTIES_ROSTERED, SINCE, UNTIL) == 4
    assert rows(conn) == []


# ---------- own source only ----------


def test_only_the_apps_own_sessions_are_removed(conn):
    start, later = utc(2026, 11, 4, 1, 0), utc(2026, 11, 8, 23, 15)
    duty = timedelta(hours=10)
    # The same kind and starts under a device, another app and this app.
    for moment in (start, later):
        write_session(conn, "garmin", DUTIES_ROSTERED, stamp(moment), stamp(moment + duty), None)
        host.WriteCtx(conn, an_app("other")).write_session(DUTIES_ROSTERED, moment, moment + duty)
        host.WriteCtx(conn, an_app("roster")).write_session(DUTIES_ROSTERED, moment, moment + duty)
    # An app named after the device writes app:garmin, never garmin.
    host.WriteCtx(conn, an_app("garmin")).write_session(DUTIES_ROSTERED, start, start + duty)

    assert host.WriteCtx(conn, an_app("roster")).delete_sessions(DUTIES_ROSTERED, SINCE, UNTIL) == 2

    assert starts(conn, "app:roster") == []
    assert starts(conn, "garmin") == [stamp(start), stamp(later)]
    assert starts(conn, "app:other") == [stamp(start), stamp(later)]
    assert starts(conn, "app:garmin") == [stamp(start)]
    # And the app called garmin reaches only app:garmin, never the device's.
    assert host.WriteCtx(conn, an_app("garmin")).delete_sessions(DUTIES_ROSTERED, SINCE, UNTIL) == 1
    assert starts(conn, "garmin") == [stamp(start), stamp(later)]
    # No argument names a source.
    with pytest.raises(TypeError):
        host.WriteCtx(conn, an_app("roster")).delete_sessions(DUTIES_ROSTERED, SINCE, UNTIL, source="garmin")


# ---------- the kind ----------


def test_only_the_kind_named_is_removed(conn):
    ctx = host.WriteCtx(conn, an_app("roster"))
    at = utc(2026, 11, 5, 2, 0)
    for kind in (DUTIES_ROSTERED, DUTIES_COMPLETE, SLEEP, WORKOUT):
        ctx.write_session(kind, at, at + timedelta(hours=6))

    assert ctx.delete_sessions(DUTIES_ROSTERED, SINCE, UNTIL) == 1

    assert [r[1] for r in rows(conn, "app:roster")] == [DUTIES_COMPLETE, SLEEP, WORKOUT]


def test_an_unknown_kind_is_refused_with_nothing_removed(conn):
    ctx = host.WriteCtx(conn, an_app("roster"))
    ctx.write_session(DUTIES_ROSTERED, utc(2026, 11, 5, 2, 0))
    before = rows(conn)

    for kind in ("duties", "Duties_Rostered", "steps", "heart_rate", "", None, 3, [DUTIES_ROSTERED]):
        with pytest.raises(ValueError) as refused:
            ctx.delete_sessions(kind, SINCE, UNTIL)
        assert str(refused.value) == f"unknown session kind: {kind!r}"
    # Checked first, before the bounds.
    with pytest.raises(ValueError, match="^unknown session kind: 'nap'$"):
        ctx.delete_sessions("nap", None, None)
    assert rows(conn) == before


# ---------- the bounds ----------


def test_both_bounds_are_required(conn):
    ctx = host.WriteCtx(conn, an_app("roster"))
    ctx.write_session(DUTIES_ROSTERED, utc(2026, 11, 5, 2, 0))
    before = rows(conn)

    for since, until, says in ((None, UNTIL, "since"), (SINCE, None, "until"), (None, None, "since")):
        with pytest.raises(ValueError) as refused:
            ctx.delete_sessions(DUTIES_ROSTERED, since, until)
        assert str(refused.value) == f"delete_sessions needs a {says}: a delete can't be open-ended"
    # Keyword or positional, neither has a default.
    with pytest.raises(TypeError):
        ctx.delete_sessions(DUTIES_ROSTERED)
    assert rows(conn) == before


def test_until_must_be_after_since(conn):
    ctx = host.WriteCtx(conn, an_app("roster"))
    ctx.write_session(DUTIES_ROSTERED, SINCE)
    before = rows(conn)

    for until in (SINCE, SINCE - timedelta(seconds=1), utc(2026, 1, 1),
                  # The same instant said another way, and one before it.
                  "2026-11-03T14:00:00+08:00", "2026-11-03T13:59:59+08:00",
                  # Inside since's second: cut to the second, as starts are stored.
                  SINCE + timedelta(microseconds=999999)):
        with pytest.raises(ValueError) as refused:
            ctx.delete_sessions(DUTIES_ROSTERED, SINCE, until)
        assert str(refused.value) == "delete_sessions' until must be after its since"
    assert rows(conn) == before


def test_bounds_resolve_as_a_write_sessions_start_does(conn):
    ctx = host.WriteCtx(conn, an_app("roster"))
    midnight = utc(2026, 11, 1, 0, 0)
    ctx.write_session(DUTIES_ROSTERED, midnight, midnight + timedelta(hours=8))
    one_second = "2026-11-01T08:00:01+08:00"

    # 08:00 in Hong Kong is 00:00Z: the session is in [08:00, 08:00:01) +08:00.
    assert ctx.delete_sessions(DUTIES_ROSTERED, "2026-11-01T08:00:00+08:00", one_second) == 1
    ctx.write_session(DUTIES_ROSTERED, midnight, midnight + timedelta(hours=8))
    # A naive 08:00 is 08:00 UTC, as write_session() takes one: not the session's.
    assert ctx.delete_sessions(DUTIES_ROSTERED, "2026-11-01T08:00:00", "2026-11-01T09:00:00") == 0
    assert ctx.delete_sessions(DUTIES_ROSTERED, datetime(2026, 11, 1, 8, 0), datetime(2026, 11, 1, 9)) == 0
    # And naive text or a naive datetime at 00:00 is the session's start.
    assert ctx.delete_sessions(DUTIES_ROSTERED, "2026-11-01T00:00:00", "2026-11-01T00:00:01") == 1
    ctx.write_session(DUTIES_ROSTERED, midnight)
    assert ctx.delete_sessions(DUTIES_ROSTERED, datetime(2026, 11, 1), datetime(2026, 11, 1, 0, 0, 1)) == 1
    # A trailing Z, an aware datetime in another zone, and a fraction cut to the second.
    ctx.write_session(DUTIES_ROSTERED, midnight)
    hong_kong = timezone(timedelta(hours=8))
    assert ctx.delete_sessions(DUTIES_ROSTERED, "2026-10-31T23:59:59.5Z",
                               datetime(2026, 11, 1, 8, 0, 1, tzinfo=hong_kong)) == 1
    assert rows(conn) == []


def test_a_bound_that_isnt_an_instant_is_refused(conn):
    ctx = host.WriteCtx(conn, an_app("roster"))
    ctx.write_session(DUTIES_ROSTERED, utc(2026, 11, 5, 2, 0))
    before = rows(conn)

    # Instants, not days: a date is refused, as write_session() refuses one.
    for bad in (date(2026, 11, 3), 20261103, 1.5, "next week", "", "2026-11-03T25:00", [SINCE]):
        with pytest.raises(ValueError) as refused:
            ctx.delete_sessions(DUTIES_ROSTERED, bad, UNTIL)
        assert str(refused.value) == f"since must be a datetime or ISO-8601 timestamp: {bad!r}"
        with pytest.raises(ValueError) as refused:
            ctx.delete_sessions(DUTIES_ROSTERED, SINCE, bad)
        assert str(refused.value) == f"until must be a datetime or ISO-8601 timestamp: {bad!r}"
    assert rows(conn) == before


# ---------- the transaction: through the action route ----------

# A roster app, as FRMS will use it: its upload clears the roster's span,
# then files what the new roster has. The payload carries the span and
# the new duties ([start, end] pairs); "fail" refuses the upload after
# clearing; "clear" only clears.
ROSTER = """
    def render(ctx, cfg):
        return ctx.empty("roster")

    def handle(ctx, action, payload):
        removed = ctx.delete_sessions("duties_rostered", payload["since"], payload["until"])
        if action == "fail":
            raise ValueError(f"No duties found in roster.ics (cleared {removed})")
        if action == "replace":
            for start, end in payload["duties"]:
                ctx.write_session("duties_rostered", start, end, {"code": "CX"})
"""

DUTIES = [
    ("2026-11-03T22:00:00Z", "2026-11-04T09:00:00Z"),
    ("2026-11-05T01:30:00Z", "2026-11-05T13:00:00Z"),
    ("2026-11-07T23:15:00Z", "2026-11-08T08:45:00Z"),
    ("2026-11-09T04:00:00Z", "2026-11-09T15:30:00Z"),
]
SPAN = {"since": "2026-11-03T06:00:00Z", "until": "2026-11-10T06:00:00Z"}


@pytest.fixture
def roster(conn, apps_dir):
    """The roster app installed, its four duties filed (committed), and a
    device's duty at the same start as one of them."""
    make_app(apps_dir, "roster", ROSTER, extra="requires: 4\n")
    ctx = host.WriteCtx(conn, an_app("roster"))
    for start, end in DUTIES:
        ctx.write_session(DUTIES_ROSTERED, start, end, {"code": "CX"})
    write_session(conn, "garmin", DUTIES_ROSTERED, DUTIES[0][0], DUTIES[0][1], None)
    conn.commit()
    return rows(conn)


def act(client, action, **payload):
    return client.post("/api/app/roster/action", json={"action": action, "payload": {**SPAN, **payload}})


def test_an_action_that_deletes_then_raises_removes_nothing(client, conn, roster):
    resp = act(client, "fail")

    assert resp.status_code == 400
    assert resp.get_json() == {"ok": False, "error": "No duties found in roster.ics (cleared 4)"}
    # Rolled back with the rest of the action: every row where it was.
    assert rows(conn) == roster


def test_an_action_that_clears_the_span_and_rewrites_a_subset_leaves_the_subset(client, conn, roster):
    # The new roster dropped the second and fourth duties and moved the
    # first's end; the device's duty at the first's start is untouched.
    kept = [(DUTIES[0][0], "2026-11-04T10:30:00Z"), DUTIES[2]]

    resp = act(client, "replace", duties=kept)

    assert resp.get_json() == {"ok": True, "spec": {"type": "empty", "message": "roster"}}
    assert rows(conn, "app:roster") == [("app:roster", DUTIES_ROSTERED, s, e) for s, e in kept]
    assert rows(conn, "garmin") == [("garmin", DUTIES_ROSTERED, *DUTIES[0])]
    # Re-created by the upsert, under the same ids.
    assert [r[0] for r in conn.execute("SELECT id FROM sessions WHERE source = 'app:roster' ORDER BY id")] == [
        f"app:roster:duties_rostered:{s}" for s, _ in kept
    ]


def test_a_successful_action_commits_the_delete(client, conn, db_path, roster):
    resp = act(client, "clear")

    assert resp.get_json()["ok"] is True
    # Read from a connection of its own: committed, not merely visible.
    fresh = sqlite3.connect(db_path)
    try:
        assert fresh.execute("SELECT source, COUNT(*) FROM sessions GROUP BY source").fetchall() == [
            ("garmin", 1),
        ]
    finally:
        fresh.close()


# ---------- render() can't reach it ----------


def test_render_has_no_delete_sessions(client, conn, apps_dir, roster):
    assert not hasattr(host.Ctx(conn), "delete_sessions")
    assert not hasattr(host.Ctx(conn, app=an_app("roster")), "delete_sessions")
    make_app(apps_dir, "sneak", """
        def render(ctx, cfg):
            ctx.delete_sessions("duties_rostered", "2000-01-01T00:00:00Z", "2100-01-01T00:00:00Z")
            return ctx.empty("cleared")
    """)

    spec = client.post("/api/app/sneak/render", json={}).get_json()

    assert spec["type"] == "error"
    assert "delete_sessions" in spec["message"]
    assert rows(conn) == roster


# ---------- app level 4 ----------

LEVEL_5 = "needs OpenFit app level 5; this OpenFit is level 4"


def test_this_openfit_is_app_level_four(client, apps_dir, capsys):
    assert apps.APP_LEVEL == 4
    assert apps.level_problem(4) is None
    assert apps.level_problem(5) == LEVEL_5
    make_app(apps_dir, "needs", ROSTER, extra="requires: 4\n")
    make_app(apps_dir, "newer", ROSTER, extra="requires: 5\n")

    # requires: 4 loads; requires: 5 is skipped ...
    assert [(a.id, a.requires) for a in apps.scan_apps(apps_dir)] == [("needs", 4)]
    assert f"[apps] skipping {apps_dir / 'newer'}: {LEVEL_5}" in capsys.readouterr().out
    # ... and refused at install.
    resp = client.post("/api/apps", json={"files": {"app.yaml": f"id: newest\nrequires: 5\n{SIZE}"}})
    assert (resp.status_code, resp.get_json()) == (400, {"ok": False, "error": LEVEL_5})
    resp = client.post("/api/apps", json={"files": {"app.yaml": f"id: fine\nrequires: 4\n{SIZE}"}})
    assert resp.status_code == 201
