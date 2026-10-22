"""
The app host: what an installed app's render() is given, and how it's run.

An app (apps.py) is a manifest plus a render(ctx, cfg) that returns a
display spec - plain JSON the Home tab draws (the renderer in
index.html). render() runs here, server-side, once per request, with a
fresh Ctx built over that request's connection:

    def render(ctx, cfg):
        data = ctx.metric("steps", 7)
        if not data:
            return ctx.empty("No steps yet")
        latest = data[-1]
        return ctx.panel(
            ctx.number(latest.value, label="steps"),
            ctx.line([(p.date, p.value) for p in data]),
        )

An app reads through the ctx, and only through it - the same answers
the access contract's routes give:

ctx.metric() is one metric's derived points, the ones GET
/api/metric/<metric> serves, as a list of Points (.date, .value,
.source), oldest first - so an app never sees raw per-source readings.
The ctx is made for the app (its InstalledApp), so it reads OpenFit's
metrics and the ones the app invents - declared in its manifest's
`metrics`, said by their short key, stored as app:<id>:<key>
(custom_metric_key()).

ctx.sessions() is one session kind's sessions, the ones GET
/api/sessions/<kind> serves, as a list of Sessions (.start, .end,
.source, .summary) - every source's, the one the pick would rank first
leading, a night two devices both tracked there twice - so an app can
read a roster's duties, or a night's sleep, back:

    duties = ctx.sessions("duties_complete", since="2026-10-01")

ctx.today() is the server's local date.

An app may also write, from an optional handle(ctx, action, payload)
beside render() - what a tile's input element sends when its Save is
pressed (POST /api/app/<id>/action):

    def handle(ctx, action, payload):
        if action == "log":
            ctx.write_metric("weight_kg", float(payload["weight_kg"]))

handle() is given a WriteCtx: the same reads, plus ctx.write_metric()
and ctx.write_session() (below).
Its source is fixed to the app's own, app:<id>, when it's made - no
writer takes a source - so an app can only ever create or overwrite its
own rows, never a device's. It writes OpenFit's metrics, and the ones
it invents - declare what you invent, write what exists:

    # app.yaml
    metrics:
      - {key: caffeine_mg, label: Caffeine, unit: mg,
         category: "Nutrition & hydration", grain: daily}

    ctx.write_metric("caffeine_mg", 95)   # stored as app:<id>:caffeine_mg

but never another app's: an app's own keys are namespaced under its id,
and a ctx knows only its own app's.

handle() can write a session too - an interval, as a night's sleep or a
workout is - with ctx.write_session(kind, start, end=None, summary=None),
under the same source: `kind` one of OpenFit's session kinds
(SESSION_KINDS in metrics.py: sleep, workout, duties_rostered,
duties_complete - an app can't invent one yet), `start` and `end` each a datetime or ISO-8601 text, stored as UTC
to the second (end may be None: open-ended; never before start), and
`summary` a dict of plain JSON, or None:

    ctx.write_session("workout", start, end, {"duration_minutes": 30})

A session is filed whole, as a sync files one - nothing is derived from
it - and read back with ctx.sessions(kind), or GET /api/sessions/<kind>,
beside every other source's. That's the whole of the scoping: the app is
trusted code, as its render() is, not sandboxed.

A file input's value in the payload is a FileUpload - the file as the
browser sent it, which the app reads and parses itself: OpenFit carries
a file, and never reads one. A file input may carry a companion toggle
- a checkbox beside the picker - whose checked state comes in the same
payload, under its own key, and which can ask for a confirm first.

The spec vocabulary, built by the ctx's builders (an app may also return
the dicts directly):

    {"type": "number", "value", "label", "caption", "unit"}
        a large value; label, caption and unit may be null
    {"type": "line", "points": [[date, value], ...], "title"}
        a sparkline, oldest first; a null value is a gap, a null date an
        undated point, and title (optional) names it
    {"type": "panel", "children": [spec, ...]}
        its children, stacked in order
    {"type": "input", "key", "action", "label", "kind", "unit", "options", "accept",
     "toggle"}
        a field and a Save button: Save sends `action` to the app's
        handle(), the payload {key: what was entered} - a number for
        kind "number", text for "text", the option chosen for "select"
        (a dropdown of `options`, a list of strings; null for the other
        kinds), and for "file" the file picked (a FileUpload; `accept`
        the picker's hints, such as ".ics,.csv", or null - null for the
        other kinds) - then the tile is drawn afresh; label and unit may
        be null. `toggle`, on a file input only when given, is
        {"key", "label", "confirm"}: a checkbox beside the picker, its
        checked state sent in the payload under its own key beside the
        file, and with it ticked the page asks `confirm` (null: none)
        before it sends
    {"type": "timeline", "title", "now", "lanes": [{"label", "grey_after_now",
                                                    "intervals": [{"start", "end", "label"}, ...],
                                                    "bands": [{"start", "end", "label"}, ...]}, ...],
     "sync"}
        one lane or two of time-blocks, side by side on one vertical
        time axis - lane 0 left and lighter, lane 1 right and darker -
        laid out by the page to its tile and scrolled: each block from
        its start to its end (UTC text), its label on it; a line at
        `now` (null: none), and a grey_after_now lane greyed below it.
        `bands`, on a lane only when it has any, are spans shaded
        behind that lane's blocks - a trip away, its label the port.
        `sync`, only when given, names a focus group: the page draws a
        fixed cursor at the frame's middle, the time under it (UTC, to
        the quarter hour) and a Now button, and every timeline of the
        group scrolls to the one focus time - following now until one
        is scrolled
    {"type": "rail", "side", "title", "cards": [{"id", "start", "end", "out", "in", "port"}, ...],
     "sync"}
        a column of fixed-size cards - a trip each, four corners: out
        and port over in and its departure day - at most five at once,
        stacked from the top, the window by the focus time and `side`:
        "plan" from the first card not over and on, its top card the
        head; "report" back from the latest card over, its bottom card
        the head - held full at the list's ends, the head outlined. A
        card's end may be null (still open), its in null (a dash).
        `sync`, only when given, names the focus group it reads: it
        restacks whenever that group's time moves, and never moves it
    {"type": "rails", "left": rail, "timeline": timeline, "right": rail}
        a timeline flanked by rails - four columns: a rail, the
        timeline's two lanes, a rail - the timeline carried verbatim and
        drawn as it is alone, either rail null for none; left out in a
        tile too small to hold them
    {"type": "grid", "cols", "rows", "cells": [{"x", "y", "w", "h", "spec"}, ...]}
        a tile split into sub-panels, each drawn as a tile of its own on
        the Home grid's cells; x, y from 0; cells inside the grid, never
        overlapping
    {"type": "strips", "now", "lanes": [{"kind", "label", "clock",
                                         "spans": [{"start", "end", "tone", "label", "place", "tz"}, ...],
                                         "troughs": [instant, ...],
                                         "bands": [{"start", "end", "label"}, ...],
                                         "wocl": [{"start", "end"}, ...]}, ...],
     "sync"}
        one lane to three of tinted spans, stacked on one horizontal time
        axis - a day across the frame, the rest a scroll away - the
        first lane taking the height the others leave, each after it a
        slim row. A "sky" lane's spans are day, night or none (hatched),
        a "duty" lane's day, night or wocl, butting square, a span's
        label drawn once at its start; troughs a solid curve over the
        lane, a sky lane's wocl spans red under it. A duty lane's bands
        are bars behind its spans - a trip away, between the flight out
        and the flight home - each label (the port) in it, past the
        flight out. A lane's tag, top-left, is its label - over, on a
        clock lane, the time under the cursor: `clock` "place" the place
        there and its local time, "date" the date and its local time
        (false for none). `now` and `sync` as a timeline's: synced, the
        page keeps a cursor at the frame's middle and scrolls every
        member of the group to one time
    {"type": "empty", "message"}
    {"type": "error", "message"}
        the host's, when a render fails; an app's for a part of its tile
        that failed - a sub-panel - so the rest is still drawn

render_app() never raises: an app whose render() raises, or returns
something that isn't a spec, gets an error spec in its place, so one
broken app shows as one error tile rather than failing the page.
"""

