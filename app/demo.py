"""Sample data for DEMO_MODE, so the dashboard can be tried on the iPad before connecting Google."""

from __future__ import annotations

import threading
import uuid
from datetime import date, datetime, time, timedelta, timezone

CALENDARS = [
    {"id": "family", "name": "Family", "color": "#f28b82", "writable": True},
    {"id": "kids", "name": "Kids", "color": "#81c995", "writable": True},
    {"id": "work", "name": "Work", "color": "#8ab4f8", "writable": True},
    {"id": "holidays", "name": "Holidays", "color": "#c58af9", "writable": False},
]

# (day offset, start, end, title, calendar, location); start None = all-day.
_EVENTS = [
    (0, None, None, "Recycling day", "family", ""),
    (0, (7, 45), (8, 15), "Preschool drop-off", "kids", ""),
    (0, (9, 0), (10, 0), "Stand-up", "work", ""),
    (0, (17, 30), (18, 30), "Football practice", "kids", "Sports hall"),
    (0, (19, 0), (20, 0), "Dinner with grandparents", "family", ""),
    (1, (8, 30), (9, 0), "Dentist", "family", "Health centre"),
    (1, (16, 0), (16, 45), "Swimming lesson", "kids", "Swimming pool"),
    (2, None, None, "Weekend trip", "family", ""),
    (2, (10, 0), (12, 0), "Birthday party", "kids", "Play centre"),
    (3, None, None, "Weekend trip", "family", ""),
    (4, (18, 0), (19, 30), "Parents' meeting", "kids", "Preschool"),
    (5, (12, 0), (13, 0), "Lunch with Sara", "work", ""),
    (6, None, None, "Name day", "holidays", ""),
]

_lock = threading.Lock()
_lists: list[dict] = []
_added: list[dict] = []  # events added from the dashboard in demo mode


def _reset_lists() -> None:
    def items(*texts: str, checked: int = 0) -> list[dict]:
        return [
            {"id": uuid.uuid4().hex, "text": t, "checked": i >= len(texts) - checked, "indented": False}
            for i, t in enumerate(texts)
        ]

    _lists[:] = [
        {
            "id": "demo-shopping",
            "title": "Shopping",
            "items": items("Milk", "Bread", "Bananas", "Coffee", "Oat yoghurt", "Dish soap", "Eggs", checked=1),
        },
        {
            "id": "demo-todo",
            "title": "To-do",
            "items": items("Book dentist for the kids", "Return library books", "Pay preschool fee",
                           "Fix the bike light", checked=1),
        },
    ]


_reset_lists()


def calendar(time_min: datetime, time_max: datetime) -> dict:
    tz = time_min.tzinfo or timezone.utc
    first_day: date = time_min.astimezone(tz).date()
    events = []
    for offset, start, end, title, cal, location in _EVENTS:
        day = first_day + timedelta(days=offset)
        if start is None:
            begin, finish = day.isoformat(), (day + timedelta(days=1)).isoformat()
        else:
            begin = datetime.combine(day, time(*start), tz).isoformat()
            finish = datetime.combine(day, time(*end), tz).isoformat()
        events.append({
            "id": f"{cal}/{offset}-{title}", "title": title, "start": begin, "end": finish,
            "allDay": start is None, "location": location, "calendar": cal,
        })
    with _lock:
        events.extend(dict(e) for e in _added)
    return {"calendars": CALENDARS, "events": events, "errors": [], "canAdd": True}


def add_event(calendar_id: str, body: dict) -> dict:
    start, end = body["start"], body["end"]
    event = {
        "id": f"{calendar_id}/{uuid.uuid4().hex}", "title": body["summary"],
        "start": start.get("date") or start.get("dateTime"), "end": end.get("date") or end.get("dateTime"),
        "allDay": "date" in start, "location": body.get("location", ""), "calendar": calendar_id,
    }
    event["eventId"] = event["id"].split("/", 1)[1]
    with _lock:
        _added.append(event)
    return dict(event)


