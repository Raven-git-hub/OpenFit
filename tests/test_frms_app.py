"""
The FRMS app's roster engine, on its own: reading an .ics, picking out
the duties, labelling them and routing them by the clock.

The app (tests/fixtures/apps/frms/app.py) is loaded by path, as the
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
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

import host
from plugins.base import iso_utc, write_session

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "rosters" / "sample.ics"
FRMS_DIR = FIXTURES / "apps" / "frms"


def load_frms_app():
    spec = importlib.util.spec_from_file_location("frms_app", FRMS_DIR / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


frms = load_frms_app()


def sample():
    # newline="": the file's CRLFs as written, as an upload's text has them
    # (open(), not Path.read_text(newline=), which only 3.13 has).
    with open(SAMPLE, encoding="utf-8", newline="") as f:
        return f.read()


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
    events = frms.parse_events(sample())

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
    found = frms.duties(events)
    assert [d.code for d in found] == ["CX9101", "CX9102", "R1E", "A1534"]
    assert len(found) == 4


@pytest.mark.parametrize("code", ["O", "G", "U", "L", "XQ7"])
def test_any_all_day_event_is_a_day_off_whatever_its_code(code):
    # Structural, not a list: a code never seen before, all day, is off.
    text = ics(["DTSTART;VALUE=DATE:20251120", "DTEND;VALUE=DATE:20251121", f"SUMMARY:20-Nov {code}"])

    assert frms.duties(frms.parse_events(text)) == []


def test_parse_unfolds_and_unescapes_the_description():
    [_, _, flight, *_] = frms.parse_events(sample())

    # The folded line joined, \n a line break, \, a comma - verbatim else.
    assert flight["description"] == (
        "CX9101 ZZA-ZZB\nRPT 23:10L\nZZA 00:10L - ZZB 08:40L BLK 09:30 A359, seat 2\nREL 12:10L\nTOTAL 13:00"
    )


def test_parse_reads_a_roster_with_bare_line_feeds_too():
    assert frms.parse_events(sample().replace("\r\n", "\n")) == frms.parse_events(sample())


def test_unescaping_takes_each_escape_once():
    [event] = frms.parse_events(ics(timed("x", description=r"a\\nb\;c\Nd\,e\\")))

    # \\n is a backslash and an n, not a backslash and a line break.
    assert event["description"] == "a\\nb;c\nd,e\\"


def test_an_alarm_inside_an_event_lends_it_nothing():
    text = ics([*timed("20-Nov R1E", description="Home reserve"),
                "BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:Reminder", "END:VALARM"])

    [event] = frms.parse_events(text)
    assert event["description"] == "Home reserve"


def test_an_event_that_cant_be_read_is_skipped_not_the_upload(capsys):
    text = ics(timed("20-Nov R1E", start="2025-11-20 01:00"), timed("21-Nov R2E", "20251121T010000Z", "20251121T090000Z"))

    events = frms.parse_events(text)

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
        frms.parse_events(text)
    assert str(refused.value) == f"roster has a non-UTC time, which isn't supported yet: {line}"


def test_a_timezone_definition_is_no_event_and_no_refusal():
    # An export may define its zones: their floating DTSTARTs are the
    # zone's, not an event's, so they neither count nor refuse the file.
    zone = ["BEGIN:VTIMEZONE", "TZID:Asia/Hong_Kong", "BEGIN:STANDARD", "DTSTART:19700101T000000",
            "TZOFFSETFROM:+0800", "TZOFFSETTO:+0800", "END:STANDARD", "END:VTIMEZONE"]
    text = ics(timed("20-Nov R1E")).replace("BEGIN:VEVENT", "\r\n".join(zone) + "\r\nBEGIN:VEVENT", 1)

    assert [e["summary"] for e in frms.parse_events(text)] == ["20-Nov R1E"]


def test_a_file_that_isnt_icalendar_has_no_events():
    assert frms.parse_events("2026-01-03 81.2\n2026-01-04 80.9\n") == []
    assert frms.parse_events("") == []


# ---------- duties ----------


def test_a_timed_event_with_no_end_or_ending_before_it_starts_is_skipped(capsys):
    text = ics(
        ["DTSTART:20251120T010000Z", "SUMMARY:20-Nov R1E"],
        timed("21-Nov R2E", "20251121T090000Z", "20251121T010000Z"),
        timed("22-Nov R3E", "20251122T010000Z", "20251122T010000Z"),   # no length, still a duty
    )

    assert [d.code for d in frms.duties(frms.parse_events(text))] == ["R3E"]
    out = capsys.readouterr().out
    assert "skipping '20-Nov R1E'" in out and "skipping '21-Nov R2E'" in out


def test_classify_flight_reserve_sim_other():
    classify = frms.classify
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

    [duty] = frms.duties(frms.parse_events(text))

    assert (duty.type, duty.code, duty.summary, duty.description) == ("other", "ZQX9", "20-Nov ZQX9 ZZA", "Something new")


def test_a_dutys_summary_is_its_label_code_and_raw_description():
    flight, _, reserve, _ = frms.duties(frms.parse_events(sample()))

    assert frms.session_summary(flight) == {
        "capture": "live", "type": "flight", "code": "CX9101", "label": "05-Nov CX 9101 ZZA-ZZB RP",
        "raw": "CX9101 ZZA-ZZB\nRPT 23:10L\nZZA 00:10L - ZZB 08:40L BLK 09:30 A359, seat 2\nREL 12:10L\nTOTAL 13:00",
        "flight_no": "CX9101", "dep": "ZZA", "arr": "ZZB", "position": "RP",
    }
    # No flight keys for a reserve: left out, not null.
    assert frms.session_summary(reserve) == {
        "capture": "live", "type": "reserve", "code": "R1E", "label": "10-Nov R1E",
        "raw": "R1E - Home reserve\nRPT 06:00L REL 18:00L\nTOTAL 12:00",
    }


# ---------- routing by the clock ----------


def test_bucket_for_complete_vs_rostered():
    now = utc(2025, 11, 10, 12, 0)

    assert frms.bucket_for(utc(2025, 11, 8, 13, 0), now) == "duties_complete"     # over
    assert frms.bucket_for(now, now) == "duties_complete"                         # just over
    assert frms.bucket_for(now + timedelta(seconds=1), now) == "duties_rostered"  # under way
    assert frms.bucket_for(utc(2025, 11, 12, 5, 30), now) == "duties_rostered"    # to come


# ---------- backfill: past duties into the plan too ----------


def test_filed_as_is_the_clock_split_and_backfill_doubles_only_a_past_duty():
    now = utc(2025, 11, 10, 12, 0)
    past, just_over, under_way = utc(2025, 11, 8, 13, 0), now, now + timedelta(seconds=1)

    # Off: one kind each, by the clock, captured live.
    assert frms.filed_as(past, now) == [("duties_complete", "live")]
    assert frms.filed_as(under_way, now) == [("duties_rostered", "live")]
    assert frms.filed_as(past, now, backfill=False) == [("duties_complete", "live")]
    # On: a duty that's over is flown, and put back into the plan - its
    # copy there captured "backfill"; one still to come is as it was.
    assert frms.filed_as(past, now, backfill=True) == [("duties_complete", "live"), ("duties_rostered", "backfill")]
    assert frms.filed_as(just_over, now, backfill=True) == [("duties_complete", "live"), ("duties_rostered", "backfill")]
    assert frms.filed_as(under_way, now, backfill=True) == [("duties_rostered", "live")]


def test_a_backfilled_summary_says_so():
    flight = frms.duties(frms.parse_events(sample()))[0]

    live = frms.session_summary(flight)
    backfilled = frms.session_summary(flight, capture="backfill")

    # The capture is the one difference: the paper trail of a plan put
    # back after the fact.
    assert live["capture"] == "live" and backfilled["capture"] == "backfill"
    assert {**backfilled, "capture": "live"} == live
    assert frms.session_summary(flight, "live") == live


# ---------- the tile ----------
#
# render() over a ctx of sessions written straight in, as the host would
# have filed them, with today fixed: what it draws, and how far back.

TODAY = date(2026, 10, 15)
BACKFILL = {
    "key": "backfill", "label": "Backfill",
    "confirm": "Backfill past duties into your rostered plan? They're also recorded as "
               "flown, and any plan you already uploaded for those duties is replaced.",
}
PICKER = {
    "type": "input", "key": "file", "action": "upload", "label": "Upload roster",
    "kind": "file", "unit": None, "options": None, "accept": ".ics", "toggle": BACKFILL,
}


def file_duty(conn, kind, start, end, code, source="app:frms"):
    write_session(conn, source, kind, start, end, {"capture": "live", "type": "other", "code": code})


def _roster_cell(spec):
    """The roster panel's spec in render()'s grid: the sub-panel at x 0, y 2."""
    [cell] = [c for c in spec["cells"] if (c["x"], c["y"]) == (0, 2)]
    return cell["spec"]


def _circadian_cell(spec):
    """The circadian panel's spec in render()'s grid: the sub-panel at x 0, y 0."""
    [cell] = [c for c in spec["cells"] if (c["x"], c["y"]) == (0, 0)]
    return cell["spec"]


def drawn(conn):
    """render()'s roster panel, and the instants either side of it."""
    before = datetime.now(timezone.utc).replace(microsecond=0)
    spec = _roster_cell(frms.render(host.Ctx(conn, today=TODAY), {}))
    return spec, before, datetime.now(timezone.utc)


