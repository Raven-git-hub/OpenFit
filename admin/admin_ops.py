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
users.json, when its deletion is confirmed. A restore (R1) fills a new
user's volume, made moments before by `up --no-start`, with a helper that
extracts only a clean archive the Admin wrote itself: the Admin reads an
archive with tarfile and never extracts one. Beside those, `docker info`,
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

Organisation repos (A6) are other GitHub repos of apps, each cloned into
the Admin's own storage, /data/repos/<slug>/repo, with a read-only deploy
key the Admin made for it (ssh-keygen, in one exact form), and published to
the shelf beside the official apps, under the source its repo.yaml names.
Their git runs only through org_git(): as root, in that clone, with the
repo's own pinned GIT_SSH_COMMAND, and only the commands org_git_allowed()
names. The private key is never read, returned or logged.

Git runs as the repo's owner (the uid and gid that own OPENFIT_REPO), so
the files it writes stay the owner's and git never refuses the repo as
someone else's - all but an organisation repo's, above. Docker runs as
root, for the socket.
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
import sqlite3
import tarfile
import threading
import time
import zlib
from datetime import datetime, timezone
from typing import NamedTuple
from urllib.parse import quote, urlsplit

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
# The only compose commands ever run on the Admin's own project: the
# Admin's build, and its helper's stop - before it puts settings back, so
# no Admin is running to write over them - and recreate.
ADMIN_COMPOSE_ARGS = (
    ("build", ADMIN_SERVICE),
    ("up", "-d", "--no-build", "--force-recreate", ADMIN_SERVICE),
    ("stop", ADMIN_SERVICE),
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
# Each app's change notes, for the What's new a user system shows when an
# installed copy updates itself (B3): the newest CHANGE_NOTES commits
# touching its folder, merges left out, by exactly this git log
# (change_notes_args()) - each {sha: its first 12 hex, subject: at most
# MAX_SUBJECT characters, date: the committer's, ISO 8601}.
CHANGE_NOTES = 10
CHANGES_FORMAT = "--format=%H%x09%cI%x09%s"
MAX_SUBJECT = 200
_CHANGE_DATE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:Z|[+-]\d\d:\d\d)\Z")

# Organisation repos (A6): another GitHub repo laid out as library/ is
# (repo.yaml and an app folder each, at its top), cloned over SSH with a
# read-only deploy key the Admin makes for it, and published to the shelf
# as the source its repo.yaml names. /data/repos.json lists them; each has
# /data/repos/<slug>/ - its key pair, deploy_key (0600, never read back)
# and deploy_key.pub, and its clone, repo/. github.com alone, over SSH
# alone: no HTTPS, no tokens.
REPOS_FILE = "repos.json"
REPOS_DIR = "repos"
REPO_KEY = "deploy_key"
REPO_CLONE = "repo"
REPO_STATES = ("pending", "connected")
REPO_OWNER = re.compile(r"[A-Za-z0-9-]{1,39}\Z")
REPO_NAME = re.compile(r"[A-Za-z0-9._-]{1,100}\Z")
REPO_SLUG = re.compile(r"[a-z0-9-]{1,39}__[a-z0-9._-]{1,100}\Z")
_GITHUB_HTTPS = re.compile(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?\Z")
_GITHUB_SSH = re.compile(r"git@github\.com:([^/]+)/([^/]+?)(?:\.git)?\Z")
# GitHub's host keys, copied into the image from admin/github_known_hosts.
KNOWN_HOSTS = "/app/github_known_hosts"
# The Admin's data folder as it goes into GIT_SSH_COMMAND, which git hands
# to a shell: plain path characters only.
_SAFE_PATH = re.compile(r"/[A-Za-z0-9._/-]*\Z")
PUBLIC_KEY = re.compile(r"ssh-ed25519 [A-Za-z0-9+/]{68}(?: [ -~]{1,300})?\Z")
NOT_OPENFIT = "Connected, but not an OpenFit repo yet: it needs a repo.yaml at the top."
KEY_REFUSED = "GitHub didn't accept the key yet. Check it's added under the repo's Deploy keys, then Connect again."
# What git and ssh say when GitHub turns a deploy key down: a key it
# doesn't know, or a key for another repo.
_KEY_REFUSALS = ("permission denied (publickey)", "repository not found")
# What git adds after any failure over SSH: never the reason.
_GIT_BOILERPLATE = ("fatal: could not read from remote repository", "please make sure you have the correct access",
                    "and the repository exists")
REPO_COMMITS = 20  # the most commits a never-published repo's update lists

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

# Restoring a backup as a new user (R1). Everything a restore works with -
# an uploaded file, a removed user's kept data packed into one, the database
# taken out to be checked, and the clean archive the helper extracts - is
# in /data/restore-uploads/, and deleted when the restore ends, however it
# ends (and at startup: anything there then is left from an Admin stopped
# part-way). The Admin never extracts an archive itself: it reads one with
# tarfile, checks every member, then writes a clean copy of the members it
# checked, and only a helper extracts that, into the new user's volume.
RESTORE_DIR = "restore-uploads"
RESTORE_FILE = re.compile(r"[0-9a-f]{32}\.tar\.gz\Z")  # an upload, or kept data packed
CLEAN_FILE = re.compile(r"[0-9a-f]{32}-clean\.tar\.gz\Z")  # the one name the helper extracts
MAX_MEMBERS = 200_000
MAX_UNPACKED = 8 * 1024 ** 3  # the sum of the members' sizes: an archive bomb stops here
MAX_PART = 255  # characters in one part of a member's name
DB_NAME = "tracker.db"
DB_WAL = "tracker.db-wal"  # recent writes, if the database wasn't closed cleanly
# The user system's own rule for a migration's file name (app/migrations).
MIGRATION_FILE = re.compile(r"^(\d+)_[\w-]+\.sql$")
NEWER_BACKUP = "This backup is from a newer OpenFit than this server runs. Update OpenFit first."
# Never restored: the password and its sessions' key, so a restored user
# always starts locked (a setup link sets their password), and the library
# apps' update notes, which start afresh with them.
NEVER_RESTORED = ("auth.json", ".session_key", "app_updates.json")
# Left out too unless the device logins are kept: the key that unlocks the
# stored device credentials (without it they can't be read, and the user
# removes and re-adds each device) and Garmin's own login tokens, a folder.
DEVICE_KEY = ".secret_key"
GARMIN_TOKENS = ".garminconnect"
_MEMBER_KINDS = {
    tarfile.SYMTYPE: "a symbolic link", tarfile.LNKTYPE: "a hard link", tarfile.CHRTYPE: "a device",
    tarfile.BLKTYPE: "a device", tarfile.FIFOTYPE: "a FIFO",
}


class Runner:
    """Runs one command, an argument list, and never through a shell.

    run(args, cwd=None, as_owner=False, env=None) -> (returncode, stdout,
    stderr). as_owner runs it as the uid and gid that own `owner_path` (the
    repo), with HOME=/tmp and no supplementary groups. env is variables to
    add to the command's environment (an organisation repo's
    GIT_SSH_COMMAND). A command that can't start is 127, one that runs too
    long 124: never an exception.
    """

    def __init__(self, owner_path, timeout=1800):
        self.owner_path = owner_path
        self.timeout = timeout

    def owner(self):
        st = os.stat(self.owner_path)
        return st.st_uid, st.st_gid

    def run(self, args, cwd=None, as_owner=False, timeout=None, env=None):
        if not isinstance(args, list) or not all(isinstance(a, str) for a in args):
            raise TypeError("a command is a list of strings")
        if env is not None and not (isinstance(env, dict) and all(
                isinstance(k, str) and isinstance(v, str) for k, v in env.items())):
            raise TypeError("a command's env is a mapping of strings")
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})}
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


def _app_files(folder, app, log, label="library"):
    """One app folder's files to publish, as tuples of path parts: its
    regular files, never a link (or anything through one); dotfiles,
    __pycache__ and .pyc left out. _Skip over MAX_APP_FILES or
    MAX_APP_BYTES, or with no app.yaml of its own. `label` names where it
    is in the log: library/, or an organisation repo's owner/repo."""
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
                log(f"Leaving out {label}/{app}/{'/'.join(parts)}: it's a link.")
            elif entry.is_dir(follow_symlinks=False):
                if entry.name != "__pycache__":
                    todo.append(parts)
            elif not entry.is_file(follow_symlinks=False):
                log(f"Leaving out {label}/{app}/{'/'.join(parts)}: it isn't a regular file.")
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


def _collect_apps(src, log, label="library", skip_hidden=False):
    """[(app, files)] for each app folder in `src`, by name: a top-level
    folder named as a library app is, at most MAX_APPS of them. Anything
    else is left out, and logged - but, `skip_hidden`, a dotfile or
    dot-folder (an organisation repo's .git, .github), left out quietly.
    No library/ at all is no apps."""
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
        if skip_hidden and entry.name.startswith("."):
            continue
        if not _SHELF_APP.match(entry.name) or not entry.is_dir(follow_symlinks=False):
            log(f"Leaving out {label}/{entry.name}: it isn't an app folder.")
        elif len(apps) >= MAX_APPS:
            log(f"Leaving out {label}/{entry.name}: the app library holds at most {MAX_APPS} apps.")
        else:
            try:
                apps.append((entry.name, _app_files(entry.path, entry.name, log, label)))
            except _Skip as e:
                log(f"Leaving out {label}/{entry.name}: {e}.")
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
    return _parse_yaml(raw)


