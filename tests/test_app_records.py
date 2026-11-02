"""
App records: a private JSON store per app (C2).

An app keeps what isn't a reading or a session - a trip report, a log
entry, a saved list - as records: one JSON value under a key, in a
collection it names, in the app_records table (migration 011).

- Reads on every ctx (render() and handle()): ctx.record(collection,
  key) -> the value or None; ctx.records(collection, prefix=None,
  limit=None) -> [{key, value, updated_at}] by key.
- Writes on the WriteCtx alone (handle()): ctx.put_record() upserts,
  keeping created_at; ctx.delete_record() -> whether one went. Inside
  the action's one transaction: nothing committed by either.
- Only the calling app's own: every query names the ctx's app, and no
  argument names another.
- The rules: a collection [a-z][a-z0-9_-]{0,39}, a name starting with _
  OpenFit's own; a key 1-200 printable characters; a value plain JSON.
  ValueError, the field named, for anything else.
- The limits: 256 KB a record, 50 MB an app, counting a write's net
  change - a shrink at the total still goes in.
- Your data: GET /api/app-sources lists each app's records {count,
  bytes}, an uninstalled app's too; Delete data (DELETE
  /api/app-sources/<id>) takes them, the per-metric delete doesn't, and
  an app uninstalled and reinstalled finds them.
- Migration 011 on a database as f4ba68e left it; app level 5.

Every test gets its own temp database and apps folder.
"""

import json
import math
import os
import sqlite3
import textwrap
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import apps
import host
import main
import shelf
from migrations import run_migrations

SIZE = "sizes: [{w: 1, h: 1}]\n"
NO_RECORDS = {"count": 0, "bytes": 0}
NO_FILES = {"count": 0, "bytes": 0}
KB = 1024
MB = 1024 * KB


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


def write_ctx(conn, app_id="notes"):
    return host.WriteCtx(conn, an_app(app_id))


def read_ctx(conn, app_id="notes"):
    return host.Ctx(conn, app=an_app(app_id))


def stored(conn, app=None):
    """Every app_records row - or `app`'s - as (app, collection, key, value,
    size, created_at, updated_at), in key order."""
    sql = "SELECT app, collection, key, value, size, created_at, updated_at FROM app_records"
    args = ()
    if app is not None:
        sql, args = sql + " WHERE app = ?", (app,)
    return [tuple(r) for r in conn.execute(sql + " ORDER BY app, collection, key", args)]


def fresh_rows(db_path):
    """app_records as a connection of its own sees them: committed only."""
    other = sqlite3.connect(db_path)
    try:
        return [tuple(r) for r in other.execute(
            "SELECT app, collection, key, value FROM app_records ORDER BY app, collection, key"
        )]
    finally:
        other.close()


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def text_of(nbytes):
    """A str whose JSON is exactly `nbytes` bytes: the quotes and ASCII."""
    return "x" * (nbytes - 2)


# ---------- 1. the round trip ----------

VALUES = [
    {"port": "LHR", "rested": True, "legs": [1, 2.5, None], "nested": {"a": {"b": []}}},
    [1, "two", 3.0, False, None, {"k": "v"}],
    "a string, with ünïcödé and 日本語 and an emoji 🛫",
    "",
    0,
    -12,
    2 ** 70,
    3.25,
    1e-300,
    True,
    False,
    None,
    {},
    [],
]


@pytest.mark.parametrize("value", VALUES, ids=[f"{type(v).__name__}-{i}" for i, v in enumerate(VALUES)])
def test_each_json_type_reads_back_as_it_was_put(conn, value):
    ctx = write_ctx(conn)

    ctx.put_record("trips", "k", value)

    got = ctx.record("trips", "k")
    assert got == value and type(got) is type(value)
    # The same from a render's ctx, and the stored text the brief's
    # serialisation, its size that text's UTF-8 bytes.
    assert read_ctx(conn).record("trips", "k") == value
    [(_, _, _, text, size, _, _)] = stored(conn)
    assert text == json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    assert size == len(text.encode("utf-8"))


def test_a_tuple_reads_back_a_list_and_number_keys_as_text(conn):
    ctx = write_ctx(conn)
    ctx.put_record("trips", "k", {"legs": (1, 2), 7: "seven"})

    assert ctx.record("trips", "k") == {"legs": [1, 2], "7": "seven"}


def test_a_value_read_back_is_a_fresh_copy(conn):
    ctx = write_ctx(conn)
    ctx.put_record("trips", "k", {"legs": [1]})

    ctx.record("trips", "k")["legs"].append(2)

    assert ctx.record("trips", "k") == {"legs": [1]}
    ctx.records("trips")[0]["value"]["legs"].append(3)
    assert ctx.records("trips")[0]["value"] == {"legs": [1]}


def test_an_update_keeps_created_at_and_moves_updated_at(conn, pin_clock):
    ctx = write_ctx(conn)
    pin_clock(utc(2026, 11, 2, 8, 30, 15, 999999))
    ctx.put_record("trips", "k", {"v": 1})
    pin_clock(utc(2026, 11, 3, 9, 0, 0))
    ctx.put_record("trips", "k", {"v": 2, "more": "text"})

    [(app, collection, key, value, size, created, updated)] = stored(conn)
    assert (app, collection, key) == ("notes", "trips", "k")
    assert value == '{"v":2,"more":"text"}' and size == len(value)
    # iso_utc() text, to the second, UTC.
    assert (created, updated) == ("2026-11-02T08:30:15Z", "2026-11-03T09:00:00Z")
    assert ctx.records("trips") == [{"key": "k", "value": {"v": 2, "more": "text"}, "updated_at": updated}]


