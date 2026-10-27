"""
The Library's per-app reads, and its two actions that change an app.

GET /api/apps lists each app's declared reads and the files in its
folder; GET /api/app/<id>/file?name= shows one of those files as text;
PUT /api/apps/<id> updates an app, its files replaced with a new set
and its data kept; DELETE /api/apps/<id> uninstalls an app, folder and
all. The file name and the id come from the request, so both are held to
the installer's containment discipline: nothing outside an app's own
folder is ever read, and nothing but an installed app's folder is ever
replaced or removed.

Every test gets its own temp apps folder; nothing reads /data/apps.
"""

import os
from pathlib import Path

import pytest

import apps

REAL_APPS = Path(__file__).parent.parent / "library"
SECRET = "TOP SECRET - never served"
# Every app declares a size; these give their apps this one.
SIZE = "sizes: [{w: 1, h: 1}]\n"


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
        "app.yaml": "id: walk\nname: Walk\nreads: [steps, distance_m]\n" + SIZE,
        "app.py": "def render(ctx, cfg):\n    return ctx.empty()\n",
        "thresholds.yaml": "goal: 10000\n",
    })
    install(client, {"app.yaml": "id: notes\n" + SIZE})

    assert client.get("/api/apps").get_json() == [
        {"id": "notes", "name": "notes", "library": None, "default_size": {"w": 1, "h": 1}, "sizes": [{"w": 1, "h": 1}],
         "reads": [], "config": [], "metrics": [], "files": ["app.yaml"]},
        {"id": "walk", "name": "Walk", "library": None, "default_size": {"w": 1, "h": 1}, "sizes": [{"w": 1, "h": 1}],
         "reads": ["steps", "distance_m"], "config": [], "metrics": [], "files": ["app.py", "app.yaml", "thresholds.yaml"]},
    ]


def test_the_files_listed_are_the_plain_ones_in_the_folder(client, apps_dir, tmp_path):
    install(client, {**STEPS, "notes.txt": "hello\n", ".env": "X=1\n"})
    folder = apps_dir / "steps"
    # Rendering imports app.py, which leaves a __pycache__ folder.
    assert client.post("/api/app/steps/render").status_code == 200
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
        "app.yaml": "id: exact\r\nname: Exact\r\nsizes: [{w: 1, h: 1}]\r\n",
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
    client.post("/api/app/steps/render")   # leaves __pycache__
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
    install(client, {"app.yaml": "id: notes\n" + SIZE})
    client.post("/api/app/steps/render")
    before = tree(tmp_path)

    resp = client.delete("/api/apps/steps")

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "id": "steps"}
    assert sorted(os.listdir(apps_dir)) == ["notes"]   # no trash left behind
    assert [a["id"] for a in client.get("/api/apps").get_json()] == ["notes"]
    assert client.post("/api/app/steps/render").status_code == 404
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
    (apps_dir / ".hidden" / "app.yaml").write_text("id: hidden\n" + SIZE)
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
    (apps_dir / "My_App" / "app.yaml").write_text("name: Mine\n" + SIZE)
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
    v1 = {"app.yaml": "id: echo\n" + SIZE, "app.py": "def render(ctx, cfg):\n    return ctx.empty('one')\n"}
    v2 = {"app.yaml": "id: echo\n" + SIZE, "app.py": "def render(ctx, cfg):\n    return ctx.empty('two')\n"}
    install(client, v1)
    assert client.post("/api/app/echo/render").get_json()["message"] == "one"
    stamps = {n: os.stat(apps_dir / "echo" / n).st_mtime_ns for n in v1}

    assert client.post("/api/apps", json={"files": v2}).status_code == 409
    assert client.delete("/api/apps/echo").status_code == 200
    install(client, v2)
    # The same sizes and, here, the same timestamps as the old files: the
    # new app is still loaded afresh, never served from the old one's cache.
    for name, ns in stamps.items():
        os.utime(apps_dir / "echo" / name, ns=(ns, ns))

    assert client.post("/api/app/echo/render").get_json()["message"] == "two"


# ---------- uninstall_app(): whole, or not at all ----------


def test_uninstall_app_removes_cleanly(apps_dir, capsys):
    (apps_dir / "notes").mkdir()
    (apps_dir / "notes" / "app.yaml").write_text("id: notes\n" + SIZE)

    assert apps.uninstall_app(str(apps_dir), "notes") == {"id": "notes"}
    assert os.listdir(apps_dir) == []
    assert apps.scan_apps(str(apps_dir)) == []
    assert f"[apps] uninstalled notes: removed {apps_dir / 'notes'}" in capsys.readouterr().out


