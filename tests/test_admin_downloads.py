"""Downloads without the password, and deleting a backup (S2).

A download never holds the user's OpenFit password (auth.json) or its
sessions' key (.session_key), at the root of their /data: a backup is
streamed as a new archive with those two members left out, a removed
user's kept data by a helper whose tar excludes them. Whoever restores a
download sets a new password with a setup link. The backups kept on the
server, which Undo puts back, are untouched and still hold everything.

Deleting a backup is one file, under the lock; one the Undo on offer
relies on is marked, and once it's gone Undo is no longer offered.

As in the other Admin tests, git runs for real against temp repos and
docker is conftest's FakeRunner; backups are real archives here.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading

import pytest
from conftest import seed_users

import admin_ops

H = {"X-OpenFit-Admin": "1"}
PASSWORD = "a long password!"
BACKUP = "20261027T120000Z-abcdef0-andy.tar.gz"
KEPT_ARGS = ["docker", "run", "--rm", "--label", "openfit.admin.helper=true", "--log-driver", "none",
             "-v", "openfit-sam_tracker-data:/v:ro", "openfit-admin:local",
             "tar", "czf", "-", "-C", "/v", "--exclude=./auth.json", "--exclude=./.session_key", "."]

# A user's /data, as a backup holds it: the database and its WAL, the apps,
# the two password files at the root - and names like theirs that aren't
# them, which stay.
DATA = {
    "tracker.db": b"SQLite format 3\x00" + bytes(range(256)) * 300,
    "tracker.db-wal": b"wal" * 1000,
    "auth.json": b'{"password_hash": "pbkdf2:sha256:secret", "session_tag": "t"}',
    ".session_key": os.urandom(32),
    "auth.json.lock": b"",
    ".secret_key": b"the credentials key",
    "apps/steps/app.yaml": b"id: steps\n",
    "apps/steps/auth.json": b"an app's own file, not the password",
}


def make_backup(path, data=DATA, prefix="./", big=0):
    """A backup as the Admin's helper makes one (`tar czf ... -C /v .`):
    the root ./, the folders, each file - modes and times kept - and a
    link. `big` adds a file of that many random bytes."""
    data = dict(data)
    if big:
        data["big.bin"] = os.urandom(big)
    with tarfile.open(path, "w:gz") as tar:
        root = tarfile.TarInfo(prefix or ".")
        root.type, root.mode = tarfile.DIRTYPE, 0o755
        tar.addfile(root)
        for folder in ("apps", "apps/steps"):
            info = tarfile.TarInfo(prefix + folder)
            info.type, info.mode = tarfile.DIRTYPE, 0o755
            tar.addfile(info)
        for name, body in data.items():
            info = tarfile.TarInfo(prefix + name)
            info.size, info.mode, info.mtime, info.uid, info.uname = len(body), 0o600, 1761566400, 1000, "andy"
            tar.addfile(info, io.BytesIO(body))
        link = tarfile.TarInfo(prefix + "latest.db")
        link.type, link.linkname = tarfile.SYMTYPE, "tracker.db"
        tar.addfile(link)
    return data


def members(blob):
    """{name: (type, mode, mtime, uname, bytes or link)} of a tar.gz."""
    out = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        for m in tar:
            body = tar.extractfile(m).read() if m.isreg() else m.linkname
            out[m.name] = (m.type, m.mode, m.mtime, m.uname, body)
    return out


@pytest.fixture
def logged_in(admin_client, admin_env):
    admin_env.auth.setup(admin_env.auth.setup_code, PASSWORD)
    assert admin_client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
    return admin_client


def hold_an_op(env):
    """An operation running: Update OpenFit, held at its build. -> release()."""
    env.repos.push({"app/main.py": "print('new')\n"}, "New OpenFit")
    env.admin.check()
    release, reached = threading.Event(), threading.Event()

    def build(args):
        reached.set()
        release.wait(10)
        return 0, "", ""

    env.fake.rules.append((lambda a: a[:2] == ["docker", "compose"] and "build" in a, build))
    env.admin.start_update()
    assert reached.wait(10)

    def done():
        release.set()
        env.admin.wait(10)

    return done


# --- a backup, without the password ----------------------------------------------------------


@pytest.mark.parametrize("prefix", ["./", ""])
def test_a_backup_downloads_whole_but_for_the_password(logged_in, admin_env, prefix):
    env = admin_env
    data = make_backup(env.backups / BACKUP, prefix=prefix)
    original = (env.backups / BACKUP).read_bytes()

    r = logged_in.get(f"/api/backups/{BACKUP}/download")

    assert r.status_code == 200
    assert r.mimetype == "application/gzip"
    assert r.headers["Content-Disposition"] == 'attachment; filename="20261027T120000Z-abcdef0-andy-nopassword.tar.gz"'
    assert r.headers["Cache-Control"] == "no-store"
    got, had = members(r.data), members(original)
    left_out = {prefix + "auth.json", prefix + ".session_key"}
    assert set(had) - set(got) == left_out
    assert set(got) == set(had) - left_out
    for name in got:
        assert got[name] == had[name], name  # its bytes, mode, time and owner, as they were
    assert got[prefix + "tracker.db"][4] == data["tracker.db"]
    assert got[prefix + "apps/steps/auth.json"][4] == data["apps/steps/auth.json"]  # not the root's: kept
    # The backup on the server is untouched: Undo still puts back everything.
    assert (env.backups / BACKUP).read_bytes() == original
    assert env.fake.calls == []  # a file read, no command


def test_a_big_backup_streams_in_chunks(logged_in, admin_env):
    env = admin_env
    data = make_backup(env.backups / BACKUP, big=3 * 1024 * 1024)

    r = logged_in.get(f"/api/backups/{BACKUP}/download", buffered=False)
    chunks = list(r.response)
    r.close()

    assert len(chunks) > 3 and max(len(c) for c in chunks) <= admin_ops.CHUNK
    assert members(b"".join(chunks))["./big.bin"][4] == data["big.bin"]


@pytest.mark.skipif(shutil.which("tar") is None, reason="needs tar")
def test_a_backup_gnu_tar_made_downloads_without_the_password(logged_in, admin_env, tmp_path):
    """As the helper's own tar writes it."""
    env = admin_env
    v = tmp_path / "v"
    for name, body in DATA.items():
        (v / name).parent.mkdir(parents=True, exist_ok=True)
        (v / name).write_bytes(body)
    subprocess.run(["tar", "czf", str(env.backups / BACKUP), "-C", str(v), "."], check=True)

    got = members(logged_in.get(f"/api/backups/{BACKUP}/download").data)

    assert "./tracker.db" in got and got["./tracker.db"][4] == DATA["tracker.db"]
    assert "./auth.json" not in got and "./.session_key" not in got
    assert {"./auth.json.lock", "./.secret_key", "./apps/steps/auth.json"} <= set(got)


