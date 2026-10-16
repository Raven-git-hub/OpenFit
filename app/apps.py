"""
Installed apps: the scanner and loader the app host runs on.

An app is a folder in the apps directory (APPS_DIR, /data/apps by
default) holding its manifest and, usually, its engine:

    <apps dir>/steps/
        app.yaml    the manifest: id, name, sizes, reads, config
                    (sizes the tile's allowed sizes in cells, the default
                    first; config an ordered list of fields:
                    {key, label, type, default})
        app.py      def render(ctx, cfg): -> a display spec (see host.py)

app.py is optional: an app with a manifest alone loads and lists, and
its tile says it has nothing to draw. Any other files in the folder
(reference data, helpers) are the app's own; the host doesn't read them.

scan_apps() lists every app that loads. A folder without an app.yaml
isn't an app and is passed over quietly; one with an app.yaml that
doesn't load - bad YAML, a manifest field of the wrong type, an app.py
that fails to import or has no callable render - is logged and skipped,
so one broken app never stops the rest from being listed, and never
takes the server down.

Loading imports app.py from its absolute path; nothing is added to
sys.path or sys.modules. A loaded app is kept until either of its files
changes, so a scan is cheap enough to run on every request, a newly
installed or edited app is picked up without a restart, and a broken one
is logged once per change rather than on every page load.

This is the one way the host finds an app: the Home tab's routes use it
now, and the Library reuses it. install_app() is the one way one is
added from the browser (POST /api/apps), uninstall_app() the one way one
is removed (DELETE /api/apps/<id>), and read_app_file() the one way a
file in one is read back (GET /api/app/<id>/file): see their sections,
below.
"""

import importlib.util
import os
import re
import secrets
import shutil
import threading
import traceback
from dataclasses import dataclass, field

import yaml

MANIFEST = "app.yaml"
MODULE = "app.py"

# The most cells a tile may span, either way: Home's grid is this many
# cells across, and a tile is held to the same height.
MAX_CELLS = 6

# An app id is part of its URL (/api/app/<id>/render).
APP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")


class AppError(Exception):
    """An app folder that doesn't load, and why."""


@dataclass(frozen=True)
class InstalledApp:
    id: str
    name: str
    sizes: list
    default_size: dict
    reads: list
    config: list
    path: str
    render: object = field(repr=False)

    def summary(self):
        """What GET /api/apps lists for it: the manifest's id, name, sizes
        (default_size their first) and reads, and the names of the files
        in its folder."""
        return {
            "id": self.id,
            "name": self.name,
            "default_size": dict(self.default_size),
            "sizes": [dict(size) for size in self.sizes],
            "reads": list(self.reads),
            "files": app_files(self.path),
        }


def _log(message):
    print(f"[apps] {message}")


def _cells(value):
    """Whether `value` is a whole number of cells, 1 to MAX_CELLS."""
    # bool is an int to Python; a manifest's `w: true` is a mistake.
    return isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= MAX_CELLS


def _size(raw, where):
    """One tile size, {w, h}, from the manifest's `where`, or AppError."""
    if not isinstance(raw, dict):
        raise AppError(f"{where} must be a mapping of w and h, such as {{w: 1, h: 1}}")
    if any(isinstance(k, str) and re.match(r"[wh]:", k) for k in raw):
        # YAML reads {w:1,h:1} as the keys 'w:1' and 'h:1', each null.
        raise AppError(f"{where} needs a space after each colon: write {{w: 1, h: 1}}, not {{w:1,h:1}}")
    for axis in ("w", "h"):
        if not _cells(raw.get(axis)):
            raise AppError(f"{where}.{axis} must be a whole number from 1 to {MAX_CELLS}")
    return {"w": raw["w"], "h": raw["h"]}


def tile_size(raw):
    """`raw` as a tile size, {w, h}, if it is one - each a whole number
    of cells, 1 to MAX_CELLS - else None."""
    try:
        return _size(raw, "size")
    except AppError:
        return None


# Why an app with no size doesn't load, or install.
NO_SIZE = (
    "no tile size: an app declares the sizes it's drawn at, such as "
    f"sizes: [{{w: 1, h: 1}}] - w and h in cells, 1 to {MAX_CELLS}"
)


