"""
Installing an app from the browser: POST /api/apps.

The request carries the app's files, {"files": {name: text}}; the
server reads the id from the app.yaml among them and writes the files
to a new APPS_DIR/<id>/. It is strict about containment - the id and
every file name are checked before anything touches the disk, and a
refusal writes nothing - and loose about correctness: past a parsable
app.yaml with a valid id, only the app's tile size is checked (it won't
deploy without one), and nothing is run.

Every test gets its own temp apps folder; nothing reads /data/apps.
"""

import json
import os
from pathlib import Path

import pytest

import apps

REAL_APPS = Path(__file__).parent / "fixtures" / "apps"

# Every app declares a size - one without won't install - so the tests
# that aren't about size give their apps this one.
SIZE = "sizes: [{w: 1, h: 1}]\n"


@pytest.fixture
def apps_dir(tmp_path, monkeypatch):
    """An empty apps folder, which the app is pointed at."""
    import main

    path = tmp_path / "apps"
    path.mkdir()
    monkeypatch.setitem(main.app.config, "APPS_DIR", str(path))
    return path


def install(client, files):
    return client.post("/api/apps", json={"files": files})


def tree(root):
    """Every path under `root`, relative - to show nothing was written."""
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob("*"))


def real(folder, name):
    return (REAL_APPS / folder / name).read_text(encoding="utf-8")


# ---------- installing ----------


def test_a_yaml_only_app_installs(client, apps_dir):
    manifest = "id: notes\nname: Notes\n" + SIZE
    resp = install(client, {"app.yaml": manifest})

    assert resp.status_code == 201
    assert resp.get_json() == {"ok": True, "id": "notes", "has_app_py": False}
    assert tree(apps_dir) == ["notes", "notes/app.yaml"]
    assert (apps_dir / "notes" / "app.yaml").read_text() == manifest


def test_a_yaml_and_py_app_installs_with_its_extra_files(client, apps_dir):
    files = {
        "app.yaml": real("steps", "app.yaml"),
        "app.py": real("steps", "app.py"),
        "thresholds.yaml": "goal: 10000\n",
        "helpers.py": "def double(x):\n    return 2 * x\n",
    }
    resp = install(client, files)

    assert resp.status_code == 201
    assert resp.get_json() == {"ok": True, "id": "steps", "has_app_py": True}
    folder = apps_dir / "steps"
    assert sorted(os.listdir(folder)) == sorted(files)
    for name, content in files.items():
        # Written as given: byte for byte, line endings and all.
        assert (folder / name).read_bytes() == content.encode("utf-8"), name


def test_files_are_written_exactly_as_given(client, apps_dir):
    files = {
        "app.yaml": "id: exact\r\nname: Exact\r\nsizes: [{w: 1, h: 1}]\r\n",
        "app.py": "# café ☕\r\ndef render(ctx, cfg):\r\n    return ctx.empty()",
        "empty.txt": "",
    }
    assert install(client, files).status_code == 201
    for name, content in files.items():
        assert (apps_dir / "exact" / name).read_bytes() == content.encode("utf-8"), name


def test_an_installed_app_is_listed_and_renders(client, apps_dir):
    # The real steps app, installed through the route rather than copied,
    # is listed by the next scan and renders - no restart.
    assert client.get("/api/apps").get_json() == []
    install(client, {"app.yaml": real("steps", "app.yaml"), "app.py": real("steps", "app.py")})

    assert [a["id"] for a in client.get("/api/apps").get_json()] == ["steps"]
    spec = client.post("/api/app/steps/render").get_json()
    assert spec == {"type": "empty", "message": "No step data yet"}


def test_a_yaml_only_app_is_listed_with_nothing_to_draw(client, apps_dir):
    # app.py is optional: the app lists, and its tile says why it's blank.
    install(client, {"app.yaml": "id: notes\nname: Notes\n" + SIZE})

    assert client.get("/api/apps").get_json() == [
        {"id": "notes", "name": "Notes", "default_size": {"w": 1, "h": 1},
         "sizes": [{"w": 1, "h": 1}], "reads": [], "config": [], "metrics": [], "files": ["app.yaml"]}
    ]
    assert client.post("/api/app/notes/render").get_json() == {
        "type": "empty",
        "message": apps.NOTHING_TO_DRAW,
    }


def test_install_makes_the_apps_folder_if_there_is_none(client, tmp_path, monkeypatch):
    import main

    # A fresh install: /data/apps doesn't exist until the first app.
    apps_dir = tmp_path / "data" / "apps"
    monkeypatch.setitem(main.app.config, "APPS_DIR", str(apps_dir))

    assert install(client, {"app.yaml": "id: first\n" + SIZE}).status_code == 201
    assert tree(apps_dir) == ["first", "first/app.yaml"]


