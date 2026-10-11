"""The HTTP API that full-stack apps call from the browser, on the same origin as the app.

    GET    /api/full-stack/<app>/data/<collection>           200 {collection, records}
    POST   /api/full-stack/<app>/data/<collection>           201 {collection, record, records}   body {data: {...}}
    PATCH  /api/full-stack/<app>/data/<collection>/<id>      200 {collection, record, records}
              body {data: {...}}                  shallow merge   (add replace: true to overwrite)
              body {increment: {field: n}}        atomic add, for counters and votes
    DELETE /api/full-stack/<app>/data/<collection>/<id>      204

A record is {id, data, createdAt, updatedAt}; the app's own fields live under `data`.

Anyone who can open an app can read and write its data: the data is exactly as public as the app.
What keeps that safe is the limits (see datastore.py), the per-app opt-in (`appType: full-stack`),
and refusing browser requests that come from other websites.
"""

import json
import logging
import math
import re
from typing import Callable

from . import datastore as ds
from .datastore import DataError, DataStore
from .registry import AppRegistry
from .validate import COLLECTION_RE, FIELD_RE, RodneyError, validate_slug

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024
MAX_DEPTH = 8
MAX_INCREMENT_FIELDS = 10

_PATH = re.compile(r"/api/full-stack/([^/]+)/data/([^/]+)(?:/([^/]+))?/?")
_ID_RE = re.compile(r"[0-9a-f]{20}")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _reply(status: int, body: dict | None = None, headers: dict | None = None) -> dict:
    h = {
        "cache-control": "no-store",
        "x-content-type-options": "nosniff",
        "content-security-policy": "default-src 'none'; frame-ancestors 'none'",
        **(headers or {}),
    }
    if body is None:
        return {"statusCode": status, "headers": h, "body": ""}
    h["content-type"] = "application/json"
    return {"statusCode": status, "headers": h, "body": json.dumps(body, separators=(",", ":"), ensure_ascii=False)}


def _error(e: ApiError) -> dict:
    return _reply(e.status, {"error": e.code, "message": e.message}, {"retry-after": "1"} if e.status == 503 else None)


# --- validating what apps send ------------------------------------------------------------------


def _reject_constant(name: str):
    raise ValueError(f"{name} is not valid JSON")


def _check_value(v, depth: int = 0) -> None:
    if depth > MAX_DEPTH:
        raise ApiError(400, "invalid_data", f"Data can be nested at most {MAX_DEPTH} levels deep.")
    if v is None or isinstance(v, (bool, str)):
        return
    if isinstance(v, int):
        if abs(v) > 2**53:
            raise ApiError(400, "invalid_data", "Whole numbers must be within ±9007199254740992.")
    elif isinstance(v, float):
        if not math.isfinite(v) or abs(v) > 1e15 or (v != 0 and abs(v) < 1e-15):
            raise ApiError(400, "invalid_data", "Numbers must be finite, between 1e-15 and 1e15 in size.")
    elif isinstance(v, list):
        for item in v:
            _check_value(item, depth + 1)
    elif isinstance(v, dict):
        for key, item in v.items():
            if not isinstance(key, str) or not 1 <= len(key) <= 200:
                raise ApiError(400, "invalid_data", "Field names must be 1-200 characters.")
            _check_value(item, depth + 1)
    else:
        raise ApiError(400, "invalid_data", "Only JSON values are allowed.")


def validate_data(data) -> dict:
    if not isinstance(data, dict):
        raise ApiError(400, "invalid_data", "`data` must be a JSON object.")
    _check_value(data)
    if ds.encoded_size(data) > ds.MAX_RECORD_BYTES:
        raise ApiError(413, "record_too_large", f"A record can be at most {ds.MAX_RECORD_BYTES // 1024} KB.")
    return data


def _parse_body(body: str) -> dict:
    if len(body.encode("utf-8")) > MAX_BODY_BYTES:
        raise ApiError(413, "body_too_large", f"The request body can be at most {MAX_BODY_BYTES // 1024} KB.")
    try:
        parsed = json.loads(body, parse_constant=_reject_constant)
    except ValueError:
        raise ApiError(400, "invalid_json", "The request body must be valid JSON.") from None
    if not isinstance(parsed, dict):
        raise ApiError(400, "invalid_json", "The request body must be a JSON object.")
    return parsed


def _parse_increment(raw) -> dict:
    if not isinstance(raw, dict) or not 1 <= len(raw) <= MAX_INCREMENT_FIELDS:
        raise ApiError(400, "invalid_increment", f"`increment` must be an object with 1-{MAX_INCREMENT_FIELDS} fields.")
    for field, amount in raw.items():
        if not FIELD_RE.fullmatch(field):
            raise ApiError(400, "invalid_increment", f'"{field}" is not a valid field name (letters, digits and _).')
        ok = isinstance(amount, (int, float)) and not isinstance(amount, bool) and math.isfinite(amount) and abs(amount) <= 1e12
        if not ok:
            raise ApiError(400, "invalid_increment", f'The amount for "{field}" must be a number up to 1e12.')
    return raw


