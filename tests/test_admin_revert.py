"""Reverting the Admin (S2): back to the version before its last update.

The Admin running now keeps its image as openfit-admin:next and its
settings as they are now in a fresh snapshot, then hands the revert to a
helper running that same image (openfit-admin:local), never the one it
goes back to. The helper (admin_swap.revert) retags openfit-admin:prev as
:local, puts back only admin.json and users.json from the update's
snapshot - the password, the sessions' key, the activity and the last
check stay as they are - and recreates the Admin; if it doesn't answer,
:next goes back as :local, all six files come back from the fresh
snapshot, and it's recreated again. The Admin running once the helper has
gone finishes the op from the helper's result file, as an update's.

As for the Admin's update, the order of the steps is the safety, so the
tests pin exact command sequences: git runs for real against temp repos,
docker is conftest's FakeRunner (which keeps the Admin's image tags, so
the container's inspect says which version it's on), the clock is fake.
"""

import json
import os
import shutil
import threading
from datetime import datetime, timezone

import pytest
from conftest import ADMIN_IMAGE_ID, built_admin_image, seed_users
from test_admin_self_update import (ADMIN_PS, HELPER_PS, OP_ID, RETAG, STAMP, UP, acompose, admin_cid, docker,
                                    hand_over, health, pulled, swap)

import admin_ops
import admin_swap

H = {"X-OpenFit-Admin": "1"}
PASSWORD = "a long password!"
FRESH = "20261027T120100Z"  # a minute after the update: the revert's own snapshot
REVERT_ID = f"{FRESH}-admin_revert"
PREV_INSPECT = docker("image", "inspect", "--format", "{{.Id}}", "openfit-admin:prev")
NEXT_RETAG = docker("tag", "openfit-admin:next", "openfit-admin:local")
OFFERED = {"offered": True, "reason": None, "update": OP_ID}


def inspect(cid):
    return docker("inspect", "--type", "container", cid)


def revert_run(env, update=STAMP, fresh=FRESH, op_id=REVERT_ID):
    repo = env.repos.repo
    return docker(
        "run", "-d", "--rm", "--name", "openfit-admin-swap", "--label", "openfit.admin.helper=true",
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-v", f"{repo}:{repo}",
        "-v", "openfit-admin_admin-data:/data",
        "-e", f"OPENFIT_REPO={repo}",
        "openfit-admin:local",
        "python", "/app/admin_swap.py", "revert", update, fresh, op_id,
    )


def updated(env, users=()):
    """An Admin update that worked, `users` (seed_users()'s) there before
    it: pulled, handed over, swapped, and finished from the helper's
    result once the helper has gone. At 12:01 (FRESH), the calls cleared.
    (The same Admin finishes it, as the old one does when its helper goes
    - so a test's Flask app, made over env.admin, stays on the Admin that
    holds the history.)"""
    if users:
        seed_users(env, *users)
    pulled(env)
    hand_over(env)
    assert swap(env) == "ok"
    env.fake.helper = False  # the helper has gone
    assert env.admin.current_op()["outcome"] == "ok"
    env.clock.now = datetime(2026, 10, 27, 12, 1, tzinfo=timezone.utc).timestamp()
    op = next(o for o in env.admin.history() if o["id"] == OP_ID)
    assert op["outcome"] == "ok" and op["snapshot"] == STAMP
    env.fake.clear()
    return op


def handed(env):
    """Revert the Admin, up to its hand-over: its op, from memory."""
    env.admin.start_admin_revert()
    env.admin.wait(10)
    return env.admin.op


def revert(env, update=STAMP, fresh=FRESH, op_id=REVERT_ID):
    return admin_swap.revert(env.fake, env.repos.repo, str(env.data), update, fresh, op_id, env.clock,
                             env.clock.sleep)


def result(env):
    return json.loads((env.data / "self_update_result.json").read_text())


def history_op(env, op_id=REVERT_ID):
    return next(o for o in json.loads((env.data / "history.json").read_text()) if o["id"] == op_id)


def files(folder, names=admin_ops.SNAPSHOT_FILES):
    return {n: (folder / n).read_bytes() for n in names if (folder / n).exists()}


