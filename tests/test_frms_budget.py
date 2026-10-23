"""
The FRMS app's rest budget (Panel D): its engine on its own - each
night's rest window in fixed slots and what the roster did to each, a
rolling window's share of them, a month's - then the panel it draws in
render()'s bottom-right sub-panel.

The app (tests/fixtures/apps/frms/app.py) is loaded by path, as the host
loads it, and called with a config of the real shape: home HKG on Hong
Kong time (UTC+8, no daylight saving), so a night's rest window, 22:00
to 08:00 home local, is 14:00Z to 00:00Z and its seven slots start at
14:00, 15:30, 17:00, 18:30, 20:00 and 21:30Z, the last 23:00-00:00Z.
London is on BST (UTC+1) until 25 October 2026, Sydney on AEDT (UTC+11)
from 4 October. Every time here is exact.
"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

import host
from plugins.base import write_session
from test_frms_app import REAL_CONFIG, load_with_config
from test_frms_circadian import frms

CONFIG = REAL_CONFIG
LATER = datetime(2026, 11, 30, tzinfo=timezone.utc)    # a now after everything here
EARLIER = datetime(2026, 10, 1, tzinfo=timezone.utc)   # ... and one before it all


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def flight(start, end, code="CX100", arr="HKG", dep="HKG"):
    return host.Session(start, end, "app:frms", {"type": "flight", "code": code, "dep": dep, "arr": arr})


def duty(kind, start, end, code):
    return host.Session(start, end, "app:frms", {"type": kind, "code": code})


def asleep(start, end):
    """A night of one stage of light sleep, as sleep_nights() gives it."""
    return {"start": start, "end": end, "source": "google_health",
            "stages": [{"start": start, "end": end, "stage": "light"}]}


def one(rostered=(), flown=(), nights=(), now=LATER, day="2026-10-20", config=CONFIG):
    """One night's slots: (cause, planned) each."""
    [got] = frms.budget_slots(list(rostered), list(flown), list(nights), now, day, day, config)
    return [(x["cause"], x["planned"]) for x in got["slots"]]


def causes(*args, **kw):
    return [cause for cause, _ in one(*args, **kw)]


# ---------- a night's slots ----------

def test_a_night_is_seven_fixed_slots_from_the_rest_windows_start():
    got = frms.budget_slots([], [], [], LATER, date(2026, 10, 20), "2026-10-21", CONFIG)
    assert [n["night"] for n in got] == ["2026-10-20", "2026-10-21"]
    # 22:00 home is 14:00Z: six of 90 minutes, the last part slot an hour.
    assert [(x["start"], x["end"]) for x in got[0]["slots"]] == [
        ("2026-10-20T14:00:00Z", "2026-10-20T15:30:00Z"), ("2026-10-20T15:30:00Z", "2026-10-20T17:00:00Z"),
        ("2026-10-20T17:00:00Z", "2026-10-20T18:30:00Z"), ("2026-10-20T18:30:00Z", "2026-10-20T20:00:00Z"),
        ("2026-10-20T20:00:00Z", "2026-10-20T21:30:00Z"), ("2026-10-20T21:30:00Z", "2026-10-20T23:00:00Z"),
        ("2026-10-20T23:00:00Z", "2026-10-21T00:00:00Z"),
    ]
    # Nothing rostered: every slot rested, over by now.
    assert {(x["cause"], x["planned"]) for n in got for x in n["slots"]} == {(None, False)}


def test_a_last_part_slot_under_half_a_slot_is_dropped():
    # 22:00 to 07:40: six slots and 40 minutes - under 45, so six.
    cfg = {**CONFIG, "personal": {**CONFIG["personal"], "rest_end": "07:40"}}
    assert len(one(config=cfg)) == 6
    # 22:00 to 07:45: the 45 minutes count.
    cfg = {**CONFIG, "personal": {**CONFIG["personal"], "rest_end": "07:45"}}
    assert len(one(config=cfg)) == 7


def test_a_slot_not_over_by_now_is_planned():
    # Now 18:00Z: the first two slots over, the third (17:00-18:30) not yet.
    assert [p for _, p in one(now=utc(2026, 10, 20, 18))] == [False, False, True, True, True, True, True]


# ---------- duty and pre-duty ----------

