# OpenFit

Self-hosted middleware for your health and fitness data. OpenFit pulls data from
the devices you own (Garmin, Google Health / Fitbit, soon Oura and others),
normalises it into one dataset you own — the **OpenFit Derived Data** — and
exposes it to apps that read it. It runs in a single Docker container on your own
hardware; nothing leaves your network except each connector's own call to its
device's API.

OpenFit is **not a tracker, and ships no tracking apps.** It is a *database
manager* and a *host for apps*. Weight logs, training planners, dashboards — those
are external apps that read the data; the platform itself just owns, derives and
serves the data. Think of it as an open, self-hosted Apple HealthKit / Google
Health Connect.

Built and run by one person on a home server — see [ROADMAP.md](ROADMAP.md) for
where it's headed, and
[docs/architecture/openfit-overview-and-design-pathway.md](docs/architecture/openfit-overview-and-design-pathway.md)
for the full design.

## Why this exists

Most trackers lock you into one app tied to one device brand, and most brands
lock your data in their cloud. Your health history should be yours — stored where
you choose, usable how you choose. OpenFit keeps it on your hardware and makes it
available to whatever apps you point at it, including ones you write yourself.

## Status

Early, and the current focus is the **data engine**: ingestion plugins, a
normalised derived dataset with per-source provenance, and the API that exposes
it. The web UI in this repo is a data console — what the dataset holds, how
continuous it is, and which source wins each metric — not the product.

## What's here today

- A versioned SQLite data store with a hand-rolled migration runner.
- **Garmin Connect** plugin — steps, resting HR, daily distance, active, total
  and basal calories, floors climbed, elevation gain, moderate and vigorous
  intensity minutes, sedentary minutes, daily SpO2, respiration rate and HRV
  (on devices that measure them), VO2 max, hydration, weight, body fat, BMI,
  muscle and bone mass, body water and visceral fat (from a compatible
  scale), sleep (a
  per-night session with stages), workouts (a session per
  activity), and intraday series: heart rate and respiration rate (a sample
  every couple of minutes) and SpO2 (hourly averages).
- **Google Health** plugin — steps, distance, floors, elevation gain, active and
  total calories, Active Zone Minutes, sedentary minutes, resting HR, HRV,
  VO2 max (cardio fitness score), SpO2 and respiration rate (nightly averages),
  sleep, workouts, and intraday series: heart rate (a per-minute average,
  computed by Google) and SpO2 (a reading a minute while asleep), from Pixel
  Watch / Fitbit via Google's cloud Health API (not the legacy Fitbit Web API,
  retired September 2026).
- Scheduled background sync plus a manual sync (`POST /api/sync/<plugin>`), with
  device connections managed in-app and credentials encrypted at rest. Each
  source's sync frequency is set from its Settings popup: every 1h, 3h, 6h (the
  default), 12h or 24h. `SYNC_INTERVAL_HOURS` still sets the default (6 unless
  you change it); a source on Default follows it. One sync runs at a time: a
  manual sync asked for while one is running gets
  `409 {"ok": false, "error": "a sync is already in progress"}` instead of a
  second pull, and a scheduled run that finds one running skips that tick.
- A **webhook** any automation can push a reading to, guarded by a
  regeneratable token and per-metric sanity ranges.
- The **access contract** — a small, stable read API apps build on: what data
  there is, one metric over a date range (with which source each value came
  from), sleep/workout sessions, and intraday series like heart rate,
  respiration and SpO2.
- A built-in **data console**: connected devices (add, sync, and a per-source
  Settings popup showing the account, setting how often it syncs, and removing
  it, keeping or wiping its data), every metric, series and session kind
  grouped by category with its latest reading and grain (what isn't
  reporting yet folded away under its category), how continuously each is
  covered (day / week / month, and the last five weeks), and drag-to-reorder
  source priority per metric.
