import json
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi.testclient import TestClient

from app import demo, main
from app.config import Settings, load_settings
from app.google_auth import AuthError, parse_redirect_url
from app.store import FileStore

RANGE = {"start": "2026-09-25T00:00:00+02:00", "end": "2026-10-02T00:00:00+02:00"}


def client(tmp_path, **overrides):
    settings = Settings(data_dir=tmp_path, **overrides)
    return TestClient(main.create_app(settings))


@pytest.fixture(autouse=True)
def fresh_demo():
    demo._reset_lists()


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(main.time, "sleep", lambda _s: None)


def test_settings_from_env():
    s = load_settings({
        "CALENDARS": "Us, Family ,Kids school=Preschool",
        "KEEP_LISTS": "",
        "DEMO_MODE": "yes",
    })
    assert s.calendars == ("Us", "Family", "Kids school=Preschool")
    assert s.keep_lists == ("family",)
    assert s.demo is True


def test_access_key_guards_api_and_setup_but_not_static(tmp_path):
    c = client(tmp_path, access_key="sesame", demo=True)
    assert c.get("/").status_code == 200
    assert c.get("/app.js").status_code == 200
    assert c.get("/api/health").status_code == 200
    assert c.get("/api/config").status_code == 401
    setup = c.get("/setup", follow_redirects=False)
    assert setup.status_code == 303 and setup.headers["location"] == "/?next=/setup"

    assert c.post("/api/unlock", json={"key": "wrong"}).status_code == 401
    ok = c.post("/api/unlock", json={"key": "sesame"})
    assert ok.status_code == 200
    assert "httponly" in ok.headers["set-cookie"].lower()
    assert c.get("/api/config").json()["demo"] is True
    assert c.get("/setup").status_code == 200


def test_header_key_works_too(tmp_path):
    c = client(tmp_path, access_key="sesame", demo=True)
    assert c.get("/api/lists", headers={"X-Dashboard-Key": "sesame"}).status_code == 200


def test_calendar_range_is_validated(tmp_path):
    c = client(tmp_path, demo=True)
    assert c.get("/api/calendar", params={"start": "2026-09-25T00:00:00", "end": RANGE["end"]}).status_code == 400
    assert c.get("/api/calendar", params={"start": RANGE["start"], "end": "2027-01-01T00:00:00+01:00"}).status_code == 400
    assert c.get("/api/calendar", params={"start": RANGE["end"], "end": RANGE["start"]}).status_code == 400
    data = c.get("/api/calendar", params=RANGE).json()
    assert data["events"] and data["calendars"]
    assert data["events"][1]["start"].endswith("+02:00")


def test_demo_lists_can_be_ticked_and_added_to(tmp_path):
    c = client(tmp_path, demo=True)
    shopping = c.get("/api/lists").json()["lists"][0]
    milk = shopping["items"][0]
    ticked = c.put(f"/api/lists/{shopping['id']}/items/{milk['id']}", json={"checked": True}).json()
    assert ticked["items"][0]["checked"] is True
    added = c.post(f"/api/lists/{shopping['id']}/items", json={"text": "  Coffee  filters "}).json()
    assert "Coffee filters" in [i["text"] for i in added["items"]]
    assert c.post(f"/api/lists/{shopping['id']}/items", json={"text": "   "}).status_code == 400
    assert c.put(f"/api/lists/{shopping['id']}/items/nope", json={"checked": True}).status_code == 404


def test_demo_list_items_can_be_reordered(tmp_path):
    c = client(tmp_path, demo=True)
    shopping = c.get("/api/lists").json()["lists"][0]
    first, second, third = (i["id"] for i in shopping["items"][:3])
    url = f"/api/lists/{shopping['id']}/items"
    moved = c.put(f"{url}/{first}/position", json={"after": third}).json()
    assert [i["id"] for i in moved["items"][:3]] == [second, third, first]
    moved = c.put(f"{url}/{first}/position", json={"after": None}).json()
    assert moved["items"][0]["id"] == first
    assert c.put(f"{url}/nope/position", json={"after": None}).status_code == 404


def test_unconfigured_integrations_explain_themselves(tmp_path):
    c = client(tmp_path)
    cal = c.get("/api/calendar", params=RANGE)
    assert cal.status_code == 503 and cal.json()["error"]["code"] == "google_not_connected"
    assert c.get("/api/lists").json()["error"]["code"] == "keep_not_configured"
    assert "GOOGLE_CLIENT_ID" in c.get("/setup").text


