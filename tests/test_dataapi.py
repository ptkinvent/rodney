"""The HTTP data API that full-stack apps call. Run against both data stores."""

import json

import pytest
from fake_dynamo_data import FakeDataDynamo

import rodney.datastore as ds
from rodney.apps import Apps
from rodney.dataapi import DataApi
from rodney.datastore import DynamoDataStore, MemoryDataStore
from rodney.registry import MemoryRegistry
from rodney.store import MemoryStore

NOW = "2026-03-03T12:00:00.000Z"
BASE = "/api/full-stack/todo/data/tasks"


class Env:
    def __init__(self, data):
        self.registry = MemoryRegistry()
        self.apps = Apps(MemoryStore(), self.registry, data, "https://example.com")
        self.api = DataApi(self.registry, data, lambda: NOW)
        self.apps.create_app("u-me", "todo", html="x", full_stack=True, collection_name="tasks")

    def call(self, method, path=BASE, body=None, headers=None, raw=None):
        payload = raw if raw is not None else (json.dumps(body) if body is not None else "")
        res = self.api.handle(method, path, headers or {}, payload)
        return res, (json.loads(res["body"]) if res["body"] else None)

    def add(self, **data):
        return self.call("POST", body={"data": data})[1]["record"]


@pytest.fixture(params=["memory", "dynamo"])
def env(request):
    return Env(MemoryDataStore() if request.param == "memory" else DynamoDataStore("t", FakeDataDynamo()))


# ---- the original Spin contract --------------------------------------------------------------


def test_list_create_update_delete_shapes_match_the_original_contract(env):
    res, body = env.call("GET")
    assert res["statusCode"] == 200 and body == {"collection": "tasks", "records": []}

    res, body = env.call("POST", body={"data": {"title": "buy milk", "done": False}})
    assert res["statusCode"] == 201 and set(body) == {"collection", "record", "records"}
    rec = body["record"]
    assert set(rec) == {"id", "data", "createdAt", "updatedAt"} and rec["data"] == {"title": "buy milk", "done": False}
    assert rec["createdAt"] == rec["updatedAt"] == NOW and body["records"] == [rec]

    res, body = env.call("PATCH", f"{BASE}/{rec['id']}", {"data": {"done": True}})
    assert res["statusCode"] == 200 and set(body) == {"collection", "record", "records"}
    assert body["record"]["data"] == {"title": "buy milk", "done": True}  # shallow merge

    res, body = env.call("PATCH", f"{BASE}/{rec['id']}", {"data": {"only": 1}, "replace": True})
    assert body["record"]["data"] == {"only": 1}  # replace overwrites

    res, body = env.call("DELETE", f"{BASE}/{rec['id']}")
    assert res["statusCode"] == 204 and body is None and res["body"] == ""
    assert env.call("GET")[1]["records"] == []


def test_records_come_back_in_creation_order(env):
    ids = [env.add(n=i)["id"] for i in range(7)]
    assert [r["id"] for r in env.call("GET")[1]["records"]] == ids


def test_increment_is_atomic_and_creates_missing_fields(env):
    rid = env.add(title="poll")["id"]
    for _ in range(10):
        res, body = env.call("PATCH", f"{BASE}/{rid}", {"increment": {"yes": 1}})
    assert res["statusCode"] == 200 and body["record"]["data"] == {"title": "poll", "yes": 10}
    body = env.call("PATCH", f"{BASE}/{rid}", {"increment": {"yes": -4, "no": 2.5}})[1]
    assert body["record"]["data"] == {"title": "poll", "yes": 6, "no": 2.5} and body["records"][0] == body["record"]


