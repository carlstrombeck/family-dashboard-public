import base64

import pytest
from fastapi.testclient import TestClient

from app import accounts, main, weather
from app.config import Settings, load_settings
from app.store import FileStore
from tests.test_gcal import FakeResponse, FakeSession

OPEN_METEO = {
    "current": {"temperature_2m": 13.4, "weather_code": 2, "is_day": 1},
    "daily": {
        "time": ["2026-09-25", "2026-09-26"],
        "weather_code": [2, 61],
        "temperature_2m_max": [15.2, 12.6],
        "temperature_2m_min": [7.9, 6.3],
        "precipitation_probability_max": [10, 70],
        "precipitation_sum": [0.0, 4.2],
        "wind_speed_10m_max": [4.1, 11.3],
    },
    "hourly": {
        "time": ["2026-09-25T08:00", "2026-09-25T12:00"],
        "temperature_2m": [9.1, 14.0],
        "weather_code": [1, 2],
        "precipitation_probability": [0, 5],
        "is_day": [1, 1],
    },
}


def test_weather_summary():
    w = weather.summarize(OPEN_METEO, "Stockholm")
    assert w["now"] == {"temp": 13.4, "code": 2, "isDay": True}
    assert w["days"][1] == {"date": "2026-09-26", "code": 61, "max": 12.6, "min": 6.3, "rainChance": 70,
                            "rain": 4.2, "wind": 11.3}
    assert w["hours"][1] == {"time": "2026-09-25T12:00", "temp": 14.0, "code": 2, "rainChance": 5, "isDay": True}


def test_weather_endpoint_uses_configured_place_and_caches(tmp_path, monkeypatch):
    calls = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return OPEN_METEO

    def fake_get(url, params, timeout):
        calls.append(params)
        return Resp()

    monkeypatch.setattr(weather.requests, "get", fake_get)
    c = TestClient(main.create_app(Settings(data_dir=tmp_path, weather_lat=59.33, weather_lon=18.07,
                                            weather_place="Stockholm")))
    assert c.get("/api/config").json()["weather"] is True
    assert c.get("/api/weather").json()["place"] == "Stockholm"
    c.get("/api/weather")
    assert len(calls) == 1 and calls[0]["latitude"] == 59.33 and calls[0]["timezone"] == "auto"


def test_weather_off_without_a_place(tmp_path):
    c = TestClient(main.create_app(Settings(data_dir=tmp_path)))
    assert c.get("/api/config").json()["weather"] is False
    assert c.get("/api/weather").status_code == 404


def test_settings():
    s = load_settings({"WEATHER_LAT": "59.33", "WEATHER_LON": "18.07", "WEATHER_PLACE": "Stockholm",
                       "SHOW_EUROBONUS": "false"})
    assert (s.weather_lat, s.weather_lon, s.weather_place, s.eurobonus) == (59.33, 18.07, "Stockholm", False)
    assert load_settings({"WEATHER_LAT": "north"}).weather_configured is False
    assert load_settings({}).eurobonus is True


def b64(text):
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def sas_email(html_body, snippet="Extra poäng på garderob"):
    return {"snippet": snippet, "payload": {"mimeType": "multipart/alternative", "parts": [
        {"mimeType": "text/html", "body": {"data": b64(html_body)}}]}}


def test_eurobonus_from_html_email():
    body = ("<html><style>.x{color:red}</style><td>CS</td><td>Alex Andersson</td>"
            "<td>EBS 123456789</td><td>146&nbsp;553 po&auml;ng</td><td>Uppdaterat 2026-09-17</td></html>")
    found = accounts.parse_eurobonus(accounts.message_text(sas_email(body)))
    assert found == {"name": "EuroBonus", "value": 146553, "unit": "points", "detail": "Silver",
                     "updated": "2026-09-17"}
    assert "123456789" not in str(found)  # the member number is never passed on


def test_fetch_eurobonus_skips_emails_without_a_balance():
    session = FakeSession({
        "/messages/m1": FakeResponse(sas_email("<p>Rean slutar vid midnatt</p>")),
        "/messages/m2": FakeResponse(sas_email("<p>EBG 111222333 20 000 poäng Uppdaterat 2026-09-01</p>")),
        "/messages": FakeResponse({"messages": [{"id": "m1"}, {"id": "m2"}]}),
    })
    found = accounts.fetch_eurobonus(session)
    assert found["value"] == 20000 and found["detail"] == "Gold"
    assert session.calls[0][1]["q"] == accounts.EUROBONUS_QUERY


def test_accounts_endpoint(tmp_path, monkeypatch):
    from app import google_auth
    from app.google_auth import SCOPES

    session = FakeSession({
        "/messages/m1": FakeResponse(sas_email("<p>EBS 123456789 146553 poäng Uppdaterat 2026-09-17</p>")),
        "/messages": FakeResponse({"messages": [{"id": "m1"}]}),
    })
    store = FileStore(tmp_path)
    store.set("google_token", {"refresh_token": "rt", "scopes": list(SCOPES)})
    monkeypatch.setattr(google_auth.GoogleAuth, "session", lambda self: session)
    c = TestClient(main.create_app(Settings(data_dir=tmp_path, google_client_id="cid", google_client_secret="s"),
                                   store=store))
    assert c.get("/api/accounts").json()["items"][0]["value"] == 146553
    off = TestClient(main.create_app(Settings(data_dir=tmp_path, eurobonus=False)))
    assert off.get("/api/accounts").json() == {"items": []}


@pytest.mark.parametrize("path", ["/api/weather", "/api/accounts"])
def test_demo_mode(tmp_path, path):
    r = TestClient(main.create_app(Settings(data_dir=tmp_path, demo=True))).get(path)
    assert r.status_code == 200 and r.json()


def test_a_failing_source_does_not_hide_the_others(tmp_path, monkeypatch):
    monkeypatch.setattr(accounts, "fetch_eurobonus",
                        lambda session: {"name": "EuroBonus", "value": 1, "unit": "points"})
    from app import google_auth
    from app.google_auth import SCOPES

    store = FileStore(tmp_path)
    store.set("google_token", {"refresh_token": "rt", "scopes": list(SCOPES)})
    monkeypatch.setattr(google_auth.GoogleAuth, "session", lambda self: object())
    c = TestClient(main.create_app(Settings(data_dir=tmp_path, google_client_id="cid", google_client_secret="s",
                                            supabase_url="https://x.supabase.co", supabase_key="k"), store=store))
    # The file store can't read Split, so the Split balance fails; EuroBonus still shows.
    assert [i["name"] for i in c.get("/api/accounts").json()["items"]] == ["EuroBonus"]
    assert c.get("/api/config").json()["accounts"] is True