def test_a_flight_under_way_is_duty_and_the_two_hours_before_its_report_pre_duty():
    # Reporting 03:00 HKT (19:00Z): pre-duty from 01:00 HKT (17:00Z).
    # Each slot by its midpoint: 22:45 and 00:15 rested, 01:45 pre-duty,
    # 03:15 on.
    out = flight("2026-10-20T19:00:00Z", "2026-10-21T04:00:00Z")
    assert causes(flown=[out]) == [None, None, "pre_duty", "duty", "duty", "duty", "duty"]
    # A sim has no pre-duty time; a reserve isn't rest lost to flying.
    sim = duty("sim", "2026-10-20T19:00:00Z", "2026-10-21T04:00:00Z", "A1534")
    assert causes(flown=[sim]) == [None, None, None, "duty", "duty", "duty", "duty"]
    reserve = duty("reserve", "2026-10-20T14:00:00Z", "2026-10-21T04:00:00Z", "R15")
    assert causes(flown=[reserve]) == [None] * 7


def test_the_disturbing_types_are_the_configs_when_it_gives_them():
    reserve = duty("reserve", "2026-10-20T14:00:00Z", "2026-10-21T04:00:00Z", "R15")
    cfg = {**CONFIG, "sleep": {"duty_types": ["flight", "sim", "reserve"]}}
    assert causes(flown=[reserve], config=cfg) == ["duty"] * 7


def test_sleep_on_board_saves_a_past_duty_slot_but_not_one_still_to_come():
    out = flight("2026-10-20T14:00:00Z", "2026-10-21T04:00:00Z")
    # 15 minutes asleep in the 20:00-21:30Z slot: rested. 14: not.
    rest = asleep("2026-10-20T20:30:00Z", "2026-10-20T20:45:00Z")
    assert causes(flown=[out], nights=[rest]) == ["duty", "duty", "duty", "duty", None, "duty", "duty"]
    short = asleep("2026-10-20T20:30:00Z", "2026-10-20T20:44:00Z")
    assert causes(flown=[out], nights=[short])[4] == "duty"
    # Still to come, the plan counts it lost - there's no sleep yet to save it.
    assert one(rostered=[out], nights=[rest], now=EARLIER)[4] == ("duty", True)


def test_the_past_is_flown_and_the_future_rostered_never_a_past_plan():
    out = flight("2026-10-20T14:00:00Z", "2026-10-21T04:00:00Z")
    # Rostered but never flown, and over: nothing known, so nothing lost.
    assert causes(rostered=[out]) == [None] * 7
    # Flown, but read before it was: not yet counted.
    assert causes(flown=[out], now=EARLIER) == [None] * 7


# ---------- the body clock ----------

LONDON_OUT = flight("2026-10-20T15:00:00Z", "2026-10-21T05:00:00Z", "CX255", "LHR")
LONDON_HOME = flight("2026-10-23T10:00:00Z", "2026-10-23T23:00:00Z", "CX256", "HKG", "LHR")


def test_away_a_slot_outside_local_night_is_the_body_clock():
    # The night of the 21st in London: 22:00-08:00 HKT is 15:00-01:00
    # BST. Midpoints 15:45, 17:15, 18:45, 20:15 and 21:45 local are outside
    # 22:00-08:00 there; 23:15 and 00:30 inside.
    got = one(flown=[LONDON_OUT, LONDON_HOME], day="2026-10-21")
    assert got == [("body_clock", False)] * 5 + [(None, False)] * 2
    # Rostered and still to come: the same maths, planned.
    assert one(rostered=[LONDON_OUT, LONDON_HOME], now=EARLIER, day="2026-10-21") == \
        [("body_clock", True)] * 5 + [(None, True)] * 2


def test_the_body_clock_never_asks_the_sleep():
    # Asleep right through the London evening: still resisting the clock.
    rest = asleep("2026-10-21T14:00:00Z", "2026-10-22T00:00:00Z")
    assert causes(flown=[LONDON_OUT, LONDON_HOME], nights=[rest], day="2026-10-21") == ["body_clock"] * 5 + [None] * 2


def test_a_port_with_no_time_zone_is_home():
    away = flight("2026-10-20T15:00:00Z", "2026-10-21T05:00:00Z", "CX999", "ZZZ")
    assert causes(flown=[away], day="2026-10-21") == [None] * 7