def delete_event(calendar_id: str, event_id: str) -> None:
    with _lock:
        for event in _added:
            if event["calendar"] == calendar_id and event["eventId"] == event_id:
                _added.remove(event)
                return
    raise KeyError(event_id)


def lists() -> list[dict]:
    with _lock:
        return [dict(lst, items=[dict(i) for i in lst["items"]]) for lst in _lists]


def _get(list_id: str) -> dict:
    for lst in _lists:
        if lst["id"] == list_id:
            return lst
    raise KeyError(list_id)


def set_checked(list_id: str, item_id: str, checked: bool) -> dict:
    with _lock:
        lst = _get(list_id)
        for item in lst["items"]:
            if item["id"] == item_id:
                item["checked"] = checked
                return dict(lst, items=[dict(i) for i in lst["items"]])
    raise KeyError(item_id)


def move_item(list_id: str, item_id: str, after_id: str | None) -> dict:
    with _lock:
        lst = _get(list_id)
        items = lst["items"]
        ids = [i["id"] for i in items]
        if item_id not in ids or (after_id is not None and after_id not in ids):
            raise KeyError(item_id)
        if after_id != item_id:
            item = items.pop(ids.index(item_id))
            index = [i["id"] for i in items].index(after_id) + 1 if after_id else 0
            items.insert(index, item)
        return dict(lst, items=[dict(i) for i in items])


def add_item(list_id: str, text: str) -> dict:
    return add_items(list_id, [text])[0]


def add_items(list_id: str, texts: list[str]) -> tuple[dict, list[str]]:
    with _lock:
        lst = _get(list_id)
        ids = []
        for text in texts:
            unchecked = [i for i in lst["items"] if not i["checked"]]
            new = {"id": uuid.uuid4().hex, "text": text, "checked": False, "indented": False}
            lst["items"].insert(len(unchecked), new)
            ids.append(new["id"])
        return dict(lst, items=[dict(i) for i in lst["items"]]), ids


def delete_item(list_id: str, item_id: str) -> dict:
    with _lock:
        lst = _get(list_id)
        before = len(lst["items"])
        lst["items"] = [i for i in lst["items"] if i["id"] != item_id]
        if len(lst["items"]) == before:
            raise KeyError(item_id)
        return dict(lst, items=[dict(i) for i in lst["items"]])


# Split expenses logged by voice in demo mode.
_expenses: dict[int, dict] = {}


def add_split_expense(fields: dict) -> int:
    with _lock:
        entry_id = max(_expenses, default=0) + 1
        _expenses[entry_id] = dict(fields)
        return entry_id


def delete_split_expense(entry_id: int) -> bool:
    with _lock:
        return _expenses.pop(entry_id, None) is not None


def weather() -> dict:
    now = datetime.now()
    today = now.date()
    hours = []
    for day in (today, today + timedelta(days=1)):
        for hour in range(24):
            code = 61 if day != today and 11 <= hour <= 15 else (2 if hour < 12 else 1)
            hours.append({"time": f"{day.isoformat()}T{hour:02d}:00", "temp": round(8 + 7 * (1 - abs(hour - 14) / 14), 1),
                          "code": code, "rainChance": 70 if code == 61 else 10, "isDay": 7 <= hour <= 19})
    return {
        "place": "Stockholm",
        "now": {"temp": 13.4, "code": 2, "isDay": 7 <= now.hour <= 19},
        "days": [
            {"date": today.isoformat(), "code": 2, "max": 15.2, "min": 7.9, "rainChance": 10, "rain": 0.0, "wind": 4.1},
            {"date": (today + timedelta(days=1)).isoformat(), "code": 61, "max": 12.6, "min": 6.3, "rainChance": 70,
             "rain": 4.2, "wind": 11.3},
        ],
        "hours": hours,
    }


def accounts() -> dict:
    return {"items": [
        {"name": "Split", "value": 1240.5, "unit": "SEK", "decimals": 2, "debtor": "Sam", "creditor": "Alex"},
        {"name": "EuroBonus", "value": 146553, "unit": "points", "detail": "Silver", "updated": date.today().isoformat()},
    ]}
