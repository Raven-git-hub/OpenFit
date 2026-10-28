"""
The app shelf: the apps the Admin publishes, read here and never written.

The Admin (admin/, A5) publishes the official apps to a volume every user
mounts read-only at /library (LIBRARY_DIR in main.py: $OPENFIT_LIBRARY,
/library by default), laid out as:

    /library/index.json     {published_at, sources: {openfit: {commit,
                             apps: {frms: {sha256}}}}}, written last
    /library/openfit        a relative link onto the source's set,
                             .sets/openfit-<commit12>/, swapped in one step
    /library/.sets/openfit-<commit12>/frms/app.yaml, app.py, ...

Browse reads it: list_shelf() is what GET /api/library lists, and
read_app_files() the files GET /api/library/<source>/<app> shows and
POST /api/library/<source>/<app>/install installs - from the shelf,
never a copy the page sends.

The shelf is the Admin's, so this OpenFit reads it as data that could
hold anything. A source or an app is taken only from the index, by name
(LIBRARY_SOURCE, INSTALL_ID), and only where its folder resolves to a
folder inside the shelf (a source's) or inside its source's folder (an
app's); a file only if it's a regular file directly in the app's folder,
opened without following a link and checked again once open. Anything
else is left out - an app or a source logged - never followed, never
shown.
"""

import json
import os
import stat

import yaml

from apps import (
    INSTALL_ID,
    LIBRARY_SOURCE,
    MANIFEST,
    AppError,
    LibraryError,
    _check_files,
    app_present,
    filename_problem,
    find_app,
    level_problem,
    manifest_description,
    manifest_replaces,
    manifest_requires,
)
from host import app_source

INDEX = "index.json"

# The most of the index and of an app.yaml read: the Admin's are a few KB.
MAX_INDEX_BYTES = 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024

# The most of one app the Admin publishes (admin_ops' MAX_APP_FILES and
# MAX_APP_BYTES): more than that was never its, so it isn't read.
MAX_APP_FILES = 64
MAX_APP_BYTES = 16 * 1024 * 1024

# An app's state in Browse, the first that applies: already installed;
# one this OpenFit can't install yet (it has folders inside it); one that
# needs a newer OpenFit (requires > APP_LEVEL); one that replaces an app
# whose data is still here (B2 moves it across); else available.
INSTALLED = "installed"
UNSUPPORTED = "unsupported"
NEEDS_UPDATE = "needs_update"
REPLACES_PENDING = "replaces_pending"
AVAILABLE = "available"

# Why an app with a folder inside it is unsupported.
HAS_FOLDERS = "It has folders inside it, which this OpenFit can't install yet."


def _log(message):
    print(f"[library] {message}")


def _inside(root, path):
    """`path` resolved, if it's a folder inside `root` (resolved already),
    else None. Links are followed - a source is a link onto its set - but
    never out of `root`."""
    real = os.path.realpath(path)
    if real != root and os.path.commonpath([root, real]) == root and os.path.isdir(real):
        return real
    return None


