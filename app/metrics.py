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
one is an entry in SESSION_KINDS rather than a migration.
"""

STEPS = "steps"
RESTING_HR_BPM = "resting_hr_bpm"
SLEEP_MINUTES = "sleep_minutes"
WEIGHT_KG = "weight_kg"

# Canonical key -> the unit its value is stored in.
METRICS = {
    STEPS: "count",
    RESTING_HR_BPM: "bpm",
    SLEEP_MINUTES: "min",
    WEIGHT_KG: "kg",
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
}


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