def test_the_tile_is_four_sub_panels_and_the_flash_on_a_six_by_five_grid(conn):
    file_duty(conn, "duties_rostered", "2099-11-05T15:10:00Z", "2099-11-06T04:10:00Z", "CX9102")
    conn.commit()

    spec = frms.render(host.Ctx(conn, today=TODAY), {})

    # The whole tile, its cells: circadian top-left, sleep top-right, the
    # roster bottom-left at 2x2, the rest budget beside it, and under
    # them the flash, 4x1 along the bottom right -
    # the roster panel the one _roster() draws, as it was alone.
    assert (spec["type"], spec["cols"], spec["rows"]) == ("grid", 6, 5)
    assert [(c["x"], c["y"], c["w"], c["h"]) for c in spec["cells"]] == [
        (0, 0, 3, 2), (3, 0, 3, 2), (0, 2, 2, 2), (2, 2, 4, 2), (2, 4, 4, 1),
    ]
    [circadian, top_right, roster, bottom_right, flash] = [c["spec"] for c in spec["cells"]]
    # The flash: the committed light image, the dark one for the dark
    # themes, hugging the bottom right.
    folder = FRMS_DIR
    assert flash == {"type": "image", "src": (folder / "flash-light.txt").read_text().strip(),
                     "dark": (folder / "flash-dark.txt").read_text().strip(),
                     "alt": "OpenFit FRMS - Fatigue Risk Management System", "position": "bottom right"}
    assert flash["src"].startswith("data:image/webp;base64,") and flash["dark"].startswith("data:image/webp;base64,")
    # The fixture's config has no positions, so the circadian panel says
    # why, in its own sub-panel - and the rest are drawn as ever.
    assert circadian == {"type": "error", "message": "home base ZZA needs a lat and lon in config.json's destinations"}
    # No sleep with stages yet: the sleep panel asks for some.
    assert top_right == {"type": "empty", "message": "No sleep stages yet - sync a sleep source that gives them"}
    # The rest budget: a row - the donuts and bars, this month's donut.
    assert bottom_right["type"] == "row" and [c["type"] for c in bottom_right["children"]] == ["column", "donut"]
    assert roster["type"] == "panel" and roster["children"][0]["type"] == "rails"
    assert _roster_cell(spec) is roster
    # Every cell of the six by five covered, once - but the bottom row's
    # left end, bare.
    covered = [(x, y) for c in spec["cells"]
               for x in range(c["x"], c["x"] + c["w"]) for y in range(c["y"], c["y"] + c["h"])]
    assert sorted(covered) == [(x, y) for x in range(6) for y in range(5) if (x, y) not in ((0, 4), (1, 4))]
    # Laid out the same before any duty: the roster's sub-panel asks for one.
    conn.execute("DELETE FROM sessions")
    conn.commit()
    empty = frms.render(host.Ctx(conn, today=TODAY), {})
    assert [{k: c[k] for k in "xywh"} for c in empty["cells"]] == [{k: c[k] for k in "xywh"} for c in spec["cells"]]
    assert _roster_cell(empty) == {"type": "panel", "children": [
        {"type": "empty", "message": "Upload your roster .ics"}, PICKER]}
    assert _circadian_cell(empty) == {"type": "empty", "message": "Upload your roster .ics"}


@pytest.mark.parametrize("files, want", [
    # Neither file: no flash, the row bare.
    ({}, None),
    # One alone - either - in every theme.
    ({"flash-light.txt": "LIGHT"}, ("LIGHT", None)),
    ({"flash-dark.txt": "DARK"}, ("DARK", None)),
    ({"flash-light.txt": "LIGHT\n", "flash-dark.txt": " DARK"}, ("LIGHT", "DARK")),
    # An empty file is none.
    ({"flash-light.txt": "\n", "flash-dark.txt": "DARK"}, ("DARK", None)),
])
def test_the_flash_is_its_files_and_the_row_is_bare_without_them(tmp_path, conn, files, want):
    uri = lambda tag: "data:image/webp;base64," + {"LIGHT": "TElHSFQ=", "DARK": "REFSSw=="}[tag]
    for name, text in files.items():
        (tmp_path / name).write_text(text.replace("LIGHT", uri("LIGHT")).replace("DARK", uri("DARK")))
    app = load_with_config(tmp_path, json.dumps(REAL_CONFIG))

    spec = app.render(host.Ctx(conn, today=TODAY), {})

    assert (spec["cols"], spec["rows"]) == (6, 5)
    flash = [c for c in spec["cells"] if (c["x"], c["y"]) == (2, 4)]
    if want is None:
        assert flash == [] and len(spec["cells"]) == 4
    else:
        [cell] = flash
        assert (cell["w"], cell["h"]) == (4, 1)
        assert (cell["spec"]["src"], cell["spec"]["dark"]) == (uri(want[0]), want[1] and uri(want[1]))


def test_a_flash_the_host_wont_draw_leaves_the_row_bare_and_is_logged(tmp_path, conn, capsys):
    # A web address: never fetched, so never drawn.
    (tmp_path / "flash-light.txt").write_text("https://example.com/flash.png")
    app = load_with_config(tmp_path, json.dumps(REAL_CONFIG))

    spec = app.render(host.Ctx(conn, today=TODAY), {})

    assert [(c["x"], c["y"]) for c in spec["cells"]] == [(0, 0), (3, 0), (0, 2), (2, 2)]
    assert "[apps] frms: the flash isn't drawn: an image's src must be a data: URI" in capsys.readouterr().out


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

    # The timeline, rostered then flown, each duty a block labelled with
    # its code; the line at now, the render's own; the flown side greyed
    # after it; in the FRMS focus group - flanked by the rails, in a
    # rails frame. The upload at the foot, under it - its picker carrying
    # the Backfill toggle and its confirm.
    [frame, picker] = spec["children"]
    assert spec["type"] == "panel" and picker == PICKER
    timeline = frame["timeline"]
    # No flight among them, so no trip: two empty rails, either side.
    assert frame == {"type": "rails", "timeline": timeline,
                     "left": {"type": "rail", "side": "plan", "title": "Plan", "cards": [], "sync": "frms"},
                     "right": {"type": "rail", "side": "report", "title": "Report", "cards": [], "sync": "frms"}}
    assert iso_utc(before) <= timeline["now"] <= iso_utc(after)
    assert timeline == {"type": "timeline", "title": None, "now": timeline["now"], "sync": "frms", "lanes": [
        {"label": "Roster", "grey_after_now": False, "intervals": [
            {"start": "2026-10-20T22:00:00Z", "end": "2026-10-21T10:00:00Z", "label": "R1E"},
            {"start": "2099-11-05T15:10:00Z", "end": "2099-11-06T04:10:00Z", "label": "CX9102"},
        ]},
        {"label": "Complete", "grey_after_now": True, "intervals": [
            {"start": "2026-04-15T00:00:00Z", "end": "2026-04-15T04:30:00Z", "label": "A1534"},
            {"start": "2026-10-10T01:00:00Z", "end": "2026-10-10T14:00:00Z", "label": "CX9101"},
        ]},
    ]}


