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

Uploading again replaces a duty of the same kind and start, and a day's
figure; it removes nothing, so a duty taken off the roster keeps its
row until another lands on its start.

All or nothing: a refusal - no duties, a time not in UTC, a day over 24
hours - writes nothing; the host rolls back whatever was already filed.

The parsing, labelling and routing are module functions - parse_events(),
duties(), classify(), bucket_for(), daily_hours() - pure, for a test to
call on their own.
"""

import re
from datetime import date, datetime, timezone
from typing import NamedTuple

ROSTERED = "duties_rostered"
COMPLETE = "duties_complete"

# Each kind's figure a day: the app's own metrics (app.yaml).
HOURS = {ROSTERED: "duty_hours_rostered", COMPLETE: "duty_hours_complete"}

NON_UTC = "roster has a non-UTC time, which isn't supported yet: {}"


def render(ctx, cfg):
    picker = ctx.input("file", "upload", label="Upload roster", kind="file", accept=".ics")
    flown = ctx.metric(HOURS[COMPLETE])
    rostered = ctx.metric(HOURS[ROSTERED])
    if not flown and not rostered:
        return ctx.panel(ctx.empty("Upload your roster .ics"), picker)

    # The flown hours, and one line of text under them - no label, no
    # sparkline - so a 2x1 tile still has room for the picker: render()
    # isn't told its tile's size, so this has to fit the smaller one.
    caption = f"flown · {_days(flown)} · rostered {_total(rostered):g} h · {_days(rostered)}"
    return ctx.panel(ctx.number(_total(flown), unit="h", caption=caption), picker)


def _total(points):
    """The hours of a metric's days, summed, to a tenth."""
    return round(sum(p.value for p in points), 1)


def _days(points):
    return f"{len(points)} day{'' if len(points) == 1 else 's'}"


def handle(ctx, action, payload):
    if action != "upload":
        raise ValueError(f"unknown action: {action!r}")
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
_FLIGHT = re.compile(r"CX\s*(\d+)")              # CX105, or CX 105
_ROUTE = re.compile(r"([A-Z]{3})-([A-Z]{3})")    # HKG-MEL
_POSITION = re.compile(r"[A-Z]{2,3}")            # RP, RQ, ...


def classify(summary):
    """What a duty is, from its title ("05-Nov CX105 HKG-MEL RP"): {type,
    code}, and for a flight its flight_no, dep, arr and position where the
    title has them. Best effort, never a refusal: a code it doesn't know
    is "other"."""
    title = summary.strip()
    day = _DAY.match(title)
    if day:
        title = title[day.end():]
    tokens = title.split()
    code = tokens[0] if tokens else ""
    flight = _FLIGHT.match(title)
    if flight:
        found = {"type": "flight", "code": "CX" + flight.group(1), "flight_no": "CX" + flight.group(1)}
        rest = title[flight.end():].split()
        route = next((m for m in map(_ROUTE.fullmatch, rest) if m), None)
        if route:
            found["dep"], found["arr"] = route.groups()
        if rest and _POSITION.fullmatch(rest[-1]):
            found["position"] = rest[-1]
        return found
    if re.match(r"A\d", code):
        return {"type": "sim", "code": code}
    if code.startswith("R"):
        return {"type": "reserve", "code": code}
    return {"type": "other", "code": code}


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
