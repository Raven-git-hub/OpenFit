"""
The Admin's self-update helper: it swaps the Admin onto its new version,
and puts the old one back, code and settings, if the new one doesn't
answer. And its revert's: it takes the Admin back to the version before
its last update, and puts the current one back if that one doesn't.

An Admin can't replace itself: stopping its own container kills the
process doing the stopping. So the running Admin builds its new image,
then starts this in a short-lived container of its own (`docker run -d
--rm --name openfit-admin-swap`, labelled as the Admin's helper) from the
image it is running now, tagged openfit-admin:prev, never the new one. A
broken new version can only fail the health check; it never gets to run
the swap or the rollback. The container mounts the docker socket, the
repo at its own path and the Admin's volume at /data, and runs

    python /app/admin_swap.py <stamp> <op id>

<stamp> names the settings snapshot the Admin took,
/data/self-update/<stamp>/, and <op id> its operation in history.json.
The log goes to /data/self_update_result.json as it runs, rewritten after
each step, so a helper that stops part-way still leaves a trail; the
Admin running once the helper has gone finishes the op from it.

A revert (S2) is the same hand-over the other way round. The Admin keeps
the image it runs as openfit-admin:next and its settings as they are now
in a fresh snapshot, then starts this from the image it runs now -
openfit-admin:local, the one known to work, which holds this code - never
the one it goes back to, and runs

    python /app/admin_swap.py revert <update stamp> <fresh stamp> <op id>

which retags openfit-admin:prev as :local, puts back admin.json and
users.json from the update's snapshot (only those: the password, the
sessions' key, the activity log and the last check stay as they are now),
and recreates the Admin. If it doesn't answer, :next is retagged as
:local, all six files are put back from the fresh snapshot, and it's
recreated again.

It keeps to the Admin's own project: compose only through admin_compose()
(admin_ops.py), to recreate the Admin's container; docker only to find
that container by its labels, health-check it, and put :prev or :next back
as openfit-admin:local (admin_tag()). Never another project, and never
down, -v, rm or a volume.
"""

import os
import shutil
import sys
import time

import admin_ops
from admin_ops import ADMIN_HEALTH_CODE, ADMIN_IMAGE, ADMIN_NEXT, ADMIN_PREV, ADMIN_SERVICE, REVERT_FILES, SNAPSHOT_FILES

WAIT = 3  # seconds, so the Admin's reply to the button reaches the browser first


class Log:
    """The helper's result file, rewritten after every line."""

    def __init__(self, data_dir, op_id, stamp, clock):
        self.path = os.path.join(data_dir, admin_ops.SWAP_RESULT)
        self.clock = clock
        self.result = {"op_id": op_id, "stamp": stamp, "outcome": "running", "lines": []}

    def say(self, text, detail=""):
        line = f"{text}\n{detail}" if detail else text
        print(f"[admin-swap] {line}", flush=True)
        self.result["lines"].append({"t": admin_ops.iso(self.clock()), "text": line})
        self._write()

    def finish(self, outcome):
        self.result["outcome"] = outcome
        self._write()
        return outcome

    def _write(self):
        try:
            admin_ops._write_json(self.path, self.result)
        except OSError as e:
            print(f"[admin-swap] couldn't write {self.path}: {e}", flush=True)


def recreate(runner, repo):
    """The Admin's container recreated on the image openfit-admin:local names
    now. It isn't this container's project, so this carries on."""
    return admin_ops.admin_compose(runner, repo, "up", "-d", "--no-build", "--force-recreate", ADMIN_SERVICE)


def healthy(runner, clock, sleep):
    """The Admin's container, found by its labels, answering on 8090."""
    return admin_ops.wait_healthy(runner, lambda: (admin_ops.find_admin(runner) or {}).get("id"),
                                  ADMIN_HEALTH_CODE, clock, sleep)


def restore(data_dir, stamp, names=SNAPSHOT_FILES):
    """Each of `names` (all six, unless given) the snapshot has copied back
    over /data's, and each it doesn't have removed from /data. The
    problems, if any."""
    folder = admin_ops.snapshot_dir(data_dir, stamp)
    problems = []
    for name in names:
        kept, live = os.path.join(folder, name), os.path.join(data_dir, name)
        try:
            if os.path.isfile(kept):
                tmp = f"{live}.restore"
                shutil.copy2(kept, tmp)
                os.replace(tmp, live)
            elif os.path.lexists(live):
                os.remove(live)
        except OSError as e:
            problems.append(f"{name}: {e}")
    return problems


def swap(runner, repo, data_dir, stamp, op_id, clock=time.time, sleep=time.sleep):
    """Swap in the new Admin; roll back if it doesn't answer. Its outcome:
    ok, rolled_back or failed, also the result file's."""
    log = Log(data_dir, op_id, stamp, clock)
    try:
        return _swap(log, runner, repo, data_dir, stamp, op_id, clock, sleep)
    except Exception as e:  # a bug, not a failed step: say so, never leave it "running"
        log.say(f"The helper stopped on an unexpected error: {e}")
        return log.finish("failed")


