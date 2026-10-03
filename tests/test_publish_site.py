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


def test_only_root_files_that_cannot_be_app_slugs():
    from rodney.validate import validate_slug
    from rodney.validate import RodneyError

    for key in publish_site.objects():
        assert "/" not in key
        try:
            validate_slug(key)
        except RodneyError:
            continue
        raise AssertionError(f"{key} could collide with an app slug")


def test_landing_page_points_at_real_endpoints():
    html = (publish_site.ROOT / "site" / "index.html").read_text()
    assert "https://mcp.ptkinvent.com/mcp" in html
    skill = (publish_site.ROOT / "SKILL.md").read_text()
    assert f'href="/{publish_site.skill_name(skill)}.zip"' in html and 'href="/SKILL.md"' in html
