"""
The OAuth add flow - "paste the code" - end to end, minus the network.

Adding an OAuth device is two calls: /oauth/start hands back a consent URL
and keeps the PKCE verifier server-side, /oauth/finish takes whatever the
provider left in the address bar and trades it for a refresh token. These
tests pin that contract, the storage it ends in (encrypted, refresh token
included) and the ways it is allowed to fail.

No network: the only outbound call in the flow is the token exchange, and
that is stubbed here. Google is never contacted.
"""

import base64
import hashlib
import json
import sqlite3
import urllib.parse

import pytest

from crypto import decrypt

CLIENT = {"client_id": "cid-123.apps.googleusercontent.com", "client_secret": "shh-secret"}
REDIRECT_URI = "http://127.0.0.1:9109/"


@pytest.fixture(autouse=True)
def no_device_env(monkeypatch):
    """Nothing is configured from the shell - the env is a fallback source."""
    for var in (
        "GARMIN_EMAIL",
        "GARMIN_PASSWORD",
        "GOOGLE_HEALTH_CLIENT_ID",
        "GOOGLE_HEALTH_CLIENT_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def no_pending_state():
    """The pending store is module state; don't leak it between tests."""
    import main

    main._oauth_pending.clear()
    yield
    main._oauth_pending.clear()


@pytest.fixture
def token_endpoint(monkeypatch):
    """Stub the one HTTP call the flow makes, and record what it was sent."""
    calls = []
    responses = []

    class FakeResponse:
        def __init__(self, payload, ok=True, text=""):
            self._payload = payload
            self.ok = ok
            self.text = text or json.dumps(payload)

        def json(self):
            return self._payload

        def raise_for_status(self):
            if not self.ok:
                raise RuntimeError(self.text)

    def fake_post(url, data=None, timeout=None, **kwargs):
        calls.append({"url": url, "data": data or {}})
        return responses.pop(0) if responses else FakeResponse(
            {"refresh_token": "refresh-abc", "access_token": "access-xyz"}
        )

    monkeypatch.setattr("plugins.google_health.plugin.requests.post", fake_post)
    return type("TokenEndpoint", (), {
        "calls": calls,
        "queue": lambda self, payload, ok=True, text="": responses.append(
            FakeResponse(payload, ok, text)
        ),
    })()


def start(client, values=None):
    return client.post("/api/connectors/google_health/oauth/start", json=values or CLIENT)


def finish(client, code):
    return client.post("/api/connectors/google_health/oauth/finish", json={"code": code})


def query_of(url):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)


def stored_row(db_path, plugin_id="google_health"):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(
            "SELECT credentials FROM accounts WHERE plugin_id = ?", (plugin_id,)
        ).fetchone()
    finally:
        conn.close()


# ---------- step one: the consent URL ----------

def test_start_returns_a_consent_url_for_this_client(client):
    resp = start(client)

    assert resp.status_code == 200
    url = resp.get_json()["auth_url"]
    assert url.startswith("https://accounts.google.com/o/oauth2/v2/auth?")

    params = query_of(url)
    assert params["client_id"] == [CLIENT["client_id"]]
    assert params["response_type"] == ["code"]
    assert params["redirect_uri"] == [REDIRECT_URI]
    # offline + forced consent is what makes Google issue a refresh token.
    assert params["access_type"] == ["offline"]
    assert params["prompt"] == ["consent"]


def test_start_asks_for_the_plugins_scopes(client):
    from plugins.google_health.plugin import SCOPES

    scopes = query_of(start(client).get_json()["auth_url"])["scope"][0].split()

    assert scopes == SCOPES


def test_start_carries_the_pkce_challenge_for_the_stashed_verifier(client):
    import main

    params = query_of(start(client).get_json()["auth_url"])
    assert params["code_challenge_method"] == ["S256"]

    pending = main._oauth_pending["google_health"]
    assert pending["credentials"] == CLIENT
    # The challenge in the URL must be the S256 hash of the verifier held
    # back here - otherwise the exchange in step two cannot succeed.
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(pending["code_verifier"].encode()).digest()
    ).rstrip(b"=").decode()
    assert params["code_challenge"] == [expected]


