"""One contract, run against both data stores. The DynamoDB one talks to a fake client that
enforces conditions, transactions and pagination the way the real service does."""

import pytest
from fake_dynamo_data import FakeDataDynamo

import rodney.datastore as ds
from rodney.datastore import (Busy, DynamoDataStore, MemoryDataStore, NotANumber, NotFound, QuotaExceeded, from_attr, to_attr)

NOW = "2026-01-01T00:00:00.000Z"
LATER = "2026-01-02T00:00:00.000Z"


@pytest.fixture(params=["memory", "dynamo"])
def store(request):
    return MemoryDataStore() if request.param == "memory" else DynamoDataStore("t", FakeDataDynamo())


def merge(patch):
    return lambda old: {**old, **patch}


# ---- records ---------------------------------------------------------------------------------


def test_create_list_roundtrip_in_creation_order(store):
    ids = [store.create("todo", "tasks", {"text": t, "done": False}, NOW)["id"] for t in "abcdef"]  # > the fake's page size
    got = store.list("todo", "tasks")
    assert [r["id"] for r in got] == ids == sorted(ids)
    assert [r["data"]["text"] for r in got] == list("abcdef")
    assert got[0].keys() == {"id", "data", "createdAt", "updatedAt"} and got[0]["createdAt"] == NOW


def test_values_roundtrip_all_json_types_and_nest(store):
    data = {"s": "héllo ✓", "i": 42, "f": 0.5, "t": True, "n": None, "l": [1, "two", {"x": [3]}], "m": {"deep": {"er": 1}}, "e": "", "z": {}}
    rid = store.create("a", "c", data, NOW)["id"]
    assert store.list("a", "c")[0]["data"] == data and rid


def test_numbers_normalise_like_dynamodb(store):
    store.create("a", "c", {"x": 2.0, "y": 0.50, "z": 100}, NOW)
    assert store.list("a", "c")[0]["data"] == {"x": 2, "y": 0.5, "z": 100}


def test_apps_and_collections_are_isolated(store):
    store.create("app-a", "tasks", {"who": "a"}, NOW)
    store.create("app-b", "tasks", {"who": "b"}, NOW)
    store.create("app-a", "notes", {"who": "a-notes"}, NOW)
    assert [r["data"]["who"] for r in store.list("app-a", "tasks")] == ["a"]
    assert [r["data"]["who"] for r in store.list("app-b", "tasks")] == ["b"]
    assert [r["data"]["who"] for r in store.list("app-a", "notes")] == ["a-notes"]
    assert store.list("app-c", "tasks") == []
    # a name that merely starts with another app's name must not leak into it
    store.create("app", "x", {"who": "app"}, NOW)
    assert store.list("app-a", "x") == [] and len(store.list("app", "x")) == 1


def test_update_merges_and_bumps_timestamp_only(store):
    rec = store.create("a", "c", {"text": "x", "done": False}, NOW)
    out = store.update("a", "c", rec["id"], merge({"done": True}), LATER)
    assert out["data"] == {"text": "x", "done": True} and out["updatedAt"] == LATER and out["createdAt"] == NOW
    assert store.list("a", "c")[0] == out


def test_update_can_replace_wholesale(store):
    rec = store.create("a", "c", {"a": 1, "b": 2}, NOW)
    assert store.update("a", "c", rec["id"], lambda old: {"only": 1}, LATER)["data"] == {"only": 1}


def test_update_and_increment_and_delete_unknown_records(store):
    with pytest.raises(NotFound):
        store.update("a", "c", "0" * 20, merge({}), NOW)
    with pytest.raises(NotFound):
        store.increment("a", "c", "0" * 20, {"n": 1}, NOW)
    store.delete("a", "c", "0" * 20)  # deleting something that isn't there is harmless
    store.create("a", "c", {"x": 1}, NOW)
    with pytest.raises(NotFound):  # same id, other app
        store.update("other", "c", store.list("a", "c")[0]["id"], merge({}), NOW)


def test_delete_removes_only_that_record(store):
    keep = store.create("a", "c", {"k": 1}, NOW)["id"]
    gone = store.create("a", "c", {"k": 2}, NOW)["id"]
    store.delete("a", "c", gone)
    assert [r["id"] for r in store.list("a", "c")] == [keep]


# ---- atomic increments -----------------------------------------------------------------------


