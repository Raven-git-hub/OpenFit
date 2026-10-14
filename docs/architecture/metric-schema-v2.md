# OpenFit metric schema v2

_Design reference · 2026-10-14. The canonical vocabulary — which metrics
OpenFit keys, in what unit, shape and group. It supersedes the vocabulary in
`data-points-and-metric-schema.md` where the two conflict; the engine itself
(derive-and-pick, the access contract) is `data-consolidation-model.md`. The
code is `app/metrics.py`._

## Status

v2 lands in four PRs:

1. **The engine (built).** The v2 vocabulary in `app/metrics.py` — every key
   with its unit, category, grain and plausible range — plus `grain_for()` and
   `category_for()`; `/api/catalog` reports each entry's `grain` and
   `category`; `/api/sessions/<kind>` lists sessions in source-priority order;
   daily `sleep_minutes` retired (migration `010`) and with it `/api/activity`'s
   sleep field.
2. **Garmin fills its new keys (built).** From `get_stats`: basal calories
   (`bmrKilocalories`), sedentary minutes (`sedentarySeconds`) and elevation
   gain (`floorsAscendedInMeters`); from `get_body_composition`: muscle and
   bone mass (grams, converted and range-guarded like weight), body water and
   visceral fat. The field names and the grams unit are unverified guesses —
   an absent `get_stats` field, or a mass out of range once converted, is
   logged so the logs say which to correct. Garmin's physique rating and
   metabolic age are excluded (law 5), and it doesn't write
   `active_minutes` (see *Intensity*).
3. **Google Health enrichment (built: daily metrics and intraday series).**
   Via dailyRollUp:
   distance, floors, elevation gain (`altitude`), active and total calories,
   Active Zone Minutes (summed across zones, as `active_minutes`) and
   sedentary minutes; via list, one point a day: HRV, VO2 max (the daily
   cardio fitness score), SpO2 and respiratory rate. Every daily fetch asks
   for, and files, the same window as the sync. Names and units come from
   the v4 discovery doc, not yet a real sync, so each type logs what it got.
   3b added Google's intraday series, as samples at one value a minute:
   `heart_rate` from `heart-rate` (kept by Google at about one a second, so
   asked for as a rollUp windowed to 60 seconds — per-minute averages,
   computed by Google) and `spo2` from `oxygen-saturation` (already about one
   a minute, listed as it comes). Google has no respiration series. Still to
   come: body composition, nutrition and skin temperature; `basal_kcal` waits
   on a decision, since v4 has no rollup for `basal-energy-burned`, only raw
   interval points.
4. **The console regroups by category and grain (built).** The "Your data"
   card groups its rows by category, in display order, with grain as the
   inner sort, each category's not-yet-reporting rows folded behind one
   button; a row's third column is its grain. The deprecated
   `cadence_for()` / `MANUAL_METRICS` shim is deleted, and with it the
   catalog's `cadence` field, whose only consumer was the console.

## The laws

1. **Reconcile, don't derive.** The engine picks the trusted source for a value
   that exists; it never computes a new value. Aggregates, totals, averages and
   scores are an app's job.
2. **Translate, don't infer.** A source maps its data faithfully onto canonical
   keys — unit conversion, renaming — but never fabricates a value it doesn't
   report.
3. **Canonical keys normalise naming, not measurements.** The same concept from
   two vendors gets the same key; genuinely different measurements get
   different keys, even when they share a name.
4. **Sources are uniform.** Device plugins, the webhook, hand entry and apps are
   all just *sources*: a reading carries its source as provenance, and the
   source-roles precedence picks the primary among them.
5. **Declare real metrics, exclude proprietary composites.** Any real,
   non-proprietary measurement gets a key, even before any source supplies it.
   Vendor black-box scores are excluded: Body Battery, readiness, sleep score,
   fitness age, physique rating, and their like.

## Grains — the shape of the data

| grain | what it is | where it lives |
|---|---|---|
| `daily` | one figure per day | `metrics` → `derived_metrics` |
| `instant` | a single timestamped point — a weigh-in, a blood-pressure reading | `metrics` → `derived_metrics`, one per day for now |
| `session` | an interval with a summary — a night's sleep, a workout | `sessions` |
| `series` | intraday timestamped samples — heart rate every two minutes | `samples` |

`instant` is a label for now: it gets no table of its own, and its readings are
still filed one per `(date, source)` in `metrics`, like a daily one.

