"""
A7, the Admin's visibility: a slow or full disk made obvious.

1. A full disk named as such: out_of_space()'s one table of patterns, and the
   steps that say "The server's disk is full" in place of their own words -
   the check, an organisation repo's fetch and connect, the build, a backup,
   a publish, the restore upload - while a network failure keeps its words.
2. Runner.lines(): a command's merged output a line at a time, stopped once
   it goes quiet for too long or runs too long, never through a shell.
3. The build's progress in the op's log: BuildKit's plain steps, boiled down
   (a transcript fixture), at most 60 lines an op.
4. The build's limits: 15 minutes in all, 5 with nothing new - a normal
   build failure, for the user system's build and the Admin's.
5. How busy the disk is: /proc/stat's I/O wait between one minute's read and
   the next, amber after two busy minutes, red at 60%.
6. How full it is: amber at 90%, red at 97%, the worse warning winning.
7. The page: the warning atop System information, the I/O wait row, and the
   update popup's "Heads up" above a Proceed that stays as it was.
8. A scheduled run's log starts with the warning it ran into.
9. The lane: the two builds' --progress plain forms, and nothing else.
"""

import errno
import io
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_admin_portal import needs_node, portal_status, run_page

import admin_ops

H = {"X-OpenFit-Admin": "1"}
PASSWORD = "a long password!"
ROOT = Path(__file__).parent.parent
TRANSCRIPT = ROOT / "tests" / "fixtures" / "buildkit" / "tracker-plain.txt"
OWNER, REPO = "Raven-git-hub", "openfit-aviation"
SLUG = "raven-git-hub__openfit-aviation"
AVIATION = {"repo.yaml": "source: openfitavi\nname: OpenFit Aviation\n", "frms/app.yaml": "id: frms\nname: FRMS\n"}
FULL = ("The server's disk is full (4 KB free). Free some space, for example by removing unused Docker images, "
        "then try again.")
ENOSPC = ("error: unable to write loose object file: No space left on device\n"
          "fatal: unable to write new index file\n")
OFFLINE = ("ssh: Could not resolve hostname github.com: Temporary failure in name resolution\r\n"
           "fatal: Could not read from remote repository.\n")
IDLE = ("The build stopped making progress for 5 minutes, at: [internal] load metadata for "
        "docker.io/library/python:3.12-slim. The server's disk or network may be very slow; see System information.")
TIMEOUT = ("The build took longer than 15 minutes, and was stopped at: [4/10] RUN pip install --no-cache-dir -r "
           "requirements.txt. The server's disk or network may be very slow; see System information.")


@pytest.fixture
def logged_in(admin_client, admin_env):
    admin_env.auth.setup(admin_env.auth.setup_code, PASSWORD)
    assert admin_client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
    return admin_client


# --- helpers ---------------------------------------------------------------------


def disk(monkeypatch, used, blocks=100, frsize=4096):
    """The disk the Admin keeps its data on: `used` of `blocks` blocks."""
    free = blocks - used
    monkeypatch.setattr(admin_ops.os, "statvfs", lambda path: SimpleNamespace(
        f_blocks=blocks, f_bfree=free, f_bavail=free, f_frsize=frsize))


def texts(op):
    return [line["text"] for line in op["log"]]


def run_op(env, start):
    start()
    env.admin.wait(10)
    return env.admin.current_op()


def pending(env, files=None):
    """An OpenFit update waiting, found by a check; the calls cleared."""
    env.repos.push(files or {"app/main.py": "print('new')\n"}, "New OpenFit")
    env.admin.check()
    env.fake.clear()


def pulled(env):
    """The Admin's new code pulled by Update OpenFit, its own update on offer."""
    env.repos.push({"admin/admin_server.py": "print('admin 2')\n"}, "Admin fix")
    env.admin.check()
    assert run_op(env, env.admin.start_update)["outcome"] == "ok"
    env.fake.clear()


def head(env):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=env.repos.repo, capture_output=True, text=True,
                          check=True).stdout.strip()


def is_build(project):
    return lambda a: a[:4] == ["docker", "compose", "-p", project] and "build" in a


def connected(env):
    """The aviation repo on GitHub, added, connected and published."""
    env.github.make(OWNER, REPO, AVIATION)
    env.admin.add_repo(f"https://github.com/{OWNER}/{REPO}")
    assert run_op(env, lambda: env.admin.start_repo_connect(SLUG))["outcome"] == "ok"
    env.fake.clear()


def cpu_text(idle, iowait, user=0, system=0):
    """/proc/stat as the kernel writes it: the `cpu` line, then the rest."""
    return (f"cpu  {user} 0 {system} {idle} {iowait} 0 0 0 0 0\n"
            f"cpu0 {user} 0 {system} {idle} {iowait} 0 0 0 0 0\n"
            "intr 1234 0 0\nctxt 5678\nbtime 1761566400\nprocesses 42\n")


def minutes(*pcts):
    """/proc/stat once a minute: a first read, then one a minute later for
    each of `pcts`, that minute's I/O wait (of 100 ticks)."""
    idle = iowait = 0
    out = [cpu_text(idle, iowait)]
    for p in pcts:
        idle, iowait = idle + 100 - p, iowait + p
        out.append(cpu_text(idle, iowait))
    return out


def fed(env, texts_or_errors):
    """The Admin's /proc/stat seam fed these, a read each."""
    queue = list(texts_or_errors)

    def read():
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    env.admin.proc_stat = read


def sampled(env, *pcts):
    """The disk sampled a minute apart, each minute's I/O wait `pcts`: the
    warning after each read."""
    fed(env, minutes(*pcts))
    seen = []
    for _ in range(len(pcts) + 1):
        env.admin.sample_disk()
        seen.append(env.admin.disk_warning())
    return seen


def busy(pct):
    return f"The server's disk is very busy (I/O wait {pct}%). Updates and builds will be slow, and may fail."


