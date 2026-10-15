# The app host: installed apps and the Home tab

OpenFit ships no apps. The app host runs the ones you install and draws them
as tiles on the **Home** tab. This is its first slice: apps are found on disk,
rendered server-side, and their tiles flow down the page at their default
size. There's no Configure mode, visible grid, drag, resize or saved layout
yet. Those come with the grid design pass, built on top of this without
changing the renderer or the scanner (**Layout**, below).

## An app

An app is a folder in the apps directory: `APPS_DIR`, `/data/apps` by
default, which is on the data volume beside the database. It holds two files:

```
/data/apps/steps/
  app.yaml     the manifest
  app.py       render(ctx, cfg) -> a display spec
```

**`app.yaml`**

```yaml
id: steps                     # optional: defaults to the folder's name
name: Steps                   # optional: defaults to the id
default_size: {w: 2, h: 2}    # optional: tile size in grid cells, 1-12 each; default 2x2
reads: [steps]                # optional: the metric keys it reads (declared, not enforced yet)
config:                       # optional: the app's settings, an ordered list of fields
  - {key: stride_m, label: Stride length (m), type: number, default: 0.75}
```

The id is part of the app's URL, so it's letters, digits, `-` and `_`,
starting with a letter or digit. Each field is optional, but one that's
present must have the right type.

**`config`** is an **ordered list** of fields, not a mapping, because the
order is the order the app's config form will show them in. Each field is a
mapping, `{key, label, type, default}`. `key` is required: a non-empty string,
used once per app. `label`, `type` and `default` are the app's to give; the
host keeps them as written and in order, and doesn't check them yet. A
mapping, or a field without a key, is malformed, so the app is skipped. The
fields aren't passed to `render()` yet: `cfg` is `{}` (below).

**`app.py`** defines `render(ctx, cfg)`, which returns a display spec. This
is the real `steps` app:

```python
def render(ctx, cfg):
    data = ctx.metric("steps", days=7)     # [Point(date, value, source), ...], oldest first
    if not data:
        return ctx.empty("No step data yet")

    latest = data[-1]                      # the latest day
    caption = latest.date
    if latest.date == ctx.today():          # today is a partial day
        caption = f"{latest.date} · so far today"
    if latest.source:
        caption += f" · via {latest.source}"

    return ctx.panel(
        ctx.number(round(latest.value), label="steps", caption=caption),
        ctx.line([(p.date, p.value) for p in data], title="last 7 days"),
    )
```

`cfg` is `{}` for now. Per-instance config, and the UI to edit it, come later;
an app reads a setting with a fallback, such as `cfg.get("stride_m", 0.75)`.

**The reference apps.** The first two real apps, `steps` and `steps-metres`,
are committed verbatim in `tests/fixtures/apps/`, exactly as they're deployed
in `/data/apps`. The test suite runs them against the host on every change
(`test_app_host.py`). They're correct by definition: when one fails, the host
is fixed to match it, never the app.

### Loading: `app/apps.py`

`scan_apps(apps_dir)` lists every app that loads, in folder-name order. The
same scanner backs `/api/apps` and the render route, and the Library will
reuse it.

- A folder with no `app.yaml` isn't an app, so it's passed over without a
  log line. So are dot-folders and loose files.
- A folder with an `app.yaml` that doesn't load is **logged and skipped**:
  `[apps] skipping <folder>: <why>`. That covers bad YAML, a field of the wrong
  type, no `app.py`, an `app.py` that fails to import (its traceback is
  printed too), and a missing or non-callable `render`. One broken app never
  stops the others, and never takes the server down.
- Two folders with the same id: the first by folder name wins, and the other
  is logged and skipped.
- `app.py` is imported from its absolute path with `importlib`. Nothing is
  added to `sys.path` or `sys.modules`.
- The folder is re-scanned on every request, so an app that's added, edited or
  removed shows on the next page load without a restart. A loaded app is kept
  until its `app.yaml` or `app.py` changes (by mtime and size), so a scan stays
  cheap, and a broken app is logged once per change, not on every load.

## The ctx: `app/host.py`

`render()` runs server-side, once per render request, with a fresh `Ctx` over
that request's database connection.

- **`ctx.metric(key, days=None)`** is the app's only data path. It
  **returns a list of points**: `list[Point]`, in ascending date order. So
  `data[-1]` is the latest day, and an empty list `[]` means no data. Each
  `Point` is a named tuple (`host.Point`) with three attributes:
  - `.date`: the day, a `YYYY-MM-DD` string.
  - `.value`: a float, in the metric's canonical unit (`METRICS` in
    `app/metrics.py`).
  - `.source`: the source the value was picked from, such as `garmin`.

  These are the metric's **derived** points, the same ones
  `GET /api/metric/<key>` serves, so an app never sees raw per-source
  readings. There's one point per day that has a value; a day with no value
  is missing from the list, never a zero. `days` keeps the trailing N days:
  today and the N − 1 days before it. `None` returns the whole history. An
  unknown key, or a `days` that isn't a whole number ≥ 1, raises
  `ValueError`, which the app's tile shows as an error.
- **`ctx.today()`** returns today as `YYYY-MM-DD`, the format points' dates
  use. It's the server's local date until a `base_tz` setting lands.
- **The spec builders**, also importable from `host`, are described in the
  next section.

## The spec vocabulary

A spec is plain JSON. An app may build it with the ctx's builders or return
the dicts itself.

