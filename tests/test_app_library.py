"""
The Library's per-app reads and its one destructive action.

GET /api/apps lists each app's declared reads and the files in its
folder; GET /api/app/<id>/file?name= shows one of those files as text;
DELETE /api/apps/<id> uninstalls an app, folder and all. The file name
and the id come from the request, so both are held to the installer's
containment discipline: nothing outside an app's own folder is ever
read, and nothing but an installed app's folder is ever removed.

Every test gets its own temp apps folder; nothing reads /data/apps.
"""

import os
from pathlib import Path

import pytest

import apps

REAL_APPS = Path(__file__).parent / "fixtures" / "apps"
SECRET = "TOP SECRET - never served"


@pytest.fixture
def apps_dir(tmp_path, monkeypatch):
    """An empty apps folder, which the app is pointed at - with a secret
    beside it, and a folder beside it, that nothing may read or remove."""
    import main

    path = tmp_path / "apps"
    path.mkdir()
    (tmp_path / "secret.txt").write_text(SECRET)
    (tmp_path / "x").mkdir()
    (tmp_path / "x" / "keep.txt").write_text("not the apps folder's")
    monkeypatch.setitem(main.app.config, "APPS_DIR", str(path))
    return path


def install(client, files):
    resp = client.post("/api/apps", json={"files": files})
    assert resp.status_code == 201, resp.get_json()
    return resp


def real(folder, name):
    return (REAL_APPS / folder / name).read_text(encoding="utf-8")


def tree(root):
    """Every path under `root`, relative - to show what changed."""
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*"))


def read_file(client, app_id, name):
    return client.get(f"/api/app/{app_id}/file", query_string={"name": name})


STEPS = {"app.yaml": real("steps", "app.yaml"), "app.py": real("steps", "app.py")}


# ---------- GET /api/apps: reads and files ----------


def test_api_apps_lists_each_apps_reads_and_files(client, apps_dir):
    install(client, {
        "app.yaml": "id: walk\nname: Walk\nreads: [steps, distance_m]\n",
        "app.py": "def render(ctx, cfg):\n    return ctx.empty()\n",
        "thresholds.yaml": "goal: 10000\n",
    })
    install(client, {"app.yaml": "id: notes\n"})

    assert client.get("/api/apps").get_json() == [
        {"id": "notes", "name": "notes", "default_size": {"w": 2, "h": 2},
         "reads": [], "files": ["app.yaml"]},
        {"id": "walk", "name": "Walk", "default_size": {"w": 2, "h": 2},
         "reads": ["steps", "distance_m"], "files": ["app.py", "app.yaml", "thresholds.yaml"]},
    ]


def test_the_files_listed_are_the_plain_ones_in_the_folder(client, apps_dir, tmp_path):
    install(client, {**STEPS, "notes.txt": "hello\n", ".env": "X=1\n"})
    folder = apps_dir / "steps"
    # Rendering imports app.py, which leaves a __pycache__ folder.
    assert client.get("/api/app/steps/render").status_code == 200
    assert (folder / "__pycache__").is_dir()
    # Neither a sub-folder nor a link is a file of the app's: a link could
    # point anywhere.
    (folder / "data").mkdir()
    (folder / "data" / "inner.csv").write_text("a,b\n")
    (folder / "secret-link.txt").symlink_to(tmp_path / "secret.txt")
    (folder / "alias.yaml").symlink_to(folder / "app.yaml")

    [listed] = client.get("/api/apps").get_json()
    assert listed["files"] == [".env", "app.py", "app.yaml", "notes.txt"]


# ---------- GET /api/app/<id>/file ----------


def test_a_listed_file_is_served_as_text_exactly(client, apps_dir):
    files = {
        "app.yaml": "id: exact\r\nname: Exact\r\n",
        "app.py": "# café ☕\ndef render(ctx, cfg):\n    return ctx.empty()\n",
        "empty.txt": "",
    }
    install(client, files)

    for name, text in files.items():
        resp = read_file(client, "exact", name)
        assert resp.status_code == 200, name
        assert resp.get_json() == {"id": "exact", "name": name, "text": text}


def test_the_real_apps_files_are_served(client, apps_dir):
    install(client, STEPS)
    for name in client.get("/api/apps").get_json()[0]["files"]:
        assert read_file(client, "steps", name).get_json()["text"] == STEPS[name]


