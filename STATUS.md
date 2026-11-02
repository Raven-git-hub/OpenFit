# OpenFit status

_Where things stand, as of 2 Nov 2026 (after C2, app records).
The phase plan this replaces is archived at
[docs/history/roadmap.md](docs/history/roadmap.md)._

## What OpenFit is

OpenFit is a **database manager + app host**, not a tracker. It ingests from
your devices, derives and maintains one dataset you own (the **OpenFit Derived
Data**), and exposes it to apps. It ships **no tracking apps**: weight logs,
planners, dashboards and insights are all apps that read the data. It runs on
your own hardware, one container per person, and nothing leaves your network
except each device plugin's call to its own API. The why:
[openfit-overview-and-design-pathway.md](docs/architecture/openfit-overview-and-design-pathway.md).

## What's built

**Data layer.** Garmin and Google Health pull plugins on a per-source
schedule, and a webhook (`POST /api/webhook/<token>`) for anything that
pushes. Readings land in three grains: daily and instant `metrics`, interval
`sessions` (sleep, workouts, duties) and intraday `samples`, all keyed by the
canonical vocabulary in `app/metrics.py`. Where sources overlap, a per-metric
source role (or the default precedence) picks one stored derived value a day,
tagged with its source; the raw rows stay. Beside them, each app's own
records (C2): a private JSON store, `app_records`, in collections the app
names. Schema changes are numbered, additive migrations (`app/migrations/`,
at `011`); device credentials are encrypted at rest. See
[data-consolidation-model.md](docs/architecture/data-consolidation-model.md),
[metric-schema-v2.md](docs/architecture/metric-schema-v2.md) and
[apps-as-source.md](docs/architecture/apps-as-source.md).

**App host.** An app is a folder with an `app.yaml` and an optional
`app.py`: `render(ctx, cfg)` returns a display spec the page draws as a tile,
and `handle(ctx, action, payload)` writes under the app's own source. The
contract is versioned by the **app level**, `APP_LEVEL` in `app/apps.py`, now
5; the list above it says what each level added, and an app declares the
level it needs with `requires:`. Anything an app keeps that isn't a reading
or a session - a report, a log, a saved list - is a record, in its own
private JSON store (C2, level 5): read in `render()` and `handle()`, written
in `handle()` inside the action's transaction, at most 256 KB a record and
50 MB an app, kept at uninstall and shown, and removed, under Your data. The spec types cover numbers, lines,
panels, inputs (field, dropdown, file), timelines and rails, grids, rows and
columns, donuts and bars, images and strips, and a tile's views (switch,
toggle, pager, choose) flip between versions of a tile without a round trip.
Browse installs apps from the shelf, and an installed library app updates
itself when its library publishes a new version - one that needs a newer
OpenFit waits, then installs itself once OpenFit is updated (F1). See
[app-host.md](docs/architecture/app-host.md),
[input-primitives.md](docs/architecture/input-primitives.md) and
[app-authoring-lessons.md](docs/architecture/app-authoring-lessons.md).

**User system.** Home is tabs of tiles, arranged in Configure; the Data tab
is the console (connected sources, what the dataset holds and which source
wins, and the installed applications). One account per container: locked
until a password is set through a one-time setup link from the Admin, with
the session cookie named by port so two OpenFits on one server don't sign
each other out. The account timezone decides which day is "today"
everywhere (`app/clock.py`). See
[frontend-redesign-and-widget-model.md](docs/architecture/frontend-redesign-and-widget-model.md),
[auth.md](docs/architecture/auth.md) and the Home tab in
[app-host.md](docs/architecture/app-host.md#the-home-tab).

**Admin.** One per server, on port 8090. It updates OpenFit for everyone
with a backup first, a health check and automatic rollback, plus Undo; it
updates itself through a helper container, and can revert that. It adds,
starts, stops, removes and restores users, and makes their setup links. Its
backups can be downloaded without passwords, deleted one at a time, or
restored as a new user. It publishes the app shelf every user mounts
read-only, from the official library and from organisation repos, each
cloned over SSH with its own read-only deploy key. The Admin Portal shows
system information, versions, the schedule, repos and the activity log; a
weekly schedule either checks for updates or checks and installs them. A
slow or full disk is obvious (A7): a step that failed for a full disk says
"The server's disk is full", the build's steps reach the update's log as
they happen and a build that stalls is stopped (15 minutes in all, 5 with
nothing new), and System information, the update popup and a scheduled
run's log warn when the disk is nearly full or very busy. How busy is the
I/O wait read from `/proc/stat` inside the VM: the VM's own view of its
disk, not the Proxmox host's. See
[admin.md](docs/architecture/admin.md).

**Repos.** OpenFit (public): the core, the Admin and an empty official
library (`library/repo.yaml` alone). `openfit-aviation` (private): FRMS,
published to the shelf as `openfitavi.frms`, with its own tests.

## How work happens now

- Testing finds something, and it becomes a slice.
- One branch, one concern.
- Each slice starts as a decision-pinned brief, is built by Claude Code, then
  gets an independent review with mutation checks before it's merged.
- Deploys go through the Admin: Check now, then Update OpenFit, then Update
  the Admin when it's offered. App-only changes go out with Update apps (the
  official library) or a repo's own update (an organisation repo).

