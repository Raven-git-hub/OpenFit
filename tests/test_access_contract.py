"""
The access contract: the read API apps build on.

GET /api/catalog says what there is (each key labelled with its grain,
category and cadence), GET /api/coverage which days hold it, GET
/api/metric/<metric> serves one metric's derived series, GET
/api/sessions/<kind> one kind's sessions (by source priority) and GET /api/series/<metric>
one series metric's intraday samples, each with its provenance. Metrics
come from derived_metrics - the picked value - never the raw per-source
rows; samples have no pick yet, so every source's are served. Also here: manual /api/weights writes
re-derive their days, so a weigh-in reaches the contract at once.
"""

from datetime import datetime, timedelta, timezone

import json

import pytest

from derived import SOURCE_ROLES_KEY, recompute_derived, set_source_role
from metrics import (
    ACTIVE_KCAL,
    BLOOD_GLUCOSE,
    BMI,
    BODY_FAT_PCT,
    DISTANCE_M,
    FLOORS,
    HEART_RATE,
    HRV_MS,
    HYDRATION_ML,
    INTENSITY_MODERATE_MIN,
    INTENSITY_VIGOROUS_MIN,
    MANUAL_METRICS,
    METRICS,
    MOOD,
    RESPIRATION,
    RESPIRATORY_RATE_BRPM,
    RESTING_HR_BPM,
    SERIES_METRICS,
    SESSION_KINDS,
    SLEEP,
    SPO2,
    SPO2_PCT,
    STEPS,
    TOTAL_KCAL,
    VO2_MAX,
    WEIGHT_KG,
    WORKOUT,
    cadence_for,
    category_for,
    grain_for,
)
from plugins.base import iso_utc, write_sample, write_session


def insert(conn, date, source, **values):
    """Seed one source's readings for a day, keyed by canonical metric."""
    for metric, value in values.items():
        conn.execute(
            "INSERT INTO metrics (date, source, metric, value, unit, synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (date, source, metric, value, METRICS[metric], "2026-01-01 06:00:00"),
        )
    conn.commit()


def session(conn, source, kind, start, end, summary):
    write_session(conn, source, kind, start, end, summary)
    conn.commit()


def sample(conn, source, ts, value, metric=HEART_RATE):
    write_sample(conn, source, metric, ts, value)
    conn.commit()


def series(client, metric, query=""):
    resp = client.get(f"/api/metric/{metric}{query}")
    assert resp.status_code == 200
    return resp.get_json()


def samples(client, metric, query=""):
    resp = client.get(f"/api/series/{metric}{query}")
    assert resp.status_code == 200
    return resp.get_json()


def sessions(client, kind, query=""):
    resp = client.get(f"/api/sessions/{kind}{query}")
    assert resp.status_code == 200
    return resp.get_json()


def picked(conn, date, metric):
    """(value, source) derived for one (date, metric), or None."""
    row = conn.execute(
        "SELECT value, source FROM derived_metrics WHERE date = ? AND metric = ?",
        (date, metric),
    ).fetchone()
    return tuple(row) if row else None


def assert_rejected(resp, status):
    assert resp.status_code == status
    body = resp.get_json()
    assert body["ok"] is False
    assert body["error"]


# ---------- the catalog ----------


def test_catalog_of_an_empty_install(client):
    assert client.get("/api/catalog").get_json() == {"metrics": [], "sessions": [], "series": []}


def test_catalog_lists_each_metric_with_data(client, conn):
    insert(conn, "2026-01-01", "garmin", steps=9000, resting_hr_bpm=52)
    insert(conn, "2026-01-02", "garmin", steps=8000)
    insert(conn, "2026-01-03", "google_health", steps=7000)
    insert(conn, "2026-01-02", "manual", weight_kg=82.5)
    recompute_derived(conn)

    catalog = client.get("/api/catalog").get_json()

    # Vocabulary order; a metric with no data isn't listed.
    assert catalog["metrics"] == [
        {
            "metric": STEPS,
            "unit": "count",
            "grain": "daily",
            "category": "Activity",
            "cadence": "daily",
            "sources": ["garmin", "google_health"],
            "first": "2026-01-01",
            "last": "2026-01-03",
            "count": 3,
            "last_value": 7000,
            "last_date": "2026-01-03",
        },
        {
            "metric": RESTING_HR_BPM,
            "unit": "bpm",
            "grain": "daily",
            "category": "Vitals",
            "cadence": "daily",
            "sources": ["garmin"],
            "first": "2026-01-01",
            "last": "2026-01-01",
            "count": 1,
            "last_value": 52,
            "last_date": "2026-01-01",
        },
        {
            "metric": WEIGHT_KG,
            "unit": "kg",
            # Instant in v2, while the deprecated cadence keeps its old label.
            "grain": "instant",
            "category": "Body composition",
            "cadence": "manual",
            "sources": ["manual"],
            "first": "2026-01-02",
            "last": "2026-01-02",
            "count": 1,
            "last_value": 82.5,
            "last_date": "2026-01-02",
        },
    ]
    assert catalog["sessions"] == []
    assert catalog["series"] == []


