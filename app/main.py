"""HTTP API + static front end for the kitchen dashboard."""

from __future__ import annotations

import html
import logging
import secrets
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
from fastapi import Body, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import accounts, demo, gcal, weather
from .assistant import Assistant, AssistantError
from .voicelog import VoiceLog, describe_sv, is_undo, public
from .auth import REFRESH_AFTER, SESSION_COOKIE, SESSION_DAYS, Members, make_session, read_session, session_key
from .cache import TTLCache
from .config import Settings, load_settings
from .gkeep import MAX_SHARED_NOTES, ItemNotFound, KeepError, KeepLists
from .google_auth import AuthError, GoogleAuth, NotConnected, is_revoked, parse_redirect_url
from .store import Store, StoreError, make_store, split_balance

log = logging.getLogger(__name__)

# On Vercel the CDN serves public/ itself; locally and in Docker FastAPI does.
STATIC_DIR = Path(__file__).resolve().parent.parent / "public"
COOKIE = "fd_key"
COOKIE_MAX_AGE = 10 * 365 * 24 * 3600
OAUTH_COOKIE = "fd_oauth"
NEXT_COOKIE = "fd_next"
MAX_RANGE = timedelta(days=42)
MAX_EVENT_LENGTH = timedelta(days=31)
SPLIT_CACHE = "split_balance"
CHECKLISTS_CACHE = "voice_checklists"
KEEP_HIDDEN = "keep_hidden"  # store key: ids of family notes an admin hid on /setup


class NewEvent(BaseModel):
    calendar: str
    title: str = Field(max_length=200)
    allDay: bool = False
    start: str  # all-day: YYYY-MM-DD; otherwise an ISO date-time with UTC offset
    end: str | None = None  # all-day: last day (inclusive); otherwise the end time
    location: str = Field("", max_length=200)


def _event_body(event: NewEvent) -> dict:
    title = " ".join(event.title.split())
    if not title:
        raise ApiError(400, "empty", "Give the event a name.")
    try:
        if event.allDay:
            first = date.fromisoformat(event.start)
            last = date.fromisoformat(event.end) if event.end else first
            start, end = first, last + timedelta(days=1)
        else:
            start = datetime.fromisoformat(event.start)
            end = datetime.fromisoformat(event.end) if event.end else start + timedelta(hours=1)
            if start.tzinfo is None or end.tzinfo is None:
                raise ValueError("no UTC offset")
    except ValueError as exc:
        raise ApiError(400, "bad_time", "Pick a valid date and time.") from exc
    if end <= start:
        raise ApiError(400, "bad_time", "The event has to end after it starts.")
    if end - start > MAX_EVENT_LENGTH:
        raise ApiError(400, "bad_time", "Events can be at most 31 days long.")
    return gcal.event_body(title, start, end, " ".join(event.location.split()))


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _google_error_message(exc: requests.HTTPError) -> str:
    try:
        return exc.response.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return f"HTTP {exc.response.status_code}"


def _is_https(request: Request) -> bool:
    forwarded = request.headers.get("x-forwarded-proto", request.url.scheme)
    return forwarded.split(",")[0].strip() == "https"