## Standing rules

- **The app level.** Every app-facing addition to core raises `APP_LEVEL` in
  `app/apps.py` and adds a line to the list above it; an app that uses the
  addition declares `requires:` that level.
- **Deploy order for app-facing changes.** Merge OpenFit, run Update OpenFit,
  then push the app change. An app pushed too early waits safely ("waiting
  for a newer OpenFit"), its installed copy unchanged, then installs by
  itself within a minute of the next Update OpenFit.
- **Privacy-first, no telemetry.** A user container reaches only its own
  device sources; the Admin is the only part that talks to GitHub.
- **One account per container**, and still no `user_id`: more people means
  more containers, managed by the Admin.

## Next

- **C3, report files**: an app produces HTML reports, always served as a
  download, never shown inside OpenFit, PDF by the browser's Print; privacy
  inside a downloaded report is the app author's. With C2 it resolves core
  item 2 (FRMS's report store), more generally than FRMS asked.
- **C4, per-app settings**: an app's own app-wide settings, declared in its
  manifest and set from a settings form, kept in the reserved `_settings`
  records collection C2 left for it.
- Timers and scheduled app jobs come after these: apps act on demand first.

## Parked

- **FRMS personal settings**: handled in the FRMS thread (FRMS still reads
  its home base and timezone from its own `config.json`).
- **B2, `replaces`' data move**: nothing to move, since FRMS was reinstalled
  fresh from Browse; `replaces` stays parsed and refused.
- **The `.local` names**: deferred with the proxy and the mDNS announcer;
  users are on `IP:port`, which works for now.
- **CI**: not until a second developer arrives, or edits start skipping the
  brief-and-review process.
- **The Admin on phones**: the portal is built for a desktop browser, and
  scrolls sideways under 1180px.
- **The layover label under the lane tag** (core item 5): polish.
- **The one-off `docker image prune -f` and the server's disk size**: Andy's,
  on the server, not a code change.
- **Acting on a slow or full disk** (out of A7, which only warns): putting a
  scheduled run off while the disk is busy, and freeing space by itself.

## Where things live

- [CLAUDE.md](CLAUDE.md): the detailed guide to the code, for Claude Code.
  [README.md](README.md): setup and use.
- [docs/history/roadmap.md](docs/history/roadmap.md): the archived phase plan.
- [distribution.md](docs/architecture/distribution.md): the full design
  record for the repo → Admin → user system setup, and its build order.
- The architecture docs, `docs/architecture/`, a line each:

| Doc | What it holds |
| --- | --- |
| [openfit-overview-and-design-pathway.md](docs/architecture/openfit-overview-and-design-pathway.md) | What OpenFit is and why; the original design pathway |
| [data-consolidation-model.md](docs/architecture/data-consolidation-model.md) | How many sources become one dataset: grains, sync, derive-and-pick, the access contract |
| [metric-schema-v2.md](docs/architecture/metric-schema-v2.md) | The canonical vocabulary: each metric's unit, category, grain and range |
| [data-points-and-metric-schema.md](docs/architecture/data-points-and-metric-schema.md) | The earlier data-point catalogue, superseded for the vocabulary by v2 |
| [apps-as-source.md](docs/architecture/apps-as-source.md) | Apps writing data: the write path, app-defined metrics, the data lifecycle |
| [app-host.md](docs/architecture/app-host.md) | The app contract: manifest, ctx, spec vocabulary, writing, installing, Home |
| [app-authoring-lessons.md](docs/architecture/app-authoring-lessons.md) | The why behind the contract's rules, and the gotchas |
| [input-primitives.md](docs/architecture/input-primitives.md) | How a tile takes input (field, dropdown, file) and what's sketched |
| [frontend-redesign-and-widget-model.md](docs/architecture/frontend-redesign-and-widget-model.md) | The page: the tab strip, the Data console, where apps fit |
| [auth.md](docs/architecture/auth.md) | Signing in: one account, setup links, the guard, the cookie |
| [distribution.md](docs/architecture/distribution.md) | The distribution design record and its build order (above) |
| [admin.md](docs/architecture/admin.md) | The Admin: users, updates, backups and restore, the shelf, repos, the portal, the schedule |
