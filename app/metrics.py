"""
The canonical metric vocabulary.

Every reading in the metrics table is filed under one of these keys, in
the unit registered for it here. Plugins and the API import the keys
from this module rather than spelling the strings out, so a device's
"restingHeartRate" and another's "rhr" both land as resting_hr_bpm and
mean the same thing.

Adding a metric is adding an entry below - the metrics table is tidy, so
there is no schema change. Keys carry their unit when it isn't obvious
(resting_hr_bpm, sleep_minutes), and each key has exactly one unit:
sources convert on the way in.
"""

STEPS = "steps"
RESTING_HR_BPM = "resting_hr_bpm"
SLEEP_MINUTES = "sleep_minutes"

# Canonical key -> the unit its value is stored in.
METRICS = {
    STEPS: "count",
    RESTING_HR_BPM: "bpm",
    SLEEP_MINUTES: "min",
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
