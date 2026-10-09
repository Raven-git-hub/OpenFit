"""
The access contract: the read API apps build on.

GET /api/catalog says what there is, GET /api/metric/<metric> serves one
metric's derived series and GET /api/sessions/<kind> one kind's
sessions, each with its provenance. Metrics come from derived_metrics -
the picked value - never the raw per-source rows. Also here: manual
/api/weights writes re-derive their days, so a weigh-in reaches the
contract at once.
"""

import pytest

from derived import recompute_derived, set_source_role
from metrics import METRICS, RESTING_HR_BPM, SLEEP, STEPS, WEIGHT_KG, WORKOUT
from plugins.base import write_session


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


def series(client, metric, query=""):
    resp = client.get(f"/api/metric/{metric}{query}")
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
    assert client.get("/api/catalog").get_json() == {"metrics": [], "sessions": []}


def test_catalog_lists_each_metric_with_data(client, conn):
    insert(conn, "2026-01-01", "garmin", steps=9000, resting_hr_bpm=52)
    insert(conn, "2026-01-02", "garmin", steps=8000)
    insert(conn, "2026-01-03", "google_health", steps=7000)
    insert(conn, "2026-01-02", "manual", weight_kg=82.5)
    recompute_derived(conn)

    catalog = client.get("/api/catalog").get_json()

    # Vocabulary order; sleep_minutes has no data, so isn't listed.
    assert catalog["metrics"] == [
        {
            "metric": STEPS,
            "unit": "count",
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
            "sources": ["manual"],
            "first": "2026-01-02",
            "last": "2026-01-02",
            "count": 1,
            "last_value": 82.5,
            "last_date": "2026-01-02",
        },
    ]
    assert catalog["sessions"] == []


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
    session(conn, "google_health", SLEEP, "2026-01-01T23:00:00Z", "2026-01-02T07:00:00Z", earlier)

    assert sessions(client, SLEEP) == [
        {
            "start": "2026-01-01T23:00:00Z",
            "end": "2026-01-02T07:00:00Z",
            "source": "google_health",
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


# ---------- from / to validation ----------


@pytest.mark.parametrize("path", [f"/api/metric/{STEPS}", f"/api/sessions/{SLEEP}"])
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


@pytest.mark.parametrize("path", [f"/api/metric/{STEPS}", f"/api/sessions/{SLEEP}"])
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
    assert client.get("/api/catalog").get_json() == {"metrics": [], "sessions": []}
