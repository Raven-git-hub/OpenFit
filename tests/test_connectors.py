"""
UI-managed device connectors.

A connector is a plugin plus whatever account has been attached to it
through the UI. These tests pin the contract the frontend builds its form
from, the validation that comes out of the manifest, and the two things
that matter most about the credentials themselves: they are encrypted
before they hit the database, and they never come back out over the API.

No network: nothing here logs in to Garmin, it only stores what a login
would need.
"""

import json
import sqlite3

import pytest

from crypto import decrypt


def connectors(client):
    return {c["id"]: c for c in client.get("/api/connectors").get_json()}


def stored_row(db_path, plugin_id="garmin"):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT plugin_id, credentials, created_at FROM accounts WHERE plugin_id = ?",
            (plugin_id,),
        ).fetchone()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def no_device_env(monkeypatch):
    """Start from "nothing configured", whatever the dev's shell holds.

    A developer with GARMIN_EMAIL exported would otherwise see different
    results from CI, since the env is the fallback credential source -
    and likewise SYNC_INTERVAL_HOURS, the baseline /detail reports.
    """
    import main

    monkeypatch.setattr(main, "SYNC_INTERVAL_HOURS", 6)
    for var in (
        "GARMIN_EMAIL",
        "GARMIN_PASSWORD",
        "GOOGLE_HEALTH_CLIENT_ID",
        "GOOGLE_HEALTH_CLIENT_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)


# ---------- the manifest the UI renders ----------

def test_lists_every_plugin_with_its_manifest(client):
    found = connectors(client)

    assert set(found) == {"garmin", "google_health"}
    for c in found.values():
        assert set(c) >= {"id", "name", "fields", "add_flow", "connected", "configured"}
        assert isinstance(c["fields"], list)


def test_lists_connectors_by_display_name(client, monkeypatch):
    """Alphabetical by name, whatever order the plugins were registered in.

    The registry is reversed first so registration order disagrees with
    the alphabet - otherwise this would pass without the sort.
    """
    import main

    monkeypatch.setattr(main, "PLUGINS", dict(reversed(list(main.PLUGINS.items()))))
    found = client.get("/api/connectors").get_json()
    names = [c["name"] for c in found]

    assert names == sorted(names, key=str.lower)
    assert [c["id"] for c in found] == ["garmin", "google_health"]


def test_garmin_manifest_is_a_credentials_form(client):
    garmin = connectors(client)["garmin"]

    assert garmin["name"] == "Garmin Connect"
    assert garmin["add_flow"] == "credentials"
    assert [(f["key"], f["type"], f["required"]) for f in garmin["fields"]] == [
        ("email", "text", True),
        ("password", "password", True),
    ]


def test_google_health_is_an_oauth_connector(client):
    """add_flow is what the UI switches on to show the two-step modal."""
    google = connectors(client)["google_health"]

    assert google["add_flow"] == "oauth"
    assert [f["key"] for f in google["fields"]] == ["client_id", "client_secret"]


def test_nothing_is_connected_on_a_fresh_install(client):
    assert [c["connected"] for c in connectors(client).values()] == [False, False]


# ---------- add / remove round trip ----------

def test_post_then_get_marks_it_connected(client):
    resp = client.post(
        "/api/connectors/garmin",
        json={"email": "me@example.com", "password": "hunter2"},
    )
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}

    found = connectors(client)
    assert found["garmin"]["connected"] is True
    assert found["garmin"]["configured"] is True
    # Adding one device doesn't connect the other.
    assert found["google_health"]["connected"] is False


def test_credentials_are_encrypted_at_rest(client, db_path):
    client.post(
        "/api/connectors/garmin",
        json={"email": "me@example.com", "password": "hunter2"},
    )

    row = stored_row(db_path)
    assert row is not None
    assert "hunter2" not in row[1]
    assert json.loads(decrypt(row[1])) == {
        "email": "me@example.com",
        "password": "hunter2",
    }

    # The password must not be findable anywhere in the database file.
    with open(db_path, "rb") as f:
        raw = f.read()
    assert b"hunter2" not in raw
    assert b"me@example.com" not in raw