def test_the_upload_is_at_the_tiles_foot_in_every_state(conn):
    # Nothing yet: the ask, then the picker.
    [ask, picker] = drawn(conn)[0]["children"]
    assert ask["type"] == "empty" and picker == PICKER
    # Duties: the timeline in its rails frame, then the picker - the
    # upload last, under it, with its Backfill toggle and the confirm it
    # asks.
    file_duty(conn, "duties_rostered", "2099-11-05T15:10:00Z", "2099-11-06T04:10:00Z", "CX9102")
    conn.commit()
    [frame, picker] = drawn(conn)[0]["children"]
    assert frame["type"] == "rails" and frame["timeline"]["type"] == "timeline" and picker == PICKER
    assert picker["toggle"] == frms.BACKFILL
    assert picker["toggle"]["confirm"].startswith("Backfill past duties into your rostered plan?")


def test_a_duty_with_no_code_is_a_block_with_no_label(conn):
    write_session(conn, "app:frms", "duties_complete", "2026-10-10T01:00:00Z", "2026-10-10T02:00:00Z", {})
    conn.commit()

    [frame, _] = drawn(conn)[0]["children"]

    assert frame["timeline"]["lanes"][1]["intervals"] == [
        {"start": "2026-10-10T01:00:00Z", "end": "2026-10-10T02:00:00Z", "label": ""},
    ]


def file_flight(conn, kind, start, end, code, route):
    """A flight duty, filed as the app files one: its route a summary's dep and arr."""
    dep, arr = route.split("-")
    write_session(conn, "app:frms", kind, start, end,
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

    [frame, _] = spec["children"]
    timeline = frame["timeline"]
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


def test_render_flanks_the_timeline_with_the_plan_and_report_rails(conn):
    # The fixture's home base is ZZA. Home before now: a trip flown out
    # and back, and a day return - out and back in one duty, its legs in
    # its title.
    file_flight(conn, "duties_complete", "2026-10-01T01:00:00Z", "2026-10-01T14:00:00Z", "CX9101", "ZZA-ZZB")
    file_flight(conn, "duties_complete", "2026-10-03T02:00:00Z", "2026-10-03T13:00:00Z", "CX9102", "ZZB-ZZA")
    write_session(conn, "app:frms", "duties_complete", "2026-10-08T01:00:00Z", "2026-10-08T09:00:00Z",
                  {"capture": "live", "type": "flight", "code": "CX9105", "flight_no": "CX9105", "dep": "ZZA",
                   "arr": "ZZA", "label": "08-Oct CX 9105 ZZA-ZZD / 08-Oct CX 9106 ZZD-ZZA", "raw": ""})
    # Under way: flown out, its way home still rostered - one trip across
    # both lanes. Then one rostered out and back, and one out the roster
    # runs out before it's home.
    file_flight(conn, "duties_complete", "2026-10-12T01:00:00Z", "2026-10-12T14:00:00Z", "CX9103", "ZZA-ZZC")
    file_flight(conn, "duties_rostered", "2099-11-01T02:00:00Z", "2099-11-01T13:00:00Z", "CX9104", "ZZC-ZZA")
    file_flight(conn, "duties_rostered", "2099-11-05T15:10:00Z", "2099-11-06T04:10:00Z", "CX9101", "ZZA-ZZB")
    file_flight(conn, "duties_rostered", "2099-11-08T02:00:00Z", "2099-11-08T13:00:00Z", "CX9102", "ZZB-ZZA")
    file_flight(conn, "duties_rostered", "2099-11-20T01:00:00Z", "2099-11-20T12:00:00Z", "CX9107", "ZZA-ZZD")
    conn.commit()

    [frame, picker] = drawn(conn)[0]["children"]

    # The timeline, carried as it's drawn alone, between the two rails -
    # both in the FRMS focus group, so they restack as it scrolls: Plan
    # looking ahead of the cursor, Report back.
    assert frame["type"] == "rails" and picker == PICKER
    assert frame["timeline"]["type"] == "timeline" and frame["timeline"]["sync"] == "frms"
    plan, report = frame["left"], frame["right"]
    assert (plan["type"], plan["side"], plan["title"], plan["sync"]) == ("rail", "plan", "Plan", "frms")
    assert (report["type"], report["side"], report["title"], report["sync"]) == ("rail", "report", "Report", "frms")

    def rail_card(start, end, out, inbound, port):
        return {"id": start, "start": start, "end": end, "out": out, "in": inbound, "port": port}

    # Plan, beside the rostered lane: every trip not over yet - the one
    # under way, then the one to come, then the one not home as far as
    # the roster runs, no flight home yet. Each its flights out and home
    # and its port, as the trip has them; its id the trip's, its band's.
    assert plan["cards"] == [
        rail_card("2026-10-12T01:00:00Z", "2099-11-01T13:00:00Z", "CX9103", "CX9104", "ZZC"),
        rail_card("2099-11-05T15:10:00Z", "2099-11-08T13:00:00Z", "CX9101", "CX9102", "ZZB"),
        rail_card("2099-11-20T01:00:00Z", None, "CX9107", None, "ZZD"),
    ]
    # Report, beside the flown: every trip home - a day return like any,
    # its port the one it turned round at.
    assert report["cards"] == [
        rail_card("2026-10-01T01:00:00Z", "2026-10-03T13:00:00Z", "CX9101", "CX9102", "ZZB"),
        rail_card("2026-10-08T01:00:00Z", "2026-10-08T09:00:00Z", "CX9105", "CX9106", "ZZD"),
    ]
    # The same trips the timeline shades, by id.
    [_, flown] = frame["timeline"]["lanes"]
    assert [b["start"] for b in flown["bands"]] == ["2026-10-01T01:00:00Z", "2026-10-12T01:00:00Z"]


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
    assert frms.classify("05-Nov QF123 ZZA-ZZB RP", other_airline) == {
        "type": "flight", "code": "QF123", "flight_no": "QF123",
        "dep": "ZZA", "arr": "ZZB", "position": "RP",
    }
    # And with no rule for it, a CX is just "other": nothing about the
    # carrier is baked into the code.
    assert frms.classify("05-Nov CX9101 ZZA-ZZB", other_airline) == {"type": "other", "code": "CX9101"}


def test_a_regex_rule_matches_and_the_first_rule_wins():
    rules = cfg([
        {"type": "sim", "regex": r"^A\d"},
        {"type": "reserve", "prefixes": ["A"]},   # also matches A1534, but sim is first
    ])
    assert frms.classify("12-Nov A1534", rules)["type"] == "sim"       # the regex, first
    assert frms.classify("12-Nov AX", rules)["type"] == "reserve"      # no digit: the prefix


def test_an_unmatched_code_takes_the_configs_default():
    assert frms.classify("13-Nov ZZZ", cfg([], default="ground")) == {"type": "ground", "code": "ZZZ"}


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
    (tmp_path / "app.py").write_text((FRMS_DIR / "app.py").read_text())
    if config_text is not None:
        (tmp_path / "config.json").write_text(config_text)
    spec = importlib.util.spec_from_file_location(f"frms_cfg_{tmp_path.name}", tmp_path / "app.py")
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

    def grid(self, cols, rows, cells):
        return {"cols": cols, "rows": rows, "cells": cells}


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
    # The tile says why, over its picker - it never reads a session - and
    # so does the circadian panel.
    assert _roster_cell(app.render(FakeCtx(), {})) == {"panel": [{"empty": app.CONFIG_ERROR}, {"input": True}]}
    assert _circadian_cell(app.render(FakeCtx(), {})) == {"empty": app.CONFIG_ERROR}
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

    [frame, _] = _roster_cell(app.render(host.Ctx(conn, today=TODAY), {}))["children"]
    timeline = frame["timeline"]

    assert [len(lane["intervals"]) for lane in timeline["lanes"]] == [0, 2]
    assert not any("bands" in lane for lane in timeline["lanes"])
    # Nor a card, on either rail.
    assert (frame["left"]["cards"], frame["right"]["cards"]) == ([], [])


def test_a_good_config_loads():
    # The fixture's own config.json, loaded as the host loads it.
    assert frms.CONFIG is not None and frms.CONFIG_ERROR is None
    assert any(r.get("type") == "flight" for r in frms.CONFIG["duty_types"])


# ---------- the circadian panel ----------
#
# The tile's top-left sub-panel: circadian() - test_frms_circadian.py's -
# over the roster panel's window, drawn as three strips in the FRMS focus
# group: where you are, its tag the port and its local time, the WOCL red
# under its curve; home, tagged Home over its date and time; the duties,
# each in pieces, its code once, on its first, and each trip away a bar
# behind them - the roster's trips(), over the duties that count. The
# fixture's config has no positions, so these load the app with one of
# the real shape - home Hong Kong, Sydney and London away - and file two
# trips about now: one flown, one still to come, so whatever the date the
# panel has a past and a future. Each duty is 14 hours - longer than any
# day, or any night outside the WOCL, at Hong Kong - so always in pieces.

PORTS = {
    "HKG": {"city": "Hong Kong", "tz": "Asia/Hong_Kong", "lat": 22.31, "lon": 113.92},
    "SYD": {"city": "Sydney", "tz": "Australia/Sydney", "lat": -33.95, "lon": 151.18},
    "LHR": {"city": "London", "tz": "Europe/London", "lat": 51.47, "lon": -0.46},
}
REAL_CONFIG = {
    "personal": {"home_base": "HKG", "timezone": "Asia/Hong_Kong", "cbt_min": "04:00"},
    "duty_types": [{"type": "flight", "prefixes": ["CX"]}, {"type": "sim", "regex": r"^A\d"},
                   {"type": "reserve", "prefixes": ["R"]}],
    "default_type": "other",
    "destinations": PORTS,
}


def real_app(tmp_path, config=REAL_CONFIG):
    return load_with_config(tmp_path, json.dumps(config))


def file_trips(conn):
    """Flown to Sydney and home, over three and two days ago; rostered to
    London and home, from tomorrow - each duty 14 hours, from the top of
    an hour. Its duties: [(code, start, end)], in order."""
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    filed = []
    for kind, at, code, route in (
        ("duties_complete", -80, "CX101", "HKG-SYD"), ("duties_complete", -40, "CX100", "SYD-HKG"),
        ("duties_rostered", 20, "CX255", "HKG-LHR"), ("duties_rostered", 70, "CX256", "LHR-HKG"),
    ):
        start, end = iso_utc(base + timedelta(hours=at)), iso_utc(base + timedelta(hours=at + 14))
        file_flight(conn, kind, start, end, code, route)
        filed.append((code, start, end))
    conn.commit()
    return filed


def spans_of(spans):
    return [{k: s[k] for k in ("start", "end", "tone")} for s in spans]


def test_the_circadian_panel_is_three_strips_in_the_frms_group(tmp_path, conn):
    app = real_app(tmp_path)
    filed = file_trips(conn)

    before = datetime.now(timezone.utc).replace(microsecond=0)
    spec = app.render(host.Ctx(conn), {})
    after = datetime.now(timezone.utc)
    strips = _circadian_cell(spec)

    # Three strips - where you are, its tag the place and its time; home,
    # tagged Home over its date and time; the duties - with the line at
    # now, the render's own, in the FRMS focus group: it scrolls with the
    # roster's timeline and rails.
    assert (strips["type"], strips["sync"]) == ("strips", "frms")
    assert iso_utc(before) <= strips["now"] <= iso_utc(after)
    local, home, duties = strips["lanes"]
    assert [(lane["kind"], lane["label"], lane["clock"]) for lane in strips["lanes"]] == [
        ("sky", None, "place"), ("sky", "Home", "date"), ("duty", None, False),
    ]

    # circadian()'s, for the same duties, now and window - the roster
    # panel's, about six months back from midnight, to a day past the
    # last duty's end.
    ctx = host.Ctx(conn)
    since = date.today() - timedelta(days=183)
    rostered = ctx.sessions("duties_rostered", since=since.isoformat())
    flown = ctx.sessions("duties_complete", since=since.isoformat())
    start = datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc)
    end = datetime.fromisoformat(filed[-1][2]) + timedelta(days=1)
    engine = app.circadian(rostered, flown, datetime.fromisoformat(strips["now"]), start, end)
    assert spans_of(local["spans"]) == spans_of(engine["location"])
    assert spans_of(home["spans"]) == spans_of(engine["body"])
    assert spans_of(duties["spans"]) == spans_of(engine["duties"])
    assert local["troughs"] == engine["troughs"] and home["troughs"] == duties["troughs"] == []
    # The WOCL under the curve, the engine's; none on the other lanes.
    assert local["wocl"] == engine["wocl"] and engine["wocl"]
    assert home["wocl"] == duties["wocl"] == [] and local["bands"] == home["bands"] == []
    assert (local["spans"][0]["start"], local["spans"][-1]["end"]) == (iso_utc(start), iso_utc(end))
    assert (home["spans"][0]["start"], home["spans"][-1]["end"]) == (iso_utc(start), iso_utc(end))

    # The clock lane: each span the port the engine has you at - "" for
    # not known - and that port's zone, for the time under the cursor.
    assert [s["place"] for s in local["spans"]] == [x["port"] or "" for x in engine["location"]]
    assert {(s["place"], s["tz"]) for s in local["spans"]} == {
        ("", None), ("SYD", "Australia/Sydney"), ("HKG", "Asia/Hong_Kong"), ("LHR", "Europe/London"),
    }
    assert local["spans"][0]["tone"] == "none"
    # The roster's panel beside it, drawn as ever.
    assert _roster_cell(spec)["children"][0]["type"] == "rails"