def test_install_leaves_no_staging_folder_behind(client, apps_dir):
    install(client, {"app.yaml": "id: one\n" + SIZE, "app.py": "x = 1\n"})
    install(client, {"app.yaml": "id: two\n" + SIZE})

    # Written to a hidden folder, then renamed into place.
    assert sorted(os.listdir(apps_dir)) == ["one", "two"]


# ---------- correctness: a size, and nothing runs ----------


def test_install_never_runs_the_apps_code(client, apps_dir, tmp_path):
    ran = tmp_path / "ran"
    code = f"open({str(ran)!r}, 'w').write('ran')\ndef render(ctx, cfg):\n    return ctx.empty()\n"

    assert install(client, {"app.yaml": "id: quiet\n" + SIZE, "app.py": code}).status_code == 201
    assert not ran.exists()


@pytest.mark.parametrize(
    "files",
    [
        # Manifest fields beyond the id and size aren't checked...
        {"app.yaml": "id: odd\n" + SIZE + "name: [1, 2]\nreads: steps\nconfig: {days: 7}\n"},
        # ... nor is app.py's syntax or contents.
        {"app.yaml": "id: odd\n" + SIZE, "app.py": "def render(ctx, cfg)\n    return (\n"},
        {"app.yaml": "id: odd\n" + SIZE, "app.py": "raise SystemExit('not run at install')\n"},
    ],
    ids=["bad manifest fields", "app.py syntax error", "app.py that would exit"],
)
def test_a_contained_but_malformed_app_still_installs(client, apps_dir, files):
    resp = install(client, files)

    assert resp.status_code == 201
    assert resp.get_json()["id"] == "odd"
    assert sorted(os.listdir(apps_dir / "odd")) == sorted(files)


def test_a_malformed_install_is_left_to_the_scanner(client, apps_dir, capsys):
    # Installed, then logged and skipped on the next scan - as a
    # hand-placed one would be - never breaking the list.
    install(client, {"app.yaml": "id: good\n" + SIZE})
    install(client, {"app.yaml": "id: bad\n" + SIZE, "app.py": "def render(ctx, cfg)\n"})

    assert [a["id"] for a in client.get("/api/apps").get_json()] == ["good"]
    assert f"[apps] skipping {apps_dir / 'bad'}" in capsys.readouterr().out


@pytest.mark.parametrize(
    "manifest, says",
    [
        ("id: plain\n", "no tile size: an app declares the sizes it's drawn at, such as "
                        "sizes: [{w: 1, h: 1}] - w and h in cells, 1 to 6"),
        ("id: plain\nname: Plain\nreads: [steps]\n", "no tile size:"),
        ("id: plain\nsizes:\n", "no tile size:"),
        ("id: plain\nsizes: []\n", "sizes must be a list of one or more {w, h}"),
        ("id: plain\nsizes: [{w: 7, h: 1}]\n", "sizes[0].w must be a whole number from 1 to 6"),
        ("id: plain\nsizes: [{w: 1, h: 0}]\n", "sizes[0].h must be a whole number from 1 to 6"),
        ("id: plain\nsizes: [{w:1,h:1}]\n", "sizes[0] needs a space after each colon"),
        ("id: plain\nsizes: [{w: 1, h: 1}, {w: 1, h: 1}]\n", "sizes lists 1×1 twice"),
        ("id: plain\ndefault_size: banana\n", "default_size must be a mapping of w and h"),
        ("id: plain\ndefault_size: {w: 12, h: 12}\n", "default_size.w must be a whole number from 1 to 6"),
        ("id: plain\nsizes: [{w: 1, h: 1}]\ndefault_size: {w: 2, h: 2}\n",
         "default_size must be the first of sizes"),
    ],
)
def test_an_app_without_a_size_it_can_have_wont_install(client, apps_dir, manifest, says):
    # check_app()'s first rule: won't deploy without a size. Refused with
    # the reason the scanner would give, and nothing is written.
    error = refused(client, apps_dir, {"app.yaml": manifest, "app.py": real("steps", "app.py")})
    assert error.startswith(says), error


@pytest.mark.parametrize(
    "manifest",
    [
        "id: sized\nsizes: [{w: 1, h: 1}, {w: 2, h: 2}, {w: 6, h: 6}]\n",
        "id: sized\ndefault_size: {w: 2, h: 2}\n",
        "id: sized\ndefault_size: {w: 3, h: 1}\nsizes: [{w: 3, h: 1}, {w: 1, h: 1}]\n",
    ],
    ids=["sizes", "the older default_size", "both"],
)
def test_an_app_with_a_size_installs(client, apps_dir, manifest):
    resp = install(client, {"app.yaml": manifest})

    assert resp.status_code == 201
    assert [a["id"] for a in client.get("/api/apps").get_json()] == ["sized"]


