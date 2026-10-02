"""Points and balances for the "Points & balances" panel.

EuroBonus: SAS's EuroBonus emails carry a header like "EBS 123456789 146553 poäng Uppdaterat
2026-09-17" (tier, member number, points, date). The newest one that has it gives the balance.
The member number is never passed on.
"""

from __future__ import annotations

import base64
import html
import re

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

EUROBONUS_QUERY = "from:msg.flysas.com (poäng OR points) newer_than:120d"
TIERS = {"EBB": "Basic", "EBS": "Silver", "EBG": "Gold", "EBD": "Diamond", "EBP": "Pandion"}
_EUROBONUS = re.compile(
    r"\b(EB[BSGDP])\b\D{0,40}?\d{6,12}\D{0,20}?(\d[\d\s.,  ]*?)\s*(?:poäng|points)\b"
    r"\D{0,40}?(\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)


def parse_eurobonus(text: str) -> dict | None:
    match = _EUROBONUS.search(" ".join(text.split()))
    if not match:
        return None
    tier, points, updated = match.groups()
    return {
        "name": "EuroBonus",
        "value": int(re.sub(r"\D", "", points)),
        "unit": "points",
        "detail": TIERS.get(tier.upper(), tier.upper()),
        "updated": updated,
    }


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def message_text(message: dict) -> str:
    """Plain text of a Gmail API message (format=full), from text/plain or tag-stripped HTML."""
    plain: list[str] = []
    rich: list[str] = []

    def walk(part: dict) -> None:
        data = part.get("body", {}).get("data")
        mime = part.get("mimeType", "")
        if data and mime == "text/plain":
            plain.append(_decode(data))
        elif data and mime == "text/html":
            rich.append(_decode(data))
        for child in part.get("parts", []) or []:
            walk(child)

    walk(message.get("payload", {}))
    stripped = [re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(style|script)\b.*?</\1>", " ", h)) for h in rich]
    return html.unescape(" ".join(plain + stripped + [message.get("snippet", "")]))


def fetch_eurobonus(session, max_messages: int = 5) -> dict | None:
    resp = session.get(f"{GMAIL_API}/messages", params={"q": EUROBONUS_QUERY, "maxResults": max_messages}, timeout=15)
    resp.raise_for_status()
    for ref in resp.json().get("messages", []):
        full = session.get(f"{GMAIL_API}/messages/{ref['id']}", params={"format": "full"}, timeout=15)
        full.raise_for_status()
        found = parse_eurobonus(message_text(full.json()))
        if found:
            return found
    return None

