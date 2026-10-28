"""
Browse (B1): the app library, read from the shelf, and installed from it.

The Admin publishes the official apps to a shelf every user mounts
read-only at /library (A5): index.json, a link <source> onto
.sets/<source>-<commit12>/, and the app folders in it. shelf.py reads
it - list_shelf() for GET /api/library, read_app_files() for GET
/api/library/<source>/<app> - and POST /api/library/<source>/<app>/install
installs an app from the shelf's own files, never the page's copy.

Beside it, the three optional manifest fields Browse reads, which the
scanner checks too: description, requires (an app level, APP_LEVEL) and
replaces (B2 moves the data; until then, Browse says it's pending).

Every test gets its own temp apps folder and shelf, laid out exactly as
the Admin publishes one; nothing reads /data/apps or /library.
"""

import json
import os
from pathlib import Path

import pytest

import admin_ops
import apps
import shelf

ROOT = Path(__file__).parent.parent
LIBRARY = ROOT / "library"
COMMIT = "0123456789abcdef0123456789abcdef01234567"
SIZE = "sizes: [{w: 1, h: 1}]\n"


@pytest.fixture
def apps_dir(tmp_path, monkeypatch):
    """An empty apps folder, which the app is pointed at."""
    import main

    path = tmp_path / "apps"
    path.mkdir()
    monkeypatch.setitem(main.app.config, "APPS_DIR", str(path))
    return path


@pytest.fixture
def library(tmp_path, monkeypatch):
    """Where the shelf goes - nothing there until a test publishes one -
    which the app is pointed at."""
    import main

    path = tmp_path / "library"
    monkeypatch.setitem(main.app.config, "LIBRARY_DIR", str(path))
    return path


def manifest(app_id, name=None, extra=""):
    lines = [f"id: {app_id}\n"]
    if name is not None:
        lines.append(f"name: {json.dumps(name)}\n")
    return "".join(lines) + SIZE + extra


def app(app_id, name=None, extra="", **files):
    """A small app's files: its app.yaml, an app.py, and any others."""
    return {
        "app.yaml": manifest(app_id, name, extra),
        "app.py": "def render(ctx, cfg):\n    return ctx.empty('hello')\n",
        **files,
    }


def publish(root, sources, listed=None):
    """A shelf at `root`, as the Admin's publish_set() leaves one: each
    source's apps in .sets/<source>-<commit12>/<app>/, the relative link
    <source> onto that set, and index.json listing them (or `listed`,
    {source: [app names]}, when the index should say otherwise).

    sources: {source: {app: {file name: text or bytes}}}."""
    root.mkdir(exist_ok=True)
    index = {"published_at": "2026-10-28T12:00:00Z", "sources": {}}
    for source, shelf_apps in sources.items():
        name = f"{source}-{COMMIT[:12]}"
        folder = root / ".sets" / name
        folder.mkdir(parents=True)
        for app_name, files in shelf_apps.items():
            write(folder / app_name, files)
        (root / source).symlink_to(f".sets/{name}")
        names = listed[source] if listed and source in listed else sorted(shelf_apps)
        index["sources"][source] = {"commit": COMMIT, "apps": {a: {"sha256": "0" * 64} for a in names}}
    (root / "index.json").write_text(json.dumps(index, indent=2))
    return root


def write(folder, files):
    folder.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            path.write_bytes(body)
        else:
            path.write_text(body, encoding="utf-8")


def on_shelf(root, source, app_name):
    return root / source / app_name


def listed(library_dir, apps_dir, conn):
    return shelf.list_shelf(str(library_dir), str(apps_dir), conn)


def states(result):
    return {a["id"]: (a["state"], a["replaces_name"]) for a in result["apps"]}


def tree(root):
    """Every path under `root`, relative - to show what changed - but the
    __pycache__ a scan's import of an app.py leaves."""
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*") if "__pycache__" not in p.parts)


# ---------- the manifest's description, requires and replaces ----------


def load(tmp_path, text, folder="x"):
    """The app in a folder holding `text` as its app.yaml, loaded."""
    path = tmp_path / folder
    path.mkdir()
    (path / "app.yaml").write_text(text, encoding="utf-8")
    return apps.load_app(str(path))


def test_a_description_is_text_of_at_most_300_characters(tmp_path):
    assert load(tmp_path, "id: a\n" + SIZE).description is None
    long = "x" * 300
    assert load(tmp_path, f"id: b\ndescription: '  {long}  '\n" + SIZE, "b").description == long
    assert load(tmp_path, "id: c\ndescription: '   '\n" + SIZE, "c").description is None
    for i, value in enumerate(["x" * 301, "5", "[a, b]", "{a: 1}", "true", "2026-10-28"]):
        with pytest.raises(apps.AppError, match="description"):
            load(tmp_path, f"id: d{i}\ndescription: {value}\n" + SIZE, f"d{i}")


