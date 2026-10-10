"""The eval runner and the voice-log converter, with a fake Claude: no API calls."""

import json
from pathlib import Path

from app.assistant import Assistant
from evals.from_log import draft
from evals.run import load, run_all, summary
from tests.test_assistant import FakeClaude, text, tool

CASES = Path(__file__).parent.parent / "evals" / "cases.json"


def test_every_case_is_well_formed():
    context, cases = load(CASES)
    assert len({c["id"] for c in cases}) == len(cases)
    for case in cases:
        assert case["text"] and case["tags"]
        assert case["expect"].get("reply") or case["expect"].get("actions"), case["id"]


def test_runner_scores_each_repeat_and_marks_flaky_cases():
    context, cases = load(CASES)
    case = next(c for c in cases if c["id"] == "list-two-items")
    good = tool("add_list_items", list_id="shopping", items=["Mjölk", "Bröd"])
    bad = tool("add_list_items", list_id="todo", items=["Mjölk", "Bröd"])
    replies = iter([good, bad, good])

    class Alternating(FakeClaude):
        def _create(self, **kwargs):
            self.blocks = (next(replies),)
            return super()._create(**kwargs)

    fake = Alternating()
    results = run_all([case], context, lambda ctx: Assistant("key", client=fake, people=ctx["people"]), repeats=3, workers=1)
    assert results[0]["status"] == "FLAKY" and results[0]["passed"] == 2
    rate, by_tag = summary(results)
    assert round(rate, 2) == 0.67 and by_tag["list"] == (2, 3)
    # The model sees the fixed date, not today's.
    assert "onsdag 2026-10-14" in fake.requests[0]["messages"][0]["content"]


def test_clarifying_case_passes_on_a_text_reply():
    context, cases = load(CASES)
    case = next(c for c in cases if c["id"] == "expense-no-payer")
    fake = FakeClaude(text("Vem betalade?"), stop_reason="end_turn")
    results = run_all([case], context, lambda ctx: Assistant("key", client=fake, people=ctx["people"]), repeats=1)
    assert results[0]["status"] == "PASS"


def test_voice_log_becomes_draft_cases_marked_for_review():
    entries = [
        {"at": "2026-10-09T07:45:00+02:00", "text": "lägg till mjölk", "reply": "",
         "done": [{"type": "list_items", "list": "Inköp", "items": ["Mjölk"]}]},
        {"at": "2026-10-09T08:00:00+02:00", "text": "ångra", "reply": "", "done": [{"type": "undo", "what": "x"}]},
        {"at": "2026-10-09T08:05:00+02:00", "text": "lägg till", "reply": "Vad ska läggas till?", "done": []},
    ]
    result = draft(entries)
    assert [c["text"] for c in result["cases"]] == ["lägg till mjölk", "lägg till"]
    first, second = result["cases"]
    assert first["review"] and first["now"] == "2026-10-09T07:45:00"
    assert first["expect"]["actions"][0] == {"type": "list_items", "list_id": "Inköp", "item_count": 1, "item_keywords": ["mjölk"]}
    assert second["expect"] == {"reply": True}
    assert result["context"]["lists"] == [{"id": "Inköp", "title": "Inköp"}]
    json.dumps(result, ensure_ascii=False)
