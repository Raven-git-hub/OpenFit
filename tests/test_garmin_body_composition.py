"""
Garmin's remaining daily metrics - VO2 max, hydration and body
composition - with the network stubbed.

VO2 max and hydration are single-value daily fetches, so they ride the
GARMIN_VITALS loop: get_training_status -> the nested
mostRecentVO2Max.generic.vo2MaxValue as vo2_max, and get_hydration_data
-> valueInML as hydration_ml, each stored as reported. Body composition
is a range call (the day as both start and end) with several readings
in it, so it has its own fetch: get_body_composition's totalAverage ->
bodyFat as body_fat_pct, bmi as bmi, bodyWater as body_water_pct and
visceralFat as visceral_fat, as reported, and weight, muscleMass and
boneMass as weight_kg, muscle_mass_kg and bone_mass_kg, converted from
the grams Garmin reports them in. physiqueRating and metabolicAge beside
them are vendor-proprietary composites, never written (schema v2, law 5).

That unit is the one thing here that couldn't be checked against a real
account (every field probed was null - an account without a compatible
smart scale), so each converted mass is only written within its metric's
plausible range; outside it, nothing is written and the raw and
converted values are logged. As with the vitals, an empty or null answer
is a day with nothing to report - written as nothing, logged as nothing
- and a failed fetch costs only its own metrics.

No network: the Garmin client is a fake handed to sync(), as in
test_garmin_daily_vitals.py.
"""

from datetime import date

import pytest

from derived import recompute_derived
from metrics import (
    BMI,
    BODY_FAT_PCT,
    BODY_WATER_PCT,
    BONE_MASS_KG,
    HYDRATION_ML,
    MUSCLE_MASS_KG,
    VISCERAL_FAT,
    VO2_MAX,
    WEIGHT_KG,
)

DAY = "2026-01-02"


class FrozenDate(date):
    """date, with today pinned to the fixture day."""

    @classmethod
    def today(cls):
        return cls(2026, 1, 2)


# get_training_status() for the day, trimmed. vo2MaxValue is the figure;
# the precise value beside it and the training-status blocks around it
# differ, so a mix-up shows.
GARMIN_TRAINING_STATUS = {
    "mostRecentVO2Max": {
        "generic": {
            "calendarDate": "2025-12-30",
            "vo2MaxPreciseValue": 48.3,
            "vo2MaxValue": 48.0,
            "fitnessAge": None,
        },
        "cycling": None,
    },
    "mostRecentTrainingStatus": {"latestTrainingStatusData": {}},
}

# get_hydration_data() for the day, trimmed. The goal and sweat loss
# beside valueInML differ, so a mix-up shows.
GARMIN_HYDRATION = {
    "calendarDate": DAY,
    "valueInML": 1750.0,
    "goalInML": 2500.0,
    "sweatLossInML": 400.0,
    "activityIntakeInML": 0.0,
}

# get_body_composition(day, day), trimmed. totalAverage holds the day's
# figures - the masses in grams - beside the two composites nothing here
# files, and the window's from/until (epoch ms).
GARMIN_BODY_COMPOSITION = {
    "startDate": DAY,
    "endDate": DAY,
    "dateWeightList": [],
    "totalAverage": {
        "from": 1767312000000,
        "until": 1767398399999,
        "weight": 75000.0,
        "bmi": 24.1,
        "bodyFat": 22.4,
        "bodyWater": 55.1,
        "boneMass": 3100,
        "muscleMass": 33000,
        "physiqueRating": 5,
        "visceralFat": 9,
        "metabolicAge": 41,
    },
}

# get_body_composition(day, day) on an account without a compatible smart
# scale: the same shape, every reading null.
GARMIN_NO_SCALE = {
    "startDate": DAY,
    "endDate": DAY,
    "dateWeightList": [],
    "totalAverage": {
        "from": 1767312000000,
        "until": 1767398399999,
        "weight": None,
        "bmi": None,
        "bodyFat": None,
        "bodyWater": None,
        "boneMass": None,
        "muscleMass": None,
        "physiqueRating": None,
        "visceralFat": None,
        "metabolicAge": None,
    },
}

# Each totalAverage field read and the metric it files as.
BODY_FIELDS = {
    "weight": WEIGHT_KG,
    "bmi": BMI,
    "bodyFat": BODY_FAT_PCT,
    "bodyWater": BODY_WATER_PCT,
    "boneMass": BONE_MASS_KG,
    "muscleMass": MUSCLE_MASS_KG,
    "visceralFat": VISCERAL_FAT,
}

# The schema v2 additions among them.
V2_BODY_FIELDS = {
    field: BODY_FIELDS[field] for field in ("bodyWater", "boneMass", "muscleMass", "visceralFat")
}