def test_start_stores_nothing_until_the_flow_finishes(client, db_path):
    start(client)

    assert stored_row(db_path) is None
    connectors = {c["id"]: c for c in client.get("/api/connectors").get_json()}
    assert connectors["google_health"]["connected"] is False


def test_start_validates_the_manifest_fields(client):
    resp = start(client, {"client_id": "cid"})

    assert resp.status_code == 400
    assert "OAuth client secret" in resp.get_json()["error"]


# ---------- step two: the pasted code ----------

def test_finish_stores_the_refresh_token_encrypted(client, db_path, token_endpoint):
    assert start(client).status_code == 200

    resp = finish(client, "4/code-from-google")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}

    stored = json.loads(decrypt(stored_row(db_path)[0]))
    assert stored == {
        "client_id": CLIENT["client_id"],
        "client_secret": CLIENT["client_secret"],
        "refresh_token": "refresh-abc",
    }


def test_finish_exchanges_the_code_with_the_matching_verifier(client, token_endpoint):
    import main

    start(client)
    verifier = main._oauth_pending["google_health"]["code_verifier"]

    finish(client, "4/code-from-google")

    sent = token_endpoint.calls[0]
    assert sent["url"] == "https://oauth2.googleapis.com/token"
    assert sent["data"]["grant_type"] == "authorization_code"
    assert sent["data"]["code"] == "4/code-from-google"
    assert sent["data"]["code_verifier"] == verifier
    assert sent["data"]["redirect_uri"] == REDIRECT_URI


def test_the_connector_reads_back_connected(client, token_endpoint):
    start(client)
    finish(client, "4/code-from-google")

    google = {c["id"]: c for c in client.get("/api/connectors").get_json()}["google_health"]
    assert google["connected"] is True
    assert google["configured"] is True


def test_the_secret_and_refresh_token_are_only_ciphertext_on_disk(client, db_path, token_endpoint):
    start(client)
    finish(client, "4/code-from-google")

    with open(db_path, "rb") as f:
        raw = f.read()
    assert b"shh-secret" not in raw
    assert b"refresh-abc" not in raw

    # ...and they never come back out over the API either.
    body = client.get("/api/connectors").get_data(as_text=True)
    assert "shh-secret" not in body
    assert "refresh-abc" not in body


@pytest.mark.parametrize("pasted", [
    "http://127.0.0.1:9109/?code=4/pasted-url&scope=https://www.googleapis.com/auth/x",
    "127.0.0.1:9109/?code=4/pasted-url&scope=x",
    "?code=4/pasted-url",
    "  4/pasted-url  ",
])
def test_finish_takes_the_whole_pasted_address_or_just_the_code(client, token_endpoint, pasted):
    start(client)

    assert finish(client, pasted).status_code == 200
    assert token_endpoint.calls[0]["data"]["code"] == "4/pasted-url"


def test_finish_without_a_code_says_so(client, token_endpoint):
    start(client)

    resp = finish(client, "http://127.0.0.1:9109/?error=access_denied")

    assert resp.status_code == 400
    assert "authorization code" in resp.get_json()["error"]
    # A bad paste doesn't burn the pending authorization - retry is free.
    import main
    assert "google_health" in main._oauth_pending


def test_finish_without_a_refresh_token_explains_the_fix(client, db_path, token_endpoint):
    """Google only issues one on first consent; the fix is to revoke."""
    start(client)
    token_endpoint.queue({"access_token": "access-only"})

    resp = finish(client, "4/code-from-google")

    assert resp.status_code == 400
    error = resp.get_json()["error"]
    assert "refresh token" in error
    assert "myaccount.google.com/permissions" in error
    # Nothing half-authorised was stored.
    assert stored_row(db_path) is None