def test_credentials_never_come_back_over_the_api(client):
    client.post(
        "/api/connectors/garmin",
        json={"email": "me@example.com", "password": "hunter2"},
    )

    body = client.get("/api/connectors").get_data(as_text=True)
    assert "hunter2" not in body
    assert "me@example.com" not in body


def test_delete_disconnects(client, db_path):
    client.post(
        "/api/connectors/garmin",
        json={"email": "me@example.com", "password": "hunter2"},
    )

    assert client.delete("/api/connectors/garmin").status_code == 200

    assert connectors(client)["garmin"]["connected"] is False
    assert stored_row(db_path) is None


def test_delete_also_clears_the_cached_token(client, tmp_path, monkeypatch):
    """Removing a device must not leave a working session behind."""
    tokenstore = tmp_path / ".garminconnect"
    tokenstore.mkdir()
    (tokenstore / "oauth1_token.json").write_text("{}")
    monkeypatch.setenv("GARMIN_TOKENSTORE", str(tokenstore))

    client.post(
        "/api/connectors/garmin",
        json={"email": "me@example.com", "password": "hunter2"},
    )
    client.delete("/api/connectors/garmin")

    assert not tokenstore.exists()


def test_delete_with_no_cached_token_is_fine(client, tmp_path, monkeypatch):
    monkeypatch.setenv("GARMIN_TOKENSTORE", str(tmp_path / "never-created"))

    assert client.delete("/api/connectors/garmin").status_code == 200


def test_delete_when_not_connected_is_not_an_error(client):
    """Idempotent: the UI can fire this without checking state first."""
    assert client.delete("/api/connectors/garmin").status_code == 200
    assert client.delete("/api/connectors/garmin").status_code == 200


def test_reconnecting_replaces_the_credentials(client, db_path):
    client.post(
        "/api/connectors/garmin",
        json={"email": "me@example.com", "password": "old"},
    )
    first_created = stored_row(db_path)[2]

    client.post(
        "/api/connectors/garmin",
        json={"email": "me@example.com", "password": "new"},
    )

    row = stored_row(db_path)
    assert json.loads(decrypt(row[1]))["password"] == "new"
    # Same connection re-authorised, so it keeps its original created_at.
    assert row[2] == first_created
    # ...and it's still one row, not two.
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 1
    conn.close()


# ---------- validation ----------

def test_missing_required_field_is_a_400_naming_the_field(client, db_path):
    resp = client.post("/api/connectors/garmin", json={"email": "me@example.com"})

    assert resp.status_code == 400
    body = resp.get_json()
    assert body["ok"] is False
    assert "Password" in body["error"]
    # Nothing was stored by the rejected request.
    assert stored_row(db_path) is None
    assert connectors(client)["garmin"]["connected"] is False


def test_blank_and_whitespace_are_treated_as_missing(client):
    for password in ("", "   ", None):
        resp = client.post(
            "/api/connectors/garmin",
            json={"email": "me@example.com", "password": password},
        )
        assert resp.status_code == 400, password


def test_an_empty_body_lists_every_missing_field(client):
    body = client.post("/api/connectors/garmin", json={}).get_json()

    assert "Email" in body["error"]
    assert "Password" in body["error"]


def test_values_are_trimmed(client, db_path):
    client.post(
        "/api/connectors/garmin",
        json={"email": "  me@example.com  ", "password": "hunter2"},
    )

    assert json.loads(decrypt(stored_row(db_path)[1]))["email"] == "me@example.com"


def test_fields_outside_the_manifest_are_ignored(client, db_path):
    client.post(
        "/api/connectors/garmin",
        json={"email": "me@example.com", "password": "hunter2", "sneaky": "x"},
    )

    assert json.loads(decrypt(stored_row(db_path)[1])) == {
        "email": "me@example.com",
        "password": "hunter2",
    }


