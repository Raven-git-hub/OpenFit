"""
B3: installed library apps update themselves.

When the Admin publishes a new version of a library app - an organisation
repo's Update apps, or the official library's - every OpenFit with it
installed takes it up by itself: shelf.sync_library_apps(), at startup and
every minute (the library-sync job), compares each installed library app's
fingerprint with the version the shelf's index lists, and swaps a new one in
through apps._replace_app() - the swap Update makes - noting what happened
in the update record, /data/app_updates.json, which GET /api/apps hands the
page as each library app's library_note.

1. The fingerprint: the Admin's, to the byte.
2. The sync: a new version swapped in byte for byte; a current one left
   alone, reading nothing of the shelf's but its index; a custom app never
   touched; no shelf, nothing done.
3. The guards: a version needing a newer OpenFit waits, one that doesn't
   install or load fails - the old copy kept, loading, and neither tried
   again until the shelf has another; an app gone from the shelf is
   removed, left as it is.
4. Data and Home: untouched by an update; the next render runs the new code.
5. update_app() still refuses a library app; no route reaches _replace_app().
6. The record: written whole, read strictly, a bad one empty; library_note.
7. The scheduler: library-sync every 60 seconds, and one run at startup.

Every shelf here is published by the Admin's own publish_set(), so its
layout and fingerprints are exactly the Admin's; nothing reads /data or
/library.
"""

import json
import os
import re
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from apscheduler.executors.pool import ThreadPoolExecutor

import admin_ops
import apps
import shelf

ROOT = Path(__file__).parent.parent
FRMS = ROOT / "tests" / "fixtures" / "apps" / "frms"
SOURCE = "openfitavi"
HELLO = f"{SOURCE}.hello"
NOW = "2026-10-29T09:00:00Z"
LATER = "2026-10-29T09:01:00Z"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """An empty apps folder and an empty shelf, the app pointed at both,
    and the update record conftest gives every test."""
    import main

    apps_dir, library = tmp_path / "apps", tmp_path / "library"
    apps_dir.mkdir()
    library.mkdir()
    monkeypatch.setitem(main.app.config, "APPS_DIR", str(apps_dir))
    monkeypatch.setitem(main.app.config, "LIBRARY_DIR", str(library))
    return SimpleNamespace(tmp=tmp_path, apps=apps_dir, library=library,
                           notes=Path(main.app.config["APP_UPDATES_PATH"]), commits=[])


# ---------- helpers ----------


def write(folder, files):
    folder.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(body, bytes):
            path.write_bytes(body)
        else:
            path.write_text(body, encoding="utf-8")


def hello(version, manifest="", **files):
    """A small app, hello, whose tile says which version it is."""
    return {
        "app.yaml": f"id: hello\nname: Hello\nsizes: [{{w: 1, h: 1}}]\n{manifest}",
        "app.py": f"def render(ctx, cfg):\n    return ctx.empty('version {version}')\n",
        **files,
    }


def publish(env, shelf_apps, source=SOURCE, changes=None):
    """The Admin's own publish of `shelf_apps`, {app: {name: text}}, as
    `source`, at a new commit - an organisation repo's clone, as its
    Update apps publishes one. `changes`, {app: [change notes]}, as its
    git log would give them. The commit."""
    commit = f"{len(env.commits) + 1:012x}".ljust(40, "c")  # a set's name is its first 12
    env.commits.append(commit)
    src = env.tmp / f"src-{len(env.commits)}"
    write(src, {"repo.yaml": f"source: {source}\nname: OpenFit Aviation\n"})
    for app, files in shelf_apps.items():
        write(src / app, files)
    admin_ops.publish_set(str(src), str(env.library), source, commit, NOW, log=lambda text: None,
                          label="test/repo", skip_hidden=True,
                          changes=(lambda app: (changes or {}).get(app, [])))
    return commit


def listed_sha(env, app="hello", source=SOURCE):
    return json.loads((env.library / "index.json").read_text())["sources"][source]["apps"][app]["sha256"]


def install(env, app="hello", source=SOURCE):
    """Browse's install: the shelf's own files, as install_library_app()."""
    apps.install_library_app(str(env.apps), source, shelf.read_app_files(str(env.library), source, app))


def sync(env, now=LATER):
    return shelf.sync_library_apps(str(env.apps), str(env.library), str(env.notes), now=now)


def files_of(folder):
    """A folder's own files, by name - not the __pycache__ a load leaves."""
    return {p.name: p.read_bytes() for p in Path(folder).iterdir() if p.is_file()}


def installed(env, app_id=HELLO):
    return files_of(env.apps / app_id)


def on_shelf(env, app="hello", source=SOURCE):
    return files_of(env.library / source / app)


def notes(env):
    return json.loads(env.notes.read_text()) if env.notes.exists() else None


def rendered(env, app_id=HELLO, conn=None):
    """The app's tile, as the host renders it now."""
    from host import render_app

    found = apps.find_app(str(env.apps), app_id)
    assert found is not None, f"{app_id} doesn't load"
    return render_app(found, conn)


class Spy:
    """Stands in for a function a run with nothing to do must never call."""

    def __init__(self, monkeypatch, *names):
        self.calls = []
        for name in names:
            monkeypatch.setattr(shelf, name, self.called(name))

    def called(self, name):
        def call(*args, **kwargs):
            self.calls.append(name)
            raise AssertionError(f"{name} was called")
        return call


CHANGE = {"sha": "a1b2c3d4e5f6", "subject": "Hello says hello", "date": "2026-10-29T08:30:00+00:00"}


# ---------- 1. the fingerprint is the Admin's ----------