def admin_image(env):
    """The image the Admin's container runs, as its inspect says."""
    return env.fake.projects["openfit-admin"]["image"]


@pytest.fixture
def logged_in(admin_client, admin_env):
    admin_env.auth.setup(admin_env.auth.setup_code, PASSWORD)
    assert admin_client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
    return admin_client


# --- 1. when it's offered -------------------------------------------------------------


def test_all_the_conditions_together_offer_it(admin_env):
    env = admin_env
    op = updated(env, users=[("sam", 8101, "running")])
    offer = env.admin.revert_offer()
    assert offer == dict(OFFERED, old=op["old"]["short"])
    # Whether it's on offer comes from Docker last: its container, its image, and :prev's.
    assert env.fake.calls == [ADMIN_PS, inspect(admin_cid(1)), PREV_INSPECT]
    assert admin_image(env) == built_admin_image(1) and env.fake.tags["openfit-admin:prev"] == ADMIN_IMAGE_ID
    assert env.admin.status()["admin_revert"] == offer


def no_update(env):
    pass


def update_failed(env):
    updated(env)
    later = env.admin._begin("admin_update")  # another one, which didn't finish
    env.admin._finish(later, "failed", "The build failed, so nothing was changed. The Admin kept running.")
    env.admin.op = None


def snapshot_gone(env):
    updated(env)
    shutil.rmtree(env.data / "self-update" / STAMP)


def snapshot_not_recorded(env):
    updated(env)
    ops = env.admin.history()
    for o in ops:
        o.pop("snapshot", None)  # an update an Admin from before S2 finished
    (env.data / "history.json").write_text(json.dumps(ops))


def user_added(env):
    updated(env)
    seed_users(env, ("sam", 8101, "running"))


def user_removed(env):
    updated(env, users=[("sam", 8101, "running"), ("kim", 8102, "running")])
    env.admin.remove_user("kim")


def kept_data_deleted(env):
    seed_users(env, ("sam", 8101, "running"))
    env.admin.remove_user("sam")
    updated(env)
    env.admin.delete_data("sam", "sam")


def user_readded(env):
    seed_users(env, ("sam", 8101, "running"))
    env.admin.remove_user("sam")
    updated(env)
    reg = json.loads((env.data / "users.json").read_text())
    reg["removed"] = []
    reg["users"] = [{"name": "sam", "port": 8101, "created_at": "2026-10-27T12:05:00Z"}]
    (env.data / "users.json").write_text(json.dumps(reg))


def something_running(env):
    updated(env)
    env.admin._begin("update")


def prev_gone(env):
    updated(env)
    del env.fake.tags["openfit-admin:prev"]


def already_reverted(env):
    updated(env)
    env.fake.tags["openfit-admin:prev"] = admin_image(env)


def docker_cant_say(env):
    updated(env)
    env.fake.fail(lambda a: a[:3] == ["docker", "image", "inspect"], err="Cannot connect to the Docker daemon")


@pytest.mark.parametrize("make, reason", [
    (no_update, "The Admin hasn't updated itself here yet, so there's no earlier version to go back to."),
    (update_failed, "The Admin's last update didn't finish, so there's no earlier version to go back to."),
    (snapshot_gone, "The Admin's settings from before that update aren't kept any more, so it can't be reverted."),
    (snapshot_not_recorded,
     "The Admin's settings from before that update aren't kept any more, so it can't be reverted."),
    (user_added, "Users were added or removed since that update, so it can't be reverted."),
    (user_removed, "Users were added or removed since that update, so it can't be reverted."),
    (kept_data_deleted, "Users were added or removed since that update, so it can't be reverted."),
    (user_readded, "Users were added or removed since that update, so it can't be reverted."),
    (something_running, "Something else is running. Wait for it to finish, then try again."),
    (prev_gone, "The Admin's previous version isn't on the server any more, so it can't be reverted."),
    (already_reverted, "The Admin is already on the version from before that update."),
    (docker_cant_say, "Couldn't ask Docker about the Admin's versions, so it can't be reverted now."),
], ids=lambda x: x.__name__ if callable(x) else None)
def test_each_condition_on_its_own_blocks_it_with_its_reason(admin_env, make, reason):
    env = admin_env
    make(env)
    offer = env.admin.status()["admin_revert"]
    assert offer["offered"] is False and offer["reason"] == reason


