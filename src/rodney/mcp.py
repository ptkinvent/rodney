"""A minimal MCP server (stateless "streamable HTTP", JSON responses only).

MCP is JSON-RPC 2.0. A client POSTs one message; we answer with one message.
Only four methods matter for a tools-only server:

    initialize   - handshake: agree on a protocol version, advertise capabilities
    tools/list   - "what can you do?" -> names, descriptions, JSON Schemas
    tools/call   - run a tool -> content blocks (+ isError for tool-level failures)
    ping         - liveness

Messages without an `id` are notifications (e.g. notifications/initialized):
the server doesn't reply to them.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any, Callable

from .apps import Apps
from .validate import RodneyError

log = logging.getLogger(__name__)

SUPPORTED_VERSIONS = ["2025-06-18", "2025-03-26", "2024-11-05"]
SERVER_INFO = {"name": "rodney", "version": "0.1.0"}


# --- Tool definitions ---------------------------------------------------------


@dataclass
class Tool:
    name: str
    description: str
    properties: dict[str, dict]  # JSON Schema for each argument
    required: list[str]
    run: Callable[[Apps, str, dict], Any]  # (apps, signed-in author, arguments) -> JSON-serialisable result

    def to_mcp(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": {"type": "object", "properties": self.properties, "required": self.required},
        }


_NAME = {"type": "string", "description": "App slug: lowercase letters, numbers, dashes (e.g. team-standup)."}
_PATH = {"type": "string", "description": "File path inside the app, e.g. index.html or css/style.css."}

_DATA_API = (
    "FULL-STACK DATA API. A full-stack app calls these with same-origin fetch() from its own page, "
    "sending Content-Type: application/json on every request:\n"
    "  GET    /api/full-stack/<app>/data/<collection>                -> {collection, records}\n"
    "  POST   /api/full-stack/<app>/data/<collection>  {data:{...}}  -> 201 {collection, record, records}\n"
    "  PATCH  /api/full-stack/<app>/data/<collection>/<id>  {data:{...}} merges fields (add replace:true to "
    "overwrite), or {increment:{field:n}} adds atomically (use for counters, tallies and votes) "
    "-> {collection, record, records}\n"
    "  DELETE /api/full-stack/<app>/data/<collection>/<id>           -> 204\n"
    "A record is {id, data, createdAt, updatedAt}: the app's own fields live under record.data. Collections are "
    "created by the first write. Limits: 1000 records and 4 MB per collection, 20 KB per record, 10 collections "
    "per app. Data modelling: shared singleton state (one counter, one setting) is ONE record created once and "
    "then updated in place (increment for numbers); create a record per item only for real lists and logs. "
    "ANYONE WHO HAS THE APP'S LINK CAN READ AND CHANGE ITS DATA: tell the user this after deploying, and never "
    "put secrets or personal information in it."
)

TOOLS = [
    Tool(
        "create-app",
        "Deploy a NEW static HTML micro-app at <base>/<name>/. Provide `html` (single-file app) or "
        "`files` (multi-file; must include index.html). Text files only. Returns the live URL and the "
        "admin URL (a static page showing the app's name and author) — always show both to the user. "
        "Set fullStack: true ONLY when the user asks for a \"full stack\" app or clearly wants data that persists "
        "or is shared between visitors (data that only needs to stay in one person's browser should use "
        "localStorage instead). A full-stack app gets a data API. " + _DATA_API,
        {
            "name": _NAME,
            "description": {"type": "string"},
            "fullStack": {"type": "boolean", "description": "Give the app a shared, persistent data API. Default false."},
            "template": {"type": "string", "enum": ["crud"], "description": "Full-stack only. Only crud is supported."},
            "collectionName": {"type": "string", "description": "Full-stack only: the primary collection, e.g. tasks. Default records."},
            "primaryField": {"type": "string", "description": "Full-stack only: the main display field, e.g. title. Default value."},
            "html": {"type": "string", "description": "Complete HTML for index.html."},
            "files": {
                "type": "array",
                "description": "Multi-file apps: [{path, content}].",
                "items": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                    "required": ["path", "content"],
                },
            },
        },
        ["name"],
        lambda apps, who, a: apps.create_app(
            who, a.get("name"), a.get("description"), a.get("html"), a.get("files"),
            a.get("fullStack"), a.get("template"), a.get("collectionName"), a.get("primaryField"),
        ),
    ),
    Tool(
        "get-app",
        "Get an app's metadata (including appType: static or full-stack) and file listing, not contents; "
        "use read-file for that. For a full-stack app it also gives the data API path and per-collection usage.",
        {"name": _NAME},
        ["name"],
        lambda apps, who, a: apps.get_app(a.get("name")),
    ),
    Tool(
        "read-file",
        "Read one file's content from an app. Do this before modifying a file.",
        {"name": _NAME, "path": _PATH},
        ["name", "path"],
        lambda apps, who, a: apps.read_file(a.get("name"), a.get("path")),
    ),
    Tool(
        "create-or-update-file",
        "Create or overwrite one file in an existing app you authored. Other files are untouched. "
        "The `admin/` path is reserved.",
        {"name": _NAME, "path": _PATH, "content": {"type": "string"}},
        ["name", "path", "content"],
        lambda apps, who, a: apps.write_file(who, a.get("name"), a.get("path"), a.get("content")),
    ),
    Tool(
        "delete-app",
        "Permanently delete an app you authored. Cannot be undone.",
        {"name": _NAME},
        ["name"],
        lambda apps, who, a: apps.delete_app(who, a.get("name")),
    ),
    Tool(
        "list-my-apps",
        "List the apps you created, with their URLs and whether each is static or full-stack.",
        {},
        [],
        lambda apps, who, a: apps.list_apps_by(who),
    ),
    Tool(
        "update-app-settings",
        "Change settings of an app you authored. Pass fullStack: true to give an existing app a backend "
        "(then update its files to use the data API below), or false to switch the backend off; switching off "
        "keeps the stored data and switching on again brings it back. Can also change the default collectionName "
        "or primaryField. " + _DATA_API,
        {
            "name": _NAME,
            "fullStack": {"type": "boolean"},
            "collectionName": {"type": "string"},
            "primaryField": {"type": "string"},
        },
        ["name"],
        lambda apps, who, a: apps.set_settings(
            who, a.get("name"), a.get("fullStack"), a.get("collectionName"), a.get("primaryField")
        ),
    ),
    Tool(
        "read-app-data",
        "Look at the data stored by a full-stack app you authored. Without `collection`, lists its collections "
        "with record counts and sizes; with `collection`, returns up to 50 of its records.",
        {"name": _NAME, "collection": {"type": "string"}},
        ["name"],
        lambda apps, who, a: apps.read_data(who, a.get("name"), a.get("collection")),
    ),
    Tool(
        "clear-app-data",
        "PERMANENTLY erase data stored by a full-stack app you authored: one `collection`, or all of it if "
        "`collection` is omitted. Cannot be undone. Confirm with the user first.",
        {"name": _NAME, "collection": {"type": "string"}},
        ["name"],
        lambda apps, who, a: apps.clear_data(who, a.get("name"), a.get("collection")),
    ),
]
_TOOLS_BY_NAME = {t.name: t for t in TOOLS}


# --- JSON-RPC plumbing --------------------------------------------------------


def _ok(id_, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "result": result}


def _err(id_, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": message}}


def _text_result(text: str, is_error: bool = False) -> dict:
    result: dict = {"content": [{"type": "text", "text": text}]}
    if is_error:
        result["isError"] = True
    return result


def _call_tool(apps: Apps, author: str, params: dict) -> dict:
    tool = _TOOLS_BY_NAME.get(params.get("name"))
    if tool is None:
        raise KeyError(params.get("name"))
    args = params.get("arguments") or {}
    try:
        result = tool.run(apps, author, args)
    except RodneyError as e:
        # A *tool* failure (name taken, bad path...). Reported inside a normal
        # result with isError so the model can read it and react.
        return _text_result(str(e), is_error=True)
    except Exception:
        log.exception("tool %s crashed", tool.name)  # goes to CloudWatch
        return _text_result("Internal error.", is_error=True)
    text = result if isinstance(result, str) else json.dumps(result, indent=2)
    return _text_result(text)


def handle_message(apps: Apps, msg: Any, author: str) -> dict | None:
    """Handles one JSON-RPC message for a signed-in author. Returns the response, or None for notifications."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _err(None, -32600, "Invalid Request")
    if "id" not in msg:
        return None  # notification: nothing to reply to
    id_, method = msg["id"], msg.get("method")
    params = msg.get("params") or {}

    if method == "initialize":
        asked = params.get("protocolVersion")
        version = asked if asked in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        return _ok(id_, {"protocolVersion": version, "capabilities": {"tools": {}}, "serverInfo": SERVER_INFO})
    if method == "ping":
        return _ok(id_, {})
    if method == "tools/list":
        return _ok(id_, {"tools": [t.to_mcp() for t in TOOLS]})
    if method == "tools/call":
        try:
            return _ok(id_, _call_tool(apps, author, params))
        except KeyError as e:
            return _err(id_, -32602, f"Unknown tool: {e.args[0]}")
    return _err(id_, -32601, f"Method not found: {method}")