def test_a_download_is_logged_by_name_never_its_contents(logged_in, admin_env, capsys):
    env = admin_env
    make_backup(env.backups / BACKUP)
    capsys.readouterr()
    logged_in.get(f"/api/backups/{BACKUP}/download").get_data()
    out = capsys.readouterr().out
    assert out.strip() == "[admin] Downloaded 20261027T120000Z-abcdef0-andy-nopassword.tar.gz (no password)"


@pytest.mark.parametrize("name", [
    "20261027T120000Z-abcdef0-kim.tar.gz",  # well formed, not there
    "notes.txt", "x.tar.gz", "..", "20261027T120000Z-abcdef0-andy.tar.gz.bak",
    "20261027T120000Z-abcdef0-Andy.tar.gz", "%2e%2e%2fadmin.json",
])
def test_a_backup_the_admin_doesnt_list_is_a_404(logged_in, admin_env, name):
    env = admin_env
    make_backup(env.backups / BACKUP)
    (env.backups / "notes.txt").write_text("not a backup")
    r = logged_in.get(f"/api/backups/{name}/download")
    assert r.status_code == 404
    if "%2f" not in name:  # a / in it never reaches the route at all
        assert r.get_json() == {"ok": False, "error": "There's no backup by that name."}


def test_a_backup_folder_named_as_one_is_a_404(logged_in, admin_env):
    os.makedirs(admin_env.backups / BACKUP)
    assert logged_in.get(f"/api/backups/{BACKUP}/download").status_code == 404


