"""Publishes the landing page, legal pages and the downloadable skill to the bucket root.

    .venv/bin/python scripts/publish_site.py [--dry-run] [--skip-skill]

Everything under site/ is uploaded at the same path (site/privacy/index.html becomes
privacy/index.html), plus SKILL.md and <skill name>.zip. Apps live under <slug>/, so every key
here must be either a root file (which can't be a valid slug) or inside a reserved slug.
"""

import argparse
import io
import re
import sys
import zipfile
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from rodney.validate import RESERVED_SLUGS, RodneyError, content_type_for, validate_slug  # noqa: E402

STACK = "Spin"  # CloudFormation stack id; unchanged by the rename to Rodney


def check_key(key: str) -> None:
    """Refuses any key an app could also own."""
    first, _, rest = key.partition("/")
    if rest:
        if first not in RESERVED_SLUGS:
            raise SystemExit(f"{key}: '{first}/' is not a reserved app name, so an app could collide with it")
        return
    try:
        validate_slug(key)
    except RodneyError:
        return  # not a valid slug (has a dot or capital), so no app can own it
    raise SystemExit(f"{key}: a root key that is also a valid app name")


def skill_name(skill_md: str) -> str:
    name = re.search(r"^name:\s*(\S+)\s*$", skill_md, re.M)
    if not name:
        raise SystemExit("SKILL.md has no `name:` in its frontmatter")
    return name.group(1)


def build_skill_zip(skill_md: str) -> bytes:
    """A zip holding `<skill name>/SKILL.md`, the layout Claude's skill upload expects."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(f"{skill_name(skill_md)}/SKILL.md", skill_md)
    return buf.getvalue()


def objects() -> dict[str, tuple[bytes, str]]:
    skill_md = (ROOT / "SKILL.md").read_text(encoding="utf-8")
    out: dict[str, tuple[bytes, str]] = {}
    for path in sorted((ROOT / "site").rglob("*")):
        if path.is_file():
            key = path.relative_to(ROOT / "site").as_posix()
            out[key] = (path.read_bytes(), content_type_for(key))
    out["SKILL.md"] = (skill_md.encode("utf-8"), "text/markdown; charset=utf-8")
    out[f"{skill_name(skill_md)}.zip"] = (build_skill_zip(skill_md), "application/zip")
    for key in out:
        check_key(key)
    return out


def stack_output(key: str) -> str:
    outputs = boto3.client("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]
    return next(o["OutputValue"] for o in outputs if o["OutputKey"] == key)


def bucket_name() -> str:
    return stack_output("BucketName")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--skip-skill", action="store_true", help="leave SKILL.md and the skill zip as they are")
    args = parser.parse_args()
    dry_run = args.dry_run

    bucket = bucket_name()
    s3 = boto3.client("s3")
    for key, (body, content_type) in objects().items():
        if args.skip_skill and (key == "SKILL.md" or key.endswith(".zip")):
            continue
        print(f"{'would upload' if dry_run else 'uploading'} s3://{bucket}/{key}  ({len(body)} bytes, {content_type})")
        if not dry_run:
            s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type, CacheControl="no-cache")


if __name__ == "__main__":
    main()