def _parse_yaml(raw):
    """_read_yaml()'s answer for a file's bytes: (mapping, None), or (None,
    why not)."""
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


def repo_info(src, source, log, label="library"):
    """The source's name and description, from library/repo.yaml (an
    organisation repo's own, at its top): {name, description}. `name` text
    of 1 to MAX_REPO_NAME characters and `description` (optional) at most
    MAX_DESCRIPTION, each trimmed; a file that's missing or isn't that is
    the source id and no description, logged."""
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
    log(f"{label}/{REPO_FILE} {why}, so the app library is named {source!r}, with no description.")
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


def change_notes_args(commit, path):
    """git log's arguments, after `git`, for the change notes of the app
    folder at `path` (library/<app> in the server's checkout, <app> in an
    organisation repo's clone) as of `commit`, a full commit: the newest
    CHANGE_NOTES, merges left out, as CHANGES_FORMAT - the path after a
    literal --."""
    if not _COMMIT.match(commit):
        raise ValueError(f"not a full commit: {commit!r}")
    return ["log", "-n", str(CHANGE_NOTES), "--no-merges", CHANGES_FORMAT, commit, "--", path]


def parse_changes(out):
    """change_notes_args()' output as the index keeps it: [{sha, subject,
    date}], newest first, at most CHANGE_NOTES - sha the commit's first 12
    hex, subject cut to MAX_SUBJECT characters. A line that isn't a full
    commit, an ISO date and a subject is left out."""
    changes = []
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3 or not _COMMIT.match(parts[0]) or not _CHANGE_DATE.match(parts[1]):
            continue
        changes.append({"sha": parts[0][:12], "subject": parts[2][:MAX_SUBJECT], "date": parts[1]})
    return changes[:CHANGE_NOTES]


def _change_notes(changes, app, log, label):
    """An app's change notes, from `changes` - publish_set()'s, a function
    of the app's folder name giving them, or None where git couldn't say -
    else []: a publish never fails for them, and one without is logged."""
    if changes is None:
        return []
    notes = changes(app)
    if notes is None:
        log(f"Couldn't read {label}/{app}'s change notes, so it's published without them.")
        return []
    return notes


def _app_entry(folder, changes=()):
    """An app's entry in the index: its fingerprint, name and description,
    and its change notes."""
    return {"sha256": fingerprint(folder), **app_info(folder), "changes": list(changes)}


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


def _published_at(index, entry):
    """When `entry`, a source's in `index`, was last published: its own
    stamp (A6), else - an entry from before there were two sources - the
    index's."""
    if not entry:
        return None
    for when in (entry.get("published_at"), (index or {}).get("published_at")):
        if isinstance(when, str):
            return when
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
    """index.json with `source`'s entry (`entry` None: without it), every
    other source's kept as it was read: a temp file, then renamed over
    it."""
    sources = dict(index["sources"]) if index else {}
    if entry is None:
        sources.pop(source, None)
    else:
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


def _same_entry(old, entry):
    """Whether the index's entry `old` already says `entry`: all but when it
    was published."""
    return isinstance(old, dict) and {k: v for k, v in old.items() if k != "published_at"} == entry


def publish_set(src, library_dir, source, commit, now, log=print, label="library", skip_hidden=False,
                changes=None):
    """Publish the app folders in `src` (the repo's library/, or an
    organisation repo's clone) to the shelf at `library_dir`, as `source`
    at `commit`. -> {commit, apps: {app: {sha256, name, description,
    changes}}, changed}. The source's entry in the index also carries the
    name and description of `src`'s repo.yaml (repo_info()), each app's
    its own app.yaml's (app_info()) and its change notes - `changes(app)`,
    the Admin's git log of its folder (None: git couldn't say, so [] and a
    log line; no `changes` at all: [] for every app) - and when this
    source was last published. `label` and `skip_hidden` are
    _collect_apps()'s.

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
            apps = {app: _app_entry(os.path.join(final, app), _change_notes(changes, app, log, label))
                    for app in sorted(os.listdir(final)) if _SHELF_APP.match(app)}
            entry = {"commit": commit, **repo_info(src, source, log, label), "apps": apps}
            if _same_entry(shelf_source(index, source), entry):
                return {"commit": commit, "apps": apps, "changed": False}
            _write_index(root, index, source, {**entry, "published_at": now}, now)
            return {"commit": commit, "apps": apps, "changed": True}
        apps = _collect_apps(src, log, label, skip_hidden)
        repo = repo_info(src, source, log, label)
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
        published = {app: _app_entry(os.path.join(tmp, app), _change_notes(changes, app, log, label))
                     for app, _files in apps}
        if os.path.lexists(final):
            aside = os.path.join(sets, f".old-{name}-{secrets.token_hex(4)}")
            os.rename(_inside(root, final), _inside(root, aside))
        os.rename(tmp, _inside(root, final))
        placed = True
        _point(root, source, f"{SHELF_SETS}/{name}")
        swapped = True
        _write_index(root, index, source, {"commit": commit, **repo, "apps": published, "published_at": now}, now)
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


def unpublish_source(library_dir, source, now, log=print):
    """Take `source` off the shelf at `library_dir`: its link removed, then
    its sets (.sets/<source>-<commit12>, and any a publish left part-way),
    then its entry in index.json, last - every other source's link, sets
    and entry exactly as they were. Never the official source. A source
    that isn't there is nothing to do. An OpError if the link or the index
    can't be changed; a set that won't delete is logged."""
    if not _SHELF_SOURCE.match(source) or source == OFFICIAL_SOURCE:
        raise ValueError(f"not a source to take off the shelf: {source!r}")
    root = os.path.realpath(library_dir)
    if not os.path.isdir(root):
        raise OpError("The app library's storage isn't there, so nothing was taken off it.", library_dir)
    index = read_shelf(root)
    try:
        _point(root, source, None)
        sets = os.path.join(root, SHELF_SETS)
        if os.path.isdir(sets) and not os.path.islink(sets):
            mine = re.compile(rf"({source}-[0-9a-f]{{12}}|\.(tmp|old)-{source}-[0-9a-f]{{12}}-[0-9a-f]{{8}})\Z")
            for entry in sorted(os.listdir(sets)):
                if mine.match(entry):
                    _remove_tree(root, os.path.join(sets, entry), log)
        if index is not None and source in index["sources"]:
            _write_index(root, index, source, None, now)
    except OSError as e:
        raise OpError("The repo's apps couldn't be taken off the app library.", str(e))


# --- organisation repos (A6) ----------------------------------------------------


def parse_github(url):
    """(owner, repo) from a GitHub repo's address as a person pastes it -
    https://github.com/<owner>/<repo>, with or without .git or a trailing
    /, or git@github.com:<owner>/<repo>.git - or None for anything else:
    another host, a path, a query or a fragment, an owner or repo GitHub
    wouldn't have. Space around it is trimmed."""
    if not isinstance(url, str):
        return None
    url = url.strip()
    found = _GITHUB_HTTPS.match(url) or _GITHUB_SSH.match(url)
    if not found:
        return None
    owner, repo = found.groups()
    if not REPO_OWNER.match(owner) or not REPO_NAME.match(repo) or repo in (".", ".."):
        return None
    return owner, repo


def repo_slug(owner, repo):
    """The repo's folder and id here: <owner>__<repo>, lowercased, every
    character outside [a-z0-9._-] made a -."""
    return re.sub(r"[^a-z0-9._-]", "-", f"{owner}__{repo}".lower())


def repo_ssh_url(owner, repo):
    """The address the Admin clones from, and stores: SSH, always."""
    return f"git@github.com:{owner}/{repo}.git"


def repo_address(owner, repo):
    """The repo on GitHub, for a person: its web page."""
    return f"https://github.com/{owner}/{repo}"


_REPO_KEYS = {"slug", "url", "owner", "repo", "added_at", "state", "source", "commit"}


