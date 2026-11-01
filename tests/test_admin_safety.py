"""The Admin stays in its lane: its users' compose projects, each with its
own compose files (the primary's exactly the repo's and the Admin's
override, in that order); their labelled containers, where it execs only
the health check and a setup link's `python auth_cli.py setup-link`; its own --rm
helpers; the user system's image; the app shelf's volume, only ever
looked at; a removed user's kept volume, only on a confirmed delete or
read out, read-only, by the download helper in its one exact form (or,
for a restore, the same helper with nothing excluded); a restored user's
new volume, created by `up --no-start` and filled by the restore helper in
its one exact form, from a clean archive the Admin wrote;
`docker info`, with one format, for the System panel; and its own
project, only to update itself or revert that - its image tagged only
among openfit-admin:{local,prev,next}, its swap helper in an update's form
or a revert's. Publishing the shelf runs no docker command at all.
Argument lists, never a shell; git as the repo's owner, docker as root -
but an organisation repo's git (A6): as root, in its own clone, only
clone, fetch origin, rev-parse, log, merge --ff-only, reset --hard and
cat-file, each with exactly its pinned GIT_SSH_COMMAND; and ssh-keygen in
its one form, for a repo's deploy key."""

import glob
import io
import json
import os
import re
import sqlite3
import subprocess
import tarfile
import tempfile

import pytest
from conftest import CONTAINERS, container_id, seed_users

import admin_ops
import admin_swap

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
C0, C1, C2 = CONTAINERS[:3]


def pending(env, message="New OpenFit"):
    env.repos.push({"app/main.py": f"print({message!r})\n"}, message)
    env.admin.check()


def update(env):
    env.admin.start_update()
    env.admin.wait(10)
    return env.admin.current_op()


def undo(env):
    env.admin.start_undo()
    env.admin.wait(10)
    return env.admin.current_op()


def add(env, name, port):
    env.admin.start_add_user(name, port, "192.168.1.10:8090")
    env.admin.wait(10)
    return env.admin.current_op()


def three_users(env):
    seed_users(env, ("sam", 8101, "running"), ("kim", 8102, "exited"))


def apps_pending(env, message="New app"):
    env.repos.push({"library/caffeine/app.yaml": f"id: caffeine  # {message}\n"}, message)
    env.admin.check()


AVIATION = {"repo.yaml": "source: openfitavi\nname: OpenFit Aviation\n", "frms/app.yaml": "id: frms\nname: FRMS\n"}
AVI_URL = "https://github.com/Raven-git-hub/openfit-aviation"
AVI_SLUG = "raven-git-hub__openfit-aviation"


def repo_op(env, start):
    start()
    env.admin.wait(10)
    return env.admin.current_op()


def aviation_connected(env):
    """The aviation repo on GitHub, added and connected."""
    env.github.make("Raven-git-hub", "openfit-aviation", AVIATION)
    env.admin.add_repo(AVI_URL)
    assert repo_op(env, lambda: env.admin.start_repo_connect(AVI_SLUG))["outcome"] == "ok"


def a_real_backup(env, name="20261027T120000Z-abcdef0-andy.tar.gz"):
    """A backup that's a real archive (the fake's helper writes a stand-in)."""
    with tarfile.open(env.backups / name, "w:gz") as tar:
        for member, body in (("./tracker.db", b"db"), ("./auth.json", b"{}"), ("./.session_key", b"k" * 32)):
            info = tarfile.TarInfo(member)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
    return name


def with_migrations(env):
    """The repo's OpenFit holding migrations 001 to 010 at HEAD, as a
    restore's version check reads them."""
    for n in range(1, 11):
        path = os.path.join(env.repos.repo, "app", "migrations", f"{n:03d}_step.sql")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write("-- a step\n")
    for args in (["add", "-A"], ["commit", "-q", "-m", "Migrations"]):
        subprocess.run(["git", *args], cwd=env.repos.repo, check=True, capture_output=True)


def restorable(link=False):
    """A backup's bytes that a restore takes: a sound database at migration
    10, the password files beside it - or, `link`, a symbolic link too."""
    db = os.path.join(tempfile.mkdtemp(), "tracker.db")
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT)")
    conn.executemany("INSERT INTO schema_migrations VALUES (?, 'then')", [(v,) for v in range(1, 11)])
    conn.commit()
    conn.close()
    with open(db, "rb") as f:
        files = {"./tracker.db": f.read(), "./auth.json": b"{}", "./.session_key": b"k" * 32,
                 "./.secret_key": b"key"}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        root = tarfile.TarInfo("./")
        root.type = tarfile.DIRTYPE
        tar.addfile(root)
        for name, body in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(body)
            tar.addfile(info, io.BytesIO(body))
        if link:
            info = tarfile.TarInfo("./latest.db")
            info.type, info.linkname = tarfile.SYMTYPE, "tracker.db"
            tar.addfile(info)
    return buf.getvalue()


def restore(env, name, port, source, blob=None, keep=True):
    if source == "upload":
        path, f = env.admin.new_upload()
        with f:
            f.write(blob)
        source = ("upload", path)
    env.admin.start_restore(name, port, keep, source, "192.168.1.10:8090")
    env.admin.wait(10)
    return env.admin.current_op()


def revert_the_admin(env, unhealthy=()):
    """After an Admin update that worked: its finish, the offer read, then
    Revert the Admin - the Admin's own steps, then its helper's revert.
    The helper's outcome."""
    assert update_the_admin(env) == "ok"
    env.fake.helper = False
    assert env.admin.current_op()["outcome"] == "ok"
    env.clock.advance(60)
    assert env.admin.status()["admin_revert"]["offered"] is True
    env.admin.start_admin_revert()
    env.admin.wait(10)
    run = env.fake.commands()[-1]
    env.fake.unhealthy = {container_id("openfit-admin", n) for n in unhealthy}
    return admin_swap.revert(env.fake, env.repos.repo, str(env.data), run[-3], run[-2], run[-1], env.clock,
                             env.clock.sleep)


