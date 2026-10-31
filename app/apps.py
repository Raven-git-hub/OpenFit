"""
Installed apps: the scanner and loader the app host runs on.

An app is a folder in the apps directory (APPS_DIR, /data/apps by
default) holding its manifest and, usually, its engine:

    <apps dir>/steps/
        app.yaml    the manifest: id, name, sizes, reads, config, metrics
                    (sizes the tile's allowed sizes in cells, the default
                    first; config an ordered list of fields:
                    {key, label, type, default}; metrics the ones the
                    app invents: {key, label, unit, category, grain, range}),
                    and, all optional, description (what Browse says of
                    it), requires (the app level it needs: APP_LEVEL) and
                    replaces (the app ids a library app takes over: B2)
        app.py      def render(ctx, cfg): -> a display spec (see host.py)
                    and, optionally, def handle(ctx, action, payload):
                    what its tile's input sends (POST /api/app/<id>/action)

app.py is optional: an app with a manifest alone loads and lists, and
its tile says it has nothing to draw. Any other files in the folder
(reference data, helpers) are the app's own; the host doesn't read them.

scan_apps() lists every app that loads. A folder without an app.yaml
isn't an app and is passed over quietly; one with an app.yaml that
doesn't load - bad YAML, a manifest field of the wrong type, an app.py
that fails to import, has no callable render or a handle that isn't
callable - is logged and skipped,
so one broken app never stops the rest from being listed, and never
takes the server down.

Loading imports app.py from its absolute path; nothing is added to
sys.path or sys.modules. A loaded app is kept until either of its files
changes, so a scan is cheap enough to run on every request, a newly
installed or edited app is picked up without a restart, and a broken one
is logged once per change rather than on every page load.

An app's id has one of two shapes. A custom app's - one pasted in,
duplicated or placed by hand - is APP_ID's, its folder's name by default.
A library app's is <source>.<name> (LIBRARY_APP_ID: openfit.frms), its
folder's name: the library assigns the source, so the app's own app.yaml
says only the bare name (id: frms) and its files stay the library's own,
byte for byte. A dot in an id always means installed from a library -
install_library_app() is the one way to make one - and such an app is
read-only here: update_app() refuses it (403); Duplicate makes a copy of
one's own, under a bare id. It follows its library instead: when the
shelf has a new version, shelf.sync_library_apps() swaps it in through
_replace_app(), the swap update_app() makes.

This is the one way the host finds an app: the Home tab's routes use it
now, and the Library reuses it. install_app() is the one way one is
added from the browser (POST /api/apps), install_library_app() the one
way one is added from a library (Browse: POST
/api/library/<source>/<app>/install, its files read from the shelf by
shelf.py, never sent by the page), update_app() the one
way one's files are replaced (PUT /api/apps/<id>), uninstall_app() the
one way one is removed (DELETE /api/apps/<id>), and read_app_file() the
one way a file in one is read back (GET /api/app/<id>/file): see their
sections, below.
"""

import importlib.util
import json
import os
import re
import secrets
import shutil
import threading
import traceback
from dataclasses import dataclass, field

import yaml

from metrics import (
    CATEGORIES,
    DAILY,
    INSTANT,
    METRICS,
    SERIES,
    SERIES_METRICS,
    SESSION,
    SESSION_KINDS,
    finite_number,
)

MANIFEST = "app.yaml"
MODULE = "app.py"

# The most cells a tile may span, either way: Home's grid is this many
# cells across, and a tile is held to the same height.
MAX_CELLS = 6

# An app id is part of its URL (/api/app/<id>/render). Also the rule a
# metric an app invents is keyed by (_metrics()), so it never gains a dot.
APP_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]*\Z")

# A library app's id: <source>.<name>, such as openfit.frms - the source
# a lowercase letter then lowercase letters and digits, one dot, then a
# name in INSTALL_ID's form. The library assigns the source, never the
# app: its own app.yaml says the bare name (id: frms), and the source is
# added at install (install_library_app()), carried by the folder's name
# alone (_manifest()). So a dot in an id always means installed from a
# library: nothing else can make one (INSTALL_ID has none).
LIBRARY_APP_ID = re.compile(r"[a-z][a-z0-9]*\.[a-z0-9][a-z0-9-]*\Z")