def test_mail_is_not_on_the_dashboard(tmp_path):
    c = client(tmp_path, demo=True)
    assert "mail" not in c.get("/api/config").json()
    assert c.get("/api/mail").status_code == 404


def test_parse_redirect_url():
    assert parse_redirect_url(" http://localhost:8080/auth/google/callback?state=s1&code=4/abc&scope=x ") == ("4/abc", "s1")
    with pytest.raises(AuthError, match="access_denied"):
        parse_redirect_url("http://localhost:8080/auth/google/callback?error=access_denied&state=s1")
    with pytest.raises(AuthError):
        parse_redirect_url("http://localhost:8080/")


class FakeTokenResponse:
    ok = True
    status_code = 200
    text = ""

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def start_sign_in(c):
    start = c.get("/auth/google/start", follow_redirects=False)
    params = parse_qs(urlsplit(start.headers["location"]).query)
    return params, start


def paste(c, state, code="c"):
    r = c.post("/auth/google/paste", data={"url": f"http://localhost:8080/auth/google/callback?state={state}&code={code}"},
               follow_redirects=False)
    return unquote(r.headers["location"])


@pytest.fixture
def token_endpoint(monkeypatch):
    from app import google_auth

    posted = {}

    def fake_post(url, data, timeout):
        posted.update(data)
        return FakeTokenResponse({"refresh_token": "1//rt", "scope": " ".join(google_auth.SCOPES)})

    monkeypatch.setattr(google_auth.requests, "post", fake_post)
    return posted


def test_google_connect_flow_stores_refresh_token(tmp_path, token_endpoint):
    c = client(tmp_path, google_client_id="cid", google_client_secret="secret")
    assert c.get("/api/config").json()["google"] == {"configured": True, "connected": False}

    params, start = start_sign_in(c)
    assert params["access_type"] == ["offline"]
    assert "fd_oauth=" in start.headers["set-cookie"]
    state = params["state"][0]

    assert "damaged" in paste(c, "x")
    assert "damaged" in paste(c, state[:-1] + ("0" if state[-1] != "0" else "1"))
    assert paste(c, state) == "/setup?ok=google"
    assert token_endpoint["code"] == "c" and token_endpoint["client_secret"] == "secret"
    saved = json.loads((tmp_path / "google_token.json").read_text())
    assert saved["refresh_token"] == "1//rt"
    assert "https://www.googleapis.com/auth/calendar.events" in saved["scopes"]
    assert c.get("/api/config").json()["google"]["connected"] is True


def test_sign_in_must_finish_in_the_same_browser(tmp_path, token_endpoint):
    c = client(tmp_path, google_client_id="cid", google_client_secret="secret")
    state = start_sign_in(c)[0]["state"][0]
    c.cookies.clear()
    c.cookies.set("fd_oauth", "someone-else")
    assert "same browser" in paste(c, state)
    assert not (tmp_path / "google_token.json").exists()


def test_sign_in_link_expires(tmp_path, token_endpoint, monkeypatch):
    from app import google_auth

    c = client(tmp_path, google_client_id="cid", google_client_secret="secret")
    state = start_sign_in(c)[0]["state"][0]
    real_time = google_auth.time.time
    monkeypatch.setattr(google_auth.time, "time", lambda: real_time() + google_auth.STATE_TTL + 5)
    assert "expired" in paste(c, state)


def test_missing_scope_is_rejected(tmp_path, monkeypatch):
    from app import google_auth

    monkeypatch.setattr(
        google_auth.requests, "post",
        lambda url, data, timeout: FakeTokenResponse({"refresh_token": "rt", "scope": google_auth.SCOPES[0]}),
    )
    c = client(tmp_path, google_client_id="cid", google_client_secret="secret")
    state = start_sign_in(c)[0]["state"][0]
    r = c.get(f"/auth/google/callback?state={state}&code=c", follow_redirects=False)
    assert "gmail.readonly" in r.headers["location"]
    assert not (tmp_path / "google_token.json").exists()


def test_on_vercel_something_must_guard_the_data(tmp_path):
    c = client(tmp_path, demo=True, serverless=True)
    assert c.get("/").status_code == 200
    r = c.get("/api/lists")
    assert r.status_code == 503 and r.json()["error"]["code"] == "signin_not_configured"
    assert c.get("/setup").status_code == 503
    assert client(tmp_path, demo=True, serverless=True, access_key="k").get(
        "/api/lists", headers={"X-Dashboard-Key": "k"}).status_code == 200


