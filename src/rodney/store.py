"""Storage. `Store` is the interface; S3 is the real thing, memory is for tests."""

# Lazy annotations: our methods are named `list`, which would otherwise shadow the
# builtin inside the class body when later annotations say `list[str]`.
from __future__ import annotations

from typing import Protocol

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


class Store(Protocol):
    def get(self, key: str) -> str | None: ...
    def put(self, key: str, body: str, content_type: str) -> None: ...
    def put_if_absent(self, key: str, body: str, content_type: str) -> bool:
        """Returns False (and writes nothing) if the key already exists."""
        ...
    def list(self, prefix: str) -> list[str]: ...
    def sizes(self, prefix: str) -> dict[str, int]:
        """Every key under `prefix`, with its size in bytes."""
        ...
    def copy(self, src: str, dst: str, content_type: str) -> None: ...
    def presign_put(self, key: str, content_type: str, size: int, expires: int) -> str:
        """A URL that lets anyone holding it PUT exactly `size` bytes of `content_type` to `key`, until it expires."""
        ...
    def delete_many(self, keys: list[str]) -> None: ...


class MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[str | bytes, str]] = {}  # key -> (body, content_type); uploads are bytes
        self.presigned: dict[str, tuple[str, str, int]] = {}  # url -> (key, content_type, size)

    def get(self, key):
        obj = self.objects.get(key)
        if obj is None:
            return None
        return obj[0].decode("utf-8") if isinstance(obj[0], bytes) else obj[0]

    def put(self, key, body, content_type):
        self.objects[key] = (body, content_type)

    def put_if_absent(self, key, body, content_type):
        if key in self.objects:
            return False
        self.objects[key] = (body, content_type)
        return True

    def list(self, prefix):
        return sorted(k for k in self.objects if k.startswith(prefix))

    def sizes(self, prefix):
        return {k: len(b if isinstance(b, bytes) else b.encode("utf-8")) for k, (b, _) in self.objects.items() if k.startswith(prefix)}

    def copy(self, src, dst, content_type):
        self.objects[dst] = (self.objects[src][0], content_type)

    def presign_put(self, key, content_type, size, expires):
        url = f"memory://upload/{len(self.presigned)}"
        self.presigned[url] = (key, content_type, size)
        return url

    def upload(self, url: str, body: bytes, content_type: str) -> bool:
        """What S3 does when someone PUTs to a presigned URL: refuse anything the signature doesn't cover."""
        if url not in self.presigned:
            return False
        key, signed_type, size = self.presigned[url]
        if content_type != signed_type or len(body) != size:
            return False
        self.objects[key] = (body, content_type)
        return True

    def delete_many(self, keys):
        for k in keys:
            self.objects.pop(k, None)


class S3Store:
    def __init__(self, bucket: str, client=None) -> None:
        self.bucket = bucket
        # SigV4 so presigned URLs can sign Content-Type and Content-Length; virtual-hosted URLs
        # (bucket.s3.<region>.amazonaws.com) because path-style is deprecated.
        self.s3 = client or boto3.client("s3", config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))

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

    def sizes(self, prefix):
        out: dict[str, int] = {}
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=prefix):
            out.update((o["Key"], o["Size"]) for o in page.get("Contents", []))
        return out

    def copy(self, src, dst, content_type):
        # Server-side copy (up to 5 GB): the bytes never pass through the Lambda.
        self.s3.copy_object(
            Bucket=self.bucket,
            Key=dst,
            CopySource={"Bucket": self.bucket, "Key": src},
            ContentType=content_type,
            MetadataDirective="REPLACE",
        )

    def presign_put(self, key, content_type, size, expires):
        # Content-Type and Content-Length are signed, so S3 refuses any other type or size.
        return self.s3.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type, "ContentLength": size},
            ExpiresIn=expires,
        )

    def delete_many(self, keys):
        for i in range(0, len(keys), 1000):  # S3 deletes at most 1000 keys per call
            chunk = keys[i : i + 1000]
            self.s3.delete_objects(
                Bucket=self.bucket, Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True}
            )
