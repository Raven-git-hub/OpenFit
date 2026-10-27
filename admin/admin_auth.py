"""
The Admin's login: a one-time setup code, one password, and sessions.

On a first start (no password yet) a setup code is made and printed once
to the Admin's log - `docker logs openfit-admin-admin-1` - and kept in
memory only; a restart before setup prints a new one. The code and a
password of at least 12 characters set the password, stored as a
werkzeug hash in /data/auth.json, after which setup is gone for good
(410). Logins are Flask sessions under a key made once into
/data/secret_key. Five failed logins from one address in five minutes
and the next is refused (429) until the oldest of them is five minutes
old; that count is in memory.
"""

import hashlib
import hmac
import json
import os
import secrets
import threading
import time

from werkzeug.security import check_password_hash, generate_password_hash

MIN_PASSWORD = 12
MAX_PASSWORD = 1024
MAX_FAILS = 5
FAIL_WINDOW = 300


class AuthError(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.message = message
        self.status = status


def load_secret_key(data_dir):
    """The session key, made once into /data/secret_key (0600)."""
    path = os.path.join(data_dir, "secret_key")
    try:
        with open(path, "rb") as f:
            key = f.read()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = secrets.token_bytes(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


class Auth:
    def __init__(self, data_dir, clock=time.time):
        self.path = os.path.join(data_dir, "auth.json")
        self.clock = clock
        self._fails = {}
        self._lock = threading.Lock()
        self.setup_code = None if self.password_set() else secrets.token_urlsafe(16)

    def _hash(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                stored = json.load(f)
        except (OSError, ValueError):
            return None
        h = stored.get("password_hash") if isinstance(stored, dict) else None
        return h if isinstance(h, str) and h else None

    def password_set(self):
        return self._hash() is not None

    def session_tag(self):
        """Ties a session to the password it was made under: a new
        password (auth.json replaced) ends every older session."""
        h = self._hash()
        return hashlib.sha256(h.encode()).hexdigest()[:16] if h else None

    def setup(self, code, password):
        if self.password_set():
            raise AuthError("The Admin is already set up.", 410)
        if (
            not isinstance(code, str)
            or self.setup_code is None
            or not hmac.compare_digest(code.strip().encode(), self.setup_code.encode())
        ):
            raise AuthError("That setup code isn't right. It's in the Admin's log: docker logs openfit-admin-admin-1", 403)
        if not isinstance(password, str) or len(password) < MIN_PASSWORD:
            raise AuthError(f"The password needs at least {MIN_PASSWORD} characters.", 400)
        if len(password) > MAX_PASSWORD:
            raise AuthError(f"The password can be at most {MAX_PASSWORD} characters.", 400)
        tmp = f"{self.path}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"password_hash": generate_password_hash(password)}, f)
        os.replace(tmp, self.path)
        self.setup_code = None

    def login(self, address, password):
        h = self._hash()
        if h is None:
            raise AuthError("Set up the Admin first.", 409)
        now = self.clock()
        with self._lock:
            recent = [t for t in self._fails.get(address, []) if now - t < FAIL_WINDOW]
            self._fails[address] = recent
            if len(recent) >= MAX_FAILS:
                raise AuthError("Too many wrong passwords. Wait a few minutes, then try again.", 429)
        if not isinstance(password, str) or not check_password_hash(h, password):
            with self._lock:
                self._fails.setdefault(address, []).append(now)
            raise AuthError("That password isn't right.", 401)
        with self._lock:
            self._fails.pop(address, None)
