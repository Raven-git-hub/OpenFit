"""
The app shelf: the apps the Admin publishes, read here and never written.

The Admin (admin/, A5) publishes the official apps to a volume every user
mounts read-only at /library (LIBRARY_DIR in main.py: $OPENFIT_LIBRARY,
/library by default), laid out as:

    /library/index.json     {published_at, sources: {openfit: {commit,
                             name, apps: {walk: {sha256}}}}}, written last
    /library/openfit        a relative link onto the source's set,
                             .sets/openfit-<commit12>/, swapped in one step
    /library/.sets/openfit-<commit12>/walk/app.yaml, app.py, ...

The official library is one source, openfit; each organisation repo the
Admin added (A6) is another - openfitavi, its apps openfitavi.frms - laid
out alike, its name (OpenFit Aviation) its repo.yaml's.

Browse reads it: list_shelf() is what GET /api/library lists, and
read_app_files() the files GET /api/library/<source>/<app> shows and
POST /api/library/<source>/<app>/install installs - from the shelf,
never a copy the page sends. And the library apps installed follow it
(B3): sync_library_apps(), at startup and every minute, swaps a new
version in wherever the index's fingerprint of an app isn't the
installed copy's - the shelf still only read - and notes what happened
in the update record, /data/app_updates.json, for the page.

The shelf is the Admin's, so this OpenFit reads it as data that could
hold anything. A source or an app is taken only from the index, by name
(LIBRARY_SOURCE, INSTALL_ID), and only where its folder resolves to a
folder inside the shelf (a source's) or inside its source's folder (an
app's); a file only if it's a regular file directly in the app's folder,
opened without following a link and checked again once open. Anything
else is left out - an app or a source logged - never followed, never
shown.
"""

import contextlib
import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import traceback
from datetime import datetime, timezone

import yaml

