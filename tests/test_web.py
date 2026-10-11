"""The website: browser sign-in, sessions, and the My apps page."""

import urllib.parse as up

import pytest

import rodney.handler as handler_module
from rodney import google
from rodney.apps import Apps
from rodney.dataapi import DataApi
from rodney.auth import author_id
from rodney.datastore import MemoryDataStore
from rodney.authstore import MemoryAuthStore
from rodney.handler import Request, route, to_request
from rodney.oauth import OAuthConfig, OAuthServer
from rodney.registry import MemoryRegistry
from rodney.store import MemoryStore
from rodney.tokens import sign
from rodney.web import LOGIN_COOKIE, SESSION_COOKIE, SESSION_TTL, WebApp

KEY = b"w" * 32
APP_URL = "https://app.example.com"
ME, FRIEND = author_id("sub-me"), author_id("sub-friend")


class Env:
    def __init__(self):
        self.now = 1_000_000.0
        self.registry = MemoryRegistry()
        self.apps = Apps(MemoryStore(), self.registry, MemoryDataStore(), "https://example.com")
        self.config = OAuthConfig("https://mcp.example.com", "gid", "gsecret", KEY)
        self.oauth = OAuthServer(self.config, MemoryAuthStore(lambda: self.now), clock=lambda: self.now)
        self.logins = {"g-me": ("me@example.com", "sub-me"), "g-friend": ("friend@example.com", "sub-friend")}
        self.web = WebApp(self.config, APP_URL, self.apps, self._google, lambda: self.now)

    def _google(self, client_id, secret, redirect_uri, code):
        if code == "g-boom":
            raise google.GoogleError("down")
        email, sub = self.logins[code]
        return {"email": email, "sub": sub}

    def req(self, method, path, query=None, cookies=None, surface="app", headers=None):
        return route(self.apps, self.oauth, Request(method, path, query or {}, headers or {}, cookies or {}, "", surface), self.web)

    def sign_in(self, who="g-me"):
        """The whole browser trip; returns the session cookie value."""
        start = self.req("GET", "/login")
        bind = start["cookies"][0].split(";")[0].split("=", 1)[1]
        state = up.parse_qs(up.urlsplit(start["headers"]["location"]).query)["state"][0]
        done = self.req("GET", "/login/callback", {"code": who, "state": state}, {LOGIN_COOKIE: bind})
        assert done["statusCode"] == 302 and done["headers"]["location"] == "/", done
        return next(c for c in done["cookies"] if c.startswith(SESSION_COOKIE)).split(";")[0].split("=", 1)[1]


@pytest.fixture
def env():
    return Env()


# ---- the My apps page -----------------------------------------------------------------------


def test_signed_out_visitor_is_prompted_to_sign_in_and_sees_no_apps(env):
    env.apps.create_app(ME, "secret-app", description="private thoughts", html="x")
    res = env.req("GET", "/")
    assert res["statusCode"] == 200 and 'href="/login"' in res["body"] and "Sign in with Google" in res["body"]
    assert "secret-app" not in res["body"] and "private thoughts" not in res["body"]
    assert "Sign out" not in res["body"]


def test_signed_in_user_sees_only_their_own_apps(env):
    env.apps.create_app(ME, "mine-a", description="first app", html="x")
    env.apps.create_app(ME, "mine-b", html="x")
    env.apps.create_app(FRIEND, "theirs", description="not yours", html="x")
    body = env.req("GET", "/", cookies={SESSION_COOKIE: env.sign_in()})["body"]
    assert "mine-a" in body and "mine-b" in body and "first app" in body
    assert "theirs" not in body and "not yours" not in body
    assert "2 apps" in body and "Signed in as <code>me@example.com</code>" in body and "Sign out" in body
    assert f"<code>{ME}</code>" not in body  # the email replaces the handle in the header
    assert 'href="https://example.com/mine-a/"' in body and 'href="https://example.com/mine-a/admin/"' in body


def test_empty_state_and_singular_count(env):
    cookie = env.sign_in()
    assert "No apps yet" in env.req("GET", "/", cookies={SESSION_COOKIE: cookie})["body"]
    env.apps.create_app(ME, "only", html="x")
    assert "1 app<" in env.req("GET", "/", cookies={SESSION_COOKIE: cookie})["body"]


def test_app_text_cannot_inject_html(env):
    env.apps.create_app(ME, "evil", description='<script>alert(1)</script><img src=x onerror=alert(2)>', html="x")
    body = env.req("GET", "/", cookies={SESSION_COOKIE: env.sign_in()})["body"]
    assert "<script>alert" not in body and "<img src=x" not in body and "&lt;script&gt;" in body


