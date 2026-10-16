# Apps-as-source — design reference

_Apps as a data source: the write capability, the weight app, and the data
lifecycle. Drafted Oct 16 2026; records the decisions and current build state._

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
  concept — and **that path is NOT built yet** (see Data lifecycle -> B0).
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

## Data lifecycle — removing app data (settled Oct 16, not yet built)

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
  also lets the leftover `manual` weigh-ins be cleared). Buildable now.
  - Open design point: per-source (remove everything that orphaned source wrote,
    like disconnect) vs per-(source, metric). Lean per-source.
- **(B) Purge a whole CUSTOM metric** (`app:<id>:<key>`) once no installed app
  defines it -> metric + data vanish from the dataset/catalog. This is "tidy up a
  custom metric I don't use any more"; built-in metrics are never purgeable.
  - **Prerequisite (B0): custom app-defined metrics don't exist yet.** The slice-B
    `WriteCtx.write_metric` rejects any key not in the canonical `METRICS` set, so
    an app currently can ONLY write canonical keys. Writing `app:<id>:<key>` was
    explicitly deferred in the slice-B brief. So B is really: **B0** build
    app-defined custom metrics (the app declares a new key's unit/category/label;
    the write path accepts it; the catalog lists it), then **B1** purge an
    orphaned one. B0 is the substantive piece — a capability in its own right.
  - Note: custom keys are namespaced per app (`app:<id>:<key>`), so two apps can't
    feed one shared custom metric. "Multiple app sources for one metric" only
    happens for canonical keys. A *shared* custom namespace would be a further
    schema change (who owns unit/category) — not planned.

## Build order (one branch each)

- Done: docs cleanup (#55/#56) and apps-as-source + weight app (#57).
- **A — base_tz universal setting:** home timezone in universal settings,
  validated IANA, read-only to apps via `ctx`. Small, independent. Weight app
  uses server-local date until it lands.
- **Data-lifecycle (A):** orphaned-source data removal (above). Small; reuses the
  disconnect path. Includes clearing the stale `source_roles` entry on removal.
- **Custom metrics (B0) then purge (B1):** larger; schedule deliberately.
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
- Uninstall keeps an app's data (by design); `WriteCtx.write_metric` rejects
  non-canonical keys (so B0 is unbuilt).
