"""
The roster app's own logic, on its own: reading an .ics, picking out the
duties, labelling them and routing them by the clock.

The app (tests/fixtures/apps/roster/app.py) is loaded by path, as the
host loads it - nothing added to sys.path - reading its rules from the
config.json beside it (the fixture's, synthetic), and its module functions
called directly, with a fixed `now`; its render() too, over a ctx of
sessions written straight in, with a fixed today. What the host does
with what it writes is test_app_actions.py's: the round trip through the
action route.

tests/fixtures/rosters/sample.ics is a synthetic roster - fake flights
and ports - in a Cathay-style export's shape: CRLF lines, a folded and
escaped description, no UIDs. Its year is 2025, so to the round trip
every duty in it is long over.
"""

import importlib.util
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import host
from plugins.base import iso_utc, write_session

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "rosters" / "sample.ics"


def load_roster_app():
    spec = importlib.util.spec_from_file_location("roster_app", FIXTURES / "apps" / "roster" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


roster = load_roster_app()


def sample():
    # newline="": the file's CRLFs as written, as an upload's text has them.
    return SAMPLE.read_text(newline="")


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def ics(*events):
    """An iCalendar text of `events`, each a VEVENT's property lines, CRLF-ended."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//OpenFit tests//EN"]
    for props in events:
        lines += ["BEGIN:VEVENT", *props, "END:VEVENT"]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def timed(summary, start="20251120T010000Z", end="20251120T090000Z", description=None):
    props = [f"DTSTART:{start}", f"DTEND:{end}", f"SUMMARY:{summary}"]
    return props + ([f"DESCRIPTION:{description}"] if description is not None else [])


# ---------- parsing ----------


def test_parse_skips_all_day_and_keeps_timed():
    events = roster.parse_events(sample())

    # Every event read: the O and G days off all-day, marked so ...
    assert [(e["summary"], e["all_day"]) for e in events] == [
        ("03-Nov O", True),
        ("04-Nov G", True),
        ("05-Nov CX 9101 ZZA-ZZB RP", False),
        ("08-Nov CX9102 ZZB-ZZA", False),
        ("10-Nov R1E", False),
        ("12-Nov A1534", False),
    ]
    assert events[0]["start"] == date(2025, 11, 3)
    # ... and only the four timed ones are duties.
    found = roster.duties(events)
    assert [d.code for d in found] == ["CX9101", "CX9102", "R1E", "A1534"]
    assert len(found) == 4


@pytest.mark.parametrize("code", ["O", "G", "U", "L", "XQ7"])
def test_any_all_day_event_is_a_day_off_whatever_its_code(code):
    # Structural, not a list: a code never seen before, all day, is off.
    text = ics(["DTSTART;VALUE=DATE:20251120", "DTEND;VALUE=DATE:20251121", f"SUMMARY:20-Nov {code}"])

    assert roster.duties(roster.parse_events(text)) == []


def test_parse_unfolds_and_unescapes_the_description():
    [_, _, flight, *_] = roster.parse_events(sample())

    # The folded line joined, \n a line break, \, a comma - verbatim else.
    assert flight["description"] == (
        "CX9101 ZZA-ZZB\nRPT 23:10L\nZZA 00:10L - ZZB 08:40L BLK 09:30 A359, seat 2\nREL 12:10L\nTOTAL 13:00"
    )


def test_parse_reads_a_roster_with_bare_line_feeds_too():
    assert roster.parse_events(sample().replace("\r\n", "\n")) == roster.parse_events(sample())


def test_unescaping_takes_each_escape_once():
    [event] = roster.parse_events(ics(timed("x", description=r"a\\nb\;c\Nd\,e\\")))

    # \\n is a backslash and an n, not a backslash and a line break.
    assert event["description"] == "a\\nb;c\nd,e\\"


def test_an_alarm_inside_an_event_lends_it_nothing():
    text = ics([*timed("20-Nov R1E", description="Home reserve"),
                "BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Reminder", "END:VALARM"])

    [event] = roster.parse_events(text)
    assert event["description"] == "Home reserve"


def test_an_event_that_cant_be_read_is_skipped_not_the_upload(capsys):
    text = ics(timed("20-Nov R1E", start="2025-11-20 01:00"), timed("21-Nov R2E", "20251121T010000Z", "20251121T090000Z"))

    events = roster.parse_events(text)

    assert [e["summary"] for e in events] == ["21-Nov R2E"]
    assert "[roster] skipping an event that can't be read, '20-Nov R1E'" in capsys.readouterr().out


@pytest.mark.parametrize(
    "line",
    [
        "DTSTART;TZID=Asia/Hong_Kong:20251120T090000",
        "DTSTART:20251120T090000",                       # floating: local to wherever it's read
    ],
)
def test_non_utc_timed_event_is_rejected(line):
    text = ics([line, "DTEND:20251120T170000Z", "SUMMARY:20-Nov R1E"])

    with pytest.raises(ValueError) as refused:
        roster.parse_events(text)
    assert str(refused.value) == f"roster has a non-UTC time, which isn't supported yet: {line}"


def test_a_timezone_definition_is_no_event_and_no_refusal():
    # An export may define its zones: their floating DTSTARTs are the
    # zone's, not an event's, so they neither count nor refuse the file.
    zone = ["BEGIN:VTIMEZONE", "TZID:Asia/Hong_Kong", "BEGIN:STANDARD", "DTSTART:19700101T000000",
            "TZOFFSETFROM:+0800", "TZOFFSETTO:+0800", "END:STANDARD", "END:VTIMEZONE"]
    text = ics(timed("20-Nov R1E")).replace("BEGIN:VEVENT", "\r\n".join(zone) + "\r\nBEGIN:VEVENT", 1)

    assert [e["summary"] for e in roster.parse_events(text)] == ["20-Nov R1E"]


def test_a_file_that_isnt_icalendar_has_no_events():
    assert roster.parse_events("2026-01-03 81.2\n2026-01-04 80.9\n") == []
    assert roster.parse_events("") == []


# ---------- duties ----------


def test_a_timed_event_with_no_end_or_ending_before_it_starts_is_skipped(capsys):
    text = ics(
        ["DTSTART:20251120T010000Z", "SUMMARY:20-Nov R1E"],
        timed("21-Nov R2E", "20251121T090000Z", "20251121T010000Z"),
        timed("22-Nov R3E", "20251122T010000Z", "20251122T010000Z"),   # no length, still a duty
    )

    assert [d.code for d in roster.duties(roster.parse_events(text))] == ["R3E"]
    out = capsys.readouterr().out
    assert "skipping '20-Nov R1E'" in out and "skipping '21-Nov R2E'" in out


def test_classify_flight_reserve_sim_other():
    classify = roster.classify
    # A flight: CX and its number, apart or together; the route; a
    # trailing crew position when there is one.
    assert classify("05-Nov CX 9101 ZZA-ZZB RP") == {
        "type": "flight", "code": "CX9101", "flight_no": "CX9101", "dep": "ZZA", "arr": "ZZB", "position": "RP",
    }
    assert classify("08-Nov CX9102 ZZB-ZZA") == {
        "type": "flight", "code": "CX9102", "flight_no": "CX9102", "dep": "ZZB", "arr": "ZZA",
    }
    assert classify("09-Nov CX9103 RQ") == {"type": "flight", "code": "CX9103", "flight_no": "CX9103", "position": "RQ"}
    assert classify("10-Nov R1E") == {"type": "reserve", "code": "R1E"}
    assert classify("11-Nov R8E") == {"type": "reserve", "code": "R8E"}
    assert classify("12-Nov A1534") == {"type": "sim", "code": "A1534"}
    # Anything else - invented here - is other, its code kept; so is a
    # title with no day before the code, or none at all.
    assert classify("13-Nov ZQX9 ZZA") == {"type": "other", "code": "ZQX9"}
    assert classify("CRM training") == {"type": "other", "code": "CRM"}
    assert classify("ADMIN") == {"type": "other", "code": "ADMIN"}   # A, but no digit: not a sim
    assert classify("") == {"type": "other", "code": ""}


def test_a_duty_of_a_code_never_seen_is_still_a_duty():
    text = ics(timed("20-Nov ZQX9 ZZA", description="Something new"))

    [duty] = roster.duties(roster.parse_events(text))

    assert (duty.type, duty.code, duty.summary, duty.description) == ("other", "ZQX9", "20-Nov ZQX9 ZZA", "Something new")


def test_a_dutys_summary_is_its_label_code_and_raw_description():
    flight, _, reserve, _ = roster.duties(roster.parse_events(sample()))

    assert roster.session_summary(flight) == {
        "capture": "live", "type": "flight", "code": "CX9101", "label": "05-Nov CX 9101 ZZA-ZZB RP",
        "raw": "CX9101 ZZA-ZZB\nRPT 23:10L\nZZA 00:10L - ZZB 08:40L BLK 09:30 A359, seat 2\nREL 12:10L\nTOTAL 13:00",
        "flight_no": "CX9101", "dep": "ZZA", "arr": "ZZB", "position": "RP",
    }
    # No flight keys for a reserve: left out, not null.
    assert roster.session_summary(reserve) == {
        "capture": "live", "type": "reserve", "code": "R1E", "label": "10-Nov R1E",
        "raw": "R1E - Home reserve\nRPT 06:00L REL 18:00L\nTOTAL 12:00",
    }


# ---------- routing by the clock ----------


def test_bucket_for_complete_vs_rostered():
    now = utc(2025, 11, 10, 12, 0)

    assert roster.bucket_for(utc(2025, 11, 8, 13, 0), now) == "duties_complete"     # over
    assert roster.bucket_for(now, now) == "duties_complete"                         # just over
    assert roster.bucket_for(now + timedelta(seconds=1), now) == "duties_rostered"  # under way
    assert roster.bucket_for(utc(2025, 11, 12, 5, 30), now) == "duties_rostered"    # to come


def test_daily_hours_sum_a_day_by_its_start_and_split_by_kind():
    now = utc(2025, 11, 20, 12, 0)
    text = ics(
        timed("19-Nov R1E", "20251119T010000Z", "20251119T050000Z"),         # 4h  over
        timed("19-Nov A1534", "20251119T080000Z", "20251119T113000Z"),       # 3.5h over, same day
        timed("20-Nov CX9101 ZZA-ZZB", "20251120T100000Z", "20251121T000000Z"),  # 14h, under way at now
        timed("20-Nov R2E", "20251120T060000Z", "20251120T080000Z"),         # 2h  over
        timed("21-Nov CX9102 ZZB-ZZA", "20251121T220000Z", "20251122T090000Z"),  # 11h to come, past midnight
    )

    assert roster.daily_hours(roster.duties(roster.parse_events(text)), now) == {
        ("duty_hours_complete", "2025-11-19"): 7.5,
        ("duty_hours_complete", "2025-11-20"): 2.0,
        ("duty_hours_rostered", "2025-11-20"): 14.0,
        ("duty_hours_rostered", "2025-11-21"): 11.0,   # whole, on the day it started
    }


def test_a_duty_twice_in_the_file_counts_once():
    now = utc(2025, 11, 30)
    twice = timed("19-Nov R1E", "20251119T010000Z", "20251119T050000Z")

    duties = roster.duties(roster.parse_events(ics(twice, twice)))

    assert len(duties) == 2
    assert roster.daily_hours(duties, now) == {("duty_hours_complete", "2025-11-19"): 4.0}


# ---------- the tile ----------
#
# render() over a ctx of sessions written straight in, as the host would
# have filed them, with today fixed: what it draws, and how far back.

TODAY = date(2026, 10, 15)
PICKER = {
    "type": "input", "key": "file", "action": "upload", "label": "Upload roster",
    "kind": "file", "unit": None, "options": None, "accept": ".ics",
}


def file_duty(conn, kind, start, end, code, source="app:roster"):
    write_session(conn, source, kind, start, end, {"capture": "live", "type": "other", "code": code})


def drawn(conn):
    """render()'s spec, and the instants either side of it."""
    before = datetime.now(timezone.utc).replace(microsecond=0)
    spec = roster.render(host.Ctx(conn, today=TODAY), {})
    return spec, before, datetime.now(timezone.utc)


def test_render_draws_the_rostered_and_the_flown_on_a_timeline(conn):
    # About six months back - 183 days before today, from that day's
    # first second - and everything still to come.
    file_duty(conn, "duties_complete", "2026-04-14T23:59:59Z", "2026-04-15T08:00:00Z", "CX9100")   # a day too early
    file_duty(conn, "duties_complete", "2026-04-15T00:00:00Z", "2026-04-15T04:30:00Z", "A1534")
    file_duty(conn, "duties_complete", "2026-10-10T01:00:00Z", "2026-10-10T14:00:00Z", "CX9101")
    file_duty(conn, "duties_rostered", "2026-10-20T22:00:00Z", "2026-10-21T10:00:00Z", "R1E")
    file_duty(conn, "duties_rostered", "2099-11-05T15:10:00Z", "2099-11-06T04:10:00Z", "CX9102")
    # Another source's duty with no end: nothing to draw a block of.
    file_duty(conn, "duties_rostered", "2026-10-22T00:00:00Z", None, "R2E", source="webhook")
    conn.commit()

    spec, before, after = drawn(conn)

    # The picker on top; the timeline under it, rostered then flown, each
    # duty a block labelled with its code; the line at now, the render's
    # own; the flown side greyed after it; in the FRMS focus group.
    [picker, timeline] = spec["children"]
    assert spec["type"] == "panel" and picker == PICKER
    assert iso_utc(before) <= timeline["now"] <= iso_utc(after)
    assert timeline == {"type": "timeline", "title": None, "now": timeline["now"], "sync": "frms", "lanes": [
        {"label": "Rostered", "grey_after_now": False, "intervals": [
            {"start": "2026-10-20T22:00:00Z", "end": "2026-10-21T10:00:00Z", "label": "R1E"},
            {"start": "2099-11-05T15:10:00Z", "end": "2099-11-06T04:10:00Z", "label": "CX9102"},
        ]},
        {"label": "Flown", "grey_after_now": True, "intervals": [
            {"start": "2026-04-15T00:00:00Z", "end": "2026-04-15T04:30:00Z", "label": "A1534"},
            {"start": "2026-10-10T01:00:00Z", "end": "2026-10-10T14:00:00Z", "label": "CX9101"},
        ]},
    ]}


def test_a_duty_with_no_code_is_a_block_with_no_label(conn):
    write_session(conn, "app:roster", "duties_complete", "2026-10-10T01:00:00Z", "2026-10-10T02:00:00Z", {})
    conn.commit()

    [_, timeline] = drawn(conn)[0]["children"]

    assert timeline["lanes"][1]["intervals"] == [
        {"start": "2026-10-10T01:00:00Z", "end": "2026-10-10T02:00:00Z", "label": ""},
    ]


def file_flight(conn, kind, start, end, code, route):
    """A flight duty, filed as the app files one: its route a summary's dep and arr."""
    dep, arr = route.split("-")
    write_session(conn, "app:roster", kind, start, end,
                  {"capture": "live", "type": "flight", "code": code, "flight_no": code, "dep": dep, "arr": arr})


def test_render_shades_each_lanes_trips_behind_its_blocks(conn):
    # The fixture's home base is ZZA. Flown: a trip out and back; and one
    # out, not back yet - on the layover now.
    file_flight(conn, "duties_complete", "2026-10-01T01:00:00Z", "2026-10-01T14:00:00Z", "CX9101", "ZZA-ZZB")
    file_flight(conn, "duties_complete", "2026-10-03T02:00:00Z", "2026-10-03T13:00:00Z", "CX9102", "ZZB-ZZA")
    file_flight(conn, "duties_complete", "2026-10-12T01:00:00Z", "2026-10-12T14:00:00Z", "CX9103", "ZZA-ZZC")
    # Rostered: a trip out and back, a reserve, and one out the roster
    # runs out before it's back - its last duty a reserve after it.
    file_flight(conn, "duties_rostered", "2099-11-05T15:10:00Z", "2099-11-06T04:10:00Z", "CX9101", "ZZA-ZZB")
    file_flight(conn, "duties_rostered", "2099-11-08T02:00:00Z", "2099-11-08T13:00:00Z", "CX9102", "ZZB-ZZA")
    file_duty(conn, "duties_rostered", "2099-11-10T22:00:00Z", "2099-11-11T10:00:00Z", "R1E")
    file_flight(conn, "duties_rostered", "2099-11-20T01:00:00Z", "2099-11-20T12:00:00Z", "CX9105", "ZZA-ZZD")
    file_duty(conn, "duties_rostered", "2099-11-25T22:00:00Z", "2099-11-26T10:00:00Z", "R2E")
    conn.commit()

    spec, before, after = drawn(conn)

    [_, timeline] = spec["children"]
    rostered, flown = timeline["lanes"]
    # Each lane its own trips, a band each from the flight out's start
    # to the flight home's end, labelled with the port it flew out to;
    # one still away ends where the lane's data does - the rostered at
    # the loaded roster's last duty, the flown now: the render's own.
    assert rostered["bands"] == [
        {"start": "2099-11-05T15:10:00Z", "end": "2099-11-08T13:00:00Z", "label": "ZZB"},
        {"start": "2099-11-20T01:00:00Z", "end": "2099-11-26T10:00:00Z", "label": "ZZD"},
    ]
    assert flown["bands"] == [
        {"start": "2026-10-01T01:00:00Z", "end": "2026-10-03T13:00:00Z", "label": "ZZB"},
        {"start": "2026-10-12T01:00:00Z", "end": timeline["now"], "label": "ZZC"},
    ]
    assert iso_utc(before) <= timeline["now"] <= iso_utc(after)
    # The blocks are as they were: every duty, the flights among them.
    assert [i["label"] for i in rostered["intervals"]] == ["CX9101", "CX9102", "R1E", "CX9105", "R2E"]
    assert [i["label"] for i in flown["intervals"]] == ["CX9101", "CX9102", "CX9103"]


def test_render_with_nothing_in_the_window_asks_for_a_roster(conn):
    empty = {"type": "panel", "children": [{"type": "empty", "message": "Upload your roster .ics"}, PICKER]}
    assert drawn(conn)[0] == empty

    # A roster flown before the window is still nothing to draw.
    file_duty(conn, "duties_complete", "2026-04-14T01:00:00Z", "2026-04-14T09:00:00Z", "CX9101")
    conn.commit()
    assert drawn(conn)[0] == empty


# ---------- the config drives the labels ----------
#
# classify() takes its types from the roster config, not from code. The
# fixture's rules are CX->flight, R->reserve, A#->sim, else other, and the
# tests above lean on them; here, that the rules really are the config's -
# a different config labels differently - and that the flight-detail parse
# (number, route, position) is the title's shape, airline-agnostic.


def cfg(duty_types, default="other"):
    return {"duty_types": duty_types, "default_type": default}


def test_classify_takes_its_types_from_the_config():
    # Another airline's prefix, read by the same engine: here QF is the
    # flight, and its number/route/position parse just as a CX's would.
    other_airline = cfg([{"type": "flight", "prefixes": ["QF"]}])
    assert roster.classify("05-Nov QF123 ZZA-ZZB RP", other_airline) == {
        "type": "flight", "code": "QF123", "flight_no": "QF123",
        "dep": "ZZA", "arr": "ZZB", "position": "RP",
    }
    # And with no rule for it, a CX is just "other": nothing about the
    # carrier is baked into the code.
    assert roster.classify("05-Nov CX9101 ZZA-ZZB", other_airline) == {"type": "other", "code": "CX9101"}


def test_a_regex_rule_matches_and_the_first_rule_wins():
    rules = cfg([
        {"type": "sim", "regex": r"^A\d"},
        {"type": "reserve", "prefixes": ["A"]},   # also matches A1534, but sim is first
    ])
    assert roster.classify("12-Nov A1534", rules)["type"] == "sim"       # the regex, first
    assert roster.classify("12-Nov AX", rules)["type"] == "reserve"      # no digit: the prefix


def test_an_unmatched_code_takes_the_configs_default():
    assert roster.classify("13-Nov ZZZ", cfg([], default="ground")) == {"type": "ground", "code": "ZZZ"}


# ---------- the config must load ----------
#
# config.json is read once, beside app.py, when the module loads (as the
# host loads it). A config that's missing or won't parse doesn't crash the
# app or mislabel a duty: the module holds CONFIG None and a plain reason,
# the tile shows it over the picker, and an upload is refused with it -
# nothing written, nothing parsed.


def load_with_config(tmp_path, config_text):
    """A fresh copy of the app loaded from tmp_path, with config_text as its
    config.json - or no config.json at all when config_text is None - so the
    module-level load runs against it. Each load gets its own module name so
    nothing is cached between cases."""
    (tmp_path / "app.py").write_text((FIXTURES / "apps" / "roster" / "app.py").read_text())
    if config_text is not None:
        (tmp_path / "config.json").write_text(config_text)
    spec = importlib.util.spec_from_file_location(f"roster_cfg_{tmp_path.name}", tmp_path / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeCtx:
    """Only what render()'s and handle()'s config-missing paths touch."""
    def input(self, *a, **k):
        return {"input": True}

    def empty(self, message):
        return {"empty": message}

    def panel(self, *children):
        return {"panel": list(children)}


@pytest.mark.parametrize("config_text, message", [
    (None, "config.json is missing — reinstall the app with one"),
    ("{ not json", None),                               # -> "config.json won't read: ..."
    ('{"duty_types": "nope"}', "config.json needs a duty_types list"),
    ('["not", "a", "dict"]', "config.json needs a duty_types list"),
])
def test_a_config_that_wont_load_is_caught_at_the_edge(tmp_path, config_text, message):
    app = load_with_config(tmp_path, config_text)

    assert app.CONFIG is None
    if message is None:
        assert app.CONFIG_ERROR.startswith("config.json won't read:")
    else:
        assert app.CONFIG_ERROR == message
    # The tile says why, over its picker - it never reads a session.
    assert app.render(FakeCtx(), {}) == {"panel": [{"empty": app.CONFIG_ERROR}, {"input": True}]}
    # And an upload is refused in the same words, before anything is read.
    with pytest.raises(ValueError) as refused:
        app.handle(FakeCtx(), "upload", {})
    assert str(refused.value) == app.CONFIG_ERROR


def test_a_config_with_no_home_base_shades_no_trips(tmp_path, conn):
    # A config with its rules but no personal block: the duties still
    # draw, as blocks; with no base to be away from, there's no trip.
    app = load_with_config(tmp_path, '{"duty_types": [{"type": "flight", "prefixes": ["CX"]}]}')
    file_flight(conn, "duties_complete", "2026-10-01T01:00:00Z", "2026-10-01T14:00:00Z", "CX9101", "ZZA-ZZB")
    file_flight(conn, "duties_complete", "2026-10-03T02:00:00Z", "2026-10-03T13:00:00Z", "CX9102", "ZZB-ZZA")
    conn.commit()

    [_, timeline] = app.render(host.Ctx(conn, today=TODAY), {})["children"]

    assert [len(lane["intervals"]) for lane in timeline["lanes"]] == [0, 2]
    assert not any("bands" in lane for lane in timeline["lanes"])


def test_a_good_config_loads():
    # The fixture's own config.json, loaded as the host loads it.
    assert roster.CONFIG is not None and roster.CONFIG_ERROR is None
    assert any(r.get("type") == "flight" for r in roster.CONFIG["duty_types"])


# ---------- trips: the layovers ----------
#
# trips() on its own: a lane's duty sessions, as ctx.sessions() gives
# them, grouped into trips away from a home base - here HKG - by their
# flights alone: a flight out of base opens one, the next flight back
# into base closes it, sectors between are inside it.

HOME = "HKG"


def flight(route, start, end, code="CX100"):
    dep, arr = route.split("-")
    return host.Session(start, end, "app:roster", {"type": "flight", "code": code, "dep": dep, "arr": arr})


def duty(kind, start, end, code, **summary):
    return host.Session(start, end, "app:roster", {"type": kind, "code": code, **summary})


def trip(start, end, out_port):
    return {"id": start, "start": start, "end": end, "out_port": out_port}


def test_a_simple_trip_is_one_band():
    out = flight("HKG-MEL", "2026-11-05T15:10:00Z", "2026-11-06T04:10:00Z", "CX105")
    back = flight("MEL-HKG", "2026-11-08T02:00:00Z", "2026-11-08T13:00:00Z", "CX104")

    # From the flight out's start to the flight home's end; the port it
    # flew out to. Taken by start, whatever order they're given in.
    assert roster.trips([back, out], HOME) == [trip("2026-11-05T15:10:00Z", "2026-11-08T13:00:00Z", "MEL")]


def test_an_integrated_pattern_is_one_band():
    sectors = [
        flight("HKG-BKK", "2026-11-05T01:00:00Z", "2026-11-05T04:00:00Z"),
        flight("BKK-SIN", "2026-11-06T03:00:00Z", "2026-11-06T05:30:00Z"),
        flight("SIN-HKG", "2026-11-07T06:00:00Z", "2026-11-07T10:00:00Z"),
    ]

    # One trip - the whole time away - not one a sector: BKK-SIN is inside
    # it, and the first port is the one it's labelled with.
    assert roster.trips(sectors, HOME) == [trip("2026-11-05T01:00:00Z", "2026-11-07T10:00:00Z", "BKK")]


def test_reserve_and_sim_between_trips_open_no_band():
    sessions = [
        flight("HKG-MEL", "2026-11-01T15:00:00Z", "2026-11-02T04:00:00Z"),
        flight("MEL-HKG", "2026-11-04T02:00:00Z", "2026-11-04T13:00:00Z"),
        duty("reserve", "2026-11-05T22:00:00Z", "2026-11-06T10:00:00Z", "R1E"),
        duty("sim", "2026-11-07T01:00:00Z", "2026-11-07T05:30:00Z", "A1534"),
        # A ground positioning, out of base and back, by road: it has a
        # route, but only flights make a trip.
        duty("ground", "2026-11-08T01:00:00Z", "2026-11-08T03:00:00Z", "BUS", dep="HKG", arr="SZX"),
        duty("ground", "2026-11-08T09:00:00Z", "2026-11-08T11:00:00Z", "BUS", dep="SZX", arr="HKG"),
        flight("HKG-BKK", "2026-11-10T01:00:00Z", "2026-11-10T04:00:00Z"),
        flight("BKK-HKG", "2026-11-11T05:00:00Z", "2026-11-11T08:00:00Z"),
    ]

    # Two trips, each its flights' - nothing between opens or stretches one.
    assert roster.trips(sessions, HOME, open_end="2026-11-30T00:00:00Z") == [
        trip("2026-11-01T15:00:00Z", "2026-11-04T13:00:00Z", "MEL"),
        trip("2026-11-10T01:00:00Z", "2026-11-11T08:00:00Z", "BKK"),
    ]


def test_an_open_rostered_trip_runs_to_the_rosters_end():
    out = flight("HKG-MEL", "2026-11-20T15:00:00Z", "2026-11-21T04:00:00Z")
    reserve = duty("reserve", "2026-11-24T22:00:00Z", "2026-11-25T10:00:00Z", "R1E")

    # Out, and the roster runs out before the flight home: the trip ends
    # where the roster does.
    assert roster.trips([out, reserve], HOME, open_end="2026-11-25T10:00:00Z") == [
        trip("2026-11-20T15:00:00Z", "2026-11-25T10:00:00Z", "MEL"),
    ]
    # With nowhere to end it, it's dropped: a band needs an end.
    assert roster.trips([out, reserve], HOME) == []
    # And never before its flight out has landed - the flight out the
    # roster's last word, an end before that is the flight's.
    assert roster.trips([out], HOME, open_end="2026-11-20T15:00:00Z") == [
        trip("2026-11-20T15:00:00Z", "2026-11-21T04:00:00Z", "MEL"),
    ]


def test_an_open_complete_trip_runs_to_now():
    now = "2026-11-06T09:30:00Z"
    flown = [
        flight("HKG-BKK", "2026-11-05T01:00:00Z", "2026-11-05T04:00:00Z"),
        flight("BKK-SIN", "2026-11-06T03:00:00Z", "2026-11-06T05:30:00Z"),
    ]

    # Flown out - and on, a sector - and not home yet: away until now.
    assert roster.trips(flown, HOME, open_end=now) == [trip("2026-11-05T01:00:00Z", now, "BKK")]


def test_back_to_back_trips_are_two_bands():
    sessions = [
        flight("HKG-MEL", "2026-11-01T15:00:00Z", "2026-11-02T04:00:00Z"),
        flight("MEL-HKG", "2026-11-04T02:00:00Z", "2026-11-04T13:00:00Z"),
        # Out again the next morning.
        flight("HKG-BKK", "2026-11-05T01:00:00Z", "2026-11-05T04:00:00Z"),
        flight("BKK-HKG", "2026-11-06T05:00:00Z", "2026-11-06T08:00:00Z"),
    ]

    assert roster.trips(sessions, HOME) == [
        trip("2026-11-01T15:00:00Z", "2026-11-04T13:00:00Z", "MEL"),
        trip("2026-11-05T01:00:00Z", "2026-11-06T08:00:00Z", "BKK"),
    ]


def test_a_flight_with_no_route_is_skipped_for_boundaries():
    bkk = [
        flight("HKG-BKK", "2026-11-10T01:00:00Z", "2026-11-10T04:00:00Z"),
        flight("BKK-HKG", "2026-11-11T05:00:00Z", "2026-11-11T08:00:00Z"),
    ]
    routeless = lambda start, end: duty("flight", start, end, "CX103")

    # Its flight out with no route: nothing opens, so the flight home
    # closes nothing - and the trip after is its own.
    out_unknown = [routeless("2026-11-01T15:00:00Z", "2026-11-02T04:00:00Z"),
                   flight("MEL-HKG", "2026-11-04T02:00:00Z", "2026-11-04T13:00:00Z"), *bkk]
    assert roster.trips(out_unknown, HOME) == [trip("2026-11-10T01:00:00Z", "2026-11-11T08:00:00Z", "BKK")]
    # Its flight home with no route: that trip never closes - the next
    # flight out of base shows it was home by then, so it's dropped, not
    # paired with the next trip's flight home.
    back_unknown = [flight("HKG-MEL", "2026-11-01T15:00:00Z", "2026-11-02T04:00:00Z"),
                    routeless("2026-11-04T02:00:00Z", "2026-11-04T13:00:00Z"), *bkk]
    assert roster.trips(back_unknown, HOME) == [trip("2026-11-10T01:00:00Z", "2026-11-11T08:00:00Z", "BKK")]
    # A sector with no route, inside a trip: nothing changes.
    inside = [bkk[0], routeless("2026-11-10T20:00:00Z", "2026-11-10T22:00:00Z"), bkk[1]]
    assert roster.trips(inside, HOME) == [trip("2026-11-10T01:00:00Z", "2026-11-11T08:00:00Z", "BKK")]
    # A flight that's another source's, still open - no end - is no
    # boundary either.
    assert roster.trips([bkk[0], bkk[1]._replace(end=None)], HOME) == []


def test_a_flight_from_base_to_base_is_no_trip():
    # Out and straight back - a turnback, a local training flight - never
    # leaves base: no trip, and nothing left open for a later flight home.
    sessions = [flight("HKG-HKG", "2026-11-01T01:00:00Z", "2026-11-01T03:00:00Z"),
                flight("MEL-HKG", "2026-11-04T02:00:00Z", "2026-11-04T13:00:00Z")]

    assert roster.trips(sessions, HOME, open_end="2026-11-30T00:00:00Z") == []


def test_the_trip_id_is_the_outbound_flight_start():
    out = flight("HKG-MEL", "2026-11-05T15:10:00Z", "2026-11-06T04:10:00Z")
    back = flight("MEL-HKG", "2026-11-08T02:00:00Z", "2026-11-08T13:00:00Z")

    [found] = roster.trips([out, back], HOME)
    assert found["id"] == out.start == "2026-11-05T15:10:00Z"
    # The flight home re-planned, a day later: the same trip - the same
    # id - longer.
    [replanned] = roster.trips([out, back._replace(start="2026-11-09T02:00:00Z", end="2026-11-09T13:00:00Z")], HOME)
    assert (replanned["id"], replanned["end"]) == (found["id"], "2026-11-09T13:00:00Z")
    # Not drawn: a band is its span and its port, nothing more.
    assert roster._band(found) == {"start": "2026-11-05T15:10:00Z", "end": "2026-11-08T13:00:00Z", "label": "MEL"}
