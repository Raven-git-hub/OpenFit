# OpenFit app authoring — lessons and conventions

Distilled from the first app-host build (PR #43: the `steps` and `steps-metres`
apps, the host, and the Home tab). This complements `app-host.md`, which is the
formal contract. This doc is the *why* behind the rules and the gotchas that
aren't obvious from the contract alone. Read both before building an app — or
before briefing a session to build one.

---

## The non-negotiable rules (these proved load-bearing)

### 1. An app reads only through the contract — never the raw tables or a vendor API.

An app reads only through the ctx: `ctx.metric()` for a metric's daily values,
and `ctx.sessions()` for a session kind's intervals (a night's sleep, a workout,
a roster's duties). Each returns what its contract route serves
(`/api/metric/<key>`, `/api/sessions/<kind>`), computed by the same function. An
app never touches the `metrics` / `sessions` tables or Google / Garmin directly.

Why it matters, concretely: steps on 2026-10-12 had two raw rows —
`google_health` 5973 and `garmin` 3773 — and the contract returned one resolved
value (5973), tagged with its winning source. An app reading the raw table would
have had to dedupe and pick, and could double-count. Reading through the contract
means the app is automatically correct about source precedence, dedup, and units,
and stays correct when those rules change. The app shows what the contract picked,
and surfaces `via <source>` so the pick is visible rather than mysterious.

Sessions are not picked yet. `ctx.sessions(kind)` returns every source's sessions,
each tagged with its `.source`, so a night two devices both tracked appears twice.
The list is in source-priority order (the source the pick would trust first
leads), and each source's sessions are in start order, so the list as a whole is
not in time order. An app that wants one timeline sorts it by `.start`, and an
app that wants only its own sessions filters on `.source == ctx.source` in
`handle()`, or on `"app:<its id>"` in `render()`. Overlap and dedup logic, such
as treating two devices' views of one night as one event, belongs to the app,
from these tagged sessions.

### 2. The host↔app contract is a written spec, not an example to infer from.

Every type and shape the host hands an app, and every shape an app hands back, is
stated explicitly in `app-host.md` — not left to be read off an example.

Why: the first build's brief gave *examples* of manifests and `ctx` calls but left
return types implicit. A session rebuilding the host from that brief had to guess
the gaps, and guessed wrong three times — `ctx.metric()` returned a dict instead
of a list, `config` was required as a mapping instead of a list, and `line()`
omitted the `title` argument the app passed. Each guess compiled and tested fine
against the session's own assumptions, then surfaced as a runtime error a deploy
later. Precise types in the contract doc prevent this entire class of failure.

### 3. Commit reference apps as test fixtures — never let them live only on the volume.

The apps that exercise the contract (currently `steps`, `steps-metres`) live in
the repo under `tests/fixtures/apps/` and are run verbatim by the acceptance test,
so any host or `ctx` change that breaks a real app fails CI.

Why: the three round-trips above had one shared root cause — the real app files
lived only in `/data/apps/` on the deploy box, invisible to the repo, so a session
tested against its own reconstruction of them. Reconstructions drift. Real files
as fixtures close it: the acceptance test runs the genuine apps on every change,
and a contract break fails in CI instead of on the box three deploys later.

---

## The data model: three buckets (decide this first)

When an app stores or reads a value, decide which of three it is:

- **Config** — a fact that's true until changed (stride length, base timezone,
  which metric a tile shows, grid position). Lives in `settings`. Overwritten when
  it changes; the old value is discarded.
- **Metric** — a scalar measurement that belongs to a date (steps, weight,
  metres-per-day if persisted). Lives in `metrics`. A new value never erases the
  old — the old was true on its date.
- **Session** — a time interval with internal structure (sleep, a flight duty).
  Lives in `sessions`. An app files one with `ctx.write_session()` — of a kind
  OpenFit has (`sleep`, `workout`, a roster's `duties_rostered` /
  `duties_complete`); its own kinds are a later job — and reads them back,
  every source's, with `ctx.sessions(kind, since, until)`.

