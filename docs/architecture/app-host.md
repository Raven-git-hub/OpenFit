# The app host: installed apps and the Home tab

OpenFit ships no apps. The app host runs the ones you install and draws them
as tiles on the **Home** tab. Apps are found on disk and rendered
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
  app.py       render(ctx, cfg) -> a display spec (optional)
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
one-line manifest edit and a reinstall.

### Loading: `app/apps.py`

`scan_apps(apps_dir)` lists every app that loads, in folder-name order. The
same scanner backs `/api/apps` and the render route, and the Library will
reuse it.

- A folder with no `app.yaml` isn't an app, so it's passed over without a
  log line. So are dot-folders and loose files.
- A folder with an `app.yaml` that doesn't load is **logged and skipped**:
  `[apps] skipping <folder>: <why>`. That covers bad YAML, a field of the wrong
  type, no size (or one outside 1-6), an `app.py` that fails to import (its
  traceback is printed too), and a missing or non-callable `render`. One broken app never stops the others, and
  never takes the server down. A folder with no `app.py` isn't broken: it
  loads, with nothing to draw (above).
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
  `—` for now. It's filled in when an app can write back (`app:`-tier
  metrics); until then nothing is counted.
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
  app's files don't reach the other. Once apps can write back, a copy writing
  an app-defined metric would do so under its own `app:<new-id>:…` stream,
  separate from the original's: a decision to confirm when write-back lands.
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

An app writes no data yet, so uninstalling removes only its folder.

## Routes

- `GET /api/apps` → `[{id, name, default_size: {w, h}, sizes: [{w, h}],
  reads: [key], config: [{key, label, type, default}], files: [name]}]`, the
  apps that load, in folder order. `sizes` are the tile sizes the manifest
  declares, in its order, and `default_size` the first of them (for a
  manifest with the older `default_size` alone, `sizes` is that one). `reads`
  is the manifest's (`[]` if none); `config` its config fields, in order and
  as written (`[]` if none), the schema a tile's settings form is built from;
  `files` names the plain files in the app's folder, sorted, each one the
  file route serves.
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
- `GET /api/app/<id>/file?name=<file>` → `{id, name, text}`, one of the app's
  files as text, for the read-only viewer. 400 for a name that isn't plain,
  404 for an unknown app or file, 413 for a file too big to show, 415 for one
  that isn't text. See
  [Showing a file](#showing-a-file-get-apiappidfilenamefile).
- `GET /api/home-layout` → `{"tabs": [{id, name, mode: "flow", subtitle,
  items: [{instance_id, app_id, size: {w, h}, config}]}], "default_tab"}`,
  Home's layout squared with the apps installed now; `PUT /api/home-layout`
  saves one. See [The layout](#the-layout-tabs-of-instances).

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
  Create are still placeholders. Still to come: the writes ticker (once an
  app can write back) and alerts (an app that doesn't load, or whose render
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
  text, a live preview as a setting is typed, **metric write-back**, and a
  custom `app.js` for an app that draws its own face.
- **Enforcing `reads`:** it's recorded, but `ctx.metric()` serves any key in
  the vocabulary.
- **A sandbox:** an app is trusted Python running in the server's process.
  The sandboxed host (no network beyond the contract) is Phase 4 of
  `ROADMAP.md`.
