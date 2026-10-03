"""Publishes the landing page and the downloadable skill to the root of the apps bucket.

    .venv/bin/python scripts/publish_site.py [--dry-run]

Writes exactly three root keys: index.html, SKILL.md and <skill name>.zip. App files all live
under <slug>/, so none of these can collide with an app.
"""

import argparse
import io
import re
import zipfile
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parent.parent
STACK = "Spin"  # CloudFormation stack id; unchanged by the rename to Rodney


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
    return {
        "index.html": ((ROOT / "site" / "index.html").read_bytes(), "text/html; charset=utf-8"),
        "SKILL.md": (skill_md.encode("utf-8"), "text/markdown; charset=utf-8"),
        f"{skill_name(skill_md)}.zip": (build_skill_zip(skill_md), "application/zip"),
    }


def bucket_name() -> str:
    outputs = boto3.client("cloudformation").describe_stacks(StackName=STACK)["Stacks"][0]["Outputs"]
    return next(o["OutputValue"] for o in outputs if o["OutputKey"] == "BucketName")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    dry_run = parser.parse_args().dry_run

    bucket = bucket_name()
    s3 = boto3.client("s3")
    for key, (body, content_type) in objects().items():
        print(f"{'would upload' if dry_run else 'uploading'} s3://{bucket}/{key}  ({len(body)} bytes, {content_type})")
        if not dry_run:
            s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType=content_type, CacheControl="no-cache")


if __name__ == "__main__":
    main()
