"""
FastAPI app - serves the single-page UI and handles WebSocket sessions.
Core flow: receive a Meet invite, join, record audio+video, and store the finished
recording in the signed-in user's private database library.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from config.settings import settings
from core.database import DB_PATH, init_db
from core.storage import (
    add_recording,
    cleanup_recording_files,
    delete_recording,
    delete_recording_admin,
    get_recording_media,
    get_recording_media_info,
    list_recordings,
    list_admin_recordings,
    max_recording_bytes,
    purge_local_recording_files,
    update_recording_summary,
)
from core.summarizer import summarize_recording, summary_enabled
from core.users import (
    google_client_id, verify_google_credential, get_or_create_user,
    create_session, user_for_session, destroy_session, user_count, session_count,
    update_username,
)
from meeting.meet.auth import (
    CONFIG_DIR,
    list_profile_slots,
    save_auth,
    has_auth,
    clear_profile,
    clear_profile_locks,
)
from meeting.meet.bot import MeetBot, RECORDINGS_DIR, runtime_diagnostics

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
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

STATIC_DIR = Path(__file__).parent / "static"
STATIC_DIR.mkdir(exist_ok=True)
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Auth flow state
_auth_state: dict = {"running": False, "done": False, "error": None}


def normalize_meet_url(raw: str) -> tuple[str, str]:
    """Return a safe Google Meet URL and meet code, or raise ValueError."""
    value = (raw or "").strip()
    if not value:
        raise ValueError("No meeting URL provided.")
    if value.startswith("http://"):
        value = "https://" + value.removeprefix("http://")
    elif not value.startswith("https://"):
        value = "https://" + value.lstrip("/")

    parsed = urlparse(value)
    if parsed.netloc.lower() != "meet.google.com":
        raise ValueError("Paste a valid Google Meet link.")

    code = parsed.path.strip("/").split("/")[0].lower()
    if not re.fullmatch(r"[a-z]{3}-[a-z]{4}-[a-z]{3}", code):
        raise ValueError("Paste a full Google Meet link like https://meet.google.com/abc-defg-hij.")
    return f"https://meet.google.com/{code}", code


@app.on_event("startup")
async def startup() -> None:
    init_db()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    (STATIC_DIR / "debug").mkdir(parents=True, exist_ok=True)
    removed = purge_local_recording_files()
    if removed:
        logger.info("Purged %d leftover local recording files", removed)
    for debug_file in (STATIC_DIR / "debug").glob("*.png"):
        try:
            debug_file.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Could not remove debug screenshot %s: %s", debug_file, exc)


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
        "auth_ready": True,
        "bot_google_profile": has_auth(),
        "bot_guest_fallback": True,
        "recording_storage": "database",
        "meeting_summaries": summary_enabled(),
        "max_recording_db_mb": max_recording_bytes() // (1024 * 1024),
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
        "database_recordings": True,
        "meeting_summaries": summary_enabled(),
        "bot_google_profile": has_auth(),
        "bot_guest_fallback": True,
    }
    return {
        "ok": checks["database"] and checks["recordings_dir"] and checks["google_client_id"],
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


@app.get("/admin/recordings")
async def admin_recordings(request: Request) -> JSONResponse:
    blocked = _admin_guard(request)
    if blocked:
        return blocked
    items = list_admin_recordings()
    return JSONResponse({
        "ok": True,
        "items": items,
        "count": len(items),
        "total_bytes": sum(item.get("size", 0) for item in items if item.get("stored")),
    })


@app.delete("/admin/recordings/{rec_id}")
async def admin_delete_recording(rec_id: str, request: Request) -> JSONResponse:
    blocked = _admin_guard(request)
    if blocked:
        return blocked
    deleted = delete_recording_admin(rec_id)
    return JSONResponse(
        {"ok": deleted, "message": "Recording deleted" if deleted else "Recording not found"},
        status_code=200 if deleted else 404,
    )


@app.post("/admin/bot-check")
async def admin_bot_check(request: Request) -> JSONResponse:
    blocked = _admin_guard(request)
    if blocked:
        return blocked
    result = await runtime_diagnostics()
    return JSONResponse(result, status_code=200 if result["ok"] else 503)


# ── End-user auth (Continue with Google) ──────────────────────────────────────
@app.get("/config")
async def app_config() -> JSONResponse:
    return JSONResponse({
        "google_client_id": google_client_id(),
        "recording_storage": "database",
        "meeting_summaries": summary_enabled(),
        "max_recording_db_mb": max_recording_bytes() // (1024 * 1024),
        "auth_ready": True,
        "bot_google_profile": has_auth(),
        "bot_guest_fallback": True,
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


# ── Private recording library (per user) ──────────────────────────────────────
@app.get("/recordings")
async def get_recordings(request: Request) -> JSONResponse:
    user = user_for_session(request.cookies.get(SESSION_COOKIE))
    if not user:
        return JSONResponse({"items": [], "storage": "database"})
    return JSONResponse({
        "items": list_recordings(user["sub"]),
        "storage": "database",
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
        "display_name": user.get("display_name") or user["username"],
        "email": user["email"],
        "created_at": user.get("created_at", 0),
        "count": len(recs),
        "total_duration": total,
        "active": active,
        "recordings": recs,
    })


@app.patch("/profile")
async def update_profile(request: Request) -> JSONResponse:
    user = user_for_session(request.cookies.get(SESSION_COOKIE))
    if not user:
        return JSONResponse({"ok": False, "message": "Not signed in"}, status_code=401)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "message": "Invalid JSON"}, status_code=400)
    ok, message, updated = update_username(user["sub"], str(body.get("username", "")))
    return JSONResponse({"ok": ok, "message": message, "user": updated}, status_code=200 if ok else 400)


@app.delete("/recordings/{rec_id}")
async def del_recording(rec_id: str, request: Request) -> JSONResponse:
    user = user_for_session(request.cookies.get(SESSION_COOKIE))
    if not user:
        return JSONResponse({"ok": False}, status_code=401)
    ok = delete_recording(rec_id, user["sub"])
    return JSONResponse({"ok": ok})


@app.get("/recordings/{rec_id}/media")
async def recording_media(
    rec_id: str,
    request: Request,
    download: bool = False,
) -> Response:
    user = user_for_session(request.cookies.get(SESSION_COOKIE))
    if not user:
        return JSONResponse({"ok": False, "message": "Not signed in"}, status_code=401)
    info = get_recording_media_info(rec_id, user["sub"])
    if not info:
        return JSONResponse({"ok": False, "message": "Recording not found"}, status_code=404)

    total = info["total"]
    start = 0
    end = total - 1
    status_code = 200
    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(total),
        "Cache-Control": "private, no-store",
        "Content-Disposition": (
            f"{'attachment' if download else 'inline'}; "
            f"filename=\"{info['filename'].replace(chr(34), '')}\""
        ),
    }
    range_header = request.headers.get("range", "")
    if range_header.startswith("bytes="):
        try:
            start_text, end_text = range_header[6:].split("-", 1)
            if start_text:
                start = int(start_text)
                end = int(end_text) if end_text else total - 1
            else:
                suffix = int(end_text)
                start = max(0, total - suffix)
                end = total - 1
            if start < 0 or end < start or start >= total:
                raise ValueError
            end = min(end, total - 1)
        except (TypeError, ValueError):
            return Response(status_code=416, headers={"Content-Range": f"bytes */{total}"})
        status_code = 206
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"
        headers["Content-Length"] = str(end - start + 1)
    media = get_recording_media(
        rec_id,
        user["sub"],
        start=start,
        length=end - start + 1,
    )
    if not media:
        return JSONResponse({"ok": False, "message": "Recording not found"}, status_code=404)
    return Response(
        content=media["data"],
        status_code=status_code,
        media_type=info["content_type"],
        headers=headers,
    )


# ── UI ────────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.get("/privacy", response_class=HTMLResponse)
async def privacy() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "privacy.html").read_text(encoding="utf-8"))


@app.get("/terms", response_class=HTMLResponse)
async def terms() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "terms.html").read_text(encoding="utf-8"))


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
                raw_url = msg.get("url", "").strip()
                try:
                    url, meet_code = normalize_meet_url(raw_url)
                except ValueError as exc:
                    await send({"type": "error", "code": "invalid_meet_url", "message": str(exc)})
                    continue
                if bot_task and not bot_task.done():
                    await send({"type": "error", "code": "meeting_already_running", "message": "Already in a meeting."})
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
                            try:
                                await send({
                                    "type": "status",
                                    "message": "Saving recording to your private Atom library...",
                                })
                                entry = await add_recording(
                                    local, meet_code=meet_code, user_sub=user["sub"]
                                )
                                if summary_enabled():
                                    await send({
                                        "type": "status",
                                        "message": "Generating your meeting summary from audio...",
                                    })
                                    try:
                                        summary = await summarize_recording(local)
                                        entry = update_recording_summary(
                                            entry["id"],
                                            user["sub"],
                                            summary=summary,
                                            status="complete",
                                        ) or entry
                                    except Exception as summary_exc:
                                        logger.exception("Meeting summary generation failed")
                                        entry = update_recording_summary(
                                            entry["id"],
                                            user["sub"],
                                            summary=(
                                                "The recording was saved, but its meeting summary could not "
                                                "be generated. You can still play or download the recording."
                                            ),
                                            status="failed",
                                            error=str(summary_exc)[:500],
                                        ) or entry
                                else:
                                    entry = update_recording_summary(
                                        entry["id"],
                                        user["sub"],
                                        summary=(
                                            "The recording was saved. AI summary generation is not configured "
                                            "on this Atom server."
                                        ),
                                        status="not_configured",
                                        error="OPENAI_API_KEY is not configured.",
                                    ) or entry
                            finally:
                                cleanup_recording_files(local)
                            await send({"type": "recording", "entry": entry})
                            await send({
                                "type": "status",
                                "message": (
                                    "Recording and summary saved to your Atom library"
                                    if entry.get("summary_status") == "complete"
                                    else "Recording saved to your Atom library"
                                ),
                            })
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
        # The bot keeps recording and stores the completed media in the user's
        # database library even if their browser connection drops.
        logger.info("WebSocket disconnected; recording continues in background")
