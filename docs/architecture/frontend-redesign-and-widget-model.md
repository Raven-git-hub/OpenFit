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

## The page: top bar, tab strip, Home's tabs and Data

**Top bar.** The logo on the left. On the right, today's date in a pill, and
under it two small pill buttons: **settings**, then the **theme** toggle, which
cycles Auto / Light / Dark (per browser, in `localStorage` as `of-theme`). The
bar no longer shows a last-sync stamp. Settings opens the Settings popup
(below).

**Tab strip.** The angled tab strip from before the console, restored with its
original markup and CSS (`.tabstrip`, `.tab`, `.tab-add`), so it looks exactly as
it did. It holds:

- **Home's tabs**, at the far left: one per tab of the saved layout, in its
  order, each labelled with the tab's name (Home, on a fresh install). The
  page draws them from the layout once it's read; until then a stand-in Home
  tab holds the place. They all share one panel: the installed apps' tiles
  for the tab picked, each app's display spec, rendered server-side, laid on
  a grid six square cells across in the saved order, each at the size chosen
  for it from the app's own. A sub-header under the strip holds that tab's
  subtitle and the **Configure** toggle, which reorders and resizes the
  tiles, moves them between tabs, makes, renames and deletes tabs, and saves
  the layout (see [app-host.md](app-host.md)).
- **Data**, at the far right. It holds the whole console, below. It isn't one
  of the layout's tabs: it can't be renamed, deleted or moved.
- **＋ Add**, the dashed tab just right of Data, shown only while Configure is
  on: it makes a new Home tab.

The page opens on the tab chosen as the **Opening page** in Settings (below),
Data by default. Switching between Data and Home shows one panel and hides the
other. Nothing is re-fetched or repainted, so the console keeps its state (open
rows, notes) underneath. Switching between Home's tabs redraws the tiles from
what was last read, with nothing fetched. The tab last viewed isn't remembered:
a reload opens the opening page again.

**The Data tab** is two even columns that grow with the window: a CSS grid
(`.console`, `minmax(0,1fr) minmax(0,1fr)`), capped at 1600px so it doesn't
sprawl across a very wide monitor. The left column holds Connected sources over
Your data, as before; the right holds **Applications** (below). At 1240px and
under it drops to one column, at the 940px cap the console always had, with
Applications under the data: narrower than that, a column is under ~580px and a
Your data row (chevron, name, reading, grain, continuity) would squeeze the
metric's name. Left column:

1. **Connected sources.** One compact row per connected device: name, status
   pill, **Sync now**, and a gear **Settings** button that opens the source's
   Settings popup (below). Below them, **Add source**. When a connector
   isn't connected yet, it opens a menu of those connectors, and picking one
   opens the existing add flow, built from the connector's manifest: the
   credentials modal, or the two-step OAuth paste-back modal. When every
   connector is connected, it opens the coming-soon placeholder, until it is
   wired to the add-source popup that replaces it.
2. **Your data.** The heading, then the list straight away: one row per catalog
   entry (below), grouped by category. Click a row to expand it.

Right column:

