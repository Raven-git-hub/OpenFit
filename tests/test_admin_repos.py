"""Organisation repos (A6): apps from GitHub repos other than OpenFit's own,
each added with a read-only deploy key the Admin makes for it, cloned into
the Admin's storage over SSH, and published to the app shelf under the
source its repo.yaml names - beside the official library, never over it.

1. A repo's address: github.com's, in its https and SSH forms, nothing else.
2. Adding one: the key pair (ssh-keygen, exactly; 0600), the public key
   handed back, the private one nowhere; repos.json, read strictly.
3. Connect: the clone, then the first publish - or why not: a key GitHub
   turned down, an empty repo or one with no repo.yaml, a refused source.
4. The SSH settings every one of a repo's git commands runs with.
5. Publishing a source alone: its own link, sets and index entry.
6. Checks: each repo fetched and its repo.yaml read again; one that fails
   doesn't fail the others; a changed source is refused.
7. Update: merge, check, publish - or back as it was. No container touched.
8. Remove: the source off the shelf, the clone and key gone.
9. The page: Add repository, the key, the rows, Connect, Remove, Show key.

GitHub is conftest's stand-in: bare repos that the test's gitconfig makes
git@github.com:<owner>/<repo>.git, so the Admin's own commands run as
written, with no network and no ssh. Docker is FakeRunner, as ever.
"""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from conftest import Call
from test_admin_safety import org_git_in_lane

import admin_ops

H = {"X-OpenFit-Admin": "1"}
PASSWORD = "a long password!"
ROOT = Path(__file__).parent.parent
FRMS = ROOT / "tests" / "fixtures" / "apps" / "frms"
OWNER, REPO = "Raven-git-hub", "openfit-aviation"
URL = f"https://github.com/{OWNER}/{REPO}"
SLUG = "raven-git-hub__openfit-aviation"
SSH_URL = f"git@github.com:{OWNER}/{REPO}.git"
REPO_YAML = "source: openfitavi\nname: OpenFit Aviation\ndescription: Aviation applications for OpenFit.\n"
KNOWN = {"clone", "fetch", "rev-parse", "log", "merge", "reset", "cat-file"}


@pytest.fixture
def logged_in(admin_client, admin_env):
    admin_env.auth.setup(admin_env.auth.setup_code, PASSWORD)
    assert admin_client.post("/api/login", json={"password": PASSWORD}, headers=H).status_code == 200
    return admin_client


# --- helpers ---------------------------------------------------------------------


def frms_files():
    """FRMS, as the aviation repo holds it: in a top-level frms/ folder."""
    return {f"frms/{p.name}": p.read_text(encoding="utf-8") for p in sorted(FRMS.iterdir()) if p.is_file()}


def aviation(env, files=None, owner=OWNER, repo=REPO):
    """The aviation repo on GitHub: repo.yaml and FRMS, unless `files` says otherwise."""
    env.github.make(owner, repo, {"repo.yaml": REPO_YAML, **frms_files()} if files is None else files)


def expected_ssh(env, slug=SLUG):
    """GIT_SSH_COMMAND, as the brief pins it."""
    return (f"ssh -i {env.data}/repos/{slug}/deploy_key -o IdentitiesOnly=yes "
            "-o UserKnownHostsFile=/app/github_known_hosts -o StrictHostKeyChecking=yes -o BatchMode=yes "
            "-o ConnectTimeout=20")


def add(env, url=URL):
    return env.admin.add_repo(url)


def run_op(env, start):
    start()
    env.admin.wait(10)
    return env.admin.current_op()


def connect(env, slug=SLUG):
    return run_op(env, lambda: env.admin.start_repo_connect(slug))


def update(env, slug=SLUG):
    return run_op(env, lambda: env.admin.start_repo_update(slug))


def added_and_connected(env, files=None, url=URL, owner=OWNER, repo=REPO):
    aviation(env, files, owner, repo)
    add(env, url)
    return connect(env, admin_ops.repo_slug(owner, repo))


def entry(env, slug=SLUG):
    return next(e for e in json.loads((env.data / "repos.json").read_text())["repos"] if e["slug"] == slug)


def index(env):
    return json.loads((env.library / "index.json").read_text())


def row(env, slug=SLUG):
    return next(r for r in env.admin.status()["repos"] if r["key"] == f"repo:{slug}")


def clone_head(env, slug=SLUG):
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=env.data / "repos" / slug / "repo", capture_output=True,
                          text=True, check=True).stdout.strip()


def shelf(env):
    """Everything a reader of the shelf could see: every link, every file's
    bytes and the index's."""
    lib = env.library
    out = {}
    for p in sorted(lib.rglob("*")):
        rel = str(p.relative_to(lib))
        out[rel] = ("link", os.readlink(p)) if p.is_symlink() else ("file", p.read_bytes()) if p.is_file() else "dir"
    return out


def org_calls(env):
    """The git commands run for an organisation repo: those run in the
    Admin's own repos folder."""
    repos = str(env.data / "repos")
    return [c for c in env.fake.calls if c.args[0] == "git" and (c.cwd or "").startswith(repos)]


# --- 1. a repo's address --------------------------------------------------------------


@pytest.mark.parametrize("url, parsed", [
    (URL, (OWNER, REPO)),
    (URL + ".git", (OWNER, REPO)),
    (URL + "/", (OWNER, REPO)),
    (URL + ".git/", (OWNER, REPO)),
    (f"  {URL}\n", (OWNER, REPO)),
    (SSH_URL, (OWNER, REPO)),
    (f"git@github.com:{OWNER}/{REPO}", (OWNER, REPO)),
    ("https://github.com/a/b", ("a", "b")),
    ("https://github.com/0-x-0/my_repo.v2", ("0-x-0", "my_repo.v2")),
    ("https://github.com/" + "o" * 39 + "/" + "r" * 100, ("o" * 39, "r" * 100)),
    ("https://github.com/o/r.git.git", ("o", "r.git")),
])
def test_a_github_address_is_accepted_in_its_forms(url, parsed):
    assert admin_ops.parse_github(url) == parsed


@pytest.mark.parametrize("url", [
    # Another host, or github.com in another shape.
    "http://github.com/o/r", "https://gitlab.com/o/r", "https://www.github.com/o/r", "https://github.com.evil.com/o/r",
    "https://evil.com/github.com/o/r", "https://user@github.com/o/r", "https://github.com:443/o/r",
    "ssh://git@github.com/o/r.git", "git@gitlab.com:o/r.git", "git@github.com/o/r.git", "HTTPS://GITHUB.COM/o/r",
    "https://api.github.com/repos/o/r", "file:///srv/o/r.git", "/srv/o/r.git", "github.com/o/r",
    # Owners GitHub wouldn't have.
    "https://github.com/o_x/r", "https://github.com/" + "o" * 40 + "/r", "https://github.com//r",
    "https://github.com/o x/r", "https://github.com/o.x/r", "git@github.com:-o;rm/r.git",
    # Repo names it wouldn't.
    "https://github.com/o/..", "https://github.com/o/.", "https://github.com/o/../r", "https://github.com/o/" + "r" * 101,
    "https://github.com/o/r x", "https://github.com/o/r%2e", "https://github.com/o/", "https://github.com/o",
    "git@github.com:o/...git", "git@github.com:o/r;id.git",
    # A path, a query or a fragment.
    "https://github.com/o/r/tree/main", "https://github.com/o/r?tab=readme", "https://github.com/o/r#readme",
    "https://github.com/o/r.git?x=1", "https://github.com/o/r/?", "https://github.com/o/r\n/x",
    # Not text.
    "", None, 5, ["https://github.com/o/r"], {"url": URL},
])
def test_anything_else_is_refused(url):
    assert admin_ops.parse_github(url) is None


