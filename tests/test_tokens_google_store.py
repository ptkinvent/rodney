import json
import time

import pytest

from rodney import google
from rodney.auth import author_id, parse_blocked
from rodney.authstore import DynamoAuthStore, MemoryAuthStore
from rodney.tokens import InvalidToken, b64url, pkce_s256, sign, verify

KEY = b"k" * 32


def test_sign_verify_roundtrip_and_tampering():
    token = sign(KEY, "access", {"sub": "a", "exp": time.time() + 60})
    assert verify(KEY, token, "access")["sub"] == "a"
    head, payload, sig = token.split(".")
    forged = b64url(json.dumps({"sub": "admin", "typ": "access", "exp": time.time() + 60}).encode())
    for bad in [f"{head}.{forged}.{sig}", token + "x", "a.b", "", "not-a-token"]:
        with pytest.raises(InvalidToken):
            verify(KEY, bad, "access")
    with pytest.raises(InvalidToken):
        verify(b"other-key" * 4, token, "access")


def test_kinds_are_not_interchangeable_and_expiry_enforced():
    state = sign(KEY, "state", {"exp": time.time() + 60})
    with pytest.raises(InvalidToken, match="wrong token type"):
        verify(KEY, state, "access")
    old = sign(KEY, "access", {"exp": 100})
    with pytest.raises(InvalidToken, match="expired"):
        verify(KEY, old, "access", now=101)
    with pytest.raises(InvalidToken):
        verify(KEY, sign(KEY, "access", {}), "access")  # no exp at all


def test_alg_none_is_rejected():
    header = b64url(b'{"alg":"none","typ":"JWT"}')
    payload = b64url(json.dumps({"typ": "access", "sub": "x", "exp": time.time() + 60}).encode())
    with pytest.raises(InvalidToken):
        verify(KEY, f"{header}.{payload}.", "access")


def test_pkce_matches_rfc7636_example():
    assert pkce_s256("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"


def test_author_ids_are_stable_anonymous_and_distinct():
    assert author_id("12345") == author_id("12345")
    assert author_id("12345") != author_id("12346")
    handle = author_id("12345")
    assert handle.startswith("u-") and len(handle) == 12 and "12345" not in handle


def test_parse_blocked():
    assert parse_blocked(None) == parse_blocked("") == frozenset()
    assert parse_blocked(" u-aaa, u-bbb ,,") == frozenset({"u-aaa", "u-bbb"})


def _id_token(**claims):
    base = {"iss": "https://accounts.google.com", "aud": "cid", "sub": "123", "exp": time.time() + 600, "email": "a@b.c", "email_verified": True}
    return "h." + b64url(json.dumps({**base, **claims}).encode()) + ".s"


def _exchange(**claims):
    return google.exchange_code("cid", "secret", "https://x/callback", "code", post=lambda url, data: {"id_token": _id_token(**claims)})


def test_google_exchange_checks_claims():
    assert _exchange()["email"] == "a@b.c"
    for bad in [{"aud": "someone-else"}, {"iss": "https://evil.example"}, {"exp": 1}, {"email_verified": False}, {"email": ""}, {"sub": ""}]:
        with pytest.raises(google.GoogleError):
            _exchange(**bad)


def test_google_exchange_sends_secret_and_survives_failures():
    seen = {}
    google.exchange_code("cid", "secret", "https://x/cb", "abc", post=lambda url, data: seen.update(url=url, **data) or {"id_token": _id_token()})
    assert seen["client_secret"] == "secret" and seen["code"] == "abc" and seen["redirect_uri"] == "https://x/cb"
    def boom(url, data):
        raise OSError("network")
    with pytest.raises(google.GoogleError):
        google.exchange_code("cid", "s", "r", "c", post=boom)


class FakeDynamo:
    """Just enough of the DynamoDB client: put_item and an atomic delete_item(ALL_OLD)."""

    def __init__(self):
        self.rows = {}

    def put_item(self, TableName, Item):
        self.rows[Item["id"]["S"]] = Item

    def delete_item(self, TableName, Key, ReturnValues):
        old = self.rows.pop(Key["id"]["S"], None)
        return {"Attributes": old} if old else {}


@pytest.mark.parametrize("make", [lambda clock: MemoryAuthStore(clock), lambda clock: DynamoAuthStore("t", FakeDynamo(), clock)])
def test_auth_store_is_single_use_and_expires(make):
    now = [1000.0]
    store = make(lambda: now[0])
    store.put("code", "tok", {"a": 1}, 60)
    assert store.take("code", "tok") == {"a": 1}
    assert store.take("code", "tok") is None  # single use
    store.put("refresh", "tok", {"a": 2}, 60)
    assert store.take("code", "tok") is None  # kinds are separate
    now[0] += 61
    assert store.take("refresh", "tok") is None  # expired
    assert store.take("refresh", "never-existed") is None


def test_dynamo_store_keeps_only_hashes():
    ddb = FakeDynamo()
    DynamoAuthStore("t", ddb).put("refresh", "super-secret-token", {"a": 1}, 60)
    assert all("super-secret-token" not in key for key in ddb.rows)


def test_google_failures_are_logged_with_googles_error_code(caplog):
    import io
    import urllib.error

    def rejected(url, data):
        raise urllib.error.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(b'{"error":"invalid_grant","error_description":"Bad Request"}'))

    with caplog.at_level("WARNING"), pytest.raises(google.GoogleError):
        google.exchange_code("cid", "the-client-secret", "r", "c", post=rejected)
    assert "HTTP 400 invalid_grant" in caplog.text
    assert "the-client-secret" not in caplog.text
