"""The Admin's users (A2): adding, starting, stopping and removing them,
deleting a removed user's kept data, and updates and undo moving every
user together.

As in test_admin_update.py, the order of the steps is the safety, so the
tests pin exact command sequences: git runs for real against temp repos,
docker is conftest's FakeRunner, which plays a compose project per user.
"""

import json
import os

import pytest
from conftest import CONTAINERS, IMAGE_ID, VOLUME, Call, container_id, seed_users

import admin_ops

H = {"X-OpenFit-Admin": "1"}
PASSWORD = "a long password!"
C0, C1, C2 = CONTAINERS[:3]

# The compose file an added user gets, byte for byte: only the image and
# the port filled in. No env_file, no environment, no bind mount; the app
# shelf read-only, from the Admin's volume.
TEMPLATE = """services:
  tracker:
    image: openfit-tracker:latest
    pull_policy: never
    ports:
      - "8101:80"
    volumes:
      - tracker-data:/data
      - openfit-library:/library:ro
    restart: unless-stopped
volumes:
  tracker-data: {}
  openfit-library:
    external: true
"""


@pytest.fixture
def logged_in(admin_client, admin_env):
    admin_env.auth.setup(admin_env.auth.setup_code, PASSWORD)
    assert admin_client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
    return admin_client


# --- helpers ---------------------------------------------------------------------


def folder(env, name):
    return str(env.data / "users" / name)


def ucompose(env, name, *args):
    """An added user's compose command: their project, their own file."""
    return Call(["docker", "compose", "-p", f"openfit-{name}", "-f",
                 os.path.join(folder(env, name), "docker-compose.yml"), *args], folder(env, name), False)


def pcompose(env, *args):
    """The primary's: project openfit, the repo's compose file, then the
    Admin's override."""
    return Call(["docker", "compose", "-p", "openfit", "-f", os.path.join(env.repos.repo, "docker-compose.yml"),
                 "-f", str(env.data / "primary.override.yml"), *args], env.repos.repo, False)


def git(env, *args):
    return Call(["git", *args], env.repos.repo, True)


def docker(*args):
    return Call(["docker", *args], None, False)


# Before anything that recreates a user: the app shelf's volume is there.
SHELF = docker("volume", "inspect", "--format", "{{.Name}}", "openfit-library")


def ps(project):
    return docker(
        "ps", "-a", "--no-trunc",
        "--filter", f"label=com.docker.compose.project={project}",
        "--filter", "label=com.docker.compose.service=tracker",
        "--filter", "label=com.docker.compose.oneoff=False",
        "--format", "{{.ID}}\t{{.State}}",
    )


def health(cid):
    return docker("exec", cid, "python", "-c", admin_ops.HEALTH_CODE)


def backup(volume, name):
    return docker(
        "run", "--rm", "--label", "openfit.admin.helper=true",
        "-v", f"{volume}:/v:ro", "-v", "openfit-admin_admin-data:/b", "openfit-admin:local",
        "tar", "czf", f"/b/backups/{name}", "-C", "/v", ".",
    )


def restore(volume, name):
    return docker(
        "run", "--rm", "--label", "openfit.admin.helper=true",
        "-v", f"{volume}:/v", "-v", "openfit-admin_admin-data:/b:ro", "openfit-admin:local",
        "sh", "-c", f"find /v -mindepth 1 -delete && tar xzf /b/backups/{name} -C /v",
    )


def sam(n):
    return container_id("openfit-sam", n)


def kim(n):
    return container_id("openfit-kim", n)


def seed(env, *users):
    seed_users(env, *users)


def three_users(env):
    """The primary running, sam running, kim stopped."""
    seed(env, ("sam", 8101, "running"), ("kim", 8102, "exited"))


def registry(env):
    return json.loads((env.data / "users.json").read_text())


def add(env, name, port=None, host="192.168.1.10:8090"):
    env.admin.start_add_user(name, port or env.admin.next_port(), host)
    env.admin.wait(10)
    return env.admin.current_op()


def pending(env, message="New OpenFit"):
    """An update waiting on GitHub, found by a check; the calls cleared."""
    env.repos.push({"app/main.py": f"print({message!r})\n"}, message)
    env.admin.check()
    env.fake.clear()


def run_update(env):
    env.admin.start_update()
    env.admin.wait(10)
    return env.admin.current_op()


def run_undo(env):
    env.admin.start_undo()
    env.admin.wait(10)
    return env.admin.current_op()


def old_short(env):
    return env.admin.head()["short"]


def texts(op):
    return [line["text"] for line in op["log"]]


# --- 1. adding a user ---------------------------------------------------------------