from apps import (
    INSTALL_ID,
    LIBRARY_APP_ID,
    LIBRARY_SOURCE,
    MANIFEST,
    AppError,
    LibraryError,
    _check_files,
    _replace_app,
    _write_lock,
    app_files,
    app_present,
    filename_problem,
    find_app,
    level_problem,
    library_of,
    manifest_description,
    manifest_replaces,
    manifest_requires,
    scan_apps,
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

# The most of a source's name Browse shows (the Admin's MAX_REPO_NAME).
MAX_SOURCE_NAME = 40


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


def _source_name(entry, source):
    """The source's name as Browse shows it: its index entry's (the name
    the Admin read from its repo.yaml), trimmed, if that's text of 1 to
    MAX_SOURCE_NAME characters - else the source's id."""
    name = entry.get("name")
    if isinstance(name, str) and 0 < len(name.strip()) <= MAX_SOURCE_NAME:
        return name.strip()
    return source


def _source(root, index, source):
    """(the source's folder, resolved, the app names its index entry
    lists, and its name - _source_name()), or None - logged - for a source
    that isn't one: not LIBRARY_SOURCE's shape, no mapping of apps, or a
    folder that doesn't resolve to one inside the shelf."""
    entry = index["sources"].get(source)
    if not LIBRARY_SOURCE.match(source) or not isinstance(entry, dict) or not isinstance(entry.get("apps"), dict):
        _log(f"skipping the source {source!r}: it isn't one the index lists as a source")
        return None
    folder = _inside(root, os.path.join(root, source))
    if folder is None:
        _log(f"skipping the source {source!r}: its folder isn't a folder inside the library")
        return None
    return folder, entry["apps"], _source_name(entry, source)


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


def _record(source_folder, source, app, source_name):
    """One app on the shelf, read - {id, source, source_name, app, name,
    description, requires, replaces, files, folders, path} - or None,
    logged, for one that isn't: a name not INSTALL_ID's, a folder that
    doesn't resolve to one inside its source's, or an app.yaml that
    doesn't read or check."""
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
    return {"id": full, "source": source, "source_name": source_name, "app": app, **manifest,
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
            folder, listed, name = found
            records.extend(r for r in (_record(folder, source, app, name) for app in sorted(listed)) if r)
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
    return _record(found[0], source, app, found[2])


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
        "source_name": record["source_name"],
        "app": record["app"],
        "name": record["name"],
        "description": record["description"],
        "files": list(record["files"]),
        "state": state,
        "replaces_name": replaces_name,
    }


def list_shelf(library_dir, apps_dir, conn):
    """What Browse lists: {available, apps: [{id, source, source_name,
    app, name, description, files, state, replaces_name}]}.

    available is false when the shelf isn't there - no folder, or no
    index.json that reads as the Admin writes it - and apps is then [].
    Every app the index lists that reads is there, sorted by name (case
    folded), then id: id <source>.<app> (openfitavi.frms), source_name
    its source's (_source_name(): OpenFit Aviation, or its id where the
    index names none), name its app.yaml's (the app's name when it says
    none), description its own
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
    return _read_files(record["id"], record["path"], record["files"])


def _read_files(app_id, folder, names):
    """The files `names` in an app's folder on the shelf, {name: text}:
    read_app_files()'s reading and its refusals - each opened without
    following a link and a regular file once open, at most MAX_APP_FILES
    and MAX_APP_BYTES in all, UTF-8 - then install's checks."""
    if len(names) > MAX_APP_FILES:
        raise LibraryError(f"{app_id} has more than {MAX_APP_FILES} files", 413)
    files, total = {}, 0
    for name in names:
        try:
            data = _read_regular(os.path.join(folder, name), MAX_APP_BYTES - total)
        except OSError:
            raise LibraryError(f"{name!r} in {app_id} isn't a plain file, so it isn't read", 404) from None
        total += len(data)
        if total > MAX_APP_BYTES:
            raise LibraryError(f"{app_id} is over {MAX_APP_BYTES >> 20} MB", 413)
        try:
            files[name] = data.decode("utf-8")
        except UnicodeDecodeError:
            raise LibraryError(f"{name!r} in {app_id} isn't text, so it can't be installed", 415) from None
    _check_files(files)
    return files


# ---------- installed library apps follow the shelf (B3) ----------

# A library app is read-only here: when the Admin publishes a new version
# of one - an organisation repo's Update apps, or the official library's -
# every OpenFit that has it installed takes it up by itself, with no
# restart: sync_library_apps(), at startup and then every
# LIBRARY_SYNC_SECONDS on the scheduler (main.py's library-sync job).
#
# The index names each app's version by its fingerprint (the Admin's
# admin_ops.fingerprint(), its sha256), and an install is the shelf's
# files byte for byte, so an installed copy whose own fingerprint() is the
# index's is current: a run with nothing to do reads the index and each
# installed library app's own files to hash them, and nothing else. One
# that differs has its new files read from the shelf, checked and swapped
# in by apps._replace_app() - the swap an Update makes - its data, Home
# tiles and their config untouched.
#
# What last happened to each is noted in the update record,
# /data/app_updates.json (APP_UPDATES_PATH in main.py) - {<app id>:
# {state, at, sha256, changes, reason}}, its latest event - and GET
# /api/apps hands it to the page as the app's library_note:
#
# - updated: the version sha256 swapped in at `at`, with the change notes
#   the index had for it (changes: up to MAX_CHANGES {sha, subject,
#   date}, newest first);
# - waiting: the version sha256 needs a newer OpenFit (requires above
#   APP_LEVEL; reason level_problem()'s) - left alone;
# - failed: the version sha256 couldn't be installed (reason why: it
#   doesn't read, has folders inside it, check_app() refuses it, or it
#   doesn't load) - left alone, the old copy as it was;
# - removed: the app, or its source, is no longer on the shelf - left as
#   it is, never updated again until it's back.
#
# A version waiting or failed is never tried again: only a new version -
# another sha256 on the shelf - is. And the shelf not being there (no
# folder, no index that reads) is nothing to do: no app is marked
# removed for it.

LIBRARY_SYNC_SECONDS = 60

UPDATED = "updated"
WAITING = "waiting"
FAILED = "failed"
REMOVED = "removed"
NOTE_STATES = (UPDATED, WAITING, FAILED, REMOVED)
NOTE_FIELDS = ("state", "at", "sha256", "changes", "reason")

# The change notes an app's index entry carries, as the Admin writes them
# (admin_ops' CHANGE_NOTES and MAX_SUBJECT).
MAX_CHANGES = 10
MAX_SUBJECT = 200
CHANGE_FIELDS = ("sha", "subject", "date")

# The most of a reason the record keeps.
MAX_REASON = 500

# The most of the record read: an entry is well under a few KB.
MAX_NOTES_BYTES = 1024 * 1024

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SHORT_SHA = re.compile(r"[0-9a-f]{12}\Z")
_STAMP = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ\Z")
# git's %cI: strict ISO 8601, its offset or Z.
_CHANGE_DATE = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:Z|[+-]\d\d:\d\d)\Z")


