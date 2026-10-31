"""Restoring a backup as a new user (R1).

A new user is made from an uploaded backup, one of the server's own
backups, or a removed user's kept data. The archive is read with tarfile,
never extracted by the Admin: every member walked and checked first (only
files and folders, plain names inside it, at most 200,000 of them and 8 GB
unpacked, a sound tracker.db no newer than this server's OpenFit), then a
clean copy written of the members it checked - never the password, its
sessions' key or the app update notes, and without the device logins unless
they're kept - and only a helper extracts that, into the new user's brand
new volume, created by `up --no-start` and started once it's filled. A
failure from the create on is taken back with `down -v`, safe because that
volume was made moments ago by this op.

As in the other Admin tests, git runs for real against temp repos and
docker is conftest's FakeRunner; the archives here are real.
"""

import gzip
import io
import json
import os
import sqlite3
import subprocess
import tarfile
import tempfile

import pytest
from conftest import Call, container_id, seed_users

import admin_ops
import admin_server

H = {"X-OpenFit-Admin": "1"}
PASSWORD = "a long password!"
BACKUP = "20261027T120000Z-abcdef0-andy.tar.gz"
NEWEST = 10  # the newest migration the test repo's OpenFit has

# A user's /data, beside its database: the password and its sessions' key,
# the library apps' update notes, the device logins (the credentials key and
# Garmin's tokens), an app, and names like theirs that aren't them.
FILES = {
    "auth.json": b'{"password_hash": "pbkdf2:sha256:secret", "session_tag": "t"}',
    ".session_key": b"s" * 32,
    "app_updates.json": b'{"openfit.frms": {"state": "updated"}}',
    ".secret_key": b"the credentials key",
    ".garminconnect/oauth1_token.json": b'{"oauth_token": "garmin"}',
    ".garminconnect/oauth2_token.json": b'{"access_token": "garmin2"}',
    "apps/weight/app.yaml": b"id: weight\nname: Weight\n",
    "apps/weight/auth.json": b"an app's own file, not the password",
    ".garminconnect-notes.txt": b"not Garmin's folder",
    "auth.json.lock": b"",
}
FOLDERS = (".garminconnect", "apps", "apps/weight")


@pytest.fixture
def logged_in(admin_client, admin_env):
    admin_env.auth.setup(admin_env.auth.setup_code, PASSWORD)
    assert admin_client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
    return admin_client