@pytest.mark.parametrize("value", ["0", "-1", "true", "false", "'2'", "1.5", "[1]", "null"])
def test_requires_is_a_whole_number_from_one(tmp_path, value):
    with pytest.raises(apps.AppError, match="requires must be a whole number from 1"):
        load(tmp_path, f"id: a\nrequires: {value}\n" + SIZE)


def test_requires_defaults_to_one_and_this_openfit_is_level_one(tmp_path):
    assert apps.APP_LEVEL == 1
    assert load(tmp_path, "id: a\n" + SIZE, "a").requires == 1
    assert load(tmp_path, "id: b\nrequires: 1\n" + SIZE, "b").requires == 1


LEVEL_2 = "needs OpenFit app level 2; this OpenFit is level 1"


def test_an_app_needing_a_newer_openfit_is_skipped_by_the_scanner(apps_dir, capsys):
    write(apps_dir / "newer", {"app.yaml": "id: newer\nrequires: 2\n" + SIZE})
    write(apps_dir / "fine", {"app.yaml": "id: fine\nrequires: 1\n" + SIZE})

    assert [a.id for a in apps.scan_apps(apps_dir)] == ["fine"]
    assert f"[apps] skipping {apps_dir / 'newer'}: {LEVEL_2}" in capsys.readouterr().out
    # Said first, whatever else a level-2 manifest has that this one can't read.
    with pytest.raises(apps.AppError, match=f"^{LEVEL_2}$"):
        load(apps_dir.parent, "id: newer2\nrequires: 2\nsizes: a new shape\n", "newer2")


def test_install_refuses_an_app_needing_a_newer_openfit(client, apps_dir):
    files = {"app.yaml": "id: newer\nrequires: 2\n" + SIZE}

    resp = client.post("/api/apps", json={"files": files})

    assert resp.status_code == 400
    assert resp.get_json() == {"ok": False, "error": LEVEL_2}
    with pytest.raises(apps.InstallError) as refused:
        apps.install_library_app(apps_dir, "openfit", files)
    assert (refused.value.status, str(refused.value)) == (400, LEVEL_2)
    bad = client.post("/api/apps", json={"files": {"app.yaml": "id: bad\nrequires: true\n" + SIZE}})
    assert bad.status_code == 400 and "requires must be a whole number" in bad.get_json()["error"]
    assert tree(apps_dir) == []


def test_an_update_to_an_app_needing_a_newer_openfit_is_refused(client, apps_dir):
    assert client.post("/api/apps", json={"files": {"app.yaml": "id: a\n" + SIZE}}).status_code == 201

    resp = client.put("/api/apps/a", json={"files": {"app.yaml": "id: a\nrequires: 2\n" + SIZE}})

    assert resp.status_code == 400 and resp.get_json()["error"] == LEVEL_2
    assert (apps_dir / "a" / "app.yaml").read_text() == "id: a\n" + SIZE


@pytest.mark.parametrize("value, why", [
    ("frms", "replaces must be a list"),
    ("{frms: 1}", "replaces must be a list"),
    ("[Bad Id]", r"replaces\[0\] isn't an app id: 'Bad Id'"),
    ("[ok, ../up]", r"replaces\[1\] isn't an app id: '../up'"),
    ("[5]", r"replaces\[0\] isn't an app id: 5"),
    ("[a, b, c, d, e, f]", "replaces lists at most 5 apps: it lists 6"),
    ("[a, b, a]", "replaces lists 'a' twice"),
    ("[frms, openfit.x]", "replaces can't name the app itself: 'openfit.x'"),
])
def test_bad_replaces_are_refused(tmp_path, value, why):
    # A library app's folder: its own id is openfit.x.
    with pytest.raises(apps.AppError, match=why):
        load(tmp_path, f"id: x\nreplaces: {value}\n" + SIZE, "openfit.x")


def test_replaces_lists_up_to_five_app_ids_of_either_shape(tmp_path):
    loaded = load(tmp_path, "id: x\nreplaces: [frms, My_App, acme.frms, a-b, c]\n" + SIZE, "openfit.x")
    assert loaded.replaces == ["frms", "My_App", "acme.frms", "a-b", "c"]
    assert load(tmp_path, "id: y\nreplaces: []\n" + SIZE, "openfit.y").replaces == []
    assert load(tmp_path, "id: z\n" + SIZE, "openfit.z").replaces == []


def test_a_custom_app_naming_its_own_bare_id_still_loads(tmp_path):
    # FRMS's library files pasted in as a custom app: replaces names frms,
    # its own id here - but a custom app's replaces is never acted on, so
    # it loads, as the frms installed before the library did.
    assert load(tmp_path, "id: frms\nreplaces: [frms]\n" + SIZE, "frms").replaces == ["frms"]