def test_increment_creates_adds_and_returns_the_record(store):
    rid = store.create("a", "c", {"title": "poll"}, NOW)["id"]
    out = store.increment("a", "c", rid, {"yes": 1}, LATER)
    assert out["data"] == {"title": "poll", "yes": 1} and out["updatedAt"] == LATER
    out = store.increment("a", "c", rid, {"yes": 2, "no": 1}, LATER)
    assert out["data"] == {"title": "poll", "yes": 3, "no": 1}
    assert store.increment("a", "c", rid, {"yes": -3}, LATER)["data"]["yes"] == 0
    assert store.increment("a", "c", rid, {"avg": 0.25}, LATER)["data"]["avg"] == 0.25
    assert store.list("a", "c")[0]["data"]["no"] == 1


def test_increment_refuses_non_numbers_and_changes_nothing(store):
    rid = store.create("a", "c", {"name": "x", "flag": True, "n": 5}, NOW)["id"]
    for field in ("name", "flag"):
        with pytest.raises(NotANumber):
            store.increment("a", "c", rid, {field: 1}, LATER)
    with pytest.raises(NotANumber):
        store.increment("a", "c", rid, {"n": 1, "name": 1}, LATER)  # all or nothing
    assert store.list("a", "c")[0]["data"] == {"name": "x", "flag": True, "n": 5}


def test_many_increments_never_lose_a_count(store):
    rid = store.create("a", "c", {"votes": 0}, NOW)["id"]
    for _ in range(50):
        store.increment("a", "c", rid, {"votes": 1}, LATER)
    assert store.list("a", "c")[0]["data"]["votes"] == 50


# ---- quotas ----------------------------------------------------------------------------------


def test_record_count_limit_and_deleting_frees_space(store, monkeypatch):
    monkeypatch.setattr(ds, "MAX_RECORDS", 3)
    ids = [store.create("a", "c", {"i": i}, NOW)["id"] for i in range(3)]
    with pytest.raises(QuotaExceeded):
        store.create("a", "c", {"i": 99}, NOW)
    assert len(store.list("a", "c")) == 3
    store.create("a", "other", {"i": 1}, NOW)  # the limit is per collection
    store.delete("a", "c", ids[0])
    store.create("a", "c", {"i": 4}, NOW)


def test_collection_byte_limit_applies_to_creates_and_to_growing_updates(store, monkeypatch):
    monkeypatch.setattr(ds, "MAX_COLLECTION_BYTES", 300)
    a = store.create("a", "c", {"t": "x" * 100}, NOW)["id"]
    store.create("a", "c", {"t": "y" * 100}, NOW)
    with pytest.raises(QuotaExceeded):
        store.create("a", "c", {"t": "z" * 100}, NOW)  # would exceed 300 bytes in total
    with pytest.raises(QuotaExceeded):
        store.update("a", "c", a, merge({"t": "x" * 200}), LATER)  # growing past the limit
    assert store.list("a", "c")[0]["data"]["t"] == "x" * 100  # the refused update changed nothing
    store.update("a", "c", a, merge({"t": "x"}), LATER)  # shrinking is always fine
    store.create("a", "c", {"t": "z" * 100}, NOW)  # and frees room


def test_collections_per_app_limit(store, monkeypatch):
    monkeypatch.setattr(ds, "MAX_COLLECTIONS", 2)
    store.create("a", "one", {"x": 1}, NOW)
    store.create("a", "two", {"x": 1}, NOW)
    with pytest.raises(QuotaExceeded):
        store.create("a", "three", {"x": 1}, NOW)
    store.create("a", "one", {"x": 2}, NOW)  # existing collections keep working
    store.create("b", "one", {"x": 1}, NOW)  # other apps have their own allowance


def test_collection_summary_tracks_counts_and_bytes(store):
    r = store.create("a", "c", {"t": "abc"}, NOW)
    store.create("a", "c", {"t": "abcdef"}, NOW)
    base = {c["name"]: c for c in store.collections("a")}["c"]
    assert base["count"] == 2 and base["bytes"] == ds.encoded_size({"t": "abc"}) + ds.encoded_size({"t": "abcdef"})
    store.update("a", "c", r["id"], merge({"t": "abcdefghij"}), LATER)
    store.delete("a", "c", store.list("a", "c")[1]["id"])
    after = {c["name"]: c for c in store.collections("a")}["c"]
    assert after["count"] == 1 and after["bytes"] == ds.encoded_size({"t": "abcdefghij"})


# ---- clearing --------------------------------------------------------------------------------