import json
import math
import traceback
from datetime import date, datetime, timedelta
from typing import NamedTuple

from apps import scan_apps
from derived import derived_points, session_rows
from metrics import METRICS, SESSION_KINDS, finite_number, plausible_value
from plugins.base import iso_utc, write_reading, write_session as _write_session


class Point(NamedTuple):
    """One day of a metric, as ctx.metric() lists them."""

    date: str      # YYYY-MM-DD
    value: float   # in the metric's canonical unit (metrics.py)
    source: str    # the source it was picked from


class Session(NamedTuple):
    """One session, as ctx.sessions() lists them: GET /api/sessions/<kind>'s fields."""

    start: str      # ISO 8601 UTC, YYYY-MM-DDTHH:MM:SSZ, as stored
    end: object     # the same, or None: no end yet
    source: str     # the source it was filed under: garmin, app:frms, ...
    summary: dict   # the kind's breakdown (beside SESSION_KINDS); {} for none


def trailing_window(days, today=None):
    """(since, until) for the trailing `days` days: today and the days - 1 before it.

    ISO dates, both inclusive - a sync's `days` counts the same way. A
    window reaching back before the calendar starts is open at that end.
    """
    today = today or date.today()
    try:
        since = (today - timedelta(days=days - 1)).isoformat()
    except OverflowError:
        since = None
    return since, today.isoformat()


def _positive_days(days):
    # bool is an int to Python, and days=True is a mistake, not 1.
    if isinstance(days, bool) or not isinstance(days, int) or days < 1:
        raise ValueError(f"days must be a whole number of at least 1: {days!r}")
    return days


# ---------- the spec builders ----------


def _finite_or_none(value):
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def number(value, label=None, caption=None, unit=None):
    """A large value, with an optional label beneath and a smaller caption."""
    return {
        "type": "number",
        "value": _finite_or_none(value),
        "label": label,
        "caption": caption,
        "unit": unit,
    }


def _line_value(raw):
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise TypeError(f"line values must be numbers or None: {raw!r}")
    return _finite_or_none(raw)


def _dated(day, value):
    """A line point as the spec carries it: [date, value]."""
    if isinstance(day, date):
        day = day.isoformat()
    elif day is not None and not isinstance(day, str):
        raise TypeError(f"a line point's date must be YYYY-MM-DD: {day!r}")
    return [day, _line_value(value)]


def _line_point(raw):
    if isinstance(raw, Point):
        return _dated(raw.date, raw.value)
    if isinstance(raw, (tuple, list)):
        if len(raw) != 2:
            raise TypeError(f"a line point pair is (date, value): {raw!r}")
        return _dated(*raw)
    if isinstance(raw, dict):
        if "value" not in raw:
            raise TypeError(f"a line point needs a value: {raw!r}")
        return _dated(raw.get("date"), raw["value"])
    return _dated(None, raw)


def line(points, title=None):
    """A sparkline over `points`, oldest first, with an optional title above it.

    `points` are (date, value) pairs - [(p.date, p.value) for p in data] -
    or ctx.metric()'s Points as they come, {date, value} dicts or bare
    numbers; each becomes [date, value] in the spec, date null for a bare
    number. A None value (or a non-finite one) is a gap, never a zero; a
    point's source is dropped. `title` is text or None.
    """
    if title is not None and not isinstance(title, str):
        raise TypeError(f"a line's title must be text: {title!r}")
    return {"type": "line", "points": [_line_point(p) for p in points], "title": title}


def panel(*children):
    """Its children, stacked in order: panel(a, b) or panel([a, b])."""
    if len(children) == 1 and isinstance(children[0], (list, tuple)):
        children = children[0]
    for child in children:
        if not isinstance(child, dict) or not isinstance(child.get("type"), str):
            raise TypeError(f"a panel's children must be specs: {child!r}")
    return {"type": "panel", "children": list(children)}


# What an input element's field takes: a number, text, one of a set of
# options (a dropdown), or a file.
INPUT_KINDS = ("number", "text", "select", "file")

OPTIONS_PROBLEM = "a select's options must be a non-empty list of strings"
ACCEPT_PROBLEM = "a file input's accept must be non-empty text, such as '.ics,.csv'"

# What a file input's companion toggle takes, and nothing else: a
# misspelt "confrim" is refused, not quietly no confirm.
TOGGLE_FIELDS = ("key", "label", "confirm")


def _text_or_none(value, what, whose="an input's"):
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{whose} {what} must be text: {value!r}")
    return value


def _options(kind, options):
    """A select's options as a plain list of strings; None for the other kinds."""
    if kind != "select":
        if options is not None:
            raise ValueError(f"options is only for a select, not a {kind}: {options!r}")
        return None
    if options is None or (isinstance(options, (list, tuple)) and not options):
        raise ValueError(f"{OPTIONS_PROBLEM}: {options!r}")
    if not isinstance(options, (list, tuple)):
        raise TypeError(f"{OPTIONS_PROBLEM}: {options!r}")
    for option in options:
        if not isinstance(option, str) or not option:
            raise TypeError(f"{OPTIONS_PROBLEM}: {option!r}")
    return [str(option) for option in options]


def _accept(kind, accept):
    """A file input's picker hints, as written, or None; None for the other kinds."""
    if kind != "file":
        if accept is not None:
            raise ValueError(f"accept is only for a file, not a {kind}: {accept!r}")
        return None
    if accept is None:
        return None
    if not isinstance(accept, str):
        raise TypeError(f"{ACCEPT_PROBLEM}: {accept!r}")
    if not accept:
        raise ValueError(f"{ACCEPT_PROBLEM}: {accept!r}")
    return accept


