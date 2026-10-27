"""Updating the user system, and undoing an update.

The order of the steps is the safety, so the tests pin the exact command
sequence: git runs for real against temp repos, docker is conftest's
FakeRunner.
"""

import os

import pytest
from conftest import CONTAINERS, IMAGE_ID, VOLUME, Call

import admin_ops

H = {"X-OpenFit-Admin": "1"}
PASSWORD = "a long password!"
C0, C1, C2 = CONTAINERS[:3]
HEAD_LOG = ["git", "log", "-1", "--format=%H%x09%h%x09%s", "HEAD"]
PS = [
    "docker", "ps", "-a", "--no-trunc",
    "--filter", "label=com.docker.compose.project=openfit",
    "--filter", "label=com.docker.compose.service=tracker",
    "--filter", "label=com.docker.compose.oneoff=False",
    "--format", "{{.ID}}\t{{.State}}",
]


def compose(env, *args):
    """The primary's compose command: the repo's file, then the Admin's override."""
    return ["docker", "compose", "-p", "openfit", "-f", os.path.join(env.repos.repo, "docker-compose.yml"),
            "-f", str(env.data / "primary.override.yml"), *args]


def git(env, *args):
    return Call(["git", *args], env.repos.repo, True)


def docker(*args):
    return Call(["docker", *args], None, False)


# Before anything that recreates a user: the app shelf's volume is there.
SHELF = docker("volume", "inspect", "--format", "{{.Name}}", "openfit-library")


def backup_cmd(name):
    return docker(
        "run", "--rm", "--label", "openfit.admin.helper=true",
        "-v", f"{VOLUME}:/v:ro", "-v", "openfit-admin_admin-data:/b", "openfit-admin:local",
        "tar", "czf", f"/b/backups/{name}", "-C", "/v", ".",
    )


def restore_cmd(name):
    return docker(
        "run", "--rm", "--label", "openfit.admin.helper=true",
        "-v", f"{VOLUME}:/v", "-v", "openfit-admin_admin-data:/b:ro", "openfit-admin:local",
        "sh", "-c", f"find /v -mindepth 1 -delete && tar xzf /b/backups/{name} -C /v",
    )


def pending(env, files=None, message="New OpenFit"):
    """An update waiting on GitHub, found by a check; the calls cleared."""
    env.repos.push(files or {"app/main.py": f"print({message!r})\n"}, message)
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


