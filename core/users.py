"""
End-user accounts for Atom (SaaS).

Users sign in with Google. We store only what's needed to scope recordings:
their Google id (sub), email, and a random unique username. Sessions are
cookie-based. No passwords, no profile data beyond this.
"""
from __future__ import annotations

import logging
import os
import random
import re
import secrets
import time

from core.database import connect

logger = logging.getLogger(__name__)

_ADJ = ["swift", "calm", "bright", "bold", "lucid", "keen", "amber", "cobalt",
        "noble", "quiet", "rapid", "vivid", "lunar", "solar", "north", "zen"]
_NOUN = ["otter", "falcon", "cedar", "comet", "delta", "ember", "fjord", "heron",
         "ibex", "koala", "lynx", "maple", "nimbus", "onyx", "quartz", "raven"]


def google_client_id() -> str:
    return os.getenv("GOOGLE_CLIENT_ID", "").strip()


def user_count() -> int:
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0])


def session_count() -> int:
    with connect() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0])


def _gen_username(existing: set[str]) -> str:
    for _ in range(50):
        u = f"{random.choice(_ADJ)}-{random.choice(_NOUN)}-{random.randint(100, 9999)}"
        if u not in existing:
            return u
    return f"user-{secrets.token_hex(4)}"


# ── users ─────────────────────────────────────────────────────────────────────
def get_or_create_user(sub: str, email: str) -> dict:
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE sub = ?", (sub,)).fetchone()
        if row:
            if row["email"] != email:
                conn.execute("UPDATE users SET email = ? WHERE sub = ?", (email, sub))
            return _user_dict(conn.execute("SELECT * FROM users WHERE sub = ?", (sub,)).fetchone())

        taken = {r["username"] for r in conn.execute("SELECT username FROM users")}
        user = {
            "sub": sub,
            "email": email,
            "username": _gen_username(taken),
            "created_at": int(time.time()),
        }
        conn.execute(
            """
            INSERT INTO users (sub, email, username, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user["sub"], user["email"], user["username"], user["created_at"]),
        )
        logger.info("New user %s (%s)", user["username"], email)
        return user


def update_username(sub: str, username: str) -> tuple[bool, str, dict | None]:
    username = re.sub(r"\s+", "-", username.strip().lower())
    username = re.sub(r"[^a-z0-9_-]", "", username)
    username = username.strip("-_")
    if len(username) < 3:
        return False, "Username must be at least 3 letters or numbers.", None
    if len(username) > 32:
        return False, "Username must be 32 characters or less.", None
    with connect() as conn:
        existing = conn.execute(
            "SELECT sub FROM users WHERE username = ? AND sub != ?",
            (username, sub),
        ).fetchone()
        if existing:
            return False, "That username is already taken.", None
        conn.execute("UPDATE users SET username = ?, display_name = ? WHERE sub = ?", (username, username, sub))
        row = conn.execute("SELECT * FROM users WHERE sub = ?", (sub,)).fetchone()
        return True, "Updated", _user_dict(row)


# ── sessions ──────────────────────────────────────────────────────────────────
def create_session(sub: str) -> str:
    token = secrets.token_urlsafe(32)
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token, user_sub, created_at) VALUES (?, ?, ?)",
            (token, sub, int(time.time())),
        )
    return token


def user_for_session(token: str | None) -> dict | None:
    if not token:
        return None
    with connect() as conn:
        row = conn.execute(
            """
            SELECT users.*
            FROM sessions
            JOIN users ON users.sub = sessions.user_sub
            WHERE sessions.token = ?
            """,
            (token,),
        ).fetchone()
        return _user_dict(row) if row else None


def destroy_session(token: str | None) -> None:
    if not token:
        return
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def _user_dict(row) -> dict:
    has_display_name = "display_name" in row.keys()
    display_name = row["display_name"] if has_display_name and row["display_name"] else row["username"]
    return {
        "sub": row["sub"],
        "email": row["email"],
        "username": row["username"],
        "display_name": display_name,
        "created_at": row["created_at"],
    }


# ── Google ID token verification ──────────────────────────────────────────────
def verify_google_credential(credential: str) -> dict | None:
    """Verify a Google Identity Services ID token; return {sub, email} or None."""
    cid = google_client_id()
    if not cid:
        logger.warning("GOOGLE_CLIENT_ID not set")
        return None
    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as g_requests
        info = id_token.verify_oauth2_token(credential, g_requests.Request(), cid)
        if not info.get("email_verified", False):
            return None
        return {"sub": info["sub"], "email": info.get("email", "")}
    except Exception as e:
        logger.warning("Google token verify failed: %s", e)
        return None