def test_increment_errors(env):
    rid = env.add(name="x", flag=True)["id"]
    p = f"{BASE}/{rid}"
    cases = [
        ({"increment": {"name": 1}}, 400, "not_a_number"),
        ({"increment": {"flag": 1}}, 400, "not_a_number"),
        ({"increment": {"n": "5"}}, 400, "invalid_increment"),
        ({"increment": {"n": True}}, 400, "invalid_increment"),
        ({"increment": {"n": float("inf")}}, 400, "invalid_json"),  # Infinity isn't valid JSON at all
        ({"increment": {"n": 10**13}}, 400, "invalid_increment"),
        ({"increment": {"bad name": 1}}, 400, "invalid_increment"),
        ({"increment": {"a.b": 1}}, 400, "invalid_increment"),
        ({"increment": {}}, 400, "invalid_increment"),
        ({"increment": [1]}, 400, "invalid_increment"),
        ({"increment": {f"f{i}": 1 for i in range(11)}}, 400, "invalid_increment"),
        ({"increment": {"n": 1}, "data": {"x": 1}}, 400, "invalid_request"),
        ({"increment": {"n": 1}, "replace": True}, 400, "invalid_request"),
        ({}, 400, "invalid_request"),
        ({"data": {"x": 1}, "replace": "yes"}, 400, "invalid_request"),
    ]
    for body, status, code in cases:
        res, out = env.call("PATCH", p, raw=json.dumps(body, allow_nan=True))
        assert (res["statusCode"], out["error"]) == (status, code), body
    assert env.call("GET")[1]["records"][0]["data"] == {"name": "x", "flag": True}  # nothing was changed


# ---- what apps may send ----------------------------------------------------------------------


@pytest.mark.parametrize("raw,status,code", [
    ("not json", 400, "invalid_json"),
    ("", 400, "invalid_json"),
    ("[1,2]", 400, "invalid_json"),
    ('"str"', 400, "invalid_json"),
    ('{"data": NaN}', 400, "invalid_json"),
    ('{"data": {"x": Infinity}}', 400, "invalid_json"),
    ('{"data": {"x": 1e999}}', 400, "invalid_data"),
    ('{"data": {"x": 12345678901234567890}}', 400, "invalid_data"),
    ('{"data": {"x": 1e-20}}', 400, "invalid_data"),
    ('{"data": [1]}', 400, "invalid_data"),
    ('{"data": "text"}', 400, "invalid_data"),
    ('{"data": null}', 400, "invalid_data"),
    ('{"other": {}}', 400, "invalid_data"),
    ('{"data": {"": 1}}', 400, "invalid_data"),
])
def test_bad_create_requests_are_refused_and_store_nothing(env, raw, status, code):
    res, body = env.call("POST", raw=raw)
    assert (res["statusCode"], body["error"]) == (status, code)
    assert env.call("GET")[1]["records"] == []


def test_unicode_empty_objects_and_nesting_up_to_the_limit_are_fine(env):
    nested = cur = {}
    for _ in range(7):
        cur["n"] = {}
        cur = cur["n"]
    for data in ({}, {"emoji": "héllo ✓ 🚀", "list": [1, [2, [3]]], "none": None, "ok": True}, nested):
        assert env.call("POST", body={"data": data})[0]["statusCode"] == 201
    deep = cur = {}
    for _ in range(12):
        cur["n"] = {}
        cur = cur["n"]
    res, body = env.call("POST", body={"data": deep})
    assert res["statusCode"] == 400 and body["error"] == "invalid_data"


def test_size_limits(env):
    big = {"text": "x" * ds.MAX_RECORD_BYTES}
    res, body = env.call("POST", body={"data": big})
    assert res["statusCode"] == 413 and body["error"] == "record_too_large"
    assert env.call("POST", body={"data": {"text": "x" * 1000}})[0]["statusCode"] == 201
    res, body = env.call("POST", raw=json.dumps({"data": {"t": "x"}, "pad": "y" * 70_000}))
    assert res["statusCode"] == 413 and body["error"] == "body_too_large"
    rid = env.call("GET")[1]["records"][0]["id"]
    res, body = env.call("PATCH", f"{BASE}/{rid}", {"data": big})
    assert res["statusCode"] == 413  # an update can't grow a record past the limit either
    assert env.call("GET")[1]["records"][0]["data"] == {"text": "x" * 1000}


def test_a_merge_that_would_overflow_is_refused(env):
    rid = env.add(a="x" * 15_000)["id"]
    res, body = env.call("PATCH", f"{BASE}/{rid}", {"data": {"b": "y" * 15_000}})
    assert res["statusCode"] == 413 and body["error"] == "record_too_large"