def test_catalog_sources_are_the_winners_not_every_reporter(client, conn):
    # Google reported steps every day, but Garmin won each one.
    for d in ("2026-01-01", "2026-01-02"):
        insert(conn, d, "garmin", steps=9000)
        insert(conn, d, "google_health", steps=8000)
    recompute_derived(conn)

    [steps] = client.get("/api/catalog").get_json()["metrics"]
    assert steps["sources"] == ["garmin"]
    assert steps["count"] == 2


def test_catalog_sources_are_in_pick_order(client, conn):
    # eufy sorts before garmin by name, but ranks after it by default -
    # until it's made the primary.
    insert(conn, "2026-01-01", "garmin", weight_kg=82)
    insert(conn, "2026-01-02", "eufy", weight_kg=81)
    recompute_derived(conn)

    def sources():
        return client.get("/api/catalog").get_json()["metrics"][0]["sources"]

    assert sources() == ["garmin", "eufy"]
    set_source_role(conn, WEIGHT_KG, "eufy")
    assert sources() == ["eufy", "garmin"]


def test_catalog_keeps_a_source_with_a_comma_whole(client, conn):
    # A pushed source can be any string.
    insert(conn, "2026-01-01", "garmin", steps=1)
    insert(conn, "2026-01-02", "scale, hall", steps=2)
    recompute_derived(conn)

    [steps] = client.get("/api/catalog").get_json()["metrics"]
    assert steps["sources"] == ["garmin", "scale, hall"]


def test_catalog_lists_each_session_kind_with_data(client, conn):
    session(conn, "garmin", SLEEP, "2026-01-02T22:30:00Z", "2026-01-03T06:30:00Z", {})
    session(conn, "google_health", SLEEP, "2026-01-01T23:00:00Z", "2026-01-02T07:00:00Z", {})
    session(conn, "garmin", SLEEP, "2026-01-03T22:00:00Z", "2026-01-04T06:00:00Z", {})

    catalog = client.get("/api/catalog").get_json()

    # No workouts recorded, so no workout entry.
    assert catalog["sessions"] == [
        {
            "kind": SLEEP,
            "grain": "session",
            "category": "Sleep",
            "cadence": "session",
            "count": 3,
            "first": "2026-01-01T23:00:00Z",
            "last": "2026-01-03T22:00:00Z",
        }
    ]
    assert catalog["metrics"] == []


def test_catalog_lists_session_kinds_by_name(client, conn):
    session(conn, "garmin", WORKOUT, "2026-01-01T07:00:00Z", "2026-01-01T07:30:00Z", {})
    session(conn, "garmin", SLEEP, "2026-01-01T22:00:00Z", "2026-01-02T06:00:00Z", {})

    kinds = [s["kind"] for s in client.get("/api/catalog").get_json()["sessions"]]
    assert kinds == [SLEEP, WORKOUT]


def test_catalog_lists_each_series_metric_with_data(client, conn):
    sample(conn, "garmin", "2026-01-02T08:00:00Z", 64)
    sample(conn, "garmin", "2026-01-01T23:58:00Z", 58)
    sample(conn, "polar", "2026-01-03T07:30:00Z", 71)

    catalog = client.get("/api/catalog").get_json()

    assert catalog["series"] == [
        {
            "metric": HEART_RATE,
            "unit": "bpm",
            "grain": "series",
            "category": "Vitals",
            "cadence": "reading",
            "sources": ["garmin", "polar"],
            "first": "2026-01-01T23:58:00Z",
            "last": "2026-01-03T07:30:00Z",
            "count": 3,
        }
    ]
    # Samples are their own grain: no daily metric or session from them.
    assert catalog["metrics"] == []
    assert catalog["sessions"] == []


def test_catalog_series_sources_are_in_default_precedence(client, conn):
    # No pick for samples yet, but the sources are listed the way the
    # daily pick ranks them: apple sorts before garmin by name, not here.
    sample(conn, "apple", "2026-01-01T08:00:00Z", 60)
    sample(conn, "google_health", "2026-01-01T08:00:00Z", 61)
    sample(conn, "garmin", "2026-01-01T08:00:00Z", 62)

    [hr] = client.get("/api/catalog").get_json()["series"]
    assert hr["sources"] == ["garmin", "google_health", "apple"]


def test_catalog_labels_every_entry_with_its_grain_category_and_cadence(client, conn):
    insert(conn, "2026-01-01", "garmin", steps=9000, body_fat_pct=18.5, vo2_max=48)
    insert(conn, "2026-01-01", "cgm", blood_glucose=5.4)
    recompute_derived(conn)
    session(conn, "garmin", WORKOUT, "2026-01-01T07:00:00Z", "2026-01-01T07:30:00Z", {})
    session(conn, "garmin", SLEEP, "2026-01-01T22:00:00Z", "2026-01-02T06:00:00Z", {})
    sample(conn, "garmin", "2026-01-01T08:00:00Z", 97, metric=SPO2)

    catalog = client.get("/api/catalog").get_json()

    def labels(entries, name):
        return [(e[name], e["grain"], e["category"], e["cadence"]) for e in entries]

    assert labels(catalog["metrics"], "metric") == [
        (STEPS, "daily", "Activity", "daily"),
        (BLOOD_GLUCOSE, "instant", "Vitals", "daily"),
        (BODY_FAT_PCT, "instant", "Body composition", "manual"),
        (VO2_MAX, "daily", "Fitness", "daily"),
    ]
    assert labels(catalog["sessions"], "kind") == [
        (SLEEP, "session", "Sleep", "session"),
        (WORKOUT, "session", "Activity", "session"),
    ]
    assert labels(catalog["series"], "metric") == [(SPO2, "series", "Vitals", "reading")]


