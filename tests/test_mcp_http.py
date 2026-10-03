import base64
import json

import pytest

from rodney.apps import Apps
from rodney.handler import handle_http
from rodney.store import MemoryStore


@pytest.fixture
def store():
    return MemoryStore()


@pytest.fixture
def post(store):
    apps = Apps(store, {"me": "pw"}, "https://example.com")

    def _post(rpc):
        res = handle_http(apps, "POST", json.dumps(rpc) if not isinstance(rpc, str) else rpc)
        return res["statusCode"], (json.loads(res["body"]) if res["body"] else None)

    return _post


def call_tool(post, tool, **arguments):
    return post({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": tool, "arguments": arguments}})


def test_initialize_negotiates_version(post):
    init = lambda v: post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": v}})
    status, body = init("2025-03-26")
    assert status == 200 and body["result"]["protocolVersion"] == "2025-03-26"
    assert body["result"]["serverInfo"]["name"] == "rodney"
    assert "tools" in body["result"]["capabilities"]
    assert init("1999-01-01")[1]["result"]["protocolVersion"] == "2025-06-18"  # unknown -> our latest


def test_tools_list_has_schemas(post):
    _, body = post({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tools = {t["name"]: t for t in body["result"]["tools"]}
    assert set(tools) == {"create-app", "get-app", "read-file", "create-or-update-file", "delete-app", "list-apps"}
    for t in tools.values():
        assert t["inputSchema"]["type"] == "object" and "password" in t["inputSchema"]["required"]


def test_notifications_get_202_and_no_body(post):
    status, body = post({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert (status, body) == (202, None)


def test_batch(post):
    status, body = post(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "ping"},
        ]
    )
    assert status == 200 and [r["id"] for r in body] == [1, 2]


def test_create_app_and_tool_errors(post, store):
    _, body = call_tool(post, "create-app", password="pw", name="http-app", html="<p>hi</p>")
    assert "https://example.com/http-app/" in body["result"]["content"][0]["text"]
    assert "isError" not in body["result"]
    assert store.get("http-app/index.html") == "<p>hi</p>"

    _, body = call_tool(post, "create-app", password="wrong", name="x", html="x")
    assert body["result"]["isError"] is True
    assert body["result"]["content"][0]["text"] == "Invalid password."


def test_missing_arguments_become_tool_errors_not_crashes(post):
    _, body = post({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "create-app"}})
    assert body["result"]["isError"] is True


def test_protocol_errors(post):
    assert call_tool(post, "nope")[1]["error"]["code"] == -32602
    assert post({"jsonrpc": "2.0", "id": 1, "method": "bogus"})[1]["error"]["code"] == -32601
    status, body = post("{not json")
    assert status == 400 and body["error"]["code"] == -32700
    assert post({"hello": "world"})[1]["error"]["code"] == -32600


def test_non_post_rejected(store):
    apps = Apps(store, {"me": "pw"}, "")
    assert handle_http(apps, "GET", "")["statusCode"] == 405


def test_lambda_event_shape_and_base64(monkeypatch, store):
    import rodney.handler as h

    monkeypatch.setattr(h, "_apps", Apps(store, {"me": "pw"}, "https://example.com"))
    rpc = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"})
    event = {
        "requestContext": {"http": {"method": "POST"}},
        "body": base64.b64encode(rpc.encode()).decode(),
        "isBase64Encoded": True,
    }
    res = h.handler(event, None)
    assert res["statusCode"] == 200 and json.loads(res["body"])["result"] == {}
