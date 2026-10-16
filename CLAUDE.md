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
  Beside it the session kinds (`SLEEP`, `WORKOUT` in `SESSION_KINDS`) and the
  series metrics (`HEART_RATE`, `RESPIRATION`, `SPO2` in `SERIES_METRICS`).
  Import keys and kinds from here; never spell metric or kind strings out in
  plugins or the API. `CATEGORIES` is the 8 labels in display order —
  Activity, Vitals, Body composition, Fitness, Sleep, Nutrition & hydration,
  Women's health (no keys yet), Other — and `GRAINS` the 4: `daily`,
  `instant` (a timestamped point like a weigh-in; a label for now, still
  filed one per day in `metrics`), `session`, `series`. `grain_for(key)` →
  `"series"` (in `SERIES_METRICS`), `"session"` (in `SESSION_KINDS`), else
  the metric's declared `"daily"`/`"instant"`; `category_for(key)` → its
  category (series metrics Vitals, `sleep` Sleep, `workout` Activity); both
  raise `ValueError` on any other key. There is no `manual` grain: hand
  entry is a source. Grain and category are labels: nothing is stored or
  picked by them.
- `app/derived.py` — the one place a source is picked per `(date, metric)`:
  `recompute_derived()` writes `derived_metrics`. Never pick a source anywhere else.