def _folders(tmp_path):
    """Folders to fingerprint both ways: FRMS (with the __pycache__ a load
    leaves - and in it the temp file Python writes a .pyc through, left by
    an import cut short: not a .pyc, so only skipping the folder skips
    it), nested files, dotfiles and dot-folders, caches, a link, an empty
    folder, and none at all."""
    frms = tmp_path / "frms"
    shutil.copytree(FRMS, frms, ignore=shutil.ignore_patterns("__pycache__"))
    write(frms / "__pycache__", {"app.cpython-312.pyc": b"\x00\x01pyc", "app.cpython-312.pyc.139872": b"\x00"})
    nested = tmp_path / "nested"
    write(nested, {
        "app.yaml": "id: nested\n", "app.py": "x = 1\n",
        "assets/icons/one.txt": "one\n", "assets/two.txt": "two\n", "z/a.txt": "z\n", "a/z.txt": "a\n",
        "ünïcode name.txt": "ü\n", "bytes.bin": b"\xff\xfe\x00",
    })
    dotted = tmp_path / "dotted"
    write(dotted, {
        "app.yaml": "id: dotted\n", ".env": "SECRET=1\n", ".hidden/inner.txt": "hidden\n",
        "sub/.dotfile": "x\n", "sub/keep.txt": "keep\n",
    })
    caches = tmp_path / "caches"
    write(caches, {
        "app.yaml": "id: caches\n", "__pycache__/app.cpython-312.pyc": b"\x00", "old.pyc": b"\x00",
        "__pycache__/app.cpython-312.pyc.4242": b"\x00", "sub/__pycache__/x.cpython-312.pyc": b"\x00",
        "sub/__pycache__/notes.txt": "x\n", "sub/keep.py": "y = 2\n",
    })
    linked = tmp_path / "linked"
    write(linked, {"app.yaml": "id: linked\n", "real.txt": "real\n"})
    os.symlink("real.txt", linked / "link.txt")
    empty = tmp_path / "empty"
    empty.mkdir()
    return [frms, nested, dotted, caches, linked, empty, tmp_path / "missing"]


def test_the_fingerprint_is_the_admins_to_the_byte(tmp_path):
    for folder in _folders(tmp_path):
        assert shelf.fingerprint(str(folder)) == admin_ops.fingerprint(str(folder)), folder.name
    assert shelf.fingerprint(str(tmp_path / "missing")) is None


def test_caches_and_dotfiles_never_change_a_fingerprint_and_a_file_does(tmp_path):
    folder = tmp_path / "app"
    write(folder, {"app.yaml": "id: app\n", "app.py": "x = 1\n", "sub/keep.txt": "keep\n"})
    before = shelf.fingerprint(str(folder))
    write(folder, {"__pycache__/app.cpython-312.pyc": b"\x00", "__pycache__/app.cpython-312.pyc.77": b"\x00",
                   "x.pyc": b"\x00", ".env": "A=1\n", ".git/HEAD": "ref\n", "sub/__pycache__/y.pyc": b"\x00"})
    assert shelf.fingerprint(str(folder)) == before
    write(folder, {"sub/keep.txt": "changed\n"})
    assert shelf.fingerprint(str(folder)) != before
    write(folder, {"sub/keep.txt": "keep\n", "sub/new.txt": ""})
    assert shelf.fingerprint(str(folder)) != before


def test_a_current_install_is_the_shelfs_version_after_it_has_run(env):
    publish(env, {"frms": {p.name: p.read_text(encoding="utf-8") for p in FRMS.iterdir() if p.is_file()}})
    install(env, "frms")
    rendered(env, f"{SOURCE}.frms")  # a load leaves its __pycache__
    assert (env.apps / f"{SOURCE}.frms" / "__pycache__").is_dir()
    assert shelf.fingerprint(str(env.apps / f"{SOURCE}.frms")) == listed_sha(env, "frms")
    assert sync(env) == {}
    assert notes(env) is None


# ---------- 2. the sync ----------


def test_a_new_version_on_the_shelf_replaces_the_installed_copy_byte_for_byte(env, capsys):
    publish(env, {"hello": hello(1, notes_txt="old notes\n")})
    install(env)
    old = shelf.fingerprint(str(env.apps / HELLO))
    publish(env, {"hello": hello(2, "description: Says hello.\n", **{"config.json": '{"a": 1}\n'})},
            changes={"hello": [CHANGE]})
    new = listed_sha(env)
    capsys.readouterr()

    result = sync(env)

    # The installed copy is the shelf's, exactly: the new file in, the one
    # left out gone, every byte the same.
    assert installed(env) == on_shelf(env)
    assert sorted(installed(env)) == ["app.py", "app.yaml", "config.json"]
    assert shelf.fingerprint(str(env.apps / HELLO)) == new
    note = {"state": "updated", "at": LATER, "sha256": new, "changes": [CHANGE], "reason": None}
    assert result == {HELLO: note}
    assert notes(env) == {HELLO: note}
    assert rendered(env) == {"type": "empty", "message": "version 2"}
    # One line said it, by the two versions - never a file's contents.
    out = capsys.readouterr().out
    assert out.splitlines() == [f"[library] updated {HELLO} ({old[:7]} → {new[:7]})"]
    assert "Says hello" not in out and "version" not in out
    # And it's current now: the next run has nothing to do.
    assert sync(env) == {}


def test_frms_follows_its_repo_as_on_the_server(env):
    """The brief's Try it: FRMS installed from Browse, a word of its
    description changed in openfit-aviation, Update apps - and within a
    minute it's updated itself."""
    frms = {p.name: p.read_text(encoding="utf-8") for p in FRMS.iterdir() if p.is_file()}
    publish(env, {"frms": frms})
    install(env, "frms")
    rendered(env, f"{SOURCE}.frms")
    assert sync(env) == {}
    changed = {**frms, "app.yaml": frms["app.yaml"].replace("Fatigue risk management", "Fatigue-risk management")}
    publish(env, {"frms": changed}, changes={"frms": [CHANGE]})

    result = sync(env)

    assert result[f"{SOURCE}.frms"]["state"] == "updated"
    assert installed(env, f"{SOURCE}.frms") == on_shelf(env, "frms")
    assert apps.find_app(str(env.apps), f"{SOURCE}.frms").description.startswith("Fatigue-risk management")


def test_a_current_copy_reads_nothing_of_the_shelfs_but_its_index_and_writes_nothing(env, monkeypatch):
    publish(env, {"hello": hello(1)})
    install(env)
    rendered(env)
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in (env.apps / HELLO).iterdir() if p.is_file()}
    read = []
    real = shelf._read_regular
    monkeypatch.setattr(shelf, "_read_regular", lambda path, limit: read.append(path) or real(path, limit))
    spy = Spy(monkeypatch, "_update_files", "_read_files", "_replace_app", "_write_notes", "_installed_files")

    assert sync(env) == {}

    assert spy.calls == []
    assert set(read) <= {os.path.join(os.path.realpath(env.library), "index.json"), str(env.notes)}
    assert os.path.join(os.path.realpath(env.library), "index.json") in read
    assert notes(env) is None
    assert {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in (env.apps / HELLO).iterdir() if p.is_file()} == before


