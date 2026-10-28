"""
Library app ids: installing from a library, and library apps read-only.

An app from a library is named <source>.<name> - openfit.frms. The
library assigns the source, never the app: a library app's own app.yaml
keeps its bare id (id: frms), install_library_app() adds the source, and
the source lives in the installed folder's name alone, so the files are
the library's own, byte for byte. A dot in an id always means installed
from a library - a pasted or duplicated app (install_app()) can never
take one - and a library app is read-only here: update_app() refuses it,
403. It uninstalls as any app does, its data kept, and writes under its
own source, app:openfit.frms, as any app writes under app:<id>.

Every test gets its own temp apps folder; nothing reads /data/apps.
"""

import os
import shutil
from datetime import date
from pathlib import Path

import pytest

import apps
from metrics import WEIGHT_KG

LIBRARY = Path(__file__).parent.parent / "library"
# The test apps, installed here from a library all the same: what's tested
# is the id mechanics, not what the official library holds (FRMS alone).
FIXTURE_APPS = Path(__file__).parent / "fixtures" / "apps"

TODAY = date.today().isoformat()

SIZE = "sizes: [{w: 1, h: 1}]\n"


@pytest.fixture
def apps_dir(tmp_path, monkeypatch):
    """An empty apps folder, which the app is pointed at."""
    import main

    path = tmp_path / "apps"
    path.mkdir()
    monkeypatch.setitem(main.app.config, "APPS_DIR", str(path))
    return path


def library_files(name):
    """A library app's files, as Browse will send them: each plain file
    directly in its folder, as text - exactly the bytes it holds."""
    folder = LIBRARY / name if name == "frms" else FIXTURE_APPS / name
    return {p.name: p.read_bytes().decode("utf-8") for p in sorted(folder.iterdir()) if p.is_file()}


def install(apps_dir, name, source="openfit"):
    """library/<name> - or a test app, tests/fixtures/apps/<name> - installed
    from the `source` library."""
    return apps.install_library_app(apps_dir, source, library_files(name))


def listing(apps_dir):
    """What's in the apps folder, hidden staging folders and all."""
    return sorted(os.listdir(apps_dir))


def rows(conn):
    """Every reading in metrics, (date, source, metric, value, unit)."""
    return [
        tuple(r) for r in conn.execute(
            "SELECT date, source, metric, value, unit FROM metrics ORDER BY date, source, metric"
        )
    ]


def act(client, app_id, action, payload):
    return client.post(f"/api/app/{app_id}/action", json={"action": action, "payload": payload})


# ---------- the id shapes ----------


@pytest.mark.parametrize("value, is_id, library", [
    # A custom app's: the scanner's APP_ID, a hand-placed app's capitals
    # and '_' included.
    ("frms", True, None), ("workout-log", True, None), ("My_App", True, None),
    # A library app's: <source>.<name>.
    ("openfit.frms", True, "openfit"), ("acme2.workout-log", True, "acme2"), ("x.y", True, "x"),
    # Neither: a source that isn't lowercase letters and digits starting
    # with a letter, a name not in INSTALL_ID's form, two dots, no part.
    ("OpenFit.frms", False, None), ("open-fit.frms", False, None), ("1fit.frms", False, None),
    ("openfit.Frms", False, None), ("openfit.my_app", False, None), ("openfit.-x", False, None),
    ("openfit.frms.x", False, None), (".frms", False, None), ("openfit.", False, None),
    ("openfit.frms\n", False, None), ("", False, None), (None, False, None), (5, False, None),
])
def test_an_app_id_is_a_custom_apps_or_a_library_apps(value, is_id, library):
    assert apps.is_app_id(value) is is_id
    assert apps.library_of(value) == library


# ---------- installing from a library: install_library_app() ----------


