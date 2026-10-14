"""
The canonical metric vocabulary, and the session kinds beside it.

Every reading in the metrics table is filed under one of these keys, in
the unit registered for it here. Plugins and the API import the keys
from this module rather than spelling the strings out, so a device's
"restingHeartRate" and another's "rhr" both land as resting_hr_bpm and
mean the same thing.

Adding a metric is adding an entry below - the metrics table is tidy, so
there is no schema change. Keys carry their unit when it isn't obvious
(resting_hr_bpm, sleep_minutes, weight_kg), and each key has exactly one
unit: sources convert on the way in.

Session kinds (the bottom of this module) are governed the same way: a
kind is a constant here, and the sessions table holds any kind, so a new
one is an entry in SESSION_KINDS rather than a migration. So are series
metrics - intraday samples, in the samples table - in SERIES_METRICS.
cadence_for() (at the end) labels any of these keys by how it's collected.
"""

STEPS = "steps"
RESTING_HR_BPM = "resting_hr_bpm"
SLEEP_MINUTES = "sleep_minutes"
WEIGHT_KG = "weight_kg"
DISTANCE_M = "distance_m"
ACTIVE_KCAL = "active_kcal"
TOTAL_KCAL = "total_kcal"
FLOORS = "floors"
INTENSITY_MODERATE_MIN = "intensity_moderate_min"
INTENSITY_VIGOROUS_MIN = "intensity_vigorous_min"
SPO2_PCT = "spo2_pct"
RESPIRATORY_RATE_BRPM = "respiratory_rate_brpm"
HRV_MS = "hrv_ms"
VO2_MAX = "vo2_max"
BODY_FAT_PCT = "body_fat_pct"
BMI = "bmi"
HYDRATION_ML = "hydration_ml"

# Canonical key -> the unit its value is stored in.
METRICS = {
    STEPS: "count",
    RESTING_HR_BPM: "bpm",
    SLEEP_MINUTES: "min",
    WEIGHT_KG: "kg",
    DISTANCE_M: "m",
    ACTIVE_KCAL: "kcal",
    TOTAL_KCAL: "kcal",
    FLOORS: "count",
    INTENSITY_MODERATE_MIN: "min",
    INTENSITY_VIGOROUS_MIN: "min",
    SPO2_PCT: "%",
    RESPIRATORY_RATE_BRPM: "brpm",
    HRV_MS: "ms",
    VO2_MAX: "ml/kg/min",
    BODY_FAT_PCT: "%",
    BMI: "kg/m2",
    HYDRATION_ML: "ml",
}


def unit_for(metric):
    """The unit a canonical metric is stored in.

    Raises ValueError for a key outside the vocabulary, so a typo in a
    plugin fails its sync loudly instead of filing readings under a
    metric nothing will ever read.
    """
    try:
        return METRICS[metric]
    except KeyError:
        raise ValueError(f"unknown metric: {metric!r}") from None


# Canonical key -> the (low, high) a reading of it can plausibly be, in
# its unit, both ends inclusive. A pushed reading (the webhook in main.py)
# outside its metric's range is refused rather than stored, so a misfired
# automation - a sensor sending 0, a scale reporting grams - can't file
# nonsense as a reading. Deliberately wide: these catch garbage, not
# unusual days. A metric with no entry accepts any number.
PLAUSIBLE_RANGES = {
    STEPS: (0, 200_000),
    RESTING_HR_BPM: (20, 250),
    SLEEP_MINUTES: (0, 1440),
    WEIGHT_KG: (20, 400),
    DISTANCE_M: (0, 500_000),
    ACTIVE_KCAL: (0, 30_000),
    TOTAL_KCAL: (0, 30_000),
    FLOORS: (0, 10_000),
    INTENSITY_MODERATE_MIN: (0, 1440),
    INTENSITY_VIGOROUS_MIN: (0, 1440),
    SPO2_PCT: (50, 100),
    RESPIRATORY_RATE_BRPM: (3, 60),
    HRV_MS: (0, 500),
    VO2_MAX: (10, 90),
    BODY_FAT_PCT: (2, 70),
    BMI: (8, 80),
    HYDRATION_ML: (0, 20_000),
}


# ---- series metrics (intraday samples) ----
# Stored in the `samples` table, NOT the daily `metrics` table. Keys are
# distinct from daily metrics even where the quantity overlaps: the heart_rate
# sample stream is a different thing from the resting_hr_bpm daily summary,
# as respiration is from respiratory_rate_brpm and spo2 from spo2_pct.
HEART_RATE = "heart_rate"
RESPIRATION = "respiration"
SPO2 = "spo2"

SERIES_METRICS = {
    HEART_RATE: "bpm",
    RESPIRATION: "brpm",
    SPO2: "%",
}


def series_unit_for(metric):
    """The unit for a series metric, or ValueError if it isn't one."""
    try:
        return SERIES_METRICS[metric]
    except KeyError:
        raise ValueError(f"unknown series metric: {metric!r}") from None


# ---- session kinds ----
#
# A session is an interval with a shape - a night's sleep, a workout -
# where a metric is one number per day (see write_session() in
# plugins/base.py). Its kind is a key from here, like a metric's, and
# write_session() refuses one outside SESSION_KINDS.
#
# Each kind's summary_json keys, in canonical units, for reference. A
# source leaves out any it doesn't report:
#
#   sleep    asleep_minutes, light_minutes, deep_minutes, rem_minutes,
#            awake_minutes - all whole minutes
#   workout  type (the source's activity type, lowercased, e.g.
#            "running"), duration_minutes (int, excluding pauses),
#            distance_m (float, metres), avg_hr_bpm (int),
#            calories_kcal (int)

SLEEP = "sleep"
WORKOUT = "workout"

SESSION_KINDS = {SLEEP, WORKOUT}


# ---- cadence ----
#
# How a key is collected, as a console labels it: "reading" for a series
# metric (sampled through the day), "session" for a session kind (one per
# interval), "manual" for a daily metric whose expected input is a scale
# or hand entry, and "daily" for any other daily metric (a device's
# once-a-day figure). A label, not a rule: nothing is stored or picked by
# it, and a device may still supply a manual metric - Garmin's weight_kg
# sits beside the manual one.

MANUAL_METRICS = {WEIGHT_KG, BODY_FAT_PCT, BMI, HYDRATION_ML}


def cadence_for(key):
    """How a metric, series metric or session kind is collected.

    One of "reading", "session", "manual" or "daily" (see above). Raises
    ValueError for a key in none of the vocabularies.
    """
    if key in SERIES_METRICS:
        return "reading"
    if key in SESSION_KINDS:
        return "session"
    # Before the plain daily fallback: every manual metric is in METRICS too.
    if key in MANUAL_METRICS:
        return "manual"
    if key in METRICS:
        return "daily"
    raise ValueError(f"unknown metric: {key!r}")