def fingerprint(folder):
    """A hash of a folder's files (dotfiles and caches left out): the
    Admin's admin_ops.fingerprint(), to the byte, so an installed copy's
    is the one the shelf's index gives its version - the relative path of
    every file, in sorted order through sorted folders, then the sha256 of
    its bytes; dot-folders, __pycache__, dotfiles and .pyc left out, as
    the Admin never publishes them - so the __pycache__ loading app.py
    leaves never makes a current install look changed. None for no
    folder."""
    h = hashlib.sha256()
    if not os.path.isdir(folder):
        return None
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d != "__pycache__")
        for name in sorted(files):
            if name.startswith(".") or name.endswith(".pyc"):
                continue
            path = os.path.join(root, name)
            h.update(os.path.relpath(path, folder).encode() + b"\0")
            try:
                with open(path, "rb") as f:
                    h.update(hashlib.sha256(f.read()).digest())
            except OSError:
                h.update(b"?")
    return h.hexdigest()


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _valid_change(change):
    return (isinstance(change, dict) and set(change) == set(CHANGE_FIELDS)
            and isinstance(change["sha"], str) and bool(_SHORT_SHA.match(change["sha"]))
            and isinstance(change["subject"], str) and len(change["subject"]) <= MAX_SUBJECT
            and isinstance(change["date"], str) and bool(_CHANGE_DATE.match(change["date"])))


def _changes(raw):
    """The change notes of an app's index entry, as the record keeps them:
    the first MAX_CHANGES that are {sha (12 hex), subject (at most
    MAX_SUBJECT characters), date (ISO 8601)} - any other left out, as
    the index is data that could hold anything - [] for none."""
    if not isinstance(raw, list):
        return []
    return [{k: c[k] for k in CHANGE_FIELDS} for c in raw if _valid_change(c)][:MAX_CHANGES]


def _valid_note(note):
    return (isinstance(note, dict) and set(note) == set(NOTE_FIELDS)
            and note["state"] in NOTE_STATES
            and isinstance(note["at"], str) and bool(_STAMP.match(note["at"]))
            and (note["sha256"] is None or (isinstance(note["sha256"], str) and bool(_SHA256.match(note["sha256"]))))
            and isinstance(note["changes"], list) and len(note["changes"]) <= MAX_CHANGES
            and all(_valid_change(c) for c in note["changes"])
            and (note["reason"] is None or (isinstance(note["reason"], str) and len(note["reason"]) <= MAX_REASON)))


# The record's (mtime_ns, size) when it last read bad, so a bad file is
# logged once, not on every page load.
_bad_notes = [None]


def read_notes(path):
    """The update record at `path`, {<app id>: {state, at, sha256,
    changes, reason}}, read strictly: a file that isn't JSON, isn't a
    mapping of library app ids (LIBRARY_APP_ID) to entries of exactly
    NOTE_FIELDS - each its type, as sync_library_apps() writes them - is
    treated as empty, and logged (once per change to it). No file is
    empty too."""
    try:
        data = _read_regular(path, MAX_NOTES_BYTES)
        st = os.stat(path)
    except FileNotFoundError:
        return {}
    except OSError as e:
        data, st = None, e
    notes = None
    if isinstance(data, bytes) and len(data) <= MAX_NOTES_BYTES:
        try:
            notes = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            notes = None
    if (isinstance(notes, dict)
            and all(isinstance(k, str) and LIBRARY_APP_ID.match(k) and _valid_note(v) for k, v in notes.items())):
        return notes
    mark = (st.st_mtime_ns, st.st_size) if isinstance(st, os.stat_result) else str(st)
    if _bad_notes[0] != mark:
        _bad_notes[0] = mark
        _log(f"the update record {path} doesn't read as one, so it's taken as empty")
    return {}