def test_a_slug_is_the_owner_and_repo_lowercased():
    assert admin_ops.repo_slug(OWNER, REPO) == SLUG
    assert admin_ops.repo_slug("Ab-C", "Open.Fit_2") == "ab-c__open.fit_2"
    assert admin_ops.REPO_SLUG.match(admin_ops.repo_slug("o" * 39, "R" * 100))


def test_a_bad_address_is_a_400_with_nothing_written(logged_in, admin_env):
    resp = logged_in.post("/api/repos", json={"url": "https://gitlab.com/o/r"}, headers=H)
    assert resp.status_code == 400
    assert resp.get_json()["error"] == ("That isn't a GitHub repository's address. Paste one like "
                                        "https://github.com/<owner>/<repo>.")
    assert not (admin_env.data / "repos.json").exists() and not (admin_env.data / "repos").exists()
    assert admin_env.fake.calls == []


# --- 2. adding ---------------------------------------------------------------------------


def test_adding_makes_a_key_pair_for_the_repo_alone(logged_in, admin_env, capsys):
    env = admin_env
    resp = logged_in.post("/api/repos", json={"url": URL + ".git"}, headers=H)
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()

    folder = env.data / "repos" / SLUG
    key = folder / "deploy_key"
    assert env.fake.calls == [Call(
        ["docker", "info", "--format", "{{json .}}"], None, False), Call(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", f"openfit-admin nas {OWNER}/{REPO}", "-f", str(key)],
        None, False)]
    assert stat.S_IMODE(os.stat(key).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
    public = (folder / "deploy_key.pub").read_text().strip()
    assert body["public_key"] == public and public.startswith("ssh-ed25519 ")
    assert public.endswith(f" openfit-admin nas {OWNER}/{REPO}")
    assert body["repo"]["state"] == "pending" and body["repo"]["address"] == URL
    assert body["repo"]["public_key"] == public
    assert json.loads((env.data / "repos.json").read_text()) == {"repos": [{
        "slug": SLUG, "url": SSH_URL, "owner": OWNER, "repo": REPO, "added_at": "2026-10-27T12:00:00Z",
        "state": "pending", "source": None, "commit": None}]}

    # The private key: never in a reply, the status, the history or the log.
    (private,) = env.fake.private_keys
    assert key.read_text() == private
    secret = private.splitlines()[1]
    status = logged_in.get("/api/status", headers=H)
    assert row(env)["repo"]["public_key"] == public
    seen = [resp.get_data(as_text=True), status.get_data(as_text=True), capsys.readouterr().out,
            json.dumps(env.admin.history())]
    seen += [p.read_text() for p in env.data.glob("*.json")]
    for text in seen:
        assert secret not in text and "PRIVATE KEY" not in text


def test_the_same_repo_twice_is_refused_whatever_its_case(admin_env):
    env = admin_env
    add(env)
    env.fake.clear()
    with pytest.raises(admin_ops.Refused) as refused:
        add(env, "https://github.com/raven-git-hub/OPENFIT-AVIATION")
    assert refused.value.status == 409
    assert refused.value.message == "raven-git-hub/OPENFIT-AVIATION is already added here."
    assert env.fake.calls == []
    assert len(json.loads((env.data / "repos.json").read_text())["repos"]) == 1


def test_a_key_that_isnt_made_adds_nothing(logged_in, admin_env):
    env = admin_env
    env.fake.fail(lambda a: a[0] == "ssh-keygen", err="ssh-keygen: something broke")
    resp = logged_in.post("/api/repos", json={"url": URL}, headers=H)
    assert resp.status_code == 502
    assert resp.get_json()["error"] == "Couldn't make a deploy key for the repo."
    assert not (env.data / "repos" / SLUG).exists() and not (env.data / "repos.json").exists()


def test_a_folder_left_by_an_add_that_stopped_is_made_anew(admin_env):
    env = admin_env
    stale = env.data / "repos" / SLUG
    stale.mkdir(parents=True)
    (stale / "deploy_key").write_text("an old key")
    add(env)
    assert (stale / "deploy_key").read_text() == env.fake.private_keys[0]


@pytest.mark.parametrize("change", [
    lambda r: r.update(extra=1),
    lambda r: r["repos"][0].update(extra=1),
    lambda r: r["repos"][0].pop("commit"),
    lambda r: r["repos"][0].update(slug="other__repo"),
    lambda r: r["repos"][0].update(slug="../../etc"),
    lambda r: r["repos"][0].update(owner="o_x"),
    lambda r: r["repos"][0].update(repo=".."),
    lambda r: r["repos"][0].update(url="git@github.com:Evil/repo.git"),
    lambda r: r["repos"][0].update(url="https://github.com/Raven-git-hub/openfit-aviation"),
    lambda r: r["repos"][0].update(added_at="yesterday"),
    lambda r: r["repos"][0].update(state="connecting"),
    lambda r: r["repos"][0].update(state="connected", source="openfit", commit="a" * 40),
    lambda r: r["repos"][0].update(state="connected", source="Open-Fit", commit="a" * 40),
    lambda r: r["repos"][0].update(state="connected", source="avi", commit="abc"),
    lambda r: r["repos"][0].update(state="connected", source="avi", commit=None),
    lambda r: r["repos"][0].update(state="pending", source="avi", commit="a" * 40),
    lambda r: r["repos"].append(dict(r["repos"][0])),
    lambda r: r.update(repos={}),
])
def test_repos_json_is_read_strictly(admin_env, change):
    env = admin_env
    add(env)
    path = env.data / "repos.json"
    reg = json.loads(path.read_text())
    change(reg)
    path.write_text(json.dumps(reg))
    with pytest.raises(ValueError):
        admin_ops.load_repos(str(env.data))
    with pytest.raises(ValueError):
        env.make()  # a bad repos.json stops the Admin starting


def test_two_repos_cant_share_a_source_in_repos_json(admin_env):
    env = admin_env
    reg = {"repos": [
        {"slug": f"o__r{i}", "url": f"git@github.com:o/r{i}.git", "owner": "o", "repo": f"r{i}",
         "added_at": "2026-10-27T12:00:00Z", "state": "connected", "source": "avi", "commit": "a" * 40}
        for i in (1, 2)]}
    (env.data / "repos.json").write_text(json.dumps(reg))
    with pytest.raises(ValueError, match=r"repos\[1\]\.source"):
        admin_ops.load_repos(str(env.data))


# --- 3. connect -----------------------------------------------------------------------------


def test_connect_clones_then_publishes_as_the_repos_source(admin_env):
    env = admin_env
    env.admin.publish_official()
    official = json.dumps(index(env)["sources"]["openfit"])
    aviation(env)
    add(env)
    env.fake.clear()
    op = connect(env)
    # Nothing on the server's checkout, nothing in docker: the clone and the
    # shelf - and the app's change notes, from the clone (B3).
    assert [c.args[:2] for c in env.fake.calls] == [
        ["git", "clone"], ["git", "rev-parse"], ["git", "log"]], env.fake.calls

    assert op["outcome"] == "ok", op["log"]
    assert op["summary"] == f"Connected {OWNER}/{REPO}, and published 1 app to the app library as 'openfitavi'."
    head = clone_head(env)
    assert entry(env) == {"slug": SLUG, "url": SSH_URL, "owner": OWNER, "repo": REPO,
                          "added_at": "2026-10-27T12:00:00Z", "state": "connected", "source": "openfitavi",
                          "commit": head}
    assert os.readlink(env.library / "openfitavi") == f".sets/openfitavi-{head[:12]}"
    shelf_frms = env.library / "openfitavi" / "frms"
    assert sorted(os.listdir(env.library / "openfitavi")) == ["frms"]  # no .git, no repo.yaml
    assert {p.name: p.read_bytes() for p in shelf_frms.iterdir()} == {
        p.name: p.read_bytes() for p in FRMS.iterdir() if p.is_file()}
    source = index(env)["sources"]["openfitavi"]
    date = subprocess.run(["git", "log", "-1", "--format=%cI", head], cwd=env.data / "repos" / SLUG / "repo",
                          capture_output=True, text=True, check=True).stdout.strip()
    assert source == {"commit": head, "name": "OpenFit Aviation", "description": "Aviation applications for OpenFit.",
                      "apps": {"frms": {"sha256": admin_ops.fingerprint(str(shelf_frms)), "name": "FRMS",
                                        "description": source["apps"]["frms"]["description"],
                                        "changes": [{"sha": head[:12], "subject": "First commit", "date": date}]}},
                      "published_at": "2026-10-27T12:00:00Z"}
    assert env.fake.calls[-1].args == ["git", *admin_ops.change_notes_args(head, "frms")]
    assert source["apps"]["frms"]["description"].startswith("Fatigue risk management for airline crew.")
    assert json.dumps(index(env)["sources"]["openfit"]) == official  # byte for byte

    r = row(env)
    assert (r["source"], r["name"], r["official"], r["commit"], r["update"]) == (
        "openfitavi", "OpenFit Aviation", False, head, None)
    assert r["repo"]["status"] == "connected" and r["repo"]["public_key"] is None
    assert [a["id"] for a in r["apps"]] == ["frms"]


@pytest.mark.parametrize("err", [
    "git@github.com: Permission denied (publickey).\r\nfatal: Could not read from remote repository.\n\n"
    "Please make sure you have the correct access rights\nand the repository exists.\n",
    "ERROR: Repository not found.\nfatal: Could not read from remote repository.\n",
])
def test_a_key_github_turned_down_stays_pending(admin_env, err):
    env = admin_env
    aviation(env)
    add(env)
    before = shelf(env)
    env.fake.fail(lambda a: a[:2] == ["git", "clone"], rc=128, err=err)

    op = connect(env)

    assert op["outcome"] == "failed"
    assert op["summary"] == ("GitHub didn't accept the key yet. Check it's added under the repo's Deploy keys, "
                             "then Connect again.")
    assert entry(env)["state"] == "pending"
    assert not (env.data / "repos" / SLUG / "repo").exists()
    assert (env.data / "repos" / SLUG / "deploy_key").exists()  # kept: it's on GitHub already
    assert shelf(env) == before
    assert row(env)["repo"]["public_key"]  # and shown again

    env.fake.rules.clear()  # the key added on GitHub after all: Connect again
    assert connect(env)["outcome"] == "ok"
    assert entry(env)["state"] == "connected"


def test_github_out_of_reach_says_so_and_stays_pending(admin_env):
    env = admin_env
    aviation(env)
    add(env)
    env.fake.fail(lambda a: a[:2] == ["git", "clone"], rc=128,
                  err="ssh: connect to host github.com port 22: Connection timed out\r\n"
                      "fatal: Could not read from remote repository.\n")
    op = connect(env)
    assert op["outcome"] == "failed"
    assert op["summary"] == ("Couldn't reach GitHub: ssh: connect to host github.com port 22: Connection timed out. "
                             f"{OWNER}/{REPO} is still waiting to connect.")
    assert entry(env)["state"] == "pending"


@pytest.mark.parametrize("files, detail", [
    ({}, "repo.yaml is missing: the repo has no commits yet."),
    (frms_files(), "repo.yaml is missing."),
    ({"repo.yaml": "name: Aviation\n", **frms_files()},
     "repo.yaml has no source: lowercase letters and digits, starting with a letter."),
    ({"repo.yaml": "source: Open-Fit\n", **frms_files()},
     "repo.yaml has no source: lowercase letters and digits, starting with a letter."),
    ({"repo.yaml": "- a list\n"}, "repo.yaml isn't a mapping."),
    ({"repo.yaml": "source: [unclosed\n"}, "repo.yaml doesn't parse."),
])
def test_a_repo_that_isnt_an_openfit_repo_yet_connects_but_isnt_published(admin_env, files, detail):
    env = admin_env
    before = shelf(env)
    op = added_and_connected(env, files)

    assert op["outcome"] == "ok", op["log"]
    assert op["summary"] == (f"Connected {OWNER}/{REPO}. It isn't an OpenFit repo yet: it needs a repo.yaml at the "
                             "top, so nothing was published.")
    assert (entry(env)["state"], entry(env)["source"], entry(env)["commit"]) == ("connected", None, None)
    assert shelf(env) == before
    r = row(env)
    assert (r["source"], r["name"], r["update"]) == (None, f"{OWNER}/{REPO}", None)
    assert (r["repo"]["status"], r["repo"]["message"], r["repo"]["detail"]) == (
        "not_openfit", "Connected, but not an OpenFit repo yet: it needs a repo.yaml at the top.", detail)


def test_a_repo_yaml_that_is_a_link_isnt_read(admin_env, tmp_path):
    env = admin_env
    aviation(env, {"real.yaml": REPO_YAML, **frms_files()})
    work = tmp_path / "github-work" / OWNER / REPO
    os.symlink("real.yaml", work / "repo.yaml")
    subprocess.run(["git", "add", "-A"], cwd=work, check=True)
    subprocess.run(["git", "commit", "-qm", "A link"], cwd=work, check=True)
    subprocess.run(["git", "push", "-q", "origin", "HEAD:main"], cwd=work, check=True)
    add(env)
    connect(env)
    assert entry(env)["source"] is None
    assert row(env)["repo"]["detail"] == "repo.yaml is a link."


def test_openfits_own_source_is_refused(admin_env):
    env = admin_env
    before = shelf(env)
    op = added_and_connected(env, {"repo.yaml": "source: openfit\nname: Not OpenFit\n", **frms_files()})
    assert op["outcome"] == "failed"
    message = "Its repo.yaml says it's 'openfit', which is OpenFit's own. Choose another source in it."
    assert op["summary"] == f"Connected {OWNER}/{REPO}, but nothing was published: {message}"
    assert (entry(env)["state"], entry(env)["source"]) == ("connected", None)
    assert shelf(env) == before
    assert (row(env)["repo"]["status"], row(env)["repo"]["message"]) == ("refused", message)


def test_a_source_another_repo_has_is_refused(admin_env):
    env = admin_env
    assert added_and_connected(env)["outcome"] == "ok"
    before = shelf(env)
    other = admin_ops.repo_slug("someone", "copycat")
    op = added_and_connected(env, {"repo.yaml": "source: openfitavi\nname: Copycat\n", **frms_files()},
                             url="https://github.com/someone/copycat", owner="someone", repo="copycat")
    assert op["outcome"] == "failed"
    message = f"Its repo.yaml says it's 'openfitavi', which {OWNER}/{REPO} already is here. Choose another source in it."
    assert op["summary"] == f"Connected someone/copycat, but nothing was published: {message}"
    assert entry(env, other)["source"] is None and entry(env)["source"] == "openfitavi"
    assert shelf(env) == before


def test_connect_a_repo_thats_connected_or_unknown_is_refused(logged_in, admin_env):
    env = admin_env
    added_and_connected(env)
    assert logged_in.post(f"/api/repos/{SLUG}/connect", headers=H, json={}).status_code == 409
    assert logged_in.post("/api/repos/nobody__nothing/connect", headers=H, json={}).status_code == 404
    assert logged_in.post("/api/repos/..%2F..%2Fetc/connect", headers=H, json={}).status_code == 404


# --- 4. the SSH settings -------------------------------------------------------------------


def check(env):
    return env.admin.check()


def broken_publish(*args, **kwargs):
    raise admin_ops.OpError("The app library couldn't be updated. It's as it was.", "disk full")


def a_repos_whole_life(env, monkeypatch):
    """Added, refused once, connected, checked with GitHub ahead, updated -
    once with a publish that fails, so it's put back - checked again,
    removed: every one of its git commands."""
    aviation(env)
    add(env)
    env.fake.fail(lambda a: a[:2] == ["git", "clone"], rc=128, err="git@github.com: Permission denied (publickey).")
    assert connect(env)["outcome"] == "failed"
    env.fake.rules.clear()
    assert connect(env)["outcome"] == "ok"
    env.github.push(OWNER, REPO, {"frms/notes.txt": "new\n"}, "Notes")
    check(env)
    with monkeypatch.context() as m:
        m.setattr(admin_ops, "publish_set", broken_publish)
        assert update(env)["outcome"] == "failed"
    assert update(env)["outcome"] == "ok"
    check(env)
    env.admin.remove_repo(SLUG)


def test_every_repo_git_command_carries_the_pinned_ssh_settings(admin_env, monkeypatch):
    env = admin_env
    a_repos_whole_life(env, monkeypatch)
    calls = org_calls(env)
    kinds = {c.args[1] for c in calls}
    assert kinds == KNOWN  # every allowed command ran, so none of this is vacuous
    folder, clone = str(env.data / "repos" / SLUG), str(env.data / "repos" / SLUG / "repo")
    for c in calls:
        assert c.env == {"GIT_SSH_COMMAND": expected_ssh(env)}, c
        assert c.as_owner is False, c  # as the Admin's own user, root: never the checkout's owner
        assert c.cwd == (folder if c.args[1] == "clone" else clone), c
        assert org_git_in_lane(env, c) == SLUG
    # And the server's checkout's git: as its owner, in it, never with a key.
    for c in env.fake.calls:
        if c.args[0] == "git" and c not in calls:
            assert c.as_owner is True and c.cwd == env.repos.repo and c.env is None, c


def test_the_ssh_command_is_exactly_the_pinned_one(admin_env):
    env = admin_env
    assert admin_ops.ssh_command(str(env.data), SLUG) == expected_ssh(env)
    assert admin_ops.ssh_command("/data", SLUG) == (
        "ssh -i /data/repos/raven-git-hub__openfit-aviation/deploy_key -o IdentitiesOnly=yes "
        "-o UserKnownHostsFile=/app/github_known_hosts -o StrictHostKeyChecking=yes -o BatchMode=yes "
        "-o ConnectTimeout=20")


@pytest.mark.parametrize("slug, data", [
    ("../x__y", "/data"), ("x__y;id", "/data"), ("x y__z", "/data"), ("x__$(id)", "/data"), ("", "/data"),
    (None, "/data"), (SLUG, "data"), (SLUG, "/da ta"), (SLUG, "/data;id"), (SLUG, "/data/../etc"), (SLUG, "/$HOME"),
])
def test_the_ssh_command_takes_only_a_checked_slug_and_folder(slug, data):
    with pytest.raises(ValueError):
        admin_ops.ssh_command(data, slug)


@pytest.mark.parametrize("args", [
    ("push", "origin"), ("fetch", "evil"), ("fetch", "origin", "--upload-pack=x"), ("fetch",),
    ("clone", "--", "https://evil.example/x.git", "DEST"), ("clone", "--", "URL", "/tmp/elsewhere"), ("clone", "URL", "DEST"),
    ("clone", "--upload-pack=touch /tmp/x", "URL", "DEST"),
    ("merge", "origin/main"), ("merge", "--ff-only", "origin/main"), ("merge", "--ff-only", "HEAD"),
    ("reset", "--hard", "HEAD~1"), ("reset", "--soft", "a" * 40), ("rev-parse", "HEAD"),
    ("rev-parse", "--verify", "-q", "origin/main"), ("cat-file", "-p", "HEAD:repo.yaml"), ("cat-file", "-p", "a" * 40),
    ("cat-file", "--batch"), ("log", "--all"), ("log", "--no-merges", "--format=%h%x09%s", "HEAD..origin/main"),
    ("log", "-p", "a" * 40), ("config", "core.sshCommand", "x"), ("checkout", "a" * 40), ("status",),
])
def test_any_other_git_command_for_a_repo_is_refused_unrun(admin_env, args):
    env = admin_env
    aviation(env)
    add(env)
    e = entry(env)
    args = tuple(SSH_URL if a == "URL" else str(env.data / "repos" / SLUG / "repo") if a == "DEST" else a for a in args)
    env.fake.clear()
    with pytest.raises(ValueError):
        env.admin.org_git(e, *args)
    assert env.fake.calls == []


# An app folder's change notes (B3): exactly change_notes_args()' form -
# ten, that format, a full commit, a literal --, one folder name.
CHANGES = ["log", "-n", "10", "--no-merges", "--format=%H%x09%cI%x09%s", "a" * 40, "--", "frms"]


def test_the_change_notes_log_is_the_one_new_form_a_repo_may_run(admin_env):
    env = admin_env
    aviation(env)
    add(env)
    e = entry(env)
    clone = str(env.data / "repos" / SLUG / "repo")
    assert admin_ops.org_git_allowed(CHANGES, e["url"], clone)
    assert admin_ops.org_git_allowed(admin_ops.change_notes_args("b" * 40, "a-b-1"), e["url"], clone)
    env.fake.clear()
    env.admin.org_git(e, *CHANGES)
    assert [c.args for c in env.fake.calls] == [["git", *CHANGES]]
    assert org_git_in_lane(env, env.fake.calls[0]) == SLUG


@pytest.mark.parametrize("args", [
    CHANGES[:2] + ["11"] + CHANGES[3:],  # another count
    CHANGES[:2] + ["20"] + CHANGES[3:],
    CHANGES[:1] + CHANGES[3:],  # no count
    CHANGES[:6] + CHANGES[7:],  # no --
    CHANGES[:6] + ["frms"],
    CHANGES + ["walk"],  # two paths
    CHANGES[:7] + ["frms", "walk"],
    CHANGES[:7] + ["frms/app.py"],  # not one plain name
    CHANGES[:7] + ["../frms"],
    CHANGES[:7] + ["-frms"],
    CHANGES[:7] + [".git"],
    CHANGES[:7] + ["Frms"],
    CHANGES[:7] + [""],
    CHANGES[:7] + ["frms\n"],
    CHANGES[:5] + ["a" * 12] + CHANGES[6:],  # not a full commit
    CHANGES[:5] + ["HEAD"] + CHANGES[6:],
    CHANGES[:5] + ["--all"] + CHANGES[6:],
    CHANGES[:4] + ["--format=%H%x09%s"] + CHANGES[5:],  # another format
    CHANGES[:3] + ["--merges"] + CHANGES[4:],
    CHANGES[:3] + CHANGES[4:],  # merges not left out
    CHANGES[:5] + ["-p"] + CHANGES[5:],
])
def test_any_other_change_notes_log_is_refused_unrun(admin_env, args):
    env = admin_env
    aviation(env)
    add(env)
    e = entry(env)
    env.fake.clear()
    assert not admin_ops.org_git_allowed(args, e["url"], str(env.data / "repos" / SLUG / "repo"))
    with pytest.raises(ValueError):
        env.admin.org_git(e, *args)
    assert env.fake.calls == []


def test_a_repos_update_brings_its_apps_new_change_notes_to_the_index(admin_env):
    env = admin_env
    added_and_connected(env)
    first = entry(env)["commit"]
    env.github.push(OWNER, REPO, {"frms/notes.txt": "new\n"}, "Notes for FRMS")
    env.github.push(OWNER, REPO, {"README.md": "hi\n"}, "A readme, not FRMS")
    check(env)
    assert update(env)["outcome"] == "ok"

    new = entry(env)["commit"]
    changes = index(env)["sources"]["openfitavi"]["apps"]["frms"]["changes"]
    assert [(c["sha"], c["subject"]) for c in changes][1:] == [(first[:12], "First commit")]
    assert changes[0]["subject"] == "Notes for FRMS" and changes[0]["sha"] != new[:12]
    # Read from the clone, with the repo's key, in the one form allowed.
    logs = [c for c in org_calls(env) if c.args[1] == "log" and c.args[-2:] == ["--", "frms"]]
    assert [c.args for c in logs][-1] == ["git", *admin_ops.change_notes_args(new, "frms")]
    for c in logs:
        assert org_git_in_lane(env, c) == SLUG


def test_a_repos_change_notes_that_cant_be_read_publish_it_without_them(admin_env):
    env = admin_env
    aviation(env)
    add(env)
    env.fake.fail(lambda a: a[:3] == ["git", "log", "-n"] and a[-2:] == ["--", "frms"], rc=128, err="fatal: bad")
    op = connect(env)
    assert op["outcome"] == "ok"
    assert index(env)["sources"]["openfitavi"]["apps"]["frms"]["changes"] == []
    assert f"Couldn't read {OWNER}/{REPO}/frms's change notes, so it's published without them." in [
        line["text"] for line in op["log"]]


# --- 5. publishing a source alone ---------------------------------------------------------------


def test_publishing_a_repo_leaves_the_official_entry_byte_identical_and_back(admin_env):
    env = admin_env
    env.admin.publish_official()
    official, official_set = json.dumps(index(env)["sources"]["openfit"]), os.readlink(env.library / "openfit")
    env.clock.advance(3600)  # so an entry written again, even unchanged, says so
    assert added_and_connected(env)["outcome"] == "ok"
    assert json.dumps(index(env)["sources"]["openfit"]) == official
    assert os.readlink(env.library / "openfit") == official_set

    # And the other way round: the official library published anew leaves
    # the repo's entry, link and set exactly as they were.
    theirs, their_set = json.dumps(index(env)["sources"]["openfitavi"]), os.readlink(env.library / "openfitavi")
    their_files = {k: v for k, v in shelf(env).items() if "openfitavi" in k}
    env.repos.push({"library/caffeine/app.yaml": "id: caffeine\n"}, "New app")
    subprocess.run(["git", "pull", "-q", "--ff-only"], cwd=env.repos.repo, check=True, capture_output=True)
    env.clock.advance(3600)
    env.admin.publish_official()
    assert json.dumps(index(env)["sources"]["openfitavi"]) == theirs
    assert os.readlink(env.library / "openfitavi") == their_set
    assert {k: v for k, v in shelf(env).items() if "openfitavi" in k} == their_files
    assert sorted(index(env)["sources"]["openfit"]["apps"]) == ["caffeine", "steps"]


def test_each_source_says_when_it_was_last_published(admin_env):
    env = admin_env
    env.admin.publish_official()
    env.clock.advance(3600)
    added_and_connected(env)
    sources = index(env)["sources"]
    assert sources["openfit"]["published_at"] == "2026-10-27T12:00:00Z"
    assert sources["openfitavi"]["published_at"] == "2026-10-27T13:00:00Z"
    repos = {r["key"]: r for r in env.admin.status()["repos"]}
    assert repos["openfit"]["published_at"] == "2026-10-27T12:00:00Z"
    assert repos[f"repo:{SLUG}"]["published_at"] == "2026-10-27T13:00:00Z"


def test_an_empty_official_library_publishes_an_empty_set(admin_env):
    env = admin_env
    subprocess.run(["git", "rm", "-rq", "library/steps"], cwd=env.repos.repo, check=True)
    (Path(env.repos.repo) / "library").mkdir()
    (Path(env.repos.repo) / "library" / "repo.yaml").write_text("name: OpenFit\ndescription: Official.\n")
    subprocess.run(["git", "add", "-A"], cwd=env.repos.repo, check=True)
    subprocess.run(["git", "commit", "-qm", "FRMS leaves"], cwd=env.repos.repo, check=True)
    result = env.admin.publish_official()
    assert result["apps"] == {}
    source = index(env)["sources"]["openfit"]
    assert (source["commit"], source["name"], source["apps"]) == (env.repos.head(), "OpenFit", {})
    assert os.listdir(env.library / "openfit") == []
    assert env.admin.status()["repos"][0]["apps"] == []


def test_publish_set_takes_a_repos_clone_quietly_past_its_dotfiles(admin_env, tmp_path, capsys):
    clone = tmp_path / "clone"
    (clone / ".git").mkdir(parents=True)
    (clone / ".github" / "workflows").mkdir(parents=True)
    (clone / "README.md").write_text("hi\n")
    (clone / "repo.yaml").write_text(REPO_YAML)
    (clone / "walk").mkdir()
    (clone / "walk" / "app.yaml").write_text("id: walk\n")
    (clone / "walk" / "link").symlink_to("/etc/passwd")
    admin_ops.publish_set(str(clone), str(admin_env.library), "openfitavi", "b" * 40, "now", label=f"{OWNER}/{REPO}",
                          skip_hidden=True)
    out = capsys.readouterr().out
    assert sorted(os.listdir(admin_env.library / "openfitavi")) == ["walk"]
    assert f"Leaving out {OWNER}/{REPO}/README.md: it isn't an app folder." in out
    assert f"Leaving out {OWNER}/{REPO}/walk/link: it's a link." in out
    assert ".git" not in out and "library/" not in out


# --- 6. checks -----------------------------------------------------------------------------------


def test_a_check_fetches_each_repo_and_finds_it_behind(admin_env):
    env = admin_env
    added_and_connected(env)
    published = entry(env)["commit"]
    new = env.github.push(OWNER, REPO, {"frms/notes.txt": "new\n"}, "Notes for FRMS")
    assert row(env)["update"] is None  # not until a check has looked

    check(env)

    r = row(env)
    assert r["update"] == "repo" and r["commit"] == published  # still published at what it was
    repo = r["repo"]
    assert (repo["behind"], repo["can_update"], repo["head"], repo["checked_at"]) == (
        True, True, new[:7], "2026-10-27T12:00:00Z")
    assert [c["subject"] for c in repo["commits"]] == ["Notes for FRMS"]
    assert repo["problem"] is None and repo["error"] is None
    assert entry(env)["commit"] == published and clone_head(env) == published  # a check changes nothing


def test_a_repo_that_cant_be_fetched_doesnt_fail_the_check_or_the_others(admin_env):
    env = admin_env
    added_and_connected(env)
    other = admin_ops.repo_slug("acme", "tools")
    added_and_connected(env, {"repo.yaml": "source: acme\nname: Acme\n", "walk/app.yaml": "id: walk\n"},
                        url="https://github.com/acme/tools", owner="acme", repo="tools")
    env.github.push("acme", "tools", {"walk/notes.txt": "x\n"}, "Acme notes")
    shutil.move(env.github.root / OWNER / f"{REPO}.git", env.github.root / "gone.git")  # GitHub out of reach, for one

    result = check(env)

    assert "behind" in result  # the check itself went fine
    mine, theirs = row(env)["repo"], row(env, other)["repo"]
    assert mine["error"]["text"].startswith("Couldn't reach GitHub: ")
    assert "does not appear to be a git repository" in mine["error"]["text"]
    assert theirs["error"] is None and theirs["can_update"] is True

    # GitHub itself out of reach for the official repo too: the check fails,
    # and the repos are checked all the same.
    shutil.rmtree(env.repos.origin)
    env.github.push("acme", "tools", {"walk/more.txt": "y\n"}, "More")
    with pytest.raises(admin_ops.OpError):
        check(env)
    assert [c["subject"] for c in row(env, other)["repo"]["commits"]] == ["More", "Acme notes"]


def test_a_changed_source_is_refused_and_never_published(logged_in, admin_env):
    env = admin_env
    added_and_connected(env)
    env.github.push(OWNER, REPO, {"repo.yaml": "source: aviation\nname: Aviation\n"}, "Renamed")
    check(env)
    before = shelf(env)

    repo = row(env)["repo"]
    message = "This repo now says it's 'aviation', but it was added as 'openfitavi'. Remove and add it again to change it."
    assert (repo["status"], repo["message"], repo["can_update"], row(env)["update"]) == ("refused", message, False, None)
    resp = logged_in.post(f"/api/repos/{SLUG}/update", json={}, headers=H)
    assert resp.status_code == 409 and resp.get_json()["error"] == message
    assert shelf(env) == before and entry(env)["source"] == "openfitavi"

    # Even an update run regardless re-reads it, and leaves the shelf alone.
    e = entry(env)
    op = env.admin._begin("repo_update", repo={"slug": SLUG, "owner": OWNER, "repo": REPO})
    env.admin._repo_update(op, SLUG)
    assert op["outcome"] == "failed" and op["summary"] == f"{message} The app library is as it was."
    assert shelf(env) == before and clone_head(env) == e["commit"]


def test_a_repo_connected_empty_catches_up_after_its_first_push(admin_env):
    env = admin_env
    assert added_and_connected(env, {})["outcome"] == "ok"
    assert row(env)["repo"]["status"] == "not_openfit"
    head = env.github.push(OWNER, REPO, {"repo.yaml": REPO_YAML, **frms_files()}, "First apps")

    check(env)
    assert row(env)["repo"]["can_update"] is True and row(env)["repo"]["head"] == head[:7]
    op = update(env)

    assert op["outcome"] == "ok", op["log"]
    assert entry(env)["source"] == "openfitavi" and entry(env)["commit"] == head
    assert sorted(os.listdir(env.library / "openfitavi")) == ["frms"]


def test_a_repo_yaml_fixed_on_github_is_offered_then_published(admin_env):
    env = admin_env
    added_and_connected(env, {"repo.yaml": "source: openfit\n", **frms_files()})
    check(env)
    assert row(env)["repo"]["can_update"] is False  # still OpenFit's own on GitHub
    env.github.push(OWNER, REPO, {"repo.yaml": REPO_YAML}, "Our own source")
    check(env)
    assert row(env)["update"] == "repo"
    assert update(env)["outcome"] == "ok"
    assert entry(env)["source"] == "openfitavi"


# --- 7. update ----------------------------------------------------------------------------------


def test_update_merges_then_publishes_and_restarts_nobody(logged_in, admin_env):
    env = admin_env
    added_and_connected(env)
    old = entry(env)["commit"]
    new = env.github.push(OWNER, REPO, {"frms/notes.txt": "new\n"}, "Notes")
    check(env)
    env.fake.clear()

    resp = logged_in.post(f"/api/repos/{SLUG}/update", json={}, headers=H)
    assert resp.status_code == 202
    env.admin.wait(10)
    op = env.admin.current_op()

    assert op["outcome"] == "ok", op["log"]
    assert op["summary"] == f"Updated {OWNER}/{REPO} to {new[:7]}: 1 app in the app library, as 'openfitavi'."
    assert (op["kind"], op["old"]["commit"], op["new"]["commit"]) == ("repo_update", old, new)
    run = [c.args[1:4] for c in env.fake.calls if c.args[0] == "git"]
    merge = run.index(["merge", "--ff-only", new])
    assert run[:merge] == [["rev-parse", "--verify", "-q"]] * 2
    assert not any(c.args[0] == "docker" for c in env.fake.calls)  # no stop, up or build: nothing in docker at all
    assert entry(env)["commit"] == new and clone_head(env) == new
    assert os.readlink(env.library / "openfitavi") == f".sets/openfitavi-{new[:12]}"
    assert (env.library / "openfitavi" / "frms" / "notes.txt").read_text() == "new\n"
    assert sorted(os.listdir(env.library / ".sets")) == sorted([f"openfitavi-{old[:12]}", f"openfitavi-{new[:12]}"])
    assert index(env)["sources"]["openfitavi"]["commit"] == new
    assert row(env)["update"] is None and row(env)["repo"]["behind"] is False
    assert env.admin.status()["activity"][0]["repo"] == {"slug": SLUG, "owner": OWNER, "repo": REPO}


def test_an_update_that_cant_publish_puts_the_repo_back(admin_env, monkeypatch):
    env = admin_env
    added_and_connected(env)
    old = entry(env)["commit"]
    env.github.push(OWNER, REPO, {"frms/notes.txt": "new\n"}, "Notes")
    check(env)
    before = shelf(env)

    monkeypatch.setattr(admin_ops, "publish_set", broken_publish)
    op = update(env)

    assert op["outcome"] == "failed"
    assert op["summary"] == "The app library couldn't be updated, so the repo was put back. The app library is as it was."
    assert ["git", "reset", "--hard", old] in [c.args for c in env.fake.calls]
    assert clone_head(env) == old and entry(env)["commit"] == old
    assert shelf(env) == before
    assert row(env)["update"] == "repo"  # still on offer, to try again


def test_an_update_that_isnt_a_fast_forward_changes_nothing(admin_env):
    env = admin_env
    added_and_connected(env)
    old = entry(env)["commit"]
    work = env.github.tmp / "github-work" / OWNER / REPO
    subprocess.run(["git", "commit", "-q", "--amend", "-m", "Rewritten"], cwd=work, check=True)
    subprocess.run(["git", "push", "-qf", "origin", "HEAD:main"], cwd=work, check=True)
    check(env)
    before = shelf(env)
    op = update(env)
    assert op["outcome"] == "failed" and op["summary"] == "Nothing was changed."
    assert clone_head(env) == old and shelf(env) == before


def test_update_with_nothing_to_apply_is_refused(logged_in, admin_env):
    env = admin_env
    aviation(env)
    add(env)
    resp = logged_in.post(f"/api/repos/{SLUG}/update", json={}, headers=H)
    assert resp.status_code == 409 and "isn't connected yet" in resp.get_json()["error"]
    connect(env)
    check(env)
    resp = logged_in.post(f"/api/repos/{SLUG}/update", json={}, headers=H)
    assert resp.status_code == 409
    assert resp.get_json()["error"] == f"There's no update for {OWNER}/{REPO} to apply. Check for updates first."
    assert logged_in.post("/api/repos/nobody__x/update", json={}, headers=H).status_code == 404


def test_one_operation_at_a_time(logged_in, admin_env, blocking_build):
    env = admin_env
    added_and_connected(env)
    env.repos.push({"app/main.py": "print('2')\n"}, "Two")
    env.admin.check()
    env.admin.start_update()
    assert blocking_build.reached.wait(10)
    for path in ("/api/repos", f"/api/repos/{SLUG}/update", f"/api/repos/{SLUG}/remove"):
        resp = logged_in.post(path, json={"url": "https://github.com/a/b"}, headers=H)
        assert resp.status_code == 409, path
    blocking_build.release.set()
    env.admin.wait(10)


# --- 8. remove ------------------------------------------------------------------------------------


def test_remove_takes_the_source_off_the_shelf_and_deletes_the_clone_and_key(logged_in, admin_env):
    env = admin_env
    env.admin.publish_official()
    added_and_connected(env)
    other = admin_ops.repo_slug("acme", "tools")
    added_and_connected(env, {"repo.yaml": "source: acme\nname: Acme\n", "walk/app.yaml": "id: walk\n"},
                        url="https://github.com/acme/tools", owner="acme", repo="tools")
    keep = {k: v for k, v in shelf(env).items() if "openfitavi" not in k and k != "index.json"}
    sources = index(env)["sources"]
    env.fake.clear()

    resp = logged_in.post(f"/api/repos/{SLUG}/remove", json={}, headers=H)

    assert resp.status_code == 200, resp.get_json()
    assert env.fake.calls == []  # no git, no docker: nobody's OpenFit, nor what they installed, is touched
    assert not os.path.lexists(env.library / "openfitavi")
    assert not any("openfitavi" in p for p in os.listdir(env.library / ".sets"))
    assert {k: v for k, v in shelf(env).items() if k != "index.json"} == keep  # the others' links and sets
    after = index(env)["sources"]
    assert "openfitavi" not in after
    assert json.dumps(after) == json.dumps({k: v for k, v in sources.items() if k != "openfitavi"})
    assert not (env.data / "repos" / SLUG).exists()
    assert [e["slug"] for e in json.loads((env.data / "repos.json").read_text())["repos"]] == [other]
    assert all(r["key"] != f"repo:{SLUG}" for r in env.admin.status()["repos"])
    assert logged_in.post(f"/api/repos/{SLUG}/remove", json={}, headers=H).status_code == 404


def test_a_pending_repo_is_removed_with_its_key(admin_env):
    env = admin_env
    add(env)
    env.admin.remove_repo(SLUG)
    assert not (env.data / "repos" / SLUG).exists()
    assert json.loads((env.data / "repos.json").read_text()) == {"repos": []}


def test_unpublishing_never_takes_the_official_source():
    with pytest.raises(ValueError):
        admin_ops.unpublish_source("/library", "openfit", "now")
    with pytest.raises(ValueError):
        admin_ops.unpublish_source("/library", "../x", "now")


def test_startup_puts_a_published_repo_back_on_a_shelf_made_anew(admin_env):
    env = admin_env
    added_and_connected(env)
    head = entry(env)["commit"]
    shutil.rmtree(env.library)
    env.library.mkdir()
    env.make().publish_at_startup()
    assert os.readlink(env.library / "openfitavi") == f".sets/openfitavi-{head[:12]}"
    assert index(env)["sources"]["openfitavi"]["commit"] == head


# --- 9. the page --------------------------------------------------------------------------------

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")


def repos_status(env):
    """A server with a repo in every state: the official library; the
    aviation repo connected and published, GitHub one commit ahead; one
    pending; one connected empty (not an OpenFit repo yet); one whose
    repo.yaml says openfit (refused) - and a check that couldn't reach one."""
    env.admin.publish_official()
    added_and_connected(env)
    env.github.push(OWNER, REPO, {"frms/notes.txt": "new\n"}, "Notes for FRMS")
    add(env, "https://github.com/acme/pending")
    added_and_connected(env, {}, url="https://github.com/acme/empty", owner="acme", repo="empty")
    added_and_connected(env, {"repo.yaml": "source: openfit\n"}, url="https://github.com/acme/bold", owner="acme",
                        repo="bold")
    env.admin.check()
    status = env.admin.status()
    bold = next(r for r in status["repos"] if r["key"] == "repo:acme__bold")
    bold["repo"]["error"] = {"at": "2026-10-27T12:00:00Z", "text": "Couldn't reach GitHub: Connection timed out",
                             "detail": ""}
    return status


@needs_node
def test_the_repo_tab_lists_every_repo_in_its_state(logged_in, admin_env):
    from test_admin_portal import run_page

    status = repos_status(admin_env)
    out = run_page(logged_in, status, r"""
      const b = boot({});
      await settle();
      const rows = b.byId('sourceRows').children;
      out.rows = rows.map(r => [r.dataset.id, r.children[0].textContent]);
      out.panels = {};
      for (const r of rows) {
        r.children[0].click();
        const panel = r.children[1];
        const link = find(panel, n => n.tagName === 'A')[0];
        out.panels[r.dataset.id] = {
          text: panel.textContent, href: link ? link.href : null,
          buttons: find(panel, n => n.className === 'acts').flatMap(a => find(a, n => n.tagName === 'BUTTON'))
            .map(n => n.textContent),
          state: (find(panel, n => n.dataset.f === 'state')[0] || {textContent: null}).textContent,
          error: (find(panel, n => n.dataset.f === 'error')[0] || {textContent: null}).textContent,
        };
      }
    """)
    assert [r[0] for r in out["rows"]] == ["openfit", "repo:acme__bold", "repo:acme__empty", "repo:acme__pending",
                                           f"repo:{SLUG}"]
    heads = dict(out["rows"])
    assert heads["openfit"].startswith("›openfit")
    assert heads[f"repo:{SLUG}"].startswith("›OpenFit Aviation")
    assert heads["repo:acme__pending"] == "›acme/pendingPending"
    assert heads["repo:acme__empty"] == "›acme/emptyNot an OpenFit repo yet"
    assert heads["repo:acme__bold"] == "›acme/boldNot published"

    p = out["panels"]
    assert p["openfit"]["buttons"] == [] and p["openfit"]["href"] is None  # OpenFit's own: nothing to connect or remove
    aviation_panel = p[f"repo:{SLUG}"]
    assert aviation_panel["href"] == URL and "openfitavi" in aviation_panel["text"]
    assert aviation_panel["buttons"] == ["Remove"]
    assert "Aviation applications for OpenFit." in aviation_panel["text"] and "FRMS" in aviation_panel["text"]
    assert p["repo:acme__pending"]["buttons"] == ["Connect", "Show key", "Remove"]
    assert p["repo:acme__pending"]["state"] == "Waiting for its deploy key: add it on GitHub, then Connect."
    assert p["repo:acme__empty"]["state"] == ("Connected, but not an OpenFit repo yet: it needs a repo.yaml at the "
                                              "top. The repo has no commits on GitHub yet.")
    assert p["repo:acme__empty"]["buttons"] == ["Remove"]
    assert p["repo:acme__bold"]["state"] == ("Its repo.yaml says it's 'openfit', which is OpenFit's own. Choose another "
                                             "source in it.")
    assert p["repo:acme__bold"]["error"] == "Couldn't reach GitHub: Connection timed out"


@needs_node
def test_show_key_connect_and_remove(logged_in, admin_env):
    from test_admin_portal import run_page

    status = repos_status(admin_env)
    key = next(r for r in status["repos"] if r["key"] == "repo:acme__pending")["repo"]["public_key"]
    assert key.startswith("ssh-ed25519 ")
    out = run_page(logged_in, status, r"""
      const b = boot({});
      await settle();
      const item = () => row(b, 'sourceRows', 'repo:acme__pending');
      item().children[0].click();
      const keyPart = () => find(item(), n => n.dataset.f === 'key')[0];
      out.before = !!keyPart();
      buttons(item(), 'Show key')[0].click();
      const shown = keyPart();
      out.key = { value: find(shown, n => n.tagName === 'TEXTAREA')[0].value, readOnly: find(shown, n => n.tagName === 'TEXTAREA')[0].readOnly,
                  steps: find(shown, n => n.tagName === 'LI').map(n => n.textContent),
                  expanded: buttons(item(), 'Hide key')[0].getAttribute('aria-expanded') };
      buttons(shown, 'Copy key')[0].click();
      await settle();
      out.copied = find(keyPart(), n => (n.className || '').includes('form-msg'))[0].textContent;
      buttons(item(), 'Hide key')[0].click();
      out.hidden = !keyPart();
      let before = posts(b).length;
      buttons(item(), 'Connect')[0].click();
      await settle();
      out.connect = posts(b).slice(before);
      const remove = buttons(item(), 'Remove').find(n => n.className.includes('sm'));
      const sure = () => find(item(), n => n.dataset.f === 'remove')[0];
      out.sureBefore = sure().hidden;
      remove.click();
      out.sure = { hidden: sure().hidden, text: sure().textContent };
      before = posts(b).length;
      buttons(sure(), 'Remove')[0].click();
      await settle();
      out.remove = posts(b).slice(before);
      out.msg = b.byId('reposMsg').textContent;
    """)
    assert out["before"] is False
    assert out["key"] == {"value": key, "readOnly": True, "expanded": "true", "steps": [
        "Open the repo on GitHub, then Settings → Deploy keys → Add deploy key.",
        'Paste the key, give it a title, and leave "Allow write access" unticked.',
        "Come back and press Connect."]}
    assert out["copied"] == "Copied."
    assert out["hidden"] is True
    assert out["connect"] == [["/api/repos/acme__pending/connect", "1", "{}"]]
    assert out["sureBefore"] is True and out["sure"]["hidden"] is False
    assert out["sure"]["text"] == (
        "Remove acme/pending?Its apps leave the app library. Users who installed them keep them, but they won't be "
        "updated. Remember to delete the deploy key on GitHub too.CancelRemove")
    assert out["remove"] == [["/api/repos/acme__pending/remove", "1", "{}"]]
    assert out["msg"] == "Removed acme/pending. Delete its deploy key on GitHub too."


@needs_node
def test_add_repository_is_live_and_shows_the_key_to_add(logged_in, admin_env):
    from test_admin_portal import run_page

    status = repos_status(admin_env)
    repo = {"slug": "acme__tools", "owner": "acme", "repo": "tools", "state": "pending"}
    key = "ssh-ed25519 " + "A" * 68 + " openfit-admin nas acme/tools"
    out = run_page(logged_in, status, r"""
      const KEY = %s, REPO = %s;
      const b = boot({});
      await settle();
      const body = b.byId('popupBody');
      b.byId('addRepoBtn').click();
      const input = find(body, n => n.tagName === 'INPUT')[0];
      out.first = { open: open(b), title: b.byId('popupTitle').textContent, placeholder: input.placeholder,
                    buttons: find(body, n => n.tagName === 'BUTTON').map(n => n.textContent) };
      b.page.replies = { '/api/repos': { status: 400, body: { ok: false, error: "That isn't a GitHub repository's address." } } };
      input.value = 'https://gitlab.com/acme/tools';
      buttons(body, 'Add')[0].click();
      await settle();
      out.refused = { open: open(b), text: body.textContent };
      b.page.replies = { '/api/repos': { status: 200, body: { ok: true, repo: REPO, public_key: KEY } } };
      input.value = 'https://github.com/acme/tools';
      let before = posts(b).length;
      buttons(body, 'Add')[0].click();
      await settle();
      out.added = { posts: posts(b).slice(before), title: b.byId('popupTitle').textContent,
                    key: find(body, n => n.tagName === 'TEXTAREA')[0].value,
                    steps: find(body, n => n.tagName === 'LI').length,
                    buttons: find(body, n => n.tagName === 'BUTTON').map(n => n.textContent) };
      before = posts(b).length;
      buttons(body, 'Connect')[0].click();
      await settle();
      out.connected = { posts: posts(b).slice(before), open: open(b) };
    """ % (json.dumps(key), json.dumps(repo)))
    assert out["first"]["open"] is True and out["first"]["title"] == "Add repository"
    assert out["first"]["placeholder"] == "https://github.com/<owner>/<repo>"
    assert out["first"]["buttons"] == ["Cancel", "Add"]
    assert out["refused"]["open"] is True and "That isn't a GitHub repository's address." in out["refused"]["text"]
    assert out["added"]["posts"] == [["/api/repos", "1", '{"url":"https://github.com/acme/tools"}']]
    assert out["added"]["title"] == "Add acme/tools's deploy key"
    assert out["added"]["key"] == key and out["added"]["steps"] == 3
    assert out["added"]["buttons"] == ["Copy key", "Later", "Connect"]
    assert out["connected"] == {"posts": [["/api/repos/acme__tools/connect", "1", "{}"]], "open": False}


@needs_node
def test_a_repos_update_available_runs_its_update(logged_in, admin_env):
    from test_admin_portal import run_page

    status = repos_status(admin_env)
    out = run_page(logged_in, status, r"""
      const b = boot({});
      await settle();
      const rows = b.byId('repoRows').children;
      out.rows = rows.map(r => [r.dataset.source || null, r.dataset.repo || null, r.textContent]);
      const mine = rows.find(r => r.dataset.repo === %s);
      buttons(mine, 'Update available')[0].click();
      const body = b.byId('popupBody');
      out.title = b.byId('popupTitle').textContent;
      out.text = body.textContent;
      const before = posts(b).length;
      buttons(body, 'Proceed')[0].click();
      await settle();
      out.posts = posts(b).slice(before);
      out.closed = !open(b);
    """ % json.dumps(SLUG))
    rows = {r[1] or r[0]: r[2] for r in out["rows"]}
    assert "Update available" in rows[SLUG] and "Last published" in rows[SLUG]
    assert "Pending" in rows["acme__pending"] and "Not published yet" in rows["acme__pending"]
    assert "Couldn't reach GitHub" in rows["acme__bold"]
    assert "Update available" not in rows["acme__empty"] + rows["acme__bold"] + rows["acme__pending"]
    assert out["title"] == "Update OpenFit Aviation"
    assert "Publishes this repo's new apps to every user. Nobody's OpenFit restarts." in out["text"]
    assert "Notes for FRMS" in out["text"]
    assert out["posts"] == [[f"/api/repos/{SLUG}/update", "1", "{}"]]
    assert out["closed"] is True


@needs_node
def test_while_something_runs_connect_remove_and_add_are_off(logged_in, admin_env):
    from test_admin_portal import run_page

    status = repos_status(admin_env)
    status["op"] = {"id": "20261027T150000Z-repo_update", "kind": "repo_update", "outcome": "running", "summary": "",
                    "started_at": "2026-10-27T15:00:00Z", "finished_at": None, "user": None,
                    "repo": {"slug": SLUG, "owner": OWNER, "repo": REPO}, "log": []}
    out = run_page(logged_in, status, r"""
      const b = boot({});
      await settle();
      const item = row(b, 'sourceRows', 'repo:acme__pending');
      item.children[0].click();
      out.off = ['Connect', 'Remove'].map(l => buttons(item, l).map(n => n.disabled));
      out.live = b.byId('liveHead').textContent;
      b.byId('addRepoBtn').click();
      out.add = buttons(b.byId('popupBody'), 'Add')[0].disabled;
    """)
    assert out["off"] == [[True], [True, True]]
    assert out["live"] == f"Update {OWNER}/{REPO}Running"
    assert out["add"] is True