def python(code):
    """A command that runs `code` in this Python: an argument list."""
    return [sys.executable, "-c", code]


# --- 1. a full disk, said as such ---------------------------------------------------


def test_each_pattern_is_a_full_disk_wherever_it_is_said():
    for words in admin_ops.OUT_OF_SPACE:
        said = "fatal: " + " ... ".join(w.upper() for w in words)  # case aside
        assert admin_ops.out_of_space(said, "")
        assert admin_ops.out_of_space("", said)
        assert admin_ops.out_of_space("Building…\n", f"step 3\n{said}\nexiting")


@pytest.mark.parametrize("out, err", [
    ("", "error: unable to write loose object file: No space left on device"),
    ("", "write /var/lib/docker/overlay2/abc/diff/x: no space left on device"),
    ("Error: ENOSPC: write failed", ""),
    ("", "npm ERR! code ENOSPC"),
    ("", "error: copy failed: Disk quota exceeded"),
    ("error: unable to write loose object file", "fatal: there isn't enough space for it"),
    ("tar: /b/backups/x.tar.gz: Cannot write: No space left on device", ""),
    (b"[Errno 28] No space left on device", None),
])
def test_out_of_space_says_yes_to_a_full_disk(out, err):
    assert admin_ops.out_of_space(out, err) is True


@pytest.mark.parametrize("out, err", [
    ("", OFFLINE),
    ("", "fatal: unable to access 'https://github.com/x/y/': Could not resolve host: github.com"),
    ("", "ssh: connect to host github.com port 22: Connection timed out"),
    ("", "git@github.com: Permission denied (publickey)."),
    ("", "error: unable to write loose object file"),  # git says it for more than a full disk
    ("", "pip: no such package"),
    ("", "Error response from daemon: No such image: openfit-tracker:prev"),
    ("", "[Errno 5] Input/output error"),
    ("", ""),
    (None, None),
])
def test_out_of_space_says_no_to_everything_else(out, err):
    assert admin_ops.out_of_space(out, err) is False


def test_the_message_names_the_free_space_as_the_page_does(admin_env, monkeypatch):
    disk(monkeypatch, used=99)
    assert admin_env.admin.disk_full_message() == FULL
    for n, words in ((0, "0 B"), (1023, "1023 B"), (4096, "4 KB"), (3 << 20, "3.0 MB"), (int(1.5 * (1 << 30)), "1.5 GB")):
        assert admin_ops.size_words(n) == words

    def broken(path):
        raise OSError("gone")

    monkeypatch.setattr(admin_ops.os, "statvfs", broken)
    assert admin_env.admin.disk_full_message() == (
        "The server's disk is full. Free some space, for example by removing unused Docker images, then try again.")


def test_a_check_on_a_full_disk_says_so(logged_in, admin_env, monkeypatch):
    env = admin_env
    disk(monkeypatch, used=99)
    env.fake.fail(lambda a: a[:2] == ["git", "fetch"], rc=128, err=ENOSPC)
    with pytest.raises(admin_ops.OpError) as e:
        env.admin.check()
    assert e.value.message == FULL and e.value.disk_full
    assert "No space left on device" in e.value.detail  # git's own words kept, behind it
    error = env.admin.last_check()["error"]
    assert error["text"] == FULL and "No space left on device" in error["detail"]
    r = logged_in.post("/api/check", json={}, headers=H)
    assert r.status_code == 502 and r.get_json()["error"] == FULL


def test_a_check_that_cant_reach_github_keeps_its_words(admin_env):
    env = admin_env
    env.fake.fail(lambda a: a[:2] == ["git", "fetch"], rc=128, err=OFFLINE)
    with pytest.raises(admin_ops.OpError) as e:
        env.admin.check()
    assert e.value.message == "Couldn't reach GitHub to check for updates." and not e.value.disk_full


def test_a_check_whose_git_cant_write_says_the_disk_is_full(admin_env, monkeypatch):
    # The fetch got through; reading what it brought didn't - _git_out(), every read's.
    env = admin_env
    disk(monkeypatch, used=99)
    env.repos.push({"app/main.py": "print('new')\n"}, "New")
    env.fake.fail(lambda a: a[:2] == ["git", "rev-list"], rc=128, err="fatal: write error: No space left on device")
    with pytest.raises(admin_ops.OpError) as e:
        env.admin.check()
    assert e.value.message == FULL


def test_an_organisation_repos_fetch_on_a_full_disk_says_so(admin_env, monkeypatch):
    env = admin_env
    connected(env)
    disk(monkeypatch, used=99)
    env.fake.fail(lambda a: a[:3] == ["git", "fetch", "origin"] and a[3:] == [], rc=128, err=ENOSPC)
    env.admin.check()
    error = env.admin.repo_checks()[SLUG]["error"]
    assert error["text"] == FULL and error["disk_full"] is True and "No space left" in error["detail"]
    row = next(r for r in env.admin.status()["repos"] if r["key"] == f"repo:{SLUG}")
    assert row["repo"]["error"]["text"] == FULL

    env.fake.rules.clear()
    env.fake.fail(lambda a: a[:3] == ["git", "fetch", "origin"] and a[3:] == [], rc=128, err=OFFLINE)
    env.admin.check()
    error = env.admin.repo_checks()[SLUG]["error"]
    assert error["text"] == "Couldn't reach GitHub: ssh: Could not resolve hostname github.com: Temporary failure in name resolution"
    assert error["disk_full"] is False


def test_connecting_a_repo_on_a_full_disk_says_so(admin_env, monkeypatch):
    env = admin_env
    disk(monkeypatch, used=99)
    env.github.make(OWNER, REPO, AVIATION)
    env.admin.add_repo(f"https://github.com/{OWNER}/{REPO}")
    env.fake.fail(lambda a: a[:2] == ["git", "clone"], rc=128, err="fatal: write error: No space left on device\n")
    op = run_op(env, lambda: env.admin.start_repo_connect(SLUG))
    assert op["outcome"] == "failed"
    assert FULL in texts(op)[-2]
    assert op["summary"] == f"{FULL} {OWNER}/{REPO} is still waiting to connect."