def test_a_library_install_files_the_app_under_its_source_byte_for_byte(apps_dir):
    installed = install(apps_dir, "frms")

    assert installed == {"id": "openfit.frms", "has_app_py": True}
    assert listing(apps_dir) == ["openfit.frms"]
    # Every file the library's own, byte for byte: the source is in the
    # folder's name alone, never written into a file.
    folder = apps_dir / "openfit.frms"
    library = LIBRARY / "frms"
    assert sorted(p.name for p in folder.iterdir()) == sorted(p.name for p in library.iterdir() if p.is_file())
    for p in library.iterdir():
        if p.is_file():
            assert (folder / p.name).read_bytes() == p.read_bytes(), p.name
    assert "id: frms\n" in (folder / "app.yaml").read_text()
    # The scanner lists it under its full id, from the library it names.
    [app] = apps.scan_apps(apps_dir)
    assert (app.id, app.name) == ("openfit.frms", "FRMS")
    assert app.summary()["library"] == "openfit"


def test_a_library_install_logs_where_it_came_from(apps_dir, capsys):
    install(apps_dir, "weight")

    assert "[apps] installed openfit.weight from the openfit library: app.py, app.yaml" in capsys.readouterr().out


def test_installing_a_library_app_twice_is_a_409(apps_dir):
    install(apps_dir, "frms")
    before = {p.name: p.read_bytes() for p in (apps_dir / "openfit.frms").iterdir()}

    with pytest.raises(apps.InstallError) as refused:
        install(apps_dir, "frms")

    assert refused.value.status == 409
    assert str(refused.value) == apps.taken_message("openfit.frms")
    assert listing(apps_dir) == ["openfit.frms"]
    assert {p.name: p.read_bytes() for p in (apps_dir / "openfit.frms").iterdir()} == before


@pytest.mark.parametrize("source", ["OpenFit", "open-fit", "1fit", "", "open.fit", "openfit ", None])
def test_a_source_that_isnt_one_is_refused_and_nothing_is_written(apps_dir, source):
    with pytest.raises(apps.InstallError) as refused:
        install(apps_dir, "frms", source=source)

    assert refused.value.status == 400
    assert str(refused.value) == (
        f"a library source is lowercase letters and digits, starting with a letter: {source!r}"
    )
    assert listing(apps_dir) == []


def test_a_library_manifest_that_already_carries_a_source_is_refused(apps_dir):
    files = library_files("frms")
    files["app.yaml"] = files["app.yaml"].replace("id: frms\n", "id: openfit.frms\n")

    with pytest.raises(apps.InstallError) as refused:
        apps.install_library_app(apps_dir, "openfit", files)

    # Its own id must be bare - install_app()'s rule - so the source is
    # only ever the library's to add.
    assert refused.value.status == 400
    assert "id must be lowercase letters, digits and '-'" in str(refused.value)
    assert "'openfit.frms'" in str(refused.value)
    assert listing(apps_dir) == []


def test_a_library_install_is_held_to_install_apps_checks(apps_dir):
    # A file name, and a size: what install_app() refuses, this refuses.
    files = library_files("weight")
    with pytest.raises(apps.InstallError) as bad_name:
        apps.install_library_app(apps_dir, "openfit", {**files, "../x.py": ""})
    assert (bad_name.value.status, str(bad_name.value)) == (400, apps.filename_problem("../x.py"))
    with pytest.raises(apps.InstallError) as no_size:
        apps.install_library_app(apps_dir, "openfit", {"app.yaml": "id: weight\n"})
    assert (no_size.value.status, str(no_size.value)) == (400, apps.NO_SIZE)
    assert listing(apps_dir) == []


def test_a_paste_install_can_never_take_a_dotted_id(client, apps_dir):
    files = {**library_files("frms")}
    files["app.yaml"] = files["app.yaml"].replace("id: frms\n", "id: openfit.frms\n")

    with pytest.raises(apps.InstallError) as refused:
        apps.install_app(apps_dir, files)
    resp = client.post("/api/apps", json={"files": files})

    # INSTALL_ID has no dot: only a library install makes one.
    assert refused.value.status == 400
    assert str(refused.value) == (
        "id must be lowercase letters, digits and '-', starting with a letter or digit: 'openfit.frms'"
    )
    assert resp.status_code == 400
    assert resp.get_json() == {"ok": False, "error": str(refused.value)}
    assert listing(apps_dir) == []