@pytest.mark.parametrize("make", [user_added, prev_gone, already_reverted, snapshot_gone],
                         ids=lambda f: f.__name__)
def test_a_revert_not_on_offer_is_refused_with_nothing_done(logged_in, admin_env, make):
    env = admin_env
    make(env)
    env.fake.clear()
    before = env.admin.history()

    r = logged_in.post("/api/admin/revert", json={}, headers=H)

    assert r.status_code == 409
    assert r.get_json()["error"] == env.admin.revert_offer()["reason"]
    assert not any(c[1] in ("tag", "run") for c in env.fake.commands() if c[0] == "docker")
    assert env.admin.history() == before
    assert not env.admin.lock.locked()


def test_a_revert_is_refused_while_an_op_runs(logged_in, admin_env):
    env = admin_env
    updated(env)
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
    before = len(env.fake.calls)

    r = logged_in.post("/api/admin/revert", json={}, headers=H)

    assert r.status_code == 409 and "Something else is running" in r.get_json()["error"]
    assert env.fake.calls[before:] == []
    assert logged_in.get("/api/status").get_json()["admin_revert"]["reason"] == (
        "Something else is running. Wait for it to finish, then try again.")
    release.set()
    env.admin.wait(10)


def test_reverting_needs_a_session_and_the_header(logged_in, admin_env):
    env = admin_env
    updated(env)
    logged_in.post("/api/logout", json={}, headers=H)
    assert logged_in.post("/api/admin/revert", json={}, headers=H).status_code == 401
    assert logged_in.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
    assert logged_in.post("/api/admin/revert", json={}).status_code == 403
    assert logged_in.post("/api/admin/revert", data="{}", headers=H).status_code == 403  # not JSON
    assert not any(o["kind"] == "admin_revert" for o in env.admin.history())

    r = logged_in.post("/api/admin/revert", json={}, headers=H)
    assert r.status_code == 202 and r.get_json()["op"]["kind"] == "admin_revert"
    env.admin.wait(10)


# --- 2. the Admin's steps ---------------------------------------------------------------


def test_the_admin_runs_its_steps_in_the_exact_order(admin_env):
    env = admin_env
    update = updated(env)

    op = handed(env)

    assert env.fake.calls == [
        # 0. on offer: its container, the image it runs, and :prev's
        ADMIN_PS, inspect(admin_cid(1)), PREV_INSPECT,
        # 1. its own container, by its labels, and that container's image
        ADMIN_PS, inspect(admin_cid(1)),
        # 2. no helper already there
        HELPER_PS,
        # 3. the image it runs now kept as :next  (4. a fresh settings snapshot: files, no command)
        docker("tag", built_admin_image(1), "openfit-admin:next"),
        # 5. the helper, on the image it runs now, with both stamps and the op
        revert_run(env),
    ]
    assert op["id"] == REVERT_ID and op["kind"] == "admin_revert" and op["outcome"] == "running"
    assert op["undoes"] == OP_ID and op["snapshot"] == FRESH
    # From the version the update brought, back to the one before it.
    assert op["old"] == update["new"] and op["new"] == update["old"]
    assert op["log"][-1]["text"] == ("Handing over to the helper. The Admin restarts in a moment, and this page "
                                     "reconnects by itself.")
    assert env.admin.lock.locked()
    assert history_op(env)["outcome"] == "running"
    assert env.fake.tags["openfit-admin:next"] == built_admin_image(1)


def test_the_helper_runs_the_current_image_never_prev(admin_env):
    env = admin_env
    updated(env)
    handed(env)
    run = env.fake.commands()[-1]
    image = run[run.index("python") - 1]
    assert image == "openfit-admin:local" and "openfit-admin:prev" not in run
    assert env.fake.tags[image] == admin_image(env)  # the one it runs now