There is **no `manual` grain.** Hand entry is a *source* (`manual`), not a
shape: a weigh-in is `instant` whether it came from a smart scale, the webhook
or a keyboard. The pre-v2 `cadence` label (`daily` / `manual` / `session` /
`reading`), which conflated the two, was removed in PR 4.

`grain_for(key)` answers for any key: `series` for a series metric, `session`
for a session kind, else the metric's declared `daily` or `instant`.

## Categories — the semantic group

The primary axis for grouping, in display order (`CATEGORIES`):

1. Activity
2. Vitals
3. Body composition
4. Fitness
5. Sleep
6. Nutrition & hydration
7. Women's health — declared, no keys yet (see *Deferred*)
8. Other

A category is metadata the key carries, independent of its source: steps are
Activity whichever device counted them. A metric an app computes and pushes
back files under the category it fits, with **Other** the catch-all.
`category_for(key)` answers for any key; series metrics are Vitals, the `sleep`
session kind is Sleep and `workout` is Activity.

## The canonical metrics

Daily unless the grain says otherwise. The range is the plausible one — both
ends inclusive, in the key's unit — that a pushed reading must fall within; it
catches garbage, not unusual days.

| key | unit | category | grain | plausible range |
|---|---|---|---|---|
| steps | count | Activity | daily | 0–100000 |
| distance_m | m | Activity | daily | 0–300000 |
| floors | count | Activity | daily | 0–1000 |
| active_kcal | kcal | Activity | daily | 0–15000 |
| total_kcal | kcal | Activity | daily | 0–20000 |
| basal_kcal | kcal | Activity | daily | 0–5000 |
| sedentary_min | min | Activity | daily | 0–1440 |
| elevation_gain_m | m | Activity | daily | 0–20000 |
| intensity_moderate_min | min | Activity | daily | 0–1440 |
| intensity_vigorous_min | min | Activity | daily | 0–1440 |
| active_minutes | min | Activity | daily | 0–1440 |
| resting_hr_bpm | bpm | Vitals | daily | 20–200 |
| hrv_ms | ms | Vitals | daily | 0–500 |
| spo2_pct | % | Vitals | daily | 50–100 |
| respiratory_rate_brpm | brpm | Vitals | daily | 3–60 |
| skin_temperature | °C | Vitals | daily | 20–45 |
| body_temperature | °C | Vitals | instant | 30–45 |
| blood_pressure_systolic | mmHg | Vitals | instant | 50–250 |
| blood_pressure_diastolic | mmHg | Vitals | instant | 30–150 |
| blood_glucose | mmol/L | Vitals | instant | 1–40 |
| weight_kg | kg | Body composition | instant | 20–400 |
| body_fat_pct | % | Body composition | instant | 2–70 |
| bmi | kg/m2 | Body composition | instant | 8–100 |
| height_cm | cm | Body composition | instant | 50–260 |
| muscle_mass_kg | kg | Body composition | instant | 5–120 |
| bone_mass_kg | kg | Body composition | instant | 0.5–10 |
| body_water_pct | % | Body composition | instant | 20–80 |
| visceral_fat | rating | Body composition | instant | 1–60 |
| vo2_max | ml/kg/min | Fitness | daily | 10–90 |
| hydration_ml | ml | Nutrition & hydration | daily | 0–20000 |
| calories_in | kcal | Nutrition & hydration | daily | 0–15000 |
| carbs_g | g | Nutrition & hydration | daily | 0–2000 |
| protein_g | g | Nutrition & hydration | daily | 0–1000 |
| fat_g | g | Nutrition & hydration | daily | 0–1000 |
| mood | scale | Other | instant | 1–5 |

Notes:

- `blood_glucose` is stored in **mmol/L**; a source reporting mg/dL converts on
  the way in (law 2 — conversion is translation). A CGM source would later
  promote it to a series; it stays `instant` until one exists.