def test_the_deployed_real_apps_install_as_they_are(client, apps_dir):
    # As deployed, the real apps declare default_size: { w: 2, h: 2 }: the
    # older form installs, and lists as one size.
    manifest = real("steps", "app.yaml").replace("sizes: [{ w: 1, h: 1 }]", "default_size: { w: 2, h: 2 }")
    assert install(client, {"app.yaml": manifest, "app.py": real("steps", "app.py")}).status_code == 201

    [listed] = client.get("/api/apps").get_json()
    assert (listed["default_size"], listed["sizes"]) == ({"w": 2, "h": 2}, [{"w": 2, "h": 2}])


def test_containment_and_a_taken_id_are_refused_before_the_size(client, apps_dir):
    # check_app() runs last: a bad id or a taken one says so, sized or not.
    error = refused(client, apps_dir, {"app.yaml": "id: ../x\n"})
    assert error.startswith("id must be lowercase letters, digits and '-'"), error
    assert install(client, {"app.yaml": "id: steps\n" + SIZE}).status_code == 201
    error = refused(client, apps_dir, {"app.yaml": "id: steps\n"}, status=409)
    assert error == "'steps' already exists — remove it first to reinstall."


def test_check_app_is_the_extension_point(client, apps_dir, monkeypatch):
    # The correctness layer that grows: a check raising InstallError there
    # refuses the install, with its message, and nothing is written.
    def no_tabs(manifest, files):
        if any("\t" in text for text in files.values()):
            raise apps.InstallError("no tabs, please")

    monkeypatch.setattr(apps, "check_app", no_tabs)
    resp = install(client, {"app.yaml": "id: tabs\n" + SIZE, "app.py": "\tx = 1\n"})

    assert resp.status_code == 400
    assert resp.get_json() == {"ok": False, "error": "no tabs, please"}
    assert tree(apps_dir) == []


# ---------- refusals: each writes nothing ----------


def refused(client, apps_dir, files, status=400):
    """POST the files, expect a refusal; return its error, having checked nothing was written."""
    before = tree(apps_dir.parent)
    resp = install(client, files)
    assert resp.status_code == status, resp.get_json()
    body = resp.get_json()
    assert body["ok"] is False
    assert isinstance(body["error"], str) and body["error"]
    assert tree(apps_dir.parent) == before
    return body["error"]


def test_no_app_yaml_is_refused(client, apps_dir):
    error = refused(client, apps_dir, {"app.py": "def render(ctx, cfg):\n    return None\n"})
    assert "app.yaml" in error


@pytest.mark.parametrize(
    "manifest, says",
    [
        ("id: x\nname: [unclosed\n", "app.yaml doesn't parse:"),
        ("id: x\n\tname: tab-indented\n", "app.yaml doesn't parse:"),
        ("id: 'unclosed\n", "app.yaml doesn't parse:"),
        ("", "app.yaml is empty"),
        ("# just a comment\n", "app.yaml is empty"),
        ("- a\n- list\n", "app.yaml must be a mapping"),
        ("just a string\n", "app.yaml must be a mapping"),
        ("name: No id\n", "app.yaml needs an id"),
        ("id:\nname: Null id\n", "app.yaml needs an id"),
    ],
)
def test_an_app_yaml_that_doesnt_parse_or_has_no_id_is_refused(client, apps_dir, manifest, says):
    error = refused(client, apps_dir, {"app.yaml": manifest, "app.py": "x = 1\n"})
    assert error.startswith(says), error


@pytest.mark.parametrize(
    "app_id",
    [
        "../x",
        "a/b",
        "A",
        "",
        "..",
        ".",
        "/abs",
        "a\\b",
        "My-App",
        "-x",
        "a_b",
        "a b",
        "a.b",
        "abc\n",
        "x\x00",
        "ünï",
        5,
        True,
        ["a"],
        {"a": 1},
    ],
)
def test_an_id_outside_the_charset_is_refused(client, apps_dir, tmp_path, app_id):
    # JSON is YAML, so this writes any id - "../x", a trailing newline -
    # exactly.
    error = refused(client, apps_dir, {"app.yaml": f"id: {json.dumps(app_id)}\n"})
    assert error.startswith("id must be lowercase letters, digits and '-'"), error
    # Never created, inside the apps folder or beside it.
    assert not (tmp_path / "x").exists()


@pytest.mark.parametrize(
    "name",
    [
        "../evil",
        "a/b.py",
        "a\\b.py",
        "",
        ".",
        "..",
        "...",
        "a..b",
        "/etc/passwd",
        "/abs.py",
        "sub/",
        "nul\x00.py",
        "line\nbreak.py",
        "tab\t.py",
        "del\x7f.py",
        "\ud800.py",
    ],
)
def test_a_file_name_that_isnt_plain_is_refused(client, apps_dir, tmp_path, name):
    error = refused(client, apps_dir, {"app.yaml": "id: ok\n" + SIZE, name: "x = 1\n"})
    assert error, name
    # Not in the apps folder, nor where the name points.
    assert not (apps_dir / "ok").exists()
    assert not (tmp_path / "evil").exists()