def _toggle(kind, toggle, key):
    """A file input's companion toggle as {key, label, confirm} - confirm
    None for none - or None; None for the other kinds."""
    if kind != "file":
        if toggle is not None:
            raise ValueError(f"a toggle is only for a file, not a {kind}: {toggle!r}")
        return None
    if toggle is None:
        return None
    what = "a file input's toggle"
    _fields(toggle, TOGGLE_FIELDS, what)
    for field in ("key", "label"):
        value = toggle.get(field)
        if not isinstance(value, str):
            raise TypeError(f"{what}'s {field} must be non-empty text: {value!r}")
        if not value:
            raise ValueError(f"{what}'s {field} must be non-empty text: {value!r}")
    if toggle["key"] == key:
        raise ValueError(f"{what}'s key can't be the input's own, {key!r}: the file is sent under it")
    confirm = toggle.get("confirm")
    if confirm is not None:
        if not isinstance(confirm, str):
            raise TypeError(f"{what}'s confirm must be text: {confirm!r}")
        if not confirm:
            raise ValueError(f"{what}'s confirm must be non-empty text, or left out: {confirm!r}")
    return {"key": toggle["key"], "label": toggle["label"], "confirm": confirm}


def input(key, action, label=None, kind="number", unit=None, options=None, accept=None, toggle=None):
    """A field and a Save button that sends `action` to the app's handle().

    The payload is {key: what was entered}: a number for kind "number",
    text for "text", for "select" the option chosen from `options`
    (required for a select, and only for one: a non-empty list of
    non-empty strings, each shown and sent as written), and for "file"
    the file picked, a FileUpload. The host doesn't check the value sent
    is one of the options, nor what a file holds - handle() does, as it
    checks a number. `accept` is a file picker's hints, only for a file
    and optional: non-empty text, as an <input type="file"> takes it
    (".ics,.csv", "text/plain") - the picker's suggestion, never
    enforced. `label` names the field, `unit` sits beside it; either may
    be None. Once handle() has run, the tile is drawn afresh from
    render().

    `toggle` is a file input's companion checkbox, only for a file and
    optional: a mapping {key, label, confirm} - `key` the payload key
    its checked state is sent under (non-empty text, never the input's
    own `key`, which the file has), `label` the checkbox's label
    (non-empty text), and `confirm`, optional, text the page asks the
    user to confirm before it sends with the box ticked (unticked, Save
    sends straight away). The payload is then {key: the FileUpload,
    toggle key: True or False}:

        ctx.input("file", "upload", kind="file", accept=".ics",
                  toggle={"key": "backfill", "label": "Backfill",
                          "confirm": "Backfill past duties too?"})

    An input without one carries no "toggle" key: its spec is the one
    from before toggles.
    """
    for what, value in (("key", key), ("action", action)):
        if not isinstance(value, str) or not value:
            raise TypeError(f"an input's {what} must be non-empty text: {value!r}")
    if kind not in INPUT_KINDS:
        raise ValueError(f"an input's kind is one of {', '.join(INPUT_KINDS)}: {kind!r}")
    spec = {
        "type": "input",
        "key": key,
        "action": action,
        "label": _text_or_none(label, "label"),
        "kind": kind,
        "unit": _text_or_none(unit, "unit"),
        "options": _options(kind, options),
        "accept": _accept(kind, accept),
    }
    companion = _toggle(kind, toggle, key)
    if companion is not None:
        spec["toggle"] = companion
    return spec


class FileUpload:
    """A file a tile's file input sent: what handle() finds at payload[key].

    The host carries the file and never reads it - what it is, and how to
    read it, is the app's: text for a .csv or an .ics, the bytes or a
    path for a PDF or a SQLite file. It lives in a temp file the host
    chose, for the length of the action, and is deleted once the action
    is done, whatever handle() did: nothing of it is kept but what the
    app writes.

    .name is the file's name as the browser gave it, for display only -
    never a path; .content_type the type the browser reported ("" for
    none), informational, so not to be trusted; .bytes the file's
    content; .text(encoding) that decoded, raising UnicodeDecodeError
    for a file that isn't text in it - the app's to catch; .path the
    temp file, for a library that wants one (sqlite3.connect(f.path)).
    """

    def __init__(self, path, name="", content_type=""):
        self.path = path
        self.name = name
        self.content_type = content_type
        self._bytes = None

    @property
    def bytes(self):
        """The file's content, read from its temp file once, when first asked for."""
        if self._bytes is None:
            with open(self.path, "rb") as f:
                self._bytes = f.read()
        return self._bytes

    def text(self, encoding="utf-8"):
        """The file's content decoded: UnicodeDecodeError if it isn't text in `encoding`."""
        return self.bytes.decode(encoding)


# A timeline's lanes: one, or two side by side - lane 0 the left column,
# lane 1 the right. What a lane, an interval and a band take, and nothing
# else: a misspelt field (gray_after_now) is refused, not quietly ignored.
TIMELINE_LANES = (1, 2)
LANE_FIELDS = ("label", "grey_after_now", "intervals", "bands")
INTERVAL_FIELDS = ("start", "end", "label")
BAND_FIELDS = ("start", "end", "label")


def _fields(raw, fields, what):
    """`raw`, a mapping of only `fields`: TypeError for one that isn't a
    mapping, ValueError for a field it doesn't take."""
    if not isinstance(raw, dict):
        raise TypeError(f"{what} must be a mapping: {raw!r}")
    unknown = [repr(field) for field in raw if field not in fields]
    if unknown:
        raise ValueError(f"{what} takes {', '.join(fields)} - not {', '.join(unknown)}")
    return raw


def _span(raw, fields, what):
    """A span of a lane - `what`, an interval or a band - as the spec
    carries it: {start, end, label}, both ends iso_utc()'s UTC text."""
    _fields(raw, fields, what)
    if raw.get("start") is None or raw.get("end") is None:
        # Drawn from its start to its end: an open one has no length to draw.
        raise ValueError(f"{what} needs a start and an end: {raw!r}")
    start = _session_instant(raw["start"], f"{what}'s start")
    end = _session_instant(raw["end"], f"{what}'s end")
    # iso_utc()'s timestamps sort and compare as plain strings.
    if end < start:
        raise ValueError(f"{what}'s end is before its start: {end} < {start}")
    return {"start": start, "end": end, "label": _text_or_none(raw.get("label"), "label", f"{what}'s")}


def _interval(raw):
    """One block on a lane."""
    return _span(raw, INTERVAL_FIELDS, "a timeline interval")


def _band(raw):
    """One band on a lane: a span shaded behind its blocks."""
    return _span(raw, BAND_FIELDS, "a timeline band")


def _lane(raw):
    """One lane as the spec carries it: {label, grey_after_now,
    intervals}, and its bands only when it has any - so a lane without
    is exactly the lane from before bands."""
    _fields(raw, LANE_FIELDS, "a timeline lane")
    grey = raw.get("grey_after_now", False)
    if not isinstance(grey, bool):
        raise TypeError(f"a timeline lane's grey_after_now must be True or False: {grey!r}")
    intervals = raw.get("intervals")
    if not isinstance(intervals, (list, tuple)):
        raise TypeError(f"a timeline lane's intervals must be a list: {intervals!r}")
    bands = raw.get("bands")
    if bands is None:
        bands = []
    if not isinstance(bands, (list, tuple)):
        raise TypeError(f"a timeline lane's bands must be a list: {bands!r}")
    lane = {
        "label": _text_or_none(raw.get("label"), "label", "a timeline lane's"),
        "grey_after_now": grey,
        "intervals": [_interval(interval) for interval in intervals],
    }
    if bands:
        lane["bands"] = [_band(band) for band in bands]
    return lane


