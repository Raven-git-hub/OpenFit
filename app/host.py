"""
The app host: what an installed app's render() is given, and how it's run.

An app (apps.py) is a manifest plus a render(ctx, cfg) that returns a
display spec - plain JSON the Home tab draws (the renderer in
index.html). render() runs here, server-side, once per request, with a
fresh Ctx built over that request's connection:

    def render(ctx, cfg):
        steps = ctx.metric("steps", days=7)
        values = [p["value"] for p in steps["points"]]
        if not values:
            return ctx.empty("No steps yet")
        return ctx.panel(ctx.number(values[-1], label="steps"), ctx.line(steps["points"]))

ctx.metric() is the only data an app gets: one metric's derived points,
read as GET /api/metric/<metric> serves them, so an app never sees raw
per-source readings. ctx.today() is the server's local date.

The spec vocabulary, built by the ctx's builders (an app may also return
the dicts directly):

    {"type": "number", "value", "label", "caption", "unit"}
        a large value; label, caption and unit may be null
    {"type": "line", "points": [value | {"date", "value"}, ...]}
        a sparkline; a null value is a gap
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

from derived import derived_points
from metrics import METRICS


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


def _line_point(raw):
    if isinstance(raw, dict):
        if "value" not in raw:
            raise TypeError(f"a line point needs a value: {raw!r}")
        point = {"value": _line_value(raw["value"])}
        if raw.get("date") is not None:
            point["date"] = str(raw["date"])
        return point
    return _line_value(raw)


def line(points):
    """A sparkline over `points`: numbers, or {date, value} dicts - ctx.metric()'s
    points as they come. A None value (or a non-finite one) is a gap, never a zero;
    a dict's other keys (its source) are dropped."""
    return {"type": "line", "points": [_line_point(p) for p in points]}


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
        """One metric's derived points: {metric, unit, points: [{date, value, source}]}.

        Oldest first, one point per day that has a value - a day with none
        is missing, not a zero. `days` keeps the trailing days: today and
        the days - 1 before it, as GET /api/metric/<key>?days= does; None
        is the whole history. `series` is the same list as `points`, kept
        for apps written against the dev harness, which named it so.

        Raises ValueError for a key outside the vocabulary.
        """
        if key not in METRICS:
            raise ValueError(f"unknown metric: {key!r}")
        since = until = None
        if days is not None:
            since, until = trailing_window(_positive_days(days), self._today)
        points = derived_points(self._conn, key, since, until)
        return {"metric": key, "unit": METRICS[key], "points": points, "series": points}


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