def test_unknown_connector_is_a_404(client):
    assert client.post("/api/connectors/nope", json={}).status_code == 404
    assert client.delete("/api/connectors/nope").status_code == 404


# ---------- status reflects the account, not just the env ----------

def test_plugins_status_follows_the_stored_account(client):
    before = {p["id"]: p for p in client.get("/api/plugins").get_json()}
    assert before["garmin"]["configured"] is False
    assert before["garmin"]["missing_env"] == ["GARMIN_EMAIL", "GARMIN_PASSWORD"]

    client.post(
        "/api/connectors/garmin",
        json={"email": "me@example.com", "password": "hunter2"},
    )

    after = {p["id"]: p for p in client.get("/api/plugins").get_json()}
    assert after["garmin"]["configured"] is True
    assert after["garmin"]["missing_env"] == []


def test_env_vars_still_configure_a_pre_ui_install(client, monkeypatch):
    """Someone whose .env holds their Garmin details keeps syncing.

    They show as not `connected` - there's no account row - but they are
    `configured`, so the scheduler still runs them until they re-add the
    device through the UI.
    """
    monkeypatch.setenv("GARMIN_EMAIL", "me@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "hunter2")

    garmin = connectors(client)["garmin"]
    assert garmin["connected"] is False
    assert garmin["configured"] is True

    plugins = {p["id"]: p for p in client.get("/api/plugins").get_json()}
    assert plugins["garmin"]["configured"] is True


def test_a_stored_account_beats_the_env(client, conn, monkeypatch):
    """Once you've added the device in the UI, a stale .env can't override it."""
    from plugins import PLUGINS

    client.post(
        "/api/connectors/garmin",
        json={"email": "ui@example.com", "password": "from-ui"},
    )
    monkeypatch.setenv("GARMIN_EMAIL", "env@example.com")
    monkeypatch.setenv("GARMIN_PASSWORD", "from-env")

    credentials = PLUGINS["garmin"].get_credentials(conn)
    assert credentials == {"email": "ui@example.com", "password": "from-ui"}


def test_an_unreadable_credential_blob_does_not_break_the_page(client, conn):
    """A lost or rotated encryption key must stay recoverable in the UI.

    If this 500'd, the config page - and with it the Remove button that
    fixes the problem - would be unreachable.
    """
    conn.execute(
        "INSERT INTO accounts (plugin_id, credentials, created_at) VALUES (?, ?, ?)",
        ("garmin", "not-a-valid-fernet-token", "2026-01-01T00:00:00Z"),
    )
    conn.commit()

    resp = client.get("/api/connectors")
    assert resp.status_code == 200

    garmin = {c["id"]: c for c in resp.get_json()}["garmin"]
    assert garmin["connected"] is True     # the row is there...
    assert garmin["configured"] is False   # ...but it can't be used

    # And it can still be removed, which is the way out.
    assert client.delete("/api/connectors/garmin").status_code == 200
    assert {c["id"]: c for c in client.get("/api/connectors").get_json()}["garmin"]["connected"] is False


# ---------- the source's Settings popup: /detail ----------

def seed(db_path, source, metrics=(), samples=(), sessions=()):
    """Seed one source's rows, each with the synced_at given.

    metrics: (date, metric, value, synced_at); samples: (metric, ts,
    value, synced_at); sessions: (kind, start, synced_at).
    """
    from metrics import METRICS, SERIES_METRICS

    conn = sqlite3.connect(db_path)
    try:
        for day, metric, value, synced_at in metrics:
            conn.execute(
                "INSERT INTO metrics (date, source, metric, value, unit, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (day, source, metric, value, METRICS[metric], synced_at),
            )
        for metric, ts, value, synced_at in samples:
            conn.execute(
                "INSERT INTO samples (source, metric, ts, value, unit, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (source, metric, ts, value, SERIES_METRICS[metric], synced_at),
            )
        for kind, start, synced_at in sessions:
            conn.execute(
                'INSERT INTO sessions (id, source, kind, "start", "end", summary_json, synced_at) '
                "VALUES (?, ?, ?, ?, NULL, '{}', ?)",
                (f"{source}:{kind}:{start}", source, kind, start, synced_at),
            )
        conn.commit()
    finally:
        conn.close()


def detail(client, plugin_id="garmin"):
    return client.get(f"/api/connectors/{plugin_id}/detail")


def add_garmin(client):
    client.post(
        "/api/connectors/garmin",
        json={"email": "me@example.com", "password": "hunter2"},
    )


def test_detail_shows_a_connected_sources_account(client, db_path):
    add_garmin(client)
    seed(
        db_path, "garmin",
        metrics=[("2026-10-01", "steps", 9000, "2026-10-01 06:00:00")],
        sessions=[("sleep", "2026-09-30T22:00:00Z", "2026-10-01 06:00:01")],
    )

    resp = detail(client)

    assert resp.status_code == 200
    assert resp.get_json() == {
        "connected_via": "credentials",
        "login": "me@example.com",
        "added": stored_row(db_path)[2],
        "last_sync": "2026-10-01 06:00:01",
        "sync_hours": 6,
        "sync_is_default": True,
    }


@pytest.mark.parametrize("latest", ["metrics", "samples", "sessions"])
def test_last_sync_is_the_latest_across_all_three_tables(client, db_path, latest):
    """Whichever table the source last wrote to - a sync touches all three."""
    stamp = {table: "2026-10-01 06:00:00" for table in ("metrics", "samples", "sessions")}
    stamp[latest] = "2026-10-02 06:00:00"
    add_garmin(client)
    seed(
        db_path, "garmin",
        metrics=[("2026-10-01", "steps", 9000, stamp["metrics"])],
        samples=[("heart_rate", "2026-10-01T12:00:00Z", 70, stamp["samples"])],
        sessions=[("sleep", "2026-09-30T22:00:00Z", stamp["sessions"])],
    )

    assert detail(client).get_json()["last_sync"] == "2026-10-02 06:00:00"


def test_last_sync_is_this_sources_own(client, db_path):
    """Another source's later sync, or a migrated reading with no stamp, doesn't count."""
    add_garmin(client)
    seed(db_path, "garmin", metrics=[
        ("2026-10-01", "steps", 9000, "2026-10-01 06:00:00"),
        ("2026-10-02", "steps", 9100, None),
    ])
    seed(db_path, "google_health", metrics=[("2026-10-03", "steps", 8000, "2026-10-03 06:00:00")])

    assert detail(client).get_json()["last_sync"] == "2026-10-01 06:00:00"


def test_last_sync_is_null_before_the_first_sync(client):
    add_garmin(client)

    assert detail(client).get_json()["last_sync"] is None


def test_detail_of_a_source_that_isnt_connected_is_null(client, db_path):
    # Data left behind by a source removed without a wipe isn't an account.
    seed(db_path, "garmin", metrics=[("2026-10-01", "steps", 9000, "2026-10-01 06:00:00")])

    assert detail(client).get_json() == {
        "connected_via": "credentials",
        "login": None,
        "added": None,
        "last_sync": None,
        "sync_hours": 6,
        "sync_is_default": True,
    }


def test_detail_of_an_unknown_connector_is_a_404(client):
    resp = detail(client, "nope")

    assert resp.status_code == 404
    assert resp.get_json()["ok"] is False


def test_detail_never_returns_the_password(client):
    add_garmin(client)

    body = detail(client).get_data(as_text=True)
    assert "hunter2" not in body
    assert "password" not in body


def test_detail_reads_the_manifest_not_the_stored_blob(client, conn):
    """Only non-password manifest fields come back, whatever else is stored.

    An oauth connector stores its refresh token beside its manifest
    fields; a key outside the manifest must never reach the response.
    """
    from crypto import encrypt

    conn.execute(
        "INSERT INTO accounts (plugin_id, credentials, created_at) VALUES (?, ?, ?)",
        ("google_health", encrypt(json.dumps({
            "client_id": "cid-123.apps.googleusercontent.com",
            "client_secret": "shh-secret",
            "refresh_token": "refresh-abc",
            "access_token": "access-xyz",
        })), "2026-10-01T06:00:00+00:00"),
    )
    conn.commit()

    resp = detail(client, "google_health")
    body = resp.get_data(as_text=True)

    assert resp.get_json()["connected_via"] == "oauth"
    # The client id is the one non-password field in its manifest.
    assert resp.get_json()["login"] == "cid-123.apps.googleusercontent.com"
    for secret in ("shh-secret", "refresh-abc", "access-xyz"):
        assert secret not in body


def test_detail_with_an_unreadable_credential_blob_still_answers(client, conn):
    """A lost key leaves no login to show - but the popup, and Remove, still open."""
    conn.execute(
        "INSERT INTO accounts (plugin_id, credentials, created_at) VALUES (?, ?, ?)",
        ("garmin", "not-a-valid-fernet-token", "2026-01-01T00:00:00Z"),
    )
    conn.commit()

    resp = detail(client)
    assert resp.status_code == 200
    assert resp.get_json()["login"] is None
    assert resp.get_json()["added"] == "2026-01-01T00:00:00Z"


# ---------- remove, keeping or wiping the source's data ----------

def rows(db_path, table, source):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE source = ?", (source,)).fetchone()[0]
    finally:
        conn.close()


def derived_pick(db_path, day, metric="steps"):
    """(value, source) derived for one (date, metric), or None."""
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT value, source FROM derived_metrics WHERE date = ? AND metric = ?",
            (day, metric),
        ).fetchone()
        return tuple(row) if row else None
    finally:
        conn.close()