def test_a_file_of_an_unknown_app_is_404(client, apps_dir):
    resp = read_file(client, "nope", "app.yaml")
    assert resp.status_code == 404
    assert resp.get_json() == {"ok": False, "error": "unknown app: nope"}


def test_a_file_of_an_app_that_doesnt_load_is_404(client, apps_dir):
    (apps_dir / "broken").mkdir()
    (apps_dir / "broken" / "app.yaml").write_text("id: broken\nname: [unclosed\n")

    assert read_file(client, "broken", "app.yaml").status_code == 404


@pytest.mark.parametrize(
    "name",
    [
        "../x",
        "../secret.txt",
        "../../secret.txt",
        "../steps/app.py",
        "a/b",
        "x/keep.txt",
        "a\\b",
        "..",
        ".",
        "",
        "...",
        "app.yaml/..",
        "/etc/passwd",
        "nul\x00.txt",
        "line\nbreak",
    ],
)
def test_a_name_that_isnt_plain_is_refused(client, apps_dir, tmp_path, name):
    install(client, STEPS)
    resp = read_file(client, "steps", name)

    assert resp.status_code == 400, name
    assert resp.get_json()["ok"] is False
    assert SECRET not in resp.get_data(as_text=True)


def test_an_absolute_path_to_a_real_file_is_refused(client, apps_dir, tmp_path):
    install(client, STEPS)
    for name in (str(tmp_path / "secret.txt"), str(apps_dir / "steps" / "app.yaml")):
        resp = read_file(client, "steps", name)
        assert resp.status_code == 400, name
        assert SECRET not in resp.get_data(as_text=True)


def test_a_name_with_no_name_param_is_refused(client, apps_dir):
    install(client, STEPS)
    assert client.get("/api/app/steps/file").status_code == 400


@pytest.mark.parametrize("name", ["missing.txt", "data", "__pycache__", "secret-link.txt", "alias.yaml"])
def test_a_name_the_folder_doesnt_hold_as_a_plain_file_is_404(client, apps_dir, tmp_path, name):
    install(client, STEPS)
    folder = apps_dir / "steps"
    client.get("/api/app/steps/render")   # leaves __pycache__
    (folder / "data").mkdir()
    (folder / "secret-link.txt").symlink_to(tmp_path / "secret.txt")
    (folder / "alias.yaml").symlink_to(folder / "app.yaml")

    resp = read_file(client, "steps", name)
    assert resp.status_code == 404, name
    assert resp.get_json() == {"ok": False, "error": f"steps has no file {name!r}"}
    assert SECRET not in resp.get_data(as_text=True)


