"""Exchange a one-time Google oauth_token cookie for the Keep master token.

Usage (see README, "Google Keep setup"):
    python scripts/keep_master_token.py

The master token gives full access to the Google account, so treat it like a password.
"""

from __future__ import annotations

import getpass
import secrets

import gpsoauth


def main() -> None:
    email = input("Google account email: ").strip()
    oauth_token = getpass.getpass("oauth_token cookie value (input hidden): ").strip()
    android_id = secrets.token_hex(8)

    result = gpsoauth.exchange_token(email, oauth_token, android_id)
    token = result.get("Token")
    if not token:
        raise SystemExit(
            f"Google refused the exchange: {result.get('Error') or result}.\n"
            "The oauth_token cookie is single-use and expires within minutes; get a fresh one and retry."
        )
    print("\nAdd these lines to .env:\n")
    print(f"KEEP_EMAIL={email}")
    print(f"KEEP_MASTER_TOKEN={token}")


if __name__ == "__main__":
    main()
