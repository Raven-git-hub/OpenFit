"""The Admin updating itself (A4).

The running Admin finds its own container, keeps its image as
openfit-admin:prev, snapshots its settings, builds the new Admin while it
keeps running, and hands the swap to a helper container running the old
image: the helper (admin_swap.py) recreates the Admin on the new one,
health-checks it, and puts the old one back - image and settings - if it
doesn't answer. The Admin running once the helper has gone finishes the
op from the helper's result file.

As for the user system's update, the order of the steps is the safety, so
the tests pin exact command sequences: git runs for real against temp
repos, docker is conftest's FakeRunner, the clock is fake.
"""

import ast
import glob
import json
import os
import stat
import subprocess
import threading

import pytest
from conftest import ADMIN_IMAGE_ID, Call, container_id

import admin_ops
import admin_swap

H = {"X-OpenFit-Admin": "1"}
PASSWORD = "a long password!"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STAMP = "20261027T120000Z"
OP_ID = f"{STAMP}-admin_update"
HANDOVER = "Handing over to the helper. The Admin restarts in a moment, and this page reconnects by itself."


def docker(*args):
    return Call(["docker", *args], None, False)


ADMIN_PS = docker(
    "ps", "-a", "--no-trunc",
    "--filter", "label=com.docker.compose.project=openfit-admin",
    "--filter", "label=com.docker.compose.service=admin",
    "--filter", "label=com.docker.compose.oneoff=False",
    "--format", "{{.ID}}\t{{.State}}",
)
HELPER_PS = docker("ps", "-a", "--no-trunc", "--filter", "name=^openfit-admin-swap$", "--format", "{{.ID}}")
RETAG = docker("tag", "openfit-admin:prev", "openfit-admin:local")
UP = ("up", "-d", "--no-build", "--force-recreate", "admin")


def admin_cid(n):
    return container_id("openfit-admin", n)


def acompose(env, *args):
    """A compose command on the Admin's own project, as admin_compose() runs it."""
    folder = os.path.join(env.repos.repo, "admin")
    return Call(["docker", "compose", "-p", "openfit-admin", "-f", os.path.join(folder, "docker-compose.yml"), *args],
                folder, False)


def health(cid):
    return docker("exec", cid, "python", "-c", admin_ops.ADMIN_HEALTH_CODE)


def helper_run(env, stamp=STAMP, op_id=OP_ID):
    repo = env.repos.repo
    return docker(
        "run", "-d", "--rm", "--name", "openfit-admin-swap", "--label", "openfit.admin.helper=true",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-v", f"{repo}:{repo}",
        "-v", "openfit-admin_admin-data:/data",
        "-e", f"OPENFIT_REPO={repo}",
        "openfit-admin:prev",
        "python", "/app/admin_swap.py", stamp, op_id,
    )


def run_update(env):
    env.admin.start_update()
    env.admin.wait(10)
    return env.admin.current_op()


def pulled(env):
    """An Admin change on GitHub, pulled by Update OpenFit: the repo's admin/
    now differs from the code the Admin runs. The calls cleared."""
    env.repos.push({"admin/admin_server.py": "print('admin 2')\n"}, "Admin fix")
    env.admin.check()
    assert run_update(env)["outcome"] == "ok"
    env.fake.clear()


def notes(env):
    """The two reads an Admin update starts with, noting what it brings for
    the Versions panel: HEAD (the code it builds), then the commit the
    running Admin was built from - here the old of the update that pulled
    its new code. git, as the owner; nothing changed."""
    base = next(o for o in env.admin.history() if o["kind"] == "update")["old"]["commit"]
    return [Call(["git", "log", "-1", "--format=%H%x09%h%x09%s", "HEAD"], env.repos.repo, True),
            Call(["git", "log", "-1", "--format=%H%x09%h%x09%cI%x09%s", base, "--"], env.repos.repo, True)]


def hand_over(env):
    """Update the Admin, up to the hand-over: its op, and the commands run.
    Read from memory, so nothing more is run."""
    env.admin.start_admin_update()
    env.admin.wait(10)
    return env.admin.op, list(env.fake.calls)


