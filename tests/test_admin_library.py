"""The app shelf (A5): publishing the official apps to it, atomically and
only inside it; when publishing runs (startup, Update OpenFit, an undo or
rollback, Update apps, Publish again); the users' read-only mounts of it;
and Restart.

As in the other Admin tests, git runs for real against temp repos and
docker is conftest's FakeRunner; the shelf is a temp folder standing in
for the Admin's /library.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml
from conftest import CONTAINERS, Call, container_id, seed_users

import admin_ops
import admin_server

H = {"X-OpenFit-Admin": "1"}
PASSWORD = "a long password!"
C0, C1, C2 = CONTAINERS[:3]
HEAD_LOG = ["git", "log", "-1", "--format=%H%x09%h%x09%s", "HEAD"]
SHELF = Call(["docker", "volume", "inspect", "--format", "{{.Name}}", "openfit-library"], None, False)
OVERRIDE = """services:
  tracker:
    volumes:
      - openfit-library:/library:ro
volumes:
  openfit-library:
    external: true
"""


@pytest.fixture
def logged_in(admin_client, admin_env):
    admin_env.auth.setup(admin_env.auth.setup_code, PASSWORD)
    assert admin_client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
    return admin_client


# --- helpers ---------------------------------------------------------------------


def run(cwd, *args):
    return subprocess.run(list(args), cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def head(env):
    return env.repos.head()


def land(env, files, message):
    """A commit that reaches the server's copy without the Admin: pushed,
    then pulled by hand."""
    env.repos.push(files, message)
    run(env.repos.repo, "git", "pull", "-q", "--ff-only")
    return head(env)


def write(folder, files):
    for name, body in files.items():
        path = Path(folder) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            path.write_bytes(body)
        else:
            path.write_text(body)


def link(env):
    return os.readlink(env.library / "openfit")


def sets(env):
    return sorted(os.listdir(env.library / ".sets"))


def index(env):
    return json.loads((env.library / "index.json").read_text())


def shelf_apps(env):
    return sorted(index(env)["sources"]["openfit"]["apps"])


def files_in(folder):
    folder = Path(folder)
    return sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file())


def snapshot(env):
    """Everything about the shelf a reader could see: the link, the index's
    bytes and every entry under .sets."""
    lib = env.library
    return (
        os.readlink(lib / "openfit") if os.path.islink(lib / "openfit") else None,
        (lib / "index.json").read_bytes() if (lib / "index.json").exists() else None,
        sorted(str(p.relative_to(lib)) for p in lib.rglob("*")),
    )


def compose(env, *args):
    return Call(["docker", "compose", "-p", "openfit", "-f", os.path.join(env.repos.repo, "docker-compose.yml"),
                 "-f", str(env.data / "primary.override.yml"), *args], env.repos.repo, False)


def ucompose(env, name, *args):
    folder = str(env.data / "users" / name)
    return Call(["docker", "compose", "-p", f"openfit-{name}", "-f", os.path.join(folder, "docker-compose.yml"),
                 *args], folder, False)


def ps(project):
    return Call(["docker", "ps", "-a", "--no-trunc",
                 "--filter", f"label=com.docker.compose.project={project}",
                 "--filter", "label=com.docker.compose.service=tracker",
                 "--filter", "label=com.docker.compose.oneoff=False",
                 "--format", "{{.ID}}\t{{.State}}"], None, False)


def health(cid):
    return Call(["docker", "exec", cid, "python", "-c", admin_ops.HEALTH_CODE], None, False)


def git(env, *args):
    return Call(["git", *args], env.repos.repo, True)


def run_op(env, start):
    start()
    env.admin.wait(10)
    return env.admin.current_op()


def texts(op):
    return [line["text"] for line in op["log"]]


def apps_pending(env, files=None, message="New app"):
    """An app library change waiting on GitHub, found by a check."""
    env.repos.push(files or {"library/caffeine/app.yaml": "id: caffeine\n", "library/caffeine/app.py": "x = 1\n"},
                   message)
    env.admin.check()


# --- 1. publishing ---------------------------------------------------------------


def test_a_publish_puts_a_set_behind_the_link_and_indexes_it(admin_env):
    env = admin_env
    commit = land(env, {
        "library/steps/app.py": "def render(ctx, cfg):\n    return ctx.empty('steps')\n",
        "library/steps/assets/note.txt": "nested\n",
        "library/caffeine/app.yaml": "id: caffeine\n",
        "library/caffeine/app.py": "x = 1\n",
    }, "Two apps")

    result = env.admin.publish_official()

    assert link(env) == f".sets/openfit-{commit[:12]}"
    assert sets(env) == [f"openfit-{commit[:12]}"]
    shelf, repo = env.library / "openfit", Path(env.repos.repo) / "library"
    assert sorted(os.listdir(shelf)) == ["caffeine", "steps"]
    for app in ("caffeine", "steps"):
        assert files_in(shelf / app) == files_in(repo / app)
        for name in files_in(repo / app):
            assert (shelf / app / name).read_bytes() == (repo / app / name).read_bytes()
    assert files_in(shelf / "steps") == ["app.py", "app.yaml", "assets/note.txt"]
    apps = {app: {"sha256": admin_ops.fingerprint(str(shelf / app))} for app in ("caffeine", "steps")}
    assert index(env) == {"published_at": "2026-10-27T12:00:00Z",
                          "sources": {"openfit": {"commit": commit, "apps": apps}}}
    # The fingerprint of the published folder is the repo folder's too.
    assert apps["steps"]["sha256"] == admin_ops.fingerprint(str(repo / "steps"))
    assert list(index(env)["sources"]["openfit"]["apps"]) == ["caffeine", "steps"]
    assert result == {"commit": commit, "apps": apps, "changed": True}
    # HEAD read, and nothing else run.
    assert env.fake.commands() == [HEAD_LOG]


def test_a_publish_leaves_out_what_isnt_an_apps_file(admin_env, tmp_path, capsys):
    env = admin_env
    lib = Path(env.repos.repo) / "library"
    outside = tmp_path / "outside"
    write(outside, {"secret.txt": "a secret\n", "app.yaml": "id: outside\n"})
    write(lib / "good", {
        "app.yaml": "id: good\n", "app.py": "x = 1\n", "sub/keep.txt": "keep\n",
        ".env": "SECRET=1\n", ".hidden/inner.txt": "hidden\n",
        "__pycache__/app.cpython-312.pyc": b"\x00pyc", "old.pyc": b"\x00pyc", "sub/__pycache__/x.pyc": b"\x00",
    })
    os.symlink(outside / "secret.txt", lib / "good" / "link.txt")
    os.symlink(outside, lib / "good" / "linkdir")
    os.mkfifo(lib / "good" / "pipe")
    os.symlink(outside, lib / "linked")  # an app folder that's a link
    write(lib / "Bad_Name", {"app.yaml": "id: bad\n"})
    write(lib / "-dash", {"app.yaml": "id: dash\n"})
    write(lib / ".dotted", {"app.yaml": "id: dotted\n"})
    (lib / "README.md").write_text("not an app\n")
    write(lib / "noyaml", {"app.py": "x = 1\n"})
    write(lib / "linkedyaml", {"app.py": "x = 1\n"})
    os.symlink(outside / "app.yaml", lib / "linkedyaml" / "app.yaml")
    write(lib / "toomany", {"app.yaml": "id: toomany\n", **{f"f{i:02}.txt": "x" for i in range(64)}})
    write(lib / "justenough", {"app.yaml": "id: justenough\n", **{f"f{i:02}.txt": "x" for i in range(63)}})
    write(lib / "huge", {"app.yaml": "id: huge\n"})
    with open(lib / "huge" / "blob.bin", "wb") as f:
        f.truncate(admin_ops.MAX_APP_BYTES)  # sparse: the cap, plus app.yaml
    capsys.readouterr()

    env.admin.publish_official()

    shelf = env.library / "openfit"
    assert sorted(os.listdir(shelf)) == ["good", "justenough", "steps"]
    assert shelf_apps(env) == ["good", "justenough", "steps"]
    assert files_in(shelf / "good") == ["app.py", "app.yaml", "sub/keep.txt"]
    assert len(files_in(shelf / "justenough")) == 64
    for p in env.library.rglob("*"):
        assert not p.is_symlink() or p == shelf, p
        assert not (p.is_file() and b"a secret" in p.read_bytes()), p
    out = capsys.readouterr().out
    for name, why in [
        ("linked", "it isn't an app folder"), ("Bad_Name", "it isn't an app folder"),
        ("-dash", "it isn't an app folder"), (".dotted", "it isn't an app folder"),
        ("README.md", "it isn't an app folder"), ("noyaml", "it has no app.yaml"),
        ("linkedyaml", "it has no app.yaml"), ("toomany", "it has more than 64 files"),
        ("huge", "it's more than 16 MB"),
    ]:
        assert f"Leaving out library/{name}: {why}." in out, name
    assert "Leaving out library/good/link.txt: it's a link." in out
    assert "Leaving out library/good/linkdir: it's a link." in out
    assert "Leaving out library/good/pipe: it isn't a regular file." in out


def test_a_publish_holds_at_most_200_apps(admin_env, capsys):
    env = admin_env
    lib = Path(env.repos.repo) / "library"
    for i in range(201):
        write(lib / f"app{i:03}", {"app.yaml": f"id: app{i:03}\n"})
    capsys.readouterr()

    env.admin.publish_official()

    apps = shelf_apps(env)
    assert len(apps) == 200
    assert "steps" not in apps and apps[-1] == "app199"
    out = capsys.readouterr().out
    assert "Leaving out library/app200: the app library holds at most 200 apps." in out
    assert "Leaving out library/steps: the app library holds at most 200 apps." in out


def test_a_repo_with_no_library_folder_publishes_an_empty_set(admin_env):
    env = admin_env
    run(env.repos.repo, "git", "rm", "-rq", "library")
    run(env.repos.repo, "git", "commit", "-qm", "No library")
    env.admin.publish_official()
    assert index(env)["sources"]["openfit"] == {"commit": head(env), "apps": {}}
    assert os.listdir(env.library / "openfit") == []


# --- 2. atomic ---------------------------------------------------------------------


def test_a_publish_that_fails_part_way_leaves_the_shelf_as_it_was(admin_env, monkeypatch):
    env = admin_env
    env.admin.publish_official()
    before = snapshot(env)
    land(env, {"library/caffeine/app.yaml": "id: caffeine\n", "library/caffeine/app.py": "x = 1\n"}, "Caffeine")
    seen, real = [], admin_ops._copy_file

    def copy(src, dst):  # what a reader sees while the new set is copied
        seen.append(snapshot(env)[:2])
        if len(seen) == 2:
            raise OSError("No space left on device")
        return real(src, dst)

    monkeypatch.setattr(admin_ops, "_copy_file", copy)

    with pytest.raises(admin_ops.OpError) as e:
        env.admin.publish_official()

    assert e.value.message == "The app library couldn't be updated. It's as it was."
    assert "No space left on device" in e.value.detail
    assert seen == [before[:2]] * 2  # the old set and the old index, all along
    assert snapshot(env) == before  # no new set, nothing hidden left behind
    assert (env.library / "openfit" / "steps" / "app.yaml").read_text() == "id: steps\n"
    assert env.admin.library_error["text"] == e.value.message


def test_a_publish_whose_index_cant_be_written_puts_the_link_back(admin_env, monkeypatch):
    env = admin_env
    env.admin.publish_official()
    before = snapshot(env)
    land(env, {"library/caffeine/app.yaml": "id: caffeine\n"}, "Caffeine")

    def write_index(*args):
        raise OSError("read-only file system")

    monkeypatch.setattr(admin_ops, "_write_index", write_index)
    with pytest.raises(admin_ops.OpError):
        env.admin.publish_official()
    assert snapshot(env) == before


def test_a_first_publish_that_fails_leaves_the_shelf_empty(admin_env, monkeypatch):
    env = admin_env

    def copy(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(admin_ops, "_copy_file", copy)
    with pytest.raises(admin_ops.OpError):
        env.admin.publish_official()
    assert os.listdir(env.library) == []


def test_old_sets_are_pruned_to_the_current_and_the_one_before(admin_env):
    env = admin_env
    commits = []
    for i in range(4):
        if i:
            land(env, {"library/steps/app.py": f"v = {i}\n"}, f"Steps v{i}")
        env.admin.publish_official()
        commits.append(head(env))
        assert len(sets(env)) <= 2
    assert sets(env) == sorted(f"openfit-{c[:12]}" for c in commits[-2:])
    assert link(env) == f".sets/openfit-{commits[-1][:12]}"
    assert (env.library / "openfit" / "steps" / "app.py").read_text() == "v = 3\n"


def test_publishing_the_commit_before_again_rebuilds_its_set(admin_env):
    env = admin_env
    old = head(env)
    env.admin.publish_official()
    land(env, {"library/caffeine/app.yaml": "id: caffeine\n"}, "Caffeine")
    env.admin.publish_official()
    run(env.repos.repo, "git", "reset", "-q", "--hard", old)  # an undo, say

    env.admin.publish_official()

    assert link(env) == f".sets/openfit-{old[:12]}"
    assert index(env)["sources"]["openfit"]["commit"] == old
    assert shelf_apps(env) == ["steps"]
    assert len(sets(env)) == 2 and not any(s.startswith(".") for s in sets(env))


def test_publishing_the_current_commit_again_never_rewrites_its_set(admin_env):
    env = admin_env
    env.admin.publish_official()
    inode = os.stat(env.library / ".sets" / sets(env)[0]).st_ino
    before = snapshot(env)

    assert env.admin.publish_official()["changed"] is False
    assert snapshot(env) == before

    os.remove(env.library / "index.json")  # as if the last index write was lost
    assert env.admin.publish_official()["changed"] is True
    after = snapshot(env)
    assert (after[0], after[2]) == (before[0], before[2])  # the same link and set
    assert index(env)["sources"]["openfit"]["commit"] == head(env)
    assert os.stat(env.library / ".sets" / sets(env)[0]).st_ino == inode


def test_another_sources_entry_in_the_index_is_kept(admin_env):
    env = admin_env
    other = {"commit": "f" * 40, "apps": {"x": {"sha256": "0" * 64}}}
    (env.library / "index.json").write_text(json.dumps({"published_at": "x", "sources": {"acme": other}}))
    env.admin.publish_official()
    assert index(env)["sources"]["acme"] == other
    assert index(env)["sources"]["openfit"]["commit"] == head(env)


# --- 3. startup --------------------------------------------------------------------


def test_startup_publishes_when_there_is_no_index(admin_env):
    env = admin_env
    assert env.admin.publish_at_startup() is True
    assert index(env)["sources"]["openfit"]["commit"] == head(env)
    assert all(c[0] == "git" for c in env.fake.commands())


def test_startup_publishes_when_the_index_is_behind_the_code(admin_env):
    env = admin_env
    env.admin.publish_official()
    new = land(env, {"library/caffeine/app.yaml": "id: caffeine\n"}, "Caffeine")
    assert env.make().publish_at_startup() is True
    assert index(env)["sources"]["openfit"]["commit"] == new
    assert shelf_apps(env) == ["caffeine", "steps"]


def test_startup_leaves_a_current_shelf_alone(admin_env):
    env = admin_env
    env.admin.publish_official()
    before = snapshot(env)
    env.fake.clear()
    assert env.make().publish_at_startup() is False
    assert snapshot(env) == before
    assert env.fake.commands() == [HEAD_LOG]


def test_a_startup_publish_that_fails_is_logged_and_shown(admin_env, monkeypatch, capsys):
    env = admin_env
    monkeypatch.setattr(admin_ops, "_copy_file", lambda src, dst: (_ for _ in ()).throw(OSError("disk full")))
    assert env.admin.publish_at_startup() is False
    assert "The app library couldn't be updated" in capsys.readouterr().out
    lib = env.admin.status()["library"]
    assert lib["current"] is False and lib["commit"] is None and "disk full" in lib["error"]["detail"]


def test_serve_publishes_before_the_checks_and_the_server_start(monkeypatch, tmp_path):
    order = []
    (tmp_path / "repo" / ".git").mkdir(parents=True)

    class FakeAdmin:
        def __init__(self, repo, data_dir):
            order.append("admin")

        def publish_at_startup(self):
            order.append("publish")

        def start_auto_checks(self):
            order.append("checks")

    class FakeAuth:
        setup_code = None

        def __init__(self, data_dir):
            pass

    class FakeApp:
        def run(self, **kw):
            order.append("server")

    monkeypatch.setenv("OPENFIT_REPO", str(tmp_path / "repo"))
    monkeypatch.setenv("ADMIN_DATA", str(tmp_path / "data"))
    monkeypatch.setattr(admin_server, "Admin", FakeAdmin)
    monkeypatch.setattr(admin_server, "Auth", FakeAuth)
    monkeypatch.setattr(admin_server, "load_secret_key", lambda d: b"k" * 32)
    monkeypatch.setattr(admin_server, "create_app", lambda *a: FakeApp())
    admin_server.serve()
    assert order == ["admin", "publish", "checks", "server"]


# --- 4. Update OpenFit, undo and rollback ---------------------------------------------


def both_pending(env):
    env.repos.push({"app/main.py": "print('new')\n", "library/caffeine/app.yaml": "id: caffeine\n"}, "Both")
    env.admin.check()


def test_update_openfit_publishes_once_everyone_answers(admin_env):
    env = admin_env
    env.admin.publish_at_startup()
    both_pending(env)
    assert admin_ops.update_offered(env.admin.last_check())
    assert not admin_ops.update_apps_offered(env.admin.last_check())

    op = run_op(env, env.admin.start_update)

    new = head(env)
    assert op["outcome"] == "ok"
    assert op["summary"].startswith(f"Updated OpenFit to {new[:7]}")
    assert index(env)["sources"]["openfit"]["commit"] == new
    assert link(env) == f".sets/openfit-{new[:12]}"
    assert shelf_apps(env) == ["caffeine", "steps"]
    log = texts(op)
    checked = log.index("Checking andy's OpenFit is answering…")
    publishing = log.index("Publishing the new apps to the app library…")
    assert checked < publishing < log.index(f"The app library is on {new[:7]}: 2 apps.")


def test_a_publish_that_fails_in_an_update_leaves_it_ok_with_publish_again(logged_in, admin_env, monkeypatch):
    env = admin_env
    env.admin.publish_at_startup()
    old = head(env)
    both_pending(env)

    def broken(*args, **kw):
        raise admin_ops.OpError("The app library couldn't be updated. It's as it was.", "No space left on device")

    monkeypatch.setattr(admin_ops, "publish_set", broken)
    op = run_op(env, env.admin.start_update)

    assert op["outcome"] == "ok"
    assert op["summary"].startswith("Updated OpenFit to")
    log = texts(op)
    assert "The app library couldn't be updated. It's as it was.\nNo space left on device" in log
    assert ("The update itself is done. Use Publish again, on the App library card, to try the app library "
            "again.") in log
    lib = logged_in.get("/api/status").get_json()["library"]
    assert lib["current"] is False and lib["commit"] == old
    assert lib["error"]["detail"] == "No space left on device"

    monkeypatch.undo()
    env.fake.clear()
    r = logged_in.post("/api/library/publish", json={}, headers=H)
    assert r.status_code == 200 and r.get_json() == {"ok": True}
    assert env.fake.commands() == [HEAD_LOG]  # HEAD read; no docker
    lib = logged_in.get("/api/status").get_json()["library"]
    assert lib["current"] is True and lib["error"] is None
    assert lib["commit"] == head(env) and lib["apps"] == ["caffeine", "steps"]


def test_publish_again_that_fails_again_is_a_502_with_why(logged_in, admin_env, monkeypatch):
    env = admin_env
    monkeypatch.setattr(admin_ops, "_copy_file", lambda src, dst: (_ for _ in ()).throw(OSError("disk full")))
    r = logged_in.post("/api/library/publish", json={}, headers=H)
    assert r.status_code == 502
    assert r.get_json()["error"] == "The app library couldn't be updated. It's as it was."
    assert "disk full" in r.get_json()["detail"]
    assert not env.admin.lock.locked()


def test_a_rollback_republishes_from_the_old_head(admin_env):
    env = admin_env
    env.admin.publish_at_startup()
    old = head(env)
    both_pending(env)
    env.fake.unhealthy = {C1}  # the new version never answers

    op = run_op(env, env.admin.start_update)

    assert op["outcome"] == "rolled_back"
    assert head(env) == old
    assert index(env)["sources"]["openfit"]["commit"] == old
    assert link(env) == f".sets/openfit-{old[:12]}"
    assert shelf_apps(env) == ["steps"]
    log = texts(op)
    assert log.index(f"Putting the app library back on {old[:7]}…") > log.index("Putting the code back as it was…")
    assert "Publishing the new apps to the app library…" not in log


def test_undo_puts_the_app_library_back_on_the_old_code(admin_env):
    env = admin_env
    env.admin.publish_at_startup()
    old = head(env)
    both_pending(env)
    assert run_op(env, env.admin.start_update)["outcome"] == "ok"
    new = head(env)
    assert shelf_apps(env) == ["caffeine", "steps"]

    op = run_op(env, env.admin.start_undo)

    assert op["outcome"] == "ok"
    assert index(env)["sources"]["openfit"]["commit"] == old
    assert link(env) == f".sets/openfit-{old[:12]}"
    assert shelf_apps(env) == ["steps"]
    assert sets(env) == sorted([f"openfit-{old[:12]}", f"openfit-{new[:12]}"])
    assert f"Putting the app library back on {old[:7]}…" in texts(op)


# --- 5. Update apps ------------------------------------------------------------------


@pytest.mark.parametrize("files, openfit, apps", [
    ({"library/caffeine/app.yaml": "id: caffeine\n"}, False, True),
    ({"library/caffeine/app.yaml": "id: caffeine\n", "docs/caffeine.md": "caffeine\n"}, False, True),
    ({"library/caffeine/app.yaml": "id: caffeine\n", "app/main.py": "print(2)\n"}, True, False),
    ({"library/caffeine/app.yaml": "id: caffeine\n", "admin/admin_server.py": "print(2)\n"}, True, False),
    ({"docs/readme.md": "# tidied\n"}, True, False),
], ids=["library", "library and docs", "library and app", "library and admin", "docs"])
def test_update_apps_is_offered_only_for_a_library_change(logged_in, admin_env, files, openfit, apps):
    admin_env.repos.push(files, "A change")
    admin_env.admin.check()
    s = logged_in.get("/api/status").get_json()
    assert (s["update_available"], s["update_apps_available"]) == (openfit, apps)
    check = admin_env.admin.last_check()
    assert (admin_ops.update_offered(check), admin_ops.update_apps_offered(check)) == (openfit, apps)


def test_nothing_is_offered_with_nothing_new(admin_env):
    check = admin_env.admin.check()
    assert not admin_ops.update_offered(check) and not admin_ops.update_apps_offered(check)
    assert not admin_ops.update_apps_offered(None)


def test_update_openfit_is_refused_when_only_apps_changed(logged_in, admin_env):
    apps_pending(admin_env)
    admin_env.fake.clear()
    r = logged_in.post("/api/update", json={}, headers=H)
    assert r.status_code == 409
    assert r.get_json()["error"] == ("Only the app library changed, so there's nothing in OpenFit to update: "
                                     "use Update apps.")
    assert admin_env.fake.calls == [] and admin_env.admin.history() == []


def test_update_apps_is_refused_when_openfit_changed_too(logged_in, admin_env):
    env = admin_env
    env.repos.push({"library/caffeine/app.yaml": "id: caffeine\n", "app/main.py": "print(2)\n"}, "Both")
    env.admin.check()
    env.fake.clear()
    r = logged_in.post("/api/library/update", json={}, headers=H)
    assert r.status_code == 409
    assert r.get_json()["error"] == "OpenFit itself changed too, so use Update OpenFit: it updates the apps as well."
    assert env.fake.calls == [] and env.admin.history() == [] and not env.admin.lock.locked()
    with pytest.raises(admin_ops.Refused) as e:
        env.admin.start_update_apps()
    assert e.value.status == 409


def test_update_apps_with_nothing_to_apply(admin_env):
    with pytest.raises(admin_ops.Refused) as e:
        admin_env.admin.start_update_apps()
    assert e.value.status == 409 and "Check for updates first" in e.value.message


def test_update_apps_refuses_a_dirty_tree_and_another_branch(admin_env):
    env = admin_env
    apps_pending(env)
    (Path(env.repos.repo) / "library" / "steps" / "app.yaml").write_text("id: edited\n")
    with pytest.raises(admin_ops.Refused) as e:
        env.admin.start_update_apps()
    assert e.value.message == ("The server's copy of OpenFit has local changes: library/steps/app.yaml. "
                               "Nothing was changed.")
    run(env.repos.repo, "git", "checkout", "-q", "--", ".")
    run(env.repos.repo, "git", "checkout", "-q", "-b", "experiment")
    with pytest.raises(admin_ops.Refused) as e:
        env.admin.start_update_apps()
    assert e.value.message == "The server's copy of OpenFit is on 'experiment', not 'main'. Nothing was changed."
    assert env.admin.history() == [] and not env.admin.lock.locked()


def test_update_apps_runs_in_the_exact_order_and_restarts_nobody(logged_in, admin_env):
    env = admin_env
    seed_users(env, ("sam", 8101, "running"), ("kim", 8102, "exited"))
    env.admin.publish_at_startup()
    old = head(env)
    apps_pending(env)
    env.fake.clear()
    containers = json.dumps(env.fake.projects, sort_keys=True)

    r = logged_in.post("/api/library/update", json={}, headers=H)
    assert r.status_code == 202 and r.get_json()["op"]["kind"] == "update_apps"
    env.admin.wait(10)
    op = env.admin.current_op()

    new = head(env)
    assert env.fake.calls == [
        # the dirty-tree and branch refusals, as for Update OpenFit
        git(env, "status", "--porcelain"),
        git(env, "rev-parse", "--abbrev-ref", "HEAD"),
        # note the version, pull
        git(env, *HEAD_LOG[1:]),
        git(env, "merge", "--ff-only", "origin/main"),
        git(env, *HEAD_LOG[1:]),
        # what really arrived
        git(env, "diff", "--name-only", old, "HEAD"),
        # (publish: file work, nothing run) then the check brought up to date
        git(env, *HEAD_LOG[1:]),
        git(env, "rev-list", "--count", "HEAD..origin/main"),
        git(env, "diff", "--name-only", "HEAD", "origin/main"),
    ]
    assert json.dumps(env.fake.projects, sort_keys=True) == containers  # nobody stopped or recreated
    assert op["outcome"] == "ok"
    assert op["summary"] == f"Updated the app library to {new[:7]}."
    assert op["old"]["commit"] == old and op["new"]["commit"] == new != old
    assert index(env)["sources"]["openfit"]["commit"] == new
    assert shelf_apps(env) == ["caffeine", "steps"]
    s = logged_in.get("/api/status").get_json()
    assert s["update_apps_available"] is False and s["update_available"] is False
    assert s["library"]["current"] is True and s["library"]["apps"] == ["caffeine", "steps"]
    assert s["history"][0]["kind"] == "update_apps"
    assert s["undo"] is None  # the code moved on from the last Update OpenFit, if any


def test_update_apps_backs_off_when_openfit_itself_arrived(admin_env):
    env = admin_env
    env.admin.publish_at_startup()
    old = head(env)
    apps_pending(env)
    # GitHub moved after the check, and something fetched it.
    env.repos.push({"app/main.py": "print('sneaked in')\n"}, "OpenFit change")
    run(env.repos.repo, "git", "fetch", "-q", "origin", "main")
    before = snapshot(env)
    env.fake.clear()

    op = run_op(env, env.admin.start_update_apps)

    assert op["outcome"] == "failed"
    assert op["summary"] == "OpenFit itself changed since the last check. Check again, then use Update OpenFit."
    commands = env.fake.commands()
    diff = commands.index(["git", "diff", "--name-only", old, "HEAD"])
    assert commands[diff + 1:] == [["git", "reset", "--hard", old]]
    assert head(env) == old and env.repos.status() == ""
    assert snapshot(env) == before
    assert "app/main.py" in "\n".join(texts(op))
    assert not any(c[0] == "docker" for c in commands)


def test_update_apps_puts_the_code_back_when_the_publish_fails(admin_env, monkeypatch):
    env = admin_env
    env.admin.publish_at_startup()
    old = head(env)
    apps_pending(env)
    before = snapshot(env)

    def copy(src, dst):
        raise OSError("No space left on device")

    monkeypatch.setattr(admin_ops, "_copy_file", copy)
    env.fake.clear()
    op = run_op(env, env.admin.start_update_apps)

    assert op["outcome"] == "failed"
    assert op["summary"] == ("The app library couldn't be updated, so the code was put back. "
                             "The app library is as it was.")
    commands = env.fake.commands()
    assert commands[-1] == ["git", "reset", "--hard", old]
    assert head(env) == old
    assert snapshot(env) == before
    assert admin_ops.update_apps_offered(env.admin.last_check())  # still on offer
    assert not any(c[0] == "docker" for c in commands)


def test_one_operation_at_a_time_for_update_apps(logged_in, admin_env):
    env = admin_env
    apps_pending(env)
    assert env.admin.lock.acquire(blocking=False)
    try:
        for path in ("/api/library/update", "/api/library/publish", "/api/users/andy/restart"):
            r = logged_in.post(path, json={}, headers=H)
            assert r.status_code == 409, path
            assert "Something else is running" in r.get_json()["error"]
    finally:
        env.admin.lock.release()


# --- 6. mounts -----------------------------------------------------------------------


def test_the_users_template_mounts_the_shelf_read_only():
    text = admin_ops.USER_COMPOSE.format(image="openfit-tracker", port=8101)
    parsed = yaml.safe_load(text)
    tracker = parsed["services"]["tracker"]
    assert tracker["volumes"] == ["tracker-data:/data", "openfit-library:/library:ro"]
    assert parsed["volumes"] == {"tracker-data": {}, "openfit-library": {"external": True}}
    assert "env_file" not in tracker and "environment" not in tracker
    for banned in ("env_file", "environment", ".env"):
        assert banned not in text


def test_the_primary_override_is_written_at_startup(admin_env):
    env = admin_env
    path = env.data / "primary.override.yml"
    assert path.read_text() == OVERRIDE
    assert yaml.safe_load(OVERRIDE) == {"services": {"tracker": {"volumes": ["openfit-library:/library:ro"]}},
                                       "volumes": {"openfit-library": {"external": True}}}
    path.write_text("services: {}\n")
    env.fake.clear()
    env.make()
    assert path.read_text() == OVERRIDE
    os.utime(path, (1, 1))
    env.make()
    assert os.stat(path).st_mtime == 1  # the same: not rewritten
    path.unlink()
    env.make()
    assert path.read_text() == OVERRIDE
    assert env.fake.calls == []  # nobody restarted, nothing run


def test_the_primarys_compose_files_are_the_repos_then_the_override(admin_env):
    env = admin_env
    env.fake.clear()
    env.admin.compose(env.admin.primary(), "ps")
    assert env.fake.calls == [compose(env, "ps")]
    for bad in (env.admin.primary()._replace(compose_file=str(env.data / "other.yml")),
                env.admin.primary()._replace(project="openfit-sam")):
        with pytest.raises(ValueError):
            env.admin.compose(bad, "ps")
    assert env.admin.compose_files(env.admin.added("sam", 8101)) == [str(env.data / "users/sam/docker-compose.yml")]


def test_added_users_files_are_rewritten_when_they_differ(admin_env):
    env = admin_env
    seed_users(env, ("sam", 8101, "running"), ("kim", 8102, "exited"))
    a2 = admin_ops.USER_COMPOSE.replace("      - openfit-library:/library:ro\n", "").replace(
        "  openfit-library:\n    external: true\n", "")
    sam, kim = env.data / "users/sam/docker-compose.yml", env.data / "users/kim/docker-compose.yml"
    sam.write_text(a2.format(image="openfit-tracker", port=8101))  # from before the shelf
    os.utime(kim, (1, 1))

    env.make()

    assert sam.read_text() == admin_ops.USER_COMPOSE.format(image="openfit-tracker", port=8101)
    assert os.stat(kim).st_mtime == 1
    assert env.fake.calls == []  # nobody restarted
    assert env.fake.projects["openfit-sam"]["n"] == 0


def test_library_in_the_status_is_read_from_inspect(logged_in, admin_env):
    env = admin_env
    seed_users(env, ("sam", 8101, "running"))

    def library():
        return {u["name"]: u["library"] for u in logged_in.get("/api/status").get_json()["users"]}

    assert library() == {"andy": False, "sam": False}  # containers from before the shelf
    assert logged_in.post("/api/users/sam/restart", json={}, headers=H).status_code == 200
    assert library() == {"andy": False, "sam": True}
    assert logged_in.post("/api/users/andy/restart", json={}, headers=H).status_code == 200
    assert library() == {"andy": True, "sam": True}
    inspects = [c for c in env.fake.commands() if c[1] == "inspect"]
    assert inspects and all(c[2:4] == ["--type", "container"] for c in inspects)
    env.fake.fail(lambda a: a[1] == "inspect")
    assert library() == {"andy": False, "sam": False}


def test_an_add_writes_the_template_and_mounts_the_shelf(admin_env):
    env = admin_env
    op = run_op(env, lambda: env.admin.start_add_user("sam", 8101, "192.168.1.10:8090"))
    assert op["outcome"] == "ok"
    assert {u["name"]: u["library"] for u in env.admin.status()["users"]} == {"andy": False, "sam": True}


# --- 7. no recreate without the shelf's volume --------------------------------------


@pytest.fixture
def no_shelf(admin_env):
    seed_users(admin_env, ("sam", 8101, "running"))
    admin_env.fake.volumes.discard("openfit-library")
    return admin_env


@pytest.mark.parametrize("path", ["/api/users/sam/restart", "/api/users/andy/restart", "/api/users/sam/start"])
def test_restart_and_start_refuse_without_the_shelfs_volume(logged_in, no_shelf, path):
    r = logged_in.post(path, json={}, headers=H)
    assert r.status_code == 409
    assert r.get_json()["error"] == "The Admin needs updating first: its app library storage isn't there yet."
    assert no_shelf.fake.calls == [SHELF]
    assert not no_shelf.admin.lock.locked()


def test_an_update_refuses_without_the_shelfs_volume(logged_in, no_shelf):
    env = no_shelf
    env.repos.push({"app/main.py": "print(2)\n"}, "New")
    env.admin.check()
    old = head(env)
    env.fake.clear()
    r = logged_in.post("/api/update", json={}, headers=H)
    assert r.status_code == 409
    assert r.get_json()["error"] == admin_ops.VOLUME_MISSING
    assert env.fake.calls[-1] == SHELF
    assert not any(c[0] == "docker" and c[1] != "volume" for c in env.fake.commands())
    assert head(env) == old and env.admin.history() == [] and not env.admin.lock.locked()


def test_an_undo_refuses_without_the_shelfs_volume(admin_env):
    env = admin_env
    env.repos.push({"app/main.py": "print(2)\n"}, "New")
    env.admin.check()
    assert run_op(env, env.admin.start_update)["outcome"] == "ok"
    new = head(env)
    env.fake.volumes.discard("openfit-library")
    env.fake.clear()
    with pytest.raises(admin_ops.Refused) as e:
        env.admin.start_undo()
    assert (e.value.status, e.value.message) == (409, admin_ops.VOLUME_MISSING)
    assert env.fake.calls[-1] == SHELF
    assert head(env) == new and [o["kind"] for o in env.admin.history()] == ["update"]


def test_an_add_refuses_without_the_shelfs_volume(no_shelf):
    env = no_shelf
    env.fake.clear()
    with pytest.raises(admin_ops.Refused) as e:
        env.admin.start_add_user("lee", 8102)
    assert (e.value.status, e.value.message) == (409, admin_ops.VOLUME_MISSING)
    assert not any(c[:2] == ["docker", "compose"] for c in env.fake.commands())
    assert not (env.data / "users" / "lee").exists()


def test_update_apps_needs_no_shelf_volume_check(admin_env):
    env = admin_env
    apps_pending(env)
    env.fake.volumes.discard("openfit-library")
    assert run_op(env, env.admin.start_update_apps)["outcome"] == "ok"  # nobody is recreated


# --- 8. Restart -------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["sam", "andy"])
def test_restart_recreates_then_checks_it_answers(logged_in, admin_env, name):
    env = admin_env
    seed_users(env, ("sam", 8101, "running"))

    r = logged_in.post(f"/api/users/{name}/restart", json={}, headers=H)

    assert r.status_code == 200 and r.get_json() == {"ok": True}
    if name == "sam":
        assert env.fake.calls == [
            SHELF,
            ucompose(env, "sam", "up", "-d", "--no-build", "--force-recreate", "tracker"),
            ps("openfit-sam"),
            health(container_id("openfit-sam", 1)),
        ]
    else:
        assert env.fake.calls == [
            SHELF,
            compose(env, "up", "-d", "--no-build", "--force-recreate", "tracker"),
            ps("openfit"),
            health(C1),
        ]
    assert not env.admin.lock.locked()


def test_restart_starts_a_stopped_user(logged_in, admin_env):
    env = admin_env
    seed_users(env, ("kim", 8102, "exited"))
    assert logged_in.post("/api/users/kim/restart", json={}, headers=H).status_code == 200
    assert {u["name"]: u["state"] for u in logged_in.get("/api/status").get_json()["users"]}["kim"] == "running"


def test_restart_an_unknown_user_is_404(logged_in, admin_env):
    r = logged_in.post("/api/users/nobody/restart", json={}, headers=H)
    assert r.status_code == 404
    assert admin_env.fake.calls == []


def test_a_restart_that_does_not_answer_is_a_502(logged_in, admin_env):
    env = admin_env
    env.fake.unhealthy = {C1}
    r = logged_in.post("/api/users/andy/restart", json={}, headers=H)
    assert r.status_code == 502
    assert r.get_json()["error"] == "andy's OpenFit restarted, but didn't answer."
    assert not env.admin.lock.locked()


def test_a_restart_that_fails_says_why(logged_in, admin_env):
    env = admin_env
    env.fake.fail(lambda a: "--force-recreate" in a, err="Error: no such image")
    r = logged_in.post("/api/users/andy/restart", json={}, headers=H)
    assert r.status_code == 502
    assert r.get_json()["error"] == "andy's OpenFit didn't restart."
    assert "no such image" in r.get_json()["detail"]
    assert not any(c[1] == "exec" for c in env.fake.commands())


def test_restart_needs_a_session_and_the_header(admin_client, admin_env):
    r = admin_client.post("/api/users/andy/restart", json={}, headers=H)
    assert r.status_code == 401
    admin_env.auth.setup(admin_env.auth.setup_code, PASSWORD)
    admin_client.post("/api/login", json={"password": PASSWORD}, headers=H)
    assert admin_client.post("/api/users/andy/restart", json={}).status_code == 403
    assert admin_env.fake.calls == []


# --- 9. path safety ----------------------------------------------------------------------


def test_links_in_the_repo_never_lead_a_publish_outside_it(admin_env, tmp_path):
    env = admin_env
    lib = Path(env.repos.repo) / "library"
    outside = tmp_path / "outside"
    write(outside, {"victim.txt": "keep\n", "app.yaml": "id: victim\n"})
    before = {p: p.read_bytes() for p in outside.rglob("*")}
    os.symlink(outside, lib / "evil")
    os.symlink(outside / "victim.txt", lib / "steps" / "victim.txt")
    os.symlink(outside, lib / "steps" / "door")
    os.symlink("../../..", lib / "steps" / "up")

    env.admin.publish_official()

    assert {p: p.read_bytes() for p in outside.rglob("*")} == before
    assert shelf_apps(env) == ["steps"]
    assert files_in(env.library / "openfit" / "steps") == ["app.yaml"]
    assert [p for p in env.library.rglob("*") if p.is_symlink()] == [env.library / "openfit"]


def test_links_planted_in_the_shelf_are_never_followed(admin_env, tmp_path):
    env = admin_env
    outside = tmp_path / "outside"
    write(outside, {"victim.txt": "keep\n"})
    os.symlink(outside / "victim.txt", env.library / "openfit.new")
    os.symlink(outside / "victim.txt", env.library / "index.json.tmp")

    env.admin.publish_official()

    assert (outside / "victim.txt").read_text() == "keep\n"
    assert os.listdir(outside) == ["victim.txt"]
    assert not os.path.lexists(env.library / "openfit.new")
    assert not os.path.lexists(env.library / "index.json.tmp")
    assert not os.path.islink(env.library / "index.json")
    assert index(env)["sources"]["openfit"]["commit"] == head(env)


def test_a_sets_folder_that_is_a_link_stops_the_publish(admin_env, tmp_path):
    env = admin_env
    outside = tmp_path / "outside"
    outside.mkdir()
    os.symlink(outside, env.library / ".sets")

    with pytest.raises(admin_ops.OpError):
        env.admin.publish_official()

    assert os.listdir(outside) == []
    assert sorted(os.listdir(env.library)) == [".sets"]


@pytest.mark.parametrize("parts", [
    ("..", "x"), (".sets", "..", "..", "x"), (".sets", "openfit-x", "..", "..", "..", "etc", "passwd"),
    ("door", "x"), ("door",) * 2,
])
def test_nothing_outside_the_shelf_passes_the_path_check(tmp_path, parts):
    root = tmp_path / "library"
    root.mkdir()
    os.symlink(tmp_path, root / "door")
    with pytest.raises(admin_ops.OpError):
        admin_ops._inside(str(root), os.path.join(str(root), *parts))
    with pytest.raises(admin_ops.OpError):
        admin_ops._inside(str(root), str(root))
    assert admin_ops._inside(str(root), str(root / ".sets" / "openfit-x" / "app.yaml"))
    assert admin_ops._inside(str(root), str(root / "door"))  # the link itself, never what it points at


@pytest.mark.parametrize("name", ["..", ".", "a..b", "..evil", "x/y", "Caps", "-x", "x_y", ""])
def test_an_app_folder_name_is_plain(name):
    assert not admin_ops._SHELF_APP.match(name)


# --- the page ------------------------------------------------------------------------------


def test_the_page_has_the_app_library_card_and_restart(logged_in):
    html = logged_in.get("/").get_data(as_text=True)
    for text in ("App library", "Update apps", "Publishes the new apps to every user. Nobody's OpenFit restarts.",
                 "The app library couldn't be updated", "Publish again", "Restart",
                 "Restart to see the app library", "/api/library/update", "/api/library/publish"):
        assert text in html, text
