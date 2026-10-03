"""AWS Lambda entry point.

Lambda Function URLs hand us an HTTP request as a dict ("payload format v2") and
expect a dict back with statusCode/headers/body.
"""

import base64
import json
import os

from .apps import Apps
from .auth import parse_users
from .mcp import handle_message
from .store import S3Store

_JSON = {"content-type": "application/json"}


def handle_http(apps: Apps, method: str, body: str) -> dict:
    """Pure function from (HTTP method, body) to a Lambda response. Easy to test."""
    if method != "POST":
        return {"statusCode": 405, "headers": {"allow": "POST"}, "body": "POST only"}
    try:
        payload = json.loads(body or "")
    except json.JSONDecodeError:
        error = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
        return {"statusCode": 400, "headers": _JSON, "body": json.dumps(error)}

    if isinstance(payload, list):  # JSON-RPC batch (older protocol versions)
        responses = [r for r in (handle_message(apps, m) for m in payload) if r is not None]
        out = responses or None
    else:
        out = handle_message(apps, payload)

    if out is None:  # only notifications were sent
        return {"statusCode": 202, "headers": {}, "body": ""}
    return {"statusCode": 200, "headers": _JSON, "body": json.dumps(out)}


_apps: Apps | None = None


def handler(event, context):
    global _apps
    if _apps is None:  # built once per Lambda container, reused across requests
        _apps = Apps(
            S3Store(os.environ["BUCKET_NAME"]),
            parse_users(os.environ.get("RODNEY_USERS")),
            os.environ.get("RODNEY_BASE_URL", "").rstrip("/"),
        )
    body = event.get("body") or ""
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    return handle_http(_apps, event["requestContext"]["http"]["method"], body)
