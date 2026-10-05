"""The CloudFront Function that sits in front of every app request, run in Node (its real runtime)."""

import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("rodney_stack", ROOT / "infra" / "stack.py")
stack = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stack)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def run(uri: str) -> dict:
    script = stack.REWRITE_FUNCTION + "\nconsole.log(JSON.stringify(handler({request: {uri: process.argv[1]}})));"
    out = subprocess.run(["node", "-e", script, uri], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


@pytest.mark.parametrize("uri,expected", [
    ("/", "/index.html"),
    ("/todo/", "/todo/index.html"),
    ("/todo/admin/", "/todo/admin/index.html"),
    ("/todo/css/style.css", "/todo/css/style.css"),
    ("/privacy/", "/privacy/index.html"),
    ("/legal.css", "/legal.css"),
])
def test_paths_are_rewritten_to_s3_keys(uri, expected):
    assert run(uri)["uri"] == expected


@pytest.mark.parametrize("uri", ["/todo", "/privacy", "/a/b"])
def test_directory_urls_without_a_slash_redirect(uri):
    res = run(uri)
    assert res["statusCode"] == 301 and res["headers"]["location"]["value"] == uri + "/"


@pytest.mark.parametrize("uri", ["/_meta/todo.json", "/_authors/u-1/todo", "/_anything", "/_"])
def test_underscore_paths_are_never_served(uri):
    res = run(uri)
    assert res["statusCode"] == 404 and "uri" not in res