def test_a_build_on_a_full_disk_says_so(admin_env, monkeypatch):
    env = admin_env
    old = head(env)
    pending(env)
    disk(monkeypatch, used=99)
    env.fake.script(is_build("openfit"), [
        (0, "#8 [tracker 4/10] RUN pip install --no-cache-dir -r requirements.txt"),
        (1, "#8 ERROR: failed to copy files: write /var/lib/docker/overlay2/x/diff: no space left on device"),
        (0, "failed to solve: write /var/lib/docker/overlay2/x/diff: no space left on device"),
    ], rc=1)
    op = run_op(env, env.admin.start_update)
    assert op["outcome"] == "failed"
    log = texts(op)
    said = next(t for t in log if t.startswith(FULL))
    assert "failed to solve" in said  # the build's tail, its detail
    assert op["summary"] == f"{FULL} The build failed, so nothing was changed. OpenFit kept running."
    assert head(env) == old
    assert not any(c[:2] == ["docker", "compose"] and ("stop" in c or "up" in c) for c in env.fake.commands())


def test_a_build_that_fails_otherwise_keeps_its_words(admin_env, monkeypatch):
    env = admin_env
    pending(env)
    env.fake.script(is_build("openfit"), [(0, "ERROR: pip: no such package")], rc=1)
    op = run_op(env, env.admin.start_update)
    assert op["summary"] == "The build failed, so nothing was changed. OpenFit kept running."
    assert "The new version didn't build.\nERROR: pip: no such package" in texts(op)


def test_a_backup_on_a_full_disk_says_so(admin_env, monkeypatch):
    env = admin_env
    old = head(env)
    pending(env)
    disk(monkeypatch, used=99)
    env.fake.fail(lambda a: a[:2] == ["docker", "run"] and "czf" in a,
                  err="tar: /b/backups/x.tar.gz: Wrote only 4096 of 10240 bytes\n"
                      "tar: Error is not recoverable: exiting now\ngzip: stdout: No space left on device\n")
    op = run_op(env, env.admin.start_update)
    assert op["outcome"] == "failed"
    assert any(t.startswith(FULL + "\n") and "No space left on device" in t for t in texts(op))
    assert op["summary"] == (f"{FULL} The data couldn't be backed up, so the update was called off. "
                             "Everyone is on the old version.")
    assert head(env) == old


def test_a_publish_on_a_full_disk_says_so(logged_in, admin_env, monkeypatch):
    env = admin_env
    disk(monkeypatch, used=99)

    def copy(src, dst):
        raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(admin_ops, "_copy_file", copy)
    with pytest.raises(admin_ops.OpError) as e:
        env.admin.publish_official()
    assert e.value.message == FULL and "No space left on device" in e.value.detail
    assert env.admin.library_error["text"] == FULL and env.admin.library_error["disk_full"] is True
    r = logged_in.post("/api/library/publish", json={}, headers=H)
    assert r.status_code == 502 and r.get_json()["error"] == FULL


def test_update_apps_on_a_full_disk_says_so(admin_env, monkeypatch):
    env = admin_env
    env.admin.publish_at_startup()
    env.repos.push({"library/caffeine/app.yaml": "id: caffeine\n"}, "New app")
    env.admin.check()
    disk(monkeypatch, used=99)
    monkeypatch.setattr(admin_ops, "_copy_file",
                        lambda src, dst: (_ for _ in ()).throw(OSError(errno.ENOSPC, "No space left on device")))
    op = run_op(env, env.admin.start_update_apps)
    assert op["outcome"] == "failed"
    assert op["summary"] == (f"{FULL} The app library couldn't be updated, so the code was put back. "
                             "The app library is as it was.")


def test_a_restore_upload_onto_a_full_disk_is_a_507(logged_in, admin_env, monkeypatch):
    env = admin_env
    disk(monkeypatch, used=99)
    made = []
    real = env.admin.new_upload

    def upload():
        path, f = real()
        made.append(path)

        class Full(io.RawIOBase):
            def writable(self):
                return True

            def write(self, data):
                raise OSError(errno.ENOSPC, "No space left on device")

        f.close()
        return path, Full()

    monkeypatch.setattr(env.admin, "new_upload", upload)
    r = logged_in.post("/api/users/restore", headers=H, content_type="multipart/form-data", data={
        "name": "kim", "port": "8102", "keep_devices": "true", "file": (io.BytesIO(os.urandom(2000)), "b.tar.gz")})
    assert r.status_code == 507 and r.get_json() == {"ok": False, "error": FULL}
    assert made and not os.path.exists(made[0])  # dropped, as any refused upload is


# --- 2. Runner.lines() ----------------------------------------------------------------


def test_lines_arrive_in_order_as_they_come(tmp_path):
    runner, seen = admin_ops.Runner(str(tmp_path)), []
    code = "import time\nfor i in range(5):\n    print(f'line {i}', flush=True)\n    time.sleep(0.05)\n"
    rc, tail, why = runner.lines(python(code), on_line=seen.append, timeout=20, idle=10)
    assert (rc, why) == (0, None)
    assert seen == [f"line {i}" for i in range(5)]
    assert tail == "\n".join(seen)


def test_the_tail_is_the_last_forty_lines_and_the_exit_code_kept(tmp_path):
    runner = admin_ops.Runner(str(tmp_path))
    rc, tail, why = runner.lines(python("import sys\nfor i in range(100): print(i)\nsys.exit(3)"), timeout=20, idle=10)
    assert (rc, why) == (3, None)
    assert tail.splitlines() == [str(i) for i in range(60, 100)]