def test_adding_a_user_runs_in_the_exact_order(logged_in, admin_env):
    env = admin_env
    env.fake.clear()
    listed = []

    def exec_(args):  # users.json, at the moment the health check runs
        path = env.data / "users.json"
        listed.append(path.exists() and "sam" in path.read_text())
        return 0, "", ""

    env.fake.rules.append((lambda a: a[1] == "exec", exec_))

    r = logged_in.post("/api/users", json={"name": "sam", "port": 8101}, headers=H)
    assert r.status_code == 202
    assert r.get_json()["op"]["kind"] == "add_user"
    env.admin.wait(10)
    op = env.admin.current_op()

    assert env.fake.calls == [
        docker("image", "inspect", "--format", "{{.Id}}", "openfit-tracker:latest"),
        SHELF,
        docker("volume", "inspect", "--format", "{{.Name}}", "openfit-sam_tracker-data"),
        ucompose(env, "sam", "up", "-d", "--no-build"),
        ps("openfit-sam"),
        health(sam(0)),
    ]
    written = (env.data / "users" / "sam" / "docker-compose.yml").read_bytes()
    assert written == TEMPLATE.encode()
    for banned in (b"env_file", b"environment", b".env", b"build", b"/home", b"/data:"):
        assert banned not in written.replace(b"tracker-data:/data", b""), banned
    assert b"openfit-library:/library:ro" in written
    assert listed == [False]  # only once it answered
    assert op["outcome"] == "ok"
    assert op["user"] == {"name": "sam", "port": 8101}
    assert op["summary"] == "Added sam. Their OpenFit is at http://localhost:8101"  # the host the page used
    assert registry(env) == {"users": [{"name": "sam", "port": 8101, "created_at": "2026-10-27T12:00:00Z"}],
                             "removed": []}
    s = logged_in.get("/api/status").get_json()
    assert s["users"] == [
        {"name": "andy", "port": 8080, "primary": True, "state": "running", "library": False},
        {"name": "sam", "port": 8101, "primary": False, "state": "running", "library": True},
    ]
    assert s["next_port"] == 8102
    assert s["removed"] == []
    assert s["history"][0]["kind"] == "add_user"


def test_add_refuses_until_openfit_has_been_built(logged_in, admin_env):
    env = admin_env
    env.fake.images.clear()
    r = logged_in.post("/api/users", json={"name": "sam", "port": 8101}, headers=H)
    assert r.status_code == 409
    assert r.get_json()["error"] == "OpenFit hasn't been built on this server yet."
    assert env.fake.commands() == [["docker", "image", "inspect", "--format", "{{.Id}}", "openfit-tracker:latest"]]
    assert not (env.data / "users").exists() and not (env.data / "users.json").exists()
    assert env.admin.history() == [] and not env.admin.lock.locked()


def test_add_never_adopts_storage_it_has_no_record_of(logged_in, admin_env):
    env = admin_env
    env.fake.volumes.add("openfit-sam_tracker-data")  # left from before a lost users.json, say
    r = logged_in.post("/api/users", json={"name": "sam", "port": 8101}, headers=H)
    assert r.status_code == 409
    assert "openfit-sam_tracker-data" in r.get_json()["error"]
    assert not any(c[:2] == ["docker", "compose"] for c in env.fake.commands())
    assert "openfit-sam_tracker-data" in env.fake.volumes
    assert env.admin.history() == []


def test_the_suggested_port_is_the_lowest_free_one(admin_env):
    env = admin_env
    assert env.admin.next_port() == 8101
    seed(env, ("sam", 8101, "running"), ("kim", 8103, "running"))
    assert env.admin.next_port() == 8102
    seed(env, ("sam", 8101, "running"), ("kim", 8102, "running"))
    assert env.admin.next_port() == 8103


@pytest.mark.parametrize("host, shown", [
    ("192.168.1.10:8090", "http://192.168.1.10:8101"),
    ("openfit.local:8090", "http://openfit.local:8101"),
    ("[fe80::1]:8090", "http://[fe80::1]:8101"),
    ("<b>x</b>", "http://<server IP>:8101"),
    (None, "http://<server IP>:8101"),
])
def test_the_address_is_the_host_the_admin_was_reached_at(host, shown):
    assert admin_ops.address(host, 8101) == shown


# --- 2. a failed add is taken back ----------------------------------------------------


def test_an_add_that_does_not_come_up_is_taken_back(admin_env):
    env = admin_env
    seed(env, ("kim", 8101, "running"))
    before = (env.data / "users.json").read_bytes()
    env.fake.unhealthy = {sam(0)}

    op = add(env, "sam", 8102)

    calls = env.fake.calls
    assert calls[:4] == [
        docker("image", "inspect", "--format", "{{.Id}}", "openfit-tracker:latest"),
        SHELF,
        docker("volume", "inspect", "--format", "{{.Name}}", "openfit-sam_tracker-data"),
        ucompose(env, "sam", "up", "-d", "--no-build"),
    ]
    assert calls[-1] == ucompose(env, "sam", "down", "-v")
    assert sum(1 for c in calls if c.args[1] == "exec") == 31  # every 2s for 60s
    # Only sam's project was ever touched.
    assert {c.args[3] for c in calls if c.args[1] == "compose"} == {"openfit-sam"}
    assert op["outcome"] == "failed"
    assert op["summary"] == "sam wasn't added: their OpenFit didn't answer."
    assert not os.path.exists(folder(env, "sam"))
    assert (env.data / "users.json").read_bytes() == before
    assert "openfit-sam_tracker-data" not in env.fake.volumes
    assert "openfit-kim_tracker-data" in env.fake.volumes