ALL_PAYLOADS = {
    "training_status": GARMIN_TRAINING_STATUS,
    "hydration": GARMIN_HYDRATION,
    "body_composition": GARMIN_BODY_COMPOSITION,
}

# What ALL_PAYLOADS files: (metric, value, unit), by metric.
BODY_READINGS = [
    ("bmi", 24.1, "kg/m2"),
    ("body_fat_pct", 22.4, "%"),
    ("body_water_pct", 55.1, "%"),
    ("bone_mass_kg", 3.1, "kg"),
    ("muscle_mass_kg", 33.0, "kg"),
    ("visceral_fat", 9, "rating"),
    ("weight_kg", 75.0, "kg"),
]
VO2_READING = ("vo2_max", 48.0, "ml/kg/min")
HYDRATION_READING = ("hydration_ml", 1750.0, "ml")
ALL_READINGS = sorted(BODY_READINGS + [VO2_READING, HYDRATION_READING])

# Each fetch's metrics, and the line a failed one logs.
FETCH_METRICS = {
    "training_status": [VO2_MAX],
    "hydration": [HYDRATION_ML],
    "body_composition": list(BODY_FIELDS.values()),
}
FAILED_FETCH_LOG = {
    "training_status": f"VO2 max fetch failed for {DAY}",
    "hydration": f"hydration fetch failed for {DAY}",
    "body_composition": f"body-composition fetch failed for {DAY}",
}


def readings(conn, source="garmin"):
    return [
        tuple(r) for r in conn.execute(
            "SELECT metric, value, unit FROM metrics WHERE source = ? ORDER BY metric",
            (source,),
        )
    ]


def readings_by_date(conn, source="garmin"):
    return [
        tuple(r) for r in conn.execute(
            "SELECT date, metric, value FROM metrics WHERE source = ? ORDER BY date, metric",
            (source,),
        )
    ]


def without(rows, *metrics):
    return [row for row in rows if row[0] not in metrics]


def with_average(**fields):
    """GARMIN_BODY_COMPOSITION with totalAverage's fields replaced."""
    return {
        **GARMIN_BODY_COMPOSITION,
        "totalAverage": {**GARMIN_BODY_COMPOSITION["totalAverage"], **fields},
    }


class FakeGarmin:
    """Stands in for garminconnect.Garmin: the given training-status,
    hydration and body-composition responses - {} for one not given -
    with an exception given in place of a response raised instead.
    Hydration and body composition answer for the fixture day only, {}
    for any other; training status answers the same for every day, as
    the real one gives the most recent estimate whatever day is asked.
    No stats, sleep, vitals, heart rate or activities. Records every
    get_body_composition call's arguments."""

    def __init__(self, payloads):
        self.payloads = payloads
        self.body_calls = []

    def _payload(self, name, cdate=DAY):
        payload = self.payloads.get(name, {}) if cdate == DAY else {}
        if isinstance(payload, Exception):
            raise payload
        return payload

    def get_stats(self, cdate):
        return {}

    def get_sleep_data(self, cdate):
        return {"dailySleepDTO": {"calendarDate": cdate, "sleepTimeSeconds": None}}

    def get_spo2_data(self, cdate):
        return {}

    def get_respiration_data(self, cdate):
        return {}

    def get_hrv_data(self, cdate):
        return {}

    def get_training_status(self, cdate):
        return self._payload("training_status")

    def get_hydration_data(self, cdate):
        return self._payload("hydration", cdate)

    def get_body_composition(self, startdate, enddate=None):
        self.body_calls.append((startdate, enddate))
        return self._payload("body_composition", startdate)

    def get_heart_rates(self, cdate):
        return {"calendarDate": cdate, "heartRateValues": None}

    def get_activities_by_date(self, startdate, enddate=None):
        return []


@pytest.fixture
def garmin(monkeypatch):
    """The real Garmin plugin, syncing from a FakeGarmin on 2026-01-02.
    The fake from the latest sync is garmin.client."""
    from plugins import PLUGINS

    plugin = PLUGINS["garmin"]
    monkeypatch.setattr("plugins.garmin.plugin.date", FrozenDate)

    def sync_with(conn, payloads, days=1):
        sync_with.client = FakeGarmin(payloads)
        monkeypatch.setattr(plugin, "_get_client", lambda conn=None: sync_with.client)
        return plugin.sync(conn, days)

    return sync_with


def test_garmin_writes_vo2_max_hydration_and_body_composition(conn, garmin, capsys):
    assert garmin(conn, ALL_PAYLOADS) == 1

    assert readings(conn) == ALL_READINGS
    assert capsys.readouterr().out == ""


