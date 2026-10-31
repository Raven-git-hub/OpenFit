"""The Admin's login: the setup code, the password, sessions, the rate
limit, and the guards on every /api route."""

import json
import os

import pytest
from conftest import seed_users

import admin_auth

H = {"X-OpenFit-Admin": "1"}
PASSWORD = "a long password!"
GET_ROUTES = ["/api/status", "/api/op", "/api/backups"]
POST_ROUTES = [
    "/api/check", "/api/update", "/api/undo", "/api/logout",
    "/api/users", "/api/users/sam/start", "/api/users/sam/stop", "/api/users/sam/remove",
    "/api/users/andy/start", "/api/users/andy/stop", "/api/removed/kim/delete-data",
    "/api/users/sam/restart", "/api/users/andy/restart", "/api/library/update", "/api/library/publish",
    "/api/users/restore-from", "/api/users/restore", "/api/schedule/dismiss",
]
# A body every route would act on: a new user, the right confirm, kim's kept data to restore.
BODY = {"name": "lee", "port": 8110, "confirm": "kim", "removed": "kim", "keep_devices": True}


def with_users(env):
    """sam an added user, kim removed with kept data; the calls cleared."""
    seed_users(env, ("sam", 8101, "running"), ("kim", 8102, "running"))
    env.admin.remove_user("kim")
    env.fake.clear()
    env.users_before = (env.data / "users.json").read_text()


def set_up(client, env):
    r = client.post("/api/setup", json={"code": env.auth.setup_code, "password": PASSWORD}, headers=H)
    assert r.status_code == 200
    return r


def page_state(client):
    html = client.get("/").get_data(as_text=True)
    return html.split('data-state="', 1)[1].split('"', 1)[0]


# --- setup ---------------------------------------------------------------------


def test_setup_needs_the_right_code(admin_client, admin_env):
    assert page_state(admin_client) == "setup"
    for code in ("wrong", "", None, 12):
        r = admin_client.post("/api/setup", json={"code": code, "password": PASSWORD}, headers=H)
        assert r.status_code == 403, code
    assert not admin_env.auth.password_set()


def test_setup_needs_a_long_enough_password(admin_client, admin_env):
    code = admin_env.auth.setup_code
    for password in ("short", "eleven char", None):
        r = admin_client.post("/api/setup", json={"code": code, "password": password}, headers=H)
        assert r.status_code == 400, password
        assert "at least 12 characters" in r.get_json()["error"]
    assert not admin_env.auth.password_set()


def test_the_right_code_sets_the_password_and_setup_is_gone(admin_client, admin_env):
    code = admin_env.auth.setup_code
    assert len(code) >= 20

    set_up(admin_client, admin_env)

    assert admin_client.get("/api/status").status_code == 200  # logged in by setup
    assert page_state(admin_client) == "app"
    stored = json.loads((admin_env.data / "auth.json").read_text())
    assert PASSWORD not in json.dumps(stored)
    assert stored["password_hash"].startswith(("scrypt:", "pbkdf2:"))
    assert oct(os.stat(admin_env.data / "auth.json").st_mode & 0o777) == "0o600"
    assert admin_env.auth.setup_code is None
    for body in ({"code": code, "password": "another long one"}, {}):
        assert admin_client.post("/api/setup", json=body, headers=H).status_code == 410


def test_a_restart_with_a_password_set_has_no_setup_code(admin_client, admin_env):
    set_up(admin_client, admin_env)
    assert admin_auth.Auth(str(admin_env.data)).setup_code is None
    fresh = admin_auth.Auth(str(admin_env.data / "elsewhere"))
    assert fresh.setup_code and fresh.setup_code != admin_env.auth.setup_code


# --- login and logout ----------------------------------------------------------


def test_login_and_logout(admin_client, admin_env):
    set_up(admin_client, admin_env)
    admin_client.post("/api/logout", json={}, headers=H)
    assert page_state(admin_client) == "login"
    assert admin_client.get("/api/status").status_code == 401

    assert admin_client.post("/api/login", json={"password": "not the password"}, headers=H).status_code == 401
    r = admin_client.post("/api/login", json={"password": PASSWORD}, headers=H)
    assert r.status_code == 200
    assert admin_client.get("/api/status").status_code == 200

    assert admin_client.post("/api/logout", json={}, headers=H).status_code == 200
    assert admin_client.get("/api/status").status_code == 401


def test_login_before_setup_is_refused(admin_client):
    assert admin_client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 409