def test_quota_errors_are_conflicts_with_a_clear_message(env, monkeypatch):
    monkeypatch.setattr(ds, "MAX_RECORDS", 2)
    env.add(i=1), env.add(i=2)
    res, body = env.call("POST", body={"data": {"i": 3}})
    assert res["statusCode"] == 409 and body["error"] == "quota_exceeded" and "full" in body["message"]
    monkeypatch.setattr(ds, "MAX_COLLECTIONS", 1)
    res, body = env.call("POST", "/api/full-stack/todo/data/another", {"data": {}})
    assert res["statusCode"] == 409 and body["error"] == "quota_exceeded"


# ---- addresses -------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["Tasks", "1tasks", "_tasks", "__collections", "a" * 33, "ta sks", "ta.sks", "tâsks", "ta%73ks"])
def test_collection_names_are_validated(env, name):
    res, body = env.call("GET", f"/api/full-stack/todo/data/{name}")
    assert res["statusCode"] in (400, 404) and "records" not in (body or {})


@pytest.mark.parametrize("path", [
    "/api/full-stack/todo/data", "/api/full-stack/todo", "/api/full-stack/todo/data/tasks/a/b", "/api/other/todo/data/tasks",
    "/api/full-stack/../x/data/tasks", "/api/full-stack/Todo/data/tasks", "/api/full-stack/to do/data/tasks",
    "/api/full-stack/a%23b/data/tasks", "/api/full-stack//data/tasks", "/api/full-stack/mcp/data/tasks", f"{BASE}/not-an-id",
    f"{BASE}/" + "0" * 19, f"{BASE}/" + "G" * 20,
])
def test_malformed_paths_are_not_found(env, path):
    res, _ = env.call("GET", path)
    assert res["statusCode"] == 404
    assert env.call("DELETE", path)[0]["statusCode"] in (404, 405)


def test_trailing_slash_is_accepted(env):
    assert env.call("GET", BASE + "/")[0]["statusCode"] == 200


def test_wrong_methods_are_405_with_allow(env):
    rid = env.add(x=1)["id"]
    for method, path, allow in [("PUT", BASE, "GET, POST"), ("DELETE", BASE, "GET, POST"), ("PATCH", BASE, "GET, POST"),
                                ("GET", f"{BASE}/{rid}", "PATCH, DELETE"), ("POST", f"{BASE}/{rid}", "PATCH, DELETE"),
                                ("PUT", f"{BASE}/{rid}", "PATCH, DELETE")]:
        res, body = env.call(method, path)
        assert res["statusCode"] == 405 and res["headers"]["allow"] == allow and body["error"] == "method_not_allowed"


def test_unknown_record_ids(env):
    ghost = f"{BASE}/{'0' * 20}"
    assert env.call("PATCH", ghost, {"data": {"x": 1}})[0]["statusCode"] == 404
    assert env.call("PATCH", ghost, {"increment": {"x": 1}})[0]["statusCode"] == 404
    assert env.call("DELETE", ghost)[0]["statusCode"] == 204  # deleting twice is harmless


def test_content_type_is_not_required_and_is_ignored(env):
    assert env.call("POST", body={"data": {"a": 1}}, headers={"content-type": "text/plain"})[0]["statusCode"] == 201
    rid = env.call("GET")[1]["records"][0]["id"]
    assert env.call("DELETE", f"{BASE}/{rid}", headers={})[0]["statusCode"] == 204  # the original returned 415 here


# ---- which apps have a backend ---------------------------------------------------------------