# ---------- the scanner: the folder's name carries the source ----------


def place(apps_dir, folder, manifest):
    path = apps_dir / folder
    path.mkdir()
    (path / apps.MANIFEST).write_text(manifest)
    return path


def test_a_library_folder_loads_under_its_name_with_its_own_id_bare_or_left_out(apps_dir, capsys):
    place(apps_dir, "openfit.notes", "id: notes\nname: Notes\n" + SIZE)
    place(apps_dir, "openfit.plain", SIZE)

    assert [(a.id, a.name) for a in apps.scan_apps(apps_dir)] == [
        ("openfit.notes", "Notes"),
        # With no id or name, both are the folder's: the id defaults to the
        # bare name, which is its own.
        ("openfit.plain", "openfit.plain"),
    ]
    assert "[apps]" not in capsys.readouterr().out


@pytest.mark.parametrize("own, said", [
    ("other", "'other'"),
    # The full id is the folder's to carry, never the manifest's.
    ("openfit.notes", "'openfit.notes'"),
    ("Notes", "'Notes'"),
    ("5", "5"),
])
def test_a_library_folder_whose_own_id_isnt_its_bare_name_is_skipped(apps_dir, capsys, own, said):
    folder = place(apps_dir, "openfit.notes", f"id: {own}\n" + SIZE)

    assert apps.scan_apps(apps_dir) == []
    assert (
        f"[apps] skipping {folder}: a library app's own id is its bare name: expected 'notes', got {said}"
    ) in capsys.readouterr().out
    with pytest.raises(apps.AppError, match="a library app's own id is its bare name"):
        apps.load_app(str(folder))


def test_a_folder_not_named_as_a_library_app_keeps_todays_rule(apps_dir, capsys):
    # A dot elsewhere is no library app: the manifest's id, held to APP_ID.
    place(apps_dir, "OpenFit.notes", "id: notes\n" + SIZE)
    place(apps_dir, "my.app.x", SIZE)

    assert [a.id for a in apps.scan_apps(apps_dir)] == ["notes"]
    assert "skipping" in capsys.readouterr().out   # my.app.x: its folder name is no id


def test_a_metric_key_still_refuses_a_dot(apps_dir):
    manifest = (
        "id: caf\n" + SIZE + "metrics:\n"
        "  - {key: caf.mg, label: Caffeine, unit: mg, category: Other, grain: daily}\n"
    )
    custom = place(apps_dir, "caf", manifest)
    library = place(apps_dir, "openfit.caf", manifest)

    assert apps.scan_apps(apps_dir) == []
    for folder in (custom, library):
        with pytest.raises(apps.AppError, match="each metric needs a key.*'caf.mg'"):
            apps.load_app(str(folder))


# ---------- read-only: no update, but uninstall ----------


def test_a_library_app_is_never_updated_here(client, apps_dir):
    install(apps_dir, "frms")
    folder = apps_dir / "openfit.frms"
    before = {p.name: p.read_bytes() for p in folder.iterdir()}
    files = library_files("frms")
    files["config.json"] = "{}\n"
    said = (
        "'openfit.frms' is from the openfit library, so it's updated there, not here — "
        "Duplicate makes a copy of your own to change"
    )

    with pytest.raises(apps.InstallError) as refused:
        apps.update_app(apps_dir, "openfit.frms", files)
    resp = client.put("/api/apps/openfit.frms", json={"files": files})

    assert (refused.value.status, str(refused.value)) == (403, said)
    assert resp.status_code == 403
    assert resp.get_json() == {"ok": False, "error": said}
    # Nothing touched: the same files, no staging left behind.
    assert {p.name: p.read_bytes() for p in folder.iterdir()} == before
    assert listing(apps_dir) == ["openfit.frms"]


