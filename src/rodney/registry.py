"""The registry of apps: which apps exist and who owns them.

The apps' files live in S3 and are public by design. This registry holds the records about
them, so it must NOT be reachable through the public bucket: it is a DynamoDB table that only
the Lambda can read. Every record is a plain dict: name, author, createdAt, updatedAt and an
optional description.
"""

from __future__ import annotations

from typing import Protocol

import boto3

AUTHOR_INDEX = "by-author"


class AppRegistry(Protocol):
    def create(self, meta: dict) -> bool:
        """Records a new app. Returns False (and writes nothing) if the name is taken."""
        ...

    def get(self, slug: str) -> dict | None: ...
    def touch(self, slug: str, updated_at: str) -> None: ...
    def set_author(self, slug: str, author: str, updated_at: str) -> None: ...
    def delete(self, slug: str) -> None: ...
    def list_all(self) -> list[dict]: ...
    def count_by_author(self, author: str) -> int: ...


class MemoryRegistry:
    def __init__(self) -> None:
        self.apps: dict[str, dict] = {}

    def create(self, meta):
        if meta["name"] in self.apps:
            return False
        self.apps[meta["name"]] = dict(meta)
        return True

    def get(self, slug):
        meta = self.apps.get(slug)
        return dict(meta) if meta else None

    def touch(self, slug, updated_at):
        if slug in self.apps:
            self.apps[slug]["updatedAt"] = updated_at

    def set_author(self, slug, author, updated_at):
        if slug in self.apps:
            self.apps[slug].update(author=author, updatedAt=updated_at)

    def delete(self, slug):
        self.apps.pop(slug, None)

    def list_all(self):
        return [dict(m) for m in sorted(self.apps.values(), key=lambda m: m["name"])]

    def count_by_author(self, author):
        return sum(1 for m in self.apps.values() if m["author"] == author)


class DynamoRegistry:
    """One table: partition key `slug`, plus a `by-author` index for per-person queries."""

    def __init__(self, table: str, client=None) -> None:
        self.table = table
        self.ddb = client or boto3.client("dynamodb")

    @staticmethod
    def _to_item(meta: dict) -> dict:
        item = {"slug": {"S": meta["name"]}, "author": {"S": meta["author"]},
                "createdAt": {"S": meta["createdAt"]}, "updatedAt": {"S": meta["updatedAt"]}}
        if meta.get("description"):
            item["description"] = {"S": meta["description"]}
        return item

    @staticmethod
    def _from_item(item: dict) -> dict:
        meta = {"name": item["slug"]["S"], "author": item["author"]["S"],
                "createdAt": item["createdAt"]["S"], "updatedAt": item["updatedAt"]["S"]}
        if "description" in item:
            meta["description"] = item["description"]["S"]
        return meta

    def create(self, meta):
        # The condition makes the claim atomic: of two simultaneous creates, exactly one wins.
        try:
            self.ddb.put_item(
                TableName=self.table, Item=self._to_item(meta), ConditionExpression="attribute_not_exists(slug)"
            )
            return True
        except self.ddb.exceptions.ConditionalCheckFailedException:
            return False

    def get(self, slug):
        res = self.ddb.get_item(TableName=self.table, Key={"slug": {"S": slug}}, ConsistentRead=True)
        return self._from_item(res["Item"]) if "Item" in res else None

    def _update(self, slug: str, expression: str, names: dict, values: dict) -> None:
        try:
            self.ddb.update_item(
                TableName=self.table,
                Key={"slug": {"S": slug}},
                UpdateExpression=expression,
                ConditionExpression="attribute_exists(slug)",  # never resurrect a deleted app
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except self.ddb.exceptions.ConditionalCheckFailedException:
            pass

    def touch(self, slug, updated_at):
        self._update(slug, "SET #u = :t", {"#u": "updatedAt"}, {":t": {"S": updated_at}})

    def set_author(self, slug, author, updated_at):
        self._update(
            slug, "SET #a = :a, #u = :t", {"#a": "author", "#u": "updatedAt"},
            {":a": {"S": author}, ":t": {"S": updated_at}},
        )

    def delete(self, slug):
        self.ddb.delete_item(TableName=self.table, Key={"slug": {"S": slug}})

    def list_all(self):
        items: list[dict] = []
        kwargs: dict = {"TableName": self.table}
        while True:
            res = self.ddb.scan(**kwargs)
            items += [self._from_item(i) for i in res.get("Items", [])]
            if "LastEvaluatedKey" not in res:
                return sorted(items, key=lambda m: m["name"])
            kwargs["ExclusiveStartKey"] = res["LastEvaluatedKey"]

    def count_by_author(self, author):
        total = 0
        kwargs: dict = {
            "TableName": self.table,
            "IndexName": AUTHOR_INDEX,
            "KeyConditionExpression": "#a = :a",
            "ExpressionAttributeNames": {"#a": "author"},
            "ExpressionAttributeValues": {":a": {"S": author}},
            "Select": "COUNT",
        }
        while True:
            res = self.ddb.query(**kwargs)
            total += res["Count"]
            if "LastEvaluatedKey" not in res:
                return total
            kwargs["ExclusiveStartKey"] = res["LastEvaluatedKey"]