def test_catalog_with_include_empty_lists_the_whole_vocabulary(client, conn):
    insert(conn, "2026-01-01", "garmin", resting_hr_bpm=52)
    recompute_derived(conn)
    session(conn, "garmin", WORKOUT, "2026-01-01T07:00:00Z", "2026-01-01T07:30:00Z", {})
    sample(conn, "garmin", "2026-01-01T08:00:00Z", 14, metric=RESPIRATION)

    catalog = client.get("/api/catalog?include_empty=1").get_json()

    # Vocabulary order, whether or not a key has data; kinds by name.
    assert [m["metric"] for m in catalog["metrics"]] == list(METRICS)
    assert [s["metric"] for s in catalog["series"]] == list(SERIES_METRICS)
    assert [s["kind"] for s in catalog["sessions"]] == sorted(SESSION_KINDS)
    # Every entry is labelled, empty or not.
    for entry in catalog["metrics"]:
        assert entry["grain"] == grain_for(entry["metric"])
        assert entry["category"] == category_for(entry["metric"])
        assert entry["cadence"] == cadence_for(entry["metric"])

    metrics = {m["metric"]: m for m in catalog["metrics"]}
    # One with data is just as it is without include_empty...
    assert metrics[RESTING_HR_BPM]["count"] == 1
    assert metrics[RESTING_HR_BPM]["sources"] == ["garmin"]
    # ...and one without is an empty slot.
    assert metrics[STEPS] == {
        "metric": STEPS,
        "unit": "count",
        "grain": "daily",
        "category": "Activity",
        "cadence": "daily",
        "sources": [],
        "first": None,
        "last": None,
        "count": 0,
        "last_value": None,
        "last_date": None,
    }
    assert catalog["sessions"] == [
        {"kind": SLEEP, "grain": "session", "category": "Sleep", "cadence": "session",
         "count": 0, "first": None, "last": None},
        {"kind": WORKOUT, "grain": "session", "category": "Activity", "cadence": "session",
         "count": 1, "first": "2026-01-01T07:00:00Z", "last": "2026-01-01T07:00:00Z"},
    ]
    series = {s["metric"]: s for s in catalog["series"]}
    assert series[RESPIRATION]["count"] == 1
    assert series[HEART_RATE] == {
        "metric": HEART_RATE,
        "unit": "bpm",
        "grain": "series",
        "category": "Vitals",
        "cadence": "reading",
        "sources": [],
        "first": None,
        "last": None,
        "count": 0,
    }


def test_catalog_with_include_empty_on_an_empty_install(client):
    catalog = client.get("/api/catalog?include_empty=1").get_json()

    assert [m["metric"] for m in catalog["metrics"]] == list(METRICS)
    assert [s["metric"] for s in catalog["series"]] == list(SERIES_METRICS)
    assert [s["kind"] for s in catalog["sessions"]] == sorted(SESSION_KINDS)
    for entry in [*catalog["metrics"], *catalog["series"], *catalog["sessions"]]:
        assert entry["count"] == 0
        assert entry["first"] is None and entry["last"] is None
        # Sessions list no sources.
        assert entry.get("sources", []) == []


@pytest.mark.parametrize("query", ["", "?include_empty=", "?include_empty=0", "?include_empty=no"])
def test_catalog_without_include_empty_leaves_out_keys_with_no_data(client, conn, query):
    insert(conn, "2026-01-01", "garmin", steps=9000)
    recompute_derived(conn)

    catalog = client.get(f"/api/catalog{query}").get_json()

    assert [m["metric"] for m in catalog["metrics"]] == [STEPS]
    assert catalog["sessions"] == []
    assert catalog["series"] == []


# ---------- cadence (the deprecated shim the console still groups by) ----------


@pytest.mark.parametrize(
    "key, cadence",
    [
        # Every pre-v2 key still in the vocabulary, with the label it had
        # before v2: the console's groups must not move until it switches
        # to grain and category.
        (STEPS, "daily"),
        (RESTING_HR_BPM, "daily"),
        (DISTANCE_M, "daily"),
        (ACTIVE_KCAL, "daily"),
        (TOTAL_KCAL, "daily"),
        (FLOORS, "daily"),
        (INTENSITY_MODERATE_MIN, "daily"),
        (INTENSITY_VIGOROUS_MIN, "daily"),
        (SPO2_PCT, "daily"),
        (RESPIRATORY_RATE_BRPM, "daily"),
        (HRV_MS, "daily"),
        (VO2_MAX, "daily"),
        (WEIGHT_KG, "manual"),
        (BODY_FAT_PCT, "manual"),
        (BMI, "manual"),
        (HYDRATION_ML, "manual"),
        (HEART_RATE, "reading"),
        (RESPIRATION, "reading"),
        (SPO2, "reading"),
        (SLEEP, "session"),
        (WORKOUT, "session"),
        # A v2 key is a plain daily one to the shim, whatever its grain.
        (MOOD, "daily"),
        (BLOOD_GLUCOSE, "daily"),
    ],
)
def test_cadence_for(key, cadence):
    assert cadence_for(key) == cadence