def _swap(log, runner, repo, data_dir, stamp, op_id, clock, sleep):
    if not (admin_ops._FILE_STAMP.match(stamp) and admin_ops.UPDATE_OP_ID.match(op_id)
            and os.path.isdir(admin_ops.snapshot_dir(data_dir, stamp))):
        log.say("The helper can't find the Admin's settings snapshot, so it changed nothing.")
        return log.finish("failed")

    log.say("The helper is here. Waiting a moment, so the Admin's reply reaches your browser…")
    sleep(WAIT)
    log.say("Swapping in the new Admin…")
    rc, out, err = recreate(runner, repo)
    if rc != 0:
        log.say("The new Admin didn't start.", admin_ops._tail(out, err))
    else:
        log.say("Checking the new Admin is answering…")
        if healthy(runner, clock, sleep):
            log.say("The new Admin is answering.")
            return log.finish("ok")
        log.say("The new Admin didn't answer.")

    log.say("Putting the old Admin back…")
    rc, out, err = admin_ops.admin_tag(runner, ADMIN_PREV, ADMIN_IMAGE)
    retagged = rc == 0
    if not retagged:
        log.say("Couldn't switch back to the old Admin's image.", admin_ops._tail(out, err))
    problems = restore(data_dir, stamp)
    if problems:
        log.say("Couldn't put all of its settings back.", "\n".join(problems))
    else:
        log.say("Its settings are put back as they were before the update.")
    rc, out, err = recreate(runner, repo)
    if rc != 0:
        log.say("The old Admin didn't start.", admin_ops._tail(out, err))
    else:
        log.say("Checking the old Admin is answering…")
        if healthy(runner, clock, sleep):
            log.say("The old Admin is answering again.")
            return log.finish("rolled_back")
        log.say("The old Admin didn't answer either.")

    log.say(
        "Where things stand: "
        + (f"{ADMIN_IMAGE} is the old Admin's image again" if retagged
           else f"{ADMIN_IMAGE} is still the new Admin's image (the old one is {ADMIN_PREV})")
        + ", "
        + ("its settings are put back" if not problems
           else f"not all of its settings are put back (the copy is self-update/{stamp}/ in the Admin's storage)")
        + ", and no Admin is answering on port 8090. On the server, `docker logs openfit-admin-admin-1` says why. "
        + f"To start the old Admin by hand: cd {repo} && docker tag {ADMIN_PREV} {ADMIN_IMAGE} && docker compose "
        + f"-f admin/docker-compose.yml -p {admin_ops.ADMIN_PROJECT} up -d --no-build --force-recreate {ADMIN_SERVICE}. "
        + f"Or to build and start the version in the repo: {admin_ops.admin_command(repo)}"
    )
    return log.finish("failed")


def revert(runner, repo, data_dir, update_stamp, fresh_stamp, op_id, clock=time.time, sleep=time.sleep):
    """Take the Admin back to the version before its last update; put the
    current one back if that one doesn't answer. Its outcome: ok,
    rolled_back or failed, also the result file's."""
    log = Log(data_dir, op_id, fresh_stamp, clock)
    try:
        return _revert(log, runner, repo, data_dir, update_stamp, fresh_stamp, op_id, clock, sleep)
    except Exception as e:  # a bug, not a failed step: say so, never leave it "running"
        log.say(f"The helper stopped on an unexpected error: {e}")
        return log.finish("failed")