def test_the_fresh_snapshot_is_the_settings_as_they_are_now(admin_env):
    env = admin_env
    updated(env)
    (env.data / "auth.json").write_text('{"password_hash": "pbkdf2:now"}')
    (env.data / "secret_key").write_bytes(b"n" * 32)
    live = files(env.data)

    handed(env)

    fresh = env.data / "self-update" / FRESH
    snap = files(fresh)
    assert set(snap) == set(live)
    for name in live:
        if name != "history.json":  # refreshed at the hand-over, with the log
            assert snap[name] == live[name], name
    kept = next(o for o in json.loads(snap["history.json"]) if o["id"] == REVERT_ID)
    assert kept["log"][-1]["text"].startswith("Handing over to the helper.")
    # The update's own snapshot, which the helper puts back, is untouched.
    assert (env.data / "self-update" / STAMP).is_dir()


def test_pruning_never_takes_the_snapshot_the_revert_puts_back(admin_env):
    env = admin_env
    updated(env)
    root = env.data / "self-update"
    between = ["20261027T120010Z", "20261027T120020Z", "20261027T120030Z"]  # newer than the update's
    for stamp in between:
        os.makedirs(root / stamp)

    handed(env)

    assert sorted(os.listdir(root)) == sorted([STAMP, FRESH, between[2], between[1]])


def test_an_existing_helper_is_a_refusal(admin_env):
    env = admin_env
    updated(env)
    env.fake.helper = True

    handed(env)

    assert env.fake.calls[3:] == [ADMIN_PS, inspect(admin_cid(1)), HELPER_PS]
    op = env.admin.current_op()
    assert op["outcome"] == "failed"
    assert op["summary"] == "An Admin update or revert is already under way. Nothing was changed."
    assert not (env.data / "self-update" / FRESH).exists()
    assert not env.admin.lock.locked()


def test_a_helper_that_wont_start_leaves_the_admin_as_it_is(admin_env):
    env = admin_env
    updated(env)
    env.fake.fail(lambda a: a[:3] == ["docker", "run", "-d"], err="Conflict")

    handed(env)

    op = env.admin.current_op()
    assert op["outcome"] == "failed"
    assert op["summary"] == "The helper didn't start, so the Admin wasn't reverted. It's still on its current version."
    assert not env.admin.lock.locked()


def test_a_tag_that_fails_starts_no_helper(admin_env):
    env = admin_env
    updated(env)
    env.fake.fail(lambda a: a[:2] == ["docker", "tag"], err="no space left")

    handed(env)

    assert not any(c[:2] == ["docker", "run"] for c in env.fake.commands())
    op = env.admin.current_op()
    assert op["outcome"] == "failed" and op["summary"] == "Nothing was changed."


# --- 3. the helper's revert ---------------------------------------------------------------


def changed_since(env):
    """The live settings as the Admin wrote them since its update: a new
    password and sessions' key, more activity, a new check, and admin.json
    and users.json as they are now (the same users). {name: bytes}."""
    d = env.data
    (d / "admin.json").write_text(json.dumps(dict(admin_ops.DEFAULT_SETTINGS, branch="main"), indent=4))
    if (d / "users.json").exists():
        reg = json.loads((d / "users.json").read_text())
        (d / "users.json").write_text(json.dumps(reg, indent=1))  # the same people, written another way
    (d / "auth.json").write_text('{"password_hash": "pbkdf2:changed since"}')
    (d / "secret_key").write_bytes(b"c" * 32)
    (d / "last_check.json").write_text(json.dumps({"checked_at": "2026-10-27T12:00:30Z", "behind": 0}))
    return files(d)


def test_the_helper_puts_back_only_the_settings_and_the_users(admin_env):
    env = admin_env
    updated(env, users=[("sam", 8101, "running")])
    (env.data / "auth.json").write_text('{"password_hash": "pbkdf2:at the update"}')
    handed(env)
    then = files(env.data / "self-update" / STAMP)
    assert {"admin.json", "users.json"} <= set(then)
    now = changed_since(env)
    assert now["admin.json"] != then["admin.json"] and now["users.json"] != then["users.json"]
    env.fake.clear()
    started = env.clock()

    assert revert(env) == "ok"

    assert env.fake.calls == [RETAG, acompose(env, *UP), ADMIN_PS, health(admin_cid(2))]
    assert admin_image(env) == ADMIN_IMAGE_ID  # the previous version, running
    for name in ("admin.json", "users.json"):
        assert (env.data / name).read_bytes() == then[name], name
    for name in ("auth.json", "secret_key", "history.json", "last_check.json"):
        assert (env.data / name).read_bytes() == now[name], name
    r = result(env)
    assert r["op_id"] == REVERT_ID and r["stamp"] == FRESH and r["outcome"] == "ok"
    texts = [line["text"] for line in r["lines"]]
    assert texts[:2] == ["The helper is here. Waiting a moment, so the Admin's reply reaches your browser…",
                         "Switching to the previous Admin's image…"]
    assert texts[-1] == "The previous Admin is answering."
    assert r["lines"][1]["t"] == admin_ops.iso(started + admin_swap.WAIT)