def test_stdout_and_stderr_are_merged_in_the_order_they_come(tmp_path):
    runner, seen = admin_ops.Runner(str(tmp_path)), []
    code = ("import sys, time\nprint('out 1', flush=True)\ntime.sleep(0.1)\n"
            "print('err 1', file=sys.stderr, flush=True)\ntime.sleep(0.1)\nprint('out 2', flush=True)\n")
    rc, tail, why = runner.lines(python(code), on_line=seen.append, timeout=20, idle=10)
    assert (rc, why) == (0, None)
    assert seen == ["out 1", "err 1", "out 2"] and tail == "out 1\nerr 1\nout 2"


def test_the_idle_limit_stops_a_command_that_goes_quiet(tmp_path):
    runner, seen = admin_ops.Runner(str(tmp_path)), []
    # It says something, then nothing for 3s - long past the idle limit, well
    # inside the total. sh's sleep, a process of its own, holds the output open
    # too: the whole group is killed, or the read would wait for it.
    start = time.monotonic()
    rc, tail, why = runner.lines(["sh", "-c", "echo starting; sleep 3; echo done"], on_line=seen.append,
                                 timeout=20, idle=0.5)
    took = time.monotonic() - start
    assert (rc, why) == (124, "idle")
    assert seen == ["starting"] and tail == "starting"
    assert took < 2.5


def test_the_total_limit_stops_a_command_that_keeps_talking(tmp_path):
    runner = admin_ops.Runner(str(tmp_path))
    code = "import time\nwhile True:\n    print('still going', flush=True)\n    time.sleep(0.05)\n"
    start = time.monotonic()
    rc, tail, why = runner.lines(python(code), timeout=1, idle=0.5)
    assert (rc, why) == (124, "timeout")
    assert tail.splitlines()[-1] == "still going"
    assert time.monotonic() - start < 4


def test_a_command_that_cant_start_is_127_and_a_command_is_a_list(tmp_path):
    runner = admin_ops.Runner(str(tmp_path))
    rc, tail, why = runner.lines(["no-such-command-anywhere"], timeout=5, idle=5)
    assert rc == 127 and tail and why is None
    for bad in ("echo hi", ("echo", "hi"), ["echo", 1]):
        with pytest.raises(TypeError):
            runner.lines(bad)
    rc, tail, _why = runner.lines(["echo", "hi; echo there"], timeout=5, idle=5)
    assert (rc, tail) == (0, "hi; echo there")  # never a shell: the argument as it is


def test_a_failing_on_line_stops_the_command_and_goes_on_up(tmp_path):
    runner = admin_ops.Runner(str(tmp_path))

    def boom(line):
        raise RuntimeError("bug")

    start = time.monotonic()
    with pytest.raises(RuntimeError):
        runner.lines(["sh", "-c", "echo one; sleep 10"], on_line=boom, timeout=20, idle=20)
    assert time.monotonic() - start < 5


def test_a_command_that_closes_its_output_but_runs_on_is_stopped_at_the_total(tmp_path):
    runner = admin_ops.Runner(str(tmp_path))
    code = "import os, sys, time\nprint('bye', flush=True)\nos.close(1)\nos.close(2)\ntime.sleep(30)\n"
    start = time.monotonic()
    rc, tail, why = runner.lines(python(code), timeout=1, idle=0.5)
    assert (rc, tail, why) == (124, "bye", "timeout")
    assert time.monotonic() - start < 5


def test_lines_never_uses_a_shell():
    import inspect

    source = inspect.getsource(admin_ops.Runner.lines)
    assert "shell" not in source.replace("never through a shell", "")
    assert "subprocess.Popen(args," in source and "start_new_session=True" in source


# --- 3. the build's progress in the log ----------------------------------------------


def boiled_down(lines, service="tracker", most=admin_ops.BUILD_LOG_MAX):
    said = []
    log = admin_ops.BuildLog(said.append, service, most)
    for line in lines:
        log(line)
    return said, log


def test_the_transcript_boils_down_to_its_steps():
    said, log = boiled_down(TRANSCRIPT.read_text(encoding="utf-8").splitlines())
    assert said == [
        "Build: [internal] load build definition from Dockerfile",
        "Build: [internal] load build definition from Dockerfile done (0.0s)",
        "Build: [internal] load metadata for docker.io/library/python:3.12-slim",
        "Build: [internal] load metadata for docker.io/library/python:3.12-slim done (1.4s)",
        "Build: [internal] load .dockerignore",
        "Build: [internal] load .dockerignore done (0.0s)",
        "Build: [1/10] FROM docker.io/library/python:3.12-slim@sha256:"
        "4c0ac1ec5bb4b6f6ae2bbd2b3e4e1b2f10e8a4ff0c4ba2ce1c4c36e7a05f8a1d",
        "Build: [1/10] done (0.0s)",
        "Build: [internal] load build context",
        "Build: [internal] load build context done (0.1s)",
        "Build: [2/10] WORKDIR /app",
        "Build: [2/10] done (cached)",
        "Build: [3/10] COPY requirements.txt .",
        "Build: [3/10] done (0.0s)",
        "Build: [4/10] RUN pip install --no-cache-dir -r requirements.txt",
        "Build: [4/10] done (10.1s)",
        "Build: [5/10] COPY main.py auth.py auth_cli.py clock.py crypto.py derived.py metrics.py first_login.py "
        "apps.py host.py home_layout.py shelf.py ./",
        "Build: [5/10] done (0.0s)",
        "Build: [6/10] COPY plugins ./plugins",
        "Build: [6/10] done (0.0s)",
        "Build: [7/10] COPY migrations ./migrations",
        "Build: [7/10] done (0.0s)",
        "Build: [8/10] COPY templates ./templates",
        "Build: [8/10] done (0.1s)",
        "Build: [9/10] COPY audio ./audio",
        "Build: [9/10] done (0.1s)",
        "Build: [10/10] RUN mkdir -p /data",
        "Build: [10/10] done (0.3s)",
        "Build: exporting to image",
        "Build: naming to docker.io/library/openfit-tracker",
        "Build: exporting to image done (0.6s)",
        "Build: resolving provenance for metadata file",
        "Build: resolving provenance for metadata file done (0.0s)",
    ]
    for noise in ("Collecting", "Downloading", "Successfully installed", "WARNING", "notice", "transferring",
                  "exporting layers", "writing image", "building with", "Built", "sha256:9d1f"):
        assert not any(noise in line for line in said), noise
    assert log.last == "resolving provenance for metadata file done (0.0s)"


