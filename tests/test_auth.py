"""The user system's one account (U1): locked until a password is set,
set through a one-time setup link, signed into with a session, and every
route but the page, the setup page, their three calls and the webhook
behind it - every write behind the X-OpenFit header too.

Every test here starts signed out, from a client of its own (`guest`);
the shared `client` is signed in by conftest and used only as a second
browser."""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import pytest
from conftest import TEST_PASSWORD, signed_in

import auth
import auth_cli
import main

APP_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app")
H = {"X-OpenFit": "1"}
NEW_PASSWORD = "a brand new password"

# What a signed-out browser may reach, as the brief lists it: the page,
# the setup page, the three calls they make, and the webhook. Each
# (method, rule) - nothing else in the URL map.
EXEMPT = {
    ("GET", "/"),
    ("GET", "/setup"),
    ("GET", "/api/auth/status"),
    ("POST", "/api/auth/login"),
    ("POST", "/api/auth/setup"),
    ("POST", "/api/webhook/"),
    ("POST", "/api/webhook/<token>"),
}


def _routes():
    out = []
    for rule in main.app.url_map.iter_rules():
        for method in sorted(rule.methods - {"HEAD", "OPTIONS"}):
            out.append((method, rule.rule))
    return sorted(out)


ROUTES = _routes()
GUARDED = [r for r in ROUTES if r not in EXEMPT]
WRITES = [r for r in ROUTES if r[0] != "GET"]


def _url(rule):
    # Every <arg> a plain value: the guard answers before any route runs.
    return re.sub(r"<[^>]+>", "x", rule)


@pytest.fixture
def guest(db_path, monkeypatch):
    """A browser that has never signed in, on the temp database."""
    monkeypatch.setitem(main.app.config, "DB_PATH", db_path)
    monkeypatch.setitem(main.app.config, "TESTING", True)
    # No `with`: its requests' contexts aren't kept, so it can be torn
    # down beside the shared client's whatever their order.
    return main.app.test_client()


def auth_path():
    return main.app.config["AUTH_PATH"]


def post(c, url, body=None, headers=H):
    return c.post(url, json=body if body is not None else {}, headers=headers)


def new_link(now=None):
    """A setup token, as auth_cli makes one."""
    return auth.make_setup_token(auth_path(), now=now)[0]


def set_password(c, password=TEST_PASSWORD):
    """`c` sets a password through a fresh setup link, and is signed in."""
    resp = post(c, "/api/auth/setup", {"token": new_link(), "password": password})
    assert resp.status_code == 200, resp.get_json()
    return resp


def status(c):
    return c.get("/api/auth/status").get_json()


def page(resp):
    text = resp.get_data(as_text=True)
    resp.close()
    return text


def view_of(text):
    m = re.search(r'<body data-view="([a-z]+)">', text)
    return m.group(1) if m else None


# --- 1. locked ---------------------------------------------------------------


def test_with_no_password_the_page_is_the_locked_view(guest):
    resp = guest.get("/")
    assert resp.status_code == 200 and "Location" not in resp.headers
    text = page(resp)
    assert view_of(text) == "locked"
    assert "This OpenFit is locked until a password is set. Ask your admin for a setup link." in text
    assert "CUSTOMISE OPENFIT HERE" not in text  # not the app
    assert status(guest) == {"signed_in": False, "password_set": False}
    # Nothing to sign in to yet: no password works, whatever's typed.
    resp = post(guest, "/api/auth/login", {"password": TEST_PASSWORD})
    assert resp.status_code == 409 and resp.get_json()["error"] == auth.LOCKED


def test_the_sweep_covers_the_url_map():
    # Every exempt route is in the map (the list isn't stale), and the
    # sweep below is over every other method of every other rule.
    assert EXEMPT <= set(ROUTES)
    assert len(GUARDED) == len(ROUTES) - len(EXEMPT) >= 40
    assert ("GET", "/api/apps") in GUARDED and ("GET", "/audio/<name>") in GUARDED