def test_pages_carry_strict_security_headers(env):
    for res in (env.req("GET", "/"), env.req("GET", "/", cookies={SESSION_COOKIE: env.sign_in()})):
        h = res["headers"]
        assert "frame-ancestors 'none'" in h["content-security-policy"] and "script-src" not in h["content-security-policy"]
        assert "default-src 'none'" in h["content-security-policy"]
        assert h["x-content-type-options"] == "nosniff" and h["cache-control"] == "no-store"
        assert not any(k.lower().startswith("access-control") for k in h)  # no CORS: other sites can't read it


# ---- signing in -----------------------------------------------------------------------------


def test_login_redirects_to_google_with_our_client_and_the_websites_callback(env):
    res = env.req("GET", "/login")
    loc = up.urlsplit(res["headers"]["location"])
    q = up.parse_qs(loc.query)
    assert res["statusCode"] == 302 and loc.netloc == "accounts.google.com"
    assert q["client_id"] == ["gid"] and q["redirect_uri"] == [f"{APP_URL}/login/callback"] and q["scope"] == ["openid email"]
    cookie = res["cookies"][0]
    for flag in ["HttpOnly", "Secure", "SameSite=Lax", "Path=/login", "Max-Age=600"]:
        assert flag in cookie


def test_session_cookie_is_hardened_and_lasts_14_days(env):
    start = env.req("GET", "/login")
    bind = start["cookies"][0].split(";")[0].split("=", 1)[1]
    state = up.parse_qs(up.urlsplit(start["headers"]["location"]).query)["state"][0]
    done = env.req("GET", "/login/callback", {"code": "g-me", "state": state}, {LOGIN_COOKIE: bind})
    session = next(c for c in done["cookies"] if c.startswith(SESSION_COOKIE))
    for flag in ["HttpOnly", "Secure", "SameSite=Lax", "Path=/", f"Max-Age={SESSION_TTL}"]:
        assert flag in session
    assert "Domain" not in session  # host-only: never sent to the apps or the MCP host
    assert any(c.startswith(f"{LOGIN_COOKIE}=;") and "Max-Age=0" in c for c in done["cookies"])


def test_session_holds_the_handle_and_the_email_for_display(env):
    import json
    from rodney.tokens import b64url_decode

    claims = json.loads(b64url_decode(env.sign_in().split(".")[1]))
    assert claims["sub"] == ME and claims["email"] == "me@example.com"


def test_older_sessions_without_an_email_still_work_and_show_the_handle(env):
    old = sign(KEY, "session", {"sub": ME, "iat": env.now, "exp": env.now + 999})
    body = env.req("GET", "/", cookies={SESSION_COOKIE: old})["body"]
    assert f"Signed in as <code>{ME}</code>" in body


def test_the_email_is_escaped_in_the_header(env):
    env.logins["g-evil"] = ("<b>x</b>@example.com", "sub-evil")
    body = env.req("GET", "/", cookies={SESSION_COOKIE: env.sign_in("g-evil")})["body"]
    assert "<b>x</b>@" not in body and "&lt;b&gt;x&lt;/b&gt;@example.com" in body


def test_other_people_never_see_your_email(env):
    env.sign_in("g-friend")
    body = env.req("GET", "/", cookies={SESSION_COOKIE: env.sign_in("g-me")})["body"]
    assert "friend@example.com" not in body


def test_already_signed_in_goes_straight_to_my_apps(env):
    res = env.req("GET", "/login", cookies={SESSION_COOKIE: env.sign_in()})
    assert res["statusCode"] == 302 and res["headers"]["location"] == "/" and "google" not in res["headers"]["location"]


def test_login_must_finish_in_the_browser_that_started_it(env):
    start = env.req("GET", "/login")
    state = up.parse_qs(up.urlsplit(start["headers"]["location"]).query)["state"][0]
    for cookies in ({}, {LOGIN_COOKIE: "someone-elses"}):
        res = env.req("GET", "/login/callback", {"code": "g-me", "state": state}, cookies)
        assert res["statusCode"] == 400 and "cookies" not in res


def test_callback_rejects_forged_expired_and_wrong_kind_state(env):
    start = env.req("GET", "/login")
    bind = start["cookies"][0].split(";")[0].split("=", 1)[1]
    state = up.parse_qs(up.urlsplit(start["headers"]["location"]).query)["state"][0]
    oauth_state = sign(KEY, "state", {"b": "x", "exp": env.now + 999})  # an OAuth-flow blob must not work here
    for bad in ["forged", oauth_state]:
        assert env.req("GET", "/login/callback", {"code": "g-me", "state": bad}, {LOGIN_COOKIE: bind})["statusCode"] == 400
    env.now += 601
    assert env.req("GET", "/login/callback", {"code": "g-me", "state": state}, {LOGIN_COOKIE: bind})["statusCode"] == 400