def test_a_custom_app_is_never_touched(env):
    """A custom hello - pasted in, or a Duplicate - beside the library's,
    the shelf's newer: only the library app follows it."""
    publish(env, {"hello": hello(1)})
    install(env)
    apps.install_app(str(env.apps), hello(1))
    custom = installed(env, "hello")
    publish(env, {"hello": hello(2)})

    result = sync(env)

    assert list(result) == [HELLO]
    assert installed(env, "hello") == custom
    assert rendered(env, "hello") == {"type": "empty", "message": "version 1"}
    assert list(notes(env)) == [HELLO]


def test_with_only_custom_apps_a_run_does_nothing(env, monkeypatch):
    publish(env, {"hello": hello(2)})
    apps.install_app(str(env.apps), hello(1))
    custom = installed(env, "hello")
    spy = Spy(monkeypatch, "read_notes", "_follow")

    assert sync(env) == {}
    assert spy.calls == [] and notes(env) is None
    assert installed(env, "hello") == custom


@pytest.mark.parametrize("unmount", ["no folder", "no index", "an index that doesn't read", "an index of nothing"])
def test_no_shelf_is_nothing_to_do_and_never_marks_an_app_removed(env, monkeypatch, unmount):
    publish(env, {"hello": hello(1)})
    install(env)
    publish(env, {"hello": hello(2)})
    copy = installed(env)
    if unmount == "no folder":
        shutil.rmtree(env.library)
    elif unmount == "no index":
        (env.library / "index.json").unlink()
    elif unmount == "an index that doesn't read":
        (env.library / "index.json").write_text("{not json")
    else:
        (env.library / "index.json").write_text(json.dumps({"published_at": NOW}))
    spy = Spy(monkeypatch, "read_notes", "_follow")

    assert sync(env) == {}
    assert spy.calls == [] and notes(env) is None
    assert installed(env) == copy


def test_one_run_at_a_time(env, monkeypatch):
    publish(env, {"hello": hello(1)})
    install(env)
    publish(env, {"hello": hello(2)})
    copy = installed(env)
    spy = Spy(monkeypatch, "_follow")
    with shelf._running:
        assert sync(env) == {}
    assert spy.calls == [] and installed(env) == copy


def test_a_file_an_install_never_takes_doesnt_make_a_current_copy_update(env, monkeypatch):
    """The shelf has a file Browse leaves out (a name an install refuses),
    so the fingerprints never match: the shelf's files are read once, found
    the same as the installed copy's, and never again until either changes.
    No swap, no note."""
    publish(env, {"hello": hello(1, **{"notes..txt": "left out\n"})})
    install(env)
    assert "notes..txt" not in installed(env)
    assert shelf.fingerprint(str(env.apps / HELLO)) != listed_sha(env)
    folder = os.stat(env.apps / HELLO).st_ino

    assert sync(env) == {}
    assert os.stat(env.apps / HELLO).st_ino == folder  # never swapped
    assert notes(env) is None
    spy = Spy(monkeypatch, "_update_files", "_replace_app")
    assert sync(env) == {}
    assert spy.calls == []


# ---------- 3. the guards ----------


def test_a_version_needing_a_newer_openfit_waits_and_isnt_tried_again(env, monkeypatch, capsys):
    publish(env, {"hello": hello(1)})
    install(env)
    copy = installed(env)
    publish(env, {"hello": hello(2, "requires: 2\nsomething_new: {x: 1}\n")})
    waiting = listed_sha(env)
    capsys.readouterr()

    result = sync(env)

    reason = "needs OpenFit app level 2; this OpenFit is level 1"
    note = {"state": "waiting", "at": LATER, "sha256": waiting, "changes": [], "reason": reason}
    assert result == {HELLO: note} and notes(env) == {HELLO: note}
    assert installed(env) == copy
    assert rendered(env) == {"type": "empty", "message": "version 1"}
    assert capsys.readouterr().out.splitlines() == [
        f"[library] {HELLO}: an update is waiting for a newer OpenFit: {reason}"]

    # Not tried again - not even read - while the shelf has the same one.
    with monkeypatch.context() as m:
        spy = Spy(m, "_update_files", "_replace_app")
        assert sync(env, now="2026-10-29T09:02:00Z") == {}
        assert spy.calls == []
    assert notes(env) == {HELLO: note}

    # Another version, one this OpenFit can run: in it goes.
    publish(env, {"hello": hello(3)})
    assert sync(env)[HELLO]["state"] == "updated"
    assert rendered(env) == {"type": "empty", "message": "version 3"}


def test_a_version_that_doesnt_load_fails_and_the_old_copy_keeps_running(env, monkeypatch, capsys):
    publish(env, {"hello": hello(1)})
    install(env)
    copy = installed(env)
    broken = hello(2)
    broken["app.py"] = "raise RuntimeError('broken at import')\n"
    publish(env, {"hello": broken})
    failed = listed_sha(env)
    capsys.readouterr()

    result = sync(env)

    note = result[HELLO]
    assert note["state"] == "failed" and note["sha256"] == failed and note["changes"] == []
    assert note["reason"].startswith(f"the update doesn't load, so '{HELLO}' is unchanged: app.py failed to import")
    assert notes(env) == {HELLO: note}
    # The old copy, intact and loading; nothing left behind.
    assert installed(env) == copy
    assert rendered(env) == {"type": "empty", "message": "version 1"}
    assert sorted(os.listdir(env.apps)) == [HELLO]
    assert f"[library] couldn't update {HELLO}: the update doesn't load" in capsys.readouterr().out

    with monkeypatch.context() as m:
        spy = Spy(m, "_update_files", "_replace_app")
        assert sync(env) == {}
        assert spy.calls == []

    publish(env, {"hello": hello(3)})
    assert sync(env)[HELLO]["state"] == "updated"
    assert rendered(env) == {"type": "empty", "message": "version 3"}


