"""
The webhook: one pushed reading at POST /api/webhook/<token>.

The token is the webhook_token settings row - made on the first GET of
/api/webhook-token, replaced by .../regenerate - and the one auth on any
route. A valid reading is written under its source ('webhook' unless
the body names one) and its day is re-derived at once; a body that fails
a check is a 400 with nothing written. Local only: nothing here, or in
the route, makes a network call.
"""

import json
from datetime import date, timedelta

import pytest

import main
from derived import recompute_derived
from metrics import (
    METRICS,
    PLAUSIBLE_RANGES,
    RESTING_HR_BPM,
    SLEEP_MINUTES,
    STEPS,
    WEIGHT_KG,
)


def get_token(client):
    resp = client.get("/api/webhook-token")
    assert resp.status_code == 200
    return resp.get_json()["token"]


def push(client, body, token=None):
    """POST a reading to the webhook - with the current token unless given one."""
    if token is None:
        token = get_token(client)
    return client.post(f"/api/webhook/{token}", json=body)


def push_raw(client, data, token=None):
    """POST raw request bytes to the webhook, for bodies json= can't express."""
    if token is None:
        token = get_token(client)
    return client.post(f"/api/webhook/{token}", data=data, content_type="application/json")


def stored_token(conn):
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (main.WEBHOOK_TOKEN_KEY,)
    ).fetchone()
    return row[0] if row else None


def readings(conn):
    """Every metrics row, as (date, source, metric, value, unit)."""
    return [
        tuple(r)
        for r in conn.execute(
            "SELECT date, source, metric, value, unit FROM metrics "
            "ORDER BY date, source, metric"
        )
    ]


def derived(conn):
    return [tuple(r) for r in conn.execute("SELECT * FROM derived_metrics")]


def picked(conn, date, metric):
    """(value, source) derived for one (date, metric), or None."""
    row = conn.execute(
        "SELECT value, source FROM derived_metrics WHERE date = ? AND metric = ?",
        (date, metric),
    ).fetchone()
    return tuple(row) if row else None


def insert(conn, date, source, **values):
    """Seed one source's readings for a day, keyed by canonical metric."""
    for metric, value in values.items():
        conn.execute(
            "INSERT INTO metrics (date, source, metric, value, unit, synced_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (date, source, metric, value, METRICS[metric], "2026-01-01 06:00:00"),
        )
    conn.commit()


def days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


def assert_rejected(resp, status=400):
    assert resp.status_code == status
    body = resp.get_json()
    assert body["ok"] is False
    assert body["error"]


# ---------- the token ----------


def test_get_makes_a_token_and_stores_it(client, conn):
    assert stored_token(conn) is None

    body = client.get("/api/webhook-token").get_json()

    token = body["token"]
    assert isinstance(token, str) and len(token) >= 32
    assert stored_token(conn) == token
    assert body["path"] == f"/api/webhook/{token}"
    assert body["url"] == f"http://localhost/api/webhook/{token}"


def test_a_second_get_returns_the_same_token(client):
    assert get_token(client) == get_token(client)


def test_an_empty_stored_token_counts_as_none(client, conn):
    # The row can be written through the generic settings route too.
    client.put(f"/api/settings/{main.WEBHOOK_TOKEN_KEY}", json={"value": ""})

    assert_rejected(client.post("/api/webhook/", json={"metric": STEPS, "value": 1}), 401)
    token = get_token(client)
    assert token
    assert stored_token(conn) == token


def test_regenerate_replaces_the_token_and_the_old_one_stops_working(client, conn):
    old = get_token(client)

    resp = client.post("/api/webhook-token/regenerate")

    assert resp.status_code == 200
    body = resp.get_json()
    new = body["token"]
    assert body["ok"] is True
    assert body["path"] == f"/api/webhook/{new}"
    assert new != old
    assert stored_token(conn) == new
    assert get_token(client) == new

    assert_rejected(push(client, {"metric": STEPS, "value": 1}, token=old), 401)
    assert push(client, {"metric": STEPS, "value": 1}, token=new).status_code == 200


def test_regenerate_before_any_token_makes_one(client, conn):
    token = client.post("/api/webhook-token/regenerate").get_json()["token"]

    assert stored_token(conn) == token
    assert get_token(client) == token


# ---------- the token as auth ----------


def test_the_current_token_is_accepted(client):
    resp = push(client, {"metric": STEPS, "value": 9000})

    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True


def test_a_wrong_or_missing_token_is_a_401_and_writes_nothing(client, conn):
    token = get_token(client)
    reading = {"metric": STEPS, "value": 9000}

    for wrong in ("nope", token[:-1], token + "x", token.upper(), "café"):
        assert_rejected(client.post(f"/api/webhook/{wrong}", json=reading), 401)
    assert_rejected(client.post("/api/webhook/", json=reading), 401)

    assert readings(conn) == []
    assert derived(conn) == []


def test_before_a_token_is_made_every_token_is_a_401(client, conn):
    assert_rejected(client.post("/api/webhook/anything", json={"metric": STEPS, "value": 1}), 401)
    assert_rejected(client.post("/api/webhook/", json={"metric": STEPS, "value": 1}), 401)

    # A refused push doesn't make one either.
    assert stored_token(conn) is None
    assert readings(conn) == []