def timeline(lanes, now=None, title=None, sync=None):
    """A two-lane interval timeline: time-blocks on a shared vertical axis, lane 0 left, lane 1 right.

    `lanes` is a list of one lane or two. Their order is what's drawn:
    lane 0 the left column, in the lighter tone, lane 1 the right, in
    the darker (one lane takes the whole width, in lane 0's). Each is a
    dict:

        label           text or None: the column's heading
        grey_after_now  True for a lane with nothing after `now` - the
                        flown side of a roster - whose column is greyed
                        from the now line down; False by default
        intervals       a list of {start, end, label}: each a block from
                        `start` to `end` - a datetime or ISO-8601 text,
                        stored as a session's are, UTC to the second (a
                        naive time taken to be UTC) - both required, the
                        end never before the start; `label` text or
                        None, a short code drawn on the block (CX105)
        bands           optional: a list of {start, end, label}, each a
                        span shaded behind the lane's blocks - a trip
                        away from base, its flights drawn over it -
                        its ends as an interval's, `label` text or None
                        (the port, MEL), drawn small inside it. A lane
                        without any (none, None or []) carries no
                        "bands" key: the spec is the one from before

    `now` is a datetime or ISO-8601 text, the same way, or None: where
    the now line is drawn - none for None. `title` is text or None.

    `sync` is text or None: the name of a focus group ("frms") the
    timeline joins - an empty name is none. Given, the spec carries it
    and the page draws the timeline around a focus clock it keeps for
    the group: a fixed cursor across the middle of the frame, the time
    under it (UTC, to the quarter hour) over the lanes, the now line
    labelled Now, and a Now button once it's been scrolled off now.
    Every timeline of one group, in any tile, scrolls to the same time:
    the time is what's shared, never a scroll position. None (the
    default) leaves the spec - and the timeline drawn - as it was.

    The host carries the times and draws nothing: the page lays the axis
    out to the tile it's in, as it does a line - a week of it to the
    tile's height (less, in a short tile), the rest a scroll away - and
    shades a band in its own lane, on the same scale as the blocks. What
    a band spans is the app's to work out (the roster's trips()). The
    focus clock is the page's too - what's in focus, and whether it's
    following now, is never stored.
    Raises TypeError or ValueError for anything it can't draw.
    """
    if not isinstance(lanes, (list, tuple)):
        raise TypeError(f"a timeline's lanes must be a list of one lane or two: {lanes!r}")
    if len(lanes) not in TIMELINE_LANES:
        raise ValueError(f"a timeline has one lane or two, not {len(lanes)}")
    if title is not None and not isinstance(title, str):
        raise TypeError(f"a timeline's title must be text: {title!r}")
    if sync is not None and not isinstance(sync, str):
        raise TypeError(f"a timeline's sync must be text, a focus group's name: {sync!r}")
    spec = {
        "type": "timeline",
        "title": title,
        "now": None if now is None else _session_instant(now, "a timeline's now"),
        "lanes": [_lane(lane) for lane in lanes],
    }
    if sync:
        spec["sync"] = sync
    return spec


# A rail's cards: what one takes, and nothing else - a misspelt field is
# refused, as a lane's is. And a rail's side: which way its window looks.
RAIL_CARD_FIELDS = ("id", "start", "end", "out", "in", "port")
RAIL_SIDES = ("plan", "report")


def _card(raw):
    """One card on a rail, as the spec carries it: {id, start, end, out,
    in, port} - start iso_utc()'s UTC text, end the same or None."""
    what = "a rail card"
    _fields(raw, RAIL_CARD_FIELDS, what)
    if raw.get("start") is None:
        raise ValueError(f"{what} needs a start: {raw!r}")
    start = _session_instant(raw["start"], f"{what}'s start")
    end = raw.get("end")
    if end is not None:
        end = _session_instant(end, f"{what}'s end")
        # iso_utc()'s timestamps sort and compare as plain strings.
        if end < start:
            raise ValueError(f"{what}'s end is before its start: {end} < {start}")
    card = {"id": None, "start": start, "end": end, "out": None, "in": None, "port": None}
    for field in ("id", "out", "in", "port"):
        card[field] = _text_or_none(raw.get(field), field, f"{what}'s")
    return card


def rail(cards, side, title=None, sync=None):
    """A column of fixed-size cards - a trip each, in the roster's - that
    restacks as a focus group's time moves.

    `cards` is a list of dicts, each:

        id      text or None: the card's stable id (a trip's: its flight
                out's start), carried for the page, never drawn
        start   a datetime or ISO-8601 text, stored as a session's are
                (UTC to the second, a naive time taken to be UTC) -
                required; its day is drawn, the departure date
        end     the same, or None for one still open (a trip not home
                as far as the data knows); never before the start
        out     text or None: the flight out (CX105)
        in      text or None: the flight home (CX104) - a dash for none
        port    text or None: the port away, the layover's or the
                turn-round's (MEL) - the card's prominent corner

    The page draws each card as four corners - out and port across its
    top, in and its day across its foot - in time order (by start), at
    most five at once, stacked from the top of the rail, and works out
    which from the time in focus, by its `side`:

        "plan"    looks ahead: the first card not over at that time and
                  the ones after it, the top card the head
        "report"  looks back: the latest card over by then and the ones
                  before it, the bottom card the head

    - the window held full at the list's ends, so a rail with five or
    more always shows five (fewer, in a frame too short to hold them).
    The head, the trip at the cursor's edge of the window, is outlined.
    Any other side raises ValueError.

    `title` is text or None: the rail's heading (Plan). `sync` is text
    or None, a focus group's name, as a timeline's: given, the rail
    reads that group's focus time and restacks whenever it moves - it
    never moves it - and the spec carries it; None (or an empty name)
    adds no key, and the rail is drawn for the time it's drawn at.
    Raises TypeError or ValueError for anything it can't draw.
    """
    if not isinstance(cards, (list, tuple)):
        raise TypeError(f"a rail's cards must be a list: {cards!r}")
    if not isinstance(side, str) or side not in RAIL_SIDES:
        raise ValueError(f"a rail's side is {' or '.join(map(repr, RAIL_SIDES))}, not {side!r}")
    if title is not None and not isinstance(title, str):
        raise TypeError(f"a rail's title must be text: {title!r}")
    if sync is not None and not isinstance(sync, str):
        raise TypeError(f"a rail's sync must be text, a focus group's name: {sync!r}")
    spec = {"type": "rail", "side": side, "title": title, "cards": [_card(card) for card in cards]}
    if sync:
        spec["sync"] = sync
    return spec


