from __future__ import annotations

import asyncio
import logging
import mimetypes
import os
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

DRIVE_FILE_SCOPE = "https://www.googleapis.com/auth/drive.file"
DRIVE_FOLDER_NAME = os.getenv("GOOGLE_DRIVE_FOLDER_NAME", "Atom Recordings").strip()


async def upload_recording_to_drive(
    *,
    access_token: str | None,
    local_path: Path | None,
    entry: dict,
) -> dict:
    """Upload the recording into the signed-in user's Google Drive."""
    if not access_token:
        return {"status": "skipped", "message": "Google Drive permission was not granted"}
    if not local_path or not local_path.exists():
        return {"status": "failed", "message": "Recording file is not available for Drive upload"}

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _upload_sync, access_token, local_path, entry)


def _upload_sync(access_token: str, local_path: Path, entry: dict) -> dict:
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        folder_id = _ensure_folder(headers)
        metadata = {
            "name": entry.get("filename") or local_path.name,
            "description": f"Atom recording for {entry.get('title') or 'meeting'}",
        }
        if folder_id:
            metadata["parents"] = [folder_id]

        mime_type = mimetypes.guess_type(local_path.name)[0] or "video/mp4"
        start = requests.post(
            "https://www.googleapis.com/upload/drive/v3/files"
            "?uploadType=resumable&fields=id,name,webViewLink,webContentLink",
            headers={
                **headers,
                "Content-Type": "application/json; charset=UTF-8",
                "X-Upload-Content-Type": mime_type,
                "X-Upload-Content-Length": str(local_path.stat().st_size),
            },
            json=metadata,
            timeout=30,
        )
        if start.status_code >= 400:
            return {"status": "failed", "message": _error_message(start)}

        upload_url = start.headers.get("Location")
        if not upload_url:
            return {"status": "failed", "message": "Google Drive did not return an upload URL"}

        with local_path.open("rb") as fh:
            finish = requests.put(
                upload_url,
                headers={"Content-Type": mime_type, "Content-Length": str(local_path.stat().st_size)},
                data=fh,
                timeout=600,
            )
        if finish.status_code >= 400:
            return {"status": "failed", "message": _error_message(finish)}

        data = finish.json()
        return {
            "status": "uploaded",
            "message": "Uploaded to Google Drive",
            "file_id": data.get("id"),
            "url": data.get("webViewLink") or data.get("webContentLink"),
        }
    except Exception as exc:
        logger.warning("Google Drive upload failed: %s", exc)
        return {"status": "failed", "message": str(exc)}


def _ensure_folder(headers: dict[str, str]) -> str | None:
    if not DRIVE_FOLDER_NAME:
        return None

    folder_name = DRIVE_FOLDER_NAME.replace("\\", "\\\\").replace("'", "\\'")
    query = (
        "mimeType='application/vnd.google-apps.folder' "
        f"and name='{folder_name}' and trashed=false"
    )
    resp = requests.get(
        "https://www.googleapis.com/drive/v3/files",
        headers=headers,
        params={"q": query, "spaces": "drive", "fields": "files(id,name)", "pageSize": 1},
        timeout=30,
    )
    if resp.status_code < 400:
        files = resp.json().get("files") or []
        if files:
            return files[0].get("id")

    resp = requests.post(
        "https://www.googleapis.com/drive/v3/files?fields=id",
        headers={**headers, "Content-Type": "application/json"},
        json={
            "name": DRIVE_FOLDER_NAME,
            "mimeType": "application/vnd.google-apps.folder",
        },
        timeout=30,
    )
    if resp.status_code >= 400:
        logger.warning("Google Drive folder creation failed: %s", _error_message(resp))
        return None
    return resp.json().get("id")


def _error_message(resp: requests.Response) -> str:
    try:
        data = resp.json()
        return data.get("error", {}).get("message") or resp.text
    except Exception:
        return resp.text or f"HTTP {resp.status_code}"