def test_a_duty_is_labelled_once_on_its_first_piece(tmp_path, conn):
    app = real_app(tmp_path)
    filed = file_trips(conn)

    [_, _, duties] = _circadian_cell(app.render(host.Ctx(conn), {}))["lanes"]

    # Every duty in pieces, its code on the first and on no other - one
    # label a duty, for the page to draw once at its start: each a
    # flight, its start its pre-duty time, two hours before its report.
    for code, start, end in filed:
        start = iso_utc(datetime.fromisoformat(start) - timedelta(hours=2))
        pieces = [s for s in duties["spans"] if start <= s["start"] < end]
        assert len(pieces) > 1, code
        assert pieces[0]["start"] == start
        assert [p["label"] for p in pieces] == [code] + [None] * (len(pieces) - 1)
    assert [s["label"] for s in duties["spans"] if s["label"]] == [code for code, _, _ in filed]


def test_labels_and_places_are_mapped_from_the_engine_never_passed_through(tmp_path, conn, monkeypatch):
    # The engine's output, as given - a duty clipped at the window's
    # start, a code flown twice, a duty with no code; ports known, not
    # known, and with a destination that isn't one.
    places = {**PORTS, "MNL": "Manila", "BKK": {"city": "Bangkok", "tz": 7}, "CEB": {"city": "Cebu", "tz": ""}}
    app = real_app(tmp_path, {**REAL_CONFIG, "destinations": places})
    file_trips(conn)

    def piece(start, end, code, tone):
        return {"start": f"2026-10-{start}:00:00Z", "end": f"2026-10-{end}:00:00Z", "code": code, "tone": tone}

    def where(start, end, port, tone="day"):
        return {"start": f"2026-10-{start}:00:00Z", "end": f"2026-10-{end}:00:00Z", "port": port, "tone": tone}

    engine = {
        "home": "HKG", "wocl": [], "troughs": ["2026-10-20T20:00:00Z", "2026-10-21T20:00:00Z"],
        "body": [{"start": "2026-10-20T00:00:00Z", "end": "2026-10-23T00:00:00Z", "tone": "day"}],
        "location": [where("20T00", "20T06", None, "none"), where("20T06", "21T00", "SYD"),
                     where("21T00", "21T06", "XXX", "none"), where("21T06", "21T12", "MNL"),
                     where("21T12", "21T18", "BKK"), where("21T18", "22T00", "CEB"), where("22T00", "23T00", "LHR")],
        "duties": [
            piece("20T00", "20T02", "CX9", "wocl"),     # its start before the window's: the first in it labelled
            piece("20T02", "20T05", "CX9", "day"),
            piece("21T01", "21T03", "CX9", "day"),      # the same code another day: its own duty
            piece("21T08", "21T09", "R1E", "night"),
            piece("21T09", "21T10", "R1E", "wocl"),
            piece("22T01", "22T02", "", "day"),         # no code: nothing to draw
        ],
    }
    monkeypatch.setattr(app, "circadian", lambda *a, **k: engine)

    local, home, duties = _circadian_cell(app.render(host.Ctx(conn), {}))["lanes"]

    assert [s["label"] for s in duties["spans"]] == ["CX9", None, "CX9", "R1E", None, ""]
    assert spans_of(duties["spans"]) == spans_of(engine["duties"])
    # A port's place and zone - none for one not known, one not among the
    # destinations, or one whose zone isn't text.
    assert [(s["place"], s["tz"]) for s in local["spans"]] == [
        ("", None), ("SYD", "Australia/Sydney"), ("XXX", None), ("MNL", None), ("BKK", None), ("CEB", None),
        ("LHR", "Europe/London"),
    ]
    # Home's spans home base and its zone, from the config - the date
    # clock's - whatever the engine says of where you are.
    assert home["spans"] == [{"start": "2026-10-20T00:00:00Z", "end": "2026-10-23T00:00:00Z", "tone": "day",
                              "label": None, "place": "HKG", "tz": "Asia/Hong_Kong"}]
    assert local["wocl"] == engine["wocl"] == []