@pytest.mark.parametrize("content", [None, 7, ["x"], {"a": 1}, True, "lone \ud800 surrogate"])
def test_a_file_that_isnt_text_is_refused(client, apps_dir, content):
    error = refused(client, apps_dir, {"app.yaml": "id: ok\n" + SIZE, "data.txt": content})
    assert "'data.txt' must be text" == error


def test_an_app_yaml_that_isnt_text_is_refused(client, apps_dir):
    assert refused(client, apps_dir, {"app.yaml": {"id": "ok"}}) == "app.yaml must be text"


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        "app.yaml",
        {},
        {"files": None},
        {"files": {}},
        {"files": [["app.yaml", "id: x\n"]]},
        {"files": "id: x\n"},
        {"app.yaml": "id: x\n"},
    ],
)
def test_a_request_without_a_files_mapping_is_refused(client, apps_dir, body):
    before = tree(apps_dir.parent)
    resp = client.post("/api/apps", json=body)

    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False
    assert "files" in resp.get_json()["error"]
    assert tree(apps_dir.parent) == before


def test_a_body_that_isnt_json_is_refused(client, apps_dir):
    resp = client.post("/api/apps", data="id: x", content_type="text/plain")

    assert resp.status_code == 400
    assert tree(apps_dir) == []


# ---------- a taken id: refused, never overwritten ----------


def test_a_taken_id_is_refused_and_the_installed_app_untouched(client, apps_dir):
    files = {"app.yaml": real("steps", "app.yaml"), "app.py": real("steps", "app.py")}
    assert install(client, files).status_code == 201
    before = {p: p.read_bytes() for p in (apps_dir / "steps").iterdir()}

    replacement = {
        "app.yaml": "id: steps\nname: Replaced\n" + SIZE,
        "app.py": "def render(ctx, cfg):\n    return ctx.empty('replaced')\n",
        "extra.txt": "new\n",
    }
    error = refused(client, apps_dir, replacement, status=409)

    assert error == "'steps' already exists — remove it first to reinstall."
    assert {p: p.read_bytes() for p in (apps_dir / "steps").iterdir()} == before
    assert client.get("/api/apps").get_json()[0]["name"] == "Steps"


@pytest.mark.parametrize("kind", ["folder", "empty folder", "file", "broken link"])
def test_an_id_whose_folder_exists_as_anything_is_taken(client, apps_dir, kind):
    path = apps_dir / "steps"
    if kind == "folder":
        path.mkdir()
        (path / "notes.txt").write_text("not an app, but not ours to replace")
    elif kind == "empty folder":
        path.mkdir()
    elif kind == "file":
        path.write_text("a stray file")
    else:
        path.symlink_to(apps_dir / "nowhere")

    error = refused(client, apps_dir, {"app.yaml": "id: steps\n" + SIZE}, status=409)
    assert error == "'steps' already exists — remove it first to reinstall."


def test_an_id_another_folders_app_loads_under_is_taken(client, apps_dir):
    # A hand-placed app whose folder isn't named for its id still holds
    # the id: a second app under it would be skipped by the scanner.
    (apps_dir / "my-steps").mkdir()
    (apps_dir / "my-steps" / "app.yaml").write_text("id: steps\n" + SIZE)
    (apps_dir / "my-steps" / "app.py").write_text(real("steps", "app.py"))
    # Listed already, as it would be (loading it leaves its __pycache__).
    assert [a["id"] for a in client.get("/api/apps").get_json()] == ["steps"]

    error = refused(client, apps_dir, {"app.yaml": "id: steps\n" + SIZE}, status=409)
    assert error == "'steps' already exists — remove it first to reinstall."


def test_a_failed_write_leaves_nothing_behind(client, apps_dir, monkeypatch):
    # Disk full, say, half way through: the staging folder goes, and the
    # app never appears.
    written = []

    def failing_open(path, mode="r", *args, **kwargs):
        written.append(path)
        if len(written) == 2:
            raise OSError(28, "No space left on device")
        return open(path, mode, *args, **kwargs)

    # apps.py's open, shadowing the builtin there only.
    monkeypatch.setattr(apps, "open", failing_open, raising=False)
    resp = install(client, {"app.yaml": "id: full\n" + SIZE, "app.py": "x = 1\n", "c.txt": "c"})

    assert len(written) == 2
    assert resp.status_code == 500
    assert resp.get_json() == {
        "ok": False,
        "error": "couldn't write the app's files: No space left on device",
    }
    assert tree(apps_dir) == []