def test_duty_comes_before_the_body_clock():
    # Home from London, reporting 18:00 BST on the 22nd (17:00Z): the
    # pre-duty from 15:00Z; the 14:00Z slot's midpoint (15:45 BST) still
    # London's evening.
    home = flight("2026-10-22T17:00:00Z", "2026-10-23T06:00:00Z", "CX256", "HKG", "LHR")
    assert causes(flown=[LONDON_OUT, home], day="2026-10-22") == [
        "body_clock", "pre_duty", "duty", "duty", "duty", "duty", "duty"]


@pytest.mark.parametrize("first, last, says", [
    ("20 Oct", "2026-10-21", "first must be a date: '20 Oct'"),
    ("2026-10-20", utc(2026, 10, 21), "last must be a date"),
])
def test_a_night_it_cant_read_is_refused(first, last, says):
    with pytest.raises(ValueError, match=says):
        frms.budget_slots([], [], [], LATER, first, last, CONFIG)


# ---------- the shares ----------

def budget():
    """The 19th to the 31st: London out on the 20th, home on the 23rd."""
    return frms.budget_slots([], [LONDON_OUT, LONDON_HOME], [], LATER, "2026-10-19", "2026-10-31", CONFIG)


def test_a_rolling_share_counts_the_slots_over_in_its_days_from_the_rosters_first_night():
    b = budget()
    # The night of the 20th: pre-duty, then six on duty. The 21st and the
    # 22nd: five London evenings each. The 23rd: on duty home, its
    # pre-duty from 08:00Z, to 23:00Z - the last slot home, rested - so six.
    nights = {n["night"]: sum(x["cause"] is not None for x in n["slots"]) for n in b}
    assert [nights[d] for d in ("2026-10-19", "2026-10-20", "2026-10-21", "2026-10-22", "2026-10-23", "2026-10-24")] \
        == [0, 7, 5, 5, 6, 0]
    # Now 06:00Z on the 25th, the last week: nights 19-24 over - 42 slots.
    now = utc(2026, 10, 25, 6)
    assert frms.rolling_share(b, now, 7, "2026-10-19") == {"red": 23, "total": 42, "days": 6}
    # The window's start cuts: two days back, the slots starting since
    # 06:00Z on the 23rd - the 23rd's (6 red) and the 24th's.
    assert frms.rolling_share(b, now, 2, "2026-10-19") == {"red": 6, "total": 14, "days": 2}
    # The roster's first night cuts too: from the 22nd, its days 3.
    assert frms.rolling_share(b, now, 30, "2026-10-22") == {"red": 11, "total": 21, "days": 3}


def test_a_months_share_is_its_flown_and_rostered_slots_from_the_rosters_first_night():
    b = frms.budget_slots([LONDON_HOME], [LONDON_OUT], [], utc(2026, 10, 22, 3), "2026-10-01", "2026-10-31", CONFIG)
    got = frms.month_share(b, "2026-10", "2026-10-19")
    # Nights from the 19th: the 19th to the 21st flown (21 slots, 12 red:
    # the 20th's seven, the 21st's five), the 22nd on rostered (70 slots:
    # the 22nd's five, the 23rd's six red).
    assert {k: got[k] for k in ("flown_red", "flown_rested", "rostered_red", "rostered_rested")} == {
        "flown_red": 12, "flown_rested": 9, "rostered_red": 11, "rostered_rested": 59}
    assert len(got["nights"]) == 31
    # Before the roster's first night: nothing known, nothing counted.
    assert got["nights"][0] == {"night": "2026-10-01", "red": 0, "rested": 0, "planned_red": 0, "planned_rested": 0}
    assert got["nights"][19] == {"night": "2026-10-20", "red": 7, "rested": 0, "planned_red": 0, "planned_rested": 0}
    assert got["nights"][22] == {"night": "2026-10-23", "red": 0, "rested": 0, "planned_red": 6, "planned_rested": 1}
    # Another month's nights are none of this one's.
    assert frms.month_share(b, "2026-09", "2026-10-19")["nights"] == []


# ---------- the panel ----------

NOW = utc(2026, 10, 23, 3)   # 11:00 HKT on the 23rd