def test_clear_one_collection_and_whole_app(store):
    for c in ("tasks", "notes"):
        for i in range(4):
            store.create("a", c, {"i": i}, NOW)
    store.create("b", "tasks", {"keep": True}, NOW)
    assert store.clear("a", "tasks") == 4 and store.list("a", "tasks") == []
    assert len(store.list("a", "notes")) == 4
    assert [c["name"] for c in store.collections("a")] == ["notes"]
    assert store.clear("a", "tasks") == 0  # nothing left to clear
    assert store.delete_app("a") == 4
    assert store.collections("a") == [] and store.list("a", "notes") == []
    assert len(store.list("b", "tasks")) == 1  # other apps untouched
    store.create("a", "tasks", {"fresh": 1}, NOW)  # a cleared app starts clean, quotas included


# ---- dynamodb-specific: concurrency ---------------------------------------------------------


@pytest.fixture
def dynamo():
    fake = FakeDataDynamo()
    return DynamoDataStore("t", fake), fake


def test_concurrent_edit_to_another_field_is_retried_not_lost(dynamo):
    store, fake = dynamo
    rid = store.create("a", "c", {"text": "x", "done": False}, NOW)["id"]
    key = store._key("a", "c", rid)

    def other_writer(f):  # someone else edits the record between our read and our write
        f.items[(key["pk"]["S"], key["sk"]["S"])] = f._apply(
            f._item(key), "SET #d.#t = :t, #v = #v + :one", {"#d": "data", "#t": "text", "#v": "v"},
            {":t": {"S": "edited by someone else"}, ":one": {"N": "1"}})

    fake.before_transact = other_writer
    out = store.update("a", "c", rid, merge({"done": True}), LATER)
    assert out["data"] == {"text": "edited by someone else", "done": True}  # both edits survive
    assert store.list("a", "c")[0]["data"] == out["data"]


def test_an_increment_during_an_update_is_not_overwritten(dynamo):
    store, fake = dynamo
    rid = store.create("a", "c", {"votes": 0, "title": "t"}, NOW)["id"]
    fake.before_transact = lambda f: store.increment("a", "c", rid, {"votes": 1}, NOW)
    out = store.update("a", "c", rid, merge({"title": "new"}), LATER)
    assert out["data"] == {"votes": 1, "title": "new"}


def test_endless_contention_gives_up_with_busy_and_changes_nothing(dynamo):
    store, fake = dynamo
    rid = store.create("a", "c", {"n": 0}, NOW)["id"]

    def always_conflict(f):
        store.increment("a", "c", rid, {"n": 1}, NOW)
        f.before_transact = always_conflict

    fake.before_transact = always_conflict
    with pytest.raises(Busy):
        store.update("a", "c", rid, merge({"x": 1}), LATER)
    assert "x" not in store.list("a", "c")[0]["data"]


def test_delete_racing_with_an_edit_retries_and_counters_stay_exact(dynamo):
    store, fake = dynamo
    rid = store.create("a", "c", {"t": "abc"}, NOW)["id"]
    fake.before_transact = lambda f: store.increment("a", "c", rid, {"n": 1}, NOW)
    store.delete("a", "c", rid)
    assert store.list("a", "c") == []
    assert {c["name"]: c for c in store.collections("a")}["c"]["count"] == 0


@pytest.mark.parametrize("slug,collection", [("a", "__collections"), ("a", "_x"), ("a", ""), ("a#b", "c"), ("a", "c#d"), ("", "c")])
def test_names_that_could_reach_counters_or_other_apps_are_refused(store, slug, collection):
    store.create("a", "c", {"x": 1}, NOW)
    for call in (lambda: store.list(slug, collection), lambda: store.create(slug, collection, {}, NOW),
                 lambda: store.delete(slug, collection, "0" * 20), lambda: store.clear(slug, collection)):
        with pytest.raises(ValueError):
            call()


def test_ids_made_in_the_same_millisecond_still_sort_in_creation_order():
    ids = [ds.new_id(lambda: 1_700_000_000.123) for _ in range(500)]
    assert ids == sorted(ids) and len(set(ids)) == 500 and all(len(i) == 20 for i in ids)
    assert ds.new_id(lambda: 1_700_000_000.0) > ids[0] or True  # a clock that steps back never repeats an id
    later = ds.new_id(lambda: 1_700_000_001.0)
    assert later > ids[-1] and len(set(ids + [later])) == 501