def swap(env, stamp=STAMP, op_id=OP_ID):
    return admin_swap.swap(env.fake, env.repos.repo, str(env.data), stamp, op_id, env.clock, env.clock.sleep)


def result(env):
    return json.loads((env.data / "self_update_result.json").read_text())


def history_op(env, op_id=OP_ID):
    return next(o for o in env.admin.history() if o["id"] == op_id)


def short(env):
    return subprocess.run(["git", "log", "-1", "--format=%h"], cwd=env.repos.repo,
                          capture_output=True, text=True, check=True).stdout.strip()


def write_settings(env):
    """All six of the Admin's settings files, the ones a test doesn't make
    otherwise written by hand: {name: bytes}."""
    d = env.data
    (d / "users.json").write_text(json.dumps({"users": [], "removed": []}))
    for name, body in (("auth.json", b'{"password_hash": "pbkdf2:old"}'), ("secret_key", b"s" * 32)):
        fd = os.open(d / name, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(body)
    return {name: (d / name).read_bytes() for name in admin_ops.SNAPSHOT_FILES}


@pytest.fixture
def logged_in(admin_client, admin_env):
    admin_env.auth.setup(admin_env.auth.setup_code, PASSWORD)
    assert admin_client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
    return admin_client


# --- 1. when it's offered ------------------------------------------------------------


def test_a_pulled_admin_change_offers_update_the_admin(logged_in, admin_env):
    pulled(admin_env)
    au = logged_in.get("/api/status").get_json()["admin_update"]
    assert au == {
        "available": True,
        "pulled": True,
        "commits": [],
        "command": f"cd {admin_env.repos.repo} && docker compose -f admin/docker-compose.yml -p openfit-admin up -d --build",
        "can_self_update": True,
    }


def test_unpulled_admin_commits_say_update_openfit_first(logged_in, admin_env):
    env = admin_env
    env.repos.push({"admin/admin_server.py": "print('admin 2')\n"}, "Admin fix")
    env.admin.check()
    env.fake.clear()

    au = logged_in.get("/api/status").get_json()["admin_update"]
    assert au["available"] is True and au["pulled"] is False and au["can_self_update"] is False
    assert [c["subject"] for c in au["commits"]] == ["Admin fix"]
    env.fake.clear()

    r = logged_in.post("/api/admin/update", json={}, headers=H)
    assert r.status_code == 409
    assert r.get_json()["error"] == "Update OpenFit first: it brings the Admin's new code with it."
    assert env.fake.calls == []
    assert env.admin.history() == []
    assert not env.admin.lock.locked()


def test_pulled_with_more_admin_commits_waiting_is_update_openfit_first(admin_env):
    env = admin_env
    pulled(env)
    env.repos.push({"admin/admin_ops.py": "print('more')\n"}, "Another Admin fix")
    env.admin.check()
    au = env.admin.admin_update(env.admin.last_check())
    assert au["pulled"] is True and au["can_self_update"] is False
    with pytest.raises(admin_ops.Refused) as e:
        env.admin.start_admin_update()
    assert e.value.status == 409 and "Update OpenFit first" in e.value.message


def test_an_admin_that_is_up_to_date_is_refused(logged_in, admin_env):
    au = logged_in.get("/api/status").get_json()["admin_update"]
    assert au["available"] is False and au["can_self_update"] is False
    admin_env.fake.clear()
    r = logged_in.post("/api/admin/update", json={}, headers=H)
    assert r.status_code == 409
    assert r.get_json()["error"] == "The Admin is up to date."
    assert admin_env.fake.calls == []


def test_update_the_admin_is_refused_while_an_op_runs(logged_in, admin_env):
    env = admin_env
    pulled(env)
    env.repos.push({"app/main.py": "print('new')\n"}, "New OpenFit")
    env.admin.check()
    release, reached = threading.Event(), threading.Event()

    def build(args):  # Update OpenFit, held at its build
        reached.set()
        release.wait(10)
        return 0, "", ""

    env.fake.rules.append((lambda a: a[:2] == ["docker", "compose"] and "build" in a, build))
    assert logged_in.post("/api/update", json={}, headers=H).status_code == 202
    assert reached.wait(10)
    assert env.admin.admin_update(env.admin.last_check())["can_self_update"] is True
    before = len(env.fake.calls)

    r = logged_in.post("/api/admin/update", json={}, headers=H)

    assert r.status_code == 409
    assert "Something else is running" in r.get_json()["error"]
    assert env.fake.calls[before:] == []
    assert [o["kind"] for o in env.admin.history()][-1] == "update"
    release.set()
    env.admin.wait(10)


# --- 2. the Admin's sequence ---------------------------------------------------------------


def test_the_admin_runs_its_steps_in_the_exact_order(admin_env):
    env = admin_env
    pulled(env)

    op, calls = hand_over(env)

    assert calls == [
        # 0. what it brings, noted (two git reads)
        *notes(env),
        # 1. its own container, by its labels, and that container's image
        ADMIN_PS,
        docker("inspect", "--type", "container", admin_cid(0)),
        # 2. no helper already there
        HELPER_PS,
        # 3. the image it runs now kept as :prev  (4. its settings snapshot: files, no command)
        docker("tag", ADMIN_IMAGE_ID, "openfit-admin:prev"),
        # 5. the new Admin built, while this one keeps running
        acompose(env, "build", "admin"),
        # 6. the helper, on the old image
        helper_run(env),
    ]
    assert op["id"] == OP_ID and op["kind"] == "admin_update"
    assert op["outcome"] == "running"
    assert op["log"][-1]["text"] == HANDOVER
    # Still running, and the lock still held: the helper finishes it.
    assert env.admin.lock.locked()
    saved = history_op(env)
    assert saved["outcome"] == "running" and saved["log"] == op["log"]
    # The snapshot's history holds the whole log, for a rollback to put back.
    snap = json.loads((env.data / "self-update" / STAMP / "history.json").read_text())
    assert next(o for o in snap if o["id"] == OP_ID)["log"][-1]["text"] == HANDOVER


def test_the_snapshot_holds_the_six_settings_files(admin_env):
    env = admin_env
    pulled(env)
    live = write_settings(env)
    os.makedirs(env.data / "users" / "sam")
    (env.data / "users" / "sam" / "docker-compose.yml").write_text("services: {}\n")
    (env.backups / "20261026T000000Z-abcdef0-andy.tar.gz").write_bytes(b"backup")

    hand_over(env)

    folder = env.data / "self-update" / STAMP
    assert sorted(os.listdir(folder)) == sorted(admin_ops.SNAPSHOT_FILES)
    for name in admin_ops.SNAPSHOT_FILES:
        if name != "history.json":  # refreshed at the hand-over, with the log
            assert (folder / name).read_bytes() == live[name], name
    for name in ("auth.json", "secret_key"):
        assert stat.S_IMODE(os.stat(folder / name).st_mode) == 0o600, name
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700


def test_the_snapshot_takes_only_the_files_there_are(admin_env):
    env = admin_env
    pulled(env)
    hand_over(env)
    assert sorted(os.listdir(env.data / "self-update" / STAMP)) == ["admin.json", "history.json", "last_check.json"]


def test_snapshots_are_pruned_to_the_newest_three(admin_env):
    env = admin_env
    pulled(env)
    root = env.data / "self-update"
    olds = ["20261020T000000Z", "20261021T000000Z", "20261022T000000Z", "20261023T000000Z"]
    for stamp in olds:
        os.makedirs(root / stamp)
        (root / stamp / "admin.json").write_text("{}")
    (root / "notes.txt").write_text("not a snapshot")

    hand_over(env)

    assert sorted(os.listdir(root)) == sorted([STAMP, olds[3], olds[2], "notes.txt"])


# --- 3., 4. nothing changed ------------------------------------------------------------------


def test_a_failed_build_starts_no_helper(admin_env):
    env = admin_env
    pulled(env)
    env.fake.fail(lambda a: a[:4] == ["docker", "compose", "-p", "openfit-admin"] and "build" in a, err="pip: no such package")

    env.admin.start_admin_update()
    env.admin.wait(10)

    commands = env.fake.commands()
    assert commands[-1] == acompose(env, "build", "admin").args
    assert not any(c[:2] == ["docker", "run"] for c in commands)
    op = env.admin.current_op()
    assert op["outcome"] == "failed"
    assert op["summary"] == "The build failed, so nothing was changed. The Admin kept running."
    assert any("pip: no such package" in line["text"] for line in op["log"])
    assert not env.admin.lock.locked()
    assert not (env.data / "self_update_result.json").exists()


def test_a_helper_that_wont_start_leaves_the_admin_as_it_is(admin_env):
    env = admin_env
    pulled(env)
    env.fake.fail(lambda a: a[:3] == ["docker", "run", "-d"], err="Conflict. The container name is already in use")

    env.admin.start_admin_update()
    env.admin.wait(10)

    op = env.admin.current_op()
    assert env.fake.commands()[-1] == helper_run(env).args
    assert op["outcome"] == "failed"
    assert op["summary"] == "The helper didn't start, so the Admin wasn't swapped. It's still on its old version."
    assert not env.admin.lock.locked()


def test_an_existing_helper_is_a_refusal(admin_env):
    env = admin_env
    pulled(env)
    env.fake.helper = True  # another update's helper, still there

    env.admin.start_admin_update()
    env.admin.wait(10)

    assert env.fake.calls == [*notes(env), ADMIN_PS, docker("inspect", "--type", "container", admin_cid(0)),
                              HELPER_PS]
    op = env.admin.current_op()
    assert op["outcome"] == "failed"
    assert op["summary"] == "An Admin update is already under way. Nothing was changed."
    assert not (env.data / "self-update").exists()
    assert not env.admin.lock.locked()


def test_no_admin_container_fails_with_nothing_changed(admin_env):
    env = admin_env
    pulled(env)
    env.fake.projects["openfit-admin"]["state"] = None

    env.admin.start_admin_update()
    env.admin.wait(10)

    assert env.fake.calls == [*notes(env), ADMIN_PS]
    op = env.admin.current_op()
    assert op["outcome"] == "failed" and op["summary"] == "Nothing was changed."
    assert "The Admin's own container wasn't found" in op["log"][-2]["text"]


def test_two_admin_containers_fail_with_nothing_changed(admin_env):
    env = admin_env
    pulled(env)
    two = f"{admin_cid(0)}\trunning\n{admin_cid(9)}\trunning\n"
    env.fake.rules.append((lambda a: a[:2] == ["docker", "ps"] and "label=com.docker.compose.service=admin" in a,
                           (0, two, "")))

    env.admin.start_admin_update()
    env.admin.wait(10)

    assert env.fake.calls == [*notes(env), ADMIN_PS]
    assert env.admin.current_op()["outcome"] == "failed"


# --- 5., 6. the helper ---------------------------------------------------------------------


def test_the_swap_when_the_new_admin_answers(admin_env):
    env = admin_env
    pulled(env)
    hand_over(env)
    env.fake.clear()
    started = env.clock()

    assert swap(env) == "ok"

    assert env.fake.calls == [acompose(env, *UP), ADMIN_PS, health(admin_cid(1))]
    r = result(env)
    assert r["op_id"] == OP_ID and r["stamp"] == STAMP and r["outcome"] == "ok"
    texts = [line["text"] for line in r["lines"]]
    assert texts[:2] == ["The helper is here. Waiting a moment, so the Admin's reply reaches your browser…",
                         "Swapping in the new Admin…"]
    assert texts[-1] == "The new Admin is answering."
    # It waited before swapping, so the Admin's reply reached the browser.
    assert r["lines"][1]["t"] == admin_ops.iso(started + admin_swap.WAIT)


def test_the_result_file_is_written_as_it_goes(admin_env):
    env = admin_env
    pulled(env)
    hand_over(env)
    seen = []

    def up(args):
        seen.append(result(env))
        return 0, "", ""

    env.fake.rules.append((lambda a: a[:4] == ["docker", "compose", "-p", "openfit-admin"], up))
    swap(env)
    assert seen[0]["outcome"] == "running"
    assert seen[0]["lines"][-1]["text"] == "Swapping in the new Admin…"


def test_the_swap_rolls_back_an_admin_that_doesnt_answer(admin_env):
    env = admin_env
    pulled(env)
    snapped = write_settings(env)
    os.remove(env.data / "users.json")  # absent at the snapshot
    snapped = {k: v for k, v in snapped.items() if k != "users.json"}
    hand_over(env)
    snapped["history.json"] = (env.data / "self-update" / STAMP / "history.json").read_bytes()
    # What the new version did to the Admin's files in its moment.
    (env.data / "admin.json").write_text(json.dumps(dict(admin_ops.DEFAULT_SETTINGS, user="newv")))
    (env.data / "history.json").write_text("[]")
    (env.data / "auth.json").write_text('{"password_hash": "pbkdf2:new"}')
    (env.data / "users.json").write_text(json.dumps({"users": [], "removed": []}))  # a file it added
    (env.data / "newv.json").write_text("{}")  # not one of the six: left alone
    env.fake.unhealthy = {admin_cid(1)}
    env.fake.clear()

    assert swap(env) == "rolled_back"

    calls = env.fake.calls
    assert calls[:2] == [acompose(env, *UP), ADMIN_PS]
    assert calls[2:33] == [health(admin_cid(1))] * 31  # every 2s for 60s
    assert calls[33:] == [RETAG, acompose(env, *UP), ADMIN_PS, health(admin_cid(2))]
    for name, body in snapped.items():
        assert (env.data / name).read_bytes() == body, name
    assert not (env.data / "users.json").exists()
    assert (env.data / "newv.json").exists()
    r = result(env)
    assert r["outcome"] == "rolled_back"
    texts = [line["text"] for line in r["lines"]]
    assert "The new Admin didn't answer." in texts
    assert "Its settings are put back as they were before the update." in texts
    assert texts[-1] == "The old Admin is answering again."


def test_a_new_admin_that_wont_start_is_rolled_back(admin_env):
    env = admin_env
    pulled(env)
    hand_over(env)
    env.fake.clear()
    ups = []

    def up(args):
        ups.append(args)
        return (1, "", "port is already allocated") if len(ups) == 1 else (0, "", "")

    env.fake.rules.append((lambda a: a[:4] == ["docker", "compose", "-p", "openfit-admin"], up))

    assert swap(env) == "rolled_back"
    assert env.fake.calls == [acompose(env, *UP), RETAG, acompose(env, *UP), ADMIN_PS, health(admin_cid(0))]
    assert any("port is already allocated" in line["text"] for line in result(env)["lines"])


def test_a_rollback_that_doesnt_answer_either_is_failed_with_the_manual_command(admin_env):
    env = admin_env
    pulled(env)
    hand_over(env)
    env.fake.unhealthy = {admin_cid(1), admin_cid(2)}
    env.fake.clear()

    assert swap(env) == "failed"

    r = result(env)
    assert r["outcome"] == "failed"
    last = r["lines"][-1]["text"]
    assert last.startswith("Where things stand: openfit-admin:local is the old Admin's image again, its settings are put back")
    assert admin_ops.admin_command(env.repos.repo) in last
    assert (f"cd {env.repos.repo} && docker tag openfit-admin:prev openfit-admin:local && docker compose "
            "-f admin/docker-compose.yml -p openfit-admin up -d --no-build --force-recreate admin") in last
    assert "The old Admin didn't answer either." in [line["text"] for line in r["lines"]]


def test_the_helper_changes_nothing_without_its_snapshot(admin_env):
    env = admin_env
    for stamp, op_id in (("20991231T000000Z", OP_ID), ("../etc", OP_ID), (STAMP, "x; reboot")):
        env.fake.clear()
        assert swap(env, stamp, op_id) == "failed"
        assert env.fake.calls == []


def test_a_helper_bug_is_a_failed_result_not_a_running_one(admin_env, monkeypatch):
    env = admin_env
    pulled(env)
    hand_over(env)
    monkeypatch.setattr(admin_swap, "recreate", lambda runner, repo: 1 / 0)
    assert swap(env) == "failed"
    assert result(env)["outcome"] == "failed"
    assert "unexpected error" in result(env)["lines"][-1]["text"]


# --- 7. finishing the op -----------------------------------------------------------------------


@pytest.mark.parametrize("outcome, summary", [
    ("ok", None),
    ("rolled_back", "The new Admin didn't come up, so the old one was put back, with its settings as they were."),
    ("failed", "The Admin update didn't finish. The log says where things stand, and what to run on the server."),
])
def test_startup_finishes_the_op_from_the_result_file(admin_env, outcome, summary):
    env = admin_env
    pulled(env)
    op, _calls = hand_over(env)
    env.fake.unhealthy = {"ok": set(), "rolled_back": {admin_cid(1)},
                          "failed": {admin_cid(1), admin_cid(2)}}[outcome]
    assert swap(env) == outcome
    helper_lines = result(env)["lines"]
    env.admin._begin("update")  # another op left "running": it is marked, as ever
    env.fake.clear()

    again = env.make()  # the Admin that starts next

    done = next(o for o in again.history() if o["id"] == OP_ID)
    assert done["outcome"] == outcome
    assert done["summary"] == (summary or f"The Admin updated itself to {short(env)}.")
    assert done["finished_at"] is not None
    assert done["log"] == op["log"] + helper_lines + [done["log"][-1]]
    assert done["log"][-1]["text"] == done["summary"]
    assert not (env.data / "self_update_result.json").exists()
    assert [o["outcome"] for o in again.history() if o["kind"] == "update"][-1] == "interrupted"
    assert not again.lock.locked()
    assert not any(c.args[0] == "docker" for c in env.fake.calls)


def test_startup_with_no_result_file_marks_a_running_admin_update_interrupted(admin_env):
    env = admin_env
    op = env.admin._begin("admin_update")  # the Admin stopped before the helper ran
    again = env.make()
    assert next(o for o in again.history() if o["id"] == op["id"])["outcome"] == "interrupted"
    assert not again.lock.locked()


def test_a_result_file_that_doesnt_read_is_removed(admin_env):
    env = admin_env
    op = env.admin._begin("admin_update")
    (env.data / "self_update_result.json").write_text('{"op_id": "nope"}')
    again = env.make()
    assert not (env.data / "self_update_result.json").exists()
    assert next(o for o in again.history() if o["id"] == op["id"])["outcome"] == "interrupted"


def test_startup_while_the_helper_is_still_at_work_waits_for_it(logged_in, admin_env):
    env = admin_env
    pulled(env)
    op, _calls = hand_over(env)
    # The helper has swapped, and is checking the new Admin answers.
    log = admin_swap.Log(str(env.data), OP_ID, STAMP, env.clock)
    log.say("Swapping in the new Admin…")
    log.say("Checking the new Admin is answering…")
    env.fake.clear()

    again = env.make()  # the new Admin, starting

    assert again.lock.locked()
    assert history_op(env)["outcome"] == "running"
    assert env.fake.calls == []  # nothing run at startup
    assert again.current_op()["outcome"] == "running"  # the helper is still there
    with pytest.raises(admin_ops.Busy):
        again.check()

    # It finds the new Admin answering, writes its outcome, and exits.
    log.say("The new Admin is answering.")
    log.finish("ok")
    env.fake.helper = False
    done = again.current_op()

    assert done["id"] == OP_ID and done["outcome"] == "ok"
    assert [line["text"] for line in done["log"]][-4:] == [
        "Swapping in the new Admin…", "Checking the new Admin is answering…", "The new Admin is answering.",
        f"The Admin updated itself to {short(env)}.",
    ]
    assert done["log"][:len(op["log"])] == op["log"]
    assert not again.lock.locked()
    assert not (env.data / "self_update_result.json").exists()
    assert history_op(env)["outcome"] == "ok"


def test_the_old_admin_finishes_the_op_if_the_helper_goes_without_swapping(admin_env):
    env = admin_env
    pulled(env)
    hand_over(env)
    assert env.admin.current_op()["outcome"] == "running"  # the helper is still there
    env.fake.helper = False  # and then it's gone, with nothing written

    op = env.admin.current_op()

    assert op["outcome"] == "failed"
    assert op["summary"] == "The helper stopped part-way. The log says how far it got."
    assert op["log"][-2]["text"] == "The helper stopped before it swapped the Admin over, so this is still the old version."
    assert not env.admin.lock.locked()
    assert history_op(env)["outcome"] == "failed"


def test_a_helper_that_dies_mid_way_leaves_its_trail(admin_env):
    env = admin_env
    pulled(env)
    hand_over(env)
    log = admin_swap.Log(str(env.data), OP_ID, STAMP, env.clock)
    log.say("Swapping in the new Admin…")  # and then the helper died
    env.fake.helper = False

    again = env.make()  # the Admin running now, started by hand
    op = again.current_op()

    assert op["outcome"] == "failed"
    texts = [line["text"] for line in op["log"]]
    assert "Swapping in the new Admin…" in texts
    assert admin_ops.admin_command(env.repos.repo) in texts[-2]
    assert not again.lock.locked()


# --- 8. the lane ---------------------------------------------------------------------------------


def all_the_ways(env):
    """Every path of an Admin update: the Admin's sequence, then the helper
    swapping, rolling back, and failing; each outcome as it should be."""
    pulled(env)
    hand_over(env)
    assert swap(env) == "ok"
    env.fake.unhealthy = {admin_cid(2)}
    assert swap(env) == "rolled_back"
    env.fake.unhealthy = {admin_cid(4), admin_cid(5)}
    assert swap(env) == "failed"


def test_the_admins_own_project_only_through_admin_compose(admin_env):
    env = admin_env
    all_the_ways(env)
    seen = set()
    for call, frames in zip(env.fake.calls, env.fake.frames):
        a = call.args
        if a[0] != "docker" or "openfit-admin" not in a:
            continue
        # Its own project, by name, appears only as -p, from admin_compose().
        assert a[:4] == ["docker", "compose", "-p", "openfit-admin"], a
        assert "admin_compose" in frames, (a, frames)
        assert a[4:6] == ["-f", os.path.join(env.repos.repo, "admin", "docker-compose.yml")], a
        assert tuple(a[6:]) in (("build", "admin"), UP), a
        seen.add(tuple(a[6:]))
    assert seen == {("build", "admin"), UP}


def test_admin_compose_runs_only_its_two_commands(admin_env):
    for args in (("down",), ("down", "-v"), ("build",), ("up", "-d"), ("build", "admin", "--no-cache"),
                 ("up", "-d", "--no-build", "--force-recreate"), ("rm", "-f", "admin"), ("stop", "admin")):
        with pytest.raises(ValueError):
            admin_ops.admin_compose(admin_env.fake, admin_env.repos.repo, *args)
    assert admin_env.fake.calls == []


def test_admin_compose_is_called_from_exactly_two_places():
    """The Admin's build, and the helper's recreate."""
    sites = []
    for path in sorted(glob.glob(os.path.join(ROOT, "admin", "*.py"))):
        tree = ast.parse(open(path, encoding="utf-8").read())
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Call):
                    f = node.func
                    name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else None
                    if name == "admin_compose":
                        sites.append((os.path.basename(path), fn.name))
    assert sorted(sites) == [("admin_ops.py", "_admin_update"), ("admin_swap.py", "recreate")]