@pytest.mark.parametrize("method, rule", GUARDED, ids=lambda v: v if isinstance(v, str) else None)
@pytest.mark.parametrize("password", [False, True], ids=["locked", "signed-out"])
def test_every_other_route_is_a_401_signed_out(guest, client, method, rule, password):
    if password:
        signed_in(client)  # a password set, in another browser
    else:
        os.remove(auth_path())  # the shared client's fixture wrote one
    assert auth.password_set(auth_path()) is password
    resp = guest.open(_url(rule), method=method, headers=H, json={} if method != "GET" else None)
    assert resp.status_code == 401, (method, rule, resp.status_code)
    assert resp.get_json() == {"ok": False, "error": "signed out"}


def test_a_path_that_isnt_a_route_is_a_401_too(guest):
    # Signed out, nothing says which paths exist.
    for method in ("GET", "POST"):
        resp = guest.open("/no/such/page", method=method, headers=H)
        assert resp.status_code == 401 and resp.get_json()["error"] == "signed out"
    # A route asked with a method it hasn't got, likewise.
    assert guest.delete("/api/catalog", headers=H).status_code == 401


def test_the_webhook_works_while_locked(guest, conn):
    conn.execute("INSERT INTO settings (key, value) VALUES ('webhook_token', 'tok-en')")
    conn.commit()
    # No session, no header: a device pushing a reading.
    resp = guest.post("/api/webhook/tok-en", json={"metric": "steps", "value": 1234, "date": "2026-01-02"})
    assert resp.status_code == 200, resp.get_json()
    assert [tuple(r) for r in conn.execute("SELECT source, value FROM metrics")] == [("webhook", 1234.0)]
    # Its own token is still the auth.
    assert guest.post("/api/webhook/wrong", json={"metric": "steps", "value": 1}).status_code == 401
    assert guest.post("/api/webhook/", json={"metric": "steps", "value": 1}).status_code == 401
    # The token itself is behind a session.
    assert guest.get("/api/webhook-token").status_code == 401


# --- 2. setup links ----------------------------------------------------------


