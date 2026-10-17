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
  `unit` are non-empty text; `category` one of the 8 `CATEGORIES`; `grain`
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

## Data lifecycle — removing app data (settled Oct 16; A and B0 built, B1 not yet)

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
  - Decided: per-source (everything that orphaned source wrote, like
    disconnect), not per-(source, metric).
  - Not yet: the legacy `manual` source — left alone for now, a possible later
    addition on the same `purge_source()`.
- **(B) Purge a whole CUSTOM metric** (`app:<id>:<key>`) once no installed app
  defines it -> metric + data vanish from the dataset/catalog. This is "tidy up a
  custom metric I don't use any more"; built-in metrics are never purgeable.
  - **B0 — app-defined custom metrics: built** (above). An app declares a new
    key's label/unit/category/grain/range, the write path accepts it, the catalog
    lists it.
  - **B1 — purge an orphaned one: not yet.** After an uninstall a custom metric's
    readings (and their derived picks) persist under `app:<id>:<key>` but it
    drops out of the catalog — it's no longer in the effective vocabulary, and
    `/api/metric/app:<id>:<key>` 404s. That's the "orphaned custom metric" state
    B1 handles. Meanwhile (A) already removes it with everything else its source
    wrote (`DELETE /api/app-sources/<id>`), and reinstalling the app brings the
    metric back with its history.
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
- **Purge (B1):** an orphaned custom metric; next on this path.
- **(later) fatigue / roster path**, unblocked by the scope reversal.

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
  canonical key or one the app declares in its manifest (B0), nothing else.
