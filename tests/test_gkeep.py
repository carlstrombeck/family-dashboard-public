import gkeepapi
import pytest
from fastapi.testclient import TestClient
from gkeepapi.node import List, Note

from app import gkeep, main
from app.config import Settings

FAMILY_SHARE = [{"role": "O", "email": "alex@example.com"}, {"role": "W", "auxiliary_type": "FAMILY"}]
PERSON_SHARE = [{"role": "O", "email": "alex@example.com"}, {"role": "W", "email": "sam@example.com"}]


def make_list(title, *texts, sharing=None, pinned=False, archived=False):
    note = List()
    note.title = title
    note.pinned = pinned
    note.archived = archived
    for text in texts:
        note.add(text, False, gkeepapi.node.NewListItemPlacementValue.Bottom)
    gkeep._sharing[note.id] = sharing or []
    return note


def make_note(title, text="", sharing=None):
    note = Note()
    note.title = title
    note.text = text
    gkeep._sharing[note.id] = sharing or []
    return note


class FakeKeep:
    def __init__(self, notes, fail_login=False):
        self.notes = notes
        self.fail_login = fail_login
        self.logins = 0
        self.syncs = 0

    def authenticate(self, email, token):
        self.logins += 1
        if self.fail_login:
            raise gkeepapi.exception.LoginException("BadAuthentication")

    def sync(self):
        self.syncs += 1

    def all(self):
        return self.notes


@pytest.fixture
def notes():
    return [
        make_list("Handla", "Mjölk", "Bröd", sharing=FAMILY_SHARE),
        make_list("Att göra", "Ring mormor", sharing=PERSON_SHARE),
        make_list("My secret list", "Surprise gift"),
        make_note("Wifi", "Network: home\nPassword: hunter2", sharing=FAMILY_SHARE),
        make_note("Diary", "private thoughts"),
        make_list("Old family list", "x", sharing=FAMILY_SHARE, archived=True),
    ]


def keep_for(notes, selection=(), hidden=lambda: (), **kwargs):
    fake = FakeKeep(notes, **kwargs)
    return gkeep.KeepLists("alex@example.com", "token", selection, keep_factory=lambda: fake, hidden=hidden), fake


def titles(lists):
    return [entry["title"] for entry in lists]


def test_sharing_is_captured_from_raw_note_data():
    raw = make_list("Handla", "Mjölk").save(False)
    raw["roleInfo"] = [{"role": "O", "email": "alex@example.com", "auxiliary_type": "None"},
                       {"role": "W", "email": "family-123@example.com", "auxiliary_type": "FAMILY"}]
    loaded = List()
    loaded.load(raw)
    assert gkeep.sharing_of(loaded) == "family"


def test_default_shows_only_family_notes(notes):
    keep, _ = keep_for(notes)
    lists = keep.lists()
    assert sorted(titles(lists)) == ["Handla", "Wifi"]
    wifi = next(entry for entry in lists if entry["title"] == "Wifi")
    assert wifi["kind"] == "note" and "hunter2" in wifi["text"]


def test_shared_mode_includes_notes_shared_with_people(notes):
    keep, _ = keep_for(notes, ["shared"])
    assert sorted(titles(keep.lists())) == ["Att göra", "Handla", "Wifi"]


def test_pinned_notes_come_first(notes):
    notes.append(make_list("Veckomat", "Tacos", sharing=FAMILY_SHARE, pinned=True))
    keep, _ = keep_for(notes)
    assert titles(keep.lists())[0] == "Veckomat"


def test_private_lists_cannot_be_changed_from_the_dashboard(notes):
    keep, _ = keep_for(notes)
    secret = notes[2]
    with pytest.raises(gkeep.ItemNotFound):
        keep.set_checked(secret.id, secret.items[0].id, True)
    with pytest.raises(gkeep.ItemNotFound):
        keep.add_item(secret.id, "peek")


def test_tick_and_add_on_family_list(notes):
    keep, fake = keep_for(notes)
    shopping = notes[0]
    result = keep.set_checked(shopping.id, shopping.items[0].id, True)
    assert result["items"][0]["checked"] is True
    result = keep.add_item(shopping.id, "  Ägg  ")
    assert [i["text"] for i in result["items"]] == ["Mjölk", "Bröd", "Ägg"]
    assert fake.syncs == 2
    with pytest.raises(gkeep.KeepError):
        keep.add_item(shopping.id, "   ")