def test_redirect_uri_defaults_to_vercel_production_domain():
    assert load_settings({"VERCEL": "1", "VERCEL_PROJECT_PRODUCTION_URL": "fam.vercel.app"}).google_redirect_uri == (
        "https://fam.vercel.app/auth/google/callback")
    assert load_settings({"VERCEL_PROJECT_PRODUCTION_URL": "fam.vercel.app",
                          "GOOGLE_REDIRECT_URI": "https://kitchen.example/auth/google/callback"}).google_redirect_uri == (
        "https://kitchen.example/auth/google/callback")
    s = load_settings({"VERCEL": "1", "SUPABASE_URL": "https://x.supabase.co", "SUPABASE_SECRET_KEY": "sb_secret_1"})
    assert s.serverless and s.supabase_configured


def test_setup_page_reports_storage_problems(tmp_path):
    from app.store import StoreError

    class BrokenStore:
        description = "Supabase table dashboard_kv"

        def get(self, key):
            raise StoreError("table dashboard_kv is missing")

        def set(self, key, value):
            raise StoreError("nope")

        def members(self):
            raise StoreError("nope")

    c = TestClient(main.create_app(Settings(data_dir=tmp_path, google_client_id="cid", google_client_secret="s"),
                                   store=BrokenStore()))
    page = c.get("/setup").text
    assert "table dashboard_kv is missing" in page
    assert c.get("/api/config").json()["google"]["connected"] is False


# -- Google sign-in -------------------------------------------------------------------------------

class MemberStore(FileStore):
    has_members_table = True

    def __init__(self, directory, table):
        super().__init__(directory)
        self.table = table

    def members(self):
        return self.table


@pytest.fixture
def google_login(monkeypatch):
    """Fake Google: the token endpoint returns an ID token; verification yields the chosen email."""
    from app import google_auth

    who = {"email": "alex@example.com"}
    monkeypatch.setattr(google_auth.requests, "post",
                        lambda url, data, timeout: FakeTokenResponse({"id_token": "jwt", "scope": "openid email"}))
    monkeypatch.setattr(google_auth.id_token, "verify_oauth2_token",
                        lambda token, request, audience: {"email": who["email"], "email_verified": True})
    return who


def signed_in_client(tmp_path, email_holder, email, table=None, **overrides):
    settings = Settings(data_dir=tmp_path, google_client_id="cid", google_client_secret="secret",
                        admins=("alex@example.com",), serverless=True, demo=True, **overrides)
    c = TestClient(main.create_app(settings, store=MemberStore(tmp_path, table or {})), base_url="https://testserver")
    email_holder["email"] = email
    start = c.get("/auth/login", params={"next": "/setup"}, follow_redirects=False)
    params = parse_qs(urlsplit(start.headers["location"]).query)
    assert params["scope"] == ["openid email"]
    done = c.get(f"/auth/google/callback?state={params['state'][0]}&code=c", follow_redirects=False)
    return c, unquote(done.headers["location"])


def test_signed_out_api_explains_how_to_sign_in(tmp_path):
    settings = Settings(data_dir=tmp_path, google_client_id="cid", google_client_secret="s",
                        admins=("alex@example.com",), serverless=True, demo=True)
    r = TestClient(main.create_app(settings)).get("/api/lists")
    assert r.status_code == 401
    assert r.json()["error"]["methods"] == {"google": True, "key": False}


def test_admin_from_env_signs_in_and_reaches_setup(tmp_path, google_login):
    c, location = signed_in_client(tmp_path, google_login, "Alex@Example.com")
    assert location == "/setup"
    assert c.get("/api/config").json()["user"] == {"name": "alex@example.com", "role": "admin"}
    page = c.get("/setup").text
    assert "Signed in as" in page and "alex@example.com" in page


def test_admin_from_members_table(tmp_path, google_login):
    c, location = signed_in_client(tmp_path, google_login, "partner@example.com", {"partner@example.com": "admin"})
    assert location == "/setup"
    assert c.get("/setup").status_code == 200


def test_member_sees_dashboard_but_not_setup(tmp_path, google_login):
    c, _ = signed_in_client(tmp_path, google_login, "nanny@example.com", {"nanny@example.com": "member"})
    assert c.get("/api/lists").status_code == 200
    assert c.get("/setup", follow_redirects=False).status_code == 403
    assert c.get("/auth/google/start", follow_redirects=False).status_code == 403