def test_a_backup_download_is_refused_while_an_op_runs(logged_in, admin_env):
    env = admin_env
    make_backup(env.backups / BACKUP)
    done = hold_an_op(env)
    r = logged_in.get(f"/api/backups/{BACKUP}/download")
    done()
    assert r.status_code == 409 and "Something else is running" in r.get_json()["error"]


def test_a_backup_download_needs_a_session(logged_in, admin_env):
    make_backup(admin_env.backups / BACKUP)
    logged_in.post("/api/logout", json={}, headers=H)
    r = logged_in.get(f"/api/backups/{BACKUP}/download")
    assert r.status_code == 401 and r.get_json()["error"] == "Log in first."


def test_a_backup_that_wont_read_is_a_502_before_anything_is_sent(logged_in, admin_env):
    (admin_env.backups / BACKUP).write_bytes(b"backup")  # not a tar.gz
    r = logged_in.get(f"/api/backups/{BACKUP}/download")
    assert r.status_code == 502
    assert r.get_json()["error"] == "The backup couldn't be read."
    assert not admin_env.admin.lock.locked()


def test_the_lock_is_released_once_the_download_is_under_way(logged_in, admin_env):
    env = admin_env
    make_backup(env.backups / BACKUP, big=2 * 1024 * 1024)
    r = logged_in.get(f"/api/backups/{BACKUP}/download", buffered=False)
    stream = iter(r.response)
    next(stream)
    assert not env.admin.lock.locked()  # streaming, and the Admin free for anything else
    r.close()


# --- a removed user's kept data, without the password ---------------------------------------


def removed_sam(env):
    seed_users(env, ("sam", 8101, "running"), ("kim", 8102, "running"))
    env.admin.remove_user("sam")
    env.fake.clear()


def test_kept_data_streams_from_the_helper_without_the_password(logged_in, admin_env):
    env = admin_env
    removed_sam(env)
    blob = os.urandom(200_000)
    env.fake.stream_out = blob

    r = logged_in.get("/api/removed/sam/download")

    assert r.status_code == 200 and r.data == blob  # every byte the helper gave
    assert r.mimetype == "application/gzip"
    assert r.headers["Content-Disposition"] == 'attachment; filename="openfit-sam-kept-2026-10-27-nopassword.tar.gz"'
    assert env.fake.commands() == [
        # no container left in their project
        ["docker", "ps", "-a", "--no-trunc", "--filter", "label=com.docker.compose.project=openfit-sam",
         "--format", "{{.ID}}"],
        KEPT_ARGS,
    ]


def test_the_helper_is_exactly_its_form():
    assert admin_ops.kept_download_args("openfit-sam_tracker-data") == KEPT_ARGS
    for bad in ("openfit_tracker-data", "openfit-admin_admin-data", "openfit-library", "/etc", "openfit-sam_tracker-data:/x",
                None):
        with pytest.raises(ValueError):
            admin_ops.kept_download_args(bad)


@pytest.mark.skipif(shutil.which("tar") is None, reason="needs tar")
def test_the_helpers_tar_leaves_out_only_the_password(tmp_path):
    """The helper's own tar command, run here on a stand-in volume: GNU tar,
    as the Admin's image has."""
    v = tmp_path / "v"
    for name, body in DATA.items():
        (v / name).parent.mkdir(parents=True, exist_ok=True)
        (v / name).write_bytes(body)
    args = admin_ops.kept_download_args("openfit-sam_tracker-data")
    tar = args[args.index("tar"):]
    tar[tar.index("/v")] = str(v)
    done = subprocess.run(tar, capture_output=True, check=True)
    got = members(done.stdout)
    assert "./auth.json" not in got and "./.session_key" not in got
    assert {"./tracker.db", "./tracker.db-wal", "./auth.json.lock", "./.secret_key", "./apps/steps/auth.json"} <= set(got)
    assert got["./tracker.db"][4] == DATA["tracker.db"]


def test_a_helper_that_fails_before_any_output_is_a_502(logged_in, admin_env):
    env = admin_env
    removed_sam(env)
    env.fake.stream_rc, env.fake.stream_err = 125, "Error: No such volume: openfit-sam_tracker-data"

    r = logged_in.get("/api/removed/sam/download")

    assert r.status_code == 502
    body = r.get_json()
    assert body["error"] == "docker stopped with exit code 125."
    assert "No such volume" in body["detail"]
    assert not env.admin.lock.locked()


