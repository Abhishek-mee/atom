from __future__ import annotations

import asyncio
import importlib
import time


def _load_storage(monkeypatch, tmp_path):
    monkeypatch.setenv("ATOM_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ATOM_DB_PATH", str(tmp_path / "atom.db"))
    monkeypatch.setenv("RECORDINGS_DIR", str(tmp_path / "captures"))
    monkeypatch.setenv("MAX_RECORDING_DB_MB", "1")

    import core.database as database
    import core.storage as storage

    importlib.reload(database)
    importlib.reload(storage)
    database.init_db()
    with database.connect() as conn:
        conn.execute(
            """
            INSERT INTO users (sub, email, username, created_at)
            VALUES (?, ?, ?, ?), (?, ?, ?, ?)
            """,
            (
                "owner-1", "one@example.com", "owner-one", int(time.time()),
                "owner-2", "two@example.com", "owner-two", int(time.time()),
            ),
        )
    return storage


def test_recording_blob_is_private_and_deletable(monkeypatch, tmp_path):
    storage = _load_storage(monkeypatch, tmp_path)
    capture = tmp_path / "captures" / "meeting.webm"
    capture.parent.mkdir(parents=True)
    capture.write_bytes(b"atom-recording-bytes")

    entry = asyncio.run(storage.add_recording(capture, "abc-defg-hij", "owner-1"))

    assert entry["stored"] is True
    assert entry["url"] == "/recordings/meeting/media"
    assert storage.list_recordings("owner-2") == []
    assert storage.get_recording_media("meeting", "owner-2") is None
    assert storage.get_recording_media("meeting", "owner-1")["data"] == b"atom-recording-bytes"
    assert "recording_data" not in storage.list_recordings("owner-1")[0]

    assert storage.delete_recording("meeting", "owner-2") is False
    assert storage.delete_recording("meeting", "owner-1") is True
    assert storage.get_recording_media("meeting", "owner-1") is None


def test_recording_size_limit(monkeypatch, tmp_path):
    storage = _load_storage(monkeypatch, tmp_path)
    capture = tmp_path / "captures" / "large.webm"
    capture.parent.mkdir(parents=True)
    capture.write_bytes(b"x" * (1024 * 1024 + 1))

    try:
        asyncio.run(storage.add_recording(capture, "abc-defg-hij", "owner-1"))
    except ValueError as exc:
        assert "too large" in str(exc)
    else:
        raise AssertionError("oversized recording was stored")


def test_media_route_requires_owner_and_supports_ranges(monkeypatch, tmp_path):
    storage = _load_storage(monkeypatch, tmp_path)
    capture = tmp_path / "captures" / "private.webm"
    capture.parent.mkdir(parents=True)
    capture.write_bytes(b"0123456789abcdef")
    asyncio.run(storage.add_recording(capture, "abc-defg-hij", "owner-1"))

    import core.users as users
    import api.routes as routes

    importlib.reload(users)
    importlib.reload(routes)

    from fastapi.testclient import TestClient

    with TestClient(routes.app) as client:
        assert client.get("/recordings/private/media").status_code == 401

        token = users.create_session("owner-1")
        client.cookies.set(routes.SESSION_COOKIE, token)
        response = client.get(
            "/recordings/private/media",
            headers={"Range": "bytes=4-8"},
        )
        assert response.status_code == 206
        assert response.content == b"45678"
        assert response.headers["content-range"] == "bytes 4-8/16"


def test_admin_can_list_and_delete_recordings(monkeypatch, tmp_path):
    storage = _load_storage(monkeypatch, tmp_path)
    capture = tmp_path / "captures" / "admin-delete.webm"
    capture.parent.mkdir(parents=True)
    capture.write_bytes(b"admin-owned-recording")
    asyncio.run(storage.add_recording(capture, "abc-defg-hij", "owner-1"))

    import api.routes as routes

    importlib.reload(routes)
    routes.settings.admin_token = "test-admin-token"

    from fastapi.testclient import TestClient

    with TestClient(routes.app) as client:
        assert client.get("/admin/recordings").status_code == 401
        headers = {"X-Atom-Admin-Token": "test-admin-token"}
        listing = client.get("/admin/recordings", headers=headers)
        assert listing.status_code == 200
        assert listing.json()["items"][0]["owner_email"] == "one@example.com"

        deleted = client.delete("/admin/recordings/admin-delete", headers=headers)
        assert deleted.status_code == 200
        assert deleted.json()["ok"] is True
        assert storage.get_recording_media("admin-delete", "owner-1") is None
