# Apps-as-source — design reference

_Apps as a data source: the write capability, the weight app, app-defined
metrics, and the data lifecycle. Drafted Oct 16 2026; records the decisions and
current build state._

## Decisions made

1. **Scope reversal — OpenFit does not gatekeep what is "health" data.**
   OpenFit stores what the user and their apps choose to keep: device readings,
   app-written canonical metrics, app-defined keys, **and** operational context
   (rosters, shift calendars, rest windows). Whether a datum is "health" is not
   OpenFit's call. Collation-not-analysis rule unchanged. Landed on main (#56 +
   folded into #55; the data-consolidation doc tier cleanup finished in 3243c75).
   Re-opens roster-as-data (duty periods as sessions) for the fatigue tracker.

2. **Apps-as-source** — the write capability, built around a **weight-entry
   app**. **LANDED as #57** (merged to main 0652c06). Apps can now write via a
   scoped action route + WriteCtx (source hard-fixed to `app:<id>`); a new
   `input` spec element makes tiles interactive. The weight app (id `weight`,
   source `app:weight`) is the first writer.

## Settled model — "manual" is retired, and so are the tiers

(Landed in docs via #55/#56 — retires the `device/manual/app` tier wording.)

- **An app is a source, full stop.** No tiers, no observed-vs-computed flag.
- **"written by an app" is the SOURCE, not the key.** App sources tagged
  `app:<id>`; `source = app:*` is the "what have my apps added?" filter.
- **Canonical keys stay canonical.** The weight app writes `weight_kg` under
  source `app:weight`. A key is only `app:<id>:<key>` when the app invents a new
  concept — **built (B0)**: see [App-defined metrics](#app-defined-metrics-b0--built), below.
- **"manual" retired as a concept** (cadence, tier) and as a write path (tile +
  `/api/weights` removed in #57). But the **historical `manual` weigh-in rows
  remain** as a frozen legacy source — slice B **froze** them (did NOT migrate),
  because an app can't rewrite another source's rows and a core migration can't
  know a user-chosen app id. Freezing keeps the history and the derived timeline
  seamless (old dates from `manual`, new from `app:weight`).

## Precedence — the real behaviour

`derived.py` sort key: a metric's `source_roles` primary first (-1), then
`DEFAULT_PRIORITY = [garmin, google_health]` in order, then **every unnamed
source tied, broken alphabetically by name**.

- Apps rank after garmin/google (can't silently outrank them).
- Among unnamed sources (apps, webhook pushes like `eufy`, frozen `manual`) it's
  alphabetical — an `app:...` source sorts early and can outrank `eufy`/`manual`
  unless the user sets `source_roles`. Arbitrary default; **open question**: apps
  sort after other unnamed sources by default, or surface contention?
- A write-app may want to claim `source_roles[metric]` on first write (so a
  device weight source doesn't silently outrank it). Deferred; decide with the
  ordering question.

## App-defined metrics (B0) — built

**Declare what you invent, write what exists.** An app lists the metrics it
invents in its `app.yaml`'s `metrics:` block, an ordered list mirroring
`config:`; a canonical metric it merely writes (`calories_in`) isn't declared.

- **Each entry** is `{key, label, unit, category, grain}` plus an optional
  `range: [low, high]`, and nothing else (a misspelt field fails, so `ragne`
  can't silently mean no range). `key` is an app-id-shaped slug, unique in the
  app, and never a key OpenFit's vocabulary already has — metric, series metric
  or session kind — so a custom key can't shadow a built-in one. `label` and
  `unit` are non-empty text; `category` one of the 9 `CATEGORIES`; `grain`
  `daily` or `instant` (`session`/`series` fail: not B0's). `range` is
  enforced like a canonical one when given; without it any finite number goes.
  Parsed by `_metrics()` in `app/apps.py`, carried on `InstalledApp.metrics` and
  listed by `/api/apps`. A bad block means the app doesn't load — logged and
  skipped, as a bad `config:` is (install doesn't check it: `check_app()` still
  checks only the size).
- **Short key in the app, full key in storage.** The app's code says
  `caffeine_mg`; the host stores `metric = app:<id>:caffeine_mg` with
  `source = app:<id>` (`custom_metric_key()` in `app/host.py`). A canonical key
  passes straight through. So two apps declaring the same short key keep two
  metrics.
- **The effective vocabulary** is canonical `METRIC_DEFS` plus every installed
  app's declarations. `app/metrics.py` stays canonical-only and pure;
  `custom_metric_defs(apps_dir)` (`app/host.py`, over `scan_apps()`) is the one
  place customs are enumerated, and the call sites with app context use it: the
  catalog (customs after the canonical metrics, each with `app` — the defining
  app's id — and `label`), `/api/coverage`, and `/api/metric/<metric>` (a
  current custom's full key serves its points in its declared unit; anything
  else 404s).
- **The ctx is made for the app.** Both the render ctx and the write ctx carry
  the `InstalledApp`. `ctx.metric(key)` reads a canonical key, or one this app
  declared (from `app:<id>:<key>`); anything else — another app's key, by
  either form — is a `ValueError`. `ctx.write_metric(key, …)` writes a
  canonical key (its unit, its plausible range) or one this app declared (its
  full key, its declared unit and range); anything else is
  `ValueError("unknown metric: … - declare it in app.yaml's metrics to write
  it")`, nothing written. So an app writes only its own customs and canonical
  keys.
- **Reference app:** `tests/fixtures/apps/caffeine/` declares `caffeine_mg`
  (mg, Nutrition & hydration, daily, 0–2000) and logs it from the existing
  number input; `tests/test_app_custom_metrics.py` runs the loop.
- **The console** names a custom row by its label, marks it `app`, and lists its
  one source from the catalog (it has no source role).

Out of B0: custom session kinds and series, new input types, cross-app custom
reads through the ctx, autonomous/background compute, and purging a custom
metric (B1, below).

## Data lifecycle — removing app data (settled Oct 16; A, B0 and B1 built)

Two distinct cleanups, both wanted. Guardrails for both: only ever for a source
**with no live app behind it** (never a live app's data); a guarded confirm
naming what's deleted; irreversible (hand-entered data can't be re-synced);
built-in/default metrics themselves are never purgeable.

- **(A) Remove an orphaned source's DATA from a (built-in) metric.** A source
  `app:<id>` (or the legacy `manual`) that has rows but whose app isn't installed
  -> offer "remove data". Deletes that source's rows (metrics/sessions/samples),
  clears any `source_roles` entry pointing at it, and re-derives the affected
  dates. The **metric stays** (e.g. `weight_kg` remains, possibly fed by other
  sources or empty). **Reuses the connector-disconnect delete+re-derive path.**
  This is the one the weight app needs (purge `app:weight` data after uninstall;
  also lets the leftover `manual` weigh-ins be cleared).
  - **Built, for `app:<id>` sources:** `GET /api/app-sources` lists every app
    source with data, each marked `orphaned`; `DELETE /api/app-sources/<id>`
    removes an orphan's data (400 while the app's folder is still on the
    volume, even if it fails to load - broken isn't gone; 404 with no rows)
    through `purge_source()` in `app/main.py` — the disconnect wipe's delete +
    re-derive, now shared, plus `clear_source_roles()`. The page shows
    an orphan in Connected sources and under a metric's sources with a red
    Remove data behind one confirm.
  - Decided for (A): per-source (everything that orphaned source wrote, like
    disconnect). B1 (below) adds per-(source, metric) beside it, on the same
    `purge_source()`.
  - Not yet: the legacy `manual` source — left alone for now, a possible later
    addition on the same `purge_source()`.
- **(B) Purge a whole CUSTOM metric** (`app:<id>:<key>`) once no installed app
  defines it -> metric + data vanish from the dataset/catalog. This is "tidy up a
  custom metric I don't use any more"; built-in metrics are never purgeable.
  - **B0 — app-defined custom metrics: built** (above). An app declares a new
    key's label/unit/category/grain/range, the write path accepts it, the catalog
    lists it.
  - **B1 — per-metric removal of an orphaned source's data: built.** After an
    uninstall a custom metric's readings (and their derived picks) persist under
    `app:<id>:<key>` but it drops out of the catalog — it's no longer in the
    effective vocabulary, and `/api/metric/app:<id>:<key>` 404s (B0: that part
    needs nothing new). B1 removes those lingering readings — and, the same
    way, one built-in metric's readings from a dead app — without wiping the
    rest of what the app wrote. (Before B1, the per-metric Remove data under a
    Your data row called the whole-app route, so removing one metric's leftovers
    wiped the dead app's every metric.) Reinstalling the app before then still
    brings the metric back with its history.
    - **The route:** `DELETE /api/app-sources/<id>/<metric>` — `<metric>` the key
      as stored (a canonical key, or the full `app:<id>:<key>`; colons are fine
      in the path, encoded or not). Guards as the whole-app route's, in the same
      order: 404 for an id failing `APP_ID`, 400 while `app_present()`, 404
      `"no <metric> data from app:<id> to remove"` when that `(source, metric)`
      has no reading or sample (`_has_metric_data()`; sessions are kind-keyed,
      so a kind is never found here). 200 `{ok, removed: {metrics, sessions,
      samples}}`, `sessions` always 0. DELETE only (405 otherwise).
      `GET /api/app-sources`' shape is unchanged — no per-metric counts.
    - **The factoring:** `purge_source(conn, source, metric=None,
      keep_roles=False)` — with `metric`, deletes that metric's `metrics` rows
      (`RETURNING date`) and `samples` rows from the source, never `sessions`,
      clears only that metric's role (`clear_source_roles(conn, source,
      only=metric)`: the entry under that key, iff it names the source) and
      re-derives the deleted span, one transaction as before. Without
      `metric`, unchanged. A built-in metric stays in the vocabulary — only
      the readings go; a dead app's metric that another source wins (the
      lost-pick case) loses only the app's rows, and the re-derive leaves the
      winner.
    - **Two entry points, one confirm:** Your data is by metric — a row's
      source list shows an orphan line for each dead app that wrote that
      metric (`/api/source-roles`' `sources` is every source that wrote it,
      winning or not), its Remove data for that metric only. Connected sources
      is by app — the orphan row expands (`expandable()`, as a Your data row
      does) to a line per metric the app left behind, each with its own
      Remove data, under a header keeping **Remove all data** (the whole-app
      route). An orphaned custom metric has no Your data row (it's out of the
      catalog), so the expanded orphan row is its only entry point. Every
      button opens `openRemoveAppData(o, metric)` — `metric` null for the
      whole app — which picks the route and wording and, on success, re-reads
      the app sources, Your data and Home.
  - Note: custom keys are namespaced per app (`app:<id>:<key>`), so two apps can't
    feed one shared custom metric. "Multiple app sources for one metric" only
    happens for canonical keys. A *shared* custom namespace would be a further
    schema change (who owns unit/category) — not planned.

## Build order (one branch each)

- Done: docs cleanup (#55/#56) and apps-as-source + weight app (#57).
- **A — base_tz universal setting:** home timezone in universal settings,
  validated IANA, read-only to apps via `ctx`. Small, independent. Weight app
  uses server-local date until it lands.
- **Data-lifecycle (A):** orphaned-source data removal (above). Done for app
  sources; reuses the disconnect path and clears the stale `source_roles` entry.
- **Custom metrics (B0):** done — app-declared `daily`/`instant` metrics stored
  as `app:<id>:<key>` (above).
- **Purge (B1):** done — per-metric removal of an orphaned app source's data
  (`DELETE /api/app-sources/<id>/<metric>`), which also clears an orphaned
  custom metric's lingering readings (above).
- **Session write-back:** done — `ctx.write_session(kind, start, end, summary)`
  on the `WriteCtx`, sessions of the existing kinds (`sleep`, `workout`) under
  the app's own source, nothing derived from them (`app-host.md`; the
  `workout-log` app is the reference).
- **Roster path:** done — two session kinds in core, `duties_rostered` and
  `duties_complete`, in a new `Roster` category (operational context, kept
  per decision 1), and the `roster` app (now `frms`, `tests/fixtures/apps/frms/`),
  which parses a roster's `.ics` itself (stdlib) and files it. Decided:
  - **Duty-ness is structural**, never a code allow-list: a timed `VEVENT` is a
    duty and is filed; an all-day one (`O`, `G`, `U`, `L`, any future code) is
    a day off and skipped. A code the classifier doesn't know is filed as
    `type: "other"`, its code kept — the label never decides whether to write.
  - **The envelope is `DTSTART`/`DTEND`, in UTC** (for reserve/sim they equal
    the roster's stated `TOTAL` exactly); the description is kept verbatim as
    `raw`, never parsed for times. A `TZID`/floating time refuses the upload.
  - **Two kinds by the clock**: `duties_complete` once a duty has ended at
    upload, else `duties_rostered` — so uploads before and after a duty leave
    the plan and the actual side by side. Every summary says `capture: "live"`,
    so a later "upload a saved plan" override is additive.
  - **Daily hours** (`duty_hours_complete`/`duty_hours_rostered`, the app's own
    metrics, `h`, 0–24) were summed per kind by `DTSTART`'s UTC date, a duty past
    midnight whole on its start day. Dropped when the app became FRMS
    (`app:frms`): nothing read them, and duty hours are read from the sessions
    when a report needs them — home-local bucketing still waits on `base_tz`.
  - **Upsert-only**: a re-upload replaces a duty of the same kind and start,
    but removes nothing; `U` (unfit) and leave aren't captured. Next on the
    path: an app replacing its own sessions/metrics over a date range (the
    stale-duty cleanup), and the rostered↔flown matching (session read-back +
    a derived duty key — the feed has no `UID`).
- **(later) fatigue tracker**, reading the duties (and sleep) the roster path
  now files.

## Minor / noted

- Cosmetic: the dataset console shows data as of page load and doesn't re-fetch
  on an app write — needs a browser refresh. The Home tile DOES live-update on
  Save (confirmed working). Fix if wanted: re-fetch the console on open; or a
  small "refresh to see latest" note.

## Grounding (verified in repo, origin/main)

- `metrics(date, source, metric, value, unit, synced_at)`, PK (date, source,
  metric): one value per day per source (last wins).
- `sessions(id = source:kind:start, ...)`; `write_session` upserts by id.
- `settings` key/value + `GET/PUT /api/settings/<key>` — base_tz goes here.
- `derived.py`: precedence as above; `recompute_derived` DELETEs stale picks +
  upserts (ON CONFLICT(date,metric) DO UPDATE).
- Connector disconnect deletes a source's metrics/samples/sessions + re-derives —
  the machinery the (A) removal reuses.
- Uninstall keeps an app's data (by design); `WriteCtx.write_metric` accepts a
  canonical key or one the app declares in its manifest (B0), nothing else;
  `WriteCtx.write_session` an existing session kind only, under the app's own
  source (id `app:<id>:<kind>:<start>`).
