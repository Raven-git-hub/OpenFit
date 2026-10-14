# OpenFit Data Consolidation Model

_Design reference · drafted 2026-10-12_

How OpenFit turns multi-source health data into one trusted dataset that hosted
apps read. This is the design we build slices against; it supersedes the ad-hoc
notes in earlier schema docs where they conflict. It records both what exists in
the code today and what is designed-but-unbuilt — each section marks which.

---

## 1. What OpenFit is (and isn't)

**A collation engine, not an analysis engine.** OpenFit *selects and arranges*
recorded facts; it never *derives new* ones. Stitching a series or picking a
source is collation — choosing which recorded value to trust by a declared rule,
and arranging it into a documented, source-tagged shape. Interpretation —
scoring, classifying ("this was REM"), computing aggregates and presenting them
as insight — is the app's job. The test: if the output is a fact a device
recorded, it's collation; if it's a fact *OpenFit concluded*, it's over the line.
Consequence already in force: OpenFit takes a vendor's computed daily summary
(resting HR, daily HRV) rather than recomputing it from samples — recomputing
would be analysis.

**A historian, not a telemetry feed.** OpenFit holds *history* — including
fine-grained historical series (an intraday HR curve the device already
recorded and synced). It is never a live/real-time monitor. "The present" is
only ever as current as each source is, bounded by device→cloud sync lag.

**Health data only (scope boundary).** OpenFit holds health data: device-sourced
readings, manually-entered health data (weight, calories, fluid), and app-derived
health data (a custom sleep detection, a computed metric). It does **not** hold
operational context — rosters, shift calendars, rest-opportunity windows. An app
brings that from its own storage and joins it against OpenFit's health data on
its own side. Governance test for any future "should OpenFit store X?": is X a
health fact from a source, or derived from one? Weight, sleep, HR pass. Rosters,
calendars, shift patterns fail.

---

## 2. The three storage grains

