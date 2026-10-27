"""
The Admin's self-update helper: it swaps the Admin onto its new version,
and puts the old one back, code and settings, if the new one doesn't
answer.

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

It keeps to the Admin's own project: compose only through admin_compose()
(admin_ops.py), to recreate the Admin's container; docker only to find
that container by its labels, health-check it, and retag the old image as
openfit-admin:local. Never another project, and never down, -v, rm or a
volume.
"""

import os
import shutil
import sys
import time

import admin_ops
from admin_ops import ADMIN_HEALTH_CODE, ADMIN_IMAGE, ADMIN_PREV, ADMIN_SERVICE, SNAPSHOT_FILES

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


def restore(data_dir, stamp):
    """Each file in the snapshot copied back over /data's, and each of the
    six names it doesn't have removed from /data. The problems, if any."""
    folder = admin_ops.snapshot_dir(data_dir, stamp)
    problems = []
    for name in SNAPSHOT_FILES:
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
    if not (admin_ops._FILE_STAMP.match(stamp) and admin_ops.SWAP_OP_ID.match(op_id)
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
    rc, out, err = runner.run(["docker", "tag", ADMIN_PREV, ADMIN_IMAGE])
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


def main(argv):
    repo = os.environ.get("OPENFIT_REPO", "").rstrip("/")
    if len(argv) != 2 or not repo:
        sys.exit("usage: OPENFIT_REPO=<repo> python admin_swap.py <stamp> <op id>")
    data_dir = os.environ.get("ADMIN_DATA", "/data")
    swap(admin_ops.Runner(repo), repo, data_dir, argv[0], argv[1])


if __name__ == "__main__":
    main(sys.argv[1:])