def test_summary_is_unchanged_by_the_new_fields(tmp_path):
    loaded = load(tmp_path, "id: x\ndescription: Hi\nrequires: 1\nreplaces: [y]\n" + SIZE)
    assert set(loaded.summary()) == {
        "id", "name", "library", "default_size", "sizes", "reads", "config", "metrics", "files",
    }


def test_frms_loads_with_its_new_fields(apps_dir):
    frms = apps.load_app(str(LIBRARY / "frms"))
    assert frms.requires == 1 and frms.replaces == ["frms"]
    assert isinstance(frms.description, str) and 0 < len(frms.description) <= apps.MAX_DESCRIPTION
    # Installed from the library, too: openfit.frms replaces frms.
    files = {p.name: p.read_text(encoding="utf-8") for p in (LIBRARY / "frms").iterdir() if p.is_file()}
    apps.install_library_app(apps_dir, "openfit", files)
    [installed] = apps.scan_apps(apps_dir)
    assert (installed.id, installed.replaces, installed.description) == ("openfit.frms", ["frms"], frms.description)


# ---------- list_shelf ----------


def test_no_shelf_is_not_available(tmp_path, conn, apps_dir):
    assert listed(tmp_path / "missing", apps_dir, conn) == {"available": False, "apps": []}


@pytest.mark.parametrize("index", [
    None, "", "not json", "[]", '{"sources": []}', '{"published_at": "x"}', b"\xff\xfe",
])
def test_a_shelf_whose_index_doesnt_read_is_not_available(tmp_path, conn, apps_dir, index):
    root = publish(tmp_path / "library", {"openfit": {"a": app("a")}})
    if index is None:
        (root / "index.json").unlink()
    elif isinstance(index, bytes):
        (root / "index.json").write_bytes(index)
    else:
        (root / "index.json").write_text(index)
    assert listed(root, apps_dir, conn) == {"available": False, "apps": []}


def test_an_index_that_is_a_link_is_not_read(tmp_path, conn, apps_dir):
    root = publish(tmp_path / "library", {"openfit": {"a": app("a")}})
    real = tmp_path / "elsewhere.json"
    (root / "index.json").rename(real)
    (root / "index.json").symlink_to(real)
    assert listed(root, apps_dir, conn) == {"available": False, "apps": []}


def test_an_empty_shelf_is_available_with_no_apps(tmp_path, conn, apps_dir):
    root = publish(tmp_path / "library", {"openfit": {}})
    assert listed(root, apps_dir, conn) == {"available": True, "apps": []}


def test_the_shelf_lists_each_app_as_browse_shows_it(tmp_path, conn, apps_dir):
    root = publish(tmp_path / "library", {"openfit": {
        "walk": app("walk", "Walk", "description: '  Counts your steps.  '\n", **{"notes.txt": "n\n"}),
    }})

    assert listed(root, apps_dir, conn) == {"available": True, "apps": [{
        "id": "openfit.walk", "source": "openfit", "app": "walk", "name": "Walk",
        "description": "Counts your steps.", "files": ["app.py", "app.yaml", "notes.txt"],
        "state": "available", "replaces_name": None,
    }]}


def test_the_shelf_is_sorted_by_name_then_id_across_sources(tmp_path, conn, apps_dir):
    root = publish(tmp_path / "library", {
        "openfit": {"zed": app("zed", "alpha"), "abe": app("abe", "Beta"), "mid": app("mid", "Alpha")},
        "acme": {"zed": app("zed", "ALPHA"), "plain": app("plain")},
    })

    result = listed(root, apps_dir, conn)

    assert [(a["id"], a["name"]) for a in result["apps"]] == [
        ("acme.zed", "ALPHA"), ("openfit.mid", "Alpha"), ("openfit.zed", "alpha"),
        ("openfit.abe", "Beta"), ("acme.plain", "plain"),
    ]
    # A name defaults to the app's own.
    assert result["apps"][-1]["source"] == "acme"