@pytest.mark.parametrize("err", [
    "Error response from daemon: driver failed programming external connectivity on endpoint "
    "openfit-sam-tracker-1: Bind for 0.0.0.0:8101 failed: port is already allocated",
    "Error starting userland proxy: listen tcp4 0.0.0.0:8101: bind: address already in use",
])
def test_a_port_in_use_on_the_server_says_so(logged_in, admin_env, err):
    env = admin_env
    env.fake.fail(lambda a: a[:2] == ["docker", "compose"] and "up" in a, err=err)

    assert logged_in.post("/api/users", json={"name": "sam", "port": 8101}, headers=H).status_code == 202
    env.admin.wait(10)
    op = logged_in.get("/api/op").get_json()["op"]

    assert op["outcome"] == "failed"
    assert op["summary"] == "Port 8101 is already in use on the server. Choose another."
    assert env.fake.calls[-2:] == [ucompose(env, "sam", "up", "-d", "--no-build"),
                                   ucompose(env, "sam", "down", "-v")]
    assert not (env.data / "users.json").exists()
    assert not os.path.exists(folder(env, "sam"))


def test_an_add_that_does_not_start_is_taken_back(admin_env):
    env = admin_env
    env.fake.fail(lambda a: a[:2] == ["docker", "compose"] and "up" in a, err="manifest unknown")
    op = add(env, "sam", 8101)
    assert op["summary"] == "sam wasn't added: their OpenFit didn't start."
    assert env.fake.commands()[-1][-2:] == ["down", "-v"]


# --- 3. a removed user added again ----------------------------------------------------


def test_adding_a_removed_user_again_reattaches_their_data(admin_env):
    env = admin_env
    seed(env, ("sam", 8101, "running"))
    env.admin.remove_user("sam")
    env.fake.clear()
    env.fake.unhealthy = {sam(1)}

    op = add(env, "sam", 8105)

    assert texts(op)[0] == "sam was removed before. Their kept data is reattached."
    commands = env.fake.commands()
    assert not any(c[1:3] == ["volume", "inspect"] and c[-1] != "openfit-library" for c in commands)  # theirs is expected
    assert env.fake.calls[-1] == ucompose(env, "sam", "down")  # never -v: the kept data stays
    assert not any("-v" in c for c in commands if c[1] == "compose")
    assert op["outcome"] == "failed"
    assert "openfit-sam_tracker-data" in env.fake.volumes
    assert [r["name"] for r in registry(env)["removed"]] == ["sam"]
    assert registry(env)["users"] == []

    env.fake.unhealthy = set()
    env.clock.advance(60)
    op = add(env, "sam", 8105)

    assert op["outcome"] == "ok"
    assert [(u["name"], u["port"]) for u in registry(env)["users"]] == [("sam", 8105)]
    assert registry(env)["removed"] == []
    assert "openfit-sam_tracker-data" in env.fake.volumes


# --- 4. what a name and a port can be -------------------------------------------------


@pytest.mark.parametrize("name", ["Sam", "sam-1", "admin", "andy", "a" * 21, "../x", "", " sam", None, 7])
def test_a_name_that_cant_be_a_users_is_400(logged_in, admin_env, name):
    r = logged_in.post("/api/users", json={"name": name, "port": 8101}, headers=H)
    assert r.status_code == 400, name
    assert admin_env.fake.calls == []
    assert admin_env.admin.history() == []
    assert not (admin_env.data / "users.json").exists()


def test_a_taken_name_is_409(logged_in, admin_env):
    seed(admin_env, ("sam", 8101, "running"))
    r = logged_in.post("/api/users", json={"name": "sam", "port": 8102}, headers=H)
    assert r.status_code == 409
    assert r.get_json()["error"] == "There's already a user called sam."
    assert admin_env.fake.calls == []


@pytest.mark.parametrize("port, status", [
    (8090, 400), (80, 400), (1023, 400), (70000, 400), ("8102", 400), (8102.0, 400), (True, 400), (None, 400),
    (8080, 409),  # the primary's
    (8101, 409),  # sam's
])
def test_a_port_that_cant_be_a_users_is_refused(logged_in, admin_env, port, status):
    seed(admin_env, ("sam", 8101, "running"))
    r = logged_in.post("/api/users", json={"name": "kim", "port": port}, headers=H)
    assert r.status_code == status, port
    assert admin_env.fake.calls == []
    assert len(registry(admin_env)["users"]) == 1


def test_a_removed_users_port_is_free_again(admin_env):
    env = admin_env
    seed(env, ("sam", 8101, "running"))
    env.admin.remove_user("sam")
    assert env.admin.next_port() == 8101
    assert add(env, "kim", 8101)["outcome"] == "ok"


GOOD = {"name": "sam", "port": 8101, "created_at": "2026-10-27T09:00:00Z"}
GONE = {"name": "kim", "port": 8102, "removed_at": "2026-10-27T10:00:00Z", "volume": "openfit-kim_tracker-data"}


