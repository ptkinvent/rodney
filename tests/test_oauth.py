"""The whole sign-in flow, driven the way Claude drives it, with a fake Google."""

import json
import urllib.parse as up

import pytest

from rodney import google
from rodney.apps import Apps
from rodney.authstore import MemoryAuthStore
from rodney.handler import Request, route
from rodney.oauth import COOKIE, OAuthConfig, OAuthServer
from rodney.store import MemoryStore
from rodney.tokens import pkce_s256, sign

ISSUER = "https://mcp.example.com"
CLAUDE_CB = "https://claude.ai/api/mcp/auth_callback"
VERIFIER = "v" * 64
USERS = {"ptkinvent": "me@example.com", "friend": "friend@example.com"}
KEY = b"s" * 32


class Env:
    def __init__(self):
        self.now = 1_000_000.0
        self.google_users = {"g-me": "me@example.com", "g-friend": "friend@example.com", "g-stranger": "stranger@example.com"}
        self.store = MemoryStore()
        self.apps = Apps(self.store, "https://example.com")
        self.config = OAuthConfig(ISSUER, "gid", "gsecret", KEY, dict(USERS))
        self.oauth = OAuthServer(self.config, MemoryAuthStore(lambda: self.now), self._google, lambda: self.now)

    def _google(self, client_id, secret, redirect_uri, code):
        if code == "g-boom":
            raise google.GoogleError("down")
        return {"email": self.google_users[code]}

    def req(self, method, path, query=None, headers=None, cookies=None, body=""):
        return route(self.apps, self.oauth, Request(method, path, query or {}, headers or {}, cookies or {}, body))

    # --- helpers that play Claude's part
    def register(self, uris=(CLAUDE_CB,), name="Claude"):
        res = self.req("POST", "/register", body=json.dumps({"redirect_uris": list(uris), "client_name": name}))
        return res["statusCode"], json.loads(res["body"])

    def authorize_query(self, client_id, **over):
        q = {"response_type": "code", "client_id": client_id, "redirect_uri": CLAUDE_CB, "state": "claude-state",
             "code_challenge": pkce_s256(VERIFIER), "code_challenge_method": "S256", "resource": f"{ISSUER}/mcp"}
        q.update(over)
        return {k: v for k, v in q.items() if v is not None}

    def begin(self, client_id, **over):
        """GET /authorize -> (consent page response, bind cookie value, signed state)."""
        res = self.req("GET", "/authorize", self.authorize_query(client_id, **over))
        if res["statusCode"] != 200:
            return res, None, None
        bind = res["cookies"][0].split(";")[0].split("=", 1)[1]
        link = up.parse_qs(up.urlsplit(up.unquote(res["body"].split('href="')[1].split('"')[0])).query)
        return res, bind, link["s"][0]

    def login(self, google_code="g-me", client_id=None, bind_override=None, **over):
        """Whole browser trip. Returns (callback response, bind)."""
        client_id = client_id or self.register()[1]["client_id"]
        _, bind, state = self.begin(client_id, **over)
        cont = self.req("GET", "/authorize/continue", {"s": state})
        google_state = up.parse_qs(up.urlsplit(cont["headers"]["location"]).query)["state"][0]
        cb = self.req("GET", "/callback", {"code": google_code, "state": google_state}, cookies={COOKIE: bind_override or bind})
        return cb, client_id

    def token(self, client_id, code, verifier=VERIFIER, **over):
        form = {"grant_type": "authorization_code", "code": code, "client_id": client_id, "redirect_uri": CLAUDE_CB,
                "code_verifier": verifier, "resource": f"{ISSUER}/mcp"}
        form.update(over)
        res = self.req("POST", "/token", body=up.urlencode(form))
        return res["statusCode"], json.loads(res["body"])

    def code_from(self, cb):
        loc = up.urlsplit(cb["headers"]["location"])
        assert loc.geturl().startswith(CLAUDE_CB), loc.geturl()
        return up.parse_qs(loc.query)["code"][0]

    def signed_in(self, who="g-me"):
        cb, cid = self.login(who)
        status, tokens = self.token(cid, self.code_from(cb))
        assert status == 200
        return cid, tokens

    def mcp(self, access, rpc):
        return self.req("POST", "/mcp", headers={"authorization": f"Bearer {access}"}, body=json.dumps(rpc))


@pytest.fixture
def env():
    return Env()


