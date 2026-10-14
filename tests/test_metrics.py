"""
The canonical vocabulary, schema v2 (app/metrics.py): every metric with
its unit, category, grain and plausible range; grain_for() and
category_for() over metrics, series metrics and session kinds alike;
and sleep_minutes retired - sleep is sessions only. The pre-v2 cadence
label is gone: the catalog and the console use grain and category.
"""

import pytest

from metrics import (
    BLOOD_GLUCOSE,
    BODY_TEMPERATURE,
    CATEGORIES,
    GRAINS,
    HEART_RATE,
    HYDRATION_ML,
    METRIC_DEFS,
    METRICS,
    MOOD,
    PLAUSIBLE_RANGES,
    RESPIRATION,
    SERIES_METRICS,
    SESSION_KINDS,
    SKIN_TEMPERATURE,
    SLEEP,
    SPO2,
    STEPS,
    VISCERAL_FAT,
    VO2_MAX,
    WEIGHT_KG,
    WORKOUT,
    category_for,
    grain_for,
    unit_for,
)

# The v2 table (docs/architecture/metric-schema-v2.md), in vocabulary
# order: key -> (unit, category, grain, plausible range).
V2_TABLE = {
    "steps": ("count", "Activity", "daily", (0, 100_000)),
    "distance_m": ("m", "Activity", "daily", (0, 300_000)),
    "floors": ("count", "Activity", "daily", (0, 1000)),
    "active_kcal": ("kcal", "Activity", "daily", (0, 15_000)),
    "total_kcal": ("kcal", "Activity", "daily", (0, 20_000)),
    "basal_kcal": ("kcal", "Activity", "daily", (0, 5000)),
    "sedentary_min": ("min", "Activity", "daily", (0, 1440)),
    "elevation_gain_m": ("m", "Activity", "daily", (0, 20_000)),
    "intensity_moderate_min": ("min", "Activity", "daily", (0, 1440)),
    "intensity_vigorous_min": ("min", "Activity", "daily", (0, 1440)),
    "active_minutes": ("min", "Activity", "daily", (0, 1440)),
    "resting_hr_bpm": ("bpm", "Vitals", "daily", (20, 200)),
    "hrv_ms": ("ms", "Vitals", "daily", (0, 500)),
    "spo2_pct": ("%", "Vitals", "daily", (50, 100)),
    "respiratory_rate_brpm": ("brpm", "Vitals", "daily", (3, 60)),
    "skin_temperature": ("°C", "Vitals", "daily", (20, 45)),
    "body_temperature": ("°C", "Vitals", "instant", (30, 45)),
    "blood_pressure_systolic": ("mmHg", "Vitals", "instant", (50, 250)),
    "blood_pressure_diastolic": ("mmHg", "Vitals", "instant", (30, 150)),
    "blood_glucose": ("mmol/L", "Vitals", "instant", (1, 40)),
    "weight_kg": ("kg", "Body composition", "instant", (20, 400)),
    "body_fat_pct": ("%", "Body composition", "instant", (2, 70)),
    "bmi": ("kg/m2", "Body composition", "instant", (8, 100)),
    "height_cm": ("cm", "Body composition", "instant", (50, 260)),
    "muscle_mass_kg": ("kg", "Body composition", "instant", (5, 120)),
    "bone_mass_kg": ("kg", "Body composition", "instant", (0.5, 10)),
    "body_water_pct": ("%", "Body composition", "instant", (20, 80)),
    "visceral_fat": ("rating", "Body composition", "instant", (1, 60)),
    "vo2_max": ("ml/kg/min", "Fitness", "daily", (10, 90)),
    "hydration_ml": ("ml", "Nutrition & hydration", "daily", (0, 20_000)),
    "calories_in": ("kcal", "Nutrition & hydration", "daily", (0, 15_000)),
    "carbs_g": ("g", "Nutrition & hydration", "daily", (0, 2000)),
    "protein_g": ("g", "Nutrition & hydration", "daily", (0, 1000)),
    "fat_g": ("g", "Nutrition & hydration", "daily", (0, 1000)),
    "mood": ("scale", "Other", "instant", (1, 5)),
}


