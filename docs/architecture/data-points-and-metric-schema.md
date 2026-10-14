# OpenFit data-point catalogue & metric schema

_Canonical reference · updated 2026-10-13. The consolidation **engine** (grains,
derive-and-pick, access contract) is specified in `data-consolidation-model.md`;
this doc is the catalogue of the data points themselves — the target vocabulary._

> **Superseded for the vocabulary by
> [`metric-schema-v2.md`](metric-schema-v2.md)** (2026-10-14): its laws, grains
> (with `instant`), categories and canonical key table win where this doc
> differs — notably, `sleep_minutes` is retired (sleep is sessions only) and the
> body-composition keys are `instant`. The source-mapping table below remains a
> useful reference.

## Principle

The dataset is the **cross-vendor, raw / standard health metrics** — the ones
multiple vendors expose, so they consolidate across sources. Vendor-proprietary
scores are excluded. "A new metric is data, not a migration," so the vocabulary
grows one entry at a time; this catalogue is the agreed target, not a frozen set,
and nothing here is gated on any one device populating it.

## Grains

- **daily** — one value per `(date, source, metric)` → `metrics`, consolidated into `derived_metrics`.
- **series** — intraday timestamped samples → `samples`.
- **session** — an interval + a vendor summary → `sessions`.

## Catalogue

Status: ✅ built · ○ future · ✕ excluded.

### Activity & fitness
| key | grain | unit | status |
|---|---|---|---|
| steps | daily | count | ✅ |
| distance_m | daily | m | ✅ |
| floors | daily | count | ✅ |
| intensity_moderate_min | daily | min | ✅ |
| intensity_vigorous_min | daily | min | ✅ |
| elevation_gain_m | daily | m | ○ |
| active_seconds / sedentary_seconds | daily | s | ○ |
| steps_intraday | series | count | ○ |
| workout | session | — | ✅ |

### Energy
| key | grain | unit | status |
|---|---|---|---|
| active_kcal | daily | kcal | ✅ |
| total_kcal | daily | kcal | ✅ |
| bmr_kcal | daily | kcal | ○ |
| calories_consumed_kcal | daily (additive) | kcal | ○ |

### Heart & vitals
| key | grain | unit | status |
|---|---|---|---|
| resting_hr_bpm | daily | bpm | ✅ |
| hrv_ms | daily | ms | ✅ |
| spo2_pct | daily | % | ✅ |
| respiratory_rate_brpm | daily | brpm | ✅ |
| vo2_max | daily | ml/kg/min | ✅ |
| heart_rate | series | bpm | ✅ |
| respiration | series | brpm | ✅ |
| spo2 | series | % | ✅ |
| hrv_intraday | series | ms | ○ |
| blood_glucose | sample | mmol/L | ○ |
| blood_pressure_systolic / _diastolic | sample | mmHg | ○ |
| skin_temp_c | series | °C | ○ |
| ecg | session | — | ○ |
| irregular_rhythm | session | — | ○ |

### Sleep & recovery
| key | grain | unit | status |
|---|---|---|---|
| sleep_minutes | daily | min | ✅ |
| sleep (interval + light/deep/rem/awake) | session | min | ✅ |
| sleep_respiratory_rate_brpm | daily | brpm | ○ |
| sleep_spo2_pct | daily | % | ○ |
| sleep_temp_delta_c | daily | °C | ○ |

### Body measurements
| key | grain | unit | status |
|---|---|---|---|
| weight_kg | daily | kg | ✅ (manual + Garmin) |
| body_fat_pct | daily | % | ✅ |
| bmi | daily | kg/m² | ✅ |
| height_cm | daily | cm | ○ |
| muscle_mass_kg / bone_mass_kg | daily | kg | ○ |
| body_water_pct | daily | % | ○ |

### Nutrition & hydration (additive / mostly manual-logged)
| key | grain | unit | status |
|---|---|---|---|
| hydration_ml | daily | ml | ✅ |
| calories_consumed_kcal | daily | kcal | ○ |
| carbs_g / protein_g / fat_g | daily | g | ○ |
| food_log | entry | — | ○ |

### Reproductive health (manual-logged)
| key | grain | unit | status |
|---|---|---|---|
| menstrual_flow | daily/interval | — | ○ |
| ovulation_test | sample | — | ○ |
| pregnancy | interval | — | ○ |

### Wellness logs (manual-logged)
| key | grain | unit | status |
|---|---|---|---|
| mood | daily | scale | ○ |
| symptoms | daily | — | ○ |

## Source mapping (canonical ↔ vendor)

| canonical | Garmin (`garminconnect`) | Google Health (v4) |
|---|---|---|
| steps | `get_stats` totalSteps | `steps` |
| distance_m | `get_stats` totalDistanceMeters | `distance` |
| floors | `get_stats` floorsAscended | `floors` |
| active/vigorous minutes | `get_stats` moderate/vigorousIntensityMinutes | `active-minutes`, `active-zone-minutes` |
| active_kcal / total_kcal | `get_stats` active/totalKilocalories | `active-energy-burned`, `total-calories` |
| resting_hr_bpm | `get_stats` restingHeartRate | `daily-resting-heart-rate` |
| heart_rate (series) | `get_heart_rates` heartRateValues | `heart-rate` |
| hrv_ms | `get_hrv_data` hrvSummary.lastNightAvg | `daily-heart-rate-variability` |
| spo2_pct / spo2 | `get_spo2_data` averageSpO2 / hourly | `daily-oxygen-saturation` / `oxygen-saturation` |
| respiratory_rate / respiration | `get_respiration_data` avgWaking / valuesArray | `daily-respiratory-rate` |
| vo2_max | `get_training_status` mostRecentVO2Max.generic.vo2MaxValue | `daily-vo2-max` |
| weight_kg / body_fat_pct / bmi | `get_body_composition` totalAverage.* | `weight`, `body-fat` |
| hydration_ml | `get_hydration_data` valueInML | `hydration-log` |
| sleep (session) | `get_sleep_data` | `sleep` |
| workout (session) | `get_activities_by_date` | `exercise` |

## Excluded — vendor-proprietary (not cross-vendor, can't consolidate)

Garmin: Body Battery, stress score, training readiness, endurance/hill scores,
fitness age, training status/load, race predictions. These may live in a
vendor-namespaced extension later, but are not part of the core dataset.

## Growth

Adding a data point = one vocabulary entry (`app/metrics.py`) + a mapping in the
relevant plugin; the derive step, the access contract and the webhook pick it up
with no migration. Grain decides the table: daily → `metrics`, intraday → `samples`,
interval → `sessions`.