# A library's source: the part of a library app's id before the dot.
LIBRARY_SOURCE = re.compile(r"[a-z][a-z0-9]*\Z")


def is_app_id(value):
    """Whether `value` is an id an app could have: a custom app's (APP_ID)
    or a library app's (LIBRARY_APP_ID)."""
    return isinstance(value, str) and bool(APP_ID.match(value) or LIBRARY_APP_ID.match(value))


def library_of(app_id):
    """The library an app with this id was installed from - 'openfit' for
    openfit.frms - or None for a custom app's id."""
    if isinstance(app_id, str) and LIBRARY_APP_ID.match(app_id):
        return app_id.split(".", 1)[0]
    return None


class AppError(Exception):
    """An app folder that doesn't load, and why."""


# The app contract this OpenFit provides; raised when apps gain something
# an older OpenFit lacks. An app's `requires` names the level it needs
# (1 when it doesn't say): the scanner skips one that needs more, and
# install refuses it (check_app()), so an app written for a newer
# OpenFit never half-runs on this one.
APP_LEVEL = 1

# The most a description says, trimmed: a line or two in Browse.
MAX_DESCRIPTION = 300

# The most apps one app replaces.
MAX_REPLACES = 5


def manifest_description(raw):
    """The manifest's description, trimmed - what Browse shows of the app -
    or None for none (absent, null or blank). Text of at most
    MAX_DESCRIPTION characters, else AppError."""
    value = raw.get("description")
    if value is None:
        return None
    if not isinstance(value, str):
        raise AppError(f"description must be text, at most {MAX_DESCRIPTION} characters: {value!r}")
    value = value.strip()
    if len(value) > MAX_DESCRIPTION:
        raise AppError(f"description must be at most {MAX_DESCRIPTION} characters: it has {len(value)}")
    return value or None


def manifest_requires(raw):
    """The app level the manifest says the app needs: a whole number from
    1 (not a bool), 1 when it doesn't say. Else AppError. Whether this
    OpenFit has that level is level_problem()'s to say."""
    value = raw.get("requires", 1)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise AppError(f"requires must be a whole number from 1, the app level it needs: {value!r}")
    return value


def level_problem(requires):
    """Why an app needing app level `requires` can't run on this OpenFit,
    or None if it can."""
    if requires > APP_LEVEL:
        return f"needs OpenFit app level {requires}; this OpenFit is level {APP_LEVEL}"
    return None


