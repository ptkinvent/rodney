"""All Rodney operations. Writes take the signed-in author; reads are open to any signed-in user."""

import json
import logging
from datetime import datetime, timezone

from .admin_page import render_admin_page
from .store import Store
from .validate import (
    RodneyError,
    content_type_for,
    validate_content,
    validate_file_path,
    validate_slug,
)

log = logging.getLogger(__name__)

JSON_TYPE = "application/json; charset=utf-8"
MAX_APPS_PER_AUTHOR = 50  # a modest brake on abuse; raise it if real users hit it


def _meta_key(slug: str) -> str:
    return f"_meta/{slug}.json"


def _index_key(author: str, slug: str) -> str:
    """An empty marker object per app, so one cheap list call counts an author's apps."""
    return f"_authors/{author}/{slug}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Apps:
    def __init__(self, store: Store, base_url: str) -> None:
        self.store = store
        self.base_url = base_url

    def urls(self, slug: str) -> dict:
        return {"live": f"{self.base_url}/{slug}/", "admin": f"{self.base_url}/{slug}/admin/"}

    def _load_meta(self, slug: str) -> dict:
        raw = self.store.get(_meta_key(slug))
        if raw is None:
            raise RodneyError(f'App "{slug}" not found.')
        return json.loads(raw)

    def _load_owned(self, author: str, name) -> tuple[str, dict]:
        """Loads the app and checks that the caller is its author."""
        slug = validate_slug(name)
        meta = self._load_meta(slug)
        if meta["author"] != author:
            raise RodneyError(f'"{slug}" was created by {meta["author"]}; only they can change it.')
        return slug, meta

    def create_app(self, author: str, name, description=None, html=None, files=None) -> dict:
        slug = validate_slug(name)

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

        if len(self.store.list(f"_authors/{author}/")) >= MAX_APPS_PER_AUTHOR:
            raise RodneyError(f"You already have {MAX_APPS_PER_AUTHOR} apps. Delete one before creating another.")

        # Claim the name atomically *before* writing anything else.
        now = _now()
        meta = {"name": slug, "author": author, "createdAt": now, "updatedAt": now}
        if description:
            meta["description"] = description
        if not self.store.put_if_absent(_meta_key(slug), json.dumps(meta), JSON_TYPE):
            raise RodneyError(f'An app named "{slug}" already exists. Pick another name.')

        self.store.put(_index_key(author, slug), "", "text/plain")
        for path, content in validated:
            self.store.put(f"{slug}/{path}", content, content_type_for(path))
        self.store.put(f"{slug}/admin/index.html", render_admin_page(slug, author), content_type_for("x.html"))
        log.info("audit create-app author=%s app=%s files=%d", author, slug, len(validated))
        return {"meta": meta, **self.urls(slug)}

    def get_app(self, name) -> dict:
        slug = validate_slug(name)
        meta = self._load_meta(slug)
        prefix = f"{slug}/"
        files = [k[len(prefix):] for k in self.store.list(prefix)]
        files = [f for f in files if not f.startswith("admin/")]
        return {"meta": meta, "files": files, **self.urls(slug)}

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
        meta["updatedAt"] = _now()
        self.store.put(_meta_key(slug), json.dumps(meta), JSON_TYPE)
        log.info("audit write-file author=%s app=%s path=%s", author, slug, path)
        return {"path": path, **self.urls(slug)}

    def delete_app(self, author: str, name) -> dict:
        slug, _ = self._load_owned(author, name)
        keys = self.store.list(f"{slug}/")
        self.store.delete_many([*keys, _meta_key(slug), _index_key(author, slug)])
        log.info("audit delete-app author=%s app=%s", author, slug)
        return {"deleted": slug, "files": len(keys)}

    def list_apps(self) -> list[dict]:
        metas = [self.store.get(k) for k in self.store.list("_meta/")]
        return [{**json.loads(m), **self.urls(json.loads(m)["name"])} for m in metas if m is not None]

    def reassign(self, name, new_author: str) -> dict:
        """Operator-only (not an MCP tool): hands an app to another author."""
        slug = validate_slug(name)
        meta = self._load_meta(slug)
        old_author = meta["author"]
        meta["author"], meta["updatedAt"] = new_author, _now()
        self.store.put(_meta_key(slug), json.dumps(meta), JSON_TYPE)
        self.store.put(f"{slug}/admin/index.html", render_admin_page(slug, new_author), content_type_for("x.html"))
        self.store.delete_many([_index_key(old_author, slug)])
        self.store.put(_index_key(new_author, slug), "", "text/plain")
        log.info("audit reassign app=%s from=%s to=%s", slug, old_author, new_author)
        return meta