- A first **app host**: the Home tab shows the apps you install (none ship with
  OpenFit) as live tiles — see [Apps on the Home tab](#apps-on-the-home-tab).

## Webhook input (push)

Beside pull plugins and manual entry, automations can push a reading the moment
it happens — Home Assistant on each smart-scale weigh-in, a Shortcut, a script.
One URL for every metric:

```bash
curl -X POST http://<your-server-ip>:8080/api/webhook/<token> \
  -H 'Content-Type: application/json' \
  -d '{"metric": "weight_kg", "value": 82.4, "source": "eufy"}'
```

- `metric` is a canonical key and `value` a number in its unit — the
  vocabulary is [metric schema v2](docs/architecture/metric-schema-v2.md)
  (`app/metrics.py`), 35 keys grouped by category: activity (`steps`,
  `distance_m`, `floors`, `active_kcal`, `total_kcal`, `basal_kcal`,
  `sedentary_min`, `elevation_gain_m`, `intensity_moderate_min`,
  `intensity_vigorous_min`, `active_minutes`), vitals (`resting_hr_bpm`,
  `hrv_ms`, `spo2_pct`, `respiratory_rate_brpm`, `skin_temperature`,
  `body_temperature`, `blood_pressure_systolic`, `blood_pressure_diastolic`,
  `blood_glucose` in mmol/L), body composition (`weight_kg`, `body_fat_pct`,
  `bmi`, `height_cm`, `muscle_mass_kg`, `bone_mass_kg`, `body_water_pct`,
  `visceral_fat`), fitness (`vo2_max`), nutrition & hydration
  (`hydration_ml`, `calories_in`, `carbs_g`, `protein_g`, `fat_g`) and
  `mood` (1–5). There is no daily sleep metric: sleep is sessions only.
- `date` (`YYYY-MM-DD`) is optional and defaults to today; `source` is optional
  and defaults to `webhook`. Name your device (`"eufy"`) and it takes part in
  source roles like any other source — make it a metric's primary and it wins.
  A pull plugin's id (`garmin`, `google_health`) is refused: its syncs own those
  readings.
- A value outside the metric's plausible range is refused (400), so a misfired
  automation can't corrupt your data — e.g. steps 0–100000, resting HR 20–200
  bpm, weight 20–400 kg, blood glucose 1–40 mmol/L, mood 1–5. Every key's
  range is in the [v2 table](docs/architecture/metric-schema-v2.md#the-canonical-metrics).
- Pushing the same metric and source for a day again replaces the reading, and
  the day's derived value is updated at once.

The token in the URL **is** the auth — the only route that has any. Get it (and
the full URL) from `GET /api/webhook-token`; it's made on first ask. If it leaks
(it sits in your automation's config and in OpenFit's request log),
`POST /api/webhook-token/regenerate` replaces it, and the old one stops working
immediately. This is also the path by which on-device sources (e.g. Apple
HealthKit) will reach OpenFit. Interval data (sleep, workouts) can't be pushed
yet.

## Reading the data (the access contract)

Apps read OpenFit through five read-only endpoints. Daily metrics are served
from the **derived** profile — the one value OpenFit picked per metric per day,
each tagged with the source it came from — never the raw per-source readings, so
an app gets one trusted answer and never has to reconcile devices itself.
Sessions and intraday series aren't picked between sources yet: every source's
are listed, each tagged. Same trusted-network model as the rest of the API: no
auth. These shapes are the stable contract; the `/api/activity` and
`/api/weights` routes are compatibility shims, not for apps.

**Discovery — `GET /api/catalog`.** Every metric, session kind and series that
has data:

```json
{
  "metrics": [
    {"metric": "steps", "unit": "count", "grain": "daily",
     "category": "Activity", "sources": ["garmin", "google_health"],
     "first": "2026-10-07", "last": "2026-10-08", "count": 2,
     "last_value": 10450.0, "last_date": "2026-10-08"}
  ],
  "sessions": [
    {"kind": "sleep", "grain": "session", "category": "Sleep", "count": 1,
     "first": "2026-10-07T22:41:00Z", "last": "2026-10-07T22:41:00Z"}
  ],
  "series": [
    {"metric": "heart_rate", "unit": "bpm", "grain": "series",
     "category": "Vitals", "sources": ["garmin"],
     "first": "2026-10-07T00:00:00Z", "last": "2026-10-08T23:58:00Z",
     "count": 1440}
  ]
}
```

`sources` are the sources that have supplied the picked value on any day, in the
order the pick tries them; `first`/`last`/`count` are the metric's derived days,
and `last_value` the value on `last_date`. A session kind's `first`/`last` are
its earliest and latest start. A series' `sources` are every source with a
sample of it, and `first`/`last` its earliest and latest sample. An empty
install returns three empty lists.

Every entry is labelled with its `grain` — `daily` (one value per day),
`instant` (a single timestamped point, like a weigh-in or a blood-pressure
reading; still one value per day for now), `session` (intervals) or `series`
(intraday samples) — and its `category`: Activity, Vitals, Body composition,
Fitness, Sleep, Nutrition & hydration, Women's health or Other. (The pre-v2
`cadence` label is gone: build on `grain` and `category`, as the built-in
console does.) Add `?include_empty=1` to list every metric, session kind and
series OpenFit knows, including those with no data yet — `"count": 0`, `"sources": []` and null
`first`/`last` (and `last_value`/`last_date`) — in the same order, for showing
what isn't reporting yet.

**Coverage — `GET /api/coverage?from=YYYY-MM-DD&to=YYYY-MM-DD`.** Which days
hold any data, whatever the source, for drawing continuity without fetching
every sample:

```json
{
  "metrics":  {"steps": ["2026-10-07", "2026-10-08"], "weight_kg": ["2026-10-08"]},
  "series":   {"heart_rate": ["2026-10-07", "2026-10-08"]},
  "sessions": {"sleep": ["2026-10-07"]}
}
```

A metric's dates are its derived days, a series' the dates it has a sample on,
and a session kind's the dates a session starts on — ascending, each once.
Series and session dates are UTC dates for now (a user-local day boundary is
planned). Only keys with data in range are listed, so an empty install returns
three empty maps.

**One metric — `GET /api/metric/<metric>?from=YYYY-MM-DD&to=YYYY-MM-DD`.**

```json
{"metric": "steps", "unit": "count", "points": [
  {"date": "2026-10-07", "value": 9120.0, "source": "garmin"},
  {"date": "2026-10-08", "value": 10450.0, "source": "google_health"}
]}
```

Oldest first, one point per day, in the metric's canonical unit
(`app/metrics.py`). An unknown metric is a 404; a known one with no data has
`"points": []`.

**Sessions — `GET /api/sessions/<kind>?from=YYYY-MM-DD&to=YYYY-MM-DD`**
(`sleep` or `workout`).

```json
[{"start": "2026-10-07T22:41:00Z", "end": "2026-10-08T06:12:00Z",
  "source": "garmin",
  "summary": {"asleep_minutes": 431, "deep_minutes": 78, "light_minutes": 251,
              "rem_minutes": 102, "awake_minutes": 20}}]
```

Times are ISO 8601 UTC, and `summary` holds the kind's fields that the device
reported (`{}` if none). Sessions aren't picked between sources yet, so a night
both devices tracked appears once per device — but they're listed by source
priority: the source OpenFit would pick first (Garmin, then Google Health, then
any other, by default), then the rest, each source's oldest start first. Sleep
is sessions only — there is no daily sleep total; sum `asleep_minutes` the way
your app wants. An unknown kind is a 404.

**Intraday series — `GET /api/series/<metric>?from=YYYY-MM-DD&to=YYYY-MM-DD`**
(`heart_rate` in bpm, `respiration` in breaths/min, `spo2` in %).

```json
{"metric": "heart_rate", "unit": "bpm", "points": [
  {"ts": "2026-10-12T15:16:00Z", "value": 77, "source": "garmin"},
  {"ts": "2026-10-12T15:18:00Z", "value": 79, "source": "garmin"}
]}
```

Every recorded sample, oldest first, each tagged with its source; times are ISO
8601 UTC. For now this is a straight passthrough: if two devices both sampled a
stretch — Garmin and Google Health both record heart rate and SpO2 — both
devices' samples are listed, a minute both covered once per device. Stitching
them into one timeline by source priority is a later step. Series keys are their
own vocabulary, so a daily key such as `resting_hr_bpm` or `spo2_pct` is a 404
here, like any unknown metric; a known one with no samples has `"points": []`.

`from` and `to` are optional and inclusive; leave one out for an open end. For
sessions they match the start's UTC date, and for series the sample's; coverage
filters each of its dates the same way. A malformed date, or `from` after `to`,
is a 400. Instead of them, `?days=N` keeps the trailing N days — today (the
server's date) and the N − 1 before it — so `GET /api/metric/steps?days=7` is
this week's steps, at most seven points; `days` with `from` or `to`, or one
that isn't a whole number from 1 up, is a 400.

## Apps on the Home tab

OpenFit ships no apps, but it hosts the ones you install: each is drawn as a
tile on the Home tab, from your live data. An app is a folder in `/data/apps`
(on the data volume; `$APPS_DIR` moves it) holding an `app.yaml` manifest and
an `app.py`:

```yaml
# /data/apps/steps/app.yaml
id: steps
name: Steps
default_size: {w: 2, h: 2}   # tile size, in grid cells
reads: [steps]
config:                      # the app's settings, in form order
  - {key: days, label: Days shown, type: int, default: 7}
```

```python
# /data/apps/steps/app.py
def render(ctx, cfg):
    data = ctx.metric("steps", 7)    # [Point(date, value, source), ...], oldest first
    if not data:
        return ctx.empty("No steps yet")
    return ctx.panel(
        ctx.number(round(data[-1].value), label="steps"),
        ctx.line([(p.date, p.value) for p in data], title="last 7 days"),
    )
```

`render()` runs on the server each time Home loads. `ctx.metric()` is its one
data path — a metric's derived points, as `/api/metric` serves them, as a list
of points with `.date`, `.value` and `.source` — and it
returns a display spec built from `number`, `line` (a sparkline, with an
optional title), `panel` and `empty`. Tiles flow left to right at their default size; an app that fails to
load is logged and left out, and one whose `render()` fails shows as an error
tile, never a broken page. `GET /api/apps` lists what's installed and
`GET /api/app/<id>/render` returns one app's spec. Apps run as trusted code
for now (a sandbox is Phase 4). The full contract is in
[docs/architecture/app-host.md](docs/architecture/app-host.md).

## Quick start

```bash
git clone https://github.com/Raven-git-hub/OpenFit.git
cd OpenFit
cp .env.example .env
docker compose up -d --build
```

Visit `http://<your-server-ip>:8080`. It runs fine with zero plugins configured.

### Connecting a device

Devices are added in the app, not a config file: **Connected sources → Add
source**. Pick the device, fill in what it asks for, hit Add.
Credentials are encrypted before they're stored; a source's **Settings** shows
its login (never the password or a token), how it connected, its last sync and
when it was added. **Sync every** there sets how often that source syncs — 1h,
3h, Default (6h, or whatever `SYNC_INTERVAL_HOURS` is set to), 12h or 24h —
taking effect at once (`PUT /api/connectors/<plugin>/cadence`, `{"hours": 3}`,
or `null` for Default). **Remove source** there deletes the credentials along with
any cached session token, and asks whether to keep the source's historical data
or wipe it from the database (`DELETE /api/connectors/<plugin>?wipe=1`, which
also re-derives the days it covered). An install that predates this flow keeps
working: the `.env` variables are read as a fallback until you re-add the device.

### Garmin Connect plugin

Add it under **Connected sources** with your Garmin email and password. If your
account has MFA/2FA, run the one-time interactive login first so the background
sync never has to handle the prompt:

```bash
docker compose run --rm tracker python3 first_login.py
```

### Google Health plugin (Pixel Watch / Fitbit)

Uses Google OAuth, so adding it is two steps, all in the UI. First, the one-off
Google-side setup: create a project in Google Cloud Console, enable the **Google
Health API**, set the OAuth consent screen to External/Testing with your account
as a test user, add the `googlehealth.*.readonly` scopes, and create an OAuth
Client ID of type **Desktop app**. Then, under **Add source → Google Health**:
paste the client ID and secret, press **Get authorization link**, approve on
Google (it redirects to `http://127.0.0.1:9109/`, which OpenFit deliberately does
not serve — the page fails to load, that's expected), and paste the address (or
the code) back. The refresh token is stored encrypted and the sync uses it from
then on.

**The weekly re-auth.** Google's Health API scopes are restricted, so while the
OAuth app is unverified (Testing mode) its refresh token expires after about 7
days, and syncs fail until you re-authorise. Verification needs a paid security
assessment, so for a self-hosted install this is routine: open the Google
source's **Settings** → **Re-authenticate**, approve on Google, and paste the
address (or code) back, as when adding it. Your stored client ID and secret are
reused (the secret never leaves the server), and the account, its data, sync
cadence and source roles all stay put — only the refresh token is replaced. No
removing the source, no re-entering the secret, and no revoking access at
myaccount.google.com first: OpenFit asks Google with `prompt=consent`, so every
authorization returns a fresh refresh token.

## Architecture

```
app/
  main.py                     Flask API + scheduler, loads plugins from plugins/
  apps.py                     Finds and loads the installed apps (/data/apps)
  host.py                     The app host: an app's ctx, its spec builders, render
  templates/index.html        The data console + Home's app tiles (vanilla JS, no build step)
  audio/                      The console's voice clips, baked into the image
  plugins/
    base.py                   SyncPlugin interface every pull plugin implements
    __init__.py               PLUGINS registry — one line per installed plugin
    garmin/plugin.py          Garmin Connect
    google_health/plugin.py   Google Health API (Pixel Watch / Fitbit)
  metrics.py                  Canonical metric, session and series vocabulary
  derived.py                  Picks the one derived value per metric per day
  crypto.py                   Encrypt/decrypt for stored device credentials
  migrations/                 Versioned .sql schema migrations + runner
tests/                        pytest suite (temp DB, no network)
docs/architecture/            Design direction (read these first)
```

Data lives in SQLite on a persistent Docker volume. Devices sync through plugins
into a per-source store, and OpenFit derives a single value per metric per day
from it while keeping every source's raw rows. That store is the tidy `metrics`
table — one row per `(date, source, metric)` reading, filed under a canonical
vocabulary (`steps`, `resting_hr_bpm`, `weight_kg`, ... in `app/metrics.py`,
each with its unit, category, grain and plausible range — see
[metric schema v2](docs/architecture/metric-schema-v2.md)) — so a new metric
needs no migration. Weight is one of those
metrics: hand-entered weigh-ins are `weight_kg` readings (in kg) under the source
`manual` (Garmin files its own from a compatible scale, under `garmin`), and
there is no separate weights table any more. A source files only what it
measured: on a day a Garmin watch wasn't worn (no heart rate recorded), the 0
active calories and full day sedentary Garmin still reports are defaults, not
readings, so they aren't stored and another source's real reading for that day
wins (or the day is an honest gap); basal calories, a profile constant, are
kept. Beside `metrics`, a `sessions`
table holds interval records, each with a start/end and a summary, filed under
a session kind from the same vocabulary. A `sleep` session is one
night (or nap) from each device, with its stage breakdown in minutes
(`asleep_minutes`, plus `light_minutes`/`deep_minutes`/`rem_minutes`/
`awake_minutes` where the device reports them); sleep is sessions only, with no
daily total beside it (schema v2 retired the old `sleep_minutes` metric). A
`workout` session is one activity, with no daily metric beside it:
its `type` (the device's activity type, lowercased), `duration_minutes`,
`distance_m`, `avg_hr_bpm` and `calories_kcal`, whichever the device reports.
A third table, `samples`, is the series (intraday) grain: one row per
`(source, metric, timestamp)` for readings like heart rate every couple of
minutes, filed under its own series vocabulary (`heart_rate` in bpm,
`respiration` in brpm and `spo2` in %, distinct from the daily `resting_hr_bpm`,
`respiratory_rate_brpm` and `spo2_pct`). A null reading — a non-wear gap — is
dropped rather than stored, as are Garmin's negative "no reading" markers in its
respiration and SpO2 arrays. Garmin fills it with intraday heart rate,
respiration and SpO2 (the last two from the same responses as its daily vitals,
so no extra requests). Google Health fills heart rate and SpO2 at one value a
minute: Google keeps heart rate at about a reading a second, so OpenFit asks
for 60-second roll-ups (each minute's average, computed on Google's side), and
lists SpO2, already about a reading a minute, as it comes. Apps read it through
`GET /api/series/<metric>`.
The per-day value is stored, not worked out on each read: a `derived_metrics`
table holds one value per `(date, metric)`, tagged with the source it came from,
picked by `app/derived.py` after every sync (for the days that sync covered) and
once on the first boot of an upgraded database; a webhook reading or a
hand-entered weight re-derives its own day as it lands (and clearing weights
re-derives the days cleared). The access contract and `GET /api/activity` read
it.
By default the pick is a fixed source precedence (Garmin, then Google Health, then
any other source); per metric, you can name a different primary source — e.g.
Google for steps, Garmin for everything else — with
`PUT /api/source-roles/<metric>` `{"primary": "google_health"}` (`null` reverts
to the default; `GET /api/source-roles` shows each metric's primary and the
sources that have reported it). On a day the primary didn't report, the default
order picks. A change is forward-only: it applies from the next sync, over the
days that sync covers, and never rewrites values already derived. See
[docs/architecture](docs/architecture/). Device credentials entered in the UI
are stored as a Fernet-encrypted JSON blob (one row per plugin) and never
returned over the API; the key comes from `$OPENFIT_SECRET_KEY` or is generated
at `/data/.secret_key` — back it up with the database. The database runs in
SQLite's WAL mode: recent writes can sit in `tracker.db-wal`, beside
`tracker.db`, until they're checkpointed into it, so back up the whole `/data`
volume — or, copying the file by hand, take `tracker.db-wal` with it when there
is one (`tracker.db-shm` is rebuilt and needn't be copied).

### Schema migrations

Numbered `.sql` files in `app/migrations/`, applied in order, each once, inside a
transaction, recorded in `schema_migrations`. They run at startup and can be run
by hand against a *copy* first when a migration touches real data. There is
deliberately no Alembic/SQLAlchemy — it's ~100 lines of stdlib `sqlite3`.

### Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Runs against a temporary SQLite file with no network calls — the plugin contract
is tested with a fake in-process plugin, never against Garmin or Google.

## Writing a plugin

A plugin is a class implementing `SyncPlugin` (`app/plugins/base.py`): declare a
connector manifest (`fields`, `add_flow`), read credentials via
`self.get_credentials(conn)`, and implement `sync(conn, days)` to fetch from your
source and write each reading with
`write_metric(conn, date, self.id, metric, value)` — a canonical key from
`app/metrics.py`, with the value already in that metric's unit. Interval data
goes in as a session with `write_session(conn, self.id, kind, start, end,
summary)`, `kind` a session kind from the same module: a night's sleep
(`SLEEP`) or a workout (`WORKOUT`), neither with a daily metric. Register
it with one line in `app/plugins/__init__.py` — no changes to
`main.py`, the API, or the UI. Oura's clean public API makes it the natural next
plugin.

## Privacy & security

- Everything lives in the SQLite volume on your machine. Nothing leaves your
  network except each plugin's own calls to its own data source.
- Google Health uses a standard OAuth refresh token, revocable anytime from your
  Google account permissions; Garmin uses the password only for the first login,
  then a cached session token.
- No login on the web UI. Fine on a trusted home network; don't expose it to the
  public internet without auth in front (reverse proxy or VPN).

**Multi-user:** not row-level multi-tenancy. If this ever runs for more than one
person, the plan is one isolated container per user behind a shared "Host" layer
(see [ROADMAP.md](ROADMAP.md)).

## License

MIT — see [LICENSE](LICENSE).