def test_a_users_json_the_update_didnt_have_is_removed(admin_env):
    env = admin_env
    updated(env)
    handed(env)
    (env.data / "users.json").write_text(json.dumps({"users": [], "removed": []}))  # nobody, still
    assert revert(env) == "ok"
    assert not (env.data / "users.json").exists()


def test_a_previous_admin_that_doesnt_answer_puts_the_current_one_back(admin_env):
    env = admin_env
    updated(env)
    handed(env)
    fresh = files(env.data / "self-update" / FRESH)
    changed_since(env)
    (env.data / "newer.json").write_text("{}")  # not one of the six: left alone
    env.fake.unhealthy = {admin_cid(2)}
    env.fake.clear()

    assert revert(env) == "rolled_back"

    calls = env.fake.calls
    assert calls[:3] == [RETAG, acompose(env, *UP), ADMIN_PS]
    assert calls[3:34] == [health(admin_cid(2))] * 31  # every 2s for 60s
    assert calls[34:] == [NEXT_RETAG, acompose(env, *UP), ADMIN_PS, health(admin_cid(3))]
    assert admin_image(env) == built_admin_image(1)  # the current version, running again
    for name in admin_ops.SNAPSHOT_FILES:
        if name in fresh:
            assert (env.data / name).read_bytes() == fresh[name], name
        else:
            assert not (env.data / name).exists(), name
    assert (env.data / "newer.json").exists()
    texts = [line["text"] for line in result(env)["lines"]]
    assert "The previous Admin didn't answer." in texts
    assert "Its settings are put back as they were before the revert." in texts
    assert texts[-1] == "The current Admin is answering again."
    # The fresh snapshot's history holds the op, for the Admin that finishes it.
    assert history_op(env)["outcome"] == "running"


def test_a_previous_admin_that_wont_start_puts_the_current_one_back(admin_env):
    env = admin_env
    updated(env)
    handed(env)
    env.fake.clear()
    ups = []

    def up(args):
        ups.append(args)
        return (1, "", "port is already allocated") if len(ups) == 1 else env.fake.docker(args)

    env.fake.rules.append((lambda a: a[:4] == ["docker", "compose", "-p", "openfit-admin"], up))

    assert revert(env) == "rolled_back"
    assert env.fake.calls == [RETAG, acompose(env, *UP), NEXT_RETAG, acompose(env, *UP), ADMIN_PS,
                              health(admin_cid(2))]


def test_a_fallback_that_doesnt_answer_either_is_failed_with_the_manual_command(admin_env):
    env = admin_env
    updated(env)
    handed(env)
    env.fake.unhealthy = {admin_cid(2), admin_cid(3)}

    assert revert(env) == "failed"

    r = result(env)
    assert r["outcome"] == "failed"
    last = r["lines"][-1]["text"]
    assert last.startswith("Where things stand: openfit-admin:local is the current Admin's image again, "
                           "its settings are put back")
    assert (f"cd {env.repos.repo} && docker tag openfit-admin:next openfit-admin:local && docker compose "
            "-f admin/docker-compose.yml -p openfit-admin up -d --no-build --force-recreate admin") in last
    assert admin_ops.admin_command(env.repos.repo) in last
    assert "The current Admin didn't answer either." in [line["text"] for line in r["lines"]]


def test_a_retag_that_fails_changes_nothing(admin_env):
    env = admin_env
    updated(env)
    handed(env)
    before = files(env.data)
    env.fake.fail(lambda a: a == RETAG.args, err="No such image: openfit-admin:prev")
    env.fake.clear()

    assert revert(env) == "failed"

    assert env.fake.commands() == [RETAG.args]
    assert files(env.data) == before
    assert result(env)["lines"][-1]["text"].startswith("Couldn't switch to the previous Admin's image, so nothing "
                                                       "was changed")


