"""All Rodney operations. Writes take the signed-in author; reads are open to any signed-in user."""

import logging
from datetime import datetime, timezone

from .admin_page import render_admin_page
from .registry import AppRegistry
from .store import Store
from .validate import (
    RodneyError,
    content_type_for,
    validate_content,
    validate_file_path,
    validate_slug,
)

log = logging.getLogger(__name__)

MAX_APPS_PER_AUTHOR = 50  # a modest brake on abuse; raise it if real users hit it


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Apps:
    def __init__(self, store: Store, registry: AppRegistry, base_url: str) -> None:
        self.store = store  # the apps' files (S3)
        self.registry = registry  # who owns each app (DynamoDB)
        self.base_url = base_url

    def urls(self, slug: str) -> dict:
        return {"live": f"{self.base_url}/{slug}/", "admin": f"{self.base_url}/{slug}/admin/"}

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

        if self.registry.count_by_author(author) >= MAX_APPS_PER_AUTHOR:
            raise RodneyError(f"You already have {MAX_APPS_PER_AUTHOR} apps. Delete one before creating another.")

        # Claim the name atomically *before* writing anything else.
        now = _now()
        meta = {"name": slug, "author": author, "createdAt": now, "updatedAt": now}
        if description:
            meta["description"] = description
        if not self.registry.create(meta):
            raise RodneyError(f'An app named "{slug}" already exists. Pick another name.')

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
        self.registry.touch(slug, _now())
        log.info("audit write-file author=%s app=%s path=%s", author, slug, path)
        return {"path": path, **self.urls(slug)}

    def delete_app(self, author: str, name) -> dict:
        slug, _ = self._load_owned(author, name)
        keys = self.store.list(f"{slug}/")
        self.store.delete_many(keys)
        self.registry.delete(slug)
        log.info("audit delete-app author=%s app=%s", author, slug)
        return {"deleted": slug, "files": len(keys)}

    def list_apps(self) -> list[dict]:
        return [{**meta, **self.urls(meta["name"])} for meta in self.registry.list_all()]

    def reassign(self, name, new_author: str) -> dict:
        """Operator-only (not an MCP tool): hands an app to another author."""
        slug = validate_slug(name)
        meta = self._load_meta(slug)
        self.registry.set_author(slug, new_author, _now())
        self.store.put(f"{slug}/admin/index.html", render_admin_page(slug, new_author), content_type_for("x.html"))
        log.info("audit reassign app=%s from=%s to=%s", slug, meta["author"], new_author)
        return self._load_meta(slug)
