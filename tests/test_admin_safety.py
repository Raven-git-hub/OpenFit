"""The Admin stays in its lane: one compose project, one labelled
container, its own --rm helpers; argument lists, never a shell; git as
the repo's owner, docker as root."""

import glob
import json
import os
import subprocess

import pytest
from conftest import CONTAINERS

import admin_ops

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
C0, C1, C2 = CONTAINERS[:3]
PROJECT = "label=com.docker.compose.project=openfit"
SERVICE = "label=com.docker.compose.service=tracker"


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


SCENARIOS = [
    scenario_success,
    scenario_rollback,
    scenario_undo,
    scenario_build_fails,
    scenario_stop_fails,
    scenario_backup_fails,
    scenario_status,
]


def assert_in_lane(env):
    compose_file = os.path.join(env.repos.repo, "docker-compose.yml")
    labelled = set()  # the container ids the labelled lookup gave back
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
            assert a[2:6] == ["-p", "openfit", "-f", compose_file], a
            assert a[-1] == "tracker", a
            assert call.cwd == env.repos.repo
        elif sub == "ps":
            assert PROJECT in a and SERVICE in a, a
            assert a[a.index(PROJECT) - 1] == "--filter" and a[a.index(SERVICE) - 1] == "--filter"
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
        else:
            pytest.fail(f"a docker command outside the Admin's lane: {a}")
        assert not (sub == "ps" and "-a" in a and PROJECT not in a), a
        assert "openfit-admin" not in a[2:4], a
    assert docker_calls


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda f: f.__name__.removeprefix("scenario_"))
def test_the_admin_stays_in_its_lane(admin_env, scenario):
    scenario(admin_env)
    assert_in_lane(admin_env)


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
    op = admin_env.admin._begin("undo")
    for name in ("../../etc/passwd", "x.tar.gz; reboot", "20261027T120000Z-abcdef0.tar.gz"):
        assert admin_env.admin._undo_steps(op, {"commit": "0" * 40}, name) is False
    assert admin_env.fake.calls == []