def test_the_swap_never_names_another_project_and_never_deletes(admin_env):
    env = admin_env
    pulled(env)
    hand_over(env)
    env.fake.clear()
    env.fake.unhealthy = {admin_cid(1)}
    assert swap(env) == "rolled_back"
    env.fake.unhealthy = {admin_cid(n) for n in range(3, 7)}
    assert swap(env) == "failed"
    assert swap(env) == "failed"  # and again
    for a in env.fake.commands():
        assert a[0] == "docker" and a[1] in ("compose", "ps", "exec", "tag"), a
        if a[1] == "compose":
            assert a[2:4] == ["-p", "openfit-admin"], a
        if a[1] == "ps":
            projects = [x.split("=", 2)[2] for x in a if x.startswith("label=com.docker.compose.project=")]
            assert projects == ["openfit-admin"], a
        if a[1] == "exec":
            assert a[2] in {admin_cid(n) for n in range(8)}, a
        if a[1] == "tag":
            assert a == RETAG.args, a
        for banned in ("down", "-v", "--volumes", "rm", "--rm", "volume", "kill", "stop"):
            assert banned not in a, a
        assert not any(x == "openfit" or x.startswith("openfit-") and x not in (
            "openfit-admin", "openfit-admin:prev", "openfit-admin:local") for x in a), a