@pytest.fixture
def two_sources(client, db_path):
    """Garmin connected, with data of every grain; Google Health beside it.

    2026-10-01: steps from Garmin only. 2026-10-02: steps from both, so
    Garmin wins it. Each has a sample and a session too.
    """
    from derived import recompute_derived

    add_garmin(client)
    seed(
        db_path, "garmin",
        metrics=[
            ("2026-10-01", "steps", 9000, "2026-10-02 06:00:00"),
            ("2026-10-02", "steps", 9100, "2026-10-02 06:00:00"),
        ],
        samples=[("heart_rate", "2026-10-01T12:00:00Z", 70, "2026-10-02 06:00:00")],
        sessions=[("sleep", "2026-09-30T22:00:00Z", "2026-10-02 06:00:00")],
    )
    seed(
        db_path, "google_health",
        metrics=[("2026-10-02", "steps", 8000, "2026-10-02 07:00:00")],
        samples=[("heart_rate", "2026-10-01T12:00:00Z", 72, "2026-10-02 07:00:00")],
        sessions=[("sleep", "2026-09-30T22:05:00Z", "2026-10-02 07:00:00")],
    )
    conn = sqlite3.connect(db_path)
    recompute_derived(conn)
    conn.close()

    assert derived_pick(db_path, "2026-10-01") == (9000, "garmin")
    assert derived_pick(db_path, "2026-10-02") == (9100, "garmin")


