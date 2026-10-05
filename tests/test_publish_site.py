import io
import re
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import publish_site  # noqa: E402


def test_skill_zip_layout_matches_skill_name():
    skill = (publish_site.ROOT / "SKILL.md").read_text()
    names = zipfile.ZipFile(io.BytesIO(publish_site.build_skill_zip(skill))).namelist()
    skill_name = re.search(r"^name:\s*(\S+)", skill, re.M).group(1)
    assert names == [f"{skill_name}/SKILL.md"]


def test_published_keys_cannot_collide_with_apps():
    keys = set(publish_site.objects())
    assert {"index.html", "legal.css", "SKILL.md", "privacy/index.html", "terms/index.html"} <= keys
    for key in keys:
        publish_site.check_key(key)  # raises if an app could own it


def test_check_key_rejects_collisions():
    import pytest

    for bad in ["todo/index.html", "about", "x/y/z.html", "my-app"]:
        with pytest.raises(SystemExit):
            publish_site.check_key(bad)
    for ok in ["index.html", "privacy/index.html", "terms/index.html", "SKILL.md", "rodney.zip"]:
        publish_site.check_key(ok)


def test_landing_page_points_at_real_endpoints():
    html = (publish_site.ROOT / "site" / "index.html").read_text()
    assert "https://mcp.ptkinvent.com/mcp" in html
    skill = (publish_site.ROOT / "SKILL.md").read_text()
    assert f'href="/{publish_site.skill_name(skill)}.zip"' in html and 'href="/SKILL.md"' in html


def test_legal_pages_are_complete_and_match_what_rodney_does():
    root = publish_site.ROOT / "site"
    privacy = (root / "privacy" / "index.html").read_text()
    terms = (root / "terms" / "index.html").read_text()
    for page in (privacy, terms):
        assert "mailto:ptkinvent@gmail.com" in page and 'href="/legal.css"' in page
    # claims that must stay true of the implementation
    assert "90 days" in privacy and "openid" in privacy and "rodney_login" in privacy
    from rodney.web import LOGIN_COOKIE, LOGIN_TTL, SESSION_COOKIE, SESSION_TTL
    assert SESSION_COOKIE in privacy and LOGIN_COOKIE in privacy and "14 days" in privacy
    assert SESSION_TTL == 14 * 24 * 3600 and LOGIN_TTL == 600
    assert "handle and your email address" in privacy and "never your email" not in privacy.split("Cookies")[1]
    assert "50 pages" in terms
    from rodney.apps import MAX_APPS_PER_AUTHOR
    from rodney.oauth import CODE_TTL, COOKIE, REFRESH_TTL
    assert MAX_APPS_PER_AUTHOR == 50 and COOKIE == "rodney_login"
    assert REFRESH_TTL == 30 * 24 * 3600 and CODE_TTL == 120


def test_landing_page_has_a_sign_in_button_to_the_website():
    html = (publish_site.ROOT / "site" / "index.html").read_text()
    assert 'class="signin" href="https://app.ptkinvent.com/login"' in html


def test_landing_page_links_to_legal_pages():
    html = (publish_site.ROOT / "site" / "index.html").read_text()
    assert 'href="/privacy/"' in html and 'href="/terms/"' in html