def _write_notes(path, notes):
    """The update record, written whole or not at all: a temp file beside
    it, flushed to disk, then renamed over it."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".app_updates-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(notes, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp)
        raise


def library_note(note):
    """What GET /api/apps says of an app's latest event, for the page:
    {state, at, changes, reason} - the record's entry less its sha256 -
    or None for none."""
    if not note:
        return None
    return {"state": note["state"], "at": note["at"], "changes": [dict(c) for c in note["changes"]],
            "reason": note["reason"]}


def _listed(index, source, app):
    """The version the index lists of `source`'s `app` - {sha256, changes},
    sha256 None where its entry doesn't give one as the Admin writes it -
    or None when it isn't listed: no such source, or the source doesn't
    list the app. Only the index is read."""
    entry = index["sources"].get(source)
    if not isinstance(entry, dict) or not isinstance(entry.get("apps"), dict) or app not in entry["apps"]:
        return None
    listed = entry["apps"][app]
    if not isinstance(listed, dict):
        return {"sha256": None, "changes": []}
    sha = listed.get("sha256")
    return {"sha256": sha if isinstance(sha, str) and _SHA256.match(sha) else None,
            "changes": _changes(listed.get("changes"))}


class _Waiting(Exception):
    """A new version this OpenFit can't run yet: it needs a newer one."""


def _update_files(root, index, source, app):
    """The files of the version of `source`'s `app` on the shelf, {name:
    text}, if it's one this OpenFit can install - in order: its app.yaml
    reads, and its `requires` is no more than APP_LEVEL (else _Waiting,
    level_problem()'s words); its own id is its bare name; it has no
    folder inside it; each file reads as read_app_files() reads one. The
    folders found as list_shelf() finds them, every path held inside the
    shelf. Else AppError or LibraryError, saying why. check_app() is
    _replace_app()'s, run as it swaps the files in."""
    found = _source(root, index, source)
    if found is None:
        raise LibraryError(f"the {source} library's folder isn't on the shelf as its index says")
    folder = _inside(found[0], os.path.join(found[0], app))
    if folder is None:
        raise LibraryError(f"{source}.{app}'s folder isn't on the shelf as its index says")
    try:
        names, folders = _listing(folder)
    except OSError as e:
        raise LibraryError(f"can't list its files: {e.strerror or e}") from None
    if MANIFEST not in names:
        raise AppError(f"it has no {MANIFEST}")
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
    # First: one written for a newer OpenFit may mean something by the
    # rest that this one can't read, so that's the reason it waits.
    problem = level_problem(manifest_requires(raw))
    if problem:
        raise _Waiting(problem)
    if raw.get("id") != app:
        raise AppError(f"a library app's own id is its bare name: expected {app!r}, got {raw.get('id')!r}")
    if folders:
        raise AppError(HAS_FOLDERS)
    return _read_files(f"{source}.{app}", folder, names)


def _installed_files(folder):
    """The installed copy's files as text, {name: text} - app_files()'s,
    each read as UTF-8 - or None if one won't read."""
    files = {}
    for name in app_files(folder):
        try:
            with open(os.path.join(folder, name), encoding="utf-8", newline="") as f:
                files[name] = f.read()
        except (OSError, UnicodeDecodeError):
            return None
    return files


def _note(state, sha256, now, changes=(), reason=None):
    if reason is not None:
        reason = reason if len(reason) <= MAX_REASON else reason[:MAX_REASON - 1] + "…"
    return {"state": state, "at": now, "sha256": sha256, "changes": list(changes), "reason": reason}


# A version found already installed as far as this OpenFit installs one -
# the shelf's files the same as the installed copy's, though the
# fingerprints differ (a file on the shelf an install never takes, such
# as a name it refuses) - by app id: (shelf sha256, installed
# fingerprint), so it isn't read again until either changes. For this
# process's life only.
_settled = {}

# One run at a time: the startup run and the scheduler's never overlap,
# and a run that finds one going leaves it to it.
_running = threading.Lock()

# What a run does with an app's note: leave it, or drop it.
_KEEP, _DROP = object(), object()


