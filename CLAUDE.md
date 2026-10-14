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
- Docker + docker-compose; one container, SQLite at `/data/tracker.db` (in WAL
  mode, so `tracker.db-wal`/`tracker.db-shm` beside it are part of the
  database); logs unbuffered (`PYTHONUNBUFFERED=1` in `app/Dockerfile`), so
  scheduler/sync output reaches `docker logs` at once
- Do NOT introduce Alembic, SQLAlchemy, React, a bundler, or similar.

## Layout
- `app/main.py` — Flask API + APScheduler. `serve()` runs migrations, then the
  one-time derived backfill, then the scheduler, then the app; none of that may
  run on import (tests import it). Every connection comes from `get_conn()`,
  per call (one per request or scheduled run — never a shared global one).
  One sync at a time: a process-level `_sync_lock` (`threading.Lock`, taken
  with `acquire(blocking=False)`, released in a `finally`) guards
  `POST /api/sync/<plugin>`, `scheduled_sync()` and `sync_one()`. A manual
  sync that finds one running gets a 409 and starts nothing:
  `{"ok": false, "error": "a sync is already in progress"}`. A scheduled tick
  that finds one logs `[scheduler] sync already running, skipping` (plus the
  plugin id, for a plugin's own tick) and returns.
  The scheduler (`start_scheduler()`, stashed in the module-level
  `_scheduler`) runs `scheduled_sync()` — every configured plugin, in turn —
  once at startup as the catch-up, then one `interval` job per connected
  plugin (or env-configured one, as a pre-UI install is), id
  `sync:<plugin_id>`, ticking `sync_one(plugin_id)` — that plugin only — at
  its effective cadence: `effective_cadence()`, its `sync_cadences` override
  (see Data model) else the baseline `SYNC_INTERVAL_HOURS` (env, default 6).
  Connecting a source (`_store_account()`, which both add flows go through)
  adds or refreshes its job via `reschedule_plugin()`, `PUT
  /api/connectors/<id>/cadence` moves it (its next tick that far from now),
  and `disconnect_connector()` drops it via `remove_plugin_job()`; both
  helpers no-op while `_scheduler` is None, as it always is in tests. One
  worker (`ThreadPoolExecutor(1)`) and `misfire_grace_time=None`: jobs that
  come due together — two sources on the same cadence, started together,
  tick in step — queue and run in turn instead of racing for the lock and
  all but one skipping, every tick.
- `app/migrations/` — hand-rolled runner. Numbered `NNN_name.sql`, applied once,
  tracked in `schema_migrations`. New schema change = a new numbered migration;
  migrations must be additive and safe against a populated production DB.
  Also `connect(db_path)`, the one way a connection is opened (`get_conn()`
  and the `python -m migrations` CLI both use it): straight after connecting,
  `PRAGMA busy_timeout = 30000` (wait up to 30s for a lock instead of failing;
  sqlite3's own default is 5s), then `PRAGMA journal_mode = WAL` (readers and
  the writer stop blocking each other; persistent, so the first connection —
  `migrate()` at startup — converts the database and later ones are no-ops).
  `synchronous` is left at its default.
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
  spell metric or kind strings out in plugins or the API. Also how each key
  is collected: `cadence_for(key)` → `"reading"` (in `SERIES_METRICS`),
  `"session"` (in `SESSION_KINDS`), `"manual"` (in `MANUAL_METRICS` =
  `weight_kg`, `body_fat_pct`, `bmi`, `hydration_ml` — daily metrics whose
  expected input is a scale or hand entry, though a device may supply them
  too), else `"daily"` (in `METRICS`); any other key raises `ValueError`. A
  pure label: nothing is stored or picked by it.
- `app/derived.py` — the one place a source is picked per `(date, metric)`:
  `recompute_derived()` writes `derived_metrics`. Never pick a source anywhere else.
- `app/crypto.py` — Fernet encrypt/decrypt for stored credentials.
- `app/templates/index.html` — the data console, one self-contained page
  (vanilla JS, no build step, no external calls or web fonts; every colour/font/
  radius in its `CUSTOMISE OPENFIT HERE` token block). Under the top bar
  (date, a `settings` button, the theme toggle) sits the original angled tab
  strip, restored verbatim: **Home** (far left, a blank panel for now) and
  **Data** (far right, the console), then the dashed **＋ Add**. `settings`
  opens the Settings popup (the `.modal` chrome): one control, **Opening
  page**, a dropdown built from the tab strip (`.tab[data-tab]`, so a new tab
  appears in it), saved per browser in `localStorage` `of-opening-tab`; boot
  opens that tab via `showTab()` (Data if unset or no longer a tab) in place
  of a hardcoded default. ＋ Add, and Add source once every connector is
  connected, each open one shared coming-soon placeholder modal
  (`comingSoon(title)`); while a connector is still unconnected, Add source
  opens its real add flow, from a `.pop` menu of the unconnected ones —
  alphabetical (the API's order), and scrolling (`max-height`/`overflow-y`)
  when long. Each connected source's row has **Sync now** and a
  gear **Settings** button (there is no bare Remove): Settings opens a
  per-source `.modal` that reads `GET /api/connectors/<id>/detail` into an
  info list — Login (`—` if null), Connected via (Password / OAuth), Last
  sync (the stored UTC stamp as a local date + time; null → Never), Added (a
  date), and **Sync every**: preset buttons 1h · 3h · Default · 12h · 24h, the
  active one `.primary` (`sync_is_default` → Default, else the `sync_hours`
  one); clicking another `PUT`s `/api/connectors/<id>/cadence` (`{hours: N}`,
  Default `{hours: null}`) and moves the highlight, or shows the error in the
  popup's `.src-msg` and leaves it — with **Remove source** at its foot. No
  global cadence control: the universal Settings popup keeps Opening page
  only. Remove is a two-step gate in
  the same modal, no `confirm()`: "Disconnecting stops future syncs." over a
  danger panel "Wipe data?" ([Keep data] / [Wipe data]), then "Are you
  sure?" naming the source and whether its data goes ([Cancel] / [Remove] or
  [Remove & wipe]); confirming `DELETE`s `/api/connectors/<id>` (`?wipe=1` on
  the wipe path), then refreshes the sources card and `loadData()`.
  "Your data" groups its reporting rows by `cadence`
  — Daily, Manual, Session, Reading, a `.dgroup` header per non-empty group —
  above the one ungrouped "Not yet reporting" group. A round voice button
  after the "Connected sources" and "Your data" titles plays that card's clip
  (`/audio/connected-sources.mp3`, `/audio/your-data.mp3`) through one shared
  `<audio>`: one clip at a time, a second click stops it, and a missing file
  (404) just resets the button, which stays. `GET /audio/<name>`
  (`app/main.py`) serves them via `send_from_directory` from `AUDIO_DIR`
  (`$AUDIO_DIR`, default `/app/audio`). The clips are committed in
  `app/audio/` and baked into the image (`COPY audio ./audio` in
  `app/Dockerfile`), so they deploy with `git pull && docker compose up -d
  --build`; a new or changed clip is a commit plus a rebuild (the old
  `/data/audio` volume drop-in is retired). Static serving is otherwise off. Reads
  `/api/catalog?include_empty=1`, `/api/coverage` and `/api/source-roles`
  (dragging a daily metric's sources `PUT`s `/api/source-roles/<metric>`), plus
  the connector endpoints (`/api/connectors`, the OAuth add flow,
  `/api/connectors/<id>/detail`, `PUT /api/connectors/<id>/cadence`,
  `DELETE /api/connectors/<id>`,
  `/api/sync/<plugin>`); not `/api/activity` (the last-sync stamp that read it
  is gone). See `docs/architecture/frontend-redesign-and-widget-model.md`.
- `tests/` — pytest, temp DB, no network.
- `docs/architecture/` — design direction (read first). `docs/design/` — frontend references.

## Data model
- Current: a tidy `metrics(date, source, metric, value, unit, synced_at)` keyed
  `(date, source, metric)` — one row per reading, so a new metric is data not a
  migration. Keys and units come from `app/metrics.py` (`steps`/count,
  `resting_hr_bpm`/bpm, `sleep_minutes`/min, `weight_kg`/kg, and the daily
  activity metrics `distance_m`/m, `active_kcal`/kcal, `total_kcal`/kcal,
  `floors`/count, `intensity_moderate_min`/min, `intensity_vigorous_min`/min,
  the daily vitals `spo2_pct`/%, `respiratory_rate_brpm`/brpm,
  `hrv_ms`/ms, and `vo2_max`/ml/kg/min, `body_fat_pct`/%, `bmi`/kg/m2,
  `hydration_ml`/ml). Garmin files those six activity metrics from the same
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
  `GARMIN_VITALS` also carries two single-value daily fetches with no series:
  `get_training_status` → the nested `mostRecentVO2Max.generic.vo2MaxValue`
  as `vo2_max` (the most recent estimate whatever day is asked, so each day of
  the window gets the current value) and `get_hydration_data` → `valueInML` as
  `hydration_ml`, both stored as reported. Body composition is its own
  request a day, `get_body_composition(day, day)` (a range call with three
  outputs, so not a `GARMIN_VITALS` entry), read by
  `_body_composition_metrics()` from `totalAverage`: `bodyFat` →
  `body_fat_pct` and `bmi` → `bmi` as reported, and `weight` → `weight_kg`
  converted from grams (`/ 1000`) — Garmin's `weight_kg` sits beside the
  `manual` one and outranks it in the default precedence. That unit is
  unverified (every field was null on the account probed), so the converted
  weight is written only within `weight_kg`'s `PLAUSIBLE_RANGES`; outside it
  nothing is written and the raw grams and converted kg are logged. As with
  the vitals, a missing/null/non-number field writes nothing and isn't
  logged, a failed fetch costs only its own metrics, and a day holding only
  these still lands.
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
  (one day at a time), after `DELETE /api/connectors/<plugin>?wipe=1` over
  the span its deleted readings covered (min..max date, one call, so the
  whole removal is one transaction), and backfilled once by `serve()` only while the table
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
- Sync cadences: the `settings` row `sync_cadences` (no migration) holds JSON
  `{plugin_id: hours}`, overrides only, e.g. `{"garmin": 1}`; a plugin absent
  from it syncs at the baseline, `SYNC_INTERVAL_HOURS` (6h). The override
  presets are `ALLOWED_CADENCE_HOURS` = 1, 3, 12, 24 — the baseline is
  Default, expressed by clearing the entry, never stored as 6. Read by
  `load_sync_cadences()` (a malformed value reads as no overrides; an entry
  outside the presets, a stored 6 or a bool included, is dropped), written by
  `set_sync_cadence()` (`None` clears; anything else outside the presets
  raises `ValueError`), resolved by `effective_cadence()` — all in
  `app/main.py`, beside the scheduler. Set per source through `PUT
  /api/connectors/<id>/cadence` (see Connectors); the baseline itself isn't
  user-configurable.
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
    start timestamps, a series' its earliest/latest sample `ts`. Every entry
    also carries `grain` (`"daily"` for metrics, `"session"`, `"series"`) and
    `cadence` (`cadence_for()`). `?include_empty=1` (also `true`/`yes`;
    default off = the above) lists every vocabulary key in each list, in the
    same order — one with no data as `count: 0`, `sources: []`,
    `first`/`last` null (metrics also `last_value`/`last_date` null) — so a
    console can show "not yet reporting" slots; split them by `count`.
  - `GET /api/coverage?from=&to=` → `{"metrics": {metric: [date, ...]},
    "series": {metric: [date, ...]}, "sessions": {kind: [date, ...]}}` — which
    `YYYY-MM-DD` dates hold any data, source-agnostic, ascending and
    de-duplicated, so a console draws continuity without pulling samples.
    Metrics from `derived_metrics.date`; series from `date(ts)` of `samples`
    (filtered on `ts` bounds as `/api/series` is, for the index); sessions
    from `date("start")`. Series/session dates are UTC-bucketed (SQLite's
    `date()` reads `iso_utc()`'s `Z`); a user-local day boundary is a later
    refinement. Only vocabulary keys with ≥1 date in range are listed (the
    empty slots come from the catalog's `?include_empty`), so an empty
    install has three empty maps.
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
- Connectors (no migration): `accounts(plugin_id, credentials, created_at)`,
  one row per connected plugin, `credentials` a Fernet-encrypted JSON blob
  (the manifest fields, plus an oauth connector's refresh token). `GET
  /api/connectors` lists every plugin with its manifest and `connected`/
  `configured`, ordered by display `name` (case-insensitive; display only —
  `PLUGINS` and the scheduler keep registration order). `GET
  /api/connectors/<plugin>/detail` → `{connected_via, login, added,
  last_sync, sync_hours, sync_is_default}` for the source's
  Settings popup:
  `connected_via` is the plugin's `add_flow`; `login` its non-secret account
  identifier (`_login()` in `app/main.py`), read only from the manifest
  fields whose `type` isn't `"password"` (Garmin's `email`; Google Health's
  `client_id`), several joined with `, `, null if none — never a password,
  and never a key outside the manifest (an oauth refresh/access token);
  `added` is `accounts.created_at`; `last_sync` the MAX `synced_at` over the
  source's `metrics`, `samples` and `sessions` rows, as stored (UTC
  `YYYY-MM-DD HH:MM:SS`), null before a first sync; `sync_hours` its
  `effective_cadence()` (an int) and `sync_is_default` true when no override
  is stored — both reported whether or not it's connected. Not connected →
  `login`/`added`/`last_sync` all null; unknown plugin → 404. `PUT
  /api/connectors/<plugin>/cadence` `{"hours": <1|3|12|24|null>}` sets the
  source's cadence override (`null` or no `hours` clears it, back to
  Default) and reschedules its job, → `{"ok": true}`; any other `hours` —
  `6`, a float, a string, a bool, out of the set — or a body that isn't an
  object → 400 (nothing stored); unknown plugin → 404. A cadence is kept
  through a disconnect, like a source role, and applies again on re-add.
  `DELETE
  /api/connectors/<plugin>` removes the account, the plugin's cached auth
  (`clear_cached_auth()`) and its scheduler job, and keeps its data. With `?wipe=1` (also
  `true`/`yes`; anything else keeps) it also deletes every `metrics`
  (`RETURNING date`), `samples` and `sessions` row with `source = <plugin>`
  and `recompute_derived()`s the min..max of those dates — a day only that
  source filled loses its derived row, a day another source filled falls to
  it — in one transaction with the account delete (a failure rolls it all
  back). Every metric on every day in that span is re-picked under the
  current source roles. Unknown plugin → 404.
- Also: `workouts` (orphaned — slated for removal); `settings`;
  `schema_migrations`.
- Target (in progress — see Direction): the other source roles (sources that
  verify the primary) and full history retained.

## Hard constraints
- No multi-tenancy in core. No `user_id`, no auth on core (the webhook's URL
  token is the one exception). Multi-user is a future per-container Host layer.
- Privacy-first: no telemetry, no external calls except a plugin's own device API.
  Credentials encrypted at rest; their secrets (passwords, tokens) never
  returned over the API — `/api/connectors/<id>/detail`'s non-password
  `login` is the one field read back.
- Ships no tracking apps. `app/templates/index.html` is the data console — what
  the dataset holds, its continuity, and which source wins each metric (see
  Layout) — not a tracker; don't build tracker features into core.

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
  migration); Garmin's remaining daily metrics (VO2 max and hydration via
  `GARMIN_VITALS`, body fat, BMI and weight via a `get_body_composition`
  block — no migration); write-lock robustness (busy_timeout 30s + WAL on
  every connection via `connect()`, a process-level sync lock — an
  overlapping sync gets 409 and a scheduled tick skips — and unbuffered logs;
  no migration); per-source sync cadence (a `sync_cadences` settings row of
  overrides on the 6h baseline, one scheduler job per connected plugin, set
  from each source's Settings popup — no migration). Still to come: the cross-source `/api/series`
  stitch (pending a second series source), more series sources, verifying
  sources + history, and committing a sync per day (it still holds one write
  transaction for the whole run).
- **Webhook / push ingestion:** landed for metrics (see Data model); still to
  come: pushing sessions (sleep, workouts) through it.
- **The access contract:** landed (see Data model): discovery, range queries
  with provenance, sessions, intraday series (passthrough); for the
  data-collation console, each catalog entry's
  `grain`/`cadence` (`cadence_for()`, `MANUAL_METRICS`), the catalog's
  `?include_empty=1` and per-day presence via `GET /api/coverage` (no
  migration); the console itself is `index.html` (frontend only). Still to come: a
  `?by_source` option for raw per-source reads, a derived pick for sessions
  (one per night across devices), and the series stitch.
Keep `GET /api/activity` returning its current flat shape, and `/api/weights` its
`[{date, weight}]` shape (manual `weight_kg` readings only), as compatibility shims
during the data-model change (the console reads neither any more). Remove the orphaned
`workouts` table + `/api/workouts` when convenient (unused by the UI).