# ---- discovery -------------------------------------------------------------------------------


def test_discovery_documents(env):
    prm = json.loads(env.req("GET", "/.well-known/oauth-protected-resource/mcp")["body"])
    assert prm["resource"] == f"{ISSUER}/mcp" and prm["authorization_servers"] == [ISSUER]
    assert env.req("GET", "/.well-known/oauth-protected-resource")["statusCode"] == 200
    asm = json.loads(env.req("GET", "/.well-known/oauth-authorization-server")["body"])
    assert asm["issuer"] == ISSUER
    assert asm["code_challenge_methods_supported"] == ["S256"]
    assert asm["token_endpoint"] == f"{ISSUER}/token" and asm["registration_endpoint"] == f"{ISSUER}/register"
    assert asm["token_endpoint_auth_methods_supported"] == ["none"]


def test_mcp_without_token_gets_401_with_discovery_pointer(env):
    res = env.req("POST", "/mcp", body="{}")
    assert res["statusCode"] == 401
    assert res["headers"]["www-authenticate"] == f'Bearer resource_metadata="{ISSUER}/.well-known/oauth-protected-resource/mcp"'
    bad = env.req("POST", "/mcp", headers={"authorization": "Bearer garbage"}, body="{}")
    assert bad["statusCode"] == 401 and 'error="invalid_token"' in bad["headers"]["www-authenticate"]


# ---- the happy path --------------------------------------------------------------------------


