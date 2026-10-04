"""Short-lived login state that must be single-use: authorization codes and refresh tokens.

Tokens are stored by their SHA-256 hash, so a leak of the table doesn't leak usable tokens.
`take` deletes and returns in one atomic step, which is what makes a code or refresh token
usable exactly once.
"""

import json
import time
from typing import Protocol

import boto3

from .tokens import sha256_hex


class AuthStore(Protocol):
    def put(self, kind: str, token: str, data: dict, ttl_seconds: int) -> None: ...
    def take(self, kind: str, token: str) -> dict | None: ...


class MemoryAuthStore:
    def __init__(self, clock=time.time) -> None:
        self.items: dict[str, tuple[dict, float]] = {}
        self.clock = clock

    def put(self, kind, token, data, ttl_seconds):
        self.items[f"{kind}#{sha256_hex(token)}"] = (data, self.clock() + ttl_seconds)

    def take(self, kind, token):
        item = self.items.pop(f"{kind}#{sha256_hex(token)}", None)
        if item is None or item[1] <= self.clock():
            return None
        return item[0]


class DynamoAuthStore:
    """One DynamoDB table: partition key `id`, TTL attribute `ttl` (epoch seconds)."""

    def __init__(self, table: str, client=None, clock=time.time) -> None:
        self.table = table
        self.ddb = client or boto3.client("dynamodb")
        self.clock = clock

    def put(self, kind, token, data, ttl_seconds):
        self.ddb.put_item(
            TableName=self.table,
            Item={
                "id": {"S": f"{kind}#{sha256_hex(token)}"},
                "data": {"S": json.dumps(data)},
                "ttl": {"N": str(int(self.clock() + ttl_seconds))},
            },
        )

    def take(self, kind, token):
        # DeleteItem with ALL_OLD is atomic: of two racing requests, only one gets the item.
        res = self.ddb.delete_item(
            TableName=self.table, Key={"id": {"S": f"{kind}#{sha256_hex(token)}"}}, ReturnValues="ALL_OLD"
        )
        item = res.get("Attributes")
        # DynamoDB removes expired items lazily (up to ~48h), so check expiry ourselves.
        if not item or int(item["ttl"]["N"]) <= self.clock():
            return None
        return json.loads(item["data"]["S"])