def _revert(log, runner, repo, data_dir, update_stamp, fresh_stamp, op_id, clock, sleep):
    stamp = admin_ops._FILE_STAMP
    if not (stamp.match(update_stamp) and stamp.match(fresh_stamp) and update_stamp != fresh_stamp
            and admin_ops.REVERT_OP_ID.match(op_id)
            and os.path.isdir(admin_ops.snapshot_dir(data_dir, update_stamp))
            and os.path.isdir(admin_ops.snapshot_dir(data_dir, fresh_stamp))):
        log.say("The helper can't find the Admin's settings snapshots, so it changed nothing.")
        return log.finish("failed")

    log.say("The helper is here. Waiting a moment, so the Admin's reply reaches your browser…")
    sleep(WAIT)
    log.say("Switching to the previous Admin's image…")
    rc, out, err = admin_ops.admin_tag(runner, ADMIN_PREV, ADMIN_IMAGE)
    if rc != 0:
        log.say("Couldn't switch to the previous Admin's image, so nothing was changed: the Admin is still on its "
                "current version.", admin_ops._tail(out, err))
        return log.finish("failed")
    problems = restore(data_dir, update_stamp, REVERT_FILES)
    if problems:
        log.say("Couldn't put the Admin's settings back as they were at that update.", "\n".join(problems))
    else:
        log.say("Its settings are put back as they were at that update. Its password, sessions and activity log "
                "are kept as they are.")
        log.say("Starting the previous Admin…")
        rc, out, err = recreate(runner, repo)
        if rc != 0:
            log.say("The previous Admin didn't start.", admin_ops._tail(out, err))
        else:
            log.say("Checking the previous Admin is answering…")
            if healthy(runner, clock, sleep):
                log.say("The previous Admin is answering.")
                log.finish("ok")
                finish_for_an_older_admin(data_dir, log.result, clock)
                return "ok"
            log.say("The previous Admin didn't answer.")

    log.say("Putting the current Admin back…")
    rc, out, err = admin_ops.admin_tag(runner, ADMIN_NEXT, ADMIN_IMAGE)
    retagged = rc == 0
    if not retagged:
        log.say("Couldn't switch back to the current Admin's image.", admin_ops._tail(out, err))
    problems = restore(data_dir, fresh_stamp)
    if problems:
        log.say("Couldn't put all of its settings back.", "\n".join(problems))
    else:
        log.say("Its settings are put back as they were before the revert.")
    rc, out, err = recreate(runner, repo)
    if rc != 0:
        log.say("The current Admin didn't start.", admin_ops._tail(out, err))
    else:
        log.say("Checking the current Admin is answering…")
        if healthy(runner, clock, sleep):
            log.say("The current Admin is answering again.")
            return log.finish("rolled_back")
        log.say("The current Admin didn't answer either.")

    log.say(
        "Where things stand: "
        + (f"{ADMIN_IMAGE} is the current Admin's image again" if retagged
           else f"{ADMIN_IMAGE} is still the previous Admin's image (the current one is {ADMIN_NEXT})")
        + ", "
        + ("its settings are put back" if not problems
           else f"not all of its settings are put back (the copy is self-update/{fresh_stamp}/ in the Admin's storage)")
        + ", and no Admin is answering on port 8090. On the server, `docker logs openfit-admin-admin-1` says why. "
        + f"To start the current Admin by hand: cd {repo} && docker tag {ADMIN_NEXT} {ADMIN_IMAGE} && docker compose "
        + f"-f admin/docker-compose.yml -p {admin_ops.ADMIN_PROJECT} up -d --no-build --force-recreate {ADMIN_SERVICE}. "
        + f"Or to build and start the version in the repo: {admin_ops.admin_command(repo)}"
    )
    return log.finish("failed")


def finish_for_an_older_admin(data_dir, result, clock):
    """A revert that worked, finished here if the Admin it went back to is
    from before reverts (S1): that Admin can't read a revert's result - at
    startup it removes the result file and marks the op interrupted, before
    it answers. So once it answers, an op so marked is finished here, as
    that Admin would have: the helper's lines on its log, its outcome and
    summary, and the result file removed. An op still running is the
    Admin's to finish: it read the result, and waits for this helper to go.
    Whether it finished the op."""
    path = os.path.join(data_dir, "history.json")
    ops = admin_ops._read_json(path, None)
    if not isinstance(ops, list):
        return False
    op = next((o for o in ops if isinstance(o, dict) and o.get("id") == result["op_id"]), None)
    if op is None or op.get("kind") != "admin_revert" or op.get("outcome") != "interrupted":
        return False
    summary = admin_ops.revert_summary(op, "ok")
    now = admin_ops.iso(clock())
    log = op["log"] if isinstance(op.get("log"), list) else []
    log.extend(result["lines"])
    log.append({"t": now, "text": summary})
    op.update(log=log, outcome="ok", summary=summary, finished_at=now)
    try:
        admin_ops._write_json(path, ops)
        os.remove(os.path.join(data_dir, admin_ops.SWAP_RESULT))
    except OSError as e:
        print(f"[admin-swap] couldn't finish the revert's record: {e}", flush=True)
        return False
    print("[admin-swap] The Admin went back to a version from before reverts, so its record was finished here.",
          flush=True)
    return True


USAGE = ("usage: OPENFIT_REPO=<repo> python admin_swap.py <stamp> <op id>\n"
         "       OPENFIT_REPO=<repo> python admin_swap.py revert <update stamp> <fresh stamp> <op id>")


def main(argv):
    repo = os.environ.get("OPENFIT_REPO", "").rstrip("/")
    data_dir = os.environ.get("ADMIN_DATA", "/data")
    if repo and len(argv) == 2:
        swap(admin_ops.Runner(repo), repo, data_dir, argv[0], argv[1])
    elif repo and len(argv) == 4 and argv[0] == "revert":
        revert(admin_ops.Runner(repo), repo, data_dir, argv[1], argv[2], argv[3])
    else:
        sys.exit(USAGE)


if __name__ == "__main__":
    main(sys.argv[1:])