@pytest.mark.parametrize("content, key", [
    ("not json", "isn't a JSON object"),
    ([GOOD], "isn't a JSON object"),
    ({"users": GOOD}, "users isn't a list"),
    ({"users": [GOOD], "extra": []}, "'extra'"),
    ({"users": [dict(GOOD, name="Sam")]}, "users[0].name"),
    ({"users": [dict(GOOD, name="admin")]}, "users[0].name"),
    ({"users": [dict(GOOD, name="andy")]}, "users[0].name"),
    ({"users": [dict(GOOD, name="x; rm -rf /")]}, "users[0].name"),
    ({"users": [GOOD, dict(GOOD, port=8102)]}, "users[1].name"),
    ({"users": [dict(GOOD, port=8090)]}, "users[0].port"),
    ({"users": [dict(GOOD, port=8080)]}, "users[0].port"),
    ({"users": [dict(GOOD, port="8101")]}, "users[0].port"),
    ({"users": [GOOD, dict(GOOD, name="kim")]}, "users[1].port"),
    ({"users": [dict(GOOD, created_at="yesterday")]}, "users[0].created_at"),
    ({"users": [dict(GOOD, extra=1)]}, "users[0]"),
    ({"removed": [dict(GONE, volume="openfit_tracker-data")]}, "removed[0].volume"),
    ({"removed": [dict(GONE, volume="n8n_data")]}, "removed[0].volume"),
    ({"users": [GOOD], "removed": [dict(GONE, name="sam")]}, "removed[0].name"),
])
def test_a_bad_users_json_stops_the_admin_starting(admin_env, content, key):
    path = admin_env.data / "users.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    with pytest.raises(ValueError) as e:
        admin_env.make()
    assert str(path) in str(e.value)
    assert key in str(e.value)
    assert admin_env.fake.calls == []


def test_a_good_users_json_loads(admin_env):
    (admin_env.data / "users.json").write_text(json.dumps({"users": [GOOD], "removed": [GONE]}))
    admin = admin_env.make()
    assert [(u.name, u.project, u.port, u.primary) for u in admin.users()] == [
        ("andy", "openfit", 8080, True), ("sam", "openfit-sam", 8101, False)]


# --- 5. start and stop ------------------------------------------------------------


@pytest.mark.parametrize("name", ["sam", "andy"])
def test_start_and_stop_any_user(logged_in, admin_env, name):
    env = admin_env
    seed(env, ("sam", 8101, "running"))

    def command(*args):
        return ucompose(env, "sam", *args) if name == "sam" else pcompose(env, *args)

    def state():
        return {u["name"]: u["state"] for u in logged_in.get("/api/status").get_json()["users"]}[name]

    assert logged_in.post(f"/api/users/{name}/stop", json={}, headers=H).status_code == 200
    assert env.fake.calls == [command("stop", "tracker")]
    assert state() == "stopped"

    env.fake.clear()
    assert logged_in.post(f"/api/users/{name}/start", json={}, headers=H).status_code == 200
    assert env.fake.calls == [SHELF, command("up", "-d", "--no-build", "tracker")]
    assert state() == "running"
    assert not env.admin.lock.locked()


def test_start_and_stop_an_unknown_user_is_404(logged_in, admin_env):
    seed(admin_env, ("sam", 8101, "running"))
    for path in ("/api/users/nobody/start", "/api/users/nobody/stop", "/api/users/Sam/start",
                 "/api/users/openfit-sam/stop"):
        assert logged_in.post(path, json={}, headers=H).status_code == 404, path
    assert admin_env.fake.calls == []


def test_a_start_that_fails_says_why(logged_in, admin_env):
    env = admin_env
    seed(env, ("sam", 8101, "exited"))
    env.fake.fail(lambda a: "up" in a, err="Bind for 0.0.0.0:8101 failed: port is already allocated")
    r = logged_in.post("/api/users/sam/start", json={}, headers=H)
    assert r.status_code == 502
    assert r.get_json()["error"] == "Port 8101 is already in use on the server. Choose another."
    assert not env.admin.lock.locked()


def test_start_and_stop_wait_their_turn(logged_in, admin_env, blocking_build):
    env = admin_env
    seed(env, ("sam", 8101, "running"))
    pending(env)
    assert logged_in.post("/api/update", json={}, headers=H).status_code == 202
    assert blocking_build.reached.wait(10)
    for path in ("/api/users/sam/start", "/api/users/sam/stop", "/api/users/sam/remove", "/api/users"):
        r = logged_in.post(path, json={"name": "kim", "port": 8102}, headers=H)
        assert r.status_code == 409, path
        assert "Something else is running" in r.get_json()["error"]


# --- 6. remove --------------------------------------------------------------------


def test_remove_takes_the_container_and_keeps_the_data(logged_in, admin_env):
    env = admin_env
    seed(env, ("sam", 8101, "running"), ("kim", 8102, "running"))

    r = logged_in.post("/api/users/sam/remove", json={}, headers=H)

    assert r.status_code == 200
    assert env.fake.calls == [ucompose(env, "sam", "down")]
    assert registry(env) == {
        "users": [{"name": "kim", "port": 8102, "created_at": "2026-10-21T09:00:00Z"}],
        "removed": [{"name": "sam", "port": 8101, "removed_at": "2026-10-27T12:00:00Z",
                     "volume": "openfit-sam_tracker-data"}],
    }
    assert not os.path.exists(folder(env, "sam"))
    assert os.path.exists(folder(env, "kim"))
    assert "openfit-sam_tracker-data" in env.fake.volumes
    s = logged_in.get("/api/status").get_json()
    assert [u["name"] for u in s["users"]] == ["andy", "kim"]
    assert s["removed"] == [{"name": "sam", "removed_at": "2026-10-27T12:00:00Z"}]