def update_the_admin(env, unhealthy=()):
    """An Admin change pulled by Update OpenFit, then Update the Admin: the
    Admin's own steps, then its helper's swap. The helper's outcome."""
    env.repos.push({"admin/admin_server.py": "print('admin 2')\n"}, "Admin fix")
    env.admin.check()
    assert update(env)["outcome"] == "ok"
    env.admin.start_admin_update()
    env.admin.wait(10)
    run = env.fake.commands()[-1]
    env.fake.unhealthy = {container_id("openfit-admin", n) for n in unhealthy}
    return admin_swap.swap(env.fake, env.repos.repo, str(env.data), run[-2], run[-1], env.clock, env.clock.sleep)


# Each scenario returns what it may do beyond the lane every scenario keeps
# to: "down_v", a failed add's `down -v`; "volume_rm", the one removed
# user whose kept volume it may delete.


def scenario_success(env):
    pending(env)
    assert update(env)["outcome"] == "ok"


def scenario_rollback(env):
    pending(env)
    env.fake.unhealthy = {C1}
    assert update(env)["outcome"] == "rolled_back"


def scenario_undo(env):
    pending(env)
    assert update(env)["outcome"] == "ok"
    assert undo(env)["outcome"] == "ok"


def scenario_build_fails(env):
    pending(env)
    env.fake.fail(lambda a: "build" in a)
    assert update(env)["outcome"] == "failed"


def scenario_stop_fails(env):
    pending(env)
    env.fake.fail(lambda a: a[:2] == ["docker", "compose"] and "stop" in a)
    assert update(env)["outcome"] == "failed"


def scenario_backup_fails(env):
    pending(env)
    env.fake.fail(lambda a: "czf" in a)
    assert update(env)["outcome"] == "failed"


def scenario_status(env):
    pending(env)
    env.admin.status()


def scenario_add_user(env):
    assert add(env, "sam", 8101)["outcome"] == "ok"
    env.admin.status()


def scenario_add_user_fails(env):
    seed_users(env, ("kim", 8101, "running"))
    env.fake.unhealthy = {container_id("openfit-sam", 0)}
    assert add(env, "sam", 8102)["outcome"] == "failed"
    return {"down_v": True}


def scenario_add_user_port_clash(env):
    env.fake.fail(lambda a: "up" in a, err="Bind for 0.0.0.0:8101 failed: port is already allocated")
    assert add(env, "sam", 8101)["outcome"] == "failed"
    return {"down_v": True}


def scenario_add_removed_user_fails(env):
    seed_users(env, ("sam", 8101, "running"))
    env.admin.remove_user("sam")
    env.fake.unhealthy = {container_id("openfit-sam", 1)}
    assert add(env, "sam", 8101)["outcome"] == "failed"


def scenario_start_stop(env):
    seed_users(env, ("sam", 8101, "running"))
    for name in ("sam", "andy"):
        env.admin.stop_user(name)
        env.admin.start_user(name)


def scenario_restart(env):
    seed_users(env, ("sam", 8101, "running"), ("kim", 8102, "exited"))
    for name in ("sam", "kim", "andy"):
        env.admin.restart_user(name)
    env.admin.status()


def scenario_restart_without_the_shelf(env):
    seed_users(env, ("sam", 8101, "running"))
    env.fake.volumes.discard("openfit-library")
    for name in ("sam", "andy"):
        with pytest.raises(admin_ops.Refused):
            env.admin.restart_user(name)
    pending(env)
    with pytest.raises(admin_ops.Refused):
        env.admin.start_update()


def scenario_update_apps(env):
    three_users(env)
    env.admin.publish_at_startup()
    apps_pending(env)
    env.admin.start_update_apps()
    env.admin.wait(10)
    assert env.admin.current_op()["outcome"] == "ok"
    env.admin.status()


def scenario_update_publishes(env):
    three_users(env)
    env.admin.publish_at_startup()
    env.repos.push({"app/main.py": "print('new')\n", "library/caffeine/app.yaml": "id: caffeine\n"}, "Both")
    env.admin.check()
    assert update(env)["outcome"] == "ok"
    assert undo(env)["outcome"] == "ok"


def scenario_publish_again(env):
    seed_users(env, ("sam", 8101, "running"))
    env.make().publish_at_startup()
    env.repos.push({"library/caffeine/app.yaml": "id: caffeine\n"}, "New app")
    subprocess.run(["git", "pull", "-q", "--ff-only"], cwd=env.repos.repo, check=True, capture_output=True)
    env.admin.publish_again()
    env.admin.status()


def scenario_remove_user(env):
    seed_users(env, ("sam", 8101, "running"), ("kim", 8102, "running"))
    env.admin.remove_user("sam")
    env.admin.status()


def scenario_delete_data(env):
    seed_users(env, ("sam", 8101, "running"), ("kim", 8102, "running"))
    env.admin.remove_user("sam")
    env.admin.delete_data("sam", "sam")
    return {"volume_rm": "sam"}


def scenario_update_everyone(env):
    three_users(env)
    pending(env)
    assert update(env)["outcome"] == "ok"


def scenario_rollback_everyone(env):
    three_users(env)
    pending(env)
    env.fake.unhealthy = {container_id("openfit-sam", 1)}
    assert update(env)["outcome"] == "rolled_back"


def scenario_undo_everyone(env):
    three_users(env)
    pending(env)
    assert update(env)["outcome"] == "ok"
    assert undo(env)["outcome"] == "ok"


def scenario_backup_fails_everyone(env):
    three_users(env)
    pending(env)
    env.fake.fail(lambda a: "czf" in a and "openfit-kim_tracker-data:/v:ro" in a)
    assert update(env)["outcome"] == "failed"


def scenario_setup_link(env):
    seed_users(env, ("sam", 8101, "running"), ("kim", 8102, "exited"))
    for name in ("andy", "sam"):
        assert env.admin.setup_link(name, "192.168.1.10:8090")["link"]
    with pytest.raises(admin_ops.Refused):
        env.admin.setup_link("kim", "192.168.1.10:8090")
    env.admin.status()


