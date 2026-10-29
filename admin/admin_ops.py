"""
The OpenFit Admin's operations: checking for updates, adding and removing
users, and updating every user's OpenFit together (and undoing that).

Every command goes through one Runner, as a list of arguments - never a
shell string. What a person types reaches a command only as a user's
name or port, and only once it has passed the strict rules below; the
rest is fixed strings, the settings in /data/admin.json and the users in
/data/users.json (both checked against strict patterns on every read),
and ids that git and docker gave back themselves (checked before they're
used).

The Admin holds the docker socket, so it has full control of the host.
It keeps to its own users there. A user is the primary - A1's OpenFit,
compose project `openfit` from the repo's own docker-compose.yml - or one
the Admin added: project `openfit-<name>`, from a compose file the Admin
wrote itself into /data/users/<name>/. Compose commands are only ever one
of those projects with its own file. Docker commands only ever touch a
container compose labels as one of those projects' - where the only
commands it execs are the health check and the user system's own
`python auth_cli.py setup-link`, for a setup link - the Admin's own
helper containers (labelled openfit.admin.helper=true, always --rm), the
user system's image, and the kept volume of a user removed from
users.json, when its deletion is confirmed. Beside those, `docker info`,
with one fixed format, for the System panel: of all it says, only the
server's name, Docker version, CPUs and memory are kept. Nothing else on
the server is listed, inspected, stopped or removed.

The Admin's own project, openfit-admin, is touched only to update the
Admin itself, and only through admin_compose(): the running Admin builds
its new image, then hands the swap to a helper container (admin_swap.py),
which recreates the Admin on it and puts the old one back if it doesn't
answer.

The app shelf (A5) is the volume openfit-library, which the Admin writes
through its own mount at /library and every user's OpenFit mounts
read-only at /library: the official apps, the repo's library/, published
as an immutable set behind a symlink swapped in one step. Publishing is
plain file work under /library - no docker command, no helper - and the
shelf volume is never removed. The primary gets the mount from an
override file the Admin writes, /data/primary.override.yml, passed after
the repo's compose file; an added user from the template below.

Git runs as the repo's owner (the uid and gid that own OPENFIT_REPO), so
the files it writes stay the owner's and git never refuses the repo as
someone else's. Docker runs as root, for the socket.
"""

import contextlib
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import shutil
import tarfile
import threading
import time
from datetime import datetime, timezone
from typing import NamedTuple
from urllib.parse import urlsplit

import yaml

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

# A user's name, the primary's included: it goes into a compose project
# (openfit-<name>), a volume, a folder and a backup's name.
USER_NAME = re.compile(r"[a-z0-9]{1,20}\Z")
USER_VOLUME = re.compile(r"openfit-[a-z0-9]{1,20}_tracker-data\Z")
_STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")

# An Admin update's or revert's settings snapshot, /data/self-update/<stamp>/,
# and its op: the two the swap helper finishes.
_FILE_STAMP = re.compile(r"\d{8}T\d{6}Z\Z")
UPDATE_OP_ID = re.compile(r"\d{8}T\d{6}Z-admin_update\Z")
REVERT_OP_ID = re.compile(r"\d{8}T\d{6}Z-admin_revert\Z")
SWAP_OP_ID = re.compile(r"\d{8}T\d{6}Z-admin_(update|revert)\Z")
SWAP_KINDS = ("admin_update", "admin_revert")

# What docker and git hand back, checked before it goes into a command.
_CONTAINER_ID = re.compile(r"[0-9a-f]{12,64}\Z")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}\Z")
_VOLUME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_COMMIT = re.compile(r"[0-9a-f]{40}\Z")
# <stamp>-<old sha>[-<user>].tar.gz: one update's backups share a stamp,
# a set. An A1 backup, with no user, is the primary's.
BACKUP_NAME = re.compile(r"(\d{8}T\d{6}Z)-([0-9a-f]{4,40})(?:-([a-z0-9]{1,20}))?\.tar\.gz\Z")

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
ADMIN_PORT = 8090
ADMIN_SERVICE = "admin"

# The Admin's own update. Its current image is kept as :prev, which is also
# the image the helper runs; its settings files are copied into a snapshot
# the helper can put back; the helper writes its log to the result file.
# Its revert keeps the current image as :next, and its helper runs that
# one, :local, the image the Admin runs now.
ADMIN_PREV = "openfit-admin:prev"
ADMIN_NEXT = "openfit-admin:next"
SWAP_NAME = "openfit-admin-swap"
SWAP_RESULT = "self_update_result.json"
SWAP_OUTCOMES = ("ok", "rolled_back", "failed")
SWAP_SCRIPT = ("python", "/app/admin_swap.py")
SNAPSHOTS = "self-update"
SNAPSHOT_FILES = ("admin.json", "users.json", "auth.json", "secret_key", "history.json", "last_check.json")
# What a revert puts back from the update's snapshot: the settings and the
# users. The password, its sessions' key, the activity and the last check
# stay as they are now, so nobody is logged out and nothing is forgotten.
REVERT_FILES = ("admin.json", "users.json")
KEEP_SNAPSHOTS = 3
# The only compose commands ever run on the Admin's own project.
ADMIN_COMPOSE_ARGS = (
    ("build", ADMIN_SERVICE),
    ("up", "-d", "--no-build", "--force-recreate", ADMIN_SERVICE),
)
# What an Admin update's step returns once the helper has it: the op stays
# running, and the lock held, until the helper has gone (Admin._settle).
HANDED_OVER = object()

FIRST_USER_PORT = 8101  # where Add user's suggested port starts
USER_SERVICE = "tracker"
# An added user's compose file, exactly: only the image and the port are
# filled in. No env_file and no environment - the repo's .env, which the
# root compose file loads, can hold the primary's logins and secret key -
# and no bind mount: the user's /data is a volume of its own. The app
# shelf is mounted read-only, from the Admin's volume (external: compose
# never makes or removes it).
USER_COMPOSE = """services:
  tracker:
    image: {image}:latest
    pull_policy: never
    ports:
      - "{port}:80"
    volumes:
      - tracker-data:/data
      - openfit-library:/library:ro
    restart: unless-stopped
volumes:
  tracker-data: {{}}
  openfit-library:
    external: true
"""
PORT_CLASH = ("port is already allocated", "address already in use")

# The app shelf. The volume is the Admin's own (its compose file makes it);
# the Admin writes it at LIBRARY_DIR, every user reads it at /library.
LIBRARY_VOLUME = "openfit-library"
LIBRARY_DIR = "/library"
LIBRARY_MOUNT = "/library"
# The primary's mount of it: written by the Admin into its own /data and
# passed after the repo's compose file, which never changes for it.
PRIMARY_OVERRIDE = "primary.override.yml"
PRIMARY_OVERRIDE_TEXT = """services:
  {service}:
    volumes:
      - openfit-library:/library:ro
volumes:
  openfit-library:
    external: true
"""
VOLUME_MISSING = "The Admin needs updating first: its app library storage isn't there yet."
# /library/index.json, /library/<source> -> .sets/<source>-<commit12>.
OFFICIAL_SOURCE = "openfit"
SHELF_INDEX = "index.json"
SHELF_SETS = ".sets"
_SHELF_SOURCE = re.compile(r"[a-z][a-z0-9]*\Z")  # as the user system's LIBRARY_SOURCE
_SHELF_APP = re.compile(r"[a-z0-9][a-z0-9-]*\Z")  # a library app's folder
MAX_APP_FILES = 64
MAX_APP_BYTES = 16 * 1024 * 1024
MAX_APPS = 200
# library/repo.yaml: the source's name and blurb, for the index. Each app's
# name and description are read from its own app.yaml, as the user system
# reads them (its MAX_DESCRIPTION, its MAX_MANIFEST_BYTES).
REPO_FILE = "repo.yaml"
MAX_REPO_NAME = 40
MAX_DESCRIPTION = 300
MAX_YAML_BYTES = 256 * 1024

# The System information panel's view of the server: `docker info` with
# exactly this format, at most once a DOCKER_INFO_EVERY seconds, of which
# only SERVER_FIELDS are kept - never the host's container counts or names.
DOCKER_INFO_ARGS = ("info", "--format", "{{json .}}")
DOCKER_INFO_EVERY = 60
SERVER_FIELDS = ("Name", "ServerVersion", "NCPU", "MemTotal")

CHECK_EVERY = 7 * 24 * 3600  # an automatic check, a week after the last
TICK_EVERY = 3600  # how often the background thread looks
HEALTH_EVERY = 2
HEALTH_FOR = 60
KEEP_BACKUPS = 3
KEEP_HISTORY = 20


def health_code(port):
    """Run in a container with `python -c`: exit 0 if its own server answers
    a GET / on `port` with a 200."""
    return (
        "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen("
        f"'http://127.0.0.1:{port}/', timeout=5).status == 200 else 1)"
    )


HEALTH_CODE = health_code(80)
ADMIN_HEALTH_CODE = health_code(ADMIN_PORT)

# Downloads (S2) never carry a password: these two files, at the root of a
# user's /data - its OpenFit password's hash and its sessions' key - are
# left out of every archive the Admin hands over, as tar names them, with
# and without the leading ./. Nothing else is. Whoever restores a download
# sets a new password with a setup link. The backups kept on the server,
# which Undo puts back, still hold everything.
NO_PASSWORD = ("auth.json", ".session_key")
NO_PASSWORD_MEMBERS = frozenset(n for name in NO_PASSWORD for n in (name, f"./{name}"))
CHUNK = 64 * 1024

# A setup link (U1): the user system's own CLI, run in a user's running
# container, makes the token and prints one JSON line - the only command
# the Admin execs there besides the health check. The token is handed
# straight back to the page, once: never in the Admin's history or log.
SETUP_LINK_ARGS = ("python", "auth_cli.py", "setup-link")
SETUP_TOKEN = re.compile(r"[A-Za-z0-9_-]{40,50}\Z")


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

    @contextlib.contextmanager
    def stream(self, args):
        """Runs one command, an argument list and never through a shell, for
        its output as it comes: a context manager yielding its stdout, a
        binary file read in chunks. Leaving the block early - the browser
        gone, or anything else raised - kills the command; leaving it at the
        end of the output waits for it, and a non-zero exit (or a command
        that can't start) is an OpError with its error output. No timeout:
        the block's reader decides how long it runs."""
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise TypeError("a command is a list of strings")
        try:
            proc = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=dict(os.environ, GIT_TERMINAL_PROMPT="0"))
        except OSError as e:
            raise OpError(f"{args[0]} didn't start.", str(e))
        err = []

        def drain():  # its error output, read meanwhile so a full pipe never stalls it
            err.append(proc.stderr.read(CHUNK))
            while proc.stderr.read(CHUNK):
                pass

        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        finished = False
        try:
            yield proc.stdout
            finished = True
        finally:
            if not finished:
                proc.kill()
            proc.stdout.close()
            rc = proc.wait()
            reader.join(10)
        if rc != 0:
            raise OpError(f"{args[0]} stopped with exit code {rc}.",
                          _tail("", (err[0] if err else b"").decode("utf-8", "replace")))


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
    for key, rule in (("project", _NAME), ("service", _NAME), ("branch", _BRANCH), ("user", USER_NAME)):
        if not isinstance(s[key], str) or not rule.match(s[key]) or ".." in s[key]:
            raise ValueError(f"{path}: {key} isn't valid: {s[key]!r}")
    if s["project"] == ADMIN_PROJECT:
        raise ValueError(f"{path}: the project can't be the Admin's own")
    if s["user"] == "admin":
        raise ValueError(f"{path}: the user can't be called 'admin'")
    if type(s["port"]) is not int or not 1 <= s["port"] <= 65535:
        raise ValueError(f"{path}: port isn't valid: {s['port']!r}")
    return s


class User(NamedTuple):
    """One OpenFit the Admin manages: the primary (A1's, adopted in place,
    from the repo's compose file) or one it added, from its own file."""

    name: str
    project: str
    service: str
    compose_file: str
    port: int
    primary: bool
    created_at: str | None = None


def user_project(name):
    return f"openfit-{name}"


def user_volume(name):
    """An added user's /data, as compose names their project's volume."""
    return f"{user_project(name)}_tracker-data"


def name_problem(name, settings):
    """Why `name` can't be an added user's, or None. Not whether it's taken."""
    if not isinstance(name, str) or not USER_NAME.match(name):
        return "A name is 1 to 20 lowercase letters and digits, and nothing else."
    if name == "admin":
        return "'admin' is the Admin's own name. Choose another."
    if name == settings["user"]:
        return f"'{name}' is the primary user's name. Choose another."
    if user_project(name) == settings["project"]:
        return f"'{name}' would share the primary user's compose project. Choose another."
    return None


def port_problem(port):
    """Why `port` can't be an added user's, or None. Not whether it's taken."""
    if type(port) is not int or not 1024 <= port <= 65535:
        return "A port is a whole number from 1024 to 65535."
    if port == ADMIN_PORT:
        return f"Port {ADMIN_PORT} is the Admin's own. Choose another."
    return None


_ENTRY_KEYS = {
    "users": {"name", "port", "created_at"},
    "removed": {"name", "port", "removed_at", "volume"},
}


def load_users(data_dir, settings):
    """/data/users.json: {"users": [{name, port, created_at}], "removed":
    [{name, port, removed_at, volume}]} - the users the Admin added, and
    the ones it removed whose data it keeps. None yet is both empty.

    Checked strictly on every read, like admin.json: anything that isn't
    exactly right is a ValueError naming the file and the key, so a
    hand-edited entry is never something run."""
    path = os.path.join(data_dir, "users.json")
    if not os.path.exists(path):
        return {"users": [], "removed": []}
    raw = _read_json(path, None)
    if not isinstance(raw, dict):
        raise ValueError(f"{path} isn't a JSON object")
    for key in raw:
        if key not in _ENTRY_KEYS:
            raise ValueError(f"{path}: {key!r} isn't a key users.json has")
    reg = {key: raw.get(key, []) for key in _ENTRY_KEYS}
    names, ports = set(), {settings["port"]}
    for key, fields in _ENTRY_KEYS.items():
        if not isinstance(reg[key], list):
            raise ValueError(f"{path}: {key} isn't a list")
        for i, entry in enumerate(reg[key]):
            where = f"{path}: {key}[{i}]"
            if not isinstance(entry, dict) or set(entry) != fields:
                raise ValueError(f"{where} isn't exactly {{{', '.join(sorted(fields))}}}")
            name, port = entry["name"], entry["port"]
            if name_problem(name, settings) or name in names:
                raise ValueError(f"{where}.name isn't valid: {name!r}")
            names.add(name)
            if port_problem(port) or (key == "users" and port in ports):
                raise ValueError(f"{where}.port isn't valid: {port!r}")
            if key == "users":
                ports.add(port)
            stamp = "created_at" if key == "users" else "removed_at"
            if not isinstance(entry[stamp], str) or not _STAMP.match(entry[stamp]):
                raise ValueError(f"{where}.{stamp} isn't valid: {entry[stamp]!r}")
            if key == "removed" and (entry["volume"] != user_volume(name) or not USER_VOLUME.match(entry["volume"])):
                raise ValueError(f"{where}.volume isn't valid: {entry['volume']!r}")
    return reg


