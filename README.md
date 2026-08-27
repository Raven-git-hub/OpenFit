# OpenFit

A self-hosted, plugin-based fitness tracker. Log weight, follow a training
program, and pull in activity data — steps, heart rate, sleep — from
whatever devices you actually own, via small isolated plugins. Runs in a
single Docker container on your own hardware. Nothing leaves your network
except each plugin's own calls to fetch your own data from its source
(e.g. Garmin's servers).

Built by, and currently used by, one person on their own home server —
see [ROADMAP.md](ROADMAP.md) for where it's headed.

## Why this exists

Most fitness trackers lock you into one app tied to one
device brand; most device brands lock your data into their own cloud.

Health data should be owned by you, and it should be your choice where it is stored and what you do with it.

OpenFit is a tracker with its own UI, built on a plugin
architecture for device data, so switching devices doesn't mean losing
your history.
It also allows you to develop your own plugins which allows you to interpret your own data in a way that is most useful to you.

## What's here today

- **Weight log** with a chart against a goal
- **Training program** with per-session checkbox tracking
- **Garmin Connect plugin** — steps, resting heart rate, sleep
- **Google Health plugin** — same three metrics, sourced from Pixel Watch
  / Fitbit devices (built against Google's newer Health API rather than
  the legacy Fitbit API, which Google is retiring in September 2026)
- Both plugins auto-sync on a schedule, plus a manual sync button each

## Planned: webhook input (push, not pull)

In addition to pull-based plugins (polling a device's API on a schedule)
and manual entry, a third input path is planned: a webhook endpoint that
external automations can push to the moment an event happens - no polling
delay. Motivating use case: a Home Assistant automation firing on every
Eufy smart-scale weigh-in, hitting OpenFit directly instead of waiting for
a scheduled sync.

Design shape:
- A single generic endpoint (`/api/webhook/<token>`) rather than one per
  metric, taking `{"metric": "weight", "value": 91.5}` in the body - one
  URL to copy into any automation tool (Home Assistant, IFTTT, Shortcuts,
  a script), regardless of what metric it's pushing.
- The token in the URL *is* the auth - copy it once from Settings into an
  automation, no separate auth header needed.
- Regeneratable token if it ever leaks, and sanity-range checks on
  incoming values so a misfired automation can't silently corrupt a
  chart with a garbage reading.

See [ROADMAP.md](ROADMAP.md) for where this sits relative to other work.

## Quick start

```bash
git clone https://github.com/Raven-git-hub/OpenFit.git
cd OpenFit
cp .env.example .env
docker compose up -d --build
```

Visit `http://<your-server-ip>:8080`. It runs fine with zero plugins
configured — you'll just be logging weight and workouts manually until
you set one up.

### Connecting a device

Devices are added in the app, not in a config file: **Configuration →
Connected sources → Add a device**. Pick the device, fill in what it
asks for, and hit Add. Credentials are encrypted before they're stored
(see [Credential storage](#credential-storage)), and **Remove** deletes
them along with any cached session token.

An install that predates this flow keeps working: the `.env` variables
below are still read as a fallback until you re-add the device through
the UI.

### Garmin Connect plugin

Add it under **Configuration → Connected sources**, entering the email
and password for your Garmin Connect account.

If your account has MFA/2FA enabled, run the one-time interactive login
first so the background sync never has to handle the prompt:

```bash
docker compose run --rm tracker python3 first_login.py
```

### Google Health plugin (Pixel Watch / Fitbit)

Uses standard Google OAuth rather than a password, so it isn't in the
"Add a device" list yet — that flow is a separate piece of work. For now
it is still set up from `.env` plus a one-time authorization:

1. Create a project in [Google Cloud Console](https://console.cloud.google.com/)
   and enable the **Google Health API**.
2. Configure the OAuth consent screen as "External", **Testing** mode,
   with your own account added as a test user — no Google review needed
   for personal use.
3. Add scopes: `googlehealth.activity_and_fitness.readonly`,
   `googlehealth.sleep.readonly`, `googlehealth.health_metrics_and_measurements.readonly`.
4. Create an OAuth Client ID of type **Desktop app**, and put the ID/secret
   into `.env` as `GOOGLE_HEALTH_CLIENT_ID` / `GOOGLE_HEALTH_CLIENT_SECRET`.
5. Run the one-time authorization:
```bash
   docker compose run --rm -p 8765:8765 tracker python3 plugins/google_health/authorize.py
```
   Open the printed URL, approve access, and a refresh token is saved to
   the data volume — no further interaction needed after that.

## Architecture

```
app/
  main.py                     Flask API + scheduler, loads plugins from plugins/
  templates/index.html        Frontend (vanilla JS, fetches the API)
  plugins/
    base.py                   SyncPlugin interface every plugin implements
    __init__.py                PLUGINS registry - one line per installed plugin
    garmin/plugin.py           Garmin Connect
    google_health/plugin.py    Google Health API (Pixel Watch / Fitbit)
    google_health/authorize.py one-time OAuth login for the above
  crypto.py                   Encrypt/decrypt for stored device credentials
  migrations/                 Versioned .sql schema migrations + runner
tests/                        pytest suite (temp DB, no network)
```

Data lives in SQLite, on a persistent Docker volume, in tables
`weights`, `workouts`, `activity`, `settings` and `accounts`. Every plugin writes into the same
`activity` table, which is keyed by `(date, source)` — one row per day
*per source*, so Garmin and Google Health never overwrite each other.
`GET /api/activity` merges those rows back into one flat row per date
before returning them, so the frontend doesn't care which plugin a given
day's steps came from. When two sources report the same metric for the
same day, the fixed precedence is `garmin` > `google_health`; pass
`?by_source=1` to get the raw per-source rows instead.

### Credential storage

Device credentials entered in the UI are stored in the `accounts` table
as a Fernet-encrypted JSON blob, one row per plugin — so a copy of
`tracker.db` (a backup, a snapshot, a stray volume mount) is not a copy
of your Garmin password. They are never read back out over the API: the
UI can see *that* a device is connected, not what it was connected with.

The key comes from `$OPENFIT_SECRET_KEY` if you set one, otherwise it is
generated on first use and kept at `/data/.secret_key` with mode `0600`,
alongside the database on the persistent volume. Back it up with the
database — lose the key and the stored credentials can't be decrypted,
at which point re-adding the device in the UI is the fix.

What a plugin needs is declared by the plugin itself, as a manifest on
its `SyncPlugin` subclass:

```python
add_flow = "credentials"          # or "oauth"
fields = [
    {"key": "email",    "label": "Email",    "type": "text",     "required": True},
    {"key": "password", "label": "Password", "type": "password", "required": True},
]
```

The form, its validation and the storage all follow from that list — the
UI has no per-device code in it.

### Schema migrations

The schema is versioned by numbered `.sql` files in `app/migrations/`,
applied in order and recorded in a `schema_migrations` table. Each one
runs exactly once, inside a transaction. Migrations run automatically at
startup, and can be run by hand — do this against a *copy* of the
database first when a migration touches real data:

```bash
# 1. back up the DB from the volume (stop first so nothing is mid-write)
docker compose stop tracker
docker compose run --rm -v "$PWD":/backup tracker \
  cp /data/tracker.db /backup/tracker-backup.db
docker compose start tracker

# 2. dry-run the migration against the copy before deploying
cd app && python -m migrations ../tracker-backup.db
```

To add a migration, drop a new `NNN_name.sql` in `app/migrations/`. There
is deliberately no Alembic/SQLAlchemy here — this is ~100 lines of
stdlib `sqlite3`, which is the right weight for a single-container app.

### Tests

```bash
pip install -r requirements-dev.txt
pytest
```

The suite runs against a temporary SQLite file and never makes a network
call — the plugin contract is tested with a fake in-process plugin, not
against Garmin or Google.

**Multi-user approach:** not row-level multi-tenancy. If this ever runs
for more than one person, the plan is one isolated container per user
behind a shared "Host" layer, not `user_id` columns and auth bolted onto
this app. See [ROADMAP.md](ROADMAP.md) for the reasoning.

## Writing a plugin

A plugin is a class implementing `SyncPlugin` (see `app/plugins/base.py`):

```python
from ..base import SyncPlugin

class MyServicePlugin(SyncPlugin):
    id = "my_service"
    name = "My Service"
    add_flow = "credentials"
    fields = [
        {"key": "token", "label": "API token", "type": "password", "required": True},
    ]

    def sync(self, conn, days: int) -> int:
        # self.get_credentials(conn) returns what the user typed in the UI
        # fetch data from your source, upsert into `activity` with
        # source = your plugin id, using
        # INSERT ... ON CONFLICT(date, source) DO UPDATE with COALESCE
        # (see plugins/garmin/plugin.py for the exact pattern)
        ...
        return days_written
```

Then register it in `app/plugins/__init__.py`. That's the entire
integration surface — no changes needed to `main.py`, the API, or the
frontend. The plugin shows up in `/api/plugins`, gets a sync button in
the UI, and joins the scheduled background sync automatically.

Good candidates for a next plugin: Oura (clean public API), Whoop, manual
CSV import.

## Roadmap

Full project map — phases, the insights/dashboards work in progress, and
the reasoning behind the multi-user approach — is in
[ROADMAP.md](ROADMAP.md).

## Privacy & security

- Everything lives in the SQLite volume on your machine. Nothing leaves
  your network except each plugin's own calls to its own data source.
- Garmin: password used only for the very first login, then a cached
  session token is reused.
- Google Health: no password ever touches this app — a standard OAuth
  refresh token, revocable anytime from
  [your Google account permissions](https://myaccount.google.com/permissions).
- No login on the tracker's own web UI. Fine on a trusted home network;
  don't port-forward this to the public internet without adding auth in
  front of it (reverse proxy with basic auth, or a VPN).

## Contributing

Early-stage, single-maintainer project — issues and PRs welcome,
especially new plugins. No CI or contribution template yet; that's on the
roadmap.

## License

MIT — see [LICENSE](LICENSE).