def test_every_manual_metric_is_a_daily_metric():
    # The manual label narrows the daily one; it isn't a fourth vocabulary.
    assert MANUAL_METRICS <= set(METRICS)


@pytest.mark.parametrize("key", ["heartrate", "nap", "", "sleep_minutes"])
def test_cadence_for_an_unknown_key_raises(key):
    with pytest.raises(ValueError, match="unknown metric"):
        cadence_for(key)


# ---------- coverage ----------


def coverage(client, query=""):
    resp = client.get(f"/api/coverage{query}")
    assert resp.status_code == 200
    return resp.get_json()


def test_coverage_of_an_empty_install(client):
    assert coverage(client) == {"metrics": {}, "series": {}, "sessions": {}}


def test_coverage_is_each_keys_dates_whatever_the_source(client, conn):
    insert(conn, "2026-01-03", "garmin", steps=7000)
    insert(conn, "2026-01-01", "garmin", steps=9000, resting_hr_bpm=52)
    # Two sources the same day: one date, not two.
    insert(conn, "2026-01-01", "google_health", steps=8000)
    insert(conn, "2026-01-02", "manual", weight_kg=82.5)
    recompute_derived(conn)
    # Many samples a day, from two sources: each date once.
    sample(conn, "garmin", "2026-01-02T08:00:00Z", 60)
    sample(conn, "garmin", "2026-01-02T08:02:00Z", 62)
    sample(conn, "polar", "2026-01-02T09:00:00Z", 70)
    sample(conn, "garmin", "2026-01-01T23:59:59Z", 58)
    sample(conn, "garmin", "2026-01-04T00:00:00Z", 15, metric=RESPIRATION)
    session(conn, "garmin", SLEEP, "2026-01-02T22:30:00Z", "2026-01-03T06:30:00Z", {})
    session(conn, "google_health", SLEEP, "2026-01-02T23:00:00Z", "2026-01-03T07:00:00Z", {})
    session(conn, "garmin", SLEEP, "2026-01-01T23:00:00Z", "2026-01-02T07:00:00Z", {})
    session(conn, "garmin", WORKOUT, "2026-01-03T07:00:00Z", "2026-01-03T07:30:00Z", {})

    assert coverage(client) == {
        "metrics": {
            STEPS: ["2026-01-01", "2026-01-03"],
            RESTING_HR_BPM: ["2026-01-01"],
            WEIGHT_KG: ["2026-01-02"],
        },
        "series": {
            HEART_RATE: ["2026-01-01", "2026-01-02"],
            RESPIRATION: ["2026-01-04"],
        },
        # A session is on the date it starts.
        "sessions": {
            SLEEP: ["2026-01-01", "2026-01-02"],
            WORKOUT: ["2026-01-03"],
        },
    }


def test_coverage_of_metrics_is_the_derived_days(client, conn):
    # Written but not derived: not a covered day yet.
    insert(conn, "2026-01-01", "garmin", steps=9000)

    assert coverage(client)["metrics"] == {}


def test_coverage_buckets_a_timestamp_by_its_utc_date(client, conn):
    # A real stored ts, from iso_utc(): 23:30 at UTC-5 is the next UTC day.
    late = iso_utc(datetime(2026, 1, 1, 23, 30, tzinfo=timezone(timedelta(hours=-5))))
    sample(conn, "garmin", late, 60)
    session(conn, "garmin", SLEEP, late, None, {})

    covered = coverage(client)
    assert covered["series"] == {HEART_RATE: ["2026-01-02"]}
    assert covered["sessions"] == {SLEEP: ["2026-01-02"]}


