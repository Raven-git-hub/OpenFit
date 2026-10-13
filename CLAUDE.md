# OpenFit — guide for Claude Code

Self-hosted **health-data engine + app host** — NOT a tracker. OpenFit ingests
from your devices, derives and maintains one dataset you own (the **OpenFit
Derived Data**), and exposes it to apps. It ships **no tracking apps**; every
tracker/dashboard/planner is an external app that reads the data. Privacy-first:
data lives on your own hardware, nothing leaves your network except a plugin's
own calls to its device API. Full direction:
`docs/architecture/openfit-overview-and-design-pathway.md`.

## Stack (keep it lightweight — no frameworks without a real reason)
- Python + Flask, raw `sqlite3` (NOT SQLAlchemy/an ORM)
- Vanilla JS, no build step
- Docker + docker-compose; one container, SQLite at `/data/tracker.db`
- Do NOT introduce Alembic, SQLAlchemy, React, a bundler, or similar.

## Layout
- `app/main.py` — Flask API + APScheduler. `serve()` runs migrations, then the
  one-time derived backfill, then the scheduler, then the app; none of that may
  run on import (tests import it).
- `app/migrations/` — hand-rolled runner. Numbered `NNN_name.sql`, applied once,
  tracked in `schema_migrations`. New schema change = a new numbered migration;
  migrations must be additive and safe against a populated production DB.
- `app/plugins/` — sync (pull) plugins. Each `app/plugins/<name>/plugin.py`
  subclasses `SyncPlugin` (`app/plugins/base.py`), registered in `__init__.py`,
  declares a connector manifest (`fields`, `add_flow`), reads creds via
  `self.get_credentials(conn)`, writes readings via `write_metric()`,
  interval records via `write_session()` and intraday samples via
  `write_sample()`. Never bypass this pattern.
- `app/metrics.py` — the canonical metric vocabulary (key → unit), plus the
  session kinds (`SLEEP`, `WORKOUT` in `SESSION_KINDS`) and the series metrics
  (`HEART_RATE`, `RESPIRATION`, `SPO2` in `SERIES_METRICS`). Import keys and
  kinds from here; never
  spell metric or kind strings out in plugins or the API.
- `app/derived.py` — the one place a source is picked per `(date, metric)`:
  `recompute_derived()` writes `derived_metrics`. Never pick a source anywhere else.
- `app/crypto.py` — Fernet encrypt/decrypt for stored credentials.
- `tests/` — pytest, temp DB, no network.
- `docs/architecture/` — design direction (read first). `docs/design/` — frontend references.

