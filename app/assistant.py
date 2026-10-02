"""Voice commands: one spoken sentence (Swedish first) becomes list items, calendar events or Split expenses.

Claude reads the sentence and answers with tool calls; this module only turns them into checked,
plain actions. main.py carries them out with the same code the dashboard's own buttons use.

Only the sentence, today's date and the names of the family's lists and calendars go to the
Anthropic API. List contents, events and money never do.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import anthropic

log = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5"  # the cheapest model; plenty for one short command
MAX_TEXT = 500
MAX_ITEMS = 20
MAX_ANSWER = 4000
CATEGORIES = ("food", "home", "kids", "travel", "other")
WEEKDAYS_SV = ("måndag", "tisdag", "onsdag", "torsdag", "fredag", "lördag", "söndag")

SYSTEM = """You are NAME, the voice of a family's kitchen dashboard. You turn one spoken command
into tool calls.

The family speaks Swedish, sometimes English. The text comes from speech recognition, so expect
misheard words and missing punctuation, and read it charitably ("mjök" is "mjölk"). The wake
word "NAME"HEARD_AS may still be at the start; ignore it.

- Adding something to a shopping list or to-do list: add_list_items. Pick the list whose title
  best matches what was said ("inköpslistan", "handla" and "shopping" mean the shopping list;
  "todo", "att göra" and "uppgifter" the to-do list). Split "mjölk och bröd" into two items.
  Write items the way a person would type them: short, capitalised, spelling fixed.
- Adding something to the calendar: add_event. Resolve relative dates ("på fredag", "imorgon",
  "nästa vecka") from today's date. "3-4 november" is an all-day event over both days. A date
  without a year means the next time that date comes. Without a time, make it all-day. If no
  calendar is named, use the first one listed.
SPLIT_RULES
- Taking back the last command ("nej, det blev fel", "ta bort det jag nyss la till"): undo_last.
- Asking about the weather ("hur är vädret idag", "blir det regn imorgon", "behöver jag jacka"):
  show_weather for today or tomorrow. Only those two days are known; for other days, use
  answer_question to say so.