def test_the_helper_changes_nothing_without_both_snapshots(admin_env):
    env = admin_env
    updated(env)
    handed(env)
    for args in ((STAMP, "20991231T000000Z", REVERT_ID), ("20991231T000000Z", FRESH, REVERT_ID),
                 (STAMP, STAMP, REVERT_ID), ("../etc", FRESH, REVERT_ID), (STAMP, FRESH, OP_ID),
                 (STAMP, FRESH, "x; reboot")):
        env.fake.clear()
        assert revert(env, *args) == "failed"
        assert env.fake.calls == []


def test_a_helper_bug_is_a_failed_result_not_a_running_one(admin_env, monkeypatch):
    env = admin_env
    updated(env)
    handed(env)
    monkeypatch.setattr(admin_swap, "recreate", lambda runner, repo: 1 / 0)
    assert revert(env) == "failed"
    assert "unexpected error" in result(env)["lines"][-1]["text"]


def test_the_helpers_command_line_takes_both_forms(admin_env, monkeypatch):
    seen = []
    monkeypatch.setenv("OPENFIT_REPO", admin_env.repos.repo)
    monkeypatch.setattr(admin_swap, "swap", lambda *a, **k: seen.append(("swap", a[3:])))
    monkeypatch.setattr(admin_swap, "revert", lambda *a, **k: seen.append(("revert", a[3:])))
    admin_swap.main([STAMP, OP_ID])
    admin_swap.main(["revert", STAMP, FRESH, REVERT_ID])
    assert seen == [("swap", (STAMP, OP_ID)), ("revert", (STAMP, FRESH, REVERT_ID))]
    for argv in ([], [STAMP], ["revert", STAMP, FRESH], ["undo", STAMP, FRESH, REVERT_ID]):
        with pytest.raises(SystemExit):
            admin_swap.main(argv)


# --- 4., 5. finishing the op ----------------------------------------------------------------


@pytest.mark.parametrize("outcome, unhealthy", [("ok", ()), ("rolled_back", (2,)), ("failed", (2, 3))])
def test_the_admin_that_starts_next_finishes_the_op_from_the_result(admin_env, outcome, unhealthy):
    env = admin_env
    update = updated(env)
    op = handed(env)
    env.fake.unhealthy = {admin_cid(n) for n in unhealthy}
    assert revert(env) == outcome
    helper_lines = result(env)["lines"]
    env.fake.helper = False
    env.fake.clear()

    again = env.make()

    done = history_op(env)
    assert done["outcome"] == outcome
    assert done["summary"] == {
        "ok": f"The Admin went back to {update['old']['short']}.",
        "rolled_back": "The previous Admin didn't come up, so this version was put back, with its settings as they were.",
        "failed": "The revert didn't finish. The log says where things stand, and what to run on the server.",
    }[outcome]
    assert done["log"] == op["log"] + helper_lines + [done["log"][-1]]
    assert done["log"][-1]["text"] == done["summary"] and done["finished_at"] is not None
    assert not (env.data / "self_update_result.json").exists()
    assert not again.lock.locked()
    assert not any(c.args[0] == "docker" for c in env.fake.calls)


def test_startup_while_the_helper_is_still_at_work_waits_for_it_never_interrupted(admin_env):
    env = admin_env
    updated(env)
    handed(env)
    log = admin_swap.Log(str(env.data), REVERT_ID, FRESH, env.clock)
    log.say("Starting the previous Admin…")
    log.say("Checking the previous Admin is answering…")

    again = env.make()  # the previous Admin, starting while the helper checks it

    assert again.lock.locked() and history_op(env)["outcome"] == "running"
    log.say("The previous Admin is answering.")
    log.finish("ok")
    env.fake.helper = False
    done = again.current_op()
    assert done["id"] == REVERT_ID and done["outcome"] == "ok"
    assert [line["text"] for line in done["log"]][-2] == "The previous Admin is answering."
    assert not again.lock.locked()