def test_a_step_that_fails_and_a_header_said_again():
    said, _log = boiled_down([
        "#8 [tracker 4/10] RUN pip install --no-cache-dir -r requirements.txt",
        "#9 [tracker internal] load build context",
        "#8 [tracker 4/10] RUN pip install --no-cache-dir -r requirements.txt",  # back to #8: said once
        "#8 3.100 ERROR: No matching distribution found for flask==9",
        '#8 ERROR: process "/bin/sh -c pip install --no-cache-dir -r requirements.txt" did not complete '
        "successfully: exit code: 1",
        "#7 DONE 0.1s",  # a step whose header never came: not one of ours
        "------",
        " > [tracker 4/10] RUN pip install --no-cache-dir -r requirements.txt:",
        "failed to solve: process did not complete successfully: exit code: 1",
    ])
    assert said == [
        "Build: [4/10] RUN pip install --no-cache-dir -r requirements.txt",
        "Build: [internal] load build context",
        'Build: [4/10] failed: process "/bin/sh -c pip install --no-cache-dir -r requirements.txt" did not '
        "complete successfully: exit code: 1",
    ]


def test_the_admins_steps_lose_its_service_name_too():
    said, _log = boiled_down(["#5 [admin 3/6] RUN pip install --no-cache-dir -r requirements.txt", "#5 CACHED",
                              "#9 [admin] exporting to image"], service="admin")
    assert said == ["Build: [3/6] RUN pip install --no-cache-dir -r requirements.txt", "Build: [3/6] done (cached)",
                    "Build: exporting to image"]


def test_at_most_sixty_build_lines_then_one_more_saying_so():
    lines = [f"#{n} [tracker {n}/100] RUN step {n}" for n in range(1, 101)]
    said, log = boiled_down(lines)
    assert len(said) == 61
    assert said[:60] == [f"Build: [{n}/100] RUN step {n}" for n in range(1, 61)]
    assert said[60] == "… (more build output not shown)"
    assert log.last == "[100/100] RUN step 100"  # where it got to, said or not


def test_an_update_logs_the_builds_steps_in_place(admin_env):
    env = admin_env
    pending(env)
    env.fake.script(is_build("openfit"), [(1, line) for line in TRANSCRIPT.read_text(encoding="utf-8").splitlines()])
    op = run_op(env, env.admin.start_update)
    assert op["outcome"] == "ok"
    log = texts(op)
    start = log.index("Building the new version. OpenFit keeps running meanwhile…")
    stop = log.index("Stopping andy's OpenFit…")
    build = log[start + 1:stop]
    assert build[0] == "Build: [internal] load build definition from Dockerfile"
    assert "Build: [internal] load metadata for docker.io/library/python:3.12-slim" in build
    assert "Build: [2/10] done (cached)" in build and "Build: [4/10] done (10.1s)" in build
    assert build[-1] == "Build: resolving provenance for metadata file done (0.0s)"
    assert all(line.startswith("Build: ") for line in build)
    assert env.fake.limits == [(15 * 60, 5 * 60)]  # the build's, the one command run a line at a time


def test_an_update_logs_at_most_sixty_build_lines(admin_env):
    env = admin_env
    pending(env)
    env.fake.script(is_build("openfit"), [(1, f"#{n} [tracker {n}/90] RUN step {n}") for n in range(1, 91)])
    op = run_op(env, env.admin.start_update)
    build = [t for t in texts(op) if t.startswith("Build: ")]
    assert len(build) == 60
    assert texts(op).count("… (more build output not shown)") == 1


def test_the_admins_build_logs_its_steps_too(admin_env):
    env = admin_env
    pulled(env)
    env.fake.script(is_build("openfit-admin"), [(1, "#2 [admin internal] load metadata for docker.io/library/python:3.12-slim"),
                                                (2, "#2 DONE 2.0s")])
    op = run_op(env, env.admin.start_admin_update)
    log = texts(op)
    start = log.index("Building the new Admin. This one keeps running meanwhile…")
    assert log[start + 1:start + 3] == ["Build: [internal] load metadata for docker.io/library/python:3.12-slim",
                                        "Build: [internal] load metadata for docker.io/library/python:3.12-slim "
                                        "done (2.0s)"]
    assert env.fake.limits == [(15 * 60, 5 * 60)]


# --- 4. the build's limits --------------------------------------------------------------


STALLED = [
    (0, "#1 [tracker internal] load build definition from Dockerfile"),
    (1, "#1 DONE 0.0s"),
    (1, "#2 [tracker internal] load metadata for docker.io/library/python:3.12-slim"),
    (400, "#2 DONE 400.0s"),  # never seen: five minutes of nothing first
]
SLOW = [(0, "#8 [tracker 4/10] RUN pip install --no-cache-dir -r requirements.txt")] + [
    (60, f"#8 {60 * i}.000 Collecting package-{i}") for i in range(1, 30)]  # a line a minute, for half an hour


@pytest.mark.parametrize("script, words, why", [(STALLED, IDLE, "idle"), (SLOW, TIMEOUT, "timeout")])
def test_a_build_that_stalls_or_runs_long_fails_and_changes_nothing(admin_env, script, words, why):
    env = admin_env
    old = head(env)
    pending(env)
    env.fake.script(is_build("openfit"), script)
    op = run_op(env, env.admin.start_update)
    assert env.fake.results[env.fake.kinds.index("lines")][0] == 124
    assert op["outcome"] == "failed"
    assert op["summary"] == f"{words} The build failed, so nothing was changed. OpenFit kept running."
    said = next(t for t in texts(op) if t.startswith(words))
    assert said.split("\n", 1)[1]  # its tail, the detail
    # A normal build failure: the code back, nobody stopped, nothing backed up.
    assert head(env) == old
    commands = env.fake.commands()
    build = next(i for i, c in enumerate(commands) if "build" in c)
    assert commands[build + 1:] == [["git", "reset", "--hard", old]]
    assert not list(env.backups.iterdir())
    assert env.admin.undo_candidate() is None