def _of_type(spec, kind, what, builder):
    """`spec`, if it's a spec of type `kind`: TypeError for one that isn't a
    mapping, ValueError for another type."""
    if not isinstance(spec, dict):
        raise TypeError(f"{what} must be a {kind} spec ({builder}): {spec!r}")
    if spec.get("type") != kind:
        raise ValueError(f"{what} must be a {kind} spec ({builder}), not {spec.get('type')!r}")
    return spec


def rails(timeline, left=None, right=None):
    """A timeline flanked by rails: four columns - a rail, the timeline's
    two lanes, a rail.

    `timeline` is a timeline spec (ctx.timeline()), carried verbatim -
    drawn exactly as it is on its own. `left` and `right` are rail specs
    (ctx.rail()) or None, the rail beside each side of it: a plan beside
    the rostered lane, reports beside the flown. The rails only read
    the focus time; the timeline keeps its own. In a tile too small to
    hold them the page leaves them out, and the timeline has the tile.
    Raises TypeError or ValueError for a part that isn't the spec it
    should be.
    """
    _of_type(timeline, "timeline", "a rails frame's timeline", "ctx.timeline()")
    sides = {
        side: None if given is None else _of_type(given, "rail", f"a rails frame's {side}", "ctx.rail()")
        for side, given in (("left", left), ("right", right))
    }
    return {"type": "rails", "left": sides["left"], "timeline": timeline, "right": sides["right"]}


# What a grid cell takes, and nothing else: a misspelt field (wdith) is
# refused, as a lane's is.
GRID_CELL_FIELDS = ("x", "y", "w", "h", "spec")


def grid(cols, rows, cells):
    """A tile split into sub-panels: `cols` by `rows` cells, each sub-panel
    drawn as a tile of its own.

    `cols` and `rows` are whole numbers, 1 to 6 (a tile's own most). `cells`
    is a list of {x, y, w, h, spec}: a sub-panel `w` cells wide and `h` high,
    its top-left corner at column `x`, row `y`, counted from 0, and `spec`
    the spec drawn in it - sized to the sub-panel as it would be to a tile.
    Sub-panels lie inside the grid and never overlap; cells none covers are
    left empty. A cell takes those five keys and no other. Raises TypeError
    or ValueError for anything it can't lay out.
    """
    for what, n in (("cols", cols), ("rows", rows)):
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= 6:
            raise ValueError(f"a grid's {what} must be a whole number from 1 to 6: {n!r}")
    if not isinstance(cells, (list, tuple)):
        raise TypeError(f"a grid's cells must be a list: {cells!r}")
    taken, out = set(), []
    for cell in cells:
        if not isinstance(cell, dict):
            raise TypeError(f"a grid cell must be a dict: {cell!r}")
        _fields(cell, GRID_CELL_FIELDS, "a grid cell")
        x, y, w, h = (cell.get(k) for k in ("x", "y", "w", "h"))
        for k, v in (("x", x), ("y", y), ("w", w), ("h", h)):
            if not isinstance(v, int) or isinstance(v, bool):
                raise TypeError(f"a grid cell's {k} must be a whole number: {v!r}")
        if x < 0 or y < 0 or w < 1 or h < 1 or x + w > cols or y + h > rows:
            raise ValueError(f"a grid cell must lie inside the {cols}x{rows} grid: {cell!r}")
        spec = cell.get("spec")
        if not isinstance(spec, dict) or not isinstance(spec.get("type"), str):
            raise TypeError(f"a grid cell's spec must be a spec: {spec!r}")
        area = {(i, j) for i in range(x, x + w) for j in range(y, y + h)}
        if area & taken:
            raise ValueError(f"grid cells overlap: {cell!r}")
        taken |= area
        out.append({"x": x, "y": y, "w": w, "h": h, "spec": spec})
    return {"type": "grid", "cols": cols, "rows": rows, "cells": out}


# A strips spec's lanes: one to three, stacked. Each is of a kind, which
# says the tones its spans take - a sky's day, night, none (not known:
# hatched) or rest (the body's rest window), a duty's day, night or wocl,
# a sleep lane's in or out (of the rest window), a ticks lane's
# disturbed. A sleep lane's span is a stretch of one sleep stage - its
# `stage` light, deep or rem, drawn as a bar that tall - and only a sleep
# lane's span takes one. What a lane, a span and a band take, and nothing
# else: a misspelt field is refused, as a timeline lane's is. A clock
# lane's tag tells the time under the cursor - the place and its local
# time, or the date and its local time.
STRIP_KINDS = {
    "sky": ("day", "night", "none", "rest"),
    "duty": ("day", "night", "wocl"),
    "sleep": ("in", "out"),
    "ticks": ("disturbed",),
}
STRIP_STAGES = ("light", "deep", "rem")
STRIP_LANES = (1, 2, 3)
STRIP_LANE_FIELDS = ("kind", "spans", "troughs", "label", "clock", "bands", "wocl")
STRIP_SPAN_FIELDS = ("start", "end", "tone", "label", "place", "tz", "stage")
# How many hours one viewport's width shows: a day unless the spec says,
# and never less than half a day or more than a week.
STRIP_HOURS = (12, 168)
STRIP_BAND_FIELDS = ("start", "end", "label")
STRIP_CLOCKS = ("place", "date")


def _strip_span(raw, kind):
    """One span of a strips lane of `kind`, as the spec carries it: {start,
    end, tone, label, place, tz} - both ends iso_utc()'s UTC text, the
    rest text or None - and, on a sleep lane only, its `stage` too."""
    what = "a strips span"
    _fields(raw, STRIP_SPAN_FIELDS, what)
    if raw.get("start") is None or raw.get("end") is None:
        # Drawn from its start to its end: an open one has no length to draw.
        raise ValueError(f"{what} needs a start and an end: {raw!r}")
    start = _session_instant(raw["start"], f"{what}'s start")
    end = _session_instant(raw["end"], f"{what}'s end")
    # iso_utc()'s timestamps sort and compare as plain strings.
    if end < start:
        raise ValueError(f"{what}'s end is before its start: {end} < {start}")
    tones = STRIP_KINDS[kind]
    tone = raw.get("tone")
    if tone not in tones:
        raise ValueError(f"{what}'s tone on a {kind} lane is one of {', '.join(tones)}, not {tone!r}")
    label = _text_or_none(raw.get("label"), "label", f"{what}'s")
    if label is not None and kind != "duty":
        # A sky's span is a stretch of day or night: a duty's is a duty's.
        raise ValueError(f"{what}'s label is only for a duty lane, not a {kind}: {label!r}")
    stage = raw.get("stage")
    if kind == "sleep" and stage not in STRIP_STAGES:
        raise ValueError(f"{what}'s stage on a sleep lane is one of {', '.join(STRIP_STAGES)}, not {stage!r}")
    if kind != "sleep" and stage is not None:
        # A stage is a sleep's: a sky's, duty's or tick's span has none.
        raise ValueError(f"{what}'s stage is only for a sleep lane, not a {kind}: {stage!r}")
    span = {
        "start": start, "end": end, "tone": tone, "label": label,
        "place": _text_or_none(raw.get("place"), "place", f"{what}'s"),
        "tz": _text_or_none(raw.get("tz"), "tz", f"{what}'s"),
    }
    if kind == "sleep":
        span["stage"] = stage
    return span