def address(host, port):
    """http://<host>:<port>, `host` as the browser reached the Admin (its
    Host header, port dropped); one that isn't a plain host name or IP is
    the words <server IP>."""
    try:
        hostname = urlsplit(f"//{host or ''}").hostname or ""
    except ValueError:
        hostname = ""
    if re.fullmatch(r"[a-z0-9.-]{1,253}", hostname):
        return f"http://{hostname}:{port}"
    if re.fullmatch(r"[0-9a-f:.]{2,45}", hostname):
        return f"http://[{hostname}]:{port}"
    return f"http://<server IP>:{port}"


def port_clash(port, out, err):
    """The message for a port something else on the server holds, if the
    output says so."""
    text = f"{out}\n{err}".lower()
    if any(clash in text for clash in PORT_CLASH):
        return f"Port {port} is already in use on the server. Choose another."
    return None


def group_of(path):
    """The group key a changed file is reported in, by its top folder."""
    for key, _title, folder in GROUPS:
        if folder and path.startswith(folder):
            return key
    return "other"


def _changed(check, key):
    """Whether the check's group `key` lists any commit or file."""
    group = next((g for g in check.get("groups", []) if g["key"] == key), None)
    return bool(group and (group["commits"] or group["files"]))


def update_apps_offered(check):
    """Whether the last check shows only an app library change for Update
    apps (docs and the rest can ride along): nothing new in the user
    system or the Admin, so nothing to rebuild or restart."""
    if not check or "behind" not in check:
        return False
    return (check["behind"] > 0 and not _changed(check, "app") and not _changed(check, "admin")
            and _changed(check, "library"))


def update_offered(check):
    """Whether the last check shows something for Update OpenFit to pull -
    never while Update apps is the one on offer."""
    if not check or "behind" not in check:
        return False
    return (check["behind"] > 0 or _changed(check, "app")) and not update_apps_offered(check)


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


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _commit_of(rev):
    """A recorded version's full commit ({commit, short, subject}, as
    head() gives), if it's one, else None."""
    commit = rev.get("commit") if isinstance(rev, dict) else None
    return commit if isinstance(commit, str) and _COMMIT.match(commit) else None


def server_fields(raw):
    """What the Admin keeps of `docker info`'s JSON: SERVER_FIELDS and
    nothing else - Name and ServerVersion as short text, NCPU and MemTotal
    as whole numbers, each None where Docker gave something else - or None
    if it isn't a JSON object."""
    if not isinstance(raw, dict):
        return None

    def text(value, most):
        return value if isinstance(value, str) and 0 < len(value) <= most and value.isprintable() else None

    def count(value):
        return value if type(value) is int and value >= 0 else None

    return {"Name": text(raw.get("Name"), 255), "ServerVersion": text(raw.get("ServerVersion"), 64),
            "NCPU": count(raw.get("NCPU")), "MemTotal": count(raw.get("MemTotal"))}


# --- the app shelf -------------------------------------------------------------


class _Skip(Exception):
    """An app left off the shelf, and why."""


def _inside(root, path):
    """`path`'s own place - its folder resolved, its name as it is - if
    that's inside `root` (resolved already), else an OpError. Checked
    before anything is written, renamed or removed: the shelf is the only
    place publishing touches."""
    where = os.path.normpath(os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path)))
    if where == root or os.path.commonpath([root, where]) != root:
        raise OpError("Publishing stopped: a path led outside the app library.", path)
    return where


def _no_follow(path, flags):
    """open()'s opener: never through a link, never blocking on a pipe."""
    return os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o644)


def _copy_file(src, dst):
    """One regular file's bytes into a new file of its own, never through a
    link at either end. The bytes copied."""
    with open(src, "rb", opener=_no_follow) as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise OSError(f"{src} isn't a regular file")
        with open(dst, "xb", opener=_no_follow) as out:
            shutil.copyfileobj(f, out)
            return out.tell()


def _app_files(folder, app, log):
    """One app folder's files to publish, as tuples of path parts: its
    regular files, never a link (or anything through one); dotfiles,
    __pycache__ and .pyc left out. _Skip over MAX_APP_FILES or
    MAX_APP_BYTES, or with no app.yaml of its own."""
    found, total, todo = [], 0, [()]
    while todo:
        rel = todo.pop()
        with os.scandir(os.path.join(folder, *rel)) as it:
            entries = sorted(it, key=lambda e: e.name)
        for entry in entries:
            parts = rel + (entry.name,)
            if entry.name.startswith("."):
                continue
            if entry.is_symlink():
                log(f"Leaving out library/{app}/{'/'.join(parts)}: it's a link.")
            elif entry.is_dir(follow_symlinks=False):
                if entry.name != "__pycache__":
                    todo.append(parts)
            elif not entry.is_file(follow_symlinks=False):
                log(f"Leaving out library/{app}/{'/'.join(parts)}: it isn't a regular file.")
            elif not entry.name.endswith(".pyc"):
                found.append(parts)
                total += entry.stat(follow_symlinks=False).st_size
                if len(found) > MAX_APP_FILES:
                    raise _Skip(f"it has more than {MAX_APP_FILES} files")
                if total > MAX_APP_BYTES:
                    raise _Skip(f"it's more than {MAX_APP_BYTES >> 20} MB")
    if ("app.yaml",) not in found:
        raise _Skip("it has no app.yaml")
    return sorted(found)


def _collect_apps(src, log):
    """[(app, files)] for each app folder in `src`, by name: a top-level
    folder named as a library app is, at most MAX_APPS of them. Anything
    else is left out, and logged. No library/ at all is no apps."""
    if os.path.islink(src):
        raise OSError(f"{src} is a link")
    if not os.path.isdir(src):
        log("The repo has no library/ folder, so the app library is published empty.")
        return []
    apps = []
    with os.scandir(src) as it:
        entries = sorted(it, key=lambda e: e.name)
    for entry in entries:
        if entry.name == REPO_FILE:
            continue  # the source's own name and blurb: repo_info() reads it
        if not _SHELF_APP.match(entry.name) or not entry.is_dir(follow_symlinks=False):
            log(f"Leaving out library/{entry.name}: it isn't an app folder.")
        elif len(apps) >= MAX_APPS:
            log(f"Leaving out library/{entry.name}: the app library holds at most {MAX_APPS} apps.")
        else:
            try:
                apps.append((entry.name, _app_files(entry.path, entry.name, log)))
            except _Skip as e:
                log(f"Leaving out library/{entry.name}: {e}.")
    return apps


def _read_yaml(path):
    """A small YAML file's mapping, read never through a link: (mapping,
    None), or (None, why not) - the why a phrase after the file's name."""
    try:
        with open(path, "rb", opener=_no_follow) as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                return None, "isn't a regular file"
            raw = f.read(MAX_YAML_BYTES + 1)
    except FileNotFoundError:
        return None, "is missing"
    except IsADirectoryError:
        return None, "isn't a regular file"
    except OSError as e:
        if e.errno == errno.ELOOP:  # O_NOFOLLOW, on a link
            return None, "is a link"
        return None, f"won't open ({e.strerror or e})"
    if len(raw) > MAX_YAML_BYTES:
        return None, f"is over {MAX_YAML_BYTES >> 10} KB"
    try:
        data = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError):
        return None, "doesn't parse"
    if not isinstance(data, dict):
        return None, "isn't a mapping"
    return data, None


def _blurb(value):
    """A description as the index keeps one: text of at most
    MAX_DESCRIPTION characters once trimmed, else None (blank too)."""
    if isinstance(value, str) and 0 < len(value.strip()) <= MAX_DESCRIPTION:
        return value.strip()
    return None


def repo_info(src, source, log):
    """The source's name and description, from library/repo.yaml: {name,
    description}. `name` text of 1 to MAX_REPO_NAME characters and
    `description` (optional) at most MAX_DESCRIPTION, each trimmed; a file
    that's missing or isn't that is the source id and no description,
    logged."""
    data, why = _read_yaml(os.path.join(src, REPO_FILE))
    if data is not None:
        name, description = data.get("name"), data.get("description")
        if not isinstance(name, str) or not 0 < len(name.strip()) <= MAX_REPO_NAME:
            why = f"has no name of 1 to {MAX_REPO_NAME} characters"
        elif description is not None and not (isinstance(description, str)
                                               and len(description.strip()) <= MAX_DESCRIPTION):
            why = f"has a description that isn't text of at most {MAX_DESCRIPTION} characters"
        else:
            return {"name": name.strip(), "description": _blurb(description)}
    log(f"library/{REPO_FILE} {why}, so the app library is named {source!r}, with no description.")
    return {"name": source, "description": None}


def app_info(folder):
    """A published app's name and description, from its app.yaml: each
    None where it's absent, isn't text (a description over MAX_DESCRIPTION
    characters too), or the file doesn't parse - the app is published all
    the same."""
    data = _read_yaml(os.path.join(folder, "app.yaml"))[0] or {}
    name = data.get("name")
    return {"name": name.strip() if isinstance(name, str) and name.strip() else None,
            "description": _blurb(data.get("description"))}


def _app_entry(folder):
    """An app's entry in the index: its fingerprint, name and description."""
    return {"sha256": fingerprint(folder), **app_info(folder)}


def read_shelf(library_dir):
    """/library/index.json as published - {published_at, sources: {source:
    {commit, name, description, apps: {app: {sha256, name, description}}}}}
    (an index from before S1 has no names or descriptions) - or None if
    there's none, or it doesn't read."""
    raw = _read_json(os.path.join(library_dir, SHELF_INDEX), None)
    if not isinstance(raw, dict) or not isinstance(raw.get("sources"), dict):
        return None
    return raw


def shelf_source(index, source):
    """`source`'s entry in an index read_shelf() gave, if it's well formed,
    else None."""
    entry = (index or {}).get("sources", {}).get(source)
    if (isinstance(entry, dict) and isinstance(entry.get("commit"), str) and _COMMIT.match(entry["commit"])
            and isinstance(entry.get("apps"), dict)):
        return entry
    return None


def _remove_tree(root, path, log):
    """A folder of the shelf's, gone (rmtree never follows a link); a
    failure logged, never raised."""
    try:
        _inside(root, path)
        shutil.rmtree(path)
    except (OSError, OpError) as e:
        log(f"Couldn't delete {os.path.relpath(path, root)} from the app library: {e}")


def _point(root, source, target):
    """The source's link onto `target`, in one step - a new link made beside
    it, renamed over it - or, `target` None, removed."""
    link = os.path.join(root, source)
    _inside(root, link)
    if target is None:
        if os.path.islink(link):
            os.remove(link)
        return
    new = os.path.join(root, f"{source}.new")
    _inside(root, new)
    if os.path.islink(new) or os.path.isfile(new):
        os.remove(new)
    os.symlink(target, new)
    try:
        os.replace(new, link)
    except OSError:
        os.remove(new)
        raise


def _write_index(root, index, source, entry, now):
    """index.json with `source`'s entry, any other source's kept: a temp
    file, then renamed over it."""
    sources = dict(index["sources"]) if index else {}
    sources[source] = entry
    path = os.path.join(root, SHELF_INDEX)
    tmp = f"{path}.tmp"
    _inside(root, path)
    _inside(root, tmp)
    if os.path.lexists(tmp):
        os.remove(tmp)
    with open(tmp, "x", encoding="utf-8", opener=_no_follow) as f:
        json.dump({"published_at": now, "sources": sources}, f, indent=2)
    os.replace(tmp, path)


def publish_set(src, library_dir, source, commit, now, log=print):
    """Publish the app folders in `src` (the repo's library/) to the shelf
    at `library_dir`, as `source` at `commit`. -> {commit, apps: {app:
    {sha256, name, description}}, changed}. The source's entry in the
    index also carries the name and description of `src`'s repo.yaml
    (repo_info()), and each app's its own app.yaml's (app_info()).

    The apps are copied into a new set, .sets/<source>-<commit12>, made
    whole under a hidden name first; then the link <source> is swapped
    onto it in one step, so a reader sees the old set or the new, never
    half of either; then index.json is written, last; then every older set
    of the source is deleted but the one the link was on before. A set
    already the link's is never rewritten (a set never changes): only the
    index, if it doesn't already say so.

    Anything that goes wrong is an OpError with the shelf exactly as it
    was: the new set deleted, the link and the index untouched, or put
    back. Every path is checked to sit inside the shelf before it's
    written."""
    if not _SHELF_SOURCE.match(source) or not _COMMIT.match(commit):
        raise ValueError(f"not a source and commit to publish: {source!r} {commit!r}")
    root = os.path.realpath(library_dir)
    if not os.path.isdir(root):
        raise OpError("The app library's storage isn't there, so nothing was published.", library_dir)
    name = f"{source}-{commit[:12]}"
    link, sets = os.path.join(root, source), os.path.join(root, SHELF_SETS)
    final = os.path.join(sets, name)
    before = os.readlink(link) if os.path.islink(link) else None
    index = read_shelf(root)
    try:
        if before == f"{SHELF_SETS}/{name}" and os.path.isdir(final) and not os.path.islink(final):
            apps = {app: _app_entry(os.path.join(final, app))
                    for app in sorted(os.listdir(final)) if _SHELF_APP.match(app)}
            entry = {"commit": commit, **repo_info(src, source, log), "apps": apps}
            if shelf_source(index, source) == entry:
                return {"commit": commit, "apps": apps, "changed": False}
            _write_index(root, index, source, entry, now)
            return {"commit": commit, "apps": apps, "changed": True}
        apps = _collect_apps(src, log)
        repo = repo_info(src, source, log)
        _inside(root, sets)
        if os.path.islink(sets) or (os.path.lexists(sets) and not os.path.isdir(sets)):
            raise OSError(f"{SHELF_SETS} isn't a folder")
        made_sets = not os.path.lexists(sets)
        if made_sets:
            os.mkdir(sets, 0o755)
    except OSError as e:
        raise OpError("The app library couldn't be updated. It's as it was.", str(e))
    leftover = re.compile(rf"\.(tmp|old)-{source}-[0-9a-f]{{12}}-[0-9a-f]{{8}}\Z")
    for entry in os.listdir(sets):  # from a publish that stopped part-way
        if leftover.match(entry):
            _remove_tree(root, os.path.join(sets, entry), log)

    tmp = os.path.join(sets, f".tmp-{name}-{secrets.token_hex(4)}")
    aside = None  # a set of this name already there (not the link's), moved aside
    placed = swapped = False
    try:
        os.mkdir(_inside(root, tmp), 0o755)
        for app, files in apps:
            copied = 0
            for parts in files:
                dst = os.path.join(tmp, app, *parts)
                os.makedirs(_inside(root, os.path.dirname(dst)), mode=0o755, exist_ok=True)
                copied += _copy_file(os.path.join(src, app, *parts), _inside(root, dst))
                if copied > MAX_APP_BYTES:
                    raise OSError(f"library/{app} grew past {MAX_APP_BYTES >> 20} MB while it was copied")
        # Read from the copies just made, never the repo's: what's indexed is what's published.
        published = {app: _app_entry(os.path.join(tmp, app)) for app, _files in apps}
        if os.path.lexists(final):
            aside = os.path.join(sets, f".old-{name}-{secrets.token_hex(4)}")
            os.rename(_inside(root, final), _inside(root, aside))
        os.rename(tmp, _inside(root, final))
        placed = True
        _point(root, source, f"{SHELF_SETS}/{name}")
        swapped = True
        _write_index(root, index, source, {"commit": commit, **repo, "apps": published}, now)
    except (OSError, OpError) as e:
        # Back as it was: the link on the set it was on, the new set gone, a
        # set moved aside back in its place.
        try:
            if swapped:
                _point(root, source, before)
            _remove_tree(root, final if placed else tmp, log)
            if aside:
                os.rename(aside, final)
            if made_sets:
                os.rmdir(sets)
        except (OSError, OpError) as undo:
            log(f"Couldn't put the app library back as it was: {undo}")
        detail = e.detail if isinstance(e, OpError) else str(e)
        raise OpError("The app library couldn't be updated. It's as it was.", detail)

    if aside:
        _remove_tree(root, aside, log)
    keep = {name, before.removeprefix(f"{SHELF_SETS}/") if before else None}
    old_set = re.compile(rf"{source}-[0-9a-f]{{12}}\Z")
    for entry in sorted(os.listdir(sets)):
        if old_set.match(entry) and entry not in keep:
            _remove_tree(root, os.path.join(sets, entry), log)
    return {"commit": commit, "apps": published, "changed": True}