def test_each_state_is_the_first_that_applies(tmp_path, conn, apps_dir):
    root = publish(tmp_path / "library", {"openfit": {
        "fresh": app("fresh"),
        "mine": app("mine"),
        "nested": app("nested", **{"assets/logo.txt": "x"}),
        "newer": app("newer", extra="requires: 2\n"),
        "heir": app("heir", extra="replaces: [old]\n"),
        "heir2": app("heir2", extra="replaces: [gone]\n"),
        # Installed already beats everything after it: folders, a level,
        # a replacement still pending.
        "both": app("both", extra="requires: 2\nreplaces: [old]\n", **{"sub/x.txt": "x"}),
    }})
    # Installed: from the library, as Browse installs it - and a library
    # app hand-placed on the volume counts too.
    apps.install_library_app(apps_dir, "openfit", app("mine"))
    write(apps_dir / "openfit.both", app("both"))
    # A replaced app still installed, by its own name...
    write(apps_dir / "old", app("old", "My old app"))
    # ... and one uninstalled whose data is still here, by its id.
    conn.execute(
        "INSERT INTO sessions (id, source, kind, \"start\", \"end\", summary_json, synced_at) "
        "VALUES ('app:gone:sleep:x', 'app:gone', 'sleep', '2026-10-01T00:00:00Z', NULL, NULL, NULL)"
    )

    assert states(listed(root, apps_dir, conn)) == {
        "openfit.fresh": ("available", None),
        "openfit.mine": ("installed", None),
        "openfit.both": ("installed", None),
        "openfit.nested": ("unsupported", None),
        "openfit.newer": ("needs_update", None),
        "openfit.heir": ("replaces_pending", "My old app"),
        "openfit.heir2": ("replaces_pending", "gone"),
    }


@pytest.mark.parametrize("table, row", [
    ("metrics", "INSERT INTO metrics (date, source, metric, value, unit, synced_at) "
                "VALUES ('2026-10-01', 'app:old', 'steps', 1, 'count', NULL)"),
    ("sessions", "INSERT INTO sessions (id, source, kind, \"start\", \"end\", summary_json, synced_at) "
                 "VALUES ('app:old:workout:x', 'app:old', 'workout', '2026-10-01T00:00:00Z', NULL, NULL, NULL)"),
    ("samples", "INSERT INTO samples (source, metric, ts, value, unit, synced_at) "
                "VALUES ('app:old', 'heart_rate', '2026-10-01T00:00:00Z', 60, 'bpm', '2026-10-01')"),
])
def test_a_replacement_is_pending_while_the_old_apps_data_is_here(tmp_path, conn, apps_dir, table, row):
    root = publish(tmp_path / "library", {"openfit": {"heir": app("heir", extra="replaces: [nope, old]\n")}})
    assert states(listed(root, apps_dir, conn)) == {"openfit.heir": ("available", None)}

    conn.execute(row)

    assert states(listed(root, apps_dir, conn)) == {"openfit.heir": ("replaces_pending", "old")}


def test_an_installed_replacement_is_named_before_one_with_data_alone(tmp_path, conn, apps_dir):
    root = publish(tmp_path / "library", {"openfit": {"heir": app("heir", extra="replaces: [gone, old]\n")}})
    conn.execute("INSERT INTO metrics (date, source, metric, value, unit, synced_at) "
                 "VALUES ('2026-10-01', 'app:gone', 'steps', 1, 'count', NULL)")
    # Present but broken - it doesn't load - so named by its id.
    write(apps_dir / "old", {"app.yaml": "id: old\n"})

    assert states(listed(root, apps_dir, conn)) == {"openfit.heir": ("replaces_pending", "old")}


def test_another_sources_data_never_makes_a_replacement_pending(tmp_path, conn, apps_dir):
    root = publish(tmp_path / "library", {"openfit": {"heir": app("heir", extra="replaces: [old]\n")}})
    # The old app's id as a device's source, or another app's: not app:old.
    conn.execute("INSERT INTO metrics (date, source, metric, value, unit, synced_at) "
                 "VALUES ('2026-10-01', 'old', 'steps', 1, 'count', NULL)")
    conn.execute("INSERT INTO metrics (date, source, metric, value, unit, synced_at) "
                 "VALUES ('2026-10-01', 'app:old-2', 'steps', 1, 'count', NULL)")
    assert states(listed(root, apps_dir, conn)) == {"openfit.heir": ("available", None)}


@pytest.mark.parametrize("files, why", [
    ({"app.yaml": "id: [unclosed\n"}, "can't read app.yaml"),
    ({"app.yaml": "- a list\n"}, "app.yaml must be a mapping"),
    ({"app.yaml": "id: other\n" + SIZE}, "its id must be its folder's name, 'bad': 'other'"),
    ({"app.yaml": "name: No id\n" + SIZE}, "its id must be its folder's name, 'bad': None"),
    ({"app.yaml": "id: bad\nname: '  '\n" + SIZE}, "name must be a non-empty string"),
    ({"app.yaml": "id: bad\ndescription: " + "x" * 301 + "\n" + SIZE}, "description must be at most 300"),
    ({"app.yaml": "id: bad\nrequires: 0\n" + SIZE}, "requires must be a whole number from 1"),
    ({"app.yaml": "id: bad\nreplaces: [openfit.bad]\n" + SIZE}, "replaces can't name the app itself"),
    ({"app.yaml": "id: bad\nreplaces: frms\n" + SIZE}, "replaces must be a list"),
    ({"app.yaml": b"id: bad\nname: \xff\n"}, "can't read app.yaml"),
    ({"app.py": "x = 1\n"}, "can't read app.yaml"),
    ({"app.yaml": "id: bad\n" + "#" * shelf.MAX_MANIFEST_BYTES + "\n"}, "app.yaml is over 256 KB"),
])
def test_an_app_whose_manifest_doesnt_read_or_check_is_left_out_and_logged(
        tmp_path, conn, apps_dir, capsys, files, why):
    root = publish(tmp_path / "library", {"openfit": {"bad": files, "good": app("good")}})

    result = listed(root, apps_dir, conn)

    assert [a["id"] for a in result["apps"]] == ["openfit.good"]
    out = capsys.readouterr().out
    assert "[library] skipping openfit.bad: " in out and why in out, out