def scenario_admin_update(env):
    three_users(env)
    pending(env)
    assert update_the_admin(env) == "ok"
    return {"admin": True}


def scenario_admin_rollback(env):
    three_users(env)
    pending(env)
    assert update_the_admin(env, unhealthy=[1]) == "rolled_back"
    return {"admin": True}


def scenario_admin_rollback_fails(env):
    pending(env)
    assert update_the_admin(env, unhealthy=[1, 2]) == "failed"
    return {"admin": True}


def scenario_admin_revert(env):
    three_users(env)
    pending(env)
    assert revert_the_admin(env) == "ok"
    return {"admin": True}


def scenario_admin_revert_falls_back(env):
    three_users(env)
    pending(env)
    assert revert_the_admin(env, unhealthy=[2]) == "rolled_back"
    return {"admin": True}


def scenario_admin_revert_fails(env):
    pending(env)
    assert revert_the_admin(env, unhealthy=[2, 3]) == "failed"
    return {"admin": True}


def scenario_download_backup(env):
    three_users(env)
    pending(env)
    assert update(env)["outcome"] == "ok"
    _filename, chunks = env.admin.download_backup(a_real_backup(env))
    assert b"".join(chunks)
    env.admin.status()


def scenario_download_kept(env):
    seed_users(env, ("sam", 8101, "running"), ("kim", 8102, "running"))
    env.admin.remove_user("sam")
    env.fake.stream_out = b"an archive"
    _filename, chunks = env.admin.download_kept("sam")
    assert b"".join(chunks) == b"an archive"
    with pytest.raises(admin_ops.Refused):
        env.admin.download_kept("kim")  # not removed
    env.admin.status()


def scenario_delete_backup(env):
    three_users(env)
    pending(env)
    assert update(env)["outcome"] == "ok"
    env.admin.delete_backup(env.admin.list_backups()[0]["name"])
    assert env.admin.status()["undo"] is None


def scenario_restore_upload(env):
    three_users(env)
    with_migrations(env)
    assert restore(env, "lee", 8110, "upload", restorable())["outcome"] == "ok"
    env.admin.status()
    return {"restore": True}


def scenario_restore_from_backup(env):
    with_migrations(env)
    (env.backups / "20261027T120000Z-abcdef0-andy.tar.gz").write_bytes(restorable())
    assert restore(env, "lee", 8110, ("backup", "20261027T120000Z-abcdef0-andy.tar.gz"), keep=False)["outcome"] == "ok"
    return {"restore": True}


def scenario_restore_from_kept(env):
    seed_users(env, ("sam", 8101, "running"), ("kim", 8102, "running"))
    with_migrations(env)
    env.admin.remove_user("sam")
    env.fake.stream_out = restorable()
    assert restore(env, "lee", 8110, ("removed", "sam"))["outcome"] == "ok"
    with pytest.raises(admin_ops.Refused):
        restore(env, "sam", 8111, ("removed", "sam"))  # kept data's name
    return {"restore": True, "pack": True}


def scenario_restore_fails(env):
    seed_users(env, ("kim", 8101, "running"))
    with_migrations(env)
    env.fake.unhealthy = {container_id("openfit-lee", 1)}
    assert restore(env, "lee", 8110, "upload", restorable())["outcome"] == "failed"
    return {"restore": True, "down_v": True}


def scenario_restore_refused(env):
    seed_users(env, ("kim", 8101, "running"))
    with_migrations(env)
    assert restore(env, "lee", 8110, "upload", restorable(link=True))["outcome"] == "failed"


def scenario_admin_build_fails(env):
    pending(env)
    env.fake.fail(lambda a: a[:4] == ["docker", "compose", "-p", "openfit-admin"] and "build" in a)
    env.repos.push({"admin/admin_server.py": "print('admin 2')\n"}, "Admin fix")
    env.admin.check()
    assert update(env)["outcome"] == "ok"
    env.admin.start_admin_update()
    env.admin.wait(10)
    assert env.admin.current_op()["outcome"] == "failed"
    return {"admin": True}


def at_the_schedule(env, **fields):
    """The schedule saved, its baseline ticked, then the clock at its next
    due time: the run's outcome."""
    env.admin.save_schedule({"day": "sun", "time": "03:00", "timezone": "Asia/Hong_Kong", "mode": "check", **fields})
    assert env.admin.tick() is None
    env.clock.now = admin_ops.next_run(env.admin.schedule(), env.clock())
    return env.admin.tick()


def scenario_scheduled_check(env):
    three_users(env)
    env.repos.push({"app/main.py": "print('new')\n", "admin/admin_server.py": "print('admin 2')\n"}, "Both")
    assert at_the_schedule(env) == "ok"
    env.admin.status()


def scenario_scheduled_apps(env):
    three_users(env)
    env.repos.push({"library/caffeine/app.yaml": "id: caffeine\n"}, "New app")
    assert at_the_schedule(env, mode="update") == "ok"
    env.admin.status()


def scenario_admin_scheduled_update(env):
    """A scheduled run that updates everyone, then hands over to the Admin's
    own update, and the helper's swap."""
    three_users(env)
    env.repos.push({"app/main.py": "print('new')\n", "admin/admin_server.py": "print('admin 2')\n"}, "Both")
    assert at_the_schedule(env, mode="update") == "ok"
    run = env.fake.commands()[-1]
    assert admin_swap.swap(env.fake, env.repos.repo, str(env.data), run[-2], run[-1], env.clock, env.clock.sleep) == "ok"
    env.fake.helper = False
    env.admin.status()
    return {"admin": True}


def scenario_repo_add(env):
    env.admin.add_repo(AVI_URL)
    env.admin.status()


def scenario_repo_connect(env):
    three_users(env)
    aviation_connected(env)
    env.admin.status()


