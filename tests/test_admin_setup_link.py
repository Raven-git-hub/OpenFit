"""The Admin's setup links (U1): a user's own `python auth_cli.py
setup-link`, run in their running container by docker exec, and the link
built from it handed back once - stored nowhere, logged nowhere.

docker is conftest's FakeRunner, whose setup-link exec prints SETUP_TOKEN;
the user system's side of the token is test_auth.py's."""

import json
import os

import pytest
from conftest import CONTAINERS, SETUP_EXPIRES, SETUP_TOKEN, container_id, seed_users

import admin_ops

H = {"X-OpenFit-Admin": "1"}
PASSWORD = "a long password!"
C0 = CONTAINERS[0]
EXEC = ["python", "auth_cli.py", "setup-link"]


@pytest.fixture
def logged_in(admin_client, admin_env):
    admin_env.auth.setup(admin_env.auth.setup_code, PASSWORD)
    return admin_client


def execs(env):
    return [a for a in env.fake.commands() if a[1] == "exec"]


def link_for(client, name, host="192.168.1.10:8090"):
    """The Setup link button, in a browser that reached the Admin at `host`
    (logged in there: a cookie is the host's)."""
    at = {"HTTP_HOST": host}
    assert client.post("/api/login", json={"password": PASSWORD}, headers=H, environ_overrides=at).status_code == 200
    return client.post(f"/api/users/{name}/setup-link", json={}, headers=H, environ_overrides=at)


# --- the command --------------------------------------------------------------


def test_the_exact_exec_in_the_users_own_container(admin_env):
    seed_users(admin_env, ("sam", 8101, "running"))
    made = admin_env.admin.setup_link("andy", "192.168.1.10:8090")
    assert made == {"link": f"http://192.168.1.10:8080/setup#{SETUP_TOKEN}", "expires_at": SETUP_EXPIRES}
    # Found by its labels, then exactly the CLI - nothing variable but the id.
    assert admin_env.fake.commands() == [
        ["docker", "ps", "-a", "--no-trunc", "--filter", "label=com.docker.compose.project=openfit",
         "--filter", "label=com.docker.compose.service=tracker", "--filter", "label=com.docker.compose.oneoff=False",
         "--format", "{{.ID}}\t{{.State}}"],
        ["docker", "exec", C0, *EXEC],
    ]
    admin_env.fake.clear()
    admin_env.admin.setup_link("sam", "192.168.1.10:8090")
    assert execs(admin_env) == [["docker", "exec", container_id("openfit-sam", 0), *EXEC]]


def test_the_link_uses_the_request_host_and_the_users_port(logged_in, admin_env):
    seed_users(admin_env, ("sam", 8101, "running"))
    r = link_for(logged_in, "andy")
    assert r.status_code == 200
    assert r.get_json() == {"ok": True, "link": f"http://192.168.1.10:8080/setup#{SETUP_TOKEN}",
                            "expires_at": SETUP_EXPIRES}
    r = link_for(logged_in, "sam", host="openfit.lan:8090")
    assert r.get_json()["link"] == f"http://openfit.lan:8101/setup#{SETUP_TOKEN}"
    # A host that isn't a plain name or address isn't put in a link.
    r = link_for(logged_in, "sam", host="evil.example%2F@x:8090")
    assert r.get_json()["link"] == f"http://<server IP>:8101/setup#{SETUP_TOKEN}"


# --- refusals -----------------------------------------------------------------


@pytest.mark.parametrize("state", ["exited", None])
def test_not_running_is_a_409(logged_in, admin_env, state):
    seed_users(admin_env, ("sam", 8101, state))
    r = link_for(logged_in, "sam")
    assert r.status_code == 409
    assert r.get_json()["error"] == "sam's OpenFit isn't running. Start it, then make the link."
    assert execs(admin_env) == []


def test_an_unknown_user_is_a_404(logged_in, admin_env):
    r = link_for(logged_in, "nobody")
    assert r.status_code == 404 and execs(admin_env) == []


def test_a_failed_exec_is_a_502_with_its_error(logged_in, admin_env):
    admin_env.fake.rules.append((lambda a: a[1] == "exec", (126, "", "OCI runtime exec failed: no such file")))
    r = link_for(logged_in, "andy")
    assert r.status_code == 502
    assert r.get_json() == {"ok": False, "error": "Couldn't make a setup link for andy.",
                            "detail": "OCI runtime exec failed: no such file"}
    # What it printed is never repeated, even on a failure.
    admin_env.fake.rules[:] = [(lambda a: a[1] == "exec",
                                (1, json.dumps({"token": SETUP_TOKEN}) + "\n", "it went wrong"))]
    r = link_for(logged_in, "andy")
    assert r.status_code == 502 and SETUP_TOKEN not in r.get_data(as_text=True)