def test_absent_reads_none_and_a_stored_none_too_which_records_tells_apart(conn):
    ctx = write_ctx(conn)
    assert ctx.record("trips", "k") is None
    assert ctx.records("trips") == []

    ctx.put_record("trips", "k", None)

    assert ctx.record("trips", "k") is None
    assert [(r["key"], r["value"]) for r in ctx.records("trips", prefix="k")] == [("k", None)]
    assert ctx.record("trips", "other") is None
    assert ctx.records("trips", prefix="other") == []


def test_a_key_lives_in_its_collection(conn):
    ctx = write_ctx(conn)
    ctx.put_record("trips", "k", 1)
    ctx.put_record("reports", "k", 2)

    assert (ctx.record("trips", "k"), ctx.record("reports", "k")) == (1, 2)
    assert ctx.delete_record("trips", "k") is True
    assert (ctx.record("trips", "k"), ctx.record("reports", "k")) == (None, 2)


def test_delete_says_whether_a_record_went(conn):
    ctx = write_ctx(conn)
    ctx.put_record("trips", "k", {"v": 1})

    assert ctx.delete_record("trips", "k") is True
    assert ctx.delete_record("trips", "k") is False
    assert ctx.delete_record("trips", "never") is False
    assert stored(conn) == []


def test_records_note_no_day_for_the_route_to_re_derive(conn):
    ctx = write_ctx(conn)
    ctx.put_record("trips", "k", 1)
    ctx.delete_record("trips", "k")

    assert ctx.written == []


# ---------- 2. listing ----------


def test_records_are_listed_by_key(conn, pin_clock):
    ctx = write_ctx(conn)
    pin_clock(utc(2026, 11, 2, 7))
    for key in ["b", "a", "2026-11-02-x", "B", "aa", "2026-01-30-y", "é"]:
        ctx.put_record("trips", key, key.upper())

    listed = ctx.records("trips")

    # As text sorts: code point order, so a date first sorts in time.
    assert [r["key"] for r in listed] == ["2026-01-30-y", "2026-11-02-x", "B", "a", "aa", "b", "é"]
    assert listed[0] == {"key": "2026-01-30-y", "value": "2026-01-30-Y", "updated_at": "2026-11-02T07:00:00Z"}
    assert [r["key"] for r in read_ctx(conn).records("trips")] == [r["key"] for r in listed]


def test_a_prefix_matches_literally(conn):
    ctx = write_ctx(conn)
    keys = ["50%", "50%-off", "500", "5", "a_b", "a_b-2", "axb", "a\\b", "a\\bc", "aXb",
            "trip", "trip-1", "TRIP-2", "Trip-3", "tri"]
    for key in keys:
        ctx.put_record("c", key, 1)

    by = lambda prefix: [r["key"] for r in ctx.records("c", prefix=prefix)]

    # % and _ are themselves, never LIKE's wildcards, and \ is itself too.
    assert by("50%") == ["50%", "50%-off"]
    assert by("a_b") == ["a_b", "a_b-2"]
    assert by("a\\b") == ["a\\b", "a\\bc"]
    assert by("%") == []
    assert by("_") == []
    # Case counts, though SQLite's LIKE alone wouldn't.
    assert by("trip") == ["trip", "trip-1"]
    assert by("TRIP") == ["TRIP-2"]
    # A whole key is its own prefix; a prefix no key has, none; an empty
    # one, all.
    assert by("tri") == ["tri", "trip", "trip-1"]
    assert by("trip-1") == ["trip-1"]
    assert by("zzz") == []
    assert by("") == sorted(keys) == by(None)


def test_limit_bounds_the_listing_and_its_default_is_a_thousand(conn):
    ctx = write_ctx(conn)
    for i in range(1001):
        ctx.put_record("c", f"k{i:04}", i)

    assert [r["key"] for r in ctx.records("c", limit=1)] == ["k0000"]
    assert [r["value"] for r in ctx.records("c", limit=3)] == [0, 1, 2]
    assert len(ctx.records("c", limit=1000)) == 1000
    # No limit: all of them, up to 1000 - the rest a prefix away.
    assert len(ctx.records("c")) == 1000
    assert [r["key"] for r in ctx.records("c", prefix="k1")] == ["k1000"]
    assert [r["key"] for r in ctx.records("c", prefix="k000", limit=2)] == ["k0000", "k0001"]


@pytest.mark.parametrize("limit", [0, 1001, -1, True, False, 1.0, 5.5, "5", [5]])
def test_a_limit_out_of_range_is_refused(conn, limit):
    ctx = write_ctx(conn)
    ctx.put_record("c", "k", 1)

    with pytest.raises(ValueError, match=r"^limit must be a whole number from 1 to 1000"):
        ctx.records("c", limit=limit)


@pytest.mark.parametrize("prefix", [5, b"k", ["k"], "k" * 201])
def test_a_prefix_that_isnt_text_of_200_is_refused(conn, prefix):
    with pytest.raises(ValueError, match=r"^prefix must be text of at most 200 characters"):
        write_ctx(conn).records("c", prefix=prefix)


# ---------- 3. isolation ----------