- `app/crypto.py` — Fernet encrypt/decrypt for stored credentials.
- `app/apps.py` — the app scanner/loader, the one way the host finds an app
  (the Library reuses it): `scan_apps(apps_dir)` → the `InstalledApp`s that
  load, in folder-name order. An app is a folder in `APPS_DIR` (`$APPS_DIR`,
  default `/data/apps`, on the data volume; `app.config["APPS_DIR"]` in tests)
  with an `app.yaml` (`id` (default: the folder's name, `[A-Za-z0-9][A-Za-z0-9_-]*`),
  `name` (default: the id), `reads` (a list of keys — recorded, not
  enforced), `config` (an ordered list of field mappings `{key, label,
  type, default}` — order drives the config form; `key` a non-empty
  string, unique; the rest kept as written, unchecked; a mapping is
  malformed), all optional but type-checked, and its **size, required**:
  `sizes`, a non-empty list of the tile sizes it's drawn at, each `{w, h}`
  in cells, whole numbers 1–`MAX_CELLS` (6, Home's width), none twice,
  the first the default (a fixed-size app lists one) — or the older
  `default_size: {w, h}` alone, a list of one; both only when
  `default_size` is the first of `sizes`. `manifest_sizes(raw)` reads them
  and, like every rule here, raises `AppError` with the reason; no size at
  all → `NO_SIZE` ("no tile size: ..."), and YAML's `{w:1,h:1}` (no space
  after a colon, so keys `'w:1'`) is named as such. The host never makes a
  size up. `tile_size(raw)` → a `{w, h}` or None, for the layout) and,
  optionally, an `app.py` whose `render(ctx, cfg)` is
  imported by `importlib` from its absolute path (nothing added to
  `sys.path`/`sys.modules`). With no `app.py` the app still loads and lists,
  its render the empty spec "Nothing to draw yet: this app has no app.py"
  (`NOTHING_TO_DRAW`); any other files in the folder are the app's own. A
  folder with no `app.yaml` isn't an app (no
  log); one that doesn't load — bad YAML or field, an import
  error, no callable `render` — logs `[apps] skipping <folder>: <why>` and is
  left out, never raising; a duplicate id keeps the first folder. Scanned
  on every request (no restart for a new app); a loaded app is cached until
  its `app.yaml`/`app.py` mtime or size changes, so a broken one logs once per
  change. `find_app(apps_dir, id)` picks one. `InstalledApp.summary()` is
  what `GET /api/apps` lists: `{id, name, default_size, sizes, reads,
  files}` — `default_size` the first of `sizes`;
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
  another folder loading under it) else 409 `"'<id>' already exists — remove
  it first to reinstall."` (install only creates, never updates); then
  `check_app(manifest, files)` — **the correctness layer that grows**; its
  one rule so far is a size (`manifest_sizes()`: "won't deploy without a
  size", refused 400 with the scanner's own reason); no other manifest
  field is checked, `app.py` isn't parsed, nothing is run. Only then is
  anything written: into a hidden
  `.install-<hex>` folder (the scanner skips dot-folders), renamed to `<id>`
  once whole, under a process lock (`_write_lock`, shared with uninstall),
  `APPS_DIR` made if missing; a failed
  write removes it (500). Every refusal raises `InstallError(message,
  status)` having written nothing; containment checks run before anything
  touches the disk and never move into `check_app()`.
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
- `app/host.py` — the app host: `Ctx(conn, today=None)`, built per render
  call, is all an app's `render()` gets. `ctx.metric(key, days=None)` → a
  list of `Point(date, value, source)` (a `NamedTuple`: `.date` a
  `YYYY-MM-DD` str, `.value` a float in the canonical unit, `.source`),
  ascending by date — `data[-1]` the latest day, `[]` for none — the derived
  points `GET /api/metric/<key>` serves (`derived_points()` in
  `app/derived.py`, shared by both), trailing `days` (`trailing_window()`:
  today − (days−1) … today) or all history; unknown key or a `days` not an
  int ≥ 1 (bools refused) → `ValueError`. `ctx.today()` → the server's
  local date as `YYYY-MM-DD`. The spec builders (also module functions):
  `number(value, label, caption, unit)`, `line(points, title=None)` →
  `{"type": "line", "points": [[date, value], ...], "title"}` (points
  `(date, value)` pairs — the apps' `[(p.date, p.value) for p in data]` — or
  `Point`s, `{date, value}` dicts or numbers, each to a `[date, value]` pair,
  date null for a bare number; a date a `YYYY-MM-DD` str, `date` or `None`;
  `None`/non-finite is a gap; anything else `TypeError`; `title` text or
  `None`, else `TypeError`),
  `panel(*children)` (or one list), `empty(message)`; the host's own is
  `{"type": "error", "message"}`. `render_app(app, conn, cfg=None,
  today=None)` calls `render(ctx, {})` and never raises: an exception, a
  non-spec (not a dict with a string `type`) or a spec that isn't plain JSON
  (NaN, a set) is logged (`[apps] <id>: ...`, with the traceback) and comes
  back as an error spec. Full contract: `docs/architecture/app-host.md`.
  The two real apps, `steps` and `steps-metres`, are committed in
  `tests/fixtures/apps/` (as deployed in `/data/apps`) and run against the
  host by `tests/test_app_host.py`: they're correct per the contract, so a
  failure there is fixed in the host, never by editing them. Their `app.py`
  is verbatim; their `app.yaml` declares `sizes: [{ w: 1, h: 1 }]`, the
  canonical form, where the deployed copies still say `default_size: { w:
  2, h: 2 }` — which keeps loading (tested from the fixtures, swapped back).
- `app/home_layout.py` — the Home tab's layout, one `settings` row
  (`LAYOUT_KEY` `home_layout`, no migration): `{"mode": "flow", "subtitle",
  "items": [{app_id, size: {w, h}}]}`. Flow is the one mode: the items'
  order is the layout, each size one its app declares; no coordinates
  (`mode` holds the seat for a later grid mode, so that's a second mode,
  not a migration). `load_layout(conn, apps)` → `reconcile(stored, apps)`
  against the apps installed now (`scan_apps()`): stored items in order,
  less any whose app isn't installed (or a repeat, or junk); each at its
  stored size if its app still offers it, else the app's default; then
  every app no item places, at its default, in folder order. A stored row
  that isn't a layout (it can be written through `/api/settings`) reads as
  none saved. A read never rewrites the row, so an app uninstalled and
  reinstalled before the next save (the update path) comes back where it
  was. `parse_layout(body)` checks a save's shape only — `mode` `"flow"`,
  `subtitle` text ≤ `MAX_SUBTITLE` (120), `items` a list of `{app_id`
  (the scanner's `APP_ID`), `size` (`tile_size()`)}`, each id once —
  dropping anything else, else `LayoutError`; it never checks which apps
  are installed or what sizes they offer. `save_layout()` stores it.
  Routes: `GET /api/home-layout` (reconciled) and `PUT /api/home-layout`
  (`{ok: true}`, or 400 storing nothing).
- `app/templates/index.html` — the data console, one self-contained page
  (vanilla JS, no build step, no external calls or web fonts; every colour/font/
  radius in its `CUSTOMISE OPENFIT HERE` token block). Under the top bar
  (date, a `settings` button, the theme toggle) sits the original angled tab
  strip, restored verbatim: **Home** (far left, the installed apps' tiles —
  below) and **Data** (far right, the console), then the dashed **＋ Add**.
  The Data tab is two even, fluid columns (`.console`, a grid of two
  `.console-col`s, capped at 1600px): Connected sources over Your data on
  the left, **Applications** on the right (below); at 1240px and under it
  drops to one column, at the old 940px cap, Applications under the data —
  narrower than that, a Your data row would squeeze the metric's name. `settings`
  opens the Settings popup (the `.modal` chrome): one control, **Opening
  page**, a dropdown built from the tab strip (`.tab[data-tab]`, so a new tab
  appears in it), saved per browser in `localStorage` `of-opening-tab`; boot
  opens that tab via `showTab()` (Data if unset or no longer a tab) in place
  of a hardcoded default. ＋ Add, Add source once every connector is
  connected, and Applications' Browse / Build / Create each open one shared
  coming-soon placeholder modal
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
  "Your data" groups its rows by `category`, the 8 in `CATEGORIES` display
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
  `start`, since `/api/sessions` lists by source priority. **Applications**
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
  separately from Home, and again after an install or uninstall. A row
  expands as a Your data row does — one mechanism, `expandable(item, head,
  o)`, which both `rowItem()` and `appRow()` hand their item and head to:
  the head a button, its click showing/hiding the panel, the chevron
  turning (`.open`), `aria-expanded`/`aria-controls` kept; the open ids in
  a Set (`state.open`, `appsList.open`) so a row stays open across a
  refresh. An app row is a `.ditem.aitem`, its head a `.drow.arow`
  (chevron · name); open, `appPanel()` is a `.dpanel.apanel` of three
  columns (one on a phone): **Reads** (the manifest's keys, `esc()`d, the
  metric's name on hover; none → a muted "No declared reads"), **Files**
  (each a button opening `openAppFile()`: a `.wide` modal reading
  `GET /api/app/<id>/file` into a read-only monospace `<pre>` as
  `textContent`, never markup; an empty file or the server's refusal shown
  as a message), and **Writes** — `writesTicker()`, a reserved slot
  (Day / Wk / Mth, each `—`; nothing counted or read), marked `//
  writes-ticker: filled in when app-tier write-back exists`. Under them
  **Refresh** (`btn sm`: re-fetches `GET /api/app/<id>/render` — the
  existing route, no rescan, no reload — and `repaintTile()` redraws that
  app's Home tile in place from the fresh spec; a render that failed says
  so) and **Uninstall** (`btn sm danger`), which only opens an inline
  `danger-panel` ("Uninstall <name>?" / "This deletes its files from
  `/data/apps/`." — [Cancel] / [Uninstall]; no `confirm()`); its confirm
  `DELETE`s `/api/apps/<id>`, then `loadApps()` + `loadHome()` (the row and
  the tile go) and notes "✓ Uninstalled <name>"; an error stays in the
  row. Alerts are their own branch. **The Add Application popup** (`openAddApp()`,
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
  preview (the last top-level `id:` line, quotes and a trailing comment
  stripped, checked against the server's `INSTALL_ID`): "Install
  application: `<id>`", or why it can't (empty, no id, an unclosed quote,
  a bad id) with Install disabled; the file names as chips; and, when any
  file ends `.py`, "This app includes custom code and runs with full
  access to your OpenFit data." (`CUSTOM_CODE`). **Install** `POST`s
  `/api/apps` `{files}`: on success the popup closes and
  `appInstalled()` re-reads Home and the list at once, then notes "✓
  Installed <name>" — or, if the new id isn't listed (installed but the
  scanner skipped it), that it doesn't load and the server's log says
  why; an error (the server's message, as given — taken id, parse error)
  stays in the popup. Every name and id reaching `innerHTML` is `esc()`d.
  The page's checks are UX only; the server re-checks everything. A
  test runs `fileNameProblem()`/`manifestId()` under node (skipped
  without it) against the server's own checks. A round voice button
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
  `DELETE /api/apps/<id>`, `/api/app/<id>/render`, `/api/app/<id>/file`)
  and Home's layout (`GET`/`PUT /api/home-layout`);
  not `/api/activity` (the last-sync stamp that read it
  is gone). See `docs/architecture/frontend-redesign-and-widget-model.md`.
  **Home** reads `GET /api/apps` and `GET /api/home-layout` together, then
  every `GET /api/app/<id>/render` in parallel (a failed fetch is that
  app's error tile), and is re-read after a Sync now or a source removal,
  as the data card is, and after an app install or uninstall; an app row's
  Refresh redraws just its own tile (keeping its Configure controls).
  Three parts, kept apart: `loadHome()` → `home.tiles`, `[{app_id, name,
  spec, w, h, sizes}]` in layout order; `layoutTiles` (= `flowLayout(items,
  apps)`, the server's reconcile mirrored — it squares the read layout with
  the apps read beside it, and an unsaved Configure with an install or
  uninstall made meanwhile; a node test holds it to `reconcile()`) gives
  the order and each size; `paintTiles(box, tiles, configuring)` draws them
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
  (`.tile-grip`) and, only if its app offers more than one size, a
  `<select>` of those sizes (`w × h`; choosing one sets the tile's span,
  the grid reflowing live) — the empty cells are marked (`--tile-guide`),
  a drop row sits under the tiles, and the subtitle is an input (≤
  `SUBTITLE_MAX`, the server's 120). `sortableTiles(grid)`: drag a tile
  (anywhere but its picker) onto another and it takes that one's place
  (`dropSpot()`: from earlier in the order, after it; from later, before
  it; into a row-end gap, after that row's last tile; into the drop row,
  last), the rest reflowing live, once per spot and only after the pointer
  moves `SETTLE` px from the last reorder (mixed sizes would chase it
  otherwise); a handle's arrows step it (←/↑ back, →/↓ on); `home.tiles`
  follows the DOM. Off: `PUT /api/home-layout` `{mode: 'flow', subtitle,
  items: layoutItems()}`; on success back to view and "✓ Layout saved",
  on failure it stays in Configure with the error. The subtitle and every
  app name are set as text (`textContent`, `.value`, attributes), never
  markup. `renderSpec()` draws the vocabulary, all as text,
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
  `panel` (children stacked; the line takes the rest), `empty`/`error` (muted,
  centred; error under "Couldn't render <name>"), any other type an error
  tile. No apps → a note pointing to ＋ Add Application (or the apps
  folder); `/api/apps` failing → a Retry.
- `tests/` — pytest, temp DB, no network.
- `docs/architecture/` — design direction (read first). `docs/design/` — frontend references.

## Data model
- Current: a tidy `metrics(date, source, metric, value, unit, synced_at)` keyed
  `(date, source, metric)` — one row per reading, so a new metric is data not a
  migration. Keys, units, categories, grains and plausible ranges come from
  `app/metrics.py` — the v2 vocabulary, 35 keys (the full table is
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
  `bone_mass_kg` — Garmin's `weight_kg` sits beside the `manual` one and
  outranks it in the default precedence. That unit (and the
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
    sessions only: there is no daily sleep metric (v2 retired `sleep_minutes`
    — a daily total is an aggregate, an app's to compute), and neither plugin
    files one.
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
- Home layout: the `settings` row `home_layout` (no migration) holds JSON
  `{"mode": "flow", "subtitle", "items": [{"app_id", "size": {"w", "h"}}]}`
  — the tiles' order and sizes and the line over them, written by
  Configure through `PUT /api/home-layout` and read, reconciled with the
  installed apps, through `GET /api/home-layout` (`app/home_layout.py`;
  see Layout). It's config, not data: an app's tile placement, never a
  metric.
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
    also carries `grain` (`grain_for()`: a metric's declared `"daily"` or
    `"instant"`, `"session"`, `"series"`) and `category` (`category_for()`)
    — the console groups by them. (The pre-v2 `cadence` label is gone,
    removed in PR 4 of the v2 rollout.) `?include_empty=1` (also `true`/`yes`;
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
  - `GET /api/sessions/<kind>?from=&to=` → `[{start, end, source, summary}]`,
    a priority-ordered passthrough: every source's sessions — no pick, filter
    or merge — the source the pick would rank first leading, then the rest
    (`source_rank()`: a `source_roles` entry under the kind's own key if one
    is stored, then `DEFAULT_PRIORITY`, then by name), each source's
    ascending by start. `summary` parsed from `summary_json` (`{}` if none),
    `end` may be null. `from`/`to` match the start's UTC date. Unknown kind
    404.
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
  `GET /api/apps`, `GET /api/app/<id>/render` (an error spec, never a 500),
  `?days=` on the contract reads, and Home drawing the tiles flowed at their
  default size (no migration). Apps run as trusted code in the server's
  process. Configure mode landed in flow form: the manifest's `sizes`
  (required, 1–6 cells either way, the older `default_size` a list of
  one; `check_app()`'s first rule), a six-column grid of square cells,
  drag-to-reorder and a size picker, an editable subtitle, saved as the
  `home_layout` settings row through `GET`/`PUT /api/home-layout` and
  reconciled with the installed apps on every read (no migration).
  Still to come: the grid pass (free cell placement as a second layout
  `mode` on the same sizes and CSS grid — coordinates, drop-anywhere,
  deliberate gaps, collision; it swaps `layoutTiles` and adds start cells
  to `spanTile`; resize stays the picker), a phone layout, the rest of the
  Library (its shell landed: the Data
  tab's Applications column, listing `/api/apps`; then the Add Application
  popup and `POST /api/apps`, installing an app from pasted or uploaded
  files — `app.py` became optional; then expandable app rows — reads, files
  in a read-only viewer (`GET /api/app/<id>/file`), a reserved writes-ticker
  slot, Refresh, and Uninstall (`DELETE /api/apps/<id>`, the first
  destructive app action); Browse / Build / Create are still placeholders;
  next the writes ticker (once app-tier write-back exists), then alerts;
  updating in place, editing files, disabling an app and zip upload are
  out — Uninstall, then add, is the update path),
  per-instance `cfg` and its UI, metric write-back, a custom `app.js`,
  enforcing `reads`, and the sandbox (Phase 4).
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
field went with `sleep_minutes` in v2), and `/api/weights` its
`[{date, weight}]` shape (manual `weight_kg` readings only), as compatibility shims
during the data-model change (the console reads neither any more). Remove the orphaned
`workouts` table + `/api/workouts` when convenient (unused by the UI).