def scenario_repo_key_refused(env):
    env.github.make("Raven-git-hub", "openfit-aviation", AVIATION)
    env.admin.add_repo(AVI_URL)
    env.fake.fail(lambda a: a[:2] == ["git", "clone"], rc=128, err="git@github.com: Permission denied (publickey).")
    assert repo_op(env, lambda: env.admin.start_repo_connect(AVI_SLUG))["outcome"] == "failed"
    env.admin.status()


def scenario_repo_check_and_update(env):
    three_users(env)
    aviation_connected(env)
    env.github.push("Raven-git-hub", "openfit-aviation", {"frms/notes.txt": "x\n"}, "Notes")
    pending(env)  # an OpenFit change too, checked in the same check
    assert repo_op(env, lambda: env.admin.start_repo_update(AVI_SLUG))["outcome"] == "ok"
    env.admin.status()


def scenario_repo_update_not_a_fast_forward(env):
    aviation_connected(env)
    work = env.github.tmp / "github-work" / "Raven-git-hub" / "openfit-aviation"
    subprocess.run(["git", "commit", "-q", "--amend", "-m", "Rewritten"], cwd=work, check=True)
    subprocess.run(["git", "push", "-qf", "origin", "HEAD:main"], cwd=work, check=True)
    env.admin.check()
    assert repo_op(env, lambda: env.admin.start_repo_update(AVI_SLUG))["outcome"] == "failed"


def scenario_repo_remove(env):
    seed_users(env, ("sam", 8101, "running"))
    env.admin.publish_official()
    aviation_connected(env)
    env.admin.remove_repo(AVI_SLUG)
    env.admin.status()


SCENARIOS = [
    scenario_success,
    scenario_rollback,
    scenario_undo,
    scenario_build_fails,
    scenario_stop_fails,
    scenario_backup_fails,
    scenario_status,
    scenario_add_user,
    scenario_add_user_fails,
    scenario_add_user_port_clash,
    scenario_add_removed_user_fails,
    scenario_start_stop,
    scenario_restart,
    scenario_restart_without_the_shelf,
    scenario_update_apps,
    scenario_update_publishes,
    scenario_publish_again,
    scenario_remove_user,
    scenario_delete_data,
    scenario_update_everyone,
    scenario_rollback_everyone,
    scenario_undo_everyone,
    scenario_backup_fails_everyone,
    scenario_setup_link,
    scenario_admin_update,
    scenario_admin_rollback,
    scenario_admin_rollback_fails,
    scenario_admin_build_fails,
    scenario_admin_revert,
    scenario_admin_revert_falls_back,
    scenario_admin_revert_fails,
    scenario_download_backup,
    scenario_download_kept,
    scenario_delete_backup,
    scenario_restore_upload,
    scenario_restore_from_backup,
    scenario_restore_from_kept,
    scenario_restore_fails,
    scenario_restore_refused,
    scenario_scheduled_check,
    scenario_scheduled_apps,
    scenario_admin_scheduled_update,
    scenario_repo_add,
    scenario_repo_connect,
    scenario_repo_key_refused,
    scenario_repo_check_and_update,
    scenario_repo_update_not_a_fast_forward,
    scenario_repo_remove,
]

REPO_SCENARIOS = (scenario_repo_connect, scenario_repo_check_and_update, scenario_repo_update_not_a_fast_forward,
                  scenario_repo_remove)


def _full(x):
    return isinstance(x, str) and len(x) == 40 and all(c in "0123456789abcdef" for c in x)


def org_git_in_lane(env, call):
    """An organisation repo's git command, held to its lane, written out
    here by hand rather than taken from the Admin: its own folder, as root,
    exactly its pinned GIT_SSH_COMMAND, and only one of the commands it may
    run, each in its one shape. The repo's slug."""
    a, repos = call.args, str(env.data / "repos") + "/"
    assert a[0] == "git" and (call.cwd or "").startswith(repos), call
    slug = call.cwd[len(repos):].removesuffix("/repo")
    assert admin_ops.REPO_SLUG.match(slug), call
    folder, clone = repos + slug, repos + slug + "/repo"
    assert call.as_owner is False, call
    assert call.env == {"GIT_SSH_COMMAND": (
        f"ssh -i {folder}/deploy_key -o IdentitiesOnly=yes -o UserKnownHostsFile=/app/github_known_hosts "
        "-o StrictHostKeyChecking=yes -o BatchMode=yes -o ConnectTimeout=20")}, call
    sub = a[1:]
    if sub[0] == "clone":
        owner, _, repo = slug.partition("__")
        assert call.cwd == folder and sub[:2] == ["clone", "--"] and sub[3:] == [clone], call
        assert sub[2].lower() == f"git@github.com:{owner}/{repo}.git" and len(sub) == 4, call
        return slug
    assert call.cwd == clone, call
    assert (sub == ["fetch", "origin"]
            or sub in (["rev-parse", "--verify", "-q", "HEAD^{commit}"], ["rev-parse", "--verify", "-q", "@{upstream}^{commit}"])
            or (sub[:2] in (["merge", "--ff-only"], ["reset", "--hard"]) and len(sub) == 3 and _full(sub[2]))
            or (sub[:2] == ["cat-file", "-p"] and len(sub) == 3 and sub[2].endswith("^{tree}") and _full(sub[2][:-7]))
            or (sub[:2] in (["cat-file", "-s"], ["cat-file", "blob"]) and len(sub) == 3 and _full(sub[2]))
            or (sub[:3] == ["log", "--no-merges", "--format=%h%x09%s"] and len(sub) == 4 and sub[3].count("..") == 1
                and all(_full(x) for x in sub[3].split("..")))
            or (sub[:5] == ["log", "-n", "20", "--no-merges", "--format=%h%x09%s"] and len(sub) == 6 and _full(sub[5]))
            # an app folder's change notes (B3): one plain folder name, after --
            or (sub[:5] == ["log", "-n", "10", "--no-merges", "--format=%H%x09%cI%x09%s"] and len(sub) == 8
                and _full(sub[5]) and sub[6] == "--" and re.fullmatch(r"[a-z0-9][a-z0-9-]*", sub[7]))
            ), call
    return slug


