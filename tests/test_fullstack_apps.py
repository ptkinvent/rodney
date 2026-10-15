"""Full-stack apps at the app level: creating them, switching the backend, the owner's data tools,
the MCP tool definitions, and routing between the three entrances."""

import json

import pytest

from rodney.apps import PUBLIC_DATA_NOTICE, READ_DATA_MAX_RECORDS, Apps
from rodney.dataapi import DataApi
from rodney.datastore import MemoryDataStore
from rodney.handler import Request, route, to_request
from rodney.mcp import TOOLS, handle_message
from rodney.oauth import OAuthConfig, OAuthServer
from rodney.authstore import MemoryAuthStore
from rodney.registry import MemoryRegistry
from rodney.store import MemoryStore
from rodney.validate import RodneyError

NOW = "2026-03-03T12:00:00.000Z"


@pytest.fixture
def parts():
    store, registry, data = MemoryStore(), MemoryRegistry(), MemoryDataStore()
    return Apps(store, registry, data, "https://example.com"), registry, data, store


@pytest.fixture
def apps(parts):
    return parts[0]


# ---- creating --------------------------------------------------------------------------------


def test_static_apps_are_unchanged_and_have_no_backend(apps, registry=None):
    r = apps.create_app("u-me", "plain", html="x")
    assert "appType" not in r["meta"] and "dataApi" not in r and "notice" not in r
    assert apps.get_app("plain").keys() == {"meta", "files", "live", "admin"}


def test_full_stack_app_gets_defaults_a_data_api_path_and_the_public_warning(apps):
    r = apps.create_app("u-me", "todo", html="x", full_stack=True)
    assert (r["meta"]["appType"], r["meta"]["template"], r["meta"]["collectionName"], r["meta"]["primaryField"]) == (
        "full-stack", "crud", "records", "value")
    assert r["dataApi"] == "/api/full-stack/todo/data/records"
    assert r["notice"] == PUBLIC_DATA_NOTICE and "Anyone who has the link" in r["notice"]


def test_custom_collection_and_primary_field(apps):
    r = apps.create_app("u-me", "todo", html="x", full_stack=True, template="crud", collection_name="tasks", primary_field="title")
    assert r["dataApi"].endswith("/data/tasks") and r["meta"]["primaryField"] == "title"


def test_extras_are_ignored_for_static_apps_like_the_original(apps):
    r = apps.create_app("u-me", "plain", html="x", full_stack=False, template="crud", collection_name="tasks", primary_field="t")
    assert set(r["meta"]) == {"name", "author", "createdAt", "updatedAt"}
    assert set(apps.create_app("u-me", "plain2", html="x", template="nonsense")["meta"]) == set(r["meta"])


@pytest.mark.parametrize("kwargs", [
    {"full_stack": "yes"}, {"full_stack": 1}, {"full_stack": True, "template": "spa"},
    {"full_stack": True, "collection_name": "Tasks"}, {"full_stack": True, "collection_name": "_x"},
    {"full_stack": True, "primary_field": "has space"}, {"full_stack": True, "primary_field": "1x"},
])
def test_bad_full_stack_options_are_refused_without_claiming_the_name(apps, parts, kwargs):
    with pytest.raises(RodneyError):
        apps.create_app("u-me", "todo", html="x", **kwargs)
    assert parts[1].get("todo") is None


def test_get_app_reports_collections_for_full_stack_apps(apps, parts):
    apps.create_app("u-me", "todo", html="x", full_stack=True, collection_name="tasks")
    parts[2].create("todo", "tasks", {"a": 1}, NOW)
    info = apps.get_app("todo")
    assert info["meta"]["appType"] == "full-stack"
    assert info["dataApi"] == "/api/full-stack/todo/data/tasks" and info["collections"] == [{"name": "tasks", "count": 1, "bytes": 7}]


# ---- switching the backend on and off ---------------------------------------------------------


def test_owner_can_add_a_backend_to_an_existing_app(apps):
    apps.create_app("u-me", "plain", html="x")
    r = apps.set_settings("u-me", "plain", full_stack=True)
    assert r["meta"]["appType"] == "full-stack" and r["meta"]["collectionName"] == "records" and r["dataApi"].endswith("/records")
    assert r["notice"] == PUBLIC_DATA_NOTICE and r["meta"]["author"] == "u-me"


