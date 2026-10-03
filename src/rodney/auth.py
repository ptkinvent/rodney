"""Stopgap auth: a password per person. The password also decides the author."""

import hashlib
import hmac
import json

from .validate import RodneyError

Users = dict[str, str]  # author name -> password


def _digest(s: str) -> bytes:
    return hashlib.sha256(s.encode("utf-8")).digest()


def authenticate(users: Users, password: object) -> str:
    """Returns the author name for a valid password; raises otherwise."""
    given = _digest(password if isinstance(password, str) else "")
    author = None
    # Compare against every user (no early exit) and compare digests in constant
    # time, so response timing doesn't reveal which password was close.
    for name, pw in users.items():
        if hmac.compare_digest(given, _digest(pw)) and author is None:
            author = name
    if author is None:
        raise RodneyError("Invalid password.")
    return author


def parse_users(raw: str | None) -> Users:
    if not raw:
        raise RuntimeError("RODNEY_USERS is not set")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or not parsed:
        raise RuntimeError("RODNEY_USERS must be a non-empty JSON object of author -> password")
    return parsed