def test_two_apps_with_the_same_collection_and_key_each_see_only_their_own(conn):
    mine, theirs = write_ctx(conn, "notes"), write_ctx(conn, "openfitavi.frms")
    mine.put_record("trips", "k", {"who": "notes"})
    theirs.put_record("trips", "k", {"who": "frms"})
    theirs.put_record("trips", "only-theirs", 1)
    mine.put_record("trips", "only-mine", 2)

    # Each read, each app's own - render()'s ctx and handle()'s alike.
    for ctx_for in (write_ctx, read_ctx):
        assert ctx_for(conn, "notes").record("trips", "k") == {"who": "notes"}
        assert ctx_for(conn, "openfitavi.frms").record("trips", "k") == {"who": "frms"}
        assert ctx_for(conn, "notes").record("trips", "only-theirs") is None
        assert ctx_for(conn, "openfitavi.frms").record("trips", "only-mine") is None
        assert [(r["key"], r["value"]) for r in ctx_for(conn, "notes").records("trips")] == [
            ("k", {"who": "notes"}), ("only-mine", 2)]
        assert [(r["key"], r["value"]) for r in ctx_for(conn, "openfitavi.frms").records("trips")] == [
            ("k", {"who": "frms"}), ("only-theirs", 1)]
        assert [r["key"] for r in ctx_for(conn, "notes").records("trips", prefix="only")] == ["only-mine"]
    # A third app, none.
    assert read_ctx(conn, "frms").records("trips") == []
    assert read_ctx(conn, "frms").record("trips", "k") is None


def test_an_apps_writes_never_reach_anothers(conn):
    mine, theirs = write_ctx(conn, "notes"), write_ctx(conn, "frms")
    theirs.put_record("trips", "k", "theirs")

    mine.put_record("trips", "k", "mine")
    assert mine.delete_record("trips", "k") is True
    assert mine.delete_record("trips", "k") is False

    assert stored(conn) == [("frms", "trips", "k", '"theirs"', 8, *stored(conn)[0][5:])]
    assert theirs.record("trips", "k") == "theirs"


def test_no_method_takes_an_app():
    """The ctx's own app, always: nothing to name another with."""
    import inspect

    for name, params in (("record", ["collection", "key"]),
                         ("records", ["collection", "prefix", "limit"]),
                         ("put_record", ["collection", "key", "value"]),
                         ("delete_record", ["collection", "key"])):
        method = getattr(host.WriteCtx, name)
        assert list(inspect.signature(method).parameters)[1:] == params, name


def test_a_ctx_for_no_app_has_no_records(conn):
    ctx = host.Ctx(conn)
    for call in (lambda: ctx.record("trips", "k"), lambda: ctx.records("trips")):
        with pytest.raises(ValueError, match="records belong to an app"):
            call()


# ---------- 4. validation ----------

GOOD_COLLECTIONS = ["a", "trips", "trip_reports", "trip-reports", "a1", "z" + "9" * 39, "a_-_-"]
BAD_COLLECTIONS = [
    ("", "must be 1 to 40 characters"),
    ("Trips", "must be 1 to 40 characters"),
    ("TRIPS", "must be 1 to 40 characters"),
    ("tRips", "must be 1 to 40 characters"),
    ("a" * 41, "must be 1 to 40 characters"),
    ("1trips", "must be 1 to 40 characters"),
    ("-trips", "must be 1 to 40 characters"),
    ("trips.old", "must be 1 to 40 characters"),
    ("trips/old", "must be 1 to 40 characters"),
    ("trip reports", "must be 1 to 40 characters"),
    ("trips\n", "must be 1 to 40 characters"),
    ("tripé", "must be 1 to 40 characters"),
    (None, "must be text"),
    (5, "must be text"),
    (b"trips", "must be text"),
]
RESERVED = ["_settings", "_", "_x", "__trips", "_Trips"]


def every_call(ctx):
    """Each of the four, on a collection and key given, as a callable."""
    return {
        "record": lambda c, k: ctx.record(c, k),
        "records": lambda c, k: ctx.records(c),
        "put_record": lambda c, k: ctx.put_record(c, k, {"v": 1}),
        "delete_record": lambda c, k: ctx.delete_record(c, k),
    }


@pytest.mark.parametrize("collection", GOOD_COLLECTIONS)
def test_a_good_collection_name_is_taken(conn, collection):
    ctx = write_ctx(conn)
    ctx.put_record(collection, "k", 1)
    assert ctx.record(collection, "k") == 1


@pytest.mark.parametrize("collection, why", BAD_COLLECTIONS)
def test_a_bad_collection_name_is_refused_by_every_method(conn, collection, why):
    ctx = write_ctx(conn)
    for name, call in every_call(ctx).items():
        with pytest.raises(ValueError, match=r"^collection " + why) as refused:
            call(collection, "k")
        assert repr(collection) in str(refused.value), name
    assert stored(conn) == []


@pytest.mark.parametrize("collection", RESERVED)
def test_a_collection_starting_with_underscore_is_openfits_own(conn, collection):
    # OpenFit's own - C4's _settings - stored as any app's record is.
    conn.execute(
        "INSERT INTO app_records VALUES ('notes', ?, 'k', '{\"theme\":\"dark\"}', 16, 't', 't')",
        (collection,),
    )
    ctx = write_ctx(conn)

    for name, call in every_call(ctx).items():
        with pytest.raises(ValueError, match=r"^collection .* is reserved: a name starting with '_' is OpenFit's own"):
            call(collection, "k")
    # Neither read nor replaced nor removed.
    assert [r[1:4] for r in stored(conn)] == [(collection, "k", '{"theme":"dark"}')]


