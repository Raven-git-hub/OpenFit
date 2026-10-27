"""
The Home tab's layout: its tabs, and on each one which apps' tiles come
in what order, and how big.

A tab is one flow layout: an ordered list of items, each an instance - a
placement of an app, with an id of its own, the size its tile is drawn
at (one of the sizes the app declares, apps.manifest_sizes()) and a
config of its own ({} until there's an editor for it). The same app can
be placed more than once, on different tabs or on one, each instance
sized and configured apart. The order is the layout. The page lays a
tab's tiles into a grid six cells across, in order, left to right, a
tile too wide for what's left of a row starting the next one, and a gap
left at a row's end stays empty. There are no coordinates, so the page
owns the flow (CSS grid does it) and this module only keeps the lists.

Stored as one settings row (no migration), LAYOUT_KEY:

    {"tabs": [{"id": "home", "name": "Home", "mode": "flow",
               "subtitle": "<the line over the tab's tiles>",
               "items": [{"instance_id": "3f9c0a1b2d4e6f70",
                          "app_id": "steps", "size": {"w": 1, "h": 1},
                          "config": {}}, ...]},
              ...],
     "default_tab": "home"}

`mode` is per tab, and holds the seat for a later grid mode - explicit
cell placement, on the same sizes and the same CSS grid - so that's a
second mode, not a migration. Nothing reads it yet: it's always "flow".
`default_tab` is the id of one of the tabs. Instance ids are the
server's to mint (new_instance_id()): a save may leave one out, and gets
a fresh one.

Before tabs, the row was one flow layout - {"mode": "flow", "subtitle",
"items": [{"app_id", "size"}]} - and a row like that reads as one tab,
"home", holding just what that layout showed (_upgraded()). So does no
row, or one that isn't a layout. There's no migration: the next save
stores the new shape.

Apps are installed and removed out of band (the Library, or by hand on
the volume), so the stored instances can be stale. Every read reconciles
them with the apps installed now (reconcile()): an instance whose app is
gone is dropped, a size its app no longer offers falls back to the app's
default, and every installed app has at least one instance - one placed
nowhere gets one, at its default size, at the end of the default tab, in
folder order. There's no installed-but-unplaced app: install one and
it's on Home. A read never rewrites the row: the reconciled layout is
what the next save stores. (So an app uninstalled and installed again
before then - the update path - comes back where it was, config and
all.)
"""

import json
import re
import secrets

from apps import MAX_CELLS, is_app_id, tile_size

LAYOUT_KEY = "home_layout"

FLOW = "flow"

# The longest subtitle a tab keeps: one line over its tiles.
MAX_SUBTITLE = 120

# The longest tab name: its label on the tab strip.
MAX_TAB_NAME = 40

# A tab's id: a short slug - never longer than a name may be, so a tab
# whose stored name won't do can go by its id.
TAB_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,31}\Z")

# An instance's id: a token, unique across every tab.
INSTANCE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")

# The tab a layout from before tabs becomes - and so, Home's first tab.
HOME_TAB_ID = "home"
HOME_TAB_NAME = "Home"

SHAPE = (
    'expected {"tabs": [{"id": "<tab id>", "name": "<text>", "mode": "flow", '
    '"subtitle": "<text>", "items": [{"instance_id": "<id>", "app_id": "<id>", '
    '"size": {"w": <cells>, "h": <cells>}, "config": {}}, ...]}, ...], '
    '"default_tab": "<tab id>"}'
)


class LayoutError(ValueError):
    """A layout that can't be saved, and why."""


def new_instance_id(taken=()):
    """A fresh instance id: a random token, none of `taken`."""
    while True:
        instance_id = secrets.token_hex(8)
        if instance_id not in taken:
            return instance_id


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


def _name_problem(name):
    """Why `name` can't be a tab's name, or None if it can."""
    if not isinstance(name, str) or not name.strip():
        return "name must be text, not blank"
    if len(name) > MAX_TAB_NAME:
        return f"name must be at most {MAX_TAB_NAME} characters"
    return None


def _is_instance_id(value):
    return isinstance(value, str) and INSTANCE_ID.match(value) is not None


def _instance(instance_id, app, size=None, config=None):
    """An instance of `app`: at `size` if the app offers it, else at its
    default - always a size of its own, never one shared with the input."""
    size = tile_size(size)
    if size not in app.sizes:
        size = dict(app.default_size)
    return {
        "instance_id": instance_id,
        "app_id": app.id,
        "size": size,
        "config": config if config is not None else {},
    }