@pytest.mark.parametrize("files, why", [
    (hello(2, "", **{"app.yaml": "id: other\nsizes: [{w: 1, h: 1}]\n"}),
     "a library app's own id is its bare name: expected 'hello', got 'other'"),
    (hello(2, "", **{"app.yaml": "name: Hello\nsizes: [{w: 1, h: 1}]\n"}),
     "a library app's own id is its bare name: expected 'hello', got None"),
    (hello(2, "", **{"assets/icon.txt": "an icon\n"}), shelf.HAS_FOLDERS),
    (hello(2, "", **{"app.yaml": "id: hello\nname: Hello\n"}), apps.NO_SIZE),
    (hello(2, "", **{"logo.png": b"\x89PNG\r\n\x1a\n\xff"}), "'logo.png' in openfitavi.hello isn't text, so it can't be installed"),
    (hello(2, "", **{"app.yaml": "id: hello\nsizes: [{w: 1, h: 1}]\nrequires: two\n"}),
     "requires must be a whole number from 1, the app level it needs: 'two'"),
    (hello(2, "", **{"app.yaml": "- not\n- a mapping\n"}), "app.yaml must be a mapping"),
])
def test_a_version_this_openfit_cant_install_fails_and_changes_nothing(env, files, why):
    publish(env, {"hello": hello(1)})
    install(env)
    copy = installed(env)
    publish(env, {"hello": files})

    result = sync(env)

    assert result[HELLO]["state"] == "failed"
    assert result[HELLO]["reason"] == why
    assert result[HELLO]["sha256"] == listed_sha(env)
    assert installed(env) == copy
    assert rendered(env) == {"type": "empty", "message": "version 1"}


def test_an_app_folder_that_leads_out_of_the_shelf_is_never_read(env, monkeypatch, capsys):
    """_update_files() holds every path inside the shelf (_inside()): an
    app's folder there that's a link out of it - openfitavi/frms onto a
    folder elsewhere on the server, holding what a read through it would
    install - fails, the installed copy as it was, and nothing outside the
    library is read: not its listing, not a file."""
    frms = {p.name: p.read_text(encoding="utf-8") for p in FRMS.iterdir() if p.is_file()}
    publish(env, {"frms": frms})
    install(env, "frms")
    app_id = f"{SOURCE}.frms"
    copy = installed(env, app_id)
    newer = {**frms, "app.yaml": frms["app.yaml"].replace("Fatigue risk management", "Fatigue-risk management")}
    assert newer != frms
    publish(env, {"frms": newer})
    sha = listed_sha(env, "frms")
    elsewhere = env.tmp / "elsewhere"
    write(elsewhere / "frms", newer)
    on_shelf_folder = env.library / SOURCE / "frms"  # in the published set, through the source's link
    shutil.rmtree(on_shelf_folder)
    os.symlink(elsewhere / "frms", on_shelf_folder)
    # Every path the shelf's reads take: a file (_read_regular()) or a folder's listing (_listing()).
    touched = []
    read, listing = shelf._read_regular, shelf._listing
    monkeypatch.setattr(shelf, "_read_regular", lambda path, limit: touched.append(path) or read(path, limit))
    monkeypatch.setattr(shelf, "_listing", lambda folder: touched.append(folder) or listing(folder))
    capsys.readouterr()

    result = sync(env)

    note = {"state": "failed", "at": LATER, "sha256": sha, "changes": [],
            "reason": f"{app_id}'s folder isn't on the shelf as its index says"}
    assert result == {app_id: note} and notes(env) == {app_id: note}
    assert installed(env, app_id) == copy
    assert apps.find_app(str(env.apps), app_id).description.startswith("Fatigue risk management")
    # Nothing outside the library read - the shelf's index and the update record aside, nothing at all.
    library, outside = os.path.realpath(env.library), os.path.realpath(elsewhere)
    assert touched
    for path in touched:
        real = os.path.realpath(path)
        assert os.path.commonpath([outside, real]) != outside, path
        assert os.path.commonpath([library, real]) == library or path == str(env.notes), path
    assert f"[library] couldn't update {app_id}: {note['reason']}" in capsys.readouterr().out


def test_an_app_that_left_the_shelf_is_removed_and_left_as_it_is(env, capsys):
    publish(env, {"hello": hello(1)})
    install(env)
    copy = installed(env)
    publish(env, {"walk": hello("walk", "", **{"app.yaml": "id: walk\nsizes: [{w: 1, h: 1}]\n"})})
    capsys.readouterr()

    result = sync(env)

    note = {"state": "removed", "at": LATER, "sha256": None, "changes": [], "reason": None}
    assert result == {HELLO: note} and notes(env) == {HELLO: note}
    assert installed(env) == copy
    assert rendered(env) == {"type": "empty", "message": "version 1"}
    assert capsys.readouterr().out.splitlines() == [
        f"[library] {HELLO} is no longer in the app library, so it won't be updated"]
    # Said once.
    assert sync(env, now="2026-10-29T09:05:00Z") == {}
    assert notes(env) == {HELLO: note}
    assert capsys.readouterr().out == ""

    # Back on the shelf, a new version: it updates again.
    publish(env, {"hello": hello(2)})
    assert sync(env)[HELLO]["state"] == "updated"
    assert rendered(env) == {"type": "empty", "message": "version 2"}


def test_an_app_whose_source_left_the_shelf_is_removed(env):
    publish(env, {"hello": hello(1)})
    install(env)
    copy = installed(env)
    admin_ops.unpublish_source(str(env.library), SOURCE, NOW, log=lambda text: None)

    assert sync(env)[HELLO]["state"] == "removed"
    assert installed(env) == copy
    assert rendered(env) == {"type": "empty", "message": "version 1"}


def test_a_warning_goes_once_the_app_is_current_again(env):
    publish(env, {"hello": hello(1)})
    install(env)
    publish(env, {"hello": hello(2, "requires: 2\n")})
    assert sync(env)[HELLO]["state"] == "waiting"
    # The Admin put the shelf back on the version installed.
    publish(env, {"hello": hello(1)})

    assert sync(env) == {HELLO: None}
    assert notes(env) == {}


def test_an_update_note_stays_while_the_app_is_current(env):
    publish(env, {"hello": hello(1)})
    install(env)
    publish(env, {"hello": hello(2)})
    note = sync(env)[HELLO]
    assert sync(env, now="2026-10-30T09:00:00Z") == {}
    assert notes(env) == {HELLO: note}


def test_an_uninstalled_apps_note_goes_so_a_reinstall_starts_afresh(env):
    publish(env, {"hello": hello(1), "walk": hello("w", "", **{"app.yaml": "id: walk\nsizes: [{w: 1, h: 1}]\n"})})
    install(env)
    publish(env, {"hello": hello(2), "walk": hello("w", "", **{"app.yaml": "id: walk\nsizes: [{w: 1, h: 1}]\n"})})
    assert sync(env)[HELLO]["state"] == "updated"
    apps.uninstall_app(str(env.apps), HELLO)

    assert sync(env) == {HELLO: None}
    assert notes(env) == {}
    install(env)
    assert sync(env) == {}
    assert notes(env) == {}