def manifest_replaces(raw, own=None):
    """The app ids the manifest says it replaces, in order: a list of at
    most MAX_REPLACES, each one an app could have (is_app_id()), none
    twice - [] for none. Else AppError.

    A library app's: openfit.frms says replaces: [frms] to take over the
    data of the frms installed before the library existed. B2 does that
    move, at the library app's install; until then it's parsed, and
    Browse and the library install say it's pending. `own` is the
    library app's id (openfit.frms), which it can never replace. A
    custom app's replaces is carried, never acted on, so its own bare id
    isn't refused: FRMS's library files pasted in as frms still load.
    """
    value = raw.get("replaces")
    if value is None:
        return []
    if not isinstance(value, list):
        raise AppError(f"replaces must be a list of the app ids it replaces, such as [frms]: {value!r}")
    if len(value) > MAX_REPLACES:
        raise AppError(f"replaces lists at most {MAX_REPLACES} apps: it lists {len(value)}")
    replaces = []
    for i, app_id in enumerate(value):
        if not is_app_id(app_id):
            raise AppError(f"replaces[{i}] isn't an app id: {app_id!r}")
        if app_id in replaces:
            raise AppError(f"replaces lists {app_id!r} twice")
        if own is not None and app_id == own:
            raise AppError(f"replaces can't name the app itself: {app_id!r}")
        replaces.append(app_id)
    return replaces


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
    # handle(ctx, action, payload), or None: an app without one can't be
    # actioned, only drawn.
    handle: object = field(default=None, repr=False)
    # The metrics it invents (_metrics()), in its manifest's order.
    metrics: list = field(default_factory=list)
    # What Browse says of it, the app level it needs and the apps it
    # replaces: the manifest's, checked (manifest_description() and its
    # siblings). Not listed by summary().
    description: str = None
    requires: int = 1
    replaces: list = field(default_factory=list)

    def summary(self):
        """What GET /api/apps lists for it: the manifest's id, name, sizes
        (default_size their first), reads, config fields - the schema a
        tile's settings form is built from - and the metrics it invents,
        and the names of the files in its folder. library is the library
        it was installed from ('openfit' for openfit.frms: read-only, so
        the page offers no Update), None for a custom app."""
        return {
            "id": self.id,
            "name": self.name,
            "library": library_of(self.id),
            "default_size": dict(self.default_size),
            "sizes": [dict(size) for size in self.sizes],
            "reads": list(self.reads),
            "config": [dict(field) for field in self.config],
            "metrics": [
                {**metric, "range": None if metric["range"] is None else list(metric["range"])}
                for metric in self.metrics
            ],
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
    config form shows them in. Each field needs a key, once; label, type
    and default are the app's to give and aren't checked - but a field is
    sent to the page as it's written (GET /api/apps), so it must be plain
    JSON: a YAML date, a .nan or a !!binary would fail the whole listing.
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
        try:
            json.dumps(field, allow_nan=False)
        except (TypeError, ValueError) as e:
            raise AppError(
                f"config field {key!r} must be plain JSON - text, numbers, true/false, "
                f"lists and mappings (quote a date): {e}"
            ) from None
        keys.add(key)
        fields.append(dict(field))
    return fields


# Every key OpenFit's own vocabulary has: a metric, a series metric or a
# session kind. None is an app's to declare: it would shadow the built-in
# one wherever the app's code names it.
RESERVED_KEYS = frozenset(METRICS) | frozenset(SERIES_METRICS) | frozenset(SESSION_KINDS)

# What an app's own metric may be: one figure a day, or a timestamped
# point. A session kind or a series of an app's own is a later job.
APP_METRIC_GRAINS = (DAILY, INSTANT)

# The fields of a metric an app declares; range is optional.
METRIC_FIELDS = ("key", "label", "unit", "category", "grain", "range")


def _metric_range(raw, key):
    """A declared metric's range, [low, high], or None for none."""
    if raw is None:
        return None
    if isinstance(raw, list) and len(raw) == 2:
        try:
            low, high = (finite_number(bound) for bound in raw)
        except ValueError:
            pass
        else:
            if low > high:
                raise AppError(f"metric {key!r}: range is [low, high], low first: {raw!r}")
            return list(raw)
    raise AppError(f"metric {key!r}: range must be [low, high], two numbers, such as [0, 1000]: {raw!r}")


def _metrics(raw):
    """The metrics the app invents, in order: [{key, label, unit, category, grain, range}, ...].

    Declare what you invent, write what exists: an app lists here only
    a metric OpenFit doesn't have. A built-in one (calories_in) it just
    writes, so a key the vocabulary already has - a metric, series
    metric or session kind - is refused: it would shadow the built-in.
    Each metric is a mapping of METRIC_FIELDS and nothing else, so a
    misspelt `range` can't quietly mean no range:

    - key: the short key the app's code says, shaped as an app id (it's
      part of a URL too: /api/metric/app:<id>:<key>), once per app. It's
      stored as app:<id>:<key> (host.py), so two apps' keys never meet.
    - label: its name, as the console shows it. Text.
    - unit: what its readings are in - the app's own, text.
    - category: one of CATEGORIES, Other the catch-all.
    - grain: daily or instant (APP_METRIC_GRAINS).
    - range: optional [low, high], finite numbers, both ends included: a
      write outside it is refused. Left out, or null, any finite number
      will do.

    A list, as config is, so the order is the manifest's: the order the
    catalog lists them in.
    """
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AppError("metrics must be a list of the metrics the app invents, each {key, label, unit, category, grain}")
    metrics, keys = [], set()
    for entry in raw:
        if not isinstance(entry, dict):
            raise AppError(f"each metric must be a mapping of {', '.join(METRIC_FIELDS)}: {entry!r}")
        key = entry.get("key")
        if not isinstance(key, str) or not APP_ID.match(key):
            raise AppError(
                "each metric needs a key: letters, digits, '-' and '_', "
                f"starting with a letter or digit: {key!r}"
            )
        if key in RESERVED_KEYS:
            raise AppError(
                f"metric {key!r} is one of OpenFit's own keys: declare only a metric the app "
                "invents (a built-in metric is written without declaring it)"
            )
        if key in keys:
            raise AppError(f"metric {key!r} is listed twice")
        unknown = sorted(str(name) for name in entry if name not in METRIC_FIELDS)
        if unknown:
            raise AppError(
                f"metric {key!r} has a field a metric doesn't: {', '.join(map(repr, unknown))} "
                f"- a metric is {{{', '.join(METRIC_FIELDS)}}}"
            )
        texts = {}
        for name in ("label", "unit"):
            value = entry.get(name)
            if not isinstance(value, str) or not value.strip():
                raise AppError(f"metric {key!r} needs a {name}, as text")
            texts[name] = value.strip()
        category = entry.get("category")
        if category not in CATEGORIES:
            raise AppError(f"metric {key!r}: category must be one of {', '.join(CATEGORIES)}: {category!r}")
        grain = entry.get("grain")
        if grain in (SESSION, SERIES):
            raise AppError(f"metric {key!r}: an app's own metric is daily or instant, not {grain}")
        if grain not in APP_METRIC_GRAINS:
            raise AppError(f"metric {key!r}: grain must be daily or instant: {grain!r}")
        keys.add(key)
        metrics.append({
            "key": key,
            **texts,
            "category": category,
            "grain": grain,
            "range": _metric_range(entry.get("range"), key),
        })
    return metrics


def _manifest(folder, name=None):
    """The folder's manifest, checked and with defaults filled in, as it
    loads in a folder called `name` - its own name unless given: an
    update's staged files are checked under the name of the folder they
    replace (_loads())."""
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
    # First: an app written for a newer OpenFit may mean something by its
    # other fields that this one can't read, so that's the reason given.
    requires = manifest_requires(raw)
    problem = level_problem(requires)
    if problem:
        raise AppError(problem)

    # The id defaults to the folder's name, the name to the id - but a
    # folder named as a library app's id (openfit.frms) is one: that's
    # its id, and its own app.yaml says the bare name (frms), or nothing.
    # The source lives in the folder's name alone, so the files are the
    # library's own, byte for byte.
    basename = name or os.path.basename(folder)
    if LIBRARY_APP_ID.match(basename):
        bare = basename.split(".", 1)[1]
        own = raw.get("id", bare)
        if own != bare:
            raise AppError(f"a library app's own id is its bare name: expected {bare!r}, got {own!r}")
        app_id = basename
    else:
        app_id = raw.get("id", basename)
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
        "metrics": _metrics(raw.get("metrics")),
        "description": manifest_description(raw),
        "requires": requires,
        "replaces": manifest_replaces(raw, own=app_id if library_of(app_id) else None),
    }