# --- 9. the compose lane guard (the A3 review's gap) -----------------------------------------------


def lane_cases(admin):
    """Users the Admin never runs compose for, each one rule broken."""
    return {
        "an added user in the Admin's project": admin_ops.User(
            "sam", "openfit-admin", "tracker", admin._compose_file_of("sam"), 8101, False),
        "a primary with another compose file": admin_ops.User(
            "andy", "openfit", "tracker", "/elsewhere/docker-compose.yml", 8080, True),
        "a primary in another project": admin_ops.User(
            "andy", "openfit-admin", "tracker", admin.compose_file, 8080, True),
        "an added user with another name's compose file": admin_ops.User(
            "sam", "openfit-sam", "tracker", admin._compose_file_of("kim"), 8101, False),
        "an added user with the repo's compose file": admin_ops.User(
            "sam", "openfit-sam", "tracker", admin.compose_file, 8101, False),
        "a name that isn't a user's": admin_ops.User(
            "Sam!", "openfit-Sam!", "tracker", admin._compose_file_of("Sam!"), 8101, False),
    }


LANE_CASES = [
    "an added user in the Admin's project",
    "a primary with another compose file",
    "a primary in another project",
    "an added user with another name's compose file",
    "an added user with the repo's compose file",
    "a name that isn't a user's",
]


