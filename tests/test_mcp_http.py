import base64
import json

import pytest

from rodney.apps import Apps
from rodney.datastore import MemoryDataStore
from rodney.handler import handle_mcp_body, to_request
from rodney.registry import MemoryRegistry
from rodney.store import MemoryStore


@pytest.fixture
def store():
    return MemoryStore()


@pytest.fixture
def registry():
    return MemoryRegistry()


@pytest.fixture
def post(store, registry):
    apps = Apps(store, registry, MemoryDataStore(), "https://example.com")

    def _post(rpc, author="me"):
        res = handle_mcp_body(apps, author, json.dumps(rpc) if not isinstance(rpc, str) else rpc)
        return res["statusCode"], (json.loads(res["body"]) if res["body"] else None)

    return _post


def call_tool(post, tool, author="me", **arguments):
    return post({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}, author)


def test_initialize_negotiates_version(post):
    init = lambda v: post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": v}})
    status, body = init("2025-03-26")
    assert status == 200 and body["result"]["protocolVersion"] == "2025-03-26"
    assert body["result"]["serverInfo"]["name"] == "rodney"
    assert "tools" in body["result"]["capabilities"]
    assert init("1999-01-01")[1]["result"]["protocolVersion"] == "2025-06-18"  # unknown -> our latest


def test_tools_list_has_schemas_and_no_password(post):
    _, body = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = {t["name"]: t for t in body["result"]["tools"]}
    assert set(tools) == {"create-app", "get-app", "read-file", "create-or-update-file", "delete-app", "list-my-apps",
                          "update-app-settings", "read-app-data", "clear-app-data"}
    for t in tools.values():
        assert t["inputSchema"]["type"] == "object" and "password" not in t["inputSchema"]["properties"]


def test_notifications_get_202_and_no_body(post):
    assert post({"jsonrpc": "2.0", "method": "notifications/initialized"}) == (202, None)


def test_batch(post):
    status, body = post([{"jsonrpc": "2.0", "id": 1, "method": "ping"}, {"jsonrpc": "2.0", "method": "notifications/initialized"}, {"jsonrpc": "2.0", "id": 2, "method": "ping"}])
    assert status == 200 and [r["id"] for r in body] == [1, 2]


def test_author_comes_from_the_token_not_the_arguments(post, registry):
    _, body = call_tool(post, "create-app", author="friend", name="http-app", html="<p>hi</p>")
    assert "https://example.com/http-app/" in body["result"]["content"][0]["text"]
    assert registry.get("http-app")["author"] == "friend"
    # a smuggled-in author argument changes nothing
    call_tool(post, "create-app", author="me", name="other", html="x", author_name="friend", password="x")
    assert registry.get("other")["author"] == "me"
    _, denied = call_tool(post, "delete-app", author="me", name="http-app")
    assert denied["result"]["isError"] is True


def test_missing_arguments_become_tool_errors_not_crashes(post):
    _, body = post({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "create-app"}})
    assert body["result"]["isError"] is True


def test_protocol_errors(post):
    assert call_tool(post, "nope")[1]["error"]["code"] == -32602
    assert post({"jsonrpc": "2.0", "id": 1, "method": "bogus"})[1]["error"]["code"] == -32601
    status, body = post("{not json")
    assert status == 400 and body["error"]["code"] == -32700
    assert post({"hello": "world"})[1]["error"]["code"] == -32600


def test_lambda_event_parsing():
    rpc = json.dumps({"a": 1})
    event = {
        "requestContext": {"http": {"method": "POST"}},
        "rawPath": "/callback",
        "rawQueryString": "code=abc&state=x%20y",
        "headers": {"Authorization": "Bearer t", "cookie": "a=1; b=2"},
        "cookies": ["rodney_login=zzz"],
        "body": base64.b64encode(rpc.encode()).decode(),
        "isBase64Encoded": True,
    }
    req = to_request(event)
    assert (req.method, req.path, req.body) == ("POST", "/callback", rpc)
    assert req.query == {"code": "abc", "state": "x y"}
    assert req.headers["authorization"] == "Bearer t"
    assert req.cookies == {"rodney_login": "zzz", "a": "1", "b": "2"}