def test_remove_refuses_the_primary_and_strangers(logged_in, admin_env):
    seed(admin_env, ("sam", 8101, "running"))
    r = logged_in.post("/api/users/andy/remove", json={}, headers=H)
    assert r.status_code == 400
    assert r.get_json()["error"] == "The primary user can't be removed."
    assert logged_in.post("/api/users/nobody/remove", json={}, headers=H).status_code == 404
    assert admin_env.fake.calls == []
    assert [u["name"] for u in registry(admin_env)["users"]] == ["sam"]


def test_a_remove_that_fails_changes_nothing(logged_in, admin_env):
    env = admin_env
    seed(env, ("sam", 8101, "running"))
    env.fake.fail(lambda a: "down" in a)
    r = logged_in.post("/api/users/sam/remove", json={}, headers=H)
    assert r.status_code == 502
    assert [u["name"] for u in registry(env)["users"]] == ["sam"]
    assert registry(env)["removed"] == []
    assert os.path.exists(folder(env, "sam"))


# --- 7. delete a removed user's data ----------------------------------------------------


@pytest.fixture
def sam_removed(admin_env):
    seed(admin_env, ("sam", 8101, "running"))
    admin_env.admin.remove_user("sam")
    admin_env.fake.clear()
    return admin_env


DELETE_PS = docker("ps", "-a", "--no-trunc", "--filter", "label=com.docker.compose.project=openfit-sam",
                   "--format", "{{.ID}}")


@pytest.mark.parametrize("body", [{}, {"confirm": None}, {"confirm": ""}, {"confirm": "Sam"},
                                  {"confirm": "sam "}, {"confirm": "kim"}, {"confirm": ["sam"]}])
def test_delete_data_needs_their_name_typed(logged_in, sam_removed, body):
    env = sam_removed
    r = logged_in.post("/api/removed/sam/delete-data", json=body, headers=H)
    assert r.status_code == 400
    assert env.fake.calls == []
    assert "openfit-sam_tracker-data" in env.fake.volumes
    assert [x["name"] for x in registry(env)["removed"]] == ["sam"]


def test_delete_data_only_for_a_removed_user(logged_in, sam_removed):
    env = sam_removed
    seed_more = registry(env)
    seed_more["users"].append({"name": "kim", "port": 8102, "created_at": "2026-10-27T09:00:00Z"})
    (env.data / "users.json").write_text(json.dumps(seed_more))
    for name in ("kim", "nobody", "andy"):
        r = logged_in.post(f"/api/removed/{name}/delete-data", json={"confirm": name}, headers=H)
        assert r.status_code == 404, name
    assert env.fake.calls == []


def test_delete_data_refuses_while_a_container_is_left(logged_in, sam_removed):
    env = sam_removed
    env.fake.projects["openfit-sam"]["state"] = "exited"  # brought back from the command line, say
    r = logged_in.post("/api/removed/sam/delete-data", json={"confirm": "sam"}, headers=H)
    assert r.status_code == 409
    assert env.fake.calls == [DELETE_PS]
    assert "openfit-sam_tracker-data" in env.fake.volumes
    assert [x["name"] for x in registry(env)["removed"]] == ["sam"]


def test_delete_data_removes_exactly_their_volume(logged_in, sam_removed):
    env = sam_removed
    r = logged_in.post("/api/removed/sam/delete-data", json={"confirm": "sam"}, headers=H)
    assert r.status_code == 200
    assert env.fake.calls == [DELETE_PS, docker("volume", "rm", "openfit-sam_tracker-data")]
    assert "openfit-sam_tracker-data" not in env.fake.volumes
    assert VOLUME in env.fake.volumes
    assert registry(env)["removed"] == []
    assert logged_in.get("/api/status").get_json()["removed"] == []


# --- 8. an update moves everyone together ----------------------------------------------


def test_an_update_moves_everyone_together(admin_env):
    env = admin_env
    three_users(env)
    old = old_short(env)
    pending(env)
    stamp = "20261027T120000Z"
    names = {u: f"{stamp}-{old}-{u}.tar.gz" for u in ("andy", "sam", "kim")}

    op = run_update(env)

    calls = env.fake.calls
    build = calls.index(pcompose(env, "build", "tracker"))
    assert sum(1 for c in calls if "build" in c.args) == 1
    assert calls[build + 1:] == [
        # who's running, primary first, then the added users in order
        ps("openfit"), ps("openfit-sam"), ps("openfit-kim"),
        # the running ones stopped: andy and sam, never kim
        pcompose(env, "stop", "tracker"),
        ucompose(env, "sam", "stop", "tracker"),
        # everyone backed up, one stamp
        docker("inspect", "--type", "container", C0), backup(VOLUME, names["andy"]),
        docker("inspect", "--type", "container", sam(0)), backup("openfit-sam_tracker-data", names["sam"]),
        docker("inspect", "--type", "container", kim(0)), backup("openfit-kim_tracker-data", names["kim"]),
        # recreated on the new image: the running ones started, kim left stopped
        pcompose(env, "up", "-d", "--no-build", "--force-recreate", "tracker"),
        ucompose(env, "sam", "up", "-d", "--no-build", "--force-recreate", "tracker"),
        ucompose(env, "kim", "up", "--no-start", "--no-build", "--force-recreate", "tracker"),
        # the running ones health-checked
        ps("openfit"), health(C1),
        ps("openfit-sam"), health(sam(1)),
        # the check brought up to date
        git(env, "log", "-1", "--format=%H%x09%h%x09%s", "HEAD"),
        git(env, "rev-list", "--count", "HEAD..origin/main"),
        git(env, "diff", "--name-only", "HEAD", "origin/main"),
    ]
    assert op["outcome"] == "ok"
    assert op["users"] == [
        {"name": "andy", "created_at": None, "was_running": True, "backup": names["andy"]},
        {"name": "sam", "created_at": "2026-10-20T09:00:00Z", "was_running": True, "backup": names["sam"]},
        {"name": "kim", "created_at": "2026-10-21T09:00:00Z", "was_running": False, "backup": names["kim"]},
    ]
    assert sorted(b["name"] for b in env.admin.list_backups()) == sorted(names.values())
    assert {b["user"] for b in env.admin.list_backups()} == {"andy", "sam", "kim"}
    assert env.fake.projects["openfit-kim"]["state"] == "created"
    assert env.admin.undo_candidate(env.admin.head())["id"] == op["id"]