Two tests settle almost every case:

- *Does this number belong on a calendar?* Yes → metric or session. No → config.
- *When a new value arrives, does the old one become history or garbage?*
  History → metric / session. Garbage → config.

Worked example: a steps-to-metres app takes a stride length. Stride is **config**
(overwrite on change, `settings`), not a metric — even though it's a number —
because it isn't dated and its history is meaningless. The steps it multiplies are
a **metric**. If the app persisted metres-per-day, that persisted output would be
a new metric (dated, history kept) — one the app invents, so it declares it
([Defining your own metric](#defining-your-own-metric), below). Same app, values
in three different homes.

---

## App anatomy and where things live

- **The host is repo code; apps are volume data.** The scanner, `ctx`, routes and
  renderer are platform code in the repo. An app is a folder on the data volume at
  `/data/apps/<id>/`, outside the shipped image, persisting across rebuilds —
  because an app is the user's, per-instance, not shipped product. (Reference apps
  are *also* mirrored into `tests/fixtures/apps/` for CI; that copy is for tests,
  the volume copy is what runs.)
- **Files in an app folder:** `app.yaml` always (the manifest — the one file the
  host must parse to see the app, and which must declare the app's tile
  `sizes`); `app.py` whenever the app computes anything
  (optional: a manifest-only app loads and lists, but until the host ships
  built-in displays its tile only says it has nothing to draw); plus, for richer
  apps, helper modules, reference-data files (domain knowledge as *data* — a
  thresholds YAML, a lookup table — so it's swappable without touching logic),
  and the optional `app.js` escape hatch when the render-spec primitives can't
  express a visual.
- **Getting it there:** copy the folder onto the volume, or paste/upload the
  files in **＋ Add Application** (Data tab → Applications), which installs
  them as `/data/apps/<id>/`. The id comes from `app.yaml` and must be
  lowercase letters, digits and dashes; an install never overwrites.
- **Changing it: use Update — the data is kept.** **Update** in the app's row
  (Applications list) opens its files as installed; edit, add or remove one,
  review, confirm, and they replace the app's files in one step
  (`PUT /api/apps/<id>`). Everything the app wrote under `app:<id>` stays, its
  Home tile keeps its place and settings, and the new files are checked first
  — staged and loaded as the scanner would load them — so a bad paste (an
  `app.py` that won't import, a bad `metrics` block) is refused and the app
  keeps running on its old files. The id in `app.yaml` must stay the app's
  own (Duplicate makes a copy under a new one), and it's a whole-folder
  replace: a file whose slot you remove is deleted. Don't uninstall and
  re-add to update — that works, but Update is the safe path. The same row
  shows what the app reads and its files, and **Refresh** re-runs its render
  after a change. See `app-host.md`, "Installing an app", "An app's row" and
  "Updating an app".
- **First app is expensive, the rest are cheap.** The first app drags the whole
  host into existence. After that, a new app is a folder with two small files.

---

## The engine / display model

- **The engine is a stateless pure function:** `render(ctx, cfg)` → a display
  spec, computed from the dataset (via `ctx`) plus this instance's config. It holds
  no state, keeps no session, makes no outbound call.
- **The host draws the spec.** The app computes; the host renders from a fixed
  vocabulary of display primitives (`number`, `line`, `panel`, `input`, `timeline`,
  `empty`, `error`).
  Keep the vocabulary tight — add a primitive only when a real app needs it.
- **Sync timelines by naming a group; the host does the rest.** A timeline that
  should scroll with others — the FRMS panels: the roster now, sleep and the
  like later — passes `sync="<group>"` to `ctx.timeline()` (the roster uses
  `"frms"`). That's the app's whole part: the page draws the fixed cursor, the
  UTC readout and the Now button, keeps the group's focus time and whether it
  follows now, and scrolls every timeline of the group to that time by its own
  scale. The focus is UI state — in memory, not in the engine, `app.yaml` or
  the database — so `render()` never sees or returns it, and stays a pure
  function of the data. Two apps using one group name are synced, whether or
  not they meant to be: pick a name for the set of panels that belong together.
- **Work out the spans yourself; the host just shades them.** A lane's `bands`
  on `ctx.timeline()` are spans drawn behind its blocks — the host checks
  their times and the page shades them, nothing more. What a band *means* is
  the app's: the roster groups its own duty sessions into trips away from
  home base (`trips()`, a pure module function its tests call directly — a
  flight out of base to the next flight back, the sectors between inside it,
  only flights counting) and hands each trip's start, end and port over as a
  band. Anything the app needs beyond the drawing — the trip's stable id, for
  a report to re-attach to later — stays on its own data, never in the spec.
  A lane with no bands is the timeline from before them, key for key.
- **A multi-sector duty's route is its first port to its last.** One duty
  can fly several legs — a day turn out and back (`CX 903 HKG-MNL / CX 902
  MNL-HKG`), or on from one port to another — so its `dep`/`arr` are the
  first leg's departure and the last leg's arrival, read from the whole
  chain of legs (the description's, which lists every sector, before the
  title's, which may abbreviate). Reading only the first sector made a day
  turn look like a flight out that never came back: a phantom layover
  running to now. Feed `trips()` where a duty started and ended, never
  where its first sector landed.
- **The spec is a transient message, not a file.** Generated per render, drawn,
  and gone. Nothing in the app folder is the "display"; the only display-side file
  is the optional `app.js`.
- **User input is a new cycle through the host, never a wire back into the
  engine.** Input from a tile goes to the host, which re-runs the engine with that
  input folded into `cfg`. This is what keeps the engine stateless and cacheable.
- **State lives in the database, never in the app folder.** The folder is the
  app's definition — identical for every user, effectively read-only. Per-instance
  config, tile position, and any persisted results live in the DB.

---

## Trust model (current)

Apps run as trusted, owner-authored Python in the server process — the same trust
level as a sync plugin. No sandbox. A manifest's `reads:` list is recorded but not
enforced yet. Audit an app the way you'd audit a plugin. (Untrusted third-party
app sharing, if it's ever built, is a separate sandboxed mechanism — not this.)

---

## Workflow gotchas (banked from the first build)

- **Look before you spec.** Inspect the real data shape before writing code against
  it. The stored sleep session carried a UTC instant (good) but only five stage
  fields; the steps metric is source-tagged and gappy. Guessing the shape costs a
  round-trip.
- **Read the traceback; don't diagnose ahead of it.** Two wrong guesses in the
  first build (a dict-vs-list cause, and a "it's just the startup race" assumption)
  were settled only by the actual error line. Get the message first.
- **Mind the deploy startup race.** `docker compose up -d --build` returns before
  Flask is listening; a check fired immediately gets a connection-refused that
  looks like a bug. Wait a few seconds (`sleep 4`) before hitting the server.
- **Partial-today is real.** The latest day's value may be incomplete; label it
  "so far today" rather than presenting a half-day as a finished figure.
- **Real data has gaps.** Days go missing (sync gaps, device not worn); a line must
  render a missing day as a gap, not a zero, and cope with one point or none.
- **Verify against real apps before merging.** The acceptance test now does this
  automatically; a human should still load Home in a browser against the real apps
  before merging.

---

## Defining your own metric

OpenFit's vocabulary (`app/metrics.py`) is what devices measure. An app can go
further and **invent a metric** — caffeine, a focus score, cups of coffee — and
write it like any other. The rule is **declare what you invent, write what
exists**:

- **A built-in metric** (`calories_in`, `weight_kg`) is just written:
  `ctx.write_metric("calories_in", 2100)`. Don't declare it — the manifest
  refuses a key OpenFit already has, so a custom metric can never shadow a
  built-in one.
- **A metric you invent** is declared in `app.yaml`'s `metrics:` block, then
  written by its short key:

  ```yaml
  metrics:
    - key: caffeine_mg            # letters, digits, - and _; once per app
      label: Caffeine             # what the data console calls it
      unit: mg                    # your unit: every reading is in it
      category: Nutrition & hydration   # one of the 9 categories; Other is the catch-all
      grain: daily                # daily or instant
      range: [0, 2000]            # optional: a write outside it is refused
  ```

  ```python
  ctx.write_metric("caffeine_mg", 95)     # today
  ctx.metric("caffeine_mg", days=7)       # read back, like any metric
  ```

**The manifest is the metric's identity; the code is where its value comes
from.** The declaration says *what* the metric is — its name, unit, category and
grain — and is the same for every install. `handle()` says *where a value comes
from* — typed in, computed, read off something else. Keep the two apart: the host
never guesses a unit or a category, and your code never states them.

**Short key in the app, full key in storage.** Your code only ever says
`caffeine_mg`. The host stores it as `app:<id>:caffeine_mg` (under your source,
`app:<id>`), and that full key is what the catalog lists and
`GET /api/metric/app:<id>:caffeine_mg` serves. So two apps can each declare a
`caffeine_mg` without colliding — they're two metrics — and no app can write
another's: a ctx knows only its own app's declarations.

**The shape is fixed per key.** Pick the unit once. Changing it later doesn't
convert what's stored: invent a new key instead (`caffeine_g`). The same goes for
the grain.

What a declaration can't be yet, and why:

- **Only `daily` or `instant`.** An app's own session kind or series is a
  later job; `session`/`series` fail to load. Writing a session of a kind
  OpenFit already has (`sleep`, `workout`, `duties_rostered`,
  `duties_complete`) is supported now: no declaration, just
  `ctx.write_session(kind, start, end, summary)` — write what exists. The
  reference apps are `tests/fixtures/apps/workout-log/` and, from a file,
  `tests/fixtures/apps/roster/`.
- **Only your own.** Reading another app's custom metric isn't offered — read the
  canonical metrics and your own.
- **Its data outlives the app.** Uninstall the app and its metrics drop out of
  the catalog, but what it wrote stays under `app:<id>` until you remove it
  (Connected sources → the app's row: Remove all data, or open it and remove
  one metric's). Reinstall it and the metric comes back,
  history and all. An **Update** keeps all of it too; if the new `app.yaml`
  stops declaring a metric, that metric drops out of the catalog the same
  way, readings kept, and comes back when it's declared again (while the app
  is installed its data isn't orphaned, so Remove data can't reach it).

A bad block — an unknown category, a built-in key, a key twice, a misspelt field
(`ragne`) — means the app doesn't load, logged like any manifest error. The
reference app is `tests/fixtures/apps/caffeine/`.

## Sizes: the app declares them, the host never guesses

Home is a grid six square cells across. An app's manifest lists the tile
sizes it can be drawn at, in cells — `sizes: [{w: 1, h: 1}, {w: 2, h: 2}]`,
each `w`/`h` 1–6, the first the default — and the owner picks among them in
Configure. It's required: an app with no size won't install and the scanner
skips it, because only the author knows the shapes their spec reads well
at. (`default_size: {w, h}`, the older form, still counts as a list of one.)

- **Design for each size you declare.** A number reads at 1×1; a number over
  a line wants room (2×2, or a 1×1 if the caption is short). Don't offer a
  size the spec looks broken at — leave it out.
- **YAML needs a space after each colon.** `{w:1,h:1}` parses as the keys
  `'w:1'` and `'h:1'`, both null; write `{w: 1, h: 1}`. The install says so.
- **A fixed-size app lists one size**, and gets no picker.

## Config and reference data: an install file, read at import

An app folder can hold more than `app.yaml` and `app.py`: any other file is
the app's own, installed, listed and updated with it. Put what's specific to
a domain or an owner there, as data — a carrier's duty codes, a home base, a
lookup table — and keep `app.py` the engine, so adding a code or a carrier
is a config edit, never a code change. The Roster app is the reference: its
`config.json` holds the duty-type rules (`prefixes` or a `regex` per type,
the first match winning, else `default_type`), the owner's home base and
timezone, and a destinations table (each port's city and IANA zone).

- **Read it once, at import, from beside the module** —
  `os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")`,
  never the working directory or a hard-coded `/data/apps/<id>/`. The host
  imports `app.py` by its path, so `__file__` is wherever the files are: the
  installed folder when the scanner loads it, and the staging folder when
  Update loads the new files before swapping them in.
- **It's an install file, not state.** Change it with Update (or by
  reinstalling); the app never writes it. Update reads every file of the app
  into its popup, so the config travels with it, and Update reloads the app.
  A hand edit on the volume isn't picked up by itself: the scanner's cache
  watches only `app.yaml` and `app.py`, so it waits for one of those to
  change or a restart.
- **Fail safe: the module must still import.** Catch a missing file, bad
  JSON and the wrong shape at load, and keep the config `None` and a
  plain-English reason, never raise. An `app.py` that raises at import
  doesn't load: the scanner skips it (its tile and row go, the reason only
  in the server log), and Update needs a loading app, so the bad config
  couldn't be fixed in place.
- **Guard at the edge, not in the helpers.** `render()` returns the reason
  (`ctx.empty(...)`) over whatever input still makes sense; `handle()`
  raises `ValueError(reason)` before it reads the payload, so nothing is
  written. Pure helpers (`classify()`) take the config as an argument,
  defaulting to the module's, and assume it loaded — their callers have
  checked, and a test can hand them any config it likes.
- **Keep personal data out of the repo.** The committed fixture's config is
  synthetic (fake ports `ZZA`/`ZZB` and just the rules the tests need); the
  owner's real one lives only on their volume.

## A file upload can carry a toggle — and confirm first

A choice that goes with a file — *how* to file it — is a companion checkbox
on the file input, not a second input or a second button:
`ctx.input("file", "upload", kind="file", accept=".ics", toggle={"key":
"backfill", "label": "Backfill", "confirm": "…"})`. The Roster app's
**Backfill** is the reference.

- **It arrives in the same payload.** `handle()` gets `{<input key>:
  FileUpload, <toggle key>: True/False}` — a JSON bool, sent ticked or not.
  Hold it to that: `payload.get("backfill") is True`, so a `"false"` string
  from a hand-made call can't turn it on. A page from before the toggle
  sends no payload at all, so a missing key means off.
- **Off must be exactly what it was before.** The roster's matrix lives in
  one pure function, `filed_as(end, now, backfill)`, which both the session
  writes and `daily_hours()` read, so a past duty backfilled into the plan
  is counted in the rostered hours too and the two can't drift. Off, it's
  the plain clock split, key for key.
- **Leave a paper trail.** What the toggle files differently should say so in
  its summary — the roster's backfilled copy is `capture: "backfill"`, never
  `"live"` — so a reconstructed record is always told from a captured one.
- **Confirm when it can overwrite.** A `confirm` text makes the page ask
  (its own modal, never the browser's `confirm()`) before it sends with the
  box ticked; unticked, Save sends at once. Write it for the worst case, in
  plain words — it's static, fixed at render, before the app has read the
  file, so it can't count what's in it. Backfill's warns that a plan already
  uploaded for those duties is replaced (a session of the same kind and start
  is the same row).
- **Put the upload at the foot.** A tile whose input feeds what it draws
  reads better with the drawing first and the input under it: the roster's
  panel is `ctx.panel(timeline, picker)`, and its empty states keep the
  picker last too.

## Known limitations (as of PR #43)

- Mobile caption truncation hides the `via <source>` provenance on phone-width
  tiles (no hover on touch). Acceptable while desktop is the target; revisit in the
  phone-layout pass. Provenance may eventually want to be its own element (a source
  badge) rather than caption text.
- `reads:` scopes are declared, not enforced.
- Home's layout is flow only (an order and a size per tile): no free cell
  placement yet, and no phone layout — under the minimum cell the grid scrolls
  sideways.