# ---------- the metric table ----------


def test_the_vocabulary_is_the_v2_table():
    assert list(METRICS) == list(V2_TABLE)
    for metric, (unit, category, grain, bounds) in V2_TABLE.items():
        assert METRIC_DEFS[metric] == (unit, category, grain, bounds), metric
        assert unit_for(metric) == METRICS[metric] == unit
        assert category_for(metric) == category
        assert grain_for(metric) == grain
        assert PLAUSIBLE_RANGES[metric] == bounds


def test_every_metric_has_a_unit_category_grain_and_range():
    assert set(PLAUSIBLE_RANGES) == set(METRICS) == set(METRIC_DEFS)
    for metric in METRICS:
        assert unit_for(metric)
        assert category_for(metric) in CATEGORIES
        # Daily or instant: session and series are the other tables' grains.
        assert grain_for(metric) in ("daily", "instant")
        low, high = PLAUSIBLE_RANGES[metric]
        assert low < high


def test_the_categories_in_display_order():
    assert CATEGORIES == (
        "Activity",
        "Vitals",
        "Body composition",
        "Fitness",
        "Sleep",
        "Nutrition & hydration",
        "Women's health",
        "Other",
    )


def test_womens_health_is_declared_with_no_keys_yet():
    """Its keys are event-shaped and deferred - the category waits for them."""
    every_key = [*METRICS, *SERIES_METRICS, *SESSION_KINDS]
    assert "Women's health" not in {category_for(key) for key in every_key}


def test_the_grains():
    assert GRAINS == ("daily", "instant", "session", "series")


# ---------- sleep is sessions only ----------


def test_sleep_minutes_is_retired():
    assert "sleep_minutes" not in METRICS
    assert "sleep_minutes" not in PLAUSIBLE_RANGES
    for lookup in (unit_for, grain_for, category_for):
        with pytest.raises(ValueError, match="unknown metric"):
            lookup("sleep_minutes")


def test_sleep_lives_in_the_sleep_category_as_a_session():
    assert grain_for(SLEEP) == "session"
    assert category_for(SLEEP) == "Sleep"


# ---------- grain_for and category_for, every vocabulary ----------


@pytest.mark.parametrize(
    "key, grain, category",
    [
        (STEPS, "daily", "Activity"),
        (SKIN_TEMPERATURE, "daily", "Vitals"),
        (BODY_TEMPERATURE, "instant", "Vitals"),
        (BLOOD_GLUCOSE, "instant", "Vitals"),
        (WEIGHT_KG, "instant", "Body composition"),
        (VISCERAL_FAT, "instant", "Body composition"),
        (VO2_MAX, "daily", "Fitness"),
        (HYDRATION_ML, "daily", "Nutrition & hydration"),
        (MOOD, "instant", "Other"),
        (HEART_RATE, "series", "Vitals"),
        (RESPIRATION, "series", "Vitals"),
        (SPO2, "series", "Vitals"),
        (SLEEP, "session", "Sleep"),
        (WORKOUT, "session", "Activity"),
    ],
)
def test_grain_and_category_for(key, grain, category):
    assert grain_for(key) == grain
    assert category_for(key) == category


def test_every_key_in_every_vocabulary_has_a_grain_and_category():
    for key in [*METRICS, *SERIES_METRICS, *SESSION_KINDS]:
        assert grain_for(key) in GRAINS
        assert category_for(key) in CATEGORIES


def test_the_vocabularies_do_not_overlap():
    # A key names one thing: grain_for() never has to choose.
    assert not set(METRICS) & set(SERIES_METRICS)
    assert not set(METRICS) & SESSION_KINDS
    assert not set(SERIES_METRICS) & SESSION_KINDS


@pytest.mark.parametrize(
    "key",
    [
        "heartrate", "nap", "",
        # Deferred until their shape is decided: reserved, not declared.
        "hr_zone_minutes", "ecg", "symptoms", "menstrual_flow", "cycle_day",
    ],
)
def test_an_unknown_key_raises(key):
    for lookup in (grain_for, category_for):
        with pytest.raises(ValueError, match="unknown metric"):
            lookup(key)
