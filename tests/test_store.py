import pytest

from app import store as store_mod
from app.store import FileStore, StoreError, SupabaseStore


class FakeResp:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.ok = status < 400
        self._payload = payload
        self.text = str(payload)

    def json(self):
        if self._payload is None:
            raise ValueError
        return self._payload


def test_file_store_round_trip(tmp_path):
    s = FileStore(tmp_path / "nested")
    assert s.get("google_token") is None
    s.set("google_token", {"refresh_token": "rt"})
    assert s.get("google_token") == {"refresh_token": "rt"}
    assert oct((tmp_path / "nested" / "google_token.json").stat().st_mode)[-3:] == "600"


def test_file_store_on_read_only_disk_raises_store_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    with pytest.raises(StoreError):
        FileStore(blocker / "sub").set("k", {})


@pytest.mark.parametrize("key,expect_bearer", [("sb_secret_abc", False), ("eyJhbGciOi.legacy.jwt", True)])
def test_supabase_store_headers_and_upsert(monkeypatch, key, expect_bearer):
    calls = []

    def fake_get(url, params, headers, timeout):
        calls.append(("get", url, params, headers))
        return FakeResp(200, [{"value": {"refresh_token": "rt"}}])

    def fake_post(url, params, json, headers, timeout):
        calls.append(("post", url, params, headers, json))
        return FakeResp(201)

    monkeypatch.setattr(store_mod.requests, "get", fake_get)
    monkeypatch.setattr(store_mod.requests, "post", fake_post)
    s = SupabaseStore("https://abc.supabase.co/", key)

    assert s.get("google_token") == {"refresh_token": "rt"}
    s.set("keep_state", {"keep_version": "7"})

    _, url, params, headers = calls[0]
    assert url == "https://abc.supabase.co/rest/v1/dashboard_kv"
    assert params == {"key": "eq.google_token", "select": "value"}
    assert headers["apikey"] == key
    assert ("Authorization" in headers) is expect_bearer

    _, _, params, headers, row = calls[1]
    assert params == {"on_conflict": "key"}
    assert "resolution=merge-duplicates" in headers["Prefer"]
    assert row["key"] == "keep_state" and row["value"] == {"keep_version": "7"}


def test_supabase_store_missing_row_and_missing_table(monkeypatch):
    s = SupabaseStore("https://abc.supabase.co", "sb_secret_abc")
    monkeypatch.setattr(store_mod.requests, "get", lambda *a, **k: FakeResp(200, []))
    assert s.get("google_token") is None

    missing = FakeResp(404, {"code": "PGRST205", "message": "Could not find the table 'public.dashboard_kv'"})
    monkeypatch.setattr(store_mod.requests, "get", lambda *a, **k: missing)
    with pytest.raises(StoreError, match="supabase/migrations"):
        s.get("google_token")
