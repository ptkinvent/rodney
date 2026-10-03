"""Storage. `Store` is the interface; S3 is the real thing, memory is for tests."""

# Lazy annotations: our methods are named `list`, which would otherwise shadow the
# builtin inside the class body when later annotations say `list[str]`.
from __future__ import annotations

from typing import Protocol

import boto3
from botocore.exceptions import ClientError


class Store(Protocol):
    def get(self, key: str) -> str | None: ...
    def put(self, key: str, body: str, content_type: str) -> None: ...
    def put_if_absent(self, key: str, body: str, content_type: str) -> bool:
        """Returns False (and writes nothing) if the key already exists."""
        ...
    def list(self, prefix: str) -> list[str]: ...
    def delete_many(self, keys: list[str]) -> None: ...


class MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[str, str]] = {}  # key -> (body, content_type)

    def get(self, key):
        obj = self.objects.get(key)
        return obj[0] if obj else None

    def put(self, key, body, content_type):
        self.objects[key] = (body, content_type)

    def put_if_absent(self, key, body, content_type):
        if key in self.objects:
            return False
        self.objects[key] = (body, content_type)
        return True

    def list(self, prefix):
        return sorted(k for k in self.objects if k.startswith(prefix))

    def delete_many(self, keys):
        for k in keys:
            self.objects.pop(k, None)


class S3Store:
    def __init__(self, bucket: str, client=None) -> None:
        self.bucket = bucket
        self.s3 = client or boto3.client("s3")

    def get(self, key):
        try:
            return self.s3.get_object(Bucket=self.bucket, Key=key)["Body"].read().decode("utf-8")
        except self.s3.exceptions.NoSuchKey:
            return None

    def put(self, key, body, content_type):
        self.s3.put_object(Bucket=self.bucket, Key=key, Body=body.encode("utf-8"), ContentType=content_type)

    def put_if_absent(self, key, body, content_type):
        # IfNoneMatch="*" makes S3 itself refuse to overwrite, so two simultaneous
        # creates of the same name can't both succeed.
        try:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body.encode("utf-8"),
                ContentType=content_type,
                IfNoneMatch="*",
            )
            return True
        except ClientError as e:
            # 412 = already exists; 409 = a concurrent write to the same key is in flight.
            if e.response["Error"]["Code"] in ("PreconditionFailed", "ConditionalRequestConflict"):
                return False
            raise

    def list(self, prefix):
        keys: list[str] = []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            keys.extend(o["Key"] for o in page.get("Contents", []))
        return keys

    def delete_many(self, keys):
        for i in range(0, len(keys), 1000):  # S3 deletes at most 1000 keys per call
            chunk = keys[i : i + 1000]
            self.s3.delete_objects(
                Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True}
            )
