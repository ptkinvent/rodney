"""All Rodney operations. Writes take the signed-in author; reads are open to any signed-in user."""

import json
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

JSON_TYPE = "application/json; charset=utf-8"


def _meta_key(slug: str) -> str:
    return f"_meta/{slug}.json"


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

        # Claim the name atomically *before* writing anything else.
        now = _now()
        meta = {"name": slug, "author": author, "createdAt": now, "updatedAt": now}
        if description:
            meta["description"] = description
        if not self.store.put_if_absent(_meta_key(slug), json.dumps(meta), JSON_TYPE):
            raise RodneyError(f'An app named "{slug}" already exists. Pick another name.')

        for path, content in validated:
            self.store.put(f"{slug}/{path}", content, content_type_for(path))
        self.store.put(f"{slug}/admin/index.html", render_admin_page(slug, author), content_type_for("x.html"))
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
        return {"path": path, **self.urls(slug)}

    def delete_app(self, author: str, name) -> dict:
        slug, _ = self._load_owned(author, name)
        keys = self.store.list(f"{slug}/")
        self.store.delete_many([*keys, _meta_key(slug)])
        return {"deleted": slug, "files": len(keys)}

    def list_apps(self) -> list[dict]:
        metas = [self.store.get(k) for k in self.store.list("_meta/")]
        return [{**json.loads(m), **self.urls(json.loads(m)["name"])} for m in metas if m is not None]
