"""Rodney as an OAuth 2.1 authorization server for Claude, with Google doing the sign-in.

The flow Claude drives (RFC 9728 discovery, RFC 7591 registration, PKCE):

    POST /mcp  (no token)        -> 401 + pointer to the discovery document
    GET  /.well-known/...        -> where the authorization server is, and what it supports
    POST /register               -> Claude gets a client_id (a signed blob, nothing stored)
    GET  /authorize              -> consent page; sets a cookie binding the login to this browser
    GET  /authorize/continue     -> redirect to Google
    GET  /callback               -> Google returns; we check the email allowlist, mint a code,
                                    and send the browser back to Claude
    POST /token                  -> Claude trades the code (+ PKCE verifier) for tokens;
                                    later trades the refresh token for new ones

State lives in two places: signed blobs (client ids, in-flight logins, access tokens) and a
single-use store (authorization codes, refresh tokens).
"""

import hmac
import html
import json
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass
from typing import Callable

from . import google
from .auth import Users, author_for_email
from .authstore import AuthStore
from .tokens import InvalidToken, pkce_s256, sha256_hex, sign, verify

ACCESS_TTL = 3600
CODE_TTL = 120
REFRESH_TTL = 30 * 24 * 3600
STATE_TTL = 600
CLIENT_TTL = 365 * 24 * 3600
COOKIE = "rodney_login"

# Where Claude is allowed to send a signed-in browser back to.
ALLOWED_REDIRECTS = {"https://claude.ai/api/mcp/auth_callback"}
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}  # Claude Code: any port (RFC 8252 7.3)
_VERIFIER_RE = re.compile(r"[A-Za-z0-9\-._~]{43,128}")
_CHALLENGE_RE = re.compile(r"[A-Za-z0-9\-_]{43}")


@dataclass
class OAuthConfig:
    issuer: str  # e.g. https://mcp.ptkinvent.com (no trailing slash)
    google_client_id: str
    google_client_secret: str
    signing_key: bytes
    users: Users

    @property
    def resource(self) -> str:
        return f"{self.issuer}/mcp"

    @property
    def callback_url(self) -> str:
        return f"{self.issuer}/callback"


class Unauthorized(Exception):
    def __init__(self, error: str | None = None) -> None:
        self.error = error


# --- response helpers -----------------------------------------------------------


def json_response(status: int, body: dict, headers: dict | None = None) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store", **(headers or {})},
        "body": json.dumps(body),
    }


def redirect(url: str, cookies: list[str] | None = None) -> dict:
    res = {"statusCode": 302, "headers": {"location": url, "cache-control": "no-store"}, "body": ""}
    if cookies:
        res["cookies"] = cookies
    return res


