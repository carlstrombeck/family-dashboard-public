"""Where the dashboard keeps its few pieces of state: the Google refresh token and the Keep cache.

A folder on disk for Docker/home servers, or a Supabase table when running on Vercel, whose
functions have no persistent disk.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

import requests

SUPABASE_TABLE = "dashboard_kv"
MEMBERS_TABLE = "dashboard_members"
# Split, a companion app for two people sharing costs in proportion to income, can keep its tables
# in the same Supabase project. The dashboard shows its balance and adds expenses by voice; the
# rules match Split's own. Split stores the FIRST person's share of each expense (parts per
# million) in a column whose name is a setting, SPLIT_RATIO_COLUMN.
SPLIT_INCOMES, SPLIT_ENTRIES = "split_incomes", "split_entries"
PPM = 1_000_000


class StoreError(Exception):
    pass


class Store(Protocol):
    description: str

    def get(self, key: str) -> dict | None: ...

    def set(self, key: str, value: dict) -> None: ...

    def members(self) -> dict[str, str]: ...


class FileStore:
    has_members_table = False

    def __init__(self, directory: Path) -> None:
        self._dir = directory
        self.description = f"folder {directory}"

    def members(self) -> dict[str, str]:
        return {}  # without Supabase, members come from DASHBOARD_ADMINS only

    def add_split_expense(self, fields: dict, people: tuple[str, str], ratio_column: str) -> int:
        raise StoreError("Split expenses need the Supabase store.")

    def delete_split_expense(self, entry_id: int) -> bool:
        raise StoreError("Split expenses need the Supabase store.")

    def split_entries(self, ratio_column: str) -> list[dict]:
        raise StoreError("Split needs the Supabase store.")

    def get(self, key: str) -> dict | None:
        try:
            return json.loads((self._dir / f"{key}.json").read_text())
        except (FileNotFoundError, ValueError):
            return None
        except OSError as exc:
            raise StoreError(f"Could not read {key}: {exc}") from exc

    def set(self, key: str, value: dict) -> None:
        path = self._dir / f"{key}.json"
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(value))
            os.chmod(tmp, 0o600)
            tmp.replace(path)
        except OSError as exc:
            raise StoreError(f"Could not save {key} in {self._dir}: {exc}") from exc


class SupabaseStore:
    """Rows in a Supabase table, through its REST API with the project's secret key."""

    has_members_table = True

    def __init__(self, url: str, key: str) -> None:
        self._rest = f"{url.rstrip('/')}/rest/v1"
        self._endpoint = f"{self._rest}/{SUPABASE_TABLE}"
        self._headers = {"apikey": key}
        if not key.startswith("sb_"):
            # Legacy service_role keys are JWTs and also go in Authorization. The newer
            # sb_secret_ keys must not: the gateway would reject them as an invalid JWT.
            self._headers["Authorization"] = f"Bearer {key}"
        self.description = f"Supabase table {SUPABASE_TABLE}"

    def _check(self, resp: requests.Response, action: str) -> None:
        if resp.ok:
            return
        try:
            body = resp.json()
            detail = body.get("message") or body.get("error") or resp.text[:200]
            code = body.get("code", "")
        except ValueError:
            detail, code = resp.text[:200], ""
        if code == "PGRST205" or "does not exist" in str(detail):
            detail = f"a dashboard table is missing. Run the SQL in supabase/migrations/ in the Supabase SQL editor. ({detail})"
        raise StoreError(f"Supabase {action} failed ({resp.status_code}): {detail}")

    def get(self, key: str) -> dict | None:
        try:
            resp = requests.get(
                self._endpoint,
                params={"key": f"eq.{key}", "select": "value"},
                headers=self._headers,
                timeout=10,
            )
        except requests.RequestException as exc:
            raise StoreError(f"Could not reach Supabase: {exc}") from exc
        self._check(resp, "read")
        rows = resp.json()
        return rows[0]["value"] if rows else None

    def set(self, key: str, value: dict) -> None:
        row = {"key": key, "value": value, "updated_at": datetime.now(timezone.utc).isoformat()}
        try:
            resp = requests.post(
                self._endpoint,
                params={"on_conflict": "key"},
                json=row,
                headers={**self._headers, "Prefer": "resolution=merge-duplicates,return=minimal"},
                timeout=10,
            )
        except requests.RequestException as exc:
            raise StoreError(f"Could not reach Supabase: {exc}") from exc
        self._check(resp, "write")

    def members(self) -> dict[str, str]:
        try:
            resp = requests.get(
                f"{self._rest}/{MEMBERS_TABLE}", params={"select": "email,role"}, headers=self._headers, timeout=10
            )
        except requests.RequestException as exc:
            raise StoreError(f"Could not reach Supabase: {exc}") from exc
        self._check(resp, "members read")
        return {row["email"].strip().lower(): row["role"] for row in resp.json()}

    def add_split_expense(self, fields: dict, people: tuple[str, str], ratio_column: str) -> int:
        """Insert an expense with the split in effect on its date; returns the new row id."""
        try:
            resp = requests.get(f"{self._rest}/{SPLIT_INCOMES}", params={"select": "*"}, headers=self._headers, timeout=10)
            self._check(resp, "Split incomes read")
            ratio = split_ratio(resp.json(), fields["date"], people)
            if ratio is None:
                raise StoreError("Set both incomes in Split first.")
            resp = requests.post(
                f"{self._rest}/{SPLIT_ENTRIES}",
                json={**fields, "type": "expense", ratio_column: ratio},
                headers={**self._headers, "Prefer": "return=representation"},
                timeout=10,
            )
        except requests.RequestException as exc:
            raise StoreError(f"Could not reach Supabase: {exc}") from exc
        self._check(resp, "Split expense write")
        return int(resp.json()[0]["id"])


    def split_entries(self, ratio_column: str) -> list[dict]:
        """Split's expenses and settle-ups: only the columns the balance needs, no descriptions.
        The share column comes back as ratio_ppm, whatever it's called in Split's table.

        Supabase returns at most 1000 rows per request, so this reads page by page."""
        rows: list[dict] = []
        while True:
            try:
                resp = requests.get(
                    f"{self._rest}/{SPLIT_ENTRIES}",
                    params={"select": f"type,amount,paid_by,paid_to,ratio_ppm:{ratio_column}", "order": "id.asc",
                            "limit": 1000, "offset": len(rows)},
                    headers=self._headers,
                    timeout=10,
                )
            except requests.RequestException as exc:
                raise StoreError(f"Could not reach Supabase: {exc}") from exc
            self._check(resp, "Split read")
            page = resp.json()
            if not page:
                return rows
            rows += page

    def delete_split_expense(self, entry_id: int) -> bool:
        try:
            resp = requests.delete(
                f"{self._rest}/{SPLIT_ENTRIES}",
                params={"id": f"eq.{int(entry_id)}", "type": "eq.expense"},
                headers={**self._headers, "Prefer": "return=representation"},
                timeout=10,
            )
        except requests.RequestException as exc:
            raise StoreError(f"Could not reach Supabase: {exc}") from exc
        self._check(resp, "Split expense delete")
        return bool(resp.json())