def test_google_failures_cancel_and_missing_code(env):
    start = env.req("GET", "/login")
    bind = start["cookies"][0].split(";")[0].split("=", 1)[1]
    state = up.parse_qs(up.urlsplit(start["headers"]["location"]).query)["state"][0]
    call = lambda **q: env.req("GET", "/login/callback", {"state": state, **q}, {LOGIN_COOKIE: bind})
    assert call(code="g-boom")["statusCode"] == 502
    assert call(error="access_denied")["statusCode"] == 400
    assert call()["statusCode"] == 400
    assert "Traceback" not in call(code="g-boom")["body"]


# ---- session lifetime, blocking, logout -----------------------------------------------------


def test_session_expires_after_14_days(env):
    cookie = env.sign_in()
    env.now += SESSION_TTL - 10
    assert "Sign out" in env.req("GET", "/", cookies={SESSION_COOKIE: cookie})["body"]
    env.now += 20
    assert "Sign in with Google" in env.req("GET", "/", cookies={SESSION_COOKIE: cookie})["body"]


def test_tampered_or_borrowed_cookies_are_ignored(env):
    cookie = env.sign_in()
    head, payload, sig = cookie.split(".")
    for bad in [cookie + "x", f"{head}.{payload}.", "garbage", ""]:
        assert "Sign in with Google" in env.req("GET", "/", cookies={SESSION_COOKIE: bad})["body"]
    access = sign(KEY, "access", {"sub": ME, "exp": env.now + 999})  # an MCP access token is not a session
    assert "Sign in with Google" in env.req("GET", "/", cookies={SESSION_COOKIE: access})["body"]


def test_a_session_is_not_an_mcp_access_token(env):
    env.apps.create_app(ME, "x", html="x")
    res = env.req("POST", "/mcp", surface="mcp", headers={"authorization": f"Bearer {env.sign_in()}"})
    assert res["statusCode"] == 401


def test_blocking_an_author_ends_their_session_and_refuses_new_ones(env):
    cookie = env.sign_in()
    env.config.blocked = frozenset({ME})
    assert "Sign in with Google" in env.req("GET", "/", cookies={SESSION_COOKIE: cookie})["body"]
    start = env.req("GET", "/login")
    bind = start["cookies"][0].split(";")[0].split("=", 1)[1]
    state = up.parse_qs(up.urlsplit(start["headers"]["location"]).query)["state"][0]
    res = env.req("GET", "/login/callback", {"code": "g-me", "state": state}, {LOGIN_COOKIE: bind})
    assert res["statusCode"] == 403 and not any(c.startswith(f"{SESSION_COOKIE}=") and "Max-Age=0" not in c for c in res.get("cookies", []))


def test_logout_clears_the_session_and_only_accepts_post(env):
    res = env.req("POST", "/logout", cookies={SESSION_COOKIE: env.sign_in()})
    assert res["statusCode"] == 302 and res["headers"]["location"] == "/"
    assert res["cookies"][0].startswith(f"{SESSION_COOKIE}=;") and "Max-Age=0" in res["cookies"][0]
    assert env.req("GET", "/logout")["statusCode"] == 405


# ---- routing between the two hosts ----------------------------------------------------------


def test_each_host_serves_only_its_own_routes(env):
    for path in ["/", "/login", "/login/callback", "/logout"]:
        assert env.req("GET", path, surface="mcp")["statusCode"] in (404, 405)
        assert env.req("POST", path, surface="mcp")["statusCode"] in (404, 405)
    for path in ["/mcp", "/authorize", "/token", "/register", "/callback", "/.well-known/oauth-authorization-server"]:
        assert env.req("GET", path, surface="app")["statusCode"] == 404
        assert env.req("POST", path, surface="app")["statusCode"] == 404
    assert env.req("GET", "/nope")["statusCode"] == 404 and env.req("POST", "/")["statusCode"] == 405


def test_surface_comes_only_from_the_cloudfront_header():
    event = lambda h: {"requestContext": {"http": {"method": "GET"}}, "rawPath": "/", "headers": h}
    assert to_request(event({"x-rodney-surface": "app"})).surface == "app"
    assert to_request(event({})).surface == "mcp"
    assert to_request(event({"x-rodney-surface": "anything-else"})).surface == "mcp"


def test_lambda_handler_wires_everything_together(env, monkeypatch):
    """The real entry point, with a real-shaped event: catches argument-order bugs in handler()."""
    data = DataApi(env.registry, env.apps.data, lambda: "2026-01-01T00:00:00.000Z")
    monkeypatch.setattr(handler_module, "_state", (env.apps, env.oauth, env.web, data))
    event = {"requestContext": {"http": {"method": "GET"}}, "rawPath": "/", "rawQueryString": "", "headers": {"x-rodney-surface": "app"}}
    res = handler_module.handler(event, None)
    assert res["statusCode"] == 200 and "Sign in with Google" in res["body"]
    mcp_event = {**event, "headers": {}, "rawPath": "/.well-known/oauth-protected-resource/mcp"}
    assert handler_module.handler(mcp_event, None)["statusCode"] == 200
