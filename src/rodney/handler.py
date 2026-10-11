"""AWS Lambda entry point and router.

Lambda Function URLs hand us an HTTP request as a dict ("payload format v2") and expect a
dict back with statusCode/headers/body (and optionally `cookies`). One function serves the
whole MCP host: the MCP endpoint itself plus the OAuth endpoints Claude needs to sign in.
"""

import base64
import json
import logging
import os
import urllib.parse
from dataclasses import dataclass, field

import boto3

from .apps import Apps, _now
from .auth import parse_blocked
from .authstore import DynamoAuthStore
from .dataapi import DataApi
from .datastore import DynamoDataStore
from .mcp import handle_message
from .oauth import OAuthConfig, OAuthServer, Unauthorized, json_response
from .registry import DynamoRegistry
from .store import S3Store
from .web import WebApp

_JSON = {"content-type": "application/json"}

logging.getLogger().setLevel(logging.INFO)  # so audit lines reach CloudWatch


@dataclass
class Request:
    method: str
    path: str
    query: dict = field(default_factory=dict)
    headers: dict = field(default_factory=dict)  # lower-case names
    cookies: dict = field(default_factory=dict)
    body: str = ""
    surface: str = "mcp"  # which entrance this came through: "mcp" (Claude), "app" (the website) or "data" (apps' data API)


def handle_mcp_body(apps: Apps, author: str, body: str) -> dict:
    """Pure function from (author, JSON-RPC body) to a Lambda response. Easy to test."""
    try:
        payload = json.loads(body or "")
    except json.JSONDecodeError:
        error = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        return {"statusCode": 400, "headers": _JSON, "body": json.dumps(error)}

    if isinstance(payload, list):  # JSON-RPC batch (older protocol versions)
        responses = [r for r in (handle_message(apps, m, author) for m in payload) if r is not None]
        out = responses or None
    else:
        out = handle_message(apps, payload, author)

    if out is None:  # only notifications were sent
        return {"statusCode": 202, "headers": {}, "body": ""}
    return {"statusCode": 200, "headers": _JSON, "body": json.dumps(out)}


def _method_not_allowed(allow: str) -> dict:
    return {"statusCode": 405, "headers": {"allow": allow}, "body": "Method not allowed"}


def route_app(web: WebApp, req: Request) -> dict:
    """The website (app.<domain>): My apps, and browser sign-in."""
    path, method = req.path, req.method
    if path == "/":
        return web.home(req.cookies) if method == "GET" else _method_not_allowed("GET")
    if path == "/login":
        return web.login(req.cookies) if method == "GET" else _method_not_allowed("GET")
    if path == "/login/callback":
        return web.login_callback(req.query, req.cookies) if method == "GET" else _method_not_allowed("GET")
    if path == "/logout":
        return web.logout() if method == "POST" else _method_not_allowed("POST")
    return json_response(404, {"error": "not_found"})


def route(apps: Apps, oauth: OAuthServer, req: Request, web: WebApp | None = None, data: DataApi | None = None) -> dict:
    """Each entrance answers only its own routes: the website never serves OAuth, and so on."""
    if req.surface == "app":
        return route_app(web, req) if web else json_response(404, {"error": "not_found"})
    if req.surface == "data":
        if data is None:
            return json_response(404, {"error": "not_found"})
        return data.handle(req.method, req.path, req.headers, req.body)
    path, method = req.path, req.method

    if path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
        return json_response(200, oauth.protected_resource_metadata()) if method == "GET" else _method_not_allowed("GET")
    if path == "/.well-known/oauth-authorization-server":
        return json_response(200, oauth.authorization_server_metadata()) if method == "GET" else _method_not_allowed("GET")
    if path == "/register":
        return oauth.register(req.body) if method == "POST" else _method_not_allowed("POST")
    if path == "/authorize":
        return oauth.authorize(req.query) if method == "GET" else _method_not_allowed("GET")
    if path == "/authorize/continue":
        return oauth.continue_to_google(req.query) if method == "GET" else _method_not_allowed("GET")
    if path == "/callback":
        return oauth.callback(req.query, req.cookies) if method == "GET" else _method_not_allowed("GET")
    if path == "/token":
        if method != "POST":
            return _method_not_allowed("POST")
        form = {k: v[0] for k, v in urllib.parse.parse_qs(req.body).items()}
        return oauth.token(form)
    if path == "/mcp":
        if method != "POST":
            return _method_not_allowed("POST")
        try:
            author = oauth.authenticate(req.headers.get("authorization"))
        except Unauthorized as e:
            return json_response(
                401,
                {"error": e.error or "unauthorized"},
                {"www-authenticate": oauth.www_authenticate(e.error)},
            )
        return handle_mcp_body(apps, author, req.body)
    return json_response(404, {"error": "not_found"})


def _parse_cookies(event: dict) -> dict:
    pairs = list(event.get("cookies") or [])
    if event.get("headers", {}).get("cookie"):
        pairs += event["headers"]["cookie"].split(";")
    out = {}
    for pair in pairs:
        name, _, value = pair.strip().partition("=")
        if name:
            out[name] = value
    return out


def _surface(header: str | None) -> str:
    return header if header in ("app", "data") else "mcp"


def to_request(event: dict) -> Request:
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return Request(
        method=event["requestContext"]["http"]["method"],
        path=event["rawPath"],
        query=dict(urllib.parse.parse_qsl(event.get("rawQueryString") or "")),
        headers={k.lower(): v for k, v in (event.get("headers") or {}).items()},
        cookies=_parse_cookies(event),
        body=body,
        # Set by CloudFront on each distribution's origin request, so viewers can't choose it.
        surface=_surface((event.get("headers") or {}).get("x-rodney-surface")),
    )


def _build() -> tuple[Apps, OAuthServer, WebApp, DataApi]:
    prefix = os.environ.get("RODNEY_SSM_PREFIX", "/rodney")
    found = boto3.client("ssm").get_parameters(
        Names=[f"{prefix}/google-client-secret", f"{prefix}/signing-key"], WithDecryption=True
    )
    secrets = {p["Name"].rsplit("/", 1)[1]: p["Value"] for p in found["Parameters"]}
    if len(secrets) != 2:
        raise RuntimeError(f"Missing SSM parameters under {prefix}: have {sorted(secrets)}")
    config = OAuthConfig(
        issuer=os.environ["RODNEY_ISSUER"].rstrip("/"),
        google_client_id=os.environ["GOOGLE_CLIENT_ID"],
        google_client_secret=secrets["google-client-secret"],
        signing_key=secrets["signing-key"].encode("utf-8"),
        blocked=parse_blocked(os.environ.get("RODNEY_BLOCKED")),
    )
    data = DynamoDataStore(os.environ["DATA_TABLE"])
    apps = Apps(
        S3Store(os.environ["BUCKET_NAME"]),
        DynamoRegistry(os.environ["APPS_TABLE"]),
        data,
        os.environ.get("RODNEY_BASE_URL", "").rstrip("/"),
    )
    return (
        apps,
        OAuthServer(config, DynamoAuthStore(os.environ["AUTH_TABLE"])),
        WebApp(config, os.environ["RODNEY_APP_URL"], apps),
        DataApi(apps.registry, data, _now),
    )


_state: tuple[Apps, OAuthServer, WebApp, DataApi] | None = None


def handler(event, context):
    global _state
    if _state is None:  # built once per Lambda container, reused across requests
        _state = _build()
    apps, oauth, web, data = _state
    return route(apps, oauth, to_request(event), web, data)
