# OpenFit — guide for Claude Code

Self-hosted **health-data engine + app host** — NOT a tracker. OpenFit ingests
from your devices, derives and maintains one dataset you own (the **OpenFit
Derived Data**), and exposes it to apps. It ships **no tracking apps**; every
tracker/dashboard/planner is an external app that reads the data. Privacy-first:
data lives on your own hardware, nothing leaves your network except a plugin's
own calls to its device API. Full direction:
`docs/architecture/openfit-overview-and-design-pathway.md`.

## Stack (keep it lightweight — no frameworks without a real reason)
- Python + Flask, raw `sqlite3` (NOT SQLAlchemy/an ORM); PyYAML only to read
  an installed app's `app.yaml`
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
  `write_sample()`. Never bypass this pattern. A plugin writes only what the
  device measured — never a source's no-data default (a 0/placeholder it
  fills in for a calendar day it had no data), so the derived pick never
  reconciles a phantom (the contract is in `SyncPlugin.sync()`'s
  docstring). Series, sessions, instant readings and the daily vitals meet
  this by nature (each exists, or is non-null, only where something was
  measured), so the one path gated is the daily activity accumulators a
  source computes for the whole day regardless of wear — Garmin's (see
  Data model). Google Health is compliant by construction: it returns a
  daily point only for a day it has data. Don't extend the gate elsewhere.
- `app/metrics.py` — the canonical vocabulary, schema v2
  (`docs/architecture/metric-schema-v2.md`). One table, `METRIC_DEFS`, gives
  each metric its unit, category, grain and plausible range
  (`key → (unit, category, grain, (low, high))`); `METRICS` (key → unit, in
  the order the API lists metrics) and `PLAUSIBLE_RANGES` are built from it.
  Beside it the session kinds (`SLEEP`, `WORKOUT`, `DUTIES_ROSTERED`,
  `DUTIES_COMPLETE` in `SESSION_KINDS`) and the
  series metrics (`HEART_RATE`, `RESPIRATION`, `SPO2` in `SERIES_METRICS`).
  Import keys and kinds from here; never spell metric or kind strings out in
  plugins or the API. `CATEGORIES` is the 9 labels in display order —
  Activity, Vitals, Body composition, Fitness, Sleep, Nutrition & hydration,
  Women's health (no keys yet), Roster (operational context: the duty
  kinds and the roster app's hours), Other — and `GRAINS` the 4: `daily`,
  `instant` (a timestamped point like a weigh-in; a label for now, still
  filed one per day in `metrics`), `session`, `series`. `grain_for(key)` →
  `"series"` (in `SERIES_METRICS`), `"session"` (in `SESSION_KINDS`), else
  the metric's declared `"daily"`/`"instant"`; `category_for(key)` → its
  category (series metrics Vitals, `sleep` Sleep, `workout` Activity, the
  two duty kinds Roster); both
  raise `ValueError` on any other key. There is no `manual` grain: hand
  entry is a source. Grain and category are labels: nothing is stored or
  picked by them.