def _strip_band(raw):
    """One band of a duty lane - a trip away - as the spec carries it:
    {start, end, label}, both ends iso_utc()'s UTC text."""
    return _span(raw, STRIP_BAND_FIELDS, "a strips band")


def _strip_wocl(raw):
    """One span of a sky lane's WOCL - its curve marked under it - as the
    spec carries it: {start, end}, both ends iso_utc()'s UTC text."""
    span = _span(raw, ("start", "end"), "a strips wocl span")
    return {"start": span["start"], "end": span["end"]}


def _strip_list(raw, field, kind, only, item):
    """A lane's `field`, a list of `item`s that only an `only` lane takes:
    [] when not given - none, None or [], on any lane, so a lane as the
    spec carries it is one strips() takes back."""
    what = "a strips lane"
    value = raw.get(field)
    if value is None:
        return []
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"{what}'s {field} must be a list: {value!r}")
    if value and kind != only:
        raise ValueError(f"{what} takes {field} only if it's a {only} lane, not a {kind}: {value!r}")
    return [item(x) for x in value]


def _strip_lane(raw):
    """One lane of a strips spec, as the spec carries it: {kind, label,
    clock, spans, troughs, bands, wocl}."""
    what = "a strips lane"
    _fields(raw, STRIP_LANE_FIELDS, what)
    kind = raw.get("kind")
    if not isinstance(kind, str) or kind not in STRIP_KINDS:
        raise ValueError(f"{what}'s kind is {' or '.join(map(repr, STRIP_KINDS))}, not {kind!r}")
    spans = raw.get("spans")
    if not isinstance(spans, (list, tuple)):
        raise TypeError(f"{what}'s spans must be a list: {spans!r}")
    troughs = raw.get("troughs")
    if troughs is None:
        troughs = []
    if not isinstance(troughs, (list, tuple)):
        raise TypeError(f"{what}'s troughs must be a list of instants: {troughs!r}")
    # None, the place and its time, or the date and its time: True is the place's.
    clock = raw.get("clock", False)
    if clock is True:
        clock = "place"
    if clock is not False and clock not in STRIP_CLOCKS:
        raise ValueError(f"{what}'s clock is False, True, 'place' or 'date', not {clock!r}")
    return {
        "kind": kind,
        # With a clock, the tag is the label over it.
        "label": _text_or_none(raw.get("label"), "label", f"{what}'s"),
        "clock": clock,
        "spans": [_strip_span(span, kind) for span in spans],
        "troughs": [_session_instant(t, f"{what}'s trough") for t in troughs],
        "bands": _strip_list(raw, "bands", kind, "duty", _strip_band),
        "wocl": _strip_list(raw, "wocl", kind, "sky", _strip_wocl),
    }


def strips(lanes, now=None, sync=None, hours=None):
    """Lanes of tinted spans on one horizontal time axis - a day across
    the viewport (or `hours` across), the rest a scroll away - the first
    lane filling the height the others leave, each after it a slim row.

    `lanes` is a list of one to three dicts {kind, spans, troughs, label,
    clock, bands, wocl}: `kind` "sky" (spans toned "day", "night", "none"
    - none drawn hatched - or "rest", the body's rest window), "duty"
    (spans toned "day", "night" or "wocl"), "sleep" (spans toned "in" or
    "out" - of the rest window - each with a `stage`, "light", "deep" or
    "rem", drawn as a bar rising that high from the lane's foot) or
    "ticks" (spans toned "disturbed", each drawn as red ticks hanging in
    the lane); `spans` a list of {start, end, tone, label, place, tz}, ends
    as a session's (datetime or ISO-8601 text, UTC), end never before
    start; on a duty lane a span's `label` (text or None) is drawn once at
    its start, over the spans after it; `troughs` optional, a list of
    instants - a solid curve drawn over the lane, -1 at each, +1 midway
    between two. `label` (text or None) is the lane's tag, top-left;
    `clock` makes the tag tell the time under the cursor - False (or
    absent) for none, True or "place" for the span there's `place` (text)
    and its local time in `tz` (an IANA name), "LHR 01:00 UTC+1", and
    "date" for the date and its local time there, "Fri 23 Oct 20:00" -
    with a `label` too, the label over the clock. `bands`, on a duty lane
    only, optional: a list of {start, end, label} - a trip away from
    base, its ends as a span's - each a bar behind the lane's spans,
    showing between the flight out and the flight home, its label (the
    port, text or None) in it past the flight out. `wocl`, on a sky lane
    only, optional: a list of {start, end} - the window of circadian low -
    under which the lane's curve is marked red (drawn only where there's
    a curve). `now` and `sync` are a timeline's: where the now line is
    drawn, and the focus group it joins - the page keeps a cursor on it
    and scrolls every member of the group to one time. Raises TypeError
    or ValueError for anything it can't draw.

    In the spec each lane is {kind, label, clock, spans, troughs, bands,
    wocl} - `clock` False, "place" or "date" (True carried as "place"),
    `bands` and `wocl` [] when not given (none, None or [] - an empty
    list taken on either kind) - each span {start, end, tone, label,
    place, tz}, each band {start, end, label} and each wocl span {start,
    end}, every key there - None where not given - its times iso_utc()'s
    UTC text; `sync` only when given (None or "" adds no key), as a
    timeline's. A sky span's label is refused (a sky's span is day or
    night, not a thing); place and tz are carried on any span, and drawn
    only on a clock lane's - a tz the page can't read shows the place
    alone (a date clock, a dash). A sleep span carries its `stage` too,
    and no other span takes one. `hours` is how many hours one viewport's
    width shows, a whole number from 12 to 168 - 24, a day, when not
    given; the spec carries it only when given (None adds no key), so a
    day's strips are as before.
    """
    if not isinstance(lanes, (list, tuple)):
        raise TypeError(f"a strips spec's lanes must be a list of one to three lanes: {lanes!r}")
    if len(lanes) not in STRIP_LANES:
        raise ValueError(f"a strips spec has one lane to three, not {len(lanes)}")
    if sync is not None and not isinstance(sync, str):
        raise TypeError(f"a strips spec's sync must be text, a focus group's name: {sync!r}")
    if hours is not None and (not isinstance(hours, int) or isinstance(hours, bool)
                              or not STRIP_HOURS[0] <= hours <= STRIP_HOURS[1]):
        raise ValueError(f"a strips spec's hours must be a whole number from {STRIP_HOURS[0]} "
                         f"to {STRIP_HOURS[1]}: {hours!r}")
    spec = {
        "type": "strips",
        "now": None if now is None else _session_instant(now, "a strips spec's now"),
        "lanes": [_strip_lane(lane) for lane in lanes],
    }
    if sync:
        spec["sync"] = sync
    if hours is not None:
        spec["hours"] = hours
    return spec


