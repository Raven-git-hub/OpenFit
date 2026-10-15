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

ctx.metric() is the only data an app gets: one metric's derived points,
the ones GET /api/metric/<metric> serves, as a list of Points (.date,
.value, .source), oldest first - so an app never sees raw per-source
readings. ctx.today() is the server's local date.

The spec vocabulary, built by the ctx's builders (an app may also return
the dicts directly):

    {"type": "number", "value", "label", "caption", "unit"}
        a large value; label, caption and unit may be null
    {"type": "line", "points": [[date, value], ...], "title"}
        a sparkline, oldest first; a null value is a gap, a null date an
        undated point, and title (optional) names it
    {"type": "panel", "children": [spec, ...]}
        its children, stacked in order
    {"type": "empty", "message"}
    {"type": "error", "message"}
        the host's, when a render fails - never an app's to return

render_app() never raises: an app whose render() raises, or returns
something that isn't a spec, gets an error spec in its place, so one
broken app shows as one error tile rather than failing the page.
"""

import json
import math
import traceback
from datetime import date, timedelta
from typing import NamedTuple

from derived import derived_points
from metrics import METRICS


class Point(NamedTuple):
    """One day of a metric, as ctx.metric() lists them."""

    date: str      # YYYY-MM-DD
    value: float   # in the metric's canonical unit (metrics.py)
    source: str    # the source it was picked from


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


def empty(message=None):
    """Nothing to show, and why."""
    return {"type": "empty", "message": message or "No data yet"}


def error(message):
    return {"type": "error", "message": message}


class Ctx:
    """What render(ctx, cfg) is given. One per render call."""

    number = staticmethod(number)
    line = staticmethod(line)
    panel = staticmethod(panel)
    empty = staticmethod(empty)

    def __init__(self, conn, today=None):
        self._conn = conn
        self._today = today or date.today()

    def today(self):
        """The server's local date, as YYYY-MM-DD - the format points' dates are in."""
        return self._today.isoformat()

    def metric(self, key, days=None):
        """One metric's derived points: a list of Point(date, value, source).

        Oldest first, so data[-1] is the latest day; one point per day
        that has a value - a day with none is missing, not a zero - and []
        when there are none. Each value is in the metric's canonical unit
        (METRICS in metrics.py). `days` keeps the trailing days: today and
        the days - 1 before it, as GET /api/metric/<key>?days= does; None
        is the whole history.

        Raises ValueError for a key outside the vocabulary.
        """
        if key not in METRICS:
            raise ValueError(f"unknown metric: {key!r}")
        since = until = None
        if days is not None:
            since, until = trailing_window(_positive_days(days), self._today)
        return [
            Point(p["date"], p["value"], p["source"])
            for p in derived_points(self._conn, key, since, until)
        ]


def render_app(installed, conn, cfg=None, today=None):
    """Run an installed app's render() and return its spec - or an error spec.

    Never raises. A render() that raises, or returns anything but a spec
    (a dict with a string `type`) that is plain JSON, is logged with its
    traceback and becomes {"type": "error", "message": ...}.
    """
    ctx = Ctx(conn, today=today)
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
