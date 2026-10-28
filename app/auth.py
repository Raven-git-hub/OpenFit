"""
The user system's one account: a password, set through one-time setup links.

One OpenFit, one person, one password - still no user_id anywhere: this
is who may use the container, not rows kept apart inside it. Until a
password is set the OpenFit is locked: the page says so and every API
route but the webhook is a 401 (main.py's guard). There is no window
where it's open.

Everything lives in one file, /data/auth.json (0600), never in the
settings table - /api/settings/<key> reads and writes any key:

    {"password_hash": "...", "session_tag": "...",
     "setup": {"token_hash": "<sha256 hex>", "expires_at": "<UTC>"} | null}

A setup link is the one way a password is set, the first time or again
(a forgotten one): `python auth_cli.py setup-link` - run by the Admin
through `docker exec`, or by hand - makes a token, stores only its
SHA-256 and an expiry SETUP_HOURS on, and prints it. There is no HTTP
route that makes one. The token works once: setting a password with it
clears it, and a newer link replaces an older one unused.

Every password set renews `session_tag`, which every session carries,
so a new password signs out every other browser. Sessions are Flask's
signed cookies under a key of their own, /data/.session_key (0600) -
not crypto.py's .secret_key, which unlocks the stored credentials.

Writes are atomic (a temp file beside it, then os.replace) and taken
under an flock on auth.json.lock, so the CLI making a link and the
server setting a password - two processes - never lose each other's
write, and a token is used once even by two requests at the same
moment.

No Flask in here: auth_cli.py imports it alone. The routes, the guard
and the cookie settings are main.py's.
"""

import fcntl
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from werkzeug.security import check_password_hash, generate_password_hash

DEFAULT_AUTH_PATH = "/data/auth.json"
DEFAULT_SESSION_KEY_PATH = "/data/.session_key"
MIN_PASSWORD = 12
MAX_PASSWORD = 1024
SETUP_HOURS = 24
MAX_FAILS = 5
FAIL_WINDOW = 300

LOCKED = "This OpenFit is locked until a password is set. Ask your admin for a setup link."
BAD_LINK = "This setup link is invalid or has expired. Ask your admin for a new one."
BAD_PASSWORD = "That password isn't right."
TOO_MANY = "Too many wrong passwords. Wait a few minutes, then try again."