def labelled(runner, project, service, what):
    """The containers compose labels as `project`'s `service` (never a
    one-off `compose run`): [{id, state}], each id checked."""
    rc, out, err = runner.run([
        "docker", "ps", "-a", "--no-trunc",
        "--filter", f"label=com.docker.compose.project={project}",
        "--filter", f"label=com.docker.compose.service={service}",
        "--filter", "label=com.docker.compose.oneoff=False",
        "--format", "{{.ID}}\t{{.State}}",
    ])
    if rc != 0:
        raise OpError(f"Couldn't ask Docker about {what}.", _tail(out, err))
    rows = [line.split("\t") for line in out.splitlines() if line.strip()]
    for row in rows:
        if not _CONTAINER_ID.match(row[0]):
            raise OpError("Docker gave back a container id the Admin doesn't recognise.", row[0])
    return [{"id": row[0], "state": row[1] if len(row) > 1 else ""} for row in rows]


def find_admin(runner):
    """The Admin's own container, by its compose labels, or None."""
    rows = labelled(runner, ADMIN_PROJECT, ADMIN_SERVICE, "the Admin's own container")
    if len(rows) > 1:
        raise OpError("There's more than one Admin container, so the Admin won't guess which is itself.")
    return rows[0] if rows else None


def swap_helper(runner):
    """Whether there's a container named as the update helper is: one
    still at work, or anything else a second helper's name would clash
    with."""
    rc, out, err = runner.run(["docker", "ps", "-a", "--no-trunc", "--filter", f"name=^{SWAP_NAME}$",
                               "--format", "{{.ID}}"])
    if rc != 0:
        raise OpError("Couldn't ask Docker about the Admin's update helper.", _tail(out, err))
    return bool(out.strip())


def admin_compose(runner, repo, *args):
    """docker compose on the Admin's own project: -p openfit-admin with the
    repo's admin/docker-compose.yml, and only ever one of its two commands -
    `build admin`, run by the Admin, and `up -d --no-build --force-recreate
    admin`, run by the helper. The one way such a command is run."""
    if args not in ADMIN_COMPOSE_ARGS:
        raise ValueError(f"a compose command on the Admin's own project that isn't one of its two: {args!r}")
    compose_file = os.path.join(repo, "admin", "docker-compose.yml")
    return runner.run(["docker", "compose", "-p", ADMIN_PROJECT, "-f", compose_file, *args],
                      cwd=os.path.dirname(compose_file))


def admin_tag(runner, source, target):
    """docker tag on the Admin's own image, and only ever one of four: the
    image the Admin runs now, by the id Docker gave for it, kept as :prev
    (its update) or :next (its revert); and :prev or :next put back as
    :local (the helper: a revert, a rollback, a revert's fallback). The one
    way the Admin's image is tagged: :local never names anything else, and
    nothing outside openfit-admin:{local,prev,next} is ever tagged."""
    by_id = isinstance(source, str) and bool(_IMAGE_ID.match(source))
    if not ((by_id and target in (ADMIN_PREV, ADMIN_NEXT))
            or (source in (ADMIN_PREV, ADMIN_NEXT) and target == ADMIN_IMAGE)):
        raise ValueError(f"a tag of the Admin's image that isn't one of its four: {source!r} -> {target!r}")
    return runner.run(["docker", "tag", source, target])


def swap_helper_args(repo, admin_volume, *command):
    """The swap helper's `docker run`, exactly, in one of its two forms: an
    update's, on :prev (the image the Admin ran before it, never the new
    one) with `<stamp> <op id>`, or a revert's, on :local (the image the
    Admin runs now, the one known to work, which holds the revert) with
    `revert <update stamp> <fresh stamp> <op id>`. Anything else is a
    ValueError."""
    def stamp(x):
        return isinstance(x, str) and bool(_FILE_STAMP.match(x))

    def op_id(x, rule):
        return isinstance(x, str) and bool(rule.match(x))

    if len(command) == 2 and stamp(command[0]) and op_id(command[1], UPDATE_OP_ID):
        image = ADMIN_PREV
    elif (len(command) == 4 and command[0] == "revert" and stamp(command[1]) and stamp(command[2])
          and command[1] != command[2] and op_id(command[3], REVERT_OP_ID)):
        image = ADMIN_IMAGE
    else:
        raise ValueError(f"a swap helper that isn't an update's or a revert's: {command!r}")
    return [
        "docker", "run", "-d", "--rm", "--name", SWAP_NAME, "--label", HELPER_LABEL,
        "-v", "/var/run/docker.sock:/var/run/docker.sock",
        "-v", f"{repo}:{repo}",
        "-v", f"{admin_volume}:/data",
        "-e", f"OPENFIT_REPO={repo}",
        image, *SWAP_SCRIPT, *command,
    ]


def kept_download_args(volume, image=ADMIN_IMAGE):
    """The helper that reads a removed user's kept volume out as a tar.gz on
    its stdout, exactly: mounted read-only, the password's two files left
    out, and no container log - Docker would otherwise keep a copy of the
    whole archive in the container's log while it runs."""
    if not isinstance(volume, str) or not USER_VOLUME.match(volume):
        raise ValueError(f"not a user's kept volume: {volume!r}")
    return ["docker", "run", "--rm", "--label", HELPER_LABEL, "--log-driver", "none",
            "-v", f"{volume}:/v:ro", image,
            "tar", "czf", "-", "-C", "/v", *(f"--exclude=./{name}" for name in NO_PASSWORD), "."]


def without_password(src):
    """A backup's archive, read from the open binary file `src` (a tar.gz),
    as a new tar.gz streamed in chunks: every member as it is - its header
    and its bytes - but the NO_PASSWORD_MEMBERS, which are left out. Never
    a temp file: a thread writes the new archive into a pipe this reads
    from, so the memory it takes is a chunk, whatever the backup's size.
    `src` is closed once it's read, or the reader gone. A backup that won't
    read raises an OpError, after whatever was streamed before it."""
    rfd, wfd = os.pipe()
    failed = []

    def write():
        try:
            with os.fdopen(wfd, "wb") as out, src, \
                    tarfile.open(fileobj=src, mode="r|gz") as old, tarfile.open(fileobj=out, mode="w|gz") as new:
                for member in old:
                    if member.name in NO_PASSWORD_MEMBERS:
                        continue
                    new.addfile(member, old.extractfile(member) if member.isreg() else None)
        except BrokenPipeError:
            pass  # the reader went away
        except (OSError, tarfile.TarError, EOFError, ValueError) as e:
            failed.append(e)

    writer = threading.Thread(target=write, name="admin-download", daemon=True)
    writer.start()
    with os.fdopen(rfd, "rb") as pipe:
        while True:
            chunk = pipe.read1(CHUNK)
            if not chunk:
                break
            yield chunk
    writer.join()
    if failed:
        raise OpError("The backup couldn't be read.", str(failed[0]))


def primed(chunks):
    """A download's chunks, its first already read: a failure before any
    byte - a backup that won't open, a helper that won't start - is raised
    here, while the reply can still say so; then a generator of them all."""
    chunks = iter(chunks)
    try:
        first = next(chunks)
    except StopIteration:
        first = b""

    def again():
        try:
            if first:
                yield first
            yield from chunks
        finally:
            close = getattr(chunks, "close", None)
            if close:
                close()

    return again()


def admin_command(repo):
    """The Admin's own update, by hand on the server."""
    return f"cd {repo} && docker compose -f admin/docker-compose.yml -p {ADMIN_PROJECT} up -d --build"


def wait_healthy(runner, find, code, clock, sleep):
    """Poll a container until `code` exits 0 in it, every HEALTH_EVERY
    seconds for up to HEALTH_FOR. `find()` gives its id, or None (an
    OpError counts as None), and is asked again until it gives one."""
    deadline = clock() + HEALTH_FOR
    cid = None
    while True:
        if cid is None:
            try:
                cid = find()
            except OpError:
                cid = None
        if cid:
            rc, _out, _err = runner.run(["docker", "exec", cid, "python", "-c", code], timeout=30)
            if rc == 0:
                return True
        if clock() >= deadline:
            return False
        sleep(HEALTH_EVERY)


def snapshot_dir(data_dir, stamp):
    return os.path.join(data_dir, SNAPSHOTS, stamp)


def read_swap_result(data_dir):
    """/data/self_update_result.json, the helper's log: {op_id, stamp,
    outcome, lines: [{t, text}]}, outcome "running" until it's done, or
    None if there's none. Anything not exactly that is a ValueError."""
    path = os.path.join(data_dir, SWAP_RESULT)
    if not os.path.lexists(path):
        return None
    raw = _read_json(path, None)
    if not isinstance(raw, dict) or set(raw) != {"op_id", "stamp", "outcome", "lines"}:
        raise ValueError(f"{path} isn't exactly {{op_id, stamp, outcome, lines}}")
    if not isinstance(raw["op_id"], str) or not SWAP_OP_ID.match(raw["op_id"]):
        raise ValueError(f"{path}: op_id isn't valid: {raw['op_id']!r}")
    if not isinstance(raw["stamp"], str) or not _FILE_STAMP.match(raw["stamp"]):
        raise ValueError(f"{path}: stamp isn't valid: {raw['stamp']!r}")
    if raw["outcome"] not in ("running", *SWAP_OUTCOMES):
        raise ValueError(f"{path}: outcome isn't valid: {raw['outcome']!r}")
    lines = raw["lines"]
    if not isinstance(lines, list) or not all(
        isinstance(line, dict) and set(line) == {"t", "text"} and all(isinstance(v, str) for v in line.values())
        for line in lines
    ):
        raise ValueError(f"{path}: lines aren't [{{t, text}}]")
    return raw


def revert_summary(op, outcome):
    """A revert's summary, once its helper is done: `outcome` ok, rolled_back
    or failed. Its op's `new` is the version it went back to. Shared by the
    Admin that finishes the op and the helper, when it finishes the op for
    an Admin from before reverts (admin_swap.py)."""
    target = op.get("new") if isinstance(op.get("new"), dict) else {}
    short = target.get("short") if isinstance(target.get("short"), str) else None
    if outcome == "ok":
        return f"The Admin went back to {short}." if short else "The Admin went back to the version before its last update."
    if outcome == "rolled_back":
        return "The previous Admin didn't come up, so this version was put back, with its settings as they were."
    return "The revert didn't finish. The log says where things stand, and what to run on the server."


def registry_people(reg):
    """Who a users.json holds, as a revert compares two: each added user by
    name and when they were added, each removed one by name and when they
    were removed."""
    return ({(e["name"], e["created_at"]) for e in reg["users"]},
            {(e["name"], e["removed_at"]) for e in reg["removed"]})


