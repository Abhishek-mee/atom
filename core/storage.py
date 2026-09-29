"""
Storage layer for Atom.

Completed recordings are stored in SQLite and scoped to their signed-in owner.
Temporary capture files are removed after the database transaction completes.
"""
from __future__ import annotations

import logging
import mimetypes
import os
import subprocess
import time
from pathlib import Path

from core.database import connect

logger = logging.getLogger(__name__)

RECORDINGS_DIR = Path(os.getenv("RECORDINGS_DIR", "api/static/recordings"))


def max_recording_bytes() -> int:
    raw = os.getenv("MAX_RECORDING_DB_MB", "200").strip()
    try:
        return max(1, int(raw)) * 1024 * 1024
    except ValueError:
        return 200 * 1024 * 1024


def _probe_duration(path: Path) -> int:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=20,
        )
        return int(float(out.stdout.strip()))
    except Exception:
        return 0


async def add_recording(local_path: Path, meet_code: str = "", user_sub: str = "") -> dict:
    """Store a completed recording in SQLite and return its metadata."""
    import asyncio

    return await asyncio.to_thread(_add_recording_sync, local_path, meet_code, user_sub)


def _add_recording_sync(local_path: Path, meet_code: str, user_sub: str) -> dict:
    size = local_path.stat().st_size if local_path.exists() else 0
    if not size:
        raise ValueError("The completed recording file is empty.")
    limit = max_recording_bytes()
    if size > limit:
        raise ValueError(
            f"Recording is too large for Atom's database ({size // (1024 * 1024)} MB). "
            f"The current limit is {limit // (1024 * 1024)} MB."
        )
    duration = _probe_duration(local_path) if local_path.exists() else 0
    content_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
    recording_data = local_path.read_bytes()
    entry = {
        "id": local_path.stem,
        "user": user_sub,                 # owner (Google sub)
        "title": (meet_code or "meeting").replace("-", " ").strip() or "meeting",
        "meet_code": meet_code,
        "created_at": int(time.time()),
        "duration": duration,
        "size": size,
        "filename": local_path.name,
        "content_type": content_type,
        "stored": True,
        "summary": build_meeting_summary(meet_code=meet_code, duration=duration),
    }

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO recordings (
                id, user_sub, title, meet_code, created_at, duration, size, filename,
                summary, content_type, recording_data
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry["id"],
                entry["user"],
                entry["title"],
                entry["meet_code"],
                entry["created_at"],
                entry["duration"],
                entry["size"],
                entry["filename"],
                entry["summary"],
                entry["content_type"],
                recording_data,
            ),
        )
    return _decorate(entry)


def build_meeting_summary(*, meet_code: str, duration: int) -> str:
    minutes = max(1, round((duration or 0) / 60))
    title = (meet_code or "meeting").replace("-", " ").strip() or "meeting"
    return (
        f"Atom recorded the Google Meet session '{title}' for about {minutes} minute"
        f"{'' if minutes == 1 else 's'}. The recording is stored privately in the user's Atom library. "
        "Content-level summaries require transcript capture and are not generated from private account screens."
    )


def _decorate(e: dict) -> dict:
    """Add the authenticated media URL without exposing recording bytes."""
    url = f"/recordings/{e['id']}/media" if e.get("stored") else None
    return {**e, "url": url}


def purge_local_recording_files() -> int:
    """Delete unfinished capture files; completed media lives in SQLite."""
    count = 0
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    for path in RECORDINGS_DIR.glob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in {".mp4", ".webm", ".mkv", ".mov"}:
            continue
        try:
            path.unlink(missing_ok=True)
            count += 1
        except Exception as exc:
            logger.warning("Could not purge local recording file %s: %s", path, exc)
    return count


def list_recordings(user_sub: str = "") -> list[dict]:
    """Return recording metadata without loading BLOB data into memory."""
    out = []
    query = """
        SELECT id, user_sub, title, meet_code, created_at, duration, size,
               filename, summary, content_type,
               recording_data IS NOT NULL AS stored
        FROM recordings
    """
    params: tuple = ()
    if user_sub:
        query += " WHERE user_sub = ?"
        params = (user_sub,)
    query += " ORDER BY created_at DESC"
    with connect() as conn:
        for row in conn.execute(query, params):
            out.append(_decorate(_row_to_entry(row)))
    return out


def get_recording_media_info(rec_id: str, user_sub: str) -> dict | None:
    """Return media metadata without loading the recording BLOB."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT filename, content_type, length(recording_data) AS total
            FROM recordings
            WHERE id = ? AND user_sub = ? AND recording_data IS NOT NULL
            """,
            (rec_id, user_sub),
        ).fetchone()
    if not row:
        return None
    return {
        "filename": row["filename"],
        "content_type": row["content_type"] or "application/octet-stream",
        "total": int(row["total"]),
    }


def get_recording_media(
    rec_id: str,
    user_sub: str,
    *,
    start: int = 0,
    length: int | None = None,
) -> dict | None:
    """Return an owner-scoped BLOB slice for playback or download."""
    info = get_recording_media_info(rec_id, user_sub)
    if not info:
        return None
    length = info["total"] - start if length is None else length
    with connect() as conn:
        row = conn.execute(
            """
            SELECT substr(recording_data, ?, ?) AS data
            FROM recordings
            WHERE id = ? AND user_sub = ? AND recording_data IS NOT NULL
            """,
            (start + 1, length, rec_id, user_sub),
        ).fetchone()
    if not row:
        return None
    return {**info, "data": bytes(row["data"])}


def cleanup_recording_files(local_path: Path | None) -> None:
    """Remove completed recording artifacts from Atom local storage."""
    if not local_path:
        return
    candidates = {
        local_path,
        RECORDINGS_DIR / local_path.name,
        RECORDINGS_DIR / f"{local_path.stem}_audio.webm",
    }
    for path in candidates:
        try:
            path.unlink(missing_ok=True)
        except Exception as exc:
            logger.warning("Could not remove recording artifact %s: %s", path, exc)


def delete_recording(rec_id: str, user_sub: str) -> bool:
    """Delete a database recording, scoped to its owner."""
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id, user_sub, title, meet_code, created_at, duration, size,
                   filename, summary, content_type,
                   recording_data IS NOT NULL AS stored
            FROM recordings WHERE id = ? AND user_sub = ?
            """,
            (rec_id, user_sub),
        ).fetchone()
    target = _row_to_entry(row) if row else None
    if not target:
        return False

    # local file
    try:
        (RECORDINGS_DIR / target["filename"]).unlink(missing_ok=True)
    except Exception:
        pass
    with connect() as conn:
        conn.execute("DELETE FROM recordings WHERE id = ? AND user_sub = ?", (rec_id, user_sub))
    logger.info("Deleted recording %s", rec_id)
    return True


def _row_to_entry(row) -> dict:
    entry = {
        "id": row["id"],
        "user": row["user_sub"],
        "title": row["title"],
        "meet_code": row["meet_code"],
        "created_at": row["created_at"],
        "duration": row["duration"],
        "size": row["size"],
        "filename": row["filename"],
        "summary": row["summary"] if "summary" in row.keys() else "",
        "content_type": row["content_type"] if "content_type" in row.keys() else None,
        "stored": bool(row["stored"]) if "stored" in row.keys() else (
            "recording_data" in row.keys() and row["recording_data"] is not None
        ),
    }
    return entry