def test_turning_the_backend_off_keeps_the_data_and_turning_it_on_restores_it(apps, parts):
    apps.create_app("u-me", "todo", html="x", full_stack=True, collection_name="tasks")
    parts[2].create("todo", "tasks", {"keep": "me"}, NOW)
    off = apps.set_settings("u-me", "todo", full_stack=False)
    assert off["meta"]["appType"] == "static" and "dataApi" not in off
    assert parts[2].collections("todo")[0]["count"] == 1  # data still there
    on = apps.set_settings("u-me", "todo", full_stack=True)
    assert on["meta"]["collectionName"] == "tasks" and parts[2].list("todo", "tasks")[0]["data"] == {"keep": "me"}


def test_settings_can_change_defaults_and_have_rules(apps):
    apps.create_app("u-me", "todo", html="x", full_stack=True)
    r = apps.set_settings("u-me", "todo", collection_name="notes", primary_field="title")
    assert (r["meta"]["collectionName"], r["meta"]["primaryField"], r["meta"]["appType"]) == ("notes", "title", "full-stack")
    for kwargs in ({}, {"full_stack": "no"}, {"collection_name": "Bad Name"}, {"primary_field": "a b"}):
        with pytest.raises(RodneyError):
            apps.set_settings("u-me", "todo", **kwargs)
    with pytest.raises(RodneyError, match="only they can"):
        apps.set_settings("u-other", "todo", full_stack=False)
    with pytest.raises(RodneyError, match="not found"):
        apps.set_settings("u-me", "ghost", full_stack=True)
    assert apps.get_app("todo")["meta"]["appType"] == "full-stack"


# ---- the owner's data tools -------------------------------------------------------------------


@pytest.fixture
def loaded(parts):
    apps, _, data, _ = parts
    apps.create_app("u-me", "todo", html="x", full_stack=True, collection_name="tasks")
    for i in range(60):
        data.create("todo", "tasks", {"n": i}, NOW)
    data.create("todo", "notes", {"n": 1}, NOW)
    return apps


def test_read_data_summarises_then_shows_a_capped_slice(loaded):
    assert {c["name"]: c["count"] for c in loaded.read_data("u-me", "todo")["collections"]} == {"tasks": 60, "notes": 1}
    out = loaded.read_data("u-me", "todo", "tasks")
    assert out["total"] == 60 and out["shown"] == READ_DATA_MAX_RECORDS == len(out["records"])
    assert loaded.read_data("u-me", "todo", "nothing-here") == {"collection": "nothing-here", "total": 0, "shown": 0, "records": []}


def test_read_data_also_caps_by_size(parts):
    apps, _, data, _ = parts
    apps.create_app("u-me", "big", html="x", full_stack=True)
    for _ in range(40):
        data.create("big", "records", {"t": "x" * 15_000}, NOW)
    out = apps.read_data("u-me", "big", "records")
    assert out["total"] == 40 and out["shown"] < 10


def test_data_tools_are_owner_only(loaded):
    with pytest.raises(RodneyError, match="only they can"):
        loaded.read_data("u-other", "todo")
    assert loaded.read_data("u-me", "todo", "tasks")["total"] == 60


def test_deleting_an_app_deletes_its_data_and_only_its_data(parts):
    apps, registry, data, store = parts
    apps.create_app("u-me", "todo", html="x", full_stack=True)
    apps.create_app("u-me", "other", html="x", full_stack=True)
    data.create("todo", "records", {"a": 1}, NOW)
    data.create("other", "records", {"b": 2}, NOW)
    assert apps.delete_app("u-me", "todo")["records"] == 1
    assert data.collections("todo") == [] and registry.get("todo") is None
    assert data.list("other", "records")[0]["data"] == {"b": 2}
    apps.create_app("u-me", "todo", html="x", full_stack=True)  # the same name later starts empty
    assert data.list("todo", "records") == []


# ---- MCP tools ---------------------------------------------------------------------------------


def call(apps, tool, author="u-me", **arguments):
    res = handle_message(apps, {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool, "arguments": arguments}}, author)
    out = res["result"]
    return out.get("isError", False), out["content"][0]["text"]


def test_tool_definitions_describe_the_data_api_and_its_public_nature():
    tools = {t.name: t for t in TOOLS}
    assert "list-apps" not in tools and "list-my-apps" in tools
    create = tools["create-app"]
    assert create.properties["fullStack"]["type"] == "boolean" and create.required == ["name"]
    for text in (create.description, tools["update-app-settings"].description):
        assert "/api/full-stack/<app>/data/<collection>" in text and "increment" in text and "replace:true" in text
        assert "ANYONE WHO HAS THE APP'S LINK CAN READ AND CHANGE ITS DATA" in text
        assert "1000 records and 4 MB" in text and "20 KB" in text
    assert "user asks for a \"full stack\" app" in create.description
    assert "clear-app-data" not in tools  # erasing data by accident is too easy; delete-app is the only way