@pytest.mark.parametrize("app_id", [None, 5, True, "", "..", ".", "a/b", "../x", ".x", "x\n", "a b"])
def test_uninstall_app_refuses_an_id_no_app_could_have(apps_dir, tmp_path, app_id):
    (apps_dir / "notes").mkdir()
    (apps_dir / "notes" / "app.yaml").write_text("id: notes\n" + SIZE)
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


# ---------- PUT /api/apps/<id>: updating an app ----------

# echo, at two versions: each says which it is. The same sizes, so a
# version's files can stand in for the other's.
def echo(version, name="Echo", **extra):
    return {
        "app.yaml": f"id: echo\nname: {name}\n" + SIZE,
        "app.py": f"def render(ctx, cfg):\n    return ctx.empty({version!r})\n",
        **extra,
    }


def update(client, app_id, files):
    return client.put(f"/api/apps/{app_id}", json={"files": files})


def says(client, app_id):
    """What the app's tile says now: its render's message, or None."""
    return client.post(f"/api/app/{app_id}/render").get_json().get("message")


def hidden(root):
    """The dot-folders in the apps folder: an update's staging or trash."""
    return sorted(name for name in os.listdir(root) if name.startswith("."))


def test_update_replaces_files_and_keeps_the_id(client, apps_dir):
    install(client, echo("one", **{"notes.txt": "v1\n"}))
    assert says(client, "echo") == "one"
    v2 = echo("two", name="Echo Two", **{"notes.txt": "v2\n", "extra.csv": "a,b\n"})
    v2["app.yaml"] += "reads: [steps]\nsizes: [{w: 2, h: 1}, {w: 1, h: 1}]\n"

    resp = update(client, "echo", v2)

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "id": "echo", "has_app_py": True}
    # The same folder, holding the new files, each exactly as given.
    assert sorted(os.listdir(apps_dir)) == ["echo"]
    assert apps.app_files(str(apps_dir / "echo")) == sorted(v2)
    for name, text in v2.items():
        assert (apps_dir / "echo" / name).read_text() == text
    # The same id, listed with the new manifest - and drawn by the new code.
    [listed] = client.get("/api/apps").get_json()
    assert (listed["id"], listed["name"], listed["reads"]) == ("echo", "Echo Two", ["steps"])
    assert listed["sizes"] == [{"w": 2, "h": 1}, {"w": 1, "h": 1}]
    assert listed["files"] == sorted(v2)
    assert says(client, "echo") == "two"


def test_an_updated_app_is_loaded_afresh_never_from_the_cache(client, apps_dir):
    install(client, echo("one"))
    assert says(client, "echo") == "one"
    stamps = {n: os.stat(apps_dir / "echo" / n).st_mtime_ns for n in ("app.yaml", "app.py")}

    assert update(client, "echo", echo("two")).status_code == 200
    # The same sizes and, here, the same timestamps as the old files: the
    # new app is still loaded afresh, never served from the old one's cache.
    for name, ns in stamps.items():
        os.utime(apps_dir / "echo" / name, ns=(ns, ns))

    assert says(client, "echo") == "two"


# log, which invents a metric and writes it, and a workout, when acted on;
# at v2 it draws the latest of its metric.
LOG_YAML = (
    "id: log\nname: Log\n" + SIZE
    + "metrics:\n  - {key: cups, label: Cups, unit: cups, category: Other, grain: daily}\n"
)
LOG_HANDLE = (
    "def handle(ctx, action, payload):\n"
    "    ctx.write_metric('cups', payload['cups'], date='2026-01-02')\n"
    "    ctx.write_session('workout', '2026-01-02T07:00:00Z', '2026-01-02T08:00:00Z', {'type': 'run'})\n"
)
LOG_V1 = {"app.yaml": LOG_YAML, "app.py": "def render(ctx, cfg):\n    return ctx.empty('one')\n" + LOG_HANDLE}
LOG_V2 = {
    "app.yaml": LOG_YAML.replace("name: Log", "name: Log v2"),
    "app.py": "def render(ctx, cfg):\n    return ctx.number(ctx.metric('cups')[-1].value)\n" + LOG_HANDLE,
}


