"""FRMS — one app, one tile, one config.

The FRMS suite is this one app: one tile, 6 wide by 4 tall (app.yaml's
only size), one config.json. render() draws the tile as four sub-panels
on a 6x4 grid (ctx.grid()), each drawn as a tile of its own: circadian
top-left (3x2) - three strips, a day across - the roster bottom-left
(2x2) - the timeline and its Plan and Report rails - and two more to
come, top-right (3x2) and bottom-right (4x2), placeholders until
they're built. The circadian strips and the roster's timeline and rails
are one focus group, "frms": scroll either and the other follows, to the
same time.

Each panel is built on its own (_panel()): one that raises - a config
without the positions the circadian panel needs, say - is an error in
its own sub-panel, logged, and the others are drawn as ever.

All of the app's code lives in this one file, by design: the host
imports app.py by its path - nothing added to sys.path, so a module
beside it couldn't be imported by name - and reloads the app only when
app.yaml or app.py changes, so code in any other file would go stale.
What's specific to an airline or an owner is config.json's.

The roster panel takes a duty roster's .ics. The file picker is
OpenFit's; the format is this app's. Save hands handle() the roster
picked - an iCalendar (.ics) export, a Cathay-style one in mind - as a
FileUpload, and the host never reads it: this app unfolds and parses it
itself, with the standard library alone.

What's a duty is structural, never a list of codes: an event with real
times (a DTSTART and a DTEND, in UTC) is a work duty, and is filed; an
all-day one (DTSTART;VALUE=DATE) is a day off or leave - O, G, U, L, or
whatever code the roster adds - and is skipped. A duty is labelled from
its title's code - a CX flight, an R.. reserve, an A#### sim, else
other - but the label never decides whether it's filed: a code never
seen before is filed as "other", the code kept.

Each duty is a session of its DTSTART..DTEND - its envelope, already in
UTC; the description's local times aren't read for it, only kept,
verbatim - under duties_complete if it had ended when the roster was
uploaded, else duties_rostered. The sessions are all it files: duty
hours, when a report needs them, are read from them.

Backfill, a checkbox beside the picker, is for a roster uploaded after
it started: a duty already over is filed into the rostered plan as well
as the flown record - its rostered copy captured "backfill", so a
reconstructed plan is always told from one captured live. A duty still
to come is filed once, backfill or not. Ticked, the page asks first: a
backfilled duty replaces any plan already uploaded for it.

The roster panel reads the duties back (ctx.sessions()) - about six
months of them, and all still to come - and draws them on a timeline over the
picker: the roster on the left (headed Roster), what's complete - flown
- on the right (Complete), each duty a block at its time and labelled
with its code, a line at now and the complete side greyed after it. Behind the blocks, each lane shades its trips away
from home base (trips()): a flight out of base to the next flight back,
any sectors between inside it - the layovers.

The same trips, read from both lanes at once and with day returns among
them, are the cards of the Plan and Report rails either side of the
timeline (trip_cards()): a whole trip out and back each - its flights
out and home, its port (a day return's, its turn-round) and the day it
left. Every trip not over yet is on the Plan rail, beside the rostered
lane, looking ahead of the cursor; every trip home on the Report rail,
beside the flown, looking back. Each rail shows five at a time,
restacking as the timeline scrolls.

Uploading again replaces a duty of the same kind and start; it removes
nothing, so a duty taken off the roster keeps its row until another
lands on its start.

All or nothing: a refusal - no duties, a time not in UTC - writes
nothing; the host rolls back whatever was already filed.

The parsing, labelling, routing and trip-finding are module functions -
parse_events(), duties(), classify() (a flight's dep and arr its origin
and final destination, first leg to last, the description's legs before
the title's), bucket_for(), filed_as(), trips(), trip_cards() - pure,
for a test to call on their own.

The circadian panel (_circadian()) draws, over the roster panel's
window - about six months back to a day past the last duty, or now -
three strips (ctx.strips()), in the FRMS focus group: where the crew
member is, day or night there, the body clock's curve over it - red
under it in the WOCL - and its tag the port under the cursor and its
local time (its destinations tz); day or night at home base, tagged
Home over home's date and time under the cursor; and the duties, each
cut into day, night and WOCL pieces, its code once, on its first piece,
with each trip away from base behind them - the roster's trips(), over
the duties that count - an amber bar, its port in it.

Its engine is pure module functions, for a test to call on their own:
sun_times() - a day's sunrise and sunset at a position, from NOAA's
sunrise equation, or "day" or "night" when the sun doesn't set or rise;
daylight() - day and night there over a window; body_clock() - the home
time zone, its WOCL (02:00 to 06:00 home local) and the troughs of the
body clock's curve (cbt_min, home local); curve_value() - the curve at
an instant, -1 at a trough and +1 midway; locations() - where the crew
member is; location_strip() - day and night there; body_strip() - day
and night at home base; duty_pieces() - each duty cut into wocl, night
and day pieces; and circadian(), all of them at once. The body clock is
home base's, always - no acclimatisation, by design - and a duty's
pieces are coloured against it alone, never the sun where it's flown.
Where you are comes from the flown duties over by now and the rostered
ones ending after it - never a past rostered one, so a past with nothing
flown is unknown, not the plan: from the moment a duty starts you're at
its final destination - a flight's arr (where you were, without one),
home base for any other duty. Times come back as UTC text, every span
half-open.

The sleep panel (_sleep()) draws, over the same window and three days
across (ctx.strips(hours=72)), in the FRMS focus group: the sleep
sessions' stage timelines - each stage a bar its height, green inside
the body's rest window and red outside it; the rest window itself, home
local (22:00 to 08:00 unless the config's personal rest_start and
rest_end say otherwise), tagged Body clock over home's date and time
under the cursor; and the disturbed cycles - each stretch a flown duty
overlapped the rest window, cut into 90-minute slots, a slot with under
15 minutes of sleep in it marked with red ticks. Its engine, pure as
the circadian one: rest_windows() - the rest window each day;
rest_strip() - rest and wake over a window; sleep_nights() - the sleep
sessions with a stage timeline, one a night; stage_pieces() - their
stages to draw, cut at the rest window's edges; disturbed_slots() - each
duty's slots in the rest window and the sleep in each. Like the body
clock, the rest window is home's, always.
"""

import bisect
import json
import math
import os
import re
import traceback
from datetime import date, datetime, time, timedelta, timezone
from typing import NamedTuple
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ROSTERED = "duties_rostered"
COMPLETE = "duties_complete"

NON_UTC = "roster has a non-UTC time, which isn't supported yet: {}"

# The upload's companion checkbox: past duties into the plan as well.
BACKFILL = {
    "key": "backfill", "label": "Backfill",
    "confirm": "Backfill past duties into your rostered plan? They're also recorded as "
               "flown, and any plan you already uploaded for those duties is replaced.",
}


# The roster config, read once from config.json beside this file (an
# install file, changed by reinstalling or Update): the duty-code rules
# and the owner's home base and timezone. app.py is the engine; the
# airline- and person-specific reference lives in the config.
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
try:
    with open(_CONFIG_PATH, encoding="utf-8") as _f:
        CONFIG = json.load(_f)
    if not isinstance(CONFIG, dict) or not isinstance(CONFIG.get("duty_types"), list):
        CONFIG, CONFIG_ERROR = None, "config.json needs a duty_types list"
    else:
        CONFIG_ERROR = None