| Builder | Spec | Drawn as |
|---|---|---|
| `ctx.number(value, label=None, caption=None, unit=None)` | `{"type": "number", "value", "label", "caption", "unit"}` | A large value in tabular figures, `unit` small beside it, `label` beneath, and `caption` smaller and muted. A null value reads `—`, and a null label, caption or unit is left out. A number is shown with thousands separators; a string is shown as given. The caption is cut to two lines, or one in a short tile (a 2×2 on a phone), with the full text on hover. |
| `ctx.line(points, title=None)` | `{"type": "line", "points": [[date, value], ...], "title"}` | A sparkline with its title above it. See [the line builder](#the-line-builder), below. |
| `ctx.panel(*children)` or `ctx.panel([children])` | `{"type": "panel", "children": [...]}` | Its children stacked in order. A number keeps its height and a line takes the rest. |
| `ctx.empty(message=None)` | `{"type": "empty", "message"}` | Muted, centred text (default "No data yet"). |
| (the host's) | `{"type": "error", "message"}` | Muted, centred: "Couldn't render <app name>" over the message. |

### The line builder

```python
ctx.line(points, title=None)
# -> {"type": "line", "points": [[date, value], ...], "title": title}
```

- **`points`** are oldest first. Each one can be any of these:
  - a `(date, value)` pair, as the apps build them:
    `[(p.date, p.value) for p in data]`;
  - one of `ctx.metric()`'s `Point`s, passed as it comes (its `source` is
    dropped);
  - a `{date, value}` dict;
  - a bare number.

  Each becomes a `[date, value]` pair in the spec. The date is a `YYYY-MM-DD`
  string (a `date` is converted to one), or null for a bare number or a
  missing date. A `None` or non-finite value is a gap, written `null`. Any
  other point (a bool, text, a pair that isn't two items, a non-date date)
  raises `TypeError`.
- **`title`** is optional text, drawn small above the line, such as
  `"last 7 days"`. With no title, the spec's `"title"` is `null` and nothing
  is drawn above the line. A title that isn't text raises `TypeError`.

**How it's drawn.** The line fills the space under its title, with its
points evenly spaced (by index, not by date) and a dot on the latest point.
Nothing is joined across a gap or drawn as zero. A null value breaks the
line, and so do two dated points with a day missing between them. A point
with a gap on both sides is drawn as a dot. No points shows "No data", and a
single point is a dot. The page also draws a hand-built spec whose points
are `{date, value}` objects or bare numbers.

**Errors never fail the page.** `render_app()` catches anything `render()`
raises and logs it with the traceback (`[apps] <id>: render failed`). It also
catches a return value that isn't a spec (a dict with a string `type`) or
isn't plain JSON, such as NaN or a set. Any of these come back as an error
spec with a 200, so the page shows one error tile. A spec type the page
doesn't know is drawn as an error tile too.

## Routes

- `GET /api/apps` → `[{id, name, default_size: {w, h}}]`, the apps that load,
  in folder order.
- `GET /api/app/<id>/render` → the app's spec, rendered now with `cfg` `{}`.
  It's always a 200 for an installed app, with an error spec if the render
  fails. An id with no app that loads gets `404 {"ok": false, "error": "unknown
  app: <id>"}`.

The access contract's reads also take **`?days=N`**: the trailing N days, as
`ctx.metric()` counts them. It can't be combined with `from`/`to`, and a value
that isn't a whole number from 1 to 999999999 is a 400. It narrows every
contract read (`/api/metric`, `/api/coverage`, `/api/sessions`,
`/api/series`) the same way, because they share one range parser.

## The Home tab

On load, the page calls `GET /api/apps`, then each app's render in parallel,
and draws the tiles. A render that can't be fetched becomes one error tile. A
**Sync now** or a source removal re-reads Home along with the data card, so
the tiles follow the data. If `/api/apps` itself fails, the page says so and
offers a Retry. With nothing installed, the page says where apps go.

Tiles are **chromeless**: the app's spec is the whole face, on the console's
card surface (`--surface`, `--border`, `--radius`, `--shadow`). A number sizes
its type to the tile, so the same app reads well at 1×1 or 4×2.

### Layout, and the seam for the grid pass

The page keeps three parts apart:

1. `loadHome()` turns the apps and their specs into a list of
   `{app_id, name, spec, w, h}`.
2. `layoutTiles(tiles)` gives each tile its cell, as `x`/`y` (0-based, in
   cells), or `null` to flow it. In this slice it's `autoLayout`, which leaves
   every tile unplaced.
3. `paintTiles(box, tiles)` draws any such list on an invisible CSS grid of
   `--tile-cell` squares (110px, `--tile-gap` 12px between, both in the token
   block). A tile spans `w` × `h` cells. With `x`/`y` it starts at that cell;
   without, the grid flows it into the next free one, left to right, wrapping.
   On a phone the cell shrinks to a quarter of the width, so two 2×2 tiles
   still sit side by side.

The grid pass (Configure, a visible grid, drag/resize, collision, saved
positions in `settings`) swaps `layoutTiles` for a function that reads the
saved layout. `paintTiles`, `renderSpec` and the scanner don't change.

## Not yet

- **Configure mode**, the visible grid, drag/resize, collision and a saved
  layout: the grid pass above.
- **The Library:** browsing and installing apps. It reuses `scan_apps()` and
  `/api/apps`.
- **Per-instance config** and the UI to edit it (`cfg` is `{}`), **metric
  write-back**, and a custom `app.js` for an app that draws its own face.
- **Enforcing `reads`:** it's recorded, but `ctx.metric()` serves any key in
  the vocabulary.
- **A sandbox:** an app is trusted Python running in the server's process.
  The sandboxed host (no network beyond the contract) is Phase 4 of
  `ROADMAP.md`.
