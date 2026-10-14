"""
The canonical metric vocabulary (schema v2), and the series metrics and
session kinds beside it.

Every reading in the metrics table is filed under one of these keys, in
the unit registered for it here. Plugins and the API import the keys
from this module rather than spelling the strings out, so a device's
"restingHeartRate" and another's "rhr" both land as resting_hr_bpm and
mean the same thing.

Each metric carries four things (METRIC_DEFS): its unit, its category -
the semantic group it files under (CATEGORIES), whatever source supplies
it - its grain - the shape of its data, "daily" or "instant" - and its
plausible range. Adding a metric is adding an entry - the metrics table
is tidy, so there is no schema change. Keys carry their unit when it
isn't obvious (resting_hr_bpm, weight_kg), and each key has exactly one
unit: sources convert on the way in. The design - the laws a key follows,
the grains and categories, and the shapes still deferred - is
docs/architecture/metric-schema-v2.md.

Series metrics - intraday samples, in the samples table - and session
kinds are governed the same way: a key here, so a new one is an entry in
SERIES_METRICS or SESSION_KINDS rather than a migration. grain_for() and
category_for() (at the end) answer for any of these keys. cadence_for()
and MANUAL_METRICS, after them, are the pre-v2 labels, kept as a
deprecated shim until the console moves to grain and category.
"""

# ---- grains ----
#
# The shape a key's data takes. A metric is DAILY - one figure per day -
# or INSTANT - a single timestamped point, like a weigh-in. INSTANT is a
# label for now: its readings are still filed one per day in the metrics
# table, there is no table of its own. SESSION and SERIES are the grains
# of the session kinds and series metrics below.
DAILY = "daily"
INSTANT = "instant"
SESSION = "session"
SERIES = "series"

GRAINS = (DAILY, INSTANT, SESSION, SERIES)

# ---- categories ----
#
# The semantic groups, in display order. A category is metadata a key
# carries, independent of its source; one with no keys yet (Women's
# health) is still listed. An app's own metric files under whichever
# fits, with Other the catch-all.
CATEGORIES = (
    "Activity",
    "Vitals",
    "Body composition",
    "Fitness",
    "Sleep",
    "Nutrition & hydration",
    "Women's health",
    "Other",
)


# ---- metrics (daily and instant) ----

# Activity
STEPS = "steps"
DISTANCE_M = "distance_m"
FLOORS = "floors"
ACTIVE_KCAL = "active_kcal"
TOTAL_KCAL = "total_kcal"
BASAL_KCAL = "basal_kcal"
SEDENTARY_MIN = "sedentary_min"
ELEVATION_GAIN_M = "elevation_gain_m"
INTENSITY_MODERATE_MIN = "intensity_moderate_min"
INTENSITY_VIGOROUS_MIN = "intensity_vigorous_min"
ACTIVE_MINUTES = "active_minutes"
# Vitals
RESTING_HR_BPM = "resting_hr_bpm"
HRV_MS = "hrv_ms"
SPO2_PCT = "spo2_pct"
RESPIRATORY_RATE_BRPM = "respiratory_rate_brpm"
SKIN_TEMPERATURE = "skin_temperature"
BODY_TEMPERATURE = "body_temperature"
BLOOD_PRESSURE_SYSTOLIC = "blood_pressure_systolic"
BLOOD_PRESSURE_DIASTOLIC = "blood_pressure_diastolic"
BLOOD_GLUCOSE = "blood_glucose"
# Body composition
WEIGHT_KG = "weight_kg"
BODY_FAT_PCT = "body_fat_pct"
BMI = "bmi"
HEIGHT_CM = "height_cm"
MUSCLE_MASS_KG = "muscle_mass_kg"
BONE_MASS_KG = "bone_mass_kg"
BODY_WATER_PCT = "body_water_pct"
VISCERAL_FAT = "visceral_fat"
# Fitness
VO2_MAX = "vo2_max"
# Nutrition & hydration
HYDRATION_ML = "hydration_ml"
CALORIES_IN = "calories_in"
CARBS_G = "carbs_g"
PROTEIN_G = "protein_g"
FAT_G = "fat_g"
# Other
MOOD = "mood"