def test_a_user_with_no_container_is_skipped(admin_env):
    env = admin_env
    three_users(env)
    env.fake.projects["openfit-kim"]["state"] = None  # its container removed by hand
    pending(env)

    op = run_update(env)

    assert op["outcome"] == "ok"
    assert "kim has no OpenFit container, so they're skipped." in texts(op)
    assert not any(c.args[3:4] == ["openfit-kim"] for c in env.fake.calls if c.args[1] == "compose")
    assert op["users"][2] == {"name": "kim", "created_at": "2026-10-21T09:00:00Z", "was_running": False,
                              "backup": None}
    # With someone's data not backed up, there's nothing to undo.
    assert env.admin.undo_candidate(env.admin.head()) is None


def test_an_update_with_a_bad_users_json_changes_nothing(admin_env):
    env = admin_env
    three_users(env)
    head = env.repos.head()
    pending(env)
    (env.data / "users.json").write_text(json.dumps({"users": [{"name": "Sam"}]}))
    with pytest.raises(admin_ops.Refused) as e:
        env.admin.start_update()
    assert e.value.status == 500
    assert "users.json" in e.value.message and e.value.message.endswith("Nothing was changed.")
    assert not any(c.args[0] == "docker" for c in env.fake.calls)
    assert env.repos.head() == head
    assert env.admin.history() == [] and not env.admin.lock.locked()


# --- 9. one unhealthy user rolls everyone back ---------------------------------------


def test_one_user_not_answering_rolls_everyone_back(admin_env):
    env = admin_env
    three_users(env)
    old = env.repos.head()
    short = old_short(env)
    pending(env)
    names = {u: f"20261027T120000Z-{short}-{u}.tar.gz" for u in ("andy", "sam", "kim")}
    env.fake.unhealthy = {sam(1)}  # sam's new container never answers

    op = run_update(env)

    calls = env.fake.calls
    execs = [i for i, c in enumerate(calls) if c.args[1] == "exec" and c.args[2] == sam(1)]
    assert len(execs) == 31
    rest = [c for c in calls[execs[-1] + 1:] if c.args[:2] not in (["git", "log"], ["git", "rev-list"], ["git", "diff"])]
    assert rest == [
        # everyone stopped first
        pcompose(env, "stop", "tracker"),
        ucompose(env, "sam", "stop", "tracker"),
        ucompose(env, "kim", "stop", "tracker"),
        # each put back from their own backup
        ps("openfit"), docker("inspect", "--type", "container", C1), restore(VOLUME, names["andy"]),
        ps("openfit-sam"), docker("inspect", "--type", "container", sam(1)),
        restore("openfit-sam_tracker-data", names["sam"]),
        ps("openfit-kim"), docker("inspect", "--type", "container", kim(1)),
        restore("openfit-kim_tracker-data", names["kim"]),
        # the old image and code
        docker("tag", "openfit-tracker:prev", "openfit-tracker:latest"),
        git(env, "reset", "--hard", old),
        # each as they were
        pcompose(env, "up", "-d", "--no-build", "--force-recreate", "tracker"),
        ucompose(env, "sam", "up", "-d", "--no-build", "--force-recreate", "tracker"),
        ucompose(env, "kim", "up", "--no-start", "--no-build", "--force-recreate", "tracker"),
        ps("openfit"), health(C2),
        ps("openfit-sam"), health(sam(2)),
    ]  # and the check brought up to date, which only reads git
    assert op["outcome"] == "rolled_back"
    assert op["summary"] == "Updated, didn't come up, rolled back."
    assert "sam's OpenFit didn't answer." in texts(op)
    assert env.repos.head() == old
    assert admin_ops.update_offered(env.admin.last_check())
    assert env.admin.undo_candidate(env.admin.head()) is None


def test_a_user_that_does_not_come_up_on_the_new_image_rolls_everyone_back(admin_env):
    env = admin_env
    three_users(env)
    old = env.repos.head()
    pending(env)
    tries = []

    def kim_up(args):  # fails on the new image, not on the old
        tries.append(args)
        return (1, "", "it went wrong") if len(tries) == 1 else (0, "", "")

    env.fake.rules.append((lambda a: a[:4] == ["docker", "compose", "-p", "openfit-kim"] and "up" in a, kim_up))

    op = run_update(env)

    assert op["outcome"] == "rolled_back"
    assert any(t.startswith("kim's OpenFit didn't come up on the new version.") for t in texts(op))
    assert len([c for c in env.fake.calls if c.args[1] == "run" and "tar xzf" in c.args[-1]]) == 3
    assert len(tries) == 2
    assert env.repos.head() == old


