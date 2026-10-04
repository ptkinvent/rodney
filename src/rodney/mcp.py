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

TOOLS = [
    Tool(
        "create-app",
        "Deploy a NEW static HTML micro-app at <base>/<name>/. Provide `html` (single-file app) or "
        "`files` (multi-file; must include index.html). Text files only. Returns the live URL and the "
        "admin URL (a static page showing the app's name and author) — always show both to the user.",
        {
            "name": _NAME,
            "description": {"type": "string"},
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
        lambda apps, who, a: apps.create_app(who, a.get("name"), a.get("description"), a.get("html"), a.get("files")),
    ),
    Tool(
        "get-app",
        "Get an app's metadata and file listing (not contents; use read-file for that).",
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
        "list-apps",
        "List all apps with their authors and URLs.",
        {},
        [],
        lambda apps, who, a: apps.list_apps(),
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