def _read_regular(path, limit):
    """Up to `limit` + 1 bytes of the regular file at `path`: opened
    without following a link (nor blocking on a pipe) and checked to be a
    regular file once open, so one swapped for a link since it was listed
    is refused all the same. OSError if it isn't one."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with open(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise OSError(f"{path} isn't a regular file")
        return f.read(limit + 1)


def _index(root):
    """index.json as the Admin writes it - a mapping with a mapping of
    sources - or None if it's missing or doesn't read so."""
    try:
        data = _read_regular(os.path.join(root, INDEX), MAX_INDEX_BYTES)
        index = json.loads(data.decode("utf-8")) if len(data) <= MAX_INDEX_BYTES else None
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(index, dict) or not isinstance(index.get("sources"), dict):
        return None
    return index


def _source(root, index, source):
    """(the source's folder, resolved, and the app names its index entry
    lists), or None - logged - for a source that isn't one: not
    LIBRARY_SOURCE's shape, no mapping of apps, or a folder that doesn't
    resolve to one inside the shelf."""
    entry = index["sources"].get(source)
    if not LIBRARY_SOURCE.match(source) or not isinstance(entry, dict) or not isinstance(entry.get("apps"), dict):
        _log(f"skipping the source {source!r}: it isn't one the index lists as a source")
        return None
    folder = _inside(root, os.path.join(root, source))
    if folder is None:
        _log(f"skipping the source {source!r}: its folder isn't a folder inside the library")
        return None
    return folder, entry["apps"]


def _listing(folder):
    """An app folder's files - their names, sorted: the regular files
    directly in it, never a link, a dotfile, a .pyc or a name an install
    refuses (filename_problem()) - and whether it has a folder inside it
    (a dot-folder or __pycache__ aside)."""
    files, folders = [], False
    with os.scandir(folder) as entries:
        for entry in entries:
            if entry.name.startswith(".") or entry.name == "__pycache__":
                continue
            if entry.is_dir(follow_symlinks=False):
                folders = True
            elif (entry.is_file(follow_symlinks=False) and not entry.name.endswith(".pyc")
                  and not filename_problem(entry.name)):
                files.append(entry.name)
    return sorted(files), folders


def _manifest(folder, source, app):
    """What Browse reads of the app's app.yaml - {name, description,
    requires, replaces} - by the scanner's own field rules, or AppError.
    Its id must be its folder's name: install takes the id from the
    manifest, so an app listed as openfit.frms installs as openfit.frms."""
    try:
        data = _read_regular(os.path.join(folder, MANIFEST), MAX_MANIFEST_BYTES)
    except OSError as e:
        raise AppError(f"can't read {MANIFEST}: {e.strerror or e}") from None
    if len(data) > MAX_MANIFEST_BYTES:
        raise AppError(f"{MANIFEST} is over {MAX_MANIFEST_BYTES // 1024} KB")
    try:
        raw = yaml.safe_load(data.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError) as e:
        raise AppError(f"can't read {MANIFEST}: {' '.join(str(e).split())}") from None
    if not isinstance(raw, dict):
        raise AppError(f"{MANIFEST} must be a mapping")
    if raw.get("id") != app:
        raise AppError(f"its id must be its folder's name, {app!r}: {raw.get('id')!r}")
    name = raw.get("name", app)
    if not isinstance(name, str) or not name.strip():
        raise AppError("name must be a non-empty string")
    return {
        "name": name.strip(),
        "description": manifest_description(raw),
        "requires": manifest_requires(raw),
        "replaces": manifest_replaces(raw, own=f"{source}.{app}"),
    }


def _record(source_folder, source, app):
    """One app on the shelf, read - {id, source, app, name, description,
    requires, replaces, files, folders, path} - or None, logged, for one
    that isn't: a name not INSTALL_ID's, a folder that doesn't resolve to
    one inside its source's, or an app.yaml that doesn't read or check."""
    full = f"{source}.{app}"
    if not INSTALL_ID.match(app):
        _log(f"skipping {source}/{app!r}: it isn't an app's name")
        return None
    folder = _inside(source_folder, os.path.join(source_folder, app))
    if folder is None:
        _log(f"skipping {full}: its folder isn't a folder inside the library")
        return None
    try:
        manifest = _manifest(folder, source, app)
        files, folders = _listing(folder)
    except AppError as e:
        _log(f"skipping {full}: {e}")
        return None
    except OSError as e:
        _log(f"skipping {full}: can't list its files: {e.strerror or e}")
        return None
    return {"id": full, "source": source, "app": app, **manifest,
            "files": files, "folders": folders, "path": folder}


def _shelf(library_dir):
    """(whether the shelf is there, every app on it that reads)."""
    root = os.path.realpath(library_dir)
    index = _index(root) if os.path.isdir(root) else None
    if index is None:
        return False, []
    records = []
    for source in sorted(index["sources"]):
        found = _source(root, index, source)
        if found:
            folder, listed = found
            records.extend(r for r in (_record(folder, source, app) for app in sorted(listed)) if r)
    return True, records


def _find(library_dir, source, app):
    """The one app's record, as _shelf() reads it, or None: the shelf
    isn't there, or the app isn't on it."""
    root = os.path.realpath(library_dir)
    index = _index(root) if os.path.isdir(root) else None
    if index is None or source not in index["sources"]:
        return None
    found = _source(root, index, source)
    if not found or app not in found[1]:
        return None
    return _record(found[0], source, app)


def _has_rows(conn, source):
    """Whether `source` has a metric, session or sample row at all."""
    return any(
        conn.execute(f"SELECT 1 FROM {table} WHERE source = ? LIMIT 1", (source,)).fetchone()
        for table in ("metrics", "sessions", "samples")
    )


def _state(record, apps_dir, conn):
    """(the app's state, the name of the app it replaces - for
    replaces_pending, else None)."""
    if app_present(apps_dir, record["id"]):
        return INSTALLED, None
    if record["folders"]:
        return UNSUPPORTED, None
    if level_problem(record["requires"]):
        return NEEDS_UPDATE, None
    # An app it replaces still installed - loading or not - says so by its
    # name; one gone but whose data is still here, by its id.
    for old in record["replaces"]:
        if app_present(apps_dir, old):
            installed = find_app(apps_dir, old)
            return REPLACES_PENDING, installed.name if installed else old
    for old in record["replaces"]:
        if _has_rows(conn, app_source(old)):
            return REPLACES_PENDING, old
    return AVAILABLE, None


def _entry(record, apps_dir, conn):
    state, replaces_name = _state(record, apps_dir, conn)
    return {
        "id": record["id"],
        "source": record["source"],
        "app": record["app"],
        "name": record["name"],
        "description": record["description"],
        "files": list(record["files"]),
        "state": state,
        "replaces_name": replaces_name,
    }


def list_shelf(library_dir, apps_dir, conn):
    """What Browse lists: {available, apps: [{id, source, app, name,
    description, files, state, replaces_name}]}.

    available is false when the shelf isn't there - no folder, or no
    index.json that reads as the Admin writes it - and apps is then [].
    Every app the index lists that reads is there, sorted by name (case
    folded), then id: id <source>.<app> (openfit.frms), name its
    app.yaml's (the app's name when it says none), description its own
    or None, files the regular files directly in its folder, and state
    the first that applies - installed, unsupported, needs_update,
    replaces_pending (replaces_name then the app it replaces: that one's
    name while it's installed, else its id), available.
    """
    available, records = _shelf(library_dir)
    apps = [_entry(record, apps_dir, conn) for record in records]
    apps.sort(key=lambda a: (a["name"].casefold(), a["id"]))
    return {"available": available, "apps": apps}


def shelf_app(library_dir, apps_dir, conn, source, app):
    """One app as list_shelf() lists it, or None: the shelf isn't there, or
    the app isn't on it."""
    record = _find(library_dir, source, app)
    return _entry(record, apps_dir, conn) if record else None


def refusal(entry):
    """Why the app list_shelf() lists as `entry` can't be installed - what
    the install route says, 409 - or None if it can."""
    state = entry["state"]
    if state == INSTALLED:
        return "Already installed"
    if state == UNSUPPORTED:
        return HAS_FOLDERS
    if state == NEEDS_UPDATE:
        return "Needs a newer OpenFit."
    if state == REPLACES_PENDING:
        return f"{entry['name']} replaces your {entry['replaces_name']}. Moving it across comes in a coming update."
    return None


def read_app_files(library_dir, source, app):
    """An app's files on the shelf, {name: text}, by name: each regular
    file directly in its folder (as list_shelf() lists them), as UTF-8
    text - exactly the bytes published.

    The app is found as list_shelf() finds it, every path held inside
    the shelf; each file opened without following a link and checked to
    be a regular file once open; at most MAX_APP_FILES files and
    MAX_APP_BYTES in all; then install's own checks on the names and
    texts (_check_files()).

    Raises LibraryError: 404 for an app that isn't on the shelf, or a
    file that's no longer a regular file; 413 for an app over the
    Admin's limits; 415 for a file that isn't UTF-8 text, named; 400 for
    one an install refuses.
    """
    record = _find(library_dir, source, app)
    if record is None:
        raise LibraryError(f"{source}.{app} isn't in the app library", 404)
    if len(record["files"]) > MAX_APP_FILES:
        raise LibraryError(f"{record['id']} has more than {MAX_APP_FILES} files", 413)
    files, total = {}, 0
    for name in record["files"]:
        try:
            data = _read_regular(os.path.join(record["path"], name), MAX_APP_BYTES - total)
        except OSError:
            raise LibraryError(f"{name!r} in {record['id']} isn't a plain file, so it isn't read", 404) from None
        total += len(data)
        if total > MAX_APP_BYTES:
            raise LibraryError(f"{record['id']} is over {MAX_APP_BYTES >> 20} MB", 413)
        try:
            files[name] = data.decode("utf-8")
        except UnicodeDecodeError:
            raise LibraryError(f"{name!r} in {record['id']} isn't text, so it can't be installed", 415) from None
    _check_files(files)
    return files