@pytest.mark.parametrize(
    "query, dates",
    [
        ("?from=2026-01-02&to=2026-01-03", ["2026-01-02", "2026-01-03"]),
        ("?from=2026-01-03", ["2026-01-03", "2026-01-04"]),
        ("?to=2026-01-01", ["2026-01-01"]),
        ("?from=2026-01-02&to=2026-01-02", ["2026-01-02"]),
        # Empty is the same as not given.
        ("?from=&to=", ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04"]),
    ],
)
def test_coverage_from_and_to_are_inclusive(client, conn, query, dates):
    for day in range(1, 5):
        insert(conn, f"2026-01-0{day}", "garmin", steps=day)
        # At each edge of the day, so the whole of the `to` day is in and
        # nothing from the day either side.
        sample(conn, "garmin", f"2026-01-0{day}T00:00:00Z", 60)
        sample(conn, "garmin", f"2026-01-0{day}T23:59:59Z", 60)
        session(conn, "garmin", SLEEP, f"2026-01-0{day}T23:59:59Z", None, {})
    recompute_derived(conn)

    assert coverage(client, query) == {
        "metrics": {STEPS: dates},
        "series": {HEART_RATE: dates},
        "sessions": {SLEEP: dates},
    }


def test_coverage_leaves_out_a_key_with_nothing_in_range(client, conn):
    insert(conn, "2026-01-01", "garmin", steps=9000)
    insert(conn, "2026-01-05", "garmin", resting_hr_bpm=52)
    recompute_derived(conn)

    assert coverage(client, "?from=2026-01-03")["metrics"] == {RESTING_HR_BPM: ["2026-01-05"]}


# ---------- a metric's series ----------


def test_series_is_the_derived_points_oldest_first(client, conn):
    insert(conn, "2026-01-03", "garmin", steps=7000)
    insert(conn, "2026-01-01", "garmin", steps=9000)
    insert(conn, "2026-01-02", "google_health", steps=8000)
    recompute_derived(conn)

    assert series(client, STEPS) == {
        "metric": STEPS,
        "unit": "count",
        "points": [
            {"date": "2026-01-01", "value": 9000, "source": "garmin"},
            {"date": "2026-01-02", "value": 8000, "source": "google_health"},
            {"date": "2026-01-03", "value": 7000, "source": "garmin"},
        ],
    }


def test_series_is_the_picked_value_not_every_source(client, conn):
    insert(conn, "2026-01-01", "garmin", steps=9000)
    insert(conn, "2026-01-01", "google_health", steps=8000)
    recompute_derived(conn)

    assert series(client, STEPS)["points"] == [
        {"date": "2026-01-01", "value": 9000, "source": "garmin"}
    ]


def test_series_follows_the_configured_primary(client, conn):
    set_source_role(conn, STEPS, "google_health")
    insert(conn, "2026-01-01", "garmin", steps=9000)
    insert(conn, "2026-01-01", "google_health", steps=8000)
    recompute_derived(conn)

    assert series(client, STEPS)["points"] == [
        {"date": "2026-01-01", "value": 8000, "source": "google_health"}
    ]


def test_series_reads_the_stored_pick_not_a_fresh_one(client, conn):
    # A primary set after the derive is forward-only: the contract still
    # serves what was picked, not what the new role would pick.
    insert(conn, "2026-01-01", "garmin", steps=9000)
    insert(conn, "2026-01-01", "google_health", steps=8000)
    recompute_derived(conn)
    set_source_role(conn, STEPS, "google_health")

    assert series(client, STEPS)["points"] == [
        {"date": "2026-01-01", "value": 9000, "source": "garmin"}
    ]


def test_series_holds_only_its_metric(client, conn):
    insert(conn, "2026-01-01", "garmin", steps=9000, resting_hr_bpm=52)
    recompute_derived(conn)

    assert series(client, RESTING_HR_BPM)["points"] == [
        {"date": "2026-01-01", "value": 52, "source": "garmin"}
    ]


@pytest.mark.parametrize(
    "query, dates",
    [
        ("?from=2026-01-02&to=2026-01-04", ["2026-01-02", "2026-01-03", "2026-01-04"]),
        ("?from=2026-01-04", ["2026-01-04", "2026-01-05"]),
        ("?to=2026-01-02", ["2026-01-01", "2026-01-02"]),
        ("?from=2026-01-03&to=2026-01-03", ["2026-01-03"]),
        ("?from=2026-02-01", []),
        # Empty is the same as not given.
        ("?from=&to=", ["2026-01-01", "2026-01-02", "2026-01-03", "2026-01-04", "2026-01-05"]),
        # Unpadded is read as the date it means.
        ("?from=2026-1-4", ["2026-01-04", "2026-01-05"]),
    ],
)
def test_series_from_and_to_are_inclusive(client, conn, query, dates):
    for day in range(1, 6):
        insert(conn, f"2026-01-0{day}", "garmin", steps=day)
    recompute_derived(conn)

    assert [p["date"] for p in series(client, STEPS, query)["points"]] == dates


def test_series_of_a_known_metric_with_no_data(client):
    assert series(client, WEIGHT_KG) == {"metric": WEIGHT_KG, "unit": "kg", "points": []}


def test_series_of_an_unknown_metric_is_404(client):
    assert_rejected(client.get("/api/metric/heart_rate"), 404)


def test_series_ignores_raw_readings_not_yet_derived(client, conn):
    # Written but not derived: the contract serves the derived profile only.
    insert(conn, "2026-01-01", "garmin", steps=9000)

    assert series(client, STEPS)["points"] == []


# ---------- sessions ----------


def test_sessions_oldest_first_with_parsed_summary(client, conn):
    later = {"asleep_minutes": 420, "deep_minutes": 80}
    earlier = {"asleep_minutes": 450, "light_minutes": 250, "rem_minutes": 100}
    session(conn, "garmin", SLEEP, "2026-01-02T22:30:00Z", "2026-01-03T06:00:00Z", later)
    session(conn, "garmin", SLEEP, "2026-01-01T23:00:00Z", "2026-01-02T07:00:00Z", earlier)

    assert sessions(client, SLEEP) == [
        {
            "start": "2026-01-01T23:00:00Z",
            "end": "2026-01-02T07:00:00Z",
            "source": "garmin",
            "summary": earlier,
        },
        {
            "start": "2026-01-02T22:30:00Z",
            "end": "2026-01-03T06:00:00Z",
            "source": "garmin",
            "summary": later,
        },
    ]


def test_sessions_hold_only_their_kind(client, conn):
    run = {"type": "running", "duration_minutes": 30, "distance_m": 5000.0}
    session(conn, "garmin", WORKOUT, "2026-01-01T07:00:00Z", "2026-01-01T07:30:00Z", run)
    session(conn, "garmin", SLEEP, "2026-01-01T22:00:00Z", "2026-01-02T06:00:00Z", {})

    assert sessions(client, WORKOUT) == [
        {
            "start": "2026-01-01T07:00:00Z",
            "end": "2026-01-01T07:30:00Z",
            "source": "garmin",
            "summary": run,
        }
    ]


def test_sessions_from_two_sources_are_both_listed(client, conn):
    # No derived pick for sessions yet: each device's night, tagged.
    session(conn, "google_health", SLEEP, "2026-01-01T23:00:00Z", "2026-01-02T07:00:00Z", {})
    session(conn, "garmin", SLEEP, "2026-01-01T23:00:00Z", "2026-01-02T06:55:00Z", {})

    assert [s["source"] for s in sessions(client, SLEEP)] == ["garmin", "google_health"]


def two_sources_of_nights(conn):
    """Two nights from each of Google Health and Garmin, Google's earlier
    on both nights, so start order alone would interleave them."""
    for start in ("2026-01-01T22:00:00Z", "2026-01-02T22:00:00Z"):
        session(conn, "google_health", SLEEP, start, None, {})
    for start in ("2026-01-01T23:00:00Z", "2026-01-02T23:00:00Z"):
        session(conn, "garmin", SLEEP, start, None, {})


def listed(client, kind, query=""):
    return [(s["source"], s["start"]) for s in sessions(client, kind, query)]


def test_sessions_list_the_priority_source_first(client, conn):
    """A priority-ordered passthrough: every source's sessions, the one
    the default precedence would pick first, each source's by start."""
    two_sources_of_nights(conn)

    assert listed(client, SLEEP) == [
        ("garmin", "2026-01-01T23:00:00Z"),
        ("garmin", "2026-01-02T23:00:00Z"),
        ("google_health", "2026-01-01T22:00:00Z"),
        ("google_health", "2026-01-02T22:00:00Z"),
    ]


def test_sessions_follow_a_primary_configured_for_the_kind(client, conn):
    two_sources_of_nights(conn)
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?)",
        (SOURCE_ROLES_KEY, json.dumps({SLEEP: "google_health"})),
    )
    conn.commit()

    assert listed(client, SLEEP) == [
        ("google_health", "2026-01-01T22:00:00Z"),
        ("google_health", "2026-01-02T22:00:00Z"),
        ("garmin", "2026-01-01T23:00:00Z"),
        ("garmin", "2026-01-02T23:00:00Z"),
    ]
    # Only that kind's: workouts keep the default order.
    session(conn, "google_health", WORKOUT, "2026-01-01T07:00:00Z", None, {})
    session(conn, "garmin", WORKOUT, "2026-01-01T08:00:00Z", None, {})
    assert [source for source, _ in listed(client, WORKOUT)] == ["garmin", "google_health"]