def empty(message=None):
    """Nothing to show, and why."""
    return {"type": "empty", "message": message or "No data yet"}


def error(message):
    """Something went wrong, and what: the host's for a render that fails,
    an app's for a part of its tile that did - a sub-panel - so the rest
    of the tile is still drawn."""
    return {"type": "error", "message": message}


class Ctx:
    """What render(ctx, cfg) is given. One per render call.

    Made for one app, `app` (an InstalledApp): it reads OpenFit's metrics
    and the ones that app declares (metric()), and every source's
    sessions of OpenFit's kinds (sessions()). A ctx for no app reads the
    same, less the app's own metrics.
    """

    number = staticmethod(number)
    line = staticmethod(line)
    panel = staticmethod(panel)
    input = staticmethod(input)
    timeline = staticmethod(timeline)
    rail = staticmethod(rail)
    rails = staticmethod(rails)
    grid = staticmethod(grid)
    strips = staticmethod(strips)
    empty = staticmethod(empty)
    error = staticmethod(error)

    def __init__(self, conn, today=None, app=None):
        self._conn = conn
        self._today = today or date.today()
        self._app_id = None
        # Short key -> its CustomMetric: the only app metrics this ctx knows.
        self._own = {}
        if app is not None:
            self._app_id = app.id
            self._own = {declared.key: declared for declared in app_metric_defs(app).values()}

    def _resolve(self, key):
        """(the key `key` is stored under, its CustomMetric or None for a
        canonical metric), or (None, None) for a key this app can't use."""
        if key in METRICS:
            return key, None
        own = self._own.get(key) if isinstance(key, str) else None
        if own is None:
            return None, None
        return custom_metric_key(self._app_id, key), own

    def today(self):
        """The server's local date, as YYYY-MM-DD - the format points' dates are in."""
        return self._today.isoformat()

    def metric(self, key, days=None):
        """One metric's derived points: a list of Point(date, value, source).

        Oldest first, so data[-1] is the latest day; one point per day
        that has a value - a day with none is missing, not a zero - and []
        when there are none. Each value is in the metric's canonical unit
        (METRICS in metrics.py), or for one the app invents the unit it
        declares. `days` keeps the trailing days: today and the days - 1
        before it, as GET /api/metric/<key>?days= does; None is the whole
        history.

        `key` is a canonical metric, or one this ctx's app declares, by
        its short key (caffeine_mg, read from app:<id>:caffeine_mg).
        Raises ValueError for anything else - another app's key among
        them.
        """
        stored, _ = self._resolve(key)
        if stored is None:
            raise ValueError(f"unknown metric: {key!r}")
        since = until = None
        if days is not None:
            since, until = trailing_window(_positive_days(days), self._today)
        return [
            Point(p["date"], p["value"], p["source"])
            for p in derived_points(self._conn, stored, since, until)
        ]

    def sessions(self, kind, since=None, until=None):
        """Every source's sessions of `kind` in the range, as the read contract serves them.

        A list of Session(start, end, source, summary): the sessions GET
        /api/sessions/<kind> serves, field for field and in its order - a
        priority-ordered passthrough, nothing picked, filtered or merged,
        so a night two devices both tracked is there twice, each with its
        source. The sessions of the source the derived pick would rank
        first lead (the kind's primary in the source roles, if one is
        set, then garmin, google_health, then the rest by name - an app's
        app:<id> among them), each source's by start; [] for none. Re-sort
        for one timeline: sorted(data, key=lambda s: s.start).

        .start is when it began and .end when it finished, ISO 8601 UTC
        text as stored (YYYY-MM-DDTHH:MM:SSZ, so they sort and compare as
        text), .end None for one with no end; .source is where it came
        from (garmin, app:frms); .summary is the kind's breakdown, a
        dict ({} when the source gave none), the app's own copy.

        `kind` is one of OpenFit's session kinds (SESSION_KINDS in
        metrics.py: sleep, workout, duties_rostered, duties_complete);
        anything else raises ValueError. `since` and `until` are days,
        YYYY-MM-DD (or dates), both included, matched against the day a
        session starts in UTC, as ?from= and ?to= are; None leaves that
        end open. A day that isn't one, or a since after the until,
        raises ValueError.
        """
        if not isinstance(kind, str) or kind not in SESSION_KINDS:
            raise ValueError(f"unknown session kind: {kind!r}")
        if since is not None:
            since = _iso_day(since, "since")
        if until is not None:
            until = _iso_day(until, "until")
        if since is not None and until is not None and since > until:
            raise ValueError(f"since must not be after until: {since} > {until}")
        return [
            Session(s["start"], s["end"], s["source"], s["summary"])
            for s in session_rows(self._conn, kind, since, until)
        ]


# An app's source is this and its id: app:weight. Nothing but the app's
# own WriteCtx writes under it (the webhook refuses the prefix).
APP_SOURCE_PREFIX = "app:"


def app_source(app_id):
    """The source an app's writes are filed under: app:<its id>."""
    return APP_SOURCE_PREFIX + app_id


def custom_metric_key(app_id, key):
    """Where a metric an app invents is stored: app:<its id>:<the key it declares>.

    The app's code says the short key (caffeine_mg); the host stores and
    serves the full one, so two apps declaring the same short key keep
    two metrics, and no app key can meet a canonical one (those have no
    colon). The source it's filed under is still app:<id>.
    """
    return f"{app_source(app_id)}:{key}"


class CustomMetric(NamedTuple):
    """A metric an installed app invents (its manifest's `metrics`, apps.py)."""

    unit: str
    category: str      # one of CATEGORIES
    grain: str         # daily or instant
    range: object      # [low, high], both ends included, or None: any finite number
    app_id: str        # the app that declares it
    key: str           # the short key the app's code says
    label: str


def app_metric_defs(installed):
    """One app's own metrics: {app:<id>:<key>: CustomMetric}, in its manifest's order."""
    return {
        custom_metric_key(installed.id, m["key"]): CustomMetric(
            m["unit"], m["category"], m["grain"], m["range"], installed.id, m["key"], m["label"],
        )
        for m in installed.metrics
    }


def custom_metric_defs(apps_dir):
    """Every installed app's own metrics: {app:<id>:<key>: CustomMetric}.

    The one place they're enumerated. With METRICS (metrics.py, canonical
    only) they make the effective vocabulary the catalog, /api/coverage
    and /api/metric/<key> serve. Folder order, then each manifest's.
    Only apps that load count (scan_apps()): an uninstalled app's metric,
    or a broken one's, drops out - its readings stay, under its source,
    until they're removed (DELETE /api/app-sources/<id>).
    """
    defs = {}
    for installed in scan_apps(apps_dir):
        defs.update(app_metric_defs(installed))
    return defs


