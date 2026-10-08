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
- **Garmin Connect** plugin — steps, resting HR, sleep.
- **Google Health** plugin — the same, from Pixel Watch / Fitbit via Google's
  cloud Health API (not the legacy Fitbit Web API, retired September 2026).
- Scheduled background sync plus a manual sync, with device connections managed
  in-app and credentials encrypted at rest.
- An interim web viewer (being replaced).

## Planned: webhook input (push)

Beside pull plugins and manual entry, a webhook endpoint external automations can
push to: `POST /api/webhook/<token>` with `{"metric": "weight", "value": 91.5}`.
The token in the URL *is* the auth (regeneratable), with sanity-range checks. One
URL to drop into Home Assistant, a Shortcut or a script — and the path by which
on-device sources (e.g. Apple HealthKit) will reach OpenFit. See
[ROADMAP.md](ROADMAP.md).

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
`manual`, and there is no separate weights table any more. Today the per-day
value comes from a fixed source precedence; `sessions` and per-metric source
roles are next — see [docs/architecture](docs/architecture/). Device credentials
entered in the UI are stored as a Fernet-encrypted JSON blob (one row per plugin)
and never returned over the API; the key comes from `$OPENFIT_SECRET_KEY` or is
generated at `/data/.secret_key` — back it up with the database.

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
`app/metrics.py`, with the value already in that metric's unit. Register it with
one line in `app/plugins/__init__.py` — no changes to `main.py`, the API, or the
UI. Oura's clean public API makes it the natural next plugin.

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
