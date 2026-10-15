import pytest

from rodney.apps import Apps
from rodney.datastore import MemoryDataStore
from rodney.registry import MemoryRegistry
from rodney.store import MemoryStore
from rodney.validate import RodneyError


@pytest.fixture
def store():
    return MemoryStore()


@pytest.fixture
def registry():
    return MemoryRegistry()


@pytest.fixture
def apps(store, registry):
    return Apps(store, registry, MemoryDataStore(), "https://example.com")


def test_create_deploys_html_admin_page_and_metadata(apps, store):
    r = apps.create_app("ptkinvent", "hello", html="<h1>hi</h1>")
    assert r["live"] == "https://example.com/hello"
    assert r["admin"] == "https://example.com/hello/admin"
    assert store.get("hello/index.html") == "<h1>hi</h1>"
    admin = store.get("hello/admin/index.html")
    assert "hello" in admin and "ptkinvent" in admin
    assert r["meta"]["author"] == "ptkinvent"


def test_multi_file_app(apps):
    apps.create_app(
        "ptkinvent",
        "multi",
        files=[
            {"path": "index.html", "content": "<link rel=stylesheet href=css/s.css>"},
            {"path": "css/s.css", "content": "body{}"},
        ],
    )
    assert apps.get_app("multi")["files"] == ["css/s.css", "index.html"]


def test_rejects_duplicates_and_missing_index(apps):
    apps.create_app("ptkinvent", "dup", html="x")
    with pytest.raises(RodneyError, match="already exists"):
        apps.create_app("friend", "dup", html="x")
    with pytest.raises(RodneyError, match="index.html"):
        apps.create_app("ptkinvent", "noindex", files=[{"path": "a.js", "content": ""}])
    with pytest.raises(RodneyError, match="either"):
        apps.create_app("ptkinvent", "both", html="x", files=[])


@pytest.mark.parametrize(
    "bad", ["Has Caps", "-lead", "trail-", "a/b", "mcp", "admin", "privacy", "terms", "", "ok\n", "x" * 41, None, 5, "_meta"]
)
def test_bad_slugs(apps, bad):
    with pytest.raises(RodneyError):
        apps.create_app("ptkinvent", bad, html="x")


@pytest.mark.parametrize(
    "path", ["admin/index.html", "admin", "../other/index.html", "/abs", "a//b", ".env", "a/./b", "ok.html\n", "sp ace.html"]
)
def test_cannot_write_reserved_or_escaping_paths(apps, registry, path):
    with pytest.raises(RodneyError):
        apps.create_app("ptkinvent", "evil", files=[{"path": "index.html", "content": ""}, {"path": path, "content": ""}])
    assert registry.get("evil") is None  # a failed create must not claim the name


def test_malformed_files_argument(apps):
    for bad in ["str", [1], [{"path": "index.html"}], [{"path": "index.html", "content": 5}]]:
        with pytest.raises(RodneyError):
            apps.create_app("ptkinvent", "bad", files=bad)


def test_oversized_file_rejected(apps):
    with pytest.raises(RodneyError, match="larger"):
        apps.create_app("ptkinvent", "big", html="x" * (4 * 1024 * 1024 + 1))


class TestOwnership:
    @pytest.fixture(autouse=True)
    def mine(self, apps):
        apps.create_app("ptkinvent", "mine", html="v1")

    def test_author_updates_others_read_only(self, apps):
        apps.write_file("ptkinvent", "mine", "index.html", "v2")
        assert apps.read_file("mine", "index.html") == "v2"
        with pytest.raises(RodneyError, match="only they can"):
            apps.write_file("friend", "mine", "index.html", "hax")
        with pytest.raises(RodneyError, match="only they can"):
            apps.delete_app("friend", "mine")

    def test_update_bumps_timestamp_and_blocks_admin_path(self, apps):
        before = apps.get_app("mine")["meta"]["updatedAt"]
        apps.write_file("ptkinvent", "mine", "new.js", "1")
        assert apps.get_app("mine")["meta"]["updatedAt"] >= before
        with pytest.raises(RodneyError, match="reserved"):
            apps.write_file("ptkinvent", "mine", "admin/index.html", "x")

    def test_delete_removes_everything(self, apps, store, registry):
        assert apps.delete_app("ptkinvent", "mine")["files"] == 2  # index.html + admin page
        assert store.objects == {} and registry.get("mine") is None
        with pytest.raises(RodneyError, match="not found"):
            apps.get_app("mine")

    def test_listing_shows_only_your_own_apps(self, apps):
        apps.create_app("friend", "theirs", html="x")
        assert [(a["name"], a["author"]) for a in apps.list_apps_by("ptkinvent")] == [("mine", "ptkinvent")]
        assert [a["name"] for a in apps.list_apps_by("friend")] == ["theirs"]
        assert apps.list_apps_by("nobody") == []
        assert not hasattr(apps, "list_apps")  # nothing lets a user enumerate everyone's apps

    def test_read_missing_file(self, apps):
        with pytest.raises(RodneyError, match="No file"):
            apps.read_file("mine", "nope.html")