def test_the_current_admin_finishes_it_if_the_helper_goes_without_reverting(admin_env):
    env = admin_env
    updated(env)
    handed(env)
    env.fake.helper = False  # gone, with nothing written

    op = env.admin.current_op()

    assert op["outcome"] == "failed"
    assert op["log"][-2]["text"] == "The helper stopped before it took the Admin back, so this is still the current version."
    assert not env.admin.lock.locked()


def older_admin_starts(env):
    """What an Admin from before S2 does as it starts, when compose brings
    it up: it can't read a revert's result, so it removes the file and
    marks the op interrupted."""
    def up(args):
        if "up" in args:
            (env.data / "self_update_result.json").unlink(missing_ok=True)
            ops = json.loads((env.data / "history.json").read_text())
            for o in ops:
                if o.get("outcome") == "running":
                    o.update(outcome="interrupted", summary="The Admin stopped while this was running.")
            (env.data / "history.json").write_text(json.dumps(ops))
        return env.fake.docker(args)

    env.fake.rules.append((lambda a: a[:4] == ["docker", "compose", "-p", "openfit-admin"], up))


def test_the_helper_finishes_the_record_for_an_admin_from_before_reverts(admin_env):
    env = admin_env
    update = updated(env)
    op = handed(env)
    older_admin_starts(env)

    assert revert(env) == "ok"

    done = history_op(env)
    assert done["outcome"] == "ok" and done["summary"] == f"The Admin went back to {update['old']['short']}."
    texts = [line["text"] for line in done["log"]]
    assert texts[:len(op["log"])] == [line["text"] for line in op["log"]]
    assert texts[-2:] == ["The previous Admin is answering.", done["summary"]]
    assert done["finished_at"] is not None
    assert not (env.data / "self_update_result.json").exists()


def test_an_admin_that_knows_reverts_is_left_to_finish_it(admin_env):
    env = admin_env
    updated(env)
    handed(env)

    assert revert(env) == "ok"

    assert history_op(env)["outcome"] == "running"  # the Admin's, once the helper has gone
    assert result(env)["outcome"] == "ok"


def test_after_a_revert_update_the_admin_is_offered_again_and_revert_isnt(admin_env):
    env = admin_env
    update = updated(env)
    handed(env)
    assert revert(env) == "ok"
    env.fake.helper = False
    env.clock.advance(60)
    env.admin = env.make()  # the previous Admin, finishing the revert

    status = env.admin.status()

    assert status["admin_revert"]["offered"] is False
    assert status["admin_revert"]["reason"] == "The Admin is already on the version from before that update."
    # Its code is older than the repo's admin/ (the test's running Admin is
    # the code as first cloned), so Update the Admin is on offer again.
    assert status["admin_update"]["can_self_update"] is True
    assert status["versions"]["admin"]["older"] is True
    assert status["versions"]["admin"]["last"]["undone_at"] is not None
    # The code it was built from is the update's old, again.
    assert env.admin._admin_base() == update["old"]["commit"]


def test_an_update_an_admin_from_before_s2_started_after_a_revert_gets_its_versions_again(admin_env):
    """The rollout: reverted to S1, whose Update the Admin notes as `old`
    the version its update went to - it doesn't know a revert put it back.
    The Admin that finishes the update notes it again."""
    env = admin_env
    first = updated(env)
    handed(env)
    assert revert(env) == "ok"
    env.fake.helper = False
    env.clock.advance(60)
    env.admin = env.make()  # the previous Admin, finishing the revert
    env.admin.start_admin_update()
    env.admin.wait(10)
    run = env.fake.commands()[-1]
    stamp, op_id = run[-2], run[-1]
    ops = json.loads((env.data / "history.json").read_text())
    for o in ops:
        if o["id"] == op_id:  # as S1 records it: no snapshot, and `old` the update before the revert's new
            del o["snapshot"]
            o["old"] = first["new"]
    (env.data / "history.json").write_text(json.dumps(ops))
    assert swap(env, stamp, op_id) == "ok"
    env.fake.helper = False

    again = env.make()

    done = history_op(env, op_id)
    assert done["outcome"] == "ok"
    assert done["old"]["commit"] == first["old"]["commit"]  # the version the revert went back to
    assert done["snapshot"] == stamp  # noted from the result, for a revert of this update
    assert again.revert_offer()["old"] == first["old"]["short"]