def test_strangers_are_turned_away(tmp_path, google_login):
    c, location = signed_in_client(tmp_path, google_login, "someone@example.com")
    assert location.startswith("/?signin_error=someone@example.com isn't on the family dashboard")
    assert c.get("/api/lists").status_code == 401


def test_removed_member_loses_access(tmp_path, google_login, monkeypatch):
    from app import auth

    table = {"nanny@example.com": "member"}
    c, _ = signed_in_client(tmp_path, google_login, "nanny@example.com", table)
    assert c.get("/api/lists").status_code == 200
    table.clear()
    monkeypatch.setattr(auth, "MEMBERS_TTL", -1)
    assert c.get("/api/lists").status_code == 401


def test_forged_or_expired_session_is_rejected(tmp_path):
    from app import auth

    key = b"k" * 32
    cookie = auth.make_session(key, "alex@example.com", now=1000)
    assert auth.read_session(key, cookie, now=2000) == ("alex@example.com", 1000)
    assert auth.read_session(b"other-key" * 4, cookie, now=2000) is None
    payload, sig = cookie.split(".")
    forged = auth.make_session(key, "evil@example.com", now=1000).split(".")[0] + "." + sig
    assert auth.read_session(key, forged, now=2000) is None
    assert auth.read_session(key, cookie, now=1000 + auth.SESSION_DAYS * 86400 + 1) is None


def test_old_session_is_renewed(tmp_path, google_login, monkeypatch):
    from app import auth

    c, _ = signed_in_client(tmp_path, google_login, "alex@example.com")
    old = auth.make_session(auth.session_key(Settings(google_client_secret="secret")), "alex@example.com",
                            now=main.time.time() - auth.REFRESH_AFTER - 10)
    c.cookies.set("fd_session", old)
    r = c.get("/api/config")
    assert r.status_code == 200 and "fd_session=" in r.headers.get("set-cookie", "")


def test_members_cannot_connect_google_data(tmp_path, google_login, token_endpoint):
    from app import google_auth

    c, _ = signed_in_client(tmp_path, google_login, "nanny@example.com", {"nanny@example.com": "member"})
    # Even with a valid connect state (made by an admin elsewhere), the callback refuses a member.
    settings = Settings(google_client_id="cid", google_client_secret="secret")
    url, nonce = google_auth.GoogleAuth(settings, FileStore(tmp_path)).authorization_url("connect")
    state = parse_qs(urlsplit(url).query)["state"][0]
    c.cookies.set("fd_oauth", nonce)
    r = c.get(f"/auth/google/callback?state={state}&code=c", follow_redirects=False)
    assert "Only a dashboard admin" in unquote(r.headers["location"])
    assert not (tmp_path / "google_token.json").exists()


def test_sign_out(tmp_path, google_login):
    c, _ = signed_in_client(tmp_path, google_login, "alex@example.com")
    c.post("/auth/logout", follow_redirects=False)
    assert c.get("/api/config").status_code == 401


# -- adding events ------------------------------------------------------------------------------

def test_demo_add_and_undo_event(tmp_path):
    c = client(tmp_path, demo=True)
    cal = c.get("/api/calendar", params=RANGE).json()
    assert cal["canAdd"] is True
    assert {x["id"]: x["writable"] for x in cal["calendars"]}["holidays"] is False

    timed = c.post("/api/events", json={"calendar": "family", "title": "  Pizza   night ",
                                         "start": "2026-09-26T18:00:00+02:00", "end": "2026-09-26T19:30:00+02:00"})
    assert timed.status_code == 200
    created = timed.json()
    assert created["title"] == "Pizza night" and created["allDay"] is False
    assert "Pizza night" in [e["title"] for e in c.get("/api/calendar", params=RANGE).json()["events"]]

    allday = c.post("/api/events", json={"calendar": "kids", "title": "Sports day", "allDay": True,
                                          "start": "2026-09-28"}).json()
    assert (allday["start"], allday["end"]) == ("2026-09-28", "2026-09-29")

    undo = c.delete("/api/events", params={"calendar": created["calendar"], "id": created["eventId"]})
    assert undo.status_code == 200
    assert "Pizza night" not in [e["title"] for e in c.get("/api/calendar", params=RANGE).json()["events"]]
    assert c.delete("/api/events", params={"calendar": "family", "id": created["eventId"]}).status_code == 404


