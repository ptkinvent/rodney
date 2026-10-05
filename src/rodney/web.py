"""The website's side of Rodney: sign in with Google in a browser, and see your apps.

Lives on its own host (app.<domain>), separate from both the apps (<domain>) and Claude's
endpoints (mcp.<domain>). The session cookie is scoped to this host only, so the untrusted
JavaScript inside apps never runs on the origin that holds a session.

    GET  /                 "My apps": your apps if signed in, a sign-in prompt if not
    GET  /login            start Google sign-in (straight to /  if already signed in)
    GET  /login/callback   Google returns here; a session cookie is set
    POST /logout           clear the session

A session is a signed cookie holding your anonymous author handle and your email, which is only
shown back to you. It can't be revoked server-side except by blocking the handle, which is
checked on every use.
"""

import hmac
import html
import logging
import secrets
import time
from datetime import datetime
from typing import Callable

from . import google
from .apps import Apps
from .auth import author_id
from .oauth import OAuthConfig, redirect
from .tokens import InvalidToken, sha256_hex, sign, verify

log = logging.getLogger(__name__)

SESSION_COOKIE = "rodney_session"
LOGIN_COOKIE = "rodney_web_login"
SESSION_TTL = 14 * 24 * 3600
LOGIN_TTL = 600

# This origin holds sessions, so be strict: nothing may frame it, run scripts in it, or sniff it.
SECURITY_HEADERS = {
    "content-type": "text/html; charset=utf-8",
    "cache-control": "no-store",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
    "content-security-policy": (
        "default-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; form-action 'self' https://accounts.google.com; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
}

_CSS = """
:root{--bg:#f3f4f8;--surface:#fff;--fg:#161a26;--muted:#5b6275;--line:#d9dce6;--accent:#3b3bd9;--accent-fg:#fff;--code:#e9ebf5;
--display:"Bricolage Grotesque","Avenir Next","Segoe UI",system-ui,sans-serif;--sans:"Instrument Sans",system-ui,-apple-system,"Segoe UI",sans-serif;color-scheme:light}
@media(prefers-color-scheme:dark){:root{--bg:#0f111a;--surface:#171a26;--fg:#e8eaf3;--muted:#9aa1b5;--line:#2a2f42;--accent:#9b9dff;--accent-fg:#0f111a;--code:#1d2132;color-scheme:dark}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font-family:var(--sans);font-size:1.0625rem;line-height:1.6}
.wrap{max-width:46rem;margin-inline:auto;padding-inline:1.25rem;padding-block:2.5rem 4rem}
header{display:flex;flex-wrap:wrap;align-items:center;justify-content:space-between;gap:.75rem 1rem;margin-bottom:2.5rem}
.brand{font-family:var(--display);font-weight:700;font-size:1.15rem;color:var(--fg);text-decoration:none}
.who{display:flex;align-items:center;gap:.75rem;font-size:.9rem;color:var(--muted)}
.who code{font-family:ui-monospace,Menlo,Consolas,monospace;background:var(--code);padding:.1em .45em;border-radius:4px;overflow-wrap:anywhere}
h1{font-family:var(--display);font-size:clamp(1.9rem,6vw,2.6rem);line-height:1.1;letter-spacing:-.03em;margin:0 0 .5rem}
.sub{color:var(--muted);margin:0 0 2rem}
.btn,button{font:inherit;font-weight:600;font-size:.95rem;cursor:pointer;border:0;border-radius:8px;padding:.7rem 1.1rem;text-decoration:none;display:inline-block;background:var(--accent);color:var(--accent-fg)}
button.quiet{background:transparent;color:var(--fg);border:1px solid var(--line);padding:.35rem .8rem;font-size:.85rem}
.apps{list-style:none;margin:0;padding:0;display:grid;gap:.75rem}
.app{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:1rem 1.2rem;display:flex;flex-wrap:wrap;justify-content:space-between;gap:.5rem 1.5rem}
.app-main{min-width:0;flex:1 1 16rem}.name{font-family:var(--display);font-weight:700;font-size:1.15rem;color:var(--accent);overflow-wrap:anywhere}
.desc{margin:.25rem 0 0;color:var(--muted);font-size:.95rem;overflow-wrap:anywhere}
.meta{display:flex;flex-direction:column;align-items:flex-start;gap:.15rem;font-size:.88rem;color:var(--muted)}.meta a{color:var(--muted)}
.empty{background:var(--surface);border:1px dashed var(--line);border-radius:10px;padding:1.5rem}
a{color:var(--accent)}footer{margin-top:3rem;font-size:.88rem;color:var(--muted)}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
"""


def render_page(status: int, title: str, body: str, account: str | None, site_url: str, cookies=None) -> dict:
    """`account` is whatever should be shown as who is signed in: an email, or a handle."""
    who = ""
    if account:
        who = (
            f'<div class="who"><span>Signed in as <code>{html.escape(account)}</code></span>'
            '<form method="post" action="/logout"><button class="quiet" type="submit">Sign out</button></form></div>'
        )
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)} · Rodney</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,700&family=Instrument+Sans:wght@400;600&display=swap">
<style>{_CSS}</style></head><body><main class="wrap">
<header><a class="brand" href="{html.escape(site_url)}/">Rodney</a>{who}</header>
{body}
<footer><a href="{html.escape(site_url)}/privacy/">Privacy</a> · <a href="{html.escape(site_url)}/terms/">Terms</a></footer>
</main></body></html>"""
    res = {"statusCode": status, "headers": dict(SECURITY_HEADERS), "body": page}
    if cookies:
        res["cookies"] = cookies
    return res


def _date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%b %-d, %Y")
    except ValueError:
        return iso


class WebApp:
    def __init__(
        self,
        config: OAuthConfig,
        app_url: str,
        apps: Apps,
        google_exchange: Callable = google.exchange_code,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.cfg = config
        self.app_url = app_url.rstrip("/")
        self.apps = apps
        self.google_exchange = google_exchange
        self.clock = clock
        self.site_url = apps.base_url.rstrip("/")

    @property
    def callback_url(self) -> str:
        return f"{self.app_url}/login/callback"

    def _cookie(self, name: str, value: str, max_age: int, path: str = "/") -> str:
        return f"{name}={value}; Max-Age={max_age}; Path={path}; HttpOnly; Secure; SameSite=Lax"

    def _page(self, status, title, body, account=None, cookies=None):
        return render_page(status, title, body, account, self.site_url, cookies)

    def _error(self, status: int, title: str, message: str, cookies=None) -> dict:
        body = f"<h1>{html.escape(title)}</h1><p class=\"sub\">{html.escape(message)}</p><p><a class=\"btn\" href=\"/login\">Try again</a></p>"
        return self._page(status, title, body, cookies=cookies)

    def session(self, cookies: dict) -> tuple[str, str | None] | None:
        """(author, email) for a valid, unexpired, unblocked session cookie; else None.

        Sessions created before the email was added to the cookie have no email (None).
        """
        try:
            claims = verify(self.cfg.signing_key, cookies.get(SESSION_COOKIE, ""), "session", self.clock())
        except InvalidToken:
            return None
        author = claims.get("sub")
        if not author or author in self.cfg.blocked:
            return None
        email = claims.get("email")
        return author, email if isinstance(email, str) and email else None

    def session_author(self, cookies: dict) -> str | None:
        found = self.session(cookies)
        return found[0] if found else None

    # -- GET /

    def home(self, cookies: dict) -> dict:
        found = self.session(cookies)
        if found is None:
            body = (
                "<h1>My apps</h1><p class=\"sub\">Sign in to see the apps you've made with Rodney.</p>"
                '<p><a class="btn" href="/login">Sign in with Google</a></p>'
            )
            return self._page(200, "My apps", body)
        author, email = found
        account = email or author  # an older session has no email yet; show the handle until next sign-in
        mine = self.apps.list_apps_by(author)
        if not mine:
            body = (
                "<h1>My apps</h1><div class=\"empty\"><p><strong>No apps yet.</strong></p>"
                "<p>Ask your assistant to build one, for example: <em>\"Ask Rodney to build me a habit tracker.\"</em></p>"
                f'<p><a href="{html.escape(self.site_url)}/">How to connect Rodney</a></p></div>'
            )
            return self._page(200, "My apps", body, account)
        rows = []
        for app in mine:
            desc = f'<p class="desc">{html.escape(app["description"])}</p>' if app.get("description") else ""
            rows.append(
                '<li class="app"><div class="app-main">'
                f'<a class="name" href="{html.escape(app["live"])}">{html.escape(app["name"])}</a>{desc}</div>'
                f'<div class="meta"><span>Updated {_date(app["updatedAt"])}</span>'
                f'<a href="{html.escape(app["admin"])}">Admin page</a></div></li>'
            )
        count = f"{len(mine)} app" + ("" if len(mine) == 1 else "s")
        body = f'<h1>My apps</h1><p class="sub">{count}</p><ul class="apps">{"".join(rows)}</ul>'
        return self._page(200, "My apps", body, account)

    # -- GET /login

    def login(self, cookies: dict) -> dict:
        if self.session_author(cookies):
            return redirect("/")
        bind = secrets.token_urlsafe(24)
        state = sign(
            self.cfg.signing_key, "web-state", {"b": sha256_hex(bind), "exp": int(self.clock()) + LOGIN_TTL}
        )
        url = google.authorization_url(self.cfg.google_client_id, self.callback_url, state)
        return redirect(url, cookies=[self._cookie(LOGIN_COOKIE, bind, LOGIN_TTL, path="/login")])

    # -- GET /login/callback

    def login_callback(self, query: dict, cookies: dict) -> dict:
        try:
            state = verify(self.cfg.signing_key, query.get("state", ""), "web-state", self.clock())
        except InvalidToken:
            return self._error(400, "Link expired", "That sign-in took too long. Try again.")
        bind = cookies.get(LOGIN_COOKIE, "")
        if not bind or not hmac.compare_digest(sha256_hex(bind), state["b"]):
            return self._error(400, "Sign-in not started here", "Start again from this site, in this browser.")
        clear = [self._cookie(LOGIN_COOKIE, "", 0, path="/login")]
        if query.get("error"):
            return self._error(400, "Sign-in cancelled", "You cancelled the Google sign-in.", cookies=clear)
        if not query.get("code"):
            return self._error(400, "Sign-in failed", "Google did not return a sign-in code.", cookies=clear)
        try:
            claims = self.google_exchange(
                self.cfg.google_client_id, self.cfg.google_client_secret, self.callback_url, query["code"]
            )
        except google.GoogleError:
            return self._error(502, "Sign-in failed", "Google sign-in did not complete. Try again.", cookies=clear)
        author = author_id(claims["sub"])
        if author in self.cfg.blocked:
            log.warning("audit web-signin-blocked author=%s email=%s", author, claims["email"])
            return self._page(403, "Blocked", "<h1>Blocked</h1><p class=\"sub\">This account can't use Rodney.</p>", cookies=clear)
        log.info("audit web-signin author=%s email=%s", author, claims["email"])
        now = int(self.clock())
        token = sign(
            self.cfg.signing_key,
            "session",
            {"sub": author, "email": claims["email"], "iat": now, "exp": now + SESSION_TTL},
        )
        return redirect("/", cookies=[*clear, self._cookie(SESSION_COOKIE, token, SESSION_TTL)])

    # -- POST /logout

    def logout(self) -> dict:
        return redirect("/", cookies=[self._cookie(SESSION_COOKIE, "", 0)])
