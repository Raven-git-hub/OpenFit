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
config: {}                    # optional: the app's settings (not yet passed to render)
```

The id is part of the app's URL, so it's letters, digits, `-` and `_`,
starting with a letter or digit. Each field is optional, but one that's
present must have the right type.

**`app.py`** defines `render(ctx, cfg)`, which returns a display spec:

```python
def render(ctx, cfg):
    week = ctx.metric("steps", days=7)
    points = week["points"]
    if not points:
        return ctx.empty("No steps yet")
    latest = points[-1]
    caption = "so far today" if latest["date"] == ctx.today() else latest["date"]
    return ctx.panel(
        ctx.number(latest["value"], label="steps", caption=caption),
        ctx.line(points),
    )
```

`cfg` is `{}` for now. Per-instance config, and the UI to edit it, come later.

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

- **`ctx.metric(key, days=None)`** is the app's only data path. It returns
  `{"metric", "unit", "points": [{"date", "value", "source"}]}`: one
  metric's **derived** points, the same as `GET /api/metric/<key>` serves
  them. They're oldest first, with one point per day that has a value. A day
  with no value is missing from the list, never a zero. `days` keeps the
  trailing N days: today and the N − 1 days before it. `None` returns the
  whole history. `series` is the same list as `points`. It's kept because
  the dev harness the first apps were written against used that name; new
  apps should read `points`. An unknown key, or a `days` that isn't a whole
  number ≥ 1, raises `ValueError`, which the app's tile shows as an error.
- **`ctx.today()`** returns today as `YYYY-MM-DD`, the format points' dates
  use. It's the server's local date until a `base_tz` setting lands.
- **The spec builders**, also importable from `host`, are described in the
  next section.

## The spec vocabulary

A spec is plain JSON. An app may build it with the ctx's builders or return
the dicts itself.

| Builder | Spec | Drawn as |
|---|---|---|
| `ctx.number(value, label=None, caption=None, unit=None)` | `{"type": "number", "value", "label", "caption", "unit"}` | A large value in tabular figures, `unit` small beside it, `label` beneath, and `caption` smaller and muted. A null value reads `—`, and a null label, caption or unit is left out. A number is shown with thousands separators; a string is shown as given. |
| `ctx.line(points)` | `{"type": "line", "points": [v \| {"date", "value"}, ...]}` | A sparkline filling its box, evenly spaced (by index, not date), with a dot on the latest point. `points` can be numbers or `ctx.metric()`'s points as they come (their `source` is dropped). A `None` or non-finite value is a gap. The line is also broken between two dated points with a day missing between them: nothing is joined across a gap or drawn as zero. A point with a gap on both sides gets a dot. No points shows "No data", and a single point is a dot. |
| `ctx.panel(*children)` or `ctx.panel([children])` | `{"type": "panel", "children": [...]}` | Its children stacked in order. A number keeps its height and a line takes the rest. |
| `ctx.empty(message=None)` | `{"type": "empty", "message"}` | Muted, centred text (default "No data yet"). |
| (the host's) | `{"type": "error", "message"}` | Muted, centred: "Couldn't render <app name>" over the message. |

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
