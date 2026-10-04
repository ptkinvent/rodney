import pytest

from rodney.apps import Apps
from rodney.store import MemoryStore
from rodney.validate import RodneyError


@pytest.fixture
def store():
    return MemoryStore()


@pytest.fixture
def apps(store):
    return Apps(store, "https://example.com")


def test_create_deploys_html_admin_page_and_metadata(apps, store):
    r = apps.create_app("ptkinvent", "hello", html="<h1>hi</h1>")
    assert r["live"] == "https://example.com/hello/"
    assert r["admin"] == "https://example.com/hello/admin/"
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
def test_cannot_write_reserved_or_escaping_paths(apps, store, path):
    with pytest.raises(RodneyError):
        apps.create_app("ptkinvent", "evil", files=[{"path": "index.html", "content": ""}, {"path": path, "content": ""}])
    assert store.get("_meta/evil.json") is None  # a failed create must not claim the name


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

    def test_delete_removes_everything(self, apps, store):
        assert apps.delete_app("ptkinvent", "mine")["files"] == 2  # index.html + admin page
        assert store.objects == {}
        with pytest.raises(RodneyError, match="not found"):
            apps.get_app("mine")

    def test_list_shows_all_authors(self, apps):
        apps.create_app("friend", "theirs", html="x")
        assert sorted((a["name"], a["author"]) for a in apps.list_apps()) == [
            ("mine", "ptkinvent"),
            ("theirs", "friend"),
        ]

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


def test_failed_create_does_not_use_up_the_cap(apps, store):
    with pytest.raises(RodneyError):
        apps.create_app("ptkinvent", "bad", files=[{"path": "nope.js", "content": ""}])
    assert store.list("_authors/ptkinvent/") == []


def test_reassign_moves_ownership_admin_page_and_cap_marker(apps, store):
    apps.create_app("old-name", "todo", html="x")
    apps.reassign("todo", "u-newowner")
    assert apps.get_app("todo")["meta"]["author"] == "u-newowner"
    assert "u-newowner" in store.get("todo/admin/index.html") and "old-name" not in store.get("todo/admin/index.html")
    assert store.list("_authors/old-name/") == [] and store.list("_authors/u-newowner/") == ["_authors/u-newowner/todo"]
    apps.write_file("u-newowner", "todo", "index.html", "v2")  # the new owner can edit
    with pytest.raises(RodneyError, match="only they can"):
        apps.write_file("old-name", "todo", "index.html", "no")
    with pytest.raises(RodneyError, match="not found"):
        apps.reassign("missing", "u-x")
