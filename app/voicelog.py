"""The log of voice commands: what was said, what it did, and how to undo it.

Kept in the store (the family's Supabase table) under one key, newest first, capped at MAX_ENTRIES,
so any iPad or phone sees the same log and "ASCA, ångra" (the wake word is configurable) works
wherever the command was given.
"""

from __future__ import annotations

import logging
import re
import secrets
import threading

from .store import Store, StoreError

log = logging.getLogger(__name__)

KEY = "voice_log"
MAX_ENTRIES = 50
MAX_ANSWER_IN_LOG = 500

# "ångra", "ångra det", "ta bort det där", "glöm det", "undo": handled without asking Claude.
_UNDO = re.compile(r"(ångra( det( där)?| senaste)?|ta bort det( där)?|glöm det|undo( that)?)", re.IGNORECASE)


def is_undo(text: str, wake_words: tuple[str, ...] = ("ASCA",)) -> bool:
    wake = "|".join(re.escape(w) for w in wake_words if w)
    words = re.sub(rf"^\W*({wake})\b", "", text, flags=re.IGNORECASE) if wake else text
    return bool(_UNDO.fullmatch(words.strip(" \t,.!?")))


def describe_sv(action: dict) -> str:
    """A few Swedish words for what an action did, for "Jag har ångrat …"."""
    kind = action.get("type")
    if kind == "list_items":
        items = action.get("items", [])
        what = f"{', '.join(items[:-1])} och {items[-1]}" if len(items) > 1 else (items[0] if items else "")
        return f"{what} i {action.get('list', 'listan')}"
    if kind == "event":
        return f"{action.get('title', 'händelsen')} i kalendern"
    if kind == "expense":
        kronor = f"{round(action.get('amount', 0) / 100):,}".replace(",", " ")
        return f"{action.get('description', 'utgiften')} på {kronor} kronor i Split"
    return ""


class VoiceLog:
    def __init__(self, store: Store) -> None:
        self._store = store
        self._lock = threading.Lock()  # one read-modify-write at a time within this instance

    def entries(self) -> list[dict]:
        try:
            return list((self._store.get(KEY) or {}).get("entries", []))
        except StoreError:
            log.warning("Could not read the voice log", exc_info=True)
            return []

    def _save(self, entries: list[dict]) -> None:
        self._store.set(KEY, {"entries": entries[:MAX_ENTRIES]})

    def add(self, entry: dict) -> str:
        entry = {**entry, "id": secrets.token_hex(6), "undone": False}
        for action in entry.get("done", []):
            if action.get("type") == "answer":
                action["text"] = action.get("text", "")[:MAX_ANSWER_IN_LOG]
        with self._lock:
            try:
                self._save([entry, *self.entries()])
            except StoreError:
                log.warning("Could not save to the voice log", exc_info=True)
        return entry["id"]

    def find(self, entry_id: str | None = None) -> dict | None:
        """The entry with this id, or without an id the latest one that can still be undone."""
        for entry in self.entries():
            if entry_id is not None and entry["id"] == entry_id:
                return entry
            if entry_id is None and entry.get("undo") and not entry.get("undone"):
                return entry
        return None

    def mark_undone(self, entry_id: str) -> None:
        with self._lock:
            entries = self.entries()
            for entry in entries:
                if entry["id"] == entry_id:
                    entry["undone"] = True
            try:
                self._save(entries)
            except StoreError:
                log.warning("Could not update the voice log", exc_info=True)


def public(entry: dict) -> dict:
    """An entry as the iPad sees it: no undo internals."""
    shown = {k: v for k, v in entry.items() if k != "undo"}
    shown["undoable"] = bool(entry.get("undo")) and not entry.get("undone")
    return shown
