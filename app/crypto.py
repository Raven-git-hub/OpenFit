"""
Encryption at rest for connector credentials.

Device credentials entered in the UI are stored in the `accounts` table
as a Fernet-encrypted JSON blob, so a copy of tracker.db (a backup, a
snapshot, a stray volume mount) is not a copy of your Garmin password.

The key comes from $OPENFIT_SECRET_KEY if set - useful if you'd rather
hold it in your secret manager or compose env - otherwise it is read
from, or created at, /data/.secret_key (0600, alongside the database on
the persistent volume). Lose that key and the stored credentials are
unrecoverable: re-adding the device in the UI is the fix.

Nothing here reaches the network. Fernet is AES-128-CBC + HMAC-SHA256
from `cryptography`, which is the only new dependency this needs.
"""

import os

from cryptography.fernet import Fernet

KEY_ENV = "OPENFIT_SECRET_KEY"
KEY_PATH_ENV = "OPENFIT_SECRET_KEY_PATH"
DEFAULT_KEY_PATH = "/data/.secret_key"


def key_path():
    """Where the generated key lives when $OPENFIT_SECRET_KEY isn't set.

    Overridable via $OPENFIT_SECRET_KEY_PATH so the tests (and anyone
    running outside the container) don't need a writable /data.
    """
    return os.getenv(KEY_PATH_ENV) or DEFAULT_KEY_PATH


def get_key() -> bytes:
    """Return the Fernet key, generating and persisting one if needed."""
    from_env = os.getenv(KEY_ENV)
    if from_env:
        return from_env.strip().encode()

    path = key_path()
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read().strip()

    key = Fernet.generate_key()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # O_EXCL so two workers racing on first start can't both write a key
    # and leave half the credentials undecryptable; 0600 because this is
    # the one file that unlocks everything else.
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        with open(path, "rb") as f:
            return f.read().strip()
    with os.fdopen(fd, "wb") as f:
        f.write(key)
    return key


def encrypt(value: str) -> str:
    """Encrypt a string, returning the URL-safe token as text."""
    return Fernet(get_key()).encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    """Decrypt a token produced by encrypt(). Raises if the key is wrong."""
    return Fernet(get_key()).decrypt(token.encode()).decode()