def test_a_library_app_uninstalls_as_any_does(client, apps_dir):
    install(apps_dir, "weight")

    resp = client.delete("/api/apps/openfit.weight")

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "id": "openfit.weight"}
    assert listing(apps_dir) == []
    assert client.get("/api/apps").get_json() == []


def test_a_custom_app_and_a_library_app_of_one_name_install_side_by_side(client, apps_dir):
    install(apps_dir, "frms")
    # The paste install of the same files: the bare id, another app's.
    resp = client.post("/api/apps", json={"files": library_files("frms")})

    assert resp.status_code == 201, resp.get_json()
    assert resp.get_json()["id"] == "frms"
    listed = client.get("/api/apps").get_json()
    assert [(a["id"], a["name"], a["library"]) for a in listed] == [
        ("frms", "FRMS", None), ("openfit.frms", "FRMS", "openfit"),
    ]
    # Each its own: the custom one updates, the library one doesn't.
    files = library_files("frms")
    assert client.put("/api/apps/frms", json={"files": files}).status_code == 200
    assert client.put("/api/apps/openfit.frms", json={"files": files}).status_code == 403


# ---------- a library app as a source ----------


def test_a_library_app_writes_under_its_own_dotted_source(client, conn, apps_dir):
    install(apps_dir, "weight")

    resp = act(client, "openfit.weight", "log", {"weight_kg": 81.2})

    assert resp.status_code == 200, resp.get_json()
    [number, _] = resp.get_json()["spec"]["children"]
    assert number["caption"] == f"{TODAY} · via app:openfit.weight"
    assert rows(conn) == [(TODAY, "app:openfit.weight", WEIGHT_KG, 81.2, "kg")]
    assert client.get("/api/metric/weight_kg").get_json()["points"] == [
        {"date": TODAY, "value": 81.2, "source": "app:openfit.weight"},
    ]


def test_a_library_apps_own_metric_is_keyed_by_its_dotted_id(client, conn, apps_dir):
    install(apps_dir, "caffeine")
    key = "app:openfit.caffeine:caffeine_mg"

    resp = act(client, "openfit.caffeine", "log", {"caffeine_mg": "95"})

    assert resp.status_code == 200, resp.get_json()
    assert rows(conn) == [(TODAY, "app:openfit.caffeine", key, 95.0, "mg")]
    assert client.get(f"/api/metric/{key}").get_json() == {
        "metric": key, "unit": "mg", "points": [{"date": TODAY, "value": 95.0, "source": "app:openfit.caffeine"}],
    }
    [entry] = client.get("/api/catalog").get_json()["metrics"]
    assert (entry["metric"], entry["app"], entry["label"]) == (key, "openfit.caffeine", "Caffeine")


def test_a_library_apps_sessions_are_filed_under_its_dotted_source(client, conn, apps_dir):
    install(apps_dir, "workout-log")

    resp = act(client, "openfit.workout-log", "log", {"minutes": 30})

    assert resp.status_code == 200, resp.get_json()
    [(session_id, source, kind, start)] = conn.execute('SELECT id, source, kind, "start" FROM sessions').fetchall()
    assert (source, kind) == ("app:openfit.workout-log", "workout")
    assert session_id == f"app:openfit.workout-log:workout:{start}"


def test_a_library_app_renders(client, apps_dir):
    install(apps_dir, "frms")

    resp = client.post("/api/app/openfit.frms/render", json={"cfg": {}})

    assert resp.status_code == 200
    assert resp.get_json()["type"] == "grid"


# ---------- the Home layout ----------