# --- 10. a failed backup backs out -----------------------------------------------------


def test_a_failed_backup_for_one_user_backs_everyone_out(admin_env):
    env = admin_env
    three_users(env)
    old = env.repos.head()
    pending(env)
    env.fake.fail(lambda a: "czf" in a and "openfit-sam_tracker-data:/v:ro" in a, err="no space left on device")

    op = run_update(env)

    calls = env.fake.calls
    failed = next(i for i, c in enumerate(calls) if "czf" in c.args and "openfit-sam_tracker-data:/v:ro" in c.args)
    assert calls[failed + 1:] == [
        git(env, "reset", "--hard", old),
        docker("tag", "openfit-tracker:prev", "openfit-tracker:latest"),
        pcompose(env, "up", "-d", "--no-build", "--force-recreate", "tracker"),
        ucompose(env, "sam", "up", "-d", "--no-build", "--force-recreate", "tracker"),
    ]
    assert op["outcome"] == "failed"
    assert "couldn't be backed up" in op["summary"]
    assert "no space left on device" in "\n".join(texts(op))
    assert env.repos.head() == old
    assert env.admin.list_backups() == []  # andy's, taken before sam's failed, went with it
    assert env.fake.projects["openfit-kim"]["state"] == "exited"  # never started


def test_a_user_that_does_not_stop_backs_everyone_out(admin_env):
    env = admin_env
    three_users(env)
    old = env.repos.head()
    pending(env)
    env.fake.fail(lambda a: a[:4] == ["docker", "compose", "-p", "openfit-sam"] and "stop" in a)

    op = run_update(env)

    calls = env.fake.calls
    stop = calls.index(ucompose(env, "sam", "stop", "tracker"))
    assert calls[stop + 1:] == [
        git(env, "reset", "--hard", old),
        docker("tag", "openfit-tracker:prev", "openfit-tracker:latest"),
        pcompose(env, "up", "-d", "--no-build", "--force-recreate", "tracker"),
        ucompose(env, "sam", "up", "-d", "--no-build", "--force-recreate", "tracker"),
    ]
    assert op["outcome"] == "failed"
    assert not any(c.args[1] == "run" for c in calls)


# --- 11. undo moves everyone together ----------------------------------------------------


def test_undo_restores_every_recorded_user(logged_in, admin_env):
    env = admin_env
    three_users(env)
    old = env.repos.head()
    pending(env)
    update = run_update(env)
    assert update["outcome"] == "ok"
    names = {r["name"]: r["backup"] for r in update["users"]}
    env.fake.clear()

    r = logged_in.post("/api/undo", json={}, headers=H)
    assert r.status_code == 202
    env.admin.wait(10)
    undo = env.admin.current_op()

    assert env.fake.calls[:21] == [
        git(env, "log", "-1", "--format=%H%x09%h%x09%s", "HEAD"),
        SHELF,
        pcompose(env, "stop", "tracker"),
        ucompose(env, "sam", "stop", "tracker"),
        ucompose(env, "kim", "stop", "tracker"),
        ps("openfit"), docker("inspect", "--type", "container", C1), restore(VOLUME, names["andy"]),
        ps("openfit-sam"), docker("inspect", "--type", "container", sam(1)),
        restore("openfit-sam_tracker-data", names["sam"]),
        ps("openfit-kim"), docker("inspect", "--type", "container", kim(1)),
        restore("openfit-kim_tracker-data", names["kim"]),
        docker("tag", "openfit-tracker:prev", "openfit-tracker:latest"),
        git(env, "reset", "--hard", old),
        pcompose(env, "up", "-d", "--no-build", "--force-recreate", "tracker"),
        ucompose(env, "sam", "up", "-d", "--no-build", "--force-recreate", "tracker"),
        ucompose(env, "kim", "up", "--no-start", "--no-build", "--force-recreate", "tracker"),
        ps("openfit"), health(C2),
    ]
    assert env.fake.calls[21:23] == [ps("openfit-sam"), health(sam(2))]
    assert undo["outcome"] == "ok"
    assert undo["summary"] == f"Back on {update['old']['short']}, with everyone's data as it was before the update."
    assert undo["users"] == update["users"]
    assert env.repos.head() == old
    assert logged_in.get("/api/status").get_json()["undo"] is None


@pytest.mark.parametrize("change", ["add", "remove", "remove and add again"])
def test_undo_is_refused_once_the_users_have_changed(logged_in, admin_env, change):
    env = admin_env
    three_users(env)
    pending(env)
    assert run_update(env)["outcome"] == "ok"
    assert logged_in.get("/api/status").get_json()["undo"] is not None

    env.clock.advance(60)
    if change == "add":
        assert add(env, "lee", 8103)["outcome"] == "ok"
    else:
        env.admin.remove_user("kim")
        if change == "remove and add again":
            assert add(env, "kim", 8102)["outcome"] == "ok"  # someone new, with kim's kept data
    env.fake.clear()

    assert logged_in.get("/api/status").get_json()["undo"] is None
    r = logged_in.post("/api/undo", json={}, headers=H)
    assert r.status_code == 409
    assert r.get_json()["error"] == "There's no update to undo."
    # The status's look, only: its users' containers, and the server (docker info, a minute on).
    assert {c.args[1] for c in env.fake.calls if c.args[0] == "docker"} == {"ps", "inspect", "info"}