def short(env, ref="HEAD"):
    import subprocess

    return subprocess.run(["git", "log", "-1", "--format=%h", ref], cwd=env.repos.repo,
                          capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def logged_in(admin_client, admin_env):
    admin_env.auth.setup(admin_env.auth.setup_code, PASSWORD)
    assert admin_client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
    return admin_client


# --- the update ----------------------------------------------------------------


def test_an_update_runs_in_the_exact_order(admin_env):
    env = admin_env
    old = env.repos.head()
    old_short = short(env)
    pending(env)
    name = f"20261027T120000Z-{old_short}-andy.tar.gz"

    op = run_update(env)

    assert env.fake.calls == [
        # 1. clean?  (and on the branch it updates, with the shelf's volume there)
        git(env, "status", "--porcelain"),
        git(env, "rev-parse", "--abbrev-ref", "HEAD"),
        SHELF,
        # 2. remember the version, keep the image as :prev
        git(env, *HEAD_LOG[1:]),
        docker(*PS[1:]),
        docker("inspect", "--type", "container", C0),
        docker("tag", IMAGE_ID, "openfit-tracker:prev"),
        # 3. pull
        git(env, "merge", "--ff-only", "origin/main"),
        git(env, *HEAD_LOG[1:]),
        # 4. build, still running the old version
        Call(compose(env, "build", "tracker"), env.repos.repo, False),
        # 5. who's running, then stop them
        docker(*PS[1:]),
        Call(compose(env, "stop", "tracker"), env.repos.repo, False),
        # 6. back up, the volume read from the stopped container
        docker("inspect", "--type", "container", C0),
        backup_cmd(name),
        # 7. recreated on the new image, and started: it was running
        Call(compose(env, "up", "-d", "--no-build", "--force-recreate", "tracker"), env.repos.repo, False),
        # 8. health, on the container compose made
        docker(*PS[1:]),
        docker("exec", C1, "python", "-c", admin_ops.HEALTH_CODE),
        # the check brought up to date, no fetch
        git(env, *HEAD_LOG[1:]),
        git(env, "rev-list", "--count", "HEAD..origin/main"),
        git(env, "diff", "--name-only", "HEAD", "origin/main"),
    ]
    assert op["outcome"] == "ok"
    assert op["summary"] == f"Updated OpenFit to {short(env)}: New OpenFit"
    assert op["old"]["commit"] == old
    assert op["new"]["commit"] == env.repos.head() != old
    assert op["users"] == [{"name": "andy", "created_at": None, "was_running": True, "backup": name}]
    assert (env.backups / name).exists()
    texts = [line["text"] for line in op["log"]]
    assert "Backing up andy's data…" in texts
    assert all(set(line) == {"t", "text"} for line in op["log"])
    assert env.admin.last_check()["behind"] == 0
    assert env.admin.history()[-1]["id"] == op["id"]


def test_a_dirty_repo_is_refused_and_nothing_runs(logged_in, admin_env):
    env = admin_env
    pending(env)
    head = env.repos.head()
    with open(os.path.join(env.repos.repo, "app", "main.py"), "a") as f:
        f.write("# a local edit\n")
    with open(os.path.join(env.repos.repo, "app", "scratch.py"), "w") as f:
        f.write("x = 1\n")

    r = logged_in.post("/api/update", json={}, headers=H)

    assert r.status_code == 409
    error = r.get_json()["error"]
    assert error == "The server's copy of OpenFit has local changes: app/main.py, app/scratch.py. Nothing was changed."
    assert env.fake.calls == [git(env, "status", "--porcelain")]
    assert env.repos.head() == head
    assert env.admin.history() == []
    assert not env.admin.lock.locked()


def test_a_failed_build_resets_the_code_and_never_stops(admin_env):
    env = admin_env
    old = env.repos.head()
    pending(env)
    env.fake.fail(lambda a: a[:2] == ["docker", "compose"] and "build" in a, err="pip: no such package")

    op = run_update(env)

    commands = env.fake.commands()
    build = commands.index(compose(env, "build", "tracker"))
    assert commands[build + 1] == ["git", "reset", "--hard", old]
    assert commands[build + 2:] == []
    assert not any(c[:2] == ["docker", "compose"] and ("stop" in c or "up" in c) for c in commands)
    assert not any(c[:2] == ["docker", "run"] for c in commands)
    assert op["outcome"] == "failed"
    assert op["summary"] == "The build failed, so nothing was changed. OpenFit kept running."
    assert any("pip: no such package" in line["text"] for line in op["log"])
    assert env.repos.head() == old
    assert env.repos.status() == ""
    assert env.admin.undo_candidate() is None


def test_a_failed_health_check_rolls_back(admin_env):
    env = admin_env
    old = env.repos.head()
    pending(env)
    name = f"20261027T120000Z-{short(env)}-andy.tar.gz"
    env.fake.unhealthy = {C1}  # the new version never answers

    op = run_update(env)

    calls = env.fake.calls
    execs = [i for i, c in enumerate(calls) if c.args[1] == "exec" and c.args[2] == C1]
    assert len(execs) == 31  # every 2s for 60s
    start = execs[-1] + 1
    assert calls[start:start + 9] == [
        Call(compose(env, "stop", "tracker"), env.repos.repo, False),
        docker(*PS[1:]),
        docker("inspect", "--type", "container", C1),
        restore_cmd(name),
        docker("tag", "openfit-tracker:prev", "openfit-tracker:latest"),
        git(env, "reset", "--hard", old),
        Call(compose(env, "up", "-d", "--no-build", "--force-recreate", "tracker"), env.repos.repo, False),
        docker(*PS[1:]),
        docker("exec", C2, "python", "-c", admin_ops.HEALTH_CODE),
    ]
    assert op["outcome"] == "rolled_back"
    assert op["summary"] == "Updated, didn't come up, rolled back."
    assert env.repos.head() == old
    assert (env.backups / name).exists()
    # The update is on offer again, and there's nothing to undo.
    assert admin_ops.update_offered(env.admin.last_check())
    assert env.admin.undo_candidate(env.admin.head()) is None


def test_a_rollback_that_does_not_come_up_says_so(admin_env):
    env = admin_env
    pending(env)
    env.fake.unhealthy = {C1, C2}

    op = run_update(env)

    assert op["outcome"] == "failed"
    assert "the rollback didn't come up either" in op["summary"]


def test_a_failed_backup_calls_the_update_off(admin_env):
    env = admin_env
    old = env.repos.head()
    pending(env)
    env.fake.fail(lambda a: a[:2] == ["docker", "run"] and "czf" in a, err="no space left on device")

    op = run_update(env)

    calls = env.fake.calls
    backup = next(i for i, c in enumerate(calls) if "czf" in c.args)
    assert calls[backup + 1:] == [
        git(env, "reset", "--hard", old),
        docker("tag", "openfit-tracker:prev", "openfit-tracker:latest"),
        Call(compose(env, "up", "-d", "--no-build", "--force-recreate", "tracker"), env.repos.repo, False),
    ]
    assert op["outcome"] == "failed"
    assert "couldn't be backed up" in op["summary"]
    assert env.repos.head() == old
    assert env.admin.list_backups() == []


def test_an_update_that_isnt_a_fast_forward_changes_nothing(admin_env):
    env = admin_env
    pending(env)
    env.repos.write({"app/local.py": "x = 1\n"}, where=env.repos.repo)
    import subprocess

    subprocess.run(["git", "add", "-A"], cwd=env.repos.repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "A local commit"], cwd=env.repos.repo, check=True)
    head = env.repos.head()
    env.fake.clear()

    op = run_update(env)

    assert op["outcome"] == "failed"
    assert op["summary"] == "Nothing was changed."
    commands = env.fake.commands()
    assert commands[-1] == ["git", "merge", "--ff-only", "origin/main"]
    assert env.repos.head() == head