def _upgraded(stored):
    """A layout from before tabs as one tab of instances, for reconcile().

    {"mode", "subtitle", "items": [{app_id, size}]} becomes the tab
    "home", its subtitle, and an instance per item - a fresh id, the
    item's size, config {} - less any repeat of an app already placed
    (one tile an app, then). reconcile() squares it as it would any tab,
    so Home shows just what that layout showed: the same apps, in the
    same order, at the same sizes, then any app it didn't place.
    """
    raw_items = stored.get("items")
    items, apps_placed, ids = [], set(), set()
    for item in raw_items if isinstance(raw_items, list) else []:
        app_id = item.get("app_id") if isinstance(item, dict) else None
        if not isinstance(app_id, str) or app_id in apps_placed:
            continue
        apps_placed.add(app_id)
        instance_id = new_instance_id(ids)
        ids.add(instance_id)
        items.append(
            {"instance_id": instance_id, "app_id": app_id, "size": item.get("size"), "config": {}}
        )
    tab = {
        "id": HOME_TAB_ID,
        "name": HOME_TAB_NAME,
        "subtitle": stored.get("subtitle"),
        "items": items,
    }
    return {"tabs": [tab], "default_tab": HOME_TAB_ID}


def reconcile(stored, apps):
    """`stored` laid over `apps`, the apps installed now (scan_apps()).

    -> {"tabs": [{"id", "name", "mode": "flow", "subtitle", "items":
    [{"instance_id", "app_id", "size", "config"}, ...]}, ...],
    "default_tab"}: the stored tabs in their order, less any that isn't
    a tab or whose id isn't one or is a repeat. On each, its stored
    instances in order, less those whose app isn't installed, or whose
    instance_id isn't one or was already used on any tab (the first
    stands); each at its stored size if its app still offers it, else
    at the app's default, with its config if that's an object, else {}.
    A tab's name that a save wouldn't take reads as its id, a subtitle
    that isn't text as none. There's always a tab - "home", empty, if no
    stored one is left - and default_tab is one of them: the stored one,
    else the first.

    Then every installed app with no instance on any tab gets one - a
    fresh id, its default size, config {} - at the end of the default
    tab, in the order `apps` lists them. So every installed app has at
    least one instance.

    A layout from before tabs - anything with no "tabs" list, so any
    stored row that isn't a layout too - is upgraded first: _upgraded().
    """
    if not isinstance(stored.get("tabs"), list):
        stored = _upgraded(stored)
    installed = {a.id: a for a in apps}

    tabs, seen = [], set()
    for raw in stored["tabs"]:
        tab_id = raw.get("id") if isinstance(raw, dict) else None
        if not isinstance(tab_id, str) or not TAB_ID.match(tab_id):
            continue
        if any(t["id"] == tab_id for t in tabs):
            continue
        raw_items = raw.get("items")
        items = []
        for item in raw_items if isinstance(raw_items, list) else []:
            if not isinstance(item, dict):
                continue
            app_id, instance_id = item.get("app_id"), item.get("instance_id")
            app = installed.get(app_id) if isinstance(app_id, str) else None
            if app is None or not _is_instance_id(instance_id) or instance_id in seen:
                continue
            seen.add(instance_id)
            config = item.get("config")
            items.append(
                _instance(instance_id, app, item.get("size"), config if isinstance(config, dict) else {})
            )
        name, subtitle = raw.get("name"), raw.get("subtitle")
        tabs.append({
            "id": tab_id,
            "name": name if _name_problem(name) is None else tab_id,
            "mode": FLOW,
            "subtitle": subtitle if isinstance(subtitle, str) else "",
            "items": items,
        })
    if not tabs:
        tabs.append(
            {"id": HOME_TAB_ID, "name": HOME_TAB_NAME, "mode": FLOW, "subtitle": "", "items": []}
        )
    default = next((t for t in tabs if t["id"] == stored.get("default_tab")), tabs[0])

    placed = {item["app_id"] for tab in tabs for item in tab["items"]}
    for a in apps:
        if a.id in placed:
            continue
        instance_id = new_instance_id(seen)
        seen.add(instance_id)
        placed.add(a.id)
        default["items"].append(_instance(instance_id, a))
    return {"tabs": tabs, "default_tab": default["id"]}


def load_layout(conn, apps):
    """The saved layout, reconciled with `apps`: see reconcile()."""
    return reconcile(_stored(conn), apps)