def test_delete_without_wipe_keeps_the_data(client, db_path, two_sources):
    assert client.delete("/api/connectors/garmin").get_json() == {"ok": True}

    assert stored_row(db_path) is None
    for table in ("metrics", "samples", "sessions"):
        assert rows(db_path, table, "garmin") > 0, table
    # Nothing is re-derived: the history it filled stays in the profile.
    assert derived_pick(db_path, "2026-10-01") == (9000, "garmin")
    assert derived_pick(db_path, "2026-10-02") == (9100, "garmin")


@pytest.mark.parametrize("flag", ["0", "no", "false", ""])
def test_only_an_explicit_wipe_wipes(client, db_path, two_sources, flag):
    client.delete(f"/api/connectors/garmin?wipe={flag}")

    assert rows(db_path, "metrics", "garmin") == 2


@pytest.mark.parametrize("flag", ["1", "true", "yes", "TRUE"])
def test_delete_with_wipe_removes_the_sources_data(client, db_path, two_sources, flag):
    assert client.delete(f"/api/connectors/garmin?wipe={flag}").get_json() == {"ok": True}

    assert stored_row(db_path) is None
    for table in ("metrics", "samples", "sessions"):
        assert rows(db_path, table, "garmin") == 0, table
        # The other source's rows are untouched.
        assert rows(db_path, table, "google_health") == 1, table