Every data type lands in one of three shapes, chosen by the source's own record
type. (Google Health's taxonomy — Daily / Session / Sample / Interval — is the
vocabulary guide; the canonical vocabulary is kept source-neutral and
extensible, since other sources carry types Google doesn't.)

1. **Daily** — one value per `(date, source, metric)`. Table: `metrics`. **Built.**
2. **Session** — an interval (`start`/`end`) plus a vendor summary, keyed by
   *kind*, source as an attribute. Table: `sessions`. **Built** (sleep, workout).
3. **Series** — timestamped samples (HR, SpO2, …), one row per sample per source.
   Table: `samples`. **Storage and read built** (Garmin intraday HR,
   respiration and SpO2; Google Health intraday HR and SpO2 at one value a
   minute; `GET /api/series` as a per-source passthrough); the cross-source
   stitch is designed, not built. This is the "intraday" grain.

A session is two things: an **interval** (universal, source-agnostic — any app
can slice the series with it) and a **summary** (a vendor interpretation OpenFit
cannot reproduce, e.g. sleep stages from proprietary algorithms on raw signals
OpenFit doesn't ingest — so it is stored verbatim).

---

## 3. The sync loop

**Built:** APScheduler syncs every source once shortly after startup, then each
connected source on its own interval job: every `SYNC_INTERVAL_HOURS` (default
6), unless the source's Settings popup overrides it with 1, 3, 12 or 24 hours
(the `sync_cadences` settings row). Still one sync at a time; jobs that come due
together queue on the scheduler's single worker.

**The model — trailing window, re-pull, re-derive:** each cycle re-pulls a fixed
trailing window (7 days) per source, writes whatever comes back into that
source's own raw rows, then re-derives the touched range. Stateless and bounded.
This is deliberately dumber than "fetch since my last data point," because gaps
are not only at the recent end:

- **Interior gaps** — a device not worn on a middle day.
- **Late / backfilled data** — a source uploads a past day after OpenFit already
  passed it; a fixed window re-checks and catches it.
- **No per-(source, type) watermark bookkeeping** to maintain.

A one-time **deep backfill** runs when a source is first connected. For heavy
series types, start with the same window; a watermark + short reconcile window is
a later optimisation only if re-pulling raw samples every cycle actually bites.

**Fetch is dumb; derive is smart.** Each source fills its own raw lane
(plugins write only their own rows). Priority/gap-fill happens *after*, in the
derive step — never during fetch. Frequency (how often we poll) and window (how
far back we reconcile) are independent knobs.

---

## 4. Derive-and-pick — building the derived dataset

**Built** for the daily grain (`derived_metrics`, `recompute_derived`);
generalises unchanged to buckets and series.

For each `(data type, bucket)`: collect every source that has a reading there,
rank by priority, take the top, write it **tagged with its winning source**
(provenance). The pick is evaluated **independently per bucket** — which is
exactly what produces cross-source gap-fill: within one HR curve, Oura wins the
stretches it covered and Google wins the stretch Oura was off. The derived series
is a patchwork, each segment carrying its source. A bucket no source covered is
**null** — OpenFit never fabricates data to fill a gap.

**Priority config:** a per-metric primary (`settings.source_roles`, e.g.
`{weight: eufy, calories: google_health}`) overrides the default precedence
(`DEFAULT_PRIORITY = garmin > google_health`, unlisted sources last). Each
metric independently names its source. Forward-only: a change applies from the
next sync's window, never a blanket re-derive of history.

**Manual input** is just another source competing in the same pick.

**Additive / manual types (calories, fluid): single source, no fall-through.**
For a measurement (weight), falling through to another source on a missing day is
harmless. For an additive log, fall-through is a trap — a partial total from a
second app (breakfast only) would masquerade as the full day. So additive metrics
use one source, period; show a gap rather than a misleading total. And **never
sum across sources** — treat additive data as a per-source daily total and pick
one. (This also disarms the rare double-count where the same datum arrives via
two sync paths.)

**Day boundary: user-local time.** "Tuesday" means the user's local Tuesday, to
match how people and vendor apps present health data. Requires OpenFit to hold a
configured timezone (a `settings` value). *To-build — today date handling leans
on each source's own labels.*

---

## 5. The series grain (intraday) — storage and passthrough read built, stitch designed

Store each source's raw samples per source: `(source, metric, timestamp, value)`.
Oura's and Google's samples sit side by side, untouched — dumb raw lanes.

**Built:** the `samples` table and `write_sample()`; Garmin writes three
intraday series: heart rate (`heart_rate`, ~2-minute cadence, non-wear nulls
dropped), respiration (`respiration`, ~2-minute cadence) and SpO2 (`spo2`,
hourly averages). Respiration and SpO2 come out of the same `get_respiration_data`
/ `get_spo2_data` responses that already give the daily vitals — one fetch,
both grains — through one reusable `[timestamp, value]` row converter that
drops Garmin's negative "no reading" sentinels (-1/-2) along with nulls and
unreadable rows: the series counterpart of the daily field maps. The read
side exists as a **per-source passthrough**: `GET /api/series/<metric>` (§8)
lists every source's raw samples in a range, each tagged, with no priority
applied.

Google Health is the second series source: heart rate (`heart_rate`) and SpO2
(`spo2`), stored at one value a minute. Google keeps heart rate at about one
reading a second; a `rollUp` windowed to 60 seconds averages it per minute on
Google's side, so only per-minute buckets are fetched. SpO2 is already about one
a minute (valid sleep minutes only) and is listed as it comes. Google has no
continuous respiration series, so `respiration` stays Garmin's alone. Garmin's
and Google's heart rate now sit side by side in the passthrough, each tagged; the
stitch below, which replaces the passthrough, is the next slice and now has a
second source to build and test against.

**The graph is a read-time stitch, not a stored copy.** When something requests a
range, OpenFit walks it and emits the samples of the **highest-priority source
with coverage**, switching source where the primary goes quiet past a
**gap-threshold** (the one knob: a 40-second silence is normal sparseness; a
2-hour silence is "took it off"). Real recorded points are returned — no
resampling onto a grid, no invented values — each tagged with its source. It is
the same priority/pick logic as the daily grain, applied to time segments.

**Compute-on-read, not materialised** (confirmed decision): a second
multi-million-row derived table buys nothing; the stitch is a view over the raw
lanes.

This is what lets a hosted app compute its *own* daily figures (resting HR, sleep
efficiency) from the raw series: the platform provides the substrate, the app
does the maths, and a derived result can re-enter OpenFit as its own source (see
§7). "Collects enough to *allow* derivation" is the bar — OpenFit still doesn't
derive those figures itself.

---

## 6. Sessions — source-agnostic, dedup per event

**Built:** one `sessions` table keyed by *kind*, source as a column;
`GET /api/sessions/<kind>` already returns every source's session together, as
equals, each tagged. The Oura night and the Google nap are both "sleep sessions."

- **Source-agnostic in organisation, tagged in provenance.** Kind is the primary
  axis; source is metadata you keep (it's what lets an app see a cross-source
  comparability seam). Never dropped, never the filing cabinet.
- **No cross-source merge.** Sessions are discrete vendor interpretations and are
  kept whole, per source. (Consistent with the long-standing ruling.)
- **Dedup is per *event*, by interval overlap — not per date** (*designed*). Two
  sleep sessions that overlap heavily = the same event seen by two devices → pick
  one by priority. Two that don't overlap = different events (nap vs night) →
  keep both. A date key cannot tell these apart; the nap case proves it. Any
  "one per night" convenience view clusters by overlap.
- **Flagship apps read the raw tagged pool** and apply their own event logic.
  Core must not bake an overlap heuristic that could hide data from the app that
  cares most (e.g. a shift-worker fatigue tracker reading short, off-cycle rests
  by their real timestamps — not any nocturnal day-bucket).
- **Custom / app-detected sleep is just another source** in the same pool (tier
  `app`), queryable alongside vendor sessions, filterable by source. Vendor
  detection stays the gold standard; custom detectors coexist, they don't
  replace. *Depends on session write-back (to-build; the webhook is metrics-only
  today).*
- **Preferred-source-per-session-kind** selection reuses the same priority
  config. *Designed.*

Honest limit: a from-scratch detector or a rest-quality assessment is only as
good as the raw signal the APIs expose (HR yes, HRV sometimes, true
actigraphy/movement often not).

---

## 7. Trust, write access, and source tiers

**One store, not a second dataset.** "Device data is read-only to apps" is
already enforced structurally, without physical separation:

- Tables are **keyed on source** (`metrics` on `(date, source, metric)`; a
  session id starts with its source). An app writing `source='myapp'` can only
  ever create or overwrite *its own* rows — it is structurally incapable of
  touching a `garmin` row.
- The **authed write path refuses device source names** (the webhook already
  rejects a pull-plugin id as source). **Built** for metrics.
- Reads go through the open, read-only contract; writes go through the authed
  push path, own-source only. That *is* the read/write split — not two datasets.

**Source tiers: `device` / `manual` / `app`.** App-derived / custom-detected
data is a **separate opt-in tier** (confirmed decision): it does **not** compete
in the default derived pick. Device + manual data form the default trusted
profile; an app that wants app-contributed data asks for it explicitly. A custom
detector's guess can never silently outrank Oura. *Tier marker is to-build.*

The app-writable tier is for derived *health* data in OpenFit's canonical
vocabulary — not a home for app context (rosters, calendars). See §1 scope.

---

## 8. The access contract (what apps read)

**Built:** `GET /api/catalog` (metrics, session kinds and series),
`GET /api/metric/<metric>`, `GET /api/sessions/<kind>`,
`GET /api/series/<metric>` (intraday samples; a per-source passthrough for now,
§5) — read-only, no auth, provenance included, stable and additive-only.

**Designed additions:**
- The stitch in `GET /api/series/<metric>` — the intraday timeline picked by
  priority, provenance per segment (§5). Deferred, though the second series
  source it waited on (Google Health) now exists.
- A preferred-source session view (per-event dedup, §6).
- An opt-in to include the `app` tier (§7).

---

## 9. Current state vs designed

**Built today:**
- `metrics` (daily) + canonical vocabulary; `derived_metrics` with per-metric
  source roles and fixed default precedence; forward-only re-derive over the sync
  window.
- `sessions` (sleep, workout), source-agnostic querying by kind.
- `samples` (series grain) storage; Garmin intraday heart rate, respiration and
  SpO2 ingested into it (respiration and SpO2 from the vitals responses already
  fetched — no extra request).
- Sync: 6-hourly, 7-day trailing window, re-derive.
- Webhook push ingestion (metrics only, token auth, own-source enforcement).
- Access contract (catalog, metric range, sessions, intraday series as a
  per-source passthrough).
- Google Health's daily metrics (daily roll-ups and daily summaries), sleep
  and exercise sessions, and its intraday heart rate and SpO2 series at one
  value a minute (heart rate via a 60-second windowed roll-up) + Garmin
  equivalents + manual weight.
- Garmin daily metrics beyond steps + resting HR — the first of the cross-vendor
  daily core: distance, active/total calories, floors, and moderate/vigorous
  intensity minutes, from the `get_stats` response it already fetches.
- Garmin daily vitals — SpO2 (`spo2_pct`), waking respiration rate
  (`respiratory_rate_brpm`) and overnight HRV (`hrv_ms`), a request each; the
  SpO2 and respiration responses also give their intraday series (above).

**Designed, to-build:**
- The **series stitch** (compute-on-read priority pick in `GET /api/series`;
  Google Health is now the second series source it needed) and further series
  sources.
- **Wide expansion** — more daily/session/series types, each a fetcher + a
  canonical key/kind (a new metric is data, not a migration).
- **Per-event session dedup** + preferred-source-per-kind selection view.
- **Source tiers** (device/manual/app) with app-tier as opt-in.
- **Session write-back** (apps contributing sessions, incl. custom detectors).
- **Additive-type single-source** handling (no fall-through).
- **User-local day-boundary** bucketing (requires a timezone setting).

---

## 10. Open / deferred decisions

- Confirmed this round: dedicated design doc (this one); app-derived data as a
  separate opt-in tier; user-local day boundary.
- **Series materialise vs compute-on-read** — leaning compute-on-read; confirm at
  build time.
- **Gap-threshold value and "stable segments" anti-flap rule** for series
  stitching — tuning parameters, set during the build.
- **Retention / downsampling** for high-volume series — deferred; a later knob,
  not a day-one concern.
- **Watermark + reconcile window** for heavy series fetch — only if the plain
  trailing window proves too costly.