@pytest.mark.parametrize("script, words", [(STALLED, IDLE), (SLOW, TIMEOUT)])
def test_the_admins_build_has_the_same_limits(admin_env, script, words):
    env = admin_env
    pulled(env)
    env.fake.script(is_build("openfit-admin"), [(d, line.replace("[tracker", "[admin")) for d, line in script])
    op = run_op(env, env.admin.start_admin_update)
    assert op["outcome"] == "failed"
    assert op["summary"] == f"{words} The build failed, so nothing was changed. The Admin kept running."
    assert not any(c[:3] == ["docker", "run", "-d"] for c in env.fake.commands())  # no helper started
    assert env.fake.helper is False


def test_the_limits_are_fifteen_minutes_and_five_quiet():
    assert (admin_ops.BUILD_TIMEOUT, admin_ops.BUILD_IDLE) == (15 * 60, 5 * 60)
    assert admin_ops.Runner("/").timeout == 1800  # every other command's, as before


# --- 5. how busy the disk is -----------------------------------------------------------


def test_iowait_is_the_share_between_two_reads(admin_env):
    env = admin_env
    fed(env, [cpu_text(idle=700, iowait=100, user=100, system=100),
              cpu_text(idle=760, iowait=120, user=110, system=110)])
    env.admin.sample_disk()
    assert env.admin.iowait_pct() is None  # one read: nothing to go between
    assert env.admin.status()["system"]["iowait_pct"] is None
    env.admin.sample_disk()
    # 20 of the 100 ticks between the reads - not 120 of 1100 (11%), the counters since boot.
    assert env.admin.iowait_pct() == 20
    assert env.admin.status()["system"]["iowait_pct"] == 20


def test_iowait_between_counts_every_field():
    before = [10, 0, 10, 70, 10, 0, 0, 0, 0, 0]
    after = [10, 0, 10, 70, 20, 5, 5, 10, 0, 0]  # irq, softirq and steal count towards the total
    assert admin_ops.iowait_between(before, after) == 33  # 10 of 30
    assert admin_ops.iowait_between(before, before) is None
    assert admin_ops.iowait_between(after, before) is None  # the counters gone back
    assert admin_ops.cpu_times(cpu_text(5, 6, 1, 2)) == [1, 0, 2, 5, 6, 0, 0, 0, 0, 0]


@pytest.mark.parametrize("bad", [
    OSError("[Errno 2] No such file or directory: '/proc/stat'"),
    "intr 1 2 3\nctxt 4\n",  # no cpu line
    "cpu  1 2 three 4 5\n",
    "cpu  1 2 3\n",  # too few fields for an iowait
    "",
])
def test_an_unreadable_proc_stat_is_no_figure_no_warning_and_said_once(admin_env, capsys, bad):
    env = admin_env
    fed(env, minutes(70, 70) + [bad, bad] + minutes(10))
    for _ in range(3):
        env.admin.sample_disk()
    assert env.admin.disk_warning()["level"] == "red"
    capsys.readouterr()
    env.admin.sample_disk()
    env.admin.sample_disk()
    system = env.admin.status()["system"]
    assert system["iowait_pct"] is None and system["disk_warning"] is None
    said = [line for line in capsys.readouterr().out.splitlines() if "[admin] disk:" in line]
    assert len(said) == 1 and "can't be read" in said[0]
    # Readable again: two reads before there's a figure, as at the start.
    env.admin.sample_disk()
    assert env.admin.iowait_pct() is None
    env.admin.sample_disk()
    assert env.admin.iowait_pct() == 10


def test_amber_takes_two_busy_minutes_in_a_row(admin_env):
    env = admin_env
    seen = sampled(env, 35, 35)
    assert seen[:2] == [None, None]  # the first read, then one busy minute: not yet
    assert seen[2] == {"level": "amber", "text": busy(35)}
    assert sampled(env, 30, 30)[-1] == {"level": "amber", "text": busy(30)}
    assert sampled(env, 25, 35)[-1] is None
    assert sampled(env, 35, 25)[-1] is None
    assert sampled(env, 29, 30)[-1] is None
    assert sampled(env, 59, 59)[-1] == {"level": "amber", "text": busy(59)}
    assert sampled(env, 35, 35, 35, 10)[-1] is None  # and gone, the first calm minute


def test_red_at_sixty(admin_env):
    env = admin_env
    assert sampled(env, 65)[-1] == {"level": "red", "text": busy(65)}
    assert sampled(env, 10, 60)[-1] == {"level": "red", "text": busy(60)}
    assert sampled(env, 60, 40)[-1] == {"level": "amber", "text": busy(40)}


def test_the_sampler_runs_every_minute_beside_the_schedule(admin_env, monkeypatch):
    env = admin_env
    seen = []
    monkeypatch.setattr(env.admin, "sample_disk", lambda: seen.append("sample"))
    monkeypatch.setattr(env.admin, "tick", lambda: seen.append("tick"))
    monkeypatch.setattr(admin_ops, "TICK_EVERY", 0.05)
    stop = env.admin.start_schedule()
    time.sleep(0.4)
    stop.set()
    assert seen.count("sample") >= 3 and seen.count("tick") >= 3


# --- 6. how full the disk is -----------------------------------------------------------


