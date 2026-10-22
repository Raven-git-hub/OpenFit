"""
The FRMS app's sleep panel: its engine on its own - the body's rest window
each day, rest and wake over a window, the sleep sessions with a stage
timeline (one a night), their stages cut at the rest window's edges, and
each flown duty's slots in the rest window and the sleep in each - then
the panel it draws in render()'s top-right sub-panel.

The app (tests/fixtures/apps/frms/app.py) is loaded by path, as the host
loads it, and called with a config of the real shape: home HKG on Hong
Kong time (UTC+8, no daylight saving), so 22:00 home local is 14:00Z and
08:00 is 00:00Z. Every time here is exact.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import host
from plugins.base import iso_utc, write_session
from test_frms_app import REAL_CONFIG, load_with_config
from test_frms_circadian import frms

CONFIG = REAL_CONFIG


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def config(sleep=None, **personal):
    mine = {**CONFIG["personal"], **personal}
    built = {**CONFIG, "personal": {k: v for k, v in mine.items() if v is not None}}
    if sleep is not None:
        built["sleep"] = sleep
    return built


def night(start, end, stages, source="google_health"):
    """A sleep session as ctx.sessions() gives it, its stages (start, end, stage)."""
    return host.Session(start, end, source, {
        "asleep_minutes": 0, "stages": [{"start": a, "end": b, "stage": s} for a, b, s in stages]})


def duty(start, end, code="CX100"):
    return host.Session(start, end, "app:frms", {"type": "flight", "code": code})


# ---------- the rest window ----------

def test_the_rest_window_is_22_to_08_home_local_each_day():
    got = frms.rest_windows(utc(2026, 10, 20, 0), utc(2026, 10, 22, 0), CONFIG)
    # Clipped at the window's start, then each night 14:00Z to 00:00Z.
    assert got == [
        {"start": "2026-10-20T14:00:00Z", "end": "2026-10-21T00:00:00Z"},
        {"start": "2026-10-21T14:00:00Z", "end": "2026-10-22T00:00:00Z"},
    ]
    # Clipped at both ends: inside the night, only the part inside.
    assert frms.rest_windows(utc(2026, 10, 20, 18), utc(2026, 10, 20, 20), CONFIG) == [
        {"start": "2026-10-20T18:00:00Z", "end": "2026-10-20T20:00:00Z"}]
    # The night before the window's first day reaches into it.
    assert frms.rest_windows(utc(2026, 10, 20, 0), utc(2026, 10, 20, 6), CONFIG) == []
    assert frms.rest_windows(utc(2026, 10, 19, 23), utc(2026, 10, 20, 1), CONFIG) == [
        {"start": "2026-10-19T23:00:00Z", "end": "2026-10-20T00:00:00Z"}]


def test_the_rest_window_is_the_configs_when_it_gives_one():
    # 23:00 to 07:00 home local: 15:00Z to 23:00Z.
    assert frms.rest_windows(utc(2026, 10, 20, 0), utc(2026, 10, 21, 0), config(rest_start="23:00", rest_end="07:00")) == [
        {"start": "2026-10-20T15:00:00Z", "end": "2026-10-20T23:00:00Z"}]
    # One that doesn't cross midnight - a day sleeper's, 09:00 to 17:00 -
    # is one day's: 01:00Z to 09:00Z.
    assert frms.rest_windows(utc(2026, 10, 20, 0), utc(2026, 10, 21, 0), config(rest_start="09:00", rest_end="17:00")) == [
        {"start": "2026-10-20T01:00:00Z", "end": "2026-10-20T09:00:00Z"}]


@pytest.mark.parametrize("personal, says", [
    ({"rest_start": "10pm"}, "config.json's personal rest_start must be HH:MM, home local: '10pm'"),
    ({"rest_end": "24:00"}, "config.json's personal rest_end must be HH:MM, home local: '24:00'"),
    ({"rest_end": 8}, "config.json's personal rest_end must be HH:MM, home local: 8"),
    ({"rest_start": "08:00", "rest_end": "08:00"}, "config.json's personal rest_start and rest_end can't be the same time"),
])
def test_a_rest_window_it_cant_read_is_refused(personal, says):
    with pytest.raises(ValueError, match=says):
        frms.rest_windows(utc(2026, 10, 20, 0), utc(2026, 10, 21, 0), config(**personal))


def test_rest_and_wake_tile_the_window():
    got = frms.rest_strip(utc(2026, 10, 20, 12), utc(2026, 10, 21, 12), CONFIG)
    assert got == [
        {"start": "2026-10-20T12:00:00Z", "end": "2026-10-20T14:00:00Z", "tone": "day"},
        {"start": "2026-10-20T14:00:00Z", "end": "2026-10-21T00:00:00Z", "tone": "rest"},
        {"start": "2026-10-21T00:00:00Z", "end": "2026-10-21T12:00:00Z", "tone": "day"},
    ]


# ---------- the nights ----------

def test_a_night_is_a_sleep_session_with_a_stage_timeline_one_a_night():
    google = night("2026-10-20T15:00:00Z", "2026-10-20T23:00:00Z", [
        ("2026-10-20T15:30:00Z", "2026-10-20T16:00:00Z", "deep"),
        ("2026-10-20T15:00:00Z", "2026-10-20T15:30:00Z", "light"),     # out of order: put in order
        ("2026-10-20T16:00:00Z", None, "rem"),                          # no end: left out
        ("2026-10-20T16:00:00Z", "2026-10-20T16:00:00Z", "rem"),       # no length: left out
        ("2026-10-20T16:10:00Z", "2026-10-20T16:20:00Z", None),        # no stage: left out
    ])
    google.summary["stages"].append("LIGHT")                            # not a segment: left out
    # Served first (the host's source priority) but only totals: no night.
    garmin_totals = host.Session("2026-10-20T15:05:00Z", "2026-10-20T23:05:00Z", "garmin", {"asleep_minutes": 470})
    # Overlapping the one taken: left out. One with no end: none.
    later = night("2026-10-20T22:00:00Z", "2026-10-21T01:00:00Z", [("2026-10-20T22:00:00Z", "2026-10-21T01:00:00Z", "light")])
    open_ended = host.Session("2026-10-21T14:00:00Z", None, "google_health", {"stages": []})
    nap = night("2026-10-21T06:00:00Z", "2026-10-21T06:40:00Z", [("2026-10-21T06:00:00Z", "2026-10-21T06:40:00Z", "asleep")])

    got = frms.sleep_nights([garmin_totals, nap, google, later, open_ended])

    assert got == [
        {"start": "2026-10-20T15:00:00Z", "end": "2026-10-20T23:00:00Z", "source": "google_health", "stages": [
            {"start": "2026-10-20T15:00:00Z", "end": "2026-10-20T15:30:00Z", "stage": "light"},
            {"start": "2026-10-20T15:30:00Z", "end": "2026-10-20T16:00:00Z", "stage": "deep"},
        ]},
        {"start": "2026-10-21T06:00:00Z", "end": "2026-10-21T06:40:00Z", "source": "google_health", "stages": [
            {"start": "2026-10-21T06:00:00Z", "end": "2026-10-21T06:40:00Z", "stage": "asleep"},
        ]},
    ]


def test_the_first_served_of_two_overlapping_nights_is_drawn():
    a = night("2026-10-20T15:00:00Z", "2026-10-20T23:00:00Z", [("2026-10-20T15:00:00Z", "2026-10-20T23:00:00Z", "light")], "garmin")
    b = night("2026-10-20T15:10:00Z", "2026-10-20T23:10:00Z", [("2026-10-20T15:10:00Z", "2026-10-20T23:10:00Z", "deep")])
    assert [n["source"] for n in frms.sleep_nights([a, b])] == ["garmin"]
    assert [n["source"] for n in frms.sleep_nights([b, a])] == ["google_health"]


# ---------- the stages drawn ----------

def test_stages_are_cut_at_the_rest_windows_edges_and_toned_in_or_out():
    nights = frms.sleep_nights([night("2026-10-20T13:30:00Z", "2026-10-21T00:30:00Z", [
        ("2026-10-20T13:30:00Z", "2026-10-20T13:40:00Z", "awake"),     # awake: not drawn
        ("2026-10-20T13:40:00Z", "2026-10-20T14:20:00Z", "light"),     # across 22:00 home: cut
        ("2026-10-20T14:20:00Z", "2026-10-20T15:00:00Z", "deep"),
        ("2026-10-20T15:00:00Z", "2026-10-20T15:10:00Z", "unknown"),   # unknown: not drawn
        ("2026-10-20T15:10:00Z", "2026-10-20T15:30:00Z", "restless"),  # stage-less: at light's level
        ("2026-10-20T23:40:00Z", "2026-10-21T00:30:00Z", "rem"),       # across 08:00 home: cut
    ])])
    windows = frms.rest_windows(utc(2026, 10, 20, 0), utc(2026, 10, 22, 0), CONFIG)

    assert frms.stage_pieces(nights, windows) == [
        {"start": "2026-10-20T13:40:00Z", "end": "2026-10-20T14:00:00Z", "stage": "light", "tone": "out"},
        {"start": "2026-10-20T14:00:00Z", "end": "2026-10-20T14:20:00Z", "stage": "light", "tone": "in"},
        {"start": "2026-10-20T14:20:00Z", "end": "2026-10-20T15:00:00Z", "stage": "deep", "tone": "in"},
        {"start": "2026-10-20T15:10:00Z", "end": "2026-10-20T15:30:00Z", "stage": "light", "tone": "in"},
        {"start": "2026-10-20T23:40:00Z", "end": "2026-10-21T00:00:00Z", "stage": "rem", "tone": "in"},
        {"start": "2026-10-21T00:00:00Z", "end": "2026-10-21T00:30:00Z", "stage": "rem", "tone": "out"},
    ]


# ---------- the disturbed cycles ----------
#
# The example locked with Andy: a duty 01:00 to 11:00 home local (17:00Z
# to 03:00Z) with two bunk rests of light sleep - 02:40-03:40 and
# 05:45-06:35 home. Its overlap with the rest window, 01:00-08:00, is five
# slots: 01:00, 02:30, 04:00, 05:30 and a last part slot 07:00-08:00 (an
# hour: over half a slot, so counted). The slots with a rest in them
# aren't disturbed; the other three are. 08:00-11:00 is outside the rest
# window: no slots.

DUTY = duty("2026-10-20T17:00:00Z", "2026-10-21T03:00:00Z")
BUNK = frms.sleep_nights([
    night("2026-10-20T18:40:00Z", "2026-10-20T19:40:00Z", [("2026-10-20T18:40:00Z", "2026-10-20T19:40:00Z", "light")]),
    night("2026-10-20T21:45:00Z", "2026-10-20T22:35:00Z", [("2026-10-20T21:45:00Z", "2026-10-20T22:35:00Z", "light")]),
])
WINDOWS = frms.rest_windows(utc(2026, 10, 19, 0), utc(2026, 10, 23, 0), CONFIG)
LATER = utc(2026, 10, 22, 8)


def test_a_duty_in_the_rest_window_is_slots_disturbed_where_no_sleep_is_in_them():
    assert frms.disturbed_slots([DUTY], BUNK, WINDOWS, LATER, CONFIG) == [
        {"start": "2026-10-20T17:00:00Z", "end": "2026-10-20T18:30:00Z", "asleep": 0, "disturbed": True},
        {"start": "2026-10-20T18:30:00Z", "end": "2026-10-20T20:00:00Z", "asleep": 60, "disturbed": False},
        {"start": "2026-10-20T20:00:00Z", "end": "2026-10-20T21:30:00Z", "asleep": 0, "disturbed": True},
        {"start": "2026-10-20T21:30:00Z", "end": "2026-10-20T23:00:00Z", "asleep": 50, "disturbed": False},
        {"start": "2026-10-20T23:00:00Z", "end": "2026-10-21T00:00:00Z", "asleep": 0, "disturbed": True},
    ]


def test_a_last_part_slot_under_half_a_slot_is_dropped():
    # Ending 07:40 home: the last part slot, 07:00-07:40, is 40 minutes.
    short = duty("2026-10-20T17:00:00Z", "2026-10-20T23:40:00Z")
    got = frms.disturbed_slots([short], [], WINDOWS, LATER, CONFIG)
    assert [x["start"] for x in got] == ["2026-10-20T17:00:00Z", "2026-10-20T18:30:00Z", "2026-10-20T20:00:00Z",
                                         "2026-10-20T21:30:00Z"]
    # Ending 07:45: 45 minutes, half a slot - counted.
    longer = duty("2026-10-20T17:00:00Z", "2026-10-20T23:45:00Z")
    assert frms.disturbed_slots([longer], [], WINDOWS, LATER, CONFIG)[-1]["end"] == "2026-10-20T23:45:00Z"


def test_fifteen_minutes_of_sleep_in_a_slot_is_rest_fourteen_is_not():
    def slept(minutes):
        end = utc(2026, 10, 20, 17) + timedelta(minutes=minutes)
        nights = frms.sleep_nights([night("2026-10-20T17:00:00Z", iso_utc(end), [("2026-10-20T17:00:00Z", iso_utc(end), "light")])])
        return frms.disturbed_slots([DUTY], nights, WINDOWS, LATER, CONFIG)[0]
    assert slept(14) == {"start": "2026-10-20T17:00:00Z", "end": "2026-10-20T18:30:00Z", "asleep": 14, "disturbed": True}
    assert slept(15)["disturbed"] is False
    # Awake isn't sleep; a stage-less asleep is.
    awake = frms.sleep_nights([night("2026-10-20T17:00:00Z", "2026-10-20T18:00:00Z", [("2026-10-20T17:00:00Z", "2026-10-20T18:00:00Z", "awake")])])
    asleep = frms.sleep_nights([night("2026-10-20T17:00:00Z", "2026-10-20T18:00:00Z", [("2026-10-20T17:00:00Z", "2026-10-20T18:00:00Z", "asleep")])])
    assert frms.disturbed_slots([DUTY], awake, WINDOWS, LATER, CONFIG)[0]["disturbed"] is True
    assert frms.disturbed_slots([DUTY], asleep, WINDOWS, LATER, CONFIG)[0]["disturbed"] is False


def test_only_slots_over_by_now_are_checked_and_a_duty_with_no_end_is_none():
    # At 04:00 home (20:00Z): the 01:00 and 02:30 slots are over, the rest to come.
    got = frms.disturbed_slots([DUTY], BUNK, WINDOWS, utc(2026, 10, 20, 20), CONFIG)
    assert [x["end"] for x in got] == ["2026-10-20T18:30:00Z", "2026-10-20T20:00:00Z"]
    assert frms.disturbed_slots([duty("2026-10-20T17:00:00Z", None)], BUNK, WINDOWS, LATER, CONFIG) == []
    # A duty outside the rest window: none.
    assert frms.disturbed_slots([duty("2026-10-21T01:00:00Z", "2026-10-21T09:00:00Z")], [], WINDOWS, LATER, CONFIG) == []


def test_the_slot_and_the_least_sleep_are_the_configs_when_it_gives_them():
    got = frms.disturbed_slots([DUTY], BUNK, WINDOWS, LATER, config(sleep={"slot_minutes": 60, "min_sleep_minutes": 55}))
    # Hour slots from 01:00 home; 55 minutes needed - the 05:45-06:35 rest,
    # split 15 and 35 across two slots, rests neither.
    assert [(x["start"][11:16], x["asleep"], x["disturbed"]) for x in got] == [
        ("17:00", 0, True), ("18:00", 20, True), ("19:00", 40, True), ("20:00", 0, True),
        ("21:00", 15, True), ("22:00", 35, True), ("23:00", 0, True)]
    for sleep, says in (({"slot_minutes": 0}, "slot_minutes must be a whole number of minutes, 1 to 1440: 0"),
                        ({"slot_minutes": "90"}, "slot_minutes must be a whole number of minutes, 1 to 1440: '90'"),
                        ({"min_sleep_minutes": 120}, "min_sleep_minutes must be a whole number of minutes, 1 to 90: 120")):
        with pytest.raises(ValueError, match=f"config.json's sleep {says}"):
            frms.disturbed_slots([DUTY], BUNK, WINDOWS, LATER, config(sleep=sleep))


# ---------- the panel ----------

def _sleep_cell(spec):
    """The sleep panel's spec in render()'s grid: the sub-panel at x 3, y 0."""
    [cell] = [c for c in spec["cells"] if (c["x"], c["y"]) == (3, 0)]
    return cell["spec"]


def test_the_sleep_panel_is_three_strips_three_days_across_in_the_frms_group(tmp_path, conn):
    app = load_with_config(tmp_path, json.dumps(CONFIG))
    # Two days ago, home local: a duty 01:00-11:00 and a bunk rest
    # 02:40-03:40 in it - and a night at home the night before.
    base = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2)
    t = lambda hours, minutes=0: iso_utc(base + timedelta(hours=hours, minutes=minutes))   # base is 08:00 home
    write_session(conn, "app:frms", "duties_complete", t(-7), t(3), {"type": "flight", "code": "CX101"})
    for start, end, stages in (
        (t(-30), t(-24), [(t(-30), t(-29, 30), "light"), (t(-29, 30), t(-28), "deep"), (t(-28), t(-24), "rem")]),
        (t(-5, -20), t(-4, -20), [(t(-5, -20), t(-4, -20), "light")]),
    ):
        write_session(conn, "google_health", "sleep", start, end,
                      {"asleep_minutes": 0, "stages": [{"start": a, "end": b, "stage": s} for a, b, s in stages]})
    conn.commit()

    spec = _sleep_cell(app.render(host.Ctx(conn), {}))

    assert spec["type"] == "strips" and spec["sync"] == "frms" and spec["hours"] == 72
    sleep, body, ticks = spec["lanes"]
    assert [(l["kind"], l["label"], l["clock"]) for l in spec["lanes"]] == [
        ("sleep", "Sleep stages", False), ("sky", "Body clock", "date"), ("ticks", "Disturbed cycles", False)]
    # The night's stages and the bunk rest, every one in the rest window.
    assert [(x["stage"], x["tone"]) for x in sleep["spans"]] == [
        ("light", "in"), ("deep", "in"), ("rem", "in"), ("light", "in")]
    # The rest window over home's date and time.
    assert {x["tone"] for x in body["spans"]} == {"rest", "day"}
    assert {(x["place"], x["tz"]) for x in body["spans"]} == {("HKG", "Asia/Hong_Kong")}
    # The duty's slots from 01:00 home: 01:00-02:30 disturbed, 02:30-04:00
    # with the rest in it not, then 04:00, 05:30 and 07:00-08:00 disturbed.
    assert [(x["start"], x["end"]) for x in ticks["spans"]] == [
        (t(-7), t(-5, -30)), (t(-4), t(-2, -30)), (t(-2, -30), t(-1)), (t(-1), t(0))]
    assert {x["tone"] for x in ticks["spans"]} == {"disturbed"}