GOOD_KEYS = ["k", "x" * 200, "2026-11-02 HKG→LHR", "a key, with spaces", "%_\\", "é", "🛫"]
BAD_KEYS = [
    ("", "must be 1 to 200 characters: it has 0"),
    ("x" * 201, "must be 1 to 200 characters: it has 201"),
    ("a\nb", "must be printable"),
    ("a\tb", "must be printable"),
    ("a\rb", "must be printable"),
    ("\x00", "must be printable"),
    ("a\x1fb", "must be printable"),
    ("a\x7fb", "must be printable"),
    ("a\x85b", "must be printable"),
    ("a\u2028b", "must be printable"),
    ("a\u200bb", "must be printable"),
    ("\ud800", "must be printable"),
    (None, "must be text"),
    (7, "must be text"),
    (b"k", "must be text"),
    (("k",), "must be text"),
]


@pytest.mark.parametrize("key", GOOD_KEYS)
def test_a_good_key_is_taken(conn, key):
    ctx = write_ctx(conn)
    ctx.put_record("c", key, 1)
    assert ctx.record("c", key) == 1
    assert [r["key"] for r in ctx.records("c")] == [key]


@pytest.mark.parametrize("key, why", BAD_KEYS)
def test_a_bad_key_is_refused_by_every_method_but_the_listing(conn, key, why):
    ctx = write_ctx(conn)
    for name, call in every_call(ctx).items():
        if name == "records":
            continue  # takes no key
        with pytest.raises(ValueError, match=r"^key " + why):
            call("c", key)
    assert stored(conn) == []


class Thing:
    pass


def nested(depth):
    value = []
    for _ in range(depth):
        value = [value]
    return value


def holds_itself():
    value = {"a": []}
    value["a"].append(value)
    return value


BAD_VALUES = [
    ({1, 2}, "set"),
    (frozenset(), "frozenset"),
    (Thing(), "Thing"),
    (math.nan, "nan"),
    (math.inf, "inf"),
    (-math.inf, "-inf"),
    ({"a": [1, math.nan]}, "nan, inside"),
    ({("a", "b"): 1}, "a tuple key"),
    (b"bytes", "bytes"),
    (datetime(2026, 11, 2), "a datetime"),
    ("lone \ud800 surrogate", "a lone surrogate"),
    (holds_itself(), "a value that holds itself"),
    (nested(100_000), "nesting too deep to write"),
]


@pytest.mark.parametrize("value", [v for v, _ in BAD_VALUES], ids=[why for _, why in BAD_VALUES])
def test_a_value_that_isnt_plain_json_is_refused(conn, value):
    ctx = write_ctx(conn)
    ctx.put_record("c", "k", "before")

    with pytest.raises(ValueError, match=r"^value must be plain JSON - a dict, list, text, number, bool or None: "):
        ctx.put_record("c", "k", value)

    assert ctx.record("c", "k") == "before"
    assert len(stored(conn)) == 1


# ---------- 5. limits ----------


def test_a_record_is_at_most_256_kb_as_json(conn):
    ctx = write_ctx(conn)
    ctx.put_record("c", "full", text_of(256 * KB))
    assert stored(conn)[0][4] == 256 * KB == host.MAX_RECORD_BYTES

    with pytest.raises(ValueError, match=r"^value is 262,145 bytes as JSON; a record holds at most 256 KB \(262,144 bytes\)$"):
        ctx.put_record("c", "over", text_of(256 * KB + 1))
    # Nor in place of one already there.
    with pytest.raises(ValueError, match=r"^value is 262,145 bytes"):
        ctx.put_record("c", "full", text_of(256 * KB + 1))

    assert [(r[2], r[4]) for r in stored(conn)] == [("full", 256 * KB)]


def test_a_records_size_is_its_utf8_bytes_not_its_characters(conn):
    ctx = write_ctx(conn)
    # é is two bytes: 131,071 of them and the quotes is exactly 256 KB.
    ctx.put_record("c", "k", "é" * 131_071)
    assert stored(conn)[0][4] == 256 * KB
    with pytest.raises(ValueError, match=r"^value is 262,146 bytes"):
        ctx.put_record("c", "k", "é" * 131_072)
    # A dict's keys, separators and nesting count too: it's the JSON text.
    ctx.put_record("c", "d", {"ab": [1, 2]})
    assert dict((r[2], r[4]) for r in stored(conn))["d"] == len('{"ab":[1,2]}')


def fill_to_the_total(ctx):
    """200 records of 256 KB each: exactly 50 MB, the most an app keeps."""
    for i in range(200):
        ctx.put_record("big", f"k{i:03}", text_of(256 * KB))


def total(conn, app):
    return conn.execute("SELECT COALESCE(SUM(size), 0) FROM app_records WHERE app = ?", (app,)).fetchone()[0]


