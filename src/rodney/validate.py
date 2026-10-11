"""Input validation. Everything an agent sends us is untrusted, so check it here."""

import re

# Names that would collide with our own routes (/mcp) or generated pages (/<slug>/admin).
RESERVED_SLUGS = {"mcp", "admin", "assets", "api", "static", "privacy", "terms"}

MAX_FILE_BYTES = 4 * 1024 * 1024  # Lambda's request limit is ~6MB, so stay under it.

# 1-40 chars; lowercase letters, digits, dashes; can't start or end with a dash.
_SLUG_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?")
_PATH_CHARS_RE = re.compile(r"[A-Za-z0-9._/-]+")

_CONTENT_TYPES = {
    "html": "text/html; charset=utf-8",
    "htm": "text/html; charset=utf-8",
    "css": "text/css; charset=utf-8",
    "js": "text/javascript; charset=utf-8",
    "mjs": "text/javascript; charset=utf-8",
    "json": "application/json; charset=utf-8",
    "svg": "image/svg+xml",
    "txt": "text/plain; charset=utf-8",
    "md": "text/plain; charset=utf-8",
    "xml": "application/xml; charset=utf-8",
    "csv": "text/csv; charset=utf-8",
}


class RodneyError(Exception):
    """An error whose message is safe to show to the agent (as opposed to a bug)."""


def validate_slug(slug: object) -> str:
    # fullmatch, not match: Python's `$` would let a trailing newline through.
    if not isinstance(slug, str) or not _SLUG_RE.fullmatch(slug):
        raise RodneyError(
            "App name must be 1-40 chars: lowercase letters, numbers and dashes, "
            "not starting or ending with a dash."
        )
    if slug in RESERVED_SLUGS:
        raise RodneyError(f'"{slug}" is a reserved name.')
    return slug


COLLECTION_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
FIELD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def validate_collection(name: object) -> str:
    if not isinstance(name, str) or not COLLECTION_RE.fullmatch(name):
        raise RodneyError(
            "Collection names are lowercase letters, digits, - and _, starting with a letter (max 32 characters)."
        )
    return name


def validate_field(name: object) -> str:
    if not isinstance(name, str) or not FIELD_RE.fullmatch(name):
        raise RodneyError("Field names are letters, digits and _, starting with a letter or _ (max 64 characters).")
    return name


def validate_file_path(path: object) -> str:
    """Validates a path inside an app, e.g. `index.html` or `css/style.css`."""
    if not isinstance(path, str) or not 0 < len(path) <= 200:
        raise RodneyError("File path must be a non-empty string up to 200 chars.")
    segments = path.split("/")
    # No empty segments (leading slash, `//`), no `.`/`..`, no dotfiles.
    if any(s == "" or s.startswith(".") for s in segments):
        raise RodneyError(
            f'Invalid file path "{path}": no empty, dot or dotfile segments, no leading slash.'
        )
    if not _PATH_CHARS_RE.fullmatch(path):
        raise RodneyError(
            f'Invalid file path "{path}": use letters, numbers, dot, dash, underscore and slash only.'
        )
    if segments[0] == "admin":
        raise RodneyError('The "admin" path is reserved for the generated admin page.')
    return path


def validate_content(content: object, path: str) -> str:
    if not isinstance(content, str):
        raise RodneyError(f"Content for {path} must be a string.")
    if len(content.encode("utf-8")) > MAX_FILE_BYTES:
        raise RodneyError(f"{path} is larger than {MAX_FILE_BYTES // 1024 // 1024}MB.")
    return content


def content_type_for(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return _CONTENT_TYPES.get(ext, "text/plain; charset=utf-8")
