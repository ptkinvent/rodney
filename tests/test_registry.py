"""One contract, run against both registries. The DynamoDB one uses a fake client that
enforces conditional writes and paginates, as the real service does."""

import pytest

from rodney.registry import DynamoRegistry, MemoryRegistry


class FakeDynamo:
    """Just the DynamoDB client calls DynamoRegistry makes, with real semantics for them."""

    PAGE = 2  # tiny pages, so pagination bugs show up

    class exceptions:
        class ConditionalCheckFailedException(Exception):
            pass

    def __init__(self):
        self.items: dict[str, dict] = {}

    def put_item(self, TableName, Item, ConditionExpression=None):
        if ConditionExpression == "attribute_not_exists(slug)" and Item["slug"]["S"] in self.items:
            raise self.exceptions.ConditionalCheckFailedException()
        self.items[Item["slug"]["S"]] = Item

    def get_item(self, TableName, Key, ConsistentRead=False):
        item = self.items.get(Key["slug"]["S"])
        return {"Item": item} if item else {}

    def update_item(self, TableName, Key, UpdateExpression, ConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues):
        slug = Key["slug"]["S"]
        if ConditionExpression == "attribute_exists(slug)" and slug not in self.items:
            raise self.exceptions.ConditionalCheckFailedException()
        assert UpdateExpression.startswith("SET ")
        for clause in UpdateExpression[4:].split(", "):
            name, value = (part.strip() for part in clause.split("="))
            self.items[slug][ExpressionAttributeNames[name]] = ExpressionAttributeValues[value]

    def delete_item(self, TableName, Key):
        self.items.pop(Key["slug"]["S"], None)

    def _page(self, rows, ExclusiveStartKey):
        rows = sorted(rows, key=lambda i: i["slug"]["S"])
        if ExclusiveStartKey:
            rows = [r for r in rows if r["slug"]["S"] > ExclusiveStartKey["slug"]["S"]]
        page, rest = rows[: self.PAGE], rows[self.PAGE :]
        return page, ({"slug": page[-1]["slug"]} if rest else None)

    def scan(self, TableName, ExclusiveStartKey=None):
        page, last = self._page(list(self.items.values()), ExclusiveStartKey)
        return {"Items": page, **({"LastEvaluatedKey": last} if last else {})}

    def query(self, TableName, IndexName, KeyConditionExpression, ExpressionAttributeNames, ExpressionAttributeValues, Select=None, ExclusiveStartKey=None):
        assert IndexName == "by-author" and KeyConditionExpression == "#a = :a" and Select in (None, "COUNT")
        author = ExpressionAttributeValues[":a"]["S"]
        mine = [i for i in self.items.values() if i["author"]["S"] == author]
        page, last = self._page(mine, ExclusiveStartKey)
        res = {"Count": len(page), **({"LastEvaluatedKey": last} if last else {})}
        if Select != "COUNT":  # like the real index, a full query returns the items
            res["Items"] = page
        return res


@pytest.fixture(params=["memory", "dynamo"])
def reg(request):
    return MemoryRegistry() if request.param == "memory" else DynamoRegistry("table", FakeDynamo())


def meta(name, author="u-a", **extra):
    return {"name": name, "author": author, "createdAt": "2026-01-01T00:00:00.000Z", "updatedAt": "2026-01-01T00:00:00.000Z", **extra}


def test_create_and_get_roundtrip(reg):
    assert reg.create(meta("todo", description="a todo list")) is True
    assert reg.get("todo") == meta("todo", description="a todo list")
    assert reg.create(meta("plain")) is True
    assert "description" not in reg.get("plain")
    assert reg.get("missing") is None


def test_name_claim_is_exclusive_and_does_not_overwrite(reg):
    assert reg.create(meta("todo", author="u-first")) is True
    assert reg.create(meta("todo", author="u-attacker")) is False
    assert reg.get("todo")["author"] == "u-first"