3. **Applications.** Two stacked blocks, not one card:
   - **The controls card** (`#appsCard`): the "Applications" title and its
     voice button, then **＋ Add Application** in Add source's style on the
     left, with **Browse**, **Build** and **Create** grouped at the far end.
     Add Application opens the install popup (below); the other three are
     placeholders for now, each opening the coming-soon modal. Under the
     buttons, a status line that stays empty until an install has something
     to say. It holds no app rows.
   - **The app list** (`#appListCard`), directly below it: the right column's
     equivalent of Your data's list, a container of rows (`.alist`), one per
     installed app from `GET /api/apps` (the list Home draws its tiles from),
     showing its name. There's no status beside it: `/api/apps` lists only the
     apps that load and carries no status of its own. The list's states live
     here too: with none installed it reads "No applications installed"; if
     the read fails, it says so with a **Retry**.

   The list is read at boot and again after an install or uninstall. **Each
   row expands** exactly as a Your data row does: the same mechanism
   (`expandable()`, which both lists' rows use) and the same look (a
   `.ditem` whose head is a `.drow`: chevron · name, the detail a `.dpanel`),
   and an open row stays open across a refresh. Open, it shows three columns
   (one on a phone):
   - **Reads**: the metric keys the app's manifest declares, or "No declared
     reads".
   - **Files**: the files in its folder; clicking one opens it in a read-only
     popup, its text in a monospace block (`GET /api/app/<id>/file`).
   - **Writes**: a reserved slot for the writes ticker (Day / Wk / Mth, each
     `—`), filled in once an app can write back.

   Under them, **Refresh** re-runs the app's render (`POST
   /api/app/<id>/render`, with its tile's settings) and redraws its Home
   tile in place, without a reload; **Duplicate** reads every one of the
   app's files back and opens the Add Application popup on a copy, its
   `app.yaml` given the next free id and name (`steps-2`, "Steps 2"), to
   review and install as a separate app (a file that can't be read, such as
   one that isn't text, stops it with a message in the row, and nothing is
   written until Install); **Uninstall** (in the danger style)
   opens a confirm in the row, "Uninstall <name>? This deletes its files
   from `/data/apps/`.", and confirming it (`DELETE /api/apps/<id>`) takes
   the app off the list and Home at once. Still to come, each its own branch: the writes ticker, then
   alerts. The server side of both actions is in
   [app-host.md](app-host.md#an-apps-row-reads-files-refresh-uninstall).

   **The Add Application popup** installs an app from the browser: a slot per
   file (`app.yaml` required, `app.py` optional, **＋ Add file** for more),
   each pasted or uploaded; **Review** shows the id read from the `app.yaml`,
   the files, a custom-code notice when any is Python, and, when Home has
   more than one tab and Configure is off, an **Add to tab** dropdown (the
   default tab selected; mid-Configure the new tile joins the tab on screen
   and is moved like any tile); **Install** posts them to `POST /api/apps`. On success it
   closes, moves the new app's tile to the chosen tab if that isn't the
   default (the layout read and saved through `/api/home-layout`), and the
   list and Home are re-read, so the app shows at once; on a refusal it stays open with the
   server's message. The full flow and the server's checks are in
   [app-host.md](app-host.md#installing-an-app).

Each card's title has a voice button just after it (below).

**The Settings popup.** Built on the same `.modal` chrome as the add-device
flows, with one control: **Opening page**, a dropdown of the tabs. Its options
are built from the tab strip itself (every `.tab[data-tab]`, labelled with the
tab's own text: Home's tabs by name, then Data), so a new tab is offered
without touching the popup. **Save** stores the choice per browser, in
`localStorage` as `of-opening-tab`, and closes the popup; **Close** (or Escape,
or a backdrop click) closes it without saving. On load the page opens that tab,
in place of the old hardcoded Data default; if nothing is stored, it opens
Data. Home's tabs aren't known until the layout is read, so a stored Home tab
opens Home straight away and is drawn once the layout arrives; one that no
longer exists (a tab since deleted) opens Home's default tab. The choice takes
effect on the next load, not at once. There is no helper text in the popup.

**A source's Settings popup.** The gear on a connected source's row opens it,
on the same `.modal` chrome, titled with the source's name. It reads
`GET /api/connectors/<id>/detail` (`{connected_via, login, added, last_sync,
sync_hours, sync_is_default}`) and shows it as a label / value list:

- **Login**: the account's non-secret identifier (a Garmin email), or `—` when
  there is none. The API only ever returns non-password manifest fields here,
  never a password or an OAuth token.
- **Connected via**: Password (`credentials`) or OAuth (`oauth`).
- **Last sync**: the latest `synced_at` across the source's rows, stored as UTC
  and shown as a local date and time; **Never** before its first sync.
- **Added**: when the account was stored, as a date.
- **Sync every**: how often the source syncs, as a row of preset buttons in this
  order: **1h · 3h · Default · 12h · 24h**. Default is the baseline
  (`SYNC_INTERVAL_HOURS`, 6h unless the install changes it) — no override
  stored — and is its own button, not "6h". The active preset is highlighted
  with the existing `.btn.primary` fill: Default when `sync_is_default`,
  otherwise the button matching `sync_hours`. Clicking another sends
  `PUT /api/connectors/<id>/cadence` with `{hours: N}` (`{hours: null}` for
  Default) and moves the highlight in place, with no reload; the source's next
  scheduled sync is then that far off. Clicking the active one does nothing. On
  failure the popup's error line shows the error and the highlight stays where
  it was. The row is inert until the detail lands. Just the label and the
  buttons, no explanatory note. There is no global cadence: the universal
  Settings popup keeps its one control.

Until the detail lands each value reads `…`; if it fails, they read `—` with
"Couldn't load this source's details." under them, nothing is highlighted under
Sync every (any preset can still be picked), and Remove still works, since
removing is the way out of an account that won't read. **Remove source**
(danger) sits at the foot, with **Close** opposite it.

Remove is a two-step gate inside the same modal. There is no browser
`confirm()` or `alert()`:

1. "Disconnecting stops future syncs.", then a danger panel: **Wipe data?**
   "Keep the source's historical data for analysis, or wipe it entirely from
   the database?" with **Keep data** and (danger) **Wipe data**. **Cancel**
   below it goes back to the info list.
2. **Are you sure?**, naming the source and whether its data stays or is wiped
   ("This can't be undone."), with **Cancel** (back to the info list) and a
   solid danger **Remove** or **Remove & wipe**.

Confirming sends `DELETE /api/connectors/<id>`, with `?wipe=1` on the wipe path,
which also deletes the source's metrics, samples and sessions and re-derives the
days its readings covered. On success the popup closes and the page re-reads
both the connectors (the row goes) and the dataset (`loadData()`), so a wipe's
effect on the rows shows at once. On failure the error shows in the popup and
the button comes back.

**The coming-soon placeholder.** One reusable modal (`comingSoon(title)`),
built on the same `.modal` chrome: a title, "Coming soon." and a Close button
(Escape and a backdrop click close it too). Add source (with nothing left to
add) and Applications' Browse, Build and Create open it, each with its own
title.

## Voice explanations: `GET /audio/<name>`

A small round button sits just after the **Connected sources**, **Your data**
and **Applications** card titles. Each plays a spoken explanation of its card:
`/audio/connected-sources.mp3`, `/audio/your-data.mp3` and
`/audio/applications.mp3`. The Applications clip isn't recorded yet (a TODO in
`VOICE_CLIPS`): until it lands in `app/audio/`, it 404s and the button resets.

- Clicking a button plays its clip from the start. Clicking it again stops and
  resets it. Clicking another button stops the first clip and plays its
  own, so only one clip plays at a time. Idle shows a play glyph, playing a
  stop glyph. There is no caption and no toast.
- The buttons share one `<audio>` element. Playing sets its `src` to the
  card's clip, sets `currentTime = 0` and calls `play()`. The button resets on
  `ended` and on `error` (and if `play()` is refused). The cards repaint (after
  a sync, say), and a repainted button picks up whatever is playing.
- The clips are served by `GET /audio/<name>` →
  `send_from_directory(AUDIO_DIR, name)`, with `AUDIO_DIR` from
  `$AUDIO_DIR` (default `/app/audio`). The clips ship in the repo, in
  `app/audio/`, and are baked into the image at build (`COPY audio ./audio`
  in `app/Dockerfile`), so they deploy with the code:
  `git pull && docker compose up -d --build`. Adding or replacing a clip is a
  commit and a rebuild. (They used to be hand-copied onto the `/data` volume
  at `/data/audio`; that approach is retired.) Static serving is otherwise off
  (`static_folder=None`).
- If a clip is missing, its request 404s and the button silently resets: no
  error dialog and no broken state. The button isn't hidden, so it works the
  moment the file exists.

## The rows: `GET /api/catalog?include_empty=1`

Each catalog entry is one row, grouped by category (**Grouping**, below);
inside a category, rows sort by grain, ties keeping the order the catalog
returns them in (vocabulary order). Each row has fixed, aligned columns:

`[chevron] Name · Reading · Grain ··· [Day] [Wk] [Mth]`

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
- **Grain.** The catalog's `grain`, capitalised: Daily, Instant, Session,
  Series.
- **Grouping.** Rows are grouped by the catalog's `category`, in the
  `CATEGORIES` display order: **Activity**, **Vitals**, **Body composition**,
  **Fitness**, **Sleep**, **Nutrition & hydration**, **Women's health**,
  **Other**. A category outside those would follow, by name, not be dropped;
  a category with no rows at all (Women's health, which has no keys yet) gets
  no header. Within a category, rows sort by grain — daily, instant, session,
  series — a stable sort, so ties keep the catalog order. Each category gets
  a divider header (the `.dgroup` style) with a muted count, "N reporting",
  plus " · M empty" when it has empty rows. Its reporting rows (`count > 0`)
  follow; then, if it has any empty slots, a fold — a "M not yet reporting in
  <category>" button with a chevron over those rows, dimmed (they still
  expand), hidden until opened. A category with nothing reporting (Body
  composition, without a smart scale) shows its header and the fold alone.
  A fold stays open across a refresh, as an expanded row does; it also opens
  while one of its rows is expanded, and closing it closes its rows.

## Continuity: `GET /api/coverage`

Coverage lists, for every key, the `YYYY-MM-DD` dates that hold any data from
any source. All of the continuity UI is **source-agnostic**: one colour (the
`--coverage` token, the accent by default), with opacity as the only signal.

**Rhombuses (Day / Wk / Mth).** These are skewed like the tab strip. Every
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
load stays on screen with a "Couldn't refresh" note under the heading.

The page doesn't read `GET /api/activity` at all. It used to, only for the top
bar's last-sync stamp (nothing in the access contract carries sync times), and
that stamp is gone.

## What went with the old tabs

The strip is back, but not what was in it. The 2×2 Home tile grid went (weight
chart, add-data accordion, activity log, steps/sleep tiles, and the `home_tiles`
setting they were saved in). Home's tiles are now installed apps, drawn by the
app host (below), and none ship with OpenFit. The Plan tab went,
which saved nothing, and so did the Configuration tab and its "Custom tabs"
placeholder. Those were tracker features, and trackers are external apps. The
manual weigh-in form went with them. `POST /api/weights` and the webhook still
accept hand-entered readings, and a manual-entry surface can come back as an
app. The Settings disclosure that briefly sat at the foot of the console went
too: the theme toggle in the top bar covers it, and the token-block note lives
in the `CUSTOMISE OPENFIT HERE` comment itself.

## The widget model

The console is the only page OpenFit ships. Anything that *uses* the data (a
weight trend, a training planner, a sleep dashboard) is an app, and none ship
with OpenFit. An app can be a page or script reading the access contract:
discovery from `/api/catalog`, values from `/api/metric/<metric>`,
`/api/sessions/<kind>` and `/api/series/<metric>`. Or it can be installed in
the app host: a folder in `/data/apps` whose `render(ctx, cfg)` returns a
display spec the Home tab draws as a tile, reading the derived profile through
`ctx.metric()` ([app-host.md](app-host.md)). Either way an app reads the
derived profile and never reconciles sources itself. This first host slice
runs apps as trusted code. Phase 4 of `ROADMAP.md` sandboxes it (no build, no
network beyond the contract). The console stays a verification view: what is
there, how continuous it is, and who wins.
