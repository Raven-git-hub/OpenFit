"""
Installed apps: the scanner and loader the app host runs on.

An app is a folder in the apps directory (APPS_DIR, /data/apps by
default) holding two files:

    <apps dir>/steps/
        app.yaml    the manifest: id, name, default_size, reads, config
                    (config an ordered list of fields: {key, label, type, default})
        app.py      def render(ctx, cfg): -> a display spec (see host.py)

scan_apps() lists every app that loads. A folder without an app.yaml
isn't an app and is passed over quietly; one with an app.yaml that
doesn't load - bad YAML, a manifest field of the wrong type, no app.py,
an app.py that fails to import or has no callable render - is logged
and skipped, so one broken app never stops the rest from being listed,
and never takes the server down.

Loading imports app.py from its absolute path; nothing is added to
sys.path or sys.modules. A loaded app is kept until either of its files
changes, so a scan is cheap enough to run on every request, a newly
installed or edited app is picked up without a restart, and a broken one
is logged once per change rather than on every page load.

This is the one way the host finds an app: the Home tab's routes use it
now, and the Library reuses it.
"""

import importlib.util
import os
import re
import threading
import traceback
from dataclasses import dataclass, field

import yaml

MANIFEST = "app.yaml"
MODULE = "app.py"

# A tile's size in grid cells when the manifest names none.
DEFAULT_SIZE = {"w": 2, "h": 2}
# The largest default_size a manifest may ask for, either way.
MAX_CELLS = 12

# An app id is part of its URL (/api/app/<id>/render).
APP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")


class AppError(Exception):
    """An app folder that doesn't load, and why."""


@dataclass(frozen=True)
class InstalledApp:
    id: str
    name: str
    default_size: dict
    reads: list
    config: list
    path: str
    render: object = field(repr=False)

    def summary(self):
        """What GET /api/apps lists for it."""
        return {"id": self.id, "name": self.name, "default_size": dict(self.default_size)}


def _log(message):
    print(f"[apps] {message}")


def _size(raw):
    if raw is None:
        return dict(DEFAULT_SIZE)
    if not isinstance(raw, dict):
        raise AppError("default_size must be a mapping of w and h")
    size = {}
    for axis in ("w", "h"):
        cells = raw.get(axis)
        # bool is an int to Python; a manifest's `w: true` is a mistake.
        if isinstance(cells, bool) or not isinstance(cells, int) or not 1 <= cells <= MAX_CELLS:
            raise AppError(f"default_size.{axis} must be a whole number from 1 to {MAX_CELLS}")
        size[axis] = cells
    return size


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
    return {
        "id": app_id,
        "name": name.strip(),
        "default_size": _size(raw.get("default_size")),
        "reads": list(reads),
        "config": _config(raw.get("config")),
    }


def _render_function(folder, app_id):
    path = os.path.join(folder, MODULE)
    if not os.path.isfile(path):
        raise AppError(f"no {MODULE}")
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