def _budget_cell(spec):
    [cell] = [c["spec"] for c in spec["cells"] if (c["x"], c["y"]) == (2, 2)]
    return cell


def test_the_rest_budget_is_three_donuts_over_the_months_bars_beside_its_donut(tmp_path, conn, monkeypatch):
    app = load_with_config(tmp_path, json.dumps(CONFIG))
    monkeypatch.setattr(app, "_now", lambda: NOW)
    # Flown: London out on the 20th, home on the 22nd. Rostered: Sydney
    # out on the 29th, home on the 31st.
    for kind, start, end, code, dep, arr in (
        ("duties_complete", "2026-10-20T15:00:00Z", "2026-10-21T05:00:00Z", "CX255", "HKG", "LHR"),
        ("duties_complete", "2026-10-22T10:00:00Z", "2026-10-22T23:00:00Z", "CX256", "LHR", "HKG"),
        ("duties_rostered", "2026-10-29T13:00:00Z", "2026-10-29T22:00:00Z", "CX101", "HKG", "SYD"),
        ("duties_rostered", "2026-10-31T00:00:00Z", "2026-10-31T09:00:00Z", "CX100", "SYD", "HKG"),
    ):
        write_session(conn, "app:frms", kind, start, end, {"type": "flight", "code": code, "dep": dep, "arr": arr})
    conn.commit()

    spec = _budget_cell(app.render(host.Ctx(conn), {}))

    assert spec["type"] == "row" and spec["widths"] == [3, 2]
    left, big = spec["children"]
    assert left["type"] == "column" and left["heights"] == [3, 2]
    donuts, bars = left["children"]
    # The roster from the night of the 19th - the evening before its
    # first duty - so 4 days known: the 19th to the 22nd, 28 slots, 18
    # red - the 20th's 7, the 21st's 5 London evenings, the 22nd's 6 on
    # duty (its last slot home). The same in each window, its days noted.
    assert [(d["caption"], d["figure"], d["note"], [(s["value"], s["tone"]) for s in d["segments"]])
            for d in donuts["children"]] == [
        ("7 days", "64%", "4 of 7 days", [(10, "good"), (18, "crit")]),
        ("30 days", "64%", "4 of 30 days", [(10, "good"), (18, "crit")]),
        ("6 months", "64%", "4 of 182 days", [(10, "good"), (18, "crit")]),
    ]
    # A bar a night of October, seven slots high, red from the foot, the
    # nights before the roster's first empty; the now line before
    # tonight's, the 23rd.
    assert (bars["title"], bars["max"], bars["now"], len(bars["bars"])) == ("Disturbed rest · October", 7, 22, 31)
    assert [b["label"] for b in bars["bars"] if b["label"]] == ["1", "8", "15", "22", "29"]
    tally = lambda b: [(s["value"], s["tone"], s["planned"]) for s in b["segments"] if s["value"]]
    assert tally(bars["bars"][0]) == []
    assert tally(bars["bars"][19]) == [(7, "crit", False)]
    assert tally(bars["bars"][20]) == [(5, "crit", False), (2, "good", False)]
    # Sydney: on duty to 22:00Z on the 29th, then Sydney's morning; on the
    # 30th Sydney's night, then the pre-duty from 22:00Z.
    assert tally(bars["bars"][28]) == [(7, "crit", True)]
    assert tally(bars["bars"][29]) == [(2, "crit", True), (5, "good", True)]
    # October's donut: 91 slots from the 19th; flown 18 red of 28,
    # rostered 9 of 63 - flown first, solid, then rostered, hatched.
    assert [(s["value"], s["tone"], s["planned"]) for s in big["segments"]] == [
        (10, "good", False), (18, "crit", False), (9, "crit", True), (54, "good", True)]
    assert (big["figure"], big["label"], big["note"], big["key"]) == (
        "30%", "October", "18 flown · 9 rostered of 91", ["Flown", "Rostered"])


def test_before_any_roster_the_rest_budget_asks_for_one(tmp_path, conn, monkeypatch):
    app = load_with_config(tmp_path, json.dumps(CONFIG))
    monkeypatch.setattr(app, "_now", lambda: NOW)
    assert _budget_cell(app.render(host.Ctx(conn), {})) == {"type": "empty", "message": "Upload your roster .ics"}