def test_the_token_is_checked_before_the_body(client):
    get_token(client)

    # A bad body with a bad token says nothing about the body.
    assert_rejected(client.post("/api/webhook/nope", json={"metric": "stepz"}), 401)


# ---------- writing ----------


def test_a_reading_is_stored_under_the_webhook_source_for_today(client, conn):
    resp = push(client, {"metric": WEIGHT_KG, "value": 82.4})

    assert resp.status_code == 200
    today = date.today().isoformat()
    assert resp.get_json() == {
        "ok": True,
        "stored": {
            "date": today, "source": "webhook", "metric": WEIGHT_KG,
            "value": 82.4, "unit": "kg",
        },
    }
    assert readings(conn) == [(today, "webhook", WEIGHT_KG, 82.4, "kg")]
    synced_at = conn.execute("SELECT synced_at FROM metrics").fetchone()[0]
    assert synced_at is not None


def test_the_body_can_name_the_date_and_the_source(client, conn):
    resp = push(client, {"metric": STEPS, "value": 9000, "date": "2026-01-05", "source": "eufy"})

    assert resp.status_code == 200
    assert resp.get_json()["stored"] == {
        "date": "2026-01-05", "source": "eufy", "metric": STEPS,
        "value": 9000, "unit": "count",
    }
    assert readings(conn) == [("2026-01-05", "eufy", STEPS, 9000, "count")]


def test_the_date_is_stored_zero_padded_and_the_source_trimmed(client, conn):
    push(client, {"metric": STEPS, "value": 1, "date": "2026-1-5", "source": " eufy "})

    assert readings(conn) == [("2026-01-05", "eufy", STEPS, 1, "count")]


def test_a_repeat_push_replaces_the_reading(client, conn):
    # Idempotent: one reading per (date, source, metric), the latest wins.
    for value in (82.4, 82.4, 82.1):
        assert push(client, {"metric": WEIGHT_KG, "value": value, "date": "2026-01-05"}).status_code == 200

    assert readings(conn) == [("2026-01-05", "webhook", WEIGHT_KG, 82.1, "kg")]
    assert picked(conn, "2026-01-05", WEIGHT_KG) == (82.1, "webhook")


def test_a_push_never_touches_another_sources_reading(client, conn):
    insert(conn, "2026-01-05", "manual", weight_kg=83.0)

    push(client, {"metric": WEIGHT_KG, "value": 82.4, "date": "2026-01-05"})

    assert readings(conn) == [
        ("2026-01-05", "manual", WEIGHT_KG, 83.0, "kg"),
        ("2026-01-05", "webhook", WEIGHT_KG, 82.4, "kg"),
    ]


# ---------- re-deriving ----------


def test_a_push_is_derived_at_once(client, conn):
    push(client, {"metric": WEIGHT_KG, "value": 82.4})
    push(client, {"metric": STEPS, "value": 9000})

    today = date.today().isoformat()
    assert picked(conn, today, WEIGHT_KG) == (82.4, "webhook")
    assert picked(conn, today, STEPS) == (9000, "webhook")
    assert client.get("/api/activity").get_json() == [{
        "date": today, "steps": 9000, "resting_hr": None, "sleep_hours": None,
        "source": "webhook",
    }]


def test_a_webhook_source_competes_like_any_other(client, conn):
    insert(conn, "2026-01-05", "garmin", weight_kg=83.0)
    recompute_derived(conn)

    # Unlisted in the default precedence: Garmin keeps the pick.
    push(client, {"metric": WEIGHT_KG, "value": 82.4, "date": "2026-01-05", "source": "eufy"})
    assert picked(conn, "2026-01-05", WEIGHT_KG) == (83.0, "garmin")

    # Made the metric's primary, its next push wins.
    client.put(f"/api/source-roles/{WEIGHT_KG}", json={"primary": "eufy"})
    push(client, {"metric": WEIGHT_KG, "value": 82.5, "date": "2026-01-05", "source": "eufy"})
    assert picked(conn, "2026-01-05", WEIGHT_KG) == (82.5, "eufy")
    assert client.get("/api/source-roles").get_json()[WEIGHT_KG] == {
        "primary": "eufy", "configured": True, "sources": ["eufy", "garmin"],
    }


def test_a_backdated_push_re_derives_its_own_day_and_no_other(client, conn):
    for d in (days_ago(3), days_ago(1)):
        insert(conn, d, "garmin", steps=9000)
        insert(conn, d, "google_health", steps=8000)
    recompute_derived(conn)

    # Forward-only: a role change re-derives nothing on its own.
    client.put(f"/api/source-roles/{STEPS}", json={"primary": "google_health"})
    push(client, {"metric": WEIGHT_KG, "value": 82.4, "date": days_ago(3)})

    # The pushed day is re-picked under the current roles, like a sync's
    # window; the days after it keep the source they were picked with.
    assert picked(conn, days_ago(3), WEIGHT_KG) == (82.4, "webhook")
    assert picked(conn, days_ago(3), STEPS) == (8000, "google_health")
    assert picked(conn, days_ago(1), STEPS) == (9000, "garmin")