def test_an_app_keeps_at_most_50_mb_and_a_shrink_at_the_total_still_goes_in(conn):
    ctx = write_ctx(conn)
    fill_to_the_total(ctx)
    assert total(conn, "notes") == 50 * MB == host.MAX_APP_RECORD_BYTES

    # At the total, anything that adds a byte is refused, writing nothing:
    # a new record, however small ...
    with pytest.raises(ValueError, match=r"^value would take the app's records to 52,428,801 bytes; an app keeps "
                                         r"at most 50 MB of records \(52,428,800 bytes\) - delete some first$"):
        ctx.put_record("small", "k", 7)
    assert ctx.record("small", "k") is None
    # ... or a bigger value for one there: shrunk by 10 bytes, a new
    # 10-byte record fills the room again, and growing the first by one
    # byte is over.
    ctx.put_record("big", "k000", text_of(256 * KB - 10))
    ctx.put_record("small", "k", text_of(10))
    assert total(conn, "notes") == 50 * MB
    with pytest.raises(ValueError, match=r"^value would take the app's records to 52,428,801 bytes"):
        ctx.put_record("big", "k000", text_of(256 * KB - 9))
    assert len(ctx.record("big", "k000")) == 256 * KB - 12
    ctx.delete_record("small", "k")
    assert total(conn, "notes") == 50 * MB - 10

    # A replacement the same size, or smaller, still goes in: the net
    # change is what counts.
    ctx.put_record("big", "k001", "y" * (256 * KB - 2))
    assert ctx.record("big", "k001")[:1] == "y"
    ctx.put_record("big", "k002", {"now": "small"})
    assert ctx.record("big", "k002") == {"now": "small"}
    assert total(conn, "notes") == 50 * MB - 10 - (256 * KB - len('{"now":"small"}'))
    # ... which leaves room for exactly that much again.
    room = 50 * MB - total(conn, "notes")
    ctx.put_record("small", "k", text_of(room))
    assert total(conn, "notes") == 50 * MB
    with pytest.raises(ValueError, match="an app keeps at most 50 MB"):
        ctx.put_record("small", "k2", 0)


def test_the_total_counts_only_that_apps_records(conn):
    fill_to_the_total(write_ctx(conn, "notes"))

    # Another app writes as though the first had none ...
    other = write_ctx(conn, "frms")
    other.put_record("big", "k", text_of(256 * KB))
    assert total(conn, "frms") == 256 * KB
    # ... and the first is still full.
    with pytest.raises(ValueError, match="an app keeps at most 50 MB"):
        write_ctx(conn, "notes").put_record("c", "k", 1)


def test_an_app_over_its_total_may_still_shrink(conn):
    """Should an app ever hold more than the limit (none can now), a
    write that shrinks it still goes in; one that grows it doesn't."""
    conn.execute(
        "INSERT INTO app_records VALUES ('notes', 'c', 'huge', '\"x\"', ?, 't', 't')", (60 * MB,),
    )
    ctx = write_ctx(conn)

    ctx.put_record("c", "huge", "")       # 3 bytes, said to be 60 MB, now 2
    assert total(conn, "notes") == 2
    conn.execute("UPDATE app_records SET size = ? WHERE key = 'huge'", (60 * MB,))
    with pytest.raises(ValueError, match="an app keeps at most 50 MB"):
        ctx.put_record("c", "new", 1)


# ---------- 6. the transaction, through the action route ----------

