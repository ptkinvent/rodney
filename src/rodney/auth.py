"""Who is allowed in: a hard-coded map of author name -> Google email."""

import json

Users = dict[str, str]  # author name -> Google email


def author_for_email(users: Users, email: str) -> str | None:
    wanted = email.strip().lower()
    for author, allowed in users.items():
        if allowed.strip().lower() == wanted:
            return author
    return None


def parse_users(raw: str | None) -> Users:
    if not raw:
        raise RuntimeError("RODNEY_USERS is not set")
    parsed = json.loads(raw)
    if not isinstance(parsed, dict) or not parsed or not all(isinstance(v, str) and "@" in v for v in parsed.values()):
        raise RuntimeError("RODNEY_USERS must be a non-empty JSON object of author -> email")
    return parsed
