"""Audio-only meeting transcription and summary generation."""
from __future__ import annotations

import asyncio
import os
import subprocess
import tempfile
from pathlib import Path

import requests

OPENAI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")


def summary_enabled() -> bool:
    return bool(os.getenv("OPENAI_API_KEY", "").strip())


async def summarize_recording(recording_path: Path) -> str:
    """Create a structured meeting summary from recording audio only."""
    return await asyncio.to_thread(_summarize_recording_sync, recording_path)


def _summarize_recording_sync(recording_path: Path) -> str:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("AI summary is not configured. Add OPENAI_API_KEY to Railway Variables.")
    if not recording_path.exists():
        raise RuntimeError("The temporary recording is no longer available for summarization.")

    with tempfile.TemporaryDirectory(prefix="atom-summary-") as temp_dir:
        chunks = _extract_audio_chunks(recording_path, Path(temp_dir))
        transcript_parts = [_transcribe(path, api_key) for path in chunks]
    transcript = "\n\n".join(part.strip() for part in transcript_parts if part.strip()).strip()
    if not transcript:
        raise RuntimeError("No speech was detected in the meeting audio.")
    return _summarize_transcript(transcript, api_key)


def _extract_audio_chunks(recording_path: Path, output_dir: Path) -> list[Path]:
    pattern = output_dir / "audio-%03d.mp3"
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error", "-i", str(recording_path), "-vn",
                "-ac", "1", "-ar", "16000", "-b:a", "32k", "-f", "segment",
                "-segment_time", "1200", "-reset_timestamps", "1", str(pattern),
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("FFmpeg is unavailable on the Atom server.") from exc
    chunks = sorted(output_dir.glob("audio-*.mp3"))
    if result.returncode != 0 or not chunks:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "no audio stream"
        raise RuntimeError(f"Could not extract meeting audio: {detail}")
    return chunks


def _transcribe(audio_path: Path, api_key: str) -> str:
    model = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-transcribe").strip()
    with audio_path.open("rb") as audio:
        response = requests.post(
            f"{OPENAI_BASE_URL}/audio/transcriptions",
            headers={"Authorization": f"Bearer {api_key}"},
            data={"model": model},
            files={"file": (audio_path.name, audio, "audio/mpeg")},
            timeout=300,
        )
    _raise_api_error(response, "Transcription")
    return str(response.json().get("text") or "")


def _summarize_transcript(transcript: str, api_key: str) -> str:
    model = os.getenv("OPENAI_SUMMARY_MODEL", "gpt-5.4-mini").strip()
    response = requests.post(
        f"{OPENAI_BASE_URL}/responses",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "instructions": (
                "Summarize a meeting transcript accurately. Do not invent facts or identify speakers "
                "unless the transcript explicitly names them. Return plain text with exactly these "
                "headings: Overview, Key points, Decisions, Action items. Use concise bullet points. "
                "Write 'None captured' when a section has no evidence."
            ),
            "input": transcript,
            "max_output_tokens": 1500,
        },
        timeout=300,
    )
    _raise_api_error(response, "Summary")
    payload = response.json()
    if payload.get("output_text"):
        return str(payload["output_text"]).strip()
    for item in payload.get("output", []):
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                return str(content["text"]).strip()
    raise RuntimeError("The summary service returned no text.")


def _raise_api_error(response: requests.Response, label: str) -> None:
    if response.ok:
        return
    try:
        message = response.json().get("error", {}).get("message")
    except ValueError:
        message = None
    raise RuntimeError(f"{label} failed: {message or f'HTTP {response.status_code}'}")