def test_update_keeps_the_apps_data(client, conn, apps_dir):
    install(client, LOG_V1)
    resp = client.post("/api/app/log/action", json={"action": "log", "payload": {"cups": 3}})
    assert resp.get_json()["ok"] is True
    stored = lambda: (
        [tuple(r) for r in conn.execute("SELECT date, source, metric, value FROM metrics")],
        [tuple(r) for r in conn.execute("SELECT date, source, metric, value FROM derived_metrics")],
        [tuple(r) for r in conn.execute('SELECT id, source, kind, "start", "end" FROM sessions')],
    )
    before = stored()
    assert before == (
        [("2026-01-02", "app:log", "app:log:cups", 3.0)],
        [("2026-01-02", "app:log", "app:log:cups", 3.0)],
        [("app:log:workout:2026-01-02T07:00:00Z", "app:log", "workout",
          "2026-01-02T07:00:00Z", "2026-01-02T08:00:00Z")],
    )

    assert update(client, "log", LOG_V2).status_code == 200

    # Every row it wrote is still there, still its own ...
    assert stored() == before
    # ... still served: its metric in the catalog and through the contract,
    # its session beside every source's, its source no orphan ...
    [entry] = client.get("/api/catalog").get_json()["metrics"]
    assert (entry["metric"], entry["count"], entry["label"]) == ("app:log:cups", 1, "Cups")
    assert client.get("/api/metric/app:log:cups").get_json()["points"] == [
        {"date": "2026-01-02", "value": 3.0, "source": "app:log"},
    ]
    [session] = client.get("/api/sessions/workout").get_json()
    assert (session["source"], session["summary"]) == ("app:log", {"type": "run"})
    [source] = client.get("/api/app-sources").get_json()
    assert (source["id"], source["orphaned"], source["readings"], source["sessions"]) == ("log", False, 1, 1)
    # ... and read by the new code, under the new manifest.
    assert client.post("/api/app/log/render").get_json()["value"] == 3.0
    assert client.get("/api/apps").get_json()[0]["name"] == "Log v2"


def test_a_custom_metric_the_update_drops_is_kept_and_comes_back(client, conn, apps_dir):
    install(client, LOG_V1)
    client.post("/api/app/log/action", json={"action": "log", "payload": {"cups": 3}})

    # Declared no more: out of the catalog, as after an uninstall - its
    # readings kept under the app's source, which is still installed.
    dropped = {"app.yaml": "id: log\nname: Log\n" + SIZE}
    assert update(client, "log", dropped).status_code == 200
    assert client.get("/api/catalog").get_json()["metrics"] == []
    assert client.get("/api/metric/app:log:cups").status_code == 404
    assert conn.execute("SELECT COUNT(*) FROM metrics WHERE metric = 'app:log:cups'").fetchone()[0] == 1
    [source] = client.get("/api/app-sources").get_json()
    assert (source["orphaned"], source["metrics"]) == (False, ["app:log:cups"])

    # Declared again, it's back, history and all.
    assert update(client, "log", LOG_V1).status_code == 200
    [entry] = client.get("/api/catalog").get_json()["metrics"]
    assert (entry["metric"], entry["count"]) == ("app:log:cups", 1)


@pytest.mark.parametrize(
    "files, why",
    [
        # app.py: won't import, has no render, has a handle that isn't one.
        ({"app.py": "1 / 0\n"}, "app.py failed to import: ZeroDivisionError: division by zero"),
        ({"app.py": "def render(ctx, cfg)\n"}, "app.py failed to import: SyntaxError"),
        ({"app.py": "x = 1\n"}, "app.py has no render(ctx, cfg) function"),
        ({"app.py": "def render(ctx, cfg):\n    return ctx.empty()\nhandle = 5\n"},
         "app.py's handle must be a function"),
        # app.yaml: fields only a load checks - install takes these.
        ({"app.yaml": "id: echo\nname: ''\n" + SIZE}, "name must be a non-empty string"),
        ({"app.yaml": "id: echo\nreads: steps\n" + SIZE}, "reads must be a list of metric keys"),
        ({"app.yaml": "id: echo\nconfig: {goal: 1}\n" + SIZE}, "config must be a list of fields"),
        ({"app.yaml": "id: echo\nmetrics: [{key: steps}]\n" + SIZE}, "metric 'steps' is one of OpenFit's own keys"),
    ],
)
def test_update_validates_before_swapping(client, apps_dir, tmp_path, files, why):
    install(client, echo("one", **{"notes.txt": "v1\n"}))
    assert says(client, "echo") == "one"   # loaded, with its __pycache__
    before = tree(tmp_path)

    resp = update(client, "echo", {**echo("two"), **files})

    assert resp.status_code == 400
    error = resp.get_json()["error"]
    assert error.startswith("the update doesn't load, so 'echo' is unchanged: ") and why in error, error
    # The app is as it was: its folder untouched, still loading, still
    # drawn by its old code - and nothing left behind.
    assert tree(tmp_path) == before
    assert [a["id"] for a in client.get("/api/apps").get_json()] == ["echo"]
    assert says(client, "echo") == "one"