# What the tile of an app with no app.py says.
NOTHING_TO_DRAW = f"Nothing to draw yet: this app has no {MODULE}"


def _nothing_to_draw(ctx, cfg):
    """The render() of an app with a manifest alone."""
    return {"type": "empty", "message": NOTHING_TO_DRAW}


def _entry_points(folder, app_id):
    """app.py's render and handle, imported once: (render, handle or None)."""
    path = os.path.join(folder, MODULE)
    if not os.path.isfile(path):
        # app.py is optional: the app still loads and lists, and its
        # tile says there's nothing to draw. Adding one later is picked
        # up like any edit (_signature watches it).
        return _nothing_to_draw, None
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
    # Optional, but an app that has one meant it to be called.
    handle = getattr(module, "handle", None)
    if handle is not None and not callable(handle):
        raise AppError(f"{MODULE}'s handle must be a function: handle(ctx, action, payload)")
    return render, handle


def load_app(folder, name=None):
    """The app in `folder`, or AppError saying why it doesn't load - as
    it loads in a folder called `name`, if given (_manifest())."""
    manifest = _manifest(folder, name)
    render, handle = _entry_points(folder, manifest["id"])
    return InstalledApp(path=folder, render=render, handle=handle, **manifest)


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


def app_present(apps_dir, app_id):
    """Whether an app with this id is still on the volume, loading or not.

    Its folder, <apps dir>/<id>, exists - as anything, even a broken
    link, and whether or not what's in it loads - or an app in another
    folder loads under the id (a hand-placed one named apart from its
    id): the rule an install's id must be free of (_taken()). Unlike
    find_app(), an app that fails to load is still present: it's broken,
    not gone. An id no app could have (is_app_id()) has no folder: False.

    What decides whether an app's data is orphaned - removable through
    DELETE /api/app-sources/<id> - or still its app's.
    """
    if not is_app_id(app_id):
        return False
    return _taken(apps_dir, app_id)


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

