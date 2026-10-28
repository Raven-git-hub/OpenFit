"""The Admin Portal (S1): the Admin's page in OpenFit's style, and the
reads behind its panels.

1. OpenFit's look: the page's theme tokens are OpenFit's own, value for
   value, in all three blocks (light, the OS's dark, the chip's dark) -
   when OpenFit's colours change, this file says to recopy them.
2. System information: of `docker info`, only the server's name, Docker
   version, CPUs and memory are kept, asked at most once a minute.
3. Versions: the users' (HEAD) and the Admin's (the newest commit touching
   admin/, or older), when each was last updated, and what an update
   brought, from its recorded commits.
4. The app library's index: library/repo.yaml's name and blurb, and each
   app's name and description - and the user system reading it still.
5. Repos, activity and backups, as status() gives them.
6. The page itself: its script, run under node over a stand-in DOM.

As in the other Admin tests, git runs for real against temp repos and
docker is conftest's FakeRunner.
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import DOCKER_INFO, seed_users
from test_admin_safety import assert_in_lane

import admin_ops
import shelf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADMIN_PAGE = os.path.join(ROOT, "admin", "templates", "index.html")
OPENFIT_PAGE = os.path.join(ROOT, "app", "templates", "index.html")
H = {"X-OpenFit-Admin": "1"}
PASSWORD = "a long password!"
INFO = ["docker", "info", "--format", "{{json .}}"]


@pytest.fixture
def logged_in(admin_client, admin_env):
    admin_env.auth.setup(admin_env.auth.setup_code, PASSWORD)
    assert admin_client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
    return admin_client


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def land(env, files, message):
    """A commit that reaches the server's copy without the Admin: pushed,
    then pulled by hand. Its full sha."""
    env.repos.push(files, message)
    git(env.repos.repo, "pull", "-q", "--ff-only")
    return env.repos.head()


def run_op(env, start):
    start()
    env.admin.wait(10)
    return env.admin.current_op()


def update(env):
    return run_op(env, env.admin.start_update)


def index(env):
    return json.loads((env.library / "index.json").read_text())


def infos(env):
    return [c for c in env.fake.commands() if c[:2] == ["docker", "info"]]


# --- 1. OpenFit's look: the tokens ----------------------------------------------------

BLOCKS = {
    "light": r"(?m)^:root\{(.*?)\}",
    "dark, the OS's": r'@media \(prefers-color-scheme:dark\)\{\s*:root:not\(\[data-theme="light"\]\)\{(.*?)\}',
    "dark, the chip's": r'(?m)^:root\[data-theme="dark"\]\{(.*?)\}',
}


def token_blocks(text):
    """{block: {property: value}} of a page's three theme blocks: each
    custom property (and color-scheme) as written, whitespace collapsed."""
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    blocks = {}
    for name, pattern in BLOCKS.items():
        found = re.search(pattern, text, re.S)
        assert found, f"no {name} token block"
        decls = {}
        for decl in found.group(1).split(";"):
            if ":" in decl:
                key, value = decl.split(":", 1)
                decls[key.strip()] = " ".join(value.split())
        blocks[name] = decls
    return blocks


def drift(admin_text, openfit_text):
    """Every way the Admin's tokens differ from OpenFit's: for each token the
    Admin defines anywhere, its value in each block must be OpenFit's in that
    block - absent from a block where OpenFit has none, there where it has
    one. [(block, token, the Admin's, OpenFit's)]."""
    admin, openfit = token_blocks(admin_text), token_blocks(openfit_text)
    used = set().union(*admin.values())
    return [(block, token, admin[block].get(token), openfit[block].get(token))
            for block in BLOCKS for token in sorted(used) if admin[block].get(token) != openfit[block].get(token)]


def test_every_admin_token_is_openfits_in_all_three_blocks():
    found = drift(read(ADMIN_PAGE), read(OPENFIT_PAGE))
    assert found == [], ("The Admin's theme tokens have drifted from OpenFit's: recopy them from "
                         f"app/templates/index.html's token blocks into admin/templates/index.html. {found}")


def test_the_drift_test_catches_a_changed_or_missing_token():
    # The mutation, kept as a test: one Admin token changed, or a dark
    # override dropped, and the drift is named.
    admin, openfit = read(ADMIN_PAGE), read(OPENFIT_PAGE)
    changed = admin.replace("  --accent:#0e9c8b;\n", "  --accent:#0e9c8c;\n", 1)
    assert changed != admin
    assert drift(changed, openfit) == [("light", "--accent", "#0e9c8c", "#0e9c8b")]
    head, chip = admin.split(':root[data-theme="dark"]{', 1)
    dropped = head + ':root[data-theme="dark"]{' + chip.replace("  --surface-2:#1d262b;\n", "", 1)
    assert drift(dropped, openfit) == [("dark, the chip's", "--surface-2", None, "#1d262b")]


def test_the_admin_defines_exactly_the_tokens_it_uses():
    text = read(ADMIN_PAGE)
    defined = {t for t in token_blocks(text)["light"] if t.startswith("--")}
    # A scroll box's row height and row count are its own, set where it's styled.
    local = set(re.findall(r"(--[a-z0-9-]+)\s*:", text)) - defined
    assert local == {"--row", "--rows"}
    used = set(re.findall(r"var\((--[a-z0-9-]+)", text))
    assert used - local == defined
    # The font stacks are OpenFit's: the design's fonts first, nothing fetched.
    for font in ("--font-display", "--font-body", "--font-mono"):
        assert token_blocks(text)["light"][font] == token_blocks(read(OPENFIT_PAGE))["light"][font]


def test_the_components_are_openfits_rules():
    # The shared components' CSS, rule for rule as OpenFit's page has it.
    admin, openfit = read(ADMIN_PAGE), read(OPENFIT_PAGE)
    for selector in (".brand-mark{", ".brand-name{", ".datepill{", ".bar-btn{", ".tabstrip{", ".tab{",
                     ".tab.active{", ".btn{", ".btn.primary{", ".btn.sm{", ".card{", ".card-title{", ".pill{",
                     ".pill.ok{", ".pill.primary{", ".drow{", ".dpanel{", ".modal{", ".modal-bg{", ".info{",
                     ".danger-panel{"):
        rule = re.search(re.escape(selector) + r"[^}]*\}", openfit).group(0)
        assert rule in admin, selector


def test_the_page_makes_no_external_request(logged_in):
    html = logged_in.get("/").get_data(as_text=True)
    for banned in ("http:", "https:", "<link", "@import", "url(", " src=", "{{", "{%"):
        assert banned not in html, banned


# --- 2. System information ----------------------------------------------------------------


def test_docker_info_keeps_only_four_fields(admin_env):
    s = admin_env.admin.status()
    assert s["system"]["server"] == {"Name": "nas", "ServerVersion": "27.5.1", "NCPU": 8, "MemTotal": 33402789888}
    # Nothing else it said reaches the page: not the host's containers, their
    # names, its images, its id or its network.
    text = json.dumps(s)
    for leak in ("Containers", "openfit-sam-tracker-1", "6c1f2a7e-host-id", "overlay2", "192.168.1.10", "Debian",
                 "Images", "Swarm", "owner=andy"):
        assert leak not in text, leak
    assert infos(admin_env) == [INFO]


@pytest.mark.parametrize("raw, kept", [
    ({"Name": 7, "ServerVersion": "27.5.1", "NCPU": 8, "MemTotal": 1},
     {"Name": None, "ServerVersion": "27.5.1", "NCPU": 8, "MemTotal": 1}),
    ({"Name": "nas\nevil", "ServerVersion": "x" * 65, "NCPU": True, "MemTotal": -1},
     {"Name": None, "ServerVersion": None, "NCPU": None, "MemTotal": None}),
    ({"Name": "n" * 255, "NCPU": 1.5}, {"Name": "n" * 255, "ServerVersion": None, "NCPU": None, "MemTotal": None}),
    ({}, {"Name": None, "ServerVersion": None, "NCPU": None, "MemTotal": None}),
    ([DOCKER_INFO], None),
    ("nas", None),
])
def test_what_isnt_a_plain_field_is_none(raw, kept):
    assert admin_ops.server_fields(raw) == kept


def test_docker_info_runs_at_most_once_a_minute(admin_env):
    env = admin_env
    env.admin.status()
    env.admin.status()
    assert infos(env) == [INFO]
    env.clock.advance(59)
    env.admin.status()
    assert len(infos(env)) == 1
    env.clock.advance(1)
    assert env.admin.status()["system"]["server"]["Name"] == "nas"
    assert infos(env) == [INFO, INFO]


def test_docker_info_that_fails_is_none_and_waits_its_minute_too(admin_env):
    env = admin_env
    env.fake.fail(lambda a: a[:2] == ["docker", "info"], err="Cannot connect to the Docker daemon")
    assert env.admin.status()["system"]["server"] is None
    env.admin.status()
    assert len(infos(env)) == 1
    env.fake.rules.clear()
    env.fake.rules.append((lambda a: a[:2] == ["docker", "info"], (0, "not json", "")))
    env.clock.advance(60)
    assert env.admin.status()["system"]["server"] is None
    assert len(infos(env)) == 2


def test_the_rest_of_the_system_panel(admin_env, monkeypatch):
    env = admin_env
    seed_users(env, ("sam", 8101, "running"), ("kim", 8102, "exited"), ("lee", 8103, None))
    for name, size in (("20261026T000000Z-abcdef0-andy.tar.gz", 100), ("20261026T000000Z-abcdef0-sam.tar.gz", 250)):
        (env.backups / name).write_bytes(b"x" * size)
    monkeypatch.setattr(admin_ops.os, "statvfs", lambda path: SimpleNamespace(f_bavail=10, f_frsize=4096))
    system = env.admin.status()["system"]
    assert system["disk_free"] == 40960
    assert system["backups_bytes"] == 350
    assert system["users"] == {"running": 2, "stopped": 1, "missing": 1}
    assert system["admin_up_since"] == "2026-10-27T12:00:00Z"  # when this Admin was made, not now
    env.clock.advance(3600)
    assert env.admin.status()["system"]["admin_up_since"] == "2026-10-27T12:00:00Z"

    def broken(path):
        raise OSError("gone")

    monkeypatch.setattr(admin_ops.os, "statvfs", broken)
    assert env.admin.status()["system"]["disk_free"] is None


@pytest.mark.parametrize("args", [
    ["docker", "info"],
    ["docker", "info", "--format", "{{json .Containers}}"],
    ["docker", "info", "--format", "{{json .}}", "--debug"],
    ["docker", "system", "info"],
])
def test_the_lane_allows_docker_info_only_with_its_format(admin_env, args):
    env = admin_env
    env.admin.status()
    assert_in_lane(env, {})  # the status's own docker info is in the lane
    env.fake.run(args)
    with pytest.raises((AssertionError, pytest.fail.Exception)):
        assert_in_lane(env, {})


# --- 3. Versions ------------------------------------------------------------------------


def sync_running_admin(env):
    """The running Admin's code made the repo's admin/: as if it was rebuilt."""
    shutil.rmtree(env.admin.self_dir)
    shutil.copytree(os.path.join(env.repos.repo, "admin"), env.admin.self_dir)


def test_the_versions_are_head_and_the_newest_commit_touching_admin(admin_env):
    env = admin_env
    admin_sha = land(env, {"admin/admin_server.py": "print('admin 2')\n"}, "Admin change")
    user_sha = land(env, {"app/main.py": "print('2')\n"}, "OpenFit change")
    sync_running_admin(env)

    v = env.admin.status()["versions"]
    assert (v["user"]["commit"], v["user"]["short"], v["user"]["subject"]) == (user_sha, user_sha[:7], "OpenFit change")
    assert (v["admin"]["commit"], v["admin"]["short"], v["admin"]["subject"]) == (admin_sha, admin_sha[:7], "Admin change")
    assert re.match(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d\Z", v["user"]["date"])
    assert v["admin"]["older"] is False and v["admin"]["update_available"] is False
    assert v["user"]["last_updated"] is None and v["admin"]["last_updated"] is None
    assert v["user"]["last"] is None and v["admin"]["last"] is None


def test_an_admin_running_older_code_is_older_and_offers_its_update(admin_env):
    env = admin_env
    env.repos.merge_pr(51, [("Admin: a fix", {"admin/admin_server.py": "print('admin 2')\n"}),
                            ("OpenFit: a fix", {"app/main.py": "print('2')\n"})])
    env.admin.check()
    assert update(env)["outcome"] == "ok"

    v = env.admin.status()["versions"]["admin"]
    assert v["older"] is True  # the repo's admin/ isn't the code running
    assert v["update_available"] is True and v["can_self_update"] is True
    # What its update would bring: the Admin's commits pulled since it was built.
    assert [c["subject"] for c in v["pending"]["commits"]] == ["Admin: a fix"]
    assert v["pending"]["prs"] == [{"number": 51, "url": admin_ops.PR_URL.format(51)}]


def test_unpulled_admin_commits_are_pending_too(admin_env):
    env = admin_env
    env.repos.push({"admin/admin_server.py": "print('admin 2')\n"}, "Admin fix")
    env.admin.check()
    v = env.admin.status()["versions"]["admin"]
    assert v["older"] is False and v["update_available"] is True and v["can_self_update"] is False
    assert [c["subject"] for c in v["pending"]["commits"]] == ["Admin fix"]


def test_last_updated_is_the_newest_ok_op_of_its_kind(admin_env):
    env = admin_env
    env.repos.push({"app/main.py": "print('2')\n"}, "Two")
    env.admin.check()
    ok = update(env)
    assert ok["outcome"] == "ok"
    env.clock.advance(3600)
    env.repos.push({"app/main.py": "print('3')\n"}, "Three")
    env.admin.check()
    env.fake.fail(lambda a: "build" in a)
    failed = update(env)
    assert failed["outcome"] == "failed"

    v = env.admin.status()["versions"]["user"]
    assert v["last_updated"] == ok["finished_at"]
    assert v["last"]["id"] == failed["id"] and v["last"]["outcome"] == "failed"
    assert v["current"]["id"] == ok["id"]  # what everyone is on came with the one that worked
    assert v["update_available"] is True


def test_the_last_update_lists_the_commits_its_range_brought(admin_env):
    env = admin_env
    old = env.repos.head()
    a, b = env.repos.merge_pr(61, [("Home: tabs", {"app/home.py": "1\n"}), ("Docs: tabs", {"docs/tabs.md": "1\n"})])
    (c,) = env.repos.merge_pr(62, [("Admin: a fix", {"admin/admin_ops.py": "1\n"})])
    env.admin.check()
    op = update(env)
    new = env.repos.head()
    assert (op["old"]["commit"], op["new"]["commit"]) == (old, new)

    last = env.admin.status()["versions"]["user"]["last"]
    # The range's own commits, merges left out, newest first, as git lists them.
    expected = git(env.repos.repo, "log", "--no-merges", "--format=%h%x09%s", f"{old}..{new}").splitlines()
    assert [f"{x['sha']}\t{x['subject']}" for x in last["changes"]["commits"]] == expected
    assert {x["sha"] for x in last["changes"]["commits"]} == {a, b, c}
    assert last["changes"]["prs"] == [{"number": 62, "url": admin_ops.PR_URL.format(62)},
                                      {"number": 61, "url": admin_ops.PR_URL.format(61)}]
    assert (last["old"], last["new"], last["outcome"], last["undone_at"]) == (old[:7], new[:7], "ok", None)

    # A range never changes, so it's read once.
    env.fake.clear()
    env.admin.status()
    assert not any(".." in arg for call in env.fake.commands() for arg in call)


def test_an_undone_update_says_when(admin_env):
    env = admin_env
    env.repos.push({"app/main.py": "print('2')\n"}, "Two")
    env.admin.check()
    op = update(env)
    env.clock.advance(60)
    undo = run_op(env, env.admin.start_undo)
    assert undo["outcome"] == "ok"
    last = env.admin.status()["versions"]["user"]["last"]
    assert last["id"] == op["id"] and last["undone_at"] == undo["finished_at"]


def test_an_admin_update_records_its_range_and_brings_only_admin_commits(admin_env):
    import admin_swap

    env = admin_env
    base = env.repos.head()
    env.repos.merge_pr(71, [("Admin: setup links", {"admin/admin_server.py": "print('admin 2')\n"}),
                            ("OpenFit: setup links", {"app/main.py": "print('2')\n"})])
    env.admin.check()
    assert update(env)["outcome"] == "ok"
    head = env.repos.head()

    env.admin.start_admin_update()
    env.admin.wait(10)
    op = env.admin.op
    assert (op["old"]["commit"], op["new"]["commit"]) == (base, head)  # from the code it was built from, to HEAD
    run = env.fake.commands()[-1]
    assert admin_swap.swap(env.fake, env.repos.repo, str(env.data), run[-2], run[-1], env.clock, env.clock.sleep) == "ok"
    env.fake.helper = False

    v = env.admin.status()["versions"]["admin"]
    assert v["last"]["outcome"] == "ok" and v["last_updated"] == v["last"]["finished_at"]
    assert [c["subject"] for c in v["last"]["changes"]["commits"]] == ["Admin: setup links"]
    assert v["last"]["changes"]["prs"] == [{"number": 71, "url": admin_ops.PR_URL.format(71)}]


def test_an_admin_update_an_older_admin_started_is_given_its_range_when_it_finishes(admin_env):
    # The rollout's own Admin update: started by an Admin from before S1,
    # which noted no range, and finished by the new one from the helper's result.
    env = admin_env
    base = env.repos.head()
    env.repos.push({"admin/admin_server.py": "print('admin 2')\n"}, "Admin: the portal")
    env.admin.check()
    assert update(env)["outcome"] == "ok"
    op = env.admin._begin("admin_update")
    assert op["old"] is None and op["new"] is None
    (env.data / "self_update_result.json").write_text(json.dumps(
        {"op_id": op["id"], "stamp": "20261027T120000Z", "outcome": "ok", "lines": []}))

    again = env.make()

    done = next(o for o in again.history() if o["id"] == op["id"])
    assert done["outcome"] == "ok"
    assert (done["old"]["commit"], done["new"]["commit"]) == (base, env.repos.head())


@pytest.mark.parametrize("history, base", [
    ([], None),
    ([("update", "ok", "a", "b")], "a"),
    ([("update", "ok", "a", "b"), ("update", "ok", "b", "c")], "a"),
    ([("update", "failed", "a", "b"), ("update", "ok", "b", "c")], "b"),
    ([("update", "ok", "a", "b"), ("admin_update", "ok", None, "b")], "b"),
    ([("update", "ok", "a", "b"), ("admin_update", "ok", None, "b"), ("update", "ok", "b", "c")], "b"),
    ([("update", "ok", "a", "b"), ("admin_update", "rolled_back", None, "b")], "a"),
])
def test_the_code_the_admin_was_built_from(admin_env, history, base):
    sha = {k: k * 40 for k in "abc"}
    ops = [{"id": f"op{i}", "kind": kind, "outcome": outcome,
            "old": {"commit": sha[old]} if old else None, "new": {"commit": sha[new]}}
           for i, (kind, outcome, old, new) in enumerate(history)]
    (admin_env.data / "history.json").write_text(json.dumps(ops))
    assert admin_env.admin._admin_base() == (sha[base] if base else None)


# --- 4. repo.yaml, and the index ---------------------------------------------------------------


REPO_YAML = "name: OpenFit\ndescription: Official applications released by OpenFit.\n"


def test_repo_yaml_and_the_apps_names_reach_the_index(admin_env, capsys):
    env = admin_env
    commit = land(env, {
        "library/repo.yaml": REPO_YAML,
        "library/frms/app.yaml": "id: frms\nname: FRMS\ndescription: >-\n  Fatigue risk management\n  for crew.\n",
    }, "Names")
    capsys.readouterr()
    env.admin.publish_official()

    entry = index(env)["sources"]["openfit"]
    assert list(entry) == ["commit", "name", "description", "apps"]
    assert (entry["commit"], entry["name"], entry["description"]) == (
        commit, "OpenFit", "Official applications released by OpenFit.")
    shelf_dir = env.library / "openfit"
    assert entry["apps"] == {
        "frms": {"sha256": admin_ops.fingerprint(str(shelf_dir / "frms")), "name": "FRMS",
                 "description": "Fatigue risk management for crew."},
        "steps": {"sha256": admin_ops.fingerprint(str(shelf_dir / "steps")), "name": None, "description": None},
    }
    # repo.yaml is the source's, not an app: not on the shelf, and not "left out".
    assert sorted(os.listdir(shelf_dir)) == ["frms", "steps"]
    out = capsys.readouterr().out
    assert "repo.yaml" not in out


@pytest.mark.parametrize("text, why", [
    (None, "is missing"),
    ("name: [OpenFit\n", "doesn't parse"),
    ("- OpenFit\n", "isn't a mapping"),
    ("description: no name\n", "has no name of 1 to 40 characters"),
    ("name: '   '\n", "has no name of 1 to 40 characters"),
    ("name: " + "x" * 41 + "\n", "has no name of 1 to 40 characters"),
    ("name: 7\n", "has no name of 1 to 40 characters"),
    ("name: OpenFit\ndescription: " + "y" * 301 + "\n", "has a description that isn't text of at most 300 characters"),
    ("name: OpenFit\ndescription: [a, b]\n", "has a description that isn't text of at most 300 characters"),
    (b"name: \xff\n", "doesn't parse"),
    ("link", "is a link"),
    ("folder", "isn't a regular file"),
])
def test_a_missing_or_invalid_repo_yaml_falls_back_and_is_logged(admin_env, capsys, tmp_path, text, why):
    env = admin_env
    src = tmp_path / "repo-library"
    (src / "steps").mkdir(parents=True)
    (src / "steps" / "app.yaml").write_text("id: steps\n")
    if text == "link":
        (tmp_path / "elsewhere.yaml").write_text(REPO_YAML)
        os.symlink(tmp_path / "elsewhere.yaml", src / "repo.yaml")
    elif text == "folder":
        (src / "repo.yaml").mkdir()
    elif isinstance(text, bytes):
        (src / "repo.yaml").write_bytes(text)
    elif text is not None:
        (src / "repo.yaml").write_text(text)
    lines = []
    admin_ops.publish_set(str(src), str(env.library), "openfit", "a" * 40, "2026-10-27T12:00:00Z", lines.append)
    entry = index(env)["sources"]["openfit"]
    assert (entry["name"], entry["description"]) == ("openfit", None)
    assert lines == [f"library/repo.yaml {why}, so the app library is named 'openfit', with no description."]
    assert list(entry["apps"]) == ["steps"]  # published all the same


def test_a_repo_yaml_at_its_limits_is_kept(admin_env, tmp_path):
    src = tmp_path / "repo-library"
    src.mkdir()
    (src / "repo.yaml").write_text("name: '  " + "N" * 40 + "  '\ndescription: '" + "d" * 300 + "'\nextra: ignored\n")
    admin_ops.publish_set(str(src), str(admin_env.library), "openfit", "a" * 40, "x", lambda m: None)
    entry = index(admin_env)["sources"]["openfit"]
    assert (entry["name"], entry["description"]) == ("N" * 40, "d" * 300)


def test_an_app_yaml_that_doesnt_parse_is_published_with_nulls(admin_env):
    env = admin_env
    land(env, {
        "library/broken/app.yaml": "id: broken\nname: [unclosed\n",
        "library/odd/app.yaml": "id: odd\nname: 42\ndescription: '" + "z" * 301 + "'\n",
        "library/blank/app.yaml": "id: blank\nname: '  '\ndescription: '  '\n",
        "library/listy/app.yaml": "- a list\n",
    }, "Odd apps")
    env.admin.publish_official()
    apps = index(env)["sources"]["openfit"]["apps"]
    assert sorted(apps) == ["blank", "broken", "listy", "odd", "steps"]
    for app in ("blank", "broken", "listy", "odd"):
        assert (apps[app]["name"], apps[app]["description"]) == (None, None), app
        assert apps[app]["sha256"] == admin_ops.fingerprint(str(env.library / "openfit" / app))
    assert os.path.isfile(env.library / "openfit" / "broken" / "app.yaml")


def test_the_user_systems_shelf_reads_the_richer_index(admin_env, conn, tmp_path):
    env = admin_env
    land(env, {"library/repo.yaml": REPO_YAML,
               "library/walk/app.yaml": "id: walk\nname: Walk\ndescription: Steps, walked.\n"}, "Walk")
    env.admin.publish_official()
    assert index(env)["sources"]["openfit"]["apps"]["walk"]["name"] == "Walk"

    listed = shelf.list_shelf(str(env.library), str(tmp_path / "apps"), conn)
    assert listed["available"] is True
    assert [(a["id"], a["name"], a["description"], a["state"]) for a in listed["apps"]] == [
        ("openfit.steps", "steps", None, "available"),
        ("openfit.walk", "Walk", "Steps, walked.", "available"),
    ]


def test_startup_brings_names_to_an_index_from_before_s1_without_a_new_set(admin_env):
    env = admin_env
    land(env, {"library/repo.yaml": REPO_YAML}, "repo.yaml")
    env.admin.publish_official()
    # The index as an Admin from before S1 wrote it: no names anywhere.
    old = index(env)
    entry = old["sources"]["openfit"]
    old["sources"]["openfit"] = {"commit": entry["commit"],
                                 "apps": {a: {"sha256": e["sha256"]} for a, e in entry["apps"].items()}}
    (env.library / "index.json").write_text(json.dumps(old))
    sets = os.listdir(env.library / ".sets")
    inode = os.stat(env.library / ".sets" / sets[0]).st_ino

    assert env.make().publish_at_startup() is True
    assert index(env)["sources"]["openfit"] == entry
    assert os.listdir(env.library / ".sets") == sets
    assert os.stat(env.library / ".sets" / sets[0]).st_ino == inode  # the set itself isn't rewritten
    assert env.make().publish_at_startup() is False


# --- 5. Repos, activity and backups ---------------------------------------------------------------


def test_the_repos_list_openfit_first_then_by_name(admin_env):
    env = admin_env
    land(env, {"library/repo.yaml": REPO_YAML, "library/frms/app.yaml": "id: frms\nname: FRMS\n"}, "Two apps")
    env.admin.publish_official()
    shelf_index = index(env)
    shelf_index["sources"]["zeta"] = {"commit": "c" * 40, "name": "Aardvark Health", "description": "Theirs.",
                                      "apps": {"walk": {"sha256": "0" * 64, "name": "Walk", "description": None},
                                               "Bad_Id": {"sha256": "0" * 64}, "aim": {"sha256": "0" * 64}}}
    shelf_index["sources"]["acme"] = {"commit": "d" * 40, "apps": {}}  # from before S1: no name
    shelf_index["sources"]["Nope"] = {"commit": "e" * 40, "apps": {}}  # not a source's name
    shelf_index["sources"]["broken"] = {"commit": "short", "apps": {}}  # not as published
    (env.library / "index.json").write_text(json.dumps(shelf_index))

    repos = env.admin.status()["repos"]
    assert [(r["source"], r["name"], r["official"]) for r in repos] == [
        ("openfit", "OpenFit", True), ("zeta", "Aardvark Health", False), ("acme", "acme", False)]
    openfit, zeta, _acme = repos
    assert (openfit["commit"], openfit["short"]) == (env.repos.head(), env.repos.head()[:7])
    assert openfit["description"] == "Official applications released by OpenFit."
    assert openfit["published_at"] == shelf_index["published_at"]
    assert [(a["id"], a["name"]) for a in openfit["apps"]] == [("frms", "FRMS"), ("steps", "steps")]
    assert [(a["id"], a["name"], a["description"]) for a in zeta["apps"]] == [
        ("aim", "aim", None), ("walk", "Walk", None)]
    assert (openfit["current"], openfit["error"], openfit["update"]) == (True, None, None)
    assert (zeta["current"], zeta["update"]) == (None, None)


def test_openfit_is_listed_before_anything_is_published(admin_env):
    (repo,) = admin_env.admin.status()["repos"]
    assert (repo["source"], repo["name"], repo["commit"], repo["apps"], repo["current"]) == (
        "openfit", "openfit", None, [], False)


@pytest.mark.parametrize("files, offered", [
    ({"library/caffeine/app.yaml": "id: caffeine\n"}, "apps"),
    ({"library/caffeine/app.yaml": "id: caffeine\n", "app/main.py": "print('2')\n"}, "openfit"),
    ({"app/main.py": "print('2')\n"}, None),
])
def test_openfits_waiting_app_changes_say_which_update_brings_them(admin_env, files, offered):
    env = admin_env
    env.admin.publish_official()
    env.repos.push(files, "Changes")
    env.admin.check()
    assert env.admin.status()["repos"][0]["update"] == offered


def test_the_activity_is_every_kept_op_newest_first_with_its_log(admin_env):
    env = admin_env
    env.repos.push({"app/main.py": "print('2')\n"}, "Two")
    env.admin.check()
    first = update(env)
    env.clock.advance(60)
    added = run_op(env, lambda: env.admin.start_add_user("sam", 8101, "192.168.1.10:8090"))
    activity = env.admin.status()["activity"]
    assert [o["id"] for o in activity] == [added["id"], first["id"]]
    assert activity[0]["log"] == added["log"] and activity[0]["user"] == {"name": "sam", "port": 8101}
    assert set(activity[1]) == {"id", "kind", "outcome", "summary", "started_at", "finished_at", "log", "user"}


def test_each_backup_names_the_update_it_was_taken_by(admin_env):
    env = admin_env
    seed_users(env, ("sam", 8101, "running"))
    env.repos.push({"app/main.py": "print('2')\n"}, "Two")
    env.admin.check()
    op = update(env)
    (env.backups / "20261001T000000Z-abcdef0-andy.tar.gz").write_bytes(b"old")  # its update long gone
    backups = env.admin.status()["backups"]
    taken = {b["name"]: b["update"] for b in backups}
    assert len(taken) == 3
    for name, upd in taken.items():
        if name.startswith("20261001"):
            assert upd is None
        else:
            assert upd == {"id": op["id"], "old": op["old"]["short"], "new": op["new"]["short"],
                           "started_at": op["started_at"], "outcome": "ok"}


# --- 6. The page ---------------------------------------------------------------------------------


def test_the_header_has_admin_portal_the_date_pill_and_both_chips(logged_in):
    html = logged_in.get("/").get_data(as_text=True)
    header = html.split('<header class="topbar">', 1)[1].split("</header>", 1)[0]
    assert '<div class="brand-name">Open<b>Fit</b></div>' in header
    assert '<div class="portal-name">Admin Portal</div>' in header  # set in caps by its CSS
    assert ".portal-name{" in html and "text-transform:uppercase" in html.split(".portal-name{", 1)[1].split("}", 1)[0]
    assert '<div class="datepill" id="today"></div>' in header
    assert '<button class="bar-btn" id="settingsBtn" type="button" aria-haspopup="dialog">settings</button>' in header
    assert '<button class="bar-btn" id="themeToggle" type="button">theme <span id="themeState">auto</span></button>' in header


def test_the_left_column_has_its_four_panels_in_order(logged_in):
    html = logged_in.get("/").get_data(as_text=True)
    left = html.split('id="leftCol"', 1)[1].split("</aside>", 1)[0]
    cards = re.findall(r'<section class="card" id="(\w+)">\s*<div class="card-title"><h3>([^<]+)</h3>', left)
    assert cards == [("systemCard", "System information"), ("versionsCard", "Versions"),
                     ("reposCard", "Repos"), ("activityCard", "Activity log")]
    right = html.split('id="rightCol"', 1)[1]
    assert re.findall(r'<button class="tab[^"]*" id="tab-(\w+)"', right) == ["users", "repo"]


def test_the_page_stores_only_its_theme_and_tab(logged_in):
    html = logged_in.get("/").get_data(as_text=True)
    assert 'const STORE = { theme: "openfit-admin-theme", tab: "openfit-admin-tab" };' in html
    assert re.findall(r"localStorage\.\w+\((\w+)", html) == ["key", "key", "key"]
    assert "sessionStorage" not in html


def portal_status(env):
    """A server with some history, as status() gives it: users andy (the
    primary), sam, bob (no container) and kim (removed, data kept); an
    update that worked, with its backups; the Admin's own update pulled,
    not run; OpenFit and app changes waiting; and two more sources on the
    shelf."""
    land(env, {"library/repo.yaml": REPO_YAML,
               "library/frms/app.yaml": "id: frms\nname: FRMS\ndescription: Fatigue risk management.\n"}, "Library")
    sync_running_admin(env)
    seed_users(env, ("sam", 8101, "running"), ("kim", 8102, "exited"), ("bob", 8103, None))
    env.admin.publish_official()
    env.repos.merge_pr(81, [("Admin: a fix", {"admin/admin_server.py": "print('admin 2')\n"}),
                            ("OpenFit: a fix", {"app/main.py": "print('2')\n"})])
    env.admin.check()
    env.clock.advance(60)
    assert update(env)["outcome"] == "ok"
    env.clock.advance(3600)
    env.admin.remove_user("kim")
    env.repos.push({"app/main.py": "print('3')\n", "library/frms/app.py": "x = 1\n"}, "Both")
    env.admin.check()
    shelf_index = index(env)
    shelf_index["sources"]["zeta"] = {"commit": "c" * 40, "name": "Aardvark Health", "description": None,
                                      "apps": {"walk": {"sha256": "0" * 64, "name": "Walk", "description": "Walks."}}}
    (env.library / "index.json").write_text(json.dumps(shelf_index))
    return env.admin.status()


# The page's script, run under node over a stand-in DOM: elements made by
# the script, or - by id - stood in for the page's own markup, each with the
# class, hidden and attributes its tag has in the template. fetch answers
# from the status given, and records every request: 202 for an operation
# that runs in the background, 200 for the rest, as the server answers.
HARNESS = r"""
const vm = require('vm');
const HTML = __HTML__, STATUS = __STATUS__;
const SCRIPT = HTML.split('<script>\n')[1].split('</script>')[0];