def load_repos(data_dir):
    """/data/repos.json: {"repos": [{slug, url, owner, repo, added_at,
    state, source, commit}]} - the organisation repos the Admin added, in
    the order they were added. None yet is none.

    Checked strictly on every read, like users.json: anything that isn't
    exactly right is a ValueError naming the file and the key, so a
    hand-edited entry never reaches a command or a path."""
    path = os.path.join(data_dir, REPOS_FILE)
    if not os.path.exists(path):
        return {"repos": []}
    raw = _read_json(path, None)
    if not isinstance(raw, dict) or set(raw) != {"repos"} or not isinstance(raw["repos"], list):
        raise ValueError(f"{path} isn't exactly {{\"repos\": [...]}}")
    slugs, sources = set(), set()
    for i, entry in enumerate(raw["repos"]):
        where = f"{path}: repos[{i}]"
        if not isinstance(entry, dict) or set(entry) != _REPO_KEYS:
            raise ValueError(f"{where} isn't exactly {{{', '.join(sorted(_REPO_KEYS))}}}")
        owner, repo = entry["owner"], entry["repo"]
        if not isinstance(owner, str) or not REPO_OWNER.match(owner):
            raise ValueError(f"{where}.owner isn't valid: {owner!r}")
        if not isinstance(repo, str) or not REPO_NAME.match(repo) or repo in (".", ".."):
            raise ValueError(f"{where}.repo isn't valid: {repo!r}")
        slug = entry["slug"]
        if slug != repo_slug(owner, repo) or not REPO_SLUG.match(slug) or slug in slugs:
            raise ValueError(f"{where}.slug isn't valid: {slug!r}")
        slugs.add(slug)
        if entry["url"] != repo_ssh_url(owner, repo):
            raise ValueError(f"{where}.url isn't valid: {entry['url']!r}")
        if not isinstance(entry["added_at"], str) or not _STAMP.match(entry["added_at"]):
            raise ValueError(f"{where}.added_at isn't valid: {entry['added_at']!r}")
        if entry["state"] not in REPO_STATES:
            raise ValueError(f"{where}.state isn't valid: {entry['state']!r}")
        source, commit = entry["source"], entry["commit"]
        if source is not None and (not isinstance(source, str) or not _SHELF_SOURCE.match(source)
                                   or source == OFFICIAL_SOURCE or source in sources):
            raise ValueError(f"{where}.source isn't valid: {source!r}")
        if commit is not None and (not isinstance(commit, str) or not _COMMIT.match(commit)):
            raise ValueError(f"{where}.commit isn't valid: {commit!r}")
        if (source is None) != (commit is None) or (entry["state"] == "pending" and source is not None):
            raise ValueError(f"{where}: source and commit are set together, and only once it's connected")
        if source is not None:
            sources.add(source)
    return {"repos": raw["repos"]}


def ssh_command(data_dir, slug):
    """GIT_SSH_COMMAND for an organisation repo's git, exactly: its own
    deploy key and no other, GitHub's pinned host keys and nothing else
    trusted, never a prompt. Built from fixed parts, the Admin's data
    folder and a checked slug - git hands it to a shell."""
    if not isinstance(slug, str) or not REPO_SLUG.match(slug):
        raise ValueError(f"not a repo's slug: {slug!r}")
    if not _SAFE_PATH.match(data_dir) or ".." in data_dir.split("/"):
        raise ValueError(f"the Admin's data folder can't go into an SSH command: {data_dir!r}")
    key = os.path.join(data_dir, REPOS_DIR, slug, REPO_KEY)
    return (f"ssh -i {key} -o IdentitiesOnly=yes -o UserKnownHostsFile={KNOWN_HOSTS} -o StrictHostKeyChecking=yes "
            "-o BatchMode=yes -o ConnectTimeout=20")


def _oid(x):
    return isinstance(x, str) and bool(_COMMIT.match(x))


def org_git_allowed(args, url, dest):
    """Whether `args` (after `git`) is one of the commands an organisation
    repo's git may be: clone (of its own address, into its own folder),
    fetch origin, rev-parse of HEAD or its upstream, log (of a span, the
    newest of a commit, or an app folder's change notes as of a full
    commit: change_notes_args(), the folder one name after a literal --),
    merge --ff-only and reset --hard of a full commit, and cat-file of a
    full commit's tree or an object by its full id. Nothing else, so
    nothing but those ever runs with a repo's key."""
    a = list(args)
    if a[:2] == ["clone", "--"]:
        return a[2:] == [url, dest]
    if a == ["fetch", "origin"]:
        return True
    if a[:3] == ["rev-parse", "--verify", "-q"]:
        return a[3:] in (["HEAD^{commit}"], ["@{upstream}^{commit}"])
    if a[:2] in (["merge", "--ff-only"], ["reset", "--hard"]):
        return len(a) == 3 and _oid(a[2])
    if a[:1] == ["cat-file"] and len(a) == 3:
        if a[1] == "-p":
            return a[2].endswith("^{tree}") and _oid(a[2][:-7])
        return a[1] in ("-s", "blob") and _oid(a[2])
    if a[:3] == ["log", "--no-merges", "--format=%h%x09%s"] and len(a) == 4:
        old, sep, new = a[3].partition("..")
        return bool(sep) and _oid(old) and _oid(new)
    if a[:5] == ["log", "-n", str(REPO_COMMITS), "--no-merges", "--format=%h%x09%s"] and len(a) == 6:
        return _oid(a[5])
    if a[:5] == ["log", "-n", str(CHANGE_NOTES), "--no-merges", CHANGES_FORMAT] and len(a) == 8:
        return _oid(a[5]) and a[6] == "--" and bool(_SHELF_APP.match(a[7]))
    return False


def _sentence(text):
    """`text`, ending as a sentence does."""
    return text if text.endswith((".", "!", "?")) else text + "."


def git_reason(out, err):
    """Why a git command over SSH failed, in one line: git's own words,
    less the lines it adds after every failure - or its exit code."""
    for line in f"{err}\n{out}".splitlines():
        line = line.strip()
        if line and not line.lower().startswith(_GIT_BOILERPLATE):
            return line.removeprefix("fatal: ")[:200]
    return "git didn't say why"


def key_refused(out, err):
    """Whether GitHub turned the repo's deploy key down."""
    text = f"{out}\n{err}".lower()
    return any(r in text for r in _KEY_REFUSALS)


def repo_source(data, why, own_slug, recorded, reg):
    """The source an organisation repo's repo.yaml names, if it can be
    published as it: (source, None) - or (None, {text, detail}) saying why
    not. `data`/`why` are _read_yaml()'s for the file, `recorded` the
    source the repo was added as (None before its first publish) and `reg`
    load_repos()' - every other repo's source is taken.

    Not there, not a mapping, no source of LIBRARY_SOURCE's shape: not an
    OpenFit repo yet. A source other than the one it was added as: never
    published, as a changed prefix would cut installed apps off from their
    data. 'openfit', OpenFit's own, or another repo's: refused."""
    if data is None:
        return None, {"text": NOT_OPENFIT, "detail": f"{REPO_FILE} {why}."}
    source = data.get("source")
    if not isinstance(source, str) or not _SHELF_SOURCE.match(source):
        return None, {"text": NOT_OPENFIT,
                      "detail": f"{REPO_FILE} has no source: lowercase letters and digits, starting with a letter."}
    if recorded is not None and source != recorded:
        return None, {"text": f"This repo now says it's '{source}', but it was added as '{recorded}'. "
                              "Remove and add it again to change it.", "detail": None}
    if source == OFFICIAL_SOURCE:
        return None, {"text": f"Its {REPO_FILE} says it's '{OFFICIAL_SOURCE}', which is OpenFit's own. "
                              "Choose another source in it.", "detail": None}
    other = next((e for e in reg["repos"] if e["source"] == source and e["slug"] != own_slug), None)
    if other is not None:
        return None, {"text": f"Its {REPO_FILE} says it's '{source}', which {other['owner']}/{other['repo']} "
                              "already is here. Choose another source in it.", "detail": None}
    return source, None


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
    repo's admin/docker-compose.yml, and only ever one of its three
    commands - `build admin`, run by the Admin, and `stop admin` and `up -d
    --no-build --force-recreate admin`, run by the helper (a stop always
    followed by an up). The one way such a command is run."""
    if args not in ADMIN_COMPOSE_ARGS:
        raise ValueError(f"a compose command on the Admin's own project that isn't one of its three: {args!r}")
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
    return _kept_tar_args(volume, image, NO_PASSWORD)


def kept_pack_args(volume, image=ADMIN_IMAGE):
    """The same helper, packing a removed user's kept volume for a restore
    (R1): its output written to a file in /data/restore-uploads/, not
    streamed to a browser, and nothing excluded - the restore's clean
    archive leaves the password out, and more besides."""
    return _kept_tar_args(volume, image, ())


def _kept_tar_args(volume, image, exclude):
    if not isinstance(volume, str) or not USER_VOLUME.match(volume):
        raise ValueError(f"not a user's kept volume: {volume!r}")
    return ["docker", "run", "--rm", "--label", HELPER_LABEL, "--log-driver", "none",
            "-v", f"{volume}:/v:ro", image,
            "tar", "czf", "-", "-C", "/v", *(f"--exclude=./{name}" for name in exclude), "."]


def restore_helper_args(volume, clean, admin_volume=ADMIN_VOLUME, image=ADMIN_IMAGE):
    """The helper that fills a restored user's new volume, exactly: their
    volume read-write, the Admin's read-only, and GNU tar extracting the
    clean archive the Admin wrote - only ever a name CLEAN_FILE matches,
    never an upload or a backup as it came."""
    if not isinstance(volume, str) or not USER_VOLUME.match(volume):
        raise ValueError(f"not a user's volume: {volume!r}")
    if not isinstance(clean, str) or not CLEAN_FILE.match(clean):
        raise ValueError(f"not a clean archive the Admin wrote: {clean!r}")
    return ["docker", "run", "--rm", "--label", HELPER_LABEL, "--log-driver", "none",
            "-v", f"{volume}:/v", "-v", f"{admin_volume}:/b:ro", image,
            "tar", "xzf", f"/b/{RESTORE_DIR}/{clean}", "-C", "/v"]


# --- checking an archive before a restore (R1) ---------------------------------


def _shown(name):
    """A member's name as a log line shows it: printable, at most 80 characters."""
    text = "".join(c if c.isprintable() else "?" for c in name)
    return text if len(text) <= 80 else text[:79] + "…"


