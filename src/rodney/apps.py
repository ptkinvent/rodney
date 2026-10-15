"""All Rodney operations. Writes take the signed-in author; reads are open to any signed-in user."""

import hashlib
import logging
from datetime import datetime, timezone

from .admin_page import render_admin_page
from .datastore import DataStore
from .registry import AppRegistry
from .store import Store
from .validate import (
    MAX_UPLOAD_BYTES,
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
UPLOAD_URL_TTL = 15 * 60  # seconds a presigned upload URL stays valid
MAX_UPLOAD_FILES = 200  # per app
# Staged uploads live under `_uploads/`, which CloudFront never serves and a lifecycle rule
# empties after a day (infra/stack.py). Nothing there is public until finalize-upload copies it.
UPLOAD_PREFIX = "_uploads"

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Apps:
    def __init__(self, store: Store, registry: AppRegistry, data: DataStore, base_url: str) -> None:
        self.store = store  # the apps' files (S3)
        self.registry = registry  # who owns each app (DynamoDB)
        self.data = data  # full-stack apps' records (DynamoDB)
        self.base_url = base_url

    def urls(self, slug: str) -> dict:
        # No trailing slash, like the original Spin's links. CloudFront redirects them to the slashed
        # form (infra/stack.py), which relative links inside the app need.
        return {"live": f"{self.base_url}/{slug}", "admin": f"{self.base_url}/{slug}/admin"}

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
        try:
            content = self.store.get(f"{slug}/{path}")
        except UnicodeDecodeError:
            raise RodneyError(f"{path} is a binary file, so it can't be shown as text.") from None
        if content is None:
            raise RodneyError(f'No file "{path}" in "{slug}".')
        return content

    def _staging(self, author: str, slug: str) -> str:
        """Where one author's uploads for one app wait. Keyed by author, so finalizing only ever
        sees the caller's own uploads. Hashed, so the key never depends on the handle's characters."""
        return f"{UPLOAD_PREFIX}/{hashlib.sha256(author.encode('utf-8')).hexdigest()[:16]}/{slug}/"

    def get_upload_url(self, author: str, name, path, size) -> dict:
        """Step 1 of a direct upload: a short-lived URL to PUT one file of a NEW app straight to S3."""
        slug = validate_slug(name)
        path = validate_file_path(path)
        if not isinstance(size, int) or isinstance(size, bool) or not 0 <= size <= MAX_UPLOAD_BYTES:
            raise RodneyError(f"`size` must be the file's size in bytes, at most {MAX_UPLOAD_BYTES // 1024 // 1024} MB.")
        # Checked before signing: once a URL exists, whoever holds it can write.
        if self.registry.get(slug) is not None:
            raise RodneyError(f'An app named "{slug}" already exists. Uploads create new apps; pick another name.')
        if self.registry.count_by_author(author) >= MAX_APPS_PER_AUTHOR:
            raise RodneyError(f"You already have {MAX_APPS_PER_AUTHOR} apps. Delete one before creating another.")
        staging = self._staging(author, slug)
        staged = self.store.list(staging)
        if len(staged) >= MAX_UPLOAD_FILES and f"{staging}{path}" not in staged:
            raise RodneyError(f"An app can have at most {MAX_UPLOAD_FILES} uploaded files.")
        content_type = content_type_for(path)
        url = self.store.presign_put(f"{staging}{path}", content_type, size, UPLOAD_URL_TTL)
        log.info("audit get-upload-url author=%s app=%s path=%s size=%d", author, slug, path, size)
        return {"uploadUrl": url, "contentType": content_type, "expiresIn": UPLOAD_URL_TTL}

    def finalize_upload(self, author: str, name, paths, description=None) -> dict:
        """Step 2 of a direct upload: check what arrived, claim the name, and publish the files."""
        slug = validate_slug(name)
        if not isinstance(paths, list) or not paths:
            raise RodneyError("`paths` must be the list of uploaded file paths.")
        paths = [validate_file_path(p) for p in paths]
        if len(set(paths)) != len(paths):
            raise RodneyError("Duplicate file paths.")
        if "index.html" not in paths:
            raise RodneyError("An app needs an index.html.")
        staging = self._staging(author, slug)
        sizes = self.store.sizes(staging)
        missing = [p for p in paths if f"{staging}{p}" not in sizes]
        if missing:
            raise RodneyError(f"Not uploaded (or the upload expired): {', '.join(missing)}. Get a new URL and upload again.")
        too_big = [p for p in paths if sizes[f"{staging}{p}"] > MAX_UPLOAD_BYTES]  # S3 enforces this too; belt and braces
        if too_big:
            raise RodneyError(f"Larger than {MAX_UPLOAD_BYTES // 1024 // 1024} MB: {', '.join(too_big)}.")
        if self.registry.count_by_author(author) >= MAX_APPS_PER_AUTHOR:
            raise RodneyError(f"You already have {MAX_APPS_PER_AUTHOR} apps. Delete one before creating another.")

        now = _now()
        meta = {"name": slug, "author": author, "createdAt": now, "updatedAt": now}
        if description:
            meta["description"] = description
        if not self.registry.create(meta):
            raise RodneyError(f'An app named "{slug}" already exists. Pick another name.')

        for path in paths:
            self.store.copy(f"{staging}{path}", f"{slug}/{path}", content_type_for(path))
        self.store.put(f"{slug}/admin/index.html", render_admin_page(slug, author), content_type_for("x.html"))
        self.store.delete_many(list(sizes))  # including anything uploaded but not listed
        log.info("audit finalize-upload author=%s app=%s files=%d", author, slug, len(paths))
        return {"meta": meta, **self.urls(slug)}

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

    def reassign(self, name, new_author: str) -> dict:
        """Operator-only (not an MCP tool): hands an app to another author."""
        slug = validate_slug(name)
        meta = self._load_meta(slug)
        self.registry.set_author(slug, new_author, _now())
        self.store.put(f"{slug}/admin/index.html", render_admin_page(slug, new_author), content_type_for("x.html"))
        log.info("audit reassign app=%s from=%s to=%s", slug, meta["author"], new_author)
        return self._load_meta(slug)