def _declared_value(key, declared, raw):
    """`raw` as a reading of an app's own metric, `declared` (a CustomMetric):
    a finite number, never a bool, as a canonical reading is - within the
    range its app declares, if any. Else ValueError."""
    value = finite_number(raw)
    if declared.range is not None:
        low, high = declared.range
        if not low <= value <= high:
            raise ValueError(f"{key} must be between {low} and {high} {declared.unit}, got {raw}")
    return value


def _iso_day(raw, field="date"):
    """`raw` - a date, or YYYY-MM-DD text - as YYYY-MM-DD, or ValueError naming `field`."""
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw.isoformat()
    if isinstance(raw, str):
        try:
            # strptime rather than date.fromisoformat, which also takes
            # '20260101' and week dates: dates are stored as YYYY-MM-DD.
            return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"{field} must be YYYY-MM-DD: {raw!r}")


def _session_instant(raw, field):
    """`raw` - a datetime, or ISO-8601 text - as a session's `field`
    (start or end) is stored: iso_utc()'s YYYY-MM-DDTHH:MM:SSZ, in UTC,
    whole seconds. A naive one is taken to be UTC, as iso_utc() takes
    it. Else ValueError."""
    if isinstance(raw, str):
        try:
            # 3.11+ takes a trailing Z, as iso_utc() writes one.
            raw = datetime.fromisoformat(raw)
        except ValueError:
            pass
    if isinstance(raw, datetime):
        return iso_utc(raw)
    raise ValueError(f"{field} must be a datetime or ISO-8601 timestamp: {raw!r}")


class WriteCtx(Ctx):
    """What handle(ctx, action, payload) is given: a Ctx that can also write.

    One per action, for the app `app` (an InstalledApp). It writes
    readings (write_metric()) and sessions (write_session(), of an
    existing kind). Its source is the app's own, app:<id>, fixed here -
    none of its writers takes one - so whatever the app does, it can only
    create or overwrite its own rows. It notes the days it wrote readings
    to, for the action route to re-derive once the action has succeeded
    (a session is derived into nothing, so it notes none); nothing is
    committed here.
    """

    def __init__(self, conn, app, today=None):
        super().__init__(conn, today=today, app=app)
        self._source = app_source(app.id)
        self._written = set()

    @property
    def source(self):
        """The source this ctx writes under: app:<id>."""
        return self._source

    @property
    def written(self):
        """The days written to so far, oldest first."""
        return sorted(self._written)

    def write_metric(self, metric, value, date=None):
        """File one reading under the app's source: of a canonical metric, or one it invents.

        A canonical metric (METRICS in metrics.py) is filed under its
        key, `value` in its canonical unit and held to the webhook's
        rule: a finite number, never a bool, within the metric's
        plausible range. A metric the app declares in its manifest's
        `metrics` is named by its short key and filed as
        app:<id>:<key>, in the unit it declares, `value` a finite number
        within its declared range, if it gives one. `date` is the day
        it's for, YYYY-MM-DD (or a date), today by default. One reading
        per (date, source, metric): writing a day again replaces it.

        Raises ValueError, writing nothing, for a key that's neither - an
        undeclared one, another app's - a value that won't do, or a date
        that isn't one.
        """
        stored, declared = self._resolve(metric)
        if stored is None:
            raise ValueError(f"unknown metric: {metric!r} - declare it in app.yaml's metrics to write it")
        if declared is None:
            reading, unit = plausible_value(metric, value), METRICS[metric]
        else:
            reading, unit = _declared_value(metric, declared, value), declared.unit
        day = self.today() if date is None else _iso_day(date)
        # write_metric()'s upsert, the unit the vocabulary's - OpenFit's,
        # or the app's.
        write_reading(self._conn, day, self._source, stored, reading, unit)
        self._written.add(day)

    def write_session(self, kind, start, end=None, summary=None):
        """File one session under the app's own source (app:<id>).

        `kind` is one of OpenFit's session kinds (SESSION_KINDS in
        metrics.py: sleep, workout, duties_rostered, duties_complete); an
        app can't invent one yet.
        `start` is when it began, a datetime or ISO-8601 text
        ("2026-01-05T07:30:00+01:00", "...Z"), stored as iso_utc() has
        every session's: UTC, whole seconds, a naive time taken to be
        UTC. `end` is when it finished, the same way, or None for one
        with no end yet; never before `start` (the same second will do).
        `summary` is the session's breakdown - a dict of plain JSON,
        keys belonging to the kind (listed beside SESSION_KINDS) - or
        None. The id is app:<id>:<kind>:<start>, so writing the same
        kind and start again replaces its end and summary.

        A session goes into no day's pick (derived_metrics is metrics
        only), so it notes no day for the route to re-derive; it's read
        back with sessions(kind), or GET /api/sessions/<kind>, beside
        every other source's.

        Raises ValueError, writing nothing, for a kind that isn't one, a
        start that's missing or isn't a time, an end that isn't one or
        is before the start, or a summary that isn't a dict of JSON.
        """
        if not isinstance(kind, str) or kind not in SESSION_KINDS:
            raise ValueError(f"unknown session kind: {kind!r}")
        if start is None:
            # Where a sync's None start is no session at all, an app's is
            # a bug: said, not skipped.
            raise ValueError("a session needs a start")
        start = _session_instant(start, "start")
        if end is not None:
            end = _session_instant(end, "end")
            # iso_utc()'s timestamps sort and compare as plain strings.
            if end < start:
                raise ValueError("a session's end is before its start")
        if summary is not None:
            if not isinstance(summary, dict):
                raise ValueError("a session's summary must be a mapping or None")
            try:
                # NaN and Infinity aren't JSON, as for a render's spec.
                json.dumps(summary, allow_nan=False)
            except (TypeError, ValueError):
                raise ValueError("a session's summary must be JSON") from None
        # write_session()'s upsert; its own kind check stays, a backstop.
        _write_session(self._conn, self._source, kind, start, end, summary)


def render_app(installed, conn, cfg=None, today=None):
    """Run an installed app's render() and return its spec - or an error spec.

    Never raises. A render() that raises, or returns anything but a spec
    (a dict with a string `type`) that is plain JSON, is logged with its
    traceback and becomes {"type": "error", "message": ...}.
    """
    ctx = Ctx(conn, today=today, app=installed)
    try:
        spec = installed.render(ctx, dict(cfg or {}))
    except Exception as e:
        print(f"[apps] {installed.id}: render failed")
        traceback.print_exc()
        return error(f"{type(e).__name__}: {e}")
    if not isinstance(spec, dict) or not isinstance(spec.get("type"), str):
        print(f"[apps] {installed.id}: render returned {type(spec).__name__}, not a spec")
        return error(f"render returned {type(spec).__name__}, not a spec")
    try:
        # NaN and Infinity aren't JSON: the browser would refuse the whole
        # response, so they're caught here, where the app can be named.
        json.dumps(spec, allow_nan=False)
    except (TypeError, ValueError) as e:
        print(f"[apps] {installed.id}: render returned a spec that isn't JSON: {e}")
        return error(f"render returned a spec that isn't JSON: {e}")
    return spec