def test_finish_reports_a_rejected_code(client, db_path, token_endpoint):
    start(client)
    token_endpoint.queue({"error": "invalid_grant"}, ok=False, text='{"error": "invalid_grant"}')

    resp = finish(client, "4/stale-code")

    assert resp.status_code == 400
    assert "invalid_grant" in resp.get_json()["error"]
    assert stored_row(db_path) is None


def test_finish_without_a_start_is_a_400(client, token_endpoint):
    resp = finish(client, "4/code-from-google")

    assert resp.status_code == 400
    assert "expired" in resp.get_json()["error"]
    assert token_endpoint.calls == []


def test_an_expired_pending_authorization_is_not_usable(client, token_endpoint):
    import main

    start(client)
    main._oauth_pending["google_health"]["expires_at"] -= main.OAUTH_PENDING_TTL_SECONDS + 1

    assert finish(client, "4/code-from-google").status_code == 400


def test_the_pending_state_is_consumed_by_a_successful_finish(client, token_endpoint):
    import main

    start(client)
    finish(client, "4/code-from-google")

    assert main._oauth_pending == {}


# ---------- the flow only applies to oauth connectors ----------

def test_oauth_endpoints_reject_a_credentials_connector(client):
    for route in ("start", "finish"):
        resp = client.post(f"/api/connectors/garmin/oauth/{route}",
                           json={"code": "x", **CLIENT})
        assert resp.status_code == 400, route
        assert "not an OAuth connector" in resp.get_json()["error"]


def test_oauth_endpoints_404_an_unknown_connector(client):
    assert client.post("/api/connectors/nope/oauth/start", json={}).status_code == 404
    assert client.post("/api/connectors/nope/oauth/finish", json={}).status_code == 404


def test_the_plain_add_route_points_an_oauth_connector_at_the_right_flow(client, db_path):
    resp = client.post("/api/connectors/google_health", json=CLIENT)

    assert resp.status_code == 400
    assert "oauth/start" in resp.get_json()["error"]
    # Client details alone would look connected and never sync.
    assert stored_row(db_path) is None


# ---------- what the sync then uses ----------

def test_sync_refreshes_with_the_stored_refresh_token(client, conn, token_endpoint):
    """The refresh token comes out of the account blob, not a file."""
    from plugins import PLUGINS

    start(client)
    finish(client, "4/code-from-google")

    assert PLUGINS["google_health"]._get_access_token(conn) == "access-xyz"

    refresh = token_endpoint.calls[-1]["data"]
    assert refresh["grant_type"] == "refresh_token"
    assert refresh["refresh_token"] == "refresh-abc"
    assert refresh["client_id"] == CLIENT["client_id"]


def test_a_legacy_token_file_still_works(conn, tmp_path, monkeypatch, token_endpoint):
    """A pre-UI install authorised by authorize.py keeps syncing.

    Its account blob has no refresh token - the one on disk is the only
    one it has - so the file stays a fallback rather than dead weight.
    """
    from plugins import PLUGINS

    tokenstore = tmp_path / ".google_health_token.json"
    tokenstore.write_text(json.dumps({"refresh_token": "from-the-file"}))
    monkeypatch.setenv("GOOGLE_HEALTH_TOKENSTORE", str(tokenstore))
    monkeypatch.setenv("GOOGLE_HEALTH_CLIENT_ID", CLIENT["client_id"])
    monkeypatch.setenv("GOOGLE_HEALTH_CLIENT_SECRET", CLIENT["client_secret"])

    PLUGINS["google_health"]._get_access_token(conn)

    assert token_endpoint.calls[-1]["data"]["refresh_token"] == "from-the-file"


def test_no_refresh_token_anywhere_is_a_clear_error(conn, tmp_path, monkeypatch):
    from plugins import PLUGINS

    monkeypatch.setenv("GOOGLE_HEALTH_TOKENSTORE", str(tmp_path / "never-authorised.json"))

    with pytest.raises(RuntimeError, match="not authorised"):
        PLUGINS["google_health"]._get_access_token(conn)