def keygen_in_lane(env, call):
    """ssh-keygen, only ever making a repo's deploy key, in its one form."""
    a = call.args
    assert call.as_owner is False and call.env is None and call.cwd is None, call
    assert len(a) == 9 and a[:7] == ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", a[6]], a
    owner, _, repo = a[6].removeprefix("openfit-admin nas ").partition("/")
    slug = admin_ops.repo_slug(owner, repo)
    assert a[6] == f"openfit-admin nas {owner}/{repo}" and admin_ops.REPO_SLUG.match(slug), a
    assert a[7:] == ["-f", str(env.data / "repos" / slug / "deploy_key")], a


def known_names(env):
    """Every name the Admin has had in users.json in the scenario: its users
    and removed users now, and any it tried to add."""
    path = env.data / "users.json"
    reg = json.loads(path.read_text()) if path.exists() else {"users": [], "removed": []}
    names = {e["name"] for e in reg["users"] + reg["removed"]}
    names.update(o["user"]["name"] for o in env.admin.history() if o.get("kind") in ("add_user", "restore_user"))
    return names


def admin_lane(env, call, out, labelled, images, frames):
    """One of the Admin's own commands, in an Admin update or revert
    (`admin` allowed): True once it's held to its exact shape, False if it
    isn't one. Its own project only through admin_compose()'s three
    commands - `stop admin` only ever from the helper, never the Admin's
    own steps (and always followed by an up: assert_in_lane());
    its own container found by its labels; the helper looked for by its
    name and started exactly so, in an update's form or a revert's; the
    image it runs kept as :prev or :next, :prev or :next put back as
    :local, and :prev looked at - never a tag outside
    openfit-admin:{local,prev,next}."""
    a, repo = call.args, env.repos.repo
    folder = os.path.join(repo, "admin")
    if a[1:4] == ["compose", "-p", "openfit-admin"]:
        assert a[4:6] == ["-f", os.path.join(folder, "docker-compose.yml")] and call.cwd == folder, a
        assert a[6:] in (["build", "admin"], ["up", "-d", "--no-build", "--force-recreate", "admin"],
                         ["stop", "admin"]), a
        if a[6:] == ["stop", "admin"]:
            # The helper's swap or revert (admin_swap.py), never the Admin stopping itself.
            assert "admin_compose" in frames and {"_swap", "_revert"} & set(frames), (a, frames)
            assert not {"_admin_update", "_admin_revert"} & set(frames), (a, frames)
        return True
    if a[1] == "ps" and "label=com.docker.compose.project=openfit-admin" in a:
        assert a[2:] == ["-a", "--no-trunc", "--filter", "label=com.docker.compose.project=openfit-admin",
                         "--filter", "label=com.docker.compose.service=admin",
                         "--filter", "label=com.docker.compose.oneoff=False", "--format", "{{.ID}}\t{{.State}}"], a
        labelled.update((line.split("\t")[0], "openfit-admin") for line in out.splitlines() if line)
        return True
    if a[1] == "ps" and any(x.startswith("name=") for x in a):
        assert a[2:] == ["-a", "--no-trunc", "--filter", "name=^openfit-admin-swap$", "--format", "{{.ID}}"], a
        return True
    if a[1] == "tag" and a[3:] in (["openfit-admin:prev"], ["openfit-admin:next"]):
        assert len(a) == 4 and (a[2], "openfit-admin") in images, a
        return True
    if a[1] == "tag" and a[2:] in (["openfit-admin:prev", "openfit-admin:local"],
                                   ["openfit-admin:next", "openfit-admin:local"]):
        return True
    if a[1:3] == ["run", "-d"]:
        common = [
            "-d", "--rm", "--name", "openfit-admin-swap", "--label", "openfit.admin.helper=true",
            "-v", "/var/run/docker.sock:/var/run/docker.sock", "-v", f"{repo}:{repo}",
            "-v", "openfit-admin_admin-data:/data", "-e", f"OPENFIT_REPO={repo}",
        ]
        assert a[2:2 + len(common)] == common, a
        rest, stamp = a[2 + len(common):], admin_ops._FILE_STAMP
        if rest[0] == "openfit-admin:prev":  # an update's: on the image it ran before
            assert rest[1:3] == ["python", "/app/admin_swap.py"] and len(rest) == 5, a
            assert stamp.match(rest[3]) and admin_ops.UPDATE_OP_ID.match(rest[4]), a
        else:  # a revert's: on the image it runs now
            assert rest[:4] == ["openfit-admin:local", "python", "/app/admin_swap.py", "revert"] and len(rest) == 7, a
            assert stamp.match(rest[4]) and stamp.match(rest[5]) and admin_ops.REVERT_OP_ID.match(rest[6]), a
        return True
    if a[1:3] == ["image", "inspect"] and a[-1] == "openfit-admin:prev":
        assert a == ["docker", "image", "inspect", "--format", "{{.Id}}", "openfit-admin:prev"], a
        return True
    if a[1] == "exec" and (a[2], "openfit-admin") in labelled:
        assert a[3:] == ["python", "-c", admin_ops.ADMIN_HEALTH_CODE], a
        return True
    if a[1] == "inspect" and len(a) == 5 and (a[4], "openfit-admin") in labelled:
        assert a[2:4] == ["--type", "container"], a
        images.update((i["Image"], "openfit-admin") for i in json.loads(out))
        return True
    return False