def test_admin_page_escapes_html(apps, store):
    apps.create_app("<script>x</script>", "esc", html="x")
    assert "<script>x" not in store.get("esc/admin/index.html")


def test_per_author_app_cap(apps, monkeypatch):
    import rodney.apps as module

    monkeypatch.setattr(module, "MAX_APPS_PER_AUTHOR", 2)
    apps.create_app("ptkinvent", "a1", html="x")
    apps.create_app("ptkinvent", "a2", html="x")
    with pytest.raises(RodneyError, match="already have 2 apps"):
        apps.create_app("ptkinvent", "a3", html="x")
    apps.create_app("friend", "b1", html="x")  # the cap is per author
    apps.delete_app("ptkinvent", "a1")  # deleting frees a slot
    apps.create_app("ptkinvent", "a3", html="x")


def test_failed_create_does_not_use_up_the_cap(apps, registry):
    with pytest.raises(RodneyError):
        apps.create_app("ptkinvent", "bad", files=[{"path": "nope.js", "content": ""}])
    assert registry.count_by_author("ptkinvent") == 0


def test_reassign_moves_ownership_admin_page_and_cap_count(apps, store, registry):
    apps.create_app("old-name", "todo", html="x")
    apps.reassign("todo", "u-newowner")
    assert apps.get_app("todo")["meta"]["author"] == "u-newowner"
    assert "u-newowner" in store.get("todo/admin/index.html") and "old-name" not in store.get("todo/admin/index.html")
    assert registry.count_by_author("old-name") == 0 and registry.count_by_author("u-newowner") == 1
    apps.write_file("u-newowner", "todo", "index.html", "v2")  # the new owner can edit
    with pytest.raises(RodneyError, match="only they can"):
        apps.write_file("old-name", "todo", "index.html", "no")
    with pytest.raises(RodneyError, match="not found"):
        apps.reassign("missing", "u-x")


# ---- direct uploads ----------------------------------------------------------------------------

PNG = b"\x89PNG\r\n\x1a\n\x00\xff"


def upload(apps, store, author, name, files):
    """What Claude does: get a URL per file, PUT the bytes, then finalize."""
    for path, body in files.items():
        r = apps.get_upload_url(author, name, path, len(body))
        assert store.upload(r["uploadUrl"], body, r["contentType"])
    return apps.finalize_upload(author, name, list(files))


def test_upload_publishes_files_with_their_types_and_cleans_up(apps, store):
    r = upload(apps, store, "u-me", "deck", {"index.html": b"<img src=logo.png>", "logo.png": PNG})
    assert r["live"] == "https://example.com/deck" and r["admin"] == "https://example.com/deck/admin"
    assert r["meta"]["author"] == "u-me" and set(r) == {"meta", "live", "admin"}
    assert store.objects["deck/logo.png"] == (PNG, "image/png")
    assert store.objects["deck/index.html"][1] == "text/html; charset=utf-8"
    assert "u-me" in store.get("deck/admin/index.html")
    assert not any(k.startswith("_uploads/") for k in store.objects)  # staging emptied
    assert apps.get_app("deck")["files"] == ["index.html", "logo.png"]
    assert apps.read_file("deck", "index.html") == "<img src=logo.png>"
    with pytest.raises(RodneyError, match="binary"):
        apps.read_file("deck", "logo.png")


def test_upload_urls_are_signed_for_one_key_type_and_size(apps, store):
    r = apps.get_upload_url("u-me", "deck", "index.html", 5)
    assert r["contentType"] == "text/html; charset=utf-8" and r["expiresIn"] == 900
    key, _, size = store.presigned[r["uploadUrl"]]
    assert key.startswith("_uploads/") and key.endswith("/deck/index.html") and size == 5
    assert not store.upload(r["uploadUrl"], b"123456", r["contentType"])  # wrong size
    assert not store.upload(r["uploadUrl"], b"12345", "image/png")  # wrong type
    assert not store.list("deck/")  # nothing is public before finalize


