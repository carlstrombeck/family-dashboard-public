"""Family photos for the photo tile, chosen in Google Photos' own picker.

Since 2025 Google no longer lets apps read a Google Photos library. The Picker API instead lets an
admin choose photos on Google's own page; the dashboard then copies those photos (resized by Google)
into the store, because the picker's links expire after an hour. Only photos are copied, no videos,
and nothing but the image and its date is kept.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime

from .store import Store, StoreError

log = logging.getLogger(__name__)

API = "https://photospicker.googleapis.com/v1"
SCOPE = "https://www.googleapis.com/auth/photospicker.mediaitems.readonly"
INDEX_KEY = "photos"
PAGE_SIZE = 8  # photos copied per request, so each stays well within a serverless time limit
MAX_PHOTOS = 1000
SIZE = "=w2048-h2048"  # Google resizes to fit 2048 px, keeping the shape


class PhotosError(Exception):
    pass


def _json(resp, action: str) -> dict:
    if resp.status_code == 403 and "SERVICE_DISABLED" in resp.text:
        raise PhotosError("The Photos Picker API isn't enabled in the Google Cloud project yet.")
    if not resp.ok:
        raise PhotosError(f"Google Photos {action} failed ({resp.status_code}): {resp.text[:200]}")
    return resp.json() if resp.content else {}


def _seconds(duration: str | None, default: float) -> float:
    try:
        return float(str(duration).rstrip("s"))
    except (TypeError, ValueError):
        return default


def start(session) -> dict:
    """A picker session: the page where photos are chosen, and how often to ask whether that's done."""
    data = _json(session.post(f"{API}/sessions", json={}, timeout=15), "start")
    return {
        "id": data["id"],
        "pickerUri": data["pickerUri"].rstrip("/") + "/autoclose",
        "pollSeconds": max(2.0, _seconds((data.get("pollingConfig") or {}).get("pollInterval"), 4)),
    }


def chosen(session, session_id: str) -> bool:
    return bool(_json(session.get(f"{API}/sessions/{session_id}", timeout=15), "check").get("mediaItemsSet"))


def finish(session, session_id: str) -> None:
    try:
        session.delete(f"{API}/sessions/{session_id}", timeout=15)
    except Exception:  # noqa: BLE001 - an unfinished session simply expires
        log.info("Could not close picker session", exc_info=True)


def index(store: Store) -> list[dict]:
    return list((store.get(INDEX_KEY) or {}).get("items", []))


def copy_page(session, store: Store, session_id: str, page_token: str = "") -> dict:
    """Copy one page of the chosen photos into the store; returns how many and the next page."""
    params = {"sessionId": session_id, "pageSize": PAGE_SIZE}
    if page_token:
        params["pageToken"] = page_token
    data = _json(session.get(f"{API}/mediaItems", params=params, timeout=20), "list")
    items = index(store)
    known = {item["google"] for item in items}
    added = 0
    for media in data.get("mediaItems", []):
        file = media.get("mediaFile") or {}
        if media.get("type") != "PHOTO" or media.get("id") in known or not file.get("baseUrl"):
            continue
        if len(items) >= MAX_PHOTOS:
            break
        resp = session.get(file["baseUrl"] + SIZE, timeout=30)
        if not resp.ok:
            log.warning("Could not download a chosen photo (%s)", resp.status_code)
            continue
        photo_id = secrets.token_hex(8)
        store.put_blob(f"{photo_id}.jpg", resp.content, resp.headers.get("content-type", "image/jpeg"))
        items.append({"id": photo_id, "google": media["id"], "taken": media.get("createTime", "")})
        known.add(media["id"])
        added += 1
    store.set(INDEX_KEY, {"items": items, "updated": datetime.now().isoformat(timespec="seconds")})
    return {"added": added, "total": len(items), "next": data.get("nextPageToken", "")}


def remove_all(store: Store) -> None:
    items = index(store)
    store.set(INDEX_KEY, {"items": []})
    try:
        store.delete_blobs([f"{item['id']}.jpg" for item in items])
    except StoreError:
        log.warning("Some photo files could not be deleted", exc_info=True)
