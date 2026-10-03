import pytest

from rodney.apps import Apps
from rodney.store import MemoryStore
from rodney.validate import RodneyError

USERS = {"ptkinvent": "pw-a", "friend": "pw-b"}


@pytest.fixture
def store():
    return MemoryStore()


@pytest.fixture
def apps(store):
    return Apps(store, USERS, "https://example.com")


def test_create_deploys_html_admin_page_and_metadata(apps, store):
    r = apps.create_app("pw-a", "hello", html="<h1>hi</h1>")
    assert r["live"] == "https://example.com/hello/"
    assert r["admin"] == "https://example.com/hello/admin/"
    assert store.get("hello/index.html") == "<h1>hi</h1>"
    admin = store.get("hello/admin/index.html")
    assert "hello" in admin and "ptkinvent" in admin
    assert r["meta"]["author"] == "ptkinvent"


def test_multi_file_app(apps):
    apps.create_app(
        "pw-a",
        "multi",
        files=[
            {"path": "index.html", "content": "<link rel=stylesheet href=css/s.css>"},
            {"path": "css/s.css", "content": "body{}"},
        ],
    )
    assert apps.get_app("pw-a", "multi")["files"] == ["css/s.css", "index.html"]


def test_rejects_duplicates_bad_password_and_missing_index(apps):
    apps.create_app("pw-a", "dup", html="x")
    with pytest.raises(RodneyError, match="already exists"):
        apps.create_app("pw-b", "dup", html="x")
    with pytest.raises(RodneyError, match="Invalid password"):
        apps.create_app("nope", "x1", html="x")
    with pytest.raises(RodneyError, match="Invalid password"):
        apps.create_app(None, "x1", html="x")
    with pytest.raises(RodneyError, match="index.html"):
        apps.create_app("pw-a", "noindex", files=[{"path": "a.js", "content": ""}])
    with pytest.raises(RodneyError, match="either"):
        apps.create_app("pw-a", "both", html="x", files=[])


@pytest.mark.parametrize(
    "bad", ["Has Caps", "-lead", "trail-", "a/b", "mcp", "admin", "", "ok\n", "x" * 41, None, 5, "_meta"]
)
def test_bad_slugs(apps, bad):
    with pytest.raises(RodneyError):
        apps.create_app("pw-a", bad, html="x")


@pytest.mark.parametrize(
    "path", ["admin/index.html", "admin", "../other/index.html", "/abs", "a//b", ".env", "a/./b", "ok.html\n", "sp ace.html"]
)
def test_cannot_write_reserved_or_escaping_paths(apps, store, path):
    with pytest.raises(RodneyError):
        apps.create_app("pw-a", "evil", files=[{"path": "index.html", "content": ""}, {"path": path, "content": ""}])
    assert store.get("_meta/evil.json") is None  # a failed create must not claim the name


def test_malformed_files_argument(apps):
    for bad in ["str", [1], [{"path": "index.html"}], [{"path": "index.html", "content": 5}]]:
        with pytest.raises(RodneyError):
            apps.create_app("pw-a", "bad", files=bad)


def test_oversized_file_rejected(apps):
    with pytest.raises(RodneyError, match="larger"):
        apps.create_app("pw-a", "big", html="x" * (4 * 1024 * 1024 + 1))


class TestOwnership:
    @pytest.fixture(autouse=True)
    def mine(self, apps):
        apps.create_app("pw-a", "mine", html="v1")

    def test_author_updates_others_read_only(self, apps):
        apps.write_file("pw-a", "mine", "index.html", "v2")
        assert apps.read_file("pw-b", "mine", "index.html") == "v2"
        with pytest.raises(RodneyError, match="only they can"):
            apps.write_file("pw-b", "mine", "index.html", "hax")
        with pytest.raises(RodneyError, match="only they can"):
            apps.delete_app("pw-b", "mine")

    def test_update_bumps_timestamp_and_blocks_admin_path(self, apps):
        before = apps.get_app("pw-a", "mine")["meta"]["updatedAt"]
        apps.write_file("pw-a", "mine", "new.js", "1")
        assert apps.get_app("pw-a", "mine")["meta"]["updatedAt"] >= before
        with pytest.raises(RodneyError, match="reserved"):
            apps.write_file("pw-a", "mine", "admin/index.html", "x")

    def test_delete_removes_everything(self, apps, store):
        assert apps.delete_app("pw-a", "mine")["files"] == 2  # index.html + admin page
        assert store.objects == {}
        with pytest.raises(RodneyError, match="not found"):
            apps.get_app("pw-a", "mine")

    def test_list_shows_all_authors(self, apps):
        apps.create_app("pw-b", "theirs", html="x")
        assert sorted((a["name"], a["author"]) for a in apps.list_apps("pw-a")) == [
            ("mine", "ptkinvent"),
            ("theirs", "friend"),
        ]

    def test_read_missing_file(self, apps):
        with pytest.raises(RodneyError, match="No file"):
            apps.read_file("pw-a", "mine", "nope.html")


def test_admin_page_escapes_html(store):
    evil = Apps(store, {"<script>x</script>": "pw"}, "https://e.com")
    evil.create_app("pw", "esc", html="x")
    assert "<script>x" not in store.get("esc/admin/index.html")