@pytest.mark.parametrize(
    "manifest, says_",
    [
        ("id: echo\nname: [unclosed\n" + SIZE, "app.yaml doesn't parse:"),
        ("- id: echo\n", "app.yaml must be a mapping"),
        ("", "app.yaml is empty"),
        ("id: echo\n", "no tile size:"),
        ("id: echo\nsizes: [{w: 9, h: 1}]\n", "sizes[0].w must be a whole number from 1 to 6"),
    ],
)
def test_an_update_whose_app_yaml_wont_do_is_refused_first(client, apps_dir, tmp_path, manifest, says_):
    install(client, echo("one"))
    assert says(client, "echo") == "one"
    before = tree(tmp_path)

    resp = update(client, "echo", {**echo("two"), "app.yaml": manifest})

    assert resp.status_code == 400
    assert resp.get_json()["error"].startswith(says_), resp.get_json()
    assert tree(tmp_path) == before
    assert says(client, "echo") == "one"


@pytest.mark.parametrize(
    "manifest, error",
    [
        ("id: other\n" + SIZE,
         "this updates 'echo', but the files are for 'other' — an update keeps the app's id "
         "(Duplicate makes a copy under a new one)"),
        ("id: Echo\n" + SIZE,
         "this updates 'echo', but the files are for 'Echo' — an update keeps the app's id "
         "(Duplicate makes a copy under a new one)"),
        ("name: Echo\n" + SIZE, "app.yaml needs an id: this updates 'echo', so id: echo"),
        ("id: 5\n" + SIZE, "id must be text: this updates 'echo', so id: 'echo'"),
    ],
)
def test_update_refuses_a_mismatched_id(client, apps_dir, tmp_path, manifest, error):
    install(client, echo("one"))
    other = {"app.yaml": "id: other\n" + SIZE, "app.py": "def render(ctx, cfg):\n    return ctx.empty('other')\n"}
    install(client, other)
    assert (says(client, "echo"), says(client, "other")) == ("one", "other")
    before = tree(tmp_path)

    resp = update(client, "echo", {**echo("two"), "app.yaml": manifest})

    assert resp.status_code == 400
    assert resp.get_json() == {"ok": False, "error": error}
    # Neither app changed: an update renames nothing, and never puts one
    # app's files in another's place.
    assert tree(tmp_path) == before
    assert [a["id"] for a in client.get("/api/apps").get_json()] == ["echo", "other"]
    assert (says(client, "echo"), says(client, "other")) == ("one", "other")


def test_update_refuses_an_app_that_isnt_installed(client, apps_dir, tmp_path):
    install(client, echo("one"))
    # A folder whose app doesn't load isn't installed, as uninstall has it.
    (apps_dir / "broken").mkdir()
    (apps_dir / "broken" / "app.yaml").write_text("id: broken\nname: [unclosed\n")
    client.get("/api/apps")
    before = tree(tmp_path)

    for app_id in ("nope", "broken"):
        resp = update(client, app_id, {"app.yaml": f"id: {app_id}\n" + SIZE})
        assert resp.status_code == 404
        assert resp.get_json() == {
            "ok": False, "error": f"'{app_id}' isn't installed — use Add Application to install it",
        }
    assert tree(tmp_path) == before


def test_update_removes_a_file_left_out_of_the_new_set(client, apps_dir):
    install(client, echo("one", **{"config.json": '{"goal": 1}\n', "helpers.py": "X = 1\n"}))
    assert apps.app_files(str(apps_dir / "echo")) == ["app.py", "app.yaml", "config.json", "helpers.py"]

    # A replace, not a merge: what isn't sent is gone - app.py included.
    assert update(client, "echo", {"app.yaml": echo("two")["app.yaml"], "helpers.py": "X = 2\n"}).status_code == 200

    assert sorted(os.listdir(apps_dir / "echo")) == ["app.yaml", "helpers.py"]
    assert (apps_dir / "echo" / "helpers.py").read_text() == "X = 2\n"
    [listed] = client.get("/api/apps").get_json()
    assert listed["files"] == ["app.yaml", "helpers.py"]
    assert says(client, "echo") == apps.NOTHING_TO_DRAW


