"""Session (thread) bookkeeping: rollout storage and resume, mirroring
~/.codex/sessions. History items live in a SQLite database via the SDK's
SQLiteSession; this module names threads and lists/resumes them.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import app_home


def sessions_dir() -> Path:
    path = app_home() / "sessions"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_session_db() -> Path:
    return sessions_dir() / "history.db"


def new_thread_id() -> str:
    stamp = time.strftime("%Y%m%dT%H%M%S")
    return f"thread-{stamp}-{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class ThreadInfo:
    thread_id: str
    created_at: str
    updated_at: str
    items: int
    preview: str


def list_threads(limit: int = 20) -> list[ThreadInfo]:
    db = default_session_db()
    if not db.exists():
        return []
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute(
            """
            SELECT s.session_id, s.created_at, MAX(m.created_at), COUNT(m.id)
            FROM agent_sessions s LEFT JOIN agent_messages m
              ON m.session_id = s.session_id
            GROUP BY s.session_id ORDER BY MAX(m.created_at) DESC LIMIT ?
            """,
            (limit,),
        ).fetchall()
        infos = []
        for session_id, created, updated, count in rows:
            preview = ""
            first = conn.execute(
                "SELECT message_data FROM agent_messages WHERE session_id=? "
                "ORDER BY id LIMIT 1",
                (session_id,),
            ).fetchone()
            if first:
                try:
                    data = json.loads(first[0])
                    content = data.get("content", "")
                    if isinstance(content, list):
                        content = " ".join(
                            c.get("text", "") for c in content if isinstance(c, dict)
                        )
                    preview = str(content)[:80]
                except (json.JSONDecodeError, AttributeError):
                    preview = ""
            infos.append(
                ThreadInfo(session_id, str(created), str(updated), count, preview)
            )
        return infos
    finally:
        conn.close()


def latest_thread_id() -> str | None:
    threads = list_threads(limit=1)
    return threads[0].thread_id if threads else None