@pytest.fixture
def env(admin_env):
    """The Admin, its repo's OpenFit holding migrations 001 to NEWEST at
    HEAD - and every clean archive the restore helper is given, kept as
    it was the moment the helper ran (`env.extracted`)."""
    repo = admin_env.repos.repo
    for n in range(1, NEWEST + 1):
        path = os.path.join(repo, "app", "migrations", f"{n:03d}_step.sql")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(f"-- migration {n}\n")
    for args in (["add", "-A"], ["commit", "-q", "-m", "Migrations"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    admin_env.extracted = []

    def helper(args):
        name = args[args.index("xzf") + 1].removeprefix("/b/restore-uploads/")
        admin_env.extracted.append((name, (admin_env.data / "restore-uploads" / name).read_bytes()))
        return 0, "", ""

    admin_env.fake.rules.append((lambda a: a[:2] == ["docker", "run"] and "xzf" in a, helper))
    admin_env.fake.clear()
    return admin_env


def database(version=NEWEST, rows=3, table=True):
    """A tracker.db's bytes: migrations 1..version applied, and some data."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    conn = sqlite3.connect(path)
    if table:
        conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT)")
        conn.executemany("INSERT INTO schema_migrations VALUES (?, 'then')", [(v,) for v in range(1, version + 1)])
    conn.execute("CREATE TABLE metrics (date TEXT, value REAL)")
    conn.executemany("INSERT INTO metrics VALUES (?, ?)", [(f"2026-10-{d:02d}", d * 1.5) for d in range(1, rows + 1)])
    conn.commit()
    conn.close()
    with open(path, "rb") as f:
        data = f.read()
    os.unlink(path)
    return data


def entry(name, body=b"", kind=tarfile.REGTYPE, **extra):
    return {"name": name, "body": body, "type": kind, **extra}


def standard(db=None, prefix="./"):
    """A backup's members, as GNU tar lists `tar czf - -C /v .`."""
    out = [entry(prefix or ".", kind=tarfile.DIRTYPE)]
    out += [entry(prefix + f, kind=tarfile.DIRTYPE) for f in FOLDERS]
    out.append(entry(prefix + "tracker.db", database() if db is None else db))
    out += [entry(prefix + name, body) for name, body in FILES.items()]
    return out


def tar_gz(entries):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        for e in entries:
            info = tarfile.TarInfo(e["name"])
            info.type = e["type"]
            info.mode = e.get("mode", 0o755 if e["type"] == tarfile.DIRTYPE else 0o600)
            info.mtime, info.uid, info.gid, info.uname, info.gname = 1761566400, 1000, 1000, "andy", "andy"
            if "linkname" in e:
                info.linkname = e["linkname"]
            if "size" in e:
                info.size = e["size"]
            elif e["type"] in (tarfile.REGTYPE, tarfile.AREGTYPE):
                info.size = len(e["body"])
            tar.addfile(info, io.BytesIO(e["body"]) if info.size else None)
    return buf.getvalue()


def contents(blob):
    """{name: (type, mode, uid, mtime, bytes)} of a tar.gz."""
    out = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for m in tar:
            out[m.name] = (m.type, m.mode, m.uid, m.mtime, tar.extractfile(m).read() if m.isreg() else None)
    return out


def upload(env, blob, name="sam", port=8101, keep=True, host="192.168.1.10:8090"):
    """An upload as the route leaves it, then the restore: its op once done."""
    path, f = env.admin.new_upload()
    with f:
        f.write(blob)
    env.upload = path
    env.admin.start_restore(name, port, keep, ("upload", path), host)
    env.admin.wait(10)
    return env.admin.current_op()


def left(env):
    """What's in /data/restore-uploads/: nothing, once a restore has ended."""
    folder = env.data / "restore-uploads"
    return sorted(os.listdir(folder)) if folder.exists() else []


def registry(env):
    path = env.data / "users.json"
    return json.loads(path.read_text()) if path.exists() else {"users": [], "removed": []}


def docker(*args):
    return Call(["docker", *args], None, False)


def ucompose(env, name, *args):
    folder = str(env.data / "users" / name)
    return Call(["docker", "compose", "-p", f"openfit-{name}", "-f", os.path.join(folder, "docker-compose.yml"),
                 *args], folder, False)


def ps(project):
    return docker("ps", "-a", "--no-trunc", "--filter", f"label=com.docker.compose.project={project}",
                  "--filter", "label=com.docker.compose.service=tracker",
                  "--filter", "label=com.docker.compose.oneoff=False", "--format", "{{.ID}}\t{{.State}}")


def helper_call(clean, name="sam"):
    return docker("run", "--rm", "--label", "openfit.admin.helper=true", "--log-driver", "none",
                  "-v", f"openfit-{name}_tracker-data:/v", "-v", "openfit-admin_admin-data:/b:ro",
                  "openfit-admin:local", "tar", "xzf", f"/b/restore-uploads/{clean}", "-C", "/v")


IMAGE = docker("image", "inspect", "--format", "{{.Id}}", "openfit-tracker:latest")
SHELF = docker("volume", "inspect", "--format", "{{.Name}}", "openfit-library")


def volume_check(name):
    return docker("volume", "inspect", "--format", "{{.Name}}", f"openfit-{name}_tracker-data")


def dockers(env):
    """The docker commands run (git's are recorded too)."""
    return [c for c in env.fake.calls if c.args[0] == "docker"]


def nothing_created(env, before=None):
    """Nothing made: no compose command and no helper run, no compose file,
    users.json as it was, the restore's own files gone."""
    commands = env.fake.commands()
    assert not any(c[:2] == ["docker", "compose"] for c in commands), commands
    assert not any(c[:2] == ["docker", "run"] for c in commands), commands
    assert not (env.data / "users" / "sam").exists()
    assert registry(env) == (before or {"users": [], "removed": []})
    assert "openfit-sam_tracker-data" not in env.fake.volumes
    assert env.extracted == []
    assert left(env) == []


# --- 1. the archive's checks: each refusal on its own -------------------------------------------


def _without(entries, name):
    return [e for e in entries if e["name"] != name]


REFUSALS = {
    "a symbolic link": (lambda: standard() + [entry("./latest.db", kind=tarfile.SYMTYPE, linkname="tracker.db")],
                        "It holds a symbolic link (./latest.db), which a backup never does."),
    "a hard link": (lambda: standard() + [entry("./copy.db", kind=tarfile.LNKTYPE, linkname="./tracker.db")],
                    "It holds a hard link (./copy.db), which a backup never does."),
    "a device": (lambda: standard() + [entry("./null", kind=tarfile.CHRTYPE, devmajor=1)],
                 "It holds a device (./null), which a backup never does."),
    "a fifo": (lambda: standard() + [entry("./pipe", kind=tarfile.FIFOTYPE)],
               "It holds a FIFO (./pipe), which a backup never does."),
    "an absolute name": (lambda: standard() + [entry("/etc/cron.d/x", b"* * * * * root sh")],
                         "It holds an absolute name (/etc/cron.d/x), which a backup never does."),
    "a .. part": (lambda: standard() + [entry("./apps/../../escape", b"x")],
                  "It holds a name that leads out of it (apps/../../escape)."),
    "a name twice": (lambda: standard() + [entry("./tracker.db", database(rows=9))],
                     "It holds tracker.db twice, which a backup never does."),
    "a long part": (lambda: standard() + [entry("./" + "x" * 256, b"x")],
                    "It holds a name with a part longer than 255 characters"),
    "no tracker.db": (lambda: _without(standard(), "./tracker.db"),
                      "There's no tracker.db in it, so it isn't an OpenFit backup."),
    "a corrupt database": (lambda: standard(db=b"SQLite format 3\x00" + b"\xff" * 4096),
                           "Its tracker.db is damaged, so it can't be restored."),
    "no schema_migrations": (lambda: standard(db=database(table=False)),
                             "Its tracker.db has no schema_migrations table, so it isn't one OpenFit wrote."),
    "a newer version": (lambda: standard(db=database(version=NEWEST + 1)),
                        "This backup is from a newer OpenFit than this server runs. Update OpenFit first."),
}


@pytest.mark.parametrize("case", list(REFUSALS))
def test_each_refusal_creates_nothing_and_runs_no_helper(env, case):
    build, said = REFUSALS[case]
    op = upload(env, tar_gz(build()))
    assert op["kind"] == "restore_user" and op["outcome"] == "failed"
    assert said in op["summary"] and op["summary"].endswith("Nothing was created.")
    assert dockers(env) == [IMAGE, SHELF, volume_check("sam")]  # the request's checks, then nothing
    nothing_created(env)


def test_too_many_members_is_refused(env, monkeypatch):
    monkeypatch.setattr(admin_ops, "MAX_MEMBERS", len(standard()) - 1)
    op = upload(env, tar_gz(standard()))
    assert op["outcome"] == "failed" and f"more than {len(standard()) - 1:,} files and folders" in op["summary"]
    nothing_created(env)
    monkeypatch.setattr(admin_ops, "MAX_MEMBERS", len(standard()))
    env.extracted.clear()
    assert upload(env, tar_gz(standard()))["outcome"] == "ok"  # exactly at it is fine


def test_too_large_unpacked_is_refused(env, monkeypatch):
    total = sum(len(e["body"]) for e in standard())
    monkeypatch.setattr(admin_ops, "MAX_UNPACKED", total - 1)
    op = upload(env, tar_gz(standard()))
    assert op["outcome"] == "failed" and "Unpacked, it would be more than" in op["summary"]
    nothing_created(env)


def test_a_huge_member_is_refused_from_its_header_alone(env):
    # A header claiming 9 GB, and no data after it: refused on the sum of the
    # sizes, before any of it would be read.
    raw = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(tar_gz(standard())), mode="r:gz") as tar, \
            tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as out:
        for m in tar:
            out.addfile(m, tar.extractfile(m) if m.isreg() else None)
        end = out.offset  # before the end-of-archive blocks
    big = tarfile.TarInfo("./big.bin")
    big.size = 9 * 1024 ** 3
    op = upload(env, gzip.compress(raw.getvalue()[:end] + big.tobuf(tarfile.PAX_FORMAT)))
    assert op["outcome"] == "failed" and "Unpacked, it would be more than 8 GB" in op["summary"]
    nothing_created(env)


@pytest.mark.parametrize("blob", [b"", b"not an archive at all", b"\x1f\x8b\x08\x00garbage"])
def test_a_file_that_isnt_a_tar_gz_is_refused(env, blob):
    op = upload(env, blob)
    assert op["outcome"] == "failed"
    assert op["summary"] == ("It isn't a backup the Admin can read: it should be a .tar.gz. Nothing was created.")
    nothing_created(env)


@pytest.mark.parametrize("name, said", [
    ("a\x00b", "NUL byte"),
    ("/abs", "absolute name"),
    ("./a/../b", "leads out of it"),
    ("..", "leads out of it"),
    ("a//b", "isn't a plain path"),
    ("a/./b", "isn't a plain path"),
    ("./" + "y" * 256 + "/z", "longer than 255"),
])
def test_member_name_refuses_each_bad_name(name, said):
    with pytest.raises(admin_ops.OpError) as e:
        admin_ops.member_name(tarfile.TarInfo(name))
    assert said in e.value.message


@pytest.mark.parametrize("name, normal", [
    ("./tracker.db", "tracker.db"), ("tracker.db", "tracker.db"), ("./", ""), (".", ""), ("././a/b", "a/b"),
    ("./" + "y" * 255, "y" * 255),
])
def test_member_name_strips_a_leading_dot_slash(name, normal):
    info = tarfile.TarInfo(name)
    if name in ("./", "."):
        info.type = tarfile.DIRTYPE
    assert admin_ops.member_name(info) == normal


def test_a_database_with_its_wal_is_checked_with_it(env, tmp_path):
    # The newest migration only in the WAL: the check sees it, as SQLite would.
    path = str(tmp_path / "w.db")
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA wal_autocheckpoint = 0")
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT)")
    conn.executemany("INSERT INTO schema_migrations VALUES (?, 'then')", [(v,) for v in range(1, NEWEST + 1)])
    conn.commit()
    def files():
        out = {}
        for name in ("", "-wal"):
            with open(path + name, "rb") as f:
                out[name] = f.read()
        return out

    snapshot = files()
    conn.execute("INSERT INTO schema_migrations VALUES (?, 'later')", (NEWEST + 1,))
    conn.commit()
    newer = files()
    conn.close()
    assert snapshot[""] == newer[""]  # the main file hasn't moved: it's all in the WAL

    entries = _without(standard(), "./tracker.db")
    op = upload(env, tar_gz(entries + [entry("./tracker.db", newer[""]), entry("./tracker.db-wal", newer["-wal"])]))
    assert op["outcome"] == "failed" and admin_ops.NEWER_BACKUP in op["summary"]
    nothing_created(env)
    op = upload(env, tar_gz(entries + [entry("./tracker.db", snapshot[""]),
                                       entry("./tracker.db-wal", snapshot["-wal"])]))
    assert op["outcome"] == "ok"