def test_creating_a_full_stack_app_through_the_tool(apps):
    err, text = call(apps, "create-app", name="todo", html="x", fullStack=True, collectionName="tasks", primaryField="title", template="crud")
    out = json.loads(text)
    assert not err and out["dataApi"] == "/api/full-stack/todo/data/tasks" and "Anyone who has the link" in out["notice"]
    assert out["meta"]["appType"] == "full-stack"
    err, text = call(apps, "create-app", name="bad", html="x", fullStack="yes")
    assert err and "true or false" in text


def test_the_other_tools_end_to_end(apps):
    call(apps, "create-app", name="todo", html="x")
    err, text = call(apps, "update-app-settings", name="todo", fullStack=True)
    assert not err and json.loads(text)["meta"]["appType"] == "full-stack"
    assert call(apps, "update-app-settings", name="todo")[0] is True  # nothing to change
    assert json.loads(call(apps, "read-app-data", name="todo")[1]) == {"collections": []}
    err, text = call(apps, "update-app-settings", author="u-intruder", name="todo", fullStack=False)
    assert err and "only they can" in text
    call(apps, "create-app", author="u-friend", name="friends", html="x")
    assert [a["name"] for a in json.loads(call(apps, "list-my-apps")[1])] == ["todo"]
    assert [a["name"] for a in json.loads(call(apps, "list-my-apps", author="u-friend")[1])] == ["friends"]


# ---- routing between the three entrances -------------------------------------------------------


@pytest.fixture
def system(parts):
    apps, registry, data, _ = parts
    config = OAuthConfig("https://mcp.example.com", "gid", "gsecret", b"k" * 32)
    oauth = OAuthServer(config, MemoryAuthStore())
    api = DataApi(registry, data, lambda: NOW)
    apps.create_app("u-me", "todo", html="x", full_stack=True, collection_name="tasks")
    return lambda surface, method, path, headers=None, body="": route(apps, oauth, Request(method, path, {}, headers or {}, {}, body, surface), None, api)


def test_the_data_api_answers_only_on_its_own_entrance(system):
    assert system("data", "GET", "/api/full-stack/todo/data/tasks")["statusCode"] == 200
    assert system("mcp", "GET", "/api/full-stack/todo/data/tasks")["statusCode"] == 404  # not on the MCP host
    assert system("app", "GET", "/api/full-stack/todo/data/tasks")["statusCode"] == 404  # nor on the website
    for path in ("/mcp", "/authorize", "/token", "/register", "/callback", "/", "/login", "/.well-known/oauth-authorization-server"):
        assert system("data", "GET", path)["statusCode"] == 404 and system("data", "POST", path)["statusCode"] == 404


def test_the_data_entrance_serves_no_data_without_a_data_api(parts):
    apps, registry, data, _ = parts
    oauth = OAuthServer(OAuthConfig("https://mcp.example.com", "gid", "gsecret", b"k" * 32), MemoryAuthStore())
    res = route(apps, oauth, Request("GET", "/api/full-stack/todo/data/tasks", {}, {}, {}, "", "data"))
    assert res["statusCode"] == 404


def test_entrance_comes_only_from_the_header_cloudfront_sets():
    event = lambda h: {"requestContext": {"http": {"method": "GET"}}, "rawPath": "/", "headers": h}
    assert [to_request(event({"x-rodney-surface": s})).surface for s in ("data", "app", "mcp", "other", "")] == ["data", "app", "mcp", "mcp", "mcp"]
    assert to_request(event({})).surface == "mcp"


def test_a_visitor_cannot_use_the_data_api_to_reach_the_wrong_app_or_the_auth_table(system):
    for path in ("/api/full-stack/todo/data/__collections", "/api/full-stack/todo%23x/data/tasks", "/api/full-stack/_meta/data/tasks"):
        assert system("data", "GET", path)["statusCode"] in (400, 404)


def test_direct_upload_through_the_tools(apps):
    store = apps.store
    err, text = call(apps, "get-upload-url", name="up", path="index.html", size=2)
    assert not err
    r = json.loads(text)
    assert store.upload(r["uploadUrl"], b"hi", r["contentType"])
    err, text = call(apps, "finalize-upload", name="up", paths=["index.html"], description="uploaded")
    out = json.loads(text)
    assert not err and out["live"].endswith("/up") and out["meta"]["description"] == "uploaded"
    err, text = call(apps, "update-app-settings", name="up", fullStack=True)  # a backend can be added afterwards
    assert not err and json.loads(text)["dataApi"] == "/api/full-stack/up/data/records"
    err, text = call(apps, "get-upload-url", name="up", path="index.html", size=2)
    assert err and "already exists" in text
