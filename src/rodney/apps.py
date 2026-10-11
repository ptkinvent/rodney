"""All Rodney operations. Writes take the signed-in author; reads are open to any signed-in user."""

import logging
from datetime import datetime, timezone

from .admin_page import render_admin_page
from .datastore import DataStore
from .registry import AppRegistry
from .store import Store
from .validate import (
    RodneyError,
    content_type_for,
    validate_collection,
    validate_content,
    validate_field,
    validate_file_path,
    validate_slug,
)

log = logging.getLogger(__name__)

MAX_APPS_PER_AUTHOR = 50  # a modest brake on abuse; raise it if real users hit it
DEFAULT_COLLECTION = "records"
DEFAULT_PRIMARY_FIELD = "value"
PUBLIC_DATA_NOTICE = (
    "Anyone who has the link to this app can read and change its data. "
    "Don't store anything private in it."
)
READ_DATA_MAX_RECORDS = 50
READ_DATA_MAX_CHARS = 100_000

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Apps:
    def __init__(self, store: Store, registry: AppRegistry, data: DataStore, base_url: str) -> None:
        self.store = store  # the apps' files (S3)
        self.registry = registry  # who owns each app (DynamoDB)
        self.data = data  # full-stack apps' records (DynamoDB)
        self.base_url = base_url

    def urls(self, slug: str) -> dict:
        return {"live": f"{self.base_url}/{slug}/", "admin": f"{self.base_url}/{slug}/admin/"}

    def _data_info(self, slug: str, meta: dict) -> dict:
        """What a full-stack app needs to know to use its backend (empty for static apps)."""
        if meta.get("appType") != "full-stack":
            return {}
        collection = meta.get("collectionName", DEFAULT_COLLECTION)
        return {"dataApi": f"/api/full-stack/{slug}/data/{collection}", "notice": PUBLIC_DATA_NOTICE}

    def _load_meta(self, slug: str) -> dict:
        meta = self.registry.get(slug)
        if meta is None:
            raise RodneyError(f'App "{slug}" not found.')
        return meta

    def _load_owned(self, author: str, name) -> tuple[str, dict]:
        """Loads the app and checks that the caller is its author."""
        slug = validate_slug(name)
        meta = self._load_meta(slug)
        if meta["author"] != author:
            raise RodneyError(f'"{slug}" was created by {meta["author"]}; only they can change it.')
        return slug, meta

    def create_app(self, author: str, name, description=None, html=None, files=None, full_stack=None,
                   template=None, collection_name=None, primary_field=None) -> dict:
        slug = validate_slug(name)
        if full_stack is not None and not isinstance(full_stack, bool):
            raise RodneyError("`fullStack` must be true or false.")
        if full_stack and template not in (None, "crud"):
            raise RodneyError('Only the "crud" template is supported.')
        settings = {}
        if full_stack:  # collectionName, primaryField and template are ignored for static apps
            settings = {
                "appType": "full-stack",
                "template": "crud",
                "collectionName": validate_collection(collection_name or DEFAULT_COLLECTION),
                "primaryField": validate_field(primary_field or DEFAULT_PRIMARY_FIELD),
            }

        if html is not None and files is not None:
            raise RodneyError("Provide either `html` or `files`, not both.")
        if html is not None:
            files = [{"path": "index.html", "content": html}]
        if not isinstance(files, list) or not all(isinstance(f, dict) for f in files):
            raise RodneyError("`files` must be a list of {path, content} objects.")

        validated = []
        for f in files:
            path = validate_file_path(f.get("path"))
            validated.append((path, validate_content(f.get("content"), path)))
        paths = [p for p, _ in validated]
        if "index.html" not in paths:
            raise RodneyError("An app needs an index.html (pass `html`, or include it in `files`).")
        if len(set(paths)) != len(paths):
            raise RodneyError("Duplicate file paths.")

        if self.registry.count_by_author(author) >= MAX_APPS_PER_AUTHOR:
            raise RodneyError(f"You already have {MAX_APPS_PER_AUTHOR} apps. Delete one before creating another.")

        # Claim the name atomically *before* writing anything else.
        now = _now()
        meta = {"name": slug, "author": author, "createdAt": now, "updatedAt": now, **settings}
        if description:
            meta["description"] = description
        if not self.registry.create(meta):
            raise RodneyError(f'An app named "{slug}" already exists. Pick another name.')

        for path, content in validated:
            self.store.put(f"{slug}/{path}", content, content_type_for(path))
        self.store.put(f"{slug}/admin/index.html", render_admin_page(slug, author), content_type_for("x.html"))
        log.info("audit create-app author=%s app=%s files=%d full_stack=%s", author, slug, len(validated), bool(settings))
        return {"meta": meta, **self.urls(slug), **self._data_info(slug, meta)}

    def get_app(self, name) -> dict:
        slug = validate_slug(name)
        meta = self._load_meta(slug)
        prefix = f"{slug}/"
        files = [k[len(prefix):] for k in self.store.list(prefix)]
        files = [f for f in files if not f.startswith("admin/")]
        info = {**self._data_info(slug, meta)}
        if info:
            info["collections"] = self.data.collections(slug)
        return {"meta": meta, "files": files, **self.urls(slug), **info}

    def read_file(self, name, path) -> str:
        slug = validate_slug(name)
        path = validate_file_path(path)
        self._load_meta(slug)
        content = self.store.get(f"{slug}/{path}")
        if content is None:
            raise RodneyError(f'No file "{path}" in "{slug}".')
        return content

    def write_file(self, author: str, name, path, content) -> dict:
        slug, meta = self._load_owned(author, name)
        path = validate_file_path(path)
        content = validate_content(content, path)
        self.store.put(f"{slug}/{path}", content, content_type_for(path))
        self.registry.touch(slug, _now())
        log.info("audit write-file author=%s app=%s path=%s", author, slug, path)
        return {"path": path, **self.urls(slug)}

    def delete_app(self, author: str, name) -> dict:
        slug, _ = self._load_owned(author, name)
        records = self.data.delete_app(slug)  # first: an app's data must never outlive the app
        keys = self.store.list(f"{slug}/")
        self.store.delete_many(keys)
        self.registry.delete(slug)
        log.info("audit delete-app author=%s app=%s records=%d", author, slug, records)
        return {"deleted": slug, "files": len(keys), "records": records}

    def list_apps_by(self, author: str) -> list[dict]:
        return [{**meta, **self.urls(meta["name"])} for meta in self.registry.list_by_author(author)]

    def set_settings(self, author: str, name, full_stack=None, collection_name=None, primary_field=None) -> dict:
        """Owner-only: turn the backend on or off (data is kept when it's off) or rename its defaults."""
        slug, meta = self._load_owned(author, name)
        if full_stack is not None and not isinstance(full_stack, bool):
            raise RodneyError("`fullStack` must be true or false.")
        if full_stack is None and collection_name is None and primary_field is None:
            raise RodneyError("Nothing to change: pass `fullStack`, `collectionName` or `primaryField`.")
        fields = {}
        if full_stack is True:
            fields["appType"] = "full-stack"
            fields.setdefault("template", "crud")
            fields["collectionName"] = meta.get("collectionName", DEFAULT_COLLECTION)
            fields["primaryField"] = meta.get("primaryField", DEFAULT_PRIMARY_FIELD)
        elif full_stack is False:
            fields["appType"] = "static"
        if collection_name is not None:
            fields["collectionName"] = validate_collection(collection_name)
        if primary_field is not None:
            fields["primaryField"] = validate_field(primary_field)
        self.registry.set_fields(slug, fields, _now())
        log.info("audit set-settings author=%s app=%s fields=%s", author, slug, sorted(fields))
        updated = self._load_meta(slug)
        return {"meta": updated, **self.urls(slug), **self._data_info(slug, updated)}

    def read_data(self, author: str, name, collection=None) -> dict:
        """Owner-only: a look at an app's stored data (a collection is cut off at a sensible size)."""
        slug, _ = self._load_owned(author, name)
        summary = self.data.collections(slug)
        if collection is None:
            return {"collections": summary}
        records = self.data.list(slug, validate_collection(collection))
        shown, chars = [], 0
        for record in records[:READ_DATA_MAX_RECORDS]:
            chars += len(str(record))
            if chars > READ_DATA_MAX_CHARS:
                break
            shown.append(record)
        return {"collection": collection, "total": len(records), "shown": len(shown), "records": shown}

    def clear_data(self, author: str, name, collection=None) -> dict:
        """Owner-only and destructive: erase one collection, or all of an app's data."""
        slug, _ = self._load_owned(author, name)
        if collection is None:
            erased = self.data.delete_app(slug)
        else:
            erased = self.data.clear(slug, validate_collection(collection))
        log.info("audit clear-data author=%s app=%s collection=%s records=%d", author, slug, collection, erased)
        return {"app": slug, "collection": collection, "recordsErased": erased}

    def reassign(self, name, new_author: str) -> dict:
        """Operator-only (not an MCP tool): hands an app to another author."""
        slug = validate_slug(name)
        meta = self._load_meta(slug)
        self.registry.set_author(slug, new_author, _now())
        self.store.put(f"{slug}/admin/index.html", render_admin_page(slug, new_author), content_type_for("x.html"))
        log.info("audit reassign app=%s from=%s to=%s", slug, meta["author"], new_author)
        return self._load_meta(slug)