def test_sessions_from_unranked_sources_come_last_by_name(client, conn):
    session(conn, "polar", SLEEP, "2026-01-01T21:00:00Z", None, {})
    session(conn, "apple", SLEEP, "2026-01-01T21:30:00Z", None, {})
    two_sources_of_nights(conn)

    assert [source for source, _ in listed(client, SLEEP)] == [
        "garmin", "garmin", "google_health", "google_health", "apple", "polar",
    ]


def test_sessions_priority_order_holds_within_a_range(client, conn):
    two_sources_of_nights(conn)

    assert listed(client, SLEEP, "?from=2026-01-02") == [
        ("garmin", "2026-01-02T23:00:00Z"),
        ("google_health", "2026-01-02T22:00:00Z"),
    ]


def test_session_with_no_summary_or_end(client, conn):
    session(conn, "garmin", SLEEP, "2026-01-01T23:00:00Z", None, None)

    assert sessions(client, SLEEP) == [
        {"start": "2026-01-01T23:00:00Z", "end": None, "source": "garmin", "summary": {}}
    ]


@pytest.mark.parametrize(
    "query, starts",
    [
        # Matched on the start's date: the whole of the `to` day is in,
        # and nothing from the day after.
        ("?from=2026-01-02&to=2026-01-02", ["2026-01-02T00:00:00Z", "2026-01-02T23:59:59Z"]),
        ("?from=2026-01-02", ["2026-01-02T00:00:00Z", "2026-01-02T23:59:59Z", "2026-01-03T00:00:00Z"]),
        ("?to=2026-01-01", ["2026-01-01T23:00:00Z"]),
        ("?from=2026-02-01", []),
    ],
)
def test_sessions_from_and_to_match_the_start_date(client, conn, query, starts):
    for start in (
        "2026-01-01T23:00:00Z",
        "2026-01-02T00:00:00Z",
        "2026-01-02T23:59:59Z",
        "2026-01-03T00:00:00Z",
    ):
        session(conn, "garmin", SLEEP, start, None, {})

    assert [s["start"] for s in sessions(client, SLEEP, query)] == starts


