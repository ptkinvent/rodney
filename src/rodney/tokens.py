"""Signed tokens (HS256 JWTs) and PKCE, using only the standard library.

Rodney signs three kinds of short blobs with one key. The `typ` claim keeps them from
being used in place of each other:
    access   what Claude sends on every MCP call
    state    the in-flight login, carried through Google and back
    client   a dynamically registered OAuth client (so we never store registrations)
"""

import base64
import hashlib
import hmac
import json
import time


class InvalidToken(Exception):
    pass


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(key: bytes, signing_input: str) -> str:
    return b64url(hmac.new(key, signing_input.encode("ascii"), hashlib.sha256).digest())


def sign(key: bytes, typ: str, claims: dict) -> str:
    header = b64url(b'{"alg":"HS256","typ":"JWT"}')
    payload = b64url(json.dumps({**claims, "typ": typ}, separators=(",", ":")).encode("utf-8"))
    return f"{header}.{payload}.{_sign(key, f'{header}.{payload}')}"


def verify(key: bytes, token: str, typ: str, now: float | None = None) -> dict:
    """Returns the claims, or raises InvalidToken (bad signature, wrong kind, expired)."""
    try:
        header, payload, signature = token.split(".")
        if not hmac.compare_digest(signature, _sign(key, f"{header}.{payload}")):
            raise InvalidToken("bad signature")
        if json.loads(b64url_decode(header)).get("alg") != "HS256":
            raise InvalidToken("unexpected algorithm")
        claims = json.loads(b64url_decode(payload))
    except InvalidToken:
        raise
    except Exception as e:  # malformed base64/JSON/shape
        raise InvalidToken("malformed token") from e
    if claims.get("typ") != typ:
        raise InvalidToken("wrong token type")
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] <= (time.time() if now is None else now):
        raise InvalidToken("expired")
    return claims


def pkce_s256(verifier: str) -> str:
    return b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