def parse_layout(body):
    """A layout to save, from a request body - checked and normalised.

    The save is dumb about the layout itself: it doesn't look at which
    apps are installed or what sizes they offer (a read reconciles
    that), and it never computes a position. It only holds the shape,
    so whatever is stored reads back:

    - tabs, a non-empty list of {id, name, mode, subtitle, items}: id a
      short slug (TAB_ID), each tab's its own; name text, not blank, at
      most MAX_TAB_NAME characters; mode "flow"; subtitle text, at most
      MAX_SUBTITLE characters (empty for none); items a list.
    - each item an instance, {instance_id, app_id, size: {w, h},
      config}: app_id one an app could have (apps.is_app_id(): a
      custom app's id, or a library app's, openfit.frms);
      size whole cells, 1 to MAX_CELLS; config an object, {} if left
      out; instance_id a token (INSTANCE_ID) used once across every
      tab - or left out, and a fresh one is minted. The same app may be
      placed any number of times.
    - default_tab, the id of one of the tabs; any other value, or none,
      is the first tab.

    Anything else in the body, a tab or an item is dropped. It doesn't
    hold a read's promise that every installed app has an instance - it
    doesn't know what's installed - so a layout saved without one gets
    it back on the next read.

    Returns the normalised layout. Raises LayoutError, saying why.
    """
    if not isinstance(body, dict):
        raise LayoutError(SHAPE)
    raw_tabs = body.get("tabs")
    if not isinstance(raw_tabs, list) or not raw_tabs:
        raise LayoutError(
            "tabs must be a non-empty list of {id, name, mode, subtitle, items}: Home has at least one"
        )

    tabs, ids = [], set()
    for raw in raw_tabs:
        if not isinstance(raw, dict):
            raise LayoutError(f"each tab must be {{id, name, mode, subtitle, items}}: {raw!r}")
        tab_id = raw.get("id")
        if not isinstance(tab_id, str) or not TAB_ID.match(tab_id):
            raise LayoutError(
                "each tab needs an id of 1 to 32 lowercase letters, digits and "
                f"dashes, not starting with a dash: {tab_id!r}"
            )
        if any(t["id"] == tab_id for t in tabs):
            raise LayoutError(f"tab {tab_id!r} is listed twice")
        where = f"tab {tab_id!r}"
        problem = _name_problem(raw.get("name"))
        if problem:
            raise LayoutError(f"{where}: {problem}")
        if raw.get("mode") != FLOW:
            raise LayoutError(f"{where}: mode must be 'flow': the one layout mode so far")
        subtitle = raw.get("subtitle")
        if not isinstance(subtitle, str):
            raise LayoutError(f"{where}: subtitle must be text (empty for none)")
        if len(subtitle) > MAX_SUBTITLE:
            raise LayoutError(f"{where}: subtitle must be at most {MAX_SUBTITLE} characters")
        raw_items = raw.get("items")
        if not isinstance(raw_items, list):
            raise LayoutError(
                f"{where}: items must be a list of {{instance_id, app_id, size: {{w, h}}, config}}"
            )
        tabs.append({
            "id": tab_id,
            "name": raw["name"],
            "mode": FLOW,
            "subtitle": subtitle,
            "items": [_parse_item(where, item, ids) for item in raw_items],
        })

    # Minted last, so a fresh id can't take one given further on.
    for tab in tabs:
        for item in tab["items"]:
            if item["instance_id"] is None:
                item["instance_id"] = new_instance_id(ids)
                ids.add(item["instance_id"])
    default = body.get("default_tab")
    if not any(t["id"] == default for t in tabs):
        default = tabs[0]["id"]
    return {"tabs": tabs, "default_tab": default}


def _parse_item(where, item, ids):
    """One instance of a tab's items, checked: see parse_layout(). Its
    instance_id is None if it's to be minted; a given one joins `ids`."""
    app_id = item.get("app_id") if isinstance(item, dict) else None
    if not is_app_id(app_id):
        raise LayoutError(f"{where}: each item needs the app_id of an app: {item!r}")
    size = tile_size(item.get("size"))
    if size is None:
        raise LayoutError(
            f"{where}: {app_id!r}'s size must be {{w, h}}, each a whole number "
            f"of cells from 1 to {MAX_CELLS}"
        )
    instance_id = item.get("instance_id")
    if "instance_id" in item:
        if not _is_instance_id(instance_id):
            raise LayoutError(
                f"{where}: {app_id!r}'s instance_id must be 1 to 64 letters, digits, "
                f"'_' or '-', or left out for a new one: {instance_id!r}"
            )
        if instance_id in ids:
            raise LayoutError(f"{where}: instance_id {instance_id!r} is used twice")
        ids.add(instance_id)
    config = item.get("config", {})
    if not isinstance(config, dict):
        raise LayoutError(f"{where}: {app_id!r}'s config must be an object: {config!r}")
    return {"instance_id": instance_id, "app_id": app_id, "size": size, "config": config}


def save_layout(conn, layout):
    """Store `layout` (parse_layout()'s) as the LAYOUT_KEY row. Commits."""
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (LAYOUT_KEY, json.dumps(layout)),
    )
    conn.commit()