def counted_trips(app, conn, end):
    """trips() over the duties the circadian panel counts - the flown
    over by now, the rostered ending after it - as its bands, `end` the
    open trip's."""
    ctx = host.Ctx(conn)
    since = (date.today() - timedelta(days=183)).isoformat()
    counted = app._counted(ctx.sessions("duties_rostered", since=since), ctx.sessions("duties_complete", since=since),
                           datetime.now(timezone.utc))
    return [{"start": t["start"], "end": t["end"], "label": t["out_port"]} for t in app.trips(counted, "HKG", end)]


def test_the_duty_strips_bands_are_the_rosters_trips_and_home_tells_its_date(tmp_path, conn):
    app = real_app(tmp_path)
    filed = file_trips(conn)
    # A day return, rostered after the London trip: base to base, no trip.
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    day = (iso_utc(base + timedelta(hours=100)), iso_utc(base + timedelta(hours=110)))
    file_flight(conn, "duties_rostered", *day, "CX903", "HKG-HKG")
    conn.commit()

    local, home, duties = _circadian_cell(app.render(host.Ctx(conn), {}))["lanes"]

    # Each trip away a band - out of base at the flight out's start, home
    # at the flight home's end - labelled with its port: Sydney, flown,
    # and London, rostered. The day return none.
    (_, out_syd, _), (_, _, home_syd), (_, out_lhr, _), (_, _, home_lhr) = filed
    assert duties["bands"] == [
        {"start": out_syd, "end": home_syd, "label": "SYD"},
        {"start": out_lhr, "end": home_lhr, "label": "LHR"},
    ]
    # Exactly the roster's trips() over the duties that count.
    assert duties["bands"] == counted_trips(app, conn, None)
    # The day return is still drawn, as a duty.
    assert "CX903" in [s["label"] for s in duties["spans"]]
    # Home: tagged Home over its date and time, every span home base in
    # its zone; the WOCL, circadian()'s, under the curve of the first.
    assert (home["label"], home["clock"]) == ("Home", "date")
    assert {(s["place"], s["tz"]) for s in home["spans"]} == {("HKG", "Asia/Hong_Kong")}
    ctx = host.Ctx(conn)
    since = date.today() - timedelta(days=183)
    rostered = ctx.sessions("duties_rostered", since=since.isoformat())
    flown = ctx.sessions("duties_complete", since=since.isoformat())
    start = datetime.combine(since, datetime.min.time(), tzinfo=timezone.utc)
    end = datetime.fromisoformat(day[1]) + timedelta(days=1)
    engine = app.circadian(rostered, flown, datetime.now(timezone.utc), start, end)
    assert local["wocl"] == engine["wocl"]


def test_a_trip_under_way_is_one_band_and_one_still_away_runs_to_the_windows_end(tmp_path, conn):
    app = real_app(tmp_path)
    base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    at = lambda hours: iso_utc(base + timedelta(hours=hours))
    # Out to London, flown; home, still rostered: under way, one trip.
    # Then out to Sydney, rostered, and the roster ends before it's home.
    # A past rostered copy of the flight out - a backfill - doesn't count.
    file_flight(conn, "duties_complete", at(-30), at(-16), "CX255", "HKG-LHR")
    file_flight(conn, "duties_rostered", at(-30), at(-16), "CX255", "HKG-LHR")
    file_flight(conn, "duties_rostered", at(20), at(34), "CX256", "LHR-HKG")
    file_flight(conn, "duties_rostered", at(60), at(69), "CX101", "HKG-SYD")
    conn.commit()

    [_, _, duties] = _circadian_cell(app.render(host.Ctx(conn), {}))["lanes"]

    # The window's end: a day past the last duty's.
    end = iso_utc(base + timedelta(hours=69, days=1))
    assert duties["bands"] == [
        {"start": at(-30), "end": at(34), "label": "LHR"},
        {"start": at(60), "end": end, "label": "SYD"},
    ]
    assert duties["bands"] == counted_trips(app, conn, end)


def test_a_panel_that_fails_is_an_error_in_its_own_sub_panel(tmp_path, conn, monkeypatch, capsys):
    app = real_app(tmp_path)
    file_trips(conn)
    circadian = app._circadian

    def fails(ctx):
        raise RuntimeError("the sun didn't rise")

    # The circadian panel raising: an error in its sub-panel, logged with
    # its traceback - and the roster's panel drawn as ever.
    fails.__name__ = "_circadian"
    monkeypatch.setattr(app, "_circadian", fails)
    spec = app.render(host.Ctx(conn), {})
    assert _circadian_cell(spec) == {"type": "error", "message": "the sun didn't rise"}
    assert _roster_cell(spec)["children"][0]["type"] == "rails"
    logged = capsys.readouterr()
    assert "[apps] frms: the circadian panel failed" in logged.out
    assert "RuntimeError: the sun didn't rise" in logged.err
    # The other way about: the roster's panel raising leaves the circadian
    # drawn. An exception that says nothing is named by its type.
    def divides(ctx):
        return 1 / 0

    monkeypatch.setattr(app, "_circadian", circadian)
    monkeypatch.setattr(app, "_roster", divides)
    spec = app.render(host.Ctx(conn), {})
    assert _roster_cell(spec) == {"type": "error", "message": "division by zero"}
    assert _circadian_cell(spec)["type"] == "strips"

    def silent(ctx):
        raise KeyError

    monkeypatch.setattr(app, "_roster", silent)
    assert _roster_cell(app.render(host.Ctx(conn), {})) == {"type": "error", "message": "KeyError"}
    # The sleep panel and the rest budget, either way, as they were.
    [sleep, budget] = [c["spec"] for c in spec["cells"] if (c["x"], c["y"]) in ((3, 0), (2, 2))]
    assert sleep == {"type": "empty", "message": "No sleep stages yet - sync a sleep source that gives them"}
    assert budget["type"] == "row"


# ---------- trips: the layovers ----------
#
# trips() on its own: a lane's duty sessions, as ctx.sessions() gives
# them, grouped into trips away from a home base - here HKG - by their
# flights alone: a flight out of base opens one, the next flight back
# into base closes it, sectors between are inside it.

HOME = "HKG"


def flight(route, start, end, code="CX100"):
    dep, arr = route.split("-")
    return host.Session(start, end, "app:frms", {"type": "flight", "code": code, "dep": dep, "arr": arr})


def duty(kind, start, end, code, **summary):
    return host.Session(start, end, "app:frms", {"type": kind, "code": code, **summary})