def test_update_leaves_no_staging_or_trash_folders(client, apps_dir):
    install(client, echo("one"))
    says(client, "echo")

    assert update(client, "echo", echo("two")).status_code == 200
    assert hidden(apps_dir) == []
    # Refused after its files were staged - they don't load - or before.
    assert update(client, "echo", echo("three", **{"app.py": "1 / 0\n"})).status_code == 400
    assert update(client, "echo", {"app.yaml": "id: echo\n"}).status_code == 400
    assert hidden(apps_dir) == []
    assert sorted(os.listdir(apps_dir)) == ["echo"]
    assert says(client, "echo") == "two"


def test_update_of_a_hand_placed_app_replaces_the_folder_it_loaded_from(client, apps_dir):
    # A folder not named for its id, and an id the installer wouldn't take
    # (capitals, '_'): each is listed, so each can be updated - in place.
    (apps_dir / "my-steps").mkdir()
    for name, text in STEPS.items():
        (apps_dir / "my-steps" / name).write_text(text)
    (apps_dir / "My_App").mkdir()
    (apps_dir / "My_App" / "app.yaml").write_text("name: Mine\n" + SIZE)
    assert [a["id"] for a in client.get("/api/apps").get_json()] == ["My_App", "steps"]

    assert update(client, "steps", {**STEPS, "notes.txt": "hi\n"}).status_code == 200
    assert update(client, "My_App", {"app.yaml": "id: My_App\nname: Yours\n" + SIZE}).status_code == 200

    assert sorted(os.listdir(apps_dir)) == ["My_App", "my-steps"]
    assert (apps_dir / "my-steps" / "notes.txt").read_text() == "hi\n"
    assert [(a["id"], a["name"]) for a in client.get("/api/apps").get_json()] == [
        ("My_App", "Yours"), ("steps", "Steps"),
    ]