def test_move_item_on_family_list(notes):
    keep, _ = keep_for(notes)
    shopping = notes[0]
    for text in ("Ägg", "Kaffe"):
        keep.add_item(shopping.id, text)
    ids = {i.text: i.id for i in shopping.items}
    order = lambda result: [i["text"] for i in result["items"]]  # noqa: E731
    assert order(keep.move_item(shopping.id, ids["Kaffe"], None)) == ["Kaffe", "Mjölk", "Bröd", "Ägg"]
    assert order(keep.move_item(shopping.id, ids["Mjölk"], ids["Ägg"])) == ["Kaffe", "Bröd", "Ägg", "Mjölk"]
    assert order(keep.move_item(shopping.id, ids["Ägg"], ids["Kaffe"])) == ["Kaffe", "Ägg", "Bröd", "Mjölk"]
    # Neighbours with no room between them get spaced out again.
    for item, sort in zip(shopping.items, (30, 29, 20, 10)):
        item.sort = sort
    assert order(keep.move_item(shopping.id, ids["Mjölk"], ids["Kaffe"])) == ["Kaffe", "Mjölk", "Ägg", "Bröd"]
    with pytest.raises(gkeep.ItemNotFound):
        keep.move_item(shopping.id, "nope", None)
    with pytest.raises(gkeep.ItemNotFound):
        keep.move_item(notes[2].id, notes[2].items[0].id, None)


def test_add_several_items_and_delete_one(notes):
    keep, _ = keep_for(notes)
    shopping = notes[0]
    result, ids = keep.add_items(shopping.id, ["Te", "  ", "Kaffe"])
    assert [i["text"] for i in result["items"]] == ["Mjölk", "Bröd", "Te", "Kaffe"] and len(ids) == 2
    result = keep.delete_item(shopping.id, ids[0])
    assert [i["text"] for i in result["items"]] == ["Mjölk", "Bröd", "Kaffe"]
    with pytest.raises(gkeep.ItemNotFound):
        keep.delete_item(shopping.id, ids[0])


def test_title_mode_still_works(notes):
    keep, _ = keep_for(notes, ["handla", "Diary", "Missing"])
    lists = keep.lists()
    assert lists[0]["title"] == "Handla" and lists[0]["kind"] == "list"
    assert lists[1]["error"] == "not_a_checklist"
    assert lists[2]["error"] == "not_found"


def test_overview_lists_only_notes_that_may_be_shown(notes):
    keep, _ = keep_for(notes)
    rows = {row["title"]: row for row in keep.overview()}
    assert set(rows) == {"Handla", "Wifi"}  # not private, person-shared or archived ones
    assert rows["Handla"] == {"id": notes[0].id, "title": "Handla", "checklist": True, "sharing": "family",
                              "hidden": False, "shown": True}


def test_hidden_notes_are_left_out(notes):
    shopping = notes[0]
    keep, _ = keep_for(notes, hidden=lambda: {shopping.id})
    assert titles(keep.lists()) == ["Wifi"]
    rows = {row["title"]: row for row in keep.overview()}
    assert rows["Handla"]["hidden"] and not rows["Handla"]["shown"]
    assert rows["Wifi"]["shown"] and not rows["Wifi"]["hidden"]
    with pytest.raises(gkeep.ItemNotFound):
        keep.set_checked(shopping.id, shopping.items[0].id, True)


def test_setup_saves_which_family_notes_are_shown(tmp_path, notes):
    fake = FakeKeep(notes)
    settings = Settings(data_dir=tmp_path, keep_email="alex@example.com", keep_master_token="token")
    c = TestClient(main.create_app(settings, keep_factory=lambda: fake))
    page = c.get("/setup").text
    assert "Handla" in page and "Wifi" in page
    assert "My secret list" not in page and "Att göra" not in page and "Diary" not in page

    shopping, wifi = notes[0], notes[3]
    r = c.post("/setup/keep", data={"listed": [shopping.id, wifi.id], "show": [shopping.id]}, follow_redirects=False)
    assert r.status_code == 303 and "ok=keep" in r.headers["location"]
    assert titles(c.get("/api/lists").json()["lists"]) == ["Handla"]

    # A form that only lists Wifi leaves Handla's setting alone.
    c.post("/setup/keep", data={"listed": [wifi.id], "show": [wifi.id]})
    assert sorted(titles(c.get("/api/lists").json()["lists"])) == ["Handla", "Wifi"]
    c.post("/setup/keep", data={"listed": [shopping.id]})
    assert titles(c.get("/api/lists").json()["lists"]) == ["Wifi"]


def test_login_failure_backs_off(notes):
    keep, fake = keep_for(notes, fail_login=True)
    with pytest.raises(gkeep.KeepError, match="sign in"):
        keep.lists()
    with pytest.raises(gkeep.KeepError, match="retry"):
        keep.lists()
    assert fake.logins == 1


@pytest.mark.parametrize("a,b", [("To-do", "todo"), ("To do", "TODO"), ("Handla!", "handla")])
def test_normalize_title(a, b):
    assert gkeep.normalize_title(a) == gkeep.normalize_title(b)