NOTES = """
import json

def render(ctx, cfg):
    return ctx.empty(json.dumps({
        "one": ctx.record("trips", "k"),
        "all": [[r["key"], r["value"]] for r in ctx.records("trips")],
    }))

def handle(ctx, action, payload):
    if action == "put":
        ctx.put_record("trips", payload["key"], payload["value"])
    elif action == "put_then_fail":
        ctx.put_record("trips", payload["key"], payload["value"])
        ctx.put_record("trips", "another", 2)
        raise ValueError("refused after the put")
    elif action == "delete":
        ctx.delete_record("trips", payload["key"])
    elif action == "delete_then_fail":
        assert ctx.delete_record("trips", payload["key"]) is True
        raise ValueError("refused after the delete")
    elif action == "put_then_crash":
        ctx.put_record("trips", payload["key"], payload["value"])
        1 / 0
    elif action == "too_big":
        ctx.put_record("trips", payload["key"], "x" * (256 * 1024))
    elif action == "caught":
        try:
            ctx.put_record("trips", "big", "x" * (256 * 1024))
        except ValueError:
            pass
        ctx.put_record("trips", "small", 1)
    elif action == "reserved":
        ctx.put_record("_settings", "k", 1)
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
def notes_app(apps_dir):
    make_app(apps_dir, "notes", NOTES, extra="requires: 5\n")
    return apps_dir / "notes"


def act(client, action, app_id="notes", **payload):
    return client.post(f"/api/app/{app_id}/action", json={"action": action, "payload": payload})


def tile(spec):
    assert spec["type"] == "empty", spec
    return json.loads(spec["message"])


def test_a_put_then_a_raise_leaves_nothing(client, db_path, notes_app):
    resp = act(client, "put_then_fail", key="k", value={"v": 1})

    assert (resp.status_code, resp.get_json()) == (400, {"ok": False, "error": "refused after the put"})
    assert fresh_rows(db_path) == []
    # A crash rolls back as a refusal does.
    resp = act(client, "put_then_crash", key="k", value={"v": 1})
    assert resp.status_code == 400 and resp.get_json()["error"].startswith("ZeroDivisionError")
    assert fresh_rows(db_path) == []


def test_a_successful_put_is_committed_and_the_tile_reads_it(client, db_path, notes_app):
    resp = act(client, "put", key="k", value={"v": 1})

    assert resp.status_code == 200
    assert tile(resp.get_json()["spec"]) == {"one": {"v": 1}, "all": [["k", {"v": 1}]]}
    # Read from a connection of its own: committed, not merely visible.
    assert fresh_rows(db_path) == [("notes", "trips", "k", '{"v":1}')]
    # And the render route reads it back, as the tile is drawn afresh.
    spec = client.post("/api/app/notes/render", json={}).get_json()
    assert tile(spec) == {"one": {"v": 1}, "all": [["k", {"v": 1}]]}


def test_a_delete_then_a_raise_rolls_back_too(client, db_path, notes_app):
    assert act(client, "put", key="k", value="kept").status_code == 200

    resp = act(client, "delete_then_fail", key="k")

    assert (resp.status_code, resp.get_json()) == (400, {"ok": False, "error": "refused after the delete"})
    assert fresh_rows(db_path) == [("notes", "trips", "k", '"kept"')]
    # Without the raise, it goes.
    assert act(client, "delete", key="k").status_code == 200
    assert fresh_rows(db_path) == []


def test_a_refusal_is_said_as_the_ctx_put_it(client, db_path, notes_app):
    resp = act(client, "too_big", key="k")
    assert resp.status_code == 400
    assert resp.get_json()["error"] == (
        "value is 262,146 bytes as JSON; a record holds at most 256 KB (262,144 bytes)")
    resp = act(client, "reserved")
    assert resp.status_code == 400 and "is reserved" in resp.get_json()["error"]
    assert fresh_rows(db_path) == []


def test_a_refusal_the_app_catches_writes_nothing_of_its_own(client, db_path, notes_app):
    resp = act(client, "caught")

    assert resp.status_code == 200
    assert fresh_rows(db_path) == [("notes", "trips", "small", "1")]


# ---------- 7. read-only in render() ----------


def test_the_render_ctx_reads_records_and_cant_write_them(conn):
    for ctx in (host.Ctx(conn), host.Ctx(conn, app=an_app("notes"))):
        assert callable(ctx.record) and callable(ctx.records)
        assert not hasattr(ctx, "put_record")
        assert not hasattr(ctx, "delete_record")
    ctx = write_ctx(conn)
    assert all(callable(getattr(ctx, m)) for m in ("record", "records", "put_record", "delete_record"))


def test_a_render_that_tries_to_write_a_record_is_an_error_and_writes_nothing(client, conn, apps_dir):
    make_app(apps_dir, "sneak", """
        def render(ctx, cfg):
            ctx.put_record("trips", "k", 1)
            return ctx.empty("wrote")
    """)
    make_app(apps_dir, "sneak2", """
        def render(ctx, cfg):
            ctx.delete_record("trips", "k")
            return ctx.empty("deleted")
    """)
    write_ctx(conn, "sneak2").put_record("trips", "k", "kept")
    conn.commit()

    for app_id, method in (("sneak", "put_record"), ("sneak2", "delete_record")):
        spec = client.post(f"/api/app/{app_id}/render", json={}).get_json()
        assert spec["type"] == "error" and method in spec["message"]
    assert [r[:4] for r in stored(conn)] == [("sneak2", "trips", "k", '"kept"')]


# ---------- 8. Your data ----------


def listing(client, app_id=None):
    got = client.get("/api/app-sources").get_json()
    if app_id is None:
        return got
    [entry] = [e for e in got if e["id"] == app_id]
    return entry


def test_the_listing_counts_each_apps_records_and_their_bytes(client, conn, apps_dir):
    make_app(apps_dir, "notes", NOTES)
    write_ctx(conn, "notes").put_record("trips", "a", {"v": 1})     # 7 bytes
    write_ctx(conn, "notes").put_record("other", "b", "two")        # 5 bytes
    write_ctx(conn, "openfitavi.frms").put_record("reports", "t", [1, 2, 3])  # 7 bytes
    conn.execute(
        "INSERT INTO metrics (date, source, metric, value, unit, synced_at) "
        "VALUES ('2026-10-02', 'app:steps', 'steps', 5, 'count', 'x')"
    )
    conn.commit()

    assert client.get("/api/app-sources").get_json() == [
        # Installed: listed live. Records alone list an app, as rows do.
        {"id": "notes", "source": "app:notes", "orphaned": False, "metrics": [],
         "readings": 0, "sessions": 0, "samples": 0, "records": {"count": 2, "bytes": 12}, "files": NO_FILES},
        # A library app's id, its records alone, its app not here: orphaned.
        {"id": "openfitavi.frms", "source": "app:openfitavi.frms", "orphaned": True, "metrics": [],
         "readings": 0, "sessions": 0, "samples": 0, "records": {"count": 1, "bytes": 7}, "files": NO_FILES},
        # Rows and no records: {0, 0}.
        {"id": "steps", "source": "app:steps", "orphaned": True, "metrics": ["steps"],
         "readings": 1, "sessions": 0, "samples": 0, "records": NO_RECORDS, "files": NO_FILES},
    ]


def test_an_uninstalled_apps_records_stay_listed_and_reinstalling_finds_them(client, conn, apps_dir):
    files = {"app.yaml": f"id: notes\nname: Notes\nrequires: 5\n{SIZE}", "app.py": textwrap.dedent(NOTES)}
    assert client.post("/api/apps", json={"files": files}).status_code == 201
    assert act(client, "put", key="k", value={"v": 1}).status_code == 200
    assert listing(client, "notes")["orphaned"] is False

    assert client.delete("/api/apps/notes").status_code == 200

    # Uninstalled: the records stay, listed, orphaned.
    assert stored(conn, "notes")[0][:4] == ("notes", "trips", "k", '{"v":1}')
    assert listing(client, "notes") == {
        "id": "notes", "source": "app:notes", "orphaned": True, "metrics": [],
        "readings": 0, "sessions": 0, "samples": 0, "records": {"count": 1, "bytes": 7}, "files": NO_FILES}

    assert client.post("/api/apps", json={"files": files}).status_code == 201

    # Back under the same id: its own again.
    spec = client.post("/api/app/notes/render", json={}).get_json()
    assert tile(spec) == {"one": {"v": 1}, "all": [["k", {"v": 1}]]}
    assert listing(client, "notes")["orphaned"] is False


def test_delete_data_removes_an_uninstalled_apps_records(client, conn, apps_dir):
    write_ctx(conn, "notes").put_record("trips", "a", 1)
    write_ctx(conn, "notes").put_record("trips", "b", 2)
    write_ctx(conn, "frms").put_record("trips", "a", "theirs")
    conn.execute(
        "INSERT INTO metrics (date, source, metric, value, unit, synced_at) "
        "VALUES ('2026-10-02', 'app:notes', 'steps', 5, 'count', 'x')"
    )
    conn.commit()

    resp = client.delete("/api/app-sources/notes")

    assert resp.get_json() == {"ok": True, "removed": {"metrics": 1, "sessions": 0, "samples": 0, "records": 2, "files": 0}}
    # Another app's - with the same collection and key - stays.
    assert [r[:4] for r in stored(conn)] == [("frms", "trips", "a", '"theirs"')]
    assert [e["id"] for e in listing(client)] == ["frms"]


def test_delete_data_takes_an_app_with_only_records(client, conn, apps_dir):
    write_ctx(conn, "openfitavi.frms").put_record("reports", "t", {"v": 1})
    conn.commit()

    resp = client.delete("/api/app-sources/openfitavi.frms")

    assert resp.get_json() == {"ok": True, "removed": {"metrics": 0, "sessions": 0, "samples": 0, "records": 1, "files": 0}}
    assert stored(conn) == []
    assert listing(client) == []
    # Nothing left: a second go is the 404 any source with nothing gets.
    assert client.delete("/api/app-sources/openfitavi.frms").status_code == 404


def test_an_installed_apps_records_are_never_removed(client, conn, notes_app):
    write_ctx(conn, "notes").put_record("trips", "a", 1)
    conn.commit()

    resp = client.delete("/api/app-sources/notes")

    assert resp.status_code == 400 and "is present" in resp.get_json()["error"]
    assert len(stored(conn)) == 1


def test_the_per_metric_delete_leaves_the_records(client, conn, apps_dir):
    write_ctx(conn, "notes").put_record("trips", "a", 1)
    conn.execute(
        "INSERT INTO metrics (date, source, metric, value, unit, synced_at) "
        "VALUES ('2026-10-02', 'app:notes', 'steps', 5, 'count', 'x')"
    )
    conn.commit()

    resp = client.delete("/api/app-sources/notes/steps")

    assert resp.get_json() == {"ok": True, "removed": {"metrics": 1, "sessions": 0, "samples": 0, "records": 0, "files": 0}}
    assert [r[:4] for r in stored(conn)] == [("notes", "trips", "a", "1")]
    assert listing(client, "notes")["records"] == {"count": 1, "bytes": 1}
    # A metric the app has none of is still a 404: records aren't a metric.
    assert client.delete("/api/app-sources/notes/steps").status_code == 404


def test_a_devices_wipe_never_reaches_an_app_of_the_same_id(conn):
    """Records are keyed by the app's id, not its source: a device whose
    id an app shares - garmin - wipes its own rows and none of the app's
    records; the app's source, app:garmin, takes them."""
    write_ctx(conn, "garmin").put_record("trips", "a", 1)
    conn.commit()

    assert main.purge_source(conn, "garmin", keep_roles=True)["records"] == 0
    assert len(stored(conn)) == 1

    assert main.purge_source(conn, "app:garmin")["records"] == 1
    assert stored(conn) == []