@pytest.mark.parametrize("out", [
    "",
    "not json\n",
    "[]\n",
    json.dumps({"token": "short", "expires_at": SETUP_EXPIRES}) + "\n",
    json.dumps({"token": "x" * 51, "expires_at": SETUP_EXPIRES}) + "\n",
    json.dumps({"token": "a b" + "x" * 40, "expires_at": SETUP_EXPIRES}) + "\n",
    json.dumps({"token": "x" * 40 + "#y", "expires_at": SETUP_EXPIRES}) + "\n",
    json.dumps({"token": SETUP_TOKEN + "\n"}) + "\n",
    json.dumps({"token": 5, "expires_at": SETUP_EXPIRES}) + "\n",
    json.dumps({"token": SETUP_TOKEN, "expires_at": "tomorrow"}) + "\n",
    json.dumps({"token": SETUP_TOKEN}) + "\n",
    json.dumps({"token": SETUP_TOKEN, "expires_at": SETUP_EXPIRES}) + "\nsomething else\n",
])
def test_a_malformed_answer_is_refused(logged_in, admin_env, out):
    admin_env.fake.rules.append((lambda a: a[1] == "exec", (0, out, "")))
    r = link_for(logged_in, "andy")
    assert r.status_code == 502
    body = r.get_json()
    assert body["error"] == "andy's OpenFit didn't give back a setup link the Admin recognises."
    assert "link" not in body and body["detail"] == ""


def test_it_waits_for_a_running_operation(admin_env, blocking_build):
    admin_env.repos.push({"app/main.py": "print('new')\n"}, "New")
    admin_env.admin.check()
    admin_env.admin.start_update()
    assert blocking_build.reached.wait(10)
    with pytest.raises(admin_ops.Busy):
        admin_env.admin.setup_link("andy", "192.168.1.10:8090")
    blocking_build.release.set()


# --- nothing kept -------------------------------------------------------------


def _every_file(root):
    for folder, _dirs, files in os.walk(root):
        for name in files:
            with open(os.path.join(folder, name), "rb") as f:
                yield os.path.join(folder, name), f.read()


def test_the_token_is_in_no_history_log_or_file(logged_in, admin_env, capfd):
    seed_users(admin_env, ("sam", 8101, "running"))
    assert link_for(logged_in, "sam").status_code == 200
    admin_env.admin.setup_link("andy", "192.168.1.10:8090")
    printed = capfd.readouterr()
    # The log says what happened, never the token.
    assert "[admin] Made a setup link for sam." in printed.out
    assert "[admin] Made a setup link for andy." in printed.out
    assert SETUP_TOKEN not in printed.out + printed.err
    # Not an operation: nothing in the history, and nothing written anywhere
    # in the Admin's data - history.json, auth.json, the users' files.
    assert all(o.get("kind") != "setup_link" for o in admin_env.admin.history())
    for path, data in _every_file(admin_env.data):
        assert SETUP_TOKEN.encode() not in data, path
    resp = logged_in.get("/api/status", environ_overrides={"HTTP_HOST": "192.168.1.10:8090"})
    assert resp.status_code == 200 and SETUP_TOKEN not in resp.get_data(as_text=True)


# --- the route's guard --------------------------------------------------------


def test_the_route_needs_a_session_and_the_header(admin_client, admin_env):
    r = admin_client.post("/api/users/andy/setup-link", json={}, headers=H)
    assert r.status_code == 401 and r.get_json() == {"ok": False, "error": "Log in first."}
    admin_env.auth.setup(admin_env.auth.setup_code, PASSWORD)
    assert admin_client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
    for headers in ({}, {"X-OpenFit-Admin": "0"}):
        r = admin_client.post("/api/users/andy/setup-link", json={}, headers=headers)
        assert r.status_code == 403
    assert execs(admin_env) == []
    assert admin_client.post("/api/users/andy/setup-link", json={}, headers=H).status_code == 200
    # A POST only: nothing makes a link by being visited.
    assert admin_client.get("/api/users/andy/setup-link").status_code == 405


# --- the page -----------------------------------------------------------------


def test_the_page_shows_the_link_once(admin_client):
    html = admin_client.get("/").get_data(as_text=True)
    # A Setup link button on every user's row, usable while they're running.
    assert 'acts.append(button("Setup link", "", (b) => makeLink(u.name, b), busy || !running));' in html
    assert "api(`/api/users/${encodeURIComponent(name)}/setup-link`, {})" in html
    # Shown in a dialog, with Copy and the brief's words.
    assert '<dialog class="card" id="linkDialog" aria-labelledby="linkTitle">' in html
    assert "Send this to ${name}. It works once, for 24 hours. A new link replaces this one." in html
    assert '<button class="primary" type="button" id="linkCopy">Copy</button>' in html
    # Kept nowhere: no storage at all, and the field emptied as it closes.
    assert "localStorage" not in html and "sessionStorage" not in html
    assert '$("linkDialog").addEventListener("close", () => { $("linkText").value = "";' in html