def test_sessions_of_a_kind_with_none(client):
    assert sessions(client, WORKOUT) == []


def test_sessions_of_an_unknown_kind_is_404(client):
    assert_rejected(client.get("/api/sessions/nap"), 404)


# ---------- a series metric's samples ----------


def test_samples_oldest_first_tagged_with_their_source(client, conn):
    sample(conn, "garmin", "2026-10-12T15:18:00Z", 79)
    sample(conn, "garmin", "2026-10-12T15:16:00Z", 77)
    sample(conn, "garmin", "2026-10-12T15:20:00Z", 81)

    assert samples(client, HEART_RATE) == {
        "metric": HEART_RATE,
        "unit": "bpm",
        "points": [
            {"ts": "2026-10-12T15:16:00Z", "value": 77, "source": "garmin"},
            {"ts": "2026-10-12T15:18:00Z", "value": 79, "source": "garmin"},
            {"ts": "2026-10-12T15:20:00Z", "value": 81, "source": "garmin"},
        ],
    }


def test_sample_values_are_whole_where_they_can_be(client, conn):
    # Stored as REAL, so 77 is read back as 77.0 - served as 77 again;
    # a value with a fraction keeps it.
    sample(conn, "garmin", "2026-01-01T08:00:00Z", 77)
    sample(conn, "garmin", "2026-01-01T08:02:00Z", 77.5)

    # JSON 77.0 would parse back as a float, so the type is what shows it.
    whole, fraction = (p["value"] for p in samples(client, HEART_RATE)["points"])
    assert whole == 77 and type(whole) is int
    assert fraction == 77.5


def test_samples_from_two_sources_are_both_listed(client, conn):
    # A passthrough, not a stitch: where two devices sampled the same
    # stretch, every sample is served, tagged - ts first, then source.
    sample(conn, "polar", "2026-01-01T08:00:00Z", 61)
    sample(conn, "garmin", "2026-01-01T08:00:00Z", 60)
    sample(conn, "garmin", "2026-01-01T08:02:00Z", 62)
    sample(conn, "polar", "2026-01-01T08:01:00Z", 63)

    assert samples(client, HEART_RATE)["points"] == [
        {"ts": "2026-01-01T08:00:00Z", "value": 60, "source": "garmin"},
        {"ts": "2026-01-01T08:00:00Z", "value": 61, "source": "polar"},
        {"ts": "2026-01-01T08:01:00Z", "value": 63, "source": "polar"},
        {"ts": "2026-01-01T08:02:00Z", "value": 62, "source": "garmin"},
    ]


@pytest.mark.parametrize(
    "query, stamps",
    [
        # Matched on the sample's UTC date: the whole of the `to` day is
        # in, and nothing from the day either side.
        ("?from=2026-01-02&to=2026-01-02", ["2026-01-02T00:00:00Z", "2026-01-02T23:59:59Z"]),
        ("?from=2026-01-02", ["2026-01-02T00:00:00Z", "2026-01-02T23:59:59Z", "2026-01-03T00:00:00Z"]),
        ("?to=2026-01-01", ["2026-01-01T23:59:59Z"]),
        ("?from=2026-02-01", []),
        # Empty is the same as not given.
        ("?from=&to=", [
            "2026-01-01T23:59:59Z",
            "2026-01-02T00:00:00Z",
            "2026-01-02T23:59:59Z",
            "2026-01-03T00:00:00Z",
        ]),
        # Unpadded is read as the date it means.
        ("?from=2026-1-3", ["2026-01-03T00:00:00Z"]),
    ],
)
def test_samples_from_and_to_match_the_utc_date(client, conn, query, stamps):
    for ts in (
        "2026-01-01T23:59:59Z",
        "2026-01-02T00:00:00Z",
        "2026-01-02T23:59:59Z",
        "2026-01-03T00:00:00Z",
    ):
        sample(conn, "garmin", ts, 60)

    assert [p["ts"] for p in samples(client, HEART_RATE, query)["points"]] == stamps


def test_samples_of_a_known_metric_with_none(client):
    assert samples(client, HEART_RATE) == {"metric": HEART_RATE, "unit": "bpm", "points": []}


@pytest.mark.parametrize(
    "metric",
    [
        "heartrate",
        # A daily key is not a series one, even for the same quantity.
        RESTING_HR_BPM,
        SPO2_PCT,
        RESPIRATORY_RATE_BRPM,
        STEPS,
    ],
)
def test_samples_of_an_unknown_series_metric_is_404(client, metric):
    assert_rejected(client.get(f"/api/series/{metric}"), 404)


