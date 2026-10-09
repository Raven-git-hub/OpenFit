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
it. The web UI in this repo is a legacy/interim viewer — it will be replaced and
is not the product.

## What's here today

- A versioned SQLite data store with a hand-rolled migration runner.
- **Garmin Connect** plugin — steps, resting HR, sleep (daily total plus a
  per-night session with stages), and workouts (a session per activity).
- **Google Health** plugin — the same, from Pixel Watch / Fitbit via Google's
  cloud Health API (not the legacy Fitbit Web API, retired September 2026).
- Scheduled background sync plus a manual sync, with device connections managed
  in-app and credentials encrypted at rest.
- A **webhook** any automation can push a reading to, guarded by a
  regeneratable token and per-metric sanity ranges.
- The **access contract** — a small, stable read API apps build on: what data
  there is, one metric over a date range (with which source each value came
  from), and sleep/workout sessions.
- An interim web viewer (being replaced).

## Webhook input (push)

Beside pull plugins and manual entry, automations can push a reading the moment
it happens — Home Assistant on each smart-scale weigh-in, a Shortcut, a script.
One URL for every metric:

```bash
curl -X POST http://<your-server-ip>:8080/api/webhook/<token> \
  -H 'Content-Type: application/json' \
  -d '{"metric": "weight_kg", "value": 82.4, "source": "eufy"}'
```

- `metric` is a canonical key (`steps`, `resting_hr_bpm`, `sleep_minutes`,
  `weight_kg`) and `value` a number in its unit (`app/metrics.py`).
- `date` (`YYYY-MM-DD`) is optional and defaults to today; `source` is optional
  and defaults to `webhook`. Name your device (`"eufy"`) and it takes part in
  source roles like any other source — make it a metric's primary and it wins.
  A pull plugin's id (`garmin`, `google_health`) is refused: its syncs own those
  readings.
- A value outside the metric's plausible range is refused (400), so a misfired
  automation can't corrupt your data: steps 0–200000, resting HR 20–250 bpm,
  sleep 0–1440 min, weight 20–400 kg.
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

Apps read OpenFit through three read-only endpoints. They serve the **derived**
profile — the one value OpenFit picked per metric per day, each tagged with the
source it came from — never the raw per-source readings, so an app gets one
trusted answer and never has to reconcile devices itself. Same trusted-network
model as the rest of the API: no auth. These shapes are the stable contract;
the `/api/activity` and `/api/weights` routes are interim-UI shims, not for apps.

**Discovery — `GET /api/catalog`.** Every metric and session kind that has data:

```json
{
  "metrics": [
    {"metric": "steps", "unit": "count", "sources": ["garmin", "google_health"],
     "first": "2026-10-07", "last": "2026-10-08", "count": 2,
     "last_value": 10450.0, "last_date": "2026-10-08"}
  ],
  "sessions": [
    {"kind": "sleep", "count": 1,
     "first": "2026-10-07T22:41:00Z", "last": "2026-10-07T22:41:00Z"}
  ]
}
```

`sources` are the sources that have supplied the picked value on any day, in the
order the pick tries them; `first`/`last`/`count` are the metric's derived days,
and `last_value` the value on `last_date`. A session kind's `first`/`last` are
its earliest and latest start. An empty install returns two empty lists.

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

Oldest start first; times are ISO 8601 UTC, and `summary` holds the kind's
fields that the device reported (`{}` if none). Sessions aren't picked between
sources yet, so a night both devices tracked appears once per device. An unknown
kind is a 404.

`from` and `to` are optional and inclusive; leave one out for an open end. For
sessions they match the start's UTC date. A malformed date, or `from` after
`to`, is a 400.

## Quick start

```bash
git clone https://github.com/Raven-git-hub/OpenFit.git
cd OpenFit
cp .env.example .env
docker compose up -d --build
```

Visit `http://<your-server-ip>:8080`. It runs fine with zero plugins configured.

### Connecting a device

Devices are added in the app, not a config file: **Configuration → Connected
sources → Add a device**. Pick the device, fill in what it asks for, hit Add.
Credentials are encrypted before they're stored, and **Remove** deletes them
along with any cached session token. An install that predates this flow keeps
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
Client ID of type **Desktop app**. Then, under **Add a device → Google Health**:
paste the client ID and secret, press **Get authorization link**, approve on
Google (it redirects to `http://127.0.0.1:9109/`, which OpenFit deliberately does
not serve — the page fails to load, that's expected), and paste the address (or
the code) back. The refresh token is stored encrypted and the sync uses it from
then on.

## Architecture

```
app/
  main.py                     Flask API + scheduler, loads plugins from plugins/
  templates/index.html        Interim web viewer (vanilla JS) — being replaced
  plugins/
    base.py                   SyncPlugin interface every pull plugin implements
    __init__.py               PLUGINS registry — one line per installed plugin
    garmin/plugin.py          Garmin Connect
    google_health/plugin.py   Google Health API (Pixel Watch / Fitbit)
  metrics.py                  Canonical metric vocabulary (key -> unit)
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
vocabulary (`steps`, `resting_hr_bpm`, `sleep_minutes`, `weight_kg`, ... in
`app/metrics.py`) — so a new metric needs no migration. Weight is one of those
metrics: hand-entered weigh-ins are `weight_kg` readings (in kg) under the source
`manual`, and there is no separate weights table any more. Beside `metrics`, a
`sessions` table holds interval records, each with a start/end and a summary,
filed under a session kind from the same vocabulary. A `sleep` session is one
night (or nap) from each device, with its stage breakdown in minutes
(`asleep_minutes`, plus `light_minutes`/`deep_minutes`/`rem_minutes`/
`awake_minutes` where the device reports them); sleep is dual-track: the daily
`sleep_minutes` total is filed exactly as before, and the session is written
beside it. A `workout` session is one activity, with no daily metric beside it:
its `type` (the device's activity type, lowercased), `duration_minutes`,
`distance_m`, `avg_hr_bpm` and `calories_kcal`, whichever the device reports.
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
at `/data/.secret_key` — back it up with the database.

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
(`SLEEP`) beside the daily metric, a workout (`WORKOUT`) on its own. Register
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
