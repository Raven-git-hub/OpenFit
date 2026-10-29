"""
Shared test fixtures.

Every test runs against a fresh temp SQLite file - never the real
/data/tracker.db - and nothing here touches the network: Garmin and
Google Health are never contacted. Rows are written by the fake plugin
in test_plugin_contract.py, or by the real plugins with their network
calls stubbed (test_sleep_sessions.py).

The same goes for the encryption key: every test gets a throwaway one in
its tmp_path, so nothing can read or create /data/.secret_key - and for
the user system's account and session key (/data/auth.json and
/data/.session_key). The shared `client` is signed in, as the page is:
a password set, its session carrying the tag, and the X-OpenFit header
on every request. A test of signing in starts from a client of its own,
signed out.

The Admin's tests (test_admin_*.py) need no docker and no network
either: git runs for real against temporary repos (a bare "origin" and a
clone standing in for the server's copy), and every docker command goes
to FakeRunner, which records it and answers from a script.
"""

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from collections import namedtuple
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, "app")
ADMIN_DIR = os.path.join(ROOT, "admin")
for _path in (ADMIN_DIR, APP_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import auth as openfit_auth  # noqa: E402
import crypto as openfit_crypto  # noqa: E402
from migrations import discover_migrations, run_migrations  # noqa: E402


@pytest.fixture(autouse=True)
def secret_key_file(tmp_path, monkeypatch):
    """Point credential encryption at a throwaway key for every test.

    Autouse and unconditional: without it a test that encrypts anything
    would read - or worse, create - the real /data/.secret_key.
    """
    path = tmp_path / ".secret_key"
    monkeypatch.delenv(openfit_crypto.KEY_ENV, raising=False)
    monkeypatch.setenv(openfit_crypto.KEY_PATH_ENV, str(path))
    return path


@pytest.fixture(autouse=True)
def auth_files(tmp_path, monkeypatch):
    """Point the user system's account and session key at throwaway files
    for every test, and forget failed sign-ins: nothing can read or write
    /data/auth.json or /data/.session_key, and one test's wrong passwords
    never count against another's."""
    import main

    files = SimpleNamespace(auth=tmp_path / "auth.json", key=tmp_path / ".session_key")
    monkeypatch.setitem(main.app.config, "AUTH_PATH", str(files.auth))
    monkeypatch.setitem(main.app.config, "SESSION_KEY_PATH", str(files.key))
    monkeypatch.setattr(main, "_fails", openfit_auth.Fails())
    return files


# The shared client's password. Its hash is made once for the whole run:
# hashing is slow on purpose, and most tests sign in.
TEST_PASSWORD = "a test password, long enough"
TEST_TAG = "c0" * 16
_test_hash = []


def signed_in(client):
    """`client`, signed in to the user system as the page is: the test's
    auth.json holding TEST_PASSWORD and TEST_TAG (written straight in, so
    no request is made), its session carrying the tag, and the X-OpenFit
    header on every request it sends."""
    import main
    from werkzeug.security import generate_password_hash

    if not _test_hash:
        _test_hash.append(generate_password_hash(TEST_PASSWORD))
    with open(main.app.config["AUTH_PATH"], "w", encoding="utf-8") as f:
        json.dump({"password_hash": _test_hash[0], "session_tag": TEST_TAG, "setup": None}, f)
    with client.session_transaction() as sess:
        sess.permanent = True
        sess["auth"] = TEST_TAG
    client.environ_base["HTTP_X_OPENFIT"] = "1"
    return client


@pytest.fixture
def db_path(tmp_path):
    """Path to an empty, fully migrated database."""
    path = str(tmp_path / "test.db")
    conn = sqlite3.connect(path)
    run_migrations(conn)
    conn.close()
    return path


@pytest.fixture
def migrations_upto(tmp_path):
    """Factory: a folder of the migrations up to `version` and no further.

    run_migrations(conn, migrations_dir=migrations_upto(7)) applies what
    is pending up to 7 - for testing what one migration does, without a
    later one changing the same rows.
    """

    def make(version):
        partial = tmp_path / f"migrations-upto-{version}"
        if not partial.exists():
            partial.mkdir()
            for v, filename, path in discover_migrations():
                if v <= version:
                    shutil.copy(path, partial / filename)
        return str(partial)

    return make


@pytest.fixture
def db_at_version(tmp_path, migrations_upto):
    """Factory: path to a database migrated up to `version` and no further.

    For testing a migration against the schema it really runs on - a
    production database sits at the previous version, holding data in
    the shape that version left it.
    """

    def make(version, name="at-version.db"):
        path = str(tmp_path / name)
        conn = sqlite3.connect(path)
        run_migrations(conn, migrations_dir=migrations_upto(version))
        conn.close()
        return path

    return make


@pytest.fixture
def conn(db_path):
    """Direct connection to the migrated test database."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    yield conn
    conn.close()


@pytest.fixture
def client(db_path):
    """Flask test client pointed at the temp database, signed in.

    Importing main must not start the scheduler or create tables; if that
    regresses, these tests are the first thing to notice.
    """
    import main

    previous = main.app.config["DB_PATH"]
    main.app.config["DB_PATH"] = db_path
    main.app.config["TESTING"] = True
    with main.app.test_client() as client:
        yield signed_in(client)
    main.app.config["DB_PATH"] = previous


# --- the Admin -------------------------------------------------------------

Call = namedtuple("Call", "args cwd as_owner")

# The containers the fake hands out to the primary (project openfit): the
# one running before an update, then a new one each time compose brings it
# up. Another project's are made up from its name and a count.
CONTAINERS = ["a1" * 32, "b2" * 32, "c3" * 32, "d4" * 32, "e6" * 32, "f7" * 32]
IMAGE_ID = "sha256:" + "e5" * 32
VOLUME = "openfit_tracker-data"
LIBRARY_VOLUME = "openfit-library"
# The Admin's own: the image its container runs, and the update helper's id.
ADMIN_IMAGE_ID = "sha256:" + "ad" * 32
HELPER_ID = "5a" * 32
# What a user's auth_cli.py prints for a setup link, in the fake.
SETUP_TOKEN = "Tk_" + "a1B2-" * 8
SETUP_EXPIRES = "2026-10-28T12:00:00Z"
# What `docker info --format '{{json .}}'` says, in the fake: the four
# fields the Admin keeps, among the many it never shows or stores - the
# host's container counts and names, its images, its plugins.
DOCKER_INFO = {
    "ID": "6c1f2a7e-host-id", "Containers": 23, "ContainersRunning": 19, "ContainersPaused": 0,
    "ContainersStopped": 4, "Images": 41, "Driver": "overlay2", "Name": "nas", "ServerVersion": "27.5.1",
    "NCPU": 8, "MemTotal": 33402789888, "OperatingSystem": "Debian GNU/Linux 12 (bookworm)",
    "KernelVersion": "6.1.0-28-amd64", "DockerRootDir": "/var/lib/docker", "Labels": ["owner=andy"],
    "Plugins": {"Volume": ["local"], "Network": ["bridge", "host"]},
    "Swarm": {"LocalNodeState": "inactive", "NodeAddr": "192.168.1.10"},
    "RegistryConfig": {"IndexConfigs": {"docker.io": {"Name": "docker.io"}}},
    "Warnings": ["a warning naming a container: openfit-sam-tracker-1"],
}


def container_id(project, n):
    if project == "openfit" and n < len(CONTAINERS):
        return CONTAINERS[n]
    return hashlib.sha256(f"{project}:{n}".encode()).hexdigest()


class FakeClock:
    def __init__(self):
        self.now = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc).timestamp()

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds

    def advance(self, seconds):
        self.now += seconds


class FakeRunner:
    """Records every command. git runs for real, against the temp repos;
    docker is answered from a script: `rules` first (a predicate on the
    argument list and a result or a function of it), then the defaults
    below, which play compose projects that build, stop, back up (the
    backup file appears in the Admin's backups) and come up healthy.

    `projects` is what Docker holds: project -> {"n": how many containers
    it's had, "state": running/exited/created, or None for no container,
    "library": whether its container has the app shelf mounted - read, when
    compose makes one, from the compose files it's given}. The primary's,
    openfit, starts with C0 running, and the Admin's own, openfit-admin,
    with its first container running; neither has the shelf (they're from
    before it). `volumes` the volumes there are: the shelf's, openfit-library,
    from the start (the Admin's compose project made it); a project's
    appears when it's brought up, goes with down -v or volume rm. `helper`
    is whether the Admin's update helper, openfit-admin-swap, is there:
    `docker run -d` makes it. A setup link's exec in a user's container
    prints SETUP_TOKEN. `docker info` prints `info` as JSON (DOCKER_INFO).

    `frames` holds, for each call, the names of the functions it was made
    through (innermost first), so a test can tell which code ran it."""

    def __init__(self, repo, backups_dir):
        import admin_ops

        self.real = admin_ops.Runner(repo)
        self.backups_dir = backups_dir
        self.calls = []
        self.results = []
        self.frames = []
        self.rules = []
        self.projects = {"openfit": {"n": 0, "state": "running", "library": False},
                         "openfit-admin": {"n": 0, "state": "running", "library": False}}
        self.helper = False
        self.volumes = {VOLUME, LIBRARY_VOLUME}
        self.images = {"openfit-tracker:latest"}
        self.unhealthy = set()
        self.info = dict(DOCKER_INFO)

    def run(self, args, cwd=None, as_owner=False, timeout=None):
        assert isinstance(args, list), f"a command must be a list, not {args!r}"
        assert all(isinstance(a, str) for a in args), args
        self.calls.append(Call(list(args), cwd, as_owner))
        frame, names = sys._getframe(1), []
        while frame is not None:
            names.append(frame.f_code.co_name)
            frame = frame.f_back
        self.frames.append(names)
        result = self._answer(args, cwd, as_owner, timeout)
        self.results.append(result)
        return result

    def _answer(self, args, cwd, as_owner, timeout):
        for matches, result in self.rules:
            if matches(args):
                return result(args) if callable(result) else result
        if args[0] == "git":
            return self.real.run(args, cwd=cwd, as_owner=as_owner, timeout=timeout)
        return self.docker(args)

    # A1's view: the primary's container, and how many it has had.
    @property
    def container(self):
        return self.projects["openfit"]["n"]

    @property
    def cid(self):
        return self.cid_of("openfit")

    def cid_of(self, project):
        p = self.projects.get(project)
        return container_id(project, p["n"]) if p and p["state"] else None

    def add_project(self, project, state="running"):
        """A project already up when the test starts: its container, its volume."""
        self.projects[project] = {"n": 0, "state": state, "library": False}
        self.volumes.add(f"{project}_tracker-data")

    def _project_of(self, cid):
        return next((name for name in self.projects if self.cid_of(name) == cid), None)

    def docker(self, args):
        sub = args[1]
        if sub == "info":
            return 0, json.dumps(self.info) + "\n", ""
        if sub == "ps" and "name=^openfit-admin-swap$" in args:
            return 0, (HELPER_ID + "\n" if self.helper else ""), ""
        if sub == "ps":
            label = next(a for a in args if a.startswith("label=com.docker.compose.project="))
            project = label.split("=", 2)[2]
            cid = self.cid_of(project)
            if not cid:
                return 0, "", ""
            if "{{.ID}}" == args[-1]:
                return 0, f"{cid}\n", ""
            return 0, f"{cid}\t{self.projects[project]['state']}\n", ""
        if sub == "inspect":
            project = self._project_of(args[-1])
            mounts = [{"Type": "volume", "Name": f"{project}_tracker-data", "Destination": "/data"}]
            if project and self.projects[project].get("library"):
                mounts.append({"Type": "volume", "Name": LIBRARY_VOLUME, "Destination": "/library", "RW": False})
            info = {"Id": args[-1], "Image": ADMIN_IMAGE_ID if project == "openfit-admin" else IMAGE_ID,
                    "Mounts": mounts}
            return 0, json.dumps([info]), ""
        if sub == "exec" and args[3:] == ["python", "auth_cli.py", "setup-link"]:
            return 0, json.dumps({"token": SETUP_TOKEN, "expires_at": SETUP_EXPIRES}) + "\n", ""
        if sub == "exec":
            return (1 if args[2] in self.unhealthy else 0), "", ""
        if sub == "run" and args[2] == "-d" and "openfit-admin-swap" in args:
            self.helper = True
            return 0, HELPER_ID + "\n", ""
        if sub == "run" and "czf" in args:
            name = args[args.index("czf") + 1].rsplit("/", 1)[1]
            with open(os.path.join(self.backups_dir, name), "wb") as f:
                f.write(b"backup")
            return 0, "", ""
        if sub == "image" and args[2] == "inspect":
            if args[-1] in self.images:
                return 0, IMAGE_ID + "\n", ""
            return 1, "", f"Error response from daemon: No such image: {args[-1]}"
        if sub == "volume" and args[2] == "inspect":
            if args[-1] in self.volumes:
                return 0, args[-1] + "\n", ""
            return 1, "", f"Error response from daemon: get {args[-1]}: no such volume"
        if sub == "volume" and args[2] == "rm":
            self.volumes.discard(args[3])
            return 0, args[3] + "\n", ""
        if sub == "compose":
            project = args[3]
            p = self.projects.setdefault(project, {"n": -1, "state": None, "library": False})
            if "up" in args:
                if not p["state"] or "--force-recreate" in args or p["state"] != "running":
                    p["n"] += 1
                    p["library"] = self.mounts_library(args)
                p["state"] = "created" if "--no-start" in args else "running"
                self.volumes.add(f"{project}_tracker-data")
            elif "stop" in args and p["state"]:
                p["state"] = "exited"
            elif "down" in args:
                p["state"] = None
                if "-v" in args:
                    self.volumes.discard(f"{project}_tracker-data")
        return 0, "", ""

    @staticmethod
    def mounts_library(args):
        """Whether the compose files in a compose command mount the shelf (a
        file the temp repo hasn't got - the Admin's own - mounts nothing)."""
        files = [args[i + 1] for i, a in enumerate(args) if a == "-f"]
        texts = []
        for path in files:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    texts.append(f.read())
        return any(f"{LIBRARY_VOLUME}:/library:ro" in text for text in texts)

    def clear(self):
        self.calls.clear()
        self.results.clear()
        self.frames.clear()

    def fail(self, matches, rc=1, err="it went wrong"):
        self.rules.append((matches, (rc, "", err)))

    def commands(self):
        return [c.args for c in self.calls]


def seed_users(env, *users):
    """Users the Admin added before the test: (name, port, state) each -
    users.json, their compose files, and their projects up in the fake
    (state None: no container). The fake's calls cleared."""
    import admin_ops

    reg = {"users": [], "removed": []}
    for i, (name, port, state) in enumerate(users):
        reg["users"].append({"name": name, "port": port, "created_at": f"2026-10-2{i}T09:00:00Z"})
        folder = env.data / "users" / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "docker-compose.yml").write_text(admin_ops.USER_COMPOSE.format(image="openfit-tracker", port=port))
        env.fake.add_project(f"openfit-{name}", state)
    (env.data / "users.json").write_text(json.dumps(reg))
    env.fake.clear()


def _git(cwd, *args):
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


class GitRepos:
    """A bare origin, an upstream clone new commits are made and pushed
    from, and `repo`, the clone standing in for OPENFIT_REPO."""

    def __init__(self, tmp_path):
        self.origin = str(tmp_path / "origin.git")
        self.upstream = str(tmp_path / "upstream")
        self.repo = str(tmp_path / "openfit")
        _git(str(tmp_path), "init", "-q", "--bare", "-b", "main", self.origin)
        _git(str(tmp_path), "clone", "-q", self.origin, self.upstream)
        _git(self.upstream, "symbolic-ref", "HEAD", "refs/heads/main")
        self.write({
            "docker-compose.yml": "services:\n  tracker:\n    build: ./app\n",
            "app/main.py": "print('openfit')\n",
            "admin/admin_server.py": "print('admin')\n",
            "library/steps/app.yaml": "id: steps\n",
            "docs/readme.md": "# OpenFit\n",
        })
        self.commit("Initial commit")
        _git(self.upstream, "push", "-q", "origin", "main")
        _git(str(tmp_path), "clone", "-q", self.origin, self.repo)

    def write(self, files, where=None):
        for name, text in files.items():
            path = os.path.join(where or self.upstream, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(text)

    def commit(self, message, files=None):
        """A commit on upstream's current branch; its short sha."""
        if files:
            self.write(files)
        _git(self.upstream, "add", "-A")
        _git(self.upstream, "commit", "-q", "-m", message)
        return _git(self.upstream, "log", "-1", "--format=%h")

    def push(self, files, message):
        """A commit straight onto main, pushed; its short sha."""
        sha = self.commit(message, files)
        _git(self.upstream, "push", "-q", "origin", "main")
        return sha

    def merge_pr(self, number, commits):
        """A pull request: `commits` [(message, files)] on a branch, merged
        into main with GitHub's merge-commit title, pushed. Short shas."""
        branch = f"pr-{number}"
        _git(self.upstream, "checkout", "-q", "-b", branch)
        shas = [self.commit(message, files) for message, files in commits]
        _git(self.upstream, "checkout", "-q", "main")
        _git(self.upstream, "merge", "-q", "--no-ff", "-m",
             f"Merge pull request #{number} from someone/{branch}\n\nThe PR", branch)
        _git(self.upstream, "push", "-q", "origin", "main")
        return shas

    def head(self, where=None):
        return _git(where or self.repo, "rev-parse", "HEAD")

    def status(self, where=None):
        return _git(where or self.repo, "status", "--porcelain")


@pytest.fixture
def admin_env(tmp_path, monkeypatch):
    """An Admin over temp repos, a FakeRunner and a FakeClock.

    git is kept off the machine's own config. `self_dir` stands for the
    Admin's own running code: a copy of the repo's admin/ as it was
    cloned, so a pull that changes admin/ reads as "pulled, not rebuilt".
    `library` stands for the app shelf, the Admin's /library: an empty
    folder, published to only when a test asks.
    """
    import admin_ops

    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = Test\n\temail = test@example.com\n"
                         "[commit]\n\tgpgsign = false\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repos = GitRepos(tmp_path)
    data = tmp_path / "admin-data"
    running = tmp_path / "running-admin"
    shutil.copytree(os.path.join(repos.repo, "admin"), running)
    library = tmp_path / "library"
    library.mkdir()
    clock = FakeClock()
    fake = FakeRunner(repos.repo, str(data / "backups"))

    def make():
        return admin_ops.Admin(repos.repo, str(data), runner=fake, clock=clock,
                               sleep=clock.sleep, self_dir=str(running), library_dir=str(library))

    env = SimpleNamespace(repos=repos, fake=fake, clock=clock, data=data, make=make, library=library)
    env.admin = make()
    env.backups = data / "backups"
    return env


@pytest.fixture
def admin_client(admin_env):
    """The Admin's Flask app over admin_env, with its Auth on the same clock."""
    import admin_auth
    import admin_server

    auth = admin_auth.Auth(str(admin_env.data), clock=admin_env.clock)
    app = admin_server.create_app(admin_env.admin, auth, b"k" * 32)
    app.config["TESTING"] = True
    admin_env.auth = auth
    with app.test_client() as client:
        yield client


@pytest.fixture
def blocking_build(admin_env):
    """Hold the update at its build until `release` is set."""
    release = threading.Event()
    reached = threading.Event()

    def build(args):
        reached.set()
        release.wait(10)
        return 0, "", ""

    admin_env.fake.rules.append((lambda a: a[:2] == ["docker", "compose"] and "build" in a, build))
    yield SimpleNamespace(release=release, reached=reached)
    release.set()
    admin_env.admin.wait(10)