def test_an_app_the_index_doesnt_list_isnt_on_the_shelf(tmp_path, conn, apps_dir):
    root = publish(tmp_path / "library", {"openfit": {"a": app("a"), "b": app("b")}},
                   listed={"openfit": ["a", "missing"]})
    assert [a["id"] for a in listed(root, apps_dir, conn)["apps"]] == ["openfit.a"]


def test_only_an_apps_own_top_level_files_are_listed(tmp_path, conn, apps_dir):
    root = publish(tmp_path / "library", {"openfit": {"a": app("a", **{
        ".env": "SECRET=1\n", "old.pyc": b"\x00", "data.json": "{}\n",
    })}})
    write(on_shelf(root, "openfit", "a") / "__pycache__", {"app.cpython-312.pyc": b"\x00"})
    write(on_shelf(root, "openfit", "a") / ".hidden", {"x.txt": "x"})

    [entry] = listed(root, apps_dir, conn)["apps"]

    # __pycache__ and a dot-folder aren't folders of the app's: it's
    # still one this OpenFit installs.
    assert (entry["files"], entry["state"]) == (["app.py", "app.yaml", "data.json"], "available")


# ---------- path safety ----------


def test_a_source_linked_outside_the_library_is_skipped(tmp_path, conn, apps_dir, capsys):
    outside = tmp_path / "outside"
    write(outside / "evil", app("evil"))
    root = publish(tmp_path / "library", {"openfit": {"good": app("good")}})
    (root / "acme").symlink_to(outside)
    index = json.loads((root / "index.json").read_text())
    index["sources"]["acme"] = {"commit": COMMIT, "apps": {"evil": {"sha256": "0" * 64}}}
    (root / "index.json").write_text(json.dumps(index))

    result = listed(root, apps_dir, conn)

    assert [a["id"] for a in result["apps"]] == ["openfit.good"]
    assert "[library] skipping the source 'acme': its folder isn't a folder inside the library" in capsys.readouterr().out
    with pytest.raises(apps.LibraryError) as refused:
        shelf.read_app_files(str(root), "acme", "evil")
    assert refused.value.status == 404


def test_a_source_linked_to_the_shelf_itself_is_skipped(tmp_path, conn, apps_dir):
    root = publish(tmp_path / "library", {"openfit": {"good": app("good")}})
    (root / "loop").symlink_to(".")
    index = json.loads((root / "index.json").read_text())
    index["sources"]["loop"] = {"commit": COMMIT, "apps": {"openfit": {}}}
    (root / "index.json").write_text(json.dumps(index))
    assert [a["id"] for a in listed(root, apps_dir, conn)["apps"]] == ["openfit.good"]


@pytest.mark.parametrize("source", ["Open", "open-fit", "1fit", "..", ".sets", "a/b", ""])
def test_a_source_the_index_names_badly_is_skipped(tmp_path, conn, apps_dir, source):
    root = publish(tmp_path / "library", {"openfit": {"good": app("good")}})
    index = json.loads((root / "index.json").read_text())
    index["sources"][source] = {"commit": COMMIT, "apps": {"good": {}}}
    (root / "index.json").write_text(json.dumps(index))
    assert [a["id"] for a in listed(root, apps_dir, conn)["apps"]] == ["openfit.good"]


@pytest.mark.parametrize("entry", ["not a mapping", {"commit": COMMIT}, {"apps": []}])
def test_a_source_whose_entry_doesnt_read_is_skipped(tmp_path, conn, apps_dir, entry):
    root = publish(tmp_path / "library", {"openfit": {"good": app("good")}, "acme": {"a": app("a")}})
    index = json.loads((root / "index.json").read_text())
    index["sources"]["acme"] = entry
    (root / "index.json").write_text(json.dumps(index))
    result = listed(root, apps_dir, conn)
    assert result["available"] and [a["id"] for a in result["apps"]] == ["openfit.good"]