def test_full_flow_then_tools_work_as_the_right_author(env):
    cid, tokens = env.signed_in("g-friend")
    assert tokens["token_type"] == "Bearer" and tokens["expires_in"] == 3600 and tokens["refresh_token"]
    init = env.mcp(tokens["access_token"], {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["statusCode"] == 200
    created = env.mcp(tokens["access_token"], {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                                               "params": {"name": "create-app", "arguments": {"name": "mine", "html": "x"}}})
    assert json.loads(json.loads(created["body"])["result"]["content"][0]["text"])["meta"]["author"] == "friend"


def test_consent_page_shows_who_gets_access_and_sets_hardened_cookie(env):
    res, bind, state = env.begin(env.register(name="Claude")[1]["client_id"])
    assert "claude.ai" in res["body"] and "Claude" in res["body"]
    cookie = res["cookies"][0]
    for flag in ["HttpOnly", "Secure", "SameSite=Lax"]:
        assert flag in cookie
    assert res["headers"]["cache-control"] == "no-store"


def test_client_name_is_escaped_on_consent_page(env):
    res, _, _ = env.begin(env.register(name="<script>alert(1)</script>")[1]["client_id"])
    assert "<script>alert" not in res["body"]


def test_callback_sends_browser_back_with_code_and_clears_cookie(env):
    cb, _ = env.login()
    loc = up.urlsplit(cb["headers"]["location"])
    assert up.parse_qs(loc.query)["state"] == ["claude-state"]
    assert "Max-Age=0" in cb["cookies"][0]


# ---- refresh and expiry ----------------------------------------------------------------------


def test_refresh_rotates_and_old_token_dies(env):
    cid, t1 = env.signed_in()
    form = lambda rt: up.urlencode({"grant_type": "refresh_token", "refresh_token": rt, "client_id": cid})
    r2 = json.loads(env.req("POST", "/token", body=form(t1["refresh_token"]))["body"])
    assert r2["access_token"] and r2["refresh_token"] != t1["refresh_token"]
    replay = env.req("POST", "/token", body=form(t1["refresh_token"]))
    assert replay["statusCode"] == 400 and json.loads(replay["body"])["error"] == "invalid_grant"
    assert env.req("POST", "/token", body=form(r2["refresh_token"]))["statusCode"] == 200


def test_access_token_expires(env):
    _, t = env.signed_in()
    env.now += 3601
    assert env.mcp(t["access_token"], {"jsonrpc": "2.0", "id": 1, "method": "ping"})["statusCode"] == 401


def test_refresh_token_expires(env):
    cid, t = env.signed_in()
    env.now += 31 * 24 * 3600
    res = env.req("POST", "/token", body=up.urlencode({"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": cid}))
    assert res["statusCode"] == 400


def test_removing_a_user_cuts_off_existing_tokens_immediately(env):
    cid, t = env.signed_in("g-friend")
    del env.config.users["friend"]
    assert env.mcp(t["access_token"], {"jsonrpc": "2.0", "id": 1, "method": "ping"})["statusCode"] == 401
    res = env.req("POST", "/token", body=up.urlencode({"grant_type": "refresh_token", "refresh_token": t["refresh_token"], "client_id": cid}))
    assert res["statusCode"] == 400


# ---- attacks on the code exchange ------------------------------------------------------------


def test_code_is_single_use(env):
    cb, cid = env.login()
    code = env.code_from(cb)
    assert env.token(cid, code)[0] == 200
    status, body = env.token(cid, code)
    assert status == 400 and body["error"] == "invalid_grant"


def test_wrong_pkce_verifier_fails_and_burns_the_code(env):
    cb, cid = env.login()
    code = env.code_from(cb)
    assert env.token(cid, code, verifier="w" * 64)[1]["error"] == "invalid_grant"
    assert env.token(cid, code)[0] == 400  # the stolen code can't be retried with the right verifier


def test_code_bound_to_client_and_redirect_uri(env):
    cb, cid = env.login()
    other = env.register(name="A different client")[1]["client_id"]
    assert other != cid
    assert env.token(other, env.code_from(cb))[0] == 400
    cb, cid = env.login()
    assert env.token(cid, env.code_from(cb), redirect_uri="https://evil.example/cb")[0] == 400


def test_token_endpoint_input_validation(env):
    cb, cid = env.login()
    code = env.code_from(cb)
    assert env.token("forged-client", code)[1]["error"] == "invalid_client"
    assert env.token(cid, code, grant_type="password")[1]["error"] == "unsupported_grant_type"
    assert env.token(cid, code, resource="https://other.example/mcp")[1]["error"] == "invalid_target"
    assert env.token(cid, code, verifier="short")[1]["error"] == "invalid_grant"
    assert env.token(cid, "", verifier="é" * 50)[1]["error"] == "invalid_grant"  # non-ascii verifier can't crash it


def test_authorization_code_expires(env):
    cb, cid = env.login()
    env.now += 121
    assert env.token(cid, env.code_from(cb))[0] == 400


# ---- registration and redirect rules ---------------------------------------------------------


@pytest.mark.parametrize("uri", [
    "https://evil.example/cb", "https://claude.ai/other", "http://claude.ai/api/mcp/auth_callback",
    "http://localhost.evil.com/callback", "http://localhost:3000/other", "http://localhost/callback?x=1",
    "javascript:alert(1)", "https://claude.ai.evil.com/api/mcp/auth_callback", "http://user@localhost/callback",
])
def test_registration_rejects_untrusted_redirects(env, uri):
    status, body = env.register(uris=[uri])
    assert status == 400 and body["error"] == "invalid_redirect_uri"


def test_registration_edge_cases(env):
    assert env.req("POST", "/register", body="not json")["statusCode"] == 400
    assert env.req("POST", "/register", body=json.dumps({"redirect_uris": []}))["statusCode"] == 400
    assert env.req("POST", "/register", body=json.dumps({}))["statusCode"] == 400
    status, body = env.register(uris=[CLAUDE_CB, "http://localhost/callback", "http://127.0.0.1/callback"])
    assert status == 201 and body["token_endpoint_auth_method"] == "none"


def test_loopback_redirect_matches_on_any_port(env):
    cid = env.register(uris=["http://localhost/callback"])[1]["client_id"]
    ok, _, _ = env.begin(cid, redirect_uri="http://localhost:53682/callback")
    assert ok["statusCode"] == 200
    res, _, _ = env.begin(cid, redirect_uri="http://localhost:53682/other")
    assert res["statusCode"] == 400


def test_authorize_never_redirects_to_unregistered_addresses(env):
    # invalid client or redirect: an error PAGE, not a redirect (would be an open redirect)
    assert env.req("GET", "/authorize", env.authorize_query("forged"))["statusCode"] == 400
    cid = env.register()[1]["client_id"]
    res = env.req("GET", "/authorize", env.authorize_query(cid, redirect_uri="https://evil.example/cb"))
    assert res["statusCode"] == 400 and "location" not in res["headers"]


def test_authorize_reports_other_errors_to_the_client(env):
    cid = env.register()[1]["client_id"]
    for over, error in [({"code_challenge": None}, "invalid_request"), ({"code_challenge_method": "plain"}, "invalid_request"),
                        ({"response_type": "token"}, "unsupported_response_type"), ({"resource": "https://other.example"}, "invalid_target")]:
        res = env.req("GET", "/authorize", env.authorize_query(cid, **over))
        loc = up.urlsplit(res["headers"]["location"])
        assert res["statusCode"] == 302 and loc.geturl().startswith(CLAUDE_CB)
        assert up.parse_qs(loc.query)["error"] == [error] and up.parse_qs(loc.query)["state"] == ["claude-state"]


def test_unknown_signed_blobs_cannot_be_used_as_other_kinds(env):
    forged_client = sign(b"not-our-key" * 3, "client", {"ru": [CLAUDE_CB], "exp": env.now + 999})
    assert env.req("GET", "/authorize", env.authorize_query(forged_client))["statusCode"] == 400
    state_as_access = sign(KEY, "state", {"sub": "ptkinvent", "aud": f"{ISSUER}/mcp", "exp": env.now + 999})
    assert env.mcp(state_as_access, {"jsonrpc": "2.0", "id": 1, "method": "ping"})["statusCode"] == 401
    wrong_aud = sign(KEY, "access", {"sub": "ptkinvent", "aud": "https://other.example/mcp", "exp": env.now + 999})
    assert env.mcp(wrong_aud, {"jsonrpc": "2.0", "id": 1, "method": "ping"})["statusCode"] == 401


# ---- the browser leg: allowlist, cookie binding, Google failures -----------------------------


def test_unlisted_google_account_is_refused_and_gets_no_code(env):
    cb, _ = env.login("g-stranger")
    assert cb["statusCode"] == 403 and "stranger@example.com" in cb["body"]
    assert "location" not in cb["headers"]
    assert env.oauth.store.items == {}  # nothing was issued


def test_login_must_finish_in_the_browser_that_started_it(env):
    # Attacker starts a flow, victim completes the Google step: victim's browser lacks the cookie.
    cid = env.register()[1]["client_id"]
    _, bind, state = env.begin(cid)
    cont = env.req("GET", "/authorize/continue", {"s": state})
    gs = up.parse_qs(up.urlsplit(cont["headers"]["location"]).query)["state"][0]
    assert env.req("GET", "/callback", {"code": "g-me", "state": gs}, cookies={})["statusCode"] == 400
    assert env.req("GET", "/callback", {"code": "g-me", "state": gs}, cookies={COOKIE: "someone-elses"})["statusCode"] == 400
    assert env.oauth.store.items == {}


def test_expired_or_forged_login_state(env):
    cid = env.register()[1]["client_id"]
    _, bind, state = env.begin(cid)
    env.now += 601
    assert env.req("GET", "/authorize/continue", {"s": state})["statusCode"] == 400
    assert env.req("GET", "/callback", {"code": "g-me", "state": state}, cookies={COOKIE: bind})["statusCode"] == 400
    assert env.req("GET", "/callback", {"code": "g-me", "state": "forged"}, cookies={COOKIE: bind})["statusCode"] == 400


def test_user_cancelling_at_google_is_reported_to_claude(env):
    cid = env.register()[1]["client_id"]
    _, bind, state = env.begin(cid)
    cb = env.req("GET", "/callback", {"error": "access_denied", "state": state}, cookies={COOKIE: bind})
    assert up.parse_qs(up.urlsplit(cb["headers"]["location"]).query)["error"] == ["access_denied"]


def test_google_failure_shows_error_not_a_stack_trace(env):
    cb, _ = env.login("g-boom")
    assert cb["statusCode"] == 502 and "Traceback" not in cb["body"]


def test_continue_redirects_to_google_with_our_client_id(env):
    cid = env.register()[1]["client_id"]
    _, _, state = env.begin(cid)
    loc = up.urlsplit(env.req("GET", "/authorize/continue", {"s": state})["headers"]["location"])
    q = up.parse_qs(loc.query)
    assert loc.netloc == "accounts.google.com" and q["client_id"] == ["gid"] and q["redirect_uri"] == [f"{ISSUER}/callback"]
    assert q["scope"] == ["openid email"]


# ---- routing ---------------------------------------------------------------------------------


def test_methods_and_unknown_paths(env):
    assert env.req("GET", "/mcp")["statusCode"] == 405
    assert env.req("POST", "/authorize")["statusCode"] == 405
    assert env.req("GET", "/token")["statusCode"] == 405
    assert env.req("GET", "/nope")["statusCode"] == 404
