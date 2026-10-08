"""API route tests - the shape the frontend depends on."""

import sqlite3
from datetime import date

from migrations import run_migrations


def test_weights_round_trip(client):
    assert client.get("/api/weights").get_json() == []

    assert client.post("/api/weights", json={"date": "2026-01-02", "weight": 81.5}).status_code == 200
    client.post("/api/weights", json={"date": "2026-01-01", "weight": 82.0})

    rows = client.get("/api/weights").get_json()
    assert rows == [
        {"date": "2026-01-01", "weight": 82.0},
        {"date": "2026-01-02", "weight": 81.5},
    ]


def test_weights_post_upserts_same_date(client):
    client.post("/api/weights", json={"date": "2026-01-01", "weight": 82.0})
    client.post("/api/weights", json={"date": "2026-01-01", "weight": 81.0})

    rows = client.get("/api/weights").get_json()
    assert rows == [{"date": "2026-01-01", "weight": 81.0}]


def test_weights_post_requires_weight(client):
    resp = client.post("/api/weights", json={"date": "2026-01-01"})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_weights_delete_clears_all(client):
    client.post("/api/weights", json={"date": "2026-01-01", "weight": 82.0})
    assert client.delete("/api/weights").status_code == 200
    assert client.get("/api/weights").get_json() == []


def test_weights_are_manual_weight_kg_readings(client, conn):
    client.post("/api/weights", json={"date": "2026-01-01", "weight": 82})

    assert [tuple(r) for r in conn.execute(
        "SELECT date, source, metric, value, unit FROM metrics"
    )] == [("2026-01-01", "manual", "weight_kg", 82.0, "kg")]
    # Read back as stored - a float, as the old REAL column gave - not
    # cast to a whole number the way steps are.
    rows = client.get("/api/weights").get_json()
    assert rows == [{"date": "2026-01-01", "weight": 82.0}]
    assert isinstance(rows[0]["weight"], float)


def test_weights_post_defaults_to_today(client):
    client.post("/api/weights", json={"weight": 80.5})

    assert client.get("/api/weights").get_json() == [
        {"date": date.today().isoformat(), "weight": 80.5}
    ]


def test_weights_are_manual_weight_only(client, conn):
    # Other sources' weight and other manual metrics share the table:
    # /api/weights neither lists them nor clears them.
    others = [
        ("2026-01-01", "manual", "steps", 9000, "count"),
        ("2026-01-01", "scale", "weight_kg", 83.0, "kg"),
        ("2026-01-02", "garmin", "steps", 8000, "count"),
    ]
    conn.executemany(
        "INSERT INTO metrics (date, source, metric, value, unit) VALUES (?, ?, ?, ?, ?)",
        others,
    )
    conn.commit()
    client.post("/api/weights", json={"date": "2026-01-02", "weight": 82.0})

    assert client.get("/api/weights").get_json() == [{"date": "2026-01-02", "weight": 82.0}]

    client.delete("/api/weights")

    assert client.get("/api/weights").get_json() == []
    assert [tuple(r) for r in conn.execute(
        "SELECT date, source, metric, value, unit FROM metrics ORDER BY date, source"
    )] == others


def test_weights_migrated_by_006_read_back_unchanged(db_at_version, monkeypatch):
    """Weigh-ins stored before 006 come out of the API byte for byte as before.

    Seeded at version 5 - the shape a production database is in - and
    the old route's answer taken straight from the weights table, then
    migrated and read back through the new one.
    """
    import main

    path = db_at_version(5)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executemany(
        "INSERT INTO weights (date, weight) VALUES (?, ?)",
        [("2026-01-02", 81.5), ("2026-01-01", 82), ("2026-01-03", 80.25)],
    )
    conn.commit()
    with main.app.app_context():
        before = main.jsonify([
            dict(r) for r in conn.execute("SELECT date, weight FROM weights ORDER BY date")
        ]).get_data()
    assert 6 in run_migrations(conn)
    conn.close()

    monkeypatch.setitem(main.app.config, "DB_PATH", path)
    after = main.app.test_client().get("/api/weights")

    assert after.get_data() == before
    assert after.get_json() == [
        {"date": "2026-01-01", "weight": 82.0},
        {"date": "2026-01-02", "weight": 81.5},
        {"date": "2026-01-03", "weight": 80.25},
    ]


def test_workouts_round_trip(client):
    assert client.get("/api/workouts").get_json() == {}

    assert client.post("/api/workouts", json={"week": 1, "idx": 0, "done": True}).status_code == 200
    client.post("/api/workouts", json={"week": 1, "idx": 1, "done": False})

    assert client.get("/api/workouts").get_json() == {"1": {"0": True, "1": False}}


def test_workouts_post_upserts(client):
    client.post("/api/workouts", json={"week": 2, "idx": 3, "done": True})
    client.post("/api/workouts", json={"week": 2, "idx": 3, "done": False})

    assert client.get("/api/workouts").get_json() == {"2": {"3": False}}


def test_workouts_post_requires_week_and_idx(client):
    resp = client.post("/api/workouts", json={"done": True})
    assert resp.status_code == 400
    assert "error" in resp.get_json()


def test_activity_empty(client):
    assert client.get("/api/activity").get_json() == []


def test_activity_returns_flat_rows(client, conn):
    conn.executemany(
        "INSERT INTO metrics (date, source, metric, value, unit, synced_at) "
        "VALUES ('2026-01-01', 'garmin', ?, ?, ?, 'now')",
        [
            ("steps", 9000, "count"),
            ("resting_hr_bpm", 52, "bpm"),
            ("sleep_minutes", 450, "min"),
        ],
    )
    conn.commit()

    rows = client.get("/api/activity").get_json()
    assert rows == [
        {
            "date": "2026-01-01",
            "steps": 9000,
            "resting_hr": 52,
            "sleep_hours": 7.5,
            "source": "garmin",
        }
    ]


def test_activity_days_limit_counts_dates_not_rows(client, conn):
    # Two sources and two metrics per day: limiting rows instead of
    # dates would return one day here rather than two.
    for d in ("2026-01-01", "2026-01-02", "2026-01-03"):
        for src in ("garmin", "google_health"):
            for metric, unit in (("steps", "count"), ("resting_hr_bpm", "bpm")):
                conn.execute(
                    "INSERT INTO metrics (date, source, metric, value, unit) "
                    "VALUES (?, ?, ?, 1, ?)",
                    (d, src, metric, unit),
                )
    conn.commit()

    rows = client.get("/api/activity?days=2").get_json()
    assert [r["date"] for r in rows] == ["2026-01-03", "2026-01-02"]


def test_plugins_list(client):
    plugins = client.get("/api/plugins").get_json()
    ids = {p["id"] for p in plugins}
    assert {"garmin", "google_health"} <= ids
    for plugin in plugins:
        assert set(plugin) >= {"id", "name", "configured", "missing_env"}


def test_sync_unknown_plugin_404(client):
    resp = client.post("/api/sync/does_not_exist")
    assert resp.status_code == 404
    body = resp.get_json()
    assert body["ok"] is False
    assert "does_not_exist" in body["error"]