def member_name(member):
    """A member's name, normalised: its leading ./ stripped, "" for the
    archive's own folder. An OpError (why, in plain words) for a name that
    could lead anywhere but inside the new volume: absolute, with a `..`
    part, a NUL byte or an empty or `.` part, or a part over 255 characters."""
    name = member.name
    if "\x00" in name:
        raise OpError("It holds a name with a NUL byte in it, which a backup never does.")
    if name.startswith("/"):
        raise OpError(f"It holds an absolute name ({_shown(name)}), which a backup never does.")
    if member.isdir():
        name = name.rstrip("/")
    while name.startswith("./"):
        name = name[2:]
    if name in ("", "."):
        return ""
    parts = name.split("/")
    if ".." in parts:
        raise OpError(f"It holds a name that leads out of it ({_shown(name)}).")
    if any(part in ("", ".") for part in parts):
        raise OpError(f"It holds a name that isn't a plain path ({_shown(name)}).")
    if any(len(part) > MAX_PART for part in parts):
        raise OpError(f"It holds a name with a part longer than {MAX_PART} characters ({_shown(name)}).")
    return name


def check_members(tar):
    """Every member of an archive open for reading, walked before anything is
    written: [(name, member)], each name member_name()'s, in the archive's
    order. Only regular files and folders, each name once, at most
    MAX_MEMBERS of them and MAX_UNPACKED bytes between them, the archive's
    own folder ("") a folder; anything else is an OpError saying why, and
    the whole archive is refused."""
    found, seen, total = [], set(), 0
    for member in tar:
        if len(found) >= MAX_MEMBERS:
            raise OpError(f"It holds more than {MAX_MEMBERS:,} files and folders, more than a backup would.")
        if member.type in _MEMBER_KINDS:
            raise OpError(f"It holds {_MEMBER_KINDS[member.type]} ({_shown(member.name)}), which a backup never "
                          "does.")
        if member.type not in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE):
            raise OpError(f"It holds something that isn't a file or a folder ({_shown(member.name)}).")
        name = member_name(member)
        if name == "" and not member.isdir():
            raise OpError("It holds a file where its own folder should be.")
        if name in seen:
            raise OpError(f"It holds {_shown(name) or 'its own folder'} twice, which a backup never does.")
        seen.add(name)
        total += member.size
        if total > MAX_UNPACKED:
            raise OpError(f"Unpacked, it would be more than {MAX_UNPACKED // 1024 ** 3} GB, more than a backup "
                          "would.")
        found.append((name, member))
    return found


def check_database(tar, members, work, newest):
    """The archive's tracker.db - with its tracker.db-wal, if it has one -
    copied into `work` (a folder of the Admin's own) and opened read-only:
    it must pass SQLite's integrity check and have a schema_migrations table
    whose newest version is at most `newest`, the newest migration this
    server's OpenFit has. Returns that version; an OpError says why not."""
    by_name = dict(members)
    db = by_name.get(DB_NAME)
    if db is None or not db.isfile():
        raise OpError(f"There's no {DB_NAME} in it, so it isn't an OpenFit backup.")
    os.makedirs(work, mode=0o700)
    path = os.path.join(work, DB_NAME)
    for name, dest in ((DB_NAME, path), (DB_WAL, path + "-wal")):
        member = by_name.get(name)
        if member is None or not member.isfile():
            continue
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as out:
            shutil.copyfileobj(tar.extractfile(member), out, CHUNK)
    damaged = f"Its {DB_NAME} is damaged, so it can't be restored."
    try:
        conn = sqlite3.connect(f"file:{quote(path)}?mode=ro", uri=True)
    except sqlite3.Error as e:
        raise OpError(damaged, str(e))
    try:
        if hasattr(conn, "setconfig"):  # Python 3.12's: no writes through the schema, even by accident
            conn.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)
        conn.execute("PRAGMA trusted_schema = OFF")
        if conn.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise OpError(damaged)
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
                        ).fetchone() is None:
            raise OpError(f"Its {DB_NAME} has no schema_migrations table, so it isn't one OpenFit wrote.")
        version = conn.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0]
    except sqlite3.Error as e:
        raise OpError(damaged, str(e))
    finally:
        conn.close()
    if version is None:
        version = 0
    if type(version) is not int:
        raise OpError(f"Its {DB_NAME} has a schema version that isn't a number, so it isn't one OpenFit wrote.")
    if version > newest:
        raise OpError(NEWER_BACKUP, f"It's at migration {version}; this server's OpenFit has up to {newest}.")
    return version


def left_out(name, keep_devices):
    """Whether a member's (normalised) name stays out of the clean archive:
    NEVER_RESTORED always, and the device logins unless they're kept."""
    if name in NEVER_RESTORED:
        return True
    return not keep_devices and (name == DEVICE_KEY or name == GARMIN_TOKENS
                                 or name.startswith(GARMIN_TOKENS + "/"))