def test_a_helper_that_fails_part_way_cuts_the_download_off(logged_in, admin_env, capsys):
    env = admin_env
    removed_sam(env)
    env.fake.stream_out, env.fake.stream_rc, env.fake.stream_err = b"x" * 300_000, 2, "tar: ./tracker.db: Read error"

    r = logged_in.get("/api/removed/sam/download", buffered=False)
    assert r.status_code == 200
    with pytest.raises(admin_ops.OpError):
        b"".join(r.response)  # never ends as a finished download
    assert "The download of openfit-sam-kept-2026-10-27-nopassword.tar.gz stopped part-way" in capsys.readouterr().out


def test_kept_data_not_in_removed_is_a_404(logged_in, admin_env):
    env = admin_env
    seed_users(env, ("sam", 8101, "running"))
    for name in ("sam", "andy", "nobody", "Sam"):
        r = logged_in.get(f"/api/removed/{name}/download")
        assert r.status_code == 404, name
    assert not any(c.args[:2] == ["docker", "run"] for c in env.fake.calls)


def test_kept_data_with_a_container_again_is_a_409(logged_in, admin_env):
    env = admin_env
    removed_sam(env)
    env.fake.projects["openfit-sam"]["state"] = "exited"  # one made by hand since
    r = logged_in.get("/api/removed/sam/download")
    assert r.status_code == 409
    assert r.get_json()["error"] == "sam has a container again, so their kept data can't be downloaded now."
    assert not any(c.args[:2] == ["docker", "run"] for c in env.fake.calls)


def test_kept_data_is_refused_while_an_op_runs(logged_in, admin_env):
    env = admin_env
    removed_sam(env)
    done = hold_an_op(env)
    r = logged_in.get("/api/removed/sam/download")
    done()
    assert r.status_code == 409 and "Something else is running" in r.get_json()["error"]
    assert KEPT_ARGS not in env.fake.commands()


def test_kept_data_needs_a_session(logged_in, admin_env):
    removed_sam(admin_env)
    logged_in.post("/api/logout", json={}, headers=H)
    assert logged_in.get("/api/removed/sam/download").status_code == 401
    assert admin_env.fake.calls == []


# --- Runner.stream ---------------------------------------------------------------------------------


def test_stream_yields_the_output_as_it_comes(tmp_path):
    runner = admin_ops.Runner(str(tmp_path))
    code = "import sys; sys.stdout.buffer.write(bytes(range(256)) * 1000)"
    with runner.stream([sys.executable, "-c", code]) as out:
        got = b"".join(iter(lambda: out.read1(65536), b""))
    assert got == bytes(range(256)) * 1000


def test_stream_raises_on_a_non_zero_exit_with_its_error_output(tmp_path):
    runner = admin_ops.Runner(str(tmp_path))
    code = "import sys; sys.stdout.write('half'); sys.stderr.write('it broke'); sys.exit(3)"
    with pytest.raises(admin_ops.OpError) as e:
        with runner.stream([sys.executable, "-c", code]) as out:
            assert out.read() == b"half"
    assert e.value.message.endswith("stopped with exit code 3.") and e.value.detail == "it broke"


def test_stream_kills_the_command_when_its_reader_goes(tmp_path):
    runner = admin_ops.Runner(str(tmp_path))
    with pytest.raises(GeneratorExit):
        with runner.stream(["yes"]) as out:  # never ends by itself
            assert out.read(4) == b"y\ny\n"
            raise GeneratorExit  # the browser gone


def test_stream_takes_only_a_list_and_never_raises_for_a_missing_command(tmp_path):
    runner = admin_ops.Runner(str(tmp_path))
    for bad in ("echo hi", ("echo", "hi"), ["echo", 1]):
        with pytest.raises(TypeError):
            with runner.stream(bad):
                pass
    with pytest.raises(admin_ops.OpError):
        with runner.stream(["no-such-command-anywhere"]):
            pass


# --- deleting a backup -------------------------------------------------------------------------------


def an_update_with_backups(env):
    """Update OpenFit for andy and sam: one set of two backups, which Undo
    would put back - and an older backup of andy's beside them. Their names."""
    seed_users(env, ("sam", 8101, "running"))
    env.repos.push({"app/main.py": "print('2')\n"}, "Two")
    env.admin.check()
    env.admin.start_update()
    env.admin.wait(10)
    assert env.admin.current_op()["outcome"] == "ok"
    (env.backups / "20261001T000000Z-abcdef0-andy.tar.gz").write_bytes(b"old")
    return sorted(b["name"] for b in env.admin.list_backups() if not b["name"].startswith("20261001"))


