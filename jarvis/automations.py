from __future__ import annotations

import sqlite3
import threading
import uuid
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .store import now


def _utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("run_at must be an ISO 8601 date and time") from exc
    if parsed.tzinfo is None:
        raise ValueError("run_at must include a timezone offset")
    return parsed.astimezone(UTC)


def _next_daily(local_time: str, timezone: str, after: datetime | None = None) -> datetime:
    try:
        zone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown IANA timezone: {timezone}") from exc
    try:
        clock = time.fromisoformat(local_time)
    except ValueError as exc:
        raise ValueError("local_time must be HH:MM or HH:MM:SS") from exc
    current = (after or datetime.now(UTC)).astimezone(zone)
    candidate = datetime.combine(current.date(), clock, zone)
    if candidate <= current:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


class AutomationStore:
    """Durable local schedules that launch normal Jarvis Core tasks."""

    def __init__(self, path: Path) -> None:
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS automations (
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              name TEXT NOT NULL, prompt TEXT NOT NULL,
              schedule_type TEXT NOT NULL, run_at TEXT,
              interval_seconds INTEGER, local_time TEXT, timezone TEXT,
              next_run_at TEXT NOT NULL, status TEXT NOT NULL,
              last_run_at TEXT, last_task_id TEXT, last_result TEXT, error TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_automations_due ON automations(status, next_run_at);
            """
        )
        self._conn.commit()
        self.recover_inflight()

    def create_once(self, session_id: str, name: str, prompt: str, run_at: str) -> dict[str, Any]:
        due = _utc(run_at)
        return self._create(session_id, name, prompt, "once", due)

    def create_interval(
        self, session_id: str, name: str, prompt: str, interval_seconds: int
    ) -> dict[str, Any]:
        interval_seconds = int(interval_seconds)
        if interval_seconds < 300:
            raise ValueError("Agent automations must be at least five minutes apart")
        due = datetime.now(UTC) + timedelta(seconds=interval_seconds)
        return self._create(
            session_id, name, prompt, "interval", due, interval_seconds=interval_seconds
        )

    def create_daily(
        self,
        session_id: str,
        name: str,
        prompt: str,
        local_time: str,
        timezone: str,
    ) -> dict[str, Any]:
        due = _next_daily(local_time, timezone)
        return self._create(
            session_id,
            name,
            prompt,
            "daily",
            due,
            local_time=local_time,
            timezone=timezone,
        )

    def _create(
        self,
        session_id: str,
        name: str,
        prompt: str,
        schedule_type: str,
        due: datetime,
        interval_seconds: int | None = None,
        local_time: str | None = None,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        name, prompt = name.strip(), prompt.strip()
        if not name or not prompt:
            raise ValueError("Automation name and prompt are required")
        automation_id = uuid.uuid4().hex
        timestamp = now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO automations(id, session_id, created_at, updated_at, name, prompt, "
                "schedule_type, run_at, interval_seconds, local_time, timezone, next_run_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')",
                (
                    automation_id,
                    session_id,
                    timestamp,
                    timestamp,
                    name,
                    prompt,
                    schedule_type,
                    due.isoformat() if schedule_type == "once" else None,
                    interval_seconds,
                    local_time,
                    timezone,
                    due.isoformat(),
                ),
            )
            self._conn.commit()
        return self.get(automation_id) or {}

    def get(self, automation_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM automations WHERE id = ?", (automation_id,)
        ).fetchone()
        return dict(row) if row else None

    def list(self, limit: int = 50, include_finished: bool = False) -> list[dict[str, Any]]:
        where = "" if include_finished else "WHERE status NOT IN ('completed', 'cancelled')"
        rows = self._conn.execute(
            f"SELECT * FROM automations {where} ORDER BY next_run_at LIMIT ?",
            (max(1, min(int(limit), 500)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def pause(self, automation_id: str) -> bool:
        return self._set_status(automation_id, "paused", ("active",))

    def resume(self, automation_id: str) -> bool:
        item = self.get(automation_id)
        if not item or item["status"] != "paused":
            return False
        next_run = self._next_after(item, datetime.now(UTC))
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE automations SET status = 'active', next_run_at = ?, updated_at = ?, error = NULL "
                "WHERE id = ? AND status = 'paused'",
                (next_run.isoformat(), now(), automation_id),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def cancel(self, automation_id: str) -> bool:
        return self._set_status(automation_id, "cancelled", ("active", "paused", "failed"))

    def _set_status(self, automation_id: str, status: str, from_statuses: tuple[str, ...]) -> bool:
        placeholders = ",".join("?" for _ in from_statuses)
        with self._lock:
            cursor = self._conn.execute(
                f"UPDATE automations SET status = ?, updated_at = ? WHERE id = ? "
                f"AND status IN ({placeholders})",
                (status, now(), automation_id, *from_statuses),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def claim_due(self, limit: int = 10) -> list[dict[str, Any]]:
        threshold = now()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM automations WHERE status = 'active' AND next_run_at <= ? "
                "ORDER BY next_run_at LIMIT ?",
                (threshold, max(1, min(int(limit), 50))),
            ).fetchall()
            ids = [row["id"] for row in rows]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                self._conn.execute(
                    f"UPDATE automations SET status = 'running', last_run_at = ?, updated_at = ? "
                    f"WHERE id IN ({placeholders})",
                    (threshold, threshold, *ids),
                )
                self._conn.commit()
        return [item for item_id in ids if (item := self.get(item_id))]

    def attach_task(self, automation_id: str, task_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE automations SET last_task_id = ?, updated_at = ? WHERE id = ?",
                (task_id, now(), automation_id),
            )
            self._conn.commit()

    def for_task(self, task_id: str) -> dict[str, Any] | None:
        return self._by_task(task_id)

    def awaiting_approval(self, task_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE automations SET status = 'awaiting_approval', updated_at = ? "
                "WHERE last_task_id = ? AND status = 'running'",
                (now(), task_id),
            )
            self._conn.commit()

    def complete_for_task(self, task_id: str, result: str) -> dict[str, Any] | None:
        item = self._by_task(task_id)
        if not item:
            return None
        if item["schedule_type"] == "once":
            status, next_run = "completed", item["next_run_at"]
        else:
            status = "active"
            next_run = self._next_after(item, datetime.now(UTC)).isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE automations SET status = ?, next_run_at = ?, last_result = ?, error = NULL, "
                "updated_at = ? WHERE id = ?",
                (status, next_run, result[:20_000], now(), item["id"]),
            )
            self._conn.commit()
        return self.get(item["id"])

    def fail_for_task(self, task_id: str, error: str) -> dict[str, Any] | None:
        item = self._by_task(task_id)
        if not item:
            return None
        status = "failed" if item["schedule_type"] == "once" else "active"
        next_run = (
            item["next_run_at"]
            if item["schedule_type"] == "once"
            else self._next_after(item, datetime.now(UTC)).isoformat()
        )
        with self._lock:
            self._conn.execute(
                "UPDATE automations SET status = ?, next_run_at = ?, error = ?, updated_at = ? "
                "WHERE id = ?",
                (status, next_run, error[:10_000], now(), item["id"]),
            )
            self._conn.commit()
        return self.get(item["id"])

    def _by_task(self, task_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT id FROM automations WHERE last_task_id = ? ORDER BY updated_at DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return self.get(row["id"]) if row else None

    def _next_after(self, item: dict[str, Any], after: datetime) -> datetime:
        if item["schedule_type"] == "interval":
            return after + timedelta(seconds=int(item["interval_seconds"]))
        if item["schedule_type"] == "daily":
            return _next_daily(item["local_time"], item["timezone"], after)
        return _utc(item["run_at"])

    def recover_inflight(self) -> None:
        timestamp = now()
        with self._lock:
            self._conn.execute(
                "UPDATE automations SET status = 'active', next_run_at = ?, updated_at = ?, "
                "error = 'Jarvis restarted before the scheduled run finished.' "
                "WHERE status = 'running'",
                (timestamp, timestamp),
            )
            self._conn.commit()