def _follow(apps_dir, root, index, installed, note, now):
    """One installed library app brought in line with the shelf: its new
    note, _DROP (a warning that no longer holds - it's current again) or
    _KEEP (nothing to say)."""
    app_id = installed.id
    source, app = app_id.split(".", 1)
    listed = _listed(index, source, app)
    if listed is None:
        if note and note["state"] == REMOVED:
            return _KEEP
        _log(f"{app_id} is no longer in the app library, so it won't be updated")
        return _note(REMOVED, None, now)
    sha, old = listed["sha256"], fingerprint(installed.path)
    if sha is not None and old == sha:
        return _DROP if note and note["state"] != UPDATED else _KEEP
    # A version tried before - swapped in, waiting or failed - is never
    # tried again: only another one is.
    if (note and note["state"] != REMOVED and note["sha256"] == sha) or _settled.get(app_id) == (sha, old):
        return _KEEP
    if sha is None:
        reason = "the app library's index doesn't say which version it has"
        _log(f"couldn't update {app_id}: {reason}")
        return _note(FAILED, None, now, reason=reason)

    with _write_lock:
        # Afresh, under the lock: an uninstall, or a reinstall, may have
        # come between.
        current = find_app(apps_dir, app_id)
        if current is None:
            return _KEEP
        old = fingerprint(current.path)
        if old == sha:
            return _KEEP
        try:
            files = _update_files(root, index, source, app)
            if files == _installed_files(current.path):
                _settled[app_id] = (sha, old)
                return _KEEP
            _replace_app(apps_dir, app_id, files)
        except _Waiting as e:
            _log(f"{app_id}: an update is waiting for a newer OpenFit: {e}")
            return _note(WAITING, sha, now, reason=str(e))
        except (AppError, LibraryError) as e:
            _log(f"couldn't update {app_id}: {e}")
            return _note(FAILED, sha, now, reason=str(e))
    _log(f"updated {app_id} ({(old or '')[:7]} → {sha[:7]})")
    return _note(UPDATED, sha, now, changes=listed["changes"])


def sync_library_apps(apps_dir, library_dir, notes_path, now=None):
    """Bring every installed library app in line with the shelf: the one
    way a library app's files change (B3).

    Nothing to do when the shelf isn't mounted - no folder, or no
    index.json that reads. Else the index is read once, and each app
    installed - loading - with a library id (<source>.<app>) is held to
    the version its source's entry lists: its own fingerprint() the
    index's sha256 is current; another version is read from the shelf
    and, if this OpenFit can install it (_update_files(), then
    check_app() and the load check), swapped in by apps._replace_app()
    under _write_lock - its cached load dropped, so the next render runs
    the new code. A custom app is never touched. The app's data, Home
    tiles and their config are never touched either.

    Each event - updated, waiting, failed, removed - is noted in the
    update record at `notes_path` (read_notes()) and logged, one line.
    A version waiting or failed isn't tried again until the shelf has
    another; a waiting, failed or removed note goes once the app is
    current again, and any app's once it's no longer on the volume
    (app_present()). `now`, a UTC stamp, for the record (the tests').

    -> {<app id>: its new note, or None where one was dropped}, for what
    changed in the record this run. Never raises for one app: a failure
    is that app's note. A run that finds another going does nothing.
    """
    if not _running.acquire(blocking=False):
        return {}
    try:
        root = os.path.realpath(library_dir)
        index = _index(root) if os.path.isdir(root) else None
        if index is None:
            return {}
        installed = [a for a in scan_apps(apps_dir) if library_of(a.id)]
        if not installed and not os.path.lexists(notes_path):
            return {}
        now = now or _now()
        notes = read_notes(notes_path)
        changed = {}
        # An app uninstalled takes its note with it, so one installed again
        # starts afresh - but not one that's there and just won't load.
        for app_id in [i for i in notes if not app_present(apps_dir, i)]:
            del notes[app_id]
            changed[app_id] = None
        for app in installed:
            try:
                result = _follow(apps_dir, root, index, app, notes.get(app.id), now)
            except Exception as e:
                # Never one app's trouble the others': that app's failure.
                traceback.print_exc()
                listed = _listed(index, *app.id.split(".", 1))
                result = _note(FAILED, listed["sha256"] if listed else None, now,
                               reason=f"{type(e).__name__}: {e}")
                _log(f"couldn't update {app.id}: {result['reason']}")
            if result is _KEEP:
                continue
            if result is _DROP:
                notes.pop(app.id, None)
                changed[app.id] = None
            else:
                notes[app.id] = result
                changed[app.id] = result
        if changed:
            try:
                _write_notes(notes_path, notes)
            except OSError as e:
                _log(f"couldn't write the update record {notes_path}: {e.strerror or e}")
        return changed
    finally:
        _running.release()