- Any other question or request for help (a recipe, how long to boil an egg, a word in English, a
  fact): always answer_question, never plain text. Answer in Swedish (English only if asked in
  English) and as briefly as possible: it is read at a glance on a kitchen screen. A fact is one
  or two sentences. A recipe is for four people unless told otherwise: at most eight ingredients
  as a "-" list, then at most five short numbered steps, with oven temperature and time. No
  introduction, tips or alternatives unless asked. "spoken" is read aloud: one or two natural
  sentences, no lists or symbols (for a recipe: what it is, oven and time, then "Receptet står på
  skärmen."). If you don't know, say so rather than guess.

Several things in one sentence mean several tool calls. If a command is unclear or something
needed is missing, don't call a tool; reply with one short sentence in Swedish (English only if
the whole command was in English) saying what was unclear."""


def _tools(lists: list[dict], calendars: list[dict], weather: bool, people: dict[str, str]) -> list[dict]:
    tools = []
    if lists:
        tools.append({
            "name": "add_list_items",
            "description": "Add one or more items to one of the family's Google Keep lists.",
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "list_id": {"type": "string", "enum": [lst["id"] for lst in lists]},
                    "items": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["list_id", "items"],
                "additionalProperties": False,
            },
        })
    if calendars:
        tools.append({
            "name": "add_event",
            "description": "Add an event to one of the family's Google calendars.",
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "calendar_id": {"type": "string", "enum": [c["id"] for c in calendars]},
                    "title": {"type": "string"},
                    "start_date": {"type": "string", "description": "YYYY-MM-DD"},
                    "end_date": {"type": ["string", "null"], "description": "YYYY-MM-DD, inclusive; null for one day"},
                    "start_time": {"type": ["string", "null"], "description": "HH:MM, 24h; null for all-day"},
                    "end_time": {"type": ["string", "null"], "description": "HH:MM, 24h; null for one hour"},
                    "location": {"type": ["string", "null"]},
                },
                "required": ["calendar_id", "title", "start_date", "end_date", "start_time", "end_time", "location"],
                "additionalProperties": False,
            },
        })
    if people:
        tools.append({
            "name": "add_expense",
            "description": f"Log a cost one of the two parents paid, in the Split app that shares costs between {' and '.join(people.values())}.",
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {
                    "description": {"type": "string", "description": "Short, e.g. 'Elräkning'"},
                    "amount_kr": {"type": "number"},
                    "paid_by": {"type": "string", "enum": list(people)},
                    "date": {"type": "string", "description": "YYYY-MM-DD; today unless another day was said"},
                    "category": {"type": "string", "enum": list(CATEGORIES)},
                },
                "required": ["description", "amount_kr", "paid_by", "date", "category"],
                "additionalProperties": False,
            },
        })
    if weather:
        tools.append({
            "name": "show_weather",
            "description": "Show the forecast on the screen and read a summary aloud.",
            "strict": True,
            "input_schema": {
                "type": "object",
                "properties": {"day": {"type": "string", "enum": ["today", "tomorrow"]}},
                "required": ["day"],
                "additionalProperties": False,
            },
        })
    tools.append({
        "name": "undo_last",
        "description": "Undo the latest voice command that added something (e.g. 'nej, ta bort det').",
        "strict": True,
        "input_schema": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
    })
    tools.append({
        "name": "answer_question",
        "description": "Answer a question: shown on the screen, with a short version read aloud.",
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "A few words, e.g. 'Pannkakor'"},
                "answer": {"type": "string", "description": "The full answer for the screen; plain text, lists with '-' or '1.'"},
                "spoken": {"type": "string", "description": "One to three sentences to read aloud"},
            },
            "required": ["title", "answer", "spoken"],
            "additionalProperties": False,
        },
    })
    return tools


class AssistantError(Exception):
    pass


@dataclass
class Plan:
    actions: list[dict] = field(default_factory=list)
    reply: str = ""


def _day(value: str) -> date:
    return date.fromisoformat(value.strip())


def _clock(value: str) -> time:
    match = re.fullmatch(r"(\d{1,2})[:.](\d{2})", value.strip())
    if not match:
        raise ValueError(value)
    return time(int(match[1]), int(match[2]))


def _action(name: str, data: dict, lists: list[dict], calendars: list[dict], zone: ZoneInfo, today: date,
            people: dict[str, str]) -> dict:
    """Check one tool call and turn it into an action main.py can carry out."""
    if name == "add_list_items":
        lst = next((x for x in lists if x["id"] == data["list_id"]), None)
        items = [" ".join(str(i).split())[:200] for i in data.get("items", [])]
        items = [i for i in items if i][:MAX_ITEMS]
        if lst is None or not items:
            raise ValueError("no list or items")
        return {"type": "list_items", "list_id": lst["id"], "list": lst["title"], "items": items}
    if name == "add_event":
        cal = next((c for c in calendars if c["id"] == data["calendar_id"]), None)
        title = " ".join(str(data.get("title", "")).split())[:200]
        if cal is None or not title:
            raise ValueError("no calendar or title")
        first = _day(data["start_date"])
        event = {"type": "event", "calendar": cal["id"], "calendar_name": cal["name"], "title": title,
                 "location": " ".join(str(data.get("location") or "").split())[:200]}
        if data.get("start_time"):
            start = datetime.combine(first, _clock(data["start_time"]), zone)
            if data.get("end_time"):
                end_day = _day(data["end_date"]) if data.get("end_date") else first
                end = datetime.combine(end_day, _clock(data["end_time"]), zone)
                if end <= start:  # "22-01" crosses midnight
                    end += timedelta(days=1)
            else:
                end = start + timedelta(hours=1)
            return {**event, "allDay": False, "start": start.isoformat(), "end": end.isoformat()}
        last = _day(data["end_date"]) if data.get("end_date") else first
        if last < first:
            raise ValueError("ends before it starts")
        return {**event, "allDay": True, "start": first.isoformat(), "end": last.isoformat()}
    if name == "add_expense":
        amount = round(float(data["amount_kr"]) * 100)
        if not 0 < amount <= 100_000_000 or data["paid_by"] not in people:
            raise ValueError("bad amount or payer")
        day = _day(data["date"])
        if abs((day - today).days) > 366:
            raise ValueError("date too far away")
        category = data.get("category") if data.get("category") in CATEGORIES else "other"
        description = " ".join(str(data.get("description", "")).split())[:200]
        return {"type": "expense", "description": description, "amount": amount,
                "paid_by": data["paid_by"], "date": day.isoformat(), "category": category}
    if name == "undo_last":
        return {"type": "undo"}
    if name == "show_weather":
        return {"type": "weather", "day": "tomorrow" if data.get("day") == "tomorrow" else "today"}
    if name == "answer_question":
        answer = str(data.get("answer", "")).strip()[:MAX_ANSWER]
        spoken = " ".join(str(data.get("spoken", "")).split())[:600] or answer[:300]
        if not answer:
            raise ValueError("empty answer")
        return {"type": "answer", "title": " ".join(str(data.get("title", "")).split())[:80],
                "text": answer, "spoken": spoken}
    raise ValueError(f"unknown tool {name}")


SPLIT_RULES = """- Logging a shared cost ("PAYER betalade…", "lägg till utgift…", "i split", "i ekonomin"):
  add_expense. Amounts are Swedish kronor. Spoken thousands come out as "12 000", "12.000" or
  "12,000": all mean twelve thousand. The payer is one of PEOPLE; if the sentence doesn't name
  one, don't guess: answer in text instead.