except FileNotFoundError:
    CONFIG, CONFIG_ERROR = None, "config.json is missing — reinstall the app with one"
except (OSError, ValueError) as _e:
    CONFIG, CONFIG_ERROR = None, f"config.json won't read: {_e}"


def render(ctx, cfg):
    return ctx.grid(6, 4, [
        {"x": 0, "y": 0, "w": 3, "h": 2, "spec": _panel(ctx, _circadian)},
        {"x": 3, "y": 0, "w": 3, "h": 2, "spec": _panel(ctx, _sleep)},
        {"x": 0, "y": 2, "w": 2, "h": 2, "spec": _panel(ctx, _roster)},
        {"x": 2, "y": 2, "w": 4, "h": 2, "spec": ctx.empty("Coming soon")},
    ])


def _panel(ctx, build):
    """One sub-panel's spec: build(ctx) - or, should it raise, an error in
    its place, the traceback logged as the host logs a failed render, so
    a fault in one panel leaves the others drawn."""
    try:
        return build(ctx)
    except Exception as e:
        print(f"[apps] frms: the {build.__name__.strip('_')} panel failed")
        traceback.print_exc()
        return ctx.error(str(e) or type(e).__name__)


def _circadian(ctx):
    """The circadian panel, its sub-panel's spec: three strips over the
    roster's window - where you are, day or night there, the body clock's
    curve over it, red under it in the WOCL, and the place and its time
    under the cursor; day or night at home, tagged Home over home's date
    and time under the cursor; each duty cut into day, night and WOCL
    pieces against the body clock, its code once at its start, and behind
    them each trip away from base - the roster's trips(), its layover
    bands' logic, over the duties that count - an amber bar, its port in
    it. In the FRMS focus group, so it scrolls with the roster's timeline
    and rails, and they with it. Before any duty, the ask, as the roster
    panel's."""
    if CONFIG is None:
        return ctx.empty(CONFIG_ERROR)
    # The roster panel's window - about six months back, and every duty
    # still to come - to the latest duty's end or now, and a day past it.
    since = date.fromisoformat(ctx.today()) - timedelta(days=183)
    rostered = ctx.sessions(ROSTERED, since=since.isoformat())
    flown = ctx.sessions(COMPLETE, since=since.isoformat())
    if not rostered and not flown:
        return ctx.empty("Upload your roster .ics")
    now = datetime.now(timezone.utc)
    ends = [_utc(s.end, "a duty's end") for s in (*rostered, *flown) if s.end is not None]
    start = datetime.combine(since, time(), tzinfo=timezone.utc)
    end = max([now, *ends]) + timedelta(days=1)
    c = circadian(rostered, flown, now, start, end)

    # Where you are: its sky, and the port and its time zone for the
    # clock under the cursor - the zone the config's, the page's to read.
    places = _destinations(CONFIG)
    local = []
    for x in c["location"]:
        place = places.get(x["port"]) if x["port"] else None
        tz = place.get("tz") if isinstance(place, dict) else None
        local.append({"start": x["start"], "end": x["end"], "tone": x["tone"], "place": x["port"] or "",
                      "tz": tz if isinstance(tz, str) and tz else None})
    duty, prev = [], None
    for x in c["duties"]:
        # One label a duty: on its first piece, not on the pieces that
        # continue it - so a duty clipped at the window's start labels
        # its first piece in it.
        first = not (prev and prev["code"] == x["code"] and prev["end"] == x["start"])
        duty.append({"start": x["start"], "end": x["end"], "tone": x["tone"], "label": x["code"] if first else None})
        prev = x
    # Home: its sky, and home base and its zone for the date and time
    # under the cursor.
    home, home_tz = c["home"], _personal(CONFIG).get("timezone")
    # The roster's trips - its layover bands' logic - over the duties
    # that count, so a trip under way (flown out, rostered home) is one;
    # one still away at the window's end runs to it.
    away = [_band(t) for t in trips(_counted(rostered, flown, now), home, _utc_text(end))]
    return ctx.strips([
        {"kind": "sky", "clock": True, "spans": local, "troughs": c["troughs"], "wocl": c["wocl"]},
        {"kind": "sky", "label": "Home", "clock": "date",
         "spans": [{"start": x["start"], "end": x["end"], "tone": x["tone"], "place": home, "tz": home_tz}
                   for x in c["body"]]},
        {"kind": "duty", "spans": duty, "bands": away},
    ], now=now, sync="frms")


def _sleep(ctx):
    """The sleep panel, its sub-panel's spec: three strips over the roster's
    window, three days across - each night's sleep stages, a bar each its
    height, green in the body's rest window and red out of it; the rest
    window itself, tagged Body clock over home's date and time under the
    cursor; and the disturbed cycles, each 90-minute slot of a flown duty
    in the rest window with under 15 minutes of sleep in it, red ticks.
    In the FRMS focus group, so it scrolls with the other panels by time.
    Before any sleep with stages, the ask."""
    if CONFIG is None:
        return ctx.empty(CONFIG_ERROR)
    since = date.fromisoformat(ctx.today()) - timedelta(days=183)
    # A night that began the evening before the window's first day is in it.
    nights = sleep_nights(ctx.sessions(SLEEP, since=(since - timedelta(days=1)).isoformat()))
    if not nights:
        return ctx.empty("No sleep stages yet - sync a sleep source that gives them")
    rostered = ctx.sessions(ROSTERED, since=since.isoformat())
    flown = ctx.sessions(COMPLETE, since=since.isoformat())
    # The circadian panel's window, so the two scroll over one axis: to
    # the latest duty's end (or sleep's, or now) and a day past it.
    now = datetime.now(timezone.utc)
    ends = [_utc(s.end, "a duty's end") for s in (*rostered, *flown) if s.end is not None]
    ends += [_utc(n["end"], "a sleep's end") for n in nights]
    start = datetime.combine(since, time(), tzinfo=timezone.utc)
    end = max([now, *ends]) + timedelta(days=1)
    windows = rest_windows(start, end)
    home, home_tz = _personal(CONFIG).get("home_base"), _personal(CONFIG).get("timezone")
    slots = disturbed_slots(flown, nights, windows, now)
    return ctx.strips([
        {"kind": "sleep", "label": "Sleep stages", "spans": stage_pieces(nights, windows)},
        {"kind": "sky", "label": "Body clock", "clock": "date",
         "spans": [{**x, "place": home, "tz": home_tz} for x in rest_strip(start, end)]},
        {"kind": "ticks", "label": "Disturbed cycles",
         "spans": [{"start": x["start"], "end": x["end"], "tone": "disturbed"} for x in slots if x["disturbed"]]},
    ], now=now, sync="frms", hours=72)


