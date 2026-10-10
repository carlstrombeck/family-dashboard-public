"""Turn your own voice log into draft eval cases (stays on your machine; the file is gitignored).

1. Signed in on the dashboard, open /api/voice/log and save the page as voice_log.json.
2. python -m evals.from_log voice_log.json
3. Open evals/private_cases.json. Every draft expects what the assistant actually did, so fix the
   ones it got wrong, then remove "review": true. Cases still marked for review are skipped.

Commands that only undid something are skipped: undo is handled before Claude is asked.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

OUT = Path(__file__).parent / "private_cases.json"


def expectation(action: dict) -> dict:
    kind = action.get("type")
    if kind == "list_items":
        return {"type": kind, "list_id": action["list"], "item_count": len(action["items"]),
                "item_keywords": [i.casefold() for i in action["items"]]}
    if kind == "event":
        return {"type": kind, "calendar": action["calendar"], "allDay": action["allDay"],
                "start": action["start"], "end": action["end"], "title_keywords": [action["title"].casefold()]}
    if kind == "expense":
        return {"type": kind, "amount": action["amount"], "paid_by": action["paid_by"], "date": action["date"]}
    if kind == "weather":
        return {"type": kind, "day": action["day"]}
    return {"type": kind}


def draft(entries: list[dict]) -> dict:
    lists, calendars, cases = set(), set(), []
    for n, entry in enumerate(entries):
        done = [a for a in entry.get("done", []) if a.get("type") != "undo"]
        if not done and not entry.get("reply"):
            continue
        for a in done:
            if a.get("type") == "list_items":
                lists.add(a["list"])
            if a.get("type") == "event":
                calendars.add(a["calendar"])
        at = datetime.fromisoformat(entry["at"])
        cases.append({
            "id": f"log-{at:%Y%m%d-%H%M}-{n}", "tags": ["log"], "text": entry["text"],
            "now": at.replace(tzinfo=None).isoformat(timespec="seconds"),
            "expect": {"actions": [expectation(a) for a in done]} if done else {"reply": True},
            "review": True,
        })
    return {
        "context": {
            "timezone": "Europe/Stockholm",
            "lists": [{"id": t, "title": t} for t in sorted(lists)],
            "calendars": [{"id": c, "name": c} for c in sorted(calendars)],
            "people": {}, "aliases": [],
        },
        "cases": cases,
    }


def main(path: str) -> None:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    result = draft(data.get("entries", data))
    OUT.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Wrote {len(result['cases'])} draft cases to {OUT}. Review them before running the evals.")


if __name__ == "__main__":
    main(sys.argv[1])
