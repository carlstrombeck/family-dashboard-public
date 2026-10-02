"""Google Keep notes (shopping list, to-dos) via the unofficial gkeepapi client.

Google offers no Keep API for personal accounts, so this signs in with a master token the
same way the Android app does. It works, but Google can break it without notice.

Which notes show up (KEEP_LISTS):
- "family" (default): notes shared with the Google family group,
- "shared": notes shared with anyone,
- otherwise a comma-separated list of note titles.

In family and shared mode an admin can hide some of those notes on /setup.

Nothing from Keep is written to disk or the database, apart from the ids of hidden notes: the
account may be someone's personal one, and the dashboard should only ever hold the notes it
shows, in memory.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from collections.abc import Callable, Collection, Sequence

import gkeepapi
from gkeepapi.node import List as KeepList
from gkeepapi.node import NewListItemPlacementValue
from gkeepapi.node import TopLevelNode

log = logging.getLogger(__name__)

SYNC_INTERVAL = 20  # seconds between background syncs when the iPad polls
RETRY_AFTER_FAILURE = 300  # don't hammer Google's login after an auth failure
MAX_ITEM_LENGTH = 200
MAX_SHARED_NOTES = 4  # the lists column fits about this many
FAMILY, SHARED = "family", "shared"


class KeepError(Exception):
    pass


class ItemNotFound(KeepError):
    pass


# gkeepapi keeps only the email and role of each person a note is shared with and drops the
# rest, including whatever marks a family-group share. Keep the raw entries per note id.
_sharing: dict[str, list[dict]] = {}
_original_load = TopLevelNode._load


def _load_keeping_sharing(self, raw: dict) -> None:
    _original_load(self, raw)
    _sharing[self.id] = [entry for entry in raw.get("roleInfo", []) if isinstance(entry, dict)]


TopLevelNode._load = _load_keeping_sharing


def sharing_of(note) -> str | None:
    """'family' if shared with a family group, 'people' if shared otherwise, None if private."""
    others = [entry for entry in _sharing.get(note.id, []) if entry.get("role") != "O"]
    if not others:
        return None
    if any("FAMILY" in str(value).upper() for entry in others for value in entry.values()):
        return FAMILY
    return "people"


def normalize_title(title: str | None) -> str:
    """'To-do', 'todo' and 'To do' all match."""
    return re.sub(r"[\W_]+", "", (title or "").casefold())


def serialize(note) -> dict:
    if isinstance(note, KeepList):
        return {
            "id": note.id,
            "title": note.title,
            "kind": "list",
            "items": [
                {"id": item.id, "text": item.text, "checked": item.checked, "indented": item.indented}
                for item in note.items
            ],
        }
    return {"id": note.id, "title": note.title, "kind": "note", "text": note.text, "items": []}


class KeepLists:
    def __init__(
        self,
        email: str,
        master_token: str,
        selection: Sequence[str],
        keep_factory: Callable[[], gkeepapi.Keep] = gkeepapi.Keep,
        hidden: Callable[[], Collection[str]] = lambda: (),
    ) -> None:
        self._email = email
        self._master_token = master_token
        wanted = [s.strip() for s in selection if s.strip()]
        self._mode = wanted[0].lower() if len(wanted) == 1 and wanted[0].lower() in (FAMILY, SHARED) else None
        self._titles = [] if self._mode or not wanted else wanted
        if not wanted:
            self._mode = FAMILY
        self._factory = keep_factory
        self._hidden = hidden
        self._lock = threading.Lock()
        self._keep: gkeepapi.Keep | None = None
        self._synced_at = 0.0
        self._failed_at: float | None = None
        self._last_error = ""

    @property
    def description(self) -> str:
        if self._mode == FAMILY:
            return "notes shared with your Google family group"
        if self._mode == SHARED:
            return "notes shared with anyone"
        return "notes titled " + ", ".join(self._titles)

    @property
    def choosable(self) -> bool:
        """Notes can be hidden on /setup, except when they're picked by title."""
        return self._mode is not None

    # -- connection -------------------------------------------------------------------------

    def _fail(self, message: str, exc: Exception, backoff: bool) -> KeepError:
        """Drop the client so the next call starts clean; after a sign-in failure, also wait a while."""
        self._keep = None
        if backoff:
            self._failed_at = time.monotonic()
            self._last_error = message
        log.warning("%s", message, exc_info=exc)
        return KeepError(message)

    def _client(self) -> gkeepapi.Keep:
        if self._keep is not None:
            return self._keep
        if self._failed_at is not None and time.monotonic() - self._failed_at < RETRY_AFTER_FAILURE:
            raise KeepError(f"{self._last_error} (will retry in a few minutes)")
        keep = self._factory()
        try:
            keep.authenticate(self._email, self._master_token)
        except Exception as exc:  # noqa: BLE001
            raise self._fail(f"Could not sign in to Google Keep: {exc}", exc, backoff=True) from exc
        self._keep = keep
        self._failed_at = None
        self._synced_at = time.monotonic()
        return keep

    def _sync(self, force: bool = False) -> gkeepapi.Keep:
        keep = self._client()
        if force or time.monotonic() - self._synced_at >= SYNC_INTERVAL:
            try:
                keep.sync()
            except Exception as exc:  # noqa: BLE001
                raise self._fail(f"Google Keep sync failed: {exc}", exc, backoff=False) from exc
            self._synced_at = time.monotonic()
        return keep

    # -- lookups ----------------------------------------------------------------------------

    def _notes(self, keep: gkeepapi.Keep) -> list:
        notes = [n for n in keep.all() if not n.trashed and not n.deleted]
        # Prefer notes that aren't archived when two share a title.
        return sorted(notes, key=lambda n: n.archived)

    def _find(self, keep: gkeepapi.Keep, title: str):
        wanted = normalize_title(title)
        return next((n for n in self._notes(keep) if normalize_title(n.title) == wanted), None)

    def _candidates(self, keep: gkeepapi.Keep) -> list:
        """Family (or shared) notes that may be shown, pinned first, then most recently edited."""
        accepted = (FAMILY,) if self._mode == FAMILY else (FAMILY, "people")
        notes = [n for n in self._notes(keep) if not n.archived and sharing_of(n) in accepted]
        notes.sort(key=lambda n: (not n.pinned, -n.timestamps.updated.timestamp()))
        return notes

    def _shown(self, keep: gkeepapi.Keep) -> list:
        """The notes on the dashboard, in order; for title mode, None where a title matched nothing."""
        if not self._mode:
            return [self._find(keep, title) for title in self._titles]
        hidden = set(self._hidden())
        return [n for n in self._candidates(keep) if n.id not in hidden][:MAX_SHARED_NOTES]

    def _shown_list(self, keep: gkeepapi.Keep, list_id: str) -> KeepList:
        """Only checklists on the dashboard can be changed from it."""
        for note in self._shown(keep):
            if isinstance(note, KeepList) and note.id == list_id:
                return note
        raise ItemNotFound("That list isn't on the dashboard.")

    # -- public API -------------------------------------------------------------------------

    def lists(self) -> list[dict]:
        with self._lock:
            keep = self._sync()
            result = []
            for title, note in zip(self._titles or [None] * MAX_SHARED_NOTES, self._shown(keep)):
                if note is None:
                    result.append({"title": title, "error": "not_found"})
                elif self._titles and not isinstance(note, KeepList):
                    result.append({"title": note.title, "error": "not_a_checklist"})
                else:
                    result.append(serialize(note))
            return result

    def overview(self) -> list[dict]:
        """For /setup: only the notes that may be shown, so titles of private notes stay private."""
        with self._lock:
            keep = self._sync()
            shown = [n for n in self._shown(keep) if n is not None]
            notes = self._candidates(keep) if self._mode else shown
            hidden = set(self._hidden()) if self._mode else set()
            return [
                {"id": n.id, "title": n.title or "(untitled)", "checklist": isinstance(n, KeepList),
                 "sharing": sharing_of(n) or "private", "hidden": n.id in hidden,
                 "shown": any(n.id == s.id for s in shown)}
                for n in notes
            ]

    def _items(self, list_id: str, *item_ids: str | None) -> tuple[KeepList, list]:
        """The list and the items with these ids (None stays None), syncing once if one is missing."""
        keep = self._client()
        for attempt in range(2):
            note = self._shown_list(keep, list_id)
            by_id = {i.id: i for i in note.items}
            found = [by_id.get(i) if i is not None else None for i in item_ids]
            if all(f is not None for f, i in zip(found, item_ids) if i is not None):
                return note, found
            if attempt == 0:
                keep = self._sync(force=True)  # maybe it was added on a phone since our last sync
        raise ItemNotFound("That item is no longer on the list.")

    def set_checked(self, list_id: str, item_id: str, checked: bool) -> dict:
        with self._lock:
            note, (item,) = self._items(list_id, item_id)
            item.checked = checked
            self._sync(force=True)
            return serialize(note)

    def move_item(self, list_id: str, item_id: str, after_id: str | None) -> dict:
        """Put an item (with its sub-items) right below another top-level item, or first if after_id is None."""
        with self._lock:
            note, (item, after) = self._items(list_id, item_id, after_id)
            if item.indented or (after is not None and after.indented):
                raise KeepError("Only top-level items can be moved.")
            if after is item:
                return serialize(note)
            # Keep shows higher sort values first; sub-items follow their parent's value.
            top = [i for i in note.items if not i.indented and i is not item]
            index = top.index(after) + 1 if after is not None else 0
            above = top[index - 1].sort if index > 0 else None
            below = top[index].sort if index < len(top) else None
            if above is None:
                item.sort = (below if below is not None else 0) + KeepList.SORT_DELTA
            elif below is None:
                item.sort = above - KeepList.SORT_DELTA
            elif above - below >= 2:
                item.sort = (above + below) // 2
            else:
                # No room between the neighbours: space everything out again in the new order.
                top.insert(index, item)
                start = top[0].sort
                for n, node in enumerate(top):
                    node.sort = start - n * KeepList.SORT_DELTA
            self._sync(force=True)
            return serialize(note)

    def add_item(self, list_id: str, text: str) -> dict:
        return self.add_items(list_id, [text])[0]

    def add_items(self, list_id: str, texts: Sequence[str]) -> tuple[dict, list[str]]:
        """Add items at the bottom; returns the list and the new items' ids."""
        texts = [t for t in (" ".join(t.split())[:MAX_ITEM_LENGTH] for t in texts) if t]
        if not texts:
            raise KeepError("Type something to add.")
        with self._lock:
            keep = self._client()
            note = self._shown_list(keep, list_id)
            ids = [note.add(text, False, NewListItemPlacementValue.Bottom).id for text in texts]
            self._sync(force=True)
            return serialize(note), ids

    def delete_item(self, list_id: str, item_id: str) -> dict:
        """Undo for items added by voice."""
        with self._lock:
            note, (item,) = self._items(list_id, item_id)
            item.delete()
            self._sync(force=True)
            return serialize(note)