def test_a_broken_apps_note_is_kept(env):
    """There but not loading is broken, not gone: its note stays."""
    publish(env, {"hello": hello(1)})
    install(env)
    publish(env, {"hello": hello(2)})
    note = sync(env)[HELLO]
    (env.apps / HELLO / "app.yaml").write_text("id: hello\n")  # no size: it won't load
    assert apps.find_app(str(env.apps), HELLO) is None

    assert sync(env) == {}
    assert notes(env) == {HELLO: note}


def test_the_change_notes_kept_are_the_indexs_well_formed_ones(env):
    good = [{**CHANGE, "sha": f"{i:012x}", "subject": f"Change {i}"} for i in range(12)]
    bad = [
        {"sha": "A1B2C3D4E5F6", "subject": "capital sha", "date": CHANGE["date"]},
        {"sha": "a1b2c3d", "subject": "short sha", "date": CHANGE["date"]},
        {**CHANGE, "subject": "x" * 201},
        {**CHANGE, "date": "yesterday"},
        {**CHANGE, "extra": 1},
        "not a change",
    ]
    publish(env, {"hello": hello(1)})
    install(env)
    publish(env, {"hello": hello(2)}, changes={"hello": [good[0], *bad, *good[1:]]})

    assert sync(env)[HELLO]["changes"] == good[:10]


def test_an_index_entry_without_a_version_fails_once(env):
    publish(env, {"hello": hello(1)})
    install(env)
    copy = installed(env)
    publish(env, {"hello": hello(2)})
    index = json.loads((env.library / "index.json").read_text())
    index["sources"][SOURCE]["apps"]["hello"]["sha256"] = "not a sha"
    (env.library / "index.json").write_text(json.dumps(index))

    note = sync(env)[HELLO]
    assert (note["state"], note["sha256"]) == ("failed", None)
    assert note["reason"] == "the app library's index doesn't say which version it has"
    assert installed(env) == copy
    assert sync(env) == {}


# ---------- 4. data and Home ----------


def test_an_update_leaves_the_apps_data_and_home_tiles_and_runs_the_new_code(client, env, conn):
    publish(env, {"hello": hello(1)})
    assert client.post(f"/api/library/{SOURCE}/hello/install").status_code == 201
    source = f"app:{HELLO}"
    conn.execute("INSERT INTO metrics (date, source, metric, value, unit) VALUES ('2026-10-28', ?, 'mood', 4, 'scale')",
                 (source,))
    conn.execute("INSERT INTO sessions (id, source, kind, \"start\", \"end\", summary_json) VALUES "
                 "(?, ?, 'workout', '2026-10-28T08:00:00Z', '2026-10-28T09:00:00Z', '{\"type\": \"run\"}')",
                 (f"{source}:workout:2026-10-28T08:00:00Z", source))
    conn.execute("INSERT INTO samples (source, metric, ts, value, unit, synced_at) VALUES (?, 'heart_rate', "
                 "'2026-10-28T08:00:00Z', 70, 'bpm', '2026-10-28 09:00:00')", (source,))
    conn.commit()
    home = client.get("/api/home-layout").get_json()
    home["tabs"][0]["items"][0]["config"] = {"greeting": "hi"}
    home["tabs"].append({"id": "trips", "name": "Trips", "mode": "flow", "subtitle": "away", "items": []})
    assert client.put("/api/home-layout", json=home).status_code == 200
    layout = client.get("/api/home-layout").get_json()

    def data():
        return [list(map(tuple, conn.execute(f"SELECT * FROM {t} ORDER BY 1, 2").fetchall()))
                for t in ("metrics", "sessions", "samples", "derived_metrics", "settings")]

    rows = data()
    assert client.post(f"/api/app/{HELLO}/render", json={}).get_json() == {"type": "empty", "message": "version 1"}
    folder = str(env.apps / HELLO)
    assert folder in apps._loaded

    publish(env, {"hello": hello(2)})
    import main
    main.library_sync()

    # The cached load dropped with the old files...
    assert folder not in apps._loaded
    # ... so the next render runs the new code.
    assert client.post(f"/api/app/{HELLO}/render", json={}).get_json() == {"type": "empty", "message": "version 2"}
    assert data() == rows
    assert client.get("/api/home-layout").get_json() == layout


# ---------- 5. update_app() still refuses a library app ----------


def test_update_still_refuses_a_library_app_and_no_route_reaches_replace_app(client, env):
    publish(env, {"hello": hello(1)})
    install(env)
    copy = installed(env)
    files = {k: v for k, v in hello(2).items()}

    r = client.put(f"/api/apps/{HELLO}", json={"files": files})
    assert r.status_code == 403
    assert "is from the openfitavi library, so it's updated there, not here" in r.get_json()["error"]
    with pytest.raises(apps.InstallError) as e:
        apps.update_app(str(env.apps), HELLO, files)
    assert e.value.status == 403
    assert installed(env) == copy

    # _replace_app() is internal: main.py never names it, and update_app()
    # reaches it only past its refusal of a library app.
    main_py = (ROOT / "app" / "main.py").read_text()
    assert "_replace_app" not in main_py
    import inspect
    source = inspect.getsource(apps.update_app)
    assert source.index("403,\n") < source.index("_replace_app(apps_dir, app_id, files)")
    callers = [p for p in (ROOT / "app").rglob("*.py") if "_replace_app(" in p.read_text()]
    assert sorted(p.name for p in callers) == ["apps.py", "shelf.py"]


def test_update_of_a_custom_app_still_works_through_the_shared_swap(client, env):
    apps.install_app(str(env.apps), hello(1))
    r = client.put("/api/apps/hello", json={"files": hello(2)})
    assert r.status_code == 200 and r.get_json() == {"ok": True, "id": "hello", "has_app_py": True}
    assert rendered(env, "hello") == {"type": "empty", "message": "version 2"}
    assert sorted(os.listdir(env.apps)) == ["hello"]