def _roster(ctx):
    """The roster panel, its sub-panel's spec: the timeline between the
    Plan and Report rails over the upload - or, before any duty, the ask
    over the picker."""
    picker = ctx.input("file", "upload", label="Upload roster", kind="file", accept=".ics", toggle=BACKFILL)
    if CONFIG is None:
        return ctx.panel(ctx.empty(CONFIG_ERROR), picker)
    # About six months back, and every duty still to come.
    since = (date.fromisoformat(ctx.today()) - timedelta(days=183)).isoformat()
    rostered = ctx.sessions(ROSTERED, since=since)
    flown = ctx.sessions(COMPLETE, since=since)
    if not rostered and not flown:
        return ctx.panel(ctx.empty("Upload your roster .ics"), picker)

    # Once: the now line, and where a trip flown out and not yet back ends.
    now = datetime.now(timezone.utc)
    home_base = _home_base()
    # A lane's trips, each a band behind its blocks: a rostered trip
    # still away when the loaded roster runs out ends there; a flown one,
    # out and not back, ends now - the layover you're on.
    rostered_end = max((s.end for s in rostered if s.end is not None), default=None)
    rostered_trips = trips(rostered, home_base, rostered_end)
    flown_trips = trips(flown, home_base, _utc_text(now))

    # The roster on the left, what's complete - flown - on the right,
    # greyed after now, when nothing's flown yet. The timeline takes the rest of the
    # tile and scrolls, laid out to whatever size the tile is (render()
    # isn't told it); the picker at its foot. It's in the FRMS focus
    # group: the page keeps a cursor on it, and scrolls every timeline
    # of the group to one time.
    timeline = ctx.timeline(
        [
            _lane(rostered, label="Roster", bands=[_band(t) for t in rostered_trips]),
            _lane(flown, label="Complete", grey_after_now=True, bands=[_band(t) for t in flown_trips]),
        ],
        now=now,
        sync="frms",
    )
    # Flanking it, the trips as cards, read from both lanes at once: the
    # Plan rail beside the rostered lane - every trip not over yet, the
    # one under way and the one not home as far as the roster runs among
    # them - looking ahead of the cursor; and the Report rail beside the
    # flown, every trip home, looking back. Both in the FRMS group: they
    # restack as the timeline scrolls.
    cards = trip_cards(rostered, flown, home_base)
    ahead = _utc_text(now)
    plan = [_card(c) for c in cards if c["end"] is None or c["end"] > ahead]
    report = [_card(c) for c in cards if c["end"] is not None and c["end"] <= ahead]
    return ctx.panel(
        ctx.rails(
            timeline,
            left=ctx.rail(plan, "plan", title="Plan", sync="frms"),
            right=ctx.rail(report, "report", title="Report", sync="frms"),
        ),
        picker,
    )


def _home_base():
    """The owner's home base (IATA) from the config's personal block, or
    None - a config without one shades no trips, its duties still drawn."""
    personal = CONFIG.get("personal")
    return personal.get("home_base") if isinstance(personal, dict) else None


def _utc_text(moment):
    """An aware datetime as a session's start and end are stored -
    2025-11-05T15:10:00Z - so it compares with them as text."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _band(trip):
    """A trip as a band - a timeline lane's, or the duty strip's: its
    span, labelled with the port it flew out to. Its id stays on the
    trip - not the spec's."""
    return {"start": trip["start"], "end": trip["end"], "label": trip["out_port"]}


def _card(trip):
    """A trip_cards() card as a rail card: its span, its flights out and
    home (None while it isn't known: the page draws a dash) and the port
    it's away at - a day return's, its turn-round. Its id is the trip's."""
    return {key: trip[key] for key in ("id", "start", "end", "out", "in", "port")}


def _lane(sessions, **lane):
    """A timeline lane of duty sessions, each a block labelled with its
    code. One with no end - another source's, still open - has no length
    to draw, so it's left out."""
    return {"intervals": [
        {"start": s.start, "end": s.end, "label": s.summary.get("code", "")}
        for s in sessions if s.end is not None
    ], **lane}


