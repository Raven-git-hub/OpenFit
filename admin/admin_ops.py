"""
The OpenFit Admin's operations: checking for updates, updating the user
system, and undoing an update.

Every command goes through one Runner, as a list of arguments - never a
shell string - and nothing a person types is ever placed in one: the
arguments are fixed strings, the settings in /data/admin.json (checked
against strict patterns when they're read), and ids that git and docker
gave back themselves (checked before they're used).

The Admin holds the docker socket, so it has full control of the host.
It keeps to two things there: the one container compose labels as the
user system's (project and service from the settings), and helper
containers it starts itself, labelled openfit.admin.helper=true and
always --rm. Compose commands are always scoped to the user system's
project and compose file. Nothing else on the server is listed,
inspected, stopped or removed.

Git runs as the repo's owner (the uid and gid that own OPENFIT_REPO), so
the files it writes stay the owner's and git never refuses the repo as
someone else's. Docker runs as root, for the socket.
"""

import hashlib
import json
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone

DEFAULT_SETTINGS = {
    "user": "andy",
    "project": "openfit",
    "service": "tracker",
    "port": 8080,
    "branch": "main",
}

# Strict, so a hand-edited admin.json can never put anything odd in a
# command: names as compose writes them, a branch as git names one.
_NAME = re.compile(r"[a-z0-9][a-z0-9_-]*\Z")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*\Z")
_USER = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._-]{0,39}\Z")

# What docker and git hand back, checked before it goes into a command.
_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}\Z")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_VOLUME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
BACKUP_NAME = re.compile(r"(\d{8}T\d{6}Z)-([0-9a-f]{4,40})\.tar\.gz\Z")

PR_TITLE = re.compile(r"Merge pull request #(\d+)\b")
PR_URL = "https://github.com/Raven-git-hub/OpenFit/pull/{}"

# The four groups a change is reported in, by top-level folder.
GROUPS = (
    ("app", "User system", "app/"),
    ("admin", "Admin", "admin/"),
    ("library", "App library", "library/"),
    ("other", "Other", None),
)

HELPER_LABEL = "openfit.admin.helper=true"
ADMIN_PROJECT = "openfit-admin"
ADMIN_IMAGE = "openfit-admin:local"
ADMIN_VOLUME = "openfit-admin_admin-data"

CHECK_EVERY = 7 * 24 * 3600  # an automatic check, a week after the last
TICK_EVERY = 3600  # how often the background thread looks
HEALTH_EVERY = 2
HEALTH_FOR = 60
KEEP_BACKUPS = 3
KEEP_HISTORY = 20

HEALTH_CODE = (
    "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen("
    "'http://127.0.0.1:80/', timeout=5).status == 200 else 1)"
)


class Runner:
    """Runs one command, an argument list, and never through a shell.

    run(args, cwd=None, as_owner=False) -> (returncode, stdout, stderr).
    as_owner runs it as the uid and gid that own `owner_path` (the repo),
    with HOME=/tmp and no supplementary groups. A command that can't start
    is 127, one that runs too long 124: never an exception.
    """

    def __init__(self, owner_path, timeout=1800):
        self.owner_path = owner_path
        self.timeout = timeout

    def owner(self):
        st = os.stat(self.owner_path)
        return st.st_uid, st.st_gid

    def run(self, args, cwd=None, as_owner=False, timeout=None):
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise TypeError("a command is a list of strings")
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        drop = {}
        if as_owner:
            env["HOME"] = "/tmp"
            if os.geteuid() == 0:
                uid, gid = self.owner()
                # Popen sets the groups, then the gid, then the uid, in the
                # child before exec - preexec_fn's job, without Python code
                # running in a forked child of a threaded server.
                drop = {"user": uid, "group": gid, "extra_groups": []}
        try:
            done = subprocess.run(
                args,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout or self.timeout,
                **drop,
            )
        except subprocess.TimeoutExpired as e:
            return 124, _text(e.stdout), f"{args[0]} timed out after {e.timeout:g}s"
        except OSError as e:
            return 127, "", str(e)
        return done.returncode, done.stdout, done.stderr


def _text(out):
    if out is None:
        return ""
    return out.decode("utf-8", "replace") if isinstance(out, bytes) else out


class OpError(Exception):
    """A step that failed: a plain-words message, and the output behind it."""

    def __init__(self, message, detail=""):
        super().__init__(message)
        self.message = message
        self.detail = detail


