"""
The FRMS app's circadian engine, on its own: sun times from a date and
a position, where the crew member is over time, day and night at home
base, the body clock's WOCL and troughs, and the duties cut into
day/night/WOCL pieces against the body clock.

The app (library/frms/app.py) is loaded by path, as the host
loads it, and its module functions called directly with a config of the
real shape - home base, timezone, cbt_min, each destination's lat and
lon - built here: the fixture's config.json is synthetic and has no
positions. Nothing draws the engine yet; render() is test_frms_app.py's.

Sunrise and sunset are held to published values within 3 minutes - Hong
Kong's to the Hong Kong Observatory's 2026 table, the other ports' to
values computed with the astral library, kept here as constants (astral
isn't a dependency). Everything else - the WOCL, the troughs, a duty's
start and end - is exact.
"""

import importlib.util
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import host

FIXTURES = Path(__file__).parent.parent / "library"


def load_frms_app():
    spec = importlib.util.spec_from_file_location("frms_app", FIXTURES / "frms" / "app.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


frms = load_frms_app()


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def at(text):
    """A span's UTC text as an aware datetime."""
    return datetime.fromisoformat(text)


def local(day, hhmm, tz):
    """A wall-clock time on a date in a zone, as an aware datetime."""
    return datetime.combine(date.fromisoformat(day), time.fromisoformat(hhmm), tzinfo=ZoneInfo(tz))


def near(got, want, minutes=3):
    return abs(got - want) <= timedelta(minutes=minutes)


def tone_at(spans, moment):
    """The tone of the span holding `moment` - [start, end) - or None."""
    found = [s["tone"] for s in spans if at(s["start"]) <= moment < at(s["end"])]
    assert len(found) <= 1, found
    return found[0] if found else None


def assert_contiguous(spans, start, end):
    assert spans, "no spans"
    assert at(spans[0]["start"]) == start and at(spans[-1]["end"]) == end
    for a, b in zip(spans, spans[1:]):
        assert a["end"] == b["start"], (a, b)


# Ports as in the owner's config: lat and lon in decimal degrees, 2 dp.
PORTS = {
    "HKG": {"city": "Hong Kong", "tz": "Asia/Hong_Kong", "lat": 22.31, "lon": 113.92},
    "SYD": {"city": "Sydney", "tz": "Australia/Sydney", "lat": -33.95, "lon": 151.18},
    "BKK": {"city": "Bangkok", "tz": "Asia/Bangkok", "lat": 13.69, "lon": 100.75},
    "LHR": {"city": "London", "tz": "Europe/London", "lat": 51.47, "lon": -0.46},
}


def config(destinations=None, **personal):
    """A config of the real shape: home HKG on Hong Kong time, the body
    clock's low at 04:00, and PORTS - `personal` overriding, a None
    taking the key out."""
    mine = {"home_base": "HKG", "timezone": "Asia/Hong_Kong", "cbt_min": "04:00", **personal}
    return {
        "personal": {k: v for k, v in mine.items() if v is not None},
        "duty_types": [{"type": "flight", "prefixes": ["CX"]}, {"type": "sim", "regex": r"^A\d"},
                       {"type": "reserve", "prefixes": ["R"]}],
        "default_type": "other",
        "destinations": PORTS if destinations is None else destinations,
    }


CONFIG = config()


def flight(route, start, end, code="CX100"):
    dep, arr = route.split("-")
    return host.Session(start, end, "app:frms", {"type": "flight", "code": code, "dep": dep, "arr": arr})


def duty(kind, start, end, code, **summary):
    return host.Session(start, end, "app:frms", {"type": kind, "code": code, **summary})


# ---------- sun times ----------

# Hong Kong (22.31, 113.92): the Hong Kong Observatory's 2026 table.
HKO_2026 = [
    ("2026-03-20", "06:28", "18:34"),
    ("2026-06-21", "05:40", "19:10"),
    ("2026-10-22", "06:22", "17:53"),
    ("2026-12-21", "06:58", "17:44"),
]

# The other ports: astral's values, cross-checked against a NOAA
# prototype. (lat, lon, tz, {day: (rise, set)}), local times.
REFERENCE = {
    "SYD": (-33.95, 151.18, "Australia/Sydney",
            {"2026-01-15": ("05:59", "20:09"), "2026-06-21": ("07:00", "16:53"), "2026-10-22": ("06:06", "19:14")}),
    "LHR": (51.47, -0.46, "Europe/London",
            {"2026-01-15": ("08:00", "16:21"), "2026-06-21": ("04:44", "21:22"), "2026-10-22": ("07:37", "17:53")}),
    "JFK": (40.64, -73.78, "America/New_York",
            {"2026-01-15": ("07:17", "16:52"), "2026-06-21": ("05:24", "20:29"), "2026-10-22": ("07:14", "18:04")}),
    "PER": (-31.94, 115.97, "Australia/Perth",
            {"2026-01-15": ("05:25", "19:25"), "2026-06-21": ("07:16", "17:19"), "2026-10-22": ("05:29", "18:32")}),
    "AKL": (-37.01, 174.79, "Pacific/Auckland",
            {"2026-01-15": ("06:17", "20:42"), "2026-06-21": ("07:34", "17:10"), "2026-10-22": ("06:27", "19:43")}),
    "DXB": (25.25, 55.36, "Asia/Dubai",
            {"2026-01-15": ("07:06", "17:49"), "2026-06-21": ("05:29", "19:11"), "2026-10-22": ("06:20", "17:45")}),
    "YVR": (49.19, -123.18, "America/Vancouver",
            {"2026-01-15": ("08:02", "16:42"), "2026-06-21": ("05:07", "21:21"), "2026-10-22": ("07:45", "18:08")}),
    "MAN": (53.35, -2.27, "Europe/London",
            {"2026-01-15": ("08:16", "16:20"), "2026-06-21": ("04:41", "21:40"), "2026-10-22": ("07:49", "17:57")}),
}

SUN_CASES = [("HKG", 22.31, 113.92, "Asia/Hong_Kong", day, rise, sunset) for day, rise, sunset in HKO_2026] + [
    (port, lat, lon, tz, day, rise, sunset)
    for port, (lat, lon, tz, days) in REFERENCE.items()
    for day, (rise, sunset) in days.items()
]


@pytest.mark.parametrize("port, lat, lon, tz, day, rise, sunset", SUN_CASES,
                         ids=[f"{c[0]}-{c[4]}" for c in SUN_CASES])
def test_sun_times_match_published_values(port, lat, lon, tz, day, rise, sunset):
    got_rise, got_set = frms.sun_times(date.fromisoformat(day), lat, lon)

    # Aware, in UTC, and within 3 minutes of the published local times.
    assert got_rise.utcoffset() == timedelta(0) and got_set.utcoffset() == timedelta(0)
    assert near(got_rise, local(day, rise, tz)), (got_rise.astimezone(ZoneInfo(tz)), rise)
    assert near(got_set, local(day, sunset, tz)), (got_set.astimezone(ZoneInfo(tz)), sunset)


def test_sun_times_are_around_the_days_local_noon_even_far_east_and_west():
    # Auckland's 15 Jan rises on the 14th UTC and Vancouver's sets on the
    # 16th UTC: `day` is the local day, its noon the longitude's.
    rise, _ = frms.sun_times(date(2026, 1, 15), -37.01, 174.79)
    _, sunset = frms.sun_times(date(2026, 1, 15), 49.19, -123.18)
    assert rise.date() == date(2026, 1, 14)
    assert sunset.date() == date(2026, 1, 16)


# ---------- polar ----------

SVALBARD = (78.22, 15.65)


@pytest.mark.parametrize("day, tone", [(date(2026, 6, 21), "day"), (date(2026, 12, 21), "night")])
def test_a_polar_day_or_night_is_one_span(day, tone):
    assert frms.sun_times(day, *SVALBARD) == tone

    start = datetime.combine(day, time(), tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    assert frms.daylight(*SVALBARD, start, end) == [
        {"start": frms._utc_text(start), "end": frms._utc_text(end), "tone": tone},
    ]


# ---------- daylight ----------


def test_daylight_is_contiguous_alternating_and_on_the_sun_times():
    lat, lon = PORTS["HKG"]["lat"], PORTS["HKG"]["lon"]
    start, end = utc(2026, 10, 20), utc(2026, 10, 24)
    spans = frms.daylight(lat, lon, start, end)

    assert_contiguous(spans, start, end)
    tones = [s["tone"] for s in spans]
    assert all(a != b for a, b in zip(tones, tones[1:]))
    assert set(tones) == {"day", "night"}

    # Every boundary inside the window is the sunrise (night to day) or
    # sunset (day to night) of its own Hong Kong date ...
    hkt = ZoneInfo("Asia/Hong_Kong")
    for a, b in zip(spans, spans[1:]):
        boundary = at(a["end"])
        rise, sunset = frms.sun_times(boundary.astimezone(hkt).date(), lat, lon)
        assert boundary == (rise if b["tone"] == "day" else sunset)

    # ... and 22 Oct's are within 3 minutes of the Observatory's.
    assert tone_at(spans, local("2026-10-22", "06:19", "Asia/Hong_Kong")) == "night"
    assert tone_at(spans, local("2026-10-22", "06:25", "Asia/Hong_Kong")) == "day"
    assert tone_at(spans, local("2026-10-22", "17:50", "Asia/Hong_Kong")) == "day"
    assert tone_at(spans, local("2026-10-22", "17:56", "Asia/Hong_Kong")) == "night"


def test_daylight_takes_its_window_as_given():
    # Clipped to the window, whatever time of day it starts and ends.
    start, end = utc(2026, 10, 21, 3, 17), utc(2026, 10, 21, 4, 41)
    assert frms.daylight(22.31, 113.92, start, end) == [
        {"start": "2026-10-21T03:17:00Z", "end": "2026-10-21T04:41:00Z", "tone": "day"},
    ]
    assert frms.daylight(22.31, 113.92, end, start) == []


# ---------- the body clock ----------


def test_the_wocl_is_two_to_six_home_time_and_the_troughs_four():
    clock = frms.body_clock(utc(2026, 10, 21), utc(2026, 10, 23), CONFIG)

    assert clock["tz"] == ZoneInfo("Asia/Hong_Kong")
    # 02:00-06:00 HKT on the 22nd and the 23rd; the 21st's is before the window.
    assert clock["wocl"] == [
        {"start": "2026-10-21T18:00:00Z", "end": "2026-10-21T22:00:00Z"},
        {"start": "2026-10-22T18:00:00Z", "end": "2026-10-22T22:00:00Z"},
    ]
    # 04:00 HKT on every Hong Kong date the window touches - the 21st to
    # the 23rd - and one either side, so one falls before it and one after.
    assert clock["troughs"] == [
        "2026-10-19T20:00:00Z", "2026-10-20T20:00:00Z", "2026-10-21T20:00:00Z",
        "2026-10-22T20:00:00Z", "2026-10-23T20:00:00Z",
    ]


def test_the_wocl_is_clipped_to_the_window():
    clock = frms.body_clock(utc(2026, 10, 21, 19), utc(2026, 10, 21, 21), CONFIG)
    assert clock["wocl"] == [{"start": "2026-10-21T19:00:00Z", "end": "2026-10-21T21:00:00Z"}]


def test_the_body_clock_keeps_home_time_across_a_clock_change():
    # London's summer time ends at 01:00Z on 25 Oct 2026.
    clock = frms.body_clock(utc(2026, 10, 23, 12), utc(2026, 10, 26, 12),
                            config(home_base="LHR", timezone="Europe/London"))

    assert clock["wocl"] == [
        {"start": "2026-10-24T01:00:00Z", "end": "2026-10-24T05:00:00Z"},   # 02:00-06:00 BST
        {"start": "2026-10-25T02:00:00Z", "end": "2026-10-25T06:00:00Z"},   # 02:00-06:00 GMT, the day it ends
        {"start": "2026-10-26T02:00:00Z", "end": "2026-10-26T06:00:00Z"},
    ]
    # 04:00 local, an hour earlier in UTC while summer time lasts.
    assert clock["troughs"] == [
        "2026-10-22T03:00:00Z", "2026-10-23T03:00:00Z", "2026-10-24T03:00:00Z",
        "2026-10-25T04:00:00Z", "2026-10-26T04:00:00Z", "2026-10-27T04:00:00Z",
    ]


def test_cbt_min_defaults_to_four_and_sets_the_troughs():
    window = (utc(2026, 10, 21), utc(2026, 10, 22))
    absent = frms.body_clock(*window, config(cbt_min=None))
    assert absent["troughs"] == frms.body_clock(*window, CONFIG)["troughs"]

    later = frms.body_clock(*window, config(cbt_min="05:30"))
    assert later["troughs"][0] == "2026-10-19T21:30:00Z"


@pytest.mark.parametrize("cbt_min", ["4am", "25:00", "04:60", "0400", 4])
def test_a_cbt_min_that_isnt_hh_mm_is_refused(cbt_min):
    with pytest.raises(ValueError, match="cbt_min"):
        frms.body_clock(utc(2026, 10, 21), utc(2026, 10, 22), config(cbt_min=cbt_min))


@pytest.mark.parametrize("tz", [None, "", "Mars/Olympus_Mons", "../etc"])
def test_a_home_timezone_missing_or_unknown_is_refused(tz):
    with pytest.raises(ValueError, match="timezone"):
        frms.body_clock(utc(2026, 10, 21), utc(2026, 10, 22), config(timezone=tz))


# ---------- the curve ----------


def test_the_curve_is_low_at_a_trough_and_high_midway():
    troughs = frms.body_clock(utc(2026, 10, 21), utc(2026, 10, 23), CONFIG)["troughs"]
    trough = utc(2026, 10, 21, 20)

    assert frms.curve_value(trough, troughs) == -1
    assert frms.curve_value(trough + timedelta(hours=12), troughs) == pytest.approx(1, abs=1e-9)
    assert frms.curve_value(trough + timedelta(hours=6), troughs) == pytest.approx(0, abs=1e-9)
    assert frms.curve_value(trough + timedelta(hours=18), troughs) == pytest.approx(0, abs=1e-9)
    # The last trough is on the curve; before the first and after the last isn't.
    assert frms.curve_value(utc(2026, 10, 23, 20), troughs) == -1
    assert frms.curve_value(utc(2026, 10, 19, 19, 59), troughs) is None
    assert frms.curve_value(utc(2026, 10, 23, 20, 1), troughs) is None
    assert frms.curve_value(trough, []) is None


def test_the_curve_takes_troughs_as_datetimes_too():
    troughs = [utc(2026, 10, 21, 20), utc(2026, 10, 22, 20)]
    assert frms.curve_value(utc(2026, 10, 22, 2), troughs) == pytest.approx(0, abs=1e-9)


# ---------- the Sydney layover, worked by hand ----------
#
# Home HKG. Flown out to Sydney, CX101, landing on the 21st; rostered
# home, CX100, departing at 08:00 Sydney time on the 23rd - which is
# 05:00 at home, inside the WOCL.

NOW = utc(2026, 10, 22)
WINDOW = (utc(2026, 10, 20), utc(2026, 10, 24))
OUT = flight("HKG-SYD", "2026-10-20T13:00:00Z", "2026-10-20T22:30:00Z", "CX101")
HOME = flight("SYD-HKG", "2026-10-22T21:00:00Z", "2026-10-23T06:30:00Z", "CX100")


def layover():
    return [HOME], [OUT], NOW, *WINDOW


def test_the_layover_where_you_are():
    assert frms.locations(*layover(), CONFIG) == [
        {"start": "2026-10-20T00:00:00Z", "end": "2026-10-20T13:00:00Z", "port": None},
        {"start": "2026-10-20T13:00:00Z", "end": "2026-10-22T21:00:00Z", "port": "SYD"},
        {"start": "2026-10-22T21:00:00Z", "end": "2026-10-24T00:00:00Z", "port": "HKG"},
    ]


def test_the_layover_location_strip_is_sydneys_sky():
    strip = frms.location_strip(frms.locations(*layover(), CONFIG), *WINDOW, CONFIG)

    assert_contiguous(strip, *WINDOW)
    assert tone_at(strip, utc(2026, 10, 20, 12)) == "none"     # before the first duty
    assert tone_at(strip, utc(2026, 10, 21, 2)) == "day"       # 13:00 AEDT
    assert tone_at(strip, utc(2026, 10, 21, 12)) == "night"    # 23:00 AEDT
    assert tone_at(strip, utc(2026, 10, 21, 20)) == "day"      # 07:00 AEDT
    assert {s["port"] for s in strip} == {None, "SYD", "HKG"}
    unknown = [s for s in strip if s["port"] is None]
    assert unknown == [{"start": "2026-10-20T00:00:00Z", "end": "2026-10-20T13:00:00Z",
                        "port": None, "tone": "none"}]


def test_the_layover_body_strip_is_home_time():
    strip = frms.body_strip(*WINDOW, CONFIG)

    assert_contiguous(strip, *WINDOW)
    assert tone_at(strip, utc(2026, 10, 21, 2)) == "day"       # 10:00 HKT
    assert tone_at(strip, utc(2026, 10, 21, 12)) == "night"    # 20:00 HKT
    assert tone_at(strip, utc(2026, 10, 21, 20)) == "night"    # 04:00 HKT


def test_the_layover_duties_are_cut_against_the_body_clock():
    pieces = frms.duty_pieces(*layover(), CONFIG)

    assert [(p["code"], p["tone"]) for p in pieces] == [
        ("CX101", "night"), ("CX101", "wocl"), ("CX101", "night"), ("CX101", "day"),
        ("CX100", "wocl"), ("CX100", "night"), ("CX100", "day"),
    ]
    out, home = pieces[:4], pieces[4:]
    # Each duty's pieces run start to end, unbroken.
    for one in (out, home):
        for a, b in zip(one, one[1:]):
            assert a["end"] == b["start"]

    # CX101: from its pre-duty time - two hours before its 21:00 HKT
    # report, 19:00 HKT, after Hong Kong's sunset - home darkness, the
    # WOCL (02:00-06:00 HKT), darkness until Hong Kong's sunrise (06:22
    # HKT on the 21st), then day.
    assert [(p["start"], p["end"]) for p in out[:2]] == [
        ("2026-10-20T11:00:00Z", "2026-10-20T18:00:00Z"),
        ("2026-10-20T18:00:00Z", "2026-10-20T22:00:00Z"),
    ]
    assert out[2]["start"] == "2026-10-20T22:00:00Z"
    assert near(at(out[2]["end"]), utc(2026, 10, 20, 22, 22))
    assert out[3]["end"] == "2026-10-20T22:30:00Z"

    # CX100, home from the layover - its pre-duty time as much as the
    # outbound's: the WOCL from 03:00 HKT, two hours before its 05:00 HKT
    # report, darkness until Hong Kong's sunrise (06:23 HKT on the 23rd),
    # day to its end - 14:30 HKT, before sunset.
    assert (home[0]["start"], home[0]["end"]) == ("2026-10-22T19:00:00Z", "2026-10-22T22:00:00Z")
    assert home[1]["start"] == "2026-10-22T22:00:00Z"
    assert near(at(home[1]["end"]), utc(2026, 10, 22, 22, 23))
    assert home[2]["end"] == "2026-10-23T06:30:00Z"

    # The point of the panel: CX100 starts in the WOCL though the sun is
    # up in Sydney - 08:00 AEDT - where it leaves from.
    sydney = frms.daylight(PORTS["SYD"]["lat"], PORTS["SYD"]["lon"], *WINDOW)
    assert tone_at(sydney, utc(2026, 10, 22, 21)) == "day"
    sydney_rise = [s for s in sydney if s["tone"] == "day" and s["start"].startswith("2026-10-21")]
    assert near(at(sydney_rise[0]["start"]), utc(2026, 10, 21, 19, 5))
    assert near(at(sydney_rise[0]["end"]), utc(2026, 10, 22, 8, 13))


# ---------- the rules ----------


def test_a_day_return_never_leaves_base():
    # HKG-BKK-HKG in one duty: its arr is its final destination, base.
    day_return = flight("HKG-HKG", "2026-10-20T01:00:00Z", "2026-10-20T09:00:00Z", "CX701")
    sim = duty("sim", "2026-10-19T02:00:00Z", "2026-10-19T06:00:00Z", "A1534")
    start, end = utc(2026, 10, 19, 12), utc(2026, 10, 21)

    assert frms.locations([], [sim, day_return], NOW, start, end, CONFIG) == [
        {"start": "2026-10-19T12:00:00Z", "end": "2026-10-21T00:00:00Z", "port": "HKG"},
    ]


@pytest.mark.parametrize("kind, code", [("sim", "A1534"), ("reserve", "R1E"), ("other", "MED")])
def test_a_duty_that_isnt_a_flight_is_at_home_base(kind, code):
    out = flight("HKG-SYD", "2026-10-19T13:00:00Z", "2026-10-19T22:30:00Z", "CX101")
    ground = duty(kind, "2026-10-20T08:00:00Z", "2026-10-20T12:00:00Z", code)

    assert frms.locations([], [out, ground], NOW, *WINDOW, CONFIG) == [
        {"start": "2026-10-20T00:00:00Z", "end": "2026-10-20T08:00:00Z", "port": "SYD"},
        {"start": "2026-10-20T08:00:00Z", "end": "2026-10-24T00:00:00Z", "port": "HKG"},
    ]


def test_a_flight_with_no_arr_leaves_you_where_you_were():
    out = flight("HKG-SYD", "2026-10-20T01:00:00Z", "2026-10-20T10:00:00Z", "CX101")
    routeless = duty("flight", "2026-10-21T01:00:00Z", "2026-10-21T03:00:00Z", "CX999")

    assert frms.locations([], [out, routeless], NOW, *WINDOW, CONFIG) == [
        {"start": "2026-10-20T00:00:00Z", "end": "2026-10-20T01:00:00Z", "port": None},
        {"start": "2026-10-20T01:00:00Z", "end": "2026-10-24T00:00:00Z", "port": "SYD"},
    ]


def test_with_nothing_flown_the_past_is_unknown_never_the_plan():
    # Only the roster: one duty over before now, one still to come.
    past = flight("HKG-SYD", "2026-10-20T13:00:00Z", "2026-10-20T22:30:00Z", "CX101")
    rostered = [past, HOME]

    assert frms.locations(rostered, [], NOW, *WINDOW, CONFIG) == [
        {"start": "2026-10-20T00:00:00Z", "end": "2026-10-22T21:00:00Z", "port": None},
        {"start": "2026-10-22T21:00:00Z", "end": "2026-10-24T00:00:00Z", "port": "HKG"},
    ]
    pieces = frms.duty_pieces(rostered, [], NOW, *WINDOW, CONFIG)
    assert pieces and all(at(p["start"]) >= NOW for p in pieces)
    assert {p["code"] for p in pieces} == {"CX100"}


def test_a_rostered_duty_under_way_counts_and_one_over_doesnt():
    over = duty("reserve", "2026-10-21T00:00:00Z", "2026-10-21T06:00:00Z", "R1E")
    under_way = flight("HKG-SYD", "2026-10-21T20:00:00Z", "2026-10-22T05:00:00Z", "CX101")
    # A flown duty still to come can't be - but if one were, it's no count either.
    early = flight("HKG-BKK", "2026-10-22T10:00:00Z", "2026-10-22T13:00:00Z", "CX701")

    assert frms.locations([over, under_way], [early], NOW, *WINDOW, CONFIG) == [
        {"start": "2026-10-20T00:00:00Z", "end": "2026-10-21T20:00:00Z", "port": None},
        {"start": "2026-10-21T20:00:00Z", "end": "2026-10-24T00:00:00Z", "port": "SYD"},
    ]
    pieces = frms.duty_pieces([over, under_way], [early], NOW, *WINDOW, CONFIG)
    assert {p["code"] for p in pieces} == {"CX101"}
    # From its pre-duty time, two hours before its report.
    assert pieces[0]["start"] == "2026-10-21T18:00:00Z" and pieces[-1]["end"] == "2026-10-22T05:00:00Z"


def test_a_duty_before_the_window_says_where_it_starts():
    out = flight("HKG-SYD", "2026-10-18T13:00:00Z", "2026-10-18T22:30:00Z", "CX101")
    assert frms.locations([], [out], NOW, *WINDOW, CONFIG) == [
        {"start": "2026-10-20T00:00:00Z", "end": "2026-10-24T00:00:00Z", "port": "SYD"},
    ]


def test_a_session_with_no_end_counts_for_nothing():
    open_ended = host.Session("2026-10-20T01:00:00Z", None, "garmin", {"type": "flight", "arr": "SYD"})
    assert frms.locations([open_ended], [open_ended], NOW, *WINDOW, CONFIG) == [
        {"start": "2026-10-20T00:00:00Z", "end": "2026-10-24T00:00:00Z", "port": None},
    ]
    assert frms.duty_pieces([open_ended], [open_ended], NOW, *WINDOW, CONFIG) == []


def test_a_port_with_no_position_is_known_but_not_drawn():
    out = flight("HKG-XXX", "2026-10-20T13:00:00Z", "2026-10-20T22:30:00Z", "CX101")
    places = {**PORTS, "BKK": {"city": "Bangkok", "tz": "Asia/Bangkok"}}   # no lat and lon
    on_to = flight("XXX-BKK", "2026-10-21T13:00:00Z", "2026-10-21T15:00:00Z", "CX102")
    cfg = config(destinations=places)

    locs = frms.locations([], [out, on_to], NOW, *WINDOW, cfg)
    assert [loc["port"] for loc in locs] == [None, "XXX", "BKK"]
    strip = frms.location_strip(locs, *WINDOW, cfg)
    assert [(s["port"], s["tone"]) for s in strip] == [(None, "none"), ("XXX", "none"), ("BKK", "none")]
    assert_contiguous(strip, *WINDOW)


@pytest.mark.parametrize("places", [
    {k: v for k, v in PORTS.items() if k != "HKG"},                  # not there
    {**PORTS, "HKG": {"city": "Hong Kong", "tz": "Asia/Hong_Kong"}},  # no lat and lon
    {**PORTS, "HKG": {"lat": "22.31", "lon": 113.92}},               # not a number
])
def test_a_home_base_with_no_position_is_refused_by_name(places):
    cfg = config(destinations=places)
    with pytest.raises(ValueError, match="HKG"):
        frms.body_strip(*WINDOW, cfg)
    with pytest.raises(ValueError, match="HKG"):
        frms.duty_pieces(*layover(), cfg)
    with pytest.raises(ValueError, match="HKG"):
        frms.circadian(*layover(), cfg)


def test_a_window_must_be_aware():
    with pytest.raises(ValueError, match="aware"):
        frms.body_strip(datetime(2026, 10, 20), datetime(2026, 10, 24), CONFIG)


def test_an_empty_window_has_nothing_in_it():
    start, end = WINDOW[1], WINDOW[0]
    assert frms.locations([HOME], [OUT], NOW, start, end, CONFIG) == []
    assert frms.location_strip([], start, end, CONFIG) == []
    assert frms.body_strip(start, end, CONFIG) == []
    assert frms.duty_pieces([HOME], [OUT], NOW, start, end, CONFIG) == []
    assert frms.body_clock(start, end, CONFIG)["wocl"] == []


# ---------- the pre-duty time ----------


def starts(pieces):
    """Each code's first start, in order."""
    found = {}
    for p in pieces:
        found.setdefault(p["code"], p["start"])
    return found


@pytest.mark.parametrize("kind, code", [("sim", "A1534"), ("reserve", "R1E"), ("other", "MED")])
def test_only_a_flight_has_a_pre_duty_time(kind, code):
    # Each at 12:00 HKT, in the day: only the flight starts two hours early.
    out = flight("HKG-BKK", "2026-10-20T04:00:00Z", "2026-10-20T07:00:00Z", "CX701")
    ground = duty(kind, "2026-10-21T04:00:00Z", "2026-10-21T08:00:00Z", code)

    pieces = frms.duty_pieces([], [out, ground], NOW, *WINDOW, CONFIG)
    assert starts(pieces) == {"CX701": "2026-10-20T02:00:00Z", code: "2026-10-21T04:00:00Z"}


def test_a_day_return_is_one_duty_so_one_pre_duty_time():
    # HKG-NRT-HKG: one duty, 08:00-17:00 HKT - the wake-up before it from
    # 06:00 HKT, darkness until Hong Kong's sunrise (06:22), then day; no
    # second pre-duty time before the sector home.
    turn = flight("HKG-HKG", "2026-10-21T00:00:00Z", "2026-10-21T09:00:00Z", "CX500")

    pieces = frms.duty_pieces([], [turn], NOW, *WINDOW, CONFIG)
    assert [(p["code"], p["tone"]) for p in pieces] == [("CX500", "night"), ("CX500", "day")]
    assert pieces[0]["start"] == "2026-10-20T22:00:00Z" and pieces[-1]["end"] == "2026-10-21T09:00:00Z"
    assert near(at(pieces[0]["end"]), utc(2026, 10, 20, 22, 22))


def test_the_pre_duty_time_is_cut_at_the_duty_before():
    # A sim to 10:00 HKT, a flight reporting at 11:00: its wake-up can't
    # start before the sim ends.
    sim = duty("sim", "2026-10-20T22:00:00Z", "2026-10-21T02:00:00Z", "A1534")
    out = flight("HKG-BKK", "2026-10-21T03:00:00Z", "2026-10-21T06:00:00Z", "CX701")

    pieces = frms.duty_pieces([], [sim, out], NOW, *WINDOW, CONFIG)
    assert starts(pieces) == {"A1534": "2026-10-20T22:00:00Z", "CX701": "2026-10-21T02:00:00Z"}
    for a, b in zip(pieces, pieces[1:]):
        assert a["end"] <= b["start"], (a, b)


def test_a_duty_running_past_the_report_leaves_no_pre_duty_time():
    # Two flights back to back, the first ending after the second reports
    # (the roster's own overlap): the second starts at its report.
    first = flight("HKG-BKK", "2026-10-20T04:00:00Z", "2026-10-20T09:00:00Z", "CX701")
    second = flight("BKK-HKG", "2026-10-20T08:00:00Z", "2026-10-20T12:00:00Z", "CX702")

    pieces = frms.duty_pieces([], [first, second], NOW, *WINDOW, CONFIG)
    assert starts(pieces) == {"CX701": "2026-10-20T02:00:00Z", "CX702": "2026-10-20T08:00:00Z"}


def test_the_pre_duty_time_is_clipped_to_the_window():
    # Reporting 01:00 UTC on the window's first day: its wake-up started
    # before the window, so its first piece starts at the window's start.
    out = flight("HKG-BKK", "2026-10-20T01:00:00Z", "2026-10-20T04:00:00Z", "CX701")

    pieces = frms.duty_pieces([], [out], NOW, *WINDOW, CONFIG)
    assert pieces[0]["start"] == "2026-10-20T00:00:00Z"


@pytest.mark.parametrize("minutes, first", [(0, "2026-10-20T04:00:00Z"), (60, "2026-10-20T03:00:00Z"),
                                            (120, "2026-10-20T02:00:00Z")])
def test_the_config_sets_the_pre_duty_time(minutes, first):
    out = flight("HKG-BKK", "2026-10-20T04:00:00Z", "2026-10-20T07:00:00Z", "CX701")
    cfg = {**CONFIG, "sleep": {"pre_duty_minutes": minutes}}

    assert frms.duty_pieces([], [out], NOW, *WINDOW, cfg)[0]["start"] == first


@pytest.mark.parametrize("value", [-1, 1441, "120", 90.0, True, None])
def test_a_bad_pre_duty_time_is_refused(value):
    cfg = {**CONFIG, "sleep": {"pre_duty_minutes": value}}
    with pytest.raises(ValueError, match="config.json's sleep pre_duty_minutes must be a whole number of minutes, 0 to 1440"):
        frms.duty_pieces(*layover(), cfg)


# ---------- the bundle ----------


def test_circadian_is_each_function_for_the_same_inputs():
    rostered, flown, now, start, end = layover()
    bundle = frms.circadian(rostered, flown, now, start, end, CONFIG)
    clock = frms.body_clock(start, end, CONFIG)

    assert bundle == {
        "home": "HKG",
        "location": frms.location_strip(frms.locations(rostered, flown, now, start, end, CONFIG),
                                        start, end, CONFIG),
        "body": frms.body_strip(start, end, CONFIG),
        "duties": frms.duty_pieces(rostered, flown, now, start, end, CONFIG),
        "wocl": clock["wocl"],
        "troughs": clock["troughs"],
    }


def test_the_engine_reads_the_apps_own_config_by_default():
    # The fixture's config.json has no positions: the body strip says so,
    # naming its home base - the engine read CONFIG, not nothing.
    with pytest.raises(ValueError, match="ZZA"):
        frms.body_strip(*WINDOW)
    assert frms.body_clock(*WINDOW)["tz"] == ZoneInfo("UTC")
