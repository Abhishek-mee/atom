from __future__ import annotations

import asyncio

import pytest

from core import storage, summarizer
from core.database import connect, init_db


def test_update_recording_summary(tmp_path, monkeypatch):
    db_path = tmp_path / "atom.db"
    monkeypatch.setattr("core.database.DB_PATH", db_path)
    monkeypatch.setattr(storage, "RECORDINGS_DIR", tmp_path / "recordings")
    init_db()
    with connect() as conn:
        conn.execute(
            "INSERT INTO users (sub, email, username, created_at) VALUES (?, ?, ?, ?)",
            ("owner-1", "owner@example.com", "owner", 1),
        )

    capture = tmp_path / "meeting.webm"
    capture.write_bytes(b"recording bytes")
    monkeypatch.setattr(storage, "_probe_duration", lambda path: 90)
    entry = asyncio.run(storage.add_recording(capture, "abc-defg-hij", "owner-1"))

    updated = storage.update_recording_summary(
        entry["id"],
        "owner-1",
        summary="Overview\n- Product review",
        status="complete",
    )

    assert updated is not None
    assert updated["summary_status"] == "complete"
    assert updated["summary"] == "Overview\n- Product review"
    assert updated["summary_updated_at"]
    assert storage.update_recording_summary(
        entry["id"], "another-owner", summary="no", status="complete"
    ) is None


def test_summarizer_requires_api_key(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    recording = tmp_path / "meeting.webm"
    recording.write_bytes(b"video")

    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        summarizer._summarize_recording_sync(recording)


def test_summary_response_output_text(monkeypatch):
    class Response:
        ok = True
        status_code = 200

        @staticmethod
        def json():
            return {"output_text": "Overview\n- Launch approved"}

    monkeypatch.setattr(summarizer.requests, "post", lambda *args, **kwargs: Response())
    result = summarizer._summarize_transcript("We approved the launch.", "secret")
    assert result == "Overview\n- Launch approved"