# ---------- 9. migration 011 ----------

RECORD_COLUMNS = ["app", "collection", "key", "value", "size", "created_at", "updated_at"]


def snapshot(conn):
    """Every table but schema_migrations: its schema and rows."""
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name != 'schema_migrations' ORDER BY name")]
    return {
        t: (conn.execute("SELECT sql FROM sqlite_master WHERE name = ?", (t,)).fetchone()[0],
            sorted(conn.execute(f"SELECT * FROM {t}").fetchall(), key=repr))
        for t in tables
    }


def test_011_applies_on_a_database_as_f4ba68e_left_it(db_at_version, migrations_upto):
    """f4ba68e's newest migration is 010: a database there, holding data,
    gets 011 alone - a table and its index - and nothing else changes."""
    conn = sqlite3.connect(db_at_version(10))
    conn.execute(
        "INSERT INTO metrics (date, source, metric, value, unit, synced_at) "
        "VALUES ('2026-10-02', 'app:notes', 'steps', 5, 'count', 'x')"
    )
    conn.execute(
        'INSERT INTO sessions (id, source, kind, "start", "end", summary_json, synced_at) '
        "VALUES ('app:frms:duties_rostered:s', 'app:frms', 'duties_rostered', 's', 'e', '{}', 'x')"
    )
    conn.execute("INSERT INTO settings VALUES ('home_layout', '{}')")
    conn.commit()
    assert conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 10
    before = snapshot(conn)
    assert "app_records" not in before

    # Up to 011, and no further: 012 is C3's (tests/test_app_files.py).
    assert run_migrations(conn, migrations_dir=migrations_upto(11)) == [11]

    columns = conn.execute("PRAGMA table_info(app_records)").fetchall()
    assert [c[1] for c in columns] == RECORD_COLUMNS
    assert all(c[3] for c in columns)  # every column NOT NULL
    assert [c[1] for c in sorted(columns, key=lambda c: c[5]) if c[5]] == ["app", "collection", "key"]
    assert [r[2] for r in conn.execute("PRAGMA index_info(idx_app_records_size)")] == ["app", "size"]
    assert conn.execute("SELECT COUNT(*) FROM app_records").fetchone()[0] == 0
    after = snapshot(conn)
    assert {t: after[t] for t in before} == before
    # Applied once: a second run is a no-op.
    assert run_migrations(conn, migrations_dir=migrations_upto(11)) == []
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations WHERE version = 11").fetchone()[0] == 1
    conn.close()


