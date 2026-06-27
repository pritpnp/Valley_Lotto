"""Password hashing — stdlib only, no external crypto dependency.

We use ``hashlib.scrypt`` (a memory-hard KDF in the same family as bcrypt/argon2,
available in the Python standard library). Each password gets its own random salt
and is stored as a single self-describing string:

    scrypt$<n>$<r>$<p>$<salt_hex>$<hash_hex>

so the work parameters travel with the hash and can be raised later without
breaking existing logins. Verification is constant-time.
"""

from __future__ import annotations

import hashlib
import hmac
import os

# scrypt cost parameters. N must be a power of two; these are a sensible 2020s
# default (~16 MB, tens of ms per hash) that a Beelink handles comfortably.
_N = 2 ** 14
_R = 8
_P = 1
_DKLEN = 32
_SALT_BYTES = 16


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Return a self-describing scrypt hash string for ``password``."""
    if not password:
        raise ValueError("password must not be empty")
    salt = salt if salt is not None else os.urandom(_SALT_BYTES)
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN
    )
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of ``password`` against a stored hash string."""
    try:
        scheme, n, r, p, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        n, r, p = int(n), int(r), int(p)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, AttributeError):
        return False
    dk = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=len(expected)
    )
    return hmac.compare_digest(dk, expected)
