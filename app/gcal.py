"""Google Calendar: pick the family's calendars and fetch their events for a date range."""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import date, datetime
from urllib.parse import quote

API = "https://www.googleapis.com/calendar/v3"
DEFAULT_COLOR = "#8e8e93"
WRITE_ROLES = ("owner", "writer")
# Tags events created from the dashboard; only those can be deleted (undo) from it.
MARKER_KEY = "familyDashboard"


class NotDashboardEvent(Exception):
    pass


@dataclass(frozen=True)
class CalendarRef:
    id: str
    name: str
    color: str
    writable: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def list_calendars(session) -> list[dict]:
    items: list[dict] = []
    page_token = None
    while True:
        resp = session.get(
            f"{API}/users/me/calendarList",
            params={"maxResults": 250, "pageToken": page_token},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        items.extend(data.get("items", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            return items


def _display_name(entry: dict) -> str:
    return entry.get("summaryOverride") or entry.get("summary") or entry["id"]


def _ref(entry: dict, alias: str = "") -> CalendarRef:
    return CalendarRef(
        entry["id"],
        alias or _display_name(entry),
        entry.get("backgroundColor") or DEFAULT_COLOR,
        entry.get("accessRole") in WRITE_ROLES,
    )


def choose_calendars(calendar_list: list[dict], wanted: Sequence[str]) -> tuple[list[CalendarRef], list[str]]:
    """Resolve CALENDARS entries ("Name", "calendar-id" or "Name=Short name") against the calendar list.

    With nothing configured, use the calendars that are ticked in Google Calendar.
    Returns the chosen calendars and the entries that matched nothing.
    """
    if not wanted:
        return [_ref(c) for c in calendar_list if c.get("selected")], []

    by_key: dict[str, dict] = {}
    for entry in calendar_list:
        for key in (entry["id"], entry.get("summaryOverride"), entry.get("summary")):
            if key:
                by_key.setdefault(key.casefold(), entry)

    chosen: list[CalendarRef] = []
    missing: list[str] = []
    for item in wanted:
        name, _, alias = item.partition("=")
        entry = by_key.get(name.strip().casefold())
        if entry is None:
            missing.append(name.strip())
        elif all(ref.id != entry["id"] for ref in chosen):
            chosen.append(_ref(entry, alias.strip()))
    return chosen, missing


def normalize_event(event: dict, calendar_id: str) -> dict | None:
    if event.get("status") == "cancelled":
        return None
    if any(a.get("self") and a.get("responseStatus") == "declined" for a in event.get("attendees", [])):
        return None
    start = event.get("start", {})
    end = event.get("end", {})
    if not (start.get("date") or start.get("dateTime")):
        return None
    return {
        "id": f"{calendar_id}/{event['id']}",
        "title": event.get("summary") or None,
        "start": start.get("date") or start.get("dateTime"),
        "end": end.get("date") or end.get("dateTime") or start.get("date") or start.get("dateTime"),
        "allDay": "date" in start,
        "location": event.get("location") or "",
        "calendar": calendar_id,
    }


def fetch_events(session, calendar: CalendarRef, time_min: datetime, time_max: datetime) -> list[dict]:
    resp = session.get(
        f"{API}/calendars/{quote(calendar.id, safe='')}/events",
        params={
            "timeMin": time_min.isoformat(),
            "timeMax": time_max.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 250,
        },
        timeout=15,
    )
    resp.raise_for_status()
    events = (normalize_event(e, calendar.id) for e in resp.json().get("items", []))
    return [e for e in events if e]


def fetch_all(session, calendars: list[CalendarRef], time_min: datetime, time_max: datetime) -> dict:
    """Fetch every calendar in parallel; one failing calendar doesn't hide the others."""
    events: list[dict] = []
    errors: list[dict] = []

    def one(cal: CalendarRef):
        try:
            return cal, fetch_events(session, cal, time_min, time_max), None
        except Exception as exc:  # noqa: BLE001 - reported per calendar
            return cal, [], exc

    with ThreadPoolExecutor(max_workers=6) as pool:
        for cal, cal_events, exc in pool.map(one, calendars):
            if exc is not None:
                errors.append({"calendar": cal.name, "error": str(exc)})
            events.extend(cal_events)

    return {"calendars": [c.as_dict() for c in calendars], "events": events, "errors": errors}


def event_body(title: str, start: date | datetime, end: date | datetime, location: str = "") -> dict:
    """Google event resource. All-day events take dates (end exclusive), others offset-aware datetimes."""
    key = "dateTime" if isinstance(start, datetime) else "date"
    body = {
        "summary": title,
        "start": {key: start.isoformat()},
        "end": {key: end.isoformat()},
        "extendedProperties": {"private": {MARKER_KEY: "1"}},
    }
    if location:
        body["location"] = location
    return body


def create_event(session, calendar_id: str, body: dict) -> dict:
    resp = session.post(f"{API}/calendars/{quote(calendar_id, safe='')}/events", json=body, timeout=15)
    resp.raise_for_status()
    event = resp.json()
    return {**normalize_event(event, calendar_id), "eventId": event["id"]}


def delete_dashboard_event(session, calendar_id: str, event_id: str) -> None:
    """Delete an event, but only one this dashboard created."""
    url = f"{API}/calendars/{quote(calendar_id, safe='')}/events/{quote(event_id, safe='')}"
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    private = resp.json().get("extendedProperties", {}).get("private", {})
    if private.get(MARKER_KEY) != "1":
        raise NotDashboardEvent("Only events added from the dashboard can be removed here.")
    session.delete(url, timeout=15).raise_for_status()