def test_undo_needs_every_backup_of_its_set(admin_env):
    env = admin_env
    three_users(env)
    pending(env)
    update = run_update(env)
    assert env.admin.undo_candidate(env.admin.head()) is not None
    os.remove(env.backups / update["users"][2]["backup"])  # kim's
    assert env.admin.undo_candidate(env.admin.head()) is None


def test_an_a1_update_can_still_be_undone(admin_env):
    """An update from before A2 recorded the primary's one backup, not users."""
    env = admin_env
    pending(env)
    assert run_update(env)["outcome"] == "ok"
    history = env.admin.history()
    a1 = history[-1]
    a1["backup"] = a1["users"][0]["backup"]
    del a1["users"]
    (env.data / "history.json").write_text(json.dumps(history))

    assert env.admin.undo_candidate(env.admin.head())["id"] == a1["id"]
    undo = run_undo(env)
    assert undo["outcome"] == "ok"
    assert restore(VOLUME, a1["backup"]) in env.fake.calls


# --- 12. a restore that fails mid-undo --------------------------------------------------


def test_a_restore_that_fails_mid_undo_stops_there(admin_env):
    env = admin_env
    three_users(env)
    pending(env)
    update = run_update(env)
    new = env.repos.head()
    names = {r["name"]: r["backup"] for r in update["users"]}
    env.fake.clear()
    env.fake.fail(lambda a: "openfit-sam_tracker-data:/v" in a and "tar xzf" in a[-1], err="tar: short read")

    undo = run_undo(env)

    calls = env.fake.calls
    failed = calls.index(restore("openfit-sam_tracker-data", names["sam"]))
    assert restore(VOLUME, names["andy"]) in calls[:failed]
    # No kim, no retag, no reset, nobody started: only HEAD read, to see the
    # shelf still matches the code.
    assert [c.args for c in calls[failed + 1:]] == [["git", "log", "-1", "--format=%H%x09%h%x09%s", "HEAD"]]
    assert undo["outcome"] == "failed"
    log = "\n".join(texts(undo))
    assert "sam's data couldn't be put back." in log
    assert ("Where everyone is: andy: data put back, stopped; "
            f"sam: data not put back (the backup is {names['sam']}), stopped; "
            "kim: data not put back yet, stopped.") in log
    assert "the code and image are still the new version" in log
    assert env.repos.head() == new
    assert {p: env.fake.projects[p]["state"] for p in ("openfit", "openfit-sam", "openfit-kim")} == {
        "openfit": "exited", "openfit-sam": "exited", "openfit-kim": "exited"}


# --- 13. backups are pruned by set ----------------------------------------------------


def test_backups_are_pruned_by_set(admin_env):
    env = admin_env
    seed(env, ("sam", 8101, "running"))
    env.backups.mkdir(parents=True, exist_ok=True)
    a1 = "20261001T000000Z-abcdef0.tar.gz"  # from A1: no user in its name
    (env.backups / a1).write_bytes(b"old")
    assert [(b["name"], b["user"]) for b in env.admin.list_backups()] == [(a1, "andy")]

    sets = []
    for i in range(4):
        env.clock.advance(3600)
        pending(env, f"Release {i}")
        op = run_update(env)
        assert op["outcome"] == "ok"
        sets.append([r["backup"] for r in op["users"]])

    names = sorted(b["name"] for b in env.admin.list_backups())
    assert names == sorted(sets[1] + sets[2] + sets[3])
    assert len(names) == 6
    assert sorted(os.listdir(env.backups)) == names
    assert {b["created"] for b in env.admin.list_backups()} == {
        "2026-10-27T14:00:00Z", "2026-10-27T15:00:00Z", "2026-10-27T16:00:00Z"}


# --- the page's view ----------------------------------------------------------------


def test_the_status_lists_every_user(logged_in, admin_env):
    env = admin_env
    three_users(env)
    env.fake.add_project("openfit-lee", None)
    s = logged_in.get("/api/status").get_json()
    assert s["users"] == [
        {"name": "andy", "port": 8080, "primary": True, "state": "running", "library": False},
        {"name": "sam", "port": 8101, "primary": False, "state": "running", "library": False},
        {"name": "kim", "port": 8102, "primary": False, "state": "stopped", "library": False},
    ]
    assert s["next_port"] == 8103
    env.fake.projects["openfit-sam"]["state"] = None
    assert logged_in.get("/api/status").get_json()["users"][1]["state"] == "missing"
    assert s["openfit"]["user"] == "andy"  # A1's view stays


def test_the_page_has_the_users_card(logged_in):
    html = logged_in.get("/").get_data(as_text=True)
    for text in ("Users", "Add user", "Kept data",
                 "Their OpenFit is removed. Their data is kept until you delete it.",
                 "This stops every user's OpenFit for about a minute and backs up everyone's data first.",
                 "for every user"):
        assert text in html, text
    assert 'id="openfitCard"' not in html
    assert IMAGE_ID not in html
