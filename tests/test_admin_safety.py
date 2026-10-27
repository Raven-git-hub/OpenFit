"""The Admin stays in its lane: its users' compose projects, each with its
own compose file; their labelled containers; its own --rm helpers; the
user system's image; a removed user's kept volume, only on a confirmed
delete. Argument lists, never a shell; git as the repo's owner, docker as
root."""

import glob
import json
import os
import subprocess

import pytest
from conftest import CONTAINERS, container_id, seed_users

import admin_ops

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
    scenario_remove_user,
    scenario_delete_data,
    scenario_update_everyone,
    scenario_rollback_everyone,
    scenario_undo_everyone,
    scenario_backup_fails_everyone,
]


def known_names(env):
    """Every name the Admin has had in users.json in the scenario: its users
    and removed users now, and any it tried to add."""
    path = env.data / "users.json"
    reg = json.loads(path.read_text()) if path.exists() else {"users": [], "removed": []}
    names = {e["name"] for e in reg["users"] + reg["removed"]}
    names.update(o["user"]["name"] for o in env.admin.history() if o.get("kind") == "add_user")
    return names


def assert_in_lane(env, allow):
    root_file = os.path.join(env.repos.repo, "docker-compose.yml")
    names = known_names(env) | ({allow["volume_rm"]} if allow.get("volume_rm") else set())
    assert "admin" not in names
    projects = {"openfit"} | {f"openfit-{n}" for n in names}
    labelled = set()  # the container ids the labelled lookups gave back
    images = set()  # those containers' images
    docker_calls = 0
    for call, (rc, out, _err) in zip(env.fake.calls, env.fake.results):
        a = call.args
        if a[0] == "git":
            continue
        assert a[0] == "docker", a
        docker_calls += 1
        sub = a[1]
        if sub == "compose":
            assert a[2] == "-p" and a[4] == "-f", a
            project, compose_file = a[3], a[5]
            assert project in projects, a
            if project == "openfit":
                assert compose_file == root_file and call.cwd == env.repos.repo, a
            else:
                name = project.removeprefix("openfit-")
                assert compose_file == str(env.data / "users" / name / "docker-compose.yml"), a
                assert call.cwd == str(env.data / "users" / name), a
            assert compose_file != root_file or project == "openfit", a  # the repo's file is the primary's alone
            if "-v" in a:
                assert a[6:] == ["down", "-v"] and project != "openfit" and allow.get("down_v"), a
            else:
                assert a[-1] == "tracker" or a[6:] in (["down"], ["up", "-d", "--no-build"]), a
        elif sub == "ps":
            label = [x for x in a if x.startswith("label=com.docker.compose.project=")]
            assert len(label) == 1 and label[0].split("=", 2)[2] in projects, a
            assert a[a.index(label[0]) - 1] == "--filter", a
            assert a[2:4] == ["-a", "--no-trunc"], a
            labelled.update(line.split("\t")[0] for line in out.splitlines() if line)
        elif sub == "inspect":
            assert a[2:4] == ["--type", "container"] and a[4] in labelled, a
            images.update(i["Image"] for i in json.loads(out))
        elif sub == "exec":
            assert a[2] in labelled, a
        elif sub == "tag":
            assert a[2] in images or a[2] == "openfit-tracker:prev", a
            assert a[3] in ("openfit-tracker:prev", "openfit-tracker:latest"), a
        elif sub == "run":
            assert a[2:5] == ["--rm", "--label", "openfit.admin.helper=true"], a
            assert "openfit-admin:local" in a, a
        elif sub == "image":
            assert a == ["docker", "image", "inspect", "--format", "{{.Id}}", "openfit-tracker:latest"], a
        elif sub == "volume" and a[2] == "inspect":
            assert a[3:5] == ["--format", "{{.Name}}"] and a[5] in {f"openfit-{n}_tracker-data" for n in names}, a
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


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda f: f.__name__.removeprefix("scenario_"))
def test_the_admin_stays_in_its_lane(admin_env, scenario):
    allow = scenario(admin_env) or {}
    assert_in_lane(admin_env, allow)
    # The two commands that delete data, where they're allowed, are really
    # there: the checks above aren't vacuous.
    commands = admin_env.fake.commands()
    assert any(a[-2:] == ["down", "-v"] for a in commands) == bool(allow.get("down_v"))
    assert any(a[1:3] == ["volume", "rm"] for a in commands) == bool(allow.get("volume_rm"))


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda f: f.__name__.removeprefix("scenario_"))
def test_git_runs_as_the_owner_and_docker_as_root(admin_env, scenario):
    scenario(admin_env)
    for call in admin_env.fake.calls:
        if call.args[0] == "git":
            assert call.as_owner is True, call
            assert call.cwd == admin_env.repos.repo
        else:
            assert call.as_owner is False, call


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