def test_a_staged_library_update_is_loaded_under_the_apps_own_folder_name(env):
    """The load check loads the staged files as they'd load in place, so a
    library app is held to its bare id there too."""
    publish(env, {"hello": hello(1)})
    install(env)
    with apps._write_lock:
        with pytest.raises(apps.InstallError) as e:
            apps._replace_app(str(env.apps), HELLO, {**hello(2), "app.yaml": "id: hello\nsizes: [{w: 1, h: 1}]\n"
                                                    "replaces: [openfitavi.hello]\n"})
    assert "replaces can't name the app itself" in str(e.value)
    assert rendered(env) == {"type": "empty", "message": "version 1"}


# ---------- 6. the record ----------


def _note(**over):
    return {"state": "updated", "at": NOW, "sha256": "a" * 64, "changes": [CHANGE], "reason": None, **over}


@pytest.mark.parametrize("text", [
    "{not json",
    "[]",
    json.dumps({"hello": _note()}),  # a custom app's id
    json.dumps({HELLO: {k: v for k, v in _note().items() if k != "reason"}}),
    json.dumps({HELLO: {**_note(), "extra": 1}}),
    json.dumps({HELLO: _note(state="done")}),
    json.dumps({HELLO: _note(at="2026-10-29 09:00:00")}),
    json.dumps({HELLO: _note(sha256="A" * 64)}),
    json.dumps({HELLO: _note(sha256="a" * 63)}),
    json.dumps({HELLO: _note(changes={})}),
    json.dumps({HELLO: _note(changes=[{**CHANGE, "sha": "a1b2c3d"}])}),
    json.dumps({HELLO: _note(changes=[{**CHANGE, "subject": "x" * 201}])}),
    json.dumps({HELLO: _note(changes=[CHANGE] * 11)}),
    json.dumps({HELLO: _note(reason=1)}),
    json.dumps({HELLO: _note(reason="x" * 501)}),
    json.dumps({HELLO: "updated"}),
    json.dumps({HELLO: _note(), "openfit.walk": _note(state="later")}),  # one bad entry: the whole file
])
def test_the_record_is_read_strictly_and_a_bad_one_is_empty_and_logged_once(env, capsys, text):
    env.notes.write_text(text)
    assert shelf.read_notes(str(env.notes)) == {}
    assert shelf.read_notes(str(env.notes)) == {}
    out = capsys.readouterr().out.splitlines()
    assert out == [f"[library] the update record {env.notes} doesn't read as one, so it's taken as empty"]


def test_a_good_record_reads_back_and_none_is_empty(env):
    assert shelf.read_notes(str(env.notes)) == {}
    record = {HELLO: _note(), "openfit.walk": _note(state="removed", sha256=None, changes=[]),
              "openfit.run": _note(state="failed", changes=[], reason="x" * 500)}
    shelf._write_notes(str(env.notes), record)
    assert shelf.read_notes(str(env.notes)) == record