def assert_in_lane(env, allow):
    root_file = os.path.join(env.repos.repo, "docker-compose.yml")
    override = str(env.data / "primary.override.yml")
    names = known_names(env) | ({allow["volume_rm"]} if allow.get("volume_rm") else set())
    assert "admin" not in names
    projects = {"openfit"} | {f"openfit-{n}" for n in names}
    labelled = set()  # (id, project) for each container the labelled lookups gave back
    images = set()  # (image, project) for those containers
    docker_calls = 0
    for call, (rc, out, _err), frames in zip(env.fake.calls, env.fake.results, env.fake.frames):
        a = call.args
        if a[0] == "git":
            if call.env is not None:
                org_git_in_lane(env, call)
            else:  # the server's checkout's, as its owner, in it
                assert call.as_owner is True and call.cwd == env.repos.repo, call
            continue
        if a[0] == "ssh-keygen":
            keygen_in_lane(env, call)
            continue
        assert a[0] == "docker", a
        docker_calls += 1
        if allow.get("admin") and admin_lane(env, call, out, labelled, images, frames):
            continue
        ids = {cid for cid, project in labelled if project != "openfit-admin"}
        sub = a[1]
        if sub == "compose":
            assert a[2] == "-p" and a[4] == "-f", a
            project = a[3]
            assert project in projects, a
            if project == "openfit":
                # The primary's files, exactly: the repo's, then the override.
                assert a[4:8] == ["-f", root_file, "-f", override] and call.cwd == env.repos.repo, a
                rest = a[8:]
            else:
                name = project.removeprefix("openfit-")
                assert a[4:6] == ["-f", str(env.data / "users" / name / "docker-compose.yml")], a
                assert call.cwd == str(env.data / "users" / name), a
                rest = a[6:]
            assert "-f" not in rest, a
            assert root_file not in a or project == "openfit", a  # the repo's file is the primary's alone
            if "-v" in rest:
                assert rest == ["down", "-v"] and project != "openfit" and allow.get("down_v"), a
            else:
                assert rest[-1] == "tracker" or rest in (["down"], ["up", "-d", "--no-build"]) or (
                    rest == ["up", "--no-start", "--no-build"] and allow.get("restore")), a
        elif sub == "ps":
            label = [x for x in a if x.startswith("label=com.docker.compose.project=")]
            assert len(label) == 1 and label[0].split("=", 2)[2] in projects, a
            assert a[a.index(label[0]) - 1] == "--filter", a
            assert a[2:4] == ["-a", "--no-trunc"], a
            project = label[0].split("=", 2)[2]
            labelled.update((line.split("\t")[0], project) for line in out.splitlines() if line)
        elif sub == "inspect":
            assert a[2:4] == ["--type", "container"] and a[4] in ids, a
            images.update((i["Image"], "users") for i in json.loads(out))
        elif sub == "exec":
            # In a user's container, only ever the health check or a setup
            # link - the Admin's own health check is the admin lane's.
            assert a[2] in ids, a
            assert a[3:] in (["python", "-c", admin_ops.HEALTH_CODE], ["python", "auth_cli.py", "setup-link"]), a
        elif sub == "tag":
            assert (a[2], "users") in images or a[2] == "openfit-tracker:prev", a
            assert a[3] in ("openfit-tracker:prev", "openfit-tracker:latest"), a
        elif sub == "run":
            assert a[2:5] == ["--rm", "--label", "openfit.admin.helper=true"], a
            assert "openfit-admin:local" in a, a
            after = a[a.index("openfit-admin:local") + 1:]
            if "xzf" in after:
                # The restore helper: a restored user's new volume filled from
                # the clean archive the Admin wrote, exactly so.
                assert allow.get("restore"), a
                filled = [n for n in names if a[8] == f"openfit-{n}_tracker-data:/v"]
                assert filled and a[:8] == ["docker", "run", "--rm", "--label", "openfit.admin.helper=true",
                                            "--log-driver", "none", "-v"], a
                assert a[9:] == ["-v", "openfit-admin_admin-data:/b:ro", "openfit-admin:local", "tar", "xzf",
                                 a[14], "-C", "/v"], a
                assert re.fullmatch(r"/b/restore-uploads/[0-9a-f]{32}-clean\.tar\.gz", a[14]), a
            elif after[:3] == ["tar", "czf", "-"] or "--log-driver" in a:
                # The download helper: a removed user's kept volume, read-only,
                # exactly so - or, packed for a restore, with nothing excluded.
                kept = [n for n in names if a[8] == f"openfit-{n}_tracker-data:/v:ro"]
                assert kept, a
                excluded = [] if allow.get("pack") and "--exclude=./auth.json" not in a else [
                    "--exclude=./auth.json", "--exclude=./.session_key"]
                assert a == ["docker", "run", "--rm", "--label", "openfit.admin.helper=true", "--log-driver", "none",
                             "-v", f"openfit-{kept[0]}_tracker-data:/v:ro", "openfit-admin:local", "tar", "czf", "-",
                             "-C", "/v", *excluded, "."], a
        elif sub == "image":
            assert a == ["docker", "image", "inspect", "--format", "{{.Id}}", "openfit-tracker:latest"], a
        elif sub == "info":
            # The System panel's look at the server: this format, exactly.
            assert a == ["docker", "info", "--format", "{{json .}}"], a
        elif sub == "volume" and a[2] == "inspect":
            looked_at = {f"openfit-{n}_tracker-data" for n in names} | {"openfit-library"}
            assert a[3:5] == ["--format", "{{.Name}}"] and a[5] in looked_at, a
            assert len(a) == 6, a
        elif sub == "volume" and a[2] == "rm":
            assert allow.get("volume_rm") and a == ["docker", "volume", "rm", f"openfit-{allow['volume_rm']}_tracker-data"], a
        else:
            pytest.fail(f"a docker command outside the Admin's lane: {a}")
        assert not (sub == "ps" and not any(x.startswith("label=com.docker.compose.project=") for x in a)), a
        assert "openfit-admin" not in a[2:4], a
        assert not any(x == "openfit-admin" or x.startswith("openfit-admin_") and x != "openfit-admin_admin-data:/b"
                       and x != "openfit-admin_admin-data:/b:ro" for x in a), a
    assert docker_calls
    # The Admin stopped is always started again: each stop is followed by
    # an up, with no other command on its project between (a second stop
    # aside - a revert's fallback after settings that wouldn't go back).
    own = [c.args[6:] for c in env.fake.calls if c.args[:4] == ["docker", "compose", "-p", "openfit-admin"]]
    for i, args in enumerate(own):
        if args == ["stop", "admin"]:
            after = [x for x in own[i + 1:] if x != ["stop", "admin"]]
            assert after and after[0] == ["up", "-d", "--no-build", "--force-recreate", "admin"], own


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda f: f.__name__.removeprefix("scenario_"))
def test_the_admin_stays_in_its_lane(admin_env, scenario):
    allow = scenario(admin_env) or {}
    assert_in_lane(admin_env, allow)
    # Its own project is named only in an Admin update, and there really.
    named = any(a[1:4] == ["compose", "-p", "openfit-admin"] for a in admin_env.fake.commands())
    assert named == bool(allow.get("admin"))
    # The two commands that delete data, where they're allowed, are really
    # there: the checks above aren't vacuous.
    commands = admin_env.fake.commands()
    assert any(a[-2:] == ["down", "-v"] for a in commands) == bool(allow.get("down_v"))
    assert any(a[1:3] == ["volume", "rm"] for a in commands) == bool(allow.get("volume_rm"))
    # And the download helper, where a scenario reads kept data out; packs
    # it, for a restore; and the restore helper, where a restore fills a volume.
    assert any("--exclude=./auth.json" in a for a in commands) == (scenario is scenario_download_kept)
    assert any("czf" in a and "--log-driver" in a and "--exclude=./auth.json" not in a for a in commands) == (
        scenario is scenario_restore_from_kept)
    assert any("xzf" in a for a in commands) == (scenario in (scenario_restore_upload, scenario_restore_from_backup,
                                                               scenario_restore_from_kept, scenario_restore_fails))
    assert any(a[-3:] == ["up", "--no-start", "--no-build"] for a in commands) == bool(allow.get("restore"))
    # And the Admin stopped, where its helper puts settings back: a
    # rollback, a revert, a revert's fallback - never an update that worked.
    assert any(a[1:4] == ["compose", "-p", "openfit-admin"] and a[-2:] == ["stop", "admin"] for a in commands) == (
        scenario in (scenario_admin_rollback, scenario_admin_rollback_fails, scenario_admin_revert,
                     scenario_admin_revert_falls_back, scenario_admin_revert_fails))