@pytest.mark.parametrize("used, level", [(0, None), (89, None), (90, "amber"), (96, "amber"), (97, "red"), (100, "red")])
def test_the_full_disk_warning(admin_env, monkeypatch, used, level):
    disk(monkeypatch, used)
    system = admin_env.admin.status()["system"]
    pct = used
    assert system["disk_used_pct"] == pct
    if level is None:
        assert system["disk_warning"] is None
    else:
        free = admin_ops.size_words((100 - used) * 4096)
        assert system["disk_warning"] == {
            "level": level,
            "text": f"The server's disk is {pct}% full ({free} free). Updates need room to build; "
                    "consider freeing space.",
        }


def test_the_used_share_is_dfs_rounded_up(admin_env, monkeypatch):
    monkeypatch.setattr(admin_ops.os, "statvfs", lambda path: SimpleNamespace(
        f_blocks=1000, f_bfree=101, f_bavail=101, f_frsize=4096))  # 89.9% used
    assert admin_env.admin.disk_used_pct() == 90
    monkeypatch.setattr(admin_ops.os, "statvfs", lambda path: SimpleNamespace(
        f_blocks=1000, f_bfree=150, f_bavail=100, f_frsize=4096))  # root's reserve counts as full, as df's
    assert admin_env.admin.disk_used_pct() == 90


def test_the_worse_warning_wins(admin_env, monkeypatch):
    env = admin_env
    disk(monkeypatch, 92)
    full = env.admin.disk_warning()
    assert full["level"] == "amber" and full["text"].startswith("The server's disk is 92% full")
    assert sampled(env, 65)[-1] == {"level": "red", "text": busy(65)}  # busy red beats full amber
    disk(monkeypatch, 98)
    assert sampled(env, 40, 40)[-1]["text"].startswith("The server's disk is 98% full")  # full red beats busy amber
    assert sampled(env, 70)[-1]["text"].startswith("The server's disk is 98% full")  # both red: full first
    disk(monkeypatch, 91)
    assert sampled(env, 40, 40)[-1]["text"].startswith("The server's disk is 91% full")  # both amber: full first


def test_status_never_fails_over_the_disk(admin_env, monkeypatch):
    def broken(path):
        raise OSError("gone")

    monkeypatch.setattr(admin_ops.os, "statvfs", broken)
    system = admin_env.admin.status()["system"]
    assert system["disk_free"] is None and system["disk_used_pct"] is None
    assert system["iowait_pct"] is None and system["disk_warning"] is None


# --- 7. the page ----------------------------------------------------------------------


def with_disk(status, warning, iowait=4, used=91):
    status["system"].update(disk_warning=warning, iowait_pct=iowait, disk_used_pct=used)
    return status


@needs_node
@pytest.mark.parametrize("level", ["amber", "red"])
def test_system_information_leads_with_the_warning_and_shows_the_iowait(logged_in, admin_env, level):
    status = with_disk(portal_status(admin_env), {"level": level, "text": busy(72)}, iowait=72)
    out = run_page(logged_in, status, r"""
      const b = boot({});
      await settle();
      const w = b.byId('diskWarn');
      out.warn = { hidden: w.hidden, cls: w.className, text: w.textContent };
      const dl = b.byId('systemInfo').children;
      out.rows = [];
      for (let i = 0; i < dl.length; i += 2) out.rows.push([dl[i].textContent, dl[i + 1].textContent]);
    """)
    assert out["warn"] == {"hidden": False, "cls": "disk-warn" + (" red" if level == "red" else ""), "text": busy(72)}
    rows = dict(out["rows"])
    assert rows["Disk busy"] == "72% I/O wait"
    assert rows["Disk free"].endswith(" (91% used)")
    labels = [r[0] for r in out["rows"]]
    assert labels.index("Disk free") + 1 == labels.index("Disk busy")


@needs_node
def test_no_warning_and_no_figure_leave_both_out(logged_in, admin_env):
    status = with_disk(portal_status(admin_env), None, iowait=None, used=None)
    out = run_page(logged_in, status, r"""
      const b = boot({});
      await settle();
      out.hidden = b.byId('diskWarn').hidden;
      out.text = b.byId('diskWarn').textContent;
      out.labels = b.byId('systemInfo').children.filter((n, i) => i % 2 === 0).map(n => n.textContent);
      out.free = b.byId('systemInfo').children[7].textContent;
    """)
    assert out["hidden"] is True and out["text"] == ""
    assert "Disk busy" not in out["labels"]
    assert out["labels"][3] == "Disk free" and "used" not in out["free"]


@needs_node
@pytest.mark.parametrize("where", ["user", "admin", "apps", "repo"])
@pytest.mark.parametrize("level", ["amber", "red", None])
def test_the_update_popup_says_heads_up_and_proceed_stays_as_it_was(logged_in, admin_env, where, level):
    status = portal_status(admin_env)
    warning = {"level": level, "text": busy(72)} if level else None
    with_disk(status, warning)
    if where == "apps":
        status["repos"][0]["update"] = "apps"
    if where == "repo":
        status["repos"].append({
            "key": f"repo:{SLUG}", "source": "openfitavi", "name": "OpenFit Aviation", "description": None,
            "official": False, "commit": "a" * 40, "short": "aaaaaaa", "published_at": None, "apps": [],
            "current": None, "error": None, "update": "repo",
            "repo": {"slug": SLUG, "owner": OWNER, "repo": REPO, "status": "connected", "message": "Connected.",
                     "commits": [{"sha": "bbbbbbb", "subject": "FRMS: a fix"}], "error": None}})
    out = run_page(logged_in, status, r"""
      const where = %s;
      const b = boot({});
      await settle();
      const rows = where === 'user' || where === 'admin'
        ? b.byId('versionRows').children.filter(r => r.dataset.kind === where)
        : b.byId('repoRows').children.filter(r => r.dataset.source === (where === 'repo' ? 'openfitavi' : 'openfit'));
      buttons(rows[0], 'Update available')[0].click();
      const body = b.byId('popupBody');
      const all = find(body, n => true);
      const heads = all.filter(n => n.tagName === 'P' && n.textContent.startsWith('Heads up'));
      const go = buttons(body, 'Proceed')[0];
      out.heads = heads.map(n => [n.textContent, n.className]);
      out.before = heads.length ? all.indexOf(heads[0]) < all.indexOf(go) : null;
      out.disabled = go.disabled;
      const before = posts(b).length;
      go.click();
      await settle();
      out.posted = posts(b).slice(before).map(p => p[0]);
    """ % json.dumps(where))
    if level is None:
        assert out["heads"] == []
    else:
        assert out["heads"] == [["Heads up: " + busy(72), "disk-warn" + (" red" if level == "red" else "")]]
        assert out["before"] is True  # above Proceed
    assert out["disabled"] is False  # information, not a block
    assert out["posted"] == [{"user": "/api/update", "admin": "/api/admin/update", "apps": "/api/library/update",
                              "repo": f"/api/repos/{SLUG}/update"}[where]]


