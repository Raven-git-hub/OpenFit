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

from secrets import decrypt


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
    results from CI, since the env is the fallback credential source.
    """
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


def test_garmin_manifest_is_a_credentials_form(client):
    garmin = connectors(client)["garmin"]

    assert garmin["name"] == "Garmin Connect"
    assert garmin["add_flow"] == "credentials"
    assert [(f["key"], f["type"], f["required"]) for f in garmin["fields"]] == [
        ("email", "text", True),
        ("password", "password", True),
    ]


def test_google_health_is_declared_but_oauth(client):
    """Declared so the API describes it; the UI filters oauth out for now."""
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
