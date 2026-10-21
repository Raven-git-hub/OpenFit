"""FRMS — one app, one tile, one config.

The FRMS suite is this one app: one tile, 6 wide by 4 tall (app.yaml's
only size), one config.json. render() draws the tile as four sub-panels
on a 6x4 grid (ctx.grid()), each drawn as a tile of its own: circadian
top-left (3x2), the roster bottom-left (2x2) - the timeline and its Plan
and Report rails - and two more to come, top-right (3x2) and
bottom-right (4x2). All but the roster are placeholders until they're
built.

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
"""

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import NamedTuple

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
        {"x": 0, "y": 0, "w": 3, "h": 2, "spec": ctx.empty("Circadian — coming soon")},
        {"x": 3, "y": 0, "w": 3, "h": 2, "spec": ctx.empty("Coming soon")},
        {"x": 0, "y": 2, "w": 2, "h": 2, "spec": _roster(ctx)},
        {"x": 2, "y": 2, "w": 4, "h": 2, "spec": ctx.empty("Coming soon")},
    ])


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
    """A trip as a timeline band: its span, labelled with the port it
    flew out to. Its id stays on the trip - not the timeline's."""
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