def split_ratio(incomes: list[dict], day: str, people: tuple[str, str]) -> int | None:
    """The first person's share (parts per million) on `day`; before someone's first income row, that row applies."""
    amounts = []
    for person in people:
        rows = sorted((r for r in incomes if r["person"] == person), key=lambda r: (r["effective_from"], r["id"]))
        if not rows:
            return None
        current = rows[0]
        for row in rows:
            if row["effective_from"] <= day:
                current = row
        amounts.append(int(current["monthly_amount"]))
    first, second = amounts
    total = first + second
    if total <= 0:
        return None
    return (2 * first * PPM + total) // (2 * total)


def split_balance(entries: list[dict], people: tuple[str, str]) -> dict:
    """Who owes whom in Split, in öre: {"owes", "to", "amount"} (same rules as the Split app)."""
    first, second = people
    balance = 0  # what the first person paid minus their share; positive means the second owes them
    for entry in entries:
        amount = int(entry["amount"])
        if entry["type"] == "settlement":
            share = amount if entry["paid_to"] == first else 0
        else:
            share = (amount * int(entry["ratio_ppm"]) + PPM // 2) // PPM
        balance += (amount if entry["paid_by"] == first else 0) - share
    if balance == 0:
        return {"owes": None, "to": None, "amount": 0}
    if balance > 0:
        return {"owes": second, "to": first, "amount": balance}
    return {"owes": first, "to": second, "amount": -balance}


def make_store(settings) -> Store:
    if settings.supabase_configured:
        return SupabaseStore(settings.supabase_url, settings.supabase_key)
    return FileStore(settings.data_dir)
