"""
Storage layer for Atom (SaaS model).

- S3 is OPERATOR infrastructure, configured once via server env vars.
  End users never see or configure it.
- Every finished recording is uploaded to the operator bucket and indexed in
  SQLite. The main page shows the library.
- Playback URLs are presigned on demand (so they never go stale in the index).
"""
from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path

from core.database import connect

logger = logging.getLogger(__name__)

RECORDINGS_DIR = Path(os.getenv("RECORDINGS_DIR", "api/static/recordings"))


# ── Operator S3 config (server env) ───────────────────────────────────────────
def _s3_cfg() -> dict:
    return {
        "bucket":     os.getenv("S3_BUCKET", "").strip(),
        "region":     os.getenv("S3_REGION", "us-east-1").strip(),
        "prefix":     os.getenv("S3_PREFIX", "recordings/").strip(),
        "access_key": os.getenv("AWS_ACCESS_KEY_ID", "").strip(),
        "secret_key": os.getenv("AWS_SECRET_ACCESS_KEY", "").strip(),
        "presign_days": int(os.getenv("S3_PRESIGN_DAYS", "7")),
        "keep_local": os.getenv("S3_KEEP_LOCAL", "false").lower() in ("1", "true", "yes"),
    }


def s3_enabled() -> bool:
    c = _s3_cfg()
    return bool(c["bucket"] and c["access_key"] and c["secret_key"])


def _client():
    import boto3
    c = _s3_cfg()
    return boto3.client(
        "s3", region_name=c["region"],
        aws_access_key_id=c["access_key"], aws_secret_access_key=c["secret_key"],
    )


def _presign(key: str) -> str | None:
    if not s3_enabled():
        return None
    c = _s3_cfg()
    try:
        return _client().generate_presigned_url(
            "get_object",
            Params={"Bucket": c["bucket"], "Key": key},
            ExpiresIn=c["presign_days"] * 86400,
        )
    except Exception as e:
        logger.warning("presign failed: %s", e)
        return None


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
    """Upload to S3 (if configured), index in the library, return the entry."""
    import asyncio, functools

    size = local_path.stat().st_size if local_path.exists() else 0
    duration = _probe_duration(local_path) if local_path.exists() else 0
    entry = {
        "id": local_path.stem,
        "user": user_sub,                 # owner (Google sub)
        "title": (meet_code or "meeting").replace("-", " ").strip() or "meeting",
        "meet_code": meet_code,
        "created_at": int(time.time()),
        "duration": duration,
        "size": size,
        "filename": local_path.name,
        "s3_key": None,
    }

    if s3_enabled():
        c = _s3_cfg()
        key = (c["prefix"] or "").lstrip("/") + local_path.name

        def _upload():
            _client().upload_file(
                str(local_path), c["bucket"], key,
                ExtraArgs={"ContentType": "video/mp4"},
            )
        try:
            await asyncio.get_event_loop().run_in_executor(None, functools.partial(_upload))
            entry["s3_key"] = key
            logger.info("Uploaded to s3://%s/%s", c["bucket"], key)
            if not c["keep_local"]:
                local_path.unlink(missing_ok=True)
        except Exception as e:
            logger.error("S3 upload failed: %s", e)

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO recordings (
                id, user_sub, title, meet_code, created_at, duration, size, filename, s3_key
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                entry["s3_key"],
            ),
        )
    return _decorate(entry)


def _decorate(e: dict) -> dict:
    """Add a resolved playback URL (presigned S3, else local static)."""
    url = None
    if e.get("s3_key"):
        url = _presign(e["s3_key"])
    if not url:
        local = RECORDINGS_DIR / e["filename"]
        url = f"/recordings/{e['filename']}" if local.exists() else None
    return {**e, "url": url}


def list_recordings(user_sub: str = "") -> list[dict]:
    """Return this user's recordings with fresh playback URLs."""
    out = []
    query = "SELECT * FROM recordings"
    params: tuple = ()
    if user_sub:
        query += " WHERE user_sub = ?"
        params = (user_sub,)
    query += " ORDER BY created_at DESC"
    with connect() as conn:
        for row in conn.execute(query, params):
            d = _decorate(_row_to_entry(row))
            if d["url"]:
                out.append(d)
    return out


def update_recording_delivery(rec_id: str, user_sub: str, delivery: dict) -> None:
    """Persist email delivery status for a user's recording."""
    with connect() as conn:
        conn.execute(
            """
            UPDATE recordings
            SET email_delivery_status = ?,
                email_delivery_message = ?,
                email_delivery_attached = ?,
                email_delivery_updated_at = ?
            WHERE id = ? AND user_sub = ?
            """,
            (
                delivery.get("status", "unknown"),
                delivery.get("message", ""),
                1 if delivery.get("attached") else 0,
                int(time.time()),
                rec_id,
                user_sub,
            ),
        )


def delete_recording(rec_id: str, user_sub: str) -> bool:
    """Delete a recording (index + local file + S3 object), scoped to its owner."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM recordings WHERE id = ? AND user_sub = ?",
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
    # S3 object
    if target.get("s3_key") and s3_enabled():
        try:
            _client().delete_object(Bucket=_s3_cfg()["bucket"], Key=target["s3_key"])
        except Exception as e:
            logger.warning("S3 delete failed: %s", e)

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
        "s3_key": row["s3_key"],
    }
    if row["email_delivery_status"]:
        entry["email_delivery"] = {
            "status": row["email_delivery_status"],
            "message": row["email_delivery_message"] or "",
            "attached": bool(row["email_delivery_attached"]),
            "updated_at": row["email_delivery_updated_at"],
        }
    return entry