def test_the_lane_holds_the_admins_stop_to_its_helper_and_an_up_after(admin_env):
    """The lane's own checks bite: a stop of the Admin from anywhere but
    the helper's swap or revert is out of it, and so is one from the
    helper that's never followed by an up."""
    env = admin_env
    log = admin_swap.Log(str(env.data), "x", "y", env.clock)

    admin_ops.admin_compose(env.fake, env.repos.repo, "stop", "admin")  # not the helper
    with pytest.raises(AssertionError):
        assert_in_lane(env, {"admin": True})

    def _swap():  # the helper's, by its name - but nothing starts it again
        admin_swap.stop(log, env.fake, env.repos.repo)

    env.fake.clear()
    _swap()
    with pytest.raises(AssertionError):
        assert_in_lane(env, {"admin": True})
    # Started again after: in the lane.
    admin_swap.recreate(env.fake, env.repos.repo)
    assert_in_lane(env, {"admin": True})


ADMIN_TAGS = {"openfit-admin:local", "openfit-admin:prev", "openfit-admin:next"}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda f: f.__name__.removeprefix("scenario_"))
def test_the_admins_image_is_tagged_only_among_its_three_tags(admin_env, scenario):
    """Every tag of the Admin's image lands on openfit-admin:{local,prev,next},
    from the image it runs or another of the three, and only through
    admin_tag(); :local only ever from :prev or :next."""
    scenario(admin_env)
    seen = 0
    for call, frames in zip(admin_env.fake.calls, admin_env.fake.frames):
        a = call.args
        if a[:2] != ["docker", "tag"]:
            continue
        assert len(a) == 4, a
        if a[3].startswith("openfit-tracker:"):
            assert a[3] in ("openfit-tracker:prev", "openfit-tracker:latest") and "admin_tag" not in frames, a
            continue
        seen += 1
        assert a[3] in ADMIN_TAGS and "admin_tag" in frames, (a, frames)
        assert a[2] in ADMIN_TAGS or admin_ops._IMAGE_ID.match(a[2]), a
        if a[3] == "openfit-admin:local":
            assert a[2] in ("openfit-admin:prev", "openfit-admin:next"), a
    # Every Admin update or revert tags it (so the check isn't vacuous); nothing else does.
    assert bool(seen) == scenario.__name__.startswith("scenario_admin_")


PUBLISHING = {"publish_set", "publish_official", "publish_at_startup", "publish_again", "_publish_step",
              "_republish", "publish_source", "unpublish_source", "_publish_repo", "_catch_up_repos"}


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda f: f.__name__.removeprefix("scenario_"))
def test_publishing_runs_no_docker_command(admin_env, scenario):
    scenario(admin_env)
    for call, frames in zip(admin_env.fake.calls, admin_env.fake.frames):
        if call.args[0] == "docker":
            assert not PUBLISHING & set(frames), (call.args, frames)
    # Where a scenario publishes, it really did: the check isn't vacuous.
    if scenario in (scenario_update_apps, scenario_update_publishes, scenario_publish_again, scenario_scheduled_apps,
                    scenario_admin_scheduled_update):
        assert admin_ops.shelf_source(admin_ops.read_shelf(str(admin_env.library)), "openfit")
    if scenario in REPO_SCENARIOS and scenario is not scenario_repo_remove:
        assert admin_ops.shelf_source(admin_ops.read_shelf(str(admin_env.library)), "openfitavi")


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda f: f.__name__.removeprefix("scenario_"))
def test_git_runs_as_the_owner_and_docker_as_root(admin_env, scenario):
    scenario(admin_env)
    org = 0
    for call in admin_env.fake.calls:
        if call.args[0] == "git" and call.env is None:
            assert call.as_owner is True, call
            assert call.cwd == admin_env.repos.repo
        elif call.args[0] == "git":
            # An organisation repo's: as root, in the Admin's own storage.
            assert call.as_owner is False and call.cwd.startswith(str(admin_env.data / "repos") + "/"), call
            org += 1
        else:
            assert call.as_owner is False and call.env is None, call
    assert bool(org) == (scenario in REPO_SCENARIOS or scenario is scenario_repo_key_refused)


