# The app host: installed apps and the Home tab

OpenFit ships no apps. The app host runs the ones you install and draws them
as tiles on the **Home** tab, and an app can write too: it's a source like any
device, filed under its own id ([Writing: apps as sources](#writing-apps-as-sources),
below). Apps are found on disk and rendered
server-side, and their tiles sit on a grid six square cells across, in the
order and at the sizes the owner chose in **Configure**, each size one the
app declares. That layout is flow: an order and a size per tile, no
coordinates. Free cell placement (the grid) is a later, second layout mode
on the same sizes ([The Home tab](#the-home-tab), below).

## An app

An app is a folder in the apps directory: `APPS_DIR`, `/data/apps` by
default, which is on the data volume beside the database. It holds its
manifest and, usually, its engine:

```
/data/apps/steps/
  app.yaml     the manifest (required)
  app.py       render(ctx, cfg) -> a display spec (optional), and
               handle(ctx, action, payload) -> its writes (optional)
```

`app.py` is optional. An app with a manifest alone loads and is listed like
any other, and its tile says it has nothing to draw: its render is
`{"type": "empty", "message": "Nothing to draw yet: this app has no app.py"}`.
Any other files in the folder, such as reference data or helper modules, are
the app's own; the host doesn't read them.

An app gets into the folder by hand (copy it onto the volume) or from the
browser, with **＋ Add Application** ([Installing an app](#installing-an-app),
below). It leaves the same two ways: delete its folder, or **Uninstall** in its
row of the Applications list ([An app's row](#an-apps-row-reads-files-refresh-uninstall),
below). In between, **Update** in the same row replaces its files in one step,
its id and data kept ([Updating an app](#updating-an-app-put-apiappsid),
below).

**`app.yaml`**

```yaml
id: steps                     # optional: defaults to the folder's name
name: Steps                   # optional: defaults to the id
sizes: [{w: 1, h: 1}, {w: 2, h: 2}]   # REQUIRED: the tile sizes, in cells; the first is the default
reads: [steps]                # optional: the metric keys it reads (declared, not enforced yet)
config:                       # optional: the app's settings, an ordered list of fields
  - {key: stride_m, label: Stride length (m), type: number, default: 0.75}
metrics:                      # optional: the metrics the app invents, an ordered list
  - {key: caffeine_mg, label: Caffeine, unit: mg, category: "Nutrition & hydration", grain: daily, range: [0, 2000]}
```

The id is part of the app's URL, so it's letters, digits, `-` and `_`,
starting with a letter or digit. Every field but the size is optional, and
one that's present must have the right type.

**A library app's id** has a second shape: `<source>.<name>`, such as
`openfit.frms` (`LIBRARY_APP_ID`: the source a lowercase letter then
lowercase letters and digits, one dot, then a name in an install's id form,
`INSTALL_ID`). The library assigns the source, never the app: a library app's
own `app.yaml` keeps its bare id (`id: frms`), and the source is added at
install ([Installing from a library](#installing-from-a-library-install_library_app),
below), carried by the folder's name alone (`/data/apps/openfit.frms/`). So
the installed files are the library's own, byte for byte, and a dot in an id
always means installed from a library: nothing else can make one. Such an app
is read-only here (no Update; Duplicate makes a copy of one's own). Anything
that checks an app's id takes either shape (`is_app_id()`); `APP_ID` itself
is unchanged, since it's also the rule for the keys of the metrics an app
invents, which never gain a dot.

**`sizes`** is required: the tile sizes the app can be drawn at, as cell
rectangles on Home's grid, which is six cells across. It's a non-empty list
of `{w, h}`, each a whole number from 1 to 6 (`MAX_CELLS`), none listed twice.
The first is the default, the size a tile is first placed at; the owner picks
among the rest in Configure. A fixed-size app lists one size, and gets no
picker. The author declares the sizes because only the author knows the
shapes the spec reads well at: the host never makes one up.

- **`default_size: {w, h}`**, the older form, still counts: alone, it's a
  list of one. An app may give both (one written for hosts either side of
  `sizes`) when `default_size` is the first of `sizes`.
- **No size, or one that won't do, and the app doesn't load**: the scanner
  skips it, saying why (`[apps] skipping <folder>: no tile size: ...`), and
  an install refuses it with the same message. That's `manifest_sizes()` in
  `app/apps.py`.
- **YAML needs a space after each colon**: `{w:1,h:1}` parses as the keys
  `'w:1'` and `'h:1'`, so the error says to write `{w: 1, h: 1}`.

**`config`** is an **ordered list** of fields, not a mapping, because the
order is the order the tile's settings form shows them in. Each field is a
mapping, `{key, label, type, default}`. `key` is required: a non-empty string,
used once per app. `label`, `type` and `default` are the app's to give; the
host keeps them as written and in order, and doesn't check them. A mapping,
or a field without a key, is malformed, so the app is skipped. So is a field
that isn't plain JSON (a YAML date, a `.nan`, a `!!binary`): `GET /api/apps`
sends the fields to the page as written, so one that isn't would fail the
whole listing. Quote a date. The fields are the schema; the values are each
placement's own (its instance's `config`, set from the tile's gear in
Configure), and reach `render()` as `cfg` (below).

**`metrics`** is an **ordered list** of the metrics the app **invents**: a key
OpenFit's vocabulary doesn't have. Declare what you invent, write what exists: a
built-in metric the app writes (`calories_in`) isn't listed here. Each is a
mapping of exactly these fields (`_metrics()` in `app/apps.py`):

- **`key`**: the short key the app's code says, shaped as an app id (letters,
  digits, `-` and `_`, starting with a letter or digit), used once per app, and
  never a key OpenFit already has (a metric, a series metric or a session kind,
  so a custom key can't shadow a built-in one). It's stored as
  `app:<id>:<key>`, so two apps' keys never meet.
- **`label`**: its name in the data console. Text, not blank.
- **`unit`**: what its readings are in, the app's own. Text, not blank.
- **`category`**: one of the nine categories (`CATEGORIES` in
  `app/metrics.py`), as written; `Other` is the catch-all.
- **`grain`**: `daily` or `instant`. An app's own `session` or `series` isn't
  open yet.
- **`range`** (optional): `[low, high]`, two finite numbers, low first, both
  ends included. A write outside it is refused; with none (or `null`), any
  finite number goes.

Anything else in an entry (a misspelt `ragne`), a block that isn't a list, or
any rule above broken, and the app doesn't load: logged and skipped, as a bad
`config` is. See [Writing](#writing-apps-as-sources) for how they're read and
written, and [Routes](#routes) for where they're served.

**`app.py`**, when there is one, defines `render(ctx, cfg)`, which returns a
display spec. This is the real `steps` app:

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

`cfg` is the tile's settings: its instance's `config`, `{key: value}`, as the
owner set them from the tile's gear in Configure (see
[Configure](#configure)), and `{}` until they set any. The host passes it as
given and checks nothing in it: the app is trusted and owns its fallbacks, so
it reads a setting defensively, such as `cfg.get("stride_m", 0.75)` (the real
`steps-metres` app does just that), and copes with a value of a type it didn't
expect.

**The library.** `library/`, at the repo's top level, is the official app
library: the apps OpenFit publishes, which the host's tests also run against as
real apps; outside the image build context, never copied into the user system.
The compose file builds `./app`, so an app reaches `/data/apps` only by being
installed. It's a folder an app, each named for its id and each installing
through `install_app()` from its files as they stand (`tests/test_library.py`),
which is what Browse will do. How it's served and updated is
[distribution.md](distribution.md)'s.

**The reference apps.** The first two real apps, `steps` and `steps-metres`,
are committed in `library/` as they're deployed in `/data/apps`.
The test suite runs them against the host on every change
(`test_app_host.py`). They're correct by definition: when one fails, the host
is fixed to match it, never the app. Their `app.py` is verbatim. Their
`app.yaml` declares `sizes: [{ w: 1, h: 1 }]`, the canonical form, where the
deployed copies still have `default_size: { w: 2, h: 2 }`: the tests check
that form keeps loading too, and moving a deployed app to one cell is a
one-line manifest edit and a reinstall. The third, `weight`, is the first app
that writes, and the write path's end-to-end test (`test_app_actions.py`; see
[The weight app](#the-weight-app), below). The fourth, `caffeine`, is the first
to invent a metric (`test_app_custom_metrics.py`; see
[The caffeine app](#the-caffeine-app), below).

### Loading: `app/apps.py`

`scan_apps(apps_dir)` lists every app that loads, in folder-name order. The
same scanner backs `/api/apps` and the render route, and the Library will
reuse it.

- A folder with no `app.yaml` isn't an app, so it's passed over without a
  log line. So are dot-folders and loose files.
- A folder with an `app.yaml` that doesn't load is **logged and skipped**:
  `[apps] skipping <folder>: <why>`. That covers bad YAML, a field of the wrong
  type, no size (or one outside 1-6), a `metrics` block that won't do (above),
  an `app.py` that fails to import (its
  traceback is printed too), a missing or non-callable `render`, and a
  `handle` that's there but not callable. One broken app never stops the others, and
  never takes the server down. A folder with no `app.py` isn't broken: it
  loads, with nothing to draw (above).
- Two folders with the same id: the first by folder name wins, and the other
  is logged and skipped.
- A folder named as a library app's id (`openfit.frms`) is that app: its id is
  the folder's name, and its own `app.yaml` must say the bare name (`id:
  frms`) or no id at all. Any other (`id: other`, or the full `openfit.frms`)
  and it's skipped: `a library app's own id is its bare name: expected
  'frms', got 'other'`. Any other folder's id is as above, the manifest's or
  the folder's, held to `APP_ID`. Staging folders start with a dot, so never
  match.
- `app.py` is imported once, from its absolute path, with `importlib`, and
  both `render` and the optional `handle` taken from it (`InstalledApp.render`,
  `.handle`; `None` for an app without one). Nothing is added to `sys.path` or
  `sys.modules`.
- The folder is re-scanned on every request, so an app that's added, edited or
  removed shows on the next page load without a restart. A loaded app is kept
  until its `app.yaml` or `app.py` changes (by mtime and size), so a scan stays
  cheap, and a broken app is logged once per change, not on every load.

## The ctx: `app/host.py`

`render()` runs server-side, once per render request, with a fresh `Ctx` over
that request's database connection. The ctx is the app's only data path, and
it has two reads: `ctx.metric()` for a metric's daily values and
`ctx.sessions()` for a session kind's intervals. Each gives the same answer
as its access-contract route (`GET /api/metric/<key>`, `GET
/api/sessions/<kind>`), because each route and its read call one shared
function in `app/derived.py` (`derived_points()`, `session_rows()`).

- **`ctx.metric(key, days=None)`** reads one metric. It
  **returns a list of points**: `list[Point]`, in ascending date order. So
  `data[-1]` is the latest day, and an empty list `[]` means no data. Each
  `Point` is a named tuple (`host.Point`) with three attributes:
  - `.date`: the day, a `YYYY-MM-DD` string.
  - `.value`: a float, in the metric's canonical unit (`METRICS` in
    `app/metrics.py`), or for a metric the app invents, its declared unit.
  - `.source`: the source the value was picked from, such as `garmin`.

  `key` is a canonical metric, or one the app declares in its manifest's
  `metrics`, by its short key: the ctx is made for the app (`Ctx(conn,
  today=None, app=None)`, the `InstalledApp`), so `ctx.metric("caffeine_mg")`
  reads `app:<id>:caffeine_mg`. Another app's metric, by either key, isn't
  readable.

  These are the metric's **derived** points, the same ones
  `GET /api/metric/<key>` serves, so an app never sees raw per-source
  readings. There's one point per day that has a value; a day with no value
  is missing from the list, never a zero. `days` keeps the trailing N days:
  today and the N − 1 days before it. `None` returns the whole history. A
  key that's neither canonical nor the app's own, or a `days` that isn't a
  whole number ≥ 1, raises `ValueError`, which the app's tile shows as an
  error.
- **`ctx.sessions(kind, since=None, until=None)`** reads one session kind:
  intervals such as a night's sleep, a workout or a roster's duties. It
  **returns a list of sessions**, `list[Session]`, or `[]` when there are
  none. Each `Session` is a named tuple (`host.Session`) with the four fields
  of `GET /api/sessions/<kind>`, in the same order:
  - `.start`: when it began, as stored: ISO 8601 UTC text,
    `YYYY-MM-DDTHH:MM:SSZ`, so starts sort and compare as plain strings.
  - `.end`: when it finished, in the same form, or `None` if it has no end.
  - `.source`: the source it was filed under, such as `garmin`,
    `google_health` or `app:frms`.
  - `.summary`: the kind's breakdown, a `dict` parsed from the stored JSON.
    Its keys belong to the kind and are listed beside `SESSION_KINDS`. It's
    `{}` when the source gave no summary. Each call returns a fresh copy, so
    the app can change it without affecting anything stored.

  It returns **every source's** sessions, exactly as `GET
  /api/sessions/<kind>` lists them, because both read `session_rows()`. This
  is a priority-ordered passthrough, not a pick: nothing is chosen, filtered
  or merged, so a night that two devices both tracked appears twice, once per
  source. Sources are ordered the way the derived pick ranks them: the kind's
  primary in the source roles if one is set, then `DEFAULT_PRIORITY`
  (`garmin`, then `google_health`), then every other source by name (an app's
  `app:<id>` among them). Within a source, sessions are in start order. So
  the list as a whole isn't in time order. For a single timeline, re-sort it
  (`sorted(data, key=lambda s: s.start)`). For only the app's own sessions,
  filter on `.source`.

  `kind` is one of OpenFit's session kinds, `SESSION_KINDS` in
  `app/metrics.py`: `sleep`, `workout`, `duties_rostered` or
  `duties_complete`. Any app can read any of them: every kind is shared, and
  an app can't declare its own yet. Anything else raises
  `ValueError("unknown session kind: '<kind>'")`.

  `since` and `until` are days, as `YYYY-MM-DD` text or a `date`, and both
  are inclusive. They're matched against the UTC date a session starts on,
  as the route's `?from=` and `?to=` are, so a night that starts at 23:30 UTC
  belongs to that date. `None`, the default, leaves that end of the range
  open. A day that isn't one raises `ValueError("since must be YYYY-MM-DD:
  <repr>")` (or `until`), and a `since` later than `until` raises
  `ValueError("since must not be after until: <since> > <until>")`. There's
  no `days=` option: session reads such as a roster timeline want explicit
  ranges.

  It only reads, in `render()` and `handle()` alike. In `handle()` it also
  sees sessions written earlier in the same action, which aren't committed
  yet.
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
| `ctx.input(key, action, label=None, kind="number", unit=None, options=None, accept=None, toggle=None)` | `{"type": "input", "key", "action", "label", "kind", "unit", "options", "accept"}`, plus `"toggle"` only when given | A field under its label (small, uppercase), its unit and a **Save** button beside it. `kind` is one of `INPUT_KINDS`: `"number"` (the default), `"text"`, `"select"` — a dropdown of `options`, a non-empty list of non-empty strings, required for a select and refused for the other kinds (`options` is `null` in their specs) — or `"file"`, a file picker offering `accept`'s hints (`".txt,.csv"`, `"text/plain"`: optional non-empty text, refused for the other kinds — `accept` is `null` in their specs, and in a file's without one). Save, or Enter, sends `action` to the app's `handle()` with the payload `{key: what was entered}` (a number for kind `"number"`, text as typed for `"text"`, the chosen option's string as written for `"select"`, and for `"file"` the file picked, a `FileUpload` — Save only, sent multipart), and the tile is redrawn from the fresh spec the reply carries; a refusal shows under the field, which keeps what was entered. A blank field, or a number that isn't one, says so and sends nothing; so does a select still on its "Choose…" placeholder, a picker with no file picked ("Choose a file.") or one over 25 MB ("File too large (max 25 MB)."). A file input can carry a companion **toggle**, `{key, label, confirm}` (only for a file — any other kind's is refused; see [A file input's toggle](#writing-apps-as-sources)): a checkbox and its label in the row before Save, its checked state sent in the payload under its own `key` beside the file, and — ticked, with a `confirm` — the page's own modal asking that first, nothing sent unless it's confirmed; a spec with none has no `toggle` key, the one from before toggles. The host doesn't check a select's value is one of its options (the action route remembers no render), nor what a file holds: `handle()` does. In a panel it sits at the foot. In Configure it's inert, like the rest of the face. See [Writing](#writing-apps-as-sources) and `docs/architecture/input-primitives.md`. |
| `ctx.timeline(lanes, now=None, title=None, sync=None)` | `{"type": "timeline", "title", "now", "lanes": [{"label", "grey_after_now", "intervals": [{"start", "end", "label"}, ...]}, ...]}`, plus `"sync"` only when given | One lane or two of time-blocks side by side on one vertical time axis, in a frame that scrolls — lane 0 the left column in the lighter tone, lane 1 the right in the darker — under the lanes' labels; a line at `now`, and a `grey_after_now` lane greyed below it. With `sync`, a focus group's name, it's drawn around the group's focus clock: a fixed cursor, the time under it and a Now button, every timeline of the group scrolling to one time. See [the timeline builder](#the-timeline-builder), below. |
| `ctx.rail(cards, side, title=None, sync=None)` | `{"type": "rail", "side", "title", "cards": [{"id", "start", "end", "out", "in", "port"}, ...]}`, plus `"sync"` only when given | A column of fixed-size cards under its title, stacked from the top — four corners each, five at most, the window moving with the focus time: ahead of it on a `"plan"` rail, back from it on a `"report"` rail, the head outlined. With `sync`, it reads that focus group's time and restacks as it moves. See [the rail builders](#the-rail-builders), below. |
| `ctx.rails(timeline, left=None, right=None)` | `{"type": "rails", "left", "timeline", "right"}` | A timeline drawn exactly as it is alone, a rail either side: four columns, a rail, the two lanes, a rail. In a tile too small for them, the timeline alone. See [the rail builders](#the-rail-builders), below. |
| `ctx.grid(cols, rows, cells)` | `{"type": "grid", "cols", "rows", "cells": [{"x", "y", "w", "h", "spec"}, ...]}` | A tile split into sub-panels, each drawn as a tile of its own on the Home grid's cells: `cols` by `rows` cells, each sub-panel `w` by `h` from column `x`, row `y` (from 0), its `spec` sized to it as to a tile. Cells lie inside the grid and never overlap. See [the grid builder](#the-grid-builder), below. |
| `ctx.strips(lanes, now=None, sync=None, hours=None)` | `{"type": "strips", "now", "lanes": [{"kind", "label", "clock", "spans": [{"start", "end", "tone", "label", "place", "tz"}, ...], "troughs", "bands": [{"start", "end", "label"}, ...], "wocl": [{"start", "end"}, ...]}, ...]}`, plus `"sync"` and `"hours"` only when given, and a sleep lane's spans each a `"stage"` too | One lane to three of tinted spans on one horizontal time axis, a day across the frame and the rest a scroll away — the first lane taking the height the others leave, on an hour grid, each after it a 45px row: a `"sky"` lane's day, night or not known (hatched), faded from the top, a `"duty"` lane's day, night and WOCL pieces a slim bar butting square, a span's label once above its start, a duty lane's bands (trips away) amber bars behind its pieces, each port in it past the flight out, a lane's troughs a solid curve over it, a sky lane's WOCL red under the curve, a tag top-left of each lane — its label, over a clock lane's time under the cursor: the place and its local time (`"place"`), or the date and its local time (`"date"`). A `"sky"` lane's span may also be `"rest"` (the body's rest window, a green tint); a `"sleep"` lane's spans are sleep stages, each a bar rising to its stage's level, `"in"` (green) or `"out"` (red) of the rest window; a `"ticks"` lane's are `"disturbed"` stretches, red ticks. `hours` (12 to 168) sets how many hours the frame's width shows. With `sync`, drawn around the group's focus clock as a synced timeline is, on its side, and scrolled with it both ways. See [the strips builder](#the-strips-builder), below. |
| `ctx.row(children, widths=None)` | `{"type": "row", "children": [...], "widths"}` | Its children side by side, left to right, each the row's full height and its share of the width by its weight in `widths` (`null`: equal shares). See [the dashboard builders](#the-dashboard-builders-row-column-donut-and-bars), below. |
| `ctx.column(children, heights=None)` | `{"type": "column", "children": [...], "heights"}` | Its children stacked, each the column's full width and its share of the height by its weight in `heights` — unlike `panel`, which gives each child its own height. |
| `ctx.donut(segments, figure=None, label=None, caption=None, note=None, key=None)` | `{"type": "donut", "segments": [{"value", "tone", "planned"}, ...], "figure", "label", "caption", "note", "key"}` | A ring of segments clockwise from twelve o'clock, each its share of the total, in the status colours (`good`, `warn`, `crit`, `neutral`), a planned one hatched; `figure` big in the middle, `label` small under it; `caption`, `note` and a solid/hatched `key` under the ring. It fits its box, the figure scaling with it. |
| `ctx.bars(bars, max=None, title=None, now=None)` | `{"type": "bars", "title", "max", "now", "bars": [{"segments": [...], "label"}, ...]}` | Bars side by side in an inset frame, each its segments (a donut's) stacked from the foot on one scale, 0 to `max` (`null`: the tallest bar's total), a bar's `label` under it where given, `title` the tag top-left, the now line before bar `now`. |
| `ctx.image(src, alt=None, dark=None, position="center")` | `{"type": "image", "src", "dark", "alt", "position"}` | A picture carried in the spec itself — a `data:` URI of a PNG, JPEG or WebP, never a web address, so the page fetches nothing — drawn whole (contained) at `position`, `dark` in its place in the dark themes. Alone in a grid's sub-panel it's drawn bare, on the page. See [the image builder](#the-image-builder), below. |
| `ctx.empty(message=None)` | `{"type": "empty", "message"}` | Muted, centred text (default "No data yet"). |
| `ctx.error(message)` — the host's, or an app's for a part that failed | `{"type": "error", "message"}` | Muted, centred: "Couldn't render <app name>" over the message. The host's when a render fails; an app may return one for a part of its tile — a sub-panel — so the rest is drawn (the FRMS app's `_panel()`). |

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

### The timeline builder

```python
ctx.timeline(lanes, now=None, title=None, sync=None)
# -> {"type": "timeline", "title": title, "now": "2026-10-19T00:00:00Z",
#     "lanes": [{"label": "Roster", "grey_after_now": False,
#                "intervals": [{"start": "...Z", "end": "...Z", "label": "CX105"}, ...]},
#               {"label": "Complete", "grey_after_now": True, "intervals": [...],
#                "bands": [{"start": "...Z", "end": "...Z", "label": "MEL"}]}]}
# a lane's "bands" only when it has any; with sync="frms", the same plus "sync": "frms"
```

A generic two-lane interval timeline — a roster's plan against what was flown,
or a night's planned sleep against the sleep a device saw — where the host
carries the times and the page owns every pixel, as with a line.

- **`lanes`** is a list (or tuple) of one lane or two, in the order drawn:
  lane 0 is the left column, in the lighter tone (`--timeline-a`, a plan),
  and lane 1 the right, in the darker (`--timeline-b`, the actual). One lane
  takes the whole width, in lane 0's tone. The colour is by lane index; there's
  no colour field. Any other count raises `ValueError`, and lanes that aren't a
  list raise `TypeError`. Each lane is a dict of:
  - `label`: text or `None`, the column's heading;
  - `grey_after_now`: `True` or `False` (the default) — `True` for a lane
    that has nothing after `now`, like the flown side of a roster, whose
    column is greyed from the now line down;
  - `intervals`: a list of `{start, end, label}`, required (`[]` for an empty
    lane). `start` and `end` are each a `datetime` or ISO-8601 text,
    normalised as a session's times are (`_session_instant()`: UTC, whole
    seconds, a naive time taken as UTC, written `YYYY-MM-DDTHH:MM:SSZ`). Both
    are required — the timeline has no open-ended blocks, unlike
    `write_session()` — and the end is never before the start (the same
    instant is allowed). `label` is text or `None`: the short code drawn on the
    block, such as `CX105`.
  - `bands`: optional, a list of `{start, end, label}` (`BAND_FIELDS`), each
    a span shaded behind the lane's blocks — a trip away from base, its
    flights drawn over it. Its ends are an interval's (`_band()` is
    `_interval()`'s twin: both through `_span()`): both required, normalised
    the same way, the end never before the start; `label` text or `None`,
    the port (`MEL`), written small inside it. The host only carries the
    spans: what one covers is the app's to work out (the roster's
    `trips()`, below). A lane with none — absent, `None` or `[]` — carries
    no `bands` key at all, so its spec is exactly the one from before bands.

  A lane, an interval or a band that isn't a dict raises `TypeError`; a field
  it doesn't take raises `ValueError` (so a misspelt `gray_after_now` is said,
  not ignored); a label that isn't text, a `grey_after_now` that isn't a bool,
  or intervals or bands that aren't a list raise `TypeError`; a missing end, a
  time that isn't one, or an end before its start raise `ValueError`.
- **`now`** is a `datetime`, ISO-8601 text or `None`, normalised the same way:
  where the now line is drawn. `None` draws no line, and greys nothing.
- **`title`** is optional text, drawn small above the timeline, as a line's is.
- **`sync`** is optional text, the name of a focus group (`"frms"`) the
  timeline joins; `None` or an empty name joins none. Given, the spec carries
  it as `"sync"`; otherwise the spec has no `sync` key at all — exactly the
  spec from before there was one. Anything but text raises `TypeError`. The
  host only carries the name: the focus clock is the page's (below).

**How it's drawn.** The axis runs from the earliest start to the latest end
(or `now`, if that's later), down the tile. The timeline fills what's left of
its tile (in a panel, the space its siblings leave) with a frame that scrolls,
and lays the axis out to the frame's height: a week fills it
(`TL_VIEW_MS`), so a longer span scrolls and a rest of days is real empty axis
you scroll past. A frame shorter than 240px (`TL_MIN_WEEK_PX` — a 2×1 tile's)
keeps that scale and shows less than a week, scrolling further, rather than
squeezing a week into it until every block is at the floor and runs into the
next. A block runs from its start to its end, never shorter than 18px
(`TL_MIN_BLOCK_PX`), so a short duty stays legible and tappable; its label
sits at its top, cut to fit. The now line crosses both lanes in `--accent`,
and a `grey_after_now` lane is hatched grey from it down, under its blocks.
A lane's bands (`.tl-band`) are drawn in its own column — a rostered trip on
the left, a flown one on the right, side by side when plan and actual agree —
first in it, so under the grey, the blocks and the lines, and never in the way
of a tap (`pointer-events: none`). Each is placed on the blocks' scale, in the
same layout pass, so a resize keeps it aligned: from its start for its true
length (no floor). It's the lane's full width, so it shows either side of the
blocks over it, a soft tint of `--layover` (amber, its own token in every
theme: `#c07d18` light, `#e0a83a` dark) with a dashed edge. Its label
(`.tl-band-label`) is placed past the block or blocks over its top edge — the
flight out, which starts with the band — so it sits on the layover rather than
under a block, cut off by the band if the layover's too short to hold it. The
axis spans every block and band, so none is drawn off it. **Day lines**: a
faint 1px line across both lanes at each midnight, UTC, on the axis (the
first at or after its start, then every 24h to its end; an axis within one
day has none, and so does one of more than ten years — 3660 lines,
`TL_MAX_DAY_LINES` — rather than a node a day) — `.tl-gridline`s in one layer (`.tl-grid`) first in the track,
so under the bands, the grey, the blocks, the now line and the cursor, never in
the way of a tap, and placed in the same layout pass as the blocks. Their
colour is `--grid`, its own token in every theme (`--muted` at 22% light,
26% dark); no labels. (UTC, as the readout is; home-local days wait for the
time-zone work.) The frame opens
with the now line in the middle (with no `now`, the end in view), and a
resize keeps the time at its top. Times are UTC. A hand-built spec is drawn
as far as it can be: two lanes at most, and
an interval or band without both ends, in order, left out (bands that aren't
a list, none). Nothing to draw at all shows "No data". The lanes' headings
are 10.5px with .8px letter-spacing; in a narrow tile (`@container
(max-width:519px)` — a 2-wide one, or a 4-wide one a small screen shrinks)
they're 10px with .3px, on the same line height, so an eight-letter heading —
COMPLETE — fits a 2×2's ~61px lane and the heading row stays level with the
rails'.

**Scrolling: six hours a step.** A wheel's notch or an arrow key (↑/↓, on
the focused frame) moves the time at the frame's middle — a synced
timeline's cursor — to the next 6-hour mark that way (00, 06, 12, 18Z;
`TL_MARK_MS`): from a mark, the next; off one, the first that way. The wheel
listener is `{passive: false}` and takes the event over (`preventDefault`);
its `deltaY` (lines a third of a notch, pages the frame's height) is
accumulated per **gesture** — a run of events with no 150ms pause
(`TL_GESTURE_MS`) and no turn back: a gesture's first event steps at once,
however small (a trackpad's swipe starts small, and some mice's notch is a few
pixels), then one step for each further 100px (`TL_NOTCH_PX`, about a mouse
notch: Chrome's 100px, Firefox's three lines) — so a mouse steps a notch at a
time, quick or slow, and a trackpad's short swipe is one step, a long one a
few, never a race. A pinch (`ctrlKey`) or a sideways turn is left to the
browser. Any other scroll by the user — the scrollbar dragged, a page key, a
touch — **settles** on the nearest mark once it ends (`scrollend`; a browser
without it leaves it where it's left). The frame's own scrolls (opening on now,
a resize, a synced one's to its group's focus) are never settled. A position
within half a pixel of a mark is on it: the browser rounds a scroll to the
pixel, which would otherwise floor a mark to the quarter hour before. Not the
CSS's `scroll-snap`: Chromium snaps a page's own `scrollTop` writes too
(checked: under `proximity` with 6h snap points, 100 → 103 and 1000.5 → 994),
so it would pull a synced timeline off its focus — the follow tick's quarter
hour — and stop it following now.

The frame's height is only known once the tile is on the page, which it isn't
while `renderSpec()` builds it. So the timeline is laid out at a 240px
fallback (`TL_FALLBACK_PX`) as it's built, then again on the next animation
frame and on every resize, at the height it has, by a `ResizeObserver` on the
frame. That observer is disconnected once its tile is drawn afresh
(`releaseTimelines()`, called wherever Home's faces are replaced, and by the
observer itself on finding its frame gone). With no height at all — Home behind
another tab — the last layout stands.

**Synced: the focus clock.** A timeline with a `sync` name is drawn around its
group's focus clock instead — the backbone for panels that scroll together by
time (the roster's is the first; a sleep panel joining `"frms"` later syncs
with it for free). Without `sync`, none of this is drawn: no cursor, no
readout, no button, the now line centred on opening as above.

- **The focus clock** is UI state the page keeps, in memory, per group name
  (`focusGroups`: `{focusMs, following, members}`) — never stored, never sent
  to the server, nothing in `app.yaml`. A group starts with the first timeline
  that names it, on now and **following** it; a page load starts every group
  afresh. A timeline joins its group when drawn and leaves once its frame is
  off the page (`releaseTimelines()`, beside its `ResizeObserver`), so a tile
  drawn afresh — an upload's reply, a Refresh, a sync — rejoins on the group's
  time, scrolled or not. What's shared is the **time**, never a scroll
  position: each member converts it to its own scroll by its own scale, so
  timelines of different spans and heights stay on one instant.
- **The cursor** (`.tl-cursor`) is a fixed `--accent` line across the middle
  of the frame, over it in a wrapper (`.tl-view`): the track scrolls under it.
  The track is padded half a frame at each end, so every time on the axis can
  reach the cursor. Every layout — the first, and every resize — puts the
  group's focus time under it (a time past an end of this axis scrolls as far
  as it goes).
- **The readout** (`.tl-readout`), in a row over the lanes' labels, is the
  focus time in UTC, to the quarter hour: `Mon 03 Nov 14:15Z`. Every focus
  time is snapped down to the quarter hour (`tlSnap()`).
- **Scrolling by hand** — a wheel, a drag, the keyboard — sets the group's
  focus to the time under that timeline's cursor — the 6h mark it's on (a
  wheel's or an arrow's step, or a scroll settled), else the quarter hour —
  and stops it following; every other member scrolls there, and every readout
  says it. The one scrolled stays where it was put while it's moving (a drag
  settles on a mark once it ends, above). Its own scrolls — to the focus —
  aren't taken for the user's, and a move of the focus (Now, the follow tick,
  another member) outdoes a settle still to come.
- **Following**: while a group follows, a single shared timer (`followNow()`,
  due on each quarter hour) moves its focus on to now, and every member with
  it. A group that isn't following stays where it was left.
- **Now** (`.tl-nowbtn`), at the end of the readout's row — in a rails frame,
  the frame's top-right (below) — is shown only while the group isn't
  following — scrolled off now — and hidden while it is. It puts the focus
  back on now, following again, on every member.
- **The now line** stays at the spec's `now`, scrolling with the track, and
  carries a **Now** tag on the greyed lane, just under the line, where the grey
  starts. It's `--accent`, like the cursor: the two lines meet while the group
  follows, and part once it's scrolled.

### The rail builders

```python
ctx.rail(cards, side, title=None, sync=None)
# -> {"type": "rail", "side": "plan", "title": "Plan", "sync": "frms", "cards": [
#       {"id": "2026-11-05T15:10:00Z", "start": "2026-11-05T15:10:00Z", "end": "2026-11-08T13:00:00Z",
#        "out": "CX105", "in": "CX104", "port": "MEL"}, ...]}
# with no sync (or ""), no "sync" key

ctx.rails(timeline, left=None, right=None)
# -> {"type": "rails", "left": <a rail spec or None>, "timeline": <the timeline spec, as given>,
#     "right": <a rail spec or None>}
```

A rail is a column of cards beside a timeline — a roster's trips, the plan on
one side and the reports on the other — that restacks as the timeline is
scrolled. The host carries the cards; the page lays them out and picks which
show.

- **`cards`** is a list (or tuple) of dicts of only `RAIL_CARD_FIELDS`:
  - `start`: required, a `datetime` or ISO-8601 text, normalised as a
    session's times are (UTC, whole seconds, naive taken as UTC);
  - `end`: the same, or `None` for a card still open (a trip not home as far
    as the data knows); never before the start;
  - `id`: text or `None`, the card's stable id (a trip's: its flight out's
    start, as its band's) — carried, never drawn;
  - `out`: text or `None`, the flight out (`CX105`);
  - `in`: text or `None`, the flight home (`CX104`) — `None` while it isn't
    known, an open trip's;
  - `port`: text or `None`, the port away — the layover's, or a day
    return's turn-round (`MEL`).

  Every key is in the spec, `None` where not given. A card that isn't a dict
  raises `TypeError`; a field it doesn't take `ValueError` (a misspelt
  `inbound` is said, not ignored); a missing start, a time that isn't one or
  an end before its start `ValueError`; a text field that isn't text
  `TypeError`.
- **`side`** is required: `"plan"`, a rail that looks ahead of the focus
  time, or `"report"`, one that looks back from it (`RAIL_SIDES`). Anything
  else raises `ValueError`.
- **`title`** is optional text, the rail's heading (`Plan`).
- **`sync`** is optional text, a focus group's name, as a timeline's; `None`
  or `""` adds no key.
- **`rails(timeline, left, right)`**: `timeline` a timeline spec
  (`ctx.timeline()`), carried verbatim — the same dict, not re-checked or
  copied — and `left`/`right` rail specs or `None`. A part that isn't a dict
  raises `TypeError`; a spec of another type `ValueError`.

**How it's drawn.** A rails frame (`railsSpec()`) is four columns: a rail, the
timeline's two lanes, a rail. The timeline is drawn by `renderSpec()` exactly
as it is on its own — its DOM node for node, its cursor, readout, bands and
focus clock untouched, bar its Now button — and takes what the rails leave;
each rail is `clamp(84px, 22cqi, 156px)` wide. A rail mirrors the rows over the
timeline's frame with spacers (its title's line, its focus row), so the rail's
heading sits in the lanes' heading row — Plan · Roster · Complete · Report —
and its cards start level with the frame. **Now is the frame's**: the
timeline's Now button — the same button, its click and its hiding while the
group follows unchanged — is moved out of the focus row into `.rails-now`,
the frame's last child, absolutely placed at its top-right (`.spec-rails` is
`position: relative`), over the Report rail; a spacer for each row above the
focus row (the title's line) puts it level with that row. The readout then
has the row to itself, centred over the lanes (the row keeps the lanes'
scrollbar gutter), so a 2×2's is no longer cut off. With the rails left out
(below) the row is as it is alone: the readout at its start, room at its end
(`padding-right: 48px`) for Now, which is then at the timeline's top-right. They show **down to a 2-wide
tile**: a 2×2 at full size (334px inside) holds the four columns, each rail at
its 84px minimum and the timeline's lanes the ~150px left. **In a tile too
small for them** — under 310px wide or 240px tall inside (`@container`): a
2×1, or a 2-wide tile whose cells a small screen has shrunk — the rails are
left out and the timeline has the tile, exactly as it's drawn without them.

A rail (`railSpec()`) is its heading — the title alone — over a list of
**fixed-size cards**, 6px apart (`RAIL_GAP_PX`), whatever they hold. Their
size is the CSS's, set by the rail's width (the rail is its cards' container,
`container-type: inline-size`): wider than 119px — a 4-wide tile's 156px — the
**roomy** card, 58px tall (`RAIL_CARD_PX`), an 11.5px flight number beside a
17px port; at up to 119px — the rail at its 84px minimum, in a 2×2 or a
4-wide tile on a small screen — a **compact** one, 46px tall, its padding
and edge slimmer and its type smaller (10px flight numbers, a 13px port, a
9px day), so a flight number still fits beside the port. A card is **four
corners**: top-left the flight out
(mono), top-right the port (the display face, the prominent one), bottom-left
the flight home (mono, muted; `—` for none — an open trip), bottom-right the
day it left (its start's, UTC: `05 Nov`). All as text, with an amber
(`--layover`) edge — a trip, as a band is; a day return is a card like any,
its port the one it turned round at. They're in time order (by start) and
**hug the top** of the rail. At most **five** show (`RAIL_MAX`), or as many
whole cards as the rail's height holds; which ones is `railWindow(cards,
focusMs, fit, side)`, by the rail's side:

- **Plan** looks ahead: from the first card not over at the focus time (open,
  or ending after it), and those after it — trips over by then drop off the
  top. Its **head** is its top card.
- **Report** looks back: up to the latest card over by the focus time (its
  end at or before it), and those before it — the latest home at the foot,
  older trips above it, and scrolling back brings older ones in. Its
  **head** is its bottom card.

Each is **clamped** at the list's ends, so it stays full: Plan near the end
stays on the last five, Report near the start on the first five (the head
there is still the end card). The head — the trip at the cursor's end of the
window — is outlined in the accent (`.head`: its border and a 1px ring). A
rail with no cards says "None".

**Synced**, a rail reads its focus group's time and joins the group as a
member beside its timelines, so every move of the focus — a timeline scrolled
by hand, Now, the follow tick — restacks it. It only ever reads the time: it
never scrolls anything or moves the focus, and nothing on a card is clickable
yet. Unsynced, it's drawn for the time it's drawn at. Its height is measured
as a timeline's is: five first, then on the next frame and on every resize (a
`ResizeObserver`, let go of with its tile by `releaseTimelines()`); with no
height, the last layout stands. A card's height is measured with it — the
size the CSS gave it, roomy or compact — so the rail counts whole cards of
the size drawn (`RAIL_CARD_PX` until a card's been measured; the last one
measured while none is): about four a side in a 2×2, five in a 4×3. A hand-built rail is drawn as far as it can
be: a card without a start, or with an end that isn't one or comes before it,
is left out; a frame's side that isn't a rail is none.

**Errors never fail the page.** `render_app()` catches anything `render()`
raises and logs it with the traceback (`[apps] <id>: render failed`). It also
catches a return value that isn't a spec (a dict with a string `type`) or
isn't plain JSON, such as NaN or a set. Any of these come back as an error
spec with a 200, so the page shows one error tile. A spec type the page
doesn't know is drawn as an error tile too.

### The grid builder

```python
ctx.grid(6, 4, [
    {"x": 0, "y": 0, "w": 3, "h": 2, "spec": circadian},
    {"x": 3, "y": 0, "w": 3, "h": 2, "spec": ctx.empty("Coming soon")},
    {"x": 0, "y": 2, "w": 2, "h": 2, "spec": roster},
    ...
])
# -> {"type": "grid", "cols": 6, "rows": 4,
#     "cells": [{"x": 0, "y": 0, "w": 3, "h": 2, "spec": {"type": "strips", ...}}, ...]}
```

- **`cols`** and **`rows`** are whole numbers, 1 to 6 — a tile's own most;
  anything else (`0`, `7`, a bool, `"6"`) raises `ValueError`.
- **`cells`** is a list or tuple (else `TypeError`) of dicts (else
  `TypeError`), each `{x, y, w, h, spec}`: a sub-panel `w` cells wide and `h`
  high, its top-left corner at column `x`, row `y`, counted from 0. Each of
  the four a whole number (missing, a float or a bool: `TypeError`), and the
  sub-panel inside the grid — `x` and `y` at least 0, `w` and `h` at least 1,
  `x + w` at most `cols`, `y + h` at most `rows` — else `ValueError`. `spec`
  is a spec (a dict with a string `type`, else `TypeError`), carried verbatim.
  No two sub-panels share a cell (`ValueError`, "grid cells overlap"); a cell
  none covers is left empty. A cell takes those five keys and no other: a
  misspelt one (`wdith`) is refused (`ValueError`, through `_fields()`, as a
  lane's or a card's is). The spec carries each cell as those five keys, in
  the order given.

**How it's drawn.** A grid (`SPECS.grid`) is a `.spec-grid`: a CSS grid of
`cols` by `rows` even tracks (`repeat(N, minmax(0, 1fr))`), its gap the Home
grid's own (`--tile-gap`), each cell a `.spec-cell` placed at its corner and
spanning its size (`grid-column: x+1 / span w`, `grid-row` the same), its spec
drawn by `renderSpec()` for the app, as a tile's is. Out of Configure the tile
drops its own face (`.tiles:not(.configuring) .tile:has(> .spec-grid)`: no
background, shadow or padding, and no border — `border-width: 0`, so not even
its pixel), so the tracks land exactly on the Home grid's own cells — and
each sub-panel wears **a tile's face** (the same
background, border, radius, shadow and `16px 18px` padding) and is **a size
container** (`container-type: size`), as a tile is. So every container query
and `cqi` size answers to the sub-panel: a spec in a 2×2 sub-panel draws as
it does in a 2×2 tile — the rails shown at full size, with the compact card,
and left out when a small screen shrinks the cells. A timeline or a rail in a
sub-panel joins its focus group exactly as at the top of a tile, and is let
go of with the tile by `releaseTimelines()`: nothing in the focus clock
changes. In Configure the tile keeps its dashed face and its controls, and is
moved and sized whole. A hand-built grid is drawn as far as it can be: cells
that aren't a list are none, and a cell's spec the page can't draw is an
error in that sub-panel alone. On the server a sub-panel's fault is the
app's to isolate: build each cell's spec in its own `try`, and on an
exception put `ctx.error(...)` in that cell (the FRMS app's `_panel()`,
below) — a `render()` that raises is still one error tile, the whole grid.
Not yet: titles on sub-panels.

### The strips builder

```python
ctx.strips([
    {"kind": "sky", "clock": True, "troughs": troughs, "wocl": [{"start": ..., "end": ...}, ...], "spans": [
        {"start": ..., "end": ..., "tone": "day", "place": "LHR", "tz": "Europe/London"}, ...]},
    {"kind": "sky", "label": "Home", "clock": "date", "spans": [
        {"start": ..., "end": ..., "tone": "night", "place": "HKG", "tz": "Asia/Hong_Kong"}, ...]},
    {"kind": "duty", "spans": [{"start": ..., "end": ..., "tone": "wocl", "label": "CX256"}, ...],
     "bands": [{"start": ..., "end": ..., "label": "LHR"}, ...]},
], now=now, sync="frms")
# -> {"type": "strips", "now": "...Z", "sync": "frms", "lanes": [
#      {"kind": "sky", "label": None, "clock": "place", "troughs": [...],
#       "spans": [{"start", "end", "tone", "label", "place", "tz"}, ...],
#       "bands": [], "wocl": [{"start", "end"}, ...]}, ...]}
```

Lanes of tinted spans on one horizontal time axis — the circadian panel's.

- **`lanes`** is a list or tuple (else `TypeError`) of one to three lanes
  (`STRIP_LANES`; else `ValueError`), in order, top to bottom. Each a dict of
  only `STRIP_LANE_FIELDS` (`_fields()`: not a dict `TypeError`, another
  field `ValueError`):
  - `kind` — `"sky"` or `"duty"` (`STRIP_KINDS`, else `ValueError`), which
    says the tones its spans take: a sky's `"day"`, `"night"` or `"none"`
    (not known — drawn hatched), a duty's `"day"`, `"night"` or `"wocl"`.
  - `spans` — a list or tuple (required, `[]` for none; else `TypeError`)
    of dicts of only `STRIP_SPAN_FIELDS`: `start` and `end` both required
    (`ValueError`) and stored as a session's are (`_session_instant()`:
    `iso_utc()` text, UTC to the second, a naive time taken to be UTC), the
    end never before the start (`ValueError`; equal allowed); `tone` one
    of its lane's kind's (`ValueError`); `label` text or `None` — a duty's
    code, only on a duty lane (a sky span's label is `ValueError`: a sky's
    span is day or night, not a thing); `place` and `tz` text or `None` —
    an IATA code and an IANA zone, read on a clock lane (else `TypeError`).
  - `troughs` — optional, a list or tuple (else `TypeError`) of instants,
    each normalised as a start (`ValueError`): a solid curve over the
    lane, −1 at each trough, +1 midway between two, `-cos` between — the
    body clock's.
  - `label` — text or `None` (`TypeError`): the lane's tag, top-left — over
    its clock, on a clock lane.
  - `clock` — `False` (the default: no clock), `True` or `"place"`, or
    `"date"` (`STRIP_CLOCKS`; anything else — `"time"`, `1`, `None` —
    `ValueError`): the tag tells the time under the cursor — `"place"` the
    span there's `place` and its local time in its `tz`, `LHR 01:00 UTC+1`;
    `"date"` the date and its local time there, `Fri 23 Oct 20:00`. With a
    `label` too, the tag is the label over the clock (Home over its date
    and time). Carried as `False`, `"place"` or `"date"` — `True` as
    `"place"`.
  - `bands` — optional, a duty lane's only: a list or tuple (else
    `TypeError`) of dicts of only `STRIP_BAND_FIELDS` (`start`, `end`,
    `label`), each checked as a timeline band is (`_span()`, the messages
    naming "a strips band"): both ends required and normalised, the end
    never before the start, `label` text or `None` — a trip away from base,
    drawn as a bar behind the lane's spans, its label (the port) in it.
    A non-empty list on a sky lane → `ValueError`.
  - `wocl` — optional, a sky lane's only: a list or tuple (else
    `TypeError`) of `{start, end}` (only those two; "a strips wocl span"),
    checked the same way — the window of circadian low, under which the
    lane's curve is marked red. Drawn only where there's a curve: on a lane
    without troughs it's carried and draws nothing. A non-empty list on a
    duty lane → `ValueError`.
- **`now`** — a time, normalised the same way, or `None`: where the now
  line is drawn.
- **`sync`** — a focus group's name, text or `None` (else `TypeError`), as
  a timeline's: `None` or `""` adds no key.

In the spec every lane is `{kind, label, clock, spans, troughs, bands,
wocl}` — `bands` and `wocl` `[]` when not given (absent, `None` or `[]`, on
either kind, so a lane as the spec carries it is one `strips()` takes back)
— every span `{start, end, tone, label, place, tz}`, every band `{start,
end, label}` and every WOCL span `{start, end}`, `None` where not given — so
the page never guesses at a key.

**The sleep panel's lanes.** Two more kinds, and a tone more for a sky:

- a **`"sky"`** span may be **`"rest"`** — the body's rest window — tinted
  green (`--sky-rest`) and faded as day and night are;
- a **`"sleep"`** lane's spans are toned **`"in"`** or **`"out"`** (of the
  rest window) and each carries a **`stage`**, `"light"`, `"deep"` or
  `"rem"` (`STRIP_STAGES`; another, or none, `ValueError`) — the only span
  that takes one (a stage on another lane's span, `ValueError`), and the
  only span the spec gives the key. Each is a bar rising from the lane's
  foot to its stage's level — deep highest, then REM, then light — in
  `--sleep-in` (green) or `--sleep-out` (red), over a hairline at each level
  (`.st-lvl`), the hour grid's lines upright only (`.st-hours-v`), the
  levels named at the right of the lane's tag (`.st-lv`);
- a **`"ticks"`** lane's spans are toned **`"disturbed"`**, each red ticks
  (`--tick`) hanging in the row under its tag.

A label is still a duty span's alone; bands a duty lane's, WOCL a sky's.
**`hours`** — a whole number from 12 to 168 (`STRIP_HOURS`; else
`ValueError`) — is how many hours the viewport's width shows: a day when not
given, and then no key, so a day's strips are as before. The hour grid's
square grows with it — an hour at a day across, three hours at three days.
The page draws a hand-built sleep span with no stage it knows not at all.

**How it's drawn.** `stripsSpec()` (`SPECS.strips`) is a timeline turned on
its side, its synced path mirrored line for line, horizontally — and drawn
in the roster's language: the timeline's day lines, block ends, cursor and
now line. The axis runs from the earliest span's start to the latest end (or
`now`); **a day** (`ST_VIEW_MS`) fills the viewport's width (`.st-scroll`,
which scrolls sideways), so the rest is a scroll away. The lanes are a CSS
grid (`.st-track`): the first takes the height the others leave
(`minmax(0,1fr)`), each after it a 45px row (`grid-auto-rows`), with no gap —
a hairline of `--border` between two (`.st-lane + .st-lane`).

- **The hour grid** — the first lane, whatever its kind, sits on a subtle
  grid of `--grid` lines, a square an hour (`.st-hours`, first in the lane,
  so under its spans): one background of two line gradients, sized at every
  layout to an hour of the axis each way (`hour = 3600000 · px`, the
  viewport's width over 24) and offset so a vertical line falls on **every
  UTC hour** (`backgroundPosition` x = `pad + (the first whole UTC hour at
  or after t0 − t0) · px`). On UTC hours, as the roster's day lines are: a
  half-hour zone's local hours don't land on a line.
- **The midnight lines** — a slightly stronger line (`.st-mid`, 1px of
  `--muted` at 45%) at each midnight UTC from the first at or after the
  axis' start to its end, in the track, so through every lane — as the
  timeline's day lines, and as its, none on an axis of more than
  `TL_MAX_DAY_LINES` days.
- **A sky** — each span its tone's token (`--sky-day`, `--sky-night`:
  translucent tints over the frame; `"none"` a hatch in `--grid`), **hard at
  its ends** — sunrise and sunset — and faded from the top: full colour at
  the lane's top, nothing halfway down (a `mask-image` gradient on
  `.st-sky .st-span`), so the grid shows through more and more below. Home's
  row the same fade, no grid.
- **A duty** — a slim 14px bar low in its row (`top: 24px`), `--duty-day`
  green, `--duty-night` amber or `--duty-wocl` red (softer: deeper in the
  light theme, pastel in the dark, the roster blocks' tone), a hairline of
  the frame round it (`box-shadow`, as a roster block's). Its pieces **butt
  square**, nothing between them, so they tessellate into one bar; **the
  duty's own two ends are rounded** (4px, as a block's): a piece is
  `.first` where a duty starts — it carries a label, or isn't straight on
  from the one before — and `.last` where it ends — the next isn't straight
  on, or carries a label of its own (another duty's, however close). A
  span's label is its own element (`.st-code`, mono, `--duty-ink`) set
  **above** the bar at the span's start — the app puts one on a duty's
  first piece, so a short first piece never clips its code.
- **A trip away** — each of a duty lane's bands is a translucent amber bar
  (`.st-band`), the duty bar's height and place (`top: 24px`, 14px), the
  timeline band's colours — `--layover` at 18% with a dashed edge at 60% —
  and a block's 4px corners, first in its lane after the hour grid, so
  **behind** the duties: it shows in the gap between the flight out and the
  flight home, the layover. Its label (`.st-band-label`, mono 10px, the
  timeline band label's amber ink) is set as the roster's band places its
  own: **past whatever covers the band's start** — the flight out, all its
  pieces — so on the layover; with nothing over its start, at its start; no
  label, no element. Placed in the same layout pass as the spans.
- **The curve** — a lane's troughs are an SVG path (`.st-curve`), **solid**,
  1.5px, in `--curve` (the roster's purple: `--timeline-b` light,
  `--timeline-a` dark), no fill, a point every twenty minutes
  (`ST_CURVE_STEP_MS`), 10px in from its lane's top and lifted clear of its
  foot: `amp = (lane height − 10 − ST_WOCL_PX − 6) / 2`, so a trough sits
  32px off the lane's foot — room for the WOCL's red to fade (never below
  0: a lane too short for it draws the curve flat, not turned over).
- **The WOCL under the curve** — a sky lane's `wocl` spans, where it has a
  curve: one `path.st-wocl` under the curve's own, in the same SVG, a closed
  subpath per span — the curve along its top, a point every ten minutes
  (`ST_WOCL_STEP_MS`) from the span's start to its end, and back along the
  same curve `ST_WOCL_PX` (26px) lower — filled by a vertical
  `linearGradient` in the SVG's `defs` (an id of its own a strips,
  `st-wocl-<n>`), from `--duty-wocl` at 55% opacity (`.st-wocl-a`) to none
  (`.st-wocl-b`): a red fill hanging under the curve and fading to nothing
  a little way down, lined up with the duties' red WOCL pieces below. Set
  by `style.fill` — the CSS's `fill: none` on the curve's paths outranks an
  attribute — and `stroke: none`. No WOCL: no gradient, no extra path.
- **The now line** — the accent, **solid**, 2px, a dot at its head
  (`.st-now::before`), tagged Now at its foot: the timeline's now line,
  turned on its side.
- **The tags** (`.st-tags`) — an overlay on the same grid rows over the
  viewport — so they stay put as the track scrolls — kept clear of a
  scrollbar that takes room under it, so each sits level with its lane. No
  pill: a lane's label (`.st-tag-l`) in the roster's small uppercase muted
  heading style (`HOME`), and under it, on a clock lane (synced only), the
  clock (`.st-tag-c`) in plain mono — the two lines (`.two`: a column of
  14px lines, centred, so the label sits nudged up) — each as text. A clock
  is `clockAt()`: a `"place"` clock the span under the cursor's `place` and
  its local time through the browser's `Intl.DateTimeFormat` in its `tz`
  (`shortOffset`, `GMT` said `UTC`: `LHR 01:00 UTC+1`, `HKG 08:00 UTC+8`,
  `LHR 00:00 UTC+0` once London's summer time is over — the zone's own
  rules), the place alone for a zone the browser can't read, and `—` where
  nothing is known; a `"date"` clock the date and local time in the span's
  `tz` (`weekday`, `day`, `month`, `hour`, `minute`, `h23`, put together as
  `Fri 23 Oct 20:00`), and `—` with no span, no `tz` or one the browser
  can't read — never the place alone. Every clock tag is refreshed on every
  `show()`.

Every colour is a token, in all three theme blocks. Everything an app sent
is set as text.

**Synced** (`s.sync`), it's drawn around the group's focus clock exactly as
a synced timeline is: the timeline's own focus row over it (`.tl-focus` —
`.tl-readout`, the UTC time under the cursor, and `.tl-nowbtn`, Now, while
the group isn't following), a fixed `--accent` cursor down the middle of the
viewport (`.st-cursor`, over it, so the track scrolls under it — solid, an
arrowhead at its top and its foot, as the timeline's cursor has at its two
ends), and the track padded half a viewport at each end, so `scrollLeft` is
the focus time's offset on the axis. With a lane of troughs (the first such
lane), **a dot** (`.st-dot`, the accent, ringed in the surface) sits where
the cursor crosses the curve: `placeDot()`, on every layout and every
`show()` — so a scroll by hand, a member's move, Now and the follow tick all
move it — at the curve's height at the focus time (`curveAt()`, the lane's
top plus `m + amp − amp · value`, the geometry the curve was last drawn
at), and hidden off the troughs (before the first, after the last). It joins the group's `members` as
`{scroll, observer, group, show}` and sits in `timelines`, so
`releaseTimelines()` lets it go with its tile. **The time is what's shared,
never a scroll position**: the strips' day across and the timeline's week
down each scroll to the group's time by their own scale. A scroll by hand
moves the group's focus to the time under the cursor — the 6h mark it's on,
else its quarter hour — and stops it following; any member's move, Now and
the follow tick re-scroll it (`show(true)` → `toFocus()`) and refresh its
readout, Now button and clock tag. It steps as the timeline does, by
`TL_MARK_MS` — the timeline's own, so the two always rest on one mark: a
wheel's notch, down **or sideways** (whichever way it turned more),
accumulated per gesture as the timeline's is, or any of the four arrow keys
(→/↓ on, ←/↑ back) to the next 6h mark; any other scroll of the user's
settles on the nearest mark at `scrollend`. It's laid out at a fallback
width first, then on the next frame and on every resize (a
`ResizeObserver`) at the width and height it has. Unsynced, there's no
focus row, cursor, dot or clock tag, and it isn't scrolled to a time.

A hand-built spec is drawn as far as it can be: three lanes at most, a kind
that isn't `"duty"` drawn as a sky, a span without both ends in order left
out, an unknown tone hatched as not known; nothing to draw → "Nothing to
draw yet".

### The image builder

```python
ctx.image("data:image/webp;base64,UklGR...", alt="OpenFit FRMS",
          dark="data:image/webp;base64,UklGR...", position="bottom right")
# -> {"type": "image", "src": "data:image/webp;base64,...", "dark": "data:image/webp;base64,...",
#     "alt": "OpenFit FRMS", "position": "bottom right"}
```

**Carried, never fetched.** `src` (and `dark`) must be a `data:` URI of a PNG,
JPEG or WebP, base64 (`IMAGE_TYPES`; `_DATA_URI`, the whole string, trimmed):
never a web address — so drawing a spec makes no request, keeping the
no-external-calls rule whole — and never an SVG, which can carry script. At most
`IMAGE_MAX_CHARS` (1 MiB) each; anything else `ValueError`, a src that isn't
text `TypeError`. `alt` text or `None`; `position` one of `IMAGE_POSITIONS` —
`center` (the default), `top`, `bottom`, `left`, `right` or a corner (`"bottom
right"`) — else `ValueError`. Every key is in the spec, `dark` and `alt`
`None` where not given. App files are text, so an app keeps its image as a text
file of the URI beside `app.py` and reads it as it reads `config.json`.

The page (`imageSpec()`) checks the URI again (`IMAGE_SRC`) — one that isn't
draws "No image", never an `<img>` — and draws an `<img class="light">` of
`src`, contained in its box (`object-fit: contain`) at `position`
(`object-position`, `center` for one it doesn't know), its `alt` as given; with
a valid `dark`, the box gains `has-dark` and an `<img class="dark">` too, and
the CSS swaps them as the tokens switch — the theme setting `dark`, or the
system's dark unless the setting says `light`. **Alone in a sub-panel it's
bare**: `.spec-cell:has(> .spec-image)` drops the card's background, border,
shadow and padding, so the picture sits on the page.

### The dashboard builders: row, column, donut and bars

Four specs that know nothing of what they show — the parts of a dashboard
(the FRMS rest budget, and Phase 2's) — all additive: no spec before them
changes.

```python
seg = lambda v, tone, planned=False: {"value": v, "tone": tone, "planned": planned}
ctx.row([
    ctx.column([
        ctx.row([ctx.donut([seg(28, "good"), seg(2, "crit")], figure="7%", caption="30 days"), ...]),
        ctx.bars([{"segments": [seg(2, "crit"), seg(5, "good")], "label": "1"}, ...],
                 max=7, title="Disturbed rest · October", now=22),
    ], heights=[3, 2]),
    ctx.donut([seg(151, "good"), seg(3, "crit"), seg(10, "crit", True), seg(53, "good", True)],
              figure="6%", label="October", note="3 flown · 10 rostered of 217", key=["Flown", "Rostered"]),
], widths=[3, 2])
```

**`row(children, widths=None)`** and **`column(children, heights=None)`**:
`children` a list or tuple of one spec or more (else `TypeError`, or
`ValueError` for none), carried verbatim; `widths`/`heights` `None` (equal
shares) or a list or tuple (else `TypeError`) of one number a child (else
`ValueError`), each finite and above 0 (a bool too, `ValueError`). The page
draws each child as it is alone and gives it `flex: <weight> 1 0px`
(`splitSpec()`), 10px apart — weights not one a child, or not each above 0,
equal shares.

**A segment** — a donut's, or one of a bar's — is `{value, tone, planned}`
(`SEGMENT_FIELDS`; another key `ValueError`): `value` a finite number, 0 or
more (a bool, text or NaN `ValueError`), 0 drawn as nothing; `tone` one of
`CHART_TONES`, `"good"`, `"warn"`, `"crit"`, `"neutral"` (else
`ValueError`); `planned` optional, `True`/`False` (`None` is `False`; else
`TypeError`), drawn hatched — the roster's not-yet-happened. A tone is a
status token (`.ch-good{--ch:var(--good);}` … `neutral` `--muted`), so the
dark themes' own carry through; solid in it, planned stripes of it over a
22% tint of it.

**`donut(segments, figure=None, label=None, caption=None, note=None,
key=None)`**: `segments` a list or tuple; the texts text or `None` (else
`TypeError`); `key` `None` or two non-empty texts, `[solid, hatched]` (else
`ValueError`). In the spec every key is there, `None` where not given. Drawn
(`donutSpec()`) in a 100-square SVG fitting its box (`.ring`, a size
container): the bare ring (`.ch-track`, `--border`), then each segment its
share of the total, clockwise from twelve o'clock, `CH_GAP` apart — one
that's all of it a whole ring — a planned one filled with its tone's hatch (a
pattern made once a tone, its id unique on the page); `figure` in the middle
in the display face, its size 18% of the ring's, `label` small under it;
under the ring `caption` (the small uppercase heading), `note` (mono — its
line kept under a caption even when empty, so captioned donuts side by side
keep rings of one size) and `key`, a solid swatch and a hatched one. A tone
it doesn't know is drawn neutral; the SVG's `aria-label` is caption, label
and figure.

**`bars(bars, max=None, title=None, now=None)`**: `bars` a list or tuple
(else `TypeError`) of `{segments, label}` (`BAR_FIELDS`; another key
`ValueError`) — `segments` as a donut's, stacked from the foot in the order
given, `label` text or `None`; `max` `None` or a finite number above 0, and
no bar totalling more (`ValueError`, naming the bar); `title` text or
`None`; `now` `None` or a whole number from 0 to the bars' count (else
`ValueError`). Drawn (`barsSpec()`) in an inset frame as the strips': the
title top-left, each bar an even share of the width, each segment its value
over the scale's top as a share of the height — 2px between, taken from the
one above — a label under its bar, centred, and the now line (and its Now
tag, as the strips') before bar `now`.

## Writing: apps as sources

An app is a source, as a device is: what it writes lands in the same tables —
its readings competing in the same per-metric pick, its sessions passed through
beside every other source's — and is read back through the same contract. Its source is `app:<id>`, its installed id (`app_source()` in
`app/host.py`), and it can write nowhere else.

**`handle(ctx, action, payload)`**, beside `render()` in `app.py`, is the
app's write entry point, and the only place an app writes. It's what a tile's
input element sends (`POST /api/app/<id>/action`, below). `action` is the
input's action, a non-empty string; `payload` a mapping, `{key: what was
entered}` from an input (a select's value the chosen option's string; a file
input's a `FileUpload`, below); the app interprets both, and checks a value is
one it expects. An app without a `handle()` can be drawn but not actioned.

**A file is the app's to read.** A file input's value is a `FileUpload`
(`app/host.py`): `.name` (the file's name as the browser gave it — for
display, never a path), `.content_type` (the type the browser reported, `""`
for none — informational, not to be trusted), `.bytes` (its content),
`.text(encoding="utf-8")` (that decoded; a `UnicodeDecodeError` for a file
that isn't text in it is the app's to catch) and `.path` (a temp file holding
it, for a library that wants one: `sqlite3.connect(f.path)`). OpenFit carries
the file and never parses it — not even an `.ics` or a `.csv`: what it is and
how to read it is the app's. Nothing of it is kept: it lives for the action,
in a temp file the host named, deleted once the action is done whatever
`handle()` did, so only what the app writes survives.

**A file input's toggle.** A file input can carry one companion checkbox —
`ctx.input("file", "upload", kind="file", toggle={"key": "backfill",
"label": "Backfill", "confirm": "…"})` — for a choice that goes with the file,
such as how to file it. `toggle` is only for a file (any other kind's →
`ValueError`) and optional: a mapping of only `key`, `label` and `confirm`
(not a mapping → `TypeError`, another field → `ValueError`); `key` the payload
key its checked state is sent under — non-empty text, never the input's own
`key` (the file has that); `label` the checkbox's text, non-empty; `confirm`
optional non-empty text. The spec carries `"toggle": {key, label, confirm}`
(`confirm` `null` for none) only when one is given, so every input without one
is the spec from before. The page draws the checkbox, unticked, in the row
between the picker and Save, and the payload is then `{<input key>: the
FileUpload, <toggle key>: true or false}` — a JSON bool, always sent, ticked or
not. Ticked, with a `confirm`, Save first opens the page's own modal (never the
browser's `confirm()`): the toggle's label over the app's text, [Cancel]
(focused) and [Continue]; only Continue sends — Cancel, Escape or a click
outside sends nothing and leaves the tile as it was, still ticked. The tile's
own refusals (no file, one over 25 MB) come before it. The confirm is static
text: the app writes it at render, before it has read any file. What the
toggle means is the app's — the host carries its state and holds nothing to
it; `handle()` reads `payload.get(<key>)`.

**The writing ctx.** `handle()` gets a `WriteCtx(conn, app, today=None)`, made
for the app: a `Ctx` (so `ctx.metric()`, `ctx.sessions()` and `ctx.today()`
read as in `render()`), plus

```python
ctx.write_metric(metric, value, date=None)                  # files one reading, under ctx.source
ctx.write_session(kind, start, end=None, summary=None)      # files one session, under ctx.source
```

- **The source is the ctx's, never the app's.** It's fixed when the ctx is
  made, `ctx.source`, and no writer takes one. So an app can only ever create
  or overwrite its own rows: an app installed with the id `garmin` writes
  `app:garmin`, beside Garmin's rows, never over them. Structural scoping, not
  a sandbox: the app is trusted code, as its `render()` is.
- **`metric`** is a canonical key (`METRICS`) or a key the app declares in its
  manifest's `metrics`. Declare what you invent, write what exists: the
  source carries "app", not the key, so a weight-entry app files `weight_kg`,
  undeclared; a caffeine app declares `caffeine_mg` and writes it by that
  short key, filed as `app:<id>:caffeine_mg` (`custom_metric_key()`) in its
  declared unit. Anything else (an undeclared key, another app's, by either
  form) is `ValueError("unknown metric: '<key>' - declare it in app.yaml's
  metrics to write it")`. So an app writes only canonical keys and its own.
- **`value`** for a canonical metric is in its canonical unit and held to the
  webhook's rule (`plausible_value()` in `app/metrics.py`): a finite number,
  never a bool, within the metric's plausible range, both ends included. For
  the app's own: a finite number, never a bool (`finite_number()`), within its
  declared `range` if it has one.
- **`date`** is `YYYY-MM-DD` (or a `date`), today (`ctx.today()`, the server's
  local date until `base_tz` lands) by default. One reading per
  `(date, source, metric)`: writing a day again replaces it.
- A refused write raises `ValueError` and writes nothing. The ctx commits
  nothing; it notes the days it wrote readings to (`ctx.written`) for the
  route.

**`ctx.write_session(kind, start, end=None, summary=None)`** files one
session — an interval with a shape, as a sync files a night's sleep or a
workout — under the same `ctx.source`, through `write_session()`
(`app/plugins/base.py`, whose own kind check stays as a backstop), and
returns nothing:

- **`kind`** is one of OpenFit's session kinds, `SESSION_KINDS` in
  `app/metrics.py`: `sleep`, `workout`, `duties_rostered` or
  `duties_complete`. An app can't invent one yet: it
  writes what exists, and declares nothing for it. Anything else (a
  misspelling, a metric key, a non-string) is `ValueError("unknown session
  kind: '<kind>'")`, checked before anything else.
- **`start`** is required: a `datetime.datetime`, or ISO-8601 text read by
  `datetime.fromisoformat()` (`"2026-01-05T07:30:00+01:00"`, a trailing `Z`
  included). Either is stored as `iso_utc()` stores every source's session
  times, `YYYY-MM-DDTHH:MM:SSZ`: converted to UTC and cut to the whole
  second, a naive time taken to be UTC. `None` is `ValueError("a session
  needs a start")` — where a sync's `None` start quietly writes nothing, an
  app's is a bug, said. Anything else (a `date`, a number, text that won't
  parse) is `ValueError("start must be a datetime or ISO-8601 timestamp:
  <repr>")`.
- **`end`** is optional: the same types, normalised the same way (`end must
  be ...` for one that won't do), or `None`, the default, for a session with
  no end yet, stored as `NULL`. An end before the start, compared once both
  are normalised, is `ValueError("a session's end is before its start")`; an
  end at the start (zero-length, or inside the same second) is fine.
- **`summary`** is a `dict` or `None` (stored as `NULL`): the session's
  breakdown, its keys the kind's, as listed beside `SESSION_KINDS`
  (`asleep_minutes` and the stage minutes for sleep, plus `main_sleep`,
  `utc_offset_minutes`, the `stages` timeline and `brief_wakes` where the
  source has them; `type`,
  `duration_minutes`, `distance_m`, `avg_hr_bpm`, `calories_kcal` for a
  workout), leaving out what the app doesn't have. The host doesn't check
  the keys, but the whole must be plain JSON — `json.dumps(summary,
  allow_nan=False)` must take it, so no `datetime`, set, NaN or Infinity.
  Not a dict is `ValueError("a session's summary must be a mapping or
  None")`; a dict that isn't JSON, `ValueError("a session's summary must be
  JSON")`.
- **The id is `app:<id>:<kind>:<start>`**, so writing the same kind and start
  again replaces its end and summary (one row, as a re-synced night is);
  another start, or another kind, is another session.
- **Nothing is derived from a session**: `derived_metrics` holds metrics only,
  so `write_session()` notes no day in `ctx.written`, and an action that
  writes only sessions re-derives nothing (it's still committed). A session
  is read back with `ctx.sessions(kind)`, or through `GET
  /api/sessions/<kind>`, alongside every other source's and tagged
  `app:<id>`: a passthrough, no pick.

`render()`'s plain `Ctx` has no writer, so a render can't write.

**The action route, `POST /api/app/<id>/action`,** body `{"action",
"payload", "cfg"}` — or, from a file input, the same as `multipart/form-data`:
`action` a form field, `payload` and `cfg` form fields holding their JSON
(either may be left out; a `cfg` that isn't a JSON object is `{}`), and each
file under its input's key, set into `payload` under that key as a
`FileUpload` (a file and a `payload` entry of one key: the file). A body over
`MAX_UPLOAD_BYTES` (25 MiB, Flask's `MAX_CONTENT_LENGTH`, on every route) is
turned away before any of it is read: `413 {"ok": false, "error": "Request
too large (max 25 MB)."}`. The page sends a `payload` field only for a file
input with a toggle — `{"<toggle key>": true|false}` — and none otherwise, as
before toggles.

1. `404` for an id no app loads under; `400` for an app with no `handle()`,
   or a body whose `action` isn't non-empty text or whose `payload` (absent:
   `{}`) isn't a mapping. Nothing is run, and no file written anywhere.
2. `handle()` runs with a `WriteCtx` for the app, in one transaction.
3. **If it raises**, everything it wrote is rolled back, and the reply is
   `400 {"ok": false, "error"}`: unlike a render, a write fails visibly, and
   never as a 500. A `ValueError` is a refusal, its message put as the app
   (or the ctx) wrote it, so `raise ValueError("Enter your weight in kg")` is
   what the tile shows; anything else is a bug, `"<Type>: <message>"`, logged
   with its traceback.
4. **Otherwise** each day it wrote a reading to is re-derived on its own (as
   a webhook reading's day is: never one span over several, which would
   re-pick the days between under the current source roles), landing its
   writes and their pick together — a session has no day to re-derive, and
   is committed all the same; then the app is rendered afresh with `cfg` (the
   instance's config; not a mapping, `{}`), and the reply is `200 {"ok":
   true, "spec"}`, which the page redraws the tile from. A render that fails
   then is an error spec, as ever: the write stands.
5. **Either way**, every uploaded file's temp file is deleted (in a
   `finally`, so a crash or a refusal too).

**Precedence.** An app's readings take part in the derived pick like any
source's. `DEFAULT_PRIORITY` names the pull plugins, so an app ranks after
them; among the other sources (a webhook's device, the legacy `manual`) it's
by name. Making an app a metric's primary is a source role the user sets
(`PUT /api/source-roles/<metric>`). An app doesn't claim one on its first
write: that, and how apps should rank against other unnamed sources, waits
for a second weight source (the open item in `data-consolidation-model.md`
§10).

**An app's own metrics in the dataset.** They take part in everything a
canonical metric does — the derived pick (their one source wins every day), the
re-derive after a write — and the contract serves them while the app is
installed: the catalog lists them after the canonical metrics, each with `app`
(the id of the app that defines it) and `label`, `/api/coverage` covers them,
and `GET /api/metric/app:<id>:<key>` serves their points in the declared unit.
That's the effective vocabulary, canonical plus every installed app's
declarations, enumerated in one place (`custom_metric_defs(apps_dir)` in
`app/host.py`, over `scan_apps()`); `app/metrics.py` stays canonical-only.
Uninstalled, an app's metrics drop out of all three (the metric route 404s);
their readings stay under `app:<id>` (below). The webhook takes canonical keys
only, so nothing else can write one.

**Nothing else writes an app's source**: the webhook refuses a source that
starts `app:`. Uninstalling an app keeps what it wrote, as a disconnected
device's data stays, and an app installed again under the same id carries
on under the same source. A Duplicate is a different app, `app:<new-id>`.

### The weight app

The first writer, committed in `library/weight/` (deployed by
installing it: the platform ships no apps) and run by `test_app_actions.py`.

```yaml
id: weight          # fixed: its source is app:weight
name: Weight
sizes: [{ w: 1, h: 1 }, { w: 2, h: 1 }]
reads: [weight_kg]
```

`render()` shows the latest derived `weight_kg` as a `number` (kg, captioned
with its date and source), over an `input` (`key: weight_kg`, `action: log`,
"Log weight", kg). With no weigh-in yet, `empty("No weigh-ins yet")` takes the
number's place, so the first can still be logged. `handle()` takes `log` only:
it coerces `payload["weight_kg"]` to a finite positive number (junk, a bool,
`0` → `ValueError("Enter your weight in kg, as a positive number")`) and files
it for today with `ctx.write_metric("weight_kg", kg)`. One weigh-in a day: a
second the same day replaces it (more than one a day, timestamped, is a later
job for the instant grain).

It replaced `/api/weights` (and the console's weigh-in form before it): it's
now the one way weight is typed in. The weigh-ins typed in before it keep
their source, `manual`, frozen as history: read and picked like any source's,
but nothing writes to it (the webhook refuses it too). They aren't migrated:
an app can only write its own source, so it can't relabel `manual`'s rows,
and a core migration can't know an app's id. Per day there's no contention,
so the derived weight timeline runs on seamlessly, old days from `manual`,
new ones from `app:weight`.

### The caffeine app

The first app to invent a metric, committed in `library/caffeine/`
and run by `test_app_custom_metrics.py`.

```yaml
id: caffeine
name: Caffeine
sizes: [{ w: 1, h: 1 }, { w: 2, h: 1 }]
reads: [caffeine_mg]
metrics:
  - key: caffeine_mg
    label: Caffeine
    unit: mg
    category: Nutrition & hydration
    grain: daily
    range: [0, 2000]
```

OpenFit has no caffeine metric, so it declares `caffeine_mg`, and the host
keeps it as `app:caffeine:caffeine_mg`. `render()` shows the latest day's figure
as a `number` (mg, captioned "today" or its date) over an `input` (`key:
caffeine_mg`, `action: log`, "Caffeine today", mg) — `empty("No caffeine logged
yet")` before the first. `handle()` takes `log` only: it coerces
`payload["caffeine_mg"]` to a finite number, not negative (junk → `ValueError("Enter
today's caffeine in mg, as a number")`), and files it for today with
`ctx.write_metric("caffeine_mg", mg)`; the declared range is the host's to hold
it to (5000 → "caffeine_mg must be between 0 and 2000 mg, got 5000.0"). One
figure a day: logging again replaces it. Its metric shows in Your data under
Nutrition & hydration, marked `app`.

### The mood app

The first app with a dropdown (an input of kind `"select"`), committed in
`library/mood/` and run by `test_app_actions.py`.

```yaml
id: mood
name: Mood
sizes: [{ w: 1, h: 1 }, { w: 2, h: 1 }]
reads: [mood]
```

`mood` is canonical (Other, a 1–5 scale), so there's no `metrics:` block: the
app just writes the built-in key. `render()` shows the latest day's mood as a
`number` (captioned "today" or its date) over an `input` (`key: mood`,
`action: log`, `kind: "select"`, `options: ["1", "2", "3", "4", "5"]`, "Mood
today") — `empty("No mood logged yet")` before the first. `handle()` takes
`log` only: a value that isn't one of its five strings → `ValueError("Choose a
mood from 1 to 5")`, nothing written — the host doesn't hold a select to its
options, the app does — else `ctx.write_metric("mood", int(raw))` for today,
under `app:mood`. One mood a day: logging again replaces it.

### The import app

The first app with a file picker (an input of kind `"file"`), committed in
`library/import/` and run by `test_app_actions.py`: it loads past
weigh-ins from a plain-text file, and is the proof that the app parses, not
OpenFit.

```yaml
id: import
name: Import weigh-ins
sizes: [{ w: 1, h: 1 }, { w: 2, h: 1 }]
reads: [weight_kg]
```

`render()` shows how many days of `weight_kg` the dataset holds as a `number`
(`unit` days, `caption` "since <the first one's date>") over an `input`
(`key: file`, `action: import`, `kind: "file"`, `accept: ".txt,.csv"`,
"Import weigh-ins") — `empty("No weigh-ins yet")` before there are any.
`handle()` takes `import` only, and reads the `FileUpload` itself: no file →
`ValueError("Choose a file of weigh-ins to import")`; `f.text("utf-8-sig")`
(a spreadsheet's byte-order mark taken), and a file that isn't text →
"<name> isn't a text file: …"; then a weigh-in a line, `YYYY-MM-DD <kg>`,
apart by spaces or a comma, blank lines skipped — a line that won't parse →
`ValueError("line N: expected 'YYYY-MM-DD <kg>'")` before anything is
written, none at all → "No weigh-ins in <name>: …". Each is filed with
`ctx.write_metric("weight_kg", kg, date=day)` — on the day it names, not
today — under `app:import`; a weight the host won't take (outside
`weight_kg`'s plausible range) is refused with its line number, `"line N:
weight_kg must be between 20 and 400 kg, got …"`, and the route rolls back
the lines already filed. All or nothing, then; a day already there is
replaced, and a day twice in the file is its last line. It competes in the
pick like the weight app: a day Garmin weighed is still Garmin's.

### The workout-log app

The first app to write a session, committed in
`library/workout-log/` and run by `test_app_actions.py`: it logs a
workout just finished, and is the proof that `ctx.write_session()` lands an
interval under the app's own source.

```yaml
id: workout-log
name: Workout log
sizes: [{ w: 1, h: 1 }, { w: 2, h: 1 }]
reads: []
```

No `metrics:` block: `workout` is one of OpenFit's session kinds, so the app
just writes it. `render()` is deliberately dull — `empty("Log a workout with
Save")` over an `input` (`key: minutes`, `action: log`, "Workout", min) — and
doesn't read its workouts back (it was written before `ctx.sessions()`
existed, and stays as it is). `handle()`
takes `log` only: it coerces `payload["minutes"]` to a finite positive number
(junk, a bool, `0` → `ValueError("Enter the workout in minutes, as a positive
number")`), takes the workout to have ended now (`datetime.now(timezone.utc)`)
and started that many minutes before, and files it with
`ctx.write_session("workout", start, end, {"type": "manual",
"duration_minutes": minutes})`, under `app:workout-log`. Each log is its own
session (its own start); it shows in `GET /api/sessions/workout` beside a
watch's, and nothing is derived from it.

### The FRMS app

The first app to file duties, committed in `library/frms/` and
run by `test_frms_app.py` (its parsing on its own, the module loaded by
path as the host loads it) and `test_app_actions.py` (the round trip), against
a synthetic roster, `tests/fixtures/rosters/sample.ics`. It was the Roster
app until it became FRMS: the whole FRMS suite as one app, with one tile and
one `config.json`, drawn as four sub-panels on a 6×5 grid (`ctx.grid()`,
above): the circadian panel top-left (3×2) — three strips, below — the roster
panel bottom-left (2×2) — the timeline and its rails, below — the sleep
panel top-right (3×2, `_sleep(ctx)`: three strips three days across — each
night's stages, the body's rest window, and the disturbed cycles: each
90-minute slot of a flown flight or sim duty (not a reserve) in the rest window with under 15 minutes of
sleep in it) and the rest budget bottom-right (4×2, `_budget(ctx)`, below).
`render()` is the layout; the circadian
panel is `_circadian(ctx)` and the roster panel `_roster(ctx)`, its spec the one the
whole tile was before the grid. Each is built through `_panel(ctx, build)`,
which returns `build(ctx)` — or, if it raises, prints `[apps] frms: the
<panel> panel failed` and the traceback, as the host logs a failed render,
and returns `ctx.error(<its message>)` (its type's name for one with none):
**a fault in one panel is an error in its own sub-panel**, and the others
are drawn as ever.
All of its code is in its one `app.py`, by decision: the host imports
`app.py` by its path, adding nothing to `sys.path`, so a module beside it
couldn't be imported by name, and it reloads an app only when `app.yaml` or
`app.py` changes. The roster panel takes a duty roster's `.ics` through a
file input and parses it itself, with the standard library alone: the host
only carries the file.

```yaml
id: frms
name: FRMS
sizes: [{ w: 6, h: 5 }]
reads: [duties_rostered, duties_complete, sleep]
```

One size, 6×5, so no size picker. No `metrics:`: the app files sessions and
nothing else. Duty hours, when a report needs them, are read from the
sessions.

Every timed event (a `DTSTART`/`DTEND` in UTC) is a duty and an all-day one
a day off, whatever its code, so a duty code never seen before is still filed
(labelled `other`). Each is written with `ctx.write_session(kind, start, end,
summary)`: `kind` `duties_complete` if it had ended at upload, else
`duties_rostered`; `start`/`end` the `DTSTART`/`DTEND` datetimes; `summary`
`{capture, type, code, label, raw}` plus a flight's `flight_no`/`dep`/`arr`/
`position`. That's all it writes, so a UTC day with more than 24 hours of
duty is filed like any. A time with a `TZID` or none (floating) refuses the
upload, as does a file with no duty; the route rolls back whatever was filed.

**Backfill.** The picker carries a toggle, **Backfill** (`toggle={"key":
"backfill", …}`, its confirm "Backfill past duties into your rostered plan?
They're also recorded as flown, and any plan you already uploaded for those
duties is replaced."), for a roster uploaded after it started. `handle()`
reads `payload.get("backfill") is True` — only the checkbox's JSON `true`
turns it on — and files each duty by `filed_as(end, now, backfill)`, the write
matrix, `[(kind, capture)]`:

| | Backfill off | Backfill on |
|---|---|---|
| A duty already over | `duties_complete`, `capture: "live"` | `duties_complete`, `"live"`, **and** `duties_rostered`, `capture: "backfill"` |
| A duty still to come | `duties_rostered`, `"live"` | `duties_rostered`, `"live"` — unchanged |

`session_summary(duty, capture="live")` stamps it, so a reconstructed plan is
always told from one captured live. A backfilled duty's rostered session has
the id a live upload of it had, so it replaces that plan — the confirm's
warning. Off — unticked, or no payload at all — it's exactly the plain clock
split from before.

Its roster panel reads the duties back: `ctx.sessions()` of both kinds since about six
months before today (183 days, by the day a duty starts in UTC), and every duty
still to come. With none, it's `empty("Upload your roster .ics")` over the
picker. Otherwise it's a timeline (`ctx.timeline()`, above), between its rails (below), over the picker —
the upload at its sub-panel's foot: rostered duties in the left lane, headed
**Roster**, flown ones in the right, headed **Complete**, with
`grey_after_now`, each a block from its start to its end labelled
with its code, and the now line at the time of the render. The timeline fills
the rest of its sub-panel and scrolls, laid out to whichever size the
sub-panel is (`render()` isn't told it). It's synced into the `"frms"` focus group
(`sync="frms"`): a fixed cursor across its middle, the UTC time under it above
the labels, following now until it's scrolled, with a Now button to come back —
and any later FRMS panel naming the same group scrolls with it. A duty with no end, which only another source could
file, is left out.

Behind each lane's blocks, its **trips** away from home base
(`CONFIG["personal"]["home_base"]`), each a band (`bands`, above) labelled
with the port it flew out to. `trips(sessions, home_base, open_end=None)`, a
pure module function, groups one lane's duties: only flights with a route
(`dep` and `arr` in the summary) and an end count, taken by start — a reserve,
a sim, a ground duty or a routeless flight never opens or closes one. A flight
out of base opens a trip (from its start; `out_port` its `arr`; `id` its start,
the trip's stable id across re-uploads, kept on the trip for a later report
store and not put in the spec); the next flight into base closes it (at its
end); any sector between is inside it, so `HKG-BKK, BKK-SIN, SIN-HKG` is one
trip. A flight into base with no trip open (away before the data starts) is
nothing; a flight out of base while one is open means that trip's way home
isn't in the data, so it's dropped and the new one opens; a flight from base
to base opens nothing. A trip still away at the end closes at `open_end` —
never before its flight out has landed — or, with none, is dropped. The
rostered lane's `open_end` is the end of the loaded roster (its latest duty's
end), the flown lane's now (the same instant as the timeline's `now`): a trip
flown out and not yet back runs to the now line — you're on the layover. A
config with no `personal.home_base` shades no trips; the duties still draw.

**The Plan and Report rails** flank the timeline (`ctx.rails()`, above). The
cards are `trip_cards(rostered, flown, home_base)`: every trip out of base and
back, read from both lanes at once (a duty in both taken once, its flown copy),
so a trip under way — flown out, rostered home — is one card; a duty out of
base and back in one (a day return) a card of its own, its port and flight home
read back from its legs; the latest trip not home yet kept, open. A card's `in`
is the flight that lands at base: the last leg of the duty home, where the
roster numbers it (one duty home can fly `CEB-MNL / MNL-HKG`, stored under its
first sector's code), else that duty's code. They're split at the render's now:
**Plan**, beside the rostered lane, every trip with no end or ending after now
(the one you're on among them); **Report**, beside the flown, every trip home.
Each card is the trip's flights out and home and its port, straight through
(`_card()`: `{id, start, end, out, in, port}`; `in` `None` while it isn't
known, drawn as a dash), a day return like any, its id the trip's — the
band's id for the same trip, for a report to re-attach to. Plan is
`ctx.rail(plan, "plan", …)`, looking ahead of the cursor; Report is
`ctx.rail(report, "report", …)`, looking back. Both rails are in `"frms"`, so
they restack as the timeline scrolls. In its 2×2 sub-panel they show, with
the compact card, as in a 2×2 tile at full size.

A roster that's all over six months old is filed but draws as the empty state.

**The circadian panel** (`_circadian(ctx)`) draws the circadian engine
(`circadian()` and the functions under it: sun times, where you are, home's
day and night and WOCL, each duty cut against the body clock, the curve's
troughs — pure, and tested on their own in `test_frms_circadian.py`) as three
strips (`ctx.strips()`, above), in the `"frms"` focus group — so **scrolling
the strips scrolls the roster's timeline and rails, and the other way
round**, to one time. Over the roster panel's window: from midnight UTC 183
days before today to a day past the latest duty's end (or now, if later).
Before any duty it's `empty("Upload your roster .ics")`, as the roster
panel is; with no config, `empty(CONFIG_ERROR)`.

1. **Where you are** — the first lane, filling the height: `location`, each
   span its port's day or night (`none`, hatched, where it isn't known), its
   `place` the port (`""` for none) and its `tz` the port's in the config's
   `destinations` (`None` without one, or one that isn't text), the lane a
   clock — the tag the port and its local time under the cursor — with the
   body clock's troughs as its curve, and its `wocl` the engine's (02:00–06:00
   home local, each night): red under the curve, lined up with the duties'
   WOCL pieces below.
2. **Home** — a 45px row: `body`, day and night at home base, tagged Home
   over **home's date and time under the cursor** (`clock: "date"`, every
   span's `place` home base and its `tz` the config's `personal.timezone`:
   `Fri 23 Oct 20:00`).
3. **The duties** — a 45px row: `duties`, each duty's day, night and WOCL
   pieces. A flight's pieces start **two hours before its report** — the
   wake-up and travel to it, at home or away (the config's
   `sleep.pre_duty_minutes` to change it) — cut at the end of the duty
   before, so they never lie over it; a day return is one duty, so it has
   one; a sim, reserve or other duty starts at its report. The duty's code
   once — on its first piece in the window (a flight's at the start of its
   pre-duty time), not on the pieces that continue it (`prev`'s code and
   end the same as this piece's), so a duty clipped at the window's start
   labels its first piece in it. Behind them, **each trip away an amber
   bar**, its port in it: the lane's `bands`, the roster's own layover
   logic — `trips()`, unchanged,
   over the duties that count (`_counted()`: the flown over by now and the
   rostered ending after it, so a trip under way — flown out, rostered home
   — is one bar), from the flight out of base to the flight home landing,
   one still away running to the window's end; each `_band()`ed as the
   roster's timeline bands are. A day return opens no trip, so has none.

The engine's keys (`port`, `code`) are mapped, never passed through: the
builder takes only its own. A config without the positions the engine needs
(home base's `lat`/`lon`) makes `circadian()` raise, so the panel says so
in its sub-panel — the fixture's synthetic config, for one — and the roster
panel beside it is drawn as ever.


**The rest budget** (`_budget(ctx)`, bottom-right, 4×2) is how much of the
body's rest window the roster has cost, and is going to — drawn with the
dashboard builders (above), not synced: it's always now (`_now()`).

- `budget_slots(rostered, flown, nights, now, first, last, config=None)` →
  `[{night, slots}]`, a night a home-local date (`first` to `last`, dates or
  YYYY-MM-DD; a datetime or other text `ValueError`), its rest window
  starting that evening, in fixed slots from the window's start —
  slot_minutes, the last a part slot only if at least half a slot: seven a
  night by default, the same every night, so a night's share adds up
  (`disturbed_slots()`, the sleep panel's, follow each duty instead; the two
  are kept apart). Each slot `{start, end, cause, planned}`: `cause`, at its
  midpoint, the first that applies — `"duty"` (a duty of the sleep block's
  `duty_types` under way), `"pre_duty"` (the duty strip's pre-duty time,
  `_with_pre_duty()`), `"body_clock"` (away — `locations()`' port, its
  `destinations` tz — local time outside the same rest band: resisting the
  local clock), else `None`. A past duty or pre-duty slot with
  `min_sleep_minutes` of sleep in it (rest on board) is rested; a body-clock
  slot never asks the sleep — it's the roster's maths. A port not known, or
  with no tz, is home's. `planned`: not over by `now`. The duties are
  `_counted()`'s: flown in the past, rostered to come.
- `rolling_share(budget, now, days, since)` → `{red, total, days}`: the slots
  over by `now` starting in the last `days` days, on nights from `since`
  (the roster's first night — the evening before its first stored duty);
  `days` how many of them the roster covers.
- `month_share(budget, "YYYY-MM", since)` → `{flown_red, flown_rested,
  rostered_red, rostered_rested, nights}`, `nights` a row a night of the
  month, `{night, red, rested, planned_red, planned_rested}` — before
  `since`, zeros.

Drawn as `row([column([row([three donuts]), bars]), month's donut],
widths=[3, 2])`, the column `heights=[3, 2]`: the donuts 7 days, 30 days and 6
months, each `[good, crit]`, its figure the share red (`-` with none), its
note `N of D days` while the roster is younger than the window; the bars a
night of this month, `max` a night's slots, segments `[crit, crit planned,
good, good planned]` from the foot, a label every seventh day from the 1st,
the now line before the first night with a slot to come, titled `Disturbed
rest · <Month>`; the month's donut `[flown rested, flown red, rostered red,
rostered rested]` — flown solid first, then rostered hatched — its figure
the month's share red, flown and rostered together, its label the month,
note `F flown · R rostered of T`, key `["Flown", "Rostered"]`. Before any
duty, `empty("Upload your roster .ics")`.

**The flash.** Under the panels, at x 2, y 4, 4×1 — the tile 6×5 for it —
the OpenFit FRMS flash hugs the bottom right (`ctx.image(..., position="bottom
right")`, bare), the row's left end empty. Its images are `flash-light.txt` and
`flash-dark.txt` beside `app.py`, each a WebP `data:` URI (1500×400, from the
3000×800 PNGs in `docs/design/brand/` — the dark one's ground is the dark
`--bg`, `#0e1316`, and the light one's white is made transparent, so each sits
on the page), read once at load (`_read_flash()`):
either alone serves every theme; with neither, or one the host refuses
(logged), the cell is left out and the panels are drawn as ever.
## Installing an app

`POST /api/apps` installs an app from its files, and the Applications panel's
**＋ Add Application** popup is how a person calls it. It's the first part of
OpenFit that writes executable code to the volume from the browser.

The trust model doesn't change: an app is the owner's own trusted code, at
the same level as a sync plugin, and runs unsandboxed. So the install doesn't
defend against the app. It guards two real things: a malformed app breaking
the install or the host, and the install mechanics (a bad id or file name)
escaping the apps folder. It's **strict on containment and loose on
correctness**. All of it is `install_app()` in `app/apps.py`, and the server
re-checks everything, whatever the popup checked.

**The request** is JSON: `{"files": {"<name>": "<text>", ...}}`. Files are
text. The app's id isn't sent separately: it comes from the `app.yaml` among
the files.

**The steps, in order.** A refusal at any step writes nothing.

1. **An `app.yaml`** must be among the files, and must parse
   (`yaml.safe_load`) to a mapping. If it doesn't, the error says why, with
   the parser's message.
2. **Containment**, checked before anything touches the disk:
   - The manifest's `id` must match `^[a-z0-9][a-z0-9-]*$` (`INSTALL_ID`):
     lowercase letters, digits and dashes, starting with a letter or digit.
     It's required here, since there's no folder name to default to, and
     it's stricter than the scanner's rule, so an installed app always loads
     under its id. The match is anchored with `\Z`, so a trailing newline
     doesn't slip through.
   - Every file name must be a plain name (`filename_problem()`): not empty,
     not `.`, and with no `/`, `\`, `..` or control character in it, so none
     can be absolute or name anywhere outside the new folder. Every file's
     content must be text.
3. **A taken id is refused** with a 409: `'<id>' already exists — use Update
   in its row to replace its files, or remove it first to reinstall.` That's
   when `<apps dir>/<id>` exists as anything (a folder, a file, a broken
   link), or an app in another folder already loads under that id. An
   install only ever creates. It never updates an app in place: **Update**
   (`PUT /api/apps/<id>`, [below](#updating-an-app-put-apiappsid)) is the
   update path, the safe one-step form of uninstalling the app and adding it
   again.
4. **Correctness** is `check_app(manifest, files)`, and it's deliberately
   small. Past the parsable `app.yaml` with a valid id, its one rule so far
   is **a size**: the manifest must declare `sizes` (or the older
   `default_size`), each `w` and `h` a whole number from 1 to 6, or the
   install is refused with the reason the scanner would give ("won't deploy
   without a size"). Nothing else is checked: not the other manifest fields
   (`name`, `reads`, `config`), not `app.py`'s syntax, and nothing is ever
   imported or run. **This is the layer that grows**: as real apps' quirks
   are learned, a check that catches a broken app before it lands goes
   there. Containment is never moved into it, and it runs last, so a bad or
   taken id is reported first.
5. **The files are written** to a hidden staging folder in the apps
   directory (`.install-<hex>`, which the scanner skips), and it's renamed to
   `<id>` once they're all there, so the app appears whole or not at all. The
   apps directory is made if it doesn't exist yet. Installs are serialised by
   a process lock.
6. **The response** is `201 {"ok": true, "id": "<id>", "has_app_py": true|false}`.

A refusal is `{"ok": false, "error": "<message>"}`: 400 for the request, the
`app.yaml`, the id, a file name or the size, 409 for a taken id, and 500 if
the files can't be written (the staging folder is removed).

Past its size, a malformed but contained app installs. The scanner then handles it as it
would a hand-placed one: an app that doesn't load (a bad manifest field, an
`app.py` that won't import) is logged and left out of the list, and one whose
`render()` fails shows an error tile.

### The Add Application popup

**＋ Add Application** opens a popup that builds the app's folder as **file
slots**: `app.yaml` (required) and `app.py` (optional), each a paste box with
an **Upload** button beside it, which reads the chosen file into the same box.
**＋ Add file** adds a named slot (a name field, Upload and Remove) for
reference data or a helper. **Review** moves to the confirm step, which reads
the id from the `app.yaml` (there's no id field of its own) and shows
"Install application: `<id>`", the files, and, when any file is a `.py`, the
notice "This app includes custom code and runs with full access to your
OpenFit data." If the `app.yaml` is empty, has no `id:` line, or its id is
invalid, the confirm step says so instead and Install is disabled. When Home
has more than one tab, the confirm step also has an **Add to tab** dropdown
of them, the layout's default tab selected; with one tab (or before Home's
layout is read) it isn't shown, nor while Configure is on: Configure-off
saves the working copy, which doesn't hold the new app, so a pick would be
lost; the new tile joins the tab on screen and is moved like any tile.
Opened by
an app row's **Duplicate**, the popup is titled "Duplicate application" and
starts filled with the copy's files (`app.yaml` and `app.py` in their slots,
an added slot for each other file), all editable before Review.
Opened by an app row's **Update**, it's that app's update: titled "Update
application", filled with the app's files as installed (the same slots as a
duplicate, `app.yaml` untouched), with the app's id shown locked over them
(not a field: the id lives in `app.yaml`). Review holds `app.yaml`'s id to
the app's own, whatever its form (a hand-placed `My_App` included): a
changed or missing id is said there (This updates "<id>", but app.yaml's
id is "<other>" …), and Update is disabled. There's no tab to
pick (the app is placed already, and stays where it is). Review is the
confirm: under the id, the files and the custom-code notice, a panel asks
"Update <name>? This replaces the app's files. Its data is kept.", and only
its **Update** sends anything.

The popup's checks mirror the server's (the id charset and the file-name
rules), for quick feedback only. It reads the id from the top-level `id:`
line without a YAML parser, so it's a preview: the server's parse is the one
that counts. **Install** posts the files. On success the popup closes; if a
tab other than the default was picked, the page reads the layout (where the
server has already given the new app an instance at the end of the default
tab), moves that instance, with its id, size and settings, to the end of the
chosen tab, and saves the layout (`GET`/`PUT /api/home-layout`, the same
routes Configure uses; no route of its own). A read or save that fails
leaves the app on the default tab. Then the
Applications list and Home are re-read at once, so the app appears without a
reload, and the controls card notes "✓ Installed <name>", or, if the new app
didn't load, that the server's log says why. On a refusal the popup stays
open and shows the server's message as it's given, such as the taken-id
message or a YAML parse error. An update's **Update** `PUT`s the files to
`/api/apps/<id>` instead, and on success does the same: the popup closes, the
list and Home are re-read (the tile drawn by the new `app.py`) and the card
notes "✓ Updated <name>". A refusal (say, a new `app.py` that won't import)
stays in the popup, the app untouched.

Not in this slice: a zip or folder upload (it would need its own path
checks), and deeper correctness checks.

### Installing from a library: `install_library_app()`

`install_library_app(apps_dir, source, files)`, in `app/apps.py` beside
`install_app()`, installs an app from a library, its files as the library has
them. No route calls it yet: Browse will
([distribution.md](distribution.md)).

1. **The source** (`openfit`) must be lowercase letters and digits, starting
   with a letter (`LIBRARY_SOURCE`), or it's a 400: `a library source is
   lowercase letters and digits, starting with a letter: '<source>'`.
2. **The `app.yaml`** must parse, as an install's does, and its id must be
   one `install_app()` takes (`INSTALL_ID`), so bare: a manifest that already
   carries a source (`id: openfit.frms`) is refused. The app's id is
   `<source>.<id>`.
3. **Then exactly `install_app()`'s path, under the full id**: every file
   name and content checked, the id free (a 409 otherwise), `check_app()`,
   and the files written whole or not at all into `<apps dir>/<source>.<id>/`.
   Both go through one private helper, so `install_app()`'s behaviour and
   messages are as they were.
4. **It returns** `{"id": "<source>.<id>", "has_app_py"}` and logs
   `[apps] installed openfit.frms from the openfit library: <files>`.

The files are written as given, so the installed folder is byte-identical to
the library's copy: "is this install current?" is a plain file comparison.
A custom `frms` and the library's `openfit.frms` are two apps, installed side
by side, each under its own id and writing under its own source
(`app:frms`, `app:openfit.frms`; its metrics `app:openfit.frms:<key>`, its
sessions' ids `app:openfit.frms:<kind>:<start>`). A paste or a duplicate goes
through `install_app()`, whose `INSTALL_ID` has no dot, so it can never take
a library app's id.

## An app's row: reads, files, Refresh, Uninstall

Each row of the Applications list expands, the same way a Your data row does
(one mechanism, `expandable()` in the page, serves both lists): click the row
and its detail opens under it, the chevron turning; click again and it shuts.
An open row stays open when the list is re-read. The detail is three columns
(one on a phone), then the actions:

- **Reads**: the metric keys the manifest's `reads` declares (recorded, not
  enforced), or "No declared reads".
- **Files**: the files in the app's folder, by name. Clicking one opens it,
  read-only, in a popup: its text in a monospace block, set as text, never as
  markup. Nothing can be edited.
- **Writes**: a reserved slot for the writes ticker (Day / Wk / Mth), each
  `—` for now. An app can write back now (metrics under its own `app:<id>`
  source, [above](#writing-apps-as-sources)), but nothing is counted here
  yet.
- **Refresh** re-runs the app's render (`POST /api/app/<id>/render`, the
  existing route, with its tile's config) against the data as it is now, and
  redraws that app's Home tile in place from the fresh spec: no new route, no
  reload, nothing else repainted. (Every request scans the apps folder, so an edited `app.py` is
  picked up too, as it would be on any load.)
- **Update** replaces the app's files, keeping its id and its data: it reads
  every file back as Duplicate does (below; a file that can't be read stops
  it, said in the row) and opens the Add Application popup in its update mode
  ([above](#the-add-application-popup)) on them, untouched: change any, add
  one, remove a slot, then Review, confirm, and **Update** sends them all
  through `PUT /api/apps/<id>` ([below](#updating-an-app-put-apiappsid)). The
  folder becomes exactly what's sent, so a file is only lost if its slot is
  removed. An app with a file that isn't text (a 415 on the read back) can't
  be updated from here, since the update would delete that file. A library
  app has no Update (`GET /api/apps` lists its `library`): it's read-only
  here, updated in its library, and the server refuses its update with a 403
  ([below](#updating-an-app-put-apiappsid)). In Update's place its row says
  "From the openfit library · updated by your admin"; Refresh, Duplicate and
  Uninstall stay.
- **Duplicate** forks the app, from the page alone: it reads every file back
  (`GET /api/app/<id>/file`), gives the `app.yaml` the next free id and name
  (the lowest n from 2 with neither `<id>-n` nor "<name> n" installed, so
  `steps`/"Steps" → `steps-2`/"Steps 2", and a copy of a copy counts on from
  the same base; a library app's source is dropped first, so a copy of
  `openfit.frms`/"FRMS" is `frms-2`/"FRMS 2", a custom app of one's own),
  and opens the Add Application popup on the copy
  ([below](#the-add-application-popup)) to review and install like any app.
  The `app.yaml` is rewritten line by line (its top-level `id:` and `name:`
  lines; a name line added under the id if there's none), not parsed: the
  result is in the popup to edit, and the server parses it on Install. A file
  that can't be read (one that isn't text, 415) stops it, with a message in
  the row. Nothing is written until Install, which goes through `POST
  /api/apps`'s checks (a clash the page didn't foresee is the server's 409),
  and the copy then gets its own tile on Home. It's a fork: later edits to one
  app's files don't reach the other, and a copy writes under its own source,
  `app:<new-id>`, separate from the original's.
- **Uninstall** opens a confirm in the row ("Uninstall <name>? This deletes its
  files from `/data/apps/`.", with Cancel); confirming removes the app, and it
  leaves both the list and Home at once.

### Showing a file: `GET /api/app/<id>/file?name=<file>`

The file name comes from the request, so this read gets the installer's
containment discipline: nothing outside the app's own folder is ever read.
It's `read_app_file()` in `app/apps.py`.

1. **The app** is found by scanning, as the render route finds it. An id with
   no app that loads is a 404.
2. **The name** must be a plain file name (`filename_problem()`, the
   installer's rule: no `/`, `\`, `..` or control character, not `.`, so
   never absolute or pointing elsewhere), or it's a 400.
3. **It must be one of the app's files**: those `GET /api/apps` lists, which
   are the plain files directly in the folder (`app_files()`): not a
   sub-folder such as `__pycache__`, and not a link, which could point
   anywhere. Anything else is a 404.
4. **The path is resolved** and must still sit in the folder, and the file is
   opened without following a link, in case one was swapped in since the
   listing. Either failing is a 404.
5. **It must be text to show**: over 1 MiB (`MAX_VIEW_BYTES`) is a 413, and
   anything that isn't UTF-8 is a 415.

The response is `{"id", "name", "text"}`, the text exactly as it's stored, line
endings and all. A refusal is `{"ok": false, "error": "<message>"}`.

### Uninstalling an app: `DELETE /api/apps/<id>`

The first destructive action on an app. As with installing, the request names
the app, never a path. It's `uninstall_app()` in `app/apps.py`.

1. **The id** must be one an app could have (`is_app_id()`): the scanner's
   id rule, `APP_ID` (`^[A-Za-z0-9][A-Za-z0-9_-]*$`, with `\Z`), or a
   library app's, `LIBRARY_APP_ID` (`openfit.frms`), or it's a 400. Every id
   the list shows meets it, so a hand-placed app with capitals or `_` in its
   id can be uninstalled too, and so can a library app, and it can never be
   `..`, hold a `/`, or name a dot-folder. (Flask's routing never hands the
   route a `/` at all.)
2. **An app must load under it**, or it's a 404. The folder removed is the one
   the scanner loaded that app from: found by scanning, never built from the
   id, always a direct child of the apps folder. A hand-placed app whose
   folder isn't named for its id loses that folder. An app that doesn't load
   isn't listed, so it isn't installed as far as the Library knows; its folder
   is left alone (the alerts slice will surface it).
3. **The folder is moved aside first**: renamed to a hidden
   `.uninstall-<hex>` folder in the apps directory, in one atomic step, under
   the same process lock as installs. The scanner skips dot-folders, so the app
   is gone whole. If the rename fails, nothing has changed (500). The app's
   loader cache entry is dropped too, so a new app installed into the same
   folder name is always loaded afresh.
4. **Then it's deleted.** A folder that's a link is unlinked; what it points
   to is never touched. If the delete fails part way, only the hidden folder
   is left, and the log says so (`[apps] uninstalled <id>, but couldn't
   delete ...`): never a half-app the scanner would try to load. The app is
   uninstalled either way.
5. **The response** is `200 {"ok": true, "id": "<id>"}`.

Uninstalling removes only its folder: what the app wrote stays, under its
source `app:<id>`, as a disconnected device's data does.

### Updating an app: `PUT /api/apps/<id>`

The safe, one-step form of uninstalling an app and adding it again: its files
replaced with a new set, `{"files": {name: text}}`, its id and its data kept.
Install and uninstall composed: the new files are staged and checked, then
swapped in for the old folder, which is kept aside until the new one is in
its place. It's `update_app()` in `app/apps.py`, and the steps run in order;
everything up to the swap leaves the app exactly as it was.

1. **The id**, from the URL, must be one an app could have (`is_app_id()`),
   as uninstall's must, or it's a 400 ("not an app id: ..."). A library app's
   (`openfit.frms`) is a 403, nothing touched: `'openfit.frms' is from the
   openfit library, so it's updated there, not here — Duplicate makes a copy
   of your own to change`.
2. **The files** are checked as an install's are: an `app.yaml` among them
   that parses to a mapping; every file name plain and every content text
   (`filename_problem()`), or a 400.
3. **The manifest's id must be the app's own** (`_update_id()`): `this updates
   '<id>', but the files are for '<other>'`, a 400, otherwise; an `app.yaml`
   with no id, or one that isn't text, likewise. An update never renames an
   app, nor puts one app's files in another's place: Duplicate makes a copy
   under a new id. The id may be any the scanner loads (a hand-placed app's
   capitals and `_` included), not only an install's `INSTALL_ID`, since it
   names the app already there.
4. **The app must be installed**, under the same process lock as installs and
   uninstalls: an app must load under the id, and its folder is the one the
   scanner loaded it from, a direct child of the apps folder. Otherwise it's a
   404: `'<id>' isn't installed — use Add Application to install it`. A
   folder whose app doesn't load isn't installed as far as the Library knows,
   as for uninstall.
5. **`check_app()`**, the install's correctness layer (a size, so far).
6. **The files are staged** in a hidden `.update-<hex>` folder (the scanner
   skips dot-folders), written as an install's are (`_stage()`, which both
   use).
7. **The staged folder must load** as the scanner would load it
   (`load_app()`): the whole manifest (`name`, `reads`, `config`, `metrics`,
   which install leaves to the scanner), and `app.py` imported, with a
   callable `render` and, if it has one, `handle`. Unlike install, which runs
   nothing, an update imports the new code once here: a broken update would
   take down an app that works, where a broken install has nothing to break.
   One that doesn't load is a 400 (`the update doesn't load, so '<id>' is
   unchanged: <why>`), the staging folder removed.
8. **The swap**: the old folder renamed aside to a hidden
   `.update-old-<hex>`, then the staged one renamed into its place, each one
   atomic step. If the first rename fails, nothing has changed; if the second
   does, the old folder is renamed back. Either way it's a 500 and the app
   runs on its old files. The app's loader cache entry is dropped, so the next
   scan loads the new files, never the old ones' cache.
9. **The old folder is deleted.** A folder that was a link is unlinked; what
   it pointed to is never touched (the app is now a real folder of the new
   files). If the delete fails, only the hidden folder is left, logged
   (`[apps] updated <id>, but couldn't delete ...`); the update stands.
10. **The response** is `200 {"ok": true, "id": "<id>", "has_app_py": true|false}`.

**A replace, not a merge.** The folder afterwards holds exactly the files
sent: one left out is deleted. The popup starts filled with every file the
app has, so nothing goes unless its slot is removed.

**The data stays.** An update does no database work: what the app wrote under
`app:<id>` (readings, sessions, its own metrics' readings) is untouched, and
the new code reads and writes the same source. Its Home tile keeps its place,
its instance's settings, and its size if the new manifest still offers it
(else the app's default). A custom metric the new `app.yaml` no longer
declares drops out of the catalog, coverage and the metric route, as after an
uninstall, its readings kept under `app:<id>:<key>`; declared again, it's back,
history and all. Since the app is still installed, its source isn't orphaned,
so Remove data doesn't reach that metric while it's undeclared: declare it
again, or uninstall the app, to see or remove it.

## Routes

- `GET /api/apps` → `[{id, name, library, default_size: {w, h}, sizes: [{w,
  h}], reads: [key], config: [{key, label, type, default}], metrics: [{key,
  label, unit, category, grain, range}], files: [name]}]`, the apps that load,
  in folder order. `library` is the library a library app came from
  (`"openfit"` for `openfit.frms`), `null` for a custom app. `sizes` are the tile sizes the manifest declares, in its
  order, and `default_size` the first of them (for a manifest with the older
  `default_size` alone, `sizes` is that one). `reads` is the manifest's (`[]`
  if none); `config` its config fields, in order and as written (`[]` if
  none), the schema a tile's settings form is built from; `metrics` the
  metrics it invents, in order, by short key (`range` `[low, high]` or null;
  `[]` if none); `files` names the plain files in the app's folder, sorted,
  each one the file route serves.
- `POST /api/apps` → installs an app from `{"files": {name: text}}`; `201
  {"ok": true, "id", "has_app_py"}`, or a refusal that writes nothing. See
  [Installing an app](#installing-an-app).
- `PUT /api/apps/<id>` → updates the app from `{"files": {name: text}}`, the
  new set replacing its files, its id and data kept; `200 {"ok": true, "id",
  "has_app_py"}`, or a 400 (an id no app could have, a bad request, a file
  name, an `app.yaml` whose id isn't the app's, files that don't load), 403
  (a library app's, read-only here), 404 (no app loads under it) or 500 that
  leaves the app as it was. See
  [Updating an app](#updating-an-app-put-apiappsid).
- `DELETE /api/apps/<id>` → uninstalls the app, its folder and all; `200
  {"ok": true, "id"}`, or a 400 (an id no app could have) or 404 (no app
  loads under it) that removes nothing. See
  [Uninstalling an app](#uninstalling-an-app-delete-apiappsid).
- `POST /api/app/<id>/render` with `{"cfg": {key: value}}` → the app's spec,
  rendered now with that `cfg`, passed to `render()` as given. A body with no
  `cfg`, or one that isn't an object (or no body at all), renders with `{}`.
  It's a POST only to carry the cfg: nothing is written, and a GET is a 405.
  It's always a 200 for an installed app, with an error spec if the render
  fails. An id with no app that loads gets `404 {"ok": false, "error": "unknown
  app: <id>"}`.
- `POST /api/app/<id>/action` with `{"action", "payload", "cfg"}` → runs the
  app's `handle()` with a writing ctx scoped to `app:<id>`; `200 {"ok": true,
  "spec"}` (the app rendered afresh) once its writes (readings and sessions)
  are committed and its readings' days re-derived, or `400 {"ok": false, "error"}` with everything rolled back.
  404 for an unknown app, 400 for one with no `handle()` or a body that isn't
  an action. A file input's comes as `multipart/form-data`, each file handed
  to `handle()` as a `FileUpload` and deleted after; 413 for a body over 25
  MiB. See [Writing](#writing-apps-as-sources).
- `GET /api/app/<id>/file?name=<file>` → `{id, name, text}`, one of the app's
  files as text, for the read-only viewer. 400 for a name that isn't plain,
  404 for an unknown app or file, 413 for a file too big to show, 415 for one
  that isn't text. See
  [Showing a file](#showing-a-file-get-apiappidfilenamefile).
- `GET /api/home-layout` → `{"tabs": [{id, name, mode: "flow", subtitle,
  items: [{instance_id, app_id, size: {w, h}, config}]}], "default_tab"}`,
  Home's layout squared with the apps installed now; `PUT /api/home-layout`
  saves one. See [The layout](#the-layout-tabs-of-instances).

The access contract's metric reads cover the installed apps' own metrics too:
`GET /api/catalog` lists them after the canonical ones (`metric` the full key,
`app:<id>:<key>`, plus `app` and `label`), `GET /api/coverage` covers them, and
`GET /api/metric/app:<id>:<key>` serves one (404 once its app is uninstalled).

The access contract's reads also take **`?days=N`**: the trailing N days, as
`ctx.metric()` counts them. It can't be combined with `from`/`to`, and a value
that isn't a whole number from 1 to 999999999 is a 400. It narrows every
contract read (`/api/metric`, `/api/coverage`, `/api/sessions`,
`/api/series`) the same way, because they share one range parser.

## The Home tab

On load, the page calls `GET /api/apps` and `GET /api/home-layout` together,
then each app's render in parallel, and draws the tiles. A render that can't
be fetched becomes one error tile. A **Sync now** or a source removal re-reads
Home along with the data card, so the tiles follow the data, and so does
installing or uninstalling an app. An app row's **Refresh** redraws only that
app's tile. If either read fails, the page says so and offers a Retry. With
nothing installed, the page says where apps go.

Tiles are **chromeless**: the app's spec is the whole face, on the console's
card surface (`--surface`, `--border`, `--radius`, `--shadow`). A number sizes
its type to the tile, so the same app reads well at 1×1 or 4×2.

### The grid

Six columns of square cells. A cell is Home's width over six, less the gaps
(`--tile-gap`, 12px), held between `--tile-cell-min` (90px) and
`--tile-cell-max` (180px), all in the token block. Past the max, the grid
centres with margins either side. Under the min, Home scrolls sideways rather
than shrink the cells further: there's no phone layout yet.

The tiles go into a CSS grid (`grid-template-columns: repeat(6, ...)`, rows
as tall as the columns are wide) in the layout's order, each spanning its size
(`grid-column: span w; grid-row: span h`), with `grid-auto-flow: row`. So
they fill left to right; a tile too wide for the cells left in a row starts
the next row, and the gap it leaves at the row's end stays empty. It's never
`dense`: a later tile never backfills a gap, which would scramble the order.
The page doesn't pack anything itself: CSS grid does the columns, spans,
wrapping and scaling.

The layout is stored in cells, so a window resize only changes how many
pixels a cell is. Nothing reflows or moves.

### The layout: tabs of instances

One `settings` row, `home_layout` (no migration), `app/home_layout.py`:

```json
{ "tabs": [
    { "id": "home",
      "name": "Home",
      "mode": "flow",
      "subtitle": "<the line over the tiles>",
      "items": [
        { "instance_id": "3f9c0a1b2d4e6f70", "app_id": "steps",
          "size": { "w": 1, "h": 1 }, "config": {} }, ... ] }, ... ],
  "default_tab": "home" }
```

- **A tab is one flow layout**, plus an `id` (a short slug: up to 32
  lowercase letters, digits and dashes, not starting with a dash) and a
  `name` (its label on the tab strip, at most 40 characters). `default_tab`
  is one tab's id. The tab strip shows every tab, before Data, and Home
  draws the one picked (see [Tabs](#tabs)).
- **An item is an instance**: a placement of an app, not the app itself.
  It has its own `instance_id` (a token, unique across every tab), its own
  `size` (one of the app's declared `sizes`) and its own `config` (an
  object: the values of the app's config fields, set from the tile's gear in
  Configure, `{}` until any are, and passed to `render()` as `cfg`). The
  stored shape lets one app be placed more than once, on different tabs or
  on one, but the page never does: the same app placed twice is a copy of
  the app, its own app id (app-level Duplicate), so each app id
  has one instance and one config. The server mints instance ids: a save
  can leave one out.
- **`items` is ordered, and the order is the layout.** There are no
  coordinates in flow mode.
- **`mode` is per tab, and always `"flow"` for now.** It holds the seat for
  the grid (free cell placement), so that lands as a second mode, not a
  migration. There's no mode switch.
- **The subtitle** is per tab: text, at most 120 characters
  (`MAX_SUBTITLE`), empty for none.

**Reading it (`GET /api/home-layout`) reconciles it** with the apps installed
now, because apps are installed and removed out of band (the Library, or by
hand on the volume):

1. On each tab, the saved instances in order, less any whose app isn't
   installed (or doesn't load), whose id isn't valid, or whose id was already
   used on any tab (the first one stands).
2. Each at its saved size if its app still offers that size, else at the
   app's default, with its config.
3. Then every installed app with no instance on any tab gets one, with a
   fresh id at its default size, at the end of the default tab, in folder
   order. So every installed app always has at least one instance: there's
   no "installed but unplaced", and an app you install appears on Home.

A tab that isn't one, or has a bad or repeated id, is dropped; a name a save
wouldn't take reads as the tab's id. There's always at least one tab, and
`default_tab` always names one (the first, if the saved one is gone).

A read never rewrites the saved row: the next save stores the reconciled
layout. So an app uninstalled and installed again before then comes back
where it was, with the same instance id and config. (An update, which keeps
the app installed throughout, never leaves the layout at all.) It also
means an instance the read added has an id for that read only, until it's
saved.

**The layout from before tabs** was `{"mode": "flow", "subtitle", "items":
[{app_id, size}]}`. A row like that (or no row, or one that isn't a layout:
the row can also be written through `/api/settings`) reads as one tab,
`home`, named "Home", holding what that layout showed: the same apps in the
same order at the same sizes under the same subtitle, each an instance with a
fresh id and `config {}`. There's no migration: the next save stores the new
shape.

**Saving it (`PUT /api/home-layout`)** takes the whole layout as JSON and
stores it once its shape is checked: `tabs` a non-empty list, each with a
valid, unique `id`, a `name` that isn't blank and is at most 40 characters,
`mode` `"flow"`, `subtitle` text up to the limit and `items` a list. Each item
needs an `app_id` an app could have and a size of whole cells from 1 to 6;
`config`, if given, must be an object (left out, it's `{}`); `instance_id`, if
given, must be a valid token used once across the whole layout (left out, the
server mints one). The same app can be placed any number of times.
`default_tab` that names no tab is taken as the first. Anything else in the
body, a tab or an item is dropped. The server doesn't check which apps are
installed or what sizes they offer (the next read squares that, and gives any
app left with no instance one), and it never works out a position: the page
owns the flow. `200 {"ok": true}`, or a 400 that stores nothing.

### Tabs

Home's tabs sit at the left of the tab strip, one per tab of the layout, in its
order, each labelled with its name (set as text, never markup). Data, at the
right, isn't one of them: it has a panel of its own, and can't be renamed,
deleted or moved. Every layout tab shares the one Home panel; clicking one
draws that tab's tiles and subtitle. Switching fetches nothing: the page keeps
the apps, their specs and the layout from its last read, and redraws from them.
Each layout tab keeps the strip's own form (a `.tab` with `data-tab`, its name
its text), so Settings' **Opening page** lists Home's tabs beside Data.

Switching tabs is always on. Managing them is Configure's (below): outside it,
the strip is just the tabs.

### Configure

A sub-header under the tab strip holds the **subtitle** on the left and the
**Configure** toggle on the right, a tab styled like ＋ Add: dashed while off,
filled with the accent while on. Configure edits a working copy of the whole
layout: switch tabs while it's on and the tab you leave keeps your edits, and
nothing is saved until it's switched off.

- **On:** each tile gains a handle; beside it, if its app declares config
  fields, a settings gear; and, if its app offers more than one size, a size
  picker listing those sizes. The empty cells are marked faintly, a spare row
  sits under the tiles to drop one at the end, and the subtitle becomes an
  input.
- **Settings = the tile's gear.** It opens a form of the app's config fields
  (`GET /api/apps` carries them), in the manifest's order, each labelled with
  its `label` (else its `key`) and filled with the instance's value, else the
  field's `default`. A `number` field is a numeric input; any other `type` is
  text, for now (a select, a toggle and the like are to come). **Save** puts
  the values into the instance's `config`: a number as a number (blank, or
  not a finite number, falls back to the field's default, and with no
  default the key is left out); text as typed; a key the form doesn't show
  is kept. Then that tile alone is re-rendered with them (`POST
  /api/app/<id>/render`) and redrawn in place. There's no preview as you
  type: the re-render is on Save. The config is saved with the rest of the
  layout as Configure is switched off: no route of its own. Until then a
  refresh (a sync, an install) renders with the unsaved settings, and keeps
  them.
- **Move = drag.** Drag a tile (anywhere but its pickers and gear) onto
  another and it takes that tile's place in the order. The grid reflows live
  to show where it will land, and the drop keeps it. Into the gap at a row's
  end, it goes after that row's last tile; into the spare row, last. It's a
  sortable list, so there are no coordinates and no collisions. From the
  keyboard, a handle's arrow keys step its tile back (← or ↑) or on (→ or ↓).
- **Resize = the picker.** Choose a size and the tile takes it, the grid
  reflowing around it. There's no drag-to-resize, and the page never offers a
  size the app didn't declare.
- **Move to another tab = its Move… picker**, listing the other tabs. The tile
  leaves this tab's grid and goes on the end of that one, with its instance id,
  size and config. It's a move, never a copy: every app stays on exactly one
  tab. (A tab that already holds the app, which only a layout saved from
  elsewhere can do, isn't offered.)
- **Make a tab = ＋ Add**, on the strip while Configure is on: a name, then
  Create. The new tab is empty, goes on the end, and is shown. The page mints
  its id (eight lowercase letters and digits, the server's tab-id form), since
  it needs one at once; the server takes it as given.
- **Rename or delete a tab = its gear**, just after it on the strip. Its
  settings hold its name and, unless it's the only tab, **Delete tab**, behind
  a confirm that says where its tiles go. Deleting never uninstalls anything:
  the tab's instances go on the end of the default tab (or, if the default
  itself is deleted, the first tab left, which becomes the default). A name
  must not be blank and is at most 40 characters, checked before anything
  changes, and again by the server on the save.
- **Off:** the whole working copy is saved at once (every tab, as made, renamed,
  deleted and rearranged, with each tile's order, size, instance id and config)
  and the tiles go back to chromeless. Home is then read again, so it shows
  what the server stored and squared (an id it minted, any app left with no
  tile). If the save fails, Configure stays on with the error, nothing lost,
  to try again.

The subtitle, every tab name and every app name are set as text, never as
markup.

### The seams

The page keeps these parts apart:

1. `loadHome()` is the fetch: it reads the apps and the layout, then a spec
   for each app, rendered with its instance's config (`cfgByApp()`; one
   instance an app, so one config and one spec an app), and keeps them
   (`home.specs`, `home.layout`). Mid-Configure it keeps the working copy
   instead of the layout it read, and renders with the working copy's
   configs, the tab on screen flushed into it first. It runs at boot, after
   a sync, an install or an uninstall, and after Configure saves, but never
   on a tab switch.
2. `renderActiveTab()` is the render, from what the fetch left: the tab on
   screen (`home.activeTab`, or the default if that names no tab), its
   instances read into `home.tiles` (`{instance_id, app_id, name, spec, w, h,
   sizes, config}`, in layout order) and its subtitle, then the sub-header,
   the tiles and the strip (`paintTabStrip()`). Tiles are keyed by `app_id`,
   one an app, which is safe because every app is on one tab: a tile moves
   between tabs, it's never copied. That stays: the same app placed twice is
   a copy of the app under its own id (app-level Duplicate), never a second
   instance, so tiles are never keyed by instance.
3. `layoutTiles(items, apps, elsewhere)` is `flowLayout`: the page's copy of
   the server's reconcile for one tab. It squares the tab's instances with the
   apps read beside it, and an unsaved Configure with an app installed or
   removed meanwhile, carrying each instance's id and config. An app placed
   nowhere (not on this tab, nor among `elsewhere`, the apps the other tabs
   place) goes on the end with no instance id yet; the save mints one. Tests
   hold it to the server's.
4. `paintTiles(box, tiles, configuring)` draws them on the grid, with
   Configure's controls when it's on.

In Configure, the tab on screen's edits (its tiles' order and sizes, its
subtitle) live in `home.tiles` and `home.subtitle` until `flushTab()` writes
them into the working copy: before every switch, a move, a delete, and the
save.

The grid pass adds a second mode: it swaps `layoutTiles` for one that reads
saved positions and gives `paintTiles` start cells. The sizes, the CSS grid,
`renderSpec` and the scanner don't change.

## Not yet

- **The grid**: free cell placement, a second layout `mode` on the same sizes
  and CSS grid (coordinates, drop anywhere, deliberate gaps, collision).
  Resize stays the picker. (Home's layout, not the `grid` spec, which splits
  one tile into sub-panels.)
- **A phone layout**: under the minimum cell, Home scrolls sideways.
- **The rest of the Library.** It reuses `scan_apps()` and `/api/apps`. Its
  shell has landed: the Data tab's right-hand **Applications** column lists
  `/api/apps` by name (see
  [frontend-redesign-and-widget-model.md](frontend-redesign-and-widget-model.md)),
  Add Application installs one ([above](#installing-an-app)), and each row
  expands to what the app reads, its files, Refresh, Update, Duplicate and
  Uninstall ([above](#an-apps-row-reads-files-refresh-uninstall)). Browse,
  Build and Create are still placeholders; Browse will install from a library
  through `install_library_app()` ([above](#installing-from-a-library-install_library_app)),
  and `replaces`, a library app taking over an older app's id and data, is
  the next slice ([distribution.md](distribution.md)). Still to come: the writes ticker
  (an app can write back now) and alerts (an app that doesn't load, or whose
  render fails, flagged in its row; Update can't reach one, since it isn't
  listed). Disabling an app is out, and so are partial updates (one file,
  merged into the rest) and version history: an update replaces the whole
  folder, and keeps no copy of the old one once it's in.
- **Multi-tab Home**, beyond the tab strip ([above](#tabs)), tile settings
  ([Configure](#configure)), Duplicate
  ([an app's row](#an-apps-row-reads-files-refresh-uninstall), the way the
  same app ends up on Home twice, each copy with its own settings) and
  deploy-to-tab in the Add Application popup
  ([above](#the-add-application-popup); not offered while Configure is on,
  where the new tile joins the tab on screen and is moved like any tile).
  Tiles keyed by instance are
  dropped for good: with duplication at the app level, an app id never has
  two instances. (The stored layout keeps its tabs of instances, see
  [The layout](#the-layout-tabs-of-instances); the page just never places
  one app twice.)
- **Richer settings fields** (a select or enum, a toggle) beyond number and
  text, a live preview as a setting is typed, and a custom `app.js` for an
  app that draws its own face.
- **More write-back**: an app's own session kinds (a custom sleep
  detector's kind) and series — it writes sessions of
  OpenFit's kinds only (`ctx.write_session()`), and its own metrics are
  `daily`/`instant` only — a trailing `days=` window on `ctx.sessions()`,
  reading another app's metrics, timestamped weigh-ins more than one a day, and a
  `base_tz` setting for the day a write lands on.
- **Enforcing `reads`:** it's recorded, but `ctx.metric()` serves any
  canonical key, and the app's own, and `ctx.sessions()` serves any session
  kind.
- **A sandbox:** an app is trusted Python running in the server's process.
  The sandboxed host (no network beyond the contract) is Phase 4 of
  `ROADMAP.md`.