- `skin_temperature` is an **absolute** °C. A source that reports a deviation
  from a personal baseline (Google's sleep-derived skin temperature) is a
  different measurement (law 3), not an absolute reading to file here. PR 3
  left Google's out; its v4 `daily-sleep-temperature-derivations` type also
  carries a `nightlyTemperatureCelsius` (the mean of the night's skin
  temperature samples), which is the one to weigh when it's picked up.
- `visceral_fat` is a unitless rating, as scales report it.
- `mood` is a 1–5 scale.

Compared with the pre-v2 vocabulary: `sleep_minutes` is gone (below); the body
composition keys are `instant` rather than daily; nineteen keys are new; and
several ranges are narrower (steps to 100000, resting HR to 200 bpm, distance to
300000 m, active and total calories to 15000 and 20000 kcal, floors to 1000) and
BMI's wider (to 100).

### Series metrics and session kinds

Unchanged by v2 apart from their category:

| key | unit | category | grain |
|---|---|---|---|
| heart_rate | bpm | Vitals | series |
| respiration | brpm | Vitals | series |
| spo2 | % | Vitals | series |
| sleep | — | Sleep | session |
| workout | — | Activity | session |

Series keys stay their own vocabulary (law 3): the `heart_rate` sample stream is
not the `resting_hr_bpm` daily summary, `respiration` is not
`respiratory_rate_brpm`, and `spo2` is not `spo2_pct`.

## Rules

### Sleep is sessions only

There is no daily sleep metric. A night's sleep is a `sleep` session — its
interval and its `asleep_minutes` / `light_minutes` / `deep_minutes` /
`rem_minutes` / `awake_minutes` summary — and that is all a source files for it.
A daily total ("slept 7h12m on the 3rd") is an aggregate over sessions, which by
law 1 is an app's to compute: which nights count toward which day, whether naps
count, are interpretation.

So `sleep_minutes` left the vocabulary. Migration `010` deletes its rows from
`metrics` and `derived_metrics` (nothing else), the Garmin and Google Health
plugins stopped writing it, and `/api/activity` dropped its `sleep_hours` field.
A night recorded *only* as a daily total — from before migration `007`, when
there were no sessions — goes with it; a sync with a longer window
(`POST /api/sync/<plugin>?days=N`) re-fetches those nights as sessions from the
device's own history, as far back as the device keeps it.

### Intensity

`intensity_moderate_min` and `intensity_vigorous_min` are a source's own
moderate- and vigorous-intensity minutes, as reported. `active_minutes` is the
home for a source's single-number "active minutes" figure, however the vendor
computes it — zone-weighted ones included: Fitbit/Google Active Zone Minutes
maps to `active_minutes`. The key is not defined as unweighted. The engine
never sums moderate and vigorous into `active_minutes`, never splits it back
into them, and never synthesises the WHO moderate/vigorous split from a
vendor's heart-rate zones (laws 1 and 2). The deferred `hr_zone_minutes` is a
different thing: the time breakdown per heart-rate zone (minutes in each
zone), not the single Active Zone Minutes number, so it doesn't displace AZM
from `active_minutes`.

### Sources and precedence

Every reading is tagged with its source, and every source competes the same way
(law 4): `garmin` and `google_health` (pull plugins), `manual` (hand entry), a
pushed source the webhook names (`eufy`), an app's own id. Per metric, the pick
in `app/derived.py` takes the configured primary (its source role) if it has
one, then `DEFAULT_PRIORITY` (Garmin, then Google Health), then any other source
by name.

Sessions have no pick yet. `GET /api/sessions/<kind>` is a **priority-ordered
passthrough**: every source's sessions, unfiltered and unmerged, the source the
pick would rank first leading, then the rest — each source's by start. The
order is a hint for an app that wants one source's nights; a kind follows a
primary configured for it under its own key in the source roles, if one is
stored.

## Deferred — reserved, not yet keys

Each waits until a plugin targets it, because its shape isn't decided. None is
in the code vocabulary.

- **`hr_zone_minutes`** — minutes per heart-rate zone. Zone thresholds are
  vendor- or user-defined, so one source's "zone 3" isn't another's: not
  cross-comparable as a scalar. Likely a per-source structured summary.
- **Menstrual-cycle data** — flow, cycle day, ovulation tests. Event-shaped
  rather than one number a day. The **Women's health** category exists for it
  and holds no keys yet.
- **`ecg`** — a waveform recording, not a value.
- **`symptoms`** — categorical tags, not a number.

## Adding a metric

One `METRIC_DEFS` entry in `app/metrics.py` — key, unit, category, grain and
plausible range — and a mapping in the plugin that supplies it. The metrics
table is tidy, so there's no migration; the derive step, the access contract and
the webhook pick it up as is. A real, non-proprietary measurement gets a key
even before any source fills it (law 5).