def test_uploads_cannot_touch_an_existing_app(apps, store):
    apps.create_app("u-victim", "theirs", html="original")
    with pytest.raises(RodneyError, match="already exists"):
        apps.get_upload_url("u-attacker", "theirs", "index.html", 4)
    with pytest.raises(RodneyError, match="already exists"):  # their own app too: uploads only create
        apps.get_upload_url("u-victim", "theirs", "index.html", 4)
    assert store.get("theirs/index.html") == "original"


def test_nobody_can_finalize_someone_elses_uploads(apps, store):
    r = apps.get_upload_url("u-me", "mine", "index.html", 2)
    store.upload(r["uploadUrl"], b"hi", r["contentType"])
    with pytest.raises(RodneyError, match="Not uploaded"):
        apps.finalize_upload("u-other", "mine", ["index.html"])
    assert apps.registry.get("mine") is None
    assert apps.finalize_upload("u-me", "mine", ["index.html"])["meta"]["author"] == "u-me"


def test_losing_a_race_for_the_name_publishes_nothing(apps, store):
    r = apps.get_upload_url("u-me", "race", "index.html", 4)
    store.upload(r["uploadUrl"], b"mine", r["contentType"])
    apps.create_app("u-fast", "race", html="theirs")
    with pytest.raises(RodneyError, match="already exists"):
        apps.finalize_upload("u-me", "race", ["index.html"])
    assert store.get("race/index.html") == "theirs"


def test_finalize_checks_what_arrived(apps, store):
    r = apps.get_upload_url("u-me", "site", "index.html", 2)
    store.upload(r["uploadUrl"], b"hi", r["contentType"])
    apps.get_upload_url("u-me", "site", "app.js", 3)  # URL issued, never used
    with pytest.raises(RodneyError, match="Not uploaded.*app.js"):
        apps.finalize_upload("u-me", "site", ["index.html", "app.js"])
    for paths, match in ([], "list"), ("index.html", "list"), (["a.js"], "index.html"), (["index.html", "index.html"], "Duplicate"):
        with pytest.raises(RodneyError, match=match):
            apps.finalize_upload("u-me", "site", paths)
    with pytest.raises(RodneyError):
        apps.finalize_upload("u-me", "site", ["index.html", "../x"])
    assert apps.registry.get("site") is None


def test_files_staged_but_not_listed_are_discarded(apps, store):
    for path, body in (("index.html", b"x"), ("stray.js", b"y")):
        r = apps.get_upload_url("u-me", "tidy", path, len(body))
        store.upload(r["uploadUrl"], body, r["contentType"])
    apps.finalize_upload("u-me", "tidy", ["index.html"])
    assert "tidy/stray.js" not in store.objects and not any(k.startswith("_uploads/") for k in store.objects)


@pytest.mark.parametrize("size", [-1, 25 * 1024 * 1024 + 1, "10", 1.5, True, None])
def test_upload_size_must_be_honest_and_bounded(apps, size):
    with pytest.raises(RodneyError, match="size"):
        apps.get_upload_url("u-me", "x", "index.html", size)


@pytest.mark.parametrize("path", ["admin/index.html", "../x", "/abs", ".env", "sp ace.png"])
def test_upload_paths_are_validated_before_signing(apps, store, path):
    with pytest.raises(RodneyError):
        apps.get_upload_url("u-me", "x", path, 1)
    assert store.presigned == {}


def test_upload_file_count_is_capped(apps, store, monkeypatch):
    import rodney.apps as module
    monkeypatch.setattr(module, "MAX_UPLOAD_FILES", 2)
    for path in ("a.js", "b.js"):
        r = apps.get_upload_url("u-me", "many", path, 1)
        store.upload(r["uploadUrl"], b"1", r["contentType"])
    apps.get_upload_url("u-me", "many", "a.js", 1)  # re-uploading a staged file is fine
    with pytest.raises(RodneyError, match="at most 2"):
        apps.get_upload_url("u-me", "many", "c.js", 1)


def test_s3_presigned_urls_sign_the_type_and_size():
    """S3 enforces whatever is signed, so check offline that boto3 really signs both headers."""
    import boto3
    from botocore.config import Config
    from rodney.store import S3Store

    client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="AKIAEXAMPLE", aws_secret_access_key="x",
                          config=Config(signature_version="s3v4", s3={"addressing_style": "virtual"}))
    url = S3Store("bucket", client).presign_put("_uploads/h/app/index.html", "text/html; charset=utf-8", 123, 900)
    assert url.startswith("https://bucket.s3.amazonaws.com/_uploads/h/app/index.html?")
    assert "X-Amz-SignedHeaders=content-length%3Bcontent-type%3Bhost" in url and "X-Amz-Expires=900" in url
