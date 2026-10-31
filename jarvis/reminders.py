from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .store import now


def _parse_due_at(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("due_at must be an ISO 8601 date and time") from exc
    if parsed.tzinfo is None:
        raise ValueError("due_at must include a timezone offset")
    return parsed.astimezone(UTC)


class ReminderStore:
    """Durable, local reminder queue used by the always-on Jarvis daemon."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS reminders (
              id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              due_at TEXT NOT NULL, message TEXT NOT NULL, status TEXT NOT NULL,
              repeat_seconds INTEGER, last_fired_at TEXT, source TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, due_at);
            """
        )
        self._conn.commit()
        self.recover_inflight()

    def create(
        self,
        message: str,
        due_at: str,
        repeat_seconds: int | None = None,
        source: str = "user_explicit",
    ) -> dict[str, Any]:
        message = message.strip()
        if not message:
            raise ValueError("Reminder message cannot be empty")
        due = _parse_due_at(due_at)
        if repeat_seconds is not None:
            repeat_seconds = int(repeat_seconds)
            if repeat_seconds < 60:
                raise ValueError("Repeating reminders must be at least 60 seconds apart")
        reminder_id = uuid.uuid4().hex
        timestamp = now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO reminders(id, created_at, updated_at, due_at, message, status, repeat_seconds, source) "
                "VALUES (?, ?, ?, ?, ?, 'scheduled', ?, ?)",
                (
                    reminder_id,
                    timestamp,
                    timestamp,
                    due.isoformat(),
                    message,
                    repeat_seconds,
                    source,
                ),
            )
            self._conn.commit()
        return self.get(reminder_id) or {}

    def get(self, reminder_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
        return dict(row) if row else None

    def list(self, limit: int = 50, include_finished: bool = False) -> list[dict[str, Any]]:
        where = "" if include_finished else "WHERE status IN ('scheduled', 'firing')"
        rows = self._conn.execute(
            f"SELECT * FROM reminders {where} ORDER BY due_at LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def cancel(self, reminder_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE reminders SET status = 'cancelled', updated_at = ? "
                "WHERE id = ? AND status IN ('scheduled', 'firing')",
                (now(), reminder_id),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def claim_due(self, at: datetime | None = None, limit: int = 20) -> list[dict[str, Any]]:
        threshold = (at or datetime.now(UTC)).astimezone(UTC).isoformat()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM reminders WHERE status = 'scheduled' AND due_at <= ? "
                "ORDER BY due_at LIMIT ?",
                (threshold, max(1, min(int(limit), 100))),
            ).fetchall()
            ids = [row["id"] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self._conn.execute(
                    f"UPDATE reminders SET status = 'firing', updated_at = ? WHERE id IN ({placeholders})",
                    (now(), *ids),
                )
                self._conn.commit()
        return [item for reminder_id in ids if (item := self.get(reminder_id))]

    def mark_fired(
        self, reminder_id: str, fired_at: datetime | None = None
    ) -> dict[str, Any] | None:
        item = self.get(reminder_id)
        if not item or item["status"] != "firing":
            return item
        fired = (fired_at or datetime.now(UTC)).astimezone(UTC)
        repeat_seconds = item.get("repeat_seconds")
        if repeat_seconds:
            next_due = _parse_due_at(item["due_at"])
            while next_due <= fired:
                next_due += timedelta(seconds=int(repeat_seconds))
            status = "scheduled"
            due_at = next_due.isoformat()
        else:
            status = "completed"
            due_at = item["due_at"]
        with self._lock:
            self._conn.execute(
                "UPDATE reminders SET status = ?, due_at = ?, last_fired_at = ?, updated_at = ? "
                "WHERE id = ? AND status = 'firing'",
                (status, due_at, fired.isoformat(), now(), reminder_id),
            )
            self._conn.commit()
        return self.get(reminder_id)

    def recover_inflight(self) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE reminders SET status = 'scheduled', updated_at = ? WHERE status = 'firing'",
                (now(),),
            )
            self._conn.commit()