# --- the API ------------------------------------------------------------------------------------


class DataApi:
    def __init__(self, registry: AppRegistry, store: DataStore, clock: Callable[[], str]) -> None:
        self.registry = registry
        self.store = store
        self.now = clock  # returns an ISO timestamp

    def handle(self, method: str, path: str, headers: dict, body: str) -> dict:
        try:
            return self._handle(method, path, headers, body or "")
        except ApiError as e:
            return _error(e)
        except ds.NotFound as e:
            return _error(ApiError(404, "not_found", str(e)))
        except ds.QuotaExceeded as e:
            return _error(ApiError(409, "quota_exceeded", str(e)))
        except ds.NotANumber as e:
            return _error(ApiError(400, "not_a_number", str(e)))
        except ds.Busy as e:
            return _error(ApiError(503, "busy", str(e)))
        except DataError as e:
            return _error(ApiError(400, "bad_request", str(e)))

    def _handle(self, method: str, path: str, headers: dict, body: str) -> dict:
        match = _PATH.fullmatch(path)
        if not match:
            raise ApiError(404, "not_found", "No such endpoint.")
        slug, collection, rid = match.groups()
        try:
            validate_slug(slug)
        except RodneyError:
            raise ApiError(404, "not_found", "No such app.") from None
        if not COLLECTION_RE.fullmatch(collection):
            raise ApiError(400, "invalid_collection", "Collection names are lowercase letters, digits, - and _, starting with a letter (max 32).")
        if rid is not None and not _ID_RE.fullmatch(rid):
            raise ApiError(404, "not_found", "No such record.")

        allowed = ("PATCH", "DELETE") if rid else ("GET", "POST")
        if method not in allowed:
            return _reply(405, {"error": "method_not_allowed", "message": f"Use {' or '.join(allowed)} here."}, {"allow": ", ".join(allowed)})
        self._same_origin_only(method, headers)

        meta = self.registry.get(slug)
        if meta is None:
            raise ApiError(404, "not_found", "No such app.")
        if meta.get("appType") != "full-stack":
            raise ApiError(403, "no_backend", "This app doesn't have a backend. Ask for a full stack app.")

        if method == "GET":
            return _reply(200, {"collection": collection, "records": self.store.list(slug, collection)})
        if method == "POST":
            data = validate_data(_parse_body(body).get("data"))
            record = self.store.create(slug, collection, data, self.now())
            log.info("data create app=%s collection=%s", slug, collection)
            return self._result(slug, collection, record, 201)
        if method == "DELETE":
            self.store.delete(slug, collection, rid)
            log.info("data delete app=%s collection=%s", slug, collection)
            return _reply(204)
        return self._patch(slug, collection, rid, _parse_body(body))

    def _patch(self, slug: str, collection: str, rid: str, payload: dict) -> dict:
        has_data, has_inc = "data" in payload, "increment" in payload
        if has_data == has_inc:
            raise ApiError(400, "invalid_request", "Send either `data` (to change fields) or `increment` (to add to numbers), not both.")
        if has_inc:
            if "replace" in payload:
                raise ApiError(400, "invalid_request", "`replace` only applies to `data`.")
            record = self.store.increment(slug, collection, rid, _parse_increment(payload["increment"]), self.now())
        else:
            patch = validate_data(payload["data"])
            replace = payload.get("replace", False)
            if not isinstance(replace, bool):
                raise ApiError(400, "invalid_request", "`replace` must be true or false.")
            record = self.store.update(
                slug, collection, rid, lambda old: validate_data(patch if replace else {**old, **patch}), self.now()
            )
        log.info("data update app=%s collection=%s", slug, collection)
        return self._result(slug, collection, record, 200)

    def _result(self, slug: str, collection: str, record: dict, status: int) -> dict:
        return _reply(status, {"collection": collection, "record": record, "records": self.store.list(slug, collection)})

    @staticmethod
    def _same_origin_only(method: str, headers: dict) -> None:
        """Browsers label every request with where it came from (and scripts can't forge it).
        Pages on this site are `same-origin`; refuse requests from other websites. Tools like
        curl send no label and are let through: this stops drive-by use from elsewhere, nothing more."""
        site = headers.get("sec-fetch-site")
        if site is None or site == "same-origin" or (site == "none" and method == "GET"):
            return
        raise ApiError(403, "cross_site", "This API can only be used from the app's own page.")