# ---------- from / to validation ----------


@pytest.mark.parametrize(
    "path",
    [
        f"/api/metric/{STEPS}",
        f"/api/sessions/{SLEEP}",
        f"/api/series/{HEART_RATE}",
        "/api/coverage",
    ],
)
@pytest.mark.parametrize(
    "query, named",
    [
        ("?from=2026-13-01", "from"),
        ("?from=01/02/2026", "from"),
        ("?from=20260101", "from"),
        ("?to=yesterday", "to"),
        ("?to=2026-02-30", "to"),
        ("?from=2026-01-01&to=2026-01-01T00:00:00Z", "to"),
    ],
)
def test_a_malformed_date_is_400(client, path, query, named):
    resp = client.get(path + query)
    assert_rejected(resp, 400)
    assert named in resp.get_json()["error"]


@pytest.mark.parametrize(
    "path",
    [
        f"/api/metric/{STEPS}",
        f"/api/sessions/{SLEEP}",
        f"/api/series/{HEART_RATE}",
        "/api/coverage",
    ],
)
def test_from_after_to_is_400(client, path):
    assert_rejected(client.get(path + "?from=2026-01-02&to=2026-01-01"), 400)


# ---------- manual weights reach the contract at once ----------


def test_posted_weight_is_in_the_contract_at_once(client):
    assert client.post("/api/weights", json={"date": "2026-01-02", "weight": 82.5}).status_code == 200

    assert series(client, WEIGHT_KG)["points"] == [
        {"date": "2026-01-02", "value": 82.5, "source": "manual"}
    ]
    [weight] = client.get("/api/catalog").get_json()["metrics"]
    assert weight["last_value"] == 82.5


def test_reposted_weight_replaces_its_derived_value(client):
    client.post("/api/weights", json={"date": "2026-01-02", "weight": 82.5})
    client.post("/api/weights", json={"date": "2026-01-02", "weight": 81.0})

    assert series(client, WEIGHT_KG)["points"] == [
        {"date": "2026-01-02", "value": 81.0, "source": "manual"}
    ]


def test_posted_weight_still_goes_through_the_pick(client, conn):
    # Garmin outranks hand entry by default, so its weigh-in stays the pick.
    insert(conn, "2026-01-02", "garmin", weight_kg=83.0)
    recompute_derived(conn)

    client.post("/api/weights", json={"date": "2026-01-02", "weight": 82.5})

    assert picked(conn, "2026-01-02", WEIGHT_KG) == (83.0, "garmin")


def test_posted_weight_re_derives_only_its_day(client, conn):
    # Forward-only: a role set since the last derive mustn't reach any
    # other day through a weigh-in.
    insert(conn, "2026-01-01", "garmin", steps=9000)
    insert(conn, "2026-01-01", "google_health", steps=8000)
    recompute_derived(conn)
    set_source_role(conn, STEPS, "google_health")

    client.post("/api/weights", json={"date": "2026-01-02", "weight": 82.5})

    assert picked(conn, "2026-01-01", STEPS) == (9000, "garmin")


def test_cleared_weights_leave_the_contract(client):
    client.post("/api/weights", json={"date": "2026-01-01", "weight": 82.0})
    client.post("/api/weights", json={"date": "2026-01-03", "weight": 81.5})

    assert client.delete("/api/weights").status_code == 200

    assert series(client, WEIGHT_KG)["points"] == []
    assert client.get("/api/catalog").get_json()["metrics"] == []


def test_cleared_weights_fall_back_to_another_source(client, conn):
    # 'manual' sorts before 'scale', so it wins until it's cleared.
    insert(conn, "2026-01-01", "scale", weight_kg=83.0)
    client.post("/api/weights", json={"date": "2026-01-01", "weight": 82.0})
    assert picked(conn, "2026-01-01", WEIGHT_KG) == (82.0, "manual")

    client.delete("/api/weights")

    assert series(client, WEIGHT_KG)["points"] == [
        {"date": "2026-01-01", "value": 83.0, "source": "scale"}
    ]


def test_clearing_weights_re_derives_only_the_cleared_days(client, conn):
    # Steps on a day between two weigh-ins: clearing them must not
    # re-pick it under a role set since its derive.
    client.post("/api/weights", json={"date": "2026-01-01", "weight": 82.0})
    insert(conn, "2026-01-02", "garmin", steps=9000)
    insert(conn, "2026-01-02", "google_health", steps=8000)
    recompute_derived(conn, since="2026-01-02", until="2026-01-02")
    client.post("/api/weights", json={"date": "2026-01-03", "weight": 81.5})
    set_source_role(conn, STEPS, "google_health")

    client.delete("/api/weights")

    assert picked(conn, "2026-01-02", STEPS) == (9000, "garmin")
    assert picked(conn, "2026-01-01", WEIGHT_KG) is None
    assert picked(conn, "2026-01-03", WEIGHT_KG) is None


def test_clearing_no_weights_is_fine(client):
    assert client.delete("/api/weights").status_code == 200
    assert client.get("/api/catalog").get_json() == {"metrics": [], "sessions": [], "series": []}
