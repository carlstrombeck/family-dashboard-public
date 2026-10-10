"""Score one assistant Plan against what an eval case expects.

Hard checks only: the right tool, the right list or calendar, exact dates and times, exact
amounts. Text the model writes freely (item wording, titles, answers) is checked with keywords
and limits, never word for word.
"""

from __future__ import annotations

import re

from app.assistant import Plan


def _fold(value) -> str:
    return " ".join(str(value or "").split()).casefold()


def _check_action(expected: dict, actual: dict) -> list[str]:
    """Reasons this action does not meet the expectation; empty when it does."""
    problems = []
    for key, want in expected.items():
        if key == "item_keywords":
            joined = [_fold(i) for i in actual.get("items", [])]
            missing = [k for k in want if not any(_fold(k) in i for i in joined)]
            if missing:
                problems.append(f"item_keywords: missing {missing} in {actual.get('items')}")
        elif key == "item_count":
            if len(actual.get("items", [])) != want:
                problems.append(f"item_count: want {want}, got {actual.get('items')}")
        elif key in ("title_keywords", "location_keywords"):
            field = key.split("_")[0]
            missing = [k for k in want if _fold(k) not in _fold(actual.get(field))]
            if missing:
                problems.append(f"{key}: {missing} not in {actual.get(field)!r}")
        elif key == "category_any":
            if actual.get("category") not in want:
                problems.append(f"category: want one of {want}, got {actual.get('category')!r}")
        elif key == "max_chars":
            if len(actual.get("text", "")) > want:
                problems.append(f"max_chars: {len(actual.get('text', ''))} > {want}")
        elif key == "max_bullets":
            bullets = len(re.findall(r"^\s*-\s", actual.get("text", ""), re.MULTILINE))
            if bullets > want:
                problems.append(f"max_bullets: {bullets} > {want}")
        elif key == "max_steps":
            steps = len(re.findall(r"^\s*\d+[.)]\s", actual.get("text", ""), re.MULTILINE))
            if steps > want:
                problems.append(f"max_steps: {steps} > {want}")
        elif key == "must_contain_any":
            body = _fold(f"{actual.get('title')} {actual.get('text')}")
            if not any(_fold(k) in body for k in want):
                problems.append(f"must_contain_any: none of {want} in the answer")
        elif key == "spoken_contains":
            if _fold(want) not in _fold(actual.get("spoken")):
                problems.append(f"spoken: {want!r} not in {actual.get('spoken')!r}")
        elif key == "spoken_max_sentences":
            sentences = [s for s in re.split(r"[.!?]+", actual.get("spoken", "")) if s.strip()]
            if len(sentences) > want:
                problems.append(f"spoken_max_sentences: {len(sentences)} > {want}")
        elif actual.get(key) != want:
            problems.append(f"{key}: want {want!r}, got {actual.get(key)!r}")
    return problems


def score(expect: dict, plan: Plan) -> tuple[bool, list[str]]:
    """(passed, reasons). Every expected action must be matched by exactly one actual action."""
    if expect.get("reply"):
        if plan.actions:
            return False, [f"reply: expected a clarifying reply, got actions {[a['type'] for a in plan.actions]}"]
        return (True, []) if plan.reply.strip() else (False, ["reply: expected a clarifying reply, got nothing"])

    wanted = expect.get("actions", [])
    left = list(plan.actions)
    if len(left) != len(wanted):
        got = [a["type"] for a in left] or f"reply {plan.reply!r}"
        return False, [f"count: want {[w['type'] for w in wanted]}, got {got}"]
    reasons = []
    for want in wanted:
        candidates = [a for a in left if a.get("type") == want["type"]]
        if not candidates:
            reasons.append(f"type: no {want['type']} action")
            continue
        checked = [(a, _check_action(want, a)) for a in candidates]
        match = next((a for a, problems in checked if not problems), None)
        if match is None:
            reasons.extend(checked[0][1])
        else:
            left.remove(match)
    return not reasons, reasons
