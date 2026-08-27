"""
Encryption at rest for connector credentials.

The point of app/crypto.py is that a copy of tracker.db is not a copy of
your Garmin password, so these tests care about two things: a round trip
that works, and ciphertext that doesn't leak the plaintext.
"""

import os
import stat

import pytest
from cryptography.fernet import Fernet, InvalidToken

import crypto as openfit_crypto
from crypto import decrypt, encrypt


def test_the_app_module_is_the_one_under_test():
    """Pin which module these tests exercise.

    `crypto` is a short, generic name; if an installed package ever
    claimed it, these tests would silently pass against the wrong module.
    """
    assert openfit_crypto.__file__.endswith(os.path.join("app", "crypto.py"))


def test_round_trip():
    assert decrypt(encrypt("hunter2")) == "hunter2"


def test_round_trip_survives_json_and_unicode():
    blob = '{"email": "me@example.com", "password": "pa££word ✓"}'
    assert decrypt(encrypt(blob)) == blob


def test_ciphertext_does_not_contain_the_plaintext():
    token = encrypt("hunter2")
    assert "hunter2" not in token
    assert token != "hunter2"


def test_encrypting_twice_gives_different_tokens():
    """Fernet includes a random IV, so equal inputs aren't equal ciphertext."""
    assert encrypt("hunter2") != encrypt("hunter2")
    assert decrypt(encrypt("hunter2")) == "hunter2"


def test_key_is_created_once_and_reused(secret_key_file):
    encrypt("a")
    assert secret_key_file.exists()
    first = secret_key_file.read_bytes()

    token = encrypt("b")
    assert secret_key_file.read_bytes() == first
    assert decrypt(token) == "b"


def test_generated_key_file_is_0600(secret_key_file):
    encrypt("a")
    mode = stat.S_IMODE(os.stat(secret_key_file).st_mode)
    assert mode == 0o600, oct(mode)


def test_env_key_wins_over_the_file(monkeypatch, secret_key_file):
    monkeypatch.setenv(openfit_crypto.KEY_ENV, Fernet.generate_key().decode())

    assert decrypt(encrypt("hunter2")) == "hunter2"
    # Nothing was written to disk - the env key is the whole story.
    assert not secret_key_file.exists()


def test_a_different_key_cannot_decrypt(monkeypatch):
    token = encrypt("hunter2")

    monkeypatch.setenv(openfit_crypto.KEY_ENV, Fernet.generate_key().decode())

    with pytest.raises(InvalidToken):
        decrypt(token)