# --- the lane ----------------------------------------------------------------------------------


def all_the_reverts(env):
    """Every path of a revert: the Admin's steps, then the helper going
    back, putting the current one back, and failing."""
    updated(env)
    handed(env)
    assert revert(env) == "ok"
    env.fake.unhealthy = {admin_cid(3)}
    assert revert(env) == "rolled_back"
    env.fake.unhealthy = {admin_cid(n) for n in range(5, 8)}
    assert revert(env) == "failed"


def test_a_revert_tags_only_among_the_admins_own_three_tags(admin_env):
    env = admin_env
    all_the_reverts(env)
    tags = [(a[2], a[3]) for a in env.fake.commands() if a[:2] == ["docker", "tag"]]
    assert set(tags) == {(built_admin_image(1), "openfit-admin:next"), ("openfit-admin:prev", "openfit-admin:local"),
                         ("openfit-admin:next", "openfit-admin:local")}
    for call, frames in zip(env.fake.calls, env.fake.frames):
        if call.args[:2] == ["docker", "tag"]:
            assert "admin_tag" in frames, call


def test_the_revert_never_names_another_project_and_never_deletes(admin_env):
    env = admin_env
    all_the_reverts(env)
    for a in env.fake.commands():
        if a[0] == "git":
            continue
        assert a[1] in ("compose", "ps", "exec", "tag", "inspect", "image", "run"), a
        if a[1] == "compose":
            assert a[2:4] == ["-p", "openfit-admin"], a
        if a[1] == "run":
            assert a == revert_run(env).args, a  # its mounts, exactly
            continue
        for banned in ("down", "-v", "--volumes", "rm", "volume", "kill", "stop"):
            assert banned not in a, a


def test_admin_tag_takes_only_its_four_forms(admin_env):
    image = "sha256:" + "ab" * 32
    for source, target in ((image, "openfit-admin:local"), ("openfit-admin:local", "openfit-admin:prev"),
                           ("openfit-admin:local", "openfit-admin:next"), ("openfit-admin:prev", "openfit-admin:next"),
                           ("openfit-admin:next", "openfit-admin:prev"), ("openfit-admin:prev", "openfit-admin:prev"),
                           ("openfit-tracker:latest", "openfit-admin:local"), (image, "openfit-tracker:latest"),
                           ("openfit-admin:prev", "openfit-tracker:latest"), ("sha256:abc", "openfit-admin:prev"),
                           (None, "openfit-admin:prev"), ("--force", "openfit-admin:local")):
        with pytest.raises(ValueError):
            admin_ops.admin_tag(admin_env.fake, source, target)
    assert admin_env.fake.calls == []
    for source, target in ((image, "openfit-admin:prev"), (image, "openfit-admin:next"),
                           ("openfit-admin:prev", "openfit-admin:local"), ("openfit-admin:next", "openfit-admin:local")):
        admin_ops.admin_tag(admin_env.fake, source, target)
    assert len(admin_env.fake.calls) == 4


def test_the_helpers_arguments_are_one_of_two_forms(admin_env):
    repo, vol = admin_env.repos.repo, "openfit-admin_admin-data"
    update = admin_ops.swap_helper_args(repo, vol, STAMP, OP_ID)
    assert update[update.index("python") - 1:] == ["openfit-admin:prev", "python", "/app/admin_swap.py", STAMP, OP_ID]
    rev = admin_ops.swap_helper_args(repo, vol, "revert", STAMP, FRESH, REVERT_ID)
    assert rev == revert_run(admin_env).args
    for bad in ((STAMP, REVERT_ID), ("revert", STAMP, FRESH, OP_ID), ("revert", STAMP, STAMP, REVERT_ID),
                ("revert", STAMP, FRESH), (STAMP,), ("../x", OP_ID), ("undo", STAMP, FRESH, REVERT_ID),
                ("revert", STAMP, FRESH, REVERT_ID, "--rm"), (STAMP, OP_ID, "sh"), (1, OP_ID)):
        with pytest.raises(ValueError):
            admin_ops.swap_helper_args(repo, vol, *bad)