def test_one_record_per_app_collection_and_key(conn):
    conn.execute("INSERT INTO app_records VALUES ('notes', 'c', 'k', '1', 1, 't', 't')")
    # The same key in another collection, or another app's, is its own.
    conn.execute("INSERT INTO app_records VALUES ('notes', 'd', 'k', '1', 1, 't', 't')")
    conn.execute("INSERT INTO app_records VALUES ('frms', 'c', 'k', '1', 1, 't', 't')")
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO app_records VALUES ('notes', 'c', 'k', '2', 1, 't', 't')")


def test_011_adopts_a_preexisting_table(tmp_path):
    path = str(tmp_path / "has-records.db")
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE app_records (app TEXT NOT NULL, collection TEXT NOT NULL, key TEXT NOT NULL, "
        "value TEXT NOT NULL, size INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, "
        "PRIMARY KEY (app, collection, key))"
    )
    conn.execute("INSERT INTO app_records VALUES ('notes', 'c', 'k', '1', 1, 't', 't')")
    conn.commit()

    run_migrations(conn)

    assert conn.execute("SELECT * FROM app_records").fetchall() == [("notes", "c", "k", "1", 1, "t", "t")]
    conn.close()


# ---------- 10. app level 5 ----------
#
# Level 5 brought records; this OpenFit is that or newer (6 brought
# report files, C3), so an app saying requires: 5 loads, and one a level
# beyond this OpenFit's is skipped and refused.


def test_an_app_needing_level_five_loads_on_this_openfit(client, apps_dir, capsys):
    newer = apps.APP_LEVEL + 1
    beyond = f"needs OpenFit app level {newer}; this OpenFit is level {apps.APP_LEVEL}"
    assert apps.APP_LEVEL >= 5
    assert apps.level_problem(5) is None
    assert apps.level_problem(newer) == beyond
    make_app(apps_dir, "notes", NOTES, extra="requires: 5\n")
    make_app(apps_dir, "newer", NOTES, extra=f"requires: {newer}\n")

    # requires: 5 loads, and its records work; a level beyond is skipped ...
    assert [(a.id, a.requires) for a in apps.scan_apps(apps_dir)] == [("notes", 5)]
    assert f"[apps] skipping {apps_dir / 'newer'}: {beyond}" in capsys.readouterr().out
    assert act(client, "put", key="k", value=1).status_code == 200
    # ... and refused at install.
    resp = client.post("/api/apps", json={"files": {"app.yaml": f"id: newest\nrequires: {newer}\n{SIZE}"}})
    assert (resp.status_code, resp.get_json()) == (400, {"ok": False, "error": beyond})
    resp = client.post("/api/apps", json={"files": {"app.yaml": f"id: fine\nrequires: 5\n{SIZE}"}})
    assert resp.status_code == 201


def test_a_library_version_needing_a_newer_level_waits(tmp_path, apps_dir, monkeypatch):
    """A library app's next version that needs more than this OpenFit
    waits, its installed copy - at level 5, using records - kept."""
    import admin_ops

    library = tmp_path / "library"
    library.mkdir()
    notes_path = Path(main.app.config["APP_UPDATES_PATH"])

    def publish(requires, commit):
        src = tmp_path / f"src-{commit}"
        (src / "notes").mkdir(parents=True)
        (src / "repo.yaml").write_text("source: openfitavi\nname: OpenFit Aviation\n")
        (src / "notes" / "app.yaml").write_text(f"id: notes\nname: Notes\nrequires: {requires}\n{SIZE}")
        (src / "notes" / "app.py").write_text(textwrap.dedent(NOTES))
        admin_ops.publish_set(str(src), str(library), "openfitavi", commit.ljust(40, "c"),
                              "2026-11-02T09:00:00Z", log=lambda text: None, label="test/repo",
                              skip_hidden=True, changes=lambda app: [])

    publish(5, "1")
    apps.install_library_app(str(apps_dir), "openfitavi",
                             shelf.read_app_files(str(library), "openfitavi", "notes"))
    assert [(a.id, a.requires) for a in apps.scan_apps(apps_dir)] == [("openfitavi.notes", 5)]
    publish(apps.APP_LEVEL + 1, "2")

    result = shelf.sync_library_apps(str(apps_dir), str(library), str(notes_path), now="2026-11-02T09:01:00Z")

    note = result["openfitavi.notes"]
    assert (note["state"], note["level"]) == ("waiting", apps.APP_LEVEL)
    assert note["reason"] == apps.level_problem(apps.APP_LEVEL + 1)
    assert "requires: 5" in (apps_dir / "openfitavi.notes" / "app.yaml").read_text()