# ---------- validation ----------


def assert_nothing_written(conn):
    assert readings(conn) == []
    assert derived(conn) == []


@pytest.mark.parametrize("metric", ["stepz", "Steps", "", None, 5, ["steps"], {"k": "steps"}])
def test_an_unknown_metric_is_a_400(client, conn, metric):
    assert_rejected(push(client, {"metric": metric, "value": 1}))
    assert_nothing_written(conn)


def test_a_missing_metric_is_a_400(client, conn):
    assert_rejected(push(client, {"value": 1}))
    assert_nothing_written(conn)


@pytest.mark.parametrize("value", ["82.4", "", None, True, False, [82.4], {"kg": 82.4}])
def test_a_non_numeric_value_is_a_400(client, conn, value):
    assert_rejected(push(client, {"metric": WEIGHT_KG, "value": value}))
    assert_nothing_written(conn)


def test_a_missing_value_is_a_400(client, conn):
    assert_rejected(push(client, {"metric": WEIGHT_KG}))
    assert_nothing_written(conn)


@pytest.mark.parametrize("raw", ["NaN", "Infinity", "-Infinity", "1" + "0" * 400])
def test_a_non_finite_or_unstorable_value_is_a_400(client, conn, raw):
    # Python's JSON parser takes NaN and Infinity, and any length of integer.
    assert_rejected(push_raw(client, '{"metric": "sleep_minutes", "value": %s}' % raw))
    assert_nothing_written(conn)


def test_the_plausible_ranges():
    assert PLAUSIBLE_RANGES == {
        STEPS: (0, 200_000),
        RESTING_HR_BPM: (20, 250),
        SLEEP_MINUTES: (0, 1440),
        WEIGHT_KG: (20, 400),
    }
    for metric in PLAUSIBLE_RANGES:
        assert metric in METRICS


@pytest.mark.parametrize("metric", sorted(PLAUSIBLE_RANGES))
def test_an_out_of_range_value_is_a_400(client, conn, metric):
    low, high = PLAUSIBLE_RANGES[metric]

    for value in (low - 1, low - 0.01, high + 0.01, high + 1):
        resp = push(client, {"metric": metric, "value": value})
        assert_rejected(resp)
        assert metric in resp.get_json()["error"]

    assert_nothing_written(conn)


@pytest.mark.parametrize("metric", sorted(PLAUSIBLE_RANGES))
def test_the_range_includes_both_ends(client, conn, metric):
    low, high = PLAUSIBLE_RANGES[metric]

    assert push(client, {"metric": metric, "value": low, "date": "2026-01-01"}).status_code == 200
    assert push(client, {"metric": metric, "value": high, "date": "2026-01-02"}).status_code == 200


def test_a_metric_with_no_range_takes_any_number(client, conn, monkeypatch):
    monkeypatch.delitem(PLAUSIBLE_RANGES, STEPS)

    for value in (-5, 10**9):
        assert push(client, {"metric": STEPS, "value": value}).status_code == 200


@pytest.mark.parametrize("body", [[{"metric": "steps", "value": 1}], "steps", 9000, None])
def test_a_non_object_body_is_a_400(client, conn, body):
    assert_rejected(push_raw(client, json.dumps(body)))
    assert_nothing_written(conn)


@pytest.mark.parametrize("data", ["", "not json", '{"metric": "steps", "value": 1'])
def test_a_body_that_isnt_json_is_a_400(client, conn, data):
    assert_rejected(push_raw(client, data))
    assert_nothing_written(conn)


@pytest.mark.parametrize("day", ["2026-13-01", "2026-02-30", "01/05/2026", "20260105",
                                 "2026-W02-1", "2026-01-05T08:00:00", 20260105, ["2026-01-05"]])
def test_a_bad_date_is_a_400(client, conn, day):
    assert_rejected(push(client, {"metric": STEPS, "value": 1, "date": day}))
    assert_nothing_written(conn)


@pytest.mark.parametrize("source", ["   ", 5, ["eufy"], {"id": "eufy"}, True])
def test_a_bad_source_is_a_400(client, conn, source):
    assert_rejected(push(client, {"metric": STEPS, "value": 1, "source": source}))
    assert_nothing_written(conn)


@pytest.mark.parametrize("source", sorted(main.PLUGINS))
def test_a_pull_plugins_source_is_a_400(client, conn, source):
    # Its syncs own those rows: a push would overwrite the device's own
    # reading, and the next sync would overwrite the push.
    assert_rejected(push(client, {"metric": STEPS, "value": 1, "source": source}))
    assert_nothing_written(conn)


# ---------- the rest stays open ----------


def test_other_routes_need_no_token(client):
    # The webhook is the one token-authed route: the trusted-network
    # model holds everywhere else, the token routes included.
    get_token(client)
    for path in ("/api/activity", "/api/weights", "/api/source-roles", "/api/webhook-token"):
        assert client.get(path).status_code == 200, path
