from types import SimpleNamespace

from fastapi.testclient import TestClient

from app import main, photos
from app.config import Settings
from app.store import FileStore


class FakePicker:
    """Stands in for an authorized Google session talking to the Photos Picker API."""

    def __init__(self, pages):
        self.pages = pages  # page token -> (media items, next token)
        self.downloads = []

    def _resp(self, json=None, content=b"", status=200):
        return SimpleNamespace(ok=status < 400, status_code=status, text="", content=content or b"{}",
                               json=lambda: json or {}, headers={"content-type": "image/jpeg"})

    def post(self, url, json=None, timeout=None):
        return self._resp({"id": "s1", "pickerUri": "https://photos.google.com/picker/s1",
                           "pollingConfig": {"pollInterval": "3s"}})

    def get(self, url, params=None, timeout=None):
        if url.endswith("/sessions/s1"):
            return self._resp({"mediaItemsSet": True})
        if url.endswith("/mediaItems"):
            items, nxt = self.pages[(params or {}).get("pageToken", "")]
            return self._resp({"mediaItems": items, "nextPageToken": nxt})
        self.downloads.append(url)
        return self._resp(content=b"JPEG" + url.encode())


def media(i, kind="PHOTO"):
    return {"id": f"g{i}", "type": kind, "createTime": "2026-06-01T10:00:00Z",
            "mediaFile": {"baseUrl": f"https://lh3.googleusercontent.com/p{i}"}}


def test_picker_session_and_copying(tmp_path):
    store = FileStore(tmp_path)
    picker = FakePicker({"": ([media(1), media(2, "VIDEO")], "p2"), "p2": ([media(3), media(1)], "")})
    session = photos.start(picker)
    assert session["pickerUri"].endswith("/autoclose") and session["pollSeconds"] == 3
    assert photos.chosen(picker, "s1")
    first = photos.copy_page(picker, store, "s1")
    assert first == {"added": 1, "total": 1, "next": "p2"}  # the video is skipped
    second = photos.copy_page(picker, store, "s1", "p2")
    assert second["added"] == 1 and second["total"] == 2  # g1 again is skipped
    assert all(url.endswith(photos.SIZE) for url in picker.downloads)
    ids = [item["id"] for item in photos.index(store)]
    assert store.get_blob(f"{ids[0]}.jpg").startswith(b"JPEG")
    photos.remove_all(store)
    assert photos.index(store) == [] and store.get_blob(f"{ids[0]}.jpg") is None


def test_photos_are_served_only_from_the_index(tmp_path):
    store = FileStore(tmp_path)
    store.put_blob("abc.jpg", b"JPEGDATA", "image/jpeg")
    store.put_blob("other.jpg", b"SECRET", "image/jpeg")
    store.set(photos.INDEX_KEY, {"items": [{"id": "abc", "google": "g", "taken": ""}]})
    c = TestClient(main.create_app(Settings(data_dir=tmp_path), store=store))
    assert c.get("/api/photos").json() == {"photos": ["abc"]}
    r = c.get("/api/photos/abc")
    assert r.content == b"JPEGDATA" and "private" in r.headers["cache-control"]
    assert c.get("/api/photos/other").status_code == 404  # a file that isn't on the tile
    assert c.get("/setup").status_code == 200 and "Photo tile" in c.get("/setup").text


def test_photos_need_sign_in(tmp_path):
    settings = Settings(data_dir=tmp_path, google_client_id="cid", google_client_secret="s",
                        admins=("alex@example.com",), serverless=True)
    c = TestClient(main.create_app(settings))
    assert c.get("/api/photos").status_code == 401
    assert c.get("/api/photos/abc").status_code == 401