@pytest.mark.parametrize("case", LANE_CASES)
def test_compose_refuses_a_user_outside_the_lane(admin_env, case):
    admin = admin_env.admin
    user = lane_cases(admin)[case]
    with pytest.raises(ValueError, match="outside the Admin's lane"):
        admin.compose(user, "up", "-d", "--no-build", "tracker")
    assert admin_env.fake.calls == []


def test_compose_runs_for_the_users_in_the_lane(admin_env):
    admin = admin_env.admin
    for user in (admin.primary(), admin.added("sam", 8101)):
        admin.compose(user, "stop", "tracker")
    assert [c.args[3] for c in admin_env.fake.calls] == ["openfit", "openfit-sam"]


# --- 10. auth, and the page ------------------------------------------------------------------------


def test_update_the_admin_needs_a_session_and_the_header(admin_client, logged_in, admin_env):
    env = admin_env
    pulled(env)
    logged_in.post("/api/logout", json={}, headers=H)
    r = logged_in.post("/api/admin/update", json={}, headers=H)
    assert r.status_code == 401
    assert logged_in.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
    assert logged_in.post("/api/admin/update", json={}).status_code == 403
    assert logged_in.post("/api/admin/update", data="{}", headers=H).status_code == 403  # not JSON
    assert env.fake.calls == [] and [o["kind"] for o in env.admin.history()] == ["update"]

    r = logged_in.post("/api/admin/update", json={}, headers=H)
    assert r.status_code == 202
    assert r.get_json()["op"]["kind"] == "admin_update"
    env.admin.wait(10)


def test_the_page_offers_the_button_with_its_confirm(logged_in):
    # The portal's update popup (S1), opened from the Admin's Update available.
    page = logged_in.get("/").get_data(as_text=True)
    assert 'admin: "Update the Admin"' in page and 'admin: "/api/admin/update"' in page
    assert ("The Admin restarts on its new version. This page will be unavailable for up to a minute and reconnects by "
            "itself. If the new version doesn't start, the old one is put back.") in page
    assert "Update OpenFit first: it brings the Admin's new code with it." in page
    assert "The Admin is restarting…" in page