def test_the_record_is_written_whole_or_not_at_all(env, monkeypatch):
    shelf._write_notes(str(env.notes), {HELLO: _note()})
    before = env.notes.read_bytes()

    def fail(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(shelf.os, "replace", fail)
    with pytest.raises(OSError):
        shelf._write_notes(str(env.notes), {HELLO: _note(state="failed")})
    assert env.notes.read_bytes() == before
    assert sorted(os.listdir(env.notes.parent)) == sorted(
        p for p in os.listdir(env.notes.parent) if not p.startswith(".app_updates-"))


def test_a_record_that_cant_be_written_is_logged_and_the_run_goes_on(env, monkeypatch, capsys):
    publish(env, {"hello": hello(1)})
    install(env)
    publish(env, {"hello": hello(2)})
    monkeypatch.setattr(shelf, "_write_notes", lambda path, notes: (_ for _ in ()).throw(OSError(28, "No space")))

    assert sync(env)[HELLO]["state"] == "updated"
    assert rendered(env) == {"type": "empty", "message": "version 2"}
    assert f"[library] couldn't write the update record {env.notes}: No space" in capsys.readouterr().out


def test_a_bad_record_is_taken_as_empty_and_written_afresh(env):
    publish(env, {"hello": hello(1)})
    install(env)
    publish(env, {"hello": hello(2)})
    env.notes.write_text(json.dumps({"openfit.walk": "junk"}))

    note = sync(env)[HELLO]

    assert notes(env) == {HELLO: note}


def test_api_apps_carries_each_library_apps_note(client, env):
    publish(env, {"hello": hello(1)})
    install(env)
    apps.install_app(str(env.apps), hello(1))

    listed = {a["id"]: a for a in client.get("/api/apps").get_json()}
    assert listed[HELLO]["library"] == SOURCE and listed[HELLO]["library_note"] is None
    assert "library_note" not in listed["hello"]  # a custom app has none

    publish(env, {"hello": hello(2)}, changes={"hello": [CHANGE]})
    sync(env)
    listed = {a["id"]: a for a in client.get("/api/apps").get_json()}
    assert listed[HELLO]["library_note"] == {"state": "updated", "at": LATER, "changes": [CHANGE], "reason": None}
    assert "library_note" not in listed["hello"]

    env.notes.write_text("{bad")
    listed = {a["id"]: a for a in client.get("/api/apps").get_json()}
    assert listed[HELLO]["library_note"] is None


def test_api_apps_never_reads_the_record_without_a_library_app(client, env, monkeypatch):
    apps.install_app(str(env.apps), hello(1))
    import main
    monkeypatch.setattr(main, "read_notes", lambda path: (_ for _ in ()).throw(AssertionError("read")))
    assert [a["id"] for a in client.get("/api/apps").get_json()] == ["hello"]


# ---------- 7. the scheduler, and startup ----------


def test_library_sync_is_a_job_every_sixty_seconds_on_its_own_worker(client, monkeypatch):
    import main
    from test_sync_cadence import FakeScheduler

    monkeypatch.setattr(main, "BackgroundScheduler", FakeScheduler)
    scheduler = main.start_scheduler()

    assert scheduler.jobs["library-sync"] == {"func": main.library_sync, "trigger": "interval", "args": [],
                                              "seconds": 60, "executor": "library"}
    assert main.LIBRARY_SYNC_SECONDS == shelf.LIBRARY_SYNC_SECONDS == 60
    executor = scheduler.config["executors"]["library"]
    assert isinstance(executor, ThreadPoolExecutor) and executor._pool._max_workers == 1
    # The plugins' worker is still the one it was.
    assert scheduler.config["executors"]["default"]._pool._max_workers == 1


def test_startup_runs_one_sync_before_the_scheduler_and_the_server(env, monkeypatch):
    import main

    calls = []
    monkeypatch.setattr(main, "migrate", lambda: calls.append("migrate"))
    monkeypatch.setattr(main, "backfill", lambda: calls.append("backfill"))
    monkeypatch.setattr(main, "sync_library_apps", lambda *args: calls.append(("sync", *args)))
    monkeypatch.setattr(main, "start_scheduler", lambda: calls.append("scheduler"))
    monkeypatch.setattr(main.app, "run", lambda **kwargs: calls.append("run"))

    main.serve()

    assert calls == ["migrate", "backfill", ("sync", str(env.apps), str(env.library), str(env.notes)),
                     "scheduler", "run"]


def test_startup_brings_an_installed_app_up_to_date(env, monkeypatch):
    import main

    publish(env, {"hello": hello(1)})
    install(env)
    publish(env, {"hello": hello(2)})
    for name in ("migrate", "backfill", "start_scheduler"):
        monkeypatch.setattr(main, name, lambda: None)
    monkeypatch.setattr(main.app, "run", lambda **kwargs: None)

    main.serve()

    assert notes(env)[HELLO]["state"] == "updated"
    assert rendered(env) == {"type": "empty", "message": "version 2"}


def test_a_sync_that_raises_is_logged_and_never_takes_the_job_down(monkeypatch, capsys):
    import main

    monkeypatch.setattr(main, "sync_library_apps", lambda *args: (_ for _ in ()).throw(RuntimeError("odd")))
    main.library_sync()
    assert "[library] the apps couldn't follow the app library: RuntimeError: odd" in capsys.readouterr().out


def test_one_apps_trouble_is_its_own(env, monkeypatch):
    """Something unforeseen in one app's update is that app's failure; the
    next is still brought up to date."""
    publish(env, {"hello": hello(1), "walk": hello("w1", "", **{"app.yaml": "id: walk\nsizes: [{w: 1, h: 1}]\n"})})
    install(env)
    install(env, "walk")
    publish(env, {"hello": hello(2), "walk": hello("w2", "", **{"app.yaml": "id: walk\nsizes: [{w: 1, h: 1}]\n"})})
    real = shelf._update_files

    def odd(root, index, source, app):
        if app == "hello":
            raise KeyError("odd")
        return real(root, index, source, app)

    monkeypatch.setattr(shelf, "_update_files", odd)
    result = sync(env)

    assert result[HELLO]["state"] == "failed" and result[HELLO]["reason"] == "KeyError: 'odd'"
    assert result[f"{SOURCE}.walk"]["state"] == "updated"
    assert rendered(env, f"{SOURCE}.walk") == {"type": "empty", "message": "version w2"}


# ---------- 8. the page: Updated, What's new, and the notes ----------
#
# The page's own appRow, its notes and What's new, run under node over
# test_routes.py's stand-in DOM - which parses whatever reaches
# innerHTML, so text set as markup would show up as elements.

NOTE_PARTS = ["el", "DAY_MS", "fmtStamp", "appsList", "appPanelId", "expandable", "openModal",
              "UPDATED_DAYS", "freshUpdate", "noteSentence", "updatedLine", "openWhatsNew", "appRow"]

NOTE_HARNESS = r"""
    const r = {};
    const appPanel = a => mk('div');
    const DAY = 864e5;
    const ago = days => new Date(Date.now() - days * DAY).toISOString().replace(/\.\d+Z$/, 'Z');
    const textOf = n => n ? n.textContent : null;
    // A row as the Applications list draws it: what the item holds first
    // (the head, or a line around it and its pill), the head's own parts,
    // and the pill.
    function row(a){
      const item = appRow(a);
      const first = item.children[0];
      const head = first.tag === 'button' ? first : first.children[0];
      const pill = first.tag === 'button' ? null : first.children[1];
      return {item, pill, first: first.className, head: head.className,
              parts: head.children.map(c => [c.className, c.textContent]), headHtml: head._html,
              pillText: textOf(pill), pillCls: pill ? pill.className : null};
    }
"""


def _note_script(client, scenario):
    from test_routes import PARSING_DOM, _js, _node, _page

    page = _page(client)
    esc = re.search(r"\n  (const esc = .*?\);)\n", page, re.S).group(1)  # two lines: as Duplicate's tests take it
    script = PARSING_DOM + esc + "\n" + "\n".join(_js(page, n) for n in NOTE_PARTS) + NOTE_HARNESS + scenario + \
        "\nconsole.log(JSON.stringify(r));"
    return _node(script)


def _app(**over):
    return {"id": HELLO, "name": "Hello <b>", "library": SOURCE, "library_note": None, **over}


needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="needs node to run the page's script")


@needs_node
def test_an_update_is_news_for_fourteen_days(client):
    r = _note_script(client, """
        const now = Date.parse('2026-10-29T12:00:00Z');
        const updated = at => freshUpdate({state:'updated', at, changes:[], reason:null}, now);
        r.fresh = [
          updated('2026-10-29T09:00:00Z'),
          updated('2026-10-15T12:00:01Z'),   // a second short of fourteen days
          updated('2026-10-15T12:00:00Z'),   // fourteen days to the second: no longer
          updated('2026-09-01T09:00:00Z'),
          updated('not a time'),
          freshUpdate({state:'waiting', at:'2026-10-29T09:00:00Z'}, now),
          freshUpdate(null, now),
        ];
        r.days = UPDATED_DAYS;
    """)
    assert r == {"fresh": [True, True, False, False, False, False, False], "days": 14}


@needs_node
def test_the_waiting_failed_and_removed_sentences(client):
    r = _note_script(client, """
        const say = (state, reason) => noteSentence({state, at:'2026-10-29T09:00:00Z', changes:[], reason});
        r.said = [
          say('waiting', 'needs OpenFit app level 2; this OpenFit is level 1'),
          say('failed', "the update doesn't load, so 'openfitavi.hello' is unchanged: app.py failed to import"),
          say('failed', 'It has folders inside it, which this OpenFit can\\'t install yet.'),
          say('failed', null),
          say('removed', null),
          say('updated', null),
          noteSentence(null),
        ];
    """)
    assert r["said"] == [
        "An update is waiting for a newer OpenFit.",
        "An update couldn't be installed: the update doesn't load, so 'openfitavi.hello' is unchanged: "
        "app.py failed to import.",
        "An update couldn't be installed: It has folders inside it, which this OpenFit can't install yet.",
        "An update couldn't be installed.",
        "No longer in the app library, so it won't be updated.",
        None,
        None,
    ]