def test_an_app_folder_linked_outside_is_skipped(tmp_path, conn, apps_dir, capsys):
    outside = tmp_path / "outside"
    write(outside / "evil", app("evil"))
    root = publish(tmp_path / "library", {"openfit": {"good": app("good")}}, listed={"openfit": ["evil", "good"]})
    (on_shelf(root, "openfit", "evil")).symlink_to(outside / "evil")

    assert [a["id"] for a in listed(root, apps_dir, conn)["apps"]] == ["openfit.good"]
    assert "[library] skipping openfit.evil: its folder isn't a folder inside the library" in capsys.readouterr().out
    with pytest.raises(apps.LibraryError) as refused:
        shelf.read_app_files(str(root), "openfit", "evil")
    assert refused.value.status == 404


@pytest.mark.parametrize("name", ["../up", "Bad_Name", "-dash", ".dotted", "a/b"])
def test_an_app_the_index_names_badly_is_skipped(tmp_path, conn, apps_dir, name):
    root = publish(tmp_path / "library", {"openfit": {"good": app("good")}}, listed={"openfit": [name, "good"]})
    assert [a["id"] for a in listed(root, apps_dir, conn)["apps"]] == ["openfit.good"]


def test_a_file_that_is_a_link_is_left_out_and_never_read(tmp_path, conn, apps_dir, monkeypatch):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET")
    root = publish(tmp_path / "library", {"openfit": {"a": app("a")}})
    folder = on_shelf(root, "openfit", "a")
    (folder / "leak.txt").symlink_to(secret)
    (folder / "inside.txt").symlink_to(folder / "app.py")  # a link, even to its own file
    os.mkfifo(folder / "pipe")

    [entry] = listed(root, apps_dir, conn)["apps"]
    files = shelf.read_app_files(str(root), "openfit", "a")

    assert entry["files"] == ["app.py", "app.yaml"]
    assert sorted(files) == ["app.py", "app.yaml"]
    # And should one be listed - swapped in after the listing - it's
    # refused as it's opened, never followed.
    real = shelf._listing
    monkeypatch.setattr(shelf, "_listing", lambda f: (real(f)[0] + ["leak.txt"], False))
    with pytest.raises(apps.LibraryError) as refused:
        shelf.read_app_files(str(root), "openfit", "a")
    assert refused.value.status == 404 and "leak.txt" in str(refused.value)
    assert "TOP SECRET" not in str(refused.value)
    monkeypatch.setattr(shelf, "_listing", lambda f: (real(f)[0] + ["pipe"], False))
    with pytest.raises(apps.LibraryError):
        shelf.read_app_files(str(root), "openfit", "a")


def test_an_app_yaml_that_is_a_link_leaves_the_app_out(tmp_path, conn, apps_dir):
    outside = tmp_path / "outside.yaml"
    outside.write_text("id: a\n" + SIZE)
    root = publish(tmp_path / "library", {"openfit": {"a": {"app.py": "x = 1\n"}, "good": app("good")}})
    (on_shelf(root, "openfit", "a") / "app.yaml").symlink_to(outside)
    assert [a["id"] for a in listed(root, apps_dir, conn)["apps"]] == ["openfit.good"]


# ---------- read_app_files ----------


def test_an_apps_files_are_read_as_published(tmp_path):
    files = app("a", **{"notes.txt": "crlf\r\nline\n", "ünï.txt": "ü\n"})
    root = publish(tmp_path / "library", {"openfit": {"a": files}})
    assert shelf.read_app_files(str(root), "openfit", "a") == files


def test_a_file_that_isnt_text_is_refused_by_name(tmp_path):
    root = publish(tmp_path / "library", {"openfit": {"a": app("a", **{"logo.png": b"\x89PNG\xff"})}})
    with pytest.raises(apps.LibraryError) as refused:
        shelf.read_app_files(str(root), "openfit", "a")
    assert refused.value.status == 415 and "'logo.png' in openfit.a isn't text" in str(refused.value)


def test_an_app_over_the_admins_limits_isnt_read(tmp_path, monkeypatch):
    root = publish(tmp_path / "library", {"openfit": {"a": app("a", **{"big.txt": "x" * 100})}})
    monkeypatch.setattr(shelf, "MAX_APP_BYTES", 120)
    with pytest.raises(apps.LibraryError) as refused:
        shelf.read_app_files(str(root), "openfit", "a")
    assert refused.value.status == 413
    monkeypatch.setattr(shelf, "MAX_APP_BYTES", 10 ** 6)
    monkeypatch.setattr(shelf, "MAX_APP_FILES", 2)
    with pytest.raises(apps.LibraryError) as refused:
        shelf.read_app_files(str(root), "openfit", "a")
    assert refused.value.status == 413


# ---------- the routes ----------


