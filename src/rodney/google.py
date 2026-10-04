"""Google as the identity provider: send the user there, then read who they are."""

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request

from .tokens import b64url_decode

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
ISSUERS = {"https://accounts.google.com", "accounts.google.com"}

log = logging.getLogger(__name__)


class GoogleError(Exception):
    pass


def _describe(e: Exception) -> str:
    """What went wrong, for the private log. Google's error codes contain no secrets."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            body = json.loads(e.read())
            return f"HTTP {e.code} {body.get('error')}: {body.get('error_description', '')}"
        except Exception:
            return f"HTTP {e.code}"
    return type(e).__name__


def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    query = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email",
            "state": state,
            "prompt": "select_account",
        }
    )
    return f"{AUTH_URL}?{query}"


def _post_form(url: str, data: dict) -> dict:
    request = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(), method="POST")
    with urllib.request.urlopen(request, timeout=8) as res:
        return json.loads(res.read())


def exchange_code(client_id: str, client_secret: str, redirect_uri: str, code: str, post=_post_form) -> dict:
    """Trades Google's authorization code for the signed-in user's verified claims.

    The ID token comes straight from Google's token endpoint over TLS, in reply to a request
    that carried our client secret, so its contents can be trusted without re-checking the
    signature (OpenID Connect Core 3.1.3.7). We still check issuer, audience and expiry.
    """
    try:
        res = post(
            TOKEN_URL,
            {
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
        )
        claims = json.loads(b64url_decode(res["id_token"].split(".")[1]))
    except Exception as e:
        log.warning("google token exchange failed: %s", _describe(e))
        raise GoogleError("Google sign-in failed") from e
    if claims.get("iss") not in ISSUERS or claims.get("aud") != client_id:
        raise GoogleError("Google returned a token meant for someone else")
    if claims.get("exp", 0) <= time.time():
        raise GoogleError("Google token expired")
    if not claims.get("sub"):
        raise GoogleError("Google returned no account id")
    if not claims.get("email") or claims.get("email_verified") is not True:
        raise GoogleError("Google account has no verified email")
    return claims
