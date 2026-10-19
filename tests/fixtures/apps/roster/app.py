"""Roster — a duty roster's .ics, filed as duty sessions.

The file picker is OpenFit's; the format is this app's. Save hands
handle() the roster picked - an iCalendar (.ics) export, a Cathay-style
one in mind - as a FileUpload, and the host never reads it: this app
unfolds and parses it itself, with the standard library alone.

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
uploaded, else duties_rostered. Beside them, a figure a day for each: the
hours of the duties that started that day (UTC), summed -
duty_hours_complete and duty_hours_rostered, metrics this app invents
(app.yaml).

The tile reads the duties back (ctx.sessions()) - about six months of
them, and all still to come - and draws them on a timeline under the
picker: rostered on the left, flown on the right, each duty a block at
its time and labelled with its code, a line at now and the flown side
greyed after it. Behind the blocks, each lane shades its trips away
from home base (trips()): a flight out of base to the next flight back,
any sectors between inside it - the layovers.

Uploading again replaces a duty of the same kind and start, and a day's
figure; it removes nothing, so a duty taken off the roster keeps its
row until another lands on its start.

All or nothing: a refusal - no duties, a time not in UTC, a day over 24
hours - writes nothing; the host rolls back whatever was already filed.

The parsing, labelling, routing and trip-finding are module functions -
parse_events(), duties(), classify(), bucket_for(), daily_hours(),
trips() - pure, for a test to call on their own.
"""

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from typing import NamedTuple

ROSTERED = "duties_rostered"
COMPLETE = "duties_complete"

# Each kind's figure a day: the app's own metrics (app.yaml).
HOURS = {ROSTERED: "duty_hours_rostered", COMPLETE: "duty_hours_complete"}

NON_UTC = "roster has a non-UTC time, which isn't supported yet: {}"


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
    picker = ctx.input("file", "upload", label="Upload roster", kind="file", accept=".ics")
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

    # The plan on the left, what was flown on the right - greyed after
    # now, when nothing's flown yet. The picker on top; the timeline
    # takes the rest of the tile and scrolls, laid out to whatever size
    # the tile is (render() isn't told it). It's in the FRMS focus
    # group: the page keeps a cursor on it, and scrolls every timeline
    # of the group to one time.
    return ctx.panel(picker, ctx.timeline(
        [
            _lane(rostered, label="Rostered", bands=[_band(t) for t in rostered_trips]),
            _lane(flown, label="Flown", grey_after_now=True, bands=[_band(t) for t in flown_trips]),
        ],
        now=now,
        sync="frms",
    ))


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
    # Once, so every duty in the file is routed against the same instant.
    now = datetime.now(timezone.utc)
    for duty in found:
        ctx.write_session(bucket_for(duty.end, now), duty.start, duty.end, session_summary(duty))
    for (metric, day), hours in sorted(daily_hours(found, now).items()):
        try:
            # The host holds each to the declared range, 0-24.
            ctx.write_metric(metric, hours, date=day)
        except ValueError as e:
            raise ValueError(f"{day}: {e}") from None


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
    flight_no: str = None   # a flight's, when its title gives them
    dep: str = None
    arr: str = None
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
        found.append(Duty(e["start"], e["end"], summary, e["description"], **classify(summary)))
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


def classify(summary, config=None):
    """What a duty is, from its title ("05-Nov CX105 HKG-MEL RP") and the
    roster config: {type, code}, and for a flight its flight_no, dep, arr
    and position where the title gives them. The type is the config's
    (airline-specific); the rest is the title's shape. Best effort, never
    a refusal: a code no rule matches is the default type."""
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
        route = next((t for t in rest if _ROUTE.fullmatch(t)), None)
        if route:
            found["dep"], found["arr"] = route.split("-")
        if rest and _POSITION.fullmatch(rest[-1]):
            found["position"] = rest[-1]
    return found


def session_summary(duty):
    """What a duty's session is filed with: its capture ("live", filed from
    the roster as it stood), type, code, label (the whole title) and raw
    description, and a flight's flight_no, dep, arr and position where it
    has them."""
    summary = {
        "capture": "live", "type": duty.type, "code": duty.code,
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


def daily_hours(duties, now):
    """{(metric, YYYY-MM-DD): hours} - each kind's duty hours a day: the
    duties that started that day (UTC) summed, one past midnight counted
    whole on the day it started. A duty twice in the file (one kind, one
    start) counts once, its last, as its session does."""
    filed = {(bucket_for(d.end, now), d.start): d for d in duties}
    seconds = {}
    for (kind, start), d in filed.items():
        key = (HOURS[kind], start.date().isoformat())
        seconds[key] = seconds.get(key, 0) + (d.end - d.start).total_seconds()
    return {key: s / 3600 for key, s in seconds.items()}


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