# Installs, updates and uninstalls one at a time, so two installs can't
# both find an id free, and an uninstall can't move a folder an install
# is checking or an update is replacing.
_write_lock = threading.Lock()


class LibraryError(Exception):
    """A Library request refused, and why, with the HTTP status to send."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class InstallError(LibraryError):
    """An install refused, nothing written - or an update refused, the
    app left as it was."""


def taken_message(app_id):
    """What an install says of an id already taken (409). A custom app is
    changed with Update in its row; a library app has no Update - it
    follows the shelf by itself (shelf.sync_library_apps())."""
    if isinstance(app_id, str) and LIBRARY_APP_ID.match(app_id):
        return f"'{app_id}' is already installed. It updates itself when your admin publishes a new version."
    return (
        f"'{app_id}' already exists — use Update in its row to replace its files, "
        "or remove it first to reinstall."
    )


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
    runs - two checks so far:

    - the app level. `requires`, if given, must be a whole number from
      1, and no more than APP_LEVEL: an app written for a newer OpenFit
      is refused with the scanner's own words ("needs OpenFit app level
      2; this OpenFit is level 1"), before anything else about it.
    - a size. The manifest must declare the tile size(s) the app is
      drawn at (manifest_sizes(): `sizes`, or the older `default_size`),
      each w and h a whole number of cells, 1 to MAX_CELLS. An app
      without one won't deploy - the scanner would skip it anyway - so
      it's refused here, with the scanner's own reason.

    Nothing else is checked yet: not the manifest's other fields (name,
    reads, config, description, replaces - the scanner checks those when
    it loads the app), not
    app.py's syntax, and nothing is ever run. As real apps' quirks are
    learned, a check that catches a broken app before it's on the volume
    goes here, raising InstallError with a message the popup shows as-is.

    Containment is not this function's job and never moves here: those
    checks are install_app()'s, they run first, and they don't relax.

    manifest: the parsed app.yaml, a dict with a valid id.
    files: {name: text}, every name a plain file name.
    """
    try:
        problem = level_problem(manifest_requires(manifest))
        if problem:
            raise AppError(problem)
        manifest_sizes(manifest)
    except AppError as e:
        raise InstallError(str(e)) from None


def install_app(apps_dir, files):
    """Install an app from its files, {name: text}, as <apps_dir>/<id>/.

    In order: app.yaml must be among the files and parse; its id and
    every file name must pass the containment checks; the id must be
    free - an install only ever creates, never updates in place (that's
    update_app()); then check_app(). Only then is anything written: the
    files go into a hidden staging folder (the scanner skips
    dot-folders), which is renamed to <id> once they're all there, so the
    app appears whole or not at all. Nothing is imported or run.

    Returns {"id", "has_app_py"}. Raises InstallError, having written
    nothing: status 400 for a bad request, manifest, id or file name,
    409 for a taken id, 500 if the files can't be written.
    """
    if not isinstance(files, dict) or not files:
        raise InstallError('expected {"files": {name: content}}, with at least an app.yaml')

    manifest = _manifest_to_install(files)
    app_id = _install_id(manifest)
    _install(apps_dir, app_id, manifest, files)

    _log(f"installed {app_id}: {', '.join(sorted(files))}")
    return {"id": app_id, "has_app_py": MODULE in files}