def manifest_sizes(raw):
    """The manifest's tile sizes, [{w, h}, ...], its default first.

    `sizes` is the canonical field: a non-empty list of sizes, each
    {w, h} in cells (1 to MAX_CELLS), none listed twice, the first the
    default - a fixed-size app lists one. `default_size: {w, h}` alone,
    the older form, is a list of one. Both may be given (an app written
    for hosts either side of `sizes`) when default_size is the first of
    sizes. Required: an app with neither has no size, so it doesn't
    load and won't install. The host never makes one up.

    raw: the parsed app.yaml, a dict. Raises AppError, saying why.
    """
    sizes, default = raw.get("sizes"), raw.get("default_size")
    if sizes is None and default is None:
        raise AppError(NO_SIZE)
    if sizes is None:
        return [_size(default, "default_size")]
    if not isinstance(sizes, list) or not sizes:
        raise AppError("sizes must be a list of one or more {w, h}, such as [{w: 1, h: 1}]")
    listed = []
    for i, size in enumerate(sizes):
        size = _size(size, f"sizes[{i}]")
        if size in listed:
            raise AppError(f"sizes lists {size['w']}×{size['h']} twice")
        listed.append(size)
    if default is not None and _size(default, "default_size") != listed[0]:
        raise AppError("default_size must be the first of sizes: the first size is the default")
    return listed