class Refused(Exception):
    """A request turned down before anything was changed."""

    def __init__(self, message, status=409):
        super().__init__(message)
        self.message = message
        self.status = status


class Busy(Refused):
    def __init__(self):
        super().__init__("Something else is running. Wait for it to finish, then try again.")


def _tail(out, err, lines=12):
    text = "\n".join(t.strip() for t in (err, out) if t and t.strip())
    return "\n".join(text.splitlines()[-lines:])


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _write_json(path, data):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_settings(data_dir):
    """/data/admin.json, written with the defaults on first start."""
    path = os.path.join(data_dir, "admin.json")
    if not os.path.exists(path):
        _write_json(path, DEFAULT_SETTINGS)
    raw = _read_json(path, None)
    if not isinstance(raw, dict):
        raise ValueError(f"{path} isn't a JSON object")
    s = dict(DEFAULT_SETTINGS, **raw)
    for key, rule in (("project", _NAME), ("service", _NAME), ("branch", _BRANCH), ("user", _USER)):
        if not isinstance(s[key], str) or not rule.match(s[key]) or ".." in s[key]:
            raise ValueError(f"{path}: {key} isn't valid: {s[key]!r}")
    if s["project"] == ADMIN_PROJECT:
        raise ValueError(f"{path}: the project can't be the Admin's own")
    if type(s["port"]) is not int or not 1 <= s["port"] <= 65535:
        raise ValueError(f"{path}: port isn't valid: {s['port']!r}")
    return s


def group_of(path):
    """The group key a changed file is reported in, by its top folder."""
    for key, _title, folder in GROUPS:
        if folder and path.startswith(folder):
            return key
    return "other"


def update_offered(check):
    """Whether the last check shows something for Update OpenFit to pull."""
    if not check or "behind" not in check:
        return False
    app = next((g for g in check.get("groups", []) if g["key"] == "app"), None)
    return check["behind"] > 0 or bool(app and (app["commits"] or app["files"]))


def fingerprint(folder):
    """A hash of a folder's files (dotfiles and caches left out)."""
    h = hashlib.sha256()
    if not os.path.isdir(folder):
        return None
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__pycache__")
        for name in sorted(files):
            if name.startswith(".") or name.endswith(".pyc"):
                continue
            path = os.path.join(root, name)
            h.update(os.path.relpath(path, folder).encode() + b"\0")
            try:
                with open(path, "rb") as f:
                    h.update(hashlib.sha256(f.read()).digest())
            except OSError:
                h.update(b"?")
    return h.hexdigest()