class N {
  constructor(tag) {
    this.tagName = tag.toUpperCase(); this.children = []; this.parentNode = null; this.attrs = {};
    this.dataset = {}; this.style = {}; this.listeners = {}; this.className = ''; this._text = '';
    this._html = ''; this.hidden = false; this.disabled = false; this.value = ''; this.id = '';
  }
  get classList() {
    const self = this, list = () => self.className.split(/\s+/).filter(Boolean);
    return {
      add: (...c) => { self.className = [...new Set([...list(), ...c])].join(' '); },
      remove: (...c) => { self.className = list().filter(x => !c.includes(x)).join(' '); },
      contains: c => list().includes(c),
      toggle: (c, on) => {
        const want = on === undefined ? !list().includes(c) : !!on;
        self.className = (want ? [...new Set([...list(), c])] : list().filter(x => x !== c)).join(' ');
        return want;
      },
    };
  }
  _own(x) {
    if (!(x instanceof N)) { const t = new N('#text'); t._text = String(x); x = t; }
    if (x.parentNode) x.remove();
    x.parentNode = this;
    return x;
  }
  append(...xs) { for (const x of xs) this.children.push(this._own(x)); }
  appendChild(x) { this.append(x); return x; }
  prepend(...xs) { this.children.unshift(...xs.map(x => this._own(x))); }
  replaceChildren(...xs) { for (const c of this.children) c.parentNode = null; this.children = []; this._text = ''; this.append(...xs); }
  remove() { if (this.parentNode) { const p = this.parentNode; p.children = p.children.filter(c => c !== this); this.parentNode = null; } }
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  get textContent() { return this._text + this.children.map(c => c.textContent).join(''); }
  set textContent(t) { this.replaceChildren(); this._text = String(t); }
  get innerHTML() { return this._html; }
  set innerHTML(h) { this.replaceChildren(); this._html = String(h); }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  removeAttribute(k) { delete this.attrs[k]; }
  addEventListener(t, f) { (this.listeners[t] = this.listeners[t] || []).push(f); }
  fire(t, extra) { const ev = Object.assign({ target: this, preventDefault() {} }, extra || {}); for (const f of this.listeners[t] || []) f(ev); }
  click() { if (!this.disabled) this.fire('click'); }
  focus() {} select() {}
  getElementsByTagName(tag) { return find(this, n => n.tagName === tag.toUpperCase()); }
}