def trip(start, end, out_port):
    return {"id": start, "start": start, "end": end, "out_port": out_port}


def test_a_simple_trip_is_one_band():
    out = flight("HKG-MEL", "2026-11-05T15:10:00Z", "2026-11-06T04:10:00Z", "CX105")
    back = flight("MEL-HKG", "2026-11-08T02:00:00Z", "2026-11-08T13:00:00Z", "CX104")

    # From the flight out's start to the flight home's end; the port it
    # flew out to. Taken by start, whatever order they're given in.
    assert frms.trips([back, out], HOME) == [trip("2026-11-05T15:10:00Z", "2026-11-08T13:00:00Z", "MEL")]


def test_an_integrated_pattern_is_one_band():
    sectors = [
        flight("HKG-BKK", "2026-11-05T01:00:00Z", "2026-11-05T04:00:00Z"),
        flight("BKK-SIN", "2026-11-06T03:00:00Z", "2026-11-06T05:30:00Z"),
        flight("SIN-HKG", "2026-11-07T06:00:00Z", "2026-11-07T10:00:00Z"),
    ]

    # One trip - the whole time away - not one a sector: BKK-SIN is inside
    # it, and the first port is the one it's labelled with.
    assert frms.trips(sectors, HOME) == [trip("2026-11-05T01:00:00Z", "2026-11-07T10:00:00Z", "BKK")]


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
    assert frms.trips(sessions, HOME, open_end="2026-11-30T00:00:00Z") == [
        trip("2026-11-01T15:00:00Z", "2026-11-04T13:00:00Z", "MEL"),
        trip("2026-11-10T01:00:00Z", "2026-11-11T08:00:00Z", "BKK"),
    ]


def test_an_open_rostered_trip_runs_to_the_rosters_end():
    out = flight("HKG-MEL", "2026-11-20T15:00:00Z", "2026-11-21T04:00:00Z")
    reserve = duty("reserve", "2026-11-24T22:00:00Z", "2026-11-25T10:00:00Z", "R1E")

    # Out, and the roster runs out before the flight home: the trip ends
    # where the roster does.
    assert frms.trips([out, reserve], HOME, open_end="2026-11-25T10:00:00Z") == [
        trip("2026-11-20T15:00:00Z", "2026-11-25T10:00:00Z", "MEL"),
    ]
    # With nowhere to end it, it's dropped: a band needs an end.
    assert frms.trips([out, reserve], HOME) == []
    # And never before its flight out has landed - the flight out the
    # roster's last word, an end before that is the flight's.
    assert frms.trips([out], HOME, open_end="2026-11-20T15:00:00Z") == [
        trip("2026-11-20T15:00:00Z", "2026-11-21T04:00:00Z", "MEL"),
    ]


def test_an_open_complete_trip_runs_to_now():
    now = "2026-11-06T09:30:00Z"
    flown = [
        flight("HKG-BKK", "2026-11-05T01:00:00Z", "2026-11-05T04:00:00Z"),
        flight("BKK-SIN", "2026-11-06T03:00:00Z", "2026-11-06T05:30:00Z"),
    ]

    # Flown out - and on, a sector - and not home yet: away until now.
    assert frms.trips(flown, HOME, open_end=now) == [trip("2026-11-05T01:00:00Z", now, "BKK")]