def test_vo2_max_is_read_from_the_generic_estimate(conn, garmin):
    """mostRecentVO2Max.generic.vo2MaxValue - not the precise value
    beside it - stored as reported."""
    garmin(conn, {"training_status": GARMIN_TRAINING_STATUS})

    assert readings(conn) == [VO2_READING]


def test_vo2_max_files_the_current_estimate_on_each_day_of_the_window(conn, garmin):
    """get_training_status answers with the most recent estimate whatever
    day is asked, so every day gets it."""
    garmin(conn, {"training_status": GARMIN_TRAINING_STATUS}, days=2)

    assert readings_by_date(conn) == [("2026-01-01", "vo2_max", 48.0), (DAY, "vo2_max", 48.0)]


def test_hydration_is_read_from_value_in_ml(conn, garmin):
    """valueInML - not the goal or sweat loss - stored as reported."""
    garmin(conn, {"hydration": GARMIN_HYDRATION})

    assert readings(conn) == [HYDRATION_READING]


def test_body_composition_files_each_reading_with_the_masses_in_kg(conn, garmin):
    """weight 75000, muscleMass 33000 and boneMass 3100 (grams) -> 75.0,
    33.0 and 3.1 kg; bodyFat, bmi, bodyWater and visceralFat as reported."""
    garmin(conn, {"body_composition": GARMIN_BODY_COMPOSITION})

    assert readings(conn) == BODY_READINGS


def test_physique_rating_and_metabolic_age_are_never_written(conn, garmin):
    """Vendor-proprietary composites (schema v2, law 5): not even a day
    holding nothing else lands."""
    only = {"totalAverage": {"physiqueRating": 5, "metabolicAge": 41}}

    assert garmin(conn, {"body_composition": only}) == 0

    assert readings(conn) == []


def test_body_composition_is_asked_for_the_day_as_start_and_end(conn, garmin):
    garmin(conn, ALL_PAYLOADS, days=2)

    assert garmin.client.body_calls == [(DAY, DAY), ("2026-01-01", "2026-01-01")]


@pytest.mark.parametrize(
    "field, grams, kg",
    [
        ("weight", 75, 0.075),
        ("weight", 750_000_000, 750_000.0),
        ("muscleMass", 33, 0.033),
        ("muscleMass", 330_000, 330.0),
        ("boneMass", 3.1, 0.0031),
        ("boneMass", 31_000, 31.0),
    ],
)
def test_a_mass_out_of_range_once_converted_is_logged_and_not_written(
    conn, garmin, capsys, field, grams, kg
):
    """The grams guess is guarded: a mass that converts to something no
    body holds - one already reported in kg, say - writes nothing, and the
    log says what came in and what it became, so the unit can be
    confirmed. The other readings still land."""
    garmin(conn, {"body_composition": with_average(**{field: grams})})

    assert readings(conn) == without(BODY_READINGS, BODY_FIELDS[field])
    assert (
        f"[garmin] body-composition {field} {grams}g -> {kg}kg out of plausible range, "
        f"not written ({DAY})"
    ) in capsys.readouterr().out


@pytest.mark.parametrize(
    "field, grams, kg",
    [
        ("weight", 20_000, 20.0),
        ("weight", 400_000, 400.0),
        ("muscleMass", 5_000, 5.0),
        ("muscleMass", 120_000, 120.0),
        ("boneMass", 500, 0.5),
        ("boneMass", 10_000, 10.0),
    ],
)
def test_a_mass_at_either_end_of_the_range_is_written(conn, garmin, field, grams, kg):
    garmin(conn, {"body_composition": with_average(**{field: grams})})

    assert (BODY_FIELDS[field], kg, "kg") in readings(conn)


