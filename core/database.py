from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(os.getenv("ATOM_CONFIG_DIR", "config"))
DB_PATH = Path(os.getenv("ATOM_DB_PATH", str(CONFIG_DIR / "atom.db")))


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    with connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                sub TEXT PRIMARY KEY,
                email TEXT NOT NULL,
                username TEXT NOT NULL UNIQUE,
                display_name TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                user_sub TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                FOREIGN KEY (user_sub) REFERENCES users(sub) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS recordings (
                id TEXT PRIMARY KEY,
                user_sub TEXT NOT NULL,
                title TEXT NOT NULL,
                meet_code TEXT NOT NULL DEFAULT '',
                created_at INTEGER NOT NULL,
                duration INTEGER NOT NULL DEFAULT 0,
                size INTEGER NOT NULL DEFAULT 0,
                filename TEXT NOT NULL,
                s3_key TEXT,
                email_delivery_status TEXT,
                email_delivery_message TEXT,
                email_delivery_attached INTEGER NOT NULL DEFAULT 0,
                email_delivery_updated_at INTEGER,
                drive_delivery_status TEXT,
                drive_delivery_message TEXT,
                drive_file_id TEXT,
                drive_url TEXT,
                drive_delivery_updated_at INTEGER,
                summary TEXT,
                FOREIGN KEY (user_sub) REFERENCES users(sub) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_user_sub ON sessions(user_sub);
            CREATE INDEX IF NOT EXISTS idx_recordings_user_created
                ON recordings(user_sub, created_at DESC);
            """
        )
        _ensure_columns(conn, "recordings", {
            "drive_delivery_status": "TEXT",
            "drive_delivery_message": "TEXT",
            "drive_file_id": "TEXT",
            "drive_url": "TEXT",
            "drive_delivery_updated_at": "INTEGER",
            "summary": "TEXT",
        })
        _ensure_columns(conn, "users", {
            "display_name": "TEXT",
        })


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def migrate_json_files() -> None:
    """Import legacy JSON data once, if present, without deleting the source files."""
    init_db()
    users_path = CONFIG_DIR / "users.json"
    sessions_path = CONFIG_DIR / "sessions.json"
    recordings_path = CONFIG_DIR / "recordings.json"

    with connect() as conn:
        if users_path.exists():
            try:
                users = json.loads(users_path.read_text(encoding="utf-8"))
                for user in users.values():
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO users (sub, email, username, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        (
                            user.get("sub", ""),
                            user.get("email", ""),
                            user.get("username", ""),
                            int(user.get("created_at", 0)),
                        ),
                    )
            except Exception as exc:
                logger.warning("Legacy users.json migration skipped: %s", exc)

        if sessions_path.exists():
            try:
                sessions = json.loads(sessions_path.read_text(encoding="utf-8"))
                for token, session in sessions.items():
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO sessions (token, user_sub, created_at)
                        VALUES (?, ?, ?)
                        """,
                        (token, session.get("sub", ""), int(session.get("created_at", 0))),
                    )
            except Exception as exc:
                logger.warning("Legacy sessions.json migration skipped: %s", exc)

        if recordings_path.exists():
            try:
                recordings = json.loads(recordings_path.read_text(encoding="utf-8"))
                for rec in recordings:
                    delivery = rec.get("email_delivery") or {}
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO recordings (
                            id, user_sub, title, meet_code, created_at, duration, size,
                            filename, s3_key, email_delivery_status,
                            email_delivery_message, email_delivery_attached,
                            email_delivery_updated_at
                        )
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            rec.get("id", ""),
                            rec.get("user", ""),
                            rec.get("title", "meeting"),
                            rec.get("meet_code", ""),
                            int(rec.get("created_at", 0)),
                            int(rec.get("duration", 0)),
                            int(rec.get("size", 0)),
                            rec.get("filename", ""),
                            rec.get("s3_key"),
                            delivery.get("status"),
                            delivery.get("message"),
                            1 if delivery.get("attached") else 0,
                            delivery.get("updated_at"),
                        ),
                    )
            except Exception as exc:
                logger.warning("Legacy recordings.json migration skipped: %s", exc)


migrate_json_files()
