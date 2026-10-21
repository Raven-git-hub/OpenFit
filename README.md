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
  source priority per metric. Beside them, in a second column on a wide
  window, an **Applications** panel — its controls on top, the apps you've
  installed listed below them. **＋ Add Application** installs an app from
  files you paste or upload, and each app's row opens onto what it reads, its
  files (viewable, read-only), **Refresh**, **Update** (its files replaced
  with an edited set, its data kept), **Duplicate** (a copy of the
  app under a new id and name, to place it twice with different settings)
  and **Uninstall** (browsing and building apps from it are still to come).
- A first **app host**: the Home tab shows the apps you install (none ship with
  OpenFit) as live tiles — see [Apps on the Home tab](#apps-on-the-home-tab).

## Webhook input (push)

Beside pull plugins and apps, automations can push a reading the moment
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
  readings. So is an app's (`app:…`), which only that app writes, and
  `manual`, the old hand-entered weigh-ins, kept as history.
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
auth. These shapes are the stable contract; the `/api/activity` route is a
compatibility shim, not for apps.

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
Fitness, Sleep, Nutrition & hydration, Women's health, Roster or Other. (The pre-v2
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
(`sleep`, `workout`, `duties_rostered` or `duties_complete`).

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
your app wants. An unknown kind is a 404. An installed app reads the same list
with `ctx.sessions(kind, since, until)` (below).

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
(on the data volume; `$APPS_DIR` moves it) holding an `app.yaml` manifest and,
usually, an `app.py`:

```yaml
# /data/apps/steps/app.yaml
id: steps
name: Steps
sizes: [{w: 1, h: 1}, {w: 2, h: 2}]   # required: tile sizes in grid cells, the first the default
reads: [steps]
config:                      # the app's settings, in form order
  - {key: days, label: Days shown, type: number, default: 7}
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

`render()` runs on the server each time Home loads, with `cfg` holding the
tile's settings, its `config` fields' values (`{}` until you set any, so an
app reads each with a fallback: `cfg.get("days", 7)`). The ctx is its one data
path. `ctx.metric()` returns a metric's derived points, as `/api/metric` serves
them: a list of points with `.date`, `.value` and `.source`.
`ctx.sessions(kind, since=None, until=None)` returns a session kind's sessions,
as `/api/sessions` serves them: every source's, each with `.start`, `.end`,
`.source` and `.summary`, by source priority and then start, with `since`/`until`
(`YYYY-MM-DD`, inclusive) matched against the start's UTC date. `render()`
returns a display spec built from `number`, `line` (a sparkline, with an
optional title), `panel`, `input` (below), `timeline` (one lane or two of
time-blocks on a scrolling time axis, with a now line and a faint line at each
midnight (UTC), scrolling six hours a step: a plan beside what
happened, each lane optionally with bands shaded behind its blocks — and, named
into a sync group, a fixed cursor with the UTC time under it and a Now button,
every timeline of the group scrolling to one time), `rail` (a column of
fixed-size cards, five at a time, that restacks as its sync group's time moves
— looking ahead of it, or back)
`rails` (a timeline drawn as it is, with a rail either side), `grid` (a tile
split into sub-panels, each drawn as a tile of its own on Home's cells),
`strips` (up to three lanes of tinted spans on a horizontal axis a day across
— day and night, a duty's pieces — that, named into a sync group, scrolls with
the group's timelines both ways, by time), `empty` and `error` (for a part of
a tile that failed). An app that fails to load is logged and
left out, and one whose `render()` fails shows as an error tile, never a
broken page.

Home is a grid six square cells across that scales with the window. Every app
declares the tile sizes it can be drawn at (`sizes`, each `w` and `h` 1–6
cells; the older `default_size: {w, h}` counts as one), and one with no size
won't install. Tiles go in order, left to right, each at its size, a tile too
wide for what's left of a row starting the next. Home can have several tabs,
each with its own tiles and subtitle: they sit at the left of the tab strip,
before Data, and clicking one shows it. **Configure**, top right of Home, lets
you drag tiles into a new order, pick each one's size from those its app
offers, move a tile to another tab, and edit the subtitle over them. A tile
whose app declares `config` fields has a gear that opens its settings, a
form of those fields (a number, or text); Save redraws the tile with them at
once. While it's on, **＋ Add** on the strip makes a new tab and each tab's
gear renames or deletes it (a deleted tab's tiles move to the default tab;
nothing is uninstalled). Switching Configure off saves all of it at once,
settings included (`GET`/`PUT /api/home-layout`, one settings row). The
layout is stored as tabs, each holding instances (an app placed, with its
own size and its settings): a tile moves between tabs, it's never copied, so
each app has one tile, and the same app twice is a copy of the app under
its own id (**Duplicate**, below). A layout saved before
tabs reads as one tab, unchanged. An app installed later gets an instance at
the end of the default tab at its default size, and one uninstalled drops
out: every installed app always has at least one. Settings' **Opening page**
lists Home's tabs beside Data. `GET /api/apps` lists what's installed (with each
app's declared reads, its config fields and its files) and `POST
/api/app/<id>/render` (`{"cfg": {...}}`, a tile's settings) returns one app's
spec; the Data tab's Applications panel lists the same apps. Open
an app's row there to see what it reads, view any of its files read-only
(`GET /api/app/<id>/file?name=`), re-run its render (**Refresh**),
**Update** it, **Duplicate** it, or **Uninstall** it (`DELETE /api/apps/<id>`,
after a confirm), which deletes its folder from `/data/apps/`. Update opens
the app's files, as installed, in the Add Application popup with its id
locked: change, add or remove files, review, confirm ("Update <name>? This
replaces the app's files. Its data is kept."), and `PUT /api/apps/<id>`
(`{"files": {name: text}}`) replaces the app's folder with them in one step.
The new files are checked first, staged and loaded as the scanner would load
them, so a bad paste is refused and the app keeps running on its old ones;
the id in `app.yaml` must stay the app's own; and it's a whole-folder
replace, so a file whose slot you remove is deleted. Nothing in the database
changes: what the app wrote stays its own, and its tile keeps its place and
settings. Use Update rather than uninstalling and adding an app again.
Duplicate copies the
app's files into the Add Application popup with the next free id and name
(`steps` → `steps-2`, "Steps" → "Steps 2", then `-3`, …) for you to review
and install as a separate app, with its own tile and settings. It's a fork:
editing one app's files later doesn't change the other.

To install one, copy its folder onto the volume, or use **＋ Add
Application** on the Data tab: paste or upload its `app.yaml`, its `app.py`
and any files it reads, review, and install. With more than one tab on
Home, the review step also asks which tab it goes on (the default tab to
start with); otherwise it lands on the default tab. While Configure is on
it isn't asked: the app joins the tab on screen, to move like any tile.
That's `POST /api/apps`
(`{"files": {name: text}}`), which writes them to `/data/apps/<id>/`, the
id read from the `app.yaml` (lowercase letters, digits and dashes). The
server checks every id and file name stays inside the apps folder, refuses
an id that's already taken (use Update to change an installed app), and
otherwise doesn't check or run the app: a broken one installs, then is
logged and left out, as a hand-placed one would be. Apps run as trusted code
for now (a sandbox is Phase 4). The full contract is in
[docs/architecture/app-host.md](docs/architecture/app-host.md).

### Apps that write

An app can also add to your data: it's a source like any device, filed under
its own id, `app:<id>`. Its tile carries an **input** — a field and Save — and
its `app.py` a `handle()` beside `render()`, which Save calls:

```python
# /data/apps/weight/app.py (abridged)
def render(ctx, cfg):
    log = ctx.input("weight_kg", "log", label="Log weight", unit="kg")
    data = ctx.metric("weight_kg")
    if not data:
        return ctx.panel(ctx.empty("No weigh-ins yet"), log)
    return ctx.panel(ctx.number(data[-1].value, unit="kg"), log)

def handle(ctx, action, payload):          # payload: {"weight_kg": <what was typed>}
    if action != "log":
        raise ValueError(f"unknown action: {action!r}")
    ctx.write_metric("weight_kg", float(payload["weight_kg"]))   # today, under app:weight
```

`ctx.write_metric(metric, value, date=None)` files one reading of a canonical
metric — today's unless given a date — under the app's own source: the app
never names one, so it can only ever write its own rows, never a device's. A
reading is held to the webhook's rule (a number within the metric's plausible
range), and writing the same day again replaces it. Save posts `POST
/api/app/<id>/action` (`{"action", "payload", "cfg"}`): if `handle()` raises,
nothing it wrote is kept and the tile shows why (a `ValueError`'s message as
written); otherwise the day is re-derived at once and the tile redrawn from a
fresh `render()`. An app's data competes in the same per-metric pick as any
source's, after Garmin and Google Health unless you make it a metric's primary
(`PUT /api/source-roles/<metric>`), and stays when the app is uninstalled.

The input needn't be a number: `kind="text"` takes text, and `kind="select"`
with `options=["1", "2", ...]` (a list of strings) draws a dropdown whose Save
sends the chosen string. The host doesn't check it's one of the options — the
app's `handle()` does, as it checks a number (the Mood app, a select of 1–5
filed as the built-in `mood`, is the example).

An input can also take a **file**: `kind="file"` draws a file picker
(`accept=".ics,.csv"` hints at what to pick), and its Save posts the file —
multipart, at most 25 MB — to the same action route. `handle()` gets it as a
`FileUpload` (`.name`, `.content_type`, `.bytes`, `.text()`, and `.path` for a
library that wants a real file). OpenFit carries the file and never parses it:
the app reads whatever format it expects, and the file itself isn't kept — it's
deleted once the action is done, so only what the app writes stays. The Import
weigh-ins app, which reads past weigh-ins from a text file (`YYYY-MM-DD <kg>`
a line) and files each on its own day, is the example. A file input can carry
a companion **toggle** — `toggle={"key": "backfill", "label": "Backfill",
"confirm": "…"}` — a checkbox beside the picker whose state the app gets in the
same payload (`{"file": <FileUpload>, "backfill": true}`); with a `confirm`,
ticking it makes Save ask first, in the page's own popup.

An app can file a **session** too — an interval, as a night's sleep or a
workout is — with `ctx.write_session(kind, start, end=None, summary=None)`:
`kind` one of OpenFit's session kinds (`sleep`, `workout`, `duties_rostered`
or `duties_complete`; an app can't invent one yet), `start` and `end` each a `datetime` or ISO-8601 text, stored
in UTC to the second (`end` may be left out, and can't be before `start`),
and `summary` a dict of plain JSON, the kind's breakdown
(`{"type": "manual", "duration_minutes": 30}`). It goes under the app's own
source like its readings, writing the same kind and start again replaces it,
and it's read back alongside every device's: by any app with
`ctx.sessions(kind)`, or through `GET /api/sessions/<kind>`.
Nothing is derived from a session, so no day is re-derived for it. The
**Workout log** app (`tests/fixtures/apps/workout-log/`), which files a workout
just finished from how many minutes it lasted, is the example.

The **FRMS** app (`tests/fixtures/apps/frms/`) puts the two together. It's
the whole FRMS suite as one app — one tile, 6 wide by 4 tall (its only size),
and one `config.json` — drawn as four sub-panels, each like a tile of its own:
its roster panel, the timeline and rails below, 2×2 at the bottom left, its
circadian panel (below) 3×2 at the top left, and two placeholders, more panels
to come. A panel that fails shows its error in its own sub-panel; the others
are drawn as ever.
All its code is in its one `app.py`, by design. You upload a duty roster's
`.ics` export, and the app — not OpenFit — reads it.
Every event with real times is a work duty (an all-day one is a day off or
leave, whatever its code, and is skipped), filed as a session from its start to
its end as the calendar gives them in UTC: `duties_complete` if it had ended
when you uploaded, else `duties_rostered`, so a roster uploaded before and after
a duty holds the plan and what was flown side by side. Each duty's summary
carries its code and a best-effort label (`flight`, `reserve`, `sim`, or
`other` for a code the app doesn't know — still filed), a flight's number and
route, and the roster's description verbatim. A duty's route is where it
started and where it ended — the first leg's departure to the last leg's
arrival, read from the description's list of sectors (the title if it has
none) — so a day turn, out and back in one duty, starts and ends at base. The labels aren't written into
the code: the app is three files, `app.yaml`, `app.py` and a `config.json`
beside them that holds the duty-code rules (a code's prefix or a pattern for
each type, the first match winning, and the type for a code none match), your
home base and timezone, and a table of destinations, so adding a carrier or a
duty code is a config edit, made with Update. The committed copy's config is a
synthetic one for the tests; your own stays on your server. If the config is
missing or won't read, the tile says so over its picker and an upload is
refused, nothing filed. The sessions are all the app files: it has no
metrics of its own, so duty hours, when a report needs them, are read from the
duties, and a long day of duty is never refused. If you upload a roster after it
started, tick **Backfill** beside the picker: a duty already over is then filed
into the rostered plan as well as the flown record (its copy there marked
`capture: "backfill"`, so a plan put back after the fact is always told from one
captured live); a duty still to come is filed once either way. Because a backfilled duty replaces any plan you'd already
uploaded for it, Save asks you to confirm first; unticked, it's the plain split.
Its roster panel draws the duties back on a timeline over the picker — the
upload at the foot: the last six months or so and everything still to come, rostered on the left
(headed Roster) and flown on the right (Complete), each duty a block at its
time labelled with its code, a line at now and the flown side greyed after it,
and a faint line at each midnight (UTC); it scrolls, a week to the panel's height,
six hours a step — a mouse wheel's notch or an arrow key moves it to the next
6-hour mark (00, 06, 12, 18Z), a trackpad swipe a step or two, and a scroll
by other means (the scrollbar, a touch) settles on the nearest. A cursor stays fixed across the middle
of it, with the time under it (UTC, to the quarter hour) shown above: it follows
now until you scroll, and a Now button, at the panel's top-right, brings it back. Behind the blocks, each
lane shades its trips away from your home base (the config's) in amber: a
flight out of base to the next flight back, any sectors between inside the one
trip, labelled with the port it flew out to — only flights count, so a reserve
or a sim never starts one, and nor does a day turn. A rostered trip the loaded roster ends before it's
back runs to the roster's end; a flown one you're still away on runs to now.
The timeline is in the
FRMS sync group, so later FRMS panels will scroll with it, by time. Either side
of it are two rails of trip cards, each a whole trip out of base and back — its
flights out and home on the left, its port and the day it left on the right:
**Plan**, beside the rostered lane, every trip not over yet (the one you're on
among them), and **Report**, beside the flown, every trip home. Each rail shows
five at a time and restacks as you scroll the timeline: Plan looks ahead of the
cursor, its next trip at the top, and Report looks back, the latest trip home
at the bottom — each held full at either end of its list, the trip nearest the
cursor outlined.
Uploading again replaces a duty with the same start, but removes nothing: a
duty dropped from a later roster keeps its row for now. Both kinds file under
the Roster category: OpenFit keeps operational context like this because you do — it
doesn't decide what counts as health data. The circadian panel: from your home
base's time zone (the body clock — never adjusted to where you've flown), a
`cbt_min` (the body clock's low, 04:00 home time unless the config says) and
each destination's `lat`/`lon` in the config, the app works out day and night
where you are and at home, the body clock's low window (WOCL, 02:00–06:00 home
time), and each duty cut into pieces of day, night and WOCL against the body
clock — and draws them as three strips, a day across: where you are, blue by
day and purple by night, the body clock's curve dashed over it and its tag the
port and its local time under the cursor (`LHR 01:00 UTC+1`); home, by day
and night; and the duties, green by day, amber by night and red in the WOCL,
each duty's flight number once. It scrolls with the roster's timeline and
rails, and they with it, to the same time.

An app isn't limited to OpenFit's metrics: it can **invent its own**. Declare
what you invent, write what exists — a built-in metric is just written, while a
new one is declared in the app's `app.yaml`:

```yaml
metrics:
  - {key: caffeine_mg, label: Caffeine, unit: mg, category: "Nutrition & hydration", grain: daily, range: [0, 2000]}
```

The app's code then reads and writes it by that short key
(`ctx.write_metric("caffeine_mg", 95)`, `ctx.metric("caffeine_mg")`), and the
host stores it as `app:<id>:caffeine_mg`, so two apps' keys never collide and no
app can write another's. Each is `daily` or `instant`, files under one of the
nine categories, and is held to its `range` if it gives one; a key OpenFit
already has can't be declared. While the app is installed its metrics are part of
your data like any other: listed in the catalog (marked with the app that defines
them), shown in Your data under their category, and served by
`GET /api/metric/app:<id>:<key>`. Uninstall the app and they drop out, though
what it wrote stays until you remove it (below). The **Caffeine** app
(`tests/fixtures/apps/caffeine/`) is the example.

Removing that data is its own, explicit step, once the app is gone. An
uninstalled app's source shows in **Connected sources** with a red **Remove
all data**; after a confirm naming what goes, it deletes every reading,
session and sample the app wrote, re-derives the days they covered (each falls
to another source, or is left empty), and clears any metric's primary that
named it. The metrics themselves stay: `weight_kg` without the weight app's
readings is still `weight_kg`. That's `DELETE /api/app-sources/<id>`, which
refuses (`400`) while the app's folder is still in `/data/apps/` — even if the
app in it fails to load: a broken app isn't a gone one, so fix or uninstall it
first — and only ever reaches `app:<id>`, never a device's, a webhook's or
`manual`'s. `GET /api/app-sources` lists every app source with data, each
marked `orphaned` (its app gone from the volume) or not.

Or one metric at a time: open the app's row in Connected sources for a line
per metric it left behind (its own metrics among them, by their short key —
uninstalled, they've already left the catalog), or open a metric in Your data,
where the uninstalled app is listed under its sources; either way **Remove
data** takes only that metric's readings from that app, re-derives their days
(a metric another source wins keeps its winner) and clears that metric's
primary if it named the app — the app's other data stays. That's `DELETE
/api/app-sources/<id>/<metric>`, with the same guards.

The **Weight** app above (in full in `tests/fixtures/apps/weight/`) is the one
way to log a weigh-in by hand: install it from its two files and its tile goes
on Home. It replaced the old `/api/weights` routes. Weigh-ins logged before it
keep their source, `manual`: history, still shown and picked, never added to.

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
  apps.py                     Finds, loads and installs the apps (/data/apps)
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
metrics: a weigh-in logged in the Weight app is a `weight_kg` reading (in kg)
under its source, `app:weight` — the ones typed in before it stay under
`manual`, as history — and Garmin files its own from a compatible scale, under
`garmin`; there is no separate weights table any more. A source files only what it
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
An app can file sessions of these kinds too, under its own source
(`ctx.write_session()`, above). Two more kinds, `duties_rostered` and
`duties_complete`, hold work duties off a roster — each the duty's report to
release, in UTC — filed by a roster app (the FRMS app, above), never a device.
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
once on the first boot of an upgraded database; a webhook reading, or an
app's write, re-derives its own day as it lands. The access contract and `GET /api/activity` read
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