class Admin:
    """The one OpenFit the Admin manages, and the operations on it.

    One operation at a time (`lock`): an update, an undo or a check. The
    update and the undo run in a background thread, their progress in
    `op`; a check runs in the caller's thread.
    """

    def __init__(
        self,
        repo,
        data_dir,
        runner=None,
        clock=time.time,
        sleep=time.sleep,
        self_dir=None,
        admin_volume=ADMIN_VOLUME,
        admin_image=ADMIN_IMAGE,
    ):
        self.repo = repo
        self.data_dir = data_dir
        os.makedirs(data_dir, exist_ok=True)
        self.settings = load_settings(data_dir)
        self.runner = runner or Runner(repo)
        self.clock = clock
        self.sleep = sleep
        self.self_dir = self_dir or os.path.dirname(os.path.abspath(__file__))
        self.admin_volume = admin_volume
        self.admin_image = admin_image
        self.compose_file = os.path.join(repo, "docker-compose.yml")
        self.image = f"{self.settings['project']}-{self.settings['service']}"
        self.backups_dir = os.path.join(data_dir, "backups")
        self.check_path = os.path.join(data_dir, "last_check.json")
        self.history_path = os.path.join(data_dir, "history.json")
        self.lock = threading.Lock()
        self._op_lock = threading.Lock()
        self.op = None
        self.thread = None
        os.makedirs(self.backups_dir, exist_ok=True)
        self._mark_interrupted()

    # --- commands -------------------------------------------------------

    def git(self, *args, timeout=None):
        return self.runner.run(["git", *args], cwd=self.repo, as_owner=True, timeout=timeout)

    def compose(self, *args):
        """docker compose, always scoped to the user system's project and file."""
        return self.runner.run(
            ["docker", "compose", "-p", self.settings["project"], "-f", self.compose_file, *args],
            cwd=self.repo,
        )

    def docker(self, *args, timeout=None):
        return self.runner.run(["docker", *args], timeout=timeout)

    def _git_out(self, *args, what):
        rc, out, err = self.git(*args)
        if rc != 0:
            raise OpError(f"Couldn't {what}.", _tail(out, err))
        return out

    # --- the repo -------------------------------------------------------

    def head(self):
        out = self._git_out("log", "-1", "--format=%H%x09%h%x09%s", "HEAD", what="read the current version")
        commit, short, subject = (out.strip().split("\t", 2) + ["", ""])[:3]
        if not _COMMIT.match(commit):
            raise OpError("Couldn't read the current version.", out)
        return {"commit": commit, "short": short, "subject": subject}

    def dirty_files(self):
        rc, out, err = self.git("status", "--porcelain")
        if rc != 0:
            raise Refused("Couldn't read the state of the server's copy of OpenFit. Nothing was changed.", 500)
        return [line[3:] for line in out.splitlines() if line.strip()]

    # --- the user system's container -------------------------------------

    def find_container(self):
        """The user system's container, by its compose labels, or None."""
        s = self.settings
        rc, out, err = self.docker(
            "ps", "-a", "--no-trunc",
            "--filter", f"label=com.docker.compose.project={s['project']}",
            "--filter", f"label=com.docker.compose.service={s['service']}",
            "--filter", "label=com.docker.compose.oneoff=False",
            "--format", "{{.ID}}\t{{.State}}",
        )
        if rc != 0:
            raise OpError("Couldn't ask Docker about OpenFit's container.", _tail(out, err))
        rows = [line.split("\t") for line in out.splitlines() if line.strip()]
        if not rows:
            return None
        if len(rows) > 1:
            raise OpError("There's more than one OpenFit container, so the Admin won't guess which.")
        cid = rows[0][0]
        if not _CONTAINER_ID.match(cid):
            raise OpError("Docker gave back a container id the Admin doesn't recognise.", cid)
        return {"id": cid, "state": rows[0][1] if len(rows[0]) > 1 else ""}

    def inspect(self, cid):
        rc, out, err = self.docker("inspect", "--type", "container", cid)
        if rc != 0:
            raise OpError("Couldn't inspect OpenFit's container.", _tail(out, err))
        try:
            info = json.loads(out)[0]
        except (ValueError, IndexError, KeyError, TypeError):
            raise OpError("Couldn't read what Docker said about OpenFit's container.", out[-400:])
        return info

    def data_volume(self, info):
        """The volume mounted at /data in the container `info` describes."""
        for m in info.get("Mounts") or []:
            if m.get("Destination") == "/data":
                name = m.get("Name") or ""
                if m.get("Type") == "volume" and _VOLUME.match(name):
                    return name
                raise OpError("OpenFit's /data isn't a Docker volume, so the Admin can't back it up.")
        raise OpError("OpenFit's container has no /data storage to back up.")

    def _container_or_fail(self):
        c = self.find_container()
        if c is None:
            raise OpError("OpenFit's container wasn't found. Start it once from the command line first.")
        return c

    def healthy(self):
        """Poll the user system until it answers, for up to HEALTH_FOR seconds."""
        deadline = self.clock() + HEALTH_FOR
        cid = None
        while True:
            if cid is None:
                try:
                    c = self.find_container()
                except OpError:
                    c = None
                cid = c and c["id"]
            if cid:
                rc, _out, _err = self.docker("exec", cid, "python", "-c", HEALTH_CODE, timeout=30)
                if rc == 0:
                    return True
            if self.clock() >= deadline:
                return False
            self.sleep(HEALTH_EVERY)

    # --- checking for updates --------------------------------------------

    def last_check(self):
        check = _read_json(self.check_path, None)
        return check if isinstance(check, dict) else None

    def check(self):
        """Fetch, then report what's new, by group. Never touches HEAD or the tree."""
        if not self.lock.acquire(blocking=False):
            raise Busy()
        try:
            return self._check()
        finally:
            self.lock.release()

    def _check(self):
        branch = self.settings["branch"]
        now = self._iso()
        rc, out, err = self.git("fetch", "origin", branch, timeout=300)
        try:
            if rc != 0:
                raise OpError("Couldn't reach GitHub to check for updates.", _tail(out, err))
            result = self._compare()
        except OpError as e:
            last = self.last_check() or {}
            last["error"] = {"at": now, "text": e.message, "detail": e.detail}
            _write_json(self.check_path, last)
            raise
        result["checked_at"] = now
        result["error"] = None
        _write_json(self.check_path, result)
        return result

    def refresh_check(self):
        """Re-compare after HEAD moved, without fetching (checked_at kept)."""
        last = self.last_check()
        if not last or not last.get("checked_at"):
            return
        try:
            result = self._compare()
        except OpError:
            return
        result["checked_at"] = last["checked_at"]
        result["error"] = last.get("error")
        _write_json(self.check_path, result)

    def _log(self, *args):
        commits = []
        for line in self._git_out("log", *args, what="list the new commits").splitlines():
            sha, _, subject = line.partition("\t")
            if sha:
                commits.append({"sha": sha, "subject": subject})
        return commits

    def _compare(self):
        upstream = f"origin/{self.settings['branch']}"
        span = f"HEAD..{upstream}"
        head = self.head()
        behind = self._git_out("rev-list", "--count", span, what="count the new commits").strip()
        if not behind.isdigit():
            raise OpError("Couldn't count the new commits.", behind)
        files = self._git_out("diff", "--name-only", "HEAD", upstream, what="list the changed files").split()
        groups = {key: {"key": key, "title": title, "files": [], "commits": [], "prs": []}
                  for key, title, _folder in GROUPS}
        for path in files:
            groups[group_of(path)]["files"].append(path)
        if int(behind) or files:
            listed = set()
            for key, _title, folder in GROUPS:
                if folder:
                    groups[key]["commits"] = self._log("--no-merges", "--format=%h%x09%s", span, "--", folder)
                    listed.update(c["sha"] for c in groups[key]["commits"])
            everything = self._log("--no-merges", "--format=%h%x09%s", span)
            groups["other"]["commits"] = [c for c in everything if c["sha"] not in listed]
            for merge in self._log("--merges", "--format=%H%x09%s", span):
                number = PR_TITLE.match(merge["subject"])
                if not number or not _COMMIT.match(merge["sha"]):
                    continue
                brought = self._git_out(
                    "diff", "--name-only", f"{merge['sha']}^1", merge["sha"], what="read a pull request's files"
                ).split()
                pr = {"number": int(number.group(1)), "url": PR_URL.format(number.group(1))}
                for key in sorted({group_of(p) for p in brought}, key=[g[0] for g in GROUPS].index):
                    groups[key]["prs"].append(pr)
        return {
            "current": {"sha": head["short"], "subject": head["subject"]},
            "behind": int(behind),
            "groups": [groups[key] for key, _title, _folder in GROUPS],
        }

    def check_due(self):
        last = self.last_check() or {}
        at = last.get("checked_at")
        if not at:
            return True
        try:
            then = datetime.strptime(at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        except (TypeError, ValueError):
            return True
        return self.clock() - then >= CHECK_EVERY

    def tick(self):
        """The background thread's hourly look: a check if one is due. Only ever a check."""
        if not self.check_due():
            return False
        try:
            result = self.check()
            print(f"[admin] automatic check: {result['behind']} new commit(s)", flush=True)
        except Busy:
            print("[admin] automatic check: something else is running, trying again later", flush=True)
        except OpError as e:
            print(f"[admin] automatic check failed: {e.message} {e.detail}", flush=True)
        return True

    def start_auto_checks(self):
        stop = threading.Event()

        def loop():
            self.tick()
            while not stop.wait(TICK_EVERY):
                self.tick()

        threading.Thread(target=loop, name="admin-auto-check", daemon=True).start()
        return stop

    def admin_update(self, check):
        """The Admin's own update, which A1 leaves to the command line.

        `commits` are new on GitHub and not pulled yet; `pulled` means the
        repo's admin/ already differs from the code this Admin is running
        (an Update OpenFit pulled it), so a rebuild is all it takes.
        """
        group = next((g for g in (check or {}).get("groups", []) if g["key"] == "admin"), None)
        commits = group["commits"] if group else []
        mine = fingerprint(self.self_dir)
        pulled = mine is not None and fingerprint(os.path.join(self.repo, "admin")) not in (None, mine)
        return {
            "available": bool(commits or (group and group["files"])) or pulled,
            "pulled": pulled,
            "commits": commits,
            "command": f"cd {self.repo} && docker compose -f admin/docker-compose.yml -p {ADMIN_PROJECT} up -d --build",
        }

    # --- operations and their record ---------------------------------------

    def _iso(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _stamp(self):
        return datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def history(self):
        ops = _read_json(self.history_path, [])
        return ops if isinstance(ops, list) else []

    def _save_op(self, op):
        with self._op_lock:
            snap = json.loads(json.dumps(op))
        ops = [o for o in self.history() if o.get("id") != snap["id"]] + [snap]
        _write_json(self.history_path, ops[-KEEP_HISTORY:])

    def _mark_interrupted(self):
        ops = self.history()
        changed = False
        for o in ops:
            if o.get("outcome") == "running":
                o["outcome"] = "interrupted"
                o["summary"] = "The Admin stopped while this was running."
                changed = True
        if changed:
            _write_json(self.history_path, ops)

    def current_op(self):
        """The running operation, else the last one, else None."""
        with self._op_lock:
            if self.op is not None:
                return json.loads(json.dumps(self.op))
        ops = self.history()
        return ops[-1] if ops else None

    def say(self, op, text, detail=""):
        line = f"{text}\n{detail}" if detail else text
        print(f"[admin] {op['kind']}: {line}", flush=True)
        with self._op_lock:
            op["log"].append({"t": self._iso(), "text": line})

    def _begin(self, kind):
        op = {
            "id": f"{self._stamp()}-{kind}",
            "kind": kind,
            "started_at": self._iso(),
            "finished_at": None,
            "outcome": "running",
            "summary": "",
            "log": [],
            "old": None,
            "new": None,
            "backup": None,
            "undoes": None,
        }
        with self._op_lock:
            self.op = op
        self._save_op(op)
        return op

    def _finish(self, op, outcome, summary):
        with self._op_lock:
            op["outcome"] = outcome
            op["summary"] = summary
            op["finished_at"] = self._iso()
        self.say(op, summary)
        self._save_op(op)

    def _spawn(self, op, step):
        def body():
            try:
                step(op)
            except Exception as e:  # a bug, not a failed step: say so, never leave it "running"
                self._finish(op, "failed", f"Stopped on an unexpected error: {e}")
            finally:
                with self._op_lock:
                    self.op = None
                self.lock.release()

        self.thread = threading.Thread(target=body, name=f"admin-{op['kind']}", daemon=True)
        self.thread.start()

    def wait(self, timeout=None):
        """Wait for the running operation's thread (for tests and the CLI)."""
        if self.thread is not None:
            self.thread.join(timeout)

    # --- backups ----------------------------------------------------------

    def list_backups(self):
        """The Admin's backups, newest first: [{name, size, created}]."""
        found = []
        try:
            names = os.listdir(self.backups_dir)
        except OSError:
            names = []
        for name in names:
            m = BACKUP_NAME.match(name)
            path = os.path.join(self.backups_dir, name)
            if not m or not os.path.isfile(path):
                continue
            when = datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%dT%H:%M:%SZ")
            found.append({"name": name, "size": os.path.getsize(path), "created": when})
        return sorted(found, key=lambda b: b["name"], reverse=True)

    def prune_backups(self):
        for b in self.list_backups()[KEEP_BACKUPS:]:
            os.remove(os.path.join(self.backups_dir, b["name"]))

    def _helper(self, *args):
        """A container of the Admin's own image, labelled as its helper and --rm."""
        return self.docker("run", "--rm", "--label", HELPER_LABEL, *args)

    # --- update -----------------------------------------------------------

    def start_update(self):
        """Step 1 here, so a refusal is a 409; the rest in the background."""
        if not self.lock.acquire(blocking=False):
            raise Busy()
        try:
            if not update_offered(self.last_check()):
                raise Refused("There's no update to apply. Check for updates first.")
            dirty = self.dirty_files()
            if dirty:
                raise Refused(f"The server's copy of OpenFit has local changes: {', '.join(dirty)}. Nothing was changed.")
            branch = self._git_out("rev-parse", "--abbrev-ref", "HEAD", what="read the branch").strip()
            if branch != self.settings["branch"]:
                raise Refused(
                    f"The server's copy of OpenFit is on '{branch}', not '{self.settings['branch']}'. Nothing was changed."
                )
            op = self._begin("update")
        except OpError as e:
            self.lock.release()
            raise Refused(e.message, 500)
        except BaseException:
            self.lock.release()
            raise
        self._spawn(op, self._update)
        return self.current_op()

    def _update(self, op):
        service = self.settings["service"]
        try:
            self.say(op, "Noting the version you're on now…")
            old = self.head()
            op["old"] = old
            container = self._container_or_fail()
            image_id = self.inspect(container["id"]).get("Image", "")
            if not _IMAGE_ID.match(image_id):
                raise OpError("Couldn't tell which image OpenFit is running.", image_id)
            rc, out, err = self.docker("tag", image_id, f"{self.image}:prev")
            if rc != 0:
                raise OpError("Couldn't keep a copy of the version you're on.", _tail(out, err))
        except OpError as e:
            self.say(op, e.message, e.detail)
            return self._finish(op, "failed", "Nothing was changed.")

        self.say(op, "Pulling the new code…")
        rc, out, err = self.git("merge", "--ff-only", f"origin/{self.settings['branch']}")
        if rc != 0:
            self.say(op, "The new code can't be added on top of the server's copy (not a fast-forward).", _tail(out, err))
            return self._finish(op, "failed", "Nothing was changed.")
        try:
            op["new"] = self.head()
        except OpError as e:
            self.say(op, e.message, e.detail)
            self._reset(op, old)
            return self._finish(op, "failed", "Nothing was changed.")

        self.say(op, "Building the new version. OpenFit keeps running meanwhile…")
        rc, out, err = self.compose("build", service)
        if rc != 0:
            self.say(op, "The new version didn't build.", _tail(out, err))
            self._reset(op, old)
            return self._finish(op, "failed", "The build failed, so nothing was changed. OpenFit kept running.")

        self.say(op, "Stopping OpenFit…")
        rc, out, err = self.compose("stop", service)
        if rc != 0:
            self.say(op, "OpenFit didn't stop.", _tail(out, err))
            self._back_out(op, old)
            return self._finish(op, "failed", "OpenFit didn't stop, so the update was called off. It's on the old version.")

        self.say(op, "Backing up your data…")
        name = f"{self._stamp()}-{old['short']}.tar.gz"
        try:
            volume = self.data_volume(self.inspect(container["id"]))
        except OpError as e:
            self.say(op, e.message, e.detail)
            self._back_out(op, old)
            return self._finish(op, "failed", "The data couldn't be backed up, so the update was called off. It's on the old version.")
        rc, out, err = self._helper(
            "-v", f"{volume}:/v:ro", "-v", f"{self.admin_volume}:/b", self.admin_image,
            "tar", "czf", f"/b/backups/{name}", "-C", "/v", ".",
        )
        if rc != 0:
            self.say(op, "The backup failed.", _tail(out, err))
            try:
                os.remove(os.path.join(self.backups_dir, name))
            except OSError:
                pass
            self._back_out(op, old)
            return self._finish(op, "failed", "The data couldn't be backed up, so the update was called off. It's on the old version.")
        op["backup"] = name

        self.say(op, "Starting the new version…")
        rc, out, err = self.compose("up", "-d", "--no-build", service)
        if rc != 0:
            self.say(op, "The new version didn't start.", _tail(out, err))

        self.say(op, "Checking OpenFit is answering…")
        if rc == 0 and self.healthy():
            self.prune_backups()
            self.refresh_check()
            return self._finish(op, "ok", f"Updated OpenFit to {op['new']['short']}: {op['new']['subject']}")

        self.say(op, "The new version didn't come up. Rolling back…")
        if self._undo_steps(op, old, name):
            self.refresh_check()
            return self._finish(op, "rolled_back", "Updated, didn't come up, rolled back.")
        return self._finish(op, "failed", "Updated, didn't come up, and the rollback didn't come up either. See the log above.")

    def _reset(self, op, old):
        self.say(op, "Putting the code back as it was…")
        rc, out, err = self.git("reset", "--hard", old["commit"])
        if rc != 0:
            self.say(op, "Couldn't put the code back.", _tail(out, err))

    def _back_out(self, op, old):
        """Called off after the build: old code, old image, OpenFit started on it."""
        self._reset(op, old)
        rc, out, err = self.docker("tag", f"{self.image}:prev", f"{self.image}:latest")
        if rc != 0:
            self.say(op, "Couldn't switch back to the previous image.", _tail(out, err))
        self.say(op, "Starting OpenFit again on the old version…")
        rc, out, err = self.compose("up", "-d", "--no-build", self.settings["service"])
        if rc != 0:
            self.say(op, "OpenFit didn't start again.", _tail(out, err))

    # --- undo -------------------------------------------------------------

    def undo_candidate(self, head=None):
        """The update Undo would reverse: the last update or undo, if it's a
        successful update whose backup is still here, and the repo is still
        on the version it updated to."""
        for o in reversed(self.history()):
            if o.get("kind") not in ("update", "undo"):
                continue
            if o["kind"] != "update" or o.get("outcome") != "ok":
                return None
            if not (o.get("old") and o.get("new") and o.get("backup")):
                return None
            if o["backup"] not in {b["name"] for b in self.list_backups()}:
                return None
            if head is not None and head.get("commit") != o["new"].get("commit"):
                return None
            return o
        return None

    def start_undo(self):
        if not self.lock.acquire(blocking=False):
            raise Busy()
        try:
            try:
                head = self.head()
            except OpError as e:
                raise Refused(e.message, 500)
            last = self.undo_candidate(head)
            if last is None:
                raise Refused("There's no update to undo.")
            op = self._begin("undo")
            with self._op_lock:
                op.update(old=last["old"], new=last["new"], backup=last["backup"], undoes=last["id"])
            self._save_op(op)
        except BaseException:
            self.lock.release()
            raise
        self._spawn(op, self._undo)
        return self.current_op()

    def _undo(self, op):
        if self._undo_steps(op, op["old"], op["backup"]):
            self.refresh_check()
            return self._finish(op, "ok", f"Back on {op['old']['short']}, with your data as it was before the update.")
        return self._finish(op, "failed", "The undo didn't finish. See the log above.")

    def _undo_steps(self, op, old, backup):
        """Stop, restore the backup, retag :prev as :latest, reset the code,
        recreate, health-check. True if OpenFit came back up."""
        service = self.settings["service"]
        if not BACKUP_NAME.match(backup or "") or backup not in {b["name"] for b in self.list_backups()}:
            self.say(op, f"The backup {backup!r} isn't in the Admin's backups, so nothing was put back.")
            return False
        self.say(op, "Stopping OpenFit…")
        rc, out, err = self.compose("stop", service)
        if rc != 0:
            self.say(op, "OpenFit didn't stop, so its data wasn't touched.", _tail(out, err))
            return False
        try:
            volume = self.data_volume(self.inspect(self._container_or_fail()["id"]))
        except OpError as e:
            self.say(op, e.message, e.detail)
            return False
        self.say(op, "Putting your data back as it was before the update…")
        rc, out, err = self._helper(
            "-v", f"{volume}:/v", "-v", f"{self.admin_volume}:/b:ro", self.admin_image,
            "sh", "-c", f"find /v -mindepth 1 -delete && tar xzf /b/backups/{backup} -C /v",
        )
        if rc != 0:
            self.say(
                op,
                f"Your data couldn't be put back, so OpenFit was left stopped. The backup is {backup}.",
                _tail(out, err),
            )
            return False
        self.say(op, "Switching back to the previous version…")
        rc, out, err = self.docker("tag", f"{self.image}:prev", f"{self.image}:latest")
        if rc != 0:
            self.say(op, "Couldn't switch back to the previous image.", _tail(out, err))
        self._reset(op, old)
        self.say(op, "Starting the previous version…")
        rc, out, err = self.compose("up", "-d", "--no-build", "--force-recreate", service)
        if rc != 0:
            self.say(op, "The previous version didn't start.", _tail(out, err))
        self.say(op, "Checking OpenFit is answering…")
        return rc == 0 and self.healthy()

    # --- the page's view ----------------------------------------------------

    def status(self):
        s = self.settings
        try:
            head = self.head()
        except OpError:
            head = None
        try:
            container = self.find_container()
            state = container["state"] if container else "missing"
        except OpError:
            state = "unknown"
        check = self.last_check()
        undo = self.undo_candidate(head) if head else None
        recent = [{k: o.get(k) for k in ("id", "kind", "outcome", "summary", "started_at", "finished_at")}
                  for o in reversed(self.history()[-5:])]
        return {
            "openfit": {
                "user": s["user"],
                "port": s["port"],
                "state": state,
                "running": state == "running",
                "version": {"sha": head["short"], "subject": head["subject"]} if head else None,
            },
            "check": check,
            "update_available": update_offered(check),
            "undo": {"id": undo["id"], "old": undo["old"], "new": undo["new"]} if undo else None,
            "admin_update": self.admin_update(check),
            "op": self.current_op(),
            "history": recent,
            "backups": self.list_backups(),
        }