def test_no_update_without_one_to_apply(admin_env):
    with pytest.raises(admin_ops.Refused) as e:
        admin_env.admin.start_update()
    assert e.value.status == 409
    assert "Check for updates first" in e.value.message
    assert admin_env.fake.calls == []
    admin_env.admin.check()
    with pytest.raises(admin_ops.Refused):
        admin_env.admin.start_update()


def test_an_update_off_the_branch_is_refused(admin_env):
    env = admin_env
    pending(env)
    import subprocess

    subprocess.run(["git", "checkout", "-q", "-b", "experiment"], cwd=env.repos.repo, check=True)
    with pytest.raises(admin_ops.Refused) as e:
        env.admin.start_update()
    assert e.value.status == 409
    assert e.value.message == "The server's copy of OpenFit is on 'experiment', not 'main'. Nothing was changed."
    assert env.fake.commands()[-1] == ["git", "rev-parse", "--abbrev-ref", "HEAD"]


# --- undo ----------------------------------------------------------------------


def test_undo_is_offered_only_after_a_successful_update(logged_in, admin_env):
    env = admin_env
    old = env.repos.head()
    assert logged_in.get("/api/status").get_json()["undo"] is None
    assert logged_in.post("/api/undo", json={}, headers=H).status_code == 409

    # A failed update: nothing to undo.
    pending(env)
    env.fake.rules.append((lambda a: a[:2] == ["docker", "compose"] and "build" in a, (1, "", "boom")))
    run_update(env)
    assert logged_in.get("/api/status").get_json()["undo"] is None
    env.fake.rules.clear()

    # A successful one: offered, for that update.
    op = run_update(env)
    assert op["outcome"] == "ok"
    offered = logged_in.get("/api/status").get_json()["undo"]
    assert offered["id"] == op["id"]
    assert offered["old"]["commit"] == old
    env.fake.clear()
    container = env.fake.cid

    r = logged_in.post("/api/undo", json={}, headers=H)
    assert r.status_code == 202
    env.admin.wait(10)
    undo = env.admin.current_op()

    assert env.fake.calls[:11] == [
        git(env, *HEAD_LOG[1:]),
        SHELF,
        Call(compose(env, "stop", "tracker"), env.repos.repo, False),
        docker(*PS[1:]),
        docker("inspect", "--type", "container", container),
        restore_cmd(op["users"][0]["backup"]),
        docker("tag", "openfit-tracker:prev", "openfit-tracker:latest"),
        git(env, "reset", "--hard", old),
        Call(compose(env, "up", "-d", "--no-build", "--force-recreate", "tracker"), env.repos.repo, False),
        docker(*PS[1:]),
        docker("exec", CONTAINERS[env.fake.container], "python", "-c", admin_ops.HEALTH_CODE),
    ]
    assert undo["kind"] == "undo"
    assert undo["outcome"] == "ok"
    assert undo["undoes"] == op["id"]
    assert env.repos.head() == old
    # Once only, and the update is on offer again.
    status = logged_in.get("/api/status").get_json()
    assert status["undo"] is None
    assert status["update_available"] is True
    assert logged_in.post("/api/undo", json={}, headers=H).status_code == 409


def test_undo_is_not_offered_once_the_repo_has_moved_on(admin_env):
    env = admin_env
    pending(env)
    assert run_update(env)["outcome"] == "ok"
    assert env.admin.undo_candidate(env.admin.head()) is not None
    pending(env, message="Another")
    assert run_update(env)["outcome"] == "ok"
    # The latest update is the one on offer; reset by hand and it isn't.
    import subprocess

    subprocess.run(["git", "reset", "-q", "--hard", "HEAD~1"], cwd=env.repos.repo, check=True)
    assert env.admin.undo_candidate(env.admin.head()) is None