def test_update_of_a_linked_folder_never_touches_its_target(client, apps_dir, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    for name, text in echo("one").items():
        (target / name).write_text(text)
    (apps_dir / "echo").symlink_to(target)
    assert says(client, "echo") == "one"
    before = tree(target)

    assert update(client, "echo", echo("two")).status_code == 200

    # The link is replaced by a folder of the new files; what it pointed
    # to is never written to, nor removed.
    assert not (apps_dir / "echo").is_symlink()
    assert sorted(os.listdir(apps_dir)) == ["echo"]
    assert tree(target) == before
    assert (target / "app.py").read_text() == echo("one")["app.py"]
    assert says(client, "echo") == "two"


@pytest.mark.parametrize(
    "body",
    [None, {}, {"files": None}, {"files": {}}, {"files": ["app.yaml"]}, {"files": "id: echo"}],
)
def test_an_update_without_a_files_mapping_is_refused(client, apps_dir, tmp_path, body):
    install(client, echo("one"))
    before = tree(tmp_path)

    resp = client.put("/api/apps/echo", json=body)

    assert resp.status_code == 400
    assert resp.get_json() == {
        "ok": False, "error": 'expected {"files": {name: content}}, with at least an app.yaml',
    }
    assert tree(tmp_path) == before


@pytest.mark.parametrize(
    "files, error",
    [
        ({"../evil.py": "x = 1\n"}, "'../evil.py' isn't a plain file name: no '/', '\\' or '..'"),
        ({"a/b.py": "x = 1\n"}, "'a/b.py' isn't a plain file name: no '/', '\\' or '..'"),
        ({"nul\x00.py": "x = 1\n"}, "'nul\\x00.py' has a control character in it"),
        ({"notes.txt": 7}, "'notes.txt' must be text"),
    ],
)
def test_an_update_with_a_file_that_isnt_plain_text_is_refused(client, apps_dir, tmp_path, files, error):
    install(client, echo("one"))
    before = tree(tmp_path)

    resp = update(client, "echo", {**echo("two"), **files})

    assert resp.status_code == 400
    assert resp.get_json() == {"ok": False, "error": error}
    assert tree(tmp_path) == before


@pytest.mark.parametrize("segment", ["..", "%2e%2e", ".", ".hidden", "..%2Fx", "a%20b", "x%0A", "-x", "st%C3%A9ps"])
def test_a_traversal_or_non_id_segment_updates_nothing(client, apps_dir, tmp_path, segment):
    install(client, echo("one"))
    (apps_dir / ".hidden").mkdir()
    (apps_dir / ".hidden" / "app.yaml").write_text("id: hidden\n" + SIZE)
    says(client, "echo")
    before = tree(tmp_path)

    resp = client.put("/api/apps/" + segment, json={"files": {"app.yaml": "id: x\n" + SIZE}})

    # Refused by the id check, or never routed to it at all.
    assert resp.status_code in (400, 404, 405), segment
    if resp.status_code == 400:
        assert resp.get_json()["error"].startswith("not an app id:")
    assert tree(tmp_path) == before


def test_a_swap_that_fails_puts_the_old_app_back(client, apps_dir, monkeypatch, capsys):
    install(client, echo("one", **{"notes.txt": "v1\n"}))
    says(client, "echo")
    before = tree(apps_dir)
    real_rename = os.rename
    renames = []

    def failing_rename(src, dst):
        renames.append((os.path.basename(src), os.path.basename(dst)))
        # The old folder moves aside; the new one can't move in.
        if os.path.basename(src).startswith(".update-") and not os.path.basename(src).startswith(".update-old-"):
            raise OSError(28, "No space left on device")
        return real_rename(src, dst)

    monkeypatch.setattr(apps.os, "rename", failing_rename)
    resp = update(client, "echo", echo("two"))

    assert resp.status_code == 500
    assert resp.get_json() == {"ok": False, "error": "couldn't replace the app's files: No space left on device"}
    # Aside, the new one refused, then back: the app was never missing
    # for long, and is whole, on its old files.
    aside, staged = renames[0][1], renames[1][0]
    assert aside.startswith(".update-old-") and staged.startswith(".update-")
    assert renames == [("echo", aside), (staged, "echo"), (aside, "echo")]
    assert tree(apps_dir) == before
    assert says(client, "echo") == "one"
    assert "couldn't put" not in capsys.readouterr().out


def test_a_folder_that_cant_be_moved_aside_leaves_the_app_whole(client, apps_dir, monkeypatch):
    install(client, echo("one"))
    says(client, "echo")
    before = tree(apps_dir)

    def failing_rename(src, dst):
        raise OSError(13, "Permission denied")

    monkeypatch.setattr(apps.os, "rename", failing_rename)
    resp = update(client, "echo", echo("two"))

    assert resp.status_code == 500
    assert resp.get_json() == {"ok": False, "error": "couldn't replace the app's files: Permission denied"}
    assert tree(apps_dir) == before
    assert says(client, "echo") == "one"


def test_an_old_folder_that_cant_be_deleted_still_updates(client, apps_dir, monkeypatch, capsys):
    # The new files are in: the old ones, hidden, are only left over.
    install(client, echo("one"))

    def failing_rmtree(path, *args, **kwargs):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(apps.shutil, "rmtree", failing_rmtree)
    capsys.readouterr()
    resp = update(client, "echo", echo("two"))

    assert resp.status_code == 200
    [left] = hidden(apps_dir)
    assert left.startswith(".update-old-")
    assert apps.app_files(str(apps_dir / left)) == ["app.py", "app.yaml"]
    assert says(client, "echo") == "two"
    out = capsys.readouterr().out
    assert "[apps] updated echo, but couldn't delete" in out
    assert "skipping" not in out


def test_an_update_never_opens_the_database(client, apps_dir, monkeypatch):
    install(client, echo("one"))
    import main

    def no_db():
        raise AssertionError("an update opened the database")

    monkeypatch.setattr(main, "get_conn", no_db)
    assert update(client, "echo", echo("two")).status_code == 200


def test_update_app_logs_what_it_did(apps_dir, capsys):
    (apps_dir / "notes").mkdir()
    (apps_dir / "notes" / "app.yaml").write_text("id: notes\n" + SIZE)

    files = {"app.yaml": "id: notes\nname: Notes\n" + SIZE, "b.txt": "b", "a.txt": "a"}
    assert apps.update_app(str(apps_dir), "notes", files) == {"id": "notes", "has_app_py": False}
    assert "[apps] updated notes: a.txt, app.yaml, b.txt" in capsys.readouterr().out
    assert [a.name for a in apps.scan_apps(str(apps_dir))] == ["Notes"]
    assert os.listdir(apps_dir) == ["notes"]