def test_the_newest_migration_is_the_repos_at_head(env):
    assert env.admin.newest_migration() == NEWEST
    repo = env.repos.repo
    with open(os.path.join(repo, "app", "migrations", "README.md"), "w") as f:
        f.write("not a migration\n")
    with open(os.path.join(repo, "app", "migrations", "099_uncommitted.sql"), "w") as f:
        f.write("-- not at HEAD\n")
    subprocess.run(["git", "add", "app/migrations/README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "Readme"], cwd=repo, check=True)
    assert env.admin.newest_migration() == NEWEST  # the working tree's stray file isn't at HEAD


# --- 2. the clean archive ---------------------------------------------------------------------------


@pytest.mark.parametrize("keep", [True, False])
@pytest.mark.parametrize("prefix", ["./", ""])
def test_the_clean_archive_is_the_checked_members_less_what_is_never_restored(env, keep, prefix):
    entries = standard(prefix=prefix)
    op = upload(env, tar_gz(entries), keep=keep)
    assert op["outcome"] == "ok", op["log"]
    [(clean, blob)] = env.extracted
    assert admin_ops.CLEAN_FILE.match(clean)
    got = contents(blob)

    names = set(got)
    for never in ("./auth.json", "./.session_key", "./app_updates.json"):
        assert never not in names
    devices = {"./.secret_key", "./.garminconnect", "./.garminconnect/oauth1_token.json",
               "./.garminconnect/oauth2_token.json"}
    assert devices <= names if keep else not devices & names
    # Every other member there, as it was: its bytes, mode, owner and time.
    original = {}
    for e in entries:
        name = "./" + e["name"].removeprefix("./") if e["name"] not in ("./", ".") else "."
        original[name] = e
    expected = {n for n in original if n not in {"./auth.json", "./.session_key", "./app_updates.json"}
                and (keep or n not in devices)}
    assert names == expected
    for name in expected:
        e = original[name]
        kind, mode, uid, mtime, body = got[name]
        assert kind == (tarfile.DIRTYPE if e["type"] == tarfile.DIRTYPE else tarfile.REGTYPE), name
        assert (mode, uid, mtime) == (e.get("mode", 0o755 if kind == tarfile.DIRTYPE else 0o600), 1000, 1761566400)
        if kind == tarfile.REGTYPE:
            assert body == e["body"], name  # byte for byte
    assert "./.garminconnect-notes.txt" in names and "./apps/weight/auth.json" in names  # only the exact names go
    said = next(line["text"] for line in op["log"] if line["text"].startswith("Left out: "))
    for name in ("auth.json", ".session_key", "app_updates.json"):
        assert name in said
    assert (".secret_key" in said) is not keep
    assert ("The device logins are kept." if keep else "The device logins aren't kept: remove and re-add each "
            "device in sam's OpenFit.") in said


def test_the_clean_archive_drops_setuid_bits_and_extra_headers(tmp_path):
    entries = standard() + [entry("./suid", b"#!/bin/sh\n", mode=0o4755)]
    src = tmp_path / "in.tar.gz"
    src.write_bytes(tar_gz(entries))
    dest = str(tmp_path / "out.tar.gz")
    with tarfile.open(src, "r:gz") as tar:
        members = admin_ops.check_members(tar)
        admin_ops.write_clean(tar, members, dest, True)
    assert oct(os.stat(dest).st_mode & 0o777) == "0o600"
    with tarfile.open(dest, "r:gz") as tar:
        assert tar.getmember("./suid").mode == 0o755


# --- 3. restoring from an upload --------------------------------------------------------------------


def test_restoring_an_upload_runs_in_the_exact_order(logged_in, env):
    listed = []

    def exec_(args):  # users.json, at the moment the health check runs
        path = env.data / "users.json"
        listed.append(path.exists() and "sam" in path.read_text())
        return 0, "", ""

    env.fake.rules.append((lambda a: a[1] == "exec", exec_))
    blob = tar_gz(standard())
    r = logged_in.post("/api/users/restore", headers=H, content_type="multipart/form-data",
                       data={"name": "sam", "port": "8101", "keep_devices": "true",
                             "file": (io.BytesIO(blob), "andy-backup.tar.gz")})
    assert r.status_code == 202, r.get_json()
    assert r.get_json()["op"]["kind"] == "restore_user"
    env.admin.wait(10)
    op = env.admin.current_op()

    [(clean, extracted)] = env.extracted
    # The helper extracts the clean archive the Admin wrote, never the upload as it came.
    assert admin_ops.CLEAN_FILE.match(clean) and extracted != blob
    assert "./tracker.db" in contents(extracted) and "./auth.json" not in contents(extracted)
    assert dockers(env) == [
        IMAGE, SHELF, volume_check("sam"),
        ucompose(env, "sam", "up", "--no-start", "--no-build"),
        helper_call(clean),
        ucompose(env, "sam", "up", "-d", "--no-build"),
        ps("openfit-sam"),
        Call(["docker", "exec", container_id("openfit-sam", 1), "python", "-c", admin_ops.HEALTH_CODE], None, False),
    ]
    assert listed == [False]  # users.json gains them only once they answered
    assert op["outcome"] == "ok"
    assert op["summary"] == "Restored sam. They start locked: use Setup link to give them a password."
    assert op["user"] == {"name": "sam", "port": 8101}
    assert op["source"] == {"kind": "upload"} and op["keep_devices"] is True
    assert "Their OpenFit is at http://localhost:8101" in [line["text"] for line in op["log"]]
    assert registry(env) == {"users": [{"name": "sam", "port": 8101, "created_at": "2026-10-27T12:00:00Z"}],
                             "removed": []}
    assert left(env) == []  # the upload, the clean archive and the database's copy all gone
    s = logged_in.get("/api/status").get_json()
    assert [u["name"] for u in s["users"]] == ["andy", "sam"]
    assert s["activity"][0]["user"] == {"name": "sam", "port": 8101}


def test_the_container_is_created_before_it_is_filled_and_started_after(env):
    seen = []

    def helper(args):
        seen.append(env.fake.projects["openfit-sam"]["state"])
        return 0, "", ""

    env.fake.rules.insert(0, (lambda a: a[:2] == ["docker", "run"] and "xzf" in a, helper))
    assert upload(env, tar_gz(standard()))["outcome"] == "ok"
    assert seen == ["created"]  # made, not started, when the data goes in
    assert env.fake.projects["openfit-sam"]["state"] == "running"


def test_the_upload_is_deleted_after_a_failure_too(env):
    env.fake.unhealthy = {container_id("openfit-sam", 1)}
    op = upload(env, tar_gz(standard()))
    assert op["outcome"] == "failed"
    assert not os.path.exists(env.upload)
    assert left(env) == []


def test_leftovers_are_deleted_at_startup(env):
    folder = env.data / "restore-uploads"
    folder.mkdir(exist_ok=True)
    (folder / ("ab" * 16 + ".tar.gz")).write_bytes(b"half an upload")
    (folder / ("cd" * 16 + "-db")).mkdir()
    (folder / ("cd" * 16 + "-db" ) / "tracker.db").write_bytes(b"x")
    env.make()
    assert left(env) == []


def test_the_upload_file_is_made_private(env):
    path, f = env.admin.new_upload()
    f.close()
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert oct(os.stat(env.data / "restore-uploads").st_mode & 0o777) == "0o700"
    assert admin_ops.RESTORE_FILE.match(os.path.basename(path))
    env.admin.drop_upload(path)
    assert left(env) == []


def test_an_upload_over_the_cap_is_413_and_deleted(logged_in, env, monkeypatch):
    blob = tar_gz(standard())
    monkeypatch.setattr(admin_server, "UPLOAD_MAX", len(blob) - 1)
    form = {"name": "sam", "port": "8101", "keep_devices": "true"}
    # The file part past the cap, the whole body inside the margin: stopped as it's written.
    r = logged_in.post("/api/users/restore", headers=H, content_type="multipart/form-data",
                       data={**form, "file": (io.BytesIO(blob), "b.tar.gz")})
    assert r.status_code == 413
    assert r.get_json() == {"ok": False, "error": "That file is too large: a backup to restore is at most 2 GB."}
    # The body past the cap and the margin: refused before it's read.
    monkeypatch.setattr(admin_server, "UPLOAD_MARGIN", 0)
    monkeypatch.setattr(admin_server, "UPLOAD_MAX", 100)
    r = logged_in.post("/api/users/restore", headers=H, content_type="multipart/form-data",
                       data={**form, "file": (io.BytesIO(blob), "b.tar.gz")})
    assert r.status_code == 413
    nothing_created(env)
    assert env.fake.calls == [] and env.admin.history() == []
    # At the cap exactly, it goes through.
    monkeypatch.setattr(admin_server, "UPLOAD_MARGIN", 64 * 1024)
    monkeypatch.setattr(admin_server, "UPLOAD_MAX", len(blob))
    r = logged_in.post("/api/users/restore", headers=H, content_type="multipart/form-data",
                       data={**form, "file": (io.BytesIO(blob), "b.tar.gz")})
    assert r.status_code == 202
    env.admin.wait(10)
    assert env.admin.current_op()["outcome"] == "ok"


def test_the_flask_cap_is_two_gb_and_a_margin(admin_client):
    assert admin_client.application.config["MAX_CONTENT_LENGTH"] == 2 * 1024 ** 3 + admin_server.UPLOAD_MARGIN
    assert admin_server.UPLOAD_MARGIN <= 1024 * 1024


def test_every_other_route_is_held_to_a_megabyte(logged_in, env):
    r = logged_in.post("/api/users", json={"name": "sam", "port": 8101, "pad": "x" * (1024 * 1024)}, headers=H)
    assert r.status_code == 413
    assert env.fake.calls == []


@pytest.mark.parametrize("data, said", [
    ({"name": "sam", "port": "8101", "keep_devices": "true"}, "Choose a backup file to restore."),
    ({"name": "sam", "port": "8101", "keep_devices": "true", "file": (io.BytesIO(b""), "empty.tar.gz")},
     "Choose a backup file to restore."),
    ({"name": "sam", "port": "8101", "keep_devices": "maybe", "file": (io.BytesIO(b"x"), "b.tar.gz")},
     "Say whether to keep the device logins: true or false."),
    ({"name": "sam", "port": "8101", "file": (io.BytesIO(b"x"), "b.tar.gz")},
     "Say whether to keep the device logins: true or false."),
    ({"name": "sam", "port": "eighty", "keep_devices": "true", "file": (io.BytesIO(b"x"), "b.tar.gz")},
     "A port is a whole number from 1024 to 65535."),
])
def test_an_upload_that_isnt_whole_is_400_and_deleted(logged_in, env, data, said):
    r = logged_in.post("/api/users/restore", headers=H, content_type="multipart/form-data", data=data)
    assert r.status_code == 400 and r.get_json()["error"] == said
    nothing_created(env)
    assert env.admin.history() == []


def test_two_files_are_refused(logged_in, env):
    r = logged_in.post("/api/users/restore", headers=H, content_type="multipart/form-data",
                       data={"name": "sam", "port": "8101", "keep_devices": "true",
                             "file": [(io.BytesIO(b"one"), "a.tar.gz"), (io.BytesIO(b"two"), "b.tar.gz")]})
    assert r.status_code == 400
    nothing_created(env)


# --- 4. from the server's own backup, and from kept data ---------------------------------------------


def test_restoring_one_of_the_servers_backups(logged_in, env):
    (env.backups / BACKUP).write_bytes(tar_gz(standard()))
    r = logged_in.post("/api/users/restore-from", headers=H,
                       json={"name": "sam", "port": 8101, "keep_devices": False, "backup": BACKUP})
    assert r.status_code == 202, r.get_json()
    env.admin.wait(10)
    op = env.admin.current_op()
    assert op["outcome"] == "ok"
    assert op["source"] == {"kind": "backup", "name": BACKUP, "user": "andy"}
    assert "Restoring sam from the backup " + BACKUP + "." in [line["text"] for line in op["log"]]
    [(_clean, blob)] = env.extracted
    assert "./tracker.db" in contents(blob) and "./.secret_key" not in contents(blob)
    assert (env.backups / BACKUP).exists()  # read, never moved or changed
    assert left(env) == []


@pytest.mark.parametrize("name", ["20261027T120000Z-abcdef0-nobody.tar.gz", "../users.json", "x", None, 7])
def test_a_backup_not_in_the_listing_is_404(logged_in, env, name):
    r = logged_in.post("/api/users/restore-from", headers=H,
                       json={"name": "sam", "port": 8101, "keep_devices": True, "backup": name})
    assert r.status_code == (400 if name is None else 404)
    nothing_created(env)
    assert env.admin.history() == []


def test_restoring_a_removed_users_kept_data_packs_it_first(logged_in, env):
    seed_users(env, ("kim", 8102, "running"))
    env.admin.remove_user("kim")
    before = registry(env)
    env.fake.clear()
    env.fake.stream_out = tar_gz(standard())
    r = logged_in.post("/api/users/restore-from", headers=H,
                       json={"name": "lee", "port": 8103, "keep_devices": True, "removed": "kim"})
    assert r.status_code == 202, r.get_json()
    env.admin.wait(10)
    op = env.admin.current_op()
    assert op["outcome"] == "ok", op["log"]
    assert op["source"] == {"kind": "removed", "name": "kim", "user": "kim"}
    pack = ["docker", "run", "--rm", "--label", "openfit.admin.helper=true", "--log-driver", "none",
            "-v", "openfit-kim_tracker-data:/v:ro", "openfit-admin:local", "tar", "czf", "-", "-C", "/v", "."]
    [(clean, _blob)] = env.extracted
    assert [c.args for c in dockers(env)] == [
        ["docker", "ps", "-a", "--no-trunc", "--filter", "label=com.docker.compose.project=openfit-kim",
         "--format", "{{.ID}}"],  # under the lock: no container in kim's project
        IMAGE.args, SHELF.args, volume_check("lee").args,
        pack,  # kim's kept data packed first, into a file, nothing excluded
        ucompose(env, "lee", "up", "--no-start", "--no-build").args,
        helper_call(clean, "lee").args,
        ucompose(env, "lee", "up", "-d", "--no-build").args,
        ps("openfit-lee").args,
        ["docker", "exec", container_id("openfit-lee", 1), "python", "-c", admin_ops.HEALTH_CODE],
    ]
    # kim's kept data is still kept, as it was; lee is new beside it.
    assert "openfit-kim_tracker-data" in env.fake.volumes
    assert registry(env)["removed"] == before["removed"]
    assert [u["name"] for u in registry(env)["users"]] == ["lee"]
    assert left(env) == []  # the packed file gone too


def test_kept_data_that_wont_pack_creates_nothing(env):
    seed_users(env, ("kim", 8102, "running"))
    env.admin.remove_user("kim")
    before = registry(env)
    env.fake.clear()
    env.fake.stream_rc, env.fake.stream_err = 1, "no such volume"
    env.admin.start_restore("lee", 8103, True, ("removed", "kim"), "h")
    env.admin.wait(10)
    op = env.admin.current_op()
    assert op["outcome"] == "failed" and op["summary"] == "Couldn't read kim's kept data. Nothing was created."
    assert not any(c[:2] == ["docker", "compose"] for c in env.fake.commands())
    assert registry(env) == before
    assert left(env) == []


def test_kept_data_with_a_container_again_is_409(logged_in, env):
    seed_users(env, ("kim", 8102, "running"))
    env.admin.remove_user("kim")
    env.fake.projects["openfit-kim"]["state"] = "exited"  # someone made one by hand
    r = logged_in.post("/api/users/restore-from", headers=H,
                       json={"name": "lee", "port": 8103, "keep_devices": True, "removed": "kim"})
    assert r.status_code == 409
    assert "kim has a container again" in r.get_json()["error"]
    assert not env.admin.lock.locked()


def test_unknown_kept_data_is_404(logged_in, env):
    r = logged_in.post("/api/users/restore-from", headers=H,
                       json={"name": "lee", "port": 8103, "keep_devices": True, "removed": "nobody"})
    assert r.status_code == 404
    assert env.fake.calls == []


@pytest.mark.parametrize("body", [
    {"name": "sam", "port": 8101, "keep_devices": True},
    {"name": "sam", "port": 8101, "keep_devices": True, "backup": BACKUP, "removed": "kim"},
])
def test_restore_from_names_exactly_one_source(logged_in, env, body):
    r = logged_in.post("/api/users/restore-from", headers=H, json=body)
    assert r.status_code == 400
    assert r.get_json()["error"] == "Name one backup, or one removed user's kept data, to restore."
    assert env.fake.calls == []


# --- 5. names and ports ------------------------------------------------------------------------------


def _restore_from(client, name, port=8101, keep=True):
    return client.post("/api/users/restore-from", headers=H,
                       json={"name": name, "port": port, "keep_devices": keep, "backup": BACKUP})


def _restore_upload(client, name, port="8101"):
    return client.post("/api/users/restore", headers=H, content_type="multipart/form-data",
                       data={"name": name, "port": port, "keep_devices": "true",
                             "file": (io.BytesIO(tar_gz(standard())), "b.tar.gz")})


def test_a_name_with_kept_data_is_409(logged_in, env):
    (env.backups / BACKUP).write_bytes(tar_gz(standard()))
    seed_users(env, ("kim", 8102, "running"))
    env.admin.remove_user("kim")
    env.fake.clear()
    for r in (_restore_from(logged_in, "kim", 8105), _restore_upload(logged_in, "kim", "8105")):
        assert r.status_code == 409
        assert r.get_json()["error"] == "kim has kept data on this server. Choose another name."
    assert env.fake.calls == [] and left(env) == []
    assert [o["kind"] for o in env.admin.history()] == []


def test_a_taken_name_is_409(logged_in, env):
    (env.backups / BACKUP).write_bytes(tar_gz(standard()))
    seed_users(env, ("sam", 8101, "running"))
    for r in (_restore_from(logged_in, "sam", 8105), _restore_upload(logged_in, "sam", "8105"),
              _restore_from(logged_in, "lee", 8101)):
        assert r.status_code == 409, r.get_json()
    assert env.fake.calls == [] and left(env) == []


@pytest.mark.parametrize("name", ["Sam", "sam-1", "admin", "andy", "a" * 21, "../x", "", None, 7])
def test_an_invalid_name_is_400(logged_in, env, name):
    (env.backups / BACKUP).write_bytes(tar_gz(standard()))
    assert _restore_from(logged_in, name).status_code == 400
    if isinstance(name, str):
        assert _restore_upload(logged_in, name).status_code == 400
    assert env.fake.calls == [] and left(env) == [] and env.admin.history() == []


@pytest.mark.parametrize("port", [80, 8090, 70000, "8101", 8101.5, True, None])
def test_an_invalid_port_is_400(logged_in, env, port):
    (env.backups / BACKUP).write_bytes(tar_gz(standard()))
    assert _restore_from(logged_in, "sam", port).status_code == 400
    assert env.fake.calls == []


@pytest.mark.parametrize("keep", ["true", 1, None])
def test_keep_devices_is_a_bool(logged_in, env, keep):
    (env.backups / BACKUP).write_bytes(tar_gz(standard()))
    r = _restore_from(logged_in, "sam", keep=keep)
    assert r.status_code == 400 and r.get_json()["error"] == "Say whether to keep the device logins: true or false."


def test_unrecorded_storage_under_the_name_is_409(logged_in, env):
    (env.backups / BACKUP).write_bytes(tar_gz(standard()))
    env.fake.volumes.add("openfit-sam_tracker-data")
    r = _restore_from(logged_in, "sam")
    assert r.status_code == 409 and "openfit-sam_tracker-data" in r.get_json()["error"]
    assert not any(c[:2] == ["docker", "compose"] for c in env.fake.commands())
    assert "openfit-sam_tracker-data" in env.fake.volumes


def test_not_built_yet_is_409(logged_in, env):
    (env.backups / BACKUP).write_bytes(tar_gz(standard()))
    env.fake.images.clear()
    r = _restore_from(logged_in, "sam")
    assert r.status_code == 409 and r.get_json()["error"] == "OpenFit hasn't been built on this server yet."


def test_a_restore_waits_its_turn(logged_in, env):
    (env.backups / BACKUP).write_bytes(tar_gz(standard()))
    env.admin.lock.acquire()  # something else running
    try:
        for r in (_restore_upload(logged_in, "sam"), _restore_from(logged_in, "sam")):
            assert r.status_code == 409 and "Something else is running" in r.get_json()["error"]
    finally:
        env.admin.lock.release()
    assert left(env) == []  # the upload went with the refusal
    assert env.admin.history() == []


# --- 6. a failure after the create is taken back ------------------------------------------------------


@pytest.mark.parametrize("how, said", [
    ("unhealthy", "sam wasn't restored: their OpenFit didn't answer."),
    ("helper", "sam wasn't restored: the backup's data didn't go into their storage."),
    ("start", "Port 8101 is already in use on the server. Choose another."),
    ("create", "sam wasn't restored: their OpenFit wasn't created."),
])
def test_a_failure_after_the_create_is_down_v_on_that_project_only(env, how, said):
    seed_users(env, ("kim", 8102, "running"), ("bob", 8103, "exited"))
    before = registry(env)
    volumes = set(env.fake.volumes)
    if how == "unhealthy":
        env.fake.unhealthy = {container_id("openfit-sam", 1)}
    elif how == "helper":
        env.fake.rules.insert(0, (lambda a: "xzf" in a, (1, "", "tar: Unexpected EOF")))
    elif how == "start":
        env.fake.fail(lambda a: a[:2] == ["docker", "compose"] and "-d" in a,
                      err="Bind for 0.0.0.0:8101 failed: port is already allocated")
    else:
        env.fake.fail(lambda a: a[:2] == ["docker", "compose"] and "--no-start" in a, err="no space left")

    op = upload(env, tar_gz(standard()))

    assert op["outcome"] == "failed" and op["summary"] == said
    calls = env.fake.calls
    assert calls[-1] == ucompose(env, "sam", "down", "-v")
    downs = [c for c in calls if "down" in c.args]
    assert downs == [ucompose(env, "sam", "down", "-v")]
    assert {c.args[3] for c in calls if c.args[1] == "compose"} == {"openfit-sam"}  # nobody else's project
    assert not any(c.args[1:3] == ["volume", "rm"] for c in calls)
    assert env.fake.volumes == volumes  # sam's new volume gone, everyone else's as it was
    assert registry(env) == before
    assert not (env.data / "users" / "sam").exists()
    assert left(env) == []


def test_a_restore_that_fails_before_the_create_never_takes_anything_down(env):
    op = upload(env, tar_gz(standard(db=database(version=NEWEST + 5))))
    assert op["outcome"] == "failed"
    assert not any("down" in c for c in env.fake.commands())


# --- 10. the session and the header --------------------------------------------------------------------


def test_both_routes_need_a_session(admin_client, env):
    env.auth.setup(env.auth.setup_code, PASSWORD)  # a password, but no login
    (env.backups / BACKUP).write_bytes(tar_gz(standard()))
    r = admin_client.post("/api/users/restore-from", headers=H,
                          json={"name": "sam", "port": 8101, "keep_devices": True, "backup": BACKUP})
    assert r.status_code == 401
    r = admin_client.post("/api/users/restore", headers=H, content_type="multipart/form-data",
                          data={"name": "sam", "port": "8101", "keep_devices": "true",
                                "file": (io.BytesIO(tar_gz(standard())), "b.tar.gz")})
    assert r.status_code == 401 and r.get_json() == {"ok": False, "error": "Log in first."}
    assert env.fake.calls == [] and left(env) == [] and env.admin.history() == []


def test_both_routes_need_the_header(logged_in, env):
    (env.backups / BACKUP).write_bytes(tar_gz(standard()))
    for headers in ({}, {"X-OpenFit-Admin": "0"}):
        r = logged_in.post("/api/users/restore-from", headers=headers,
                           json={"name": "sam", "port": 8101, "keep_devices": True, "backup": BACKUP})
        assert r.status_code == 403
        r = logged_in.post("/api/users/restore", headers=headers, content_type="multipart/form-data",
                           data={"name": "sam", "port": "8101", "keep_devices": "true",
                                 "file": (io.BytesIO(tar_gz(standard())), "b.tar.gz")})
        assert r.status_code == 403
    # restore-from takes JSON only; a multipart body on any route but the upload's is refused.
    r = logged_in.post("/api/users/restore-from", headers=H, content_type="multipart/form-data",
                       data={"name": "sam", "port": "8101", "keep_devices": "true", "backup": BACKUP})
    assert r.status_code == 403
    r = logged_in.post("/api/users", headers=H, content_type="multipart/form-data",
                       data={"name": "sam", "port": "8101"})
    assert r.status_code == 403
    r = logged_in.post("/api/users/restore", headers=H, data={"name": "sam", "port": "8101"})  # urlencoded
    assert r.status_code == 403
    assert env.fake.calls == [] and left(env) == [] and env.admin.history() == []


# --- 7 and 9. the page -------------------------------------------------------------------------------

from test_admin_portal import needs_node, portal_status, run_page  # noqa: E402  (its node harness)


@needs_node
def test_a_file_turns_add_user_into_restore_user(logged_in, admin_env):
    status = admin_env.admin.status()
    out = run_page(logged_in, status, r"""
      const b = boot({});
      await settle();
      const state = () => ({ button: b.byId('addBtn').textContent, keep: !b.byId('addKeepRow').hidden,
                             ticked: !!b.byId('addKeep').checked, hint: b.byId('addHint').textContent });
      out.before = state();
      b.byId('addFile').files = [{ name: 'andy-nopassword.tar.gz', size: 123456 }];
      b.byId('addFile').fire('change');
      out.chosen = state();
      b.byId('addName').value = 'lee';
      b.byId('addPort').value = '8110';
      b.byId('addForm').fire('submit');
      await settle();
      out.sent = b.fetches.filter(f => f.method === 'POST').map(f => [f.url, f.header, f.type, f.body]);
      out.after = { ...state(), msg: b.byId('addMsg').textContent, name: b.byId('addName').value };
      // Unticked, it says so.
      b.byId('addFile').files = [{ name: 'b.tar.gz', size: 10 }];
      b.byId('addFile').fire('change');
      b.byId('addKeep').checked = false;
      b.byId('addName').value = 'max';
      b.byId('addForm').fire('submit');
      await settle();
      out.unticked = b.fetches.filter(f => f.method === 'POST').slice(-1)[0].body.entries;
      // Over 2 GB: said, never sent.
      b.byId('addFile').files = [{ name: 'huge.tar.gz', size: 3 * 1024 ** 3 }];
      b.byId('addFile').fire('change');
      const before = b.fetches.length;
      b.byId('addForm').fire('submit');
      await settle();
      out.huge = { msg: b.byId('addMsg').textContent, sent: b.fetches.length - before };
      // Clear file: Add user again, and a plain add is JSON, as before.
      b.byId('addClear').click();
      out.cleared = state();
      b.byId('addName').value = 'zed';
      b.byId('addForm').fire('submit');
      await settle();
      const last = b.fetches.filter(f => f.method === 'POST').slice(-1)[0];
      out.plain = [last.url, last.type, last.body];
    """)
    assert out["before"]["button"] == "Add user" and out["before"]["keep"] is False
    assert out["before"]["hint"].startswith("1 to 20 lowercase letters and digits.")
    assert out["chosen"]["button"] == "Restore user" and out["chosen"]["keep"] is True and out["chosen"]["ticked"]
    assert "They start locked" in out["chosen"]["hint"] and "Keep device logins" in out["chosen"]["hint"]
    assert out["sent"] == [["/api/users/restore", "1", None, {"entries": [
        ["name", "lee"], ["port", "8110"], ["keep_devices", "true"],
        ["file", {"file": "andy-nopassword.tar.gz", "size": 123456}]]}]]  # multipart: no Content-Type of its own
    assert out["after"]["button"] == "Add user" and out["after"]["keep"] is False and out["after"]["name"] == ""
    assert out["after"]["msg"] == "Restoring… its progress is in the Activity log."
    assert ["keep_devices", "false"] in out["unticked"]
    assert out["huge"] == {"msg": "That file is too large: a backup to restore is at most 2 GB.", "sent": 0}
    assert out["cleared"]["button"] == "Add user" and out["cleared"]["keep"] is False
    assert out["plain"][:2] == ["/api/users", "application/json"] and json.loads(out["plain"][2])["name"] == "zed"


@needs_node
def test_the_popups_restore_as_a_new_user_with_the_warning(logged_in, admin_env):
    status = portal_status(admin_env)
    andy = next(b for b in status["backups"] if b["user"] == "andy")
    kim = next(b for b in status["backups"] if b["user"] == "kim")
    out = run_page(logged_in, status, r"""
      const b = boot({});
      await settle();
      const body = b.byId('popupBody');
      const part = f => find(body, n => n.dataset.f === f)[0];
      const field = id => find(body, n => n.id === id)[0];  // made by the popup, not the template's
      const rows = () => b.byId('dataRows').children;
      const pick = (kind, user) => rows().find(r => r.dataset.kind === kind && r.textContent.startsWith(user));
      const panel = () => ({ shown: !part('restore').hidden, warn: !part('warn').hidden, warning: part('warn').textContent,
                             name: field('restoreName').value, port: field('restorePort').value,
                             ticked: !!field('restoreKeep').checked, text: part('restore').textContent });
      pick('backup', 'andy').click();
      out.closed = !part('restore') ? null : !part('restore').hidden;
      buttons(body, 'Restore as a new user')[0].click();
      out.andy = panel();
      field('restoreKeep').checked = false;
      field('restoreKeep').fire('change');
      out.andyUnticked = panel().warn;
      field('restoreName').value = 'lee';
      buttons(part('restore'), 'Restore')[0].click();
      await settle();
      out.andyPost = posts(b).slice(-1)[0];
      out.andyAfter = { open: open(b), msg: b.byId('usersMsg').textContent };
      pick('backup', 'kim').click();
      buttons(body, 'Restore as a new user')[0].click();
      out.kim = panel();
      pick('kept', 'kim').click();
      buttons(body, 'Restore as a new user')[0].click();
      out.kept = panel();
      field('restoreName').value = 'max';
      field('restorePort').value = '8120';
      buttons(part('restore'), 'Restore')[0].click();
      await settle();
      out.keptPost = posts(b).slice(-1)[0];
    """)
    assert out["closed"] is False  # the form waits for its button
    andy_said = ("andy is still a user here. Two OpenFits syncing the same Garmin or Google account can sign one of "
                 "them out. Untick Keep device logins, or remove andy first.")
    assert out["andy"]["shown"] and out["andy"]["warn"] and out["andy"]["warning"] == andy_said
    assert out["andy"]["name"] == "" and out["andy"]["port"] == str(status["next_port"]) and out["andy"]["ticked"]
    assert out["andyUnticked"] is False
    assert out["andyPost"] == ["/api/users/restore-from", "1", json.dumps(
        {"name": "lee", "port": status["next_port"], "keep_devices": False, "backup": andy["name"]},
        separators=(",", ":"))]
    assert out["andyAfter"] == {"open": False, "msg": "Restoring lee… its progress is in the Activity log."}
    assert out["kim"]["shown"] and out["kim"]["ticked"] and out["kim"]["warn"] is False  # kim isn't a user now
    assert kim["user"] == "kim"
    assert out["kept"]["warn"] is False
    assert "To bring kim back as they were, add a user called kim instead" in out["kept"]["text"]
    assert out["keptPost"] == ["/api/users/restore-from", "1",
                               '{"name":"max","port":8120,"keep_devices":true,"removed":"kim"}']


@needs_node
def test_a_restore_that_worked_offers_its_setup_link(logged_in, admin_env):
    status = portal_status(admin_env)
    op = {"id": "20261027T160000Z-restore_user", "kind": "restore_user", "outcome": "ok",
          "summary": "Restored sam. They start locked: use Setup link to give them a password.",
          "started_at": "2026-10-27T16:00:00Z", "finished_at": "2026-10-27T16:01:00Z",
          "user": {"name": "sam", "port": 8101}, "repo": None, "log": []}
    failed = {**op, "id": "20261027T150000Z-restore_user", "outcome": "failed", "summary": "Nothing was created.",
              "user": {"name": "max", "port": 8120}}
    status["activity"] = [op, failed] + status["activity"]
    status["op"] = op
    out = run_page(logged_in, status, r"""
      const b = boot({});
      await settle();
      const item = id => row(b, 'activityRows', id);
      out.head = item('20261027T160000Z-restore_user').children[0].textContent;
      item('20261027T160000Z-restore_user').children[0].click();
      const panel = item('20261027T160000Z-restore_user').children[1];
      out.buttons = buttons(panel, 'Setup link').map(n => n.disabled);
      buttons(panel, 'Setup link')[0].click();
      await settle();
      out.posts = posts(b).map(p => p[0]);
      out.dialog = !b.byId('linkDialog').hidden;
      item('20261027T150000Z-restore_user').children[0].click();
      out.failed = buttons(item('20261027T150000Z-restore_user').children[1], 'Setup link').length;
    """)
    assert out["head"].startswith("›Restore user sam")
    assert out["buttons"] == [False]
    assert out["posts"] == ["/api/users/sam/setup-link"]
    assert out["dialog"] is True
    assert out["failed"] == 0


@needs_node
def test_while_something_runs_restore_is_off(logged_in, admin_env):
    status = portal_status(admin_env)
    status["op"] = {"id": "20261027T150000Z-update", "kind": "update", "outcome": "running", "summary": "",
                    "started_at": "2026-10-27T15:00:00Z", "finished_at": None, "user": None, "log": []}
    out = run_page(logged_in, status, r"""
      const b = boot({});
      await settle();
      const body = b.byId('popupBody');
      const rows = b.byId('dataRows').children;
      rows.find(r => r.dataset.kind === 'backup').click();
      out.backup = buttons(body, 'Restore as a new user').map(n => n.disabled);
      rows.find(r => r.dataset.kind === 'kept').click();
      out.kept = buttons(body, 'Restore as a new user').map(n => n.disabled);
      out.add = b.byId('addBtn').disabled;
    """)
    assert out == {"backup": [True], "kept": [True], "add": True}
