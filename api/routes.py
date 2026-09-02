"""
FastAPI app - serves the single-page UI and handles WebSocket sessions.
Core flow: receive a Meet invite, join, record audio+video, send it to Gmail/Drive,
and remove Atom's temporary server copy.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config.settings import settings
from core.database import DB_PATH, init_db
from core.drive import upload_recording_to_drive
from core.mailer import gmail_enabled, send_recording_email
from core.storage import (
    add_recording,
    cleanup_recording_files,
    delete_recording,
    list_recordings,
    s3_enabled,
    update_recording_delivery,
    update_recording_drive_delivery,
)
from core.users import (
    google_client_id, verify_google_credential, get_or_create_user,
    create_session, user_for_session, destroy_session, user_count, session_count,
)
from meeting.meet.auth import (
    CONFIG_DIR,
    list_profile_slots,
    save_auth,
    has_auth,
    clear_profile,
    clear_profile_locks,
)
from meeting.meet.bot import MeetBot, RECORDINGS_DIR

SESSION_COOKIE = "atom_session"

# Live recording sessions: session_id -> {user, meet_code, started_at, status}
_active: dict[str, dict] = {}

logger = logging.getLogger(__name__)

app = FastAPI(title="Atom", version="0.2.0")

_cors_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
if _cors_origins:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/recordings", StaticFiles(directory=str(RECORDINGS_DIR)), name="recordings")

# Auth flow state
_auth_state: dict = {"running": False, "done": False, "error": None}


@app.on_event("startup")
async def startup() -> None:
    init_db()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    (STATIC_DIR / "debug").mkdir(parents=True, exist_ok=True)


def _admin_allowed(request: Request) -> bool:
    if not settings.admin_token:
        return request.client is not None and request.client.host in {"127.0.0.1", "::1", "localhost"}
    supplied = request.headers.get("x-atom-admin-token") or request.query_params.get("token")
    return supplied == settings.admin_token


def _admin_guard(request: Request) -> JSONResponse | None:
    if _admin_allowed(request):
        return None
    return JSONResponse({"ok": False, "message": "Admin token required"}, status_code=401)


def _snapshot_health() -> dict:
    return {
        "ok": True,
        "app": "atom",
        "recordings": len(list_recordings()),
        "users": user_count(),
        "sessions": session_count(),
        "active_sessions": len(_active),
        "auth_ready": has_auth(),
        "s3_enabled": s3_enabled(),
        "gmail_enabled": gmail_enabled(),
        "google_auth_enabled": bool(google_client_id()),
        "database": str(DB_PATH),
        "admin_secured": bool(settings.admin_token),
        "ts": int(time.time()),
    }


def _snapshot_readiness() -> dict:
    checks = {
        "database": DB_PATH.exists(),
        "recordings_dir": RECORDINGS_DIR.exists(),
        "google_client_id": bool(google_client_id()),
        "gmail": gmail_enabled(),
        "bot_google_profile": has_auth(),
    }
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "health": _snapshot_health(),
    }


# ── Auth ────────────────────────────────────────────────────────────────────
@app.get("/auth/status")
async def auth_status(request: Request) -> JSONResponse:
    blocked = _admin_guard(request)
    if blocked:
        return blocked
    return JSONResponse({
        **_auth_state,
        "slots": list_profile_slots(),
        "has_auth": has_auth(),
    })


@app.get("/health")
async def health() -> JSONResponse:
    return JSONResponse(_snapshot_health())


@app.get("/ready")
async def ready() -> JSONResponse:
    status = _snapshot_readiness()
    return JSONResponse(status, status_code=200 if status["ok"] else 503)


@app.post("/auth/reset")
async def auth_reset(request: Request) -> JSONResponse:
    """Clear a stuck auth state so the user can retry."""
    blocked = _admin_guard(request)
    if blocked:
        return blocked
    removed_locks = clear_profile_locks(slot=0)
    _auth_state.update(running=False, done=False, error=None)
    return JSONResponse({"ok": True, "removed_locks": removed_locks})


@app.post("/auth/clear")
async def auth_clear(request: Request) -> JSONResponse:
    """Wipe the saved login so a different Google account can sign in."""
    blocked = _admin_guard(request)
    if blocked:
        return blocked
    if _auth_state["running"]:
        return JSONResponse({"ok": False, "message": "Sign-in in progress"})
    cleared = clear_profile(slot=0)
    _auth_state.update(running=False, done=False, error=None)
    return JSONResponse({"ok": True, "cleared": cleared})


@app.post("/auth/start")
async def auth_start(request: Request) -> JSONResponse:
    blocked = _admin_guard(request)
    if blocked:
        return blocked
    # Already signed in? Nothing to do.
    if has_auth():
        _auth_state.update(running=False, done=True, error=None)
        return JSONResponse({"ok": True, "message": "Already signed in"})
    if _auth_state["running"]:
        return JSONResponse({"ok": False, "message": "Auth already in progress"})

    _auth_state.update(running=True, done=False, error=None)

    async def _run():
        try:
            await save_auth(slot=0)
            _auth_state.update(done=True, error=None)
        except Exception as e:
            logger.warning("Auth failed: %s", e)
            _auth_state.update(done=False, error=str(e))
        finally:
            _auth_state.update(running=False)   # ALWAYS clear running

    asyncio.create_task(_run())
    return JSONResponse({"ok": True, "message": "Auth started"})


# ── End-user auth (Continue with Google) ──────────────────────────────────────
@app.get("/config")
async def app_config() -> JSONResponse:
    return JSONResponse({
        "google_client_id": google_client_id(),
        "gmail_enabled": gmail_enabled(),
        "auth_ready": has_auth(),
    })


@app.get("/auth/me")
async def auth_me(request: Request) -> JSONResponse:
    user = user_for_session(request.cookies.get(SESSION_COOKIE))
    if not user:
        return JSONResponse({"authenticated": False})
    return JSONResponse({
        "authenticated": True,
        "username": user["username"],
        "email": user["email"],
    })


@app.post("/auth/google")
async def auth_google(request: Request) -> JSONResponse:
    body = await request.json()
    info = verify_google_credential(body.get("credential", ""))
    if not info:
        return JSONResponse({"ok": False, "message": "Sign-in failed"}, status_code=401)
    user = get_or_create_user(info["sub"], info["email"])
    token = create_session(info["sub"])
    resp = JSONResponse({"ok": True, "username": user["username"], "email": user["email"]})
    resp.set_cookie(
        SESSION_COOKIE, token,
        httponly=True,
        samesite=settings.cookie_samesite,
        secure=settings.cookie_secure,
        max_age=60 * 60 * 24 * 30,
        path="/",
    )
    return resp


@app.post("/auth/logout")
async def auth_logout(request: Request) -> JSONResponse:
    destroy_session(request.cookies.get(SESSION_COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(
        SESSION_COOKIE,
        path="/",
        samesite=settings.cookie_samesite,
        secure=settings.cookie_secure,
    )
    return resp


# ── Recording delivery history (per user) ─────────────────────────────────────
@app.get("/recordings")
async def get_recordings(request: Request) -> JSONResponse:
    user = user_for_session(request.cookies.get(SESSION_COOKIE))
    if not user:
        return JSONResponse({"items": [], "cloud": s3_enabled(), "gmail": gmail_enabled()})
    return JSONResponse({
        "items": list_recordings(user["sub"]),
        "cloud": s3_enabled(),
        "gmail": gmail_enabled(),
    })


@app.get("/profile")
async def profile(request: Request) -> JSONResponse:
    user = user_for_session(request.cookies.get(SESSION_COOKIE))
    if not user:
        return JSONResponse({"authenticated": False})
    recs = list_recordings(user["sub"])
    active = [a for a in _active.values() if a["user"] == user["sub"]]
    total = sum(r.get("duration", 0) for r in recs)
    return JSONResponse({
        "authenticated": True,
        "username": user["username"],
        "email": user["email"],
        "created_at": user.get("created_at", 0),
        "count": len(recs),
        "total_duration": total,
        "active": active,
        "recordings": recs,
    })


@app.delete("/recordings/{rec_id}")
async def del_recording(rec_id: str, request: Request) -> JSONResponse:
    user = user_for_session(request.cookies.get(SESSION_COOKIE))
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    ok = delete_recording(rec_id, user["sub"])
    return JSONResponse({"ok": ok})


# ── UI ────────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/config.js")
async def frontend_config() -> HTMLResponse:
    return HTMLResponse(
        (STATIC_DIR / "config.js").read_text(encoding="utf-8"),
        media_type="application/javascript",
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "admin.html").read_text(encoding="utf-8"))


# ── WebSocket session ─────────────────────────────────────────────────────────
@app.websocket("/ws")
async def meeting_ws(ws: WebSocket) -> None:
    await ws.accept()
    bot: MeetBot | None = None
    bot_task: asyncio.Task | None = None

    # Identify the end user from their session cookie
    user = user_for_session(ws.cookies.get(SESSION_COOKIE))

    async def send(data: dict) -> None:
        try:
            await ws.send_text(json.dumps(data))
        except Exception:
            pass

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            action = msg.get("action")

            if action == "join":
                if not user:
                    await send({"type": "error", "message": "Please sign in first."})
                    continue
                url = msg.get("url", "").strip()
                drive_token = msg.get("drive_token", "").strip()
                if not url:
                    await send({"type": "error", "message": "No meeting URL provided."})
                    continue
                if not drive_token:
                    await send({"type": "error", "message": "Google Drive permission is required before recording."})
                    continue
                if bot_task and not bot_task.done():
                    await send({"type": "error", "message": "Already in a meeting."})
                    continue

                async def on_status(text: str) -> None:
                    await send({"type": "status", "message": text})

                async def on_count(count: int) -> None:
                    await send({"type": "participant_count", "count": count})

                bot = MeetBot(
                    meeting_url=url,
                    bot_name=settings.agent_name,
                    on_status=on_status,
                    on_count=on_count,
                )

                # Derive a friendly title from the meet code (…/abc-defg-hij)
                meet_code = url.rstrip("/").split("/")[-1].split("?")[0]

                # Register as a live session for the profile card
                _active[bot.session_id] = {
                    "id": bot.session_id, "user": user["sub"], "meet_code": meet_code,
                    "started_at": int(__import__("time").time()), "status": "recording",
                }

                async def run_bot() -> None:
                    try:
                        await bot.join()
                        if bot.recording_path:
                            local = RECORDINGS_DIR / Path(bot.recording_path).name
                            entry = await add_recording(
                                local, meet_code=meet_code, user_sub=user["sub"]
                            )
                            try:
                                await send({"type": "status", "message": "Uploading recording to your Google Drive..."})
                                drive_delivery = await upload_recording_to_drive(
                                    access_token=drive_token,
                                    local_path=local,
                                    entry=entry,
                                )
                                update_recording_drive_delivery(entry["id"], user["sub"], drive_delivery)
                                entry["drive_delivery"] = drive_delivery

                                await send({"type": "status", "message": "Sending recording email..."})
                                delivery_entry = {**entry, "drive_url": drive_delivery.get("url")}
                                delivery = await send_recording_email(
                                    entry=delivery_entry,
                                    recipient=user["email"],
                                    local_path=local,
                                )
                                update_recording_delivery(entry["id"], user["sub"], delivery)
                                entry["email_delivery"] = delivery
                            finally:
                                cleanup_recording_files(local)
                            await send({"type": "recording", "entry": entry})
                            if entry.get("drive_delivery", {}).get("status") == "uploaded" and entry.get("email_delivery", {}).get("status") == "sent":
                                await send({"type": "status", "message": "Recording sent to Gmail and Google Drive"})
                            else:
                                await send({"type": "status", "message": "Recording finished - delivery needs attention"})
                        else:
                            await send({"type": "status", "message": "Meeting ended (no recording captured)"})
                    except Exception as e:
                        logger.exception("Bot error")
                        await send({"type": "error", "message": str(e)})
                    finally:
                        _active.pop(bot.session_id, None)

                bot_task = asyncio.create_task(run_bot())

            elif action == "leave":
                if bot:
                    await bot.stop()
                    await send({"type": "status", "message": "Stopping - saving recording..."})

    except WebSocketDisconnect:
        # Do NOT cancel the recording. The socket may drop or reconnect, but the
        # bot keeps recording and finalizes server-side — the finished MP4 still
        # is delivered server-side. (Explicit "leave" is the only stop.)
        logger.info("WebSocket disconnected; recording continues in background")