@pytest.mark.parametrize("field, metric", sorted(BODY_FIELDS.items()))
@pytest.mark.parametrize("value", [None, "22.4", True, float("nan"), float("inf"), [22.4]])
def test_a_body_composition_field_that_is_not_a_number_is_skipped(
    conn, garmin, capsys, field, metric, value
):
    garmin(conn, {"body_composition": with_average(**{field: value})})

    assert readings(conn) == without(BODY_READINGS, metric)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "payload",
    [
        {},
        None,
        {"totalAverage": None},
        {"totalAverage": {}},
        {"totalAverage": {"weight": None, "bmi": None, "bodyFat": None}},
        {"totalAverage": [75000]},
        "75000",
        GARMIN_NO_SCALE,
    ],
)
def test_no_weigh_in_writes_nothing_and_logs_nothing(conn, garmin, capsys, payload):
    garmin(conn, {**ALL_PAYLOADS, "body_composition": payload})

    assert readings(conn) == [HYDRATION_READING, VO2_READING]
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "name, payload",
    [
        ("training_status", {}),
        ("training_status", None),
        ("training_status", {"mostRecentVO2Max": None}),
        ("training_status", {"mostRecentVO2Max": {"generic": None}}),
        ("training_status", {"mostRecentVO2Max": {"generic": {"vo2MaxValue": None}}}),
        ("training_status", {"mostRecentVO2Max": {"generic": {"vo2MaxValue": "48"}}}),
        ("hydration", {}),
        ("hydration", None),
        ("hydration", {"valueInML": None}),
        ("hydration", {"valueInML": True}),
    ],
)
def test_an_empty_or_unreadable_vo2_max_or_hydration_writes_nothing(
    conn, garmin, capsys, name, payload
):
    garmin(conn, {**ALL_PAYLOADS, name: payload})

    assert readings(conn) == without(ALL_READINGS, *FETCH_METRICS[name])
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("name", sorted(FETCH_METRICS))
def test_a_failed_fetch_costs_only_its_own_metrics(conn, garmin, capsys, name):
    payloads = {**ALL_PAYLOADS, name: RuntimeError("503 Service Unavailable")}

    assert garmin(conn, payloads) == 1

    assert readings(conn) == without(ALL_READINGS, *FETCH_METRICS[name])
    assert FAILED_FETCH_LOG[name] in capsys.readouterr().out


@pytest.mark.parametrize("name", sorted(FETCH_METRICS))
def test_a_day_whose_only_data_is_one_of_these_still_lands(conn, garmin, name):
    """Not dropped by the no-steps/resting-HR/sleep/stats/vitals skip."""
    assert garmin(conn, {name: ALL_PAYLOADS[name]}) == 1

    assert sorted(m for m, _, _ in readings(conn)) == sorted(FETCH_METRICS[name])


@pytest.mark.parametrize("field", sorted(V2_BODY_FIELDS))
def test_a_day_whose_only_data_is_one_new_body_reading_still_lands(conn, garmin, field):
    """Not dropped by the day's skip: a weigh-in that reports only this
    one reading still files it."""
    only = {"totalAverage": {field: GARMIN_BODY_COMPOSITION["totalAverage"][field]}}

    assert garmin(conn, {"body_composition": only}) == 1

    assert readings(conn) == [r for r in BODY_READINGS if r[0] == V2_BODY_FIELDS[field]]


def test_a_day_whose_fetches_all_fail_is_still_skipped(conn, garmin, capsys):
    error = RuntimeError("503 Service Unavailable")

    assert garmin(conn, {name: error for name in FETCH_METRICS}) == 0

    assert readings(conn) == []
    out = capsys.readouterr().out
    for line in FAILED_FETCH_LOG.values():
        assert line in out


def test_a_resync_replaces_the_days_readings(conn, garmin):
    garmin(conn, {**ALL_PAYLOADS, "body_composition": with_average(weight=76500.0, bodyFat=23.0)})
    garmin(conn, ALL_PAYLOADS)

    assert readings(conn) == ALL_READINGS


def test_garmin_weight_sits_beside_manual_weight_and_wins_the_pick(client, conn, garmin):
    """weight_kg already exists, from hand entry; Garmin is a second source
    of it, and the default precedence puts Garmin first."""
    client.post("/api/weights", json={"date": DAY, "weight": 80.0})
    garmin(conn, {"body_composition": GARMIN_BODY_COMPOSITION})
    recompute_derived(conn)

    assert readings(conn, source="manual") == [("weight_kg", 80.0, "kg")]
    assert client.get("/api/metric/weight_kg").get_json()["points"] == [
        {"date": DAY, "value": 75.0, "source": "garmin"}
    ]


def test_the_new_metrics_flow_through_derive_and_the_access_contract(client, conn, garmin):
    garmin(conn, ALL_PAYLOADS)
    recompute_derived(conn)

    catalog = {m["metric"]: m for m in client.get("/api/catalog").get_json()["metrics"]}
    for metrics in FETCH_METRICS.values():
        for metric in metrics:
            assert catalog[metric]["sources"] == ["garmin"]

    assert client.get("/api/metric/vo2_max").get_json() == {
        "metric": "vo2_max",
        "unit": "ml/kg/min",
        "points": [{"date": DAY, "value": 48.0, "source": "garmin"}],
    }
    assert client.get("/api/metric/muscle_mass_kg").get_json() == {
        "metric": "muscle_mass_kg",
        "unit": "kg",
        "points": [{"date": DAY, "value": 33.0, "source": "garmin"}],
    }