@needs_node
def test_a_repos_full_disk_says_disk_full_not_github(logged_in, admin_env):
    status = portal_status(admin_env)
    status["repos"].append({
        "key": f"repo:{SLUG}", "source": "openfitavi", "name": "OpenFit Aviation", "description": None,
        "official": False, "commit": "a" * 40, "short": "aaaaaaa", "published_at": None, "apps": [],
        "current": None, "error": None, "update": None,
        "repo": {"slug": SLUG, "owner": OWNER, "repo": REPO, "status": "connected", "message": "Connected.",
                 "commits": [], "error": {"at": "2026-10-27T12:00:00Z", "text": FULL, "detail": "", "disk_full": True}}})
    out = run_page(logged_in, status, r"""
      const b = boot({});
      await settle();
      const row = b.byId('repoRows').children.find(r => r.dataset.source === 'openfitavi');
      out.pills = find(row, n => n.className.startsWith('pill')).map(n => [n.textContent, n.title]);
    """)
    assert out["pills"] == [["Disk full", FULL]]


# --- 8. a scheduled run starts with the warning ----------------------------------------


def scheduled_run(env, mode="check"):
    env.admin.save_schedule({"day": "sun", "time": "03:00", "timezone": "UTC", "mode": mode})
    assert env.admin.tick() is None  # the baseline: nothing run
    env.clock.now = datetime(2026, 11, 1, 3, 0, tzinfo=timezone.utc).timestamp()  # the next due time
    env.admin.tick()
    [run] = [o for o in env.admin.history() if o["kind"] == "scheduled_run"]
    return run


def test_a_scheduled_run_with_a_warning_starts_its_log_with_it(admin_env, monkeypatch):
    env = admin_env
    disk(monkeypatch, 98)
    run = scheduled_run(env)
    assert run["outcome"] == "ok"  # it runs all the same
    assert texts(run)[0] == ("Heads up: The server's disk is 98% full (8 KB free). Updates need room to build; "
                             "consider freeing space.")
    assert texts(run)[1] == "Checking for updates…"


def test_a_busy_disk_heads_a_scheduled_update_too(admin_env):
    env = admin_env
    sampled(env, 65)
    pending(env)
    run = scheduled_run(env, mode="update")
    assert texts(run)[0] == "Heads up: " + busy(65)
    assert run["outcome"] == "ok" and run["summary"] == "Updated OpenFit."


def test_a_scheduled_run_with_no_warning_starts_as_ever(admin_env):
    run = scheduled_run(admin_env)
    assert texts(run)[0] == "Checking for updates…"
    assert not any(t.startswith("Heads up") for t in texts(run))


# --- 9. the lane --------------------------------------------------------------------------


def test_compose_builds_only_the_primarys_plain_form(admin_env):
    env = admin_env
    primary = env.admin.primary()
    for args in (("build", "tracker"), ("build",), ("--progress", "tty", "build", "tracker"),
                 ("--progress", "plain", "build"), ("--progress", "plain", "build", "tracker", "--no-cache"),
                 ("build", "--progress", "plain", "tracker"), ("--progress", "plain", "up", "-d", "tracker"),
                 ("--progress", "plain", "stop", "tracker"), ("up", "-d", "--build", "tracker")):
        with pytest.raises(ValueError):
            env.admin.compose(primary, *args)
    added = env.admin.added("sam", 8101)
    with pytest.raises(ValueError):  # one build, the primary's: an added user's image is the same one
        env.admin.compose(added, "--progress", "plain", "build", "tracker")
    assert env.fake.calls == []
    rc, _tail, why = env.admin.compose(primary, "--progress", "plain", "build", "tracker")
    assert (rc, why) == (0, None)
    assert env.fake.kinds == ["lines"] and env.fake.limits == [(900, 300)]
    assert env.fake.commands() == [["docker", "compose", "-p", "openfit", "-f",
                                    os.path.join(env.repos.repo, "docker-compose.yml"), "-f",
                                    str(env.data / "primary.override.yml"), "--progress", "plain", "build",
                                    "tracker"]]
    env.admin.compose(primary, "stop", "tracker")
    assert env.fake.kinds == ["lines", "run"]


def test_the_admins_build_is_its_plain_form_a_line_at_a_time(admin_env):
    env = admin_env
    folder = os.path.join(env.repos.repo, "admin")
    rc, _tail, why = admin_ops.admin_compose(env.fake, env.repos.repo, "--progress", "plain", "build", "admin")
    assert (rc, why) == (0, None)
    assert env.fake.commands() == [["docker", "compose", "-p", "openfit-admin", "-f",
                                    os.path.join(folder, "docker-compose.yml"), "--progress", "plain", "build", "admin"]]
    assert env.fake.kinds == ["lines"] and env.fake.limits == [(900, 300)] and env.fake.calls[0].cwd == folder
    admin_ops.admin_compose(env.fake, env.repos.repo, "stop", "admin")
    assert env.fake.kinds == ["lines", "run"]
