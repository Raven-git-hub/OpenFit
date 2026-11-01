"""The Admin's update check: what's new on GitHub, by group - and nothing else.

git runs for real against temp repos (conftest's GitRepos); docker never
comes into it.
"""

import json

import pytest

import admin_ops

READ_ONLY_GIT = {"fetch", "log", "rev-list", "diff"}


def subjects(group):
    return {c["subject"] for c in group["commits"]}


def by_key(check):
    return {g["key"]: g for g in check["groups"]}


def test_check_classifies_changes_into_the_four_groups(admin_env):
    r = admin_env.repos
    before_head, before_tree = r.head(), r.status()
    with open(f"{r.repo}/app/main.py") as f:
        before_file = f.read()

    r.merge_pr(7, [
        ("Steps faster", {"app/main.py": "print('faster')\n"}),
        ("Docs for steps", {"docs/steps.md": "steps\n"}),
    ])
    r.merge_pr(8, [("Admin tweak", {"admin/admin_server.py": "print('admin 2')\n"})])
    r.push({"library/caffeine/app.yaml": "id: caffeine\n"}, "New library app")
    r.push({"docs/readme.md": "# OpenFit, tidied\n"}, "Tidy docs")
    r.push({"app/host.py": "x = 1\n", "docs/host.md": "host\n"}, "Steps and docs together")

    check = admin_env.admin.check()

    # 2 + merge, 1 + merge, and three straight onto main.
    assert check["behind"] == 8
    assert check["current"] == {"sha": before_head[:7], "subject": "Initial commit"}
    groups = by_key(check)
    assert [g["key"] for g in check["groups"]] == ["app", "admin", "library", "other"]
    assert [g["title"] for g in check["groups"]] == ["User system", "Admin", "App library", "Other"]

    assert subjects(groups["app"]) == {"Steps faster", "Steps and docs together"}
    assert subjects(groups["admin"]) == {"Admin tweak"}
    assert subjects(groups["library"]) == {"New library app"}
    # Other: only the commits no other group lists - the one touching app/
    # and docs/ is the user system's.
    assert subjects(groups["other"]) == {"Docs for steps", "Tidy docs"}
    assert not any(c["subject"].startswith("Merge") for g in check["groups"] for c in g["commits"])

    pr7 = {"number": 7, "url": "https://github.com/Raven-git-hub/OpenFit/pull/7"}
    pr8 = {"number": 8, "url": "https://github.com/Raven-git-hub/OpenFit/pull/8"}
    assert groups["app"]["prs"] == [pr7]
    assert groups["admin"]["prs"] == [pr8]
    assert groups["library"]["prs"] == []
    assert groups["other"]["prs"] == [pr7]

    assert sorted(groups["app"]["files"]) == ["app/host.py", "app/main.py"]
    assert groups["admin"]["files"] == ["admin/admin_server.py"]
    assert groups["library"]["files"] == ["library/caffeine/app.yaml"]
    assert sorted(groups["other"]["files"]) == ["docs/host.md", "docs/readme.md", "docs/steps.md"]
    for g in check["groups"]:
        for c in g["commits"]:
            assert len(c["sha"]) >= 7 and c["subject"]

    # Saved for the page, with its time.
    saved = json.loads((admin_env.data / "last_check.json").read_text())
    assert saved == check
    assert check["checked_at"] == "2026-10-27T12:00:00Z"
    assert check["error"] is None

    # HEAD and the working tree are untouched.
    assert r.head() == before_head
    assert r.status() == before_tree == ""
    with open(f"{r.repo}/app/main.py") as f:
        assert f.read() == before_file
    used = {c.args[1] for c in admin_env.fake.calls}
    assert used <= READ_ONLY_GIT
    assert all(c.args[0] == "git" for c in admin_env.fake.calls)


def test_check_with_nothing_new(admin_env):
    check = admin_env.admin.check()
    assert check["behind"] == 0
    assert all(not g["commits"] and not g["files"] and not g["prs"] for g in check["groups"])
    assert not admin_ops.update_offered(check)


def test_a_failed_check_records_the_error_and_its_time(admin_env):
    admin_env.repos.push({"app/main.py": "print(2)\n"}, "Two")
    good = admin_env.admin.check()
    admin_env.clock.advance(3600)
    admin_env.fake.fail(lambda a: a[:2] == ["git", "fetch"], rc=128, err="Could not resolve host: github.com")

    with pytest.raises(admin_ops.OpError) as e:
        admin_env.admin.check()

    assert "Couldn't reach GitHub" in e.value.message
    saved = admin_env.admin.last_check()
    assert saved["error"]["at"] == "2026-10-27T13:00:00Z"
    assert "Couldn't reach GitHub" in saved["error"]["text"]
    assert "Could not resolve host" in saved["error"]["detail"]
    # The last good result is kept, and its time.
    assert saved["checked_at"] == good["checked_at"]
    assert saved["behind"] == 1


def test_a_failed_check_is_shown_by_the_route(admin_client, admin_env):
    admin_env.fake.fail(lambda a: a[:2] == ["git", "fetch"], rc=128, err="no network")
    admin_env.auth.setup(admin_env.auth.setup_code, "a long password!")
    admin_client.post("/api/login", json={"password": "a long password!"}, headers={"X-OpenFit-Admin": "1"})

    r = admin_client.post("/api/check", json={}, headers={"X-OpenFit-Admin": "1"})

    assert r.status_code == 502
    assert r.get_json()["check"]["error"]["at"] == "2026-10-27T12:00:00Z"
    status = admin_client.get("/api/status").get_json()
    assert "Couldn't reach GitHub" in status["check"]["error"]["text"]


# The automatic check is the schedule's now (SU1): tests/test_admin_schedule.py.