def test_a_layout_naming_a_library_app_saves_and_loads(client, apps_dir):
    install(apps_dir, "weight")
    # Placed on Home on its own, as any installed app is.
    [tab] = client.get("/api/home-layout").get_json()["tabs"]
    [item] = tab["items"]
    assert (item["app_id"], item["size"]) == ("openfit.weight", {"w": 1, "h": 1})

    layout = {
        "tabs": [
            {"id": "home", "name": "Home", "mode": "flow", "subtitle": "", "items": []},
            {"id": "body", "name": "Body", "mode": "flow", "subtitle": "", "items": [
                {"instance_id": "w1", "app_id": "openfit.weight", "size": {"w": 2, "h": 1}, "config": {"x": 1}},
            ]},
        ],
        "default_tab": "home",
    }
    resp = client.put("/api/home-layout", json=layout)

    assert resp.status_code == 200, resp.get_json()
    assert client.get("/api/home-layout").get_json() == layout


# ---------- Your data: an uninstalled library app's data ----------


def test_a_library_apps_data_is_orphaned_once_uninstalled_and_then_removable(client, conn, apps_dir):
    install(apps_dir, "weight")
    assert act(client, "openfit.weight", "log", {"weight_kg": 81.2}).status_code == 200
    entry = {
        "id": "openfit.weight", "source": "app:openfit.weight", "metrics": [WEIGHT_KG],
        "readings": 1, "sessions": 0, "samples": 0,
    }

    assert client.get("/api/app-sources").get_json() == [{**entry, "orphaned": False}]
    # Installed: its data is its app's, never removed.
    assert client.delete("/api/app-sources/openfit.weight").status_code == 400
    assert client.delete(f"/api/app-sources/openfit.weight/{WEIGHT_KG}").status_code == 400

    assert client.delete("/api/apps/openfit.weight").status_code == 200
    assert client.get("/api/app-sources").get_json() == [{**entry, "orphaned": True}]
    resp = client.delete("/api/app-sources/openfit.weight")

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "removed": {"metrics": 1, "sessions": 0, "samples": 0}}
    assert rows(conn) == []
    assert client.get("/api/app-sources").get_json() == []


def test_one_metric_of_an_uninstalled_library_app_is_removable(client, conn, apps_dir):
    install(apps_dir, "weight")
    assert act(client, "openfit.weight", "log", {"weight_kg": 81.2}).status_code == 200
    assert client.delete("/api/apps/openfit.weight").status_code == 200

    resp = client.delete(f"/api/app-sources/openfit.weight/{WEIGHT_KG}")

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "removed": {"metrics": 1, "sessions": 0, "samples": 0}}
    assert rows(conn) == []


def test_installed_again_a_library_app_carries_on_with_its_data(client, conn, apps_dir):
    install(apps_dir, "weight")
    assert act(client, "openfit.weight", "log", {"weight_kg": 81.2}).status_code == 200
    assert client.delete("/api/apps/openfit.weight").status_code == 200

    install(apps_dir, "weight")

    [entry] = client.get("/api/app-sources").get_json()
    assert (entry["id"], entry["orphaned"]) == ("openfit.weight", False)
    [number, _] = client.post("/api/app/openfit.weight/render").get_json()["children"]
    assert (number["value"], number["caption"]) == (81.2, f"{TODAY} · via app:openfit.weight")


def test_a_hand_placed_copy_is_a_library_app_by_its_folders_name(client, apps_dir):
    # Copied onto the volume under the library's name, as an admin might:
    # the folder carries the source, so it loads, writes and is read-only
    # just as installed.
    shutil.copytree(FIXTURE_APPS / "weight", apps_dir / "openfit.weight", ignore=shutil.ignore_patterns("__pycache__"))

    [listed] = client.get("/api/apps").get_json()
    assert (listed["id"], listed["library"]) == ("openfit.weight", "openfit")
    assert client.put("/api/apps/openfit.weight", json={"files": library_files("weight")}).status_code == 403
    # ... and its app.py, imported under a dotted id, renders.
    spec = client.post("/api/app/openfit.weight/render").get_json()
    assert spec["children"][0] == {"type": "empty", "message": "No weigh-ins yet"}