@pytest.mark.parametrize("body,code", [
    ({"calendar": "holidays", "title": "x", "start": "2026-09-26T18:00:00+02:00"}, "not_writable"),
    ({"calendar": "family", "title": "   ", "start": "2026-09-26T18:00:00+02:00"}, "empty"),
    ({"calendar": "family", "title": "x", "start": "2026-09-26T18:00:00"}, "bad_time"),
    ({"calendar": "family", "title": "x", "start": "2026-09-26T18:00:00+02:00", "end": "2026-09-26T17:00:00+02:00"}, "bad_time"),
    ({"calendar": "family", "title": "x", "allDay": True, "start": "2026-09-26", "end": "2026-12-01"}, "bad_time"),
])
def test_event_validation(tmp_path, body, code):
    r = client(tmp_path, demo=True).post("/api/events", json=body)
    assert r.status_code == 400 and r.json()["error"]["code"] == code


class FakeCalendarSession:
    def __init__(self, marker=True):
        self.posted = None
        self.deleted = []
        self.marker = marker

    def get(self, url, params=None, timeout=None):
        if url.endswith("/users/me/calendarList"):
            return _Resp({"items": [
                {"id": "asca@group", "summary": "ASCA", "accessRole": "owner", "selected": True},
                {"id": "sv#holiday", "summary": "Holidays", "accessRole": "reader", "selected": True},
            ]})
        private = {"familyDashboard": "1"} if self.marker else {}
        return _Resp({"id": "evt1", "extendedProperties": {"private": private}})

    def post(self, url, json=None, timeout=None):
        self.posted = (url, json)
        return _Resp({"id": "evt1", "summary": json["summary"], "start": json["start"], "end": json["end"]})

    def delete(self, url, timeout=None):
        self.deleted.append(url)
        return _Resp({})


class _Resp:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload

    def raise_for_status(self):
        pass


def google_client(tmp_path, monkeypatch, session, scopes):
    from app import google_auth

    store = FileStore(tmp_path)
    store.set("google_token", {"refresh_token": "rt", "scopes": scopes})
    monkeypatch.setattr(google_auth.GoogleAuth, "session", lambda self: session)
    return TestClient(main.create_app(Settings(data_dir=tmp_path, google_client_id="cid", google_client_secret="s"),
                                      store=store))


def test_add_event_to_google(tmp_path, monkeypatch):
    from app.google_auth import SCOPES

    session = FakeCalendarSession()
    c = google_client(tmp_path, monkeypatch, session, list(SCOPES))
    r = c.post("/api/events", json={"calendar": "asca@group", "title": "Dentist", "location": "Health centre",
                                     "start": "2026-09-29T10:00:00+02:00"})
    assert r.status_code == 200 and r.json()["eventId"] == "evt1"
    url, body = session.posted
    assert url.endswith("/calendars/asca%40group/events")
    assert body["start"] == {"dateTime": "2026-09-29T10:00:00+02:00"}
    assert body["end"] == {"dateTime": "2026-09-29T11:00:00+02:00"}
    assert body["location"] == "Health centre"
    assert body["extendedProperties"] == {"private": {"familyDashboard": "1"}}

    # Read-only calendars (holidays, subscriptions) are refused.
    r = c.post("/api/events", json={"calendar": "sv#holiday", "title": "x", "start": "2026-09-29T10:00:00+02:00"})
    assert r.json()["error"]["code"] == "not_writable"

    assert c.delete("/api/events", params={"calendar": "asca@group", "id": "evt1"}).status_code == 200
    assert session.deleted and session.deleted[0].endswith("/calendars/asca%40group/events/evt1")


def test_undo_refuses_events_not_made_by_the_dashboard(tmp_path, monkeypatch):
    from app.google_auth import SCOPES

    session = FakeCalendarSession(marker=False)
    c = google_client(tmp_path, monkeypatch, session, list(SCOPES))
    r = c.delete("/api/events", params={"calendar": "asca@group", "id": "evt1"})
    assert r.status_code == 403 and not session.deleted


def test_read_only_connection_must_reconnect_to_add(tmp_path, monkeypatch):
    session = FakeCalendarSession()
    c = google_client(tmp_path, monkeypatch, session, ["https://www.googleapis.com/auth/calendar.readonly"])
    r = c.post("/api/events", json={"calendar": "asca@group", "title": "x", "start": "2026-09-29T10:00:00+02:00"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "reconnect_needed"
    assert session.posted is None
    assert "Reconnect below to allow adding events" in c.get("/setup").text