def test_unknown_static_and_switched_off_apps(env):
    assert env.call("GET", "/api/full-stack/nope/data/tasks")[0]["statusCode"] == 404
    env.apps.create_app("u-me", "plain", html="x")
    res, body = env.call("GET", "/api/full-stack/plain/data/tasks")
    assert res["statusCode"] == 403 and body["error"] == "no_backend"
    assert env.call("POST", "/api/full-stack/plain/data/tasks", {"data": {"a": 1}})[0]["statusCode"] == 403
    env.registry.create({"name": "legacy", "author": "u-x", "createdAt": NOW, "updatedAt": NOW})  # an old record, no appType
    assert env.call("GET", "/api/full-stack/legacy/data/tasks")[0]["statusCode"] == 403

    env.add(keep="me")
    env.apps.set_settings("u-me", "todo", full_stack=False)
    assert env.call("GET")[0]["statusCode"] == 403 and env.call("POST", body={"data": {}})[0]["statusCode"] == 403
    env.apps.set_settings("u-me", "todo", full_stack=True)
    assert [r["data"] for r in env.call("GET")[1]["records"]] == [{"keep": "me"}]  # switching back on restores the data


def test_apps_cannot_see_each_others_data(env):
    env.apps.create_app("u-other", "other", html="x", full_stack=True, collection_name="tasks")
    secret = env.add(secret="mine")
    assert env.call("GET", "/api/full-stack/other/data/tasks")[1]["records"] == []
    env.call("POST", "/api/full-stack/other/data/tasks", {"data": {"theirs": 1}})
    assert [r["data"] for r in env.call("GET")[1]["records"]] == [{"secret": "mine"}]
    # an id from one app does nothing in another
    assert env.call("PATCH", f"/api/full-stack/other/data/tasks/{secret['id']}", {"data": {"x": 1}})[0]["statusCode"] == 404
    assert env.call("DELETE", f"/api/full-stack/other/data/tasks/{secret['id']}")[0]["statusCode"] == 204
    assert len(env.call("GET")[1]["records"]) == 1  # ...and deleted nothing


# ---- keeping other websites out ---------------------------------------------------------------


@pytest.mark.parametrize("site,method,ok", [
    (None, "GET", True), (None, "POST", True), ("same-origin", "POST", True), ("same-origin", "GET", True),
    ("none", "GET", True), ("none", "POST", False), ("cross-site", "GET", False), ("cross-site", "POST", False),
    ("same-site", "GET", False), ("same-site", "POST", False),
])
def test_requests_from_other_websites_are_refused(env, site, method, ok):
    headers = {"sec-fetch-site": site} if site else {}
    res, body = env.call(method, BASE, {"data": {"a": 1}} if method == "POST" else None, headers)
    assert (res["statusCode"] in (200, 201)) == ok
    if not ok:
        assert res["statusCode"] == 403 and body["error"] == "cross_site"
    assert len(env.call("GET")[1]["records"]) == (1 if ok and method == "POST" else 0)  # refused writes wrote nothing


def test_other_websites_cannot_edit_or_delete_existing_records(env):
    rec = env.add(title="keep me")
    for site in ("cross-site", "same-site", "none"):
        h = {"sec-fetch-site": site}
        for method, body in (("PATCH", {"data": {"title": "vandalised"}}), ("PATCH", {"increment": {"n": 1}}), ("DELETE", None)):
            res, out = env.call(method, f"{BASE}/{rec['id']}", body, h)
            assert res["statusCode"] == 403 and out["error"] == "cross_site", (site, method)
    assert env.call("GET")[1]["records"][0]["data"] == {"title": "keep me"}
    assert env.call("PATCH", f"{BASE}/{rec['id']}", {"data": {"title": "edited"}}, {"sec-fetch-site": "same-origin"})[0]["statusCode"] == 200


def test_responses_are_hardened_and_have_no_cors_headers(env):
    for res in (env.call("GET")[0], env.call("POST", body={"data": {}})[0], env.call("GET", "/api/full-stack/nope/data/x")[0]):
        h = res["headers"]
        assert h["cache-control"] == "no-store" and h["x-content-type-options"] == "nosniff"
        assert "default-src 'none'" in h["content-security-policy"]
        assert not any(k.lower().startswith("access-control") for k in h)
    assert env.call("GET")[0]["headers"]["content-type"] == "application/json"


def test_user_text_in_records_stays_inside_json(env):
    env.add(note="</script><script>alert(1)</script>")
    res = env.call("GET")[0]
    assert res["headers"]["content-type"] == "application/json" and res["headers"]["x-content-type-options"] == "nosniff"