def write_clean(tar, members, dest, keep_devices):
    """The clean archive: a fresh tar.gz at `dest` (made here, 0600) of the
    checked `members`, read from `tar` - never the archive as it came - but
    those left_out(). Each a new header, `./<name>` (the archive's own
    folder `.`), a regular file or a folder, its mode (no setuid, setgid or
    sticky bit), owner, group and time kept, and nothing else of the
    original's; each file's bytes as they were. The names left out."""
    dropped = []
    fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as out, tarfile.open(fileobj=out, mode="w:gz", format=tarfile.PAX_FORMAT) as new:
        for name, member in members:
            if left_out(name, keep_devices):
                dropped.append(name)
                continue
            info = tarfile.TarInfo(f"./{name}" if name else ".")
            info.type = tarfile.DIRTYPE if member.isdir() else tarfile.REGTYPE
            info.mode = member.mode & 0o777
            info.uid, info.gid, info.uname, info.gname = member.uid, member.gid, member.uname, member.gname
            info.mtime = member.mtime
            info.size = member.size if member.isfile() else 0
            new.addfile(info, tar.extractfile(member) if member.isfile() else None)
    return dropped


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
    restoring, starting, stopping, restarting or removing a user, making a
    setup link, deleting a removed user's data or a backup, starting a
    download, updating the apps, publishing them again, updating the Admin
    itself or reverting it, or adding, connecting, updating or removing an
    organisation repo. An update, an undo, adding or restoring a user,
    updating the apps, an Admin update, an Admin revert and a repo's
    connect and update run in a background thread, their
    progress in `op`; the rest run in the caller's thread (a download only
    while it starts: once its file is open, or its helper under way, it
    streams without the lock). An Admin update or revert keeps the lock
    after its thread has handed over to the helper, until the helper has
    gone (`_settle`). A restore's upload arrives before the lock is taken.

    Constructing one deletes what an interrupted restore left in
    /data/restore-uploads/, writes the users' mounts of the app shelf
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
        self.repos_path = os.path.join(data_dir, REPOS_FILE)
        self.repo_checks_path = os.path.join(data_dir, "repo_checks.json")
        self.restore_dir = os.path.join(data_dir, RESTORE_DIR)
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
        self._clear_restores()  # left from a restore the Admin was stopped in the middle of
        self.registry()  # a bad users.json stops the Admin starting
        self.repos_registry()  # and so does a bad repos.json
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
            self._check_repos(now)  # a repo that answers is checked all the same
            raise
        result["checked_at"] = now
        result["error"] = None
        _write_json(self.check_path, result)
        self._check_repos(now)
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

    def publish_source(self, source, src_dir, commit, log=None, label="library", skip_hidden=False, changes=None):
        """The app folders in `src_dir` published to the shelf as `source`, at
        `commit` - the official library or an organisation repo's clone,
        alike (publish_set()): its link, its sets and its entry in the
        index, and no other source's, each app's change notes `changes`'s.
        publish_set()'s result; an OpError with the shelf as it was."""
        log = log or (lambda text: print(f"[admin] app library: {text}", flush=True))
        return publish_set(src_dir, self.library_dir, source, commit, self._iso(), log, label=label,
                           skip_hidden=skip_hidden, changes=changes)

    def _official_changes(self, commit):
        """publish_set()'s `changes` for the official library at `commit`:
        each app's change notes, git log of library/<app> in the server's
        checkout, as its owner - None where git fails."""
        def changes(app):
            rc, out, _err = self.git(*change_notes_args(commit, f"library/{app}"))
            return parse_changes(out) if rc == 0 else None
        return changes

    def _repo_changes(self, entry, commit):
        """publish_set()'s `changes` for an organisation repo's clone at
        `commit`: each app's change notes, git log of <app> through
        org_git() - None where git fails."""
        def changes(app):
            rc, out, _err = self.org_git(entry, *change_notes_args(commit, app))
            return parse_changes(out) if rc == 0 else None
        return changes

    def publish_official(self, commit=None, log=None):
        """The repo's library/ published to the shelf as the openfit source,
        at `commit` - HEAD's, read if not given: the working tree, which the
        Admin keeps clean. publish_source()'s result; an OpError with the
        shelf as it was, noted for the page until a publish succeeds."""
        try:
            if commit is None:
                commit = self.head()["commit"]
            result = self.publish_source(OFFICIAL_SOURCE, os.path.join(self.repo, "library"), commit, log,
                                         changes=self._official_changes(commit))
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
        rewritten). Then each organisation repo's catch-up
        (_catch_up_repos()). Whether the official library published; never
        raises."""
        published = self._publish_official_at_startup()
        self._catch_up_repos()
        return published

    def _catch_up_repos(self):
        """At startup: every published organisation repo whose shelf entry is
        missing, or on another commit, published again from its clone - if
        the clone is on the commit recorded - so a shelf made anew holds
        every repo's apps again. Never raises."""
        try:
            reg = self.repos_registry()
        except ValueError as e:
            print(f"[admin] app library: the repos aren't caught up: {e}", flush=True)
            return
        index = read_shelf(self.library_dir)
        for entry in reg["repos"]:
            source, name = entry["source"], f"{entry['owner']}/{entry['repo']}"
            entry_now = shelf_source(index, source) if source else None
            if source is None or (entry_now and entry_now["commit"] == entry["commit"]):
                continue
            if self._org_rev(entry, "HEAD^{commit}") != entry["commit"]:
                print(f"[admin] app library: {name} isn't on the shelf, and its clone isn't on the version it "
                      "was published at: its next update publishes it", flush=True)
                continue
            try:
                self.publish_source(source, self._clone_of(entry["slug"]), entry["commit"], label=name,
                                    skip_hidden=True, changes=self._repo_changes(entry, entry["commit"]))
                print(f"[admin] app library: published {name} again, as '{source}'", flush=True)
            except OpError as e:
                print(f"[admin] app library: {name}: {e.message} {e.detail}", flush=True)

    def _publish_official_at_startup(self):
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
            "published_at": _published_at(index, entry),
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

    # --- organisation repos (A6) ---------------------------------------------

    def repos_registry(self):
        return load_repos(self.data_dir)

    def _save_repos(self, reg):
        _write_json(self.repos_path, reg)

    def _repo_folder(self, slug):
        return os.path.join(self.data_dir, REPOS_DIR, slug)

    def _clone_of(self, slug):
        return os.path.join(self._repo_folder(slug), REPO_CLONE)

    def _known_repo(self, slug):
        """An organisation repo's entry, by the slug in a URL, or a 404."""
        found = next((e for e in self.repos_registry()["repos"] if e["slug"] == slug), None)
        if found is None:
            raise Refused("There's no repository by that name here.", 404)
        return found

    @staticmethod
    def _clear(path):
        """A file, link or folder of the Admin's own, gone if it's there: a
        link unlinked, never followed."""
        if os.path.islink(path) or os.path.isfile(path):
            os.remove(path)
        elif os.path.isdir(path):
            shutil.rmtree(path)

    def org_git(self, entry, *args, timeout=None):
        """git for an organisation repo, and only one of the commands
        org_git_allowed() lets through: as the Admin's own user (root),
        never as the server checkout's owner; in the repo's own clone (a
        clone, in the folder it's made in), never the server's checkout;
        and with its own GIT_SSH_COMMAND - its deploy key, GitHub's pinned
        host keys, no prompt."""
        slug = entry["slug"]
        clone = self._clone_of(slug)
        if not org_git_allowed(args, entry["url"], clone):
            raise ValueError(f"a git command outside an organisation repo's lane: {args!r}")
        cwd = self._repo_folder(slug) if args[0] == "clone" else clone
        return self.runner.run(["git", *args], cwd=cwd, timeout=timeout,
                               env={"GIT_SSH_COMMAND": ssh_command(self.data_dir, slug)})

    def _org_rev(self, entry, ref):
        """The full commit `ref` (HEAD^{commit} or @{upstream}^{commit}) is
        in the repo's clone, or None: no such commit (an empty repo), or no
        clone."""
        if not os.path.isdir(self._clone_of(entry["slug"])):
            return None
        rc, out, _err = self.org_git(entry, "rev-parse", "--verify", "-q", ref)
        commit = out.strip()
        return commit if rc == 0 and _COMMIT.match(commit) else None

    def _org_log(self, entry, old, new):
        """The commits from `old` (None: the newest REPO_COMMITS) to `new`,
        merges left out: [{sha, subject}], or [] if git can't say."""
        span = ("--no-merges", "--format=%h%x09%s", f"{old}..{new}") if old else (
            "-n", str(REPO_COMMITS), "--no-merges", "--format=%h%x09%s", new)
        rc, out, _err = self.org_git(entry, "log", *span)
        if rc != 0:
            return []
        commits = []
        for line in out.splitlines():
            sha, _, subject = line.partition("\t")
            if sha:
                commits.append({"sha": sha, "subject": subject})
        return commits

    def _repo_yaml_at(self, entry, commit):
        """_read_yaml()'s answer for the repo's repo.yaml at `commit`, read
        from git's own objects (a check never touches the clone's files):
        only a regular file at its top - a link is refused, as a link in
        the clone is - of at most MAX_YAML_BYTES. An OpError if git can't
        read the commit."""
        rc, out, err = self.org_git(entry, "cat-file", "-p", f"{commit}^{{tree}}")
        if rc != 0:
            raise OpError("Couldn't read the repo's files.", _tail(out, err))
        for line in out.splitlines():
            meta, tab, name = line.partition("\t")
            parts = meta.split(" ")
            if not tab or name != REPO_FILE or len(parts) != 3:
                continue
            mode, kind, oid = parts
            if mode == "120000":
                return None, "is a link"
            if kind != "blob" or mode not in ("100644", "100755") or not _oid(oid):
                return None, "isn't a regular file"
            rc, out, err = self.org_git(entry, "cat-file", "-s", oid)
            if rc != 0 or not out.strip().isdigit():
                raise OpError("Couldn't read the repo's repo.yaml.", _tail(out, err))
            if int(out.strip()) > MAX_YAML_BYTES:
                return None, f"is over {MAX_YAML_BYTES >> 10} KB"
            rc, out, err = self.org_git(entry, "cat-file", "blob", oid)
            if rc != 0:
                raise OpError("Couldn't read the repo's repo.yaml.", _tail(out, err))
            return _parse_yaml(out.encode("utf-8"))
        return None, "is missing"

    def repo_checks(self):
        """/data/repo_checks.json, what the checks, connects and updates last
        found of each repo, by slug: {checked_at, head, commits, problem,
        error} - the Admin's own record, for the page."""
        raw = _read_json(self.repo_checks_path, {})
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items() if isinstance(k, str) and REPO_SLUG.match(k) and isinstance(v, dict)}

    def _note_repo_check(self, slug, **fields):
        checks = self.repo_checks()
        checks[slug] = {**checks.get(slug, {}), **fields}
        _write_json(self.repo_checks_path, checks)

    def _drop_repo_check(self, slug):
        checks = self.repo_checks()
        if checks.pop(slug, None) is not None:
            _write_json(self.repo_checks_path, checks)

    def _public_key(self, slug):
        """The repo's public deploy key, as ssh-keygen wrote it, if it reads
        as one: never the private key, which the Admin never reads."""
        try:
            with open(os.path.join(self._repo_folder(slug), REPO_KEY + ".pub"), "rb", opener=_no_follow) as f:
                text = f.read(1024).decode("ascii").strip()
        except (OSError, UnicodeDecodeError):
            return None
        return text if PUBLIC_KEY.match(text) else None

    def _server_name(self):
        """The server's name for a deploy key's comment, so GitHub's list of
        keys says which server each is: docker info's, made plain."""
        name = (self.server() or {}).get("Name") or ""
        return re.sub(r"[^A-Za-z0-9._-]", "-", name)[:63] or "server"

    def add_repo(self, url):
        """Add repository: the repo's address checked (400), its slug free
        (409), then a key pair made for it alone and the repo saved as
        pending - in the request, under the lock. {repo, public_key}: the
        public key, to add on GitHub as a read-only deploy key. The private
        key stays in /data/repos/<slug>/deploy_key, 0600: it's never read
        back, returned or logged."""
        parsed = parse_github(url)
        if parsed is None:
            raise Refused("That isn't a GitHub repository's address. Paste one like "
                          "https://github.com/<owner>/<repo>.", 400)
        owner, repo = parsed
        slug = repo_slug(owner, repo)
        self._take_lock()
        try:
            reg = self.repos_registry()
            if any(e["slug"] == slug for e in reg["repos"]):
                raise Refused(f"{owner}/{repo} is already added here.", 409)
            folder = self._repo_folder(slug)
            key = os.path.join(folder, REPO_KEY)
            try:
                # A folder left by an add that stopped part-way is the
                # Admin's own, and holds no repo anyone's connected: made anew.
                self._clear(folder)
                os.makedirs(os.path.dirname(folder), mode=0o700, exist_ok=True)
                os.mkdir(folder, 0o700)
            except OSError as e:
                raise OpError("Couldn't make the repo's folder.", str(e))
            rc, _out, err = self.runner.run(
                ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", f"openfit-admin {self._server_name()} {owner}/{repo}",
                 "-f", key], timeout=60)
            public = None
            if rc == 0:
                try:
                    os.chmod(key, 0o600)
                    public = self._public_key(slug)
                except OSError:
                    public = None
            if public is None:
                shutil.rmtree(folder, ignore_errors=True)
                raise OpError("Couldn't make a deploy key for the repo.", _tail("", err) if rc != 0 else "")
            entry = {"slug": slug, "url": repo_ssh_url(owner, repo), "owner": owner, "repo": repo,
                     "added_at": self._iso(), "state": "pending", "source": None, "commit": None}
            reg["repos"].append(entry)
            self._save_repos(reg)
            self._drop_repo_check(slug)
            print(f"[admin] added the repository {owner}/{repo}: its deploy key waits to be added on GitHub", flush=True)
            return {"repo": self._repo_view(entry, None), "public_key": public}
        finally:
            self.lock.release()

    def start_repo_connect(self, slug):
        """Connect: a pending repo's clone, then its first publish, as an op
        with a log; the refusals here, so one is a 404 or 409 with nothing
        done."""
        self._known_repo(slug)
        self._take_lock()
        try:
            entry = self._known_repo(slug)
            if entry["state"] != "pending":
                raise Refused(f"{entry['owner']}/{entry['repo']} is connected already.", 409)
            op = self._begin("repo_connect", repo={"slug": slug, "owner": entry["owner"], "repo": entry["repo"]})
        except BaseException:
            self.lock.release()
            raise
        self._spawn(op, self._repo_connect, slug)
        return self.current_op()

    def _clone(self, entry):
        """The repo cloned afresh into its own folder: None if it worked, else
        (what went wrong, git's words behind it), with no clone left."""
        clone = self._clone_of(entry["slug"])
        try:
            self._clear(clone)  # one an earlier Connect left part-way
        except OSError as e:
            return "Couldn't clear the repo's folder.", str(e)
        rc, out, err = self.org_git(entry, "clone", "--", entry["url"], clone, timeout=600)
        if rc == 0:
            return None
        try:
            self._clear(clone)
        except OSError:
            pass
        if key_refused(out, err):
            return KEY_REFUSED, _tail(out, err)
        return f"Couldn't reach GitHub: {git_reason(out, err)}", _tail(out, err)

    def _repo_connect(self, op, slug):
        entry = self._known_repo(slug)
        name = f"{entry['owner']}/{entry['repo']}"
        self.say(op, f"Cloning {name} from GitHub, with its deploy key…")
        failed = self._clone(entry)
        if failed:
            self.say(op, *failed)
            text = failed[0]
            return self._finish(op, "failed", text if text == KEY_REFUSED
                                else f"{_sentence(text)} {name} is still waiting to connect.")
        reg = self.repos_registry()
        for e in reg["repos"]:
            if e["slug"] == slug:
                e["state"] = "connected"
        self._save_repos(reg)
        self.say(op, f"Connected to {name}.")
        head = self._org_rev(entry, "HEAD^{commit}")
        self._note_repo_check(slug, checked_at=self._iso(), head=head, commits=[], problem=None, error=None)
        return self._publish_repo(op, slug, head)

    def _publish_repo(self, op, slug, head, old=None, updating=False):
        """A connected repo's clone, at `head`, published to the shelf as the
        source its repo.yaml names - if that's one it can be (repo_source())
        - and recorded. Connect's last step and Update's. An update that
        can't publish puts the clone back on `old`: the shelf is untouched
        either way."""
        entry = self._known_repo(slug)
        name = f"{entry['owner']}/{entry['repo']}"
        clone = self._clone_of(slug)
        if head is None:
            data, why = None, "is missing: the repo has no commits yet"
        else:
            data, why = _read_yaml(os.path.join(clone, REPO_FILE))
        source, problem = repo_source(data, why, slug, entry["source"], self.repos_registry())

        def back():
            if not updating:
                return
            if old:
                self.say(op, "Putting the repo's files back as they were…")
                rc, out, err = self.org_git(entry, "reset", "--hard", old)
                if rc != 0:
                    self.say(op, "Couldn't put the repo's files back.", _tail(out, err))
            else:
                self.say(op, "It had no commits before, so its files stay as they are.")

        if problem:
            self.say(op, problem["text"], problem["detail"] or "")
            self._note_repo_check(slug, head=head, problem=problem)
            back()
            if updating:
                return self._finish(op, "failed", f"{problem['text']} The app library is as it was.")
            if problem["text"] == NOT_OPENFIT:
                return self._finish(op, "ok", f"Connected {name}. It isn't an OpenFit repo yet: it needs a repo.yaml "
                                              "at the top, so nothing was published.")
            return self._finish(op, "failed", f"Connected {name}, but nothing was published: {problem['text']}")

        self.say(op, f"Publishing its apps to the app library as '{source}'…")
        try:
            result = self.publish_source(source, clone, head, log=lambda text: self.say(op, text), label=name,
                                         skip_hidden=True, changes=self._repo_changes(entry, head))
        except OpError as e:
            self.say(op, e.message, e.detail)
            back()
            if updating:
                return self._finish(op, "failed", "The app library couldn't be updated, so the repo was put back. "
                                                  "The app library is as it was.")
            return self._finish(op, "failed", f"Connected {name}, but its apps couldn't be published. Check now, "
                                              "then its Update available, tries again.")
        reg = self.repos_registry()
        for e in reg["repos"]:
            if e["slug"] == slug:
                e.update(source=source, commit=head)
        self._save_repos(reg)
        self._note_repo_check(slug, head=head, commits=[], problem=None)
        n = len(result["apps"])
        apps = f"{n} app{'' if n == 1 else 's'}"
        if updating:
            return self._finish(op, "ok", f"Updated {name} to {head[:7]}: {apps} in the app library, as '{source}'.")
        return self._finish(op, "ok", f"Connected {name}, and published {apps} to the app library as '{source}'.")

    def start_repo_update(self, slug):
        """A connected repo's update, as the last check found it on GitHub:
        the refusals here (404, 409), the rest in the background."""
        self._known_repo(slug)
        self._take_lock()
        try:
            entry = self._known_repo(slug)
            name = f"{entry['owner']}/{entry['repo']}"
            view = self._repo_view(entry, self.repo_checks().get(slug))
            if entry["state"] != "connected":
                raise Refused(f"{name} isn't connected yet: add its deploy key on GitHub, then Connect.", 409)
            if view["behind"] and view["problem"]:
                raise Refused(view["problem"]["text"], 409)
            if not view["can_update"]:
                raise Refused(f"There's no update for {name} to apply. Check for updates first.", 409)
            op = self._begin("repo_update", repo={"slug": slug, "owner": entry["owner"], "repo": entry["repo"]})
        except BaseException:
            self.lock.release()
            raise
        self._spawn(op, self._repo_update, slug)
        return self.current_op()

    def _repo_update(self, op, slug):
        """Merge what the last check fetched (a fast-forward, never anything
        else), check its repo.yaml again, publish. No container is touched."""
        entry = self._known_repo(slug)
        self.say(op, "Noting the version the repo is on now…")
        old = self._org_rev(entry, "HEAD^{commit}")
        target = self._org_rev(entry, "@{upstream}^{commit}")
        with self._op_lock:
            op["old"] = {"commit": old, "short": old[:7]} if old else None
            op["new"] = {"commit": target, "short": target[:7]} if target else None
        if target is None:
            self.say(op, "There's nothing on GitHub to update to.")
            return self._finish(op, "failed", "Nothing was changed.")
        self.say(op, "Adding the repo's new commits…")
        rc, out, err = self.org_git(entry, "merge", "--ff-only", target)
        if rc != 0:
            self.say(op, "The new commits can't be added on top of the Admin's copy (not a fast-forward).",
                     _tail(out, err))
            return self._finish(op, "failed", "Nothing was changed.")
        head = self._org_rev(entry, "HEAD^{commit}")
        if head != target:
            self.say(op, "The repo didn't end up on the version it was updated to.")
            if old:
                self.org_git(entry, "reset", "--hard", old)
            return self._finish(op, "failed", "The app library is as it was.")
        return self._publish_repo(op, slug, head, old=old, updating=True)

    def remove_repo(self, slug):
        """Remove: the repo's apps taken off the shelf (unpublish_source()),
        then its folder - clone and key - deleted, then its entry, in the
        request, under the lock. What users installed from it stays theirs.
        A step that fails stops there, with the repo still listed."""
        self._known_repo(slug)
        self._take_lock()
        try:
            entry = self._known_repo(slug)
            name = f"{entry['owner']}/{entry['repo']}"
            if entry["source"]:
                unpublish_source(self.library_dir, entry["source"], self._iso(),
                                 lambda text: print(f"[admin] app library: {text}", flush=True))
            try:
                self._clear(self._repo_folder(slug))
            except OSError as e:
                raise OpError(f"{name}'s clone and key weren't deleted, so it's still listed. Try again.", str(e))
            reg = self.repos_registry()
            reg["repos"] = [e for e in reg["repos"] if e["slug"] != slug]
            self._save_repos(reg)
            self._drop_repo_check(slug)
            print(f"[admin] removed the repository {name}", flush=True)
        finally:
            self.lock.release()

    def _check_repos(self, now):
        """A check's look at every connected repo: each fetched, and its
        repo.yaml read again at what GitHub has. One that fails is marked,
        and the others go on: never an exception."""
        try:
            reg = self.repos_registry()
        except ValueError as e:
            print(f"[admin] check: the repos aren't checked: {e}", flush=True)
            return
        for entry in reg["repos"]:
            if entry["state"] != "connected":
                continue
            try:
                self._check_repo(entry, now, reg)
            except OpError as e:
                self._note_repo_check(entry["slug"], error={"at": now, "text": e.message, "detail": e.detail})
                print(f"[admin] check: {entry['owner']}/{entry['repo']}: {e.message}", flush=True)

    def _check_repo(self, entry, now, reg):
        slug, name = entry["slug"], f"{entry['owner']}/{entry['repo']}"
        if self._org_rev(entry, "HEAD^{commit}") is None:
            # An empty clone - connected before its first push - or a clone
            # that's gone is cloned afresh: a fetch into an empty one can miss
            # the branch GitHub makes its default.
            failed = self._clone(entry)
            if failed:
                raise OpError(*failed)
            head = self._org_rev(entry, "HEAD^{commit}")
        else:
            rc, out, err = self.org_git(entry, "fetch", "origin", timeout=300)
            if rc != 0:
                raise OpError(f"Couldn't reach GitHub: {git_reason(out, err)}", _tail(out, err))
            head = self._org_rev(entry, "@{upstream}^{commit}")
        commits, problem = [], None
        if head is None:
            problem = {"text": NOT_OPENFIT, "detail": "The repo has no commits on GitHub yet."}
        else:
            data, why = self._repo_yaml_at(entry, head)
            _source, problem = repo_source(data, why, slug, entry["source"], reg)
            if head != entry["commit"]:
                commits = self._org_log(entry, entry["commit"], head)
        self._note_repo_check(slug, checked_at=now, head=head, commits=commits, problem=problem, error=None)
        behind = head is not None and head != entry["commit"]
        print(f"[admin] check: {name}: " + ("up to date" if not behind else f"{len(commits)} new commit(s)")
              + (f" - {problem['text']}" if problem else ""), flush=True)

    def _repo_view(self, entry, check):
        """One organisation repo as the page shows it: its entry, its state
        in words (pending, connected, or not an OpenFit repo yet - or the
        reason its repo.yaml was refused), and what the last check found:
        whether GitHub is ahead of what's published, the commits that'd
        bring, and whether a check couldn't reach it. The public key while
        it's pending, to add on GitHub; never the private one."""
        check = check or {}
        head = check.get("head") if _oid(check.get("head")) else None
        problem = check.get("problem") if isinstance(check.get("problem"), dict) else None
        error = check.get("error") if isinstance(check.get("error"), dict) else None
        commits = [c for c in check.get("commits") or [] if isinstance(c, dict)] if isinstance(
            check.get("commits"), list) else []
        connected = entry["state"] == "connected"
        behind = connected and head is not None and head != entry["commit"]
        if not connected:
            status, message = "pending", "Waiting for its deploy key: add it on GitHub, then Connect."
        elif problem:
            status, message = ("not_openfit" if problem.get("text") == NOT_OPENFIT else "refused"), problem.get("text")
        elif entry["source"] is None:
            status, message = "connected", "Connected. Its apps aren't published yet: its Update available publishes them."
        else:
            status, message = "connected", f"Connected, and published as '{entry['source']}'."
        return {
            "slug": entry["slug"], "owner": entry["owner"], "repo": entry["repo"], "url": entry["url"],
            "address": repo_address(entry["owner"], entry["repo"]), "added_at": entry["added_at"],
            "state": entry["state"], "status": status, "message": message,
            "detail": problem.get("detail") if problem else None, "problem": problem,
            "public_key": None if connected else self._public_key(entry["slug"]),
            "checked_at": check.get("checked_at"), "error": error,
            "head": head[:7] if head else None, "behind": behind, "commits": commits if behind else [],
            "can_update": behind and problem is None,
        }

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

    def _check_new_user(self, name, port, kept_ok=True):
        """400 for a name or port that can't be a user's, 409 for one taken -
        and, unless `kept_ok` (a restore), for a name with kept data."""
        users = self.users()
        problem = name_problem(name, self.settings)
        if problem:
            raise Refused(problem, 400)
        if any(u.name == name for u in users):
            raise Refused(f"There's already a user called {name}.", 409)
        if not kept_ok and any(r["name"] == name for r in self.registry()["removed"]):
            raise Refused(f"{name} has kept data on this server. Choose another name.", 409)
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
            # volume was made moments ago, in this op, by its own `up` (an
            # add's `up -d`, a restore's `up --no-start`): start_add_user and
            # start_restore refused a name whose volume was already on the
            # server, so it holds nothing but what this op put there in the
            # last minutes - a failed OpenFit's first writes, or a restore's
            # copy of a backup that's still where it came from - and it's
            # nobody's data.
            rc, out, err = self.compose(user, "down", "-v")
        if rc != 0:
            self.say(op, "Their OpenFit didn't come down.", _tail(out, err))
        shutil.rmtree(os.path.dirname(user.compose_file), ignore_errors=True)

    # --- restoring a backup as a new user (R1) -----------------------------------

    def _clear_restores(self):
        """At startup: anything in /data/restore-uploads/ is left from a
        restore (or an upload) the Admin was stopped in the middle of."""
        try:
            names = os.listdir(self.restore_dir)
        except FileNotFoundError:
            return
        for name in names:
            path = os.path.join(self.restore_dir, name)
            try:
                if os.path.isdir(path) and not os.path.islink(path):
                    shutil.rmtree(path)
                else:
                    os.unlink(path)
                print(f"[admin] deleted {name}, left from a restore", flush=True)
            except OSError as e:
                print(f"[admin] couldn't delete {path}: {e}", flush=True)

    def _restore_path(self, name):
        os.makedirs(self.restore_dir, mode=0o700, exist_ok=True)
        return os.path.join(self.restore_dir, name)

    def new_upload(self):
        """A new, empty file for an uploaded backup, made here (0600): (path,
        an open binary file to write it to) - /data/restore-uploads/<random>.tar.gz."""
        path = self._restore_path(secrets.token_hex(16) + ".tar.gz")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        return path, os.fdopen(fd, "wb")

    def drop_upload(self, path):
        """An upload that never became a restore (refused, cut off or too
        large), deleted - one new_upload() made, and nothing else."""
        if os.path.dirname(path) == self.restore_dir and RESTORE_FILE.match(os.path.basename(path)):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)

    def newest_migration(self):
        """The newest migration of the OpenFit this server runs: the highest
        NNN of app/migrations/NNN_*.sql in the repo at HEAD, by the user
        system's own file name rule. An OpError if there's none."""
        out = self._git_out("ls-tree", "--name-only", "HEAD", "--", "app/migrations/",
                            what="read which migrations OpenFit has")
        versions = [int(m.group(1)) for line in out.splitlines()
                    if (m := MIGRATION_FILE.match(line.rsplit("/", 1)[-1]))]
        if not versions:
            raise OpError("Couldn't read which migrations OpenFit has.")
        return max(versions)

    def start_restore(self, name, port, keep_devices, source, host=None):
        """A new user from a backup: `source` ("upload", the path new_upload()
        made), ("backup", a backup's name) or ("removed", a removed user's
        name, their kept data). The new user's name and port, the source and
        the lock checked here, as Add user's are, so a refusal is a 400, 404
        or 409 with nothing done - an upload stays the caller's to delete;
        the rest, the archive's checks among it, is an op with a log
        (`restore_user`), which deletes the upload however it ends."""
        if type(keep_devices) is not bool:
            raise Refused("Say whether to keep the device logins: true or false.", 400)
        kind, ref = source if isinstance(source, tuple) and len(source) == 2 else (None, None)
        if kind == "upload":
            if not (isinstance(ref, str) and os.path.dirname(ref) == self.restore_dir
                    and RESTORE_FILE.match(os.path.basename(ref))):
                raise ValueError(f"not an upload new_upload() made: {ref!r}")
        elif kind not in ("backup", "removed"):
            raise ValueError(f"not a restore's source: {source!r}")
        self._check_new_user(name, port, kept_ok=False)
        self._restore_source(kind, ref)
        self._take_lock()
        try:
            self._check_new_user(name, port, kept_ok=False)  # again: another may have finished meanwhile
            record = self._restore_source(kind, ref, locked=True)
            self._image_built()
            self._shelf_ready()
            # As Add user: a volume the Admin has no record of is someone's
            # data - never adopted, and never at risk from the failure
            # path's `down -v`.
            if self._volume_exists(user_volume(name)):
                raise Refused(
                    f"There's already storage called {user_volume(name)} on the server that the Admin "
                    "has no record of, so it won't use it. Choose another name.", 409)
            op = self._begin("restore_user", user={"name": name, "port": port}, source=record,
                             keep_devices=keep_devices)
        except OpError as e:
            self.lock.release()
            raise Refused(e.message, 500)
        except BaseException:
            self.lock.release()
            raise
        self._spawn(op, self._restore_user, self.added(name, port), kind, ref, keep_devices, address(host, port))
        return self.current_op()

    def _restore_source(self, kind, ref, locked=False):
        """The source a restore reads, checked: what the op records of it, or
        a refusal - a backup not in the Admin's own listing, or no kept data
        by that name, a 404; kept data whose project has a container again
        (only looked for under the lock), a 409."""
        if kind == "upload":
            return {"kind": "upload"}
        if kind == "backup":
            path = self._backup_path(ref)
            m = BACKUP_NAME.match(os.path.basename(path))
            return {"kind": "backup", "name": ref, "user": m.group(3) or self.settings["user"]}
        entry = self._known(ref, removed=True)
        if locked:
            if entry["volume"] != user_volume(ref) or not USER_VOLUME.match(entry["volume"]):
                raise OpError("Their kept data isn't stored under the name the Admin expects, so it can't be "
                              "restored.")
            rc, out, err = self.docker(
                "ps", "-a", "--no-trunc", "--filter", f"label=com.docker.compose.project={user_project(ref)}",
                "--format", "{{.ID}}",
            )
            if rc != 0:
                raise OpError(f"Couldn't ask Docker about {ref}'s containers.", _tail(out, err))
            if out.strip():
                raise Refused(f"{ref} has a container again, so their kept data can't be restored now.", 409)
        return {"kind": "removed", "name": ref, "user": ref}

    def _restore_user(self, op, user, kind, ref, keep, where):
        made = []  # every file and folder this restore wrote in /data/restore-uploads/, deleted as it ends
        if kind == "upload":
            made.append(ref)
        try:
            return self._restore_steps(op, user, kind, ref, keep, where, made)
        finally:
            for path in made:
                try:
                    if os.path.isdir(path) and not os.path.islink(path):
                        shutil.rmtree(path)
                    else:
                        os.unlink(path)
                except FileNotFoundError:
                    pass
                except OSError as e:
                    self.say(op, f"Couldn't delete {os.path.basename(path)} from the restore's own files.", str(e))

    def _restore_steps(self, op, user, kind, ref, keep, where, made):
        stamp = secrets.token_hex(16)
        # 1. The archive: the upload, the backup itself, or the kept data packed.
        if kind == "upload":
            archive = ref
        elif kind == "backup":
            archive = os.path.join(self.backups_dir, ref)
            self.say(op, f"Restoring {user.name} from the backup {ref}.")
        else:
            archive = self._restore_path(f"{stamp}.tar.gz")
            made.append(archive)
            self.say(op, f"Packing {ref}'s kept data…")
            try:
                fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
                with os.fdopen(fd, "wb") as out, \
                        self.runner.stream(kept_pack_args(user_volume(ref), self.admin_image)) as stdout:
                    while True:
                        chunk = stdout.read1(CHUNK)
                        if not chunk:
                            break
                        out.write(chunk)
            except OpError as e:
                self.say(op, e.message, e.detail)
                return self._finish(op, "failed", f"Couldn't read {ref}'s kept data. Nothing was created.")
            except OSError as e:
                self.say(op, "Couldn't write their kept data to a file.", str(e))
                return self._finish(op, "failed", f"Couldn't read {ref}'s kept data. Nothing was created.")

        # 2. Every member checked, the database too, before anything is created.
        self.say(op, "Checking the backup…")
        clean = self._restore_path(f"{stamp}-clean.tar.gz")
        work = self._restore_path(f"{stamp}-db")
        made += [clean, work]
        try:
            newest = self.newest_migration()
            with tarfile.open(archive, "r:gz") as tar:
                members = check_members(tar)
                version = check_database(tar, members, work, newest)
                self.say(op, f"It holds {len(members)} files and folders, and its database is sound, at "
                             f"migration {version}.")
                # 3. The clean archive: the checked members, but what's never restored.
                dropped = write_clean(tar, members, clean, keep)
        except OpError as e:
            self.say(op, e.message, e.detail)
            return self._finish(op, "failed", f"{e.message} Nothing was created.")
        except (OSError, tarfile.TarError, EOFError, zlib.error, ValueError) as e:
            self.say(op, "It isn't a backup the Admin can read: it should be a .tar.gz.", str(e))
            return self._finish(op, "failed", "It isn't a backup the Admin can read: it should be a .tar.gz. "
                                              "Nothing was created.")
        shutil.rmtree(work, ignore_errors=True)
        self.say(op, "Left out: " + (", ".join(_shown(n) for n in dropped) if dropped else "nothing") + ". "
                 + ("The device logins are kept." if keep else "The device logins aren't kept: remove and re-add "
                                                             f"each device in {user.name}'s OpenFit."))

        # 4. Their compose file, from the template.
        folder = os.path.dirname(user.compose_file)
        self.say(op, f"Writing {user.name}'s compose file…")
        try:
            os.makedirs(folder, exist_ok=True)
            with open(user.compose_file, "w", encoding="utf-8") as f:
                f.write(self._compose_text(user))
        except OSError as e:
            self.say(op, "Couldn't write their compose file.", str(e))
            shutil.rmtree(folder, ignore_errors=True)
            return self._finish(op, "failed", f"{user.name} wasn't restored. Nothing was created.")

        # From here on a failure is taken back with `down -v`: this project
        # and its volume were made moments ago, by this op's own `up
        # --no-start` - start_restore refused a name whose volume was already
        # on the server - so the volume holds nothing but the copy of a
        # backup that is still where it came from. It's nobody's data.
        def fail(summary):
            self._take_back(op, user, kept=False)
            return self._finish(op, "failed", summary)

        # 5. Created, not started: the container and its new, empty volume.
        self.say(op, f"Creating {user.name}'s OpenFit, without starting it…")
        rc, out, err = self.compose(user, "up", "--no-start", "--no-build")
        if rc != 0:
            self.say(op, "Their OpenFit wasn't created.", _tail(out, err))
            return fail(f"{user.name} wasn't restored: their OpenFit wasn't created.")

        # 6. The volume filled, by a helper, from the clean archive alone.
        self.say(op, f"Putting the backup's data into {user.name}'s storage…")
        rc, out, err = self.runner.run(restore_helper_args(user_volume(user.name), os.path.basename(clean),
                                                           self.admin_volume, self.admin_image))
        if rc != 0:
            self.say(op, "The data didn't go in.", _tail(out, err))
            return fail(f"{user.name} wasn't restored: the backup's data didn't go into their storage.")

        # 7. Started (its migrations run as it starts), and checked.
        self.say(op, f"Starting {user.name}'s OpenFit…")
        rc, out, err = self.compose(user, "up", "-d", "--no-build")
        if rc != 0:
            clash = port_clash(user.port, out, err)
            self.say(op, clash or "Their OpenFit didn't start.", _tail(out, err))
            return fail(clash or f"{user.name} wasn't restored: their OpenFit didn't start.")
        self.say(op, f"Checking {user.name}'s OpenFit is answering…")
        if not self.healthy(user):
            self.say(op, "Their OpenFit didn't answer.")
            return fail(f"{user.name} wasn't restored: their OpenFit didn't answer.")

        reg = self.registry()
        reg["users"].append({"name": user.name, "port": user.port, "created_at": self._iso()})
        self._save_registry(reg)
        self.say(op, f"Their OpenFit is at {where}")
        return self._finish(op, "ok", f"Restored {user.name}. They start locked: use Setup link to give them a "
                                      "password.")

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
        """The Repos panel's and the Repo tab's rows: the shelf's sources, from
        its index.json, and the organisation repos, from repos.json - OpenFit
        first, then the rest by name. Each {key, source, name, description,
        official, commit, short, published_at, apps: [{id, name,
        description}], current, error, update, repo} (the apps by name,
        then id) - names falling back to ids where the index has none.

        For OpenFit: whether its shelf matches the code, the last publish's
        failure, and which update would bring its waiting app changes:
        "apps" (Update apps), "openfit" (they come with Update OpenFit) or
        None. With nothing published yet, OpenFit is listed all the same,
        empty. An organisation repo's row is its source's, once it's
        published (its name repo.yaml's), else one of its own named
        owner/repo; `repo` is _repo_view()'s, and `update` "repo" when its
        Update is on offer. `key` names the row: the source, or
        repo:<slug> for an organisation repo's."""
        index = read_shelf(self.library_dir) or {"sources": {}}

        def text(value, most):
            return value.strip() if isinstance(value, str) and 0 < len(value.strip()) <= most else None

        def blank(source, name):
            return {"key": source, "source": source, "name": name, "description": None,
                    "official": source == OFFICIAL_SOURCE, "commit": None, "short": None, "published_at": None,
                    "apps": [], "current": None, "error": None, "update": None, "repo": None}

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
            found.append({**blank(source, text(entry.get("name"), MAX_REPO_NAME) or source),
                          "description": text(entry.get("description"), MAX_DESCRIPTION),
                          "commit": entry["commit"], "short": entry["commit"][:7],
                          "published_at": _published_at(index, entry), "apps": apps})
        if not any(r["official"] for r in found):
            found.append(blank(OFFICIAL_SOURCE, OFFICIAL_SOURCE))
        official = next(r for r in found if r["official"])
        official.update(current=library["current"], error=library["error"],
                        update="apps" if update_apps_offered(check)
                        else "openfit" if update_offered(check) and _changed(check, "library") else None)
        try:
            reg = self.repos_registry()
        except ValueError:
            reg = {"repos": []}
        checks = self.repo_checks()
        for entry in reg["repos"]:
            view = self._repo_view(entry, checks.get(entry["slug"]))
            row = next((r for r in found if entry["source"] and r["source"] == entry["source"]), None)
            if row is None:
                row = blank(entry["source"], f"{entry['owner']}/{entry['repo']}")
                found.append(row)
            row.update(key=f"repo:{entry['slug']}", repo=view, update="repo" if view["can_update"] else None)
        return sorted(found, key=lambda r: (not r["official"], r["name"].casefold(), r["key"]))

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
                                                 "log", "user", "repo")} for o in reversed(ops)],
        }
