import tempfile
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app import demo, main
from app.assistant import Assistant
from app.config import Settings
from app.store import SupabaseStore, split_balance, split_ratio

STOCKHOLM = ZoneInfo("Europe/Stockholm")


class FakeClaude:
    """Stands in for anthropic.Anthropic: answers every request with the given blocks."""

    def __init__(self, *blocks, stop_reason="tool_use"):
        self.blocks = blocks
        self.stop_reason = stop_reason
        self.requests = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return SimpleNamespace(stop_reason=self.stop_reason, content=list(self.blocks))


def tool(name, **data):
    return SimpleNamespace(type="tool_use", name=name, input=data)


def text(value):
    return SimpleNamespace(type="text", text=value)


PEOPLE = {"alex": "Alex", "sam": "Sam"}  # Split's two people, as SPLIT_PEOPLE sets them


def app_with(*blocks, **kwargs):
    fake = FakeClaude(*blocks, **kwargs)
    settings = Settings(demo=True, data_dir=Path(tempfile.mkdtemp()), split_people=tuple(PEOPLE.items()))
    assistant = Assistant("key", client=fake, aliases=("aska",), people=PEOPLE)
    return TestClient(main.create_app(settings, assistant=assistant)), fake


@pytest.fixture(autouse=True)
def fresh_demo():
    demo._reset_lists()


def test_voice_adds_list_items_and_undo_removes_them():
    c, fake = app_with(tool("add_list_items", list_id="demo-shopping", items=["Kaffe", " Mjölk  "]))
    assert c.get("/api/config").json()["assistant"]["name"] == "ASCA"
    result = c.post("/api/assistant", json={"text": "ASCA lägg till kaffe och mjölk i inköpslistan"}).json()
    assert result["done"][0]["items"] == ["Kaffe", "Mjölk"]
    shopping = c.get("/api/lists").json()["lists"][0]
    assert [i["text"] for i in shopping["items"]].count("Kaffe") == 1

    # Claude only sees the command, today's date and the names of lists and calendars.
    sent = fake.requests[0]
    assert sent["model"] == "claude-haiku-4-5"
    assert "Shopping" in sent["messages"][0]["content"] and "Milk" not in sent["messages"][0]["content"]
    list_tool = next(t for t in sent["tools"] if t["name"] == "add_list_items")
    assert list_tool["input_schema"]["properties"]["list_id"]["enum"] == ["demo-shopping", "demo-todo"]

    assert result["undoable"] and "undo" not in result["done"][0]  # undo steps stay on the server
    assert c.post(f"/api/voice/log/{result['log_id']}/undo").json()["what"] == "Kaffe och Mjölk i Shopping"
    assert c.post(f"/api/voice/log/{result['log_id']}/undo").status_code == 409
    shopping = c.get("/api/lists").json()["lists"][0]
    assert "Kaffe" not in [i["text"] for i in shopping["items"]]


def test_voice_adds_all_day_and_timed_events():
    c, _ = app_with(
        tool("add_event", calendar_id="family", title="Konferens", start_date="2026-11-03",
             end_date="2026-11-04", start_time=None, end_time=None, location=None),
        tool("add_event", calendar_id="kids", title="Tandläkare", start_date="2026-11-10",
             end_date=None, start_time="8.30", end_time=None, location="Health centre"),
    )
    result = c.post("/api/assistant", json={"text": "…"}).json()
    conference, dentist = result["done"]
    assert conference["allDay"] and (conference["start"], conference["end"]) == ("2026-11-03", "2026-11-04")
    # November is winter time in Stockholm.
    assert dentist["start"] == "2026-11-10T08:30:00+01:00" and dentist["end"] == "2026-11-10T09:30:00+01:00"
    assert c.post(f"/api/voice/log/{result['log_id']}/undo").status_code == 200