def handle(ctx, action, payload):
    if action != "upload":
        raise ValueError(f"unknown action: {action!r}")
    if CONFIG is None:
        raise ValueError(CONFIG_ERROR)
    upload = payload.get("file")
    if not hasattr(upload, "text"):
        raise ValueError("Choose a roster .ics to upload")
    try:
        # utf-8-sig: an export may start with a byte-order mark.
        text = upload.text("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError(f"{upload.name} isn't a text roster") from None
    found = duties(parse_events(text))
    if not found:
        raise ValueError(f"No duties found in {upload.name}")
    # The checkbox's state, a JSON bool: only a true one backfills.
    backfill = payload.get("backfill") is True
    # Once, so every duty in the file is routed against the same instant.
    now = datetime.now(timezone.utc)
    for duty in found:
        for kind, capture in filed_as(duty.end, now, backfill):
            ctx.write_session(kind, duty.start, duty.end, session_summary(duty, capture))


# ---------- reading the .ics ----------


class _Malformed(Exception):
    """An event that can't be read: skipped and logged, never the upload's end."""


# RFC 5545's text escapes: \n or \N a line break; \, \; and \\ themselves.
_ESCAPES = {"n": "\n", "N": "\n", ",": ",", ";": ";", "\\": "\\"}


def _unescape(value):
    return re.sub(r"\\(.)", lambda m: _ESCAPES.get(m.group(1), m.group(0)), value)


def _content_line(line):
    """NAME;PARAM=value...:value as (NAME, {PARAM: value}, value), or None
    for a line that isn't one. The value starts at the first colon outside
    a quoted parameter."""
    quoted = False
    for i, ch in enumerate(line):
        if ch == '"':
            quoted = not quoted
        elif ch == ":" and not quoted:
            break
    else:
        return None
    name, *pairs = line[:i].split(";")
    if not name.strip():
        return None
    params = {}
    for pair in pairs:
        key, _, value = pair.partition("=")
        params[key.strip().upper()] = value.strip().strip('"')
    return name.strip().upper(), params, line[i + 1:]


def _when(params, value, line):
    """A DTSTART or DTEND: a date for an all-day event, else an aware UTC
    datetime. ValueError for a time not in UTC - floating, or in a TZID's
    zone, a timezone lookup away - so the upload is refused rather than a
    duty mistimed; _Malformed for one that isn't a date or a time."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        form, utc = "%Y%m%d", False
    elif "TZID" in params or re.fullmatch(r"\d{8}T\d{6}", value):
        raise ValueError(NON_UTC.format(line.strip()))
    elif re.fullmatch(r"\d{8}T\d{6}Z", value):
        form, utc = "%Y%m%dT%H%M%SZ", True
    else:
        raise _Malformed(f"not a date or a time: {line.strip()!r}")
    try:
        when = datetime.strptime(value, form)
    except ValueError:
        raise _Malformed(f"not a date or a time: {line.strip()!r}") from None
    return when.replace(tzinfo=timezone.utc) if utc else when.date()


def _event(props):
    """One VEVENT, from its properties ({NAME: (params, value, line)}), as
    parse_events() gives it."""
    def text(name):
        return _unescape(props[name][1]) if name in props else ""

    def when(name):
        return _when(*props[name]) if name in props else None

    start, end = when("DTSTART"), when("DTEND")
    all_day = isinstance(start, date) and not isinstance(start, datetime)
    if start is not None and end is not None and all_day == isinstance(end, datetime):
        raise _Malformed("its DTSTART and DTEND aren't both dates or both times")
    return {
        "summary": text("SUMMARY"), "description": text("DESCRIPTION"),
        "start": start, "end": end, "all_day": all_day,
    }


def parse_events(text):
    """The events (VEVENTs) in an iCalendar text, in the file's order.

    Each a dict: summary and description - text, unescaped, "" if absent;
    start and end - a date for an all-day event, an aware UTC datetime for
    a timed one, None if absent; and all_day. A component inside an event
    (an alarm) lends it nothing. An event that can't be read is skipped
    and logged. A timed one not in UTC raises ValueError.
    """
    # Unfold: a line break followed by a space or a tab continues the line.
    text = re.sub(r"\r?\n[ \t]", "", text)
    events, stack, props = [], [], {}
    for line in re.split(r"\r\n|\n|\r", text):
        prop = _content_line(line)
        if prop is None:
            continue
        name, params, value = prop
        if name == "BEGIN":
            stack.append(value.strip().upper())
            if stack[-1] == "VEVENT":
                props = {}
        elif name == "END":
            component = value.strip().upper()
            if component not in stack:
                continue
            while stack.pop() != component:
                pass
            if component == "VEVENT":
                try:
                    events.append(_event(props))
                except _Malformed as e:
                    summary = _unescape(props.get("SUMMARY", ({}, ""))[1])
                    print(f"[roster] skipping an event that can't be read, {summary!r}: {e}")
        elif stack and stack[-1] == "VEVENT":
            props.setdefault(name, (params, value, line))
    return events


# ---------- duties ----------


class Duty(NamedTuple):
    start: datetime         # DTSTART, UTC: the duty's envelope ...
    end: datetime           # ... to DTEND, UTC
    summary: str            # the roster's title for it, trimmed
    description: str        # its description, unescaped, verbatim
    type: str               # flight, reserve, sim or other
    code: str               # e.g. CX105, R1E, A1534
    flight_no: str = None   # a flight's, when its roster gives them
    dep: str = None         # its origin - the first leg's departure ...
    arr: str = None         # ... and final destination, the last leg's arrival
    position: str = None


def duties(events):
    """The work duties among parse_events()' events, in the file's order:
    every timed event with a start and an end. An all-day event - a day
    off, leave - isn't one; a timed one with no end, or ending before it
    starts, is skipped and logged. What a duty is labelled never decides
    whether it's kept."""
    found = []
    for e in events:
        if e["all_day"]:
            continue
        if e["start"] is None or e["end"] is None:
            print(f"[roster] skipping {e['summary']!r}: a timed event needs a DTSTART and a DTEND")
            continue
        if e["end"] < e["start"]:
            print(f"[roster] skipping {e['summary']!r}: it ends before it starts")
            continue
        summary = e["summary"].strip()
        found.append(Duty(e["start"], e["end"], summary, e["description"],
                          **classify(summary, description=e["description"])))
    return found


_DAY = re.compile(r"\d{1,2}-[A-Za-z]{3}\s+")     # a title's leading day, "05-Nov "
_ROUTE = re.compile(r"([A-Z]{3})-([A-Z]{3})")    # HKG-MEL
_POSITION = re.compile(r"[A-Z]{2,3}")            # RP, RQ, ...


def _duty_type(code, config):
    """The type a duty code falls under, by config's duty_types rules
    (first match wins), else the default. A rule matches by `prefixes`
    (the code starts with one) or by a `regex`."""
    for rule in config.get("duty_types", []):
        if "regex" in rule and re.match(rule["regex"], code):
            return rule.get("type", "other")
        if any(code.startswith(p) for p in rule.get("prefixes", [])):
            return rule.get("type", "other")
    return config.get("default_type", "other")


def classify(summary, config=None, description=""):
    """What a duty is, from its title ("05-Nov CX105 HKG-MEL RP"), its
    description and the roster config: {type, code}, and for a flight its
    flight_no, dep, arr and position where they're given. The type is the
    config's (airline-specific); the code, flight_no and position are the
    title's shape. dep and arr are the duty's origin and final destination
    - the first leg's departure and the last leg's arrival - read from its
    chain of legs: the description's, which lists every sector, else the
    title's. So a day turn out and back in one duty ("CX 903 HKG-MNL / CX
    902 MNL-HKG") is base to base. Best effort, never a refusal: a code no
    rule matches is the default type."""
    config = CONFIG if config is None else config
    title = summary.strip()
    day = _DAY.match(title)
    if day:
        title = title[day.end():]
    tokens = title.split()
    code = tokens[0] if tokens else ""
    found = {"type": _duty_type(code, config), "code": code}
    if found["type"] == "flight" and tokens:
        # "CX 105 HKG-MEL RP" or "QF123 SYD-HKG": the number may be its own
        # token or joined to the carrier; the rest is route and position.
        num = tokens[1] if len(tokens) > 1 and tokens[1].isdigit() else ""
        found["flight_no"] = found["code"] = tokens[0] + num
        rest = tokens[(2 if num else 1):]
        legs = _ROUTE.findall(description) or _ROUTE.findall(title)
        if legs:
            found["dep"], found["arr"] = legs[0][0], legs[-1][1]
        if rest and _POSITION.fullmatch(rest[-1]):
            found["position"] = rest[-1]
    return found


def session_summary(duty, capture="live"):
    """What a duty's session is filed with: its capture ("live", filed from
    the roster as it stood; "backfill", a past duty filed into the plan
    after the fact - filed_as()), type, code, label (the whole title) and
    raw description, and a flight's flight_no, dep, arr and position
    where it has them."""
    summary = {
        "capture": capture, "type": duty.type, "code": duty.code,
        "label": duty.summary, "raw": duty.description,
    }
    for key in ("flight_no", "dep", "arr", "position"):
        if getattr(duty, key) is not None:
            summary[key] = getattr(duty, key)
    return summary


def bucket_for(end, now):
    """The kind a duty ending at `end` is filed as at `now`: complete once
    it's over, else rostered - still to come, or under way."""
    return COMPLETE if end <= now else ROSTERED


def filed_as(end, now, backfill=False):
    """Where a duty ending at `end` is filed at `now`, and how it was
    captured: [(kind, capture)]. Its bucket_for() kind, captured "live" -
    and, backfilling, a duty already over is filed into the rostered plan
    as well, captured "backfill": the plan of a roster uploaded after it
    started, put back. A duty still to come is filed once either way."""
    kind = bucket_for(end, now)
    filed = [(kind, "live")]
    if backfill and kind == COMPLETE:
        filed.append((ROSTERED, "backfill"))
    return filed


# ---------- trips ----------


def trips(sessions, home_base, open_end=None):
    """The trips away from `home_base` among a lane's duty sessions, in order.

    `sessions` are one kind's duties as ctx.sessions() gives them
    (Session tuples: .start and .end UTC text, .summary the duty's
    dict); `home_base` an IATA code, the config's; `open_end` UTC text
    - where a trip still away when the data runs out ends - or None.

    Only flights make a trip: a session whose summary is a flight's,
    with its route (dep and arr) and an end, taken by start. A reserve,
    a sim, a ground duty, or a flight whose title gave no route, never
    opens or closes one - it still draws, as a block. Walking them:

        a flight out of base (dep home_base) opens a trip - from its
        start, to the port it flew to (out_port), its id that start;
        a flight into base (arr home_base) closes the open trip, at its
        end - and with none open (away before the data starts), is
        nothing; any other flight is a sector away, inside the trip.

    So a pattern of sectors - out to one port, on to another, home from
    a third - is one trip, the whole time away. A flight out of base
    while a trip is open means that trip's way home isn't in the data:
    it has no end, so it's dropped, and the new one opens. A flight from
    base to base leaves nothing open.

    A trip still open at the end closes at `open_end` - never before
    its flight out has landed - or, with no `open_end`, is dropped: a
    band needs an end to draw.

    Each a dict {id, start, end, out_port}. `id` - the flight out's
    start - is the trip's stable id: the same trip, uploaded again,
    has the same one.
    """
    flights = sorted(
        (s for s in sessions
         if s.end is not None and s.summary.get("type") == "flight"
         and s.summary.get("dep") and s.summary.get("arr")),
        key=lambda s: s.start,
    )
    found, trip, out_end = [], None, None
    for f in flights:
        dep, arr = f.summary["dep"], f.summary["arr"]
        if dep == home_base:
            # At base: whatever was open lost its way home. Out again,
            # unless it's straight back.
            trip = None if arr == home_base else {"id": f.start, "start": f.start, "end": None, "out_port": arr}
            out_end = f.end
        elif arr == home_base and trip is not None:
            trip["end"] = f.end
            found.append(trip)
            trip = None
    if trip is not None and open_end is not None:
        trip["end"] = max(open_end, out_end)
        found.append(trip)
    return found


# ---------- trip cards: the Plan and Report rails ----------

# A leg as a roster lists it, in a description or a title: its flight
# number where given - a carrier and a number, apart or joined ("CX 902",
# "CX9102") - then its route.
_LEG = re.compile(r"(?:\b([A-Z][A-Z0-9]|\d[A-Z])\s?(\d{1,4})\s+)?([A-Z]{3})-([A-Z]{3})")


def _legs(summary):
    """A duty's legs read back from its stored summary, first to last:
    [(flight_no, dep, arr)], flight_no None where the roster gave none.
    From its description (raw), which lists every sector, else its title
    (label) - where classify() read its route from."""
    for text in (summary.get("raw"), summary.get("label")):
        legs = [(carrier + number if carrier else None, dep, arr)
                for carrier, number, dep, arr in _LEG.findall(text if isinstance(text, str) else "")]
        if legs:
            return legs
    return []


def _code(session):
    return session.summary.get("code") or None


def trip_cards(rostered, flown, home_base):
    """Every trip out of `home_base` and back among the duties, in order:
    a card each, for the Plan and Report rails.

    `rostered` and `flown` are the two lanes' duty sessions, as
    ctx.sessions() gives them; `home_base` an IATA code, the config's.
    A card is a whole trip - out, away, back - so it's read from both
    lanes at once: a trip under way, flown out and still rostered home,
    is split across them. A duty in both - a backfilled plan, or one
    uploaded before and after it flew: the same start, end, code, dep
    and arr - is taken once, its flown copy.

    Only flights make a trip, as for trips(): a session whose summary is
    a flight's, with its route (dep and arr) and an end, taken by start.
    Walking them:

        a flight out of base to a port opens a trip, to that port - its
        id, as a band's, the flight out's start;
        a flight into base closes the open trip - with none open (out
        before the data starts), it's nothing; any other flight is a
        sector away, inside the trip;
        a duty out of base and back in one - a day return - is a trip
        of its own, its port the one it turned round at.

    A flight out of base while a trip is open means that trip's way home
    isn't in the data, so it's dropped - as trips() drops it. The latest
    trip may still be open at the end: it's kept, its way home not
    rostered yet.

    Each a dict {id, start, end, out, in, port, day}: `start` the flight
    out's, `end` the flight home's - None for a trip still open, not home
    as far as the data knows; `out` the flight out's code, its flight
    number, and `in` the number of the flight that lands at base - the
    last leg of the duty home, read back from its legs (one duty home
    can fly two sectors, CEB-MNL / MNL-HKG, stored under the first's
    code), else that duty's code - None while open; `port` the first
    port away - the layover's; `day` True for a day return, its start,
    end and `out` its one duty's.

    A day return's duty is stored base to base with one code, its first
    sector's, so its flight home and its turn-round port are read back
    from its legs - its description, else its title: `port` the first
    leg's arrival, `in` the last leg's flight number (None if the roster
    gave none). One that never left base - a turnback, a local training
    flight, a route of base to base alone - is no trip.
    """
    duties = {}
    for s in (*rostered, *flown):
        if s.end is None or s.summary.get("type") != "flight":
            continue
        dep, arr = s.summary.get("dep"), s.summary.get("arr")
        if dep and arr:
            # Flown last: a duty in both lanes is its flown copy.
            duties[(s.start, s.end, s.summary.get("code"), dep, arr)] = s
    flights = sorted(duties.values(), key=lambda s: s.start)

    cards, trip = [], None
    for f in flights:
        dep, arr = f.summary["dep"], f.summary["arr"]
        if dep == home_base:
            # At base: whatever was open lost its way home.
            trip = None
            if arr != home_base:
                trip = {"id": f.start, "start": f.start, "end": None,
                        "out": _code(f), "in": None, "port": arr, "day": False}
                continue
            legs = _legs(f.summary)
            if legs and legs[0][2] != home_base:
                cards.append({"id": f.start, "start": f.start, "end": f.end,
                              "out": _code(f), "in": legs[-1][0], "port": legs[0][2], "day": True})
        elif arr == home_base and trip is not None:
            # In: the flight that lands at base - its duty's last leg's,
            # where the roster numbers it - not the duty's first sector's.
            legs = _legs(f.summary)
            trip["end"], trip["in"] = f.end, (legs[-1][0] if legs else None) or _code(f)
            cards.append(trip)
            trip = None
    if trip is not None:
        cards.append(trip)
    return cards


# ---------- circadian: the body clock against the sun ----------
#
# The circadian panel's engine - drawn by _circadian(). Three strips over a
# window of time: where the crew member is, day or night there; the body
# clock, day or night at home base, always - no acclimatisation, by
# design; and the duties, each cut into pieces coloured against the body
# clock alone, never where it's flown. Beside them the body clock's WOCL
# and the curve's troughs. Instants are compared as aware UTC datetimes
# and given back as UTC text, as a session's are; every span is
# half-open, [start, end).

_J2000 = datetime(2000, 1, 1, 12, tzinfo=timezone.utc)   # J2000.0, JD 2451545.0
_SUN_ALTITUDE = -0.833      # degrees: the sun's upper limb on the horizon, refraction in
_OBLIQUITY = 23.4397        # degrees: the Earth's axial tilt
WOCL = ((2, 0), (6, 0))     # the window of circadian low, home local: [02:00, 06:00)
CBT_MIN = "04:00"           # the body clock's low, home local, unless the config gives one

SLEEP = "sleep"
REST_WINDOW = ("22:00", "08:00")   # the body's rest window, home local, unless personal gives rest_start, rest_end
SLOT_MINUTES = 90                  # a disturbed-cycle check's slot, unless the config's sleep block gives slot_minutes
MIN_SLEEP_MINUTES = 15             # ... a slot with less sleep than this is disturbed, unless it gives min_sleep_minutes
# A stage drawn, and at what level: a stage-less sleep (asleep, restless:
# a short nap a device doesn't stage) at light's. Every stage drawn is
# sleep for a slot; awake, out_of_bed and unknown aren't, and aren't drawn.
DRAWN_STAGES = {"light": "light", "deep": "deep", "rem": "rem", "asleep": "light", "restless": "light"}


def sun_times(day, lat, lon):
    """Sunrise and sunset around local solar noon of `day` (a date) at
    `lat`, `lon` - decimal degrees, north and east positive - from NOAA's
    solar equations (the sunrise equation): (rise, set), two aware UTC
    datetimes to the second; or "day" if the sun doesn't set that day,
    "night" if it doesn't rise.

    The day count is from J2000.0 - 2000-01-01 12:00 UTC, JD 2451545.0 -
    less the longitude's share of a day, so it lands on mean solar noon
    at `lon`; that's the day count's own noon, with nothing added."""
    rad = math.radians
    n = (day - _J2000.date()).days - lon / 360                    # mean solar noon, days from J2000.0
    m = rad((357.5291 + 0.98560028 * n) % 360)                    # mean anomaly
    centre = 1.9148 * math.sin(m) + 0.0200 * math.sin(2 * m) + 0.0003 * math.sin(3 * m)
    ecliptic = rad((math.degrees(m) + centre + 180 + 102.9372) % 360)   # ecliptic longitude
    transit = n + 0.0053 * math.sin(m) - 0.0069 * math.sin(2 * ecliptic)   # solar noon
    declination = math.asin(math.sin(ecliptic) * math.sin(rad(_OBLIQUITY)))
    cos_hour = ((math.sin(rad(_SUN_ALTITUDE)) - math.sin(rad(lat)) * math.sin(declination))
                / (math.cos(rad(lat)) * math.cos(declination)))
    if cos_hour < -1:
        return "day"
    if cos_hour > 1:
        return "night"
    half = math.degrees(math.acos(cos_hour)) / 360                # half the daylight, in days
    return tuple(_J2000 + timedelta(seconds=round(days * 86400)) for days in (transit - half, transit + half))


def daylight(lat, lon, start, end):
    """Day and night at `lat`, `lon` over [start, end) (aware datetimes):
    contiguous spans {start, end, tone}, tone "day" or "night", in order,
    the window's edges theirs. Built from sun_times() for every date from
    the day before start's to the day after end's; a date the sun
    doesn't set (or rise) on is a whole UTC day of day (or night)."""
    return [_text(s, e, tone=tone) for s, e, tone in _daylight(lat, lon, _utc(start, "start"), _utc(end, "end"))]


def body_clock(start, end, config=None):
    """The body clock - home base's time zone, personal.timezone, always -
    over [start, end) (aware datetimes): {tz, wocl, troughs}.

    `tz` the ZoneInfo; `wocl` the window of circadian low, 02:00 to 06:00
    home local of every home-local date the window touches, as UTC spans
    {start, end}, clipped to it - through zoneinfo, so right across a
    clock change; `troughs` the curve's low points, personal.cbt_min (HH:MM,
    home local, CBT_MIN when absent) on each of those dates and one either
    side, so one falls before the window and one after: UTC text, in order.
    ValueError for a timezone missing or unknown, or a cbt_min that isn't
    HH:MM."""
    config = _config(config)
    start, end = _utc(start, "start"), _utc(end, "end")
    tz = _home_zone(config)
    cbt = _cbt_min(config)
    return {
        "tz": tz,
        "wocl": [_text(s, e) for s, e in _wocl(start, end, tz)],
        "troughs": [_utc_text(_home_local(d, cbt, tz)) for d in _home_days(start, end, tz, margin=1)],
    }


def curve_value(t, troughs):
    """The body clock's curve at `t` (an aware datetime), between the
    troughs (body_clock()'s - UTC text, or datetimes) either side of it:
    -cos(2*pi*f), f the fraction of the way from the one before to the one
    after. -1 at a trough, +1 midway, 0 a quarter of the way. None outside
    the troughs' range."""
    t = _utc(t, "t")
    lows = sorted(_utc(low, "a trough") for low in troughs)
    if not lows or not lows[0] <= t <= lows[-1]:
        return None
    i = bisect.bisect_right(lows, t)
    if i == len(lows):                    # the last trough itself
        return -1.0
    before, after = lows[i - 1], lows[i]
    return -math.cos(2 * math.pi * ((t - before) / (after - before)))


def locations(rostered, flown, now, start, end, config=None):
    """Where the crew member is over [start, end): contiguous spans
    {start, end, port}, port an IATA code or None - not known.

    `rostered` and `flown` the two lanes' duty sessions, as ctx.sessions()
    gives them; `now` an aware datetime. The duties that count are the
    flown ones over by now and the rostered ones ending after it - those
    to come and the one under way - never a past rostered one: a past
    stretch with nothing flown is None, not the plan. A session with no
    end counts for neither.

    Walking them by start, from the moment a duty starts you're at its
    final destination: a flight's arr (a day return's is base); a flight
    with no arr leaves you where you were; any other duty - a sim, a
    reserve - is at home base. Before the first, None. A duty before the
    window still says where it starts. The same port twice in a row is
    one span."""
    config = _config(config)
    start, end, now = _utc(start, "start"), _utc(end, "end"), _utc(now, "now")
    return [_text(s, e, port=port) for s, e, port in _locations(rostered, flown, now, start, end, config)]


def location_strip(locs, start, end, config=None):
    """locations()' spans cut into day and night where they are: {start,
    end, port, tone}, tone "day" or "night" at that port - daylight() at
    its destinations lat and lon - or "none" for a port not known (None)
    or one with no lat and lon in the config's destinations, the port
    kept. In order, clipped to [start, end)."""
    config = _config(config)
    start, end = _utc(start, "start"), _utc(end, "end")
    places = _destinations(config)
    pieces = []
    for loc in sorted(locs, key=lambda loc: _utc(loc["start"], "a location's start")):
        s = max(_utc(loc["start"], "a location's start"), start)
        e = min(_utc(loc["end"], "a location's end"), end)
        if e <= s:
            continue
        port = loc.get("port")
        position = _position(places.get(port)) if port else None
        for a, b, tone in _daylight(*position, s, e) if position else [(s, e, "none")]:
            last = pieces[-1] if pieces else None
            if last and (last["port"], last["tone"], last["end"]) == (port, tone, a):
                last["end"] = b
            else:
                pieces.append({"start": a, "end": b, "port": port, "tone": tone})
    return [_text(p["start"], p["end"], port=p["port"], tone=p["tone"]) for p in pieces]


def body_strip(start, end, config=None):
    """Day and night at home base over [start, end) - the body clock's
    sky: daylight() at personal.home_base's lat and lon in the config's
    destinations. ValueError naming the home base if it isn't there, or
    has no lat and lon."""
    config = _config(config)
    start, end = _utc(start, "start"), _utc(end, "end")
    return [_text(s, e, tone=tone) for s, e, tone in _home_sky(start, end, config)]


def duty_pieces(rostered, flown, now, start, end, config=None):
    """The duties that count (as locations() counts them), each clipped
    to [start, end) and cut against the body clock - never where it's
    flown: {start, end, code, tone}, tone "wocl" inside home's WOCL
    (02:00-06:00 home local - first), else "night" in home base's
    darkness, else "day". A duty's pieces are contiguous, the same tone
    side by side merged; all in order by start. `code` the summary's."""
    config = _config(config)
    start, end, now = _utc(start, "start"), _utc(end, "end"), _utc(now, "now")
    return [_text(s, e, code=code, tone=tone) for s, e, code, tone in _duty_pieces(rostered, flown, now, start, end, config)]


def circadian(rostered, flown, now, start, end, config=None):
    """Everything the circadian panel draws over [start, end), at once:
    {home, location, body, duties, wocl, troughs} - home base, then
    location_strip() of locations(), body_strip(), duty_pieces(), and
    body_clock()'s wocl and troughs."""
    config = _config(config)
    clock = body_clock(start, end, config)
    return {
        "home": _personal(config).get("home_base"),
        "location": location_strip(locations(rostered, flown, now, start, end, config), start, end, config),
        "body": body_strip(start, end, config),
        "duties": duty_pieces(rostered, flown, now, start, end, config),
        "wocl": clock["wocl"],
        "troughs": clock["troughs"],
    }


def rest_windows(start, end, config=None):
    """The body's rest window each day, clipped to [start, end): [{start,
    end}] in order - rest_start to rest_end home local (personal's, HH:MM;
    22:00 to 08:00 by default), over midnight when it ends no later than
    it starts. ValueError for a time that isn't HH:MM, or a window that
    starts when it ends."""
    config = _config(config)
    start, end = _utc(start, "start"), _utc(end, "end")
    return [_text(s, e) for s, e in _rest_windows(start, end, config)]


def rest_strip(start, end, config=None):
    """[start, end) as the body clock's rest and wake: contiguous {start,
    end, tone}, in order - tone "rest" in a rest window, else "day"."""
    config = _config(config)
    start, end = _utc(start, "start"), _utc(end, "end")
    layers = [(s, e, "rest") for s, e in _rest_windows(start, end, config)]
    return [_text(s, e, tone=tone) for s, e, tone in _tones(start, end, "day", layers)]


def sleep_nights(sessions):
    """The sleep sessions with a stage timeline, one a night: [{start, end,
    source, stages}] by start, stages each {start, end, stage} by start,
    as the session's summary carries them - one without a stage, or a
    start and end in order, left out. Taken in the order they're served
    (the host's source priority): a session overlapping one already taken
    is left out, so a night two devices tracked is drawn once. A session
    with no end, or no timeline (a source that gives only totals), is no
    night."""
    taken = []
    for session in sessions:
        summary = session.summary if isinstance(session.summary, dict) else {}
        raw = summary.get("stages")
        if session.end is None or not isinstance(raw, list):
            continue
        stages = []
        for seg in raw:
            if not isinstance(seg, dict) or not isinstance(seg.get("stage"), str):
                continue
            try:
                a, b = _utc(seg.get("start"), "a stage's start"), _utc(seg.get("end"), "a stage's end")
            except ValueError:
                continue
            if a < b:
                stages.append((a, b, seg["stage"]))
        if not stages:
            continue
        a, b = _utc(session.start, "a sleep's start"), _utc(session.end, "a sleep's end")
        if any(a < tb and ta < b for ta, tb, _, _ in taken):
            continue
        taken.append((a, b, session.source, sorted(stages, key=lambda x: x[0])))
    return [{"start": _utc_text(a), "end": _utc_text(b), "source": source,
             "stages": [_text(x, y, stage=stage) for x, y, stage in segs]}
            for a, b, source, segs in sorted(taken, key=lambda night: night[0])]


def stage_pieces(nights, windows):
    """The nights' stages to draw: [{start, end, stage, tone}] by start -
    light, deep and rem as they are, a stage-less asleep or restless at
    light's level (DRAWN_STAGES), awake and anything else not drawn - each
    cut at the rest window's edges, tone "in" inside one of `windows`
    ({start, end}, as rest_windows() gives them), "out" outside."""
    wins = [(_utc(w["start"], "a rest window's start"), _utc(w["end"], "a rest window's end")) for w in windows]
    pieces = []
    for night in nights:
        for seg in night["stages"]:
            stage = DRAWN_STAGES.get(seg["stage"])
            if stage is None:
                continue
            a, b = _utc(seg["start"], "a stage's start"), _utc(seg["end"], "a stage's end")
            under = [(s, e, "in") for s, e in wins if s < b and a < e]
            pieces += [_text(x, y, stage=stage, tone=tone) for x, y, tone in _tones(a, b, "out", under)]
    return sorted(pieces, key=lambda piece: piece["start"])


def disturbed_slots(duties, nights, windows, now, config=None):
    """Each stretch a duty overlapped the rest window, in slots: [{start,
    end, asleep, disturbed}] by start. A duty's overlap with each of
    `windows` is cut into slots of slot_minutes (the config's sleep
    block's; 90 by default) from where the overlap starts, the last a
    part slot counted only if it's at least half a slot. `asleep` is the
    whole minutes of sleep in the slot - every stage DRAWN_STAGES draws -
    and `disturbed` is True when that's under min_sleep_minutes (15 by
    default). Only slots over by `now`: a slot still to come has no sleep
    to count yet. `duties` are sessions (.start, .end) - the flown ones,
    as the circadian panel takes the past; one with no end is none."""
    config = _config(config)
    now = _utc(now, "now")
    slot, least = _slot_rules(config)
    wins = [(_utc(w["start"], "a rest window's start"), _utc(w["end"], "a rest window's end")) for w in windows]
    asleep = [(_utc(seg["start"], "a stage's start"), _utc(seg["end"], "a stage's end"))
              for night in nights for seg in night["stages"] if seg["stage"] in DRAWN_STAGES]
    found = []
    for duty in duties:
        if duty.end is None:
            continue
        ds, de = _utc(duty.start, "a duty's start"), _utc(duty.end, "a duty's end")
        for ws, we in wins:
            a, b = max(ds, ws), min(de, we)
            while a < b:
                e = min(a + slot, b)
                if e - a >= slot / 2 and e <= now:
                    seconds = sum(max(0.0, (min(e, y) - max(a, x)).total_seconds()) for x, y in asleep)
                    minutes = int(seconds // 60)
                    found.append({**_text(a, e), "asleep": minutes, "disturbed": seconds < least.total_seconds()})
                a = e
    return sorted(found, key=lambda x: x["start"])


def _rest_windows(start, end, config):
    tz = _home_zone(config)
    begin, finish = _rest_times(config)
    spans = []
    for day in _home_days(start, end, tz, margin=1):
        s = _home_local(day, begin, tz)
        e = _home_local(day + timedelta(days=1) if finish <= begin else day, finish, tz)
        s, e = max(s, start), min(e, end)
        if s < e:
            spans.append((s, e))
    return spans


def _rest_times(config):
    personal, times = _personal(config), []
    for key, default in (("rest_start", REST_WINDOW[0]), ("rest_end", REST_WINDOW[1])):
        raw = personal.get(key)
        raw = default if raw is None else raw
        found = re.fullmatch(r"(\d{1,2}):(\d{2})", raw) if isinstance(raw, str) else None
        if not found or int(found[1]) > 23 or int(found[2]) > 59:
            raise ValueError(f"config.json's personal {key} must be HH:MM, home local: {raw!r}")
        times.append((int(found[1]), int(found[2])))
    if times[0] == times[1]:
        raise ValueError("config.json's personal rest_start and rest_end can't be the same time")
    return tuple(times)


def _slot_rules(config):
    block = config.get("sleep")
    block = block if isinstance(block, dict) else {}
    slot, least = block.get("slot_minutes", SLOT_MINUTES), block.get("min_sleep_minutes", MIN_SLEEP_MINUTES)
    for key, value, top in (("slot_minutes", slot, 24 * 60), ("min_sleep_minutes", least, slot)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= top:
            raise ValueError(f"config.json's sleep {key} must be a whole number of minutes, 1 to {top}: {value!r}")
    return timedelta(minutes=slot), timedelta(minutes=least)


def _config(config):
    config = CONFIG if config is None else config
    if not isinstance(config, dict):
        raise ValueError(CONFIG_ERROR or "no config")
    return config


def _personal(config):
    personal = config.get("personal")
    return personal if isinstance(personal, dict) else {}


def _destinations(config):
    places = config.get("destinations")
    return places if isinstance(places, dict) else {}


def _position(place):
    """A destination's (lat, lon), or None without both, as numbers on the globe."""
    if not isinstance(place, dict):
        return None
    lat, lon = place.get("lat"), place.get("lon")
    for value, limit in ((lat, 90), (lon, 180)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not -limit <= value <= limit:
            return None
    return float(lat), float(lon)


def _utc(moment, what):
    """An instant as an aware UTC datetime: an aware datetime, or UTC text
    as a session's start and end are stored."""
    if isinstance(moment, str):
        try:
            moment = datetime.fromisoformat(moment)
        except ValueError:
            raise ValueError(f"{what} isn't a time: {moment!r}") from None
    if not isinstance(moment, datetime) or moment.utcoffset() is None:
        raise ValueError(f"{what} must be an aware datetime: {moment!r}")
    return moment.astimezone(timezone.utc)


def _text(start, end, **rest):
    return {"start": _utc_text(start), "end": _utc_text(end), **rest}


def _tones(start, end, base, layers):
    """[start, end) as contiguous (start, end, tone) spans, in order:
    each the tone of the last of `layers` - (start, end, tone) spans,
    each laid over those before - that covers it, else `base`; the same
    tone side by side merged."""
    if end <= start:
        return []
    cuts = sorted({start, end, *(t for s, e, _ in layers for t in (s, e) if start < t < end)})
    spans = []
    for a, b in zip(cuts, cuts[1:]):
        tone = next((tone for s, e, tone in reversed(layers) if s <= a and b <= e), base)
        if spans and spans[-1][2] == tone:
            spans[-1] = (spans[-1][0], b, tone)
        else:
            spans.append((a, b, tone))
    return spans


def _daylight(lat, lon, start, end):
    days, polar = [], []
    day, last = start.date() - timedelta(days=1), end.date() + timedelta(days=1)
    while day <= last:
        sun = sun_times(day, lat, lon)
        if isinstance(sun, str):
            midnight = datetime.combine(day, time(), tzinfo=timezone.utc)
            polar.append((midnight, midnight + timedelta(days=1), sun))
        else:
            days.append((*sun, "day"))
        day += timedelta(days=1)
    # A polar date's whole UTC day is its own, over a neighbour's daylight.
    return _tones(start, end, "night", days + polar)


def _home_zone(config):
    name = _personal(config).get("timezone")
    if not isinstance(name, str) or not name:
        raise ValueError("config.json's personal block needs a timezone, an IANA name such as Asia/Hong_Kong")
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(f"config.json's personal timezone isn't a time zone: {name!r}") from None


def _cbt_min(config):
    raw = _personal(config).get("cbt_min")
    raw = CBT_MIN if raw is None else raw
    found = re.fullmatch(r"(\d{1,2}):(\d{2})", raw) if isinstance(raw, str) else None
    if not found or int(found[1]) > 23 or int(found[2]) > 59:
        raise ValueError(f"config.json's personal cbt_min must be HH:MM, home local: {raw!r}")
    return int(found[1]), int(found[2])


def _home_days(start, end, tz, margin=0):
    """The home-local dates [start, end) touches, `margin` more either side."""
    if end <= start:
        return []
    first = start.astimezone(tz).date() - timedelta(days=margin)
    last = (end - timedelta(microseconds=1)).astimezone(tz).date() + timedelta(days=margin)
    return [first + timedelta(days=i) for i in range((last - first).days + 1)]


def _home_local(day, hour_minute, tz):
    """`day` at that home-local time, as a UTC instant."""
    return datetime.combine(day, time(*hour_minute), tzinfo=tz).astimezone(timezone.utc)


def _wocl(start, end, tz):
    spans = []
    for day in _home_days(start, end, tz):
        s, e = max(_home_local(day, WOCL[0], tz), start), min(_home_local(day, WOCL[1], tz), end)
        if s < e:
            spans.append((s, e))
    return spans


def _home_sky(start, end, config):
    home = _personal(config).get("home_base")
    if not home:
        raise ValueError("config.json's personal block needs a home_base")
    position = _position(_destinations(config).get(home))
    if position is None:
        raise ValueError(f"home base {home} needs a lat and lon in config.json's destinations")
    return _daylight(*position, start, end)


def _counted(rostered, flown, now):
    """The duties that count, by start: flown ones over by now, rostered
    ones ending after it; none with no end."""
    found = [s for s in flown if s.end is not None and _utc(s.end, "a duty's end") <= now]
    found += [s for s in rostered if s.end is not None and _utc(s.end, "a duty's end") > now]
    return sorted(found, key=lambda s: (_utc(s.start, "a duty's start"), _utc(s.end, "a duty's end")))


def _locations(rostered, flown, now, start, end, config):
    if end <= start:
        return []
    home = _personal(config).get("home_base") or None
    spans, since, here = [], start, None
    for s in _counted(rostered, flown, now):
        at = _utc(s.start, "a duty's start")
        if at >= end:
            break
        if s.summary.get("type") != "flight":
            there = home
        else:
            there = s.summary.get("arr") or here
        if at > start and there != here:
            spans.append((since, at, here))
            since = at
        here = there
    spans.append((since, end, here))
    return spans


def _duty_pieces(rostered, flown, now, start, end, config):
    tz = _home_zone(config)
    # Home's sky, the WOCL laid over it: the body clock's tones.
    layers = [*_home_sky(start, end, config), *((s, e, "wocl") for s, e in _wocl(start, end, tz))]
    pieces = []
    for duty in _counted(rostered, flown, now):
        s = max(_utc(duty.start, "a duty's start"), start)
        e = min(_utc(duty.end, "a duty's end"), end)
        under = [layer for layer in layers if layer[0] < e and s < layer[1]]
        code = duty.summary.get("code", "")
        pieces += [(a, b, code, tone) for a, b, tone in _tones(s, e, "day", under)]
    return sorted(pieces, key=lambda piece: piece[0])