def create_app(
    settings: Settings | None = None, keep_factory: Callable | None = None, store: Store | None = None,
    assistant: Assistant | None = None,
) -> FastAPI:
    settings = settings or load_settings()
    store = store or make_store(settings)
    google = GoogleAuth(settings, store)
    cache = TTLCache()

    def hidden_notes() -> frozenset[str]:
        return cache.get_or_set(KEEP_HIDDEN, 60, lambda: frozenset((store.get(KEEP_HIDDEN) or {}).get("ids", [])))

    keep: KeepLists | None = None
    if settings.keep_configured and not settings.demo:
        extra = {"keep_factory": keep_factory} if keep_factory else {}
        keep = KeepLists(
            settings.keep_email, settings.keep_master_token, settings.keep_lists, hidden=hidden_notes, **extra
        )

    if assistant is None and settings.anthropic_key:
        assistant = Assistant(settings.anthropic_key, name=settings.assistant_name,
                              aliases=settings.assistant_aliases, people=dict(settings.split_people))
    voice_log = VoiceLog(store)

    app = FastAPI(title="Family dashboard", docs_url=None, redoc_url=None, openapi_url=None)

    # -- who is asking ----------------------------------------------------------------------
    #
    # Family members sign in with Google (DASHBOARD_ADMINS + the dashboard_members table). The
    # optional access key is a shared admin password for installs without Google sign-in. A home
    # install with neither stays open to its local network; on Vercel that's never allowed.

    members = Members(settings, store)
    signing_key = session_key(settings) if settings.google_configured else b""
    login_enabled = settings.google_configured and members.possible

    def identity(request: Request) -> tuple[str, str, int | None] | None:
        """(who, role, session issued-at) or None."""
        if login_enabled:
            session = read_session(signing_key, request.cookies.get(SESSION_COOKIE, ""))
            if session:
                role = members.role(session[0])
                if role:
                    return session[0], role, session[1]
        if settings.access_key:
            supplied = request.cookies.get(COOKIE) or request.headers.get("x-dashboard-key") or ""
            if secrets.compare_digest(supplied.encode(), settings.access_key.encode()):
                return "access key", "admin", None
        if not login_enabled and not settings.access_key and not settings.serverless:
            return "local network", "admin", None
        return None

    def set_session(response, request: Request, email: str) -> None:
        response.set_cookie(
            SESSION_COOKIE, make_session(signing_key, email), max_age=SESSION_DAYS * 86400,
            httponly=True, samesite="lax", secure=_is_https(request),
        )

    open_paths = {"/api/health", "/api/unlock", "/auth/login", "/auth/logout", "/auth/google/callback"}
    admin_paths = ("/setup", "/auth/google/start", "/auth/google/paste")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        who = None
        if path.startswith(("/api/", "/auth/", "/setup")) and path not in open_paths:
            who = identity(request)
            if who is None:
                if settings.serverless and not login_enabled and not settings.access_key:
                    message = ("Sign-in isn't set up: add GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET and "
                               "DASHBOARD_ADMINS to the Vercel project's environment variables, then redeploy.")
                    if path.startswith("/api/"):
                        return JSONResponse({"error": {"code": "signin_not_configured", "message": message}}, 503)
                    return HTMLResponse(f"<p>{html.escape(message)}</p>", 503)
                if path.startswith("/api/"):
                    return JSONResponse({"error": {
                        "code": "signin_required", "message": "Sign in to see the dashboard.",
                        "methods": {"google": login_enabled, "key": bool(settings.access_key)},
                    }}, 401)
                return RedirectResponse(f"/?next={quote(path)}", 303)
            if path.startswith(admin_paths) and who[1] != "admin":
                return HTMLResponse("<p>Only dashboard admins can open this page.</p>", 403)
            request.state.user = who
        response = await call_next(request)
        if who and who[2] is not None and time.time() - who[2] > REFRESH_AFTER:
            set_session(response, request, who[0])  # keep active devices signed in
        if not path.startswith("/api/"):
            # iOS home-screen apps cache hard; always revalidate so updates show up.
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    @app.exception_handler(ApiError)
    async def api_error(_request: Request, exc: ApiError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, exc.status)

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.post("/api/unlock")
    def unlock(request: Request, key: str = Body(..., embed=True)):
        if not settings.access_key:
            raise ApiError(404, "no_key", "This dashboard uses Google sign-in.")
        if not secrets.compare_digest(key.strip().encode(), settings.access_key.encode()):
            time.sleep(1)  # slow down guessing
            raise ApiError(401, "wrong_key", "That key doesn't match.")
        response = JSONResponse({"ok": True})
        response.set_cookie(
            COOKIE, settings.access_key, max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax",
            secure=_is_https(request),
        )
        return response

    @app.get("/auth/login")
    def login(request: Request, next: str = "/"):
        if not login_enabled:
            return RedirectResponse("/?signin_error=" + quote("Google sign-in isn't set up on this dashboard."), 303)
        url, nonce = google.authorization_url("login")
        response = RedirectResponse(url, 303)
        secure = _is_https(request)
        response.set_cookie(OAUTH_COOKIE, nonce, max_age=900, httponly=True, samesite="lax", secure=secure)
        safe_next = next if next.startswith("/") and not next.startswith("//") else "/"
        response.set_cookie(NEXT_COOKIE, safe_next, max_age=900, httponly=True, samesite="lax", secure=secure)
        return response

    @app.post("/auth/logout")
    def logout():
        response = RedirectResponse("/", 303)
        response.delete_cookie(SESSION_COOKIE)
        response.delete_cookie(COOKIE)
        return response

    # -- dashboard data -----------------------------------------------------------------------

    def with_google(fn: Callable):
        try:
            return fn(google.session())
        except NotConnected as exc:
            raise ApiError(503, "google_not_connected", str(exc)) from exc
        except StoreError as exc:
            raise ApiError(502, "storage_error", str(exc)) from exc
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 401:
                google.forget_session()
            raise ApiError(502, "google_error", _google_error_message(exc)) from exc
        except requests.RequestException as exc:
            raise ApiError(502, "network", "Could not reach Google.") from exc
        except Exception as exc:
            if is_revoked(exc):
                google.forget_session()
                raise ApiError(
                    503, "google_not_connected", "Google access expired or was revoked. Reconnect on /setup."
                ) from exc
            raise

    def calendar_list(session) -> list[dict]:
        return cache.get_or_set("calendar_list", 1800, lambda: gcal.list_calendars(session))

    @app.get("/api/config")
    def config(request: Request):
        who, role, _ = request.state.user
        return {
            "user": {"name": who, "role": role},
            "title": settings.title,
            "language": settings.language,
            "demo": settings.demo,
            "google": {"configured": google.configured, "connected": google.connected},
            "keep": {"configured": keep is not None, "lists": list(settings.keep_lists)},
            "weather": settings.demo or settings.weather_configured,
            "accounts": settings.eurobonus or settings.supabase_configured,
            "assistant": assistant is not None and {"name": assistant.name, "aliases": list(settings.assistant_aliases)},
            "split": dict(settings.split_people) or None,
            "financesUrl": settings.finances_url,
        }

    @app.get("/api/calendar")
    def calendar(start: str, end: str):
        try:
            time_min, time_max = datetime.fromisoformat(start), datetime.fromisoformat(end)
        except ValueError as exc:
            raise ApiError(400, "bad_range", "start and end must be ISO 8601 date-times.") from exc
        if time_min.tzinfo is None or time_max.tzinfo is None:
            raise ApiError(400, "bad_range", "start and end need a UTC offset.")
        if not timedelta(0) < time_max - time_min <= MAX_RANGE:
            raise ApiError(400, "bad_range", "The range must be between 0 and 42 days.")
        if settings.demo:
            return demo.calendar(time_min, time_max)

        def load(session):
            chosen, missing = gcal.choose_calendars(calendar_list(session), settings.calendars)
            result = gcal.fetch_all(session, chosen, time_min, time_max)
            result["errors"] += [{"calendar": name, "error": "not_found"} for name in missing]
            return result

        result = cache.get_or_set(("events", start, end), 120, lambda: with_google(load))
        return {**result, "canAdd": google.can_add_events}

    def forget_events() -> None:
        cache.discard(lambda key: isinstance(key, tuple) and key[0] == "events")

    @app.post("/api/events")
    def add_event(event: NewEvent):
        body = _event_body(event)
        if settings.demo:
            if not any(c["id"] == event.calendar and c["writable"] for c in demo.CALENDARS):
                raise ApiError(400, "not_writable", "Events can't be added to that calendar.")
            return demo.add_event(event.calendar, body)
        if not google.can_add_events:
            raise ApiError(403, "reconnect_needed", "Reconnect Google on the setup page to allow adding events.")

        def create(session):
            chosen, _ = gcal.choose_calendars(calendar_list(session), settings.calendars)
            if not any(c.id == event.calendar and c.writable for c in chosen):
                raise ApiError(400, "not_writable", "Events can't be added to that calendar.")
            return gcal.create_event(session, event.calendar, body)

        created = with_google(create)
        forget_events()
        return created

    @app.delete("/api/events")
    def delete_event(calendar: str, id: str):
        """Undo for events added from the dashboard; other events are refused."""
        if settings.demo:
            try:
                demo.delete_event(calendar, id)
            except KeyError as exc:
                raise ApiError(404, "not_found", "That event is already gone.") from exc
            return {"ok": True}
        try:
            with_google(lambda session: gcal.delete_dashboard_event(session, calendar, id))
        except gcal.NotDashboardEvent as exc:
            raise ApiError(403, "not_dashboard_event", str(exc)) from exc
        forget_events()
        return {"ok": True}

    @app.get("/api/weather")
    def weather_now():
        if settings.demo:
            return demo.weather()
        if not settings.weather_configured:
            raise ApiError(404, "weather_off", "Set WEATHER_LAT and WEATHER_LON to show the weather.")

        def load():
            try:
                return weather.fetch_weather(settings.weather_lat, settings.weather_lon, settings.weather_place)
            except requests.RequestException as exc:
                raise ApiError(502, "weather_error", "Couldn't reach the weather service.") from exc

        return cache.get_or_set("weather", 900, load)

    @app.get("/api/accounts")
    def account_balances():
        if settings.demo:
            return demo.accounts()
        items: list[dict] = []
        # Each source on its own: one failing shouldn't hide the others.
        split = None
        people = tuple(pid for pid, _ in settings.split_people)
        if settings.supabase_configured and people:
            try:
                split = cache.get_or_set(SPLIT_CACHE, 60, lambda: split_balance(store.split_entries(settings.split_ratio_column), people))
            except StoreError:
                log.warning("Split balance unavailable", exc_info=True)
        if split is not None:
            names = dict(settings.split_people)
            items.append({"name": "Split", "value": split["amount"] / 100, "unit": "SEK", "decimals": 2,
                          "debtor": names.get(split["owes"]), "creditor": names.get(split["to"])})
        if settings.eurobonus:
            try:
                found = cache.get_or_set("eurobonus", 3600, lambda: with_google(accounts.fetch_eurobonus))
                items += [found] if found else []
            except ApiError:
                log.warning("EuroBonus balance unavailable", exc_info=True)
        return {"items": items}

    def with_keep(fn: Callable):
        if settings.demo:
            try:
                return fn(demo)
            except KeyError as exc:
                raise ApiError(404, "not_found", "That item is no longer on the list.") from exc
        if keep is None:
            raise ApiError(503, "keep_not_configured", "Set KEEP_EMAIL and KEEP_MASTER_TOKEN (see README).")
        try:
            return fn(keep)
        except ItemNotFound as exc:
            raise ApiError(404, "not_found", str(exc)) from exc
        except (KeepError, StoreError) as exc:
            raise ApiError(502, "keep_error", str(exc)) from exc

    @app.get("/api/lists")
    def lists():
        shown = with_keep(lambda k: k.lists())
        remember_checklists(shown)
        return {"lists": shown}

    @app.post("/api/lists/{list_id}/items")
    def add_item(list_id: str, text: str = Body(..., embed=True)):
        text = " ".join(text.split())
        if not text:
            raise ApiError(400, "empty", "Type something to add.")
        return with_keep(lambda k: k.add_item(list_id, text))

    @app.put("/api/lists/{list_id}/items/{item_id}")
    def set_checked(list_id: str, item_id: str, checked: bool = Body(..., embed=True)):
        return with_keep(lambda k: k.set_checked(list_id, item_id, checked))

    @app.put("/api/lists/{list_id}/items/{item_id}/position")
    def move_item(list_id: str, item_id: str, after: str | None = Body(None, embed=True)):
        return with_keep(lambda k: k.move_item(list_id, item_id, after))

    @app.delete("/api/lists/{list_id}/items/{item_id}")
    def delete_item(list_id: str, item_id: str):
        return with_keep(lambda k: k.delete_item(list_id, item_id))

    # -- voice commands -----------------------------------------------------------------------

    def writable_calendars() -> list[dict]:
        if settings.demo:
            return [{"id": c["id"], "name": c["name"]} for c in demo.CALENDARS if c["writable"]]
        if not google.can_add_events:
            return []
        try:
            chosen = with_google(lambda session: gcal.choose_calendars(calendar_list(session), settings.calendars)[0])
        except ApiError:
            return []
        return [{"id": c.id, "name": c.name} for c in chosen if c.writable]

    def remember_checklists(shown: list[dict]) -> None:
        """The iPad polls /api/lists every 30 s; keep the list names for voice commands from that."""
        names = [{"id": lst["id"], "title": lst["title"]} for lst in shown
                 if lst.get("id") and lst.get("kind", "list") == "list" and not lst.get("error")]
        cache.discard(lambda key: key == CHECKLISTS_CACHE)
        cache.get_or_set(CHECKLISTS_CACHE, 600, lambda: names)

    def checklists() -> list[dict]:
        def load():
            try:
                shown = with_keep(lambda k: k.lists())
            except ApiError:
                return []
            remember_checklists(shown)
            return cache.get_or_set(CHECKLISTS_CACHE, 600, list)
        return cache.get_or_set(CHECKLISTS_CACHE, 600, load)

    def carry_out(action: dict) -> dict:
        """Do one planned action; returns what the toast says and how to undo it."""
        kind = action["type"]
        if kind == "list_items":
            _, ids = with_keep(lambda k: k.add_items(action["list_id"], action["items"]))
            return {"type": kind, "list": action["list"], "items": action["items"],
                    "undo": [{"kind": "list_item", "list": action["list_id"], "item": i} for i in ids]}
        if kind == "event":
            created = add_event(NewEvent(
                calendar=action["calendar"], title=action["title"], allDay=action["allDay"],
                start=action["start"], end=action["end"], location=action["location"],
            ))
            return {"type": kind, "title": action["title"], "calendar": action["calendar_name"],
                    "allDay": action["allDay"], "start": action["start"], "end": action["end"],
                    "undo": [{"kind": "event", "calendar": action["calendar"], "id": created["eventId"]}]}
        if kind in ("weather", "answer"):
            return {**action, "undo": []}  # shown and read aloud by the iPad; nothing to change
        if kind == "undo":
            return {**undo_entry(voice_log.find()), "undo": []}
        if kind == "expense":
            fields = {k: action[k] for k in ("date", "description", "amount", "paid_by", "category")}
            try:
                people = tuple(pid for pid, _ in settings.split_people)
                entry_id = demo.add_split_expense(fields) if settings.demo else store.add_split_expense(fields, people, settings.split_ratio_column)
            except StoreError as exc:
                log.warning("Could not add a Split expense: %s", exc)
                raise ApiError(502, "split_error", "Kunde inte spara utgiften i Split.") from exc
            cache.discard(lambda key: key == SPLIT_CACHE)
            return {"type": kind, "description": action["description"], "amount": action["amount"],
                    "paid_by": action["paid_by"], "date": action["date"],
                    "undo": [{"kind": "expense", "id": entry_id}]}
        raise ApiError(400, "unknown_action", "Unknown action.")

    def undo_step(step: dict) -> None:
        if step["kind"] == "list_item":
            with_keep(lambda k: k.delete_item(step["list"], step["item"]))
        elif step["kind"] == "event":
            delete_event(step["calendar"], step["id"])
        elif step["kind"] == "expense":
            delete_split_expense(int(step["id"]))

    def undo_entry(entry: dict | None) -> dict:
        """Undo everything one logged command did; what's already gone counts as undone."""
        if entry is None:
            raise ApiError(404, "nothing_to_undo", "Det finns inget att ångra.")
        if entry.get("undone"):
            raise ApiError(409, "already_undone", "Det är redan ångrat.")
        for step in entry.get("undo", []):
            try:
                undo_step(step)
            except ApiError as exc:
                if exc.status != 404:
                    raise ApiError(exc.status, exc.code, f"Kunde inte ångra allt: {exc.message}") from exc
        voice_log.mark_undone(entry["id"])
        what = [describe_sv(a) for a in entry.get("done", [])]
        return {"type": "undo", "entry": entry["id"], "what": " och ".join(w for w in what if w)}

    def finish_command(request: Request, text: str, zone: ZoneInfo, done: list[dict], failed: list[str],
                       reply: str) -> dict:
        """Log the command and answer the iPad; undo steps stay on the server."""
        undo = [step for action in done for step in action.get("undo", [])]
        shown = [{k: v for k, v in action.items() if k != "undo"} for action in done]
        log_id = voice_log.add({
            "at": datetime.now(zone).isoformat(timespec="seconds"), "who": request.state.user[0],
            "text": text, "done": shown, "failed": failed, "reply": reply, "undo": undo,
        })
        touched = {a["type"] for a in done} | ({"list_items", "event", "expense"} if "undo" in {a["type"] for a in done} else set())
        return {"text": text, "done": shown, "failed": failed, "reply": reply, "log_id": log_id,
                "undoable": bool(undo), "touched": sorted(touched)}

    @app.post("/api/assistant")
    def run_assistant(request: Request, text: str = Body(..., embed=True),
                      timezone: str = Body("Europe/Stockholm", embed=True)):
        if assistant is None:
            raise ApiError(404, "assistant_off", "Set ANTHROPIC_API_KEY to use voice commands.")
        try:
            zone = ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            zone = ZoneInfo("Europe/Stockholm")
        if is_undo(text, (settings.assistant_name, *settings.assistant_aliases)):  # "ASCA, ångra": no Claude needed
            try:
                return finish_command(request, text, zone, [carry_out({"type": "undo"})], [], "")
            except ApiError as exc:
                return finish_command(request, text, zone, [], [exc.message], "")
        started = time.monotonic()
        # List and calendar names usually come from the cache; when not, fetch both at once.
        with ThreadPoolExecutor(max_workers=2) as pool:
            lists_future, calendars_future = pool.submit(checklists), pool.submit(writable_calendars)
            names = lists_future.result(), calendars_future.result()
        context_done = time.monotonic()
        try:
            plan = assistant.plan(text, *names, zone, datetime.now(zone),
                                  weather=settings.demo or settings.weather_configured)
        except AssistantError as exc:
            raise ApiError(502, "assistant_error", str(exc)) from exc
        planned = time.monotonic()
        done, failed = [], []
        for action in plan.actions:
            try:
                done.append(carry_out(action))
            except ApiError as exc:
                failed.append(exc.message)
        finished = time.monotonic()
        log.info("Voice command timing: names %.1fs, Claude %.1fs, actions %.1fs (%s), total %.1fs",
                 context_done - started, planned - context_done, finished - planned,
                 ",".join(a["type"] for a in plan.actions) or "none", finished - started)
        return finish_command(request, text, zone, done, failed, plan.reply)

    @app.get("/api/voice/log")
    def voice_log_entries():
        return {"entries": [public(entry) for entry in voice_log.entries()]}

    @app.post("/api/voice/log/{entry_id}/undo")
    def undo_logged(entry_id: str):
        result = undo_entry(voice_log.find(entry_id))
        return {"ok": True, "what": result["what"]}

    @app.post("/api/voice/report")
    def voice_report(request: Request, problem: str = Body("", embed=True), standalone: bool = Body(False, embed=True)):
        """Speech recognition runs in Safari; this puts its failures in the server log."""
        log.warning("Voice problem on the iPad: %s (home screen app: %s, %s)",
                    problem[:80], standalone, request.headers.get("user-agent", "")[:160])
        return {"ok": True}

    @app.delete("/api/split/expenses/{entry_id}")
    def delete_split_expense(entry_id: int):
        """Undo for expenses added by voice."""
        try:
            gone = demo.delete_split_expense(entry_id) if settings.demo else store.delete_split_expense(entry_id)
        except StoreError as exc:
            raise ApiError(502, "split_error", "Kunde inte ta bort utgiften i Split.") from exc
        if not gone:
            raise ApiError(404, "not_found", "That expense is already gone.")
        cache.discard(lambda key: key == SPLIT_CACHE)
        return {"ok": True}

    @app.post("/api/refresh")
    def refresh():
        cache.clear()
        return {"ok": True}

    # -- setup --------------------------------------------------------------------------------

    @app.get("/auth/google/start")
    def google_start(request: Request):
        if not google.configured:
            return RedirectResponse("/setup?error=" + quote("Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET first."), 303)
        url, nonce = google.authorization_url("connect")
        response = RedirectResponse(url, 303)
        response.set_cookie(
            OAUTH_COOKIE, nonce, max_age=900, httponly=True, samesite="lax", secure=_is_https(request)
        )
        return response

    def finish_google(request: Request, redirect_url: str) -> RedirectResponse:
        """Handle Google's redirect for both signing in and connecting Calendar + Gmail."""
        purpose = None
        try:
            code, state = parse_redirect_url(redirect_url)
            purpose = google.check_state(state, request.cookies.get(OAUTH_COOKIE, ""))
            if purpose == "login":
                email = google.login_email(code)
                if not members.role(email):
                    raise AuthError(f"{email} isn't on the family dashboard. Ask an admin to add you.")
                target = request.cookies.get(NEXT_COOKIE) or "/"
                response = RedirectResponse(target if target.startswith("/") and not target.startswith("//") else "/", 303)
                set_session(response, request, email)
            else:
                who = identity(request)
                if not who or who[1] != "admin":
                    raise AuthError("Only a dashboard admin can connect Google Calendar and Gmail.")
                google.complete(code)
                cache.clear()
                response = RedirectResponse("/setup?ok=google", 303)
        except AuthError as exc:
            who = identity(request)
            to_setup = purpose == "connect" or (purpose is None and who and who[1] == "admin")
            return RedirectResponse(("/setup?error=" if to_setup else "/?signin_error=") + quote(str(exc)), 303)
        response.delete_cookie(OAUTH_COOKIE)
        response.delete_cookie(NEXT_COOKIE)
        return response

    @app.get("/auth/google/callback")
    def google_callback(request: Request):
        return finish_google(request, str(request.url))

    @app.post("/auth/google/paste")
    def google_paste(request: Request, url: str = Form(...)):
        return finish_google(request, url)

    @app.get("/setup", response_class=HTMLResponse)
    def setup(request: Request, ok: str = "", error: str = ""):
        return _render_setup(settings, store, google, keep, members, request.state.user, calendar_list, ok, error)

    @app.post("/setup/keep")
    def setup_keep(listed: list[str] = Form(default=[]), show: list[str] = Form(default=[])):
        """Save which of the listed notes are hidden; notes that weren't on the form keep their setting."""
        try:
            hidden = set((store.get(KEEP_HIDDEN) or {}).get("ids", []))
            hidden = (hidden - set(listed)) | (set(listed) - set(show))
            store.set(KEEP_HIDDEN, {"ids": sorted(hidden)})
        except StoreError as exc:
            return RedirectResponse("/setup?error=" + quote(str(exc)) + "#keep", 303)
        cache.discard(lambda key: key == KEEP_HIDDEN)
        return RedirectResponse("/setup?ok=keep#keep", 303)

    if STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
    return app