## Data model
- Current: a tidy `metrics(date, source, metric, value, unit, synced_at)` keyed
  `(date, source, metric)` — one row per reading, so a new metric is data not a
  migration. Keys and units come from `app/metrics.py` (`steps`/count,
  `resting_hr_bpm`/bpm, `sleep_minutes`/min, `weight_kg`/kg, and the daily
  activity metrics `distance_m`/m, `active_kcal`/kcal, `total_kcal`/kcal,
  `floors`/count, `intensity_moderate_min`/min, `intensity_vigorous_min`/min,
  and the daily vitals `spo2_pct`/%, `respiratory_rate_brpm`/brpm,
  `hrv_ms`/ms). Garmin files those six activity metrics from the same
  `get_stats` response as steps and resting HR, through its `STATS_METRICS`
  field map (`app/plugins/garmin/plugin.py`; each rounded to a whole number):
  a field absent or not a finite number writes nothing (an absent one is
  logged with the response's keys — the floors/intensity field names are
  unverified guesses), and a day holding only these still lands. The three
  vitals are a request a day each, through the plugin's `GARMIN_VITALS` table
  (each entry's `daily` path and metric): `get_spo2_data` → `averageSpO2`,
  `get_respiration_data` → `avgWakingRespirationValue`, `get_hrv_data` → the
  nested `hrvSummary.lastNightAvg` (the whole payload is `{}` on a device
  without HRV). Each is stored as reported (not rounded), in its own
  try/except (a failed fetch costs only that vital — its metric and its
  series); a value absent or not a finite number writes nothing and isn't
  logged — an empty response is a device without that sensor, not a wrong
  field name — and a day holding only a vital still lands. The same SpO2 and
  respiration responses also carry the intraday series (an entry's `series`
  key; see `samples` below), read from the one fetch — no extra request.
  Migration `005` unpivoted the old wide `activity` table into it and dropped
  `activity`; migration `006` moved the old `weights` table in as `weight_kg`
  readings under source `manual` (hand entry; `synced_at` NULL for migrated
  rows) and dropped `weights`.
- Beside it, `sessions(id, source, kind, "start", "end", summary_json, synced_at)`
  (migration `007`, purely additive) for interval records: `id` is
  `source:kind:start` so a re-sync upserts, `start`/`end` are ISO 8601 UTC
  (`iso_utc()` in `app/plugins/base.py`), `summary_json` is the kind's
  breakdown as JSON. Quote `"start"`/`"end"` in SQL (`end` is a keyword).
  Written via `write_session()`, which raises on a `kind` outside
  `SESSION_KINDS` (`app/metrics.py`, where each kind's summary keys are listed).
  Two kinds, both from Garmin and Google; a summary holds only what the source
  gives:
  - `sleep` — one session per night/nap, summary in minutes: `asleep_minutes`
    plus `light_minutes`/`deep_minutes`/`rem_minutes`/`awake_minutes`. Sleep is
    dual-track: the daily `sleep_minutes` metric is written exactly as before
    and the session sits beside it.
  - `workout` — one session per activity, and no daily metric: `type` (the
    source's activity type, lowercased — not yet mapped across sources),
    `duration_minutes`, `distance_m`, `avg_hr_bpm`, `calories_kcal`.
  Read through the access contract (`GET /api/sessions/<kind>`, below).
- `samples(source, metric, ts, value, unit, synced_at)` keyed
  `(source, metric, ts)` (migration `009`, purely additive): the series
  (intraday) grain, the third beside daily `metrics` and interval `sessions` —
  one row per source/metric/timestamp, `ts` ISO 8601 UTC from `iso_utc()`,
  every column NOT NULL, indexed on `(metric, ts)`. Written only via
  `write_sample(conn, source, metric, ts, value)` (`app/plugins/base.py`),
  which fills the unit from `SERIES_METRICS` (`app/metrics.py`), raises on a
  metric outside it, drops a `None` value (a source's non-wear gap passes
  straight through and writes nothing), and upserts a re-sync in place.
  Series keys are their own vocabulary, distinct from the daily keys even
  where the quantity overlaps: `heart_rate` (bpm) is not `resting_hr_bpm`,
  `respiration` (brpm) is not `respiratory_rate_brpm`, `spo2` (%) is not
  `spo2_pct`. Garmin fills all three, each day of its sync window:
  - `heart_rate` — `get_heart_rates` gives `[epoch_ms, bpm]` pairs on a
    ~2-minute cadence, parsed by `_heart_rate_samples()`
    (`app/plugins/garmin/plugin.py`; null bpm/non-wear and unparseable pairs
    dropped), in its own try/except (a failed fetch costs only the samples).
  - `respiration` and `spo2` — from the daily vitals' own responses
    (`GARMIN_VITALS`' `series` keys, no extra request):
    `respirationValuesArray` (`[epoch_ms, brpm]`, ~2-minute cadence) and
    `spO2HourlyAverages` (`[epoch_ms, %]`, hourly; the finer
    `spO2SingleValues` can be null, so it isn't read). Both go through the one
    reusable converter `_series_samples(rows, drop_non_positive=True)`, which
    drops nulls, non-numbers, unreadable rows and — Garmin's "no reading"
    sentinels in these arrays being negative (-1/-2) — non-positive values;
    never raises. A failed fetch costs that vital's metric and series.
  All three are written ahead of the day's no-summary skip, so a day with
  samples but no daily figures keeps them. Nothing derives from them; apps
  read them through the access contract (`GET /api/series/<metric>`, below) as
  a per-source passthrough — the cross-source stitch is a later slice.
- `derived_metrics(date, metric, value, unit, source, synced_at)` keyed
  `(date, metric)` (migration `008`, purely additive): the one trusted value per
  metric per day, every metric including `weight_kg`. `source` is the winning
  source (provenance); value/unit/synced_at are the winning reading's. Written
  only by `recompute_derived(conn, since=None, until=None)` in `app/derived.py`: the
  highest-ranked source with a reading wins — the metric's configured primary
  (its source role) if it has one, then `DEFAULT_PRIORITY` = garmin >
  google_health, unlisted sources last, then by name; a `(date, metric)` in
  range with no readings left loses its row; dates outside `since`..`until`
  are untouched.
  Refreshed after every sync for that sync's window (`today - (days-1)` on),
  after every webhook reading and manual `POST /api/weights` for its one day
  (`since=until=date`), after `DELETE /api/weights` for each day it cleared
  (one day at a time), and backfilled once by `serve()` only while the table
  is empty — never a blanket re-derive (a policy change is forward-only). The
  access contract and the default `/api/activity` read it; `?by_source=1`
  still reads `metrics`.
- Source roles: the `settings` row `source_roles` (no migration) holds JSON
  `{metric: primary_source}`, e.g. `{"steps": "google_health"}`; a metric absent
  from it uses the default precedence. Read by `load_source_roles()`, written by
  `set_source_role()` (both `app/derived.py`; a malformed value reads as no
  overrides). `GET /api/source-roles` lists every canonical metric's effective
  `primary`, whether it's `configured`, and the `sources` that have reported it
  (in pick order); `PUT /api/source-roles/<metric>` `{"primary": "<source>"}`
  sets it, `null`/empty clears it. Forward-only: a change re-derives nothing —
  it applies from the next sync's window.
- Webhook (push input, no migration): `POST /api/webhook/<token>` with
  `{"metric", "value", "date"?, "source"?}` writes one reading via
  `write_metric()` then `recompute_derived(since=date, until=date)`, in one
  transaction. Token-as-auth — the only authed route: the `settings` row
  `webhook_token` (`secrets.token_urlsafe`), compared with
  `hmac.compare_digest`; none stored or a mismatch → 401. `GET
  /api/webhook-token` returns it (made on first ask) with its `path`/`url`;
  `POST /api/webhook-token/regenerate` replaces it. 400 unless `metric` is a
  canonical key, `value` a finite number (not a bool) within the metric's
  `PLAUSIBLE_RANGES` (`app/metrics.py`; inclusive; no entry = any number),
  `date` is `YYYY-MM-DD` (default today), and `source` a non-empty string
  (default `webhook`) that isn't a pull plugin's id. Idempotent per
  `(date, source, metric)`. Metrics only — no session ingest yet.
- The access contract — the app-facing read API (read-only, no auth, no
  migration; in `app/main.py`). Apps read the DERIVED profile from
  `derived_metrics`, never raw per-source `metrics` rows, so they never
  reconcile sources:
  - `GET /api/catalog` → `{"metrics": [{metric, unit, sources, first, last,
    count, last_value, last_date}], "sessions": [{kind, count, first, last}],
    "series": [{metric, unit, sources, first, last, count}]}` — only
    metrics/kinds/series with data (vocabulary order; kinds by name), so an
    empty install has three empty lists. A metric's `sources` = sources that
    have won any day, in pick order; a series' = every source with a sample
    of it, in default precedence (no pick yet). Session `first`/`last` are
    start timestamps, a series' its earliest/latest sample `ts`.
  - `GET /api/metric/<metric>?from=&to=` → `{metric, unit, points: [{date,
    value, source}]}` ascending; `value` as stored (a float). Unknown metric
    404; known with no data → `points: []`.
  - `GET /api/sessions/<kind>?from=&to=` → `[{start, end, source, summary}]`
    ascending by start (then source); `summary` parsed from `summary_json`
    (`{}` if none), `end` may be null. Sessions have no derived pick: each
    source's session is listed, tagged with its source. `from`/`to` match the
    start's UTC date. Unknown kind 404.
  - `GET /api/series/<metric>?from=&to=` → `{metric, unit, points: [{ts, value,
    source}]}` from `samples`, ascending by `ts` then source; `value` a whole
    number as an int (`_whole()`: 77.0 reads as 77). A per-source passthrough,
    no pick: every source's samples are listed, tagged (the cross-source
    stitch comes once a second series source exists). `from`/`to` match the
    sample's UTC date, as `ts` bounds (`>= fromT00:00:00Z`, `<= toT23:59:59Z`)
    so the `(metric, ts)` index is used. 404 unless `<metric>` is in
    `SERIES_METRICS` (`heart_rate`, `respiration`, `spo2`) — a daily key like
    `resting_hr_bpm` included; known with no samples → `points: []`.
  - `from`/`to`: optional, inclusive `YYYY-MM-DD` (empty = omitted); malformed,
    or `from` after `to` → 400. Errors are `{"ok": false, "error"}`.
  These shapes are the stable contract: change them additively only.
- Also: `workouts` (orphaned — slated for removal); `settings`; `accounts`;
  `schema_migrations`.
- Target (in progress — see Direction): the other source roles (sources that
  verify the primary) and full history retained.

## Hard constraints
- No multi-tenancy in core. No `user_id`, no auth on core (the webhook's URL
  token is the one exception). Multi-user is a future per-container Host layer.
- Privacy-first: no telemetry, no external calls except a plugin's own device API.
  Credentials encrypted at rest, never returned over the API.
- Ships no tracking apps. `app/templates/index.html` is a legacy/disposable UI
  slated to become a barebones data viewer — don't build tracker features into core.

## Workflow
- Always work on a branch and open a PR — never push to `main`.
- Any new API route gets a pytest test in the existing style.
- Run `pytest` before opening a PR.

## Direction (in progress — not all built yet)
See `docs/architecture/openfit-overview-and-design-pathway.md` and `ROADMAP.md`.
These are NOT yet in the code — don't assume they exist until a PR lands them, and
when you implement one, update the Data model section above and the README in the
same PR:
- **The engine:** the `metrics` table + canonical vocabulary landed in migration
  `005`; weight folded in as `weight_kg` in `006`; `sessions` in `007` (sleep,
  then workouts with no further migration); the stored derived value in `008`
  (fixed precedence) and the user-overridable per-metric primary in
  `settings.source_roles` (no migration); Garmin's daily metrics broadened
  from `get_stats` (distance, calories, floors, intensity minutes — no
  migration); Garmin daily vitals (SpO2, respiration, HRV — daily values, a
  request each, no migration); the `samples` table (series grain)
  and `write_sample()` in `009`; Garmin intraday heart rate fetched into it,
  and read back through `GET /api/series/<metric>` (a per-source passthrough);
  Garmin's respiration and SpO2 series from the vitals responses it already
  fetches (sentinels dropped by `_series_samples()` — no new request, no
  migration). Still to come: the cross-source `/api/series` stitch (pending a
  second series source), more series sources, verifying sources + history.
- **Webhook / push ingestion:** landed for metrics (see Data model); still to
  come: pushing sessions (sleep, workouts) through it.
- **The access contract:** landed (see Data model): discovery, range queries
  with provenance, sessions, intraday series (passthrough). Still to come: a
  `?by_source` option for raw per-source reads, a derived pick for sessions
  (one per night across devices), and the series stitch.
Keep `GET /api/activity` returning its current flat shape, and `/api/weights` its
`[{date, weight}]` shape (manual `weight_kg` readings only), as compatibility shims
during the data-model change. Remove the orphaned `workouts` table +
`/api/workouts` when convenient (unused by the UI).