# --- no shell ------------------------------------------------------------------


def test_no_shell_anywhere_in_the_admin():
    sources = sorted(glob.glob(os.path.join(ROOT, "admin", "*.py")))
    assert {os.path.basename(p) for p in sources} >= {"admin_ops.py", "admin_auth.py", "admin_server.py"}
    for path in sources:
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for banned in ("shell=True", "os.system", "os.popen"):
            assert banned not in text, f"{banned} in {path}"


def test_the_runner_takes_only_a_list(tmp_path):
    runner = admin_ops.Runner(str(tmp_path))
    for bad in ("echo hi", ("echo", "hi"), ["echo", 1]):
        with pytest.raises(TypeError):
            runner.run(bad)
    assert runner.run(["echo", "hi; echo there"]) == (0, "hi; echo there\n", "")


def test_the_runner_adds_env_to_a_command_and_takes_only_strings(tmp_path):
    runner = admin_ops.Runner(str(tmp_path))
    rc, out, _err = runner.run(["sh", "-c", 'printf "%s|%s" "$GIT_SSH_COMMAND" "$GIT_TERMINAL_PROMPT"'],
                               env={"GIT_SSH_COMMAND": "ssh -o BatchMode=yes"})
    assert (rc, out) == (0, "ssh -o BatchMode=yes|0")
    assert runner.run(["sh", "-c", 'printf "%s" "$GIT_SSH_COMMAND"'])[1] == os.environ.get("GIT_SSH_COMMAND", "")
    for bad in ({"A": 1}, {1: "a"}, [("A", "b")], "A=b"):
        with pytest.raises(TypeError):
            runner.run(["true"], env=bad)


def test_the_runner_never_raises_for_a_command(tmp_path):
    runner = admin_ops.Runner(str(tmp_path))
    rc, _out, err = runner.run(["no-such-command-anywhere"])
    assert rc == 127 and err
    rc, _out, err = runner.run(["sleep", "5"], timeout=0.2)
    assert rc == 124 and "timed out" in err


# --- git as the owner ------------------------------------------------------------


def test_as_owner_drops_to_the_repos_uid_and_gid(monkeypatch, tmp_path):
    seen = []

    def run(args, **kw):
        seen.append(kw)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(admin_ops.subprocess, "run", run)
    monkeypatch.setattr(admin_ops.os, "geteuid", lambda: 0)
    monkeypatch.setattr(admin_ops.Runner, "owner", lambda self: (1000, 1001))
    runner = admin_ops.Runner(str(tmp_path))

    runner.run(["git", "status"], cwd=str(tmp_path), as_owner=True)
    runner.run(["docker", "ps"])

    git, docker = seen
    assert (git["user"], git["group"], git["extra_groups"]) == (1000, 1001, [])
    assert git["env"]["HOME"] == "/tmp"
    assert git["env"]["GIT_TERMINAL_PROMPT"] == "0"
    assert "user" not in docker and "group" not in docker and "extra_groups" not in docker
    assert docker["env"].get("HOME") == os.environ.get("HOME")
    assert "shell" not in git and "shell" not in docker and "preexec_fn" not in git


@pytest.mark.skipif(os.geteuid() != 0, reason="dropping to another user needs root")
def test_as_owner_really_runs_as_the_owner(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    os.chown(repo, 65534, 65534)
    runner = admin_ops.Runner(str(repo))
    assert runner.run(["id", "-u"], as_owner=True)[1].strip() == "65534"
    assert runner.run(["id", "-G"], as_owner=True)[1].strip() == "65534"  # no root group kept
    assert runner.run(["id", "-u"])[1].strip() == "0"


# --- nothing odd reaches a command -------------------------------------------------


@pytest.mark.parametrize(
    "key, value",
    [
        ("user", "Andy"),
        ("user", "andy smith"),
        ("user", "admin"),
        ("user", "a" * 21),
        ("project", "openfit; rm -rf /"),
        ("project", "-p"),
        ("project", "openfit-admin"),
        ("service", "Tracker"),
        ("branch", "-x"),
        ("branch", "main/../other"),
        ("port", "8080"),
        ("port", True),
    ],
)
def test_settings_that_could_reach_a_command_are_refused(tmp_path, key, value):
    path = tmp_path / "admin.json"
    path.write_text(json.dumps({key: value}))
    with pytest.raises(ValueError):
        admin_ops.load_settings(str(tmp_path))


def test_settings_are_written_with_the_defaults_on_first_start(tmp_path):
    assert admin_ops.load_settings(str(tmp_path)) == {
        "user": "andy", "project": "openfit", "service": "tracker", "port": 8080, "branch": "main",
    }
    assert json.loads((tmp_path / "admin.json").read_text()) == admin_ops.DEFAULT_SETTINGS


def test_undo_restores_only_a_backup_from_its_own_listing(admin_env):
    admin = admin_env.admin
    op = admin._begin("undo")
    for name in ("../../etc/passwd", "x.tar.gz; reboot", "20261027T120000Z-abcdef0.tar.gz",
                 "20261027T120000Z-abcdef0-andy.tar.gz"):
        record = {"name": "andy", "created_at": None, "was_running": True, "backup": name}
        assert admin._undo_steps(op, {"commit": "0" * 40}, [(admin.primary(), record)]) is False
    assert admin_env.fake.calls == []
