# Frontend: the data console, and where widgets go

OpenFit's built-in page (`app/templates/index.html`) is a **data console**, not a
tracker. It shows what the engine holds, how continuous each part of it is, and
which source wins each metric. It also manages the devices data comes from. It is
the first consumer of the access contract (step 7 of the design pathway in
`openfit-overview-and-design-pathway.md`). Trackers, dashboards and planners are
external apps (the widget model, below), and the console doesn't grow into one.

Constraints: vanilla JS, no build step, one self-contained template. No external
calls and no web fonts: the font stack names Archivo / IBM Plex first but only
uses them if installed locally, and otherwise falls back to system fonts. Every
colour, radius and font comes from the `CUSTOMISE OPENFIT HERE` token block at
the top of the file.

## One page

The old Home / Plan / Configuration tabs are gone. The page is one column under
the top bar. The bar keeps the logo, today's date, the last-sync indicator and the
Auto / Light / Dark theme toggle (per browser, in `localStorage` as `of-theme`).

1. **Connected sources.** One compact row per connected device: name, status
   pill, **Sync now**, **Remove**. Below them, **Add source** opens a menu of
   the connectors that aren't connected yet. Picking one opens the existing
   add flow, built from the connector's manifest: the credentials modal, or the
   two-step OAuth paste-back modal.
2. **Your data.** One row per catalog entry (below). Click a row to expand it.
3. **Settings.** A plain disclosure holding the theme control and the note about
   the token block.

## The rows: `GET /api/catalog?include_empty=1`

Each catalog entry is one row: metrics first, then series, then sessions, in
the order the catalog returns them (vocabulary order). Each row has fixed,
aligned columns:

`[chevron] Name · Reading · Cadence ··· [Day] [Wk] [Mth]`

- **Name.** The catalog returns raw keys. The page maps them to display names
  (`steps` → Steps, `resting_hr_bpm` → Resting HR, `spo2_pct` → SpO₂ (daily),
  `spo2` → SpO₂, …). An unmapped key is title-cased with its unit suffix removed,
  so a new vocabulary key still shows up. This map could move into
  `app/metrics.py` later, beside the keys. Hovering the name shows the raw key
  and unit.
- **Reading.**
  - Daily: `last_value` in its unit. Whole numbers have no decimals and get
    thousands separators. `count` has no suffix, `%` is appended (`94%`), and
    any other unit follows a space (`80 bpm`, `2,343 kcal`).
  - Series: `{count} pts`.
  - Session: the latest session's duration as `Xh Ym` (sleep reads
    `asleep_minutes`, workout reads `duration_minutes`). The latest session
    comes from `GET /api/sessions/<kind>?from=<date of last start>`, a one-day
    read, not the whole history. If that isn't available, the reading falls
    back to `{count} sessions`.
  - Nothing reported: `—`.
- **Cadence.** The catalog's `cadence`, capitalised: Daily, Reading, Session,
  Manual.
- **Grouping.** Rows with `count > 0` come first. A **Not yet reporting**
  divider follows, then the empty slots, dimmed (they still expand).

## Continuity: `GET /api/coverage`

Coverage lists, for every key, the `YYYY-MM-DD` dates that hold any data from
any source. All of the continuity UI is **source-agnostic**: one colour (the
`--coverage` token, the accent by default), with opacity as the only signal.

**Rhombuses (Day / Wk / Mth).** These are skewed like the old tab strip. Every
row is measured over the same window, so rows can be compared directly: from
the earliest date in any coverage list to today, or the last 90 days if there is
no data at all. For a row's dates inside that window:

- daily = distinct days with data ÷ days in the window
- weekly = 7-day buckets (counted from the window's start) holding ≥1 date ÷
  all such buckets
- monthly = calendar months holding ≥1 date ÷ months the window spans

Fill opacity is `clamp((coverage − 0.05) / 0.90, 0, 1)`: 5% or less reads as an
outline only, and 95% or more as solid. A row with no dates at all gets an
outline in the neutral `--coverage-edge` token. Hovering a rhombus shows the
exact figure (`Daily: 73% · 210 of 287 days`).

**5-week grid (expanded, right).** The 35 days ending today, in 5 rows of 7,
each row labelled with its first day. A tile is on if its date is in the key's
coverage. Today is outlined. Hovering a tile shows its date.

Series and session dates are UTC-bucketed by the API. A user-local day boundary
is a later refinement (`data-consolidation-model.md`).

## Source priority: `GET /api/source-roles`, `PUT /api/source-roles/<metric>`

The left side of an expanded row lists the row's sources. How it behaves
depends on the grain:

- **Daily metrics** have a pick, so their sources form a priority list. The
  sources come from `/api/source-roles` (`{metric: {primary, configured,
  sources}}`, `sources` in pick order), primary first and marked PRIMARY. Each
  tile carries its source id. Tiles can be reordered by pointer drag (mouse,
  pen or touch) or with the arrow keys.
  - Only the primary is configurable, because the sources below it always keep
    OpenFit's default order. So a drop that changes the top sends `PUT
    /api/source-roles/<metric> {"primary": "<top source id>"}`, re-reads
    `/api/source-roles` and shows the server's order. A drop that leaves the
    top unchanged snaps back with a note explaining why. A failed PUT reverts
    and shows the error.
  - The note under the list reads "Changing the primary source re-derives the
    dataset at the next sync." This is because roles are forward-only. With a
    single source it reads "<Source> is the only source. Connect another to set
    a priority."
- **Series and sessions** are a per-source passthrough with no pick yet, so
  their sources are read-only, with a note saying so. Series sources come from
  the catalog's `sources`. Session kinds have no `sources` in the catalog, so
  the page reads `/api/sessions/<kind>` once, on first expand, and lists who
  reported them.
- **No source yet:** "No source is reporting this yet." The page doesn't name
  devices that could report a metric, because the backend has no capability
  registry.

Source ids are shown title-cased (`garmin` → Garmin, `google_health` → Google
Health, a pushed `eufy` → Eufy).

## Loading and refresh

On load the page fetches `/api/catalog?include_empty=1`, `/api/coverage` and
`/api/source-roles` in parallel and renders once all three resolve. Until then
it shows a loading line. If any of them fails, the card shows "Couldn't load the
dataset" with a retry button. A successful **Sync now** re-reads all three.
Expanded rows stay open across the refresh. If a refresh fails, the last good
load stays on screen with a note.

The top bar's last-sync stamp still comes from `GET
/api/activity?by_source=1` (its `synced_at`). Nothing in the access contract
carries sync times. That is the page's only use of that compatibility shim.

## What went with the old tabs

The 2×2 Home tile grid went (weight chart, add-data accordion, activity log,
steps/sleep tiles, and the `home_tiles` setting they were saved in). So did the
Plan tab, which saved nothing, and the "Custom tabs" placeholder. Those were
tracker features, and trackers are external apps. The manual weigh-in form went
with them. `POST /api/weights` and the webhook still accept hand-entered
readings, and a manual-entry surface can come back as an app.

## The widget model

The console is the only page OpenFit ships. Anything that *uses* the data (a
weight trend, a training planner, a sleep dashboard) is an external app reading
the access contract: discovery from `/api/catalog`, values from
`/api/metric/<metric>`, `/api/sessions/<kind>` and `/api/series/<metric>`. Each
reads the derived profile and never reconciles sources itself. Phase 4 of
`ROADMAP.md` gives those apps a sandboxed host (no build, no network beyond the
contract). Until then they're just pages or scripts that call the API. The
console stays a verification view: what is there, how continuous it is, and
who wins.
