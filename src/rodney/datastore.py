"""Storage for full-stack apps' data: JSON records, grouped into collections, per app.

A record is `{id, data, createdAt, updatedAt}`; the app's own fields live under `data`.
All apps share one DynamoDB table, kept apart by key construction: every key is built here from
an app name and collection name that the caller has already validated, never from raw input.

    record:    pk = "<app>#<collection>"      sk = "<id>"          data (a DynamoDB map), size, v, ...
    counters:  pk = "<app>#__collections"     sk = "<collection>"  cnt, bytes   (one per collection)

The counter item is how the quotas are enforced atomically: a record and its counter change in
one transaction, so two simultaneous writes can't both squeeze past a limit. `v` is a version
number for optimistic concurrency, so two simultaneous edits never silently lose one of them.
`data` is a native map (not a JSON string) so numeric increments are true server-side atomic
updates, safe for counters and votes.
"""

from __future__ import annotations

import json
import random
import re
import secrets
import time
from decimal import Decimal
from typing import Callable, Protocol

import boto3
from botocore.exceptions import ClientError

MAX_RECORDS = 1000  # per collection
MAX_RECORD_BYTES = 20 * 1024  # per record, as compact JSON
MAX_COLLECTION_BYTES = 4 * 1024 * 1024  # per collection; keeps a full listing under Lambda's 6 MB reply limit
MAX_COLLECTIONS = 10  # per app
_RETRIES = 8


def _backoff(attempt: int) -> None:
    """A short random pause between retries, so simultaneous editors don't keep colliding."""
    time.sleep(random.uniform(0, 0.015 * (attempt + 1)))

META_SUFFIX = "__collections"  # can't be a collection name: those must start with a letter


class DataError(Exception):
    """An error whose message is safe to show to the caller."""


class NotFound(DataError):
    pass


class QuotaExceeded(DataError):
    pass


class NotANumber(DataError):
    pass


class Busy(DataError):
    """Too many simultaneous edits to one record; the caller can simply retry."""