# Canonical key -> (unit, category, grain, plausible (low, high)).
#
# The range is what a reading can plausibly be, in its unit, both ends
# inclusive (see PLAUSIBLE_RANGES). blood_glucose is in mmol/L - a source
# reporting mg/dL converts - and is INSTANT until a CGM source makes it a
# series. skin_temperature is absolute; visceral_fat is a unitless rating.
METRIC_DEFS = {
    STEPS:                    ("count",     "Activity",              DAILY,   (0, 100_000)),
    DISTANCE_M:               ("m",         "Activity",              DAILY,   (0, 300_000)),
    FLOORS:                   ("count",     "Activity",              DAILY,   (0, 1000)),
    ACTIVE_KCAL:              ("kcal",      "Activity",              DAILY,   (0, 15_000)),
    TOTAL_KCAL:               ("kcal",      "Activity",              DAILY,   (0, 20_000)),
    BASAL_KCAL:               ("kcal",      "Activity",              DAILY,   (0, 5000)),
    SEDENTARY_MIN:            ("min",       "Activity",              DAILY,   (0, 1440)),
    ELEVATION_GAIN_M:         ("m",         "Activity",              DAILY,   (0, 20_000)),
    INTENSITY_MODERATE_MIN:   ("min",       "Activity",              DAILY,   (0, 1440)),
    INTENSITY_VIGOROUS_MIN:   ("min",       "Activity",              DAILY,   (0, 1440)),
    ACTIVE_MINUTES:           ("min",       "Activity",              DAILY,   (0, 1440)),
    RESTING_HR_BPM:           ("bpm",       "Vitals",                DAILY,   (20, 200)),
    HRV_MS:                   ("ms",        "Vitals",                DAILY,   (0, 500)),
    SPO2_PCT:                 ("%",         "Vitals",                DAILY,   (50, 100)),
    RESPIRATORY_RATE_BRPM:    ("brpm",      "Vitals",                DAILY,   (3, 60)),
    SKIN_TEMPERATURE:         ("°C",        "Vitals",                DAILY,   (20, 45)),
    BODY_TEMPERATURE:         ("°C",        "Vitals",                INSTANT, (30, 45)),
    BLOOD_PRESSURE_SYSTOLIC:  ("mmHg",      "Vitals",                INSTANT, (50, 250)),
    BLOOD_PRESSURE_DIASTOLIC: ("mmHg",      "Vitals",                INSTANT, (30, 150)),
    BLOOD_GLUCOSE:            ("mmol/L",    "Vitals",                INSTANT, (1, 40)),
    WEIGHT_KG:                ("kg",        "Body composition",      INSTANT, (20, 400)),
    BODY_FAT_PCT:             ("%",         "Body composition",      INSTANT, (2, 70)),
    BMI:                      ("kg/m2",     "Body composition",      INSTANT, (8, 100)),
    HEIGHT_CM:                ("cm",        "Body composition",      INSTANT, (50, 260)),
    MUSCLE_MASS_KG:           ("kg",        "Body composition",      INSTANT, (5, 120)),
    BONE_MASS_KG:             ("kg",        "Body composition",      INSTANT, (0.5, 10)),
    BODY_WATER_PCT:           ("%",         "Body composition",      INSTANT, (20, 80)),
    VISCERAL_FAT:             ("rating",    "Body composition",      INSTANT, (1, 60)),
    VO2_MAX:                  ("ml/kg/min", "Fitness",               DAILY,   (10, 90)),
    HYDRATION_ML:             ("ml",        "Nutrition & hydration", DAILY,   (0, 20_000)),
    CALORIES_IN:              ("kcal",      "Nutrition & hydration", DAILY,   (0, 15_000)),
    CARBS_G:                  ("g",         "Nutrition & hydration", DAILY,   (0, 2000)),
    PROTEIN_G:                ("g",         "Nutrition & hydration", DAILY,   (0, 1000)),
    FAT_G:                    ("g",         "Nutrition & hydration", DAILY,   (0, 1000)),
    MOOD:                     ("scale",     "Other",                 INSTANT, (1, 5)),
}

# Canonical key -> the unit its value is stored in. In METRIC_DEFS order,
# which is the order the API lists metrics in.
METRICS = {metric: unit for metric, (unit, _, _, _) in METRIC_DEFS.items()}


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
PLAUSIBLE_RANGES = {metric: bounds for metric, (_, _, _, bounds) in METRIC_DEFS.items()}


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

# Series metric -> its category.
SERIES_CATEGORIES = {
    HEART_RATE: "Vitals",
    RESPIRATION: "Vitals",
    SPO2: "Vitals",
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
# Sleep is sessions only: there is no daily sleep metric (sleep_minutes
# was retired in schema v2, migration 010) - a night's total is its
# session's asleep_minutes.
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

# Session kind -> its category.
SESSION_CATEGORIES = {
    SLEEP: "Sleep",
    WORKOUT: "Activity",
}


# ---- grain and category, for any key ----


def grain_for(key):
    """The grain of a metric, series metric or session kind.

    "series" for a series metric, "session" for a session kind, else the
    metric's declared grain - "daily" or "instant". Raises ValueError for
    a key in none of the vocabularies.
    """
    if key in SERIES_METRICS:
        return SERIES
    if key in SESSION_KINDS:
        return SESSION
    try:
        return METRIC_DEFS[key][2]
    except KeyError:
        raise ValueError(f"unknown metric: {key!r}") from None


def category_for(key):
    """The category (one of CATEGORIES) of a metric, series metric or session kind.

    Raises ValueError for a key in none of the vocabularies.
    """
    if key in SERIES_CATEGORIES:
        return SERIES_CATEGORIES[key]
    if key in SESSION_CATEGORIES:
        return SESSION_CATEGORIES[key]
    try:
        return METRIC_DEFS[key][1]
    except KeyError:
        raise ValueError(f"unknown metric: {key!r}") from None


# ---- cadence (deprecated shim) ----
#
# Pre-v2, and kept only for the console, which still groups its rows by
# these labels until it moves to grain and category - that change
# removes MANUAL_METRICS and cadence_for(). Use grain_for() for anything
# new. Unchanged from before v2: "manual" is not a grain - hand entry is
# a source, not a shape - so a weigh-in's grain is "instant" while its
# cadence here still reads "manual".
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
    """How a metric, series metric or session kind is collected. Deprecated: see above.

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
