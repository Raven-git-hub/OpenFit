"""
The roster app's own logic, on its own: reading an .ics, picking out the
duties, labelling them and routing them by the clock.

The app (tests/fixtures/apps/roster/app.py) is loaded by path, as the
host loads it - nothing added to sys.path - and its module functions
called directly, with a fixed `now`. What the host does with what it
writes is test_app_actions.py's: the round trip through the action route.

tests/fixtures/rosters/sample.ics is a synthetic roster - fake flights
and ports - in a Cathay-style export's shape: CRLF lines, a folded and
escaped description, no UIDs. Its year is 2025, so to the round trip
every duty in it is long over.
"""

import importlib.util
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

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
