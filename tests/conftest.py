"""
Shared test fixtures.

Every test runs against a fresh temp SQLite file - never the real
/data/tracker.db - and nothing here touches the network: Garmin and
Google Health are never contacted. Rows are written by the fake plugin
in test_plugin_contract.py, or by the real plugins with their network
calls stubbed (test_sleep_sessions.py).

The same goes for the encryption key: every test gets a throwaway one in
its tmp_path, so nothing can read or create /data/.secret_key.

The Admin's tests (test_admin_*.py) need no docker and no network
either: git runs for real against temporary repos (a bare "origin" and a
clone standing in for the server's copy), and every docker command goes
to FakeRunner, which records it and answers from a script.
"""

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
    """Flask test client pointed at the temp database.

    Importing main must not start the scheduler or create tables; if that
    regresses, these tests are the first thing to notice.
    """
    import main

    previous = main.app.config["DB_PATH"]
    main.app.config["DB_PATH"] = db_path
    main.app.config["TESTING"] = True
    with main.app.test_client() as client:
        yield client
    main.app.config["DB_PATH"] = previous


# --- the Admin -------------------------------------------------------------

Call = namedtuple("Call", "args cwd as_owner")

# The containers the fake hands out: the user system's before an update,
# then a new one each time compose brings it up.
CONTAINERS = ["a1" * 32, "b2" * 32, "c3" * 32, "d4" * 32]
IMAGE_ID = "sha256:" + "e5" * 32
VOLUME = "openfit_tracker-data"


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
    below, which play a compose project that builds, stops, backs up (the
    backup file appears in the Admin's backups) and comes up healthy."""

    def __init__(self, repo, backups_dir):
        import admin_ops

        self.real = admin_ops.Runner(repo)
        self.backups_dir = backups_dir
        self.calls = []
        self.results = []
        self.rules = []
        self.container = 0
        self.unhealthy = set()
        self.on_backup = None

    def run(self, args, cwd=None, as_owner=False, timeout=None):
        assert isinstance(args, list), f"a command must be a list, not {args!r}"
        assert all(isinstance(a, str) for a in args), args
        self.calls.append(Call(list(args), cwd, as_owner))
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

    @property
    def cid(self):
        return CONTAINERS[self.container]

    def docker(self, args):
        sub = args[1]
        if sub == "ps":
            return 0, f"{self.cid}\trunning\n", ""
        if sub == "inspect":
            info = {"Id": args[-1], "Image": IMAGE_ID,
                    "Mounts": [{"Type": "volume", "Name": VOLUME, "Destination": "/data"}]}
            return 0, json.dumps([info]), ""
        if sub == "exec":
            return (1 if args[2] in self.unhealthy else 0), "", ""
        if sub == "run" and "czf" in args:
            name = args[args.index("czf") + 1].rsplit("/", 1)[1]
            with open(os.path.join(self.backups_dir, name), "wb") as f:
                f.write(b"backup")
            return 0, "", ""
        if sub == "compose" and "up" in args:
            self.container += 1
        return 0, "", ""

    def clear(self):
        self.calls.clear()
        self.results.clear()

    def fail(self, matches, rc=1, err="it went wrong"):
        self.rules.append((matches, (rc, "", err)))

    def commands(self):
        return [c.args for c in self.calls]


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
    clock = FakeClock()
    fake = FakeRunner(repos.repo, str(data / "backups"))

    def make():
        return admin_ops.Admin(repos.repo, str(data), runner=fake, clock=clock,
                               sleep=clock.sleep, self_dir=str(running))

    env = SimpleNamespace(repos=repos, fake=fake, clock=clock, data=data, make=make)
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