def test_get_library_says_when_there_is_no_shelf(client, apps_dir, library):
    resp = client.get("/api/library")
    assert resp.status_code == 200
    assert resp.get_json() == {"available": False, "apps": []}


def test_get_library_lists_the_shelf(client, apps_dir, library):
    publish(library, {"openfit": {"walk": app("walk", "Walk", "description: Steps.\n"), "box": app("box")}})
    apps.install_library_app(apps_dir, "openfit", app("box"))

    resp = client.get("/api/library")

    assert resp.status_code == 200
    assert resp.get_json() == {"available": True, "apps": [
        {"id": "openfit.box", "source": "openfit", "app": "box", "name": "box", "description": None,
         "files": ["app.py", "app.yaml"], "state": "installed", "replaces_name": None},
        {"id": "openfit.walk", "source": "openfit", "app": "walk", "name": "Walk", "description": "Steps.",
         "files": ["app.py", "app.yaml"], "state": "available", "replaces_name": None},
    ]}


def test_get_library_app_serves_its_files_app_yaml_then_app_py_then_by_name(client, apps_dir, library):
    files = app("walk", "Walk", **{"Zeta.txt": "z\n", "config.json": "{}\n", "a.txt": "a\n"})
    publish(library, {"openfit": {"walk": files}})

    resp = client.get("/api/library/openfit/walk")

    assert resp.status_code == 200
    assert resp.get_json() == {"id": "openfit.walk", "name": "Walk", "files": [
        {"name": "app.yaml", "text": files["app.yaml"]}, {"name": "app.py", "text": files["app.py"]},
        {"name": "Zeta.txt", "text": "z\n"}, {"name": "a.txt", "text": "a\n"}, {"name": "config.json", "text": "{}\n"},
    ]}


def test_get_library_app_without_an_app_py(client, apps_dir, library):
    publish(library, {"openfit": {"bare": {"app.yaml": manifest("bare"), "b.txt": "b", "a.txt": "a"}}})
    names = [f["name"] for f in client.get("/api/library/openfit/bare").get_json()["files"]]
    assert names == ["app.yaml", "a.txt", "b.txt"]


@pytest.mark.parametrize("path, status", [
    ("/api/library/Open/walk", 400), ("/api/library/open-fit/walk", 400), ("/api/library/1fit/walk", 400),
    ("/api/library/openfit/Walk", 400), ("/api/library/openfit/my_app", 400), ("/api/library/openfit/..", 400),
    ("/api/library/openfit/-x", 400), ("/api/library/openfit/missing", 404), ("/api/library/acme/walk", 404),
])
def test_get_library_app_checks_the_names_then_the_shelf(client, apps_dir, library, path, status):
    publish(library, {"openfit": {"walk": app("walk")}})
    resp = client.get(path)
    assert resp.status_code == status, resp.get_json()
    assert resp.get_json()["ok"] is False
    install = client.post(path + "/install")
    assert install.status_code == status, install.get_json()
    assert tree(apps_dir) == []


def test_get_library_app_with_no_shelf_is_404(client, apps_dir, library):
    assert client.get("/api/library/openfit/walk").status_code == 404
    assert client.post("/api/library/openfit/walk/install").status_code == 404


def test_get_library_app_refuses_a_file_that_isnt_text(client, apps_dir, library):
    publish(library, {"openfit": {"walk": app("walk", **{"logo.png": b"\x89PNG\xff"})}})
    resp = client.get("/api/library/openfit/walk")
    assert resp.status_code == 415 and "'logo.png'" in resp.get_json()["error"]


def test_install_from_the_library_installs_the_shelfs_own_files(client, apps_dir, library):
    files = app("walk", "Walk", **{"config.json": '{"stride": 0.75}\n', "notes.txt": "crlf\r\n"})
    publish(library, {"openfit": {"walk": files}})

    resp = client.post("/api/library/openfit/walk/install")

    assert resp.status_code == 201
    assert resp.get_json() == {"ok": True, "id": "openfit.walk", "has_app_py": True}
    folder = apps_dir / "openfit.walk"
    assert sorted(p.name for p in folder.iterdir()) == sorted(files)
    shelved = on_shelf(library, "openfit", "walk")
    for name in files:
        assert (folder / name).read_bytes() == (shelved / name).read_bytes(), name
    listed_apps = client.get("/api/apps").get_json()
    assert [(a["id"], a["name"], a["library"]) for a in listed_apps] == [("openfit.walk", "Walk", "openfit")]
    # And now it's installed.
    assert client.get("/api/library").get_json()["apps"][0]["state"] == "installed"