class AuthError(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.message = message
        self.status = status


def _utc_text(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_utc(text):
    try:
        return datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


# --- auth.json ----------------------------------------------------------------


def read_state(path):
    """auth.json as a dict of its three keys, each None where it's missing
    or not the right shape. A file that's missing or won't read is no
    password: locked, never open."""
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    state = {"password_hash": None, "session_tag": None, "setup": None}
    for key in ("password_hash", "session_tag"):
        if isinstance(raw.get(key), str) and raw[key]:
            state[key] = raw[key]
    setup = raw.get("setup")
    if (isinstance(setup, dict) and isinstance(setup.get("token_hash"), str)
            and isinstance(setup.get("expires_at"), str)):
        state["setup"] = {"token_hash": setup["token_hash"], "expires_at": setup["expires_at"]}
    return state


def _write_state(path, state):
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    # mkstemp makes the file 0600, so the hash is never readable by others
    # even for the moment before it's renamed into place.
    fd, tmp = tempfile.mkstemp(prefix=".auth-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_thread_lock = threading.Lock()


@contextmanager
def _locked(path):
    """auth.json held for a read-modify-write: by this process's threads
    and, through an flock on auth.json.lock, by the CLI too."""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    with _thread_lock:
        fd = os.open(f"{path}.lock", os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def password_set(path):
    return read_state(path)["password_hash"] is not None


def session_tag(path):
    """The tag every valid session carries, or None while no password is
    set (so no session is valid)."""
    state = read_state(path)
    return state["session_tag"] if state["password_hash"] else None


# --- setup links --------------------------------------------------------------


def make_setup_token(path, now=None):
    """A new setup token, replacing any earlier one: (token, expires_at).
    Only its SHA-256 is stored; the token itself is the caller's to hand
    on, once."""
    now = datetime.now(timezone.utc) if now is None else now
    token = secrets.token_urlsafe(32)
    expires_at = _utc_text(now + timedelta(hours=SETUP_HOURS))
    with _locked(path):
        state = read_state(path)
        state["setup"] = {"token_hash": _token_hash(token), "expires_at": expires_at}
        _write_state(path, state)
    return token, expires_at


def password_problem(password):
    if not isinstance(password, str) or len(password) < MIN_PASSWORD:
        return f"The password needs at least {MIN_PASSWORD} characters."
    if len(password) > MAX_PASSWORD:
        return f"The password can be at most {MAX_PASSWORD} characters."
    return None


def setup(path, token, password, now=None):
    """Set the password with a setup token: 403 for a token that isn't the
    stored one or has expired, 400 for a password too short or long (the
    token kept, to try again). On success the token is gone, the session
    tag is new - every other session ends - and the new tag is returned,
    for this browser's session."""
    now = datetime.now(timezone.utc) if now is None else now
    with _locked(path):
        state = read_state(path)
        setup_ = state["setup"]
        expires = _parse_utc(setup_["expires_at"]) if setup_ else None
        if (
            not isinstance(token, str)
            or setup_ is None
            or expires is None
            or now >= expires
            # As bytes: compare_digest refuses a non-ASCII str.
            or not hmac.compare_digest(_token_hash(token).encode(), setup_["token_hash"].encode())
        ):
            raise AuthError(BAD_LINK, 403)
        problem = password_problem(password)
        if problem:
            raise AuthError(problem, 400)
        tag = secrets.token_hex(16)
        _write_state(path, {"password_hash": generate_password_hash(password), "session_tag": tag, "setup": None})
    return tag


# --- signing in ---------------------------------------------------------------


class Fails:
    """Failed sign-ins per address, in memory: five within FAIL_WINDOW
    seconds and the next is refused (429) until the oldest of them ages
    out. A restart forgets them, as the Admin's does."""

    def __init__(self, clock=time.time):
        self.clock = clock
        self._fails = {}
        self._lock = threading.Lock()

    def check(self, address):
        now = self.clock()
        with self._lock:
            recent = [t for t in self._fails.get(address, []) if now - t < FAIL_WINDOW]
            self._fails[address] = recent
            if len(recent) >= MAX_FAILS:
                raise AuthError(TOO_MANY, 429)

    def record(self, address):
        with self._lock:
            self._fails.setdefault(address, []).append(self.clock())

    def clear(self, address):
        with self._lock:
            self._fails.pop(address, None)


def login(path, fails, address, password):
    """The session tag for the right password; else 409 while locked, 429
    after too many wrong ones, 401 for a wrong one."""
    state = read_state(path)
    if state["password_hash"] is None or state["session_tag"] is None:
        raise AuthError(LOCKED, 409)
    fails.check(address)
    if not isinstance(password, str) or not check_password_hash(state["password_hash"], password):
        fails.record(address)
        raise AuthError(BAD_PASSWORD, 401)
    fails.clear(address)
    return state["session_tag"]


# --- the session key ----------------------------------------------------------

_keys = {}


def session_key(path):
    """Flask's secret key, made once into `path` (0600) and kept in memory
    after the first read."""
    key = _keys.get(path)
    if key is not None:
        return key
    try:
        with open(path, "rb") as f:
            key = f.read()
    except OSError:
        key = b""
    if len(key) < 32:
        folder = os.path.dirname(path) or "."
        os.makedirs(folder, exist_ok=True)
        key = secrets.token_bytes(32)
        # O_EXCL: two first requests at once both end up with the one
        # that landed first, not a key the other then overwrote.
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            with open(path, "rb") as f:
                key = f.read()
        else:
            with os.fdopen(fd, "wb") as f:
                f.write(key)
    _keys[path] = key
    return key
