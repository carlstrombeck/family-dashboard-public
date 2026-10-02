from app import gcal

CALENDARS = [
    {"id": "family@group", "summary": "Family", "backgroundColor": "#f00", "selected": True},
    {"id": "kids@group", "summary": "Kids school", "backgroundColor": "#0f0"},
    {"id": "me@gmail.com", "summary": "me@gmail.com", "summaryOverride": "Me", "selected": True},
]


def test_defaults_to_calendars_ticked_in_google():
    chosen, missing = gcal.choose_calendars(CALENDARS, ())
    assert [c.id for c in chosen] == ["family@group", "me@gmail.com"]
    assert missing == []


def test_matches_names_ids_and_aliases_case_insensitively():
    chosen, missing = gcal.choose_calendars(
        CALENDARS, ("family", "kids school=Preschool", "me@gmail.com", "Family", "Nope")
    )
    assert [(c.id, c.name, c.color) for c in chosen] == [
        ("family@group", "Family", "#f00"),
        ("kids@group", "Preschool", "#0f0"),
        ("me@gmail.com", "Me", gcal.DEFAULT_COLOR),
    ]
    assert missing == ["Nope"]


def test_normalize_skips_cancelled_and_declined():
    assert gcal.normalize_event({"id": "1", "status": "cancelled", "start": {"date": "2026-09-25"}}, "c") is None
    declined = {
        "id": "2",
        "start": {"dateTime": "2026-09-25T10:00:00+02:00"},
        "attendees": [{"self": True, "responseStatus": "declined"}],
    }
    assert gcal.normalize_event(declined, "c") is None


def test_normalize_all_day_and_timed():
    all_day = gcal.normalize_event(
        {"id": "a", "summary": "Trip", "start": {"date": "2026-09-26"}, "end": {"date": "2026-09-28"}}, "fam"
    )
    assert all_day == {
        "id": "fam/a", "title": "Trip", "start": "2026-09-26", "end": "2026-09-28",
        "allDay": True, "location": "", "calendar": "fam",
    }
    timed = gcal.normalize_event(
        {"id": "b", "start": {"dateTime": "2026-09-25T17:30:00+02:00"}, "end": {"dateTime": "2026-09-25T18:30:00+02:00"},
         "location": "Sports hall"}, "kids"
    )
    assert timed["allDay"] is False
    assert timed["title"] is None
    assert timed["location"] == "Sports hall"


class FakeResponse:
    def __init__(self, payload, status=200):
        self._payload, self.status_code = payload, status

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        for fragment, response in self.routes.items():
            if fragment in url:
                return response
        raise AssertionError(url)


def test_fetch_all_reports_failing_calendar_without_hiding_others():
    from datetime import datetime, timezone

    session = FakeSession({
        "family%40group": FakeResponse({"items": [{"id": "x", "summary": "Dinner", "start": {"dateTime": "2026-09-25T18:00:00Z"},
                                                    "end": {"dateTime": "2026-09-25T19:00:00Z"}}]}),
        "kids%40group": FakeResponse({}, status=403),
    })
    cals = [gcal.CalendarRef("family@group", "Family", "#f00"), gcal.CalendarRef("kids@group", "Kids", "#0f0")]
    start = datetime(2026, 9, 25, tzinfo=timezone.utc)
    result = gcal.fetch_all(session, cals, start, start.replace(day=30))
    assert [e["title"] for e in result["events"]] == ["Dinner"]
    assert result["errors"] == [{"calendar": "Kids", "error": "HTTP 403"}]
    assert session.calls[0][1]["singleEvents"] == "true"