@pytest.mark.parametrize("kind", ["json", "form", "junk"])
def test_install_from_the_library_ignores_the_body(client, apps_dir, library, kind):
    files = app("walk", "Walk")
    publish(library, {"openfit": {"walk": files}})
    forged = {"app.yaml": "id: walk\nname: Forged\n" + SIZE, "app.py": "import os  # forged\n", "extra.txt": "x"}
    if kind == "json":
        resp = client.post("/api/library/openfit/walk/install", json={"files": forged})
    elif kind == "form":
        resp = client.post("/api/library/openfit/walk/install", data={"files": json.dumps(forged)})
    else:
        resp = client.post("/api/library/openfit/walk/install", data=b"\x00not json", content_type="application/json")

    assert resp.status_code == 201, resp.get_json()
    folder = apps_dir / "openfit.walk"
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == {
        name: (on_shelf(library, "openfit", "walk") / name).read_bytes() for name in files
    }


def test_install_from_the_library_refuses_each_state_but_available(client, conn, apps_dir, library):
    publish(library, {"openfit": {
        "mine": app("mine", "Mine"),
        "nested": app("nested", **{"assets/x.txt": "x"}),
        "newer": app("newer", extra="requires: 2\n"),
        "heir": app("heir", "Heir", extra="replaces: [old]\n"),
        "heir2": app("heir2", "Heir 2", extra="replaces: [gone]\n"),
    }})
    apps.install_library_app(apps_dir, "openfit", app("mine", "Mine"))
    write(apps_dir / "old", app("old", "My old app"))
    conn.execute("INSERT INTO metrics (date, source, metric, value, unit, synced_at) "
                 "VALUES ('2026-10-01', 'app:gone', 'steps', 1, 'count', NULL)")
    conn.commit()
    before = tree(apps_dir)

    said = {}
    for name in ("mine", "nested", "newer", "heir", "heir2"):
        resp = client.post(f"/api/library/openfit/{name}/install")
        assert resp.status_code == 409, name
        body = resp.get_json()
        assert body["ok"] is False
        said[name] = body["error"]

    assert said == {
        "mine": "Already installed",
        "nested": "It has folders inside it, which this OpenFit can't install yet.",
        "newer": "Needs a newer OpenFit.",
        "heir": "Heir replaces your My old app. Moving it across comes in a coming update.",
        "heir2": "Heir 2 replaces your gone. Moving it across comes in a coming update.",
    }
    assert tree(apps_dir) == before


def test_a_custom_app_of_the_same_bare_id_doesnt_block_the_library_install(client, apps_dir, library):
    # walk and openfit.walk are two apps: only a declared replaces ties them.
    publish(library, {"openfit": {"walk": app("walk")}})
    write(apps_dir / "walk", app("walk"))
    assert client.post("/api/library/openfit/walk/install").status_code == 201
    assert sorted(a["id"] for a in client.get("/api/apps").get_json()) == ["openfit.walk", "walk"]


# ---------- end to end: the Admin's own publish, then Browse ----------


def test_frms_published_by_the_admin_is_browsed_and_installed_byte_for_byte(client, conn, apps_dir, library):
    library.mkdir()
    admin_ops.publish_set(str(LIBRARY), str(library), "openfit", COMMIT, "2026-10-28T12:00:00Z", log=lambda m: None)

    [frms] = client.get("/api/library").get_json()["apps"]
    assert (frms["id"], frms["name"], frms["state"]) == ("openfit.frms", "FRMS", "available")
    assert frms["description"] == apps.load_app(str(LIBRARY / "frms")).description
    published = sorted(p.name for p in (LIBRARY / "frms").iterdir() if p.is_file())
    assert frms["files"] == published
    shown = client.get("/api/library/openfit/frms").get_json()
    assert [f["name"] for f in shown["files"]][:2] == ["app.yaml", "app.py"]

    assert client.post("/api/library/openfit/frms/install").status_code == 201
    folder = apps_dir / "openfit.frms"
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == {
        name: (LIBRARY / "frms" / name).read_bytes() for name in published
    }
    assert client.get("/api/library").get_json()["apps"][0]["state"] == "installed"


def test_frms_on_the_shelf_waits_for_b2_while_a_custom_frms_is_installed(client, apps_dir, library):
    library.mkdir()
    admin_ops.publish_set(str(LIBRARY), str(library), "openfit", COMMIT, "2026-10-28T12:00:00Z", log=lambda m: None)
    write(apps_dir / "frms", {"app.yaml": "id: frms\nname: FRMS\n" + SIZE})

    [frms] = client.get("/api/library").get_json()["apps"]
    assert (frms["state"], frms["replaces_name"]) == ("replaces_pending", "FRMS")
    resp = client.post("/api/library/openfit/frms/install")
    assert resp.status_code == 409
    assert resp.get_json()["error"] == "FRMS replaces your FRMS. Moving it across comes in a coming update."
    assert sorted(os.listdir(apps_dir)) == ["frms"]
