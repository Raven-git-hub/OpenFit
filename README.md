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

Fuck the cloud, fuck SaaS — every household can run its own server, and
health data is exactly the kind of thing that shouldn't live somewhere
else by default. Most fitness trackers lock you into one app tied to one
device brand; most device brands lock your data into their own cloud.

Two projects get close to solving this and stop short in opposite
directions:

- **[wger](https://github.com/wger-project/wger)** is a mature,
  self-hosted workout/nutrition/weight tracker with a real UI — but
  pulling in data from actual wearables has never been a first-class
  feature.
- **[Open Wearables](https://github.com/the-momentum/open-wearables)** is
  a self-hosted platform that normalizes Garmin/Oura/Whoop/Fitbit data
  behind one API — but it's developer infrastructure, no tracker UI, no
  training program, nothing to actually open and use.

OpenFit is the overlap: a tracker with its own UI, built on a plugin
architecture for device data, so switching devices doesn't mean losing
your history.

## What's here today

- **Weight log** with a chart against a goal
- **Training program** with per-session checkbox tracking
- **Garmin Connect plugin** — steps, resting heart rate, sleep
- **Google Health plugin** — same three metrics, sourced from Pixel Watch
  / Fitbit devices (built against Google's newer Health API rather than
  the legacy Fitbit API, which Google is retiring in September 2026)
- Both plugins auto-sync on a schedule, plus a manual sync button each

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

### Garmin Connect plugin

1. Fill in `GARMIN_EMAIL` / `GARMIN_PASSWORD` in `.env`.
2. If your account has MFA/2FA enabled, run the one-time interactive
   login first so the background sync never has to handle the prompt:
```bash
   docker compose run --rm tracker python3 first_login.py
```

### Google Health plugin (Pixel Watch / Fitbit)

Uses standard Google OAuth rather than a password. One-time setup:

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
```

Data lives in SQLite, on a persistent Docker volume, in three tables:
`weights`, `workouts`, and `activity`. Every plugin writes into the same
`activity` table, keyed by date, so the frontend doesn't care which
plugin a given day's steps came from.

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
    required_env = ["MY_SERVICE_TOKEN"]

    def sync(self, conn, days: int) -> int:
        # fetch data from your source, upsert into `activity`
        # using INSERT ... ON CONFLICT(date) DO UPDATE with COALESCE
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