- `app/derived.py` — the one place a source is picked per `(date, metric)`:
  `recompute_derived()` writes `derived_metrics`. Never pick a source anywhere else.
  Beside it the source roles (`load_source_roles()`, `set_source_role()`,
  and `clear_source_roles(conn, source, only=None)` — every entry naming
  that source dropped, a session kind's too, or with `only` just that
  key's if it names the source, committing nothing: a step of
  `purge_source()`'s transaction). And the two contract reads an app's ctx
  shares with its route, so the two can't drift: `derived_points(conn,
  metric, since, until)` (`/api/metric/<key>` and `ctx.metric()`) and
  `session_rows(conn, kind, since=None, until=None)` (`/api/sessions/<kind>`
  and `ctx.sessions()`) — `[{start, end, source, summary}]`, the query,
  range (`substr("start", 1, 10)`, inclusive, built inline: `main` imports
  `derived`, never the reverse) and `source_rank()` order once; the caller
  checks `kind`.
- `app/crypto.py` — Fernet encrypt/decrypt for stored credentials.
- `app/apps.py` — the app scanner/loader, the one way the host finds an app
  (the Library reuses it): `scan_apps(apps_dir)` → the `InstalledApp`s that
  load, in folder-name order. An app is a folder in `APPS_DIR` (`$APPS_DIR`,
  default `/data/apps`, on the data volume; `app.config["APPS_DIR"]` in tests)
  with an `app.yaml` (`id` (default: the folder's name, `[A-Za-z0-9][A-Za-z0-9_-]*`),
  `name` (default: the id), `reads` (a list of keys — recorded, not
  enforced), `config` (an ordered list of field mappings `{key, label,
  type, default}` — order drives a tile's settings form; `key` a
  non-empty string, unique; the rest kept as written, unchecked; a
  mapping, or a field that isn't plain JSON (`json.dumps(allow_nan=False)`
  fails: a YAML date, `.nan`, `!!binary` — `/api/apps` sends the fields
  as written, so one would fail the whole listing), is malformed),
  `metrics` (the metrics the app **invents** — declare what you invent,
  write what exists: a canonical key it writes isn't listed — an ordered
  list read by `_metrics()`, each exactly `{key, label, unit, category,
  grain}` plus an optional `range` (`METRIC_FIELDS`; any other field
  refused, so a misspelt `range` can't mean none): `key` `APP_ID`-shaped,
  unique, and never in `RESERVED_KEYS` (every canonical metric, series
  metric and session kind — a custom key can't shadow a built-in);
  `label`/`unit` non-blank text (trimmed); `category` one of `CATEGORIES`
  as written; `grain` `daily`/`instant` (`APP_METRIC_GRAINS`; `session`/
  `series` refused — not yet); `range` `[low, high]`, finite non-bool
  numbers, low ≤ high, kept as written, or absent/null (any finite
  number); a bad block fails the load, as a bad `config` does — install's
  `check_app()` doesn't check it), all
  optional but type-checked, and its **size, required**:
  `sizes`, a non-empty list of the tile sizes it's drawn at, each `{w, h}`
  in cells, whole numbers 1–`MAX_CELLS` (6, Home's width), none twice,
  the first the default (a fixed-size app lists one) — or the older
  `default_size: {w, h}` alone, a list of one; both only when
  `default_size` is the first of `sizes`. `manifest_sizes(raw)` reads them
  and, like every rule here, raises `AppError` with the reason; no size at
  all → `NO_SIZE` ("no tile size: ..."), and YAML's `{w:1,h:1}` (no space
  after a colon, so keys `'w:1'`) is named as such. The host never makes a
  size up. `tile_size(raw)` → a `{w, h}` or None, for the layout) and,
  optionally, an `app.py` whose `render(ctx, cfg)` — and, if it has one,
  `handle(ctx, action, payload)`, the app's write entry point
  (`InstalledApp.handle`, else None: an app without one can't be actioned)
  — is imported once by `importlib` from its absolute path (nothing added
  to `sys.path`/`sys.modules`; `_entry_points()`). With no `app.py` the app still loads and lists,
  its render the empty spec "Nothing to draw yet: this app has no app.py"
  (`NOTHING_TO_DRAW`); any other files in the folder are the app's own. A
  folder with no `app.yaml` isn't an app (no
  log); one that doesn't load — bad YAML or field, an import
  error, no callable `render`, a `handle` that isn't callable — logs
  `[apps] skipping <folder>: <why>` and is
  left out, never raising; a duplicate id keeps the first folder. Scanned
  on every request (no restart for a new app); a loaded app is cached until
  its `app.yaml`/`app.py` mtime or size changes, so a broken one logs once per
  change. `find_app(apps_dir, id)` picks one. `app_present(apps_dir, id)`
  says whether an app is still on the volume, loading or not — its
  folder `<apps dir>/<id>` there as anything (a broken link included) or
  another folder's app loading under the id: `_taken()`, install's rule;
  an id failing `APP_ID` → False — which decides whether its data is
  orphaned (`/api/app-sources`). `InstalledApp.summary()` is
  what `GET /api/apps` lists: `{id, name, default_size, sizes, reads,
  config, metrics, files}` — `default_size` the first of `sizes`; `config` the
  manifest's fields as written (the schema of a tile's settings form);
  `metrics` `InstalledApp.metrics`, the declared metrics by short key
  (`range` a list or null, `[]` for none);
  `files` from `app_files(folder)`, the sorted names of the plain files
  directly in the folder (no sub-folder such as `__pycache__`, no link, no
  name `filename_problem()` refuses), so every name listed is one the file
  route serves. `install_app(apps_dir, files)`
  is the one way an app is added from the browser (`POST /api/apps`, the
  host's first write route): `files` `{name: text}`, in order — `app.yaml`
  among them and parsing (`yaml.safe_load`) to a mapping; its `id`
  (required here: no folder name to default to) matching `INSTALL_ID`,
  `[a-z0-9][a-z0-9-]*` with `\Z` (stricter than the scanner's, so an install
  always loads under its id); every file name plain (`filename_problem()`:
  non-empty, not `.`, no `/`, `\`, `..` or control character, writable as
  UTF-8) and every content a string; the id free (`_taken()`: no
  `<apps dir>/<id>` as anything, a broken link included, and no app in
  another folder loading under it) else 409 `"'<id>' already exists — use
  Update in its row to replace its files, or remove it first to
  reinstall."` (install only creates, never updates — that's
  `update_app()`); then
  `check_app(manifest, files)` — **the correctness layer that grows**; its
  one rule so far is a size (`manifest_sizes()`: "won't deploy without a
  size", refused 400 with the scanner's own reason); no other manifest
  field is checked, `app.py` isn't parsed, nothing is run. Only then is
  anything written: into a hidden
  `.install-<hex>` folder (the scanner skips dot-folders; `_stage()`,
  shared with update), renamed to `<id>` once whole, under a process lock
  (`_write_lock`, shared with update and uninstall), `APPS_DIR` made if
  missing; a failed
  write removes it (500). Every refusal raises `InstallError(message,
  status)` having written nothing; containment checks run before anything
  touches the disk and never move into `check_app()`.
  `update_app(apps_dir, app_id, files)` is the one way an installed app's
  files are replaced (`PUT /api/apps/<id>`, the Library's Update): the safe,
  one-step form of uninstall-then-add, its id and data kept. In order,
  before the app is touched: `app_id` (the URL's) matching the scanner's
  `APP_ID` else 400 "not an app id: ..." (uninstall's rule, so a
  hand-placed `My_App` can be updated); `files` a non-empty mapping, its
  `app.yaml` parsing (`_manifest_to_install()`); the manifest's `id` the
  app's own (`_update_id()`: absent, not text, or another →
  400 `"this updates '<id>', but the files are for '<other>' — an update
  keeps the app's id (Duplicate makes a copy under a new one)"`); every
  file name and content install's (`_check_files()`, shared). Then under
  `_write_lock`: the app installed — loading, its folder the one
  `find_app()` loaded it from, a direct child of `APPS_DIR` — else 404
  `"'<id>' isn't installed — use Add Application to install it"`;
  `check_app()`; the files staged in a hidden `.update-<hex>`
  (`_stage()`), which must load as the scanner loads it (`_loads()`:
  `load_app()` — the whole manifest, and `app.py` imported, unlike
  install, which runs nothing: a broken update would take down a working
  app) else 400 `"the update doesn't load, so '<id>' is unchanged:
  <why>"`, staging removed; then `_swap()`: the old folder renamed aside
  to a hidden `.update-old-<hex>`, the staged one renamed into its place
  (each atomic; the second failing → the old one renamed back; either →
  500, the app on its old files), the `_loaded` entry dropped, and the old
  folder deleted — a link unlinked, never its target; a delete that fails
  leaves it hidden, logged (`[apps] updated <id>, but couldn't delete
  ...`). A replace, not a merge: the folder holds exactly `files`, a file
  left out gone. No database work: what it wrote under `app:<id>` stays
  (a custom metric the new manifest drops leaves the catalog, readings
  kept, and is back when declared again; its source isn't orphaned while
  the app is installed). → `{"id", "has_app_py"}`; every refusal an
  `InstallError` with the app as it was.
  `uninstall_app(apps_dir, app_id)` is the one way an app is removed
  (`DELETE /api/apps/<id>`): the id, from the URL, must match the scanner's
  `APP_ID` (every listed id does, so a hand-placed `My_App` can go too;
  `..`, a `/`, a dot-folder never) else 400; the folder removed is the one
  `find_app()` loaded the app from — found by scanning, never built from
  the id, always a direct child of `APPS_DIR` — else 404 (an app that
  doesn't load isn't installed as far as the Library knows). Under the
  lock it's renamed to a hidden `.uninstall-<hex>` (one atomic step; a
  failed rename → 500, nothing moved) and its `_loaded` cache entry
  dropped (a reinstall into the same folder always loads afresh); then
  deleted — a link unlinked, never its target — and a delete that fails
  part way leaves only that hidden folder, logged, never a half-app.
  `read_app_file(app, name)` is the one way a file in one is read back
  (`GET /api/app/<id>/file?name=`): `name` must pass `filename_problem()`
  (400) and be in `app_files()` (404); its resolved path must still sit in
  the folder, and it's opened `O_NOFOLLOW` (404); over `MAX_VIEW_BYTES`
  (1 MiB) → 413, not UTF-8 → 415; else its text, as given. Nothing outside
  the app's folder is ever read. `UninstallError`/`AppFileError` are
  `InstallError`'s siblings, all `LibraryError(message, status)`.
- `app/host.py` — the app host: `Ctx(conn, today=None, app=None)`, built
  per render call for the app (`app` its `InstalledApp`; None, a ctx for no
  app, reads canonical keys only), is all an app's `render()` gets — its
  two reads, `ctx.metric()` and `ctx.sessions()`, each its contract
  route's answer from the same `app/derived.py` function.
  `ctx.metric(key, days=None)` → a
  list of `Point(date, value, source)` (a `NamedTuple`: `.date` a
  `YYYY-MM-DD` str, `.value` a float in the canonical unit — or an app
  metric's declared one — `.source`),
  ascending by date — `data[-1]` the latest day, `[]` for none — the derived
  points `GET /api/metric/<key>` serves (`derived_points()` in
  `app/derived.py`, shared by both), trailing `days` (`trailing_window()`:
  today − (days−1) … today) or all history; `key` a canonical metric or
  one the ctx's app declares, by its short key (`_resolve()`: read from
  `custom_metric_key(id, key)` = `app:<id>:<key>`); any other key —
  another app's, by either form — or a `days` not an
  int ≥ 1 (bools refused) → `ValueError`. **An app's own metrics**:
  `CustomMetric(unit, category, grain, range, app_id, key, label)` (a
  `NamedTuple`); `app_metric_defs(installed)` → one app's, `{app:<id>:<key>:
  CustomMetric}`; `custom_metric_defs(apps_dir)` → every installed app's
  (over `scan_apps()`, folder then manifest order) — the one place they're
  enumerated: with `METRICS` the effective vocabulary the catalog,
  `/api/coverage` and `/api/metric/<key>` serve (`app/metrics.py` stays
  canonical-only). `ctx.sessions(kind, since=None, until=None)` → a list
  of `Session(start, end, source, summary)` (a `NamedTuple`: `.start` ISO
  8601 UTC text as stored, `YYYY-MM-DDTHH:MM:SSZ`; `.end` the same or
  `None`; `.source`; `.summary` a dict parsed from `summary_json`, `{}` for
  none, a fresh copy each call), exactly what `GET /api/sessions/<kind>`
  serves, field for field and in its order (`session_rows()`, shared by
  both): every source's — a priority-ordered passthrough, no pick — the
  kind's role primary, then `DEFAULT_PRIORITY`, then by name, each source's
  by start; `[]` for none. `kind` in `SESSION_KINDS` (any kind, any source:
  all are shared — no per-app scoping until app-declared kinds), else
  `ValueError("unknown session kind: <repr>")`; `since`/`until` inclusive
  days matched on the start's UTC date (`?from=`/`?to=`'s rule), `None`
  open, each `YYYY-MM-DD` text or a `date` (`_iso_day()`, else
  `ValueError("since must be YYYY-MM-DD: <repr>")`/`until`), a `since`
  after the `until` → `ValueError("since must not be after until: <since>
  > <until>")`; no `days=`. On the base `Ctx`, so `render()` and
  `handle()` both have it (in `handle()` it sees the action's own
  uncommitted sessions); reads only. `ctx.today()` → the server's
  local date as `YYYY-MM-DD`. The spec builders (also module functions):
  `number(value, label, caption, unit)`, `line(points, title=None)` →
  `{"type": "line", "points": [[date, value], ...], "title"}` (points
  `(date, value)` pairs — the apps' `[(p.date, p.value) for p in data]` — or
  `Point`s, `{date, value}` dicts or numbers, each to a `[date, value]` pair,
  date null for a bare number; a date a `YYYY-MM-DD` str, `date` or `None`;
  `None`/non-finite is a gap; anything else `TypeError`; `title` text or
  `None`, else `TypeError`),
  `panel(*children)` (or one list), `input(key, action, label=None,
  kind="number", unit=None, options=None, accept=None, toggle=None)` →
  `{"type": "input", key, action, label, kind, unit, options, accept}`,
  plus `toggle` only when given (the one
  interactive element: a
  field and Save, which sends `action` to the app's `handle()` with `{key:
  what was entered}`; `key` and `action` non-empty text, `kind`
  `"number"`/`"text"`/`"select"`/`"file"` (`INPUT_KINDS`), `label`/`unit` text or
  `None`, else `TypeError`/`ValueError`; `options` a select's dropdown —
  required for one (missing/`None`/empty → `ValueError`), a list or tuple
  of non-empty strings (else `TypeError`, "a select's options must be a
  non-empty list of strings"), in the spec a plain list as written — and
  refused for every other kind (`ValueError`, "options is only for a
  select"), whose specs carry `options: None`; the host never checks a select's
  submitted value is one of its options — `handle()` does, as it checks a
  number; `accept` a file picker's hints (`".ics,.csv"`, `"text/plain"`),
  optional and only for a file: non-empty text as written (not text →
  `TypeError`, empty → `ValueError`, "a file input's accept must be
  non-empty text"), never enforced, and refused for the other kinds
  (`ValueError`, "accept is only for a file"), whose specs — and a file's
  without one — carry `accept: None`; `toggle` a file input's companion
  checkbox, optional and only for a file (another kind's → `ValueError`,
  "a toggle is only for a file"): a mapping of only `TOGGLE_FIELDS`
  (`_fields()`: not a mapping `TypeError`, another field `ValueError`) —
  `key` the payload key its checked state is sent under and `label` its
  text, each non-empty text (not text `TypeError`, empty `ValueError`),
  `key` never the input's own (`ValueError`: the file is sent under it),
  and `confirm` optional non-empty text the page asks before sending with
  it ticked — carried as `{key, label, confirm}` (`confirm` `None` for
  none), and no `toggle` key at all without one (`_toggle()`), so every
  input spec from before is unchanged; the payload is then `{key:
  FileUpload, <toggle key>: True/False}`;
  `docs/architecture/input-primitives.md`),
  `timeline(lanes, now=None, title=None, sync=None)` → `{"type":
  "timeline", title, now, lanes: [{label, grey_after_now, intervals:
  [{start, end, label}], [bands: [{start, end, label}]]}]}`, plus `sync`
  only when given (a generic
  two-lane interval timeline — a plan beside the
  actual — whose pixels are all the page's: `lanes` a list or tuple of 1
  or 2 (`TIMELINE_LANES`; else `ValueError`, not a list `TypeError`), in
  visual order, lane 0 left and lighter, lane 1 right and darker, coloured
  by index (no colour field); each lane a dict of only `LANE_FIELDS` —
  `label` text or `None`, `grey_after_now` a bool (default `False`; `True`
  for a lane with nothing after `now`, greyed below it), `intervals` a
  list (required, `[]` for an empty lane) of dicts of only
  `INTERVAL_FIELDS` — `start` and `end` both required (no open blocks, unlike
  `write_session()`) and normalised by `_session_instant()` to `iso_utc()`
  text, `end >= start` (compared as those strings), `label` text or `None`;
  and `bands`, optional, a list or tuple of dicts of only `BAND_FIELDS`
  (`start`, `end`, `label`) — spans the page shades behind the lane's
  blocks (a trip away, `label` the port), each checked as an interval is
  (`_band()` and `_interval()` both `_span(raw, fields, what)`, the
  messages naming "a timeline band"/"a timeline interval"), not a list
  `TypeError`; emitted only when non-empty (absent, `None` or `[]` → no
  `bands` key, so a lane without is exactly the lane from before bands);
  what a band spans is the app's to work out, the host only carries it;
  a lane, interval or band not a dict `TypeError`, an unknown field (a
  misspelt `gray_after_now`) `ValueError` (`_fields()`), a missing end, a
  time that isn't one or an end before its start `ValueError`; `now` normalised the
  same way or `None` (no line); `title` text or `None`, else `TypeError`;
  `sync` a focus group's name (`"frms"`), text or `None`, else
  `TypeError` — `None` or `""` adds no key, so an unsynced spec is
  exactly the one from before `sync`; the host only carries the name, the
  focus clock is the page's, below),
  `empty(message)`; the host's own is `{"type": "error", "message"}`. `render_app(app, conn, cfg=None,
  today=None)` calls `render(ctx, dict(cfg or {}))` (the ctx made for
  `app`) — `cfg` the
  instance's config, passed as given (the app owns its fallbacks,
  `cfg.get("stride_m", 0.75)`) — and never raises: an exception, a
  non-spec (not a dict with a string `type`) or a spec that isn't plain JSON
  (NaN, a set) is logged (`[apps] <id>: ...`, with the traceback) and comes
  back as an error spec. **Apps as sources**: an app writes only from its
  `handle(ctx, action, payload)`, run by `POST /api/app/<id>/action` (the
  one place an app writes) with a `WriteCtx(conn, app, today=None)` (`app`
  the `InstalledApp`) — a
  `Ctx` (the same reads) plus `ctx.write_metric(metric, value, date=None)`,
  which files through `write_reading()` (`app/plugins/base.py`,
  `write_metric()`'s upsert with the unit given) under
  `ctx.source`, `app_source(id)` = `APP_SOURCE_PREFIX` `app:` + the id,
  fixed when the ctx is made: no writer takes a source, so an app can only
  ever create or overwrite its own rows (an app whose id is `garmin`
  writes `app:garmin`). A canonical key is filed as itself, in its unit,
  held to the webhook's rule (`plausible_value()` in `app/metrics.py`: a
  finite non-bool number within `PLAUSIBLE_RANGES`); a key the app
  declares (its manifest's `metrics`), by its short key, is filed as
  `app:<id>:<key>` in the declared unit, a finite non-bool number
  (`finite_number()`) within the declared `range` if any
  (`_declared_value()`); any other key (undeclared, another app's by
  either form) → `ValueError("unknown metric: '<key>' - declare it in
  app.yaml's metrics to write it")`. `date` to `YYYY-MM-DD`
  (or a `date`; default `ctx.today()`), else `ValueError`, writing
  nothing; it commits nothing and notes the days written (`ctx.written`,
  sorted) for the route to re-derive. Beside it, `ctx.write_session(kind,
  start, end=None, summary=None)` (returns nothing) files one session
  through `write_session()` (`app/plugins/base.py`, unchanged — its own
  kind check kept as a backstop) under the same `ctx.source`, id
  `app:<id>:<kind>:<start>`, so a rewrite of that kind and start replaces
  its end and summary: `kind` in `SESSION_KINDS` (else
  `ValueError("unknown session kind: <repr>")`, checked first; existing
  kinds only — an app can't invent one yet); `start` required (`None` →
  `ValueError("a session needs a start")`, where `write_session()` itself
  silently skips one) and `end` optional (`None` → NULL), each a
  `datetime` or ISO-8601 text (`datetime.fromisoformat()`, a `Z` taken)
  normalised by `_session_instant()` through `iso_utc()` (naive = UTC;
  else `ValueError("<start|end> must be a datetime or ISO-8601 timestamp:
  <repr>")`); an end before the start once normalised (compared as
  `iso_utc()` strings, which sort as time) → `ValueError("a session's end
  is before its start")`, equal allowed; `summary` a dict or None (else
  `ValueError("a session's summary must be a mapping or None")`) that
  `json.dumps(allow_nan=False)` takes (else `ValueError("a session's
  summary must be JSON")`). It commits nothing and notes no day in
  `ctx.written` — nothing derives from a session (read back through
  `ctx.sessions()` or `GET /api/sessions/<kind>`), so a session-only
  action re-derives nothing and
  is still committed. The render
  path's `Ctx` has no writer. The app is trusted code, as its `render()`
  is: the scoping is the ctx, not a sandbox. **A file input's value**
  is a `FileUpload(path, name="", content_type="")`: `.name` the file's
  name as the browser gave it (display only, never a path),
  `.content_type` the type it reported (`""` for none; informational),
  `.bytes` its content (read from `path` when first asked, then kept),
  `.text(encoding="utf-8")` that decoded (a `UnicodeDecodeError`
  propagates — the app's to catch), `.path` the temp file (for a library
  that wants one, `sqlite3.connect(f.path)`). OpenFit carries the file
  and never parses it: the app does. The action route (`act_on_app()`,
  `app/main.py`) takes it as `multipart/form-data` (`request.mimetype`;
  any other body is the JSON path, unchanged): `action` a form field,
  `payload` and `cfg` form fields holding their JSON (`_form_json()`;
  absent → `{}`; a `payload` that isn't a JSON object the JSON path's
  400, a `cfg` `{}`), and each of `request.files` written by
  `_uploaded()` — a context manager — to a temp file it names
  (`tempfile.mkstemp`, prefix `openfit-upload-`, never the client's
  name) and set into `payload` under its field as a `FileUpload` (over a
  `payload` entry of the same key), after the body's checks (a 400
  writes no file); every temp file is removed in its `finally`, whatever
  `handle()` did (one already gone is fine; another failure logged) —
  nothing uploaded is kept. `MAX_UPLOAD_BYTES` = 25 MiB is Flask's
  `MAX_CONTENT_LENGTH` (set beside `DB_PATH`/`APPS_DIR`), so a body over
  it, on any route, is turned away unread with a 413, said by an
  `errorhandler(413)` as `{"ok": false, "error": "Request too large (max
  25 MB)."}`. Full contract:
  `docs/architecture/app-host.md`.
  The two real apps, `steps` and `steps-metres`, are committed in
  `tests/fixtures/apps/` (as deployed in `/data/apps`) and run against the
  host by `tests/test_app_host.py`: they're correct per the contract, so a
  failure there is fixed in the host, never by editing them. Their `app.py`
  is verbatim; their `app.yaml` declares `sizes: [{ w: 1, h: 1 }]`, the
  canonical form, where the deployed copies still say `default_size: { w:
  2, h: 2 }` — which keeps loading (tested from the fixtures, swapped back).
  Beside them, the first writer: `weight` (`tests/fixtures/apps/weight/`,
  deployed by installing it — the platform ships no apps), the end-to-end
  test of the write path (`tests/test_app_actions.py`). Its `app.yaml`:
  id `weight`, fixed (its source is `app:weight`), name "Weight", `sizes:
  [{ w: 1, h: 1 }, { w: 2, h: 1 }]`, `reads: [weight_kg]`. `render()`: the
  latest derived `weight_kg` as a `number` (`unit` kg, `caption` "<date> ·
  via <source>") over an `input` (`key` `weight_kg`, `action` `log`,
  "Log weight", kg), panelled — `empty("No weigh-ins yet")` in the
  number's place when there's none, so the first can still be logged.
  `handle()`: `log` only (anything else a `ValueError`); `payload
  ["weight_kg"]` coerced to a finite positive float (a bool, junk, `0`,
  `nan` → `ValueError("Enter your weight in kg, as a positive number")`),
  then `ctx.write_metric("weight_kg", kg)` for today — one a day, a
  second the same day replacing it. And the first to invent a metric:
  `caffeine` (`tests/fixtures/apps/caffeine/`, run by
  `tests/test_app_custom_metrics.py`, the end-to-end test of app metrics).
  Its `app.yaml`: id `caffeine`, name "Caffeine", `sizes: [{ w: 1, h: 1 },
  { w: 2, h: 1 }]`, `reads: [caffeine_mg]`, `metrics:` one — `caffeine_mg`,
  "Caffeine", mg, Nutrition & hydration, daily, `range: [0, 2000]` (stored
  `app:caffeine:caffeine_mg`). `render()`: the latest day's `caffeine_mg`
  as a `number` (mg, `caption` "today" or its date) over an `input` (`key`
  `caffeine_mg`, `action` `log`, "Caffeine today", mg), panelled —
  `empty("No caffeine logged yet")` before the first. `handle()`: `log`
  only; `payload["caffeine_mg"]` coerced to a finite float ≥ 0 (a bool,
  junk, a negative → `ValueError("Enter today's caffeine in mg, as a
  number")`), then `ctx.write_metric("caffeine_mg", mg)` for today, the
  declared range the host's to hold it to — one a day, replacing. And
  the first with a dropdown: `mood` (`tests/fixtures/apps/mood/`, run by
  `tests/test_app_actions.py`, the end-to-end test of an input of kind
  `"select"`). Its `app.yaml`: id `mood`, name "Mood", `sizes: [{ w: 1,
  h: 1 }, { w: 2, h: 1 }]`, `reads: [mood]` — no `metrics:` (`mood` is
  canonical). `render()`: the latest day's `mood` as a `number`
  (`caption` "today" or its date) over an `input` (`key` `mood`, `action`
  `log`, `kind` `"select"`, `options` `"1"`–`"5"`, "Mood today"),
  panelled — `empty("No mood logged yet")` before the first. `handle()`:
  `log` only; `payload["mood"]` one of the five strings, else
  `ValueError("Choose a mood from 1 to 5")` (the app holds the choice to
  its options, not the host), then `ctx.write_metric("mood", int(raw))`
  for today — one a day, replacing. And the first with a file picker:
  `import` (`tests/fixtures/apps/import/`, run by
  `tests/test_app_actions.py`, the end-to-end test of an input of kind
  `"file"` and the multipart route — the proof the app parses, not
  OpenFit). Its `app.yaml`: id `import`, name "Import weigh-ins", `sizes:
  [{ w: 1, h: 1 }, { w: 2, h: 1 }]`, `reads: [weight_kg]` — no
  `metrics:`. `render()`: how many days of derived `weight_kg` there are
  as a `number` (`unit` days, `caption` "since <the first's date>") over
  an `input` (`key` `file`, `action` `import`, `kind` `"file"`, `accept`
  `".txt,.csv"`, "Import weigh-ins"), panelled — `empty("No weigh-ins
  yet")` before any. `handle()`: `import` only; `payload["file"]` must
  have a `.text` (else `ValueError("Choose a file of weigh-ins to
  import")`), decoded with `text("utf-8-sig")` (not text → "<name> isn't a
  text file: expected 'YYYY-MM-DD <kg>' on each line"), parsed by the app
  a weigh-in a line — `YYYY-MM-DD <kg>`, apart by spaces or a comma, blank
  lines skipped; a line that won't → `ValueError("line N: expected
  'YYYY-MM-DD <kg>'")` before any write, none → "No weigh-ins in <name>:
  …" — then `ctx.write_metric("weight_kg", kg, date=day)` for each, on
  its own day, a refusal from the host (the plausible range) said as
  "line N: <why>" and the earlier lines rolled back by the route: all or
  nothing; a day replaced, a day twice the file's last line. And the
  first to write a session: `workout-log`
  (`tests/fixtures/apps/workout-log/`, run by `tests/test_app_actions.py`,
  the end-to-end test of `ctx.write_session()`). Its `app.yaml`: id
  `workout-log`, name "Workout log", `sizes: [{ w: 1, h: 1 }, { w: 2, h:
  1 }]`, `reads: []` — no `metrics:` (`workout` is an existing kind).
  `render()`: `empty("Log a workout with Save")` over an `input` (`key`
  `minutes`, `action` `log`, "Workout", min), panelled — it doesn't read
  sessions back (it predates `ctx.sessions()`). `handle()`: `log` only;
  `payload["minutes"]` coerced to a finite positive float (a bool, junk,
  `0` → `ValueError("Enter the workout in minutes, as a positive
  number")`), then `ctx.write_session("workout", start, end, {"type":
  "manual", "duration_minutes": minutes})`, `end` now
  (`datetime.now(timezone.utc)`) and `start` that many minutes before —
  each log its own session. And the first to file duties: `roster`
  (`tests/fixtures/apps/roster/`; its pure functions run by
  `tests/test_roster_app.py`, loaded by path as the host loads it, and
  the round trip by `tests/test_app_actions.py`, against the synthetic
  `tests/fixtures/rosters/sample.ics` — fake flights and ports). Its
  `app.yaml`: id `roster`, name "Roster", `sizes: [{ w: 2, h: 1 }, { w: 2,
  h: 2 }]`, `reads: [duty_hours_rostered, duty_hours_complete]`, and
  `metrics:` those two — "Rostered duty hours"/"Flown duty hours", `h`,
  Roster, daily, `range: [0, 24]`. Config-driven: three files, `app.yaml`,
  `app.py` (the engine) and `config.json` (the reference it reads — the
  round trip installs all three). The config: `duty_types`, an ordered
  list of rules `{type, prefixes: [...]}` or `{type, regex}`, the first
  match winning; `default_type`; `personal` (`home_base`, `timezone`); and
  `destinations` (IATA → `{city, tz}`, an IANA zone — not read yet: staged
  for the time-zone/layover work). The fixture's is synthetic (`CX` →
  flight, `^A\d` → sim, `R` → reserve, else other; home base and ports
  `ZZA`/`ZZB`); the owner's real one stays on their volume, never
  committed. Read once at import from beside the module (`__file__`, so
  wherever the host loads it — installed or staged by Update) into
  `CONFIG`; missing, not JSON, or not a dict with a `duty_types` list →
  `CONFIG` None and a plain `CONFIG_ERROR` ("config.json is missing —
  reinstall the app with one", "config.json won't read: …", "config.json
  needs a duty_types list"), never raising, so the app still loads (and
  can be Updated). Guarded at the edge, never in the parser: `render()`
  then returns `empty(CONFIG_ERROR)` over the picker (the picker last, at
  the foot), `handle()` raises
  `ValueError(CONFIG_ERROR)` before it reads the upload. `app.py` stdlib
  only (no `icalendar` in the image), its logic module functions:
  `parse_events(text)` (unfold — a CRLF/LF then a space or tab removed —
  then each `VEVENT`'s `SUMMARY`/`DESCRIPTION` (RFC 5545-unescaped) and
  `DTSTART`/`DTEND`: `VALUE=DATE` (or bare `YYYYMMDD`) a date, all-day;
  `…Z` an aware UTC `datetime`; a `TZID=` or floating time →
  `ValueError("roster has a non-UTC time, which isn't supported yet:
  <the line>")`; an event that won't read skipped with a `print`; a
  nested component, an alarm, lends nothing), `duties(events)` (every
  timed event with both ends, in file order — duty-ness structural,
  never a list of codes: all-day `O`/`G`/`U`/`L`/anything skipped; no
  end, or an end before the start, skipped and logged; each event's
  description handed to `classify()`), `classify(title, config=None,
  description="")` (labels only, never filters; `config` defaults to
  `CONFIG`: after a leading `DD-Mon`, `code` the first token and its type
  `_duty_type(code, config)` — the first `duty_types` rule matching by
  `regex` (`re.match`) or `prefixes` (`startswith`), else `default_type`;
  no carrier in the code — and a `flight` gets `flight_no` (the code,
  joined to an all-digit next token: `CX 105` → `CX105`), `dep`/`arr`
  the duty's origin and final destination — the first leg's departure,
  the last leg's arrival — from its chain of `XXX-XXX` legs, the
  description's (every sector, a line each) else the title's, so a day
  turn in one duty (`CX 903 HKG-MNL / CX 902 MNL-HKG`) is `HKG`→`HKG`,
  base to base, and no trip; `position` a trailing 2–3-letter code of
  the title),
  `bucket_for(end, now)` (`end <= now` → `duties_complete`, else
  `duties_rostered`), `filed_as(end, now, backfill=False)` (the write
  matrix, `[(kind, capture)]`: its `bucket_for()` kind, `"live"` — and,
  backfilling, a duty already over (`duties_complete`) filed into the
  plan too, `(duties_rostered, "backfill")`; a duty still to come once
  either way), `session_summary(duty, capture="live")` (the summary
  below, `capture` as given), `daily_hours(duties, now, backfill=False)`
  (each kind's hours by `DTSTART`'s UTC date, a duty past midnight whole
  on its start day, each kind `filed_as()` files it as — so a backfilled
  day has `duty_hours_rostered` too; a kind+start twice counted once)
  and `trips(sessions, home_base,
  open_end=None)` (one lane's duty `Session`s grouped into trips away
  from base, by flights alone — a session whose summary `type` is
  `flight` with `dep` and `arr` and an end, sorted by `start`; a reserve,
  sim, ground duty or routeless flight never opens or closes one: a
  flight out of base (`dep == home_base`) opens a trip — `start` its
  start, `out_port` its `arr`, `id` its start, the stable layover id
  (for the report store to come; never in the spec) — the next flight
  into base (`arr == home_base`) closes it at its end, a sector between
  is inside it (`HKG-BKK, BKK-SIN, SIN-HKG` one trip); a flight into base
  with none open is nothing; a flight out of base with one open drops it
  (its way home isn't in the data) and opens anew; base to base opens
  nothing; still open at the end → closed at `open_end` (UTC text),
  never before its flight out's end (`max`), or dropped when `None` →
  `[{id, start, end, out_port}]`, in order). `handle()`: `upload` only;
  `payload["file"]` decoded `utf-8-sig` (not text → "<name> isn't a text
  roster"); no duty (a file of days off, not iCalendar, empty) → "No
  duties found in <name>"; `backfill` `payload.get("backfill") is True`
  (the picker's toggle, below — only its JSON `true`; absent, `false` or
  anything else off); `now` taken once; each duty, for each `(kind,
  capture)` of `filed_as(end, now, backfill)`,
  `ctx.write_session(kind, start, end, summary)` with `start`/`end` the
  `DTSTART`/`DTEND` datetimes and the summary `{capture, type, code,
  label, raw, [flight_no, dep, arr, position]}` — `capture` `"live"`, or
  `"backfill"` on a past duty's rostered copy (the paper trail: a plan
  put back is always told from one captured live; its id the one a live
  upload of it had, so it replaces that plan); then each `(metric, day)`
  total of `daily_hours(found, now, backfill)` once,
  `ctx.write_metric(…, date=day)` — a day over the declared 24 h refused
  as "<day>: <why>". Backfill off is exactly the plain clock split. All
  or nothing (the route rolls back). `render()` reads the duties back, both kinds through
  `ctx.sessions(kind, since=…)`, `since` 183 days before `ctx.today()`
  (about six months, by the start's UTC date) and no `until` (every duty
  to come): none → `empty("Upload your roster .ics")` over the picker
  (`input` `file`/`upload`, kind `"file"`, `accept` `.ics`, "Upload
  roster", `toggle` `BACKFILL` — `{key: "backfill", label: "Backfill",
  confirm: "Backfill past duties into your rostered plan? They're also
  recorded as flown, and any plan you already uploaded for those duties
  is replaced."}`), a roster all older than that included; else
  `panel(timeline, picker)` — the upload at the tile's foot — the
  `timeline` lane 0 "Rostered" (`duties_rostered`), lane 1 "Flown"
  (`duties_complete`, `grey_after_now`), each session an interval of its
  `start`/`end` labelled with `summary["code"]` (`""` for none; a session
  with no end, only another source's, left out — `_lane()`), and each
  lane's `bands` its own `trips()` (`_band()`: `{start, end, label:
  out_port}`), home base `_home_base()` — `CONFIG["personal"]
  ["home_base"]`, None (no bands) without one — the rostered lane's
  `open_end` its latest duty's end (the loaded roster's), the flown
  lane's now (`_utc_text(now)`: a trip flown out and not back runs to the
  now line); `now` `datetime.now(timezone.utc)`, taken once for both,
  `sync` `"frms"` (the FRMS focus group:
  a later FRMS panel naming it scrolls with the roster, by time). The
  timeline fills the rest of the tile
  and scrolls, laid out to its size by the page (`render()` isn't told
  it). The `duty_hours_*` metrics are still written; the tile no longer
  reads them (`app.yaml`'s `reads` still names them).
- `app/home_layout.py` — the Home tab's layout, one `settings` row
  (`LAYOUT_KEY` `home_layout`, no migration): `{"tabs": [{id, name,
  mode: "flow", subtitle, items: [{instance_id, app_id, size: {w, h},
  config}]}], "default_tab"}`. A tab is one flow layout plus its `id`
  (`TAB_ID`, a slug of up to 32 `[a-z0-9-]`, not starting with `-`) and
  `name` (the strip label, ≤ `MAX_TAB_NAME` 40); `default_tab` is one
  tab's id. An item is an **instance** — a placement of an app, not the
  app: its own `instance_id` (`INSTANCE_ID`, `[A-Za-z0-9_-]{1,64}`,
  unique across every tab; the server mints them, `new_instance_id()`,
  `secrets.token_hex(8)`), `size` and `config` (a dict: the values of
  its app's config fields, set from the tile's gear in Configure, `{}`
  until any are; passed to `render()` as `cfg`) — so one app can be placed
  more than once, across tabs or on one, as stored, though the page never
  does (the same app twice is a copy under its own id: app-level
  Duplicate, so one instance, one config an app id). Flow is the one mode, per tab: the
  items' order is the layout, each size one its app declares; no
  coordinates (`mode` holds the seat for a later grid mode, so that's a
  second mode, not a migration). `load_layout(conn, apps)` →
  `reconcile(stored, apps)` against the apps installed now
  (`scan_apps()`). A row with no `tabs` list — the shape from before tabs,
  `{mode, subtitle, items: [{app_id, size}]}`, or none, or junk (it can be
  written through `/api/settings`) — is upgraded first (`_upgraded()`):
  one tab `home`/"Home" with its subtitle and an instance per item (a
  fresh id, `config {}`; a repeat of an app dropped), so it reads as
  exactly what it showed — no migration; the next save stores the new
  shape. Then per tab: stored instances in order, less any whose app
  isn't installed, or whose `instance_id` is invalid or already used on
  any tab (the first stands); each at its stored size if its app still
  offers it, else the app's default; `config` kept if a dict, else `{}`;
  a tab that isn't one, or whose id is invalid or a repeat, dropped; a
  name a save wouldn't take reads as the id, a non-text subtitle as `""`,
  `mode` always `"flow"`; no tab left → an empty `home` tab; a dangling
  `default_tab` → the first tab. Then the **≥1-instance invariant**:
  every installed app with no instance on any tab gets one (a fresh id,
  its default size, `config {}`) at the end of the default tab, in folder
  order — so there's no installed-but-unplaced app, and an install
  appears on Home. A read never rewrites the row (so an unsaved
  instance's id is that read's alone), and an app uninstalled and
  reinstalled before the next save comes back where it was, id and
  config included (an update, `update_app()`, never leaves the layout). `parse_layout(body)` checks a save's shape
  only: `tabs` a non-empty list, each `{id` (`TAB_ID`, unique), `name`
  (text, not blank, ≤ 40), `mode` `"flow"`, `subtitle` (text ≤
  `MAX_SUBTITLE` 120), `items` (a list)`}`; each item `{app_id` (the
  scanner's `APP_ID`), `size` (`tile_size()`), `config` (a dict; absent →
  `{}`), `instance_id` (`INSTANCE_ID`, unique across the whole layout;
  absent → minted, in a second pass so a fresh one can't take an id given
  further on)`}` — the same app any number of times; `default_tab`
  coerced to the first tab's id if it names none — dropping anything
  else, else `LayoutError` (messages name the tab, `tab '<id>': ...`). It
  never checks which apps are installed or what sizes they offer, so it
  doesn't enforce the invariant: that's the read's. `save_layout()` stores
  it. Routes: `GET /api/home-layout` (reconciled) and `PUT
  /api/home-layout` (`{ok: true}`, or 400 storing nothing).
- `app/templates/index.html` — the data console, one self-contained page
  (vanilla JS, no build step, no external calls or web fonts; every colour/font/
  radius in its `CUSTOMISE OPENFIT HERE` token block — the timeline's two lane
  tones, `--timeline-a`/`--timeline-b`, and the ink each takes,
  `--timeline-a-ink`/`--timeline-b-ink`, and its bands' amber,
  `--layover` (`#c07d18` light, `#e0a83a` dark), in all three theme
  blocks). Under the top bar
  (date, a `settings` button, the theme toggle) sits the original angled tab
  strip: Home's tabs (far left, one per tab of the layout, drawn by
  `paintTabStrip()` into `#layoutTabs`, a `display: contents` region — until
  the layout is read, a stand-in `home`/"Home"; each a `.tab` with
  `data-tab` = its id and its name as text, the installed apps' tiles —
  below) and **Data** (far right, the console), then the dashed **＋ Add**,
  shown only while Configure is on (it makes a tab — below).
  The Data tab is two even, fluid columns (`.console`, a grid of two
  `.console-col`s, capped at 1600px): Connected sources over Your data on
  the left, **Applications** on the right (below); at 1240px and under it
  drops to one column, at the old 940px cap, Applications under the data —
  narrower than that, a Your data row would squeeze the metric's name. `settings`
  opens the Settings popup (the `.modal` chrome): one control, **Opening
  page**, a dropdown built from the tab strip (`.tab[data-tab]`, so Home's
  tabs, by name, and Data are in it), saved per browser in `localStorage`
  `of-opening-tab`; boot opens that tab via `showTab(openingTab())` in place
  of a hardcoded default: Data if unset; before the layout is read the
  stored choice stands (Home's panel, loading — drawn once read); once it's
  read, one no longer on the strip (a tab since deleted) is Home's default
  tab. `showTab(name)` is split: a `PANEL_TABS` name (`data`) shows its own
  panel (`showPanel()`); anything else is a layout tab, `switchTab(id)` —
  the one `data-panel="home"` panel, `home.activeTab = id`,
  `renderActiveTab()` — and `markTabs()` sets `.active`/`aria-current` on the
  panel's tab, or on Home the layout tab it draws. Add source once every
  connector is connected, and Applications' Browse / Build / Create each
  open one shared coming-soon placeholder modal
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
  popup's `.src-msg` and leaves it — with **Remove source** at its foot. An
  OAuth source (`add_flow` `oauth`; not Garmin) also has a plain, non-danger
  **Re-authenticate** button beside its Connected via value: it closes
  Settings and opens `openOAuthDevice(c, true)` — the add flow's consent
  modal minus the client fields and the `add_note`, which `POST`s
  `oauth/start` with `{}` at once (the server fills in the stored client
  details) and goes straight to the approval link; the pasted address/code
  `POST`s `oauth/finish`, and on success the modal closes, the sources card
  refreshes and the row's pill reads "✓ re-authenticated" for a moment
  (`flashSource()`); an error (`finish`'s own, the no-refresh-token help
  included) shows in the modal. Only the refresh token is replaced:
  `_store_account()`'s upsert keeps the account's Added date, and its data,
  cadence and source roles are untouched. No
  global cadence control: the universal Settings popup keeps Opening page
  only. Remove is a two-step gate in
  the same modal, no `confirm()`: "Disconnecting stops future syncs." over a
  danger panel "Wipe data?" ([Keep data] / [Wipe data]), then "Are you
  sure?" naming the source and whether its data goes ([Cancel] / [Remove] or
  [Remove & wipe]); confirming `DELETE`s `/api/connectors/<id>` (`?wipe=1` on
  the wipe path), then refreshes the sources card and `loadData()`.
  An uninstalled app's data (an `orphaned` entry of `GET /api/app-sources`,
  read into `state.appSources` by `loadAppSources()` — at boot, after an
  app is uninstalled or installed, after a removal — which repaints the
  card and any open Your data row) is a `.src.orphan` row after the
  connected ones: its head (`.orphan-head`, a button: chevron, its id, its
  source under it, `.src-note`) and a `.pill.wait` "○ app uninstalled"
  and a red **Remove all data** (`btn sm danger`) — no Sync now, no
  Settings; a live app's source gets no row. The head opens it
  (`expandable()`, the open sources in `state.orphanOpen`, so it stays open
  across a refresh) on `orphanMetrics()`: a line per key in its `metrics`
  (`.orphan-metric`, `metricName()` — an orphaned custom one by its short
  key, prettified) with its own red **Remove data**; none left → a note
  that Remove all data takes its sessions/samples. A Your data row's
  source list (`fillSources()`, metrics and series/sessions alike) ends
  with `orphanLines(row, …)`: for each of its sources an orphan,
  "`app:<id>` is from an uninstalled app." and a Remove data for that
  row's metric (`row.key`). Every one opens `openRemoveAppData(o,
  metric)` — `metric` null (Remove all data) for the whole app: a
  `.modal` titled with the source, one `danger-panel` "Remove data?" —
  "Remove all data from the uninstalled app '<id>'? <`appDataSummary()`:
  N readings across M metrics (their names), then any sessions and
  samples>. This can't be undone."; a metric: titled with its name, "From
  <source>, whose app is no longer installed. The rest of what it wrote
  stays." over "Remove <name> from the uninstalled app '<id>'? This can't
  be undone." — [Cancel] (focused) / [Remove data]; no `confirm()`.
  Confirming `DELETE`s `/api/app-sources/<id>` (`api.removeAppSource()`)
  or `/api/app-sources/<id>/<metric>` (`api.removeAppSourceMetric()`),
  then closes and re-reads the app sources, `loadData()` and
  `loadHome()`; a refusal (the app reinstalled meanwhile: 400) stays in
  the modal, the button back. Every id, source, metric and count is set
  as text.
  "Your data" groups its rows by `category`, the 9 in `CATEGORIES` display
  order (one outside them would follow, by name; one with no rows at all,
  Women's health today, gets no header), and within a category sorts them
  by `grain` — daily, instant, session, series (a stable sort, so ties keep
  vocabulary order). Each category has a `.dgroup` header with a muted
  "N reporting · M empty" count, then its reporting rows, then — if it has
  any `count: 0` rows — a fold: a "M not yet reporting in <category>"
  button over those rows (still dimmed, still expandable), hidden until
  opened, so a category with nothing reporting shows its header and the
  fold alone. A fold's open state (`state.foldOpen`, by category) persists
  across a refresh, as an open row's does (`state.open`); a fold also
  renders open while one of its rows is expanded, and closing it closes
  its rows. The row's third column is its grain (Daily / Instant /
  Session / Series, `.d-grain`), under a "Grain" caption. A row from the
  catalog's `metrics` list (`row.cov === 'metrics'`) — `daily` or `instant`
  grain — renders as a picked metric (latest value, draggable source
  priority); a session kind's latest session is the one with the latest
  `start`, since `/api/sessions` lists by source priority. An installed
  app's own metric (a catalog entry with `app`/`label`) is a row in its
  declared category like any: named by its `label` (`buildRows()` notes
  it in `CUSTOM_LABELS`, which `metricName()` reads; an `app:<id>:<key>`
  with none — an orphan's, in `appDataSummary()` — is its short key,
  prettified), a small `.d-app` "app" mark beside it (`.d-name.custom`,
  the label ellipsised, the mark whole; the tooltip "… · defined by the
  app '<id>'"), and — with no `/api/source-roles` entry — its source from
  the catalog's `sources`, never draggable, noted "Defined by the app
  '<id>': only it writes this metric." ("… Nothing logged yet." with
  none). **Applications**
  is two stacked blocks in the right column, never one card. On top, the
  controls card (`#appsCard`, painted once at boot by `paintAppControls()`):
  the title and its voice button, then **＋ Add Application** (Add source's
  style; it opens the install popup, below) with **Browse** / **Build** /
  **Create** grouped at the far end — placeholders, each opening
  `comingSoon()` — then a status line (`#appsMsg`, empty until an install
  reports, `appsNote()`), and no app rows. Below it,
  the list panel (`#appListCard`, `paintApps()`), the right column's
  equivalent of Your data's list: a `.alist` container of rows, one per
  app `GET /api/apps` lists, its name (`esc()`d; `/api/apps` lists only
  apps that load and carries no status), and the list's states — "No
  applications installed" when there are none, a Retry when the read
  fails. Read at boot (`loadApps()`, which returns what it read, or null),
  separately from Home, and again after an install, update or uninstall. A row
  expands as a Your data row does — one mechanism, `expandable(item, head,
  o)`, which both `rowItem()` and `appRow()` hand their item and head to:
  the head a button, its click showing/hiding the panel, the chevron
  turning (`.open`), `aria-expanded`/`aria-controls` kept; the open ids in
  a Set (`state.open`, `appsList.open`) so a row stays open across a
  refresh. An app row is a `.ditem.aitem`, its head a `.drow.arow`
  (chevron · name); open, `appPanel()` is a `.dpanel.apanel` of three
  columns (one on a phone): **Reads** (the manifest's keys, `esc()`d, the
  metric's name on hover — a key the app invents by its declared label; none → a muted "No declared reads"), **Files**
  (each a button opening `openAppFile()`: a `.wide` modal reading
  `GET /api/app/<id>/file` into a read-only monospace `<pre>` as
  `textContent`, never markup; an empty file or the server's refusal shown
  as a message), and **Writes** — `writesTicker()`, a reserved slot
  (Day / Wk / Mth, each `—`; nothing counted or read), marked `//
  writes-ticker: filled in when app-tier write-back exists`. Under them
  **Refresh** (`btn sm`: re-fetches `POST /api/app/<id>/render` with the
  tile's config, `tileCfg()` — the existing route, no rescan, no reload —
  and `repaintTile()` redraws that
  app's Home tile in place from the fresh spec; a render that failed says
  so), then at the far end **Update**, **Duplicate** (both `btn sm`,
  `aria-haspopup="dialog"`) and **Uninstall** (`btn sm danger`). Update
  and Duplicate both start with `readBack(btn, verb)`: every file in
  `a.files` read back (`api.appFile()`, the button off meanwhile, one
  `Promise.all`; an empty file is read empty) — any read that fails
  (a non-text file's 415, a network error) stops it with "Couldn't read
  <file> (<why>) — can't <update|duplicate> <name>." in the row and
  nothing opened — as `[{name, text}]`. Update then opens the Add popup
  on them as they are, in its update mode: `openAddApp(current, null,
  a)` (below). Duplicate forks the app, client-only:
  `dupeTarget(a, appsList.apps)` → `{id, name}`, the
  lowest n ≥ 2 with neither `<baseId>-n` nor `"<baseName> n"` installed
  (`baseId` the id less a trailing `-<digits>`, lowercased with `_` → `-`
  so a hand-placed `My_App` gives a valid install id; `baseName` the name
  less a trailing ` <digits>`; so `steps`/"Steps" → `steps-2`/"Steps 2",
  and a copy's copy counts on from the same base), `bumpManifest(yaml,
  id, name)` rewriting the `app.yaml` as a line edit, not a parse (the
  last top-level `id:` line, as `manifestId()` reads it, → `id: <id>`;
  the last `name:` → `name: "<name>"`, double-quoted with `\`, `"` and
  control/line-separator characters escaped — or, with none, inserted
  under the id; no `id:` line (it defaulted to the folder) → one inserted
  above the first top-level key; line endings kept, every other line as
  written), and `openAddApp(copy, name)` opens the Add popup on the copy.
  Nothing is written until Install there: the copy installs through
  `POST /api/apps` like any app (the server's 409 the backstop for a
  clash), and reconcile gives it its own instance on the default tab — a
  fork: later edits to one app's files don't reach the other.
  Uninstall only opens an inline
  `danger-panel` ("Uninstall <name>?" / "This deletes its files from
  `/data/apps/`." — [Cancel] / [Uninstall]; no `confirm()`); its confirm
  `DELETE`s `/api/apps/<id>`, then `loadApps()` + `loadHome()` (the row and
  the tile go) and `loadAppSources()` (what it wrote stays, orphaned now:
  Connected sources' Remove data) and notes "✓ Uninstalled <name>"; an
  error stays in the row. Alerts are their own branch. **The Add Application popup** (`openAddApp(prefill,
  copyOf, update)` — ＋ Add Application calls it bare; Duplicate passes the copy's
  `[{name, text}]` and the source's name, and the popup is titled
  "Duplicate application", says it's a copy, and starts with `app.yaml`
  and `app.py` in their slots and an added slot per other file, each
  filled through `fileSlot()`'s `prefill` `{name, text}` as values, never
  markup — all editable before Review; Update passes the app's files and
  the app itself as `update`, and the popup is that app's update: titled
  "Update application", the same slots filled with its files untouched,
  the id shown locked over them (`data-f="locked"`: "Application:
  `<id>`" and a Locked tag — no field: the id lives in `app.yaml`),
  Review reading the id with `updateId(text, keep)` (the same parse,
  `yamlId()`, held to the app's own id, any `APP_ID` form, as the
  server's `_update_id()`; another or none says so, "This updates
  "<id>", but app.yaml's id is "<other>": …", Update disabled) and
  showing "Update application: `<id>`", no tab picker (the app is
  placed already), and at its foot the confirm — a `danger-panel`
  (`.update-sure`, `data-f="sure"`, shown only when the id holds)
  "Update <name>?" / "This replaces the app's files. Its data is kept." —
  over [Cancel] [Back] **Update**; only Update sends anything:
  `api.updateApp(id, files)` → `PUT /api/apps/<id>` `{files}`, then as
  Install's success (`appInstalled(res, 'Updated')`, "✓ Updated
  <name>"), a refusal (the server's message — an update that doesn't
  load) staying in the popup, nothing changed;
  the `.modal` chrome widened by `.wide`) builds the app folder as file
  slots (`fileSlot()`): `app.yaml` (Required) and `app.py` (Optional), each
  a monospace paste `<textarea>` with an **Upload** button beside it
  (a hidden file input; the file's text fills the same box), and **＋ Add
  file** for named slots (a name field, Upload — which also fills an
  empty name — and Remove). **Review** gathers them (`slotFiles()`: an
  empty `app.py` or a blank added slot is left out; a bad or duplicate
  name stops on the files step, the server's rules mirrored by
  `fileNameProblem()`), then the confirm step reads the id from the
  `app.yaml` — there's no id field — with `manifestId()`, a parser-free
  preview (`yamlId()`: the last top-level `id:` line, quotes and a
  trailing comment stripped; checked against the server's `INSTALL_ID`): "Install
  application: `<id>`", or why it can't (empty, no id, an unclosed quote,
  a bad id) with Install disabled; under it, only when Home's layout
  (`home.layout`, as last read) has more than one tab and Configure is
  off (`!home.configuring`), an **Add to tab** `<select>`
  (`.field.install-tab`, the modal's own field styling) of its tabs by
  name, in order, the layout's `default_tab` selected — one tab, none
  read yet, or Configure on (its save writes the working copy, which
  doesn't hold the new app, so a pick would be lost: the new tile joins
  the tab on screen and is moved like any tile), and it stays hidden;
  the file names as chips; and, when any
  file ends `.py`, "This app includes custom code and runs with full
  access to your OpenFit data." (`CUSTOM_CODE`). **Install** `POST`s
  `/api/apps` `{files}`: on success the popup closes, a tab picked
  (the picker shown) goes to `placeNewAppOnTab(id, tabId)` — in Home's
  section, the layout's one write besides Configure-off: it reads `GET
  /api/home-layout` (the server's reconcile has already put the new
  app's instance on the end of the default tab), and unless the tab is
  that default lifts the instance — id, size, config — onto the end of
  the chosen tab and `PUT`s the layout once; a failed read, a tab it
  doesn't hold or no instance found leaves it on the default, as before
  the picker — and then
  `appInstalled(res, done)` re-reads Home and the list at once, then notes "✓
  Installed <name>" — or, if the new id isn't listed (installed but the
  scanner skipped it), that it doesn't load and the server's log says
  why; an error (the server's message, as given — taken id, parse error)
  stays in the popup. Every name and id reaching `innerHTML` is `esc()`d.
  The page's checks are UX only; the server re-checks everything. A
  test runs `fileNameProblem()`/`manifestId()`/`updateId()` under node
  (skipped without it) against the server's own checks. A round voice button
  after the "Connected sources", "Your data" and "Applications" titles plays
  that card's clip (`/audio/connected-sources.mp3`, `/audio/your-data.mp3`,
  `/audio/applications.mp3` — the last not recorded yet, so it 404s and
  the button resets: a TODO in `VOICE_CLIPS`) through one shared
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
  `/api/sync/<plugin>`), and the app routes (`/api/apps`, `POST /api/apps`,
  `PUT /api/apps/<id>`, `DELETE /api/apps/<id>`, `POST /api/app/<id>/render`, `POST
  /api/app/<id>/action`, `/api/app/<id>/file`, `/api/app-sources`, `DELETE
  /api/app-sources/<id>`, `DELETE /api/app-sources/<id>/<metric>`) and Home's layout (`GET`/`PUT /api/home-layout`);
  not `/api/activity` (the last-sync stamp that read it
  is gone). See `docs/architecture/frontend-redesign-and-widget-model.md`.
  **Home** reads `GET /api/apps` and `GET /api/home-layout` together, then
  every `POST /api/app/<id>/render` in parallel, each with its instance's
  config (`api.renderApp(id, cfg)` → `postJSON()`, `{cfg}`; `cfgByApp()`
  over the layout read — mid-Configure the working copy, the tab on screen
  flushed into it first, so an unsaved setting holds; a failed fetch is that
  app's error tile), and is re-read after a Sync now or a source removal,
  as the data card is, and after an app install, update or uninstall; an app row's
  Refresh redraws just its own tile (keeping its Configure controls).
  Home draws one tab of the layout at a time, the one picked on the strip
  (`home.activeTab`; none picked, or one that's no tab, → the default,
  else the first). The parts, kept apart: `loadHome()`, the fetch — the
  apps, the layout and a spec each, into `home.specs` (`Map(app_id → {a,
  spec})`, folder order) and `home.layout` (`{tabs, default_tab}` as read;
  mid-Configure the working copy is kept instead, the tab on screen
  flushed into it) — then `renderActiveTab()`, the render from memory (a
  tab switch fetches nothing): the tab's items → `home.tiles`,
  `[{instance_id, app_id, name, spec, w, h, sizes, config}]` in layout
  order — keyed and deduped by `app_id` (one tile an app; safe because
  every app is on one tab: a tile moves, never copies; and for good — the
  same app twice is a copy under its own id, never a second instance) and
  each rendered with its instance's config — and its subtitle
  → `home.subtitle`, then the sub-header, the tiles and the strip; an
  empty tab says so ("Nothing on this tab yet"), apart from no apps
  installed; `layoutTiles` (= `flowLayout(items, apps, elsewhere)`, the
  server's reconcile mirrored — it squares the tab's read instances with
  the apps read beside it, and an unsaved Configure with an install or
  uninstall made meanwhile, carrying each instance's `instance_id` and
  `config`; an app placed nowhere — not on this tab nor among
  `elsewhere`, the apps the other tabs place — goes on the end with no
  `instance_id` (the save mints one) and `config {}`; node tests hold it
  to `reconcile()`) gives the order and each size; `paintTiles(box, tiles, configuring)` draws them
  on a CSS grid six cells across (`GRID_COLS`, = `MAX_CELLS`):
  `grid-template-columns: repeat(6, var(--tile-cell))`, `grid-auto-rows`
  the same (square cells), `grid-auto-flow: row` (never `dense`: a row-end
  gap stays empty, order-faithful), each tile `span w` × `span h` (no start
  cell). `--tile-cell` is `clamp(--tile-cell-min 90px, (100cqi − 5 gaps) /
  6, --tile-cell-max 180px)` against `.home`'s width (a container), so a
  resize changes only a cell's pixels — the layout never moves; past the
  max the grid centres (`width: max-content; margin: 0 auto`), under the
  min `.home` scrolls sideways (no phone pass yet). Tiles are chromeless
  (the card surface, no title bar) in view. Over the grid, the sub-header
  (`#homeHead`, `paintHomeHead()`): the subtitle (`.home-sub`, text) on the
  left and the **Configure** toggle on the right (`.tab.tab-add.home-cfg`,
  no `data-tab` so it's no Opening page; `aria-pressed`, `.active` (the
  accent) while on; disabled until Home has loaded), then `#homeMsg`.
  `toggleConfigure()`: on, every tile gets `tileControls()` — a handle
  (`.tile-grip`), beside it, only if its app declares config fields
  (`configFields()`, the app's `config` from `/api/apps`), a gear
  (`.tile-gear`, "Settings of <name>") opening `openTileConfig(t)`: a
  `.modal` "<name> settings" with a field per schema entry, in order —
  `label` else `key`, `esc()`d; a `number` field `type="number"
  step="any"`, any other type text — each `.value` the instance's value,
  else the field's `default`, else blank; Save builds the config from
  `t.config` (a key the form doesn't show kept): a number field
  `Number()`d, blank or non-finite → its default (no default → the key
  dropped), text as typed; sets `t.config`, then one
  `api.renderApp(app_id, cfg)` and `repaintTile()` (no live preview; Enter
  saves; Cancel changes nothing) — carried by `layoutItems()` → `flushTab()`
  into the working copy and saved with it on Configure-off, no route of
  its own — then only if its app offers more than one size a
  `<select>` of those sizes (`w × h`; choosing one sets the tile's span,
  the grid reflowing live), and, when there's another tab without its app
  (a layout from elsewhere can place one twice: a move, never a merge), a
  "Move…" `<select>` of those tabs (`moveTile()`: its instance — id, size,
  config — to the end of that tab, off this grid) — the empty cells are
  marked (`--tile-guide`), a drop row sits under the tiles, the subtitle is
  an input (≤ `SUBTITLE_MAX`, the server's 120), and on the strip each
  layout tab gets a gear (`.tab-gear`) and ＋ Add shows. Configure edits
  `home.layout` as a working copy: the tab on screen's edits live in
  `home.tiles`/`home.subtitle` until `flushTab()` writes them back
  (`items: layoutItems()`, `subtitle`) — before every switch, move, delete
  and the save. ＋ Add (`openTabEditor(null)`, a `.modal`): a name, then
  Create — `createTab()` appends `{id, name, mode: "flow", subtitle: "",
  items: []}`, its id minted on the page (`newTabId()`, eight `[a-z0-9]`,
  the server's `TAB_ID`, none taken), and switches to it. A tab's gear
  (`openTabEditor(tab)`): its name (`renameTab()`) and, unless it's the
  only tab, Delete tab behind an in-modal confirm saying where its tiles
  go — `deleteTab()` appends its instances to `deleteTarget()` (the
  default, or — the default deleted — the first tab left, made the
  default), never uninstalling anything; Home shows that tab if it showed
  the deleted one. A name is trimmed and checked by `tabNameProblem()` —
  the server's rule: not blank, ≤ `TAB_NAME_MAX` (40) code points (a node
  test holds it to `_name_problem()`) — before anything changes, and set
  as text only. `sortableTiles(grid)`: drag a tile
  (anywhere but its pickers and gear) onto another and it takes that one's place
  (`dropSpot()`: from earlier in the order, after it; from later, before
  it; into a row-end gap, after that row's last tile; into the drop row,
  last), the rest reflowing live, once per spot and only after the pointer
  moves `SETTLE` px from the last reorder (mixed sizes would chase it
  otherwise); a handle's arrows step it (←/↑ back, →/↓ on); `home.tiles`
  follows the DOM. Off: `flushTab()`, then `PUT /api/home-layout` with
  `home.layout`, the whole working copy (each tile's `{instance_id, app_id,
  size, config}`, an undefined id left out by JSON); on success back to
  view, "✓ Layout saved", and `loadHome()` again so the page shows what the
  server's reconcile makes of it (minted ids, ≥1 instance); on failure it
  stays in Configure with the error, the working copy intact. Outside
  Configure the strip is only the tabs: switching is always on, managing
  them is Configure's. The subtitle, every tab name and every app name are
  set as text (`textContent`, `.value`, attributes), never markup. `renderSpec()` draws the vocabulary, all as text,
  never markup: `number` (big tabular value, `unit` beside, `label` beneath,
  muted `caption`, clamped to 2 lines — 1 in a tile under 190px tall, an
  `@container` query, so a panel's line keeps its room on a phone — full text
  on hover; null → `—`/left out; `--font-display`, sized to the tile
  with `cqi`), `line` (its `title`, if any, small and uppercase over the plot;
  points a value, `{date, value}` or `[date, value]`; an
  index-spaced SVG sparkline in `--spark`, dot on the
  latest point; broken at a null and between two dated points with a day
  missing between them — never interpolated or zeroed — a lone point a dot,
  none "No data"),
  `panel` (children stacked; the line takes the rest, an input sits at the
  foot), `input` (`specInput(s, appId)`: the `label` small and uppercase
  over a row of the field — `type="number" step="any"` unless `kind` is
  `"text"`, or for `kind` `"select"` a `<select class="input">`: a
  "Choose…" placeholder first (value `""`, selected and disabled), then an
  `<option>` per `options` entry, its text (`textContent`) and value the
  string; or for `kind` `"file"` an `<input type="file">`, its `accept`
  property `accept` when given (the browser's button styled as the small
  buttons, the file's name beside it; Save wraps under them, at the row's
  end, in a tile too narrow — `.row:has(> input[type="file"])`); named by
  its label else its key — the `unit` and a **Save**
  `btn sm`, then a `.m` message line; Save or Enter (Save only for a
  file: Enter opens a picker): a blank field, or a
  number that isn't one, says so ("Enter a number.") and sends nothing —
  a select still on its placeholder likewise ("Choose an option."), a
  picker with no file ("Choose a file.") or the first file over
  `UPLOAD_MAX` (25 MiB, the server's `MAX_UPLOAD_BYTES`; "File too large
  (max 25 MB).") too — else
  `api.appAction(appId, action, {[key]: value}, tileCfg(appId))` (a
  select's value the chosen string, never `Number()`d) →
  `POST /api/app/<id>/action` `{action, payload, cfg}`, or for a file
  `api.appActionFile(appId, action, key, file, tileCfg(appId), payload)`
  — a `FormData` of `action`, `cfg` (JSON), `payload` (JSON, only when
  given — a toggle's state) and the file under `key`, POSTed
  multipart by `sendForm()` (no `Content-Type` of its own, so the
  browser sets the boundary; `send()`'s `{ok, ...}` reply). A file input
  with a `toggle` (a spec's with a non-empty `key`; none on any other
  kind) has a checkbox in its label (`label.tg`, the toggle's `label`
  else its key, as text) in the row between the picker (and unit) and
  Save, unticked; its state goes as `payload` `{[toggle.key]:
  checked}`, ticked or not — none at all without a toggle, the request
  as before — and ticked, with a `confirm`, after the tile's own refusals
  it first `await`s `confirmFirst(label, confirm)` (Save, the picker and
  the box held meanwhile): a `.modal` from `openModal(html, onClose)` —
  whose optional `onClose` runs however it closes — the label as its
  `h4` over a `danger-panel` "Are you sure?" with the app's text, [Cancel]
  (focused) / [Continue], both texts `textContent`; true only on
  Continue, and anything else (Cancel, Escape, a click outside) sends
  nothing, the tile as it was (still ticked); no `confirm()`. Then with Save,
  the field and a toggle held meanwhile (`hold()`), `{ok, spec}` →
  `repaintTile(appId, spec)`, or
  `{ok: false}` → its `error` in the message line, the field kept as
  typed (a file still picked, a toggle still ticked); drawn with no app id, Save is off; in Configure the face is
  inert, its input with it), `timeline` (`timelineSpec(s)`: its `title`
  over the lanes' labels (`.tl-head`, a heading a lane, drawn if any lane
  has one) over a frame (`.tl-scroll`, `overflow-y: auto`, filling the rest
  of the tile — in a panel, what its siblings leave) holding the axis
  (`.tl-track`, `position: relative`, its height the axis'): a `.tl-lane`
  column a lane (`.l0` left half and `--timeline-a`/`--timeline-a-ink`,
  `.l1` right half and `--timeline-b`/`--timeline-b-ink`; one lane the
  whole width), each interval an absolutely placed `.tl-block` with its
  label as text; a `.tl-now` rule across both in `--accent` when `now` is
  set; and in a `grey_after_now` lane, with a `now`, a `.tl-future` hatch
  from it down, under the blocks. A lane's bands are `.tl-band`s, first
  in its column (so under the hatch, the blocks and the lines;
  `pointer-events: none`), its full width, a 14% tint of `--layover`
  with dashed top and bottom edges, placed in the same layout pass as the
  blocks — `top = (start − t0)·px`, `height = length·px`, no floor — each
  label a `.tl-band-label` (as text) set past the block(s) covering the
  band's top edge (the flight out), so on the layover, clipped by the
  band. The axis runs from the earliest start to
  the latest end (or `now`) of every block and band; `TL_VIEW_MS`, a week, fills the frame's
  height (`px = max(height, TL_MIN_WEEK_PX 240) / week`, so a frame under
  240px — a 2×1's — shows less than a week and scrolls further, never
  squeezing a week until every block is at the floor), each block `top =
  (start − t0)·px`, `height = max(TL_MIN_BLOCK_PX 18, length·px)`, and the
  track as tall as the axis or its last block. The page builds it detached,
  so the layout runs at `TL_FALLBACK_PX` (240) at once, then in a
  `requestAnimationFrame` and on every `ResizeObserver` callback on the
  frame, at its `clientHeight`; no height once laid out (Home behind
  Data) keeps the last layout. The first real layout scrolls `now` to the
  middle (none: the end into view); after that the time at the top is kept
  (`onscroll`), through a resize. Each observer is in `timelines` and let
  go of by `releaseTimelines()` — called at the end of `paintHome()` and
  `repaintTile()`, and by the observer finding its frame gone —
  disconnecting any whose frame `!isConnected` (and taking a synced one
  out of its group). A hand-built spec draws
  what it can: two lanes at most, an interval or band without both ends
  in order dropped (bands not a list: none); nothing → "No data". **Synced** — `s.sync` a non-empty
  string; anything else draws all of the above and nothing more, the
  same DOM as before `sync` — it's drawn around its group's **focus
  clock**: `focusGroups`, a `Map` of group name → `{focusMs, following,
  members}`, UI state in memory for the page's life (never stored: a
  load starts every group on now); `focusGroup(name)` gets or starts one
  (on `tlSnap(Date.now())`, following, and arms the one shared follow
  tick, `armFollow()`); `setFocus(g, focusMs, following, from)` publishes
  — every member's `show(rescroll)` redraws its readout and Now button
  and, bar `from` (the one scrolled by hand), scrolls the time under its
  cursor; `followNow()`, run by a `setTimeout` due on each quarter hour
  (`TL_STEP_MS`) and re-armed, moves every following group on to now;
  every focus time `tlSnap()`ed down to the quarter hour. The time is
  shared, never a scroll position: each member scrolls to it by its own
  `px`. A member (its `timelines` entry, `{scroll, observer, group,
  show}`) joins on render and leaves in `releaseTimelines()`, so a tile
  drawn afresh rejoins on the group's time. A synced timeline's DOM: a
  `.tl-focus` row over the headings — `.tl-readout` (`fmtFocus()`, UTC:
  `Mon 03 Nov 14:15Z`) and a `.tl-nowbtn` (`btn sm`, "Now", `hidden`
  while following) at its end — and the frame wrapped in `.tl-view` with
  a `.tl-cursor` over it, a fixed `--accent` line across its middle (the
  track scrolls under it); the track padded half a frame each end, so
  `scrollTop = (focusMs − t0)·px` puts the focus under the cursor — on
  every layout, in place of the now-centring; the `.tl-now` line still at
  `s.now`, with a `.tl-now-tag` "Now" under it on the greyed lane (the
  first, with none). Its `onscroll`: a scroll it made itself (to
  `ours`, the `scrollTop` it set, read back) passes; any other is the
  user's — the group's focus to the quarter hour under its cursor,
  `following` false. Now: `setFocus(group, tlSnap(Date.now()), true)`.
  `.tl-view > .tl-scroll` has `overflow-anchor: none`), `empty`/`error` (muted, centred; error under
  "Couldn't render <name>"), any other type an error tile. A spec is drawn
  for its app — `renderSpec(spec, name, appId)`, the id threaded from the
  tile (`paintTiles()`, `repaintTile()`) through a panel to its input. No apps → a note pointing to ＋ Add Application (or the apps
  folder); `/api/apps` failing → a Retry.
- `tests/` — pytest, temp DB, no network.
- `docs/architecture/` — design direction (read first). `docs/design/` — frontend references.

## Data model
- Current: a tidy `metrics(date, source, metric, value, unit, synced_at)` keyed
  `(date, source, metric)` — one row per reading, so a new metric is data not a
  migration. Keys, units, categories, grains and plausible ranges come from
  `app/metrics.py` — the v2 vocabulary, 35 keys (beside them, at the
  call sites with app context, each installed app's own, `app:<id>:<key>`
  — below) (the full table is
  `docs/architecture/metric-schema-v2.md`). Activity: `steps`/count,
  `distance_m`/m, `floors`/count, `active_kcal`, `total_kcal`, `basal_kcal`
  (kcal), `sedentary_min`/min, `elevation_gain_m`/m,
  `intensity_moderate_min`, `intensity_vigorous_min`, `active_minutes` (min).
  Vitals: `resting_hr_bpm`/bpm, `hrv_ms`/ms, `spo2_pct`/%,
  `respiratory_rate_brpm`/brpm, `skin_temperature`/°C (absolute), and the
  instant `body_temperature`/°C, `blood_pressure_systolic`/`_diastolic`
  (mmHg), `blood_glucose`/mmol/L. Body composition, all instant:
  `weight_kg`/kg, `body_fat_pct`/%, `bmi`/kg/m2, `height_cm`/cm,
  `muscle_mass_kg`/kg, `bone_mass_kg`/kg, `body_water_pct`/%,
  `visceral_fat`/rating. Fitness: `vo2_max`/ml/kg/min. Nutrition &
  hydration: `hydration_ml`/ml, `calories_in`/kcal, `carbs_g`, `protein_g`,
  `fat_g` (g). Other: the instant `mood`/scale (1–5). Garmin fills its v2
  keys (below: basal, sedentary, elevation, and the body-composition
  masses, water and visceral fat); Google Health its daily ones (below:
  `active_minutes` from Active Zone Minutes among them); the rest (blood
  pressure/glucose, body/skin temperature, `height_cm`, nutrition, `mood`)
  wait for a source — Garmin must not write `active_minutes`: it files the
  moderate/vigorous split, and a single-number active-minutes figure is
  another vendor's measurement (Google's AZM). An
  instant reading is still one per `(date, source)` here. There is no daily
  sleep metric (see `sessions`). Garmin files nine activity metrics from the same
  `get_stats` response as steps and resting HR, through its `STATS_METRICS`
  field map (`app/plugins/garmin/plugin.py`; each rounded to a whole
  number): `totalDistanceMeters` → `distance_m`, `activeKilocalories` →
  `active_kcal`, `totalKilocalories` → `total_kcal`, `floorsAscended` →
  `floors`, `moderateIntensityMinutes`/`vigorousIntensityMinutes` →
  `intensity_moderate_min`/`_vigorous_min`, `bmrKilocalories` →
  `basal_kcal`, `sedentarySeconds` → `sedentary_min` (`/ 60` first) and
  `floorsAscendedInMeters` → `elevation_gain_m`. A field absent or not a
  finite number writes nothing (an absent one is logged with the response's
  keys — the floors, intensity, basal, sedentary and elevation field names
  are unverified guesses, and `floorsAscendedInMeters` may not be in the
  daily summary at all), and a day holding only these still lands. Those
  accumulators are written only on a day Garmin recorded heart rate
  (`restingHeartRate` or `maxHeartRate` present — either alone counts, for
  a day worn only by day or only overnight): on a no-wear day `get_stats`
  still fills in full-day defaults (`activeKilocalories` 0,
  `sedentarySeconds` 86400, `floorsAscended` 0), so that day files none of
  them and logs `no device data (no HR) - skipping daily activity
  accumulators, keeping basal`. `basal_kcal` is excepted — `bmrKilocalories`
  is a profile constant, the same worn or not — so it stays a continuous
  series and a no-wear day still lands on it. `includesWellnessData` and
  `includesActivityData` are not wear signals (True and False on a worn and
  a no-wear day alike), so the gate doesn't read them. Steps and the sensor
  vitals (SpO2, respiration, HRV) need no gate: Garmin reports them null on a
  no-wear day. A forward fix:
  defaults filed before it stay until the source is wiped and re-synced.
  The three
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
  request a day, `get_body_composition(day, day)` (a range call with several
  outputs, so not a `GARMIN_VITALS` entry), read by
  `_body_composition_metrics()` from `totalAverage`. As reported
  (`BODY_COMPOSITION_FIELDS`): `bodyFat` → `body_fat_pct`, `bmi` → `bmi`,
  `bodyWater` → `body_water_pct` and `visceralFat` → `visceral_fat`.
  Converted from grams (`/ 1000`, `BODY_COMPOSITION_GRAMS`): `weight` →
  `weight_kg`, `muscleMass` → `muscle_mass_kg` and `boneMass` →
  `bone_mass_kg` — Garmin's `weight_kg` sits beside the weight app's
  (`app:weight`) and the frozen `manual` history, and outranks both in the
  default precedence. That unit (and the
  `muscleMass`/`boneMass` names) is unverified (every field was null on the
  account probed — one without a compatible smart scale, which is correct
  behaviour, not a failure), so each converted mass is written only within
  its metric's `PLAUSIBLE_RANGES`; outside it nothing is written and the
  field, raw grams and converted kg are logged. `physiqueRating` and
  `metabolicAge`, beside them, are vendor-proprietary composites and never
  read (schema v2, law 5). As with the vitals, a missing/null/non-number
  field writes nothing and isn't logged, a failed fetch costs only its own
  metrics, and a day holding only these still lands.
  Google Health files thirteen daily metrics, every one over the same
  window — the sync's `days` (today − (days−1) … today) — and drops a point
  dated outside it, so a sync writes one consistent span. Field names and
  units are the v4 discovery doc's
  (`https://health.googleapis.com/$discovery/rest?version=v4`), each type
  in its own fetch (a failed or empty one costs only its metric). Two
  tables in `app/plugins/google_health/plugin.py`: `DAILY_ROLLUPS`, via
  `dailyRollUp` (Google's own per-day sums; 90 days a request, 14 for
  `total-calories`): `steps` `countSum` → `steps`, `distance`
  `millimetersSum` → `distance_m` (`/ 1000`), `floors` `countSum` →
  `floors`, `altitude` `gainMillimetersSum` → `elevation_gain_m`
  (`/ 1000`), `active-energy-burned` / `total-calories` `kcalSum` (kcal, no
  kJ) → `active_kcal` / `total_kcal`, `active-zone-minutes` → the sum of
  whichever of `sumInFatBurnHeartZone`/`sumInCardioHeartZone`/
  `sumInPeakHeartZone` are present (cardio/peak already count double; a
  zero is left out) → `active_minutes`, and `sedentary-period`
  `durationSum` (a duration string, seconds) → `sedentary_min` (`/ 60`) —
  each rounded to a whole number. `DAILY_SUMMARIES`, via list, filtered
  `<type>.date >= start AND <type>.date < day-after-today` (the filter
  takes `>=`/`<`, not `<=`), stored as reported (resting HR rounded):
  `daily-resting-heart-rate` `beatsPerMinute` → `resting_hr_bpm`,
  `daily-heart-rate-variability` `averageHeartRateVariabilityMilliseconds`
  (the night's RMSSD) → `hrv_ms`, `daily-vo2-max` `vo2Max` → `vo2_max`
  (the daily summary type — not the sample type `vo2-max` nor
  `run-vo2-max`), `daily-oxygen-saturation` `averagePercentage` →
  `spo2_pct` and `daily-respiratory-rate` `breathsPerMinute` →
  `respiratory_rate_brpm`. Only a finite number (never a bool) within the
  metric's `PLAUSIBLE_RANGES` once converted is written; a null or absent
  field writes nothing, and a day holding only one of these still lands.
  Google returns a point only for a day it has data (no 0 for a missing
  day), so it writes only measured values by construction — no wear gate.
  Its auth URL (`oauth_auth_url()`) asks for `access_type=offline` and
  `prompt=consent`, so every authorization, a repeat included, returns a
  fresh refresh token with no revoke step: an unverified (Testing-mode)
  client's token lapses after ~7 days, and this is what makes the weekly
  re-auth a one-click-and-paste (Re-authenticate, in the source's Settings
  popup) rather than remove, re-add and revoke.
  Every type logs one line a sync: the days filed with the latest raw
  fields beside the converted value, or "no data", or — points but none
  with the field — a sample point and its keys; values not filed and
  out-of-window points are logged too. The mappings are confirmed against
  the discovery doc only, not yet a real sync. Google also files two
  intraday series as `samples` (see below). Not fetched: `basal_kcal`
  (`basal-energy-burned` has no rollup in v4, only raw interval points),
  body composition, nutrition and skin temperature.
  Migration `005` unpivoted the old wide `activity` table into it and dropped
  `activity`; migration `006` moved the old `weights` table in as `weight_kg`
  readings under source `manual` (hand entry; `synced_at` NULL for migrated
  rows) and dropped `weights`; migration `010` deleted every `sleep_minutes`
  row from `metrics` and `derived_metrics` — nothing else — when schema v2
  retired the key (a night only ever filed as a daily total, from before
  `007`, went with it; a longer sync window re-fetches it as a session).
  **Apps are sources** (no migration): an app files under `app:<id>` (its
  installed id) through its action route and `WriteCtx` (see Layout:
  `app/host.py`) — canonical keys stay canonical (`weight_kg` under
  `app:weight`), competing in the same pick, after the pull plugins
  (unlisted, by name) unless a source role says otherwise; no tier, no
  opt-in, no observed/computed flag. Sessions too, of an existing kind
  (`ctx.write_session()`, id `app:<id>:<kind>:<start>`), passed through by
  `GET /api/sessions/<kind>` beside every source's — no pick, nothing
  derived. **An app's own metrics** (no
  migration): declare what you invent, write what exists — a metric the
  app invents is declared in its manifest's `metrics` (see Layout:
  `app/apps.py`) and stored as `metric = app:<id>:<key>`, `source =
  app:<id>`, in the declared unit (the `unit` column; the derived row
  copies it), its one source winning every day of the pick; two apps
  declaring one short key keep two metrics, and an app writes only its
  own (and canonical keys). The effective vocabulary is `METRICS` plus
  every installed app's declarations (`custom_metric_defs()`): the
  catalog, `/api/coverage` and `/api/metric/<metric>` serve them while the
  app is installed; uninstalled, they drop out (the metric route 404s)
  and their readings stay under the source — the "orphaned custom
  metric": `DELETE /api/app-sources/<id>/app:<id>:<key>` (B1) removes
  just its readings, `DELETE /api/app-sources/<id>` takes them with the
  rest of the source's rows, and a reinstall before either brings the
  metric back, history and all. No source role names one (`/api/source-roles` lists canonical
  metrics, and its `PUT` 404s an app's key); the webhook refuses one (a
  canonical key only). Nothing else writes an `app:` source
  (the webhook refuses the prefix). Uninstalling an app keeps its rows —
  hand-entered data can't be re-synced — until they're removed, a separate
  explicit step for an app no longer installed (no migration; `app/main.py`):
  `GET /api/app-sources` → `[{id, source, orphaned, metrics, readings,
  sessions, samples}]`, one per `app:<id>` source with any row, by id —
  `orphaned` once its app is gone from the volume (`app_present()` false:
  no `<apps dir>/<id>`, as anything, and no other folder's app loading
  under the id) — an app whose folder is there but fails to load is
  broken, not gone, so isn't, nor is an id failing the scanner's `APP_ID`
  (no folder to look for) — `metrics` the keys it has readings of
  (vocabulary order), the rest row counts; the prefix matched as a range
  (`>= 'app:' AND < 'app;'`, case-sensitive, unlike `LIKE`), so
  `apple_health` or `APP:x` isn't one. `DELETE /api/app-sources/<id>` →
  `purge_source(conn, "app:" + id)`, then `{ok: true, removed: {metrics,
  sessions, samples}}` (rows deleted); 404 for an id failing `APP_ID`
  (checked first); 400 while `app_present()` — "'<id>' is present — fix
  or uninstall it first; …": a live or broken app's data is never
  removed; 404 when `app:<id>` has no rows (checked before anything is
  written, so a role naming it stays) — nothing changes on any of them,
  and the listing marks orphaned exactly what this would remove. The id is always read as
  `app:<id>` (the prefix never stripped), so it can't reach a device, a
  webhook source or `manual` (deliberately out of scope for now).
  `DELETE /api/app-sources/<id>/<metric>` (B1) is the same for one
  metric — `<metric>` the key as stored, a canonical one or the full
  `app:<id>:<key>`: `purge_source(conn, "app:" + id, metric=metric)`,
  the same reply (`sessions` always 0) and guards in the same order, the
  last 404 "no <metric> data from app:<id> to remove" when that
  `(source, metric)` has no reading or sample (`_has_metric_data()`;
  sessions are kind-keyed, never looked at); DELETE only. The app's other
  metrics, its sessions and its roles for anything else stay; a metric
  another source wins keeps its winner.
  `purge_source(conn, source, metric=None, keep_roles=False)`
  (`app/main.py`, shared with the connector wipe) deletes every `metrics`
  (`RETURNING date`), `samples` and `sessions` row of the source, clears
  every source role naming it (`clear_source_roles()`) unless
  `keep_roles`, and `recompute_derived()`s the min..max of the deleted
  dates, one transaction with whatever the caller did first (a failure
  rolls it all back); → `{days, metrics, sessions, samples}`. With
  `metric`, only that metric's `metrics` and `samples` rows go (no
  sessions) and only its role is cleared (`only=metric`). The metrics
  stay: `weight_kg` without `app:weight`'s readings is still `weight_kg`.
  **`manual` is frozen**: the hand-entered weigh-ins — migration `006`'s
  and the retired `POST /api/weights`' — stay under source `manual` as
  history (where they came from), read and picked like any source's, but
  nothing writes to it any more (`FROZEN_SOURCES` in `app/main.py`; the
  webhook refuses it). Not migrated: an app can only write its own source,
  so it can't relabel `manual`'s rows, and a core migration can't know an
  app's id. Per date there's no contention, so the weight timeline runs
  on from `manual`'s days to the weight app's.
- Beside it, `sessions(id, source, kind, "start", "end", summary_json, synced_at)`
  (migration `007`, purely additive) for interval records: `id` is
  `source:kind:start` so a re-sync upserts, `start`/`end` are ISO 8601 UTC
  (`iso_utc()` in `app/plugins/base.py`), `summary_json` is the kind's
  breakdown as JSON. Quote `"start"`/`"end"` in SQL (`end` is a keyword).
  Written via `write_session()`, which raises on a `kind` outside
  `SESSION_KINDS` (`app/metrics.py`, where each kind's summary keys are listed).
  Four kinds: `sleep` and `workout`, both from Garmin and Google (and from
  an app, through `ctx.write_session()`, under `app:<id>`), and the two
  duty kinds, from a roster app (the `roster` fixture, under
  `app:roster`); a summary holds only what the source gives:
  - `sleep` — one session per night/nap, summary in minutes: `asleep_minutes`
    plus `light_minutes`/`deep_minutes`/`rem_minutes`/`awake_minutes`. Sleep is
    sessions only: there is no daily sleep metric (v2 retired `sleep_minutes`
    — a daily total is an aggregate, an app's to compute), and neither plugin
    files one.
  - `workout` — one session per activity, and no daily metric: `type` (the
    source's activity type, lowercased — not yet mapped across sources),
    `duration_minutes`, `distance_m`, `avg_hr_bpm`, `calories_kcal`.
  - `duties_rostered` / `duties_complete` (category Roster) — one session
    per work duty off a roster, its `start`/`end` the duty's envelope
    (report to release, UTC); `duties_complete` if it had ended when filed,
    else `duties_rostered` (to come or under way), so a duty filed before
    and after it ends is one of each — plan and actual side by side. The
    same summary keys for both: `capture` (`"live"`), `type`
    (`flight`/`reserve`/`sim`/`other`), `code`, `label` (the roster's
    title), `raw` (its description, verbatim), and for a flight, when
    given, `flight_no`, `dep`, `arr`, `position`.
  Read through the access contract (`GET /api/sessions/<kind>`, below) — or,
  by an installed app, `ctx.sessions(kind)`, the same rows.
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
  samples but no daily figures keeps them.
  Google Health is the second `heart_rate` and `spo2` source (no
  `respiration` — Google has no continuous respiration series, only the
  nightly `daily-respiratory-rate`), at one value a minute:
  `SERIES_WINDOW_SECONDS = 60` in `app/plugins/google_health/plugin.py`, the
  one place that resolution is set. Its `INTRADAY_SERIES` table, each type
  its own fetch through `_fetch_series()`, in its own try/except (a failed or
  empty one costs only its own samples, never the other series or the daily
  metrics):
  - `heart_rate` — `heart-rate`, which Google keeps at ~1 s, via the windowed
    `rollUp` (`POST .../dataTypes/heart-rate/dataPoints:rollUp`, body
    `{range: {startTime, endTime}, windowSize: "60s", pageSize: 1440}`):
    Google averages each minute server-side, so only per-minute buckets cross
    the wire — never the per-second points. Each bucket's
    `heartRate.beatsPerMinuteAvg`, rounded, at its `startTime`. rollUp takes
    at most 14 days of `heart-rate` a request (`MAX_SERIES_DAYS`).
  - `spo2` — `oxygen-saturation` (already ~1 a minute, valid sleep minutes
    only) via list, filtered `oxygen_saturation.sample_time.physical_time
    >= start AND < end`: each point's `percentage`, rounded, at its own
    `sampleTime.physicalTime` — no downsampling.
  Both over the sync window's days in the server's local time (the zone
  `date.today()` reads): local midnight on its first day up to the start of
  the current minute, in back-to-back windows of at most 14 days of elapsed
  time (`_spans()`, so a clock change never stretches one past rollUp's
  limit), `SERIES_PAGE_SIZE` = 1440 (a day of minutes) to a page, so a full
  window is 14 pages and `MAX_PAGES` only stops a runaway. Each window's
  points are converted as they arrive (`_series_reading()`); a failed window
  costs only its span. A null value is no reading; a non-finite, non-positive
  or bool value, or a point with no usable time, is dropped and logged, as is
  a point timed outside the window. One log line per series per sync: the
  sample count and span with the latest raw value beside the one filed, or
  "no data", or — points but none with the field — a sample point and its
  keys. Samples don't count toward a sync's days written (as with Garmin).
  Names are the v4 discovery doc's (`HeartRateRollupValue`,
  `OxygenSaturation`), not yet confirmed by a real sync.
  Nothing derives from samples; apps read them through the access contract
  (`GET /api/series/<metric>`, below) as a per-source passthrough — Garmin's
  and Google's heart rate (and SpO2) both listed, each tagged. The
  cross-source stitch is still a later slice, though a second series source
  now exists.
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
  after every webhook reading for its one day (`since=until=date`), after
  an app's action for each day it wrote (one day at a time), after `DELETE
  /api/connectors/<plugin>?wipe=1` over the span its deleted readings covered (min..max date, one call, so the
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
- Home layout: the `settings` row `home_layout` (no migration) holds JSON
  `{"tabs": [{"id", "name", "mode": "flow", "subtitle", "items":
  [{"instance_id", "app_id", "size": {"w", "h"}, "config"}]}],
  "default_tab"}` — Home's tabs, and on each its instances' order, sizes
  and configs and the line over them, written by Configure (and by an
  install the Add popup put on a tab other than the default) through `PUT
  /api/home-layout` and read, reconciled with the installed apps (every
  installed app at least one instance), through `GET /api/home-layout`
  (`app/home_layout.py`; see Layout). The shape from before tabs, `{"mode":
  "flow", "subtitle", "items": [{"app_id", "size"}]}`, is upgraded on
  read to one `home` tab — no migration. It's config, not data: an app's
  tile placement, never a metric.
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
  (default `webhook`) that isn't a pull plugin's id, an app's (`app:`
  prefix — `APP_SOURCE_PREFIX`) or a frozen one (`manual`). The value
  rule is `plausible_value()` (`app/metrics.py`), shared with an app's
  `ctx.write_metric()`. Idempotent per `(date, source, metric)`. Metrics
  only — no session ingest yet.
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
    also carries `grain` (`grain_for()`: a metric's declared `"daily"` or
    `"instant"`, `"session"`, `"series"`) and `category` (`category_for()`)
    — the console groups by them. (The pre-v2 `cadence` label is gone,
    removed in PR 4 of the v2 rollout.) `?include_empty=1` (also `true`/`yes`;
    default off = the above) lists every vocabulary key in each list, in the
    same order — one with no data as `count: 0`, `sources: []`,
    `first`/`last` null (metrics also `last_value`/`last_date` null) — so a
    console can show "not yet reporting" slots; split them by `count`.
    After the canonical metrics come the installed apps' own
    (`custom_metric_defs()`, folder then manifest order), `metric` the full
    key `app:<id>:<key>`, `unit`/`grain`/`category` as declared, plus two
    fields only they carry: `app` (the defining app's id) and `label`;
    with `?include_empty` a declared one with no data is listed as above.
    An uninstalled app's are left out, data or not.
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
    install has three empty maps; metrics' vocabulary is the catalog's,
    the installed apps' own included.
  - `GET /api/metric/<metric>?from=&to=` → `{metric, unit, points: [{date,
    value, source}]}` ascending; `value` as stored (a float). `<metric>` a
    canonical key or an installed app's own by its full key
    (`app:<id>:<key>`, `unit` the declared one); anything else — a short
    key, an undeclared or uninstalled app's — 404; known with no data →
    `points: []`.
  - `GET /api/sessions/<kind>?from=&to=` → `[{start, end, source, summary}]`,
    a priority-ordered passthrough: every source's sessions — no pick, filter
    or merge — the source the pick would rank first leading, then the rest
    (`source_rank()`: a `source_roles` entry under the kind's own key if one
    is stored, then `DEFAULT_PRIORITY`, then by name), each source's
    ascending by start. `summary` parsed from `summary_json` (`{}` if none),
    `end` may be null. `from`/`to` match the start's UTC date. Unknown kind
    404. Served by `session_rows()` (`app/derived.py`), which an app's
    `ctx.sessions()` reads too, so the two agree.
  - `GET /api/series/<metric>?from=&to=` → `{metric, unit, points: [{ts, value,
    source}]}` from `samples`, ascending by `ts` then source; `value` a whole
    number as an int (`_whole()`: 77.0 reads as 77). A per-source passthrough,
    no pick: every source's samples are listed, tagged — Garmin's and
    Google Health's `heart_rate`/`spo2` side by side, a minute both sampled
    listed once per source (the cross-source stitch is deferred, though a
    second series source now exists). `from`/`to` match the
    sample's UTC date, as `ts` bounds (`>= fromT00:00:00Z`, `<= toT23:59:59Z`)
    so the `(metric, ts)` index is used. 404 unless `<metric>` is in
    `SERIES_METRICS` (`heart_rate`, `respiration`, `spo2`) — a daily key like
    `resting_hr_bpm` included; known with no samples → `points: []`.
  - `from`/`to`: optional, inclusive `YYYY-MM-DD` (empty = omitted); malformed,
    or `from` after `to` → 400. Or `days=N` (every read above; one parser,
    `_date_range()`): the trailing N days, `trailing_window()` (today, the
    server's local date, − (N−1) … today), so `?days=7` is ≤ 7 points and a
    reading dated after today is out; `1`–`999999999` digits only, else 400;
    with a non-empty `from`/`to` → 400; empty = omitted. Errors are
    `{"ok": false, "error"}`.
  These shapes are the stable contract: change them additively only.
- Connectors (no migration): `accounts(plugin_id, credentials, created_at)`,
  one row per connected plugin, `credentials` a Fernet-encrypted JSON blob
  (the manifest fields, plus an oauth connector's refresh token). An oauth
  connector is added in two calls: `POST /api/connectors/<plugin>/oauth/start`
  (the manifest fields → `{ok, auth_url}`; the PKCE verifier and the
  credentials wait server-side in `_oauth_pending`, 10 minutes), then
  `.../oauth/finish` `{code}` (the plugin's `oauth_exchange()`, stored via
  `_store_account()`; no refresh token back → 400 with its
  `oauth_refresh_help`). `start_oauth` fills any manifest field the body
  leaves out from `plugin.get_credentials(conn)` (the stored account, else
  the env) — the body wins where it has a value, only manifest fields are
  taken (never the old refresh token), and the missing-fields 400 runs on
  the merge — so a connected source re-authorises with `{}`, nothing
  re-entered, while a fresh add with nothing stored still 400s. The secret
  stays server-side: the auth URL carries only the client id, and the
  response only `auth_url`. `GET
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
  back): `purge_source(conn, plugin, keep_roles=True)`, after the account
  delete. Every metric on every day in that span is re-picked under the
  current source roles, which a disconnect keeps, wiped or not (as it
  keeps the cadence: a device added back is primary again where it was).
  Unknown plugin → 404.
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
  from each source's Settings popup — no migration); Google Health's
  intraday `heart_rate` (windowed rollUp, per-minute averages) and `spo2`
  (list, as-is) as the second series source, at one value a minute — no
  migration. Still to come: the cross-source `/api/series` stitch (deferred;
  the second series source it was waiting on now exists), more series
  sources, verifying
  sources + history, and committing a sync per day (it still holds one write
  transaction for the whole run).
- **Metric schema v2** (`docs/architecture/metric-schema-v2.md`), four PRs, all landed.
  PR 1 landed: the v2 vocabulary (unit/category/grain/range per key,
  `CATEGORIES`, `grain_for()`/`category_for()`), the catalog's `category`
  and `grain_for()` grain, priority-ordered `/api/sessions`, `sleep_minutes`
  retired (migration `010`; the Garmin and Google Health daily sleep writes
  removed; `/api/activity` without `sleep_hours`). PR 2 landed: Garmin fills
  its new keys — basal, sedentary and elevation from `get_stats`, muscle and
  bone mass, body water and visceral fat from `get_body_composition` (field
  names/units unverified, logged on absence or out of range; no migration).
  PR 3 landed: Google Health's daily metrics — distance, floors, elevation,
  active/total calories, Active Zone Minutes as `active_minutes`,
  sedentary, HRV, VO2 max, SpO2 and respiratory rate — every daily fetch
  over the one sync window (no migration). PR 3b landed: Google's intraday
  series — `heart_rate` via a 60-second windowed rollUp and `spo2` from
  `oxygen-saturation`, as samples at one value a minute (no migration; no
  respiration series, Google has none). PR 4 landed, completing the v2
  rollout: the console groups by category with grain as the inner sort,
  each category's empty rows behind a fold, and the
  `cadence_for()`/`MANUAL_METRICS` shim is deleted, with the catalog's
  `cadence` field (no migration). Still to come, beyond the rollout:
  Google's body composition, nutrition, skin temperature and basal
  calories (no rollup — a decision on summing raw points). Deferred until a plugin
  targets them: `hr_zone_minutes`, Women's-health keys, `ecg`, `symptoms`.
- **Webhook / push ingestion:** landed for metrics (see Data model); still to
  come: pushing sessions (sleep, workouts) through it.
- **The app host** (`docs/architecture/app-host.md`): the first slice landed —
  the scanner (`app/apps.py`), the ctx and spec builders (`app/host.py`),
  `GET /api/apps`, the render route (an error spec, never a 500),
  `?days=` on the contract reads, and Home drawing the tiles flowed at their
  default size (no migration). Apps run as trusted code in the server's
  process. Configure mode landed in flow form: the manifest's `sizes`
  (required, 1–6 cells either way, the older `default_size` a list of
  one; `check_app()`'s first rule), a six-column grid of square cells,
  drag-to-reorder and a size picker, an editable subtitle, saved as the
  `home_layout` settings row through `GET`/`PUT /api/home-layout` and
  reconciled with the installed apps on every read (no migration).
  Multi-tab Home, slice 1 landed (invisible by design): the layout became
  `{tabs, default_tab}`, each tab a flow of instances (`instance_id` →
  `app_id` + `size` + `config`, one app placeable more than once), the
  old shape upgraded on read to one `home` tab and every installed app
  kept at ≥1 instance (no migration); Home still shows one tab, looks the
  same and keys tiles by `app_id`. Slice 2 landed (client only — no change
  to `home_layout.py` or the routes): the tab strip of the layout's tabs,
  switching between them (the `showTab` split, a fetch/render split of
  `loadHome`), and in Configure creating (client-minted ids), renaming and
  deleting tabs (tiles to the default) and moving a tile to another tab,
  all in a working copy saved at once on Configure-off; every app stays on
  exactly one tab, so tiles are still keyed by `app_id`. Slice 3 landed:
  per-instance config — `/api/apps` carries each app's `config` schema,
  the render route became `POST /api/app/<id>/render` `{cfg}` (a missing
  or non-dict `cfg` → `{}`; GET is a 405), and in Configure a tile whose
  app declares config fields gets a gear opening its settings form, the
  values saved into the instance's `config` with the layout and the tile
  re-rendered on Save (no migration). Instance-keyed tiles are dropped
  for good: one app placed twice is done at the app level (Duplicate,
  below), so an app id never has two instances and tiles stay `app_id`-
  keyed. Slice 4 landed (client only — it reuses `GET /api/app/<id>/file`
  and `POST /api/apps`, no route or layout change): app-level
  **Duplicate** in a Data app row — its files copied into a new app via
  the Add popup, the id and name bumped with the next free sequential
  number (`steps-2`/"Steps 2"), reviewed and installed like any app; a
  fork, so edits to the original's files don't reach the copy — and a
  copy writes under its own source, `app:<new-id>` (an app's source is its
  installed id), never the original's.
  Slice 5 landed (client only — it reuses `POST /api/apps` and `GET`/`PUT
  /api/home-layout`, no route or layout change): deploy-to-tab — the Add
  popup's Review offers an **Add to tab** picker when Home has more than
  one tab, the default tab picked; a non-default choice has the new
  app's instance lifted onto that tab (appended — reordering stays
  Configure's) and the layout saved, by `placeNewAppOnTab()`. Duplicate
  opens the same popup, so a copy gets the same picker. Its follow-up
  hides the picker while Configure is on (a placement then was lost to
  Configure-off's save of the working copy, which doesn't hold the app):
  the new tile joins the tab on screen (`flowLayout()` squaring the
  working copy with the apps) and is moved like any tile, the
  Configure-off save the only writer.
  App write-back landed (apps-as-source slice B, no migration): an
  optional `handle(ctx, action, payload)` in `app.py`, run by `POST
  /api/app/<id>/action` with a `WriteCtx` whose source is fixed to
  `app:<id>`; the `input` spec element (a field and Save) that sends it
  from a tile, which is redrawn from the reply; and the `weight` app, the
  first writer, which retired `/api/weights` and `MANUAL_SOURCE` (the
  console's weigh-in form had already gone) — `manual` frozen as history,
  not migrated. Custom app-defined metrics (B0) landed (no migration): an
  app declares the metrics it invents in its manifest's `metrics` block
  (`{key, label, unit, category, grain, range?}`, daily/instant only, no
  built-in key), reads and writes them by short key, and the host stores
  them as `app:<id>:<key>` under `app:<id>`; the effective vocabulary
  (canonical + installed apps' own, `custom_metric_defs()`) feeds the
  catalog (entries marked `app`/`label`), coverage and the metric route;
  the console names them by label with an `app` mark; the `caffeine` app
  is the reference. The first input primitive beyond a typed field landed
  (no migration, no route change): a dropdown, `ctx.input(...,
  kind="select", options=[...])` of plain-string options, drawn by
  `specInput`'s select branch and sent as the chosen string; the `mood`
  app is the reference (`docs/architecture/input-primitives.md`). Then
  file upload landed (no migration; the action route also takes
  multipart): `ctx.input(..., kind="file", accept=...)`, the file posted
  `multipart/form-data` and handed to `handle()` as a `FileUpload` in a
  temp file deleted after the action, a 25 MiB `MAX_CONTENT_LENGTH` (413
  over it) — OpenFit carries the file and never parses it, the app does;
  the `import` app (past weigh-ins from a text file, each on its own
  day) is the reference. Then app session write-back landed (no
  migration, no route change): `ctx.write_session(kind, start, end,
  summary)` on the `WriteCtx`, reusing `write_session()`/`iso_utc()`,
  existing kinds only, under the app's own source, deriving nothing; the
  `workout-log` app is the reference.
  Then the roster path landed (no migration, no route or ctx change): two
  session kinds in core, `duties_rostered`/`duties_complete`, under a new
  Roster category, and the `roster` app, which parses a roster's `.ics`
  itself and files each timed event as a duty — `duties_complete` once
  ended, else `duties_rostered` — with its own daily
  `duty_hours_complete`/`duty_hours_rostered`. Upsert-only: a re-upload
  replaces a duty of the same kind and start, never removes one.
  Then session read-back landed (no migration, no route or app change):
  `ctx.sessions(kind, since=None, until=None)` on the base `Ctx` — every
  source's sessions of a kind as `Session` tuples, the in-process mirror
  of `GET /api/sessions/<kind>`, whose query moved into
  `derived.session_rows()` so the route and the ctx share it (the route's
  output byte-for-byte unchanged); the data the roster timeline and
  sleep analysis build on.
  Then the timeline display primitive landed (no migration, no route
  change): `ctx.timeline(lanes, now, title)`, one lane or two of
  intervals on one vertical time axis — the host validating and
  normalising, the page laying it out to its tile (a week to the frame's
  height, never under 240px to the week; an 18px block floor; a now line;
  a `grey_after_now` lane greyed after it; scrolled, opening on now) and
  measuring after attach (a `requestAnimationFrame`, a `ResizeObserver`
  released with its tile) — and the `roster` app's tile, its centre
  column: rostered against flown over about six months and all to come.
  Then the roster app went config-driven (no host change: an app fixture
  and its tests): its duty-code rules, the owner's base and timezone and
  a destinations table moved out of `app.py` into a `config.json`
  installed beside it, read at import and fail-safe (see Layout).
  Then timeline v2 landed (no migration, no route change): a timeline
  opts into a shared **focus clock** with `ctx.timeline(..., sync=
  "<group>")` — the spec carries `sync`; the page keeps the group's
  focus time and whether it follows now (in memory, never stored, every
  group on now at load), draws a fixed cursor across the frame's middle,
  the UTC time under it to the quarter hour, the now line tagged Now and
  a Now button once scrolled off now, and scrolls every timeline of the
  group to one time — by time, never pixels; a scroll by hand moves the
  group and stops it following, a tick on each quarter hour moves a
  following group on. No `sync`: exactly the timeline before it. The
  roster's timeline is the first member (`sync="frms"`); a second panel
  joining `"frms"` syncs with it for free.
  Then trips & layover bands landed (no migration, no route change): a
  timeline lane takes optional `bands` — spans the page shades amber
  (`--layover`) behind its blocks, on the blocks' scale, the label on the
  layover past the flight out; a lane without is the spec from before —
  and the roster app's engine groups each lane's duties into trips
  (`trips()`: a flight out of `home_base` to the next flight back, the
  sectors between inside it, flights only; an open trip to the roster's
  end, rostered, or to now, flown), each a band labelled with its first
  port, its stable id the flight out's start (kept for the report store).
  Then the upload moved to the foot, with a Backfill checkbox (no
  migration, no route change): a file input's companion `toggle` (`{key,
  label, confirm}`) in the input vocabulary — its state sent in the same
  payload, the page asking its `confirm` in its own modal before a ticked
  send — and the roster tile `panel(timeline, picker)`, its picker's
  **Backfill** filing a past duty into `duties_rostered` too, captured
  `"backfill"` (`filed_as()`), with its rostered hours; for a roster
  uploaded after it started. Off is the plain split. Still to come: a
  count-aware confirm (after parsing) and an undo for a backfill.
  Still to come for it: reading `destinations` (time-zone shifts), the
  trip lists either side (planning, reports), their popups and PDF, the
  report store (re-attached to a trip by its id), per-block or per-band
  detail on click or hover, and `base_tz` day gridlines and WOCL shading
  on the axis.
  Still to come for write-back: an app replacing its own sessions and
  metrics over a date range (so a re-planned roster's dropped duties go),
  the rostered↔flown per-duty matching (needs a derived duty key — the
  feed has no `UID`), an app's own session
  kinds and series (and, with them, own-only scoping of a kind's read),
  a trailing `days=` on `ctx.sessions()`, cross-app reads
  of a custom metric, the later input bricks (date/time,
  auto-recompute), per-app Python dependencies (an app gets the image's
  Python; a PDF library isn't in it), several files in one input and,
  for a select, `{value, label}` options,
  host-side option-membership enforcement and multi-select,
  `base_tz` (slice A: an app's write lands on the server's local date
  until then), timestamped weigh-ins more than one a day (the instant
  grain), and how apps rank against other unnamed sources
  (`data-consolidation-model.md` §10 — today by name).
  Data lifecycle (A) landed (no migration): an uninstalled app's data
  removed on request — `GET /api/app-sources`, `DELETE
  /api/app-sources/<id>` (refused while the app's folder is there,
  loading or not — `app_present()`) through
  `purge_source()`, the connector wipe's delete and re-derive plus
  clearing the source's stale source roles, and a red Remove data in
  Connected sources and under a metric's sources. B1 landed (no
  migration): per-metric removal — `DELETE /api/app-sources/<id>/<metric>`
  through `purge_source(metric=…)` and a scoped `clear_source_roles(only=…)`;
  Your data's per-metric Remove data now takes only that metric (it used to
  wipe the whole app), and the orphan row expands to a Remove data per
  metric under its Remove all data — the one path to an orphaned custom
  metric's readings. Still to come for the data lifecycle: the same for
  `manual`'s frozen weigh-ins (a possible later addition) —
  `docs/architecture/apps-as-source.md`.
  Still to come for multi-tab: richer settings field types
  (select/enum/boolean) and a live preview. Also still to come: the
  grid pass (free cell placement as a second layout
  `mode` on the same sizes and CSS grid — coordinates, drop-anywhere,
  deliberate gaps, collision; it swaps `layoutTiles` and adds start cells
  to `spanTile`; resize stays the picker), a phone layout, the rest of the
  Library (its shell landed: the Data
  tab's Applications column, listing `/api/apps`; then the Add Application
  popup and `POST /api/apps`, installing an app from pasted or uploaded
  files — `app.py` became optional; then expandable app rows — reads, files
  in a read-only viewer (`GET /api/app/<id>/file`), a reserved writes-ticker
  slot, Refresh, Duplicate (slice 4, above) and Uninstall (`DELETE /api/apps/<id>`, the first
  destructive app action); then **App Update** (no migration): an app's
  files replaced in place, its id and data kept — `PUT /api/apps/<id>`
  (`update_app()`: the new set validated and staged, loaded as the
  scanner would load it, then atomically swapped for the old folder,
  which is kept aside until the new one is in; a whole-folder replace;
  no database work), and an **Update** button in the app row, left of
  Duplicate, opening the Add popup on the app's files with the id
  locked and Review as the confirm ("Update <name>? This replaces the
  app's files. Its data is kept."); the safe form of uninstall-then-add,
  which it replaces as the update path; Browse / Build / Create are still placeholders;
  next the writes ticker (now app write-back exists to count), then alerts;
  partial (single-file) updates, version history beyond the swap's
  recovery, disabling an app and zip upload are out),
  a custom `app.js`, enforcing `reads`, and the sandbox (Phase 4).
- **The access contract:** landed (see Data model): discovery, range queries
  with provenance, sessions, intraday series (passthrough); for the
  data-collation console, each catalog entry's
  `grain`/`category`, the catalog's
  `?include_empty=1` and per-day presence via `GET /api/coverage` (no
  migration); the console itself is `index.html` (frontend only). Still to come: a
  `?by_source` option for raw per-source reads, a derived pick for sessions
  (one per night across devices), and the series stitch.
Keep `GET /api/activity` returning its current flat shape (`{date, steps,
resting_hr, source}`, plus `synced_at` with `?by_source=1` — its `sleep_hours`
field went with `sleep_minutes` in v2) as a compatibility shim during the
data-model change (the console doesn't read it any more); `/api/weights`, the
other one, is retired — the weight app writes weight now. Remove the orphaned
`workouts` table + `/api/workouts` when convenient (unused by the UI).
