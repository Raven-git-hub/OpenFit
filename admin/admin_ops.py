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
users.json, when its deletion is confirmed. Nothing else on the server is
listed, inspected, stopped or removed.

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

import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
import shutil
import threading
import time
from datetime import datetime, timezone
from typing import NamedTuple
from urllib.parse import urlsplit

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

# An Admin update's settings snapshot, /data/self-update/<stamp>/, and its op.
_FILE_STAMP = re.compile(r"\d{8}T\d{6}Z\Z")
SWAP_OP_ID = re.compile(r"\d{8}T\d{6}Z-admin_update\Z")

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
ADMIN_PREV = "openfit-admin:prev"
SWAP_NAME = "openfit-admin-swap"
SWAP_RESULT = "self_update_result.json"
SWAP_OUTCOMES = ("ok", "rolled_back", "failed")
SNAPSHOTS = "self-update"
SNAPSHOT_FILES = ("admin.json", "users.json", "auth.json", "secret_key", "history.json", "last_check.json")
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


def read_shelf(library_dir):
    """/library/index.json as published - {published_at, sources: {source:
    {commit, apps: {app: {sha256}}}}} - or None if there's none, or it
    doesn't read."""
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


def _write_index(root, index, source, commit, apps, now):
    """index.json with `source`'s entry, any other source's kept: a temp
    file, then renamed over it."""
    sources = dict(index["sources"]) if index else {}
    sources[source] = {"commit": commit, "apps": apps}
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
    {sha256}}, changed}.

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
            apps = {app: {"sha256": fingerprint(os.path.join(final, app))}
                    for app in sorted(os.listdir(final)) if _SHELF_APP.match(app)}
            if shelf_source(index, source) == {"commit": commit, "apps": apps}:
                return {"commit": commit, "apps": apps, "changed": False}
            _write_index(root, index, source, commit, apps, now)
            return {"commit": commit, "apps": apps, "changed": True}
        apps = _collect_apps(src, log)
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
        published = {app: {"sha256": fingerprint(os.path.join(tmp, app))} for app, _files in apps}
        if os.path.lexists(final):
            aside = os.path.join(sets, f".old-{name}-{secrets.token_hex(4)}")
            os.rename(_inside(root, final), _inside(root, aside))
        os.rename(tmp, _inside(root, final))
        placed = True
        _point(root, source, f"{SHELF_SETS}/{name}")
        swapped = True
        _write_index(root, index, source, commit, published, now)
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


class Admin:
    """The OpenFit users the Admin manages, and the operations on them.

    One operation at a time (`lock`): a check, an update, an undo, adding,
    starting, stopping, restarting or removing a user, making a setup link,
    deleting a removed user's data, updating the apps, publishing them
    again, or updating the Admin itself. An update, an undo, adding a user, updating the apps and
    an Admin update run in a background thread, their progress in `op`;
    the rest run in the caller's thread. An Admin update keeps the lock
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
            rc, out, err = self.docker("tag", image_id, ADMIN_PREV)
            if rc != 0:
                raise OpError("Couldn't keep a copy of the Admin's version.", _tail(out, err))
            stamp = self._stamp()
            self.say(op, "Keeping a copy of the Admin's settings…")
            self._snapshot(stamp)
        except OpError as e:
            self.say(op, e.message, e.detail)
            return self._finish(op, "failed", "Nothing was changed.")

        self.say(op, "Building the new Admin. This one keeps running meanwhile…")
        rc, out, err = admin_compose(self.runner, self.repo, "build", ADMIN_SERVICE)
        if rc != 0:
            self.say(op, "The new Admin didn't build.", _tail(out, err))
            return self._finish(op, "failed", "The build failed, so nothing was changed. The Admin kept running.")

        self.say(op, "Starting the helper that swaps the Admin over…")
        rc, out, err = self.docker(
            "run", "-d", "--rm", "--name", SWAP_NAME, "--label", HELPER_LABEL,
            "-v", "/var/run/docker.sock:/var/run/docker.sock",
            "-v", f"{self.repo}:{self.repo}",
            "-v", f"{self.admin_volume}:/data",
            "-e", f"OPENFIT_REPO={self.repo}",
            ADMIN_PREV,
            "python", "/app/admin_swap.py", stamp, op["id"],
        )
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

    def _snapshot(self, stamp):
        """The Admin's settings files, each if present, copied into
        /data/self-update/<stamp>/ for the helper to put back; the newest
        KEEP_SNAPSHOTS kept."""
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
                shutil.rmtree(os.path.join(root, old), ignore_errors=True)
        except OSError as e:
            raise OpError("Couldn't keep a copy of the Admin's settings.", str(e))

    def _pick_up_swap(self):
        """At startup, before _mark_interrupted: the helper's result, if an
        Admin update left one. Finished, it finishes its op. Still "running",
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
        op = next((o for o in self.history() if o.get("id") == result["op_id"] and o.get("kind") == "admin_update"), None)
        if op is None:
            print(f"[admin] removing the update helper's result: no Admin update {result['op_id']} to finish", flush=True)
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
        with self._op_lock:
            op["log"].extend({"t": line["t"], "text": line["text"]} for line in (result or {}).get("lines", []))
        if outcome == "ok":
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

    def shelf_commit(self):
        """The commit the shelf's openfit apps were published from, or None."""
        entry = shelf_source(read_shelf(self.library_dir), OFFICIAL_SOURCE)
        return entry["commit"] if entry else None

    def publish_at_startup(self):
        """serve()'s, before the server starts: publish if the shelf's index is
        missing, or its openfit commit isn't HEAD's - so the shelf appears by
        itself on first rollout, and catches up after anything that moved the
        code without publishing. Whether it published; never raises."""
        try:
            head = self.head()
        except OpError as e:
            print(f"[admin] app library: {e.message} {e.detail}", flush=True)
            return False
        if self.shelf_commit() == head["commit"]:
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
        """The page's App library card: when the shelf was published and from
        which commit, its apps by folder name, whether it matches the code
        (None when the code's version can't be read) and the last publish's
        failure."""
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
                self.say(op, "The update itself is done. Use Publish again, on the App library card, to try the "
                         "app library again.")
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
        recent = [{k: o.get(k) for k in ("id", "kind", "outcome", "summary", "started_at", "finished_at")}
                  for o in reversed(self.history()[-5:])]
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
            "library": self.library_status(head),
            "undo": {"id": undo["id"], "old": undo["old"], "new": undo["new"]} if undo else None,
            "admin_update": self.admin_update(check),
            "op": self.current_op(),
            "history": recent,
            "backups": self.list_backups(),
        }