def install_library_app(apps_dir, source, files):
    """Install an app from the `source` library ('openfit'), its files
    {name: text} as the library has them, as <apps_dir>/<source>.<id>/.

    The library assigns the source, never the app: its app.yaml says
    its bare id (id: frms), which must be one install_app() takes - no
    dot - and the app's id is <source>.<id> (openfit.frms), carried by
    its folder's name alone. The files are written as given, byte for
    byte the library's own. Past the source and the id, exactly
    install_app()'s path, under the full id: every file name and content
    checked, the id free (409 if not - a custom app's bare id is another
    app's, so frms and openfit.frms install side by side), check_app(),
    then written whole or not at all. Nothing is imported or run.

    Browse's install (POST /api/library/<source>/<app>/install) calls it,
    with the files read from the shelf (shelf.read_app_files()) - never
    the page's copy.

    Returns {"id", "has_app_py"}, the id the full one. Raises
    InstallError, having written nothing: 400 for a source that isn't
    lowercase letters and digits starting with a letter, or for anything
    install_app() refuses with a 400; 409 for a taken id; 500 if the
    files can't be written.
    """
    if not isinstance(source, str) or not LIBRARY_SOURCE.match(source):
        raise InstallError(
            f"a library source is lowercase letters and digits, starting with a letter: {source!r}"
        )
    if not isinstance(files, dict) or not files:
        raise InstallError('expected {"files": {name: content}}, with at least an app.yaml')

    manifest = _manifest_to_install(files)
    app_id = f"{source}.{_install_id(manifest)}"
    _install(apps_dir, app_id, manifest, files)

    _log(f"installed {app_id} from the {source} library: {', '.join(sorted(files))}")
    return {"id": app_id, "has_app_py": MODULE in files}


def _install(apps_dir, app_id, manifest, files):
    """An install, once its id is known - install_app()'s and
    install_library_app()'s, which differ only in how they get it: every
    file checked, then, one at a time with other installs, updates and
    uninstalls, the id free (409), check_app(), and the files written
    into a new <apps_dir>/<app_id>/. InstallError, having written
    nothing, if any of that fails."""
    _check_files(files)

    apps_dir = os.path.abspath(apps_dir)
    with _write_lock:
        if _taken(apps_dir, app_id):
            raise InstallError(taken_message(app_id), 409)
        check_app(manifest, dict(files))
        try:
            _write_app(apps_dir, app_id, files)
        except OSError as e:
            raise InstallError(f"couldn't write the app's files: {e.strerror or e}", 500) from None


def _check_files(files):
    """Every file's name plain and its content text, else InstallError."""
    for name, content in files.items():
        problem = filename_problem(name)
        if problem:
            raise InstallError(problem)
        if not isinstance(content, str) or not _is_text(content):
            raise InstallError(f"{name!r} must be text")