def _config(raw):
    """The manifest's config fields, in order: [{key, label, type, default}, ...].

    A list, not a mapping, because the order is the order the app's
    config form will show them in. Each field needs a key, once; label,
    type and default are the app's to give and aren't checked yet.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AppError("config must be a list of fields, each {key, label, type, default}")
    fields, keys = [], set()
    for field in raw:
        if not isinstance(field, dict):
            raise AppError(f"each config field must be a mapping: {field!r}")
        key = field.get("key")
        if not isinstance(key, str) or not key:
            raise AppError(f"each config field needs a key: {field!r}")
        if key in keys:
            raise AppError(f"config field {key!r} is listed twice")
        keys.add(key)
        fields.append(dict(field))
    return fields


def _manifest(folder):
    """The folder's manifest, checked and with defaults filled in."""
    try:
        with open(os.path.join(folder, MANIFEST), encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as e:
        # YAML errors run over several lines; the log keeps one per app.
        reason = " ".join(str(e).split())
        raise AppError(f"can't read {MANIFEST}: {reason}") from None
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise AppError(f"{MANIFEST} must be a mapping")

    # The id defaults to the folder's name, the name to the id.
    app_id = raw.get("id", os.path.basename(folder))
    if not isinstance(app_id, str) or not APP_ID.match(app_id):
        raise AppError(
            f"id must be letters, digits, '-' and '_', starting with a letter or digit: {app_id!r}"
        )
    name = raw.get("name", app_id)
    if not isinstance(name, str) or not name.strip():
        raise AppError("name must be a non-empty string")
    reads = raw.get("reads") or []
    if not isinstance(reads, list) or not all(isinstance(key, str) for key in reads):
        raise AppError("reads must be a list of metric keys")
    sizes = manifest_sizes(raw)
    return {
        "id": app_id,
        "name": name.strip(),
        "sizes": sizes,
        "default_size": dict(sizes[0]),
        "reads": list(reads),
        "config": _config(raw.get("config")),
    }


# What the tile of an app with no app.py says.
NOTHING_TO_DRAW = f"Nothing to draw yet: this app has no {MODULE}"


def _nothing_to_draw(ctx, cfg):
    """The render() of an app with a manifest alone."""
    return {"type": "empty", "message": NOTHING_TO_DRAW}


def _render_function(folder, app_id):
    path = os.path.join(folder, MODULE)
    if not os.path.isfile(path):
        # app.py is optional: the app still loads and lists, and its
        # tile says there's nothing to draw. Adding one later is picked
        # up like any edit (_signature watches it).
        return _nothing_to_draw
    spec = importlib.util.spec_from_file_location(f"openfit_app_{app_id}", path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        # The traceback points at the line in the app that failed.
        traceback.print_exc()
        raise AppError(f"{MODULE} failed to import: {type(e).__name__}: {e}") from None
    render = getattr(module, "render", None)
    if not callable(render):
        raise AppError(f"{MODULE} has no render(ctx, cfg) function")
    return render


def load_app(folder):
    """The app in `folder`, or AppError saying why it doesn't load."""
    manifest = _manifest(folder)
    render = _render_function(folder, manifest["id"])
    return InstalledApp(path=folder, render=render, **manifest)


def _signature(folder):
    """What changes when either file does: (mtime_ns, size) of each, or None."""
    marks = []
    for name in (MANIFEST, MODULE):
        try:
            st = os.stat(os.path.join(folder, name))
        except OSError:
            marks.append(None)
        else:
            marks.append((st.st_mtime_ns, st.st_size))
    return tuple(marks)


# folder -> (its signature when loaded, the InstalledApp or the AppError)
_loaded = {}
_loaded_lock = threading.Lock()


def _load_cached(folder):
    signature = _signature(folder)
    with _loaded_lock:
        cached = _loaded.get(folder)
        if cached and cached[0] == signature:
            return cached[1]
    try:
        result = load_app(folder)
    except AppError as e:
        result = e
        # Logged when it's loaded, so once per change to the app.
        _log(f"skipping {folder}: {e}")
    with _loaded_lock:
        _loaded[folder] = (signature, result)
    return result


def scan_apps(apps_dir):
    """Every app in `apps_dir` that loads, in folder-name order.

    A missing apps_dir means no apps. Two folders claiming one id: the
    first, by folder name, wins and the other is logged and skipped.
    """
    try:
        names = sorted(os.listdir(apps_dir))
    except OSError:
        return []
    apps, seen = [], {}
    for name in names:
        folder = os.path.join(os.path.abspath(apps_dir), name)
        if name.startswith(".") or not os.path.isfile(os.path.join(folder, MANIFEST)):
            continue
        loaded = _load_cached(folder)
        if isinstance(loaded, AppError):
            continue
        if loaded.id in seen:
            _log(f"skipping {folder}: id {loaded.id!r} is already taken by {seen[loaded.id]}")
            continue
        seen[loaded.id] = folder
        apps.append(loaded)
    return apps


def find_app(apps_dir, app_id):
    """The installed app with this id, or None."""
    return next((a for a in scan_apps(apps_dir) if a.id == app_id), None)


# ---------- installing an app: POST /api/apps ----------

# The one way an app reaches the volume from the browser, and the first
# thing in OpenFit that writes executable code there. Apps are the
# owner's own trusted code (as a sync plugin is), so this doesn't defend
# against the app. It guards the install itself, and is strict about
# containment: every file lands in a new <apps dir>/<id>/ folder and
# nowhere else, whatever the request says. About correctness it is
# loose (check_app(), below): an app that's contained but malformed
# still installs, and the scanner handles it as it would a hand-placed
# one - logged and skipped, or an error tile if its render() fails.
#
# The browser checks the same things first, for quick feedback, but
# only these checks count.

# An installed app's id: its folder's name, and part of its URL. Stricter
# than the scanner's APP_ID - no capitals, no '_' - so every id this
# installs, the scanner loads. \Z, not $: $ would let a trailing newline
# through.
INSTALL_ID = re.compile(r"[a-z0-9][a-z0-9-]*\Z")

# The control characters, NUL among them: never in a file name.
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# Installs and uninstalls one at a time, so two installs can't both find
# an id free, and an uninstall can't move a folder an install is checking.
_write_lock = threading.Lock()


class LibraryError(Exception):
    """A Library request refused, and why, with the HTTP status to send."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class InstallError(LibraryError):
    """An install refused. Nothing was written."""


def taken_message(app_id):
    return f"'{app_id}' already exists — remove it first to reinstall."


def _manifest_to_install(files):
    """The app.yaml among `files`, parsed: a mapping, or InstallError."""
    if MANIFEST not in files:
        raise InstallError(f"an app needs an {MANIFEST}")
    text = files[MANIFEST]
    if not isinstance(text, str):
        raise InstallError(f"{MANIFEST} must be text")
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as e:
        # As the scanner logs it: YAML errors run over several lines.
        reason = " ".join(str(e).split())
        raise InstallError(f"{MANIFEST} doesn't parse: {reason}") from None
    if raw is None:
        raise InstallError(f"{MANIFEST} is empty: it needs at least an id")
    if not isinstance(raw, dict):
        raise InstallError(f"{MANIFEST} must be a mapping, such as id: my-app")
    return raw


def _install_id(manifest):
    """The manifest's id, if it's one an install may use as a folder name."""
    app_id = manifest.get("id")
    if app_id is None:
        raise InstallError(f"{MANIFEST} needs an id, such as id: my-app")
    if not isinstance(app_id, str) or not INSTALL_ID.match(app_id):
        raise InstallError(
            "id must be lowercase letters, digits and '-', "
            f"starting with a letter or digit: {app_id!r}"
        )
    return app_id


def filename_problem(name):
    """Why `name` can't be written into an app's folder, or None.

    A plain file name only: never empty, '.', absolute, or holding a
    '/', '\\' or '..' - anything that could name a place other than a
    new file in the folder - nor a control character.
    """
    if not isinstance(name, str) or not name:
        return "every file needs a name"
    if (
        "/" in name
        or "\\" in name
        or ".." in name
        or name == "."
        or os.path.isabs(name)
    ):
        return f"{name!r} isn't a plain file name: no '/', '\\' or '..'"
    if _CONTROL.search(name):
        return f"{name!r} has a control character in it"
    if not _is_text(name):
        return f"{name!r} isn't valid text"
    return None


def _is_text(s):
    """Whether `s` can be written as UTF-8: JSON can carry a lone surrogate, which can't."""
    try:
        s.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _taken(apps_dir, app_id):
    """Whether `app_id` is in use: its folder exists - as anything, even
    a broken link - or an app in another folder already loads under it."""
    if os.path.lexists(os.path.join(apps_dir, app_id)):
        return True
    return find_app(apps_dir, app_id) is not None


def check_app(manifest, files):
    """The correctness checks an app must pass to be installed.

    THE LAYER THAT GROWS. Past containment - app.yaml parsing to a
    mapping with a valid id, install_app()'s, met by the time this
    runs - one check so far:

    - a size. The manifest must declare the tile size(s) the app is
      drawn at (manifest_sizes(): `sizes`, or the older `default_size`),
      each w and h a whole number of cells, 1 to MAX_CELLS. An app
      without one won't deploy - the scanner would skip it anyway - so
      it's refused here, with the scanner's own reason.

    Nothing else is checked yet: not the manifest's other fields (name,
    reads, config - the scanner checks those when it loads the app), not
    app.py's syntax, and nothing is ever run. As real apps' quirks are
    learned, a check that catches a broken app before it's on the volume
    goes here, raising InstallError with a message the popup shows as-is.

    Containment is not this function's job and never moves here: those
    checks are install_app()'s, they run first, and they don't relax.

    manifest: the parsed app.yaml, a dict with a valid id.
    files: {name: text}, every name a plain file name.
    """
    try:
        manifest_sizes(manifest)
    except AppError as e:
        raise InstallError(str(e)) from None


def install_app(apps_dir, files):
    """Install an app from its files, {name: text}, as <apps_dir>/<id>/.

    In order: app.yaml must be among the files and parse; its id and
    every file name must pass the containment checks; the id must be
    free - an install only ever creates, never updates in place; then
    check_app(). Only then is anything written: the files go into a
    hidden staging folder (the scanner skips dot-folders), which is
    renamed to <id> once they're all there, so the app appears whole or
    not at all. Nothing is imported or run.

    Returns {"id", "has_app_py"}. Raises InstallError, having written
    nothing: status 400 for a bad request, manifest, id or file name,
    409 for a taken id, 500 if the files can't be written.
    """
    if not isinstance(files, dict) or not files:
        raise InstallError('expected {"files": {name: content}}, with at least an app.yaml')

    manifest = _manifest_to_install(files)
    app_id = _install_id(manifest)
    for name, content in files.items():
        problem = filename_problem(name)
        if problem:
            raise InstallError(problem)
        if not isinstance(content, str) or not _is_text(content):
            raise InstallError(f"{name!r} must be text")

    apps_dir = os.path.abspath(apps_dir)
    with _write_lock:
        if _taken(apps_dir, app_id):
            raise InstallError(taken_message(app_id), 409)
        check_app(manifest, dict(files))
        try:
            _write_app(apps_dir, app_id, files)
        except OSError as e:
            raise InstallError(f"couldn't write the app's files: {e.strerror or e}", 500) from None

    _log(f"installed {app_id}: {', '.join(sorted(files))}")
    return {"id": app_id, "has_app_py": MODULE in files}


def _write_app(apps_dir, app_id, files):
    """Write `files` into a new <apps_dir>/<app_id>/, whole or not at all."""
    os.makedirs(apps_dir, exist_ok=True)
    staging = os.path.join(apps_dir, f".install-{secrets.token_hex(8)}")
    os.mkdir(staging)
    try:
        for name, content in files.items():
            path = os.path.join(staging, name)
            # The names were checked before anything was written; this
            # is the last word, in case a check above is ever loosened.
            if os.path.dirname(os.path.abspath(path)) != staging:
                raise InstallError(f"{name!r} isn't a plain file name")
            # "x": create, never overwrite; newline="": as given.
            with open(path, "x", encoding="utf-8", newline="") as f:
                f.write(content)
        os.rename(staging, os.path.join(apps_dir, app_id))
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


# ---------- an app's files: GET /api/apps, GET /api/app/<id>/file ----------

# The Library lists an app's files and shows one on request. The name to
# show comes from the request, so it gets the installer's discipline:
# whatever it says, nothing outside the app's own folder is ever read.

# The most of one file the viewer shows: past this it's data, not
# something to read in a popup.
MAX_VIEW_BYTES = 1024 * 1024


class AppFileError(LibraryError):
    """A file read refused. Nothing was read."""


def app_files(folder):
    """The names of the files in an app's folder, sorted.

    Plain files directly in the folder only: not a sub-folder (such as
    the __pycache__ that loading app.py leaves), not a link - which could
    point anywhere - and not a name filename_problem() refuses. So every
    name listed is one read_app_file() serves.
    """
    try:
        with os.scandir(folder) as entries:
            return sorted(
                e.name
                for e in entries
                if e.is_file(follow_symlinks=False) and not filename_problem(e.name)
            )
    except OSError:
        return []


def read_app_file(app, name):
    """The text of the file `name` in an installed app's folder.

    `name` is held to the installer's rule first (filename_problem(): a
    plain name - no '/', '\\' or '..', never absolute) and must then be
    one app_files() lists: a plain file directly in the folder, not a
    link. The path it names is resolved and must still sit in the folder,
    and it's opened without following a link, in case one was swapped in
    since. Read as UTF-8, as given (line endings and all).

    Raises AppFileError: 400 for a name that isn't plain, 404 for one the
    folder doesn't hold, 413 for a file over MAX_VIEW_BYTES, 415 for one
    that isn't UTF-8 text.
    """
    problem = filename_problem(name)
    if problem:
        raise AppFileError(problem)
    if name not in app_files(app.path):
        raise AppFileError(f"{app.id} has no file {name!r}", 404)
    folder = os.path.realpath(app.path)
    path = os.path.join(folder, name)
    # The last word, as _write_app() has: in the folder, whatever the
    # checks above let through.
    if os.path.dirname(os.path.realpath(path)) != folder:
        raise AppFileError(f"{app.id} has no file {name!r}", 404)
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with open(fd, "rb") as f:
            data = f.read(MAX_VIEW_BYTES + 1)
    except OSError:
        raise AppFileError(f"{app.id} has no file {name!r}", 404) from None
    if len(data) > MAX_VIEW_BYTES:
        raise AppFileError(f"{name!r} is too large to show (over {MAX_VIEW_BYTES / 2**20:g} MB)", 413)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise AppFileError(f"{name!r} isn't text, so it can't be shown", 415) from None


# ---------- uninstalling an app: DELETE /api/apps/<id> ----------

# The first destructive action on an app. As with installing, the
# request names the app, never a path: the id is checked, then looked up
# by scanning, and what's removed is the folder the scanner loaded that
# app from - always a direct child of the apps folder.


class UninstallError(LibraryError):
    """An uninstall refused. Nothing was removed."""


def uninstall_app(apps_dir, app_id):
    """Remove the installed app with this id: its whole folder.

    The id must match APP_ID - the scanner's rule, which every listed
    app's id meets, so it can never be '..', hold a '/', or name a
    dot-folder - and an app must load under it. The folder is renamed to
    a hidden .uninstall-<hex> one beside it (one atomic step; the scanner
    skips dot-folders, so the app is gone whole), and only then deleted.
    A delete that fails part way leaves only that hidden folder, logged,
    never a half-app the scanner would try to load. A folder that's a
    link is unlinked: what it points to is never touched.

    Returns {"id"}. Raises UninstallError, having removed nothing: 400 for
    an id no app could have, 404 for one no app loads under, 500 if the
    folder can't be moved.
    """
    if not isinstance(app_id, str) or not APP_ID.match(app_id):
        raise UninstallError(
            f"not an app id: letters, digits, '-' and '_', starting with a letter or digit: {app_id!r}"
        )
    apps_dir = os.path.abspath(apps_dir)
    with _write_lock:
        installed = find_app(apps_dir, app_id)
        folder = installed.path if installed else None
        # The scanner only loads a direct child of the apps folder; this
        # is the last word, in case that ever changes.
        if folder is None or os.path.dirname(folder) != apps_dir:
            raise UninstallError(f"'{app_id}' isn't installed", 404)
        trash = os.path.join(apps_dir, f".uninstall-{secrets.token_hex(8)}")
        try:
            os.rename(folder, trash)
        except OSError as e:
            raise UninstallError(f"couldn't remove the app's folder: {e.strerror or e}", 500) from None
        # A new app in the same folder is loaded afresh, never from cache.
        with _loaded_lock:
            _loaded.pop(folder, None)

    try:
        if os.path.islink(trash):
            os.unlink(trash)
        else:
            shutil.rmtree(trash)
    except OSError as e:
        # Uninstalled all the same: what's left is hidden from the scanner.
        _log(f"uninstalled {app_id}, but couldn't delete {trash}: {e.strerror or e}")
    else:
        _log(f"uninstalled {app_id}: removed {folder}")
    return {"id": app_id}