def test_voice_adds_split_expense():
    c, _ = app_with(tool("add_expense", description="Elräkning", amount_kr=1200, paid_by="alex",
                         date=datetime.now(STOCKHOLM).date().isoformat(), category="other"))
    result = c.post("/api/assistant", json={"text": "ASCA lägg till Alex betalade elräkningen 1 200"}).json()
    expense = result["done"][0]
    assert (expense["amount"], expense["paid_by"]) == (120_000, "alex")
    assert c.post(f"/api/voice/log/{result['log_id']}/undo").json()["what"] == "Elräkning på 1 200 kronor i Split"
    assert c.post(f"/api/voice/log/{result['log_id']}/undo").status_code == 409


def test_unclear_commands_get_a_reply_and_bad_tool_calls_are_dropped():
    c, _ = app_with(
        text("Vem betalade?"),
        tool("add_expense", description="El", amount_kr=-5, paid_by="alex", date="2026-09-26", category="other"),
        tool("add_list_items", list_id="not-shown", items=["x"]),
    )
    result = c.post("/api/assistant", json={"text": "lägg till elräkningen 1 200"}).json()
    assert result["done"] == [] and result["reply"] == "Vem betalade?"


def test_refusal_and_missing_key():
    c, _ = app_with(stop_reason="refusal")
    assert c.post("/api/assistant", json={"text": "…"}).json()["reply"]
    plain = TestClient(main.create_app(Settings(demo=True, data_dir=Path(tempfile.mkdtemp()))))
    assert plain.get("/api/config").json()["assistant"] is False
    assert plain.post("/api/assistant", json={"text": "hej"}).status_code == 404


def test_split_ratio_matches_the_split_app():
    incomes = [
        {"id": 1, "person": "alex", "monthly_amount": 3_000, "effective_from": "2026-01-01"},  # made-up incomes
        {"id": 2, "person": "sam", "monthly_amount": 2_000, "effective_from": "2026-01-01"},
        {"id": 3, "person": "sam", "monthly_amount": 3_000, "effective_from": "2026-09-01"},
    ]
    assert split_ratio(incomes, "2025-12-01", ("alex", "sam")) == 600_000  # before the first row, the first row applies
    assert split_ratio(incomes, "2026-08-31", ("alex", "sam")) == 600_000
    assert split_ratio(incomes, "2026-09-26", ("alex", "sam")) == 500_000
    assert split_ratio(incomes[:1], "2026-09-26", ("alex", "sam")) is None


def test_split_balance_counts_every_entry_past_the_1000_row_cap(monkeypatch):
    import requests

    entries = [{"type": "expense", "amount": 10_000, "paid_by": "alex", "paid_to": None, "ratio_ppm": 600_000}] * 2281
    entries.append({"type": "settlement", "amount": 500_000, "paid_by": "sam", "paid_to": "alex", "ratio_ppm": None})

    class Response:
        ok = True

        def __init__(self, rows):
            self.rows = rows

        def json(self):
            return self.rows

    def fake_get(url, params=None, **_kwargs):
        assert params["select"].endswith("ratio_ppm:first_ratio_ppm")  # the column comes back under one name
        offset = int(params["offset"])
        return Response(entries[offset:offset + min(int(params["limit"]), 1000)])

    monkeypatch.setattr(requests, "get", fake_get)
    rows = SupabaseStore("https://x.supabase.co", "sb_secret_x").split_entries("first_ratio_ppm")
    assert len(rows) == 2282
    # Alex paid 2281 × 100 kr and carries 60 % of it: Sam owes 40 % = 91 240 kr, minus the 5 000 kr she paid back.
    assert split_balance(rows, ("alex", "sam")) == {"owes": "sam", "to": "alex", "amount": 2281 * 4_000 - 500_000}


def test_voice_problems_are_logged(caplog):
    c, _ = app_with()
    r = c.post("/api/voice/report", json={"problem": "no-result-in-8s", "standalone": True})
    assert r.status_code == 200
    assert "no-result-in-8s" in caplog.text and "home screen app: True" in caplog.text


