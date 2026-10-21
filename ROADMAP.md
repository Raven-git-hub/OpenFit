# OpenFit Roadmap

> Architecture direction and reasoning:
> `docs/architecture/openfit-overview-and-design-pathway.md`. Read that first for
> what OpenFit *is*; this file is the phase plan.

## What OpenFit is (and isn't)

OpenFit is a **database manager + app host**, not a tracker. It ingests from your
devices, derives and maintains one dataset you own (the **OpenFit Derived
Data**), and exposes it to apps. It ships **no tracking apps** — weight logs,
planners, dashboards and insights are all external apps that read the data.

## Where this sits in the landscape

- **[wger](https://github.com/wger-project/wger)** — mature self-hosted tracker
  with a real UI; thin on wearable sync.
- **[Open Wearables](https://github.com/the-momentum/open-wearables)** —
  self-hosted normaliser for Garmin/Oura/Whoop/Fitbit, but developer
  infrastructure with no product on top.

OpenFit's niche: the normalised, owned dataset of Open Wearables *plus* a host
for apps (where wger has a UI) — but OpenFit ships none of the apps itself. An
open, self-hosted equivalent of Apple HealthKit / Google Health Connect.

## The one decision that shapes everything — resolved

One container per user, managed by a shared host (like Umbrel/YunoHost manage
self-hosted app fleets). The app needs zero changes — each person's container has
its own SQLite file, so isolation is physical, not row-level. No `user_id`, no
auth on core. The provisioning complexity moves to a separate Host service (last
phase).

## Phase 1 — Data layer hardening — DONE

A hand-rolled versioned migration runner, `activity` reshaped to one row per
`(date, source)`, and a pytest suite (routes + plugin contract + migrations, temp
DB, no network).

## Phase 2 — The engine (the OpenFit Derived Data)  ← current focus

- Tidy `metrics(date, source, metric, value, unit, synced_at)` + `sessions`
  model + a canonical vocabulary; migration `005`; fold the `weights` table in as
  a `weight_kg` metric. A new metric becomes data, not a migration.
- Per-metric **source roles** (one controlling source, others verify) with an
  OpenFit default the user can override; a stored **derived value** tagged with
  its source; full history retained. Simple — no trust-scoring engine.
- Ingestion: pull connectors (Garmin, then Oura, then the Google Health API) plus
  **webhook push** (`POST /api/webhook/<token>`) for automations and on-device
  sources.

`GET /api/activity` keeps its current flat shape as a compatibility shim through
this work; the interim UI isn't touched. The orphaned `workouts` table is removed.

## Phase 3 — The access layer + a barebones viewer

- The read **contract** apps use: metric discovery ("what do I have") and range
  queries ("metric X over range Y", with provenance).
- A minimal built-in **viewer** to see the data graphically — the first consumer
  of the contract and a way to confirm the engine. A verification view, not a
  tracker.

## Phase 4 — The app host

A sandboxed mechanism to run **external/imported apps** against the contract
(no-build, no network beyond the contract). Trackers, insight dashboards and
planners all live here as external apps — none are bundled. Charting stays within
the no-build constraint (hand-rolled SVG or a no-build library via a plain
`<script>`, no bundler).

First slice landed (not yet sandboxed — apps run as trusted code): apps
installed as folders in `/data/apps` (an `app.yaml` manifest + a
`render(ctx, cfg)` returning a display spec), drawn as tiles that flow down the
Home tab at their default size — `docs/architecture/app-host.md`. The
Library's shell followed: an Applications column on the Data tab listing the
installed apps. Then the Add Application popup: an app installed from pasted
or uploaded files through `POST /api/apps` (the host's first write route,
strict on containment, loose on correctness). Then expandable app rows: what
an app reads, its files in a read-only viewer, Refresh (its render re-run), and
Uninstall (`DELETE /api/apps/<id>`, the first destructive app action), with a
slot reserved for the writes ticker. Then Configure mode, in flow form: every
app declares its tile sizes (`sizes`, 1–6 cells either way — an app without one
won't install), Home lays the tiles on a six-column grid of square cells in the
saved order, and Configure reorders them by drag, resizes them from each app's
sizes, edits the subtitle over them, and saves it all (`/api/home-layout`, one
settings row). Then the first slice of multi-tab Home, invisible by design: the
stored layout became tabs of instances (an app placed, with its own size and
config, one app placeable more than once), the old layout upgraded on read to
one tab, and every installed app kept at least one instance (no migration).
Then the second slice: the tab strip shows the layout's tabs and switches
between them, and Configure makes, renames and deletes tabs (a deleted tab's
tiles go to the default) and moves a tile from one tab to another, all saved
at once (client only, no route change). Then the third: per-instance config.
`/api/apps` carries each app's config fields, the render route takes the
instance's `cfg` (`POST /api/app/<id>/render`), and in Configure a tile's
gear opens a form of those fields, saved with the layout, the tile
re-rendered on Save. Then the fourth: app-level Duplicate. An app's Data row
copies its files into the Add popup as a new app, with the next free id and
name (`steps-2`, "Steps 2"), to review and install. That's how the same app is
placed twice, so tiles stay keyed by app (client only, no route change). Then
the fifth: deploy-to-tab. With more than one tab, the Add popup's Review asks
which tab the app goes on (the default tab picked to start with), and a
different choice moves the new app's instance there and saves the layout
(client only, no route change); while Configure is on it isn't asked, and
the app is moved by hand like any tile. Then apps as sources: an app's
`handle(ctx, action, payload)`, run from its tile's input (a field and Save)
through `POST /api/app/<id>/action`, writes under its own source, `app:<id>`,
fixed by the host so it can only ever write its own rows; the Weight app is
the first writer, and `/api/weights` is retired (its `manual` weigh-ins kept as
history). Then an uninstalled app's data removed on request, and then
app-defined metrics: an app declares the metrics it invents in its manifest
(daily or instant, a unit, a category, an optional range) and writes them by
short key, stored as `app:<id>:<key>` and listed beside the built-in metrics
(the Caffeine app the example; purging an uninstalled app's metric is next on
that path). Then the first input primitive beyond a typed field: a dropdown
(`kind="select"`, plain-string options, the chosen string sent to `handle()`,
which owns checking it; the Mood app the example), then file upload
(`kind="file"`: the file posted multipart, at most 25 MB, handed to `handle()`
as a `FileUpload` and deleted after the action — OpenFit never parses it, the
app does; the Import weigh-ins app the example), with date/time the next brick
(`docs/architecture/input-primitives.md`). Then session write-back: an app's
`handle()` files a session of an existing kind (`sleep`, `workout`) with
`ctx.write_session()`, under its own source, read back through
`/api/sessions/<kind>` beside every device's (the Workout log app the
example). Then the roster path: two session kinds in core,
`duties_rostered` and `duties_complete`, under a new Roster category, and the
Roster app, which reads a roster's `.ics` itself and files every timed event
as a duty — complete once it has ended, else rostered — with its own daily
duty hours beside them (upsert-only: replacing an app's own sessions over a
date range, so a re-planned roster's dropped duties go, is a later host brick,
as is matching each rostered duty to its flown one). Then session read-back:
`ctx.sessions(kind, since, until)`, in `render()` and `handle()` alike, every
source's sessions of a kind as `/api/sessions/<kind>` serves them (one shared
query, so the two can't drift) — the data the roster timeline and sleep
analysis build on. Then App Update: an app row's Update opens the Add popup
on the app's files, its id locked, and after a confirm `PUT /api/apps/<id>`
replaces them in one step, the new files staged and loaded before an atomic
swap, so a bad paste leaves the app running and its data is never touched —
the safe form of uninstalling and adding it again. Then the Roster app became
FRMS (`app:frms`): one 6×4 app with one config, to be drawn as four sub-panels
— the roster timeline and rails first, a circadian panel next — its duty-hour
metrics dropped (duty hours are read from the sessions). Then the grid spec:
one tile split into sub-panels, each drawn as a tile of its own on the Home
grid's cells — FRMS its first user, the roster panel 2×2 bottom-left beside
three placeholders. Next: the rest of the
Library (the writes ticker, now apps write back; alerts;
Browse / Build / Create are still placeholders), and the grid pass (free cell
placement, a second layout mode on the same sizes).

## Phase 5 — OpenFit Host (per-user provisioning)

A separate project: reverse proxy with dynamic routing, a control-plane service
that spins up a container per user via the Docker API, per-container backups, a
minimal admin view. Sequenced after the platform itself is solid.

## Community-readiness (ongoing)

`CONTRIBUTING.md` + issue templates, CI (lint + the test suite on every PR),
more plugins as contributions (Oura the natural next), versioned releases once
past prototype.

## Immediate next steps (Phase 2 slices — each a branch/PR)

1. Metrics model — migration `005` + plugin contract + `/api/activity` compat.
2. Fold weight into metrics.
3. Sessions (sleep stages, workouts).
4. Source roles + derived value + history.
5. Webhook ingestion.
6. The access contract — paired design first.
