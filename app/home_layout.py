"""
The Home tab's layout: which apps' tiles come in what order, and how big.

One mode so far, flow: an ordered list of items, each an app and the
size its tile is drawn at - one of the sizes the app declares
(apps.manifest_sizes()). The order is the layout. The page lays the
tiles into a grid six cells across, in order, left to right, a tile too
wide for what's left of a row starting the next one, and a gap left at
a row's end stays empty. There are no coordinates, so the page owns the
flow (CSS grid does it) and this module only keeps the list.

Stored as one settings row (no migration), LAYOUT_KEY:

    {"mode": "flow",
     "subtitle": "<the line under the tab strip>",
     "items": [{"app_id": "steps", "size": {"w": 1, "h": 1}}, ...]}

`mode` holds the seat for a later grid mode - explicit cell placement,
on the same sizes and the same CSS grid - so that's a second mode, not a
migration. Nothing reads it yet: it's always "flow".

Apps are installed and removed out of band (the Library, or by hand on
the volume), so the stored items can be stale. Every read reconciles
them with the apps installed now (reconcile()): an item whose app is
gone is dropped, a size its app no longer offers falls back to the app's
default, and an installed app with no item is added at the end at its
default size, in folder order. A read never rewrites the row: the
reconciled layout is what the next save stores. (So an app uninstalled
and installed again before then - the update path - comes back where it
was.)
"""

import json

from apps import APP_ID, MAX_CELLS, tile_size

LAYOUT_KEY = "home_layout"

FLOW = "flow"

# The longest subtitle a layout keeps: one line under the tab strip.
MAX_SUBTITLE = 120


class LayoutError(ValueError):
    """A layout that can't be saved, and why."""


def _stored(conn):
    """The stored layout as a dict: {} if there's none, or it isn't one.

    The row can also be written through /api/settings, so anything
    there is tolerated here - read as no layout, never raised.
    """
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (LAYOUT_KEY,)
    ).fetchone()
    try:
        stored = json.loads(row[0]) if row and row[0] else {}
    except ValueError:
        return {}
    return stored if isinstance(stored, dict) else {}


def reconcile(stored, apps):
    """`stored` laid over `apps`, the apps installed now (scan_apps()).

    -> {"mode": "flow", "subtitle", "items": [{"app_id", "size"}, ...]}:
    the stored items in their order, less those whose app isn't
    installed (and any repeat of one already placed, or one that isn't
    an item at all); each at its stored size if its app still offers
    it, else at the app's default. Then every installed app the items
    don't place, at its default size, in the order `apps` lists them.
    """
    installed = {a.id: a for a in apps}
    raw_items = stored.get("items")
    items, placed = [], set()
    for item in raw_items if isinstance(raw_items, list) else []:
        app_id = item.get("app_id") if isinstance(item, dict) else None
        app = installed.get(app_id) if isinstance(app_id, str) else None
        if app is None or app.id in placed:
            continue
        size = tile_size(item.get("size"))
        if size not in app.sizes:
            size = dict(app.default_size)
        items.append({"app_id": app.id, "size": size})
        placed.add(app.id)
    items += [
        {"app_id": a.id, "size": dict(a.default_size)}
        for a in apps
        if a.id not in placed
    ]
    subtitle = stored.get("subtitle")
    return {
        "mode": FLOW,
        "subtitle": subtitle if isinstance(subtitle, str) else "",
        "items": items,
    }


def load_layout(conn, apps):
    """The saved layout, reconciled with `apps`: see reconcile()."""
    return reconcile(_stored(conn), apps)


def parse_layout(body):
    """A layout to save, from a request body - checked and normalised.

    The save is dumb about the layout itself: it doesn't look at which
    apps are installed or what sizes they offer (a read reconciles
    that), and it never computes a position. It only holds the shape,
    so whatever is stored reads back: mode "flow"; subtitle text, at
    most MAX_SUBTITLE characters (empty for none); items a list of
    {app_id, size: {w, h}}, each app_id one an app could have (the
    scanner's APP_ID) and listed once, each size whole cells, 1 to
    MAX_CELLS. Anything else in the body or an item is dropped.

    Returns the normalised layout. Raises LayoutError, saying why.
    """
    if not isinstance(body, dict):
        raise LayoutError(
            'expected {"mode": "flow", "subtitle": "<text>", '
            '"items": [{"app_id": "<id>", "size": {"w": <cells>, "h": <cells>}}, ...]}'
        )
    if body.get("mode") != FLOW:
        raise LayoutError("mode must be 'flow': the one layout mode so far")
    subtitle = body.get("subtitle")
    if not isinstance(subtitle, str):
        raise LayoutError("subtitle must be text (empty for none)")
    if len(subtitle) > MAX_SUBTITLE:
        raise LayoutError(f"subtitle must be at most {MAX_SUBTITLE} characters")
    raw_items = body.get("items")
    if not isinstance(raw_items, list):
        raise LayoutError("items must be a list of {app_id, size: {w, h}}")

    items, seen = [], set()
    for item in raw_items:
        app_id = item.get("app_id") if isinstance(item, dict) else None
        if not isinstance(app_id, str) or not APP_ID.match(app_id):
            raise LayoutError(f"each item needs the app_id of an app: {item!r}")
        if app_id in seen:
            raise LayoutError(f"{app_id!r} is listed twice")
        size = tile_size(item.get("size"))
        if size is None:
            raise LayoutError(
                f"{app_id!r}'s size must be {{w, h}}, each a whole number "
                f"of cells from 1 to {MAX_CELLS}"
            )
        seen.add(app_id)
        items.append({"app_id": app_id, "size": size})
    return {"mode": FLOW, "subtitle": subtitle, "items": items}


def save_layout(conn, layout):
    """Store `layout` (parse_layout()'s) as the LAYOUT_KEY row. Commits."""
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (LAYOUT_KEY, json.dumps(layout)),
    )
    conn.commit()