def test_back_to_back_trips_are_two_bands():
    sessions = [
        flight("HKG-MEL", "2026-11-01T15:00:00Z", "2026-11-02T04:00:00Z"),
        flight("MEL-HKG", "2026-11-04T02:00:00Z", "2026-11-04T13:00:00Z"),
        # Out again the next morning.
        flight("HKG-BKK", "2026-11-05T01:00:00Z", "2026-11-05T04:00:00Z"),
        flight("BKK-HKG", "2026-11-06T05:00:00Z", "2026-11-06T08:00:00Z"),
    ]

    assert frms.trips(sessions, HOME) == [
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
    assert frms.trips(out_unknown, HOME) == [trip("2026-11-10T01:00:00Z", "2026-11-11T08:00:00Z", "BKK")]
    # Its flight home with no route: that trip never closes - the next
    # flight out of base shows it was home by then, so it's dropped, not
    # paired with the next trip's flight home.
    back_unknown = [flight("HKG-MEL", "2026-11-01T15:00:00Z", "2026-11-02T04:00:00Z"),
                    routeless("2026-11-04T02:00:00Z", "2026-11-04T13:00:00Z"), *bkk]
    assert frms.trips(back_unknown, HOME) == [trip("2026-11-10T01:00:00Z", "2026-11-11T08:00:00Z", "BKK")]
    # A sector with no route, inside a trip: nothing changes.
    inside = [bkk[0], routeless("2026-11-10T20:00:00Z", "2026-11-10T22:00:00Z"), bkk[1]]
    assert frms.trips(inside, HOME) == [trip("2026-11-10T01:00:00Z", "2026-11-11T08:00:00Z", "BKK")]
    # A flight that's another source's, still open - no end - is no
    # boundary either.
    assert frms.trips([bkk[0], bkk[1]._replace(end=None)], HOME) == []


def test_a_flight_from_base_to_base_is_no_trip():
    # Out and straight back - a turnback, a local training flight - never
    # leaves base: no trip, and nothing left open for a later flight home.
    sessions = [flight("HKG-HKG", "2026-11-01T01:00:00Z", "2026-11-01T03:00:00Z"),
                flight("MEL-HKG", "2026-11-04T02:00:00Z", "2026-11-04T13:00:00Z")]

    assert frms.trips(sessions, HOME, open_end="2026-11-30T00:00:00Z") == []


def test_the_trip_id_is_the_outbound_flight_start():
    out = flight("HKG-MEL", "2026-11-05T15:10:00Z", "2026-11-06T04:10:00Z")
    back = flight("MEL-HKG", "2026-11-08T02:00:00Z", "2026-11-08T13:00:00Z")

    [found] = frms.trips([out, back], HOME)
    assert found["id"] == out.start == "2026-11-05T15:10:00Z"
    # The flight home re-planned, a day later: the same trip - the same
    # id - longer.
    [replanned] = frms.trips([out, back._replace(start="2026-11-09T02:00:00Z", end="2026-11-09T13:00:00Z")], HOME)
    assert (replanned["id"], replanned["end"]) == (found["id"], "2026-11-09T13:00:00Z")
    # Not drawn: a band is its span and its port, nothing more.
    assert frms._band(found) == {"start": "2026-11-05T15:10:00Z", "end": "2026-11-08T13:00:00Z", "label": "MEL"}


# ---------- a duty's route: its legs, first to last ----------
#
# One duty can fly more than one sector - a day turn out and back, or on
# from one port to another - and the roster lists every leg: in its
# description, a line a sector, and in its title. A flight's dep and arr
# are the duty's origin and final destination - the first leg's
# departure, the last leg's arrival - so trips() sees where it started
# and where it ended, not just its first sector. A Cathay-style day turn,
# its shape as exported (the date and times made up):

TURN_TITLE = "20-Nov CX 903 HKG-MNL / 20-Nov CX 902 MNL-HKG"
TURN_DESCRIPTION = (
    "DUTY START: 20-Nov 07:25\n"
    "20-Nov CX 903 HKG-MNL  35G 08:35 - 10:45\n"
    "20-Nov CX 902 MNL-HKG  35G 12:05 - 14:30 TOTAL: 07:35 \n"
    "DUTY END: 20-Nov 15:00\n"
)


def filed(title, description, start="20261119T232500Z", end="20261120T070000Z"):
    """The one duty of an .ics holding just this event, as duties() reads it."""
    [found] = frms.duties(frms.parse_events(ics(timed(title, start, end, description.replace("\n", "\\n")))))
    return found


def as_session(d):
    """A duty as ctx.sessions() gives it back once filed."""
    return host.Session(iso_utc(d.start), iso_utc(d.end), "app:frms", frms.session_summary(d))


def test_a_day_turn_is_not_a_layover():
    # Out to MNL and back to base, one duty: base to base.
    assert frms.classify(TURN_TITLE, description=TURN_DESCRIPTION) == {
        "type": "flight", "code": "CX903", "flight_no": "CX903", "dep": "HKG", "arr": "HKG",
    }
    turn = filed(TURN_TITLE, TURN_DESCRIPTION)
    assert (turn.dep, turn.arr) == ("HKG", "HKG")

    # The last duty flown, no flight home after it: still no trip - not a
    # layover in MNL running to now.
    now = "2026-11-26T00:00:00Z"
    assert frms.trips([as_session(turn)], HOME, open_end=now) == []
    # And a real trip before it is still its one band.
    out = flight("HKG-BLR", "2026-11-10T15:00:00Z", "2026-11-11T00:30:00Z")
    back = flight("BLR-HKG", "2026-11-13T19:00:00Z", "2026-11-14T03:00:00Z")
    assert frms.trips([out, back, as_session(turn)], HOME, open_end=now) == [
        trip("2026-11-10T15:00:00Z", "2026-11-14T03:00:00Z", "BLR"),
    ]


def test_the_leg_chain_is_read_from_the_description():
    # The title shows only the way out; the description lists both legs,
    # and wins: the duty ends back at base.
    found = frms.classify("20-Nov CX 903 HKG-MNL", description=TURN_DESCRIPTION)
    assert (found["dep"], found["arr"]) == ("HKG", "HKG")
    # duties() hands classify() the event's description.
    turn = filed("20-Nov CX 903 HKG-MNL", TURN_DESCRIPTION)
    assert (turn.dep, turn.arr) == ("HKG", "HKG")


def test_without_a_description_the_title_is_the_fallback():
    found = frms.classify("12-Oct CX 903 HKG-MNL / 12-Oct CX 902 MNL-HKG")
    assert (found["dep"], found["arr"]) == ("HKG", "HKG")
    # A description with no legs in it falls back to the title too.
    found = frms.classify(TURN_TITLE, description="DUTY START: 20-Nov 07:25\nDUTY END: 20-Nov 15:00")
    assert (found["dep"], found["arr"]) == ("HKG", "HKG")


def test_a_one_way_duty_keeps_origin_and_destination():
    title = "20-Nov CX 903 HKG-MNL / 20-Nov CX 9031 MNL-CEB"
    description = (
        "DUTY START: 20-Nov 07:25\n"
        "20-Nov CX 903 HKG-MNL  35G 08:35 - 10:45\n"
        "20-Nov CX 9031 MNL-CEB  35G 12:05 - 13:30 TOTAL: 06:35 \n"
        "DUTY END: 20-Nov 14:00\n"
    )

    # Out of base and on, ending away: from HKG - the first leg's origin,
    # not the last's - to CEB.
    assert frms.classify(title, description=description) == {
        "type": "flight", "code": "CX903", "flight_no": "CX903", "dep": "HKG", "arr": "CEB",
    }
    # So it still opens a real trip, to where the duty ended.
    one_way = filed(title, description, end="20261120T060000Z")
    assert frms.trips([as_session(one_way)], HOME, open_end="2026-11-22T00:00:00Z") == [
        trip("2026-11-19T23:25:00Z", "2026-11-22T00:00:00Z", "CEB"),
    ]


# ---------- trip cards: the Plan and Report rails ----------
#
# trip_cards() on its own: both lanes' duty sessions, as ctx.sessions()
# gives them, as the rails' cards - a whole trip out of base and back
# each, a day return included - here from HKG. render() splits them into
# the Plan and Report rails (above).


def card(start, end, out, inbound, port, day=False):
    return {"id": start, "start": start, "end": end, "out": out, "in": inbound, "port": port, "day": day}


def test_an_out_and_back_trip_is_one_card():
    out = flight("HKG-MEL", "2026-11-05T15:10:00Z", "2026-11-06T04:10:00Z", "CX105")
    back = flight("MEL-HKG", "2026-11-08T02:00:00Z", "2026-11-08T13:00:00Z", "CX104")

    # From the flight out's start to the flight home's end: both flight
    # numbers, and the port away. Taken by start, whatever the order.
    assert frms.trip_cards([back, out], [], HOME) == [
        card("2026-11-05T15:10:00Z", "2026-11-08T13:00:00Z", "CX105", "CX104", "MEL"),
    ]
    # Flown, the same card.
    assert frms.trip_cards([], [back, out], HOME) == frms.trip_cards([out, back], [], HOME)


def test_an_integrated_pattern_is_one_card():
    sectors = [
        flight("HKG-BKK", "2026-11-05T01:00:00Z", "2026-11-05T04:00:00Z", "CX701"),
        flight("BKK-SIN", "2026-11-06T03:00:00Z", "2026-11-06T05:30:00Z", "CX711"),
        flight("SIN-HKG", "2026-11-07T06:00:00Z", "2026-11-07T10:00:00Z", "CX730"),
    ]

    # Out on the first, home on the last; the sector between is inside it,
    # and the port is the first away - the band's label.
    assert frms.trip_cards(sectors, [], HOME) == [
        card("2026-11-05T01:00:00Z", "2026-11-07T10:00:00Z", "CX701", "CX730", "BKK"),
    ]


def test_a_trips_flight_home_is_the_leg_that_lands_at_base():
    # Out to MNL; home a day later on one duty of two sectors, CEB-MNL
    # then MNL-HKG, stored under its first sector's code.
    out = flight("HKG-MNL", "2026-11-05T01:00:00Z", "2026-11-05T04:00:00Z", "CX907")
    home = duty("flight", "2026-11-07T01:00:00Z", "2026-11-07T09:00:00Z", "CX913", dep="CEB", arr="HKG",
                label="07-Nov CX 913 CEB-MNL / 07-Nov CX 906 MNL-HKG",
                raw="07-Nov CX 913 CEB-MNL  33G 09:00 - 10:30\n07-Nov CX 906 MNL-HKG  33G 12:00 - 14:00\n")

    # In is the flight that lands at base - its last leg's number, read
    # back from the description - not the duty's code.
    assert frms.trip_cards([out, home], [], HOME) == [
        card("2026-11-05T01:00:00Z", "2026-11-07T09:00:00Z", "CX907", "CX906", "MNL"),
    ]
    # Its title's legs, with no description to read.
    titled = home._replace(summary={**home.summary, "raw": ""})
    assert frms.trip_cards([out, titled], [], HOME)[0]["in"] == "CX906"
    # Legs with no flight numbers, or none to read: the duty's code.
    bare = home._replace(summary={**home.summary, "label": "07-Nov CX913", "raw": "CEB-MNL\nMNL-HKG"})
    assert frms.trip_cards([out, bare], [], HOME)[0]["in"] == "CX913"
    unread = home._replace(summary={"type": "flight", "code": "CX913", "dep": "CEB", "arr": "HKG"})
    assert frms.trip_cards([out, unread], [], HOME)[0]["in"] == "CX913"


def test_a_day_return_is_a_card_of_its_own():
    # Out to MNL and back in one duty, stored base to base under its first
    # sector's code: no band, but a trip - one with no layover.
    turn = as_session(filed(TURN_TITLE, TURN_DESCRIPTION))
    assert (turn.summary["code"], turn.summary["dep"], turn.summary["arr"]) == ("CX903", "HKG", "HKG")

    # Its flight home and the port it turned round at, read back from the
    # description it was stored with - nothing new stored, nothing uploaded
    # again.
    assert frms.trip_cards([], [turn], HOME) == [
        card("2026-11-19T23:25:00Z", "2026-11-20T07:00:00Z", "CX903", "CX902", "MNL", day=True),
    ]
    assert frms.trips([turn], HOME, open_end="2026-11-26T00:00:00Z") == []


def test_a_day_returns_legs_fall_back_to_its_title():
    # A description with no legs: the title's, as its route was read.
    turn = as_session(filed(TURN_TITLE, "DUTY START: 20-Nov 07:25\nDUTY END: 20-Nov 15:00"))
    [found] = frms.trip_cards([turn], [], HOME)
    assert (found["out"], found["in"], found["port"], found["day"]) == ("CX903", "CX902", "MNL", True)

    # A flight number joined to its carrier reads the same.
    joined = duty("flight", "2026-11-20T01:00:00Z", "2026-11-20T09:00:00Z", "CX9101", dep="HKG", arr="HKG",
                  label="20-Nov CX9101 HKG-ZZB / 20-Nov CX9102 ZZB-HKG", raw="")
    [found] = frms.trip_cards([joined], [], HOME)
    assert (found["out"], found["in"], found["port"]) == ("CX9101", "CX9102", "ZZB")

    # Legs with no flight numbers: the port still, but no flight home to name.
    bare = duty("flight", "2026-11-20T01:00:00Z", "2026-11-20T09:00:00Z", "CX903", dep="HKG", arr="HKG",
                label="20-Nov CX903", raw="HKG-MNL\nMNL-HKG")
    [found] = frms.trip_cards([bare], [], HOME)
    assert (found["out"], found["in"], found["port"]) == ("CX903", None, "MNL")


def test_a_duty_that_never_left_base_is_no_card():
    # A turnback, a local training flight: base to base, no port away.
    turnback = duty("flight", "2026-11-03T01:00:00Z", "2026-11-03T03:00:00Z", "CX100", dep="HKG", arr="HKG",
                    label="03-Nov CX 100 HKG-HKG", raw="")
    assert frms.trip_cards([turnback], [], HOME) == []
    # Nor one whose legs can't be read back - another source's, with no
    # description or title: no port to turn round at.
    unread = duty("flight", "2026-11-03T01:00:00Z", "2026-11-03T03:00:00Z", "CX100", dep="HKG", arr="HKG")
    assert frms.trip_cards([unread], [], HOME) == []

    # And a trip open before it lost its way home, as trips() has it:
    # dropped, the flight home after it closing nothing.
    out = flight("HKG-MEL", "2026-11-01T15:00:00Z", "2026-11-02T04:00:00Z")
    back = flight("MEL-HKG", "2026-11-04T02:00:00Z", "2026-11-04T13:00:00Z")
    assert frms.trip_cards([out, turnback, back], [], HOME) == []
    assert frms.trips([out, turnback, back], HOME) == []


def test_a_trip_under_way_is_read_across_both_lanes():
    out = flight("HKG-MEL", "2026-11-05T15:10:00Z", "2026-11-06T04:10:00Z", "CX105")
    back = flight("MEL-HKG", "2026-11-08T02:00:00Z", "2026-11-08T13:00:00Z", "CX104")
    whole = [card("2026-11-05T15:10:00Z", "2026-11-08T13:00:00Z", "CX105", "CX104", "MEL")]

    # Flown out - filed complete - and still rostered home: one card, whole.
    assert frms.trip_cards([back], [out], HOME) == whole
    # The flight out in the plan too - uploaded before it flew, or
    # backfilled - is the same duty, taken once; and so for a whole trip
    # in both lanes.
    assert frms.trip_cards([out, back], [out], HOME) == whole
    assert frms.trip_cards([out, back], [out, back], HOME) == whole


def test_a_duty_in_both_lanes_is_one_card():
    # A day return flown, and backfilled into the plan: the same start,
    # end, code and route - its capture apart - so one card, not two.
    flown = as_session(filed(TURN_TITLE, TURN_DESCRIPTION))
    planned = flown._replace(summary={**flown.summary, "capture": "backfill"})

    assert frms.trip_cards([planned], [flown], HOME) == frms.trip_cards([], [flown], HOME)
    assert len(frms.trip_cards([planned], [flown], HOME)) == 1


def test_the_latest_trip_still_open_is_a_card_with_no_way_home_yet():
    out = flight("HKG-MEL", "2026-11-20T15:00:00Z", "2026-11-21T04:00:00Z", "CX105")
    reserve = duty("reserve", "2026-11-24T22:00:00Z", "2026-11-25T10:00:00Z", "R1E")

    # Out, and the roster runs out before the flight home: a card still -
    # one to plan for - its end and its flight home not known yet.
    assert frms.trip_cards([out, reserve], [], HOME) == [
        card("2026-11-20T15:00:00Z", None, "CX105", None, "MEL"),
    ]

    # One a later flight out of base overtakes lost its way home: dropped.
    bkk = [flight("HKG-BKK", "2026-11-26T01:00:00Z", "2026-11-26T04:00:00Z", "CX701"),
           flight("BKK-HKG", "2026-11-27T05:00:00Z", "2026-11-27T08:00:00Z", "CX700")]
    bkk_card = card("2026-11-26T01:00:00Z", "2026-11-27T08:00:00Z", "CX701", "CX700", "BKK")
    assert frms.trip_cards([out, *bkk], [], HOME) == [bkk_card]
    # A flight into base with none open - out before the data starts - is
    # nothing.
    early_back = flight("MEL-HKG", "2026-11-04T02:00:00Z", "2026-11-04T13:00:00Z")
    assert frms.trip_cards([early_back], bkk, HOME) == [bkk_card]


def test_only_flights_with_a_route_and_an_end_make_a_card():
    sessions = [
        flight("HKG-MEL", "2026-11-01T15:00:00Z", "2026-11-02T04:00:00Z", "CX105"),
        duty("reserve", "2026-11-02T22:00:00Z", "2026-11-03T10:00:00Z", "R1E"),
        flight("MEL-HKG", "2026-11-04T02:00:00Z", "2026-11-04T13:00:00Z", "CX104"),
        duty("sim", "2026-11-07T01:00:00Z", "2026-11-07T05:30:00Z", "A1534"),
        # Out of base and back by road: a route, but not a flight.
        duty("ground", "2026-11-08T01:00:00Z", "2026-11-08T03:00:00Z", "BUS", dep="HKG", arr="SZX"),
        duty("ground", "2026-11-08T09:00:00Z", "2026-11-08T11:00:00Z", "BUS", dep="SZX", arr="HKG"),
        # A flight whose title gave no route, and another source's still open.
        duty("flight", "2026-11-09T01:00:00Z", "2026-11-09T04:00:00Z", "CX103"),
        flight("HKG-BKK", "2026-11-10T01:00:00Z", None),
    ]

    assert frms.trip_cards(sessions, [], HOME) == [
        card("2026-11-01T15:00:00Z", "2026-11-04T13:00:00Z", "CX105", "CX104", "MEL"),
    ]


def test_cards_are_in_order_and_a_cards_id_is_its_bands():
    mel = [flight("HKG-MEL", "2026-11-10T15:00:00Z", "2026-11-11T04:00:00Z", "CX105"),
           flight("MEL-HKG", "2026-11-13T02:00:00Z", "2026-11-13T13:00:00Z", "CX104")]
    turn = as_session(filed(TURN_TITLE, TURN_DESCRIPTION))
    bkk = [flight("HKG-BKK", "2026-11-22T01:00:00Z", "2026-11-22T04:00:00Z", "CX701"),
           flight("BKK-HKG", "2026-11-23T05:00:00Z", "2026-11-23T08:00:00Z", "CX700")]

    # Across both lanes, in any order: by start, a day return among them.
    cards = frms.trip_cards([bkk[1], turn, mel[0]], [bkk[0], mel[1]], HOME)
    assert [(c["start"], c["port"], c["day"]) for c in cards] == [
        ("2026-11-10T15:00:00Z", "MEL", False),
        ("2026-11-19T23:25:00Z", "MNL", True),
        ("2026-11-22T01:00:00Z", "BKK", False),
    ]
    # The same trip on a rail and on the timeline has one id - the flight
    # out's start - for a report to re-attach to either.
    bands = frms.trips([*mel, turn, *bkk], HOME)
    assert [c["id"] for c in cards if not c["day"]] == [t["id"] for t in bands]


def test_the_sample_rosters_trip_is_one_card():
    # The synthetic roster end to end: parsed, filed as its sessions would
    # be, and carded from the config's home base. One trip, ZZA to ZZB and
    # back; the reserve and the sim aren't trips.
    found = frms.duties(frms.parse_events(sample()))
    sessions = [as_session(d) for d in found]

    assert frms.trip_cards([], sessions, frms._home_base()) == [
        card("2025-11-05T15:10:00Z", "2025-11-08T13:00:00Z", "CX9101", "CX9102", "ZZB"),
    ]
