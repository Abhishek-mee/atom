from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin

logger = logging.getLogger(__name__)


def gmail_enabled() -> bool:
    return bool(_smtp_user() and _smtp_password())


def _smtp_user() -> str:
    return os.getenv("GMAIL_SMTP_USER", "").strip()


def _smtp_password() -> str:
    return os.getenv("GMAIL_APP_PASSWORD", "").strip()


def _from_email() -> str:
    return os.getenv("GMAIL_FROM_EMAIL", "").strip() or _smtp_user()


def _attach_limit_bytes() -> int:
    raw = os.getenv("EMAIL_ATTACH_LIMIT_MB", "20").strip()
    try:
        return max(1, int(raw)) * 1024 * 1024
    except ValueError:
        return 20 * 1024 * 1024


def _absolute_url(url: str | None) -> str | None:
    if not url:
        return None
    if url.startswith(("http://", "https://")):
        return url
    base = os.getenv("APP_BASE_URL", "").strip()
    if not base:
        return None
    return urljoin(base.rstrip("/") + "/", url.lstrip("/"))


async def send_recording_email(
    *,
    entry: dict,
    recipient: str,
    local_path: Path | None,
) -> dict:
    """Send the completed recording to the signed-in user's Gmail address."""
    if not gmail_enabled():
        return {"status": "skipped", "message": "Gmail SMTP is not configured"}
    if not recipient:
        return {"status": "skipped", "message": "No recipient email is available"}

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _send_sync, entry, recipient, local_path)


def _send_sync(entry: dict, recipient: str, local_path: Path | None) -> dict:
    msg = EmailMessage()
    title = entry.get("title") or "meeting"
    filename = entry.get("filename") or "recording.mp4"
    playback_url = _absolute_url(entry.get("url"))

    msg["Subject"] = f"Atom recording ready: {title}"
    msg["From"] = _from_email()
    msg["To"] = recipient

    body = [
        "Your Atom meeting recording is ready.",
        "",
        f"Meeting: {title}",
    ]
    if playback_url:
        body += ["", f"Recording link: {playback_url}"]

    should_attach = bool(
        local_path and local_path.exists() and local_path.stat().st_size <= _attach_limit_bytes()
    )
    if should_attach:
        body += ["", "The recording is attached to this email."]
    elif local_path and local_path.exists():
        limit_mb = _attach_limit_bytes() // (1024 * 1024)
        body += ["", f"The file is larger than {limit_mb} MB, so Atom kept it in your library."]
    elif not playback_url:
        body += ["", "The recording is available in your Atom library."]

    msg.set_content("\n".join(body))

    attached = False
    if should_attach and local_path:
        ctype, _ = mimetypes.guess_type(filename)
        maintype, subtype = (ctype or "video/mp4").split("/", 1)
        msg.add_attachment(
            local_path.read_bytes(),
            maintype=maintype,
            subtype=subtype,
            filename=filename,
        )
        attached = True

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context, timeout=30) as smtp:
            smtp.login(_smtp_user(), _smtp_password())
            smtp.send_message(msg)
    except Exception as exc:
        logger.warning("Gmail delivery failed: %s", exc)
        return {"status": "failed", "message": str(exc), "attached": False}

    logger.info("Sent recording email to %s (attached=%s)", recipient, attached)
    return {"status": "sent", "message": "Sent with Gmail", "attached": attached}