def _stage(apps_dir, files, kind):
    """Write `files` into a new hidden <apps_dir>/.<kind>-<hex>/ - the
    scanner skips dot-folders - and return its path. Whole or not at all:
    a failure part way removes it."""
    os.makedirs(apps_dir, exist_ok=True)
    staging = os.path.join(apps_dir, f".{kind}-{secrets.token_hex(8)}")
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
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def _write_app(apps_dir, app_id, files):
    """Write `files` into a new <apps_dir>/<app_id>/, whole or not at all."""
    staging = _stage(apps_dir, files, "install")
    try:
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

    The id must be one an app could have (is_app_id(): the scanner's
    APP_ID, or a library app's LIBRARY_APP_ID - which every listed app's
    id meets, so it can never be '..', hold a '/', or name a dot-folder)
    and an app must load under it: a library app uninstalls as any does.
    The folder is renamed to a hidden .uninstall-<hex> one beside it (one
    atomic step; the scanner skips dot-folders, so the app is gone
    whole), and only then deleted.
    A delete that fails part way leaves only that hidden folder, logged,
    never a half-app the scanner would try to load. A folder that's a
    link is unlinked: what it points to is never touched.

    Only the app's files go: what it wrote stays, under its source
    (app:<id>), as a disconnected device's data does - and an app
    installed again under the same id carries on writing to it.

    Returns {"id"}. Raises UninstallError, having removed nothing: 400 for
    an id no app could have, 404 for one no app loads under, 500 if the
    folder can't be moved.
    """
    if not is_app_id(app_id):
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


# ---------- updating an app: PUT /api/apps/<id> ----------

# An installed app's files replaced, in one step: the safe form of
# uninstalling it and adding it again. Install and uninstall composed -
# the new files staged and checked, then swapped in for the old folder -
# with the old folder kept aside until the new one is in its place. The
# request names the app, never a path: the folder replaced is the one
# the scanner loaded it from, as uninstall's is.


def _update_id(manifest, app_id):
    """The manifest's id, which must be the app's own: an update never
    renames an app, nor puts one app's files in another's place."""
    new_id = manifest.get("id")
    if new_id is None:
        raise InstallError(f"{MANIFEST} needs an id: this updates '{app_id}', so id: {app_id}")
    if not isinstance(new_id, str):
        raise InstallError(f"id must be text: this updates '{app_id}', so id: '{app_id}'")
    if new_id != app_id:
        raise InstallError(
            f"this updates '{app_id}', but the files are for '{new_id}' — an update keeps "
            "the app's id (Duplicate makes a copy under a new one)"
        )
    return new_id


def _loads(staging, folder, app_id):
    """Whether the staged files load as the scanner would load them in
    `folder`, the app's own - under its name, so a library app's are
    held to its bare id - else InstallError: the app is left on its old
    files."""
    try:
        load_app(staging, os.path.basename(folder))
    except AppError as e:
        raise InstallError(f"the update doesn't load, so '{app_id}' is unchanged: {e}") from None


def _swap(folder, staging):
    """Put the staged folder in `folder`'s place: the old folder renamed
    aside to a hidden .update-old-<hex> - one atomic step - then the new
    one renamed in, another. Returns the old folder's new path. If the
    second rename fails, the old folder is put back. Either way, the app
    is never missing: InstallError, 500, with the old files in place."""
    old = os.path.join(os.path.dirname(folder), f".update-old-{secrets.token_hex(8)}")
    try:
        os.rename(folder, old)
    except OSError as e:
        raise InstallError(f"couldn't replace the app's files: {e.strerror or e}", 500) from None
    try:
        os.rename(staging, folder)
    except OSError as e:
        try:
            os.rename(old, folder)
        except OSError as again:
            # Nothing's left to try: the files are kept, hidden, and said.
            _log(f"couldn't put {folder} back: its files are in {old}: {again.strerror or again}")
        raise InstallError(f"couldn't replace the app's files: {e.strerror or e}", 500) from None
    return old


def update_app(apps_dir, app_id, files):
    """Replace the installed app's files with `files`, {name: text}: the
    same app, its id kept, under its own folder.

    In order, before the app is touched: the URL's id must be one an app
    could have (is_app_id(), as uninstall's), and not a library app's -
    read-only here: it's updated in its library, and Duplicate makes a
    copy of one's own to change; app.yaml must be among the files
    and parse, with an id - this app's own, not another's; every file
    name and content must pass install's containment checks. Then, one
    at a time with installs and uninstalls (_write_lock), _replace_app():
    the app must be installed - loading - and its folder is the one the
    scanner loaded it from; check_app(); the files are written to a
    hidden staging folder, which must load as the scanner would load it
    (load_app(): the whole manifest, and app.py imported - unlike
    install, which runs nothing, because a broken update would take down
    an app that works); then the swap (_swap()): the old folder aside,
    the new one in, each an atomic rename. The app's cached load is
    dropped, so the next scan loads the new files, and only then is the
    old folder deleted - a link unlinked, its target never touched; a
    delete that fails leaves it hidden, and logged.

    A replace, not a merge: the folder afterwards holds exactly `files`,
    so a file left out is gone. No database work: what the app wrote,
    under app:<id>, is untouched. A custom metric the new app.yaml no
    longer declares drops out of the catalog, its readings kept, until
    it's declared again.

    Returns {"id", "has_app_py"}. Raises InstallError, having changed
    nothing - the app runs on its old files - 400 for a bad request,
    manifest, id, file name or app that doesn't load, 403 for a library
    app, 404 for an app that isn't installed, 500 if the files can't be
    written or swapped.
    """
    if not is_app_id(app_id):
        raise InstallError(
            f"not an app id: letters, digits, '-' and '_', starting with a letter or digit: {app_id!r}"
        )
    source = library_of(app_id)
    if source:
        raise InstallError(
            f"'{app_id}' is from the {source} library, so it's updated there, not here — "
            "Duplicate makes a copy of your own to change",
            403,
        )
    if not isinstance(files, dict) or not files:
        raise InstallError('expected {"files": {name: content}}, with at least an app.yaml')

    manifest = _manifest_to_install(files)
    _update_id(manifest, app_id)
    _check_files(files)

    with _write_lock:
        _replace_app(apps_dir, app_id, files)
    _log(f"updated {app_id}: {', '.join(sorted(files))}")
    return {"id": app_id, "has_app_py": MODULE in files}


def _replace_app(apps_dir, app_id, files):
    """Swap the installed app's files for `files`, {name: text}, in its
    own folder: the heart of an update, shared by update_app() - the one
    a person asks for, which first refuses a library app (403) - and a
    library app following its shelf (shelf.sync_library_apps()), the one
    way a library app's files change. Internal: no route reaches it but
    through update_app().

    The caller holds _write_lock and has checked `files`' names and
    contents (_check_files()) and that app.yaml parses with the app's
    own id (a library app's, its bare name). Here: the app must be installed - loading - and its folder
    is the one the scanner loaded it from, a direct child of the apps
    folder; check_app(); the files are staged in a hidden folder, which
    must load as the scanner would load it in the app's folder
    (_loads(): the whole manifest, and app.py imported); then _swap(),
    the old folder aside and the new one in, each an atomic rename. The
    app's cached load is dropped, so the next scan - the next render -
    loads the new files, and the old folder is deleted: a link
    unlinked, its target never touched; a delete that fails leaves it
    hidden, logged. No database work.

    InstallError, the app on its old files: 404 if it isn't installed,
    400 for files check_app() refuses or that don't load, 500 if they
    can't be written or swapped in.
    """
    apps_dir = os.path.abspath(apps_dir)
    manifest = _manifest_to_install(files)
    installed = find_app(apps_dir, app_id)
    folder = installed.path if installed else None
    # The scanner only loads a direct child of the apps folder; this
    # is the last word, in case that ever changes.
    if folder is None or os.path.dirname(folder) != apps_dir:
        raise InstallError(f"'{app_id}' isn't installed — use Add Application to install it", 404)
    check_app(manifest, dict(files))
    try:
        staging = _stage(apps_dir, files, "update")
    except OSError as e:
        raise InstallError(f"couldn't write the app's files: {e.strerror or e}", 500) from None
    try:
        _loads(staging, folder, app_id)
        old = _swap(folder, staging)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    # The new files are loaded afresh, never from the old ones' cache.
    with _loaded_lock:
        _loaded.pop(folder, None)

    try:
        if os.path.islink(old):
            os.unlink(old)
        else:
            shutil.rmtree(old)
    except OSError as e:
        # Updated all the same: what's left is hidden from the scanner.
        _log(f"updated {app_id}, but couldn't delete {old}: {e.strerror or e}")
