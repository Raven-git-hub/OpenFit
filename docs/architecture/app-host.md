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
- **`category`**: one of the eight categories (`CATEGORIES` in
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

**The reference apps.** The first two real apps, `steps` and `steps-metres`,
are committed in `tests/fixtures/apps/` as they're deployed in `/data/apps`.
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
that request's database connection.

- **`ctx.metric(key, days=None)`** is the app's only data path. It
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
| `ctx.input(key, action, label=None, kind="number", unit=None, options=None, accept=None)` | `{"type": "input", "key", "action", "label", "kind", "unit", "options", "accept"}` | A field under its label (small, uppercase), its unit and a **Save** button beside it. `kind` is one of `INPUT_KINDS`: `"number"` (the default), `"text"`, `"select"` — a dropdown of `options`, a non-empty list of non-empty strings, required for a select and refused for the other kinds (`options` is `null` in their specs) — or `"file"`, a file picker offering `accept`'s hints (`".txt,.csv"`, `"text/plain"`: optional non-empty text, refused for the other kinds — `accept` is `null` in their specs, and in a file's without one). Save, or Enter, sends `action` to the app's `handle()` with the payload `{key: what was entered}` (a number for kind `"number"`, text as typed for `"text"`, the chosen option's string as written for `"select"`, and for `"file"` the file picked, a `FileUpload` — Save only, sent multipart), and the tile is redrawn from the fresh spec the reply carries; a refusal shows under the field, which keeps what was entered. A blank field, or a number that isn't one, says so and sends nothing; so does a select still on its "Choose…" placeholder, a picker with no file picked ("Choose a file.") or one over 25 MB ("File too large (max 25 MB)."). The host doesn't check a select's value is one of its options (the action route remembers no render), nor what a file holds: `handle()` does. In a panel it sits at the foot. In Configure it's inert, like the rest of the face. See [Writing](#writing-apps-as-sources) and `docs/architecture/input-primitives.md`. |
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

**The writing ctx.** `handle()` gets a `WriteCtx(conn, app, today=None)`, made
for the app: a `Ctx` (so `ctx.metric()` and `ctx.today()` read as in
`render()`), plus

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
  `app/metrics.py`: `sleep` or `workout`. An app can't invent one yet: it
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
  (`asleep_minutes` and the stage minutes for sleep; `type`,
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
  is read back through `GET /api/sessions/<kind>`, beside every other
  source's and tagged `app:<id>`: a passthrough, no pick. An app can't read
  sessions itself yet (there's no `ctx.session()`).

`render()`'s plain `Ctx` has no writer, so a render can't write.

**The action route, `POST /api/app/<id>/action`,** body `{"action",
"payload", "cfg"}` — or, from a file input, the same as `multipart/form-data`:
`action` a form field, `payload` and `cfg` form fields holding their JSON
(either may be left out; a `cfg` that isn't a JSON object is `{}`), and each
file under its input's key, set into `payload` under that key as a
`FileUpload` (a file and a `payload` entry of one key: the file). A body over
`MAX_UPLOAD_BYTES` (25 MiB, Flask's `MAX_CONTENT_LENGTH`, on every route) is
turned away before any of it is read: `413 {"ok": false, "error": "Request
too large (max 25 MB)."}`.

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

The first writer, committed in `tests/fixtures/apps/weight/` (deployed by
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

The first app to invent a metric, committed in `tests/fixtures/apps/caffeine/`
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
`tests/fixtures/apps/mood/` and run by `test_app_actions.py`.

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
`tests/fixtures/apps/import/` and run by `test_app_actions.py`: it loads past
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
`tests/fixtures/apps/workout-log/` and run by `test_app_actions.py`: it logs a
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
doesn't read its workouts back (an app can't read sessions yet). `handle()`
takes `log` only: it coerces `payload["minutes"]` to a finite positive number
(junk, a bool, `0` → `ValueError("Enter the workout in minutes, as a positive
number")`), takes the workout to have ended now (`datetime.now(timezone.utc)`)
and started that many minutes before, and files it with
`ctx.write_session("workout", start, end, {"type": "manual",
"duration_minutes": minutes})`, under `app:workout-log`. Each log is its own
session (its own start); it shows in `GET /api/sessions/workout` beside a
watch's, and nothing is derived from it.

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
3. **A taken id is refused** with a 409: `'<id>' already exists — remove it
   first to reinstall.` That's when `<apps dir>/<id>` exists as anything (a
   folder, a file, a broken link), or an app in another folder already loads
   under that id. An install only ever creates. It never updates an app in
   place: uninstalling the app, then adding it again, is the update path.
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
message or a YAML parse error.

Not in this slice: updating an app in place (refused by design), a zip or
folder upload (it would need its own path checks), and deeper correctness
checks.

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
- **Duplicate** forks the app, from the page alone: it reads every file back
  (`GET /api/app/<id>/file`), gives the `app.yaml` the next free id and name
  (the lowest n from 2 with neither `<id>-n` nor "<name> n" installed, so
  `steps`/"Steps" → `steps-2`/"Steps 2", and a copy of a copy counts on from
  the same base), and opens the Add Application popup on the copy
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

1. **The id** must match the scanner's id rule, `APP_ID`
   (`^[A-Za-z0-9][A-Za-z0-9_-]*$`, with `\Z`), or it's a 400. Every id the
   list shows meets it, so a hand-placed app with capitals or `_` in its id
   can be uninstalled too, and it can never be `..`, hold a `/`, or name a
   dot-folder. (Flask's routing never hands the route a `/` at all.)
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

## Routes

- `GET /api/apps` → `[{id, name, default_size: {w, h}, sizes: [{w, h}],
  reads: [key], config: [{key, label, type, default}], metrics: [{key, label,
  unit, category, grain, range}], files: [name]}]`, the apps that load, in
  folder order. `sizes` are the tile sizes the manifest declares, in its
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
layout. So an app uninstalled and installed again before then (the update
path) comes back where it was, with the same instance id and config. It also
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
  Resize stays the picker.
- **A phone layout**: under the minimum cell, Home scrolls sideways.
- **The rest of the Library.** It reuses `scan_apps()` and `/api/apps`. Its
  shell has landed: the Data tab's right-hand **Applications** column lists
  `/api/apps` by name (see
  [frontend-redesign-and-widget-model.md](frontend-redesign-and-widget-model.md)),
  Add Application installs one ([above](#installing-an-app)), and each row
  expands to what the app reads, its files, Refresh, Duplicate and Uninstall
  ([above](#an-apps-row-reads-files-refresh-uninstall)). Browse, Build and
  Create are still placeholders. Still to come: the writes ticker (an app can
  write back now) and alerts (an app that doesn't load, or whose render
  fails, flagged in its row). Editing an app's files, updating one in place
  and disabling one are out: there's no mechanism for them.
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
- **More write-back**: an app's own session kinds (a roster's duties, a
  custom sleep detector's kind) and series — it writes sessions of
  OpenFit's kinds only (`ctx.write_session()`), and its own metrics are
  `daily`/`instant` only — reading sessions back (`ctx.session()`), reading
  another app's metrics, timestamped weigh-ins more than one a day, and a
  `base_tz` setting for the day a write lands on.
- **Enforcing `reads`:** it's recorded, but `ctx.metric()` serves any
  canonical key, and the app's own.
- **A sandbox:** an app is trusted Python running in the server's process.
  The sandboxed host (no network beyond the contract) is Phase 4 of
  `ROADMAP.md`.