def encoded_size(data: dict) -> int:
    return len(json.dumps(data, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


_last_id = [0, 0]  # [millisecond, sequence within that millisecond]


def new_id(clock: Callable[[], float] = time.time) -> str:
    """20 hex chars that sort by creation time: millisecond, a sequence number for ids made in
    the same millisecond, then randomness so separate servers can't collide."""
    ms = int(clock() * 1000)
    _last_id[1] = _last_id[1] + 1 if ms <= _last_id[0] else 0
    _last_id[0] = max(ms, _last_id[0])
    return f"{_last_id[0]:012x}{_last_id[1]:04x}{secrets.token_hex(2)}"


def check_names(slug: str, collection: str) -> None:
    """Defence in depth: the API validates names, but the store won't build a key from a name
    that could reach another app's data or the counter rows."""
    if not slug or "#" in slug or not collection or "#" in collection or collection.startswith("_"):
        raise ValueError(f"invalid app or collection name: {slug!r}, {collection!r}")


# --- JSON <-> DynamoDB attribute values -----------------------------------------------------------


def _number(f: float) -> str:
    return format(Decimal(repr(f)), "f")  # never exponent notation


def to_attr(v):
    if v is None:
        return {"NULL": True}
    if isinstance(v, bool):
        return {"BOOL": v}
    if isinstance(v, int):
        return {"N": str(v)}
    if isinstance(v, float):
        return {"N": _number(v)}
    if isinstance(v, str):
        return {"S": v}
    if isinstance(v, list):
        return {"L": [to_attr(x) for x in v]}
    if isinstance(v, dict):
        return {"M": {k: to_attr(x) for k, x in v.items()}}
    raise TypeError(f"unsupported value {type(v).__name__}")


def _normalize(attr):
    """DynamoDB stores numbers canonically ("2.0" and "2" are the same number); do the same."""
    (kind, v), = attr.items()
    if kind == "N":
        return {"N": format(Decimal(v).normalize(), "f")}
    if kind == "L":
        return {"L": [_normalize(x) for x in v]}
    if kind == "M":
        return {"M": {k: _normalize(x) for k, x in v.items()}}
    return attr


def from_attr(a):
    (kind, v), = a.items()
    if kind == "NULL":
        return None
    if kind == "N":
        return int(v) if re.fullmatch(r"-?\d+", v) else float(v)
    if kind == "L":
        return [from_attr(x) for x in v]
    if kind == "M":
        return {k: from_attr(x) for k, x in v.items()}
    return v  # S, BOOL


class DataStore(Protocol):
    def list(self, slug: str, collection: str) -> list[dict]: ...
    def create(self, slug: str, collection: str, data: dict, now: str) -> dict: ...
    def update(self, slug: str, collection: str, rid: str, change: Callable[[dict], dict], now: str) -> dict: ...
    def increment(self, slug: str, collection: str, rid: str, amounts: dict, now: str) -> dict: ...
    def delete(self, slug: str, collection: str, rid: str) -> None: ...
    def collections(self, slug: str) -> list[dict]: ...
    def clear(self, slug: str, collection: str) -> int: ...
    def delete_app(self, slug: str) -> int: ...


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class MemoryDataStore:
    """The same behaviour as DynamoDataStore, in a dict. Values round-trip through the DynamoDB
    attribute encoding so numbers and types normalise exactly as they would in production."""

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        # slug -> collection -> {"records": {id: rec}, "cnt": int, "bytes": int}
        self.apps: dict[str, dict[str, dict]] = {}

    def _coll(self, slug: str, collection: str, create: bool = False) -> dict | None:
        check_names(slug, collection)
        colls = self.apps.setdefault(slug, {}) if create else self.apps.get(slug, {})
        if collection not in colls and create:
            if len(colls) >= MAX_COLLECTIONS:
                raise QuotaExceeded(f"An app can have at most {MAX_COLLECTIONS} collections.")
            colls[collection] = {"records": {}, "cnt": 0, "bytes": 0}
        return colls.get(collection)

    @staticmethod
    def _public(rec: dict) -> dict:
        return {"id": rec["id"], "data": from_attr(rec["data"]), "createdAt": rec["createdAt"], "updatedAt": rec["updatedAt"]}

    def list(self, slug, collection):
        c = self._coll(slug, collection)
        return [self._public(r) for _, r in sorted(c["records"].items())] if c else []

    def create(self, slug, collection, data, now):
        size = encoded_size(data)
        c = self._coll(slug, collection, create=True)
        if c["cnt"] >= MAX_RECORDS or c["bytes"] + size > MAX_COLLECTION_BYTES:
            raise QuotaExceeded(f"This collection is full (limit {MAX_RECORDS} records and {MAX_COLLECTION_BYTES // 1024 // 1024} MB).")
        rec = {"id": new_id(self.clock), "data": _normalize(to_attr(data)), "size": size, "v": 1, "createdAt": now, "updatedAt": now}
        c["records"][rec["id"]] = rec
        c["cnt"] += 1
        c["bytes"] += size
        return self._public(rec)

    def update(self, slug, collection, rid, change, now):
        c = self._coll(slug, collection)
        rec = c["records"].get(rid) if c else None
        if rec is None:
            raise NotFound("No such record.")
        new_data = change(from_attr(rec["data"]))
        size = encoded_size(new_data)
        delta = size - rec["size"]
        if delta > 0 and c["bytes"] + delta > MAX_COLLECTION_BYTES:
            raise QuotaExceeded("This collection is full (4 MB limit).")
        rec.update(data=_normalize(to_attr(new_data)), size=size, v=rec["v"] + 1, updatedAt=now)
        c["bytes"] += delta
        return self._public(rec)

    def increment(self, slug, collection, rid, amounts, now):
        c = self._coll(slug, collection)
        rec = c["records"].get(rid) if c else None
        if rec is None:
            raise NotFound("No such record.")
        data = from_attr(rec["data"])
        for field in amounts:
            if field in data and not _is_number(data[field]):
                raise NotANumber(f'The field "{field}" is not a number.')
        for field, amount in amounts.items():
            data[field] = data.get(field, 0) + amount
        rec.update(data=_normalize(to_attr(data)), v=rec["v"] + 1, updatedAt=now)
        return self._public(rec)

    def delete(self, slug, collection, rid):
        c = self._coll(slug, collection)
        rec = c["records"].pop(rid, None) if c else None
        if rec is not None:
            c["cnt"] -= 1
            c["bytes"] -= rec["size"]

    def collections(self, slug):
        return [{"name": n, "count": c["cnt"], "bytes": c["bytes"]} for n, c in sorted(self.apps.get(slug, {}).items())]

    def clear(self, slug, collection):
        check_names(slug, collection)
        c = self.apps.get(slug, {}).pop(collection, None)
        return c["cnt"] if c else 0

    def delete_app(self, slug):
        return sum(c["cnt"] for c in self.apps.pop(slug, {}).values())


def _n(value) -> dict:
    return {"N": str(value)}


class DynamoDataStore:
    def __init__(self, table: str, client=None, clock: Callable[[], float] = time.time) -> None:
        self.table = table
        self.ddb = client or boto3.client("dynamodb")
        self.clock = clock

    @staticmethod
    def _pk(slug: str, collection: str) -> str:
        check_names(slug, collection)
        return f"{slug}#{collection}"

    @staticmethod
    def _meta_pk(slug: str) -> str:
        return f"{slug}#{META_SUFFIX}"

    @staticmethod
    def _public(item: dict) -> dict:
        return {"id": item["sk"]["S"], "data": from_attr(item["data"]), "createdAt": item["createdAt"]["S"], "updatedAt": item["updatedAt"]["S"]}

    def _key(self, slug, collection, rid) -> dict:
        return {"pk": {"S": self._pk(slug, collection)}, "sk": {"S": rid}}

    def _counter_key(self, slug, collection) -> dict:
        return {"pk": {"S": self._meta_pk(slug)}, "sk": {"S": collection}}

    def _query(self, pk: str, **extra) -> list[dict]:
        items: list[dict] = []
        kwargs = {
            "TableName": self.table,
            "KeyConditionExpression": "#pk = :pk",
            "ExpressionAttributeNames": {"#pk": "pk", **extra.pop("names", {})},
            "ExpressionAttributeValues": {":pk": {"S": pk}},
            "ConsistentRead": True,
            **extra,
        }
        while True:
            res = self.ddb.query(**kwargs)
            items += res.get("Items", [])
            if "LastEvaluatedKey" not in res:
                return items
            kwargs["ExclusiveStartKey"] = res["LastEvaluatedKey"]

    def _get(self, slug, collection, rid) -> dict | None:
        res = self.ddb.get_item(TableName=self.table, Key=self._key(slug, collection, rid), ConsistentRead=True)
        return res.get("Item")

    def _reasons(self, e) -> list[str]:
        return [r.get("Code", "") for r in e.response.get("CancellationReasons", [])]

    def list(self, slug, collection):
        return [self._public(i) for i in self._query(self._pk(slug, collection))]

    def collections(self, slug):
        items = self._query(self._meta_pk(slug))
        return [{"name": i["sk"]["S"], "count": int(i["cnt"]["N"]), "bytes": int(i["bytes"]["N"])} for i in items]

    def create(self, slug, collection, data, now):
        size, rid = encoded_size(data), new_id(self.clock)
        if collection not in {c["name"] for c in self.collections(slug)} and len(self.collections(slug)) >= MAX_COLLECTIONS:
            raise QuotaExceeded(f"An app can have at most {MAX_COLLECTIONS} collections.")
        record = {
            "TableName": self.table,
            "Item": {**self._key(slug, collection, rid), "data": to_attr(data), "size": _n(size), "v": _n(1),
                     "createdAt": {"S": now}, "updatedAt": {"S": now}},
            "ConditionExpression": "attribute_not_exists(#pk)",
            "ExpressionAttributeNames": {"#pk": "pk"},
        }
        counter = {
            "TableName": self.table,
            "Key": self._counter_key(slug, collection),
            "UpdateExpression": "ADD #c :one, #b :size",
            "ConditionExpression": "attribute_not_exists(#c) OR (#c < :maxc AND #b <= :maxb)",
            "ExpressionAttributeNames": {"#c": "cnt", "#b": "bytes"},
            "ExpressionAttributeValues": {":one": _n(1), ":size": _n(size), ":maxc": _n(MAX_RECORDS), ":maxb": _n(MAX_COLLECTION_BYTES - size)},
        }
        try:
            self.ddb.transact_write_items(TransactItems=[{"Put": record}, {"Update": counter}])
        except self.ddb.exceptions.TransactionCanceledException as e:
            if "ConditionalCheckFailed" in self._reasons(e):
                raise QuotaExceeded(f"This collection is full (limit {MAX_RECORDS} records and {MAX_COLLECTION_BYTES // 1024 // 1024} MB).") from e
            raise Busy("The database is busy; try again.") from e
        return {"id": rid, "data": data, "createdAt": now, "updatedAt": now}

    def update(self, slug, collection, rid, change, now):
        for attempt in range(_RETRIES):
            if attempt:
                _backoff(attempt)
            old = self._get(slug, collection, rid)
            if old is None:
                raise NotFound("No such record.")
            new_data = change(from_attr(old["data"]))
            size = encoded_size(new_data)
            delta = size - int(old["size"]["N"])
            version = int(old["v"]["N"])
            items = [{"Update": {
                "TableName": self.table,
                "Key": self._key(slug, collection, rid),
                "UpdateExpression": "SET #d = :d, #s = :s, #u = :u, #v = :nv",
                "ConditionExpression": "attribute_exists(#pk) AND #v = :v",
                "ExpressionAttributeNames": {"#pk": "pk", "#d": "data", "#s": "size", "#u": "updatedAt", "#v": "v"},
                "ExpressionAttributeValues": {":d": to_attr(new_data), ":s": _n(size), ":u": {"S": now}, ":nv": _n(version + 1), ":v": _n(version)},
            }}]
            if delta:
                items.append({"Update": {
                    "TableName": self.table,
                    "Key": self._counter_key(slug, collection),
                    "UpdateExpression": "ADD #b :delta",
                    "ConditionExpression": "attribute_exists(#c) AND #b <= :maxb",
                    "ExpressionAttributeNames": {"#c": "cnt", "#b": "bytes"},
                    "ExpressionAttributeValues": {":delta": _n(delta), ":maxb": _n(MAX_COLLECTION_BYTES - max(delta, 0))},
                }})
            try:
                self.ddb.transact_write_items(TransactItems=items)
            except self.ddb.exceptions.TransactionCanceledException as e:
                reasons = self._reasons(e)
                if len(reasons) > 1 and reasons[1] == "ConditionalCheckFailed":
                    raise QuotaExceeded("This collection is full (4 MB limit).") from e
                continue  # the record changed under us: read it again and redo the change
            return {"id": rid, "data": new_data, "createdAt": old["createdAt"]["S"], "updatedAt": now}
        raise Busy("That record is being edited too quickly; try again.")

    def increment(self, slug, collection, rid, amounts, now):
        names = {"#pk": "pk", "#d": "data", "#u": "updatedAt", "#v": "v"}
        values = {":z": _n(0), ":one": _n(1), ":t": {"S": now}}
        sets = []
        for i, (field, amount) in enumerate(amounts.items()):
            names[f"#f{i}"] = field
            values[f":n{i}"] = {"N": _number(amount) if isinstance(amount, float) else str(amount)}
            sets.append(f"#d.#f{i} = if_not_exists(#d.#f{i}, :z) + :n{i}")
        try:
            res = self.ddb.update_item(
                TableName=self.table,
                Key=self._key(slug, collection, rid),
                UpdateExpression="SET " + ", ".join(sets + ["#u = :t", "#v = #v + :one"]),
                ConditionExpression="attribute_exists(#pk)",
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
        except self.ddb.exceptions.ConditionalCheckFailedException as e:
            raise NotFound("No such record.") from e
        except ClientError as e:
            if e.response["Error"]["Code"] == "ValidationException":
                raise NotANumber("One of those fields is not a number.") from e
            raise
        return self._public({**res["Attributes"]})

    def delete(self, slug, collection, rid):
        for attempt in range(_RETRIES):
            if attempt:
                _backoff(attempt)
            old = self._get(slug, collection, rid)
            if old is None:
                return
            items = [
                {"Delete": {
                    "TableName": self.table,
                    "Key": self._key(slug, collection, rid),
                    "ConditionExpression": "#v = :v",
                    "ExpressionAttributeNames": {"#v": "v"},
                    "ExpressionAttributeValues": {":v": _n(int(old["v"]["N"]))},
                }},
                {"Update": {
                    "TableName": self.table,
                    "Key": self._counter_key(slug, collection),
                    "UpdateExpression": "ADD #c :neg, #b :negsize",
                    "ExpressionAttributeNames": {"#c": "cnt", "#b": "bytes"},
                    "ExpressionAttributeValues": {":neg": _n(-1), ":negsize": _n(-int(old["size"]["N"]))},
                }},
            ]
            try:
                self.ddb.transact_write_items(TransactItems=items)
                return
            except self.ddb.exceptions.TransactionCanceledException:
                continue
        raise Busy("That record is being edited too quickly; try again.")

    def _delete_keys(self, keys: list[dict]) -> None:
        for i in range(0, len(keys), 25):
            pending = [{"DeleteRequest": {"Key": k}} for k in keys[i : i + 25]]
            for _ in range(_RETRIES):
                res = self.ddb.batch_write_item(RequestItems={self.table: pending})
                pending = res.get("UnprocessedItems", {}).get(self.table, [])
                if not pending:
                    break

    def clear(self, slug, collection):
        items = self._query(self._pk(slug, collection), ProjectionExpression="#pk, #sk", names={"#sk": "sk"})
        self._delete_keys([{"pk": i["pk"], "sk": i["sk"]} for i in items])
        self._delete_keys([self._counter_key(slug, collection)])
        return len(items)

    def delete_app(self, slug):
        return sum(self.clear(slug, c["name"]) for c in self.collections(slug))
