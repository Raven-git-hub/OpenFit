"""API route tests - the shape the frontend depends on."""


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
    conn.execute(
        "INSERT INTO activity (date, steps, resting_hr, sleep_hours, source, synced_at) "
        "VALUES ('2026-01-01', 9000, 52, 7.5, 'garmin', 'now')"
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
    # Two sources per day: limiting rows instead of dates would return
    # one day here rather than two.
    for d in ("2026-01-01", "2026-01-02", "2026-01-03"):
        for src in ("garmin", "google_health"):
            conn.execute(
                "INSERT INTO activity (date, steps, source) VALUES (?, 1, ?)", (d, src)
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
