"""Settings, read once from environment variables (see .env.example)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


def _bool(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _list(value: str | None) -> tuple[str, ...]:
    return tuple(part.strip() for part in (value or "").split(",") if part.strip())


@dataclass(frozen=True)
class Settings:
    access_key: str = ""
    data_dir: Path = Path("data")
    demo: bool = False
    title: str = ""
    language: str = ""

    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://localhost:8080/auth/google/callback"
    google_refresh_token: str = ""

    calendars: tuple[str, ...] = ()

    keep_email: str = ""
    keep_master_token: str = ""
    keep_lists: tuple[str, ...] = ("family",)  # or "shared", or note titles

    supabase_url: str = ""
    supabase_key: str = ""
    # Set on Vercel: the dashboard is on the public internet and has no persistent disk.
    serverless: bool = False

    # Weather (Open-Meteo); off unless a place is set.
    weather_lat: float | None = None
    weather_lon: float | None = None
    weather_place: str = ""
    # EuroBonus points read from Gmail.
    eurobonus: bool = True
    # Voice commands ("ASCA, lägg till mjölk i inköpslistan") are read by Claude.
    anthropic_key: str = ""
    assistant_name: str = "ASCA"  # the wake word
    assistant_aliases: tuple[str, ...] = ()  # how speech recognition tends to mishear it
    # Split, the companion cost-sharing app: its two people as (id, name), first person first.
    split_people: tuple[tuple[str, str], ...] = ()
    split_ratio_column: str = "first_ratio_ppm"  # where Split keeps the first person's share
    finances_url: str = ""  # a "Family finances" link in the balances panel

    # Google accounts that are always admins; more members can live in the dashboard_members table.
    admins: tuple[str, ...] = ()
    session_secret: str = ""

    @property
    def google_configured(self) -> bool:
        return bool(self.google_client_id and self.google_client_secret)

    @property
    def supabase_configured(self) -> bool:
        return bool(self.supabase_url and self.supabase_key)

    @property
    def weather_configured(self) -> bool:
        return self.weather_lat is not None and self.weather_lon is not None

    @property
    def keep_configured(self) -> bool:
        return bool(self.keep_email and self.keep_master_token)


def _float(value: str | None) -> float | None:
    try:
        return float(value) if value and value.strip() else None
    except ValueError:
        return None


# How Safari's Swedish speech recognition tends to hear the default wake word.
ASCA_ALIASES = ("aska", "asker", "ask a", "ascha", "ascar")


def _aliases(env: Mapping[str, str]) -> tuple[str, ...]:
    if (env.get("ASSISTANT_ALIASES") or "").strip():
        return tuple(a.lower() for a in _list(env.get("ASSISTANT_ALIASES")))
    return ASCA_ALIASES if (env.get("ASSISTANT_NAME") or "ASCA").strip().upper() == "ASCA" else ()


def _people(value: str | None) -> tuple[tuple[str, str], ...]:
    """SPLIT_PEOPLE="alex=Alex,sam=Sam": Split's person ids and names. Anything but two turns Split off."""
    pairs = []
    for part in _list(value):
        pid, _, name = part.partition("=")
        pairs.append((pid.strip().lower(), (name or pid).strip()))
    return tuple(pairs) if len(pairs) == 2 else ()


def _redirect_uri(env: Mapping[str, str], default: str) -> str:
    if env.get("GOOGLE_REDIRECT_URI", "").strip():
        return env["GOOGLE_REDIRECT_URI"].strip()
    production_host = env.get("VERCEL_PROJECT_PRODUCTION_URL", "").strip()
    if production_host:
        return f"https://{production_host}/auth/google/callback"
    return default


def load_settings(env: Mapping[str, str] = os.environ) -> Settings:
    defaults = Settings()
    return Settings(
        access_key=env.get("DASHBOARD_ACCESS_KEY", "").strip(),
        data_dir=Path(env.get("DATA_DIR") or defaults.data_dir),
        demo=_bool(env.get("DEMO_MODE")),
        title=env.get("DASHBOARD_TITLE", "").strip(),
        language=env.get("DASHBOARD_LANGUAGE", "").strip().lower(),
        google_client_id=env.get("GOOGLE_CLIENT_ID", "").strip(),
        google_client_secret=env.get("GOOGLE_CLIENT_SECRET", "").strip(),
        google_redirect_uri=_redirect_uri(env, defaults.google_redirect_uri),
        google_refresh_token=env.get("GOOGLE_REFRESH_TOKEN", "").strip(),
        calendars=_list(env.get("CALENDARS")),
        keep_email=env.get("KEEP_EMAIL", "").strip(),
        keep_master_token=env.get("KEEP_MASTER_TOKEN", "").strip(),
        keep_lists=_list(env.get("KEEP_LISTS")) or defaults.keep_lists,
        supabase_url=env.get("SUPABASE_URL", "").strip(),
        supabase_key=(env.get("SUPABASE_SECRET_KEY") or env.get("SUPABASE_SERVICE_ROLE_KEY") or "").strip(),
        serverless=bool(env.get("VERCEL")),
        admins=tuple(e.lower() for e in _list(env.get("DASHBOARD_ADMINS"))),
        session_secret=env.get("SESSION_SECRET", "").strip(),
        weather_lat=_float(env.get("WEATHER_LAT")),
        weather_lon=_float(env.get("WEATHER_LON")),
        weather_place=env.get("WEATHER_PLACE", "").strip(),
        eurobonus=(env.get("SHOW_EUROBONUS") or "true").strip().lower() not in {"0", "false", "no", "off"},
        anthropic_key=env.get("ANTHROPIC_API_KEY", "").strip(),
        assistant_name=(env.get("ASSISTANT_NAME") or defaults.assistant_name).strip(),
        assistant_aliases=_aliases(env),
        split_people=_people(env.get("SPLIT_PEOPLE")),
        split_ratio_column=(env.get("SPLIT_RATIO_COLUMN") or defaults.split_ratio_column).strip(),
        finances_url=env.get("FINANCES_URL", "").strip(),
    )