def test_the_sixth_bad_login_in_five_minutes_is_429(admin_client, admin_env):
    set_up(admin_client, admin_env)
    admin_client.post("/api/logout", json={}, headers=H)

    for _ in range(5):
        assert admin_client.post("/api/login", json={"password": "nope nope nope"}, headers=H).status_code == 401
    r = admin_client.post("/api/login", json={"password": "nope nope nope"}, headers=H)
    assert r.status_code == 429
    # Even the right password waits.
    assert admin_client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 429
    # Another address isn't held up.
    other = admin_client.post("/api/login", json={"password": PASSWORD}, headers=H,
                              environ_base={"REMOTE_ADDR": "192.168.1.50"})
    assert other.status_code == 200
    admin_client.post("/api/logout", json={}, headers=H)

    admin_env.clock.advance(300)
    assert admin_client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200


def test_the_session_cookie(admin_client, admin_env):
    r = set_up(admin_client, admin_env)
    cookie = r.headers["Set-Cookie"]
    assert cookie.startswith("openfit_admin=")
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert "Secure" not in cookie


def test_a_new_password_ends_older_sessions(admin_client, admin_env):
    set_up(admin_client, admin_env)
    assert admin_client.get("/api/status").status_code == 200
    (admin_env.data / "auth.json").write_text(json.dumps({"password_hash": "scrypt:other"}))
    assert admin_client.get("/api/status").status_code == 401


def test_the_secret_key_is_made_once(tmp_path):
    key = admin_auth.load_secret_key(str(tmp_path))
    assert len(key) == 32
    assert oct(os.stat(tmp_path / "secret_key").st_mode & 0o777) == "0o600"
    assert admin_auth.load_secret_key(str(tmp_path)) == key


# --- the guards ----------------------------------------------------------------


@pytest.mark.parametrize("path", GET_ROUTES + POST_ROUTES)
def test_every_api_route_needs_a_session(admin_client, admin_env, path):
    set_up(admin_client, admin_env)
    admin_client.post("/api/logout", json={}, headers=H)
    admin_env.repos.push({"app/main.py": "print(2)\n"}, "New")
    admin_env.admin.check()  # so an update would otherwise start
    with_users(admin_env)  # and the user routes would otherwise act

    if path in GET_ROUTES:
        r = admin_client.get(path, headers=H)
    else:
        r = admin_client.post(path, json=BODY, headers=H)

    assert r.status_code == 401
    assert r.get_json() == {"ok": False, "error": "Log in first."}
    assert admin_env.fake.calls == []
    assert admin_env.admin.history() == []
    assert (admin_env.data / "users.json").read_text() == admin_env.users_before
    assert os.listdir(admin_env.library) == []  # nothing published


def test_an_unknown_api_route_needs_a_session_too(admin_client):
    assert admin_client.get("/api/anything").status_code == 401


@pytest.mark.parametrize("path", ["/api/setup", "/api/login"] + POST_ROUTES)
def test_a_post_without_the_header_is_403(admin_client, admin_env, path):
    set_up(admin_client, admin_env)
    admin_env.repos.push({"app/main.py": "print(2)\n"}, "New")
    admin_env.admin.check()
    with_users(admin_env)

    for headers in ({}, {"X-OpenFit-Admin": "0"}):
        assert admin_client.post(path, json=BODY, headers=headers).status_code == 403
    # The header, but not JSON.
    assert admin_client.post(path, data={"password": PASSWORD}, headers=H).status_code == 403
    assert admin_env.fake.calls == []
    assert admin_env.admin.history() == []
    assert (admin_env.data / "users.json").read_text() == admin_env.users_before
    assert os.listdir(admin_env.library) == []  # nothing published
    assert admin_client.get("/api/status").status_code == 200  # still logged in


def test_a_check_and_an_update_through_the_routes(admin_client, admin_env):
    set_up(admin_client, admin_env)
    admin_env.repos.push({"app/main.py": "print(2)\n"}, "New")

    r = admin_client.post("/api/check", json={}, headers=H)
    assert r.status_code == 200
    assert r.get_json()["check"]["behind"] == 1

    r = admin_client.post("/api/update", json={}, headers=H)
    assert r.status_code == 202
    assert r.get_json()["op"]["kind"] == "update"
    admin_env.admin.wait(10)
    op = admin_client.get("/api/op").get_json()["op"]
    assert op["outcome"] == "ok"
    backups = admin_client.get("/api/backups").get_json()["backups"]
    assert [b["name"] for b in backups] == [op["users"][0]["backup"]]
    status = admin_client.get("/api/status").get_json()
    assert status["history"][0]["id"] == op["id"]
    assert status["undo"]["id"] == op["id"]
