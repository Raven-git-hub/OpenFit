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
readings. ctx.today() is the server's local date. The ctx is made for
the app (its InstalledApp), so it reads OpenFit's metrics and the ones
the app invents - declared in its manifest's `metrics`, said by their
short key, stored as app:<id>:<key> (custom_metric_key()).

An app may also write, from an optional handle(ctx, action, payload)
beside render() - what a tile's input element sends when its Save is
pressed (POST /api/app/<id>/action):

    def handle(ctx, action, payload):
        if action == "log":
            ctx.write_metric("weight_kg", float(payload["weight_kg"]))

handle() is given a WriteCtx: the same reads, plus ctx.write_metric().
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
and a ctx knows only its own app's. That's the whole of the scoping: the
app is trusted code, as its render() is, not sandboxed.

The spec vocabulary, built by the ctx's builders (an app may also return
the dicts directly):

    {"type": "number", "value", "label", "caption", "unit"}
        a large value; label, caption and unit may be null
    {"type": "line", "points": [[date, value], ...], "title"}
        a sparkline, oldest first; a null value is a gap, a null date an
        undated point, and title (optional) names it
    {"type": "panel", "children": [spec, ...]}
        its children, stacked in order
    {"type": "input", "key", "action", "label", "kind", "unit"}
        a field and a Save button: Save sends `action` to the app's
        handle(), the payload {key: what was typed} - a number for kind
        "number", text for "text" - then the tile is drawn afresh;
        label and unit may be null
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
from datetime import date, datetime, timedelta
from typing import NamedTuple

from apps import scan_apps
from derived import derived_points
from metrics import METRICS, finite_number, plausible_value
from plugins.base import write_reading


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


# What an input element's field takes: a number, or text.
INPUT_KINDS = ("number", "text")


def _text_or_none(value, what):
    if value is not None and not isinstance(value, str):
        raise TypeError(f"an input's {what} must be text: {value!r}")
    return value


def input(key, action, label=None, kind="number", unit=None):
    """A field and a Save button that sends `action` to the app's handle().

    The payload is {key: what was typed}: a number for kind "number",
    text for "text". `label` names the field, `unit` sits beside it;
    either may be None. Once handle() has run, the tile is drawn afresh
    from render().
    """
    for what, value in (("key", key), ("action", action)):
        if not isinstance(value, str) or not value:
            raise TypeError(f"an input's {what} must be non-empty text: {value!r}")
    if kind not in INPUT_KINDS:
        raise ValueError(f"an input's kind is one of {', '.join(INPUT_KINDS)}: {kind!r}")
    return {
        "type": "input",
        "key": key,
        "action": action,
        "label": _text_or_none(label, "label"),
        "kind": kind,
        "unit": _text_or_none(unit, "unit"),
    }


def empty(message=None):
    """Nothing to show, and why."""
    return {"type": "empty", "message": message or "No data yet"}


def error(message):
    return {"type": "error", "message": message}


class Ctx:
    """What render(ctx, cfg) is given. One per render call.

    Made for one app, `app` (an InstalledApp): it reads OpenFit's metrics
    and the ones that app declares. A ctx for no app reads only the
    former.
    """

    number = staticmethod(number)
    line = staticmethod(line)
    panel = staticmethod(panel)
    input = staticmethod(input)
    empty = staticmethod(empty)

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


def _iso_day(raw):
    """`raw` - a date, or YYYY-MM-DD text - as YYYY-MM-DD, or ValueError."""
    if isinstance(raw, date) and not isinstance(raw, datetime):
        return raw.isoformat()
    if isinstance(raw, str):
        try:
            # strptime rather than date.fromisoformat, which also takes
            # '20260101' and week dates: dates are stored as YYYY-MM-DD.
            return datetime.strptime(raw, "%Y-%m-%d").date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"date must be YYYY-MM-DD: {raw!r}")


class WriteCtx(Ctx):
    """What handle(ctx, action, payload) is given: a Ctx that can also write.

    One per action, for the app `app` (an InstalledApp). Its source is
    the app's own, app:<id>, fixed here - none of its writers takes one -
    so whatever the app does, it can only create or overwrite its own
    rows. It notes the days it wrote to, for the action route to
    re-derive once the action has succeeded; nothing is committed here.
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