# --- backups ---------------------------------------------------------------------


def test_backups_are_pruned_to_the_newest_three(admin_env):
    env = admin_env
    env.backups.mkdir(parents=True, exist_ok=True)
    olds = [f"202610{d:02d}T000000Z-abcdef{d}.tar.gz" for d in (20, 21, 22, 23)]
    for name in olds:
        (env.backups / name).write_bytes(b"old")
    (env.backups / "notes.txt").write_text("not a backup")
    pending(env)

    op = run_update(env)

    assert op["outcome"] == "ok"
    names = [b["name"] for b in env.admin.list_backups()]
    assert names == [op["users"][0]["backup"], olds[3], olds[2]]
    assert sorted(os.listdir(env.backups)) == sorted(names + ["notes.txt"])
    b = env.admin.list_backups()[0]
    assert b["size"] == len(b"backup")
    assert b["created"] == "2026-10-27T12:00:00Z"
    assert b["user"] == "andy"
    assert env.admin.list_backups()[1]["user"] == "andy"  # an A1 backup, no user in its name


# --- one at a time ---------------------------------------------------------------


def test_one_operation_at_a_time(logged_in, admin_env, blocking_build):
    env = admin_env
    pending(env)

    first = logged_in.post("/api/update", json={}, headers=H)
    assert first.status_code == 202
    assert blocking_build.reached.wait(10)

    for path in ("/api/update", "/api/undo", "/api/check"):
        r = logged_in.post(path, json={}, headers=H)
        assert r.status_code == 409, path
        assert "Something else is running" in r.get_json()["error"]
    op = logged_in.get("/api/op").get_json()["op"]
    assert op["outcome"] == "running"
    assert op["log"][-1]["text"] == "Building the new version. OpenFit keeps running meanwhile…"
    assert sum(1 for c in env.fake.commands() if c[:2] == ["git", "merge"]) == 1

    blocking_build.release.set()
    env.admin.wait(10)
    assert logged_in.get("/api/op").get_json()["op"]["outcome"] == "ok"
    assert not env.admin.lock.locked()


# --- the Admin's own update ------------------------------------------------------


def test_admin_only_changes_are_flagged_and_never_touched(logged_in, admin_env):
    env = admin_env
    pending(env, {"admin/admin_server.py": "print('admin 2')\n"}, "Admin fix")

    status = logged_in.get("/api/status").get_json()
    au = status["admin_update"]
    assert au["available"] is True
    assert au["pulled"] is False
    assert [c["subject"] for c in au["commits"]] == ["Admin fix"]
    assert au["command"] == (
        f"cd {env.repos.repo} && docker compose -f admin/docker-compose.yml -p openfit-admin up -d --build"
    )
    assert status["update_available"] is True  # pulling brings everything

    op = run_update(env)

    assert op["outcome"] == "ok"
    for c in env.fake.commands():
        if c[:2] == ["docker", "compose"]:
            assert c[2:4] == ["-p", "openfit"], c
        assert "admin/docker-compose.yml" not in " ".join(c)
        assert not (c[0] == "docker" and "openfit-admin" in c[2:4]), c
        assert not any(a.startswith("openfit-admin-") for a in c), c
    # Pulled now, not rebuilt: still flagged, with the command.
    au = logged_in.get("/api/status").get_json()["admin_update"]
    assert au["available"] is True
    assert au["pulled"] is True
    assert au["commits"] == []


def test_no_admin_note_when_admin_is_current(logged_in, admin_env):
    pending(admin_env)
    au = logged_in.get("/api/status").get_json()["admin_update"]
    assert au["available"] is False and au["pulled"] is False


def test_the_status_reports_the_user_system(logged_in, admin_env):
    env = admin_env
    s = logged_in.get("/api/status").get_json()
    assert s["openfit"] == {
        "user": "andy",
        "port": 8080,
        "state": "running",
        "running": True,
        "version": {"sha": short(env), "subject": "Initial commit"},
    }
    assert s["check"] is None and s["update_available"] is False
    assert s["op"] is None and s["history"] == [] and s["backups"] == []
    assert (env.data / "admin.json").exists()


def test_an_interrupted_operation_is_marked_on_restart(admin_env):
    env = admin_env
    pending(env)
    op = env.admin._begin("update")  # as if the Admin stopped mid-update
    again = env.make()
    assert again.history()[-1]["id"] == op["id"]
    assert again.history()[-1]["outcome"] == "interrupted"