def test_weather_and_questions_are_passed_to_the_ipad():
    c, fake = app_with(
        tool("show_weather", day="tomorrow"),
        tool("answer_question", title="Pannkakor", answer="- 3 dl vetemjöl\n- 6 dl mjölk\n- 3 ägg",
             spoken="Pannkakor för fyra. Receptet står på skärmen."),
    )
    result = c.post("/api/assistant", json={"text": "hur blir vädret imorgon och hur gör man pannkakor"}).json()
    weather, answer = result["done"]
    assert weather == {"type": "weather", "day": "tomorrow"}
    assert answer["title"] == "Pannkakor" and "6 dl mjölk" in answer["text"] and not result["undoable"]
    assert {"show_weather", "answer_question"} <= {t["name"] for t in fake.requests[0]["tools"]}


def test_no_weather_tool_without_a_forecast(tmp_path):
    fake = FakeClaude(text("?"))
    plain = TestClient(main.create_app(Settings(data_dir=tmp_path), assistant=Assistant("k", client=fake)))
    plain.post("/api/assistant", json={"text": "hur är vädret"})
    assert "show_weather" not in {t["name"] for t in fake.requests[0]["tools"]}


def test_saying_undo_takes_back_the_latest_command_without_asking_claude():
    c, fake = app_with(tool("add_list_items", list_id="demo-shopping", items=["Kaffe"]))
    c.post("/api/assistant", json={"text": "ASCA lägg till kaffe"})
    asked = len(fake.requests)
    undone = c.post("/api/assistant", json={"text": "ASCA, ångra!"}).json()
    assert len(fake.requests) == asked  # no Claude call
    assert undone["done"] == [{"type": "undo", "entry": undone["done"][0]["entry"], "what": "Kaffe i Shopping"}]
    assert "Kaffe" not in [i["text"] for i in c.get("/api/lists").json()["lists"][0]["items"]]
    nothing = c.post("/api/assistant", json={"text": "ångra"}).json()
    assert nothing["done"] == [] and nothing["failed"] == ["Det finns inget att ångra."]

    log = c.get("/api/voice/log").json()["entries"]
    assert [e["text"] for e in log] == ["ångra", "ASCA, ångra!", "ASCA lägg till kaffe"]
    assert log[2]["undone"] and not log[2]["undoable"] and "undo" not in log[2]


def test_undo_phrases():
    from app.voicelog import is_undo

    wake = ("ASCA", "aska")
    assert all(is_undo(t, wake) for t in ["ångra", "Aska, ångra det.", "ta bort det där", "glöm det", "undo"])
    assert not any(is_undo(t, wake) for t in ["ångra kaffe", "lägg till ångra", "ta bort mjölk från listan"])
    assert is_undo("Kompis, ångra", ("Kompis",)) and not is_undo("Kompis, ångra", wake)


def test_settings_for_other_families():
    from app.config import load_settings

    s = load_settings({"ANTHROPIC_API_KEY": "k", "ASSISTANT_NAME": "Kompis", "SPLIT_PEOPLE": "a=Anna, b=Bo",
                       "FINANCES_URL": "https://example.com/split"})
    assert (s.assistant_name, s.assistant_aliases, s.split_people) == ("Kompis", (), (("a", "Anna"), ("b", "Bo")))
    assert load_settings({}).assistant_aliases[0] == "aska"  # the default name comes with its mishearings
    assert load_settings({"ASSISTANT_ALIASES": ""}).assistant_aliases[0] == "aska"  # empty, as in .env.example
    assert load_settings({"SPLIT_PEOPLE": "just-one"}).split_people == ()  # Split needs exactly two people
    fake = FakeClaude(text("?"))
    Assistant("k", client=fake, name="Kompis").plan("hej", [], [], STOCKHOLM, datetime.now(STOCKHOLM))
    sent = fake.requests[0]
    assert "You are Kompis" in sent["system"] and "betalade" not in sent["system"]
    assert "add_expense" not in {t["name"] for t in sent["tools"]}  # no Split people, no expenses