def test_a_token_from_the_cli_sets_the_password_and_signs_in(guest, capsys, monkeypatch):
    monkeypatch.setenv("OPENFIT_AUTH_PATH", auth_path())
    assert auth_cli.main(["setup-link"]) == 0
    line = capsys.readouterr().out
    assert line.count("\n") == 1
    made = json.loads(line)
    assert set(made) == {"token", "expires_at"}
    assert re.fullmatch(r"[A-Za-z0-9_-]{40,50}", made["token"])
    expires = datetime.strptime(made["expires_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    assert abs(expires - datetime.now(timezone.utc) - timedelta(hours=24)) < timedelta(minutes=1)

    # The setup page, before and after: the setup view, signed out.
    assert view_of(page(guest.get("/setup"))) == "setup"
    resp = post(guest, "/api/auth/setup", {"token": made["token"], "password": NEW_PASSWORD})
    assert resp.status_code == 200 and resp.get_json() == {"ok": True}
    assert status(guest) == {"signed_in": True, "password_set": True}
    assert guest.get("/api/apps").status_code == 200
    assert "CUSTOMISE OPENFIT HERE" in page(guest.get("/"))
    # And that password signs in another browser.
    with main.app.test_client() as other:
        assert post(other, "/api/auth/login", {"password": NEW_PASSWORD}).status_code == 200


def test_the_cli_is_run_as_the_admin_runs_it(tmp_path):
    # Exactly `python auth_cli.py setup-link`, from the app's folder (the
    # image's WORKDIR), one JSON line out and the token stored hashed.
    path = tmp_path / "elsewhere" / "auth.json"
    env = {**os.environ, "OPENFIT_AUTH_PATH": str(path)}
    done = subprocess.run([sys.executable, "auth_cli.py", "setup-link"], cwd=APP_DIR, env=env,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    made = json.loads(done.stdout)
    assert done.stdout == json.dumps(made) + "\n"
    stored = json.loads(path.read_text())
    assert stored["setup"]["token_hash"] != made["token"] and made["token"] not in path.read_text()
    assert stored["setup"]["expires_at"] == made["expires_at"]
    # Anything else is a usage error, and makes nothing.
    done = subprocess.run([sys.executable, "auth_cli.py"], cwd=APP_DIR, env=env, capture_output=True, text=True)
    assert done.returncode == 2 and "usage" in done.stderr and done.stdout == ""


def test_a_setup_token_works_once(guest):
    token = new_link()
    assert post(guest, "/api/auth/setup", {"token": token, "password": NEW_PASSWORD}).status_code == 200
    with main.app.test_client() as other:
        resp = post(other, "/api/auth/setup", {"token": token, "password": "another long password"})
        assert resp.status_code == 403
        assert resp.get_json() == {"ok": False, "error": auth.BAD_LINK}
        assert not status(other)["signed_in"]
    # The password stands: the second use changed nothing.
    assert auth.read_state(auth_path())["setup"] is None
    assert status(guest)["signed_in"]


def test_an_expired_token_is_refused(guest):
    token = new_link(now=datetime.now(timezone.utc) - timedelta(hours=24, seconds=5))
    resp = post(guest, "/api/auth/setup", {"token": token, "password": NEW_PASSWORD})
    assert resp.status_code == 403 and resp.get_json()["error"] == auth.BAD_LINK
    assert not auth.password_set(auth_path())
    # One still within its 24 hours works.
    token = new_link(now=datetime.now(timezone.utc) - timedelta(hours=23, minutes=59))
    assert post(guest, "/api/auth/setup", {"token": token, "password": NEW_PASSWORD}).status_code == 200


@pytest.mark.parametrize("token", ["wrong", "", None, 12, "ü" * 43, ["x"]])
def test_a_wrong_token_is_refused(guest, token):
    real = new_link()
    resp = post(guest, "/api/auth/setup", {"token": token, "password": NEW_PASSWORD})
    assert resp.status_code == 403 and resp.get_json()["error"] == auth.BAD_LINK
    assert real != token and not auth.password_set(auth_path())


def test_no_token_made_yet_is_refused(guest):
    resp = post(guest, "/api/auth/setup", {"token": "a" * 43, "password": NEW_PASSWORD})
    assert resp.status_code == 403 and not os.path.exists(auth_path())


def test_a_newer_link_replaces_an_older_one(guest):
    older, newer = new_link(), new_link()
    assert post(guest, "/api/auth/setup", {"token": older, "password": NEW_PASSWORD}).status_code == 403
    assert post(guest, "/api/auth/setup", {"token": newer, "password": NEW_PASSWORD}).status_code == 200


@pytest.mark.parametrize("password", ["short", "x" * 11, "", None, 123456789012, "x" * 1025])
def test_a_bad_password_is_refused_and_the_link_kept(guest, password):
    token = new_link()
    resp = post(guest, "/api/auth/setup", {"token": token, "password": password})
    assert resp.status_code == 400
    assert "at least 12" in resp.get_json()["error"] or "at most 1024" in resp.get_json()["error"]
    assert not auth.password_set(auth_path()) and not status(guest)["signed_in"]
    # Not used up: tried again with a good one, it works.
    assert post(guest, "/api/auth/setup", {"token": token, "password": "x" * 12}).status_code == 200


def test_auth_json_is_private_and_holds_no_token_or_password(guest):
    token = new_link()
    path = auth_path()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    text = open(path).read()
    assert token not in text and auth._token_hash(token) in text
    set_password(guest, NEW_PASSWORD)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    text = open(path).read()
    assert NEW_PASSWORD not in text and "setup" in json.loads(text) and json.loads(text)["setup"] is None
    # Nothing left beside it: the write went through a temp file, renamed.
    assert sorted(n for n in os.listdir(os.path.dirname(path)) if n.startswith(("auth", ".auth"))) == [
        "auth.json", "auth.json.lock"]
    # And the sessions' key is its own file, private too - not the
    # credentials' .secret_key.
    key = main.app.config["SESSION_KEY_PATH"]
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600 and len(open(key, "rb").read()) == 32
    assert os.path.basename(key) == ".session_key"


def test_auth_json_that_wont_read_is_locked_never_open(guest):
    for junk in ("", "not json", "[]", '{"password_hash": 5}', '{"session_tag": "x"}'):
        with open(auth_path(), "w") as f:
            f.write(junk)
        assert view_of(page(guest.get("/"))) == "locked", junk
        assert guest.get("/api/apps").status_code == 401, junk


# --- 3. signing in -----------------------------------------------------------


def test_the_right_password_signs_in_and_a_wrong_one_doesnt(guest, client):
    signed_in(client)  # the password, set
    assert view_of(page(guest.get("/"))) == "signin"
    assert status(guest) == {"signed_in": False, "password_set": True}
    resp = post(guest, "/api/auth/login", {"password": "not the password"})
    assert resp.status_code == 401 and resp.get_json() == {"ok": False, "error": "That password isn't right."}
    assert guest.get("/api/apps").status_code == 401
    for body in ({}, {"password": None}, {"password": 5}):
        assert guest.post("/api/auth/login", json=body, headers=H).status_code == 401
    resp = post(guest, "/api/auth/login", {"password": TEST_PASSWORD})
    assert resp.status_code == 200 and resp.get_json() == {"ok": True}
    assert status(guest) == {"signed_in": True, "password_set": True}
    assert guest.get("/api/apps").status_code == 200


def test_the_sixth_failure_in_five_minutes_is_a_429(guest, client, monkeypatch):
    signed_in(client)
    clock = [1_000_000.0]
    monkeypatch.setattr(main, "_fails", auth.Fails(clock=lambda: clock[0]))
    for _ in range(5):
        assert post(guest, "/api/auth/login", {"password": "wrong"}).status_code == 401
        clock[0] += 30
    # The sixth - even the right password - is refused, until the oldest
    # failure is five minutes old.
    resp = post(guest, "/api/auth/login", {"password": TEST_PASSWORD})
    assert resp.status_code == 429 and resp.get_json()["error"] == auth.TOO_MANY
    assert not status(guest)["signed_in"]
    clock[0] = 1_000_000.0 + 299
    assert post(guest, "/api/auth/login", {"password": TEST_PASSWORD}).status_code == 429
    clock[0] = 1_000_000.0 + 301
    assert post(guest, "/api/auth/login", {"password": TEST_PASSWORD}).status_code == 200
    # A success clears the count.
    assert main._fails._fails == {}


def test_logout_ends_the_session(guest):
    set_password(guest)
    assert guest.get("/api/apps").status_code == 200
    resp = post(guest, "/api/auth/logout")
    assert resp.status_code == 200 and resp.get_json() == {"ok": True}
    assert status(guest)["signed_in"] is False
    assert guest.get("/api/apps").status_code == 401
    assert view_of(page(guest.get("/"))) == "signin"
    # Signed out, signing out again is a 401 like any other call.
    assert post(guest, "/api/auth/logout").status_code == 401


def test_a_reset_through_setup_signs_every_other_session_out(guest, client):
    set_password(guest)
    with main.app.test_client() as phone:
        assert post(phone, "/api/auth/login", {"password": TEST_PASSWORD}).status_code == 200
        assert phone.get("/api/apps").status_code == 200
        before = auth.read_state(auth_path())["session_tag"]
        # A second link, used in a third browser: a new password.
        with main.app.test_client() as laptop:
            set_password(laptop, NEW_PASSWORD)
            assert laptop.get("/api/apps").status_code == 200
        assert auth.read_state(auth_path())["session_tag"] != before
        # Both older sessions are over.
        assert phone.get("/api/apps").status_code == 401
        assert guest.get("/api/apps").status_code == 401
        # The old password is gone too; the new one signs in.
        assert post(phone, "/api/auth/login", {"password": TEST_PASSWORD}).status_code == 401
        assert post(phone, "/api/auth/login", {"password": NEW_PASSWORD}).status_code == 200


def test_a_session_from_another_key_or_tag_isnt_one(guest, client):
    signed_in(client)
    with guest.session_transaction() as sess:
        sess["auth"] = "not the tag"
    assert guest.get("/api/apps").status_code == 401
    with guest.session_transaction() as sess:
        sess["auth"] = 5
    assert guest.get("/api/apps").status_code == 401
    # The shared client's cookie, signed under this test's key, works -
    # and the same cookie under another key (another user's OpenFit on
    # the same host) doesn't.
    assert client.get("/api/apps").status_code == 200
    auth._keys[main.app.config["SESSION_KEY_PATH"]] = b"k" * 32
    assert client.get("/api/apps").status_code == 401


# --- 4. CSRF -----------------------------------------------------------------


@pytest.mark.parametrize("method, rule", [r for r in WRITES if not r[1].startswith("/api/webhook/")],
                         ids=lambda v: v if isinstance(v, str) else None)
def test_a_write_without_the_header_is_a_403(guest, client, method, rule):
    signed_in(client)
    del client.environ_base["HTTP_X_OPENFIT"]
    for headers in ({}, {"X-OpenFit": "0"}, {"X-OpenFit": "yes"}):
        resp = client.open(_url(rule), method=method, json={}, headers=headers)
        assert resp.status_code == 403, (method, rule, headers)
        assert resp.get_json()["error"] == "This request didn't come from OpenFit's page."
    # Signed out too - the sign-in and setup calls included.
    assert guest.open(_url(rule), method=method, json={}).status_code == 403


def test_the_header_lets_a_write_through(client):
    del client.environ_base["HTTP_X_OPENFIT"]
    assert client.put("/api/settings/x", json={"value": "1"}).status_code == 403
    assert client.put("/api/settings/x", json={"value": "1"}, headers=H).status_code == 200
    # A read needs no header.
    assert client.get("/api/settings/x").get_json()["value"] == "1"


def test_the_webhook_is_exempt_from_the_header(guest, conn):
    conn.execute("INSERT INTO settings (key, value) VALUES ('webhook_token', 'tok-en')")
    conn.commit()
    assert guest.post("/api/webhook/tok-en", json={"metric": "steps", "value": 5}).status_code == 200


# --- 5. the cookie -----------------------------------------------------------


def _cookie(resp):
    [header] = [v for k, v in resp.headers.items() if k == "Set-Cookie"]
    return header


def test_the_session_cookie(guest, client):
    signed_in(client)
    resp = post(guest, "/api/auth/login", {"password": TEST_PASSWORD})
    cookie = _cookie(resp)
    parts = [p.strip() for p in cookie.split(";")]
    assert parts[0].startswith("openfit_session_80=")  # named by the port: none is 80
    flags = {p.split("=", 1)[0].lower(): (p.split("=", 1) + [""])[1] for p in parts[1:]}
    assert "httponly" in flags
    assert flags["samesite"] == "Lax"
    assert "secure" not in flags  # plain HTTP on the LAN: docs/architecture/auth.md
    assert flags["path"] == "/"
    expires = parsedate_to_datetime(flags["expires"])
    assert abs(expires - datetime.now(timezone.utc) - timedelta(days=30)) < timedelta(minutes=2)
    # Renewed on use: any signed-in request sets it again, a fresh 30 days.
    again = _cookie(guest.get("/api/apps"))
    assert again.startswith("openfit_session_80=") and "Expires=" in again
    # Signing out deletes it.
    out = _cookie(post(guest, "/api/auth/logout"))
    assert out.startswith("openfit_session_80=;") and "1970" in out


def test_a_signed_out_request_sets_no_cookie(guest):
    for url in ("/", "/setup", "/api/auth/status", "/api/apps"):
        resp = guest.get(url)
        resp.close()
        assert "Set-Cookie" not in resp.headers, url



# --- 6. one cookie per port --------------------------------------------------

# A browser keeps one cookie of a name per host, whatever the port - as the
# test client does - so the cookie is named by the port: two users'
# OpenFits on one server never overwrite each other's session.

SERVER = "192.168.1.127"


@pytest.fixture
def two_openfits(tmp_path, db_path, monkeypatch):
    """Two users' OpenFits on one server, at :8080 and :8101 - each its own
    account (auth.json: a password and its own session tag) and session
    key, as each container has its own /data - told apart by the port a
    request comes to. And one browser, that has never signed in to
    either."""
    from werkzeug.security import generate_password_hash

    monkeypatch.setitem(main.app.config, "DB_PATH", db_path)
    monkeypatch.setitem(main.app.config, "TESTING", True)
    hashed = generate_password_hash(TEST_PASSWORD)
    openfits = {}
    for port, tag in (("8080", "a1" * 16), ("8101", "b2" * 16)):
        folder = tmp_path / f"openfit-{port}"
        folder.mkdir()
        (folder / "auth.json").write_text(json.dumps({"password_hash": hashed, "session_tag": tag, "setup": None}))
        openfits[port] = (str(folder / "auth.json"), str(folder / ".session_key"))
    inner = main.app.wsgi_app

    def by_port(environ, start_response):
        # conftest's monkeypatch puts both paths back after the test.
        main.app.config["AUTH_PATH"], main.app.config["SESSION_KEY_PATH"] = openfits[
            environ["HTTP_HOST"].rpartition(":")[2]]
        return inner(environ, start_response)

    monkeypatch.setattr(main.app, "wsgi_app", by_port)
    return main.app.test_client()


def at(port):
    return f"http://{SERVER}:{port}"


def signed_in_at(browser, port):
    """Whether `browser` is signed in to the OpenFit at `port`: its status,
    and a guarded route - which answers 401 when it isn't."""
    signed = browser.get("/api/auth/status", base_url=at(port)).get_json()["signed_in"]
    apps = browser.get("/api/apps", base_url=at(port)).status_code
    assert apps == (200 if signed else 401), (port, signed, apps)
    return signed


def test_signing_in_to_one_openfit_keeps_the_browser_signed_in_to_the_other(two_openfits):
    browser = two_openfits

    resp = browser.post("/api/auth/login", json={"password": TEST_PASSWORD}, headers=H, base_url=at(8080))
    assert resp.status_code == 200
    assert (signed_in_at(browser, 8080), signed_in_at(browser, 8101)) == (True, False)

    # Signing in to the second leaves the first signed in.
    resp = browser.post("/api/auth/login", json={"password": TEST_PASSWORD}, headers=H, base_url=at(8101))
    assert resp.status_code == 200
    assert (signed_in_at(browser, 8080), signed_in_at(browser, 8101)) == (True, True)
    # A cookie each, for the one host, by the port.
    assert browser.get_cookie("openfit_session_8080", domain=SERVER) is not None
    assert browser.get_cookie("openfit_session_8101", domain=SERVER) is not None
    assert browser.get_cookie("openfit_session", domain=SERVER) is None

    # And signing out of one leaves the other.
    assert browser.post("/api/auth/logout", headers=H, base_url=at(8101)).status_code == 200
    assert (signed_in_at(browser, 8080), signed_in_at(browser, 8101)) == (True, False)
    assert browser.get_cookie("openfit_session_8101", domain=SERVER) is None


@pytest.mark.parametrize("host, name", [
    (f"{SERVER}:8080", "openfit_session_8080"),
    (f"{SERVER}:8101", "openfit_session_8101"),
    ("openfit.lan:65535", "openfit_session_65535"),
    ("[::1]:8101", "openfit_session_8101"),
    # No port - nor 80 itself, which the request's host leaves out - is 80.
    (SERVER, "openfit_session_80"),
    ("localhost", "openfit_session_80"),
    (f"{SERVER}:80", "openfit_session_80"),
    ("[::1]", "openfit_session_80"),
    # A host that isn't one falls back to 80.
    ("a:b:c", "openfit_session_80"),
    ("x:99999999", "openfit_session_80"),
    ("x:", "openfit_session_80"),
])
def test_the_cookie_is_named_by_the_port(guest, client, host, name):
    signed_in(client)
    resp = guest.post("/api/auth/login", json={"password": TEST_PASSWORD}, headers={**H, "Host": host})
    assert resp.status_code == 200
    assert _cookie(resp).startswith(f"{name}=")
    # Signing out deletes that one.
    out = _cookie(guest.post("/api/auth/logout", headers={**H, "Host": host}))
    assert out.startswith(f"{name}=;") and "1970" in out


@pytest.mark.parametrize("host, port", [
    ("192.168.1.127:8080", "8080"), ("h:1", "1"), ("h:65535", "65535"), ("h:00080", "00080"),
    ("[::1]:8101", "8101"),
    # The digits after the last ':', 1 to 5 of them - else 80.
    ("", "80"), ("localhost", "80"), ("8080", "80"), ("a:b:c", "80"), ("x:99999999", "80"),
    ("x:123456", "80"), ("x:", "80"), ("x:80a", "80"), ("x:-1", "80"), ("x: 81", "80"),
    ("x:8080\n", "80"), ("x:\u0668\u0660\u0668\u0660", "80"), ("[::1]", "80"),
])
def test_the_port_is_the_digits_after_the_last_colon(host, port):
    assert main.session_cookie_name("openfit_session", host) == f"openfit_session_{port}"


def test_outside_a_request_the_cookie_is_openfit_session():
    assert main.app.session_interface.get_cookie_name(main.app) == "openfit_session"
    with main.app.test_request_context(base_url=at(8101)):
        assert main.app.session_interface.get_cookie_name(main.app) == "openfit_session_8101"

# --- 7. the page's three views -----------------------------------------------


def _views(guest, client):
    os.remove(auth_path())  # the shared client's fixture set a password
    locked = page(guest.get("/"))
    setup = page(guest.get("/setup#" + "t" * 43))
    signed_in(client)
    signin = page(guest.get("/"))
    return {"locked": locked, "setup": setup, "signin": signin}


def test_each_view_renders_with_no_external_url(guest, client):
    for view, text in _views(guest, client).items():
        assert view_of(text) == view
        assert re.search(rf'<section id="{view}" hidden>', text), view
        # Self-contained, as index.html is: nothing fetched from anywhere.
        for banned in ("http:", "https:", "<link", "@import", "url(", " src=", " href=", "{{", "{%"):
            assert banned not in text, (view, banned)
        # Its calls are this OpenFit's own, with the header.
        assert set(re.findall(r"post\('([^']*)'", text)) == {"/api/auth/login", "/api/auth/setup"}
        assert "'X-OpenFit':'1'" in text
        # Light and dark, on the page's own tokens.
        assert "@media (prefers-color-scheme:dark)" in text and ':root[data-theme="dark"]' in text
        assert "localStorage.getItem('of-theme')" in text


def test_the_views_are_never_cached(guest, client):
    for url in ("/", "/setup"):
        resp = guest.get(url)
        resp.close()
        assert resp.status_code == 200 and resp.headers["Cache-Control"] == "no-store"
    signed_in(client)
    resp = client.get("/")
    resp.close()
    assert resp.status_code == 200 and resp.headers["Cache-Control"] == "no-store"


def test_the_setup_view_reads_the_token_from_the_fragment(guest):
    text = page(guest.get("/setup"))
    assert "const token = location.hash.slice(1);" in text
    assert "post('/api/auth/setup', {token, password})" in text
    # Signed in, the link's address leaves this tab's history.
    assert "const enter = () => location.replace('/');" in text


@pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")
def test_the_setup_view_checks_and_posts(guest, tmp_path):
    # The view's script, run as written over a stand-in page: a short or
    # mismatched password is said and nothing sent; a good one posts the
    # fragment's token with the header, then goes to /.
    text = page(guest.get("/setup"))
    script = re.search(r"<script>\n(.*?)</script>", text, re.S).group(1)
    harness = r"""
    const nodes = {};
    const node = id => nodes[id] || (nodes[id] = {id, hidden:true, value:'', textContent:'', disabled:false,
      focus(){}, select(){}, querySelectorAll(){ return []; }});
    globalThis.document = {getElementById:node, body:{dataset:{view:'setup'}}, documentElement:{setAttribute(){}}};
    globalThis.localStorage = {getItem(){ return null; }};
    const went = [], sent = [];
    globalThis.location = {hash:'#TOKEN', replace:u => went.push(u)};
    globalThis.fetch = async (url, opts) => { sent.push([url, opts]); return {status:200, json:async () => ({ok:true})}; };
    SCRIPT
    (async () => {
      const form = nodes.setupForm, ev = {preventDefault(){}};
      const r = {shown: nodes.setup.hidden === false};
      node('setupPassword').value = 'short'; node('setupAgain').value = 'short';
      await form.onsubmit(ev); r.short = [node('setupMsg').textContent, sent.length];
      node('setupPassword').value = 'long enough pass'; node('setupAgain').value = 'long enough pasz';
      await form.onsubmit(ev); r.mismatch = [node('setupMsg').textContent, sent.length];
      node('setupAgain').value = 'long enough pass';
      await form.onsubmit(ev);
      r.sent = sent.map(([u, o]) => [u, o.method, o.headers, JSON.parse(o.body)]);
      r.went = went;
      console.log(JSON.stringify(r));
    })().catch(e => { console.error(e); process.exit(1); });
    """.replace("SCRIPT", script)
    out = subprocess.run(["node", "-e", harness], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    r = json.loads(out.stdout)
    assert r["shown"] is True
    assert r["short"] == ["The password needs at least 12 characters.", 0]
    assert r["mismatch"] == ["The two passwords aren't the same.", 0]
    assert r["sent"] == [["/api/auth/setup", "POST", {"Content-Type": "application/json", "X-OpenFit": "1"},
                          {"token": "TOKEN", "password": "long enough pass"}]]
    assert r["went"] == ["/"]