def test_a_wipe_re_derives_the_days_the_source_covered(client, db_path, two_sources):
    client.delete("/api/connectors/garmin?wipe=1")

    # A day only Garmin filled has nothing left to derive from...
    assert derived_pick(db_path, "2026-10-01") is None
    # ...and a day Google Health also filled falls to it.
    assert derived_pick(db_path, "2026-10-02") == (8000, "google_health")
    conn = sqlite3.connect(db_path)
    assert conn.execute(
        "SELECT COUNT(*) FROM derived_metrics WHERE source = 'garmin'"
    ).fetchone()[0] == 0
    conn.close()


def test_a_wipe_leaves_days_outside_its_range_alone(client, db_path, two_sources):
    """Only the span the wiped readings covered is re-derived."""
    conn = sqlite3.connect(db_path)
    # A derived row outside Garmin's span, deliberately not what a
    # re-derive would pick now: it must survive as it is.
    conn.execute(
        "INSERT INTO derived_metrics (date, metric, value, unit, source, synced_at) "
        "VALUES ('2026-09-01', 'steps', 1234, 'count', 'google_health', 'x')"
    )
    conn.commit()
    conn.close()

    client.delete("/api/connectors/garmin?wipe=1")

    assert derived_pick(db_path, "2026-09-01") == (1234, "google_health")


def test_a_wipe_with_no_readings_still_removes_the_rest(client, db_path):
    """Samples and sessions alone: nothing to re-derive, but they still go."""
    add_garmin(client)
    seed(
        db_path, "garmin",
        samples=[("heart_rate", "2026-10-01T12:00:00Z", 70, "2026-10-02 06:00:00")],
        sessions=[("sleep", "2026-09-30T22:00:00Z", "2026-10-02 06:00:00")],
    )

    assert client.delete("/api/connectors/garmin?wipe=1").status_code == 200

    assert stored_row(db_path) is None
    assert rows(db_path, "samples", "garmin") == 0
    assert rows(db_path, "sessions", "garmin") == 0


def test_a_wipe_also_clears_the_cached_token(client, tmp_path, monkeypatch):
    tokenstore = tmp_path / ".garminconnect"
    tokenstore.mkdir()
    monkeypatch.setenv("GARMIN_TOKENSTORE", str(tokenstore))
    add_garmin(client)

    client.delete("/api/connectors/garmin?wipe=1")

    assert not tokenstore.exists()


def test_a_wipe_of_an_unknown_connector_is_a_404(client):
    assert client.delete("/api/connectors/nope?wipe=1").status_code == 404


def test_a_wipe_is_one_transaction(client, db_path, two_sources, monkeypatch):
    """A re-derive that fails takes the whole removal with it - account included."""
    import main

    def broken(*args, **kwargs):
        raise RuntimeError("re-derive failed")

    monkeypatch.setattr(main, "recompute_derived", broken)

    with pytest.raises(RuntimeError):
        client.delete("/api/connectors/garmin?wipe=1")

    assert stored_row(db_path) is not None
    for table in ("metrics", "samples", "sessions"):
        assert rows(db_path, table, "garmin") > 0, table
    assert derived_pick(db_path, "2026-10-01") == (9000, "garmin")