def test_undo_relies_marks_the_backups_the_undo_on_offer_puts_back(logged_in, admin_env):
    env = admin_env
    relied = an_update_with_backups(env)
    status = logged_in.get("/api/status").get_json()
    assert status["undo"] is not None
    marks = {b["name"]: b["undo_relies"] for b in status["backups"]}
    assert marks == {**{n: True for n in relied}, "20261001T000000Z-abcdef0-andy.tar.gz": False}


def test_no_undo_on_offer_relies_on_nothing(logged_in, admin_env):
    make_backup(admin_env.backups / BACKUP)
    status = logged_in.get("/api/status").get_json()
    assert status["undo"] is None and [b["undo_relies"] for b in status["backups"]] == [False]


def test_deleting_a_backup_removes_the_file(logged_in, admin_env, capsys):
    env = admin_env
    relied = an_update_with_backups(env)
    old = "20261001T000000Z-abcdef0-andy.tar.gz"

    r = logged_in.post(f"/api/backups/{old}/delete", json={}, headers=H)

    assert r.status_code == 200 and r.get_json() == {"ok": True}
    assert not (env.backups / old).exists()
    assert sorted(b["name"] for b in env.admin.list_backups()) == relied
    assert logged_in.get("/api/status").get_json()["undo"] is not None  # not one Undo needs
    assert f"[admin] deleted the backup {old}" in capsys.readouterr().out


def test_deleting_a_backup_undo_relies_on_takes_the_undo_away(logged_in, admin_env):
    env = admin_env
    relied = an_update_with_backups(env)
    r = logged_in.post(f"/api/backups/{relied[0]}/delete", json={}, headers=H)
    assert r.status_code == 200
    status = logged_in.get("/api/status").get_json()
    assert status["undo"] is None
    assert logged_in.post("/api/undo", json={}, headers=H).status_code == 409


@pytest.mark.parametrize("name", ["20261027T120000Z-abcdef0-kim.tar.gz", "notes.txt", "x.tar.gz",
                                  "admin.json", ".."])
def test_deleting_a_backup_the_admin_doesnt_list_is_a_404(logged_in, admin_env, name):
    env = admin_env
    make_backup(env.backups / BACKUP)
    (env.backups / "notes.txt").write_text("not a backup")
    r = logged_in.post(f"/api/backups/{name}/delete", json={}, headers=H)
    assert r.status_code == 404
    assert sorted(os.listdir(env.backups)) == sorted([BACKUP, "notes.txt"])
    assert (env.data / "admin.json").exists()


def test_deleting_a_backup_is_refused_while_an_op_runs(logged_in, admin_env):
    env = admin_env
    make_backup(env.backups / BACKUP)
    done = hold_an_op(env)
    r = logged_in.post(f"/api/backups/{BACKUP}/delete", json={}, headers=H)
    done()
    assert r.status_code == 409 and (env.backups / BACKUP).exists()


def test_deleting_a_backup_needs_the_header_and_a_session(logged_in, admin_env):
    env = admin_env
    make_backup(env.backups / BACKUP)
    assert logged_in.post(f"/api/backups/{BACKUP}/delete", json={}).status_code == 403
    assert logged_in.post(f"/api/backups/{BACKUP}/delete", data="{}", headers=H).status_code == 403  # not JSON
    assert logged_in.get(f"/api/backups/{BACKUP}/delete").status_code == 405  # never a GET
    logged_in.post("/api/logout", json={}, headers=H)
    assert logged_in.post(f"/api/backups/{BACKUP}/delete", json={}, headers=H).status_code == 401
    assert (env.backups / BACKUP).exists()


def test_the_page_never_sees_the_kept_volume_name(logged_in, admin_env):
    """The kept data's download is by the user's name: the volume is the Admin's to look up."""
    removed_sam(admin_env)
    status = logged_in.get("/api/status").get_json()
    assert status["removed"] == [{"name": "sam", "removed_at": status["removed"][0]["removed_at"]}]
    assert "tracker-data" not in json.dumps(status["removed"])