def test_touch_set_author_and_delete(reg):
    reg.create(meta("todo"))
    reg.touch("todo", "2026-02-02T00:00:00.000Z")
    assert reg.get("todo")["updatedAt"] == "2026-02-02T00:00:00.000Z"
    reg.set_author("todo", "u-new", "2026-03-03T00:00:00.000Z")
    got = reg.get("todo")
    assert got["author"] == "u-new" and got["updatedAt"] == "2026-03-03T00:00:00.000Z" and got["createdAt"].startswith("2026-01-01")
    reg.delete("todo")
    assert reg.get("todo") is None
    reg.delete("todo")  # deleting twice is harmless


def test_updates_never_resurrect_a_deleted_app(reg):
    reg.touch("ghost", "2026-02-02T00:00:00.000Z")
    reg.set_author("ghost", "u-x", "2026-02-02T00:00:00.000Z")
    assert reg.get("ghost") is None and reg.list_all() == []


def test_list_all_sorted_and_complete_across_pages(reg):
    for name in ["e", "c", "a", "d", "b"]:  # 5 items > the fake's page size of 2
        reg.create(meta(name))
    assert [m["name"] for m in reg.list_all()] == ["a", "b", "c", "d", "e"]


def test_count_by_author_across_pages(reg):
    for i in range(5):
        reg.create(meta(f"mine-{i}", author="u-me"))
    reg.create(meta("theirs", author="u-them"))
    assert reg.count_by_author("u-me") == 5
    assert reg.count_by_author("u-them") == 1
    assert reg.count_by_author("u-nobody") == 0
    reg.delete("mine-0")
    assert reg.count_by_author("u-me") == 4


def test_returned_records_are_copies(reg):
    reg.create(meta("todo"))
    reg.get("todo")["author"] = "tampered"
    reg.list_all()[0]["author"] = "tampered"
    assert reg.get("todo")["author"] == "u-a"


def test_list_by_author_returns_only_that_authors_apps_across_pages(reg):
    for i in range(5):
        reg.create(meta(f"mine-{i}", author="u-me", description=f"app {i}"))
    reg.create(meta("theirs", author="u-them"))
    mine = reg.list_by_author("u-me")
    assert [m["name"] for m in mine] == [f"mine-{i}" for i in range(5)]
    assert mine[2]["description"] == "app 2" and all(m["author"] == "u-me" for m in mine)
    assert reg.list_by_author("u-nobody") == []


def test_settings_roundtrip_and_stay_absent_when_unset(reg):
    reg.create(meta("plain"))
    reg.create(meta("full", appType="full-stack", collectionName="tasks", primaryField="title", template="crud"))
    assert "appType" not in reg.get("plain")  # legacy records are untouched
    got = reg.get("full")
    assert (got["appType"], got["collectionName"], got["primaryField"], got["template"]) == ("full-stack", "tasks", "title", "crud")
    assert [m["appType"] for m in reg.list_by_author("u-a") if m["name"] == "full"] == ["full-stack"]


def test_set_fields_updates_settings_and_refuses_everything_else(reg):
    reg.create(meta("todo"))
    reg.set_fields("todo", {"appType": "full-stack", "collectionName": "tasks"}, "2026-05-05T00:00:00.000Z")
    got = reg.get("todo")
    assert got["appType"] == "full-stack" and got["collectionName"] == "tasks" and got["updatedAt"] == "2026-05-05T00:00:00.000Z"
    reg.set_fields("todo", {"appType": "static"}, "2026-06-06T00:00:00.000Z")
    assert reg.get("todo")["appType"] == "static" and reg.get("todo")["collectionName"] == "tasks"
    for forbidden in ({"author": "u-evil"}, {"slug": "x"}, {"createdAt": "x"}, {"appType": "full-stack", "author": "u-evil"}):
        with pytest.raises(ValueError):
            reg.set_fields("todo", forbidden, "2026-07-07T00:00:00.000Z")
    assert reg.get("todo")["author"] == "u-a"
    reg.set_fields("ghost", {"appType": "full-stack"}, "2026-07-07T00:00:00.000Z")  # never resurrects a deleted app
    assert reg.get("ghost") is None