def _render_setup(
    settings: Settings, store: Store, google: GoogleAuth, keep: KeepLists | None, members: Members,
    user: tuple, calendar_list, ok: str, error: str,
) -> str:
    e = html.escape
    env = "Vercel's environment variables" if settings.serverless else "<code>.env</code>"
    apply = "redeploy" if settings.serverless else "restart"
    parts: list[str] = []
    if ok == "google":
        parts.append('<p class="notice ok">Google is connected.</p>')
    if ok == "keep":
        parts.append('<p class="notice ok">Saved which notes are shown.</p>')
    if error:
        parts.append(f'<p class="notice bad">{e(error)}</p>')

    # Who
    parts.append(f"<p>Signed in as <strong>{e(user[0])}</strong> ({e(user[1])}).</p>")
    if user[2] is not None:
        parts.append('<form method="post" action="/auth/logout"><button class="secondary" type="submit">Sign out</button></form>')

    # Members
    parts.append("<section><h2>Who can sign in</h2>")
    rows = "".join(f"<tr><td>{e(email)}</td><td>{e(role)}</td><td>{e(source)}</td></tr>"
                   for email, role, source in members.listing())
    parts.append(f"<table>{rows}</table>" if rows else "<p>Nobody yet.</p>")
    parts.append("<p>Admins can also open this page. Add people to the <code>dashboard_members</code> table in "
                 "Supabase (role <code>admin</code> or <code>member</code>), or to <code>DASHBOARD_ADMINS</code>. "
                 "Changes apply within a minute.</p>")
    parts.append("</section>")

    # Storage
    parts.append("<section><h2>Storage</h2>")
    if settings.serverless and not settings.supabase_configured:
        parts.append('<p class="notice bad">Vercel has no persistent disk. Set <code>SUPABASE_URL</code> and '
                     f"<code>SUPABASE_SECRET_KEY</code> in {env} and {apply}.</p>")
    try:
        store.get("google_token")
        parts.append(f"<p>Saving tokens in the {e(store.description)}. ✓</p>")
    except StoreError as exc:
        parts.append(f'<p class="notice bad">{e(str(exc))}</p>')
    parts.append("</section>")

    # Google
    parts.append("<section><h2>Google Calendar &amp; Gmail</h2>")
    if settings.demo:
        parts.append(f"<p>DEMO_MODE is on, so the dashboard shows sample data. Turn it off in {env} to use your own.</p>")
    if not google.configured:
        parts.append(f"<p>Add <code>GOOGLE_CLIENT_ID</code> and <code>GOOGLE_CLIENT_SECRET</code> to {env} "
                     f"and {apply}. The README explains how to create them.</p>")
    else:
        status = "Connected" if google.connected else "Not connected"
        parts.append(f'<p>Status: <strong>{status}</strong></p>')
        if google.connected and not google.can_add_events:
            parts.append('<p class="notice bad">This connection can only read the calendar. '
                         'Reconnect below to allow adding events from the iPad.</p>')
        parts.append(f"<p>Authorized redirect URI to add on the Google OAuth client: "
                     f"<code>{e(settings.google_redirect_uri)}</code></p>")
        parts.append('<p><a class="button" href="/auth/google/start">'
                     f'{"Reconnect" if google.connected else "Connect"} Google account</a></p>')
        if "://localhost" in settings.google_redirect_uri:
            parts.append(
                "<details><summary>Landed on a page that couldn't load after approving?</summary>"
                "<p>That's expected when the dashboard runs on another computer. "
                "Copy the whole address from that page's address bar and paste it here:</p>"
                '<form method="post" action="/auth/google/paste">'
                '<input name="url" type="url" required placeholder="http://localhost:8080/auth/google/callback?state=…&amp;code=…">'
                '<button type="submit">Finish connecting</button></form></details>'
            )
    parts.append("</section>")

    # Calendars
    if google.connected and not settings.demo:
        parts.append("<section><h2>Calendars</h2>")
        try:
            entries = calendar_list(google.session())
            chosen, missing = gcal.choose_calendars(entries, settings.calendars)
            shown = {c.id for c in chosen}
            if settings.calendars:
                parts.append(f"<p><code>CALENDARS={e(','.join(settings.calendars))}</code></p>")
            else:
                parts.append("<p>No <code>CALENDARS</code> set, so the calendars ticked in Google Calendar are shown. "
                             "Set <code>CALENDARS</code> to a comma-separated list of names below to choose. "
                             "Add <code>=Short name</code> to rename one, e.g. <code>Kids' school=School</code>.</p>")
            if missing:
                parts.append(f'<p class="notice bad">Not found: {e(", ".join(missing))}</p>')
            rows = "".join(
                f'<tr><td><span class="dot" style="background:{e(c.get("backgroundColor") or gcal.DEFAULT_COLOR)}"></span>'
                f'{e(c.get("summaryOverride") or c.get("summary") or c["id"])}</td>'
                f'<td>{"✓ shown" if c["id"] in shown else ""}</td></tr>'
                for c in entries
            )
            parts.append(f"<table>{rows}</table>")
        except Exception as exc:  # noqa: BLE001 - show whatever went wrong
            parts.append(f'<p class="notice bad">Could not read calendars: {e(str(exc))}</p>')
        parts.append("</section>")

    # Keep
    parts.append('<section id="keep"><h2>Google Keep lists</h2>')
    if settings.demo:
        parts.append("<p>Using sample lists in demo mode.</p>")
    elif keep is None:
        parts.append(f"<p>Add <code>KEEP_EMAIL</code> and <code>KEEP_MASTER_TOKEN</code> to {env} and {apply}. "
                     "The README explains how to get the token.</p>")
    else:
        parts.append(f"<p>Showing {e(keep.description)} "
                     f"(<code>KEEP_LISTS={e(','.join(settings.keep_lists))}</code>). "
                     "Other options: <code>family</code>, <code>shared</code>, or a comma-separated list of titles.</p>")
        try:
            notes = keep.overview()
            if not notes:
                parts.append("<p>No notes to show yet. Share a note with your family group in Keep; if you shared "
                             "your notes with people one by one instead, set <code>KEEP_LISTS=shared</code>.</p>")
            elif keep.choosable:
                parts.append(_keep_form(notes))
            else:
                parts.append("<table>" + "".join(
                    f"<tr><td>{e(n['title'])}</td><td>{'checklist' if n['checklist'] else 'note'}</td></tr>"
                    for n in notes) + "</table>")
        except (KeepError, StoreError) as exc:
            parts.append(f'<p class="notice bad">{e(str(exc))}</p>')
    parts.append("</section>")

    body = "\n".join(parts)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dashboard setup</title><link rel="stylesheet" href="/styles.css"></head>