@needs_node
def test_a_fresh_update_is_a_pill_opening_whats_new_all_as_text(client):
    app = _app(library_note={"state": "updated", "at": "AT", "reason": None, "changes": [
        {"sha": "a1b2c3d4e5f6", "subject": "<img src=x onerror=alert(1)> & <b>bold</b>",
         "date": "2026-10-29T08:30:00+00:00"},
        {"sha": "0123456789ab", "subject": "Hello says hello", "date": "2026-10-20T08:30:00+00:00"},
    ]})
    r = _note_script(client, f"""
        const a = {json.dumps(app)};
        a.library_note.at = ago(1);
        const out = row(a);
        r.row = {{first: out.first, head: out.head, parts: out.parts, pill: out.pillText, pillCls: out.pillCls,
                 dialog: out.pill.attrs['aria-haspopup'], type: out.pill.type}};
        r.expected = 'Updated ' + fmtStamp(a.library_note.at);
        out.pill.onclick();
        const box = document.body.children[0].children[0];
        r.title = box.querySelector('h4').textContent;
        r.hint = box.querySelector('.hint').textContent;
        r.changes = box.querySelectorAll('.a-change').map(li => li.children.map(c => [c.className, c.textContent]));
        r.dates = [fmtStamp('2026-10-29T08:30:00+00:00'), fmtStamp('2026-10-20T08:30:00+00:00')];
        r.markup = below(box).filter(n => ['img', 'b'].includes(n.tag)).length;
        box.querySelector('[data-act="close"]').onclick();
        r.closed = document.body.children.length;
    """)
    # The pill, beside the head - a button of its own, never inside it.
    assert r["row"] == {"first": "a-line", "head": "drow arow",
                        "parts": [["chev", "›"], ["a-name", "Hello <b>"]],
                        "pill": r["expected"], "pillCls": "pill ok a-upd", "dialog": "dialog", "type": "button"}
    assert r["expected"].startswith("Updated ") and len(r["expected"]) > len("Updated ")
    # What's new: each change its date and subject - every one as text.
    assert r["title"] == "What's new in Hello <b>"
    assert r["hint"].startswith("Updated ") and r["hint"].endswith(", from the openfitavi library.")
    assert r["changes"] == [
        [["a-chg-date", r["dates"][0]], ["a-chg-subj", "<img src=x onerror=alert(1)> & <b>bold</b>"]],
        [["a-chg-date", r["dates"][1]], ["a-chg-subj", "Hello says hello"]],
    ]
    assert r["markup"] == 0
    assert r["closed"] == 0


@needs_node
def test_whats_new_with_no_change_notes_says_it_came_from_the_library(client):
    app = _app(library_note={"state": "updated", "at": "AT", "reason": None, "changes": []})
    r = _note_script(client, f"""
        const a = {json.dumps(app)};
        a.library_note.at = ago(0);
        row(a).pill.onclick();
        const box = document.body.children[0].children[0];
        r.hint = box.querySelector('.hint').textContent;
        r.list = box.querySelectorAll('.a-changes').length;
    """)
    assert r == {"hint": "Updated from the library.", "list": 0}


@needs_node
def test_an_update_fourteen_days_old_and_custom_apps_look_as_ever(client):
    old = _app(library_note={"state": "updated", "at": "AT", "reason": None, "changes": []})
    custom = _app(id="hello", library=None, library_note={"state": "failed", "at": "AT", "reason": "x", "changes": []})
    plain = _app(library_note=None)
    r = _note_script(client, f"""
        const old = {json.dumps(old)}, custom = {json.dumps(custom)}, plain = {json.dumps(plain)};
        old.library_note.at = ago(14.01);
        custom.library_note.at = ago(0);
        r.rows = [old, custom, plain, {{id: 'steps', name: 'Steps'}}].map(a => {{
          const out = row(a);
          return [out.first, out.parts, out.pill];
        }});
    """)
    looks = lambda name: ["drow arow", [["chev", "›"], ["a-name", name]], None]
    assert r["rows"] == [looks("Hello <b>"), looks("Hello <b>"), looks("Hello <b>"), looks("Steps")]


@needs_node
@pytest.mark.parametrize("state, reason, said", [
    ("waiting", "needs OpenFit app level 2; this OpenFit is level 1", "An update is waiting for a newer OpenFit."),
    ("failed", "<i>it</i> doesn't load", "An update couldn't be installed: <i>it</i> doesn't load."),
    ("removed", None, "No longer in the app library, so it won't be updated."),
])
def test_a_waiting_failed_or_removed_note_is_said_under_the_name_as_text(client, state, reason, said):
    app = _app(library_note={"state": state, "at": "2026-10-01T09:00:00Z", "reason": reason, "changes": []})
    r = _note_script(client, f"""
        const out = row({json.dumps(app)});
        r.out = [out.first, out.parts, out.pill];
        r.markup = below(out.item).filter(n => n.tag === 'i').length;
    """)
    # Said for as long as it holds: an old note too. No pill.
    assert r["out"] == ["drow arow", [["chev", "›"], ["a-name", "Hello <b>"], ["a-libnote", said]], None]
    assert r["markup"] == 0


def test_the_notes_reach_the_page_only_as_text(client):
    from test_routes import _js_function, _page

    page = _page(client)
    row = _js_function(page, "appRow")
    assert "a.library ? a.library_note : null" in row
    assert "head.appendChild(el('span','a-libnote')).textContent = said;" in row
    assert "${note" not in row and "${said" not in row
    line = _js_function(page, "updatedLine")
    assert "pill.textContent = 'Updated ' + fmtStamp(a.library_note.at);" in line
    assert "line.appendChild(head);" in line and "${" not in line and "innerHTML" not in line
    whats_new = _js_function(page, "openWhatsNew")
    assert "${" not in whats_new.split("box.querySelector('h4')")[0]  # the popup's markup is fixed
    assert "li.appendChild(el('span','a-chg-subj')).textContent = c.subject;" in whats_new
    assert "innerHTML" not in whats_new and "innerHTML" not in row
    # The pill is beside the head, in a line expandable() holds in its place.
    assert "item.appendChild(o.line || head);" in _js_function(page, "expandable")
    assert ".pill.a-upd{" in page and ".a-libnote{" in page
