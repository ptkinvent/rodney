"""Who a signed-in Google account is, as far as Rodney is concerned.

Anyone with a Google account may sign in. Each person becomes an *author*, identified by a
short anonymous handle derived from Google's stable account id, so apps and admin pages
never expose an email address. Individual authors can be blocked.
"""

import hashlib


def author_id(google_sub: str) -> str:
    """A stable, anonymous handle such as `u-3f9a1c2b7e` for a Google account."""
    return "u-" + hashlib.sha256(f"rodney:{google_sub}".encode("utf-8")).hexdigest()[:10]


def parse_blocked(raw: str | None) -> frozenset[str]:
    """RODNEY_BLOCKED is a comma-separated list of author handles."""
    return frozenset(part.strip() for part in (raw or "").split(",") if part.strip())