_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:30rem;margin:4rem auto;padding:0 1.25rem;line-height:1.55;color:#161a26;background:#f3f4f8}}
@media(prefers-color-scheme:dark){{body{{color:#e8eaf3;background:#0f111a}}a.go{{color:#0f111a!important;background:#9b9dff!important}}}}
h1{{font-size:1.5rem;margin:0 0 1rem}} p{{margin:0 0 1rem}} code{{background:#8883;padding:.1em .35em;border-radius:4px}}
a.go{{display:inline-block;padding:.75rem 1.2rem;border-radius:8px;background:#3b3bd9;color:#fff;text-decoration:none;font-weight:600}}
</style></head><body>{body}</body></html>"""


def page(status: int, title: str, body_html: str, cookies: list[str] | None = None) -> dict:
    res = {
        "statusCode": status,
        "headers": {"content-type": "text/html; charset=utf-8", "cache-control": "no-store"},
        "body": _PAGE.format(title=html.escape(title), body=body_html),
    }
    if cookies:
        res["cookies"] = cookies
    return res


def error_page(status: int, title: str, message: str) -> dict:
    return page(status, title, f"<h1>{html.escape(title)}</h1><p>{html.escape(message)}</p>")


def add_query(url: str, params: dict) -> str:
    return url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)


def oauth_error(error: str, description: str, status: int = 400) -> dict:
    return json_response(status, {"error": error, "error_description": description})


# --- redirect URI rules ---------------------------------------------------------


def is_allowed_redirect(uri: str) -> bool:
    if uri in ALLOWED_REDIRECTS:
        return True
    try:
        p = urllib.parse.urlsplit(uri)
        p.port  # noqa: B018 - raises ValueError on a bad port
    except ValueError:
        return False
    return (
        p.scheme == "http"
        and p.hostname in LOOPBACK_HOSTS
        and p.path == "/callback"
        and not (p.query or p.fragment or p.username or p.password)
    )


def redirect_matches(registered: str, requested: str) -> bool:
    if registered == requested:
        return True
    try:
        a, b = urllib.parse.urlsplit(registered), urllib.parse.urlsplit(requested)
    except ValueError:
        return False
    return (  # loopback redirects may differ only in port
        a.scheme == b.scheme == "http"
        and a.hostname == b.hostname
        and a.hostname in LOOPBACK_HOSTS
        and a.path == b.path
        and not (b.query or b.fragment)
    )


# --- the server -----------------------------------------------------------------


class OAuthServer:
    def __init__(
        self,
        config: OAuthConfig,
        store: AuthStore,
        google_exchange: Callable = google.exchange_code,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = config
        self.store = store
        self.google_exchange = google_exchange
        self.clock = clock

    # -- discovery

    def protected_resource_metadata(self) -> dict:
        return {
            "resource": self.cfg.resource,
            "authorization_servers": [self.cfg.issuer],
            "bearer_methods_supported": ["header"],
            "resource_name": "Rodney",
        }

    def authorization_server_metadata(self) -> dict:
        iss = self.cfg.issuer
        return {
            "issuer": iss,
            "authorization_endpoint": f"{iss}/authorize",
            "token_endpoint": f"{iss}/token",
            "registration_endpoint": f"{iss}/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
        }

    def www_authenticate(self, error: str | None = None) -> str:
        value = f'Bearer resource_metadata="{self.cfg.issuer}/.well-known/oauth-protected-resource/mcp"'
        return value + (f', error="{error}"' if error else "")

    # -- helpers

    def _now(self) -> int:
        return int(self.clock())

    def _is_our_resource(self, resource: str) -> bool:
        return resource.rstrip("/") in (self.cfg.resource, self.cfg.issuer)

    def _client(self, client_id: str) -> dict:
        return verify(self.cfg.signing_key, client_id, "client", self.clock())

    def _login_cookie(self, value: str, max_age: int) -> str:
        return f"{COOKIE}={value}; Max-Age={max_age}; Path=/; HttpOnly; Secure; SameSite=Lax"

    # -- POST /register

    def register(self, body: str) -> dict:
        try:
            meta = json.loads(body or "{}")
        except json.JSONDecodeError:
            return oauth_error("invalid_client_metadata", "Body must be JSON.")
        uris = meta.get("redirect_uris") if isinstance(meta, dict) else None
        if not (isinstance(uris, list) and 1 <= len(uris) <= 5 and all(isinstance(u, str) and is_allowed_redirect(u) for u in uris)):
            return oauth_error("invalid_redirect_uri", "Redirect URIs must be Claude's callback or a loopback /callback.")
        name = meta.get("client_name")
        name = name[:80] if isinstance(name, str) and name else "MCP client"
        now = self._now()
        client_id = sign(
            self.cfg.signing_key, "client", {"ru": uris, "name": name, "iat": now, "exp": now + CLIENT_TTL}
        )
        return json_response(
            201,
            {
                "client_id": client_id,
                "client_id_issued_at": now,
                "client_name": name,
                "redirect_uris": uris,
                "token_endpoint_auth_method": "none",
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
            },
        )

    # -- GET /authorize

    def authorize(self, q: dict) -> dict:
        try:
            client = self._client(q.get("client_id", ""))
        except InvalidToken:
            return error_page(400, "Unknown app", "This sign-in link is not valid. Start again from Claude.")
        registered = client["ru"]
        redirect_uri = q.get("redirect_uri") or (registered[0] if len(registered) == 1 else "")
        if not (is_allowed_redirect(redirect_uri) and any(redirect_matches(r, redirect_uri) for r in registered)):
            return error_page(400, "Invalid redirect", "The redirect address does not match this app's registration.")

        client_state = q.get("state", "")

        def fail(error: str, description: str) -> dict:
            params = {"error": error, "error_description": description}
            if client_state:
                params["state"] = client_state
            return redirect(add_query(redirect_uri, params))

        if q.get("response_type") != "code":
            return fail("unsupported_response_type", "Only the code flow is supported.")
        challenge = q.get("code_challenge", "")
        if q.get("code_challenge_method") != "S256" or not _CHALLENGE_RE.fullmatch(challenge):
            return fail("invalid_request", "PKCE with S256 is required.")
        resource = q.get("resource") or self.cfg.resource
        if not self._is_our_resource(resource):
            return fail("invalid_target", "Unknown resource.")

        bind = secrets.token_urlsafe(24)
        now = self._now()
        state = sign(
            self.cfg.signing_key,
            "state",
            {
                "cid": q["client_id"],
                "ru": redirect_uri,
                "cc": challenge,
                "st": client_state,
                "res": self.cfg.resource,
                "b": sha256_hex(bind),
                "exp": now + STATE_TTL,
            },
        )
        host = urllib.parse.urlsplit(redirect_uri).netloc
        body = (
            "<h1>Connect to Rodney</h1>"
            f"<p><strong>{html.escape(client['name'])}</strong> wants to build and edit apps for you on Rodney.</p>"
            f"<p>After you sign in it will receive access at <code>{html.escape(host)}</code>.</p>"
            f'<p><a class="go" href="/authorize/continue?{urllib.parse.urlencode({"s": state})}">Continue with Google</a></p>'
            "<p><small>Only approved accounts can sign in.</small></p>"
        )
        return page(200, "Connect to Rodney", body, cookies=[self._login_cookie(bind, STATE_TTL)])

    # -- GET /authorize/continue

    def continue_to_google(self, q: dict) -> dict:
        state = q.get("s", "")
        try:
            verify(self.cfg.signing_key, state, "state", self.clock())
        except InvalidToken:
            return error_page(400, "Link expired", "This sign-in took too long. Start again from Claude.")
        return redirect(google.authorization_url(self.cfg.google_client_id, self.cfg.callback_url, state))

    # -- GET /callback

    def callback(self, q: dict, cookies: dict) -> dict:
        try:
            st = verify(self.cfg.signing_key, q.get("state", ""), "state", self.clock())
        except InvalidToken:
            return error_page(400, "Link expired", "This sign-in took too long. Start again from Claude.")
        bind = cookies.get(COOKIE, "")
        if not bind or not hmac.compare_digest(sha256_hex(bind), st["b"]):
            # A login completed in a different browser than the one that started it.
            return error_page(400, "Sign-in not started here", "Start again from Claude, in this browser.")
        clear = [self._login_cookie("", 0)]

        def to_client(params: dict) -> dict:
            if st["st"]:
                params["state"] = st["st"]
            return redirect(add_query(st["ru"], params), cookies=clear)

        if q.get("error"):
            return to_client({"error": "access_denied", "error_description": "Sign-in was cancelled."})
        if not q.get("code"):
            return error_page(400, "Sign-in failed", "Google did not return a sign-in code.")
        try:
            claims = self.google_exchange(
                self.cfg.google_client_id, self.cfg.google_client_secret, self.cfg.callback_url, q["code"]
            )
        except google.GoogleError:
            return error_page(502, "Sign-in failed", "Google sign-in did not complete. Try again.")
        author = author_for_email(self.cfg.users, claims["email"])
        if author is None:
            return page(
                403,
                "Not approved",
                f"<h1>Not approved</h1><p><code>{html.escape(claims['email'])}</code> isn't on Rodney's list. "
                "Ask the owner to add it.</p>",
                cookies=clear,
            )
        code = secrets.token_urlsafe(32)
        self.store.put(
            "code", code, {"author": author, "cid": st["cid"], "ru": st["ru"], "cc": st["cc"], "res": st["res"]}, CODE_TTL
        )
        return to_client({"code": code})

    # -- POST /token

    def token(self, form: dict) -> dict:
        grant = form.get("grant_type")
        client_id = form.get("client_id", "")
        try:
            self._client(client_id)
        except InvalidToken:
            return oauth_error("invalid_client", "Unknown client.")
        if form.get("resource") and not self._is_our_resource(form["resource"]):
            return oauth_error("invalid_target", "Unknown resource.")
        if grant == "authorization_code":
            return self._code_grant(form, client_id)
        if grant == "refresh_token":
            return self._refresh_grant(form, client_id)
        return oauth_error("unsupported_grant_type", "Use authorization_code or refresh_token.")

    def _code_grant(self, form: dict, client_id: str) -> dict:
        data = self.store.take("code", form.get("code", ""))  # consumed even if the checks below fail
        verifier = form.get("code_verifier", "")
        if (
            data is None
            or data["cid"] != client_id
            or form.get("redirect_uri") != data["ru"]
            or not _VERIFIER_RE.fullmatch(verifier)
            or not hmac.compare_digest(pkce_s256(verifier), data["cc"])
            or data["author"] not in self.cfg.users
        ):
            return oauth_error("invalid_grant", "The code is invalid, expired or already used.")
        return self._issue(data["author"], client_id, data["res"])

    def _refresh_grant(self, form: dict, client_id: str) -> dict:
        data = self.store.take("refresh", form.get("refresh_token", ""))  # rotated: single use
        if data is None or data["cid"] != client_id or data["author"] not in self.cfg.users:
            return oauth_error("invalid_grant", "The refresh token is invalid, expired or already used.")
        return self._issue(data["author"], client_id, data["res"])

    def _issue(self, author: str, client_id: str, resource: str) -> dict:
        now = self._now()
        access = sign(
            self.cfg.signing_key,
            "access",
            {"sub": author, "aud": resource, "cid": client_id, "iat": now, "exp": now + ACCESS_TTL},
        )
        refresh = secrets.token_urlsafe(32)
        self.store.put("refresh", refresh, {"author": author, "cid": client_id, "res": resource}, REFRESH_TTL)
        return json_response(
            200,
            {"access_token": access, "token_type": "Bearer", "expires_in": ACCESS_TTL, "refresh_token": refresh},
        )

    # -- bearer tokens on /mcp

    def authenticate(self, authorization: str | None) -> str:
        """Returns the author for a valid `Authorization: Bearer ...` header; raises Unauthorized."""
        if not authorization or authorization[:7].lower() != "bearer ":
            raise Unauthorized(None)
        try:
            claims = verify(self.cfg.signing_key, authorization[7:].strip(), "access", self.clock())
        except InvalidToken:
            raise Unauthorized("invalid_token") from None
        author = claims.get("sub")
        # `aud` pins the token to this server; checking the allowlist here means removing
        # someone from RODNEY_USERS cuts them off immediately, not at token expiry.
        if claims.get("aud") != self.cfg.resource or author not in self.cfg.users:
            raise Unauthorized("invalid_token")
        return author