<body class="setup"><main>
<h1>Dashboard setup</h1>
{body}
<p><a class="button secondary" href="/">Open the dashboard</a></p>
</main></body></html>"""



def _keep_form(notes: list[dict]) -> str:
    """Tick boxes to choose which of the family's notes the dashboard shows."""
    e = html.escape
    with_sharing = any(n["sharing"] != "family" for n in notes)

    def status(n: dict) -> str:
        if n["shown"]:
            return "✓ on the dashboard"
        if n["hidden"]:
            return "hidden"
        return f"doesn't fit (max {MAX_SHARED_NOTES})"

    rows = "".join(
        f'<tr><td><label class="check"><input type="checkbox" name="show" value="{e(n["id"])}"'
        f'{"" if n["hidden"] else " checked"}> {e(n["title"])}</label>'
        f'<input type="hidden" name="listed" value="{e(n["id"])}"></td>'
        f'<td>{"checklist" if n["checklist"] else "note"}{" · " + e(n["sharing"]) if with_sharing else ""}</td>'
        f"<td>{status(n)}</td></tr>"
        for n in notes
    )
    return (f"<p>Tick the notes to show. At most {MAX_SHARED_NOTES} fit: pinned notes first, then the most recently "
            "edited. New notes shared with the family show up automatically.</p>"
            f'<form class="keep-notes" method="post" action="/setup/keep"><table>{rows}</table>'
            '<button type="submit">Save</button></form>')


app = create_app()