"""


def system_prompt(name: str, aliases: Sequence[str], people: dict[str, str]) -> str:
    heard = ", ".join(f'"{a}"' for a in aliases)
    split = ""
    if people:
        names = " or ".join(f"{label} ({pid})" for pid, label in people.items())
        split = SPLIT_RULES.replace("PAYER", next(iter(people.values()))).replace("PEOPLE", names)
    return (SYSTEM.replace("SPLIT_RULES", split).replace("NAME", name)
            .replace("HEARD_AS", f" (often heard as {heard})" if heard else ""))


class Assistant:
    def __init__(
        self, api_key: str, client: anthropic.Anthropic | None = None, name: str = "ASCA",
        aliases: Sequence[str] = (), people: dict[str, str] | None = None,
    ) -> None:
        # A stuck request is retried soon, well within the iPad's 60 s wait.
        self._client = client or anthropic.Anthropic(api_key=api_key, timeout=10.0, max_retries=2)
        self.name = name
        self._people = dict(people or {})  # Split's person ids -> names; empty turns expenses off
        self._system = system_prompt(name, aliases, self._people)

    def plan(self, text: str, lists: list[dict], calendars: list[dict], zone: ZoneInfo, now: datetime,
             weather: bool = False) -> Plan:
        text = " ".join(text.split())[:MAX_TEXT]
        if not text:
            raise AssistantError(f"Säg något efter {self.name}.")
        today = now.date()
        context = {
            "today": f"{WEEKDAYS_SV[today.weekday()]} {today.isoformat()}",
            "time": now.strftime("%H:%M"),
            "lists": [lst["title"] for lst in lists],
            "calendars": [c["name"] for c in calendars],
        }
        try:
            response = self._client.messages.create(
                model=MODEL,
                max_tokens=2048,  # room for a recipe
                system=self._system,
                tools=_tools(lists, calendars, weather, self._people),
                messages=[{"role": "user", "content": f"{json.dumps(context, ensure_ascii=False)}\n\nCommand: {text}"}],
            )
        except anthropic.APIStatusError as exc:
            log.warning("Assistant request failed (%s, request %s)", exc.status_code, exc.request_id)
            raise AssistantError("AI-tjänsten svarade inte. Försök igen.") from exc
        except anthropic.APIConnectionError as exc:
            log.warning("Assistant request failed: %s", exc)
            raise AssistantError("Kunde inte nå AI-tjänsten. Försök igen.") from exc
        if response.stop_reason == "refusal":
            return Plan(reply="Det kan jag inte hjälpa till med.")

        plan = Plan()
        for block in response.content:
            if block.type == "text":
                plan.reply += block.text.strip()
            elif block.type == "tool_use":
                try:
                    plan.actions.append(_action(block.name, dict(block.input), lists, calendars, zone, today, self._people))
                except (KeyError, TypeError, ValueError) as exc:
                    log.warning("Ignored an assistant tool call %s: %s", block.name, exc)
        if not plan.actions and not plan.reply:
            plan.reply = "Jag förstod inte riktigt. Försök igen."
        return plan