function find(root, pred) {
  const out = [];
  const walk = n => { for (const c of n.children) { if (pred(c)) out.push(c); walk(c); } };
  walk(root);
  return out;
}
const buttons = (root, label) => find(root, n => n.tagName === 'BUTTON' && n.textContent === label);
const html = n => n._html + n.children.map(html).join('');

function statics() {
  const out = {};
  for (const m of HTML.matchAll(/<([a-z]+)\b([^>]*)>/g)) {
    const attrs = {};
    for (const a of m[2].matchAll(/([a-zA-Z][-a-zA-Z0-9]*)(?:="([^"]*)")?/g)) attrs[a[1]] = a[2] === undefined ? '' : a[2];
    if (attrs.id) out[attrs.id] = { tag: m[1], attrs };
  }
  return out;
}
const STATICS = statics();

function boot(stored, status) {
  const ids = {}, fetches = [], store = new Map(Object.entries(stored || {}));
  const byId = id => {
    if (!ids[id]) {
      const st = STATICS[id], n = new N(st ? st.tag : 'div');
      n.id = id;
      for (const [k, v] of Object.entries(st ? st.attrs : {})) {
        if (k === 'class') n.className = v;
        else if (k === 'hidden') n.hidden = true;
        else if (k.startsWith('data-')) n.dataset[k.slice(5)] = v;
        else if (k !== 'id') n.attrs[k] = v;
      }
      ids[id] = n;
    }
    return ids[id];
  };
  const page = { status: status || STATUS };
  const sandbox = {
    document: {
      getElementById: byId, createElement: t => new N(t), documentElement: new N('html'),
      body: { dataset: { state: 'app' } }, addEventListener() {}, execCommand: () => true,
    },
    localStorage: { getItem: k => store.has(k) ? store.get(k) : null, setItem: (k, v) => store.set(k, String(v)), removeItem: k => store.delete(k) },
    location: { protocol: 'http:', hostname: '192.168.1.10', reload() {} },
    navigator: {}, window: { isSecureContext: false },
    fetch: async (url, opts) => {
      opts = opts || {};
      fetches.push({ url, method: opts.method || 'GET', header: (opts.headers || {})['X-OpenFit-Admin'] || null, body: opts.body || null });
      const background = ['/api/update', '/api/undo', '/api/admin/update', '/api/library/update', '/api/users'];
      const body = url === '/api/status' ? JSON.parse(JSON.stringify(page.status)) : url === '/api/op' ? { op: null } : { ok: true };
      return { status: background.includes(url) ? 202 : 200, ok: true, json: async () => body };
    },
    setInterval: () => 1, clearInterval() {}, console,
  };
  vm.createContext(sandbox);
  vm.runInContext(SCRIPT, sandbox);
  return { sandbox, byId, fetches, store, page };
}
const settle = async () => { for (let i = 0; i < 30; i++) await new Promise(r => setImmediate(r)); };
const row = (b, box, id) => b.byId(box).children.find(r => r.dataset.id === id);
const open = (b) => !b.byId('popupBg').hidden;
const posts = (b) => b.fetches.filter(f => f.method === 'POST').map(f => [f.url, f.header, f.body]);

(async () => {
  const out = {};
  __SCENARIO__
  console.log(JSON.stringify(out));
})().catch(e => console.log(JSON.stringify({ error: String(e && e.stack || e) })));
"""


def run_page(client, status, scenario):
    html = client.get("/").get_data(as_text=True)
    script = (HARNESS.replace("__HTML__", json.dumps(html)).replace("__STATUS__", json.dumps(status))
              .replace("__SCENARIO__", scenario))
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert "error" not in result, result["error"]
    return result


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")


@needs_node
def test_the_tabs_switch_and_remember_the_choice(logged_in, admin_env):
    out = run_page(logged_in, admin_env.admin.status(), r"""
      const shown = b => ({ users: !b.byId('panel-users').hidden, repo: !b.byId('panel-repo').hidden });
      const b = boot({});
      await settle();
      out.start = { ...shown(b), stored: b.store.get('openfit-admin-tab') || null };
      b.byId('tab-repo').click();
      out.clicked = { ...shown(b), stored: b.store.get('openfit-admin-tab'),
                      active: b.byId('tab-repo').classList.contains('active') && !b.byId('tab-users').classList.contains('active'),
                      current: b.byId('tab-repo').getAttribute('aria-current') };
      const again = boot(Object.fromEntries(b.store));
      await settle();
      out.reloaded = shown(again);
      out.junk = shown(boot({ 'openfit-admin-tab': 'elsewhere' }));
      const t = boot({ 'openfit-admin-theme': 'dark' });
      out.theme = [t.sandbox.document.documentElement.getAttribute('data-theme'), t.byId('themeState').textContent];
      t.byId('themeToggle').click();
      out.theme.push(t.sandbox.document.documentElement.getAttribute('data-theme'), t.store.get('openfit-admin-theme') || null);
    """)
    assert out["start"] == {"users": True, "repo": False, "stored": None}
    assert out["clicked"] == {"users": False, "repo": True, "stored": "repo", "active": True, "current": "page"}
    assert out["reloaded"] == {"users": False, "repo": True}
    assert out["junk"] == {"users": True, "repo": False}
    assert out["theme"] == ["dark", "dark", None, None]  # dark, then on to auto: the OS's again, nothing stored


@needs_node
def test_the_users_list_is_alphabetical_and_the_primary_cant_be_removed(logged_in, admin_env):
    status = portal_status(admin_env)
    out = run_page(logged_in, status, r"""
      const b = boot({});
      await settle();
      const rows = b.byId('userRows').children;
      out.names = rows.map(r => r.dataset.id);
      out.heads = rows.map(r => r.children[0].textContent);
      for (const name of ['andy', 'sam']) {
        const item = row(b, 'userRows', name);
        item.children[0].click();
        const panel = item.children[1];
        out[name] = { text: panel.textContent, remove: buttons(panel, 'Remove').length,
                      buttons: find(panel, n => n.tagName === 'BUTTON').map(n => n.textContent) };
      }
    """)
    assert out["names"] == ["andy", "bob", "sam"]
    assert out["heads"] == ["›andyRunning", "›bobNot found", "›samRunning"]
    assert not any("you" in h for h in out["heads"])
    note = "This is the server's main OpenFit: new versions are built from it, so it can't be removed."
    assert note in out["andy"]["text"] and out["andy"]["remove"] == 0
    assert out["andy"]["buttons"] == ["Stop", "Restart", "Setup link"]
    assert "http://192.168.1.10:8080" in out["andy"]["text"]
    assert note not in out["sam"]["text"] and out["sam"]["buttons"][:4] == ["Stop", "Restart", "Setup link", "Remove"]


@needs_node
def test_user_data_mixes_backups_and_kept_data_and_s2_is_disabled(logged_in, admin_env):
    status = portal_status(admin_env)
    out = run_page(logged_in, status, r"""
      const b = boot({});
      await settle();
      const rows = b.byId('dataRows').children;
      out.rows = rows.map(r => [r.dataset.kind, r.textContent, html(r).includes('warn-ico')]);
      rows.find(r => r.dataset.kind === 'backup').click();
      const body = b.byId('popupBody');
      out.backup = { open: open(b), text: body.textContent,
                     s2: find(body, n => n.dataset.next === 's2').map(n => [n.textContent, n.disabled]) };
      buttons(body, 'Close')[0].click();
      rows.find(r => r.dataset.kind === 'kept').click();
      const go = buttons(body, 'Delete data')[0];
      out.kept = { text: body.textContent, before: go.disabled,
                   s2: find(body, n => n.dataset.next === 's2').map(n => [n.textContent, n.disabled]) };
      const field = find(body, n => n.tagName === 'INPUT')[0];
      field.value = 'ki'; field.fire('input');
      out.kept.partly = go.disabled;
      field.value = 'kim'; field.fire('input');
      out.kept.typed = go.disabled;
      go.click();
      await settle();
      out.kept.posts = posts(b);
      b.byId('versionRows').children.find(r => r.dataset.kind === 'admin').children[0].click();
      out.revert = find(body, n => n.dataset.next === 's2').map(n => [n.textContent, n.disabled]);
      out.revertNote = body.textContent.includes('Coming in the next update.');
    """)
    kinds = [r[0] for r in out["rows"]]
    assert kinds == ["kept", "backup", "backup", "backup"]  # kim removed after the update's backups: newest first
    kept, *backups = out["rows"]
    assert kept[1].startswith("kim") and "Removed" in kept[1] and kept[2] is True  # the red triangle
    assert [r[1].split(" ")[0][:3] for r in backups] == ["and", "kim", "sam"]
    assert all(r[2] is False and r[1].endswith(" B") for r in backups)
    assert out["backup"]["open"] is True
    assert out["backup"]["s2"] == [["Download", True], ["Delete", True]]
    assert "Coming in the next update." in out["backup"]["text"] and "Update OpenFit" in out["backup"]["text"]
    assert out["kept"]["s2"] == [["Download", True]]
    assert "This deletes everything kim's OpenFit stored, for good." in out["kept"]["text"]
    assert (out["kept"]["before"], out["kept"]["partly"], out["kept"]["typed"]) == (True, True, False)
    assert out["kept"]["posts"] == [["/api/removed/kim/delete-data", "1", '{"confirm":"kim"}']]
    assert out["revert"] == [["Revert the Admin", True]] and out["revertNote"] is True


@needs_node
@pytest.mark.parametrize("where, label, route", [
    ("user", "Update available", "/api/update"),
    ("admin", "Update available", "/api/admin/update"),
    ("apps", "Update available", "/api/library/update"),
    ("openfit", "Comes with the OpenFit update", "/api/update"),
])
def test_the_update_popups_proceed_runs_the_existing_route(logged_in, admin_env, where, label, route):
    status = portal_status(admin_env)
    if where == "apps":
        status["repos"][0]["update"] = "apps"
    out = run_page(logged_in, status, r"""
      const where = %s, label = %s;
      const b = boot({});
      await settle();
      const rows = where === 'user' || where === 'admin'
        ? b.byId('versionRows').children.filter(r => r.dataset.kind === where)
        : b.byId('repoRows').children.filter(r => r.dataset.source === 'openfit');
      buttons(rows[0], label)[0].click();
      const body = b.byId('popupBody');
      out.title = b.byId('popupTitle').textContent;
      out.text = body.textContent;
      const before = posts(b).length;
      buttons(body, 'Proceed')[0].click();
      await settle();
      out.posts = posts(b).slice(before);
      out.closed = !open(b);
    """ % (json.dumps(where), json.dumps(label)))
    assert out["posts"] == [[route, "1", "{}"]]
    assert out["closed"] is True
    title, sentence = {
        "/api/update": ("Update OpenFit", "This stops every user's OpenFit for about a minute and backs up everyone's data first."),
        "/api/admin/update": ("Update the Admin", "The Admin restarts on its new version."),
        "/api/library/update": ("Update apps", "Publishes the new apps to every user. Nobody's OpenFit restarts."),
    }[route]
    assert out["title"] == title and sentence in out["text"]
    if route == "/api/admin/update":
        assert "Admin: a fix" in out["text"] and "#81" in out["text"]
    else:
        assert "Both" in out["text"]  # the waiting changes, grouped as the check reports them


@needs_node
def test_the_repo_tab_puts_openfit_first_and_nests_the_apps(logged_in, admin_env):
    status = portal_status(admin_env)
    out = run_page(logged_in, status, r"""
      const b = boot({});
      await settle();
      const rows = b.byId('sourceRows').children;
      out.sources = rows.map(r => [r.dataset.id, r.children[0].textContent]);
      rows[0].children[0].click();
      const panel = rows[0].children[1];
      out.info = find(panel, n => n.className.startsWith('infobox'))[0].textContent;
      out.heading = find(panel, n => n.className === 'subhead').map(n => n.textContent);
      const apps = find(panel, n => n.className === 'nested')[0].children;
      out.apps = apps.map(a => a.dataset.id);
      for (const a of apps) a.children[0].click();
      out.blurbs = apps.map(a => a.children[1].textContent);
      out.nestedOpen = apps.map(a => a.classList.contains('open'));
    """)
    assert [s[0] for s in out["sources"]] == ["openfit", "zeta"]
    assert out["sources"][0][1].startswith("›OpenFit") and out["sources"][1][1].startswith("›Aardvark Health")
    assert out["info"] == "Official applications released by OpenFit."
    assert out["heading"] == ["Applications"]
    assert out["apps"] == ["openfit/frms", "openfit/steps"]
    assert out["blurbs"] == ["Fatigue risk management.", "No description."]
    assert out["nestedOpen"] == [True, True]


@needs_node
def test_the_activity_log_shows_the_running_op_live_then_the_rest(logged_in, admin_env):
    status = portal_status(admin_env)
    running = {"id": "20261027T150000Z-update", "kind": "update", "outcome": "running", "summary": "",
               "started_at": "2026-10-27T15:00:00Z", "finished_at": None, "user": None,
               "log": [{"t": "2026-10-27T15:00:00Z", "text": "Pulling the new code…"}]}
    status["op"] = running
    status["activity"] = [running] + status["activity"]
    out = run_page(logged_in, status, r"""
      const b = boot({});
      await settle();
      out.live = [!b.byId('live').hidden, b.byId('liveHead').textContent, b.byId('liveLog').textContent];
      const rows = b.byId('activityRows').children;
      out.rows = rows.map(r => r.children[0].textContent);
      rows[0].children[0].click();
      out.log = rows[0].children[1].textContent;
      out.busy = buttons(b.byId('versionRows'), 'Update available').map(n => n.disabled);
    """)
    assert out["live"][0] is True and out["live"][1] == "Update OpenFitRunning"
    assert out["live"][2].endswith("Pulling the new code…")
    # The past ops under it - the running one is live, not listed twice.
    assert len(out["rows"]) == len(status["activity"]) - 1 == 1
    assert out["rows"][0].startswith("›Update OpenFit") and out["rows"][0].endswith("Done")
    assert "Noting the version you're on now…" in out["log"]
    assert out["busy"] and all(out["busy"])  # nothing else starts while it runs
