# OpenFit app authoring — lessons and conventions

Distilled from the first app-host build (PR #43: the `steps` and `steps-metres`
apps, the host, and the Home tab). This complements `app-host.md`, which is the
formal contract. This doc is the *why* behind the rules and the gotchas that
aren't obvious from the contract alone. Read both before building an app — or
before briefing a session to build one.

---

## The non-negotiable rules (these proved load-bearing)

### 1. An app reads only through the contract — never the raw tables or a vendor API.

An app's single data path is `ctx.metric()` (and the other `ctx` reads). It never
touches the `metrics` / `sessions` tables or Google / Garmin directly.

Why it matters, concretely: steps on 2026-10-12 had two raw rows —
`google_health` 5973 and `garmin` 3773 — and the contract returned one resolved
value (5973), tagged with its winning source. An app reading the raw table would
have had to dedupe and pick, and could double-count. Reading through the contract
means the app is automatically correct about source precedence, dedup, and units,
and stays correct when those rules change. The app shows what the contract picked,
and surfaces `via <source>` so the pick is visible rather than mysterious.

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
  Lives in `sessions`.

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
  lowercase letters, digits and dashes; an install never overwrites —
  **Uninstall** the app (its row in the Applications list), then add it
  again, to update it. The same row shows what it reads and its files, and
  **Refresh** re-runs its render after a change. See `app-host.md`,
  "Installing an app" and "An app's row".
- **First app is expensive, the rest are cheap.** The first app drags the whole
  host into existence. After that, a new app is a folder with two small files.

---

## The engine / display model

- **The engine is a stateless pure function:** `render(ctx, cfg)` → a display
  spec, computed from the dataset (via `ctx`) plus this instance's config. It holds
  no state, keeps no session, makes no outbound call.
- **The host draws the spec.** The app computes; the host renders from a fixed
  vocabulary of display primitives (`number`, `line`, `panel`, `empty`, `error`).
  Keep the vocabulary tight — add a primitive only when a real app needs it.
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
      category: Nutrition & hydration   # one of the 8 categories; Other is the catch-all
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

- **Only `daily` or `instant`.** An app's own session kind (a roster) or series
  is a later job; `session`/`series` fail to load.
- **Only your own.** Reading another app's custom metric isn't offered — read the
  canonical metrics and your own.
- **Its data outlives the app.** Uninstall the app and its metrics drop out of
  the catalog, but what it wrote stays under `app:<id>` until you remove it
  (Connected sources → Remove data). Reinstall it and the metric comes back,
  history and all.

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

## Known limitations (as of PR #43)

- Mobile caption truncation hides the `via <source>` provenance on phone-width
  tiles (no hover on touch). Acceptable while desktop is the target; revisit in the
  phone-layout pass. Provenance may eventually want to be its own element (a source
  badge) rather than caption text.
- `reads:` scopes are declared, not enforced.
- Home's layout is flow only (an order and a size per tile): no free cell
  placement yet, and no phone layout — under the minimum cell the grid scrolls
  sideways.