def test_nothing_outside_the_apps_folder_is_opened(client, apps_dir, monkeypatch):
    install(client, STEPS)
    opened = []
    real_open = os.open

    def recording_open(path, *args, **kwargs):
        opened.append(os.path.realpath(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(apps.os, "open", recording_open)
    for name in ("app.yaml", "../secret.txt", "/etc/passwd", "missing.txt", "a/b"):
        read_file(client, "steps", name)

    folder = os.path.realpath(apps_dir / "steps")
    assert opened == [os.path.join(folder, "app.yaml")]


def test_a_link_that_slips_past_the_listing_is_still_refused(client, apps_dir, tmp_path, monkeypatch):
    # Say the folder changed between the listing and the read: the
    # resolved path must still sit in the folder ...
    install(client, STEPS)
    folder = apps_dir / "steps"
    (folder / "out.txt").symlink_to(tmp_path / "secret.txt")
    # ... and a link to a file inside it isn't followed either.
    (folder / "in.yaml").symlink_to(folder / "app.yaml")
    monkeypatch.setattr(apps, "app_files", lambda path: ["app.yaml", "out.txt", "in.yaml"])

    for name in ("out.txt", "in.yaml"):
        resp = read_file(client, "steps", name)
        assert resp.status_code == 404, name
        assert SECRET not in resp.get_data(as_text=True)
    assert read_file(client, "steps", "app.yaml").status_code == 200


def test_a_file_that_isnt_text_is_415(client, apps_dir):
    install(client, STEPS)
    (apps_dir / "steps" / "icon.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")

    resp = read_file(client, "steps", "icon.png")
    assert resp.status_code == 415
    assert resp.get_json() == {"ok": False, "error": "'icon.png' isn't text, so it can't be shown"}


def test_a_file_too_big_to_show_is_413(client, apps_dir):
    install(client, STEPS)
    (apps_dir / "steps" / "big.csv").write_text("x" * (apps.MAX_VIEW_BYTES + 1))
    (apps_dir / "steps" / "fits.csv").write_text("x" * apps.MAX_VIEW_BYTES)

    assert read_file(client, "steps", "big.csv").status_code == 413
    assert len(read_file(client, "steps", "fits.csv").get_json()["text"]) == apps.MAX_VIEW_BYTES


# ---------- DELETE /api/apps/<id> ----------


def test_uninstall_removes_the_app_its_folder_and_its_tile(client, apps_dir, tmp_path):
    install(client, {**STEPS, "thresholds.yaml": "goal: 10000\n"})
    install(client, {"app.yaml": "id: notes\n"})
    client.get("/api/app/steps/render")
    before = tree(tmp_path)

    resp = client.delete("/api/apps/steps")

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "id": "steps"}
    assert sorted(os.listdir(apps_dir)) == ["notes"]   # no trash left behind
    assert [a["id"] for a in client.get("/api/apps").get_json()] == ["notes"]
    assert client.get("/api/app/steps/render").status_code == 404
    # Nothing else changed: only the app's own folder went.
    assert tree(tmp_path) == [p for p in before if not p.startswith("apps/steps")]


def test_uninstall_of_an_unknown_app_is_404_and_removes_nothing(client, apps_dir, tmp_path):
    install(client, STEPS)
    client.get("/api/apps")   # loads app.py, which leaves its __pycache__
    before = tree(tmp_path)

    resp = client.delete("/api/apps/nope")
    assert resp.status_code == 404
    assert resp.get_json() == {"ok": False, "error": "'nope' isn't installed"}
    assert tree(tmp_path) == before


def test_uninstall_of_an_app_that_doesnt_load_is_404(client, apps_dir, tmp_path):
    # Not listed, so not installed as far as the Library knows: left for
    # the alerts slice (and the owner) to deal with.
    (apps_dir / "broken").mkdir()
    (apps_dir / "broken" / "app.yaml").write_text("id: broken\nname: [unclosed\n")
    before = tree(tmp_path)

    assert client.delete("/api/apps/broken").status_code == 404
    assert tree(tmp_path) == before


@pytest.mark.parametrize(
    "segment",
    [
        "..",
        "%2e%2e",
        ".",
        ".hidden",
        ".uninstall-0123456789abcdef",
        "..%2Fx",
        "../x",
        "%2E%2E%2Fx",
        "a%20b",
        "x%0A",
        "%00",
        "-x",
        "st%C3%A9ps",
    ],
)
def test_a_traversal_or_non_id_segment_removes_nothing(client, apps_dir, tmp_path, segment):
    install(client, STEPS)
    (apps_dir / ".hidden").mkdir()
    (apps_dir / ".hidden" / "app.yaml").write_text("id: hidden\n")
    client.get("/api/apps")   # loads app.py, which leaves its __pycache__
    before = tree(tmp_path)

    resp = client.delete("/api/apps/" + segment)

    # Refused by the id check, or never routed to it at all.
    assert resp.status_code in (400, 404), segment
    if resp.status_code == 400:
        assert resp.get_json()["error"].startswith("not an app id:")
    assert tree(tmp_path) == before


def test_uninstall_removes_the_folder_the_app_loaded_from(client, apps_dir):
    # A hand-placed app whose folder isn't named for its id: the id finds
    # it, and its own folder goes.
    (apps_dir / "my-steps").mkdir()
    for name, text in STEPS.items():
        (apps_dir / "my-steps" / name).write_text(text)
    assert [a["id"] for a in client.get("/api/apps").get_json()] == ["steps"]

    assert client.delete("/api/apps/steps").status_code == 200
    assert os.listdir(apps_dir) == []


def test_uninstall_takes_any_id_the_scanner_lists(client, apps_dir):
    # A hand-placed id the installer wouldn't take (capitals, '_') is
    # still listed, so it can still be uninstalled.
    (apps_dir / "My_App").mkdir()
    (apps_dir / "My_App" / "app.yaml").write_text("name: Mine\n")
    assert [a["id"] for a in client.get("/api/apps").get_json()] == ["My_App"]

    assert client.delete("/api/apps/My_App").status_code == 200
    assert os.listdir(apps_dir) == []


def test_uninstall_of_a_linked_folder_never_touches_its_target(client, apps_dir, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    for name, text in STEPS.items():
        (target / name).write_text(text)
    (apps_dir / "steps").symlink_to(target)
    assert [a["id"] for a in client.get("/api/apps").get_json()] == ["steps"]

    assert client.delete("/api/apps/steps").status_code == 200
    assert os.listdir(apps_dir) == []
    assert sorted(os.listdir(target)) == ["__pycache__", "app.py", "app.yaml"]
    assert (target / "app.yaml").read_text() == STEPS["app.yaml"]


def test_remove_then_add_is_the_update_path(client, apps_dir):
    v1 = {"app.yaml": "id: echo\n", "app.py": "def render(ctx, cfg):\n    return ctx.empty('one')\n"}
    v2 = {"app.yaml": "id: echo\n", "app.py": "def render(ctx, cfg):\n    return ctx.empty('two')\n"}
    install(client, v1)
    assert client.get("/api/app/echo/render").get_json()["message"] == "one"
    stamps = {n: os.stat(apps_dir / "echo" / n).st_mtime_ns for n in v1}

    assert client.post("/api/apps", json={"files": v2}).status_code == 409
    assert client.delete("/api/apps/echo").status_code == 200
    install(client, v2)
    # The same sizes and, here, the same timestamps as the old files: the
    # new app is still loaded afresh, never served from the old one's cache.
    for name, ns in stamps.items():
        os.utime(apps_dir / "echo" / name, ns=(ns, ns))

    assert client.get("/api/app/echo/render").get_json()["message"] == "two"


# ---------- uninstall_app(): whole, or not at all ----------


def test_uninstall_app_removes_cleanly(apps_dir, capsys):
    (apps_dir / "notes").mkdir()
    (apps_dir / "notes" / "app.yaml").write_text("id: notes\n")

    assert apps.uninstall_app(str(apps_dir), "notes") == {"id": "notes"}
    assert os.listdir(apps_dir) == []
    assert apps.scan_apps(str(apps_dir)) == []
    assert f"[apps] uninstalled notes: removed {apps_dir / 'notes'}" in capsys.readouterr().out


@pytest.mark.parametrize("app_id", [None, 5, True, "", "..", ".", "a/b", "../x", ".x", "x\n", "a b"])
def test_uninstall_app_refuses_an_id_no_app_could_have(apps_dir, tmp_path, app_id):
    (apps_dir / "notes").mkdir()
    (apps_dir / "notes" / "app.yaml").write_text("id: notes\n")
    before = tree(tmp_path)

    with pytest.raises(apps.UninstallError) as raised:
        apps.uninstall_app(str(apps_dir), app_id)
    assert raised.value.status == 400
    assert tree(tmp_path) == before


def test_a_folder_that_cant_be_moved_leaves_the_app_whole(client, apps_dir, monkeypatch):
    install(client, STEPS)
    client.get("/api/apps")   # loads app.py, which leaves its __pycache__
    before = tree(apps_dir)

    def failing_rename(src, dst):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(apps.os, "rename", failing_rename)
    resp = client.delete("/api/apps/steps")

    assert resp.status_code == 500
    assert resp.get_json() == {
        "ok": False,
        "error": "couldn't remove the app's folder: Permission denied",
    }
    assert tree(apps_dir) == before
    assert [a["id"] for a in client.get("/api/apps").get_json()] == ["steps"]


def test_a_delete_that_fails_part_way_leaves_no_half_app(client, apps_dir, monkeypatch, capsys):
    # Say the disk errors half way through deleting: the app is already
    # out of the scanner's sight, in a hidden folder, never half loaded.
    install(client, {**STEPS, "thresholds.yaml": "goal: 10000\n"})

    def partial_rmtree(path):
        os.remove(os.path.join(path, "app.py"))
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(apps.shutil, "rmtree", partial_rmtree)
    capsys.readouterr()
    resp = client.delete("/api/apps/steps")

    assert resp.status_code == 200
    [left] = os.listdir(apps_dir)
    assert left.startswith(".uninstall-")
    assert sorted(os.listdir(apps_dir / left)) == ["__pycache__", "app.yaml", "thresholds.yaml"]
    assert client.get("/api/apps").get_json() == []
    out = capsys.readouterr().out
    assert "[apps] uninstalled steps, but couldn't delete" in out
    assert "skipping" not in out