class Admin:
    """The OpenFit users the Admin manages, and the operations on them.

    One operation at a time (`lock`): a check, an update, an undo, adding,
    starting, stopping, restarting or removing a user, making a setup link,
    deleting a removed user's data or a backup, starting a download,
    updating the apps, publishing them again, or updating the Admin itself
    or reverting it. An update, an undo, adding a user, updating the apps,
    an Admin update and an Admin revert run in a background thread, their
    progress in `op`; the rest run in the caller's thread (a download only
    while it starts: once its file is open, or its helper under way, it
    streams without the lock). An Admin update or revert keeps the lock
    after its thread has handed over to the helper, until the helper has
    gone (`_settle`).

    Constructing one writes the users' mounts of the app shelf
    (write_mounts()) and nothing else; publish_at_startup() is serve()'s
    to call.
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
        library_dir=LIBRARY_DIR,
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
        self.users_dir = os.path.join(data_dir, "users")
        self.users_path = os.path.join(data_dir, "users.json")
        self.check_path = os.path.join(data_dir, "last_check.json")
        self.history_path = os.path.join(data_dir, "history.json")
        self.swap_result = os.path.join(data_dir, SWAP_RESULT)
        self.override_path = os.path.join(data_dir, PRIMARY_OVERRIDE)
        self.library_dir = library_dir
        self.library_error = None  # the last publish's failure, until one succeeds
        self.lock = threading.Lock()
        self._op_lock = threading.Lock()
        self._settle_lock = threading.Lock()
        self.op = None
        self.thread = None
        self._awaiting = None  # an Admin update the helper has: (op, "handover" or "startup")
        self.started_at = self._iso()  # this Admin process's start, for the System panel
        self._server = None  # (when, server_fields()) of the last `docker info`
        self._ranges = {}  # (old, new, folder) -> the commits and PRs between: fixed once read
        os.makedirs(self.backups_dir, exist_ok=True)
        self.registry()  # a bad users.json stops the Admin starting
        self._mark_interrupted(keep=self._pick_up_swap())
        self.write_mounts()

    # --- the users --------------------------------------------------------

    def registry(self):
        return load_users(self.data_dir, self.settings)

    def _save_registry(self, reg):
        _write_json(self.users_path, reg)

    def primary(self):
        s = self.settings
        return User(s["user"], s["project"], s["service"], self.compose_file, s["port"], True)

    def _compose_file_of(self, name):
        return os.path.join(self.users_dir, name, "docker-compose.yml")

    def added(self, name, port, created_at=None):
        return User(name, user_project(name), USER_SERVICE, self._compose_file_of(name), port, False, created_at)

    def users(self, reg=None):
        """The primary first, then the added users in the order they were added."""
        reg = reg or self.registry()
        return [self.primary()] + [self.added(e["name"], e["port"], e["created_at"]) for e in reg["users"]]

    def find_user(self, name):
        return next((u for u in self.users() if u.name == name), None)

    def next_port(self, users=None):
        """The lowest port from 8101 up that none of the Admin's users has."""
        taken = {u.port for u in users or self.users()} | {ADMIN_PORT}
        return next((p for p in range(FIRST_USER_PORT, 65536) if p not in taken), None)

    def _compose_text(self, user):
        """An added user's compose file, as the Admin writes it."""
        return USER_COMPOSE.format(image=self.image, port=user.port)

    def write_mounts(self):
        """At startup: the primary's override (its mount of the app shelf),
        and every added user's compose file from the template, each written
        only if it's missing or differs. Nobody is restarted: each picks the
        mount up the next time they're recreated - an update, or Restart. A
        file that can't be written is logged."""
        wanted = [(self.override_path, PRIMARY_OVERRIDE_TEXT.format(service=self.settings["service"]))]
        wanted += [(u.compose_file, self._compose_text(u)) for u in self.users()[1:]]
        for path, text in wanted:
            try:
                with open(path, encoding="utf-8") as f:
                    if f.read() == text:
                        continue
            except OSError:
                pass
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = f"{path}.tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(text)
                os.replace(tmp, path)
                print(f"[admin] wrote {path}, with the app library mounted", flush=True)
            except OSError as e:
                print(f"[admin] couldn't write {path}: {e}", flush=True)

    # --- commands -------------------------------------------------------

    def git(self, *args, timeout=None):
        return self.runner.run(["git", *args], cwd=self.repo, as_owner=True, timeout=timeout)

    def compose_files(self, user):
        """The compose files of one user's project, in order: the repo's,
        then the Admin's override, for the primary; the file the Admin
        wrote, for an added user. Nothing else is ever passed."""
        if user.primary:
            if (user.project, user.compose_file) == (self.settings["project"], self.compose_file):
                return [self.compose_file, self.override_path]
        elif (USER_NAME.match(user.name) and user.project == user_project(user.name)
              and user.compose_file == self._compose_file_of(user.name)):
            return [user.compose_file]
        raise ValueError(f"a compose command outside the Admin's lane: {user!r}")

    def compose(self, user, *args):
        """docker compose, scoped to one user's project and its own files:
        -p <project> with the repo's compose file and the override for the
        primary, -p openfit-<name> with the file the Admin wrote for an
        added user."""
        files = self.compose_files(user)
        return self.runner.run(
            ["docker", "compose", "-p", user.project, *[a for f in files for a in ("-f", f)], *args],
            cwd=os.path.dirname(user.compose_file),
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

    def _commit_info(self, rev, *paths):
        """{commit, short, date, subject} of `rev` (the newest commit at or
        before it touching `paths`, if any are given) - date the committer's,
        ISO 8601 - or None for no such commit. An OpError if git fails."""
        out = self._git_out("log", "-1", "--format=%H%x09%h%x09%cI%x09%s", rev, "--", *paths,
                            what="read a version")
        if not out.strip():
            return None
        commit, short, date, subject = (out.strip().split("\t", 3) + ["", "", ""])[:4]
        if not _COMMIT.match(commit):
            raise OpError("Couldn't read a version.", out)
        return {"commit": commit, "short": short, "date": date, "subject": subject}

    def dirty_files(self):
        rc, out, err = self.git("status", "--porcelain")
        if rc != 0:
            raise Refused("Couldn't read the state of the server's copy of OpenFit. Nothing was changed.", 500)
        return [line[3:] for line in out.splitlines() if line.strip()]

    # --- a user's container ----------------------------------------------

    def find_container(self, user):
        """`user`'s container, by its compose labels, or None."""
        rows = labelled(self.runner, user.project, user.service, f"{user.name}'s OpenFit container")
        if len(rows) > 1:
            raise OpError(f"{user.name} has more than one OpenFit container, so the Admin won't guess which.")
        return rows[0] if rows else None

    def inspect(self, cid):
        rc, out, err = self.docker("inspect", "--type", "container", cid)
        if rc != 0:
            raise OpError("Couldn't inspect an OpenFit container.", _tail(out, err))
        try:
            info = json.loads(out)[0]
        except (ValueError, IndexError, KeyError, TypeError):
            raise OpError("Couldn't read what Docker said about an OpenFit container.", out[-400:])
        return info

    def data_volume(self, info):
        """The volume mounted at /data in the container `info` describes."""
        for m in info.get("Mounts") or []:
            if m.get("Destination") == "/data":
                name = m.get("Name") or ""
                if m.get("Type") == "volume" and _VOLUME.match(name):
                    return name
                raise OpError("An OpenFit's /data isn't a Docker volume, so the Admin can't back it up.")
        raise OpError("An OpenFit container has no /data storage to back up.")

    @staticmethod
    def has_library(info):
        """Whether the container `info` describes has the app shelf's volume
        mounted at /library."""
        return any(m.get("Type") == "volume" and m.get("Name") == LIBRARY_VOLUME
                   and m.get("Destination") == LIBRARY_MOUNT for m in info.get("Mounts") or [])

    def _container_or_fail(self, user):
        c = self.find_container(user)
        if c is None:
            if user.primary:
                raise OpError("OpenFit's container wasn't found. Start it once from the command line first.")
            raise OpError(f"{user.name}'s OpenFit container wasn't found.")
        return c

    def container_view(self, user):
        """(Docker's state of `user`'s container, whether it has the app shelf
        mounted, read from inspect): missing for none, unknown if Docker
        didn't say - and no shelf, for either."""
        try:
            c = self.find_container(user)
        except OpError:
            return "unknown", False
        if c is None:
            return "missing", False
        try:
            return c["state"], self.has_library(self.inspect(c["id"]))
        except OpError:
            return c["state"], False

    @staticmethod
    def plain_state(raw):
        """running, stopped or missing (or unknown), as the page shows a user."""
        return raw if raw in ("running", "missing", "unknown") else "stopped"

    def healthy(self, user):
        """Poll `user`'s OpenFit until it answers, for up to HEALTH_FOR seconds."""
        return wait_healthy(self.runner, lambda: (self.find_container(user) or {}).get("id"), HEALTH_CODE,
                            self.clock, self.sleep)

    # --- checking for updates --------------------------------------------

    def last_check(self):
        check = _read_json(self.check_path, None)
        return check if isinstance(check, dict) else None

    def check(self):
        """Fetch, then report what's new, by group. Never touches HEAD or the tree."""
        self._take_lock()
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

    # --- the Admin's own update ------------------------------------------

    def admin_update(self, check):
        """The Admin's own update.

        `commits` are new on GitHub and not pulled yet; `pulled` means the
        repo's admin/ already differs from the code this Admin is running
        (an Update OpenFit pulled it), so a rebuild is all it takes.
        `can_self_update`: pulled, and nothing more for the Admin waiting
        on GitHub - one repo is one pull, so Update OpenFit comes first.
        `command` is the same update by hand, on the server.
        """
        group = next((g for g in (check or {}).get("groups", []) if g["key"] == "admin"), None)
        commits = group["commits"] if group else []
        unpulled = bool(commits or (group and group["files"]))
        mine = fingerprint(self.self_dir)
        pulled = mine is not None and fingerprint(os.path.join(self.repo, "admin")) not in (None, mine)
        return {
            "available": unpulled or pulled,
            "pulled": pulled,
            "commits": commits,
            "command": admin_command(self.repo),
            "can_self_update": pulled and not unpulled,
        }

    def start_admin_update(self):
        """The lock and the offer here, so a refusal is a 409 with nothing
        done; the rest (an op with a log) in the background."""
        self._take_lock()
        try:
            au = self.admin_update(self.last_check())
            if not au["can_self_update"]:
                raise Refused("Update OpenFit first: it brings the Admin's new code with it."
                              if au["available"] else "The Admin is up to date.")
            op = self._begin("admin_update")
        except BaseException:
            self.lock.release()
            raise
        self._spawn(op, self._admin_update)
        return self.current_op()

    def _admin_update(self, op):
        """Build the new Admin while this one runs, then hand the swap to the
        helper - which runs the image this Admin is running now, kept as
        openfit-admin:prev, never the new one."""
        try:
            self._note_admin_range(op)
            self.say(op, "Finding the Admin's own container…")
            me = find_admin(self.runner)
            if me is None:
                raise OpError("The Admin's own container wasn't found: it's started with "
                              f"`docker compose -f admin/docker-compose.yml -p {ADMIN_PROJECT} up -d --build`.")
            image_id = self.inspect(me["id"]).get("Image", "")
            if not _IMAGE_ID.match(image_id):
                raise OpError("Couldn't tell which image the Admin is running.", image_id)
            if swap_helper(self.runner):
                self.say(op, f"There's already a container called {SWAP_NAME}: another Admin update's helper.")
                return self._finish(op, "failed", "An Admin update is already under way. Nothing was changed.")
            self.say(op, "Keeping a copy of the version the Admin is on…")
            rc, out, err = admin_tag(self.runner, image_id, ADMIN_PREV)
            if rc != 0:
                raise OpError("Couldn't keep a copy of the Admin's version.", _tail(out, err))
            stamp = self._stamp()
            self.say(op, "Keeping a copy of the Admin's settings…")
            self._snapshot(stamp)
            with self._op_lock:
                op["snapshot"] = stamp  # what a revert of this update puts back
        except OpError as e:
            self.say(op, e.message, e.detail)
            return self._finish(op, "failed", "Nothing was changed.")

        self.say(op, "Building the new Admin. This one keeps running meanwhile…")
        rc, out, err = admin_compose(self.runner, self.repo, "build", ADMIN_SERVICE)
        if rc != 0:
            self.say(op, "The new Admin didn't build.", _tail(out, err))
            return self._finish(op, "failed", "The build failed, so nothing was changed. The Admin kept running.")

        self.say(op, "Starting the helper that swaps the Admin over…")
        rc, out, err = self.runner.run(swap_helper_args(self.repo, self.admin_volume, stamp, op["id"]))
        if rc != 0:
            self.say(op, "The helper didn't start.", _tail(out, err))
            return self._finish(op, "failed", "The helper didn't start, so the Admin wasn't swapped. It's still on its old version.")
        self.say(op, "Handing over to the helper. The Admin restarts in a moment, and this page reconnects by itself.")
        # The log so far, for whichever Admin finishes the op: saved, and in
        # the snapshot too, which a rollback puts back. The helper waits a
        # few seconds before it touches anything.
        self._save_op(op)
        try:
            shutil.copy2(self.history_path, os.path.join(snapshot_dir(self.data_dir, stamp), "history.json"))
        except OSError as e:
            print(f"[admin] admin_update: couldn't add the log to the settings snapshot: {e}", flush=True)
        with self._settle_lock:
            self._awaiting = (op, "handover")
        return HANDED_OVER

    def _admin_base(self, before=None):
        """The commit the running Admin was built from, as far as the history
        says, or None: the `new` of the last ok Admin update or revert (a
        revert's `new` the version it went back to), else the `old` of the
        first ok OpenFit update after it (the one that pulled the Admin's
        new code), else - none since - None. Only the ops before the one
        with id `before` count."""
        base, since = None, None
        for o in self.history():
            if before is not None and o.get("id") == before:
                break
            if o.get("outcome") != "ok":
                continue
            if o.get("kind") == "admin_update" and _commit_of(o.get("new")):
                base, since = _commit_of(o["new"]), None
            elif o.get("kind") == "admin_revert":
                base, since = _commit_of(o.get("new")), None
            elif o.get("kind") == "update" and since is None and _commit_of(o.get("old")):
                since = _commit_of(o["old"])
        return since or base

    def _note_admin_range(self, op):
        """An Admin update's own record of what it brings: `new` HEAD (the
        code it builds), `old` the code the running Admin was built from
        (_admin_base()), for the Versions panel's last update. Either is
        None where git can't say; the update goes ahead regardless."""
        try:
            new = self.head()
        except OpError:
            return
        base = self._admin_base(before=op["id"])
        old = None
        if base:
            try:
                old = self._commit_info(base)
            except OpError:
                old = None
        with self._op_lock:
            op["new"], op["old"] = new, old

    def _snapshot(self, stamp, keep=()):
        """The Admin's settings files, each if present, copied into
        /data/self-update/<stamp>/ for the helper to put back; the newest
        KEEP_SNAPSHOTS kept, and the stamps in `keep` (the update a revert
        puts back) whatever their age."""
        root = os.path.join(self.data_dir, SNAPSHOTS)
        folder = snapshot_dir(self.data_dir, stamp)
        try:
            shutil.rmtree(folder, ignore_errors=True)
            os.makedirs(folder, mode=0o700)
            for name in SNAPSHOT_FILES:
                live = os.path.join(self.data_dir, name)
                if os.path.isfile(live):
                    shutil.copy2(live, os.path.join(folder, name))
            stamps = sorted((n for n in os.listdir(root) if _FILE_STAMP.match(n)
                             and os.path.isdir(os.path.join(root, n))), reverse=True)
            for old in stamps[KEEP_SNAPSHOTS:]:
                if old not in keep:
                    shutil.rmtree(os.path.join(root, old), ignore_errors=True)
        except OSError as e:
            raise OpError("Couldn't keep a copy of the Admin's settings.", str(e))

    def _pick_up_swap(self):
        """At startup, before _mark_interrupted: the helper's result, if an
        Admin update or revert left one. Finished, it finishes its op. Still "running",
        the helper is at work - checking this very Admin answers - so the op
        stays running and the lock is held until the helper has gone; its
        id is returned, for _mark_interrupted to leave alone."""
        try:
            result = read_swap_result(self.data_dir)
        except ValueError as e:
            print(f"[admin] removing the update helper's result, which doesn't read: {e}", flush=True)
            self._drop_swap_result()
            return None
        if result is None:
            return None
        op = next((o for o in self.history() if o.get("id") == result["op_id"] and o.get("kind") in SWAP_KINDS), None)
        if op is None:
            print(f"[admin] removing the update helper's result: no Admin update or revert {result['op_id']} to finish",
                  flush=True)
            self._drop_swap_result()
            return None
        if result["outcome"] != "running":
            self._adopt(op, result, "startup")
            return None
        self.lock.acquire()
        op.update(outcome="running", summary="", finished_at=None)
        self._save_op(op)
        with self._op_lock:
            self.op = op
        self._awaiting = (op, "startup")
        return op["id"]

    def _settle(self):
        """An Admin update the helper has, finished once the helper has gone,
        from its result; until then the op stays running and the lock held,
        so nothing starts that the swap or its rollback would cut off. A
        no-op unless one is waiting. Run before the op is read or the lock
        taken."""
        if self._awaiting is None:
            return
        with self._settle_lock:
            if self._awaiting is None:
                return
            op, where = self._awaiting
            try:
                if swap_helper(self.runner):
                    return
            except OpError:
                return  # asked again next time
            try:
                # Read once the helper has gone: its last write is its outcome.
                result = read_swap_result(self.data_dir)
            except ValueError as e:
                print(f"[admin] the update helper's result doesn't read: {e}", flush=True)
                result = None
            if result is not None and result["op_id"] != op["id"]:
                result = None
            self._adopt(op, result, where)
            self._awaiting = None
            with self._op_lock:
                if self.op is op:
                    self.op = None
            self.lock.release()

    def _adopt(self, op, result, where):
        """The helper's lines and outcome onto its op, and its result file
        removed. With no outcome (no result, or one still "running" when
        the helper has gone), the helper stopped part-way: failed."""
        outcome = result["outcome"] if result else "running"
        revert = op.get("kind") == "admin_revert"
        if not revert and not op.get("snapshot"):
            # Started by an Admin from before S2: one from before S1 noted no
            # versions at all, and S1's `old` doesn't know a revert can have
            # put the Admin on an older version. Noted again, as this one would.
            self._note_admin_range(op)
        with self._op_lock:
            op["log"].extend({"t": line["t"], "text": line["text"]} for line in (result or {}).get("lines", []))
            if result and not op.get("snapshot"):  # started by an Admin from before S2, which didn't note it
                op["snapshot"] = result["stamp"]
        if revert and outcome in SWAP_OUTCOMES:
            summary = revert_summary(op, outcome)
        elif revert:
            outcome = "failed"
            if where == "handover":
                self.say(op, "The helper stopped before it took the Admin back, so this is still the current version.")
            else:
                self.say(op, "The helper stopped before it finished, after the step above. This Admin is answering; "
                         "if it isn't the version you expected, run this on the server: " + admin_command(self.repo))
            summary = "The helper stopped part-way. The log says how far it got."
        elif outcome == "ok":
            try:
                summary = f"The Admin updated itself to {self.head()['short']}."
            except OpError:
                summary = "The Admin updated itself."
        elif outcome == "rolled_back":
            summary = "The new Admin didn't come up, so the old one was put back, with its settings as they were."
        elif outcome == "failed":
            summary = "The Admin update didn't finish. The log says where things stand, and what to run on the server."
        else:
            outcome = "failed"
            if where == "handover":
                self.say(op, "The helper stopped before it swapped the Admin over, so this is still the old version.")
            else:
                self.say(op, "The helper stopped before it finished, after the step above. This Admin is answering; "
                         "if it isn't the version you expected, run this on the server: " + admin_command(self.repo))
            summary = "The helper stopped part-way. The log says how far it got."
        self._finish(op, outcome, summary)
        self._drop_swap_result()

    def _drop_swap_result(self):
        try:
            os.remove(self.swap_result)
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f"[admin] couldn't remove {self.swap_result}: {e}", flush=True)

    # --- reverting the Admin -----------------------------------------------

    def _admin_images(self):
        """(the Admin's own container, the image it runs, :prev's image) -
        the images by the ids Docker gave, :prev's None if there's no such
        image. An OpError if Docker can't say."""
        me = find_admin(self.runner)
        if me is None:
            raise OpError("The Admin's own container wasn't found: it's started with "
                          f"`docker compose -f admin/docker-compose.yml -p {ADMIN_PROJECT} up -d --build`.")
        running = self.inspect(me["id"]).get("Image", "")
        if not _IMAGE_ID.match(running):
            raise OpError("Couldn't tell which image the Admin is running.", running)
        rc, out, err = self.docker("image", "inspect", "--format", "{{.Id}}", ADMIN_PREV)
        if rc != 0:
            if "no such image" in f"{out}\n{err}".lower():
                return me, running, None
            raise OpError("Couldn't ask Docker about the Admin's previous version.", _tail(out, err))
        prev = out.strip()
        if not _IMAGE_ID.match(prev):
            raise OpError("Docker gave back an image id the Admin doesn't recognise.", prev)
        return me, running, prev

    def revert_offer(self, busy=False, ops=None):
        """Whether Revert the Admin is on offer: {offered, reason, update,
        old} - `reason` the one line saying why not, `update` the Admin
        update it would reverse (its id), `old` the version it goes back to
        (short), where known.

        Offered only when the newest Admin update worked; its settings
        snapshot is still here; the users are the ones it had - nobody
        added, removed or deleted since (the snapshot's users.json and the
        live one hold the same people), so putting users.json back loses
        no one; nothing is running (`busy`); the image it ran before,
        openfit-admin:prev, is still on the server; and the Admin isn't
        already running that image (reverted already). The Docker checks
        last, so they run only when the rest hold."""
        ops = self.history() if ops is None else ops
        update = next((o for o in reversed(ops) if o.get("kind") == "admin_update"), None)
        old = update.get("old") if update and isinstance(update.get("old"), dict) else {}
        view = {"offered": False, "reason": None, "update": update.get("id") if update else None,
                "old": old.get("short") if isinstance(old.get("short"), str) else None}

        def no(reason):
            return dict(view, reason=reason)

        if update is None:
            return no("The Admin hasn't updated itself here yet, so there's no earlier version to go back to.")
        if update.get("outcome") != "ok":
            return no("The Admin's last update didn't finish, so there's no earlier version to go back to.")
        stamp = update.get("snapshot")
        if not (isinstance(stamp, str) and _FILE_STAMP.match(stamp)
                and os.path.isdir(snapshot_dir(self.data_dir, stamp))):
            return no("The Admin's settings from before that update aren't kept any more, so it can't be reverted.")
        try:
            then = load_users(snapshot_dir(self.data_dir, stamp), self.settings)
            now = self.registry()
        except ValueError:
            return no("The users kept with that update don't read, so it can't be reverted.")
        if registry_people(then) != registry_people(now):
            return no("Users were added or removed since that update, so it can't be reverted.")
        if busy:
            return no("Something else is running. Wait for it to finish, then try again.")
        try:
            _me, running, prev = self._admin_images()
        except OpError:
            return no("Couldn't ask Docker about the Admin's versions, so it can't be reverted now.")
        if prev is None:
            return no("The Admin's previous version isn't on the server any more, so it can't be reverted.")
        if running == prev:
            return no("The Admin is already on the version from before that update.")
        return dict(view, offered=True)

    def start_admin_revert(self):
        """Revert the Admin: the lock and the offer here, so a refusal is a
        409 with nothing done; the rest (an op with a log) in the
        background."""
        self._take_lock()
        try:
            ops = self.history()
            offer = self.revert_offer(ops=ops)
            if not offer["offered"]:
                raise Refused(offer["reason"])
            update = next(o for o in ops if o.get("id") == offer["update"])
            op = self._begin("admin_revert", undoes=update["id"])
            with self._op_lock:
                # From the version the update brought, back to the one before it.
                op.update(old=update.get("new"), new=update.get("old"))
            self._save_op(op)
        except BaseException:
            self.lock.release()
            raise
        self._spawn(op, self._admin_revert, update["snapshot"])
        return self.current_op()

    def _admin_revert(self, op, update_stamp):
        """Keep the image the Admin runs now as openfit-admin:next and its
        settings as they are now, then hand the revert to the helper - which
        runs that same image (openfit-admin:local), the one known to work,
        never the one it goes back to - with the update's snapshot to put
        back and the fresh one to fall back on."""
        try:
            self.say(op, "Finding the Admin's own container…")
            me = find_admin(self.runner)
            if me is None:
                raise OpError("The Admin's own container wasn't found: it's started with "
                              f"`docker compose -f admin/docker-compose.yml -p {ADMIN_PROJECT} up -d --build`.")
            image_id = self.inspect(me["id"]).get("Image", "")
            if not _IMAGE_ID.match(image_id):
                raise OpError("Couldn't tell which image the Admin is running.", image_id)
            if swap_helper(self.runner):
                self.say(op, f"There's already a container called {SWAP_NAME}: another Admin update's or revert's "
                         "helper.")
                return self._finish(op, "failed", "An Admin update or revert is already under way. Nothing was changed.")
            self.say(op, "Keeping a copy of the version the Admin is on, to come back to if the previous one "
                     "doesn't start…")
            rc, out, err = admin_tag(self.runner, image_id, ADMIN_NEXT)
            if rc != 0:
                raise OpError("Couldn't keep a copy of the Admin's version.", _tail(out, err))
            fresh = self._stamp()
            if fresh == update_stamp or os.path.lexists(snapshot_dir(self.data_dir, fresh)):
                raise OpError("The Admin's settings were copied a moment ago. Try again in a few seconds.")
            self.say(op, "Keeping a copy of the Admin's settings as they are now…")
            self._snapshot(fresh, keep={update_stamp})
            with self._op_lock:
                op["snapshot"] = fresh
        except OpError as e:
            self.say(op, e.message, e.detail)
            return self._finish(op, "failed", "Nothing was changed.")

        self.say(op, "Starting the helper that takes the Admin back…")
        rc, out, err = self.runner.run(swap_helper_args(self.repo, self.admin_volume, "revert", update_stamp, fresh,
                                                        op["id"]))
        if rc != 0:
            self.say(op, "The helper didn't start.", _tail(out, err))
            return self._finish(op, "failed", "The helper didn't start, so the Admin wasn't reverted. It's still on "
                                              "its current version.")
        self.say(op, "Handing over to the helper. The Admin restarts in a moment, and this page reconnects by itself.")
        # As an update's: the log so far saved, and in the fresh snapshot too,
        # which the helper puts back if the previous version doesn't come up.
        self._save_op(op)
        try:
            shutil.copy2(self.history_path, os.path.join(snapshot_dir(self.data_dir, fresh), "history.json"))
        except OSError as e:
            print(f"[admin] admin_revert: couldn't add the log to the settings snapshot: {e}", flush=True)
        with self._settle_lock:
            self._awaiting = (op, "handover")
        return HANDED_OVER

    # --- the app shelf ------------------------------------------------------

    def publish_official(self, commit=None, log=None):
        """The repo's library/ published to the shelf as the openfit source,
        at `commit` - HEAD's, read if not given: the working tree, which the
        Admin keeps clean. publish_set()'s result; an OpError with the shelf
        as it was, noted for the page until a publish succeeds."""
        log = log or (lambda text: print(f"[admin] app library: {text}", flush=True))
        try:
            if commit is None:
                commit = self.head()["commit"]
            result = publish_set(os.path.join(self.repo, "library"), self.library_dir, OFFICIAL_SOURCE,
                                 commit, self._iso(), log)
        except OpError as e:
            self.library_error = {"at": self._iso(), "text": e.message, "detail": e.detail}
            raise
        self.library_error = None
        return result

    def publish_at_startup(self):
        """serve()'s, before the server starts: publish if the shelf's index is
        missing, its openfit commit isn't HEAD's, or its entry has no name
        (published by an Admin from before S1) - so the shelf appears by
        itself on first rollout, catches up after anything that moved the
        code without publishing, and an Admin update brings repo.yaml's name
        and the apps' to the index (the set itself, unchanged, isn't
        rewritten). Whether it published; never raises."""
        try:
            head = self.head()
        except OpError as e:
            print(f"[admin] app library: {e.message} {e.detail}", flush=True)
            return False
        entry = shelf_source(read_shelf(self.library_dir), OFFICIAL_SOURCE)
        if entry and entry["commit"] == head["commit"] and "name" in entry:
            return False
        print(f"[admin] app library: publishing {head['short']}…", flush=True)
        try:
            result = self.publish_official(head["commit"])
        except OpError as e:
            print(f"[admin] app library: {e.message} {e.detail}", flush=True)
            return False
        print(f"[admin] app library: on {head['short']}, {len(result['apps'])} app(s)", flush=True)
        return True

    def publish_again(self):
        """Publish again, after a publish that failed: from HEAD, under the
        lock, in the caller's thread. An OpError says why it failed again."""
        self._take_lock()
        try:
            result = self.publish_official()
            print(f"[admin] app library: published again, {len(result['apps'])} app(s)", flush=True)
        finally:
            self.lock.release()

    def _publish_step(self, op, commit, putting_back=False):
        """An op's publish: its lines in the op's log. True if it worked."""
        short = commit[:7]
        self.say(op, f"Putting the app library back on {short}…" if putting_back
                 else "Publishing the new apps to the app library…")
        try:
            result = self.publish_official(commit, log=lambda text: self.say(op, text))
        except OpError as e:
            self.say(op, e.message, e.detail)
            return False
        n = len(result["apps"])
        self.say(op, f"The app library is on {short}: {n} app{'' if n == 1 else 's'}.")
        return True

    def _republish(self, op):
        """After an undo or rollback reset the code: the shelf published from
        HEAD, so it matches the code again."""
        try:
            commit = self.head()["commit"]
        except OpError as e:
            self.say(op, f"The app library wasn't put back: {e.message}", e.detail)
            return
        self._publish_step(op, commit, putting_back=True)

    def library_status(self, head):
        """The app library's state, for the Repos panel: when the shelf was
        published and from which commit, its apps by folder name, whether it
        matches the code (None when the code's version can't be read) and
        the last publish's failure."""
        index = read_shelf(self.library_dir)
        entry = shelf_source(index, OFFICIAL_SOURCE)
        commit = entry["commit"] if entry else None
        return {
            "published_at": index.get("published_at") if entry else None,
            "commit": commit,
            "short": commit[:7] if commit else None,
            "apps": sorted(a for a in entry["apps"] if isinstance(a, str)) if entry else [],
            "current": (commit == head["commit"]) if head else None,
            "error": self.library_error,
        }

    def start_update_apps(self):
        """Update apps: the refusals here, so one is a 409 with nothing done;
        the rest (an op with a log) in the background."""
        self._take_lock()
        try:
            check = self.last_check()
            if not update_apps_offered(check):
                raise Refused("OpenFit itself changed too, so use Update OpenFit: it updates the apps as well."
                              if update_offered(check) else "There's no app update to apply. Check for updates first.")
            self._refuse_unless_ready()
            op = self._begin("update_apps")
        except OpError as e:
            self.lock.release()
            raise Refused(e.message, 500)
        except BaseException:
            self.lock.release()
            raise
        self._spawn(op, self._update_apps)
        return self.current_op()

    def _update_apps(self, op):
        """Pull, check that only apps (and docs and the rest) arrived, publish.
        No container is stopped, recreated or restarted; a step that fails
        puts the code back, with the shelf as it was."""
        try:
            self.say(op, "Noting the version you're on now…")
            old = self.head()
            op["old"] = old
        except OpError as e:
            self.say(op, e.message, e.detail)
            return self._finish(op, "failed", "Nothing was changed.")

        self.say(op, "Pulling the new apps…")
        rc, out, err = self.git("merge", "--ff-only", f"origin/{self.settings['branch']}")
        if rc != 0:
            self.say(op, "The new code can't be added on top of the server's copy (not a fast-forward).", _tail(out, err))
            return self._finish(op, "failed", "Nothing was changed.")
        try:
            op["new"] = self.head()
            arrived = self._git_out("diff", "--name-only", old["commit"], "HEAD", what="list what arrived").splitlines()
        except OpError as e:
            self.say(op, e.message, e.detail)
            self._reset(op, old)
            return self._finish(op, "failed", "Nothing was changed.")
        core = [p for p in arrived if group_of(p) in ("app", "admin")]
        if core:
            self.say(op, "OpenFit itself changed on GitHub since the last check: " + ", ".join(core[:10])
                     + (" and more" if len(core) > 10 else "") + ".")
            self._reset(op, old)
            return self._finish(op, "failed", "OpenFit itself changed since the last check. Check again, then use Update OpenFit.")

        if not self._publish_step(op, op["new"]["commit"]):
            self._reset(op, old)
            return self._finish(op, "failed", "The app library couldn't be updated, so the code was put back. "
                                              "The app library is as it was.")
        self.refresh_check()
        return self._finish(op, "ok", f"Updated the app library to {op['new']['short']}.")

    # --- operations and their record ---------------------------------------

    def _iso(self):
        return iso(self.clock())

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

    def _mark_interrupted(self, keep=None):
        """Every op still "running" at startup, but `keep` (one the helper
        still has), is one the Admin stopped in the middle of."""
        ops = self.history()
        changed = False
        for o in ops:
            if o.get("outcome") == "running" and o.get("id") != keep:
                o["outcome"] = "interrupted"
                o["summary"] = "The Admin stopped while this was running."
                changed = True
        if changed:
            _write_json(self.history_path, ops)

    def current_op(self):
        """The running operation, else the last one, else None."""
        self._settle()
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

    def _begin(self, kind, **extra):
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
            "users": None,
            "undoes": None,
            **extra,
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

    def _spawn(self, op, step, *args):
        def body():
            handed_over = False
            try:
                handed_over = step(op, *args) is HANDED_OVER
            except Exception as e:  # a bug, not a failed step: say so, never leave it "running"
                self._finish(op, "failed", f"Stopped on an unexpected error: {e}")
            finally:
                if not handed_over:  # else the op and the lock are _settle's
                    with self._op_lock:
                        self.op = None
                    self.lock.release()

        self.thread = threading.Thread(target=body, name=f"admin-{op['kind']}", daemon=True)
        self.thread.start()

    def wait(self, timeout=None):
        """Wait for the running operation's thread (for tests and the CLI)."""
        if self.thread is not None:
            self.thread.join(timeout)

    def _take_lock(self):
        self._settle()
        if not self.lock.acquire(blocking=False):
            raise Busy()

    # --- backups ----------------------------------------------------------

    def list_backups(self):
        """The Admin's backups, newest first: [{name, size, created, user}].
        `created` is the set's stamp; a backup with no user in its name (an
        A1 one) is the primary's."""
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
            found.append({"name": name, "size": os.path.getsize(path), "created": when,
                          "user": m.group(3) or self.settings["user"]})
        found.sort(key=lambda b: b["name"])
        return sorted(found, key=lambda b: b["created"], reverse=True)  # each set by name

    def prune_backups(self):
        """Keep the newest KEEP_BACKUPS sets (an update's backups share one
        stamp), and delete every file of an older set."""
        backups = self.list_backups()
        keep = sorted({b["created"] for b in backups}, reverse=True)[:KEEP_BACKUPS]
        for b in backups:
            if b["created"] not in keep:
                os.remove(os.path.join(self.backups_dir, b["name"]))

    def _helper(self, *args):
        """A container of the Admin's own image, labelled as its helper and --rm."""
        return self.docker("run", "--rm", "--label", HELPER_LABEL, *args)

    def _backup_path(self, name):
        """A backup in the Admin's own listing, by the name in a URL - its
        path - else a 404."""
        if not isinstance(name, str) or not BACKUP_NAME.match(name) or name not in {
                b["name"] for b in self.list_backups()}:
            raise Refused("There's no backup by that name.", 404)
        return os.path.join(self.backups_dir, name)

    def undo_relies_on(self, undo):
        """The backups the Undo on offer (undo_candidate()'s update) would put
        back: deleting any of them takes the Undo away."""
        return {r.get("backup") for r in (self._op_users(undo) or []) if r.get("backup")} if undo else set()

    def delete_backup(self, name):
        """One backup deleted, under the lock: only a name in the Admin's own
        listing (404), never while an operation runs (409)."""
        self._backup_path(name)
        self._take_lock()
        try:
            path = self._backup_path(name)
            try:
                os.remove(path)
            except OSError as e:
                raise OpError("The backup wasn't deleted.", str(e))
            print(f"[admin] deleted the backup {name}", flush=True)
        finally:
            self.lock.release()

    @staticmethod
    def _download(filename, chunks):
        """A download's chunks, and one line on the Admin's log once it has
        all gone - the file's name, never anything in it. A failure part-way
        is logged and raised, so the browser's download is cut off, never
        finished short."""
        try:
            yield from chunks
        except GeneratorExit:
            print(f"[admin] The download of {filename} stopped: the browser went away.", flush=True)
            raise
        except OpError as e:
            print(f"[admin] The download of {filename} stopped part-way: {e.message} {e.detail}".rstrip(), flush=True)
            raise
        print(f"[admin] Downloaded {filename} (no password)", flush=True)

    def download_backup(self, name):
        """A backup to download, without its password: (filename, chunks) -
        `<backup>-nopassword.tar.gz`, a copy of its archive with auth.json
        and .session_key left out (without_password()). Only a name in the
        Admin's own listing (404); never while an operation runs (409), so
        it never races an update's backup or prune. The backup is opened, and
        its first chunk read, under the lock; the rest streams without it (an
        open file is read whole, even if it's pruned meanwhile)."""
        self._backup_path(name)
        self._take_lock()
        try:
            path = self._backup_path(name)
            try:
                src = open(path, "rb")
            except OSError as e:
                raise OpError("The backup couldn't be read.", str(e))
            filename = name.removesuffix(".tar.gz") + "-nopassword.tar.gz"
            chunks = primed(self._download(filename, without_password(src)))
        finally:
            self.lock.release()
        return filename, chunks

    def download_kept(self, name):
        """A removed user's kept data to download, without its password:
        (filename, chunks) - `openfit-<name>-kept-<UTC date>-nopassword.tar.gz`,
        streamed from a helper reading their volume read-only, the password's
        two files left out (kept_download_args()). Only a name in users.json's
        removed list (404), with no container left in their project and no
        operation running (409). The helper is started, and its first chunk
        read, under the lock; the rest streams without it."""
        self._known(name, removed=True)
        self._take_lock()
        try:
            entry = self._known(name, removed=True)
            volume = entry["volume"]
            if volume != user_volume(name) or not USER_VOLUME.match(volume):
                raise OpError("Their kept data isn't stored under the name the Admin expects, so it can't be "
                              "downloaded.")
            rc, out, err = self.docker(
                "ps", "-a", "--no-trunc", "--filter", f"label=com.docker.compose.project={user_project(name)}",
                "--format", "{{.ID}}",
            )
            if rc != 0:
                raise OpError(f"Couldn't ask Docker about {name}'s containers.", _tail(out, err))
            if out.strip():
                raise Refused(f"{name} has a container again, so their kept data can't be downloaded now.", 409)
            day = datetime.fromtimestamp(self.clock(), timezone.utc).strftime("%Y-%m-%d")
            filename = f"openfit-{name}-kept-{day}-nopassword.tar.gz"
            args = kept_download_args(volume, self.admin_image)

            def read():
                with self.runner.stream(args) as stdout:
                    while True:
                        chunk = stdout.read1(CHUNK)
                        if not chunk:
                            break
                        yield chunk

            chunks = primed(self._download(filename, read()))
        finally:
            self.lock.release()
        return filename, chunks

    # --- update -----------------------------------------------------------

    def start_update(self):
        """Step 1 here, so a refusal is a 409; the rest in the background."""
        self._take_lock()
        try:
            check = self.last_check()
            if not update_offered(check):
                raise Refused("Only the app library changed, so there's nothing in OpenFit to update: use Update apps."
                              if update_apps_offered(check) else "There's no update to apply. Check for updates first.")
            self._refuse_unless_ready()
            try:
                users = self.users()  # the update's users, read before anything changes
            except ValueError as e:
                raise Refused(f"{e}. Nothing was changed.", 500)
            self._shelf_ready()
            op = self._begin("update")
        except OpError as e:
            self.lock.release()
            raise Refused(e.message, 500)
        except BaseException:
            self.lock.release()
            raise
        self._spawn(op, self._update, users)
        return self.current_op()

    def _refuse_unless_ready(self):
        """Refused unless the server's copy of OpenFit is clean and on the
        branch it updates: the code is only ever moved by a fast-forward."""
        dirty = self.dirty_files()
        if dirty:
            raise Refused(f"The server's copy of OpenFit has local changes: {', '.join(dirty)}. Nothing was changed.")
        branch = self._git_out("rev-parse", "--abbrev-ref", "HEAD", what="read the branch").strip()
        if branch != self.settings["branch"]:
            raise Refused(
                f"The server's copy of OpenFit is on '{branch}', not '{self.settings['branch']}'. Nothing was changed."
            )

    def _update(self, op, users):
        primary = users[0]
        try:
            self.say(op, "Noting the version you're on now…")
            old = self.head()
            op["old"] = old
            container = self._container_or_fail(primary)
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

        # One build, for everyone: an added user's compose file names the
        # image the primary's project builds.
        self.say(op, "Building the new version. OpenFit keeps running meanwhile…")
        rc, out, err = self.compose(primary, "build", primary.service)
        if rc != 0:
            self.say(op, "The new version didn't build.", _tail(out, err))
            self._reset(op, old)
            return self._finish(op, "failed", "The build failed, so nothing was changed. OpenFit kept running.")

        # Every user, primary first: who's running, so each comes back as it was.
        targets = []
        try:
            for user in users:
                c = self.find_container(user)
                if c is None:
                    self.say(op, f"{user.name} has no OpenFit container, so they're skipped.")
                rec = {"name": user.name, "created_at": user.created_at,
                       "was_running": bool(c) and c["state"] == "running", "backup": None}
                targets.append((user, c, rec))
        except OpError as e:
            self.say(op, e.message, e.detail)
            self._back_out(op, old, [])
            return self._finish(op, "failed", "The update was called off before anyone was stopped. Everyone is on the old version.")
        with self._op_lock:
            op["users"] = [rec for _user, _c, rec in targets]

        for user, _c, rec in targets:
            if not rec["was_running"]:
                continue
            self.say(op, f"Stopping {user.name}'s OpenFit…")
            rc, out, err = self.compose(user, "stop", user.service)
            if rc != 0:
                self.say(op, f"{user.name}'s OpenFit didn't stop.", _tail(out, err))
                self._back_out(op, old, targets)
                return self._finish(op, "failed", "An OpenFit didn't stop, so the update was called off. Everyone is on the old version.")

        # One stamp for the whole update: its backups are one set.
        stamp = self._stamp()
        for user, c, rec in targets:
            if c is None:
                continue
            self.say(op, f"Backing up {user.name}'s data…")
            name = f"{stamp}-{old['short']}-{user.name}.tar.gz"
            try:
                volume = self.data_volume(self.inspect(c["id"]))
                rc, out, err = self._helper(
                    "-v", f"{volume}:/v:ro", "-v", f"{self.admin_volume}:/b", self.admin_image,
                    "tar", "czf", f"/b/backups/{name}", "-C", "/v", ".",
                )
                if rc != 0:
                    raise OpError(f"{user.name}'s backup failed.", _tail(out, err))
            except OpError as e:
                self.say(op, e.message, e.detail)
                self._drop_backups(op, [r["backup"] for _u, _c, r in targets if r["backup"]] + [name])
                self._back_out(op, old, targets)
                return self._finish(op, "failed", "The data couldn't be backed up, so the update was called off. Everyone is on the old version.")
            with self._op_lock:
                rec["backup"] = name

        problem = False
        for user, c, rec in targets:
            if c is None:
                continue
            self.say(op, f"Putting {user.name} on the new version…")
            rc, out, err = self.compose(user, *self._recreate(user, rec["was_running"]))
            if rc != 0:
                self.say(op, f"{user.name}'s OpenFit didn't come up on the new version.", _tail(out, err))
                problem = True
                break
        if not problem:
            for user, _c, rec in targets:
                if not rec["was_running"]:
                    continue
                self.say(op, f"Checking {user.name}'s OpenFit is answering…")
                if not self.healthy(user):
                    self.say(op, f"{user.name}'s OpenFit didn't answer.")
                    problem = True
                    break
        if not problem:
            # The apps the new code brings. A failure here doesn't undo the
            # update: the shelf stays as it was, and Publish again retries.
            if not self._publish_step(op, op["new"]["commit"]):
                self.say(op, "The update itself is done. Use Publish again, under Repos, to try the app library "
                         "again.")
            self.prune_backups()
            self.refresh_check()
            return self._finish(op, "ok", f"Updated OpenFit to {op['new']['short']}: {op['new']['subject']}")

        self.say(op, "The new version didn't come up. Rolling everyone back…")
        rolled_back = self._undo_steps(op, old, [(user, rec) for user, _c, rec in targets])
        self._republish(op)
        if rolled_back:
            self.refresh_check()
            return self._finish(op, "rolled_back", "Updated, didn't come up, rolled back.")
        return self._finish(op, "failed", "Updated, didn't come up, and the rollback didn't come up either. See the log above.")

    @staticmethod
    def _recreate(user, was_running):
        """Onto the image :latest names now, as it was: running, or created
        and left stopped - so a stopped user is on it the next time they're
        started."""
        if was_running:
            return ("up", "-d", "--no-build", "--force-recreate", user.service)
        return ("up", "--no-start", "--no-build", "--force-recreate", user.service)

    def _drop_backups(self, op, names):
        """The backups an update called off had taken: its partial set."""
        for name in names:
            try:
                os.remove(os.path.join(self.backups_dir, name))
            except OSError:
                pass
        with self._op_lock:
            for rec in op["users"] or []:
                rec["backup"] = None

    def _reset(self, op, old):
        self.say(op, "Putting the code back as it was…")
        rc, out, err = self.git("reset", "--hard", old["commit"])
        if rc != 0:
            self.say(op, "Couldn't put the code back.", _tail(out, err))

    def _back_out(self, op, old, targets):
        """Called off after the build: old code, old image, and everyone who
        was running started again on it."""
        self._reset(op, old)
        rc, out, err = self.docker("tag", f"{self.image}:prev", f"{self.image}:latest")
        if rc != 0:
            self.say(op, "Couldn't switch back to the previous image.", _tail(out, err))
        for user, _c, rec in targets:
            if not rec["was_running"]:
                continue
            self.say(op, f"Starting {user.name}'s OpenFit again on the old version…")
            rc, out, err = self.compose(user, "up", "-d", "--no-build", "--force-recreate", user.service)
            if rc != 0:
                self.say(op, f"{user.name}'s OpenFit didn't start again.", _tail(out, err))

    # --- undo -------------------------------------------------------------

    def _op_users(self, op):
        """An update's record of its users; an A1 update's is the primary
        alone, running, with its one backup."""
        if isinstance(op.get("users"), list):
            return op["users"]
        if op.get("backup"):
            return [{"name": self.settings["user"], "created_at": None, "was_running": True, "backup": op["backup"]}]
        return None

    def _targets(self, records):
        """[(user, record)] for an update's records, if its users are exactly
        today's - nobody added or removed since (a name removed and added
        again is someone new) - else None."""
        users = self.users()
        recorded = {(r.get("name"), r.get("created_at")) for r in records or []}
        if len(recorded) != len(records or []) or recorded != {(u.name, u.created_at) for u in users}:
            return None
        by_name = {u.name: u for u in users}
        return [(by_name[r["name"]], r) for r in records]

    def undo_candidate(self, head=None):
        """The update Undo would reverse: the last update or undo, if it's a
        successful update whose every backup is still here, its users are
        exactly today's, and the repo is still on the version it updated to."""
        for o in reversed(self.history()):
            if o.get("kind") not in ("update", "undo"):
                continue
            if o["kind"] != "update" or o.get("outcome") != "ok":
                return None
            if not (o.get("old") and o.get("new")):
                return None
            records = self._op_users(o)
            listed = {b["name"] for b in self.list_backups()}
            if not records or any(r.get("backup") not in listed for r in records):
                return None
            if head is not None and head.get("commit") != o["new"].get("commit"):
                return None
            if self._targets(records) is None:
                return None
            return o
        return None

    def start_undo(self):
        self._take_lock()
        try:
            try:
                head = self.head()
            except OpError as e:
                raise Refused(e.message, 500)
            last = self.undo_candidate(head)
            if last is None:
                raise Refused("There's no update to undo.")
            self._shelf_ready()
            op = self._begin("undo")
            with self._op_lock:
                op.update(old=last["old"], new=last["new"], users=self._op_users(last), undoes=last["id"])
            self._save_op(op)
        except BaseException:
            self.lock.release()
            raise
        self._spawn(op, self._undo)
        return self.current_op()

    def _undo(self, op):
        targets = self._targets(op["users"])
        if targets is None:
            self.say(op, "The users have changed since the update, so nothing was put back.")
            return self._finish(op, "failed", "The undo didn't finish. See the log above.")
        undone = self._undo_steps(op, op["old"], targets)
        self._republish(op)
        if undone:
            self.refresh_check()
            return self._finish(op, "ok", f"Back on {op['old']['short']}, with everyone's data as it was before the update.")
        return self._finish(op, "failed", "The undo didn't finish. See the log above.")

    def _undo_steps(self, op, old, targets):
        """Every recorded user stopped, each one's data put back from their
        own backup, :prev retagged as :latest, the code reset, each user
        recreated as they were (running or not), and the running ones
        health-checked. True if everyone who was running came back up.

        A user the update skipped (no container, so no backup) is left as
        they are. A restore that fails stops it there, with everyone left
        stopped and on the new code: the log says who is where."""
        listed = {b["name"] for b in self.list_backups()}
        todo = [(u, r) for u, r in targets if r.get("backup")]
        for _u, r in todo:
            if not BACKUP_NAME.match(r["backup"]) or r["backup"] not in listed:
                self.say(op, f"The backup {r['backup']!r} isn't in the Admin's backups, so nothing was put back.")
                return False
        if not todo:
            self.say(op, "There are no backups to put back, so nothing was.")
            return False
        for u, r in targets:
            if not r.get("backup"):
                self.say(op, f"{u.name} had no OpenFit container at the update, so nothing of theirs is put back.")

        for i, (u, _r) in enumerate(todo):
            self.say(op, f"Stopping {u.name}'s OpenFit…")
            rc, out, err = self.compose(u, "stop", u.service)
            if rc != 0:
                self.say(op, f"{u.name}'s OpenFit didn't stop, so nobody's data was touched.", _tail(out, err))
                self.say(op, "Where everyone is: " + "; ".join(
                    [f"{v.name}: stopped, on the new version" for v, _ in todo[:i]]
                    + [f"{u.name}: didn't stop, on the new version"]
                    + [f"{v.name}: not touched, on the new version" for v, _ in todo[i + 1:]]
                ) + ".")
                return False

        for i, (u, r) in enumerate(todo):
            self.say(op, f"Putting {u.name}'s data back as it was before the update…")
            try:
                volume = self.data_volume(self.inspect(self._container_or_fail(u)["id"]))
                rc, out, err = self._helper(
                    "-v", f"{volume}:/v", "-v", f"{self.admin_volume}:/b:ro", self.admin_image,
                    "sh", "-c", f"find /v -mindepth 1 -delete && tar xzf /b/backups/{r['backup']} -C /v",
                )
                if rc != 0:
                    raise OpError(f"{u.name}'s data couldn't be put back.", _tail(out, err))
            except OpError as e:
                self.say(op, e.message, e.detail)
                self.say(op, "Stopped there, so everyone is left stopped, and the code and image are still "
                         "the new version. Where everyone is: " + "; ".join(
                             [f"{v.name}: data put back, stopped" for v, _ in todo[:i]]
                             + [f"{u.name}: data not put back (the backup is {r['backup']}), stopped"]
                             + [f"{v.name}: data not put back yet, stopped" for v, _ in todo[i + 1:]]
                         ) + ".")
                return False

        self.say(op, "Switching back to the previous version…")
        rc, out, err = self.docker("tag", f"{self.image}:prev", f"{self.image}:latest")
        if rc != 0:
            self.say(op, "Couldn't switch back to the previous image.", _tail(out, err))
        self._reset(op, old)
        ok = True
        for u, r in todo:
            self.say(op, f"Putting {u.name} back on the previous version…")
            rc, out, err = self.compose(u, *self._recreate(u, r["was_running"]))
            if rc != 0:
                self.say(op, f"{u.name}'s OpenFit didn't come up on the previous version.", _tail(out, err))
                ok = False
        for u, r in todo:
            if r["was_running"] and ok:
                self.say(op, f"Checking {u.name}'s OpenFit is answering…")
                if not self.healthy(u):
                    self.say(op, f"{u.name}'s OpenFit didn't answer.")
                    ok = False
        return ok

    # --- adding, starting, stopping and removing users -------------------------

    def _check_new_user(self, name, port):
        """400 for a name or port that can't be a user's, 409 for one taken."""
        users = self.users()
        problem = name_problem(name, self.settings)
        if problem:
            raise Refused(problem, 400)
        if any(u.name == name for u in users):
            raise Refused(f"There's already a user called {name}.", 409)
        problem = port_problem(port)
        if problem:
            raise Refused(problem, 400)
        holder = next((u for u in users if u.port == port), None)
        if holder:
            raise Refused(f"Port {port} is {holder.name}'s. Choose another.", 409)

    def _image_built(self):
        rc, out, err = self.docker("image", "inspect", "--format", "{{.Id}}", f"{self.image}:latest")
        if rc == 0:
            return
        if "no such image" in f"{out}\n{err}".lower():
            raise Refused("OpenFit hasn't been built on this server yet.", 409)
        raise OpError("Couldn't ask Docker about OpenFit's image.", _tail(out, err))

    def _volume_exists(self, volume, what="the user's storage"):
        rc, out, err = self.docker("volume", "inspect", "--format", "{{.Name}}", volume)
        if rc == 0:
            return True
        if "no such volume" in f"{out}\n{err}".lower():
            return False
        raise OpError(f"Couldn't ask Docker about {what}.", _tail(out, err))

    def _shelf_ready(self):
        """Before anything that recreates a user: the app shelf's volume is
        there, else Refused. Every user's compose files name it as external,
        so a user recreated without it wouldn't start - and it's missing
        only if the Admin wasn't recreated from its new compose file."""
        try:
            there = self._volume_exists(LIBRARY_VOLUME, "the app library's storage")
        except OpError as e:
            raise Refused(f"{e.message} Nothing was changed.", 500)
        if not there:
            raise Refused(VOLUME_MISSING, 409)

    def start_add_user(self, name, port, host=None):
        """Checks and the lock here, so a refusal is a 400 or 409 with
        nothing done; the rest (an op with a log) in the background."""
        self._check_new_user(name, port)
        self._take_lock()
        try:
            self._check_new_user(name, port)  # again: another may have finished meanwhile
            kept = any(r["name"] == name for r in self.registry()["removed"])
            self._image_built()
            self._shelf_ready()
            # A volume the Admin has no record of - left from before a lost
            # users.json, say - is never adopted (it's someone's data) and
            # never at risk from the failure path's `down -v` below.
            if not kept and self._volume_exists(user_volume(name)):
                raise Refused(
                    f"There's already storage called {user_volume(name)} on the server that the Admin "
                    "has no record of, so it won't use it. Choose another name.", 409)
            op = self._begin("add_user", user={"name": name, "port": port})
        except OpError as e:
            self.lock.release()
            raise Refused(e.message, 500)
        except BaseException:
            self.lock.release()
            raise
        self._spawn(op, self._add_user, self.added(name, port), kept, address(host, port))
        return self.current_op()

    def _add_user(self, op, user, kept, where):
        if kept:
            self.say(op, f"{user.name} was removed before. Their kept data is reattached.")
        folder = os.path.dirname(user.compose_file)
        self.say(op, f"Writing {user.name}'s compose file…")
        try:
            os.makedirs(folder, exist_ok=True)
            with open(user.compose_file, "w", encoding="utf-8") as f:
                f.write(self._compose_text(user))
        except OSError as e:
            self.say(op, "Couldn't write their compose file.", str(e))
            shutil.rmtree(folder, ignore_errors=True)
            return self._finish(op, "failed", f"{user.name} wasn't added. Nothing was changed.")

        self.say(op, f"Starting {user.name}'s OpenFit…")
        rc, out, err = self.compose(user, "up", "-d", "--no-build")
        if rc != 0:
            clash = port_clash(user.port, out, err)
            self.say(op, clash or "Their OpenFit didn't start.", _tail(out, err))
            self._take_back(op, user, kept)
            return self._finish(op, "failed", clash or f"{user.name} wasn't added: their OpenFit didn't start.")
        self.say(op, f"Checking {user.name}'s OpenFit is answering…")
        if not self.healthy(user):
            self.say(op, "Their OpenFit didn't answer.")
            self._take_back(op, user, kept)
            return self._finish(op, "failed", f"{user.name} wasn't added: their OpenFit didn't answer.")

        reg = self.registry()
        reg["users"].append({"name": user.name, "port": user.port, "created_at": self._iso()})
        reg["removed"] = [r for r in reg["removed"] if r["name"] != user.name]
        self._save_registry(reg)
        return self._finish(op, "ok", f"Added {user.name}. Their OpenFit is at {where}")

    def _take_back(self, op, user, kept):
        """A failed add, undone: the container gone, the compose file gone,
        users.json never touched."""
        self.say(op, f"Taking {user.name}'s OpenFit back down…")
        if kept:
            # Their kept volume stays, as it was before this add.
            rc, out, err = self.compose(user, "down")
        else:
            # The one place -v is ever used. Safe only because this project's
            # volume was made moments ago, in this op, by the `up` above:
            # start_add_user refused a name whose volume was already on the
            # server, so it holds nothing but what this failed OpenFit wrote
            # in the last minute, and it's nobody's data.
            rc, out, err = self.compose(user, "down", "-v")
        if rc != 0:
            self.say(op, "Their OpenFit didn't come down.", _tail(out, err))
        shutil.rmtree(os.path.dirname(user.compose_file), ignore_errors=True)

    def _known(self, name, removed=False):
        """An added or primary user (or, `removed`, a removed one's entry) by
        the name in a URL, or a 404."""
        if removed:
            found = next((r for r in self.registry()["removed"] if r["name"] == name), None)
            if found is None:
                raise Refused("There's no kept data for a user by that name.", 404)
            return found
        found = self.find_user(name)
        if found is None:
            raise Refused("There's no user by that name.", 404)
        return found

    def start_user(self, name):
        """compose up -d --no-build: recreated on the image :latest names now,
        if it has moved on since they were last started, or from compose
        files that have changed."""
        self._user_command(name, ("up", "-d", "--no-build"), "didn't start", recreates=True)

    def stop_user(self, name):
        self._user_command(name, ("stop",), "didn't stop")

    def restart_user(self, name):
        """compose up -d --no-build --force-recreate, then the health check:
        a container made anew from the user's compose files, on the image
        :latest names - how a user picks up the app shelf's mount."""
        self._user_command(name, ("up", "-d", "--no-build", "--force-recreate"), "didn't restart",
                           recreates=True, check_health=True)

    def setup_link(self, name, host):
        """A setup link for `name`'s OpenFit: {link, expires_at}. Its own
        CLI, `python auth_cli.py setup-link`, run in their running container
        by docker exec, makes the token (replacing any earlier one) and
        prints it; the link is http://<host>:<their port>/setup#<token>,
        `host` as the browser reached the Admin. Nothing is stored here,
        and the token is logged nowhere - not even in a failure's detail.
        409 unless their OpenFit is running."""
        self._known(name)
        self._take_lock()
        try:
            user = self._known(name)
            c = self.find_container(user)
            if c is None or c["state"] != "running":
                raise Refused(f"{user.name}'s OpenFit isn't running. Start it, then make the link.", 409)
            rc, out, err = self.docker("exec", c["id"], *SETUP_LINK_ARGS, timeout=60)
            if rc != 0:
                # Its error output alone: what it printed is never repeated.
                raise OpError(f"Couldn't make a setup link for {user.name}.", _tail("", err))
            try:
                made = json.loads(out)
            except ValueError:
                made = None
            token = made.get("token") if isinstance(made, dict) else None
            expires_at = made.get("expires_at") if isinstance(made, dict) else None
            if (len(out.strip().splitlines()) != 1 or not isinstance(token, str) or not SETUP_TOKEN.match(token)
                    or not isinstance(expires_at, str) or not _STAMP.match(expires_at)):
                raise OpError(f"{user.name}'s OpenFit didn't give back a setup link the Admin recognises.")
            print(f"[admin] Made a setup link for {user.name}.", flush=True)
            return {"link": f"{address(host, user.port)}/setup#{token}", "expires_at": expires_at}
        finally:
            self.lock.release()

    def _user_command(self, name, args, failed, recreates=False, check_health=False):
        self._known(name)
        self._take_lock()
        try:
            user = self._known(name)
            if recreates:
                self._shelf_ready()
            rc, out, err = self.compose(user, *args, user.service)
            if rc != 0:
                raise OpError(port_clash(user.port, out, err) or f"{user.name}'s OpenFit {failed}.", _tail(out, err))
            if check_health and not self.healthy(user):
                raise OpError(f"{user.name}'s OpenFit restarted, but didn't answer.")
            print(f"[admin] {' '.join(args)} {user.name}: done", flush=True)
        finally:
            self.lock.release()

    def remove_user(self, name):
        """compose down - never -v: the container goes, the volume stays, kept
        in users.json's removed list until its deletion is confirmed."""
        if self._known(name).primary:
            raise Refused("The primary user can't be removed.", 400)
        self._take_lock()
        try:
            user = self._known(name)
            rc, out, err = self.compose(user, "down")
            if rc != 0:
                raise OpError(f"{user.name}'s OpenFit didn't come down, so nothing was changed.", _tail(out, err))
            reg = self.registry()
            reg["users"] = [e for e in reg["users"] if e["name"] != user.name]
            reg["removed"].append({"name": user.name, "port": user.port, "removed_at": self._iso(),
                                   "volume": user_volume(user.name)})
            self._save_registry(reg)
            shutil.rmtree(os.path.dirname(user.compose_file), ignore_errors=True)
            print(f"[admin] removed {user.name}: their data is kept as {user_volume(user.name)}", flush=True)
        finally:
            self.lock.release()

    def delete_data(self, name, confirm):
        """docker volume rm, of a removed user's kept volume: only with their
        name typed as `confirm`, and only with no container left in their
        project."""
        if not isinstance(confirm, str) or confirm != name:
            raise Refused("Type their name exactly to delete their data.", 400)
        self._known(name, removed=True)
        self._take_lock()
        try:
            entry = self._known(name, removed=True)
            volume = entry["volume"]
            if volume != user_volume(name) or not USER_VOLUME.match(volume):
                raise OpError("Their kept data isn't stored under the name the Admin expects, so it wasn't deleted.")
            rc, out, err = self.docker(
                "ps", "-a", "--no-trunc", "--filter", f"label=com.docker.compose.project={user_project(name)}",
                "--format", "{{.ID}}",
            )
            if rc != 0:
                raise OpError(f"Couldn't ask Docker about {name}'s containers.", _tail(out, err))
            if out.strip():
                raise Refused(f"{name} still has a container, so their data wasn't deleted.", 409)
            rc, out, err = self.docker("volume", "rm", volume)
            if rc != 0 and "no such volume" not in f"{out}\n{err}".lower():
                raise OpError(f"{name}'s data wasn't deleted.", _tail(out, err))
            reg = self.registry()
            reg["removed"] = [r for r in reg["removed"] if r["name"] != name]
            self._save_registry(reg)
            print(f"[admin] deleted {name}'s kept data ({volume})", flush=True)
        finally:
            self.lock.release()

    # --- the page's view ----------------------------------------------------

    def server(self):
        """server_fields() of `docker info`, run with exactly
        DOCKER_INFO_ARGS at most once a DOCKER_INFO_EVERY seconds (its answer,
        or None if Docker didn't give one, kept meanwhile)."""
        now = self.clock()
        cached = self._server
        if cached is not None and 0 <= now - cached[0] < DOCKER_INFO_EVERY:
            return cached[1]
        rc, out, _err = self.docker(*DOCKER_INFO_ARGS, timeout=30)
        try:
            server = server_fields(json.loads(out)) if rc == 0 else None
        except ValueError:
            server = None
        self._server = (now, server)
        return server

    def system(self, users, views, backups):
        """The System information panel: the server (server()), the free
        space where the Admin keeps its data and backups, the backups' total
        size, how many of the Admin's users are running, stopped or not
        found, and when this Admin started."""
        try:
            st = os.statvfs(self.data_dir)
            disk_free = st.f_bavail * st.f_frsize
        except OSError:
            disk_free = None
        counts = {"running": 0, "stopped": 0, "missing": 0}
        for u in users:
            state = self.plain_state(views[u.name][0])
            counts[state if state in counts else "missing"] += 1
        return {"server": self.server(), "disk_free": disk_free,
                "backups_bytes": sum(b["size"] for b in backups), "users": counts,
                "admin_up_since": self.started_at}

    def _range(self, old, new, folder=None):
        """The commits from `old` to `new` (full commits; touching `folder`,
        if given) - {commits: [{sha, subject}], prs: [{number, url}]}, as a
        check lists them, merges left out and the pull requests read from
        their titles - or None if git can't say. Kept once read: a range
        between two commits never changes."""
        if not (isinstance(old, str) and _COMMIT.match(old) and isinstance(new, str) and _COMMIT.match(new)):
            return None
        key = (old, new, folder)
        if key in self._ranges:
            return self._ranges[key]
        span = f"{old}..{new}"
        paths = ("--", folder) if folder else ()
        try:
            commits = self._log("--no-merges", "--format=%h%x09%s", span, *paths)
            prs = []
            for merge in self._log("--merges", "--format=%H%x09%s", span):
                number = PR_TITLE.match(merge["subject"])
                if not number or not _COMMIT.match(merge["sha"]):
                    continue
                if folder:
                    brought = self._git_out("diff", "--name-only", f"{merge['sha']}^1", merge["sha"],
                                            what="read a pull request's files").split()
                    if not any(p.startswith(folder) for p in brought):
                        continue
                prs.append({"number": int(number.group(1)), "url": PR_URL.format(number.group(1))})
        except OpError:
            return None
        self._ranges[key] = {"commits": commits, "prs": prs}
        return self._ranges[key]

    def _op_view(self, op, ops, folder=None):
        """An update's record for the Versions panel: when, its outcome, the
        versions it went between and the commits it brought (_range(), of
        `folder` if given), and when it was undone - an Admin update,
        reverted - if it was."""
        if not op:
            return None
        old, new = _commit_of(op.get("old")), _commit_of(op.get("new"))
        undone = next((o.get("finished_at") for o in ops if o.get("kind") in ("undo", "admin_revert")
                       and o.get("undoes") == op.get("id") and o.get("outcome") == "ok"), None)
        return {
            **{k: op.get(k) for k in ("id", "kind", "outcome", "summary", "started_at", "finished_at")},
            "old": old[:7] if old else None,
            "new": new[:7] if new else None,
            "changes": self._range(old, new, folder) if old and new else None,
            "undone_at": undone,
        }

    def versions(self, head, check, au, ops):
        """The Versions panel: what the users and the Admin are on, when each
        was last updated (the newest ok op of its kind), whether an update is
        on offer, and the last update of each.

        The User version is HEAD; the Admin's the newest commit touching
        admin/ at or before HEAD - unless the running Admin's code isn't the
        repo's admin/ (au["pulled"]), when it's older than that, and said
        so. The Admin's `pending` are the changes its update would bring:
        those pulled since the code it was built from (_admin_base()), and
        any still on GitHub."""
        def newest(kind, ok=False):
            return next((o for o in reversed(ops) if o.get("kind") == kind
                         and (not ok or o.get("outcome") == "ok")), None)

        try:
            user = self._commit_info("HEAD") if head else None
            admin = self._commit_info("HEAD", "admin/") if head else None
        except OpError:
            user = admin = None
        updated, admin_updated = newest("update", ok=True), newest("admin_update", ok=True)
        last = newest("update")
        group = next((g for g in (check or {}).get("groups", []) if g["key"] == "admin"), None)
        pending = {"commits": [], "prs": []}
        base = self._admin_base() if au["pulled"] and head else None
        pulled = self._range(base, head["commit"], "admin/") if base else None
        for part in (pulled, group):
            if part:
                pending["commits"] += part["commits"]
                pending["prs"] += [pr for pr in part["prs"] if pr not in pending["prs"]]
        return {
            "user": {
                **(user or {"commit": None, "short": None, "date": None, "subject": None}),
                "last_updated": updated.get("finished_at") if updated else None,
                "update_available": update_offered(check),
                "last": self._op_view(last, ops),
                # What everyone is running now came with the last update that worked.
                "current": self._op_view(updated, ops),
            },
            "admin": {
                **(admin or {"commit": None, "short": None, "date": None, "subject": None}),
                "older": au["pulled"],
                "last_updated": admin_updated.get("finished_at") if admin_updated else None,
                "update_available": au["available"],
                "can_self_update": au["can_self_update"],
                "command": au["command"],
                "pending": pending,
                "last": self._op_view(newest("admin_update"), ops, "admin/"),
            },
        }

    def repos(self, library, check):
        """The shelf's sources, from its index.json, for the Repos panel and
        the Repo tab: OpenFit first, then the rest by name. Each {source,
        name, description, official, commit, short, published_at, apps:
        [{id, name, description}]} (the apps by name, then id) - names
        falling back to ids where the index has none - and for OpenFit,
        whether its shelf matches the code, the last publish's failure, and
        which update would bring its waiting app changes: "apps" (Update
        apps), "openfit" (they come with Update OpenFit) or None. With
        nothing published yet, OpenFit is listed all the same, empty."""
        index = read_shelf(self.library_dir) or {"sources": {}}
        published_at = index.get("published_at") if isinstance(index.get("published_at"), str) else None

        def text(value, most):
            return value.strip() if isinstance(value, str) and 0 < len(value.strip()) <= most else None

        found = []
        for source in index["sources"]:
            entry = shelf_source(index, source)
            if entry is None or not isinstance(source, str) or not _SHELF_SOURCE.match(source):
                continue
            apps = []
            for app_id, meta in entry["apps"].items():
                if not isinstance(app_id, str) or not _SHELF_APP.match(app_id):
                    continue
                meta = meta if isinstance(meta, dict) else {}
                apps.append({"id": app_id, "name": text(meta.get("name"), 200) or app_id,
                             "description": text(meta.get("description"), MAX_DESCRIPTION)})
            apps.sort(key=lambda a: (a["name"].casefold(), a["id"]))
            found.append({"source": source, "name": text(entry.get("name"), MAX_REPO_NAME) or source,
                          "description": text(entry.get("description"), MAX_DESCRIPTION),
                          "official": source == OFFICIAL_SOURCE, "commit": entry["commit"],
                          "short": entry["commit"][:7], "published_at": published_at, "apps": apps,
                          "current": None, "error": None, "update": None})
        if not any(r["official"] for r in found):
            found.append({"source": OFFICIAL_SOURCE, "name": OFFICIAL_SOURCE, "description": None, "official": True,
                          "commit": None, "short": None, "published_at": None, "apps": [],
                          "current": None, "error": None, "update": None})
        official = next(r for r in found if r["official"])
        official.update(current=library["current"], error=library["error"],
                        update="apps" if update_apps_offered(check)
                        else "openfit" if update_offered(check) and _changed(check, "library") else None)
        return sorted(found, key=lambda r: (not r["official"], r["name"].casefold(), r["source"]))

    @staticmethod
    def _backup_update(backup, ops):
        """The update a backup was taken by, from the history: {id, old, new,
        started_at, outcome} (old and new short), or None once it's gone
        from the history."""
        for o in ops:
            if o.get("kind") != "update":
                continue
            records = o.get("users") if isinstance(o.get("users"), list) else []
            if o.get("backup") == backup["name"] or any(
                    isinstance(r, dict) and r.get("backup") == backup["name"] for r in records):
                old, new = _commit_of(o.get("old")), _commit_of(o.get("new"))
                return {"id": o.get("id"), "old": old[:7] if old else None, "new": new[:7] if new else None,
                        "started_at": o.get("started_at"), "outcome": o.get("outcome")}
        return None

    def status(self):
        self._settle()
        s = self.settings
        try:
            head = self.head()
        except OpError:
            head = None
        reg = self.registry()
        users = self.users(reg)
        views = {u.name: self.container_view(u) for u in users}
        raw = views[users[0].name][0]
        check = self.last_check()
        undo = self.undo_candidate(head) if head else None
        ops = self.history()
        op = self.current_op()
        recent = [{k: o.get(k) for k in ("id", "kind", "outcome", "summary", "started_at", "finished_at")}
                  for o in reversed(ops[-5:])]
        backups = self.list_backups()
        relied = self.undo_relies_on(undo)
        for b in backups:
            b["update"] = self._backup_update(b, ops)
            b["undo_relies"] = b["name"] in relied
        au = self.admin_update(check)
        library = self.library_status(head)
        return {
            "openfit": {
                "user": s["user"],
                "port": s["port"],
                "state": raw,
                "running": raw == "running",
                "version": {"sha": head["short"], "subject": head["subject"]} if head else None,
            },
            "users": [{"name": u.name, "port": u.port, "primary": u.primary,
                       "state": self.plain_state(views[u.name][0]), "library": views[u.name][1]}
                      for u in users],
            "removed": [{"name": r["name"], "removed_at": r["removed_at"]} for r in reg["removed"]],
            "next_port": self.next_port(users),
            "check": check,
            "update_available": update_offered(check),
            "update_apps_available": update_apps_offered(check),
            "library": library,
            "undo": {"id": undo["id"], "old": undo["old"], "new": undo["new"]} if undo else None,
            "admin_update": au,
            "admin_revert": self.revert_offer(busy=bool(op and op.get("outcome") == "running"), ops=ops),
            "op": op,
            "history": recent,
            "backups": backups,
            # The portal's (S1): the left column's panels, and the Repo tab.
            "system": self.system(users, views, backups),
            "versions": self.versions(head, check, au, ops),
            "repos": self.repos(library, check),
            # Every op the history keeps, newest first, each with its log.
            "activity": [{k: o.get(k) for k in ("id", "kind", "outcome", "summary", "started_at", "finished_at",
                                                 "log", "user")} for o in reversed(ops)],
        }
