"""The eval scorer: checks one assistant Plan against what a case expects. No API calls."""

from app.assistant import Plan
from evals.score import score


def items(list_id, *texts):
    return {"type": "list_items", "list_id": list_id, "list": "Inköpslista", "items": list(texts)}


def test_list_items_match_on_keywords_and_count():
    expect = {"actions": [{"type": "list_items", "list_id": "shopping", "item_keywords": ["mjölk", "bröd"], "item_count": 2}]}
    ok, why = score(expect, Plan(actions=[items("shopping", "Mjölk", "Bröd")]))
    assert ok, why
    ok, why = score(expect, Plan(actions=[items("shopping", "Mjölk och bröd")]))
    assert not ok and "item_count" in why[0]
    ok, why = score(expect, Plan(actions=[items("todo", "Mjölk", "Bröd")]))
    assert not ok and "list_id" in why[0]


def test_extra_or_missing_actions_fail():
    expect = {"actions": [{"type": "undo"}]}
    assert score(expect, Plan(actions=[{"type": "undo"}]))[0]
    assert not score(expect, Plan(actions=[{"type": "undo"}, {"type": "undo"}]))[0]
    assert not score(expect, Plan(reply="Vad menar du?"))[0]


def test_event_times_must_match_exactly():
    expect = {"actions": [{"type": "event", "calendar": "family", "allDay": False,
                           "start": "2026-10-16T09:00:00+02:00", "end": "2026-10-16T10:00:00+02:00",
                           "title_keywords": ["tandläk"]}]}
    event = {"type": "event", "calendar": "family", "allDay": False, "title": "Tandläkaren", "location": "",
             "start": "2026-10-16T09:00:00+02:00", "end": "2026-10-16T10:00:00+02:00"}
    assert score(expect, Plan(actions=[event]))[0]
    ok, why = score(expect, Plan(actions=[{**event, "start": "2026-10-16T21:00:00+02:00"}]))
    assert not ok and "start" in why[0]


def test_expense_amount_payer_and_category_choices():
    expect = {"actions": [{"type": "expense", "amount": 125000, "paid_by": "alex", "date": "2026-10-14",
                           "category_any": ["home", "other"]}]}
    expense = {"type": "expense", "description": "Elräkning", "amount": 125000, "paid_by": "alex",
               "date": "2026-10-14", "category": "home"}
    assert score(expect, Plan(actions=[expense]))[0]
    assert not score(expect, Plan(actions=[{**expense, "amount": 1250}]))[0]
    assert not score(expect, Plan(actions=[{**expense, "category": "food"}]))[0]


def test_clarifying_reply_means_no_actions():
    expect = {"reply": True}
    assert score(expect, Plan(reply="Vem betalade?"))[0]
    assert not score(expect, Plan(actions=[{"type": "undo"}], reply="Klart"))[0]
    assert not score(expect, Plan())[0]


def test_answer_checks_length_lists_and_content():
    expect = {"actions": [{"type": "answer", "max_chars": 900, "max_bullets": 8, "max_steps": 5,
                           "must_contain_any": ["mjöl"], "spoken_contains": "receptet står på skärmen"}]}
    recipe = ("- 3 dl mjöl\n- 6 dl mjölk\n- 3 ägg\n- 1 krm salt\n- Smör\n\n"
              "1. Vispa mjöl och hälften av mjölken.\n2. Vispa i resten och äggen.\n3. Stek i smör.")
    answer = {"type": "answer", "title": "Pannkakor", "text": recipe,
              "spoken": "Pannkakor i stekpanna, ingen ugn behövs. Receptet står på skärmen."}
    assert score(expect, Plan(actions=[answer]))[0]
    too_many = recipe + "\n" + "\n".join(f"- extra {i}" for i in range(5))
    ok, why = score(expect, Plan(actions=[{**answer, "text": too_many}]))
    assert not ok and "max_bullets" in why[0]
    ok, why = score(expect, Plan(actions=[{**answer, "spoken": "Här är receptet."}]))
    assert not ok and "spoken" in why[0]
