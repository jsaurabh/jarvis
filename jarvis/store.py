from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .learning import harness_snapshot
from .security import redact


def now() -> str:
    return datetime.now(UTC).isoformat()


class TraceStore:
    def __init__(self, path: Path, trace_mode: str = "full") -> None:
        self.path = path
        self.trace_mode = trace_mode
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()
        self._backfill_learning_metadata()

    def _migrate(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
              id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              title TEXT NOT NULL DEFAULT 'New conversation', last_response_id TEXT
            );
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL, created_at TEXT NOT NULL,
              completed_at TEXT, status TEXT NOT NULL, user_text TEXT NOT NULL,
              assistant_text TEXT, model TEXT NOT NULL, error TEXT,
              rating INTEGER, correction TEXT,
              FOREIGN KEY(session_id) REFERENCES sessions(id)
            );
            CREATE TABLE IF NOT EXISTS events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
              created_at TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
              FOREIGN KEY(run_id) REFERENCES runs(id)
            );
            CREATE TABLE IF NOT EXISTS approvals (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL, session_id TEXT NOT NULL,
              created_at TEXT NOT NULL, status TEXT NOT NULL,
              response_id TEXT NOT NULL, call_id TEXT NOT NULL,
              tool_name TEXT NOT NULL, arguments TEXT NOT NULL,
              FOREIGN KEY(run_id) REFERENCES runs(id)
            );
            CREATE TABLE IF NOT EXISTS tasks (
              id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              status TEXT NOT NULL, request TEXT NOT NULL,
              result TEXT, error TEXT, run_id TEXT, approval_id TEXT,
              active_phase TEXT NOT NULL DEFAULT 'queued',
              auto_retry INTEGER NOT NULL DEFAULT 1,
              cancel_requested INTEGER NOT NULL DEFAULT 0,
              FOREIGN KEY(session_id) REFERENCES sessions(id),
              FOREIGN KEY(run_id) REFERENCES runs(id)
            );
            CREATE TABLE IF NOT EXISTS task_events (
              id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
              created_at TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
              FOREIGN KEY(task_id) REFERENCES tasks(id)
            );
            CREATE TABLE IF NOT EXISTS task_approvals (
              id TEXT PRIMARY KEY, task_id TEXT NOT NULL, run_id TEXT NOT NULL,
              session_id TEXT NOT NULL, created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL, status TEXT NOT NULL,
              tool_name TEXT NOT NULL, arguments TEXT NOT NULL,
              call_id TEXT NOT NULL, checkpoint TEXT NOT NULL,
              FOREIGN KEY(task_id) REFERENCES tasks(id),
              FOREIGN KEY(run_id) REFERENCES runs(id)
            );
            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status, updated_at);
            CREATE INDEX IF NOT EXISTS idx_task_events_task ON task_events(task_id, id);
            CREATE TABLE IF NOT EXISTS eval_snapshots (
              id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
              metrics TEXT NOT NULL, passed INTEGER NOT NULL, notes TEXT
            );
            CREATE TABLE IF NOT EXISTS model_registry (
              name TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              artifact_path TEXT NOT NULL, base_model TEXT NOT NULL,
              metrics TEXT NOT NULL, status TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS run_contexts (
              run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL,
              schema_version TEXT NOT NULL, context TEXT NOT NULL,
              FOREIGN KEY(run_id) REFERENCES runs(id)
            );
            CREATE TABLE IF NOT EXISTS run_scores (
              run_id TEXT NOT NULL, grader TEXT NOT NULL, created_at TEXT NOT NULL,
              score REAL NOT NULL, label TEXT NOT NULL, rationale TEXT NOT NULL,
              details TEXT NOT NULL, PRIMARY KEY(run_id, grader),
              FOREIGN KEY(run_id) REFERENCES runs(id)
            );
            CREATE INDEX IF NOT EXISTS idx_run_scores_grader ON run_scores(grader, score);
            """
        )
        self._conn.commit()

    def _backfill_learning_metadata(self) -> None:
        rows = self._conn.execute(
            "SELECT id, model FROM runs WHERE id NOT IN (SELECT run_id FROM run_contexts)"
        ).fetchall()
        for row in rows:
            interface = "realtime_voice" if "realtime" in row["model"] else "legacy_agent"
            self.set_run_context(
                row["id"],
                harness_snapshot(
                    interface=interface,
                    model=row["model"],
                    instructions="",
                    tools=[],
                    version="legacy-unversioned",
                ),
            )
        ungraded = self._conn.execute(
            "SELECT id FROM runs WHERE status IN ('completed', 'failed', 'cancelled') "
            "AND id NOT IN (SELECT run_id FROM run_scores WHERE grader = 'task_completion.v1')"
        ).fetchall()
        for row in ungraded:
            self.grade_run(row["id"])

    def create_session(self) -> str:
        session_id = uuid.uuid4().hex
        timestamp = now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions(id, created_at, updated_at) VALUES (?, ?, ?)",
                (session_id, timestamp, timestamp),
            )
            self._conn.commit()
        return session_id

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        return dict(row) if row else None

    def list_sessions(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, title, created_at, updated_at FROM sessions ORDER BY updated_at DESC LIMIT 100"
        ).fetchall()
        return [dict(row) for row in rows]

    def runs_for_session(self, session_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT id, created_at, completed_at, status, user_text, assistant_text, model, error, rating, correction "
            "FROM runs WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def all_runs(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM runs ORDER BY created_at").fetchall()
        return [dict(row) for row in rows]

    def create_run(self, session_id: str, user_text: str, model: str) -> str:
        run_id = uuid.uuid4().hex
        timestamp = now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO runs(id, session_id, created_at, status, user_text, model) VALUES (?, ?, ?, 'running', ?, ?)",
                (run_id, session_id, timestamp, user_text, model),
            )
            self._conn.execute(
                "UPDATE sessions SET updated_at = ?, title = CASE WHEN title = 'New conversation' THEN ? ELSE title END WHERE id = ?",
                (timestamp, user_text[:72], session_id),
            )
            self._conn.commit()
        self.event(run_id, "user_message", {"text": user_text})
        return run_id

    def record_completed_turn(
        self,
        session_id: str,
        user_text: str,
        assistant_text: str,
        model: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Record a completed externally-driven turn, such as a Realtime voice turn."""
        run_id = self.create_run(session_id, user_text, model)
        metadata = dict(metadata or {})
        harness = metadata.pop("harness", None)
        supplied = harness or {}
        context = harness_snapshot(
            interface=str(metadata.get("interface", "realtime_voice")),
            model=model,
            instructions=str(supplied.get("instructions", "")),
            tools=list(supplied.get("tools", [])),
            model_settings=dict(supplied.get("model_settings", {})),
            version=str(supplied.get("version", "realtime-legacy")),
        )
        self.set_run_context(run_id, context)
        if metadata:
            self.event(run_id, "realtime_metadata", metadata)
        self.finish_run(run_id, assistant_text)
        return run_id

    def set_run_context(self, run_id: str, context: dict[str, Any]) -> None:
        clean = redact(context)
        schema = str(clean.get("schema", "jarvis.harness.v1"))
        with self._lock:
            self._conn.execute(
                "INSERT INTO run_contexts(run_id, created_at, schema_version, context) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET schema_version = excluded.schema_version, context = excluded.context",
                (run_id, now(), schema, json.dumps(clean, ensure_ascii=False, default=str)),
            )
            self._conn.commit()

    def run_context(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT context FROM run_contexts WHERE run_id = ?", (run_id,)
        ).fetchone()
        return json.loads(row["context"]) if row else None

    def distinct_harness_contexts(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT context FROM run_contexts ORDER BY created_at").fetchall()
        unique: dict[str, dict[str, Any]] = {}
        for row in rows:
            context = json.loads(row["context"])
            key = str(
                context.get("harness_sha256")
                or context.get("version")
                or json.dumps(context, sort_keys=True)
            )
            unique[key] = context
        return list(unique.values())

    def event(self, run_id: str, kind: str, payload: Any) -> None:
        if self.trace_mode == "off":
            return
        clean = redact(payload)
        if self.trace_mode == "metadata" and kind in {"model_output", "tool_result", "tool_call"}:
            clean = {"recorded": False, "reason": "metadata trace mode"}
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(run_id, created_at, kind, payload) VALUES (?, ?, ?, ?)",
                (run_id, now(), kind, json.dumps(clean, ensure_ascii=False, default=str)),
            )
            self._conn.commit()

    def update_response_id(self, session_id: str, response_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET last_response_id = ?, updated_at = ? WHERE id = ?",
                (response_id, now(), session_id),
            )
            self._conn.commit()

    def finish_run(self, run_id: str, text: str, response_id: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status = 'completed', completed_at = ?, assistant_text = ? WHERE id = ?",
                (now(), text, run_id),
            )
            self._conn.commit()
        self.event(run_id, "assistant_message", {"text": text, "response_id": response_id})
        self.grade_run(run_id)

    def fail_run(self, run_id: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status = 'failed', completed_at = ?, error = ? WHERE id = ?",
                (now(), error, run_id),
            )
            self._conn.commit()
        self.event(run_id, "error", {"message": error})
        self.grade_run(run_id)

    def cancel_run(self, run_id: str, reason: str = "Cancelled by the user") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status = 'cancelled', completed_at = ?, error = ? WHERE id = ?",
                (now(), reason, run_id),
            )
            self._conn.commit()
        self.event(run_id, "cancelled", {"message": reason})
        self.grade_run(run_id)

    def mark_run_awaiting_approval(self, run_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET status = 'awaiting_approval' WHERE id = ?", (run_id,)
            )
            self._conn.commit()

    def create_approval(
        self,
        run_id: str,
        session_id: str,
        response_id: str,
        call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> str:
        approval_id = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                "INSERT INTO approvals VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                (
                    approval_id,
                    run_id,
                    session_id,
                    now(),
                    response_id,
                    call_id,
                    tool_name,
                    json.dumps(arguments),
                ),
            )
            self._conn.execute(
                "UPDATE runs SET status = 'awaiting_approval' WHERE id = ?", (run_id,)
            )
            self._conn.commit()
        self.event(
            run_id,
            "approval_requested",
            {"approval_id": approval_id, "tool": tool_name, "arguments": arguments},
        )
        return approval_id

    def get_approval(self, approval_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if not row:
            return None
        result = dict(row)
        result["arguments"] = json.loads(result["arguments"])
        return result

    def resolve_approval(self, approval_id: str, approved: bool) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE approvals SET status = ? WHERE id = ? AND status = 'pending'",
                ("approved" if approved else "denied", approval_id),
            )
            self._conn.commit()

    def resume_run(self, run_id: str) -> None:
        with self._lock:
            self._conn.execute("UPDATE runs SET status = 'running' WHERE id = ?", (run_id,))
            self._conn.commit()

    def feedback(self, run_id: str, rating: int, correction: str | None) -> None:
        if rating not in {-1, 1}:
            raise ValueError("rating must be -1 or 1")
        with self._lock:
            self._conn.execute(
                "UPDATE runs SET rating = ?, correction = ? WHERE id = ?",
                (rating, correction, run_id),
            )
            self._conn.commit()
        self.event(run_id, "human_feedback", {"rating": rating, "correction": correction})

    def exportable_runs(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE status = 'completed' AND (rating = 1 OR correction IS NOT NULL) ORDER BY created_at"
        ).fetchall()
        return [dict(row) for row in rows]

    def events_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM events WHERE run_id = ? ORDER BY id", (run_id,)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def task_for_run(self, run_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT id FROM tasks WHERE run_id = ? ORDER BY updated_at DESC LIMIT 1", (run_id,)
        ).fetchone()
        return self.get_task(row["id"]) if row else None

    def set_score(
        self,
        run_id: str,
        grader: str,
        score: float,
        label: str,
        rationale: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO run_scores(run_id, grader, created_at, score, label, rationale, details) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(run_id, grader) DO UPDATE SET "
                "created_at = excluded.created_at, score = excluded.score, label = excluded.label, "
                "rationale = excluded.rationale, details = excluded.details",
                (
                    run_id,
                    grader,
                    now(),
                    float(score),
                    label,
                    rationale,
                    json.dumps(redact(details or {}), ensure_ascii=False, default=str),
                ),
            )
            self._conn.commit()

    def scores_for_run(self, run_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT grader, created_at, score, label, rationale, details FROM run_scores WHERE run_id = ? ORDER BY grader",
            (run_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def grade_run(self, run_id: str) -> None:
        run = self._conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not run:
            return
        events = self.events_for_run(run_id)
        kinds = [event["kind"] for event in events]
        completed = run["status"] == "completed"
        self.set_score(
            run_id,
            "task_completion.v1",
            1.0 if completed else 0.0,
            "pass" if completed else "fail",
            "The run completed with a final response."
            if completed
            else f"The run ended with status {run['status']}.",
            {"status": run["status"], "error": run["error"]},
        )
        has_user = "user_message" in kinds
        has_assistant = "assistant_message" in kinds
        trace_score = (int(has_user) + int(has_assistant)) / 2
        self.set_score(
            run_id,
            "trace_completeness.v1",
            trace_score,
            "pass" if trace_score == 1 else "incomplete",
            "Observable user and assistant messages are present."
            if trace_score == 1
            else "The trace is missing an observable endpoint.",
            {"user_message": has_user, "assistant_message": has_assistant},
        )
        outputs: list[Any] = []
        for event in events:
            payload = event["payload"]
            if event["kind"] == "tool_result":
                outputs.append(payload.get("output"))
            if event["kind"] in {"sdk_trace", "realtime_metadata"}:
                outputs.extend(
                    item.get("output")
                    for item in payload.get("items", [])
                    if isinstance(item, dict) and item.get("type") == "function_call_output"
                )
        if outputs:
            failures = 0
            for output in outputs:
                parsed = output
                if isinstance(output, str):
                    try:
                        parsed = json.loads(output)
                    except json.JSONDecodeError:
                        parsed = output
                if isinstance(parsed, dict) and (parsed.get("ok") is False or parsed.get("error")):
                    failures += 1
            score = (len(outputs) - failures) / len(outputs)
            self.set_score(
                run_id,
                "tool_execution.v1",
                score,
                "pass" if failures == 0 else "partial",
                "All recorded tool outputs succeeded."
                if failures == 0
                else f"{failures} of {len(outputs)} tool outputs reported an error.",
                {"tool_outputs": len(outputs), "failures": failures},
            )

    # Durable agent tasks are intentionally separate from model runs. A task can
    # have more than one run after a safe restart recovery or retry.
    def create_task(self, session_id: str, request: str) -> str:
        task_id = uuid.uuid4().hex
        timestamp = now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO tasks(id, session_id, created_at, updated_at, status, request) "
                "VALUES (?, ?, ?, ?, 'queued', ?)",
                (task_id, session_id, timestamp, timestamp, request),
            )
            self._conn.commit()
        self.task_event(task_id, "queued", {"request": request})
        return task_id

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        if item.get("result"):
            with suppress(json.JSONDecodeError):
                item["result"] = json.loads(item["result"])
        item["auto_retry"] = bool(item["auto_retry"])
        item["cancel_requested"] = bool(item["cancel_requested"])
        return item

    def list_tasks(self, limit: int = 20, active_only: bool = False) -> list[dict[str, Any]]:
        where = (
            "WHERE status IN ('queued', 'running', 'awaiting_approval', 'interrupted')"
            if active_only
            else ""
        )
        rows = self._conn.execute(
            f"SELECT * FROM tasks {where} ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [self.get_task(row["id"]) for row in rows if row]

    def start_task(
        self, task_id: str, run_id: str, phase: str = "reasoning", auto_retry: bool = True
    ) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET status = 'running', updated_at = ?, run_id = ?, approval_id = NULL, "
                "active_phase = ?, auto_retry = ?, cancel_requested = 0, error = NULL WHERE id = ?",
                (now(), run_id, phase, int(auto_retry), task_id),
            )
            self._conn.commit()
        self.task_event(
            task_id, "running", {"run_id": run_id, "phase": phase, "auto_retry": auto_retry}
        )

    def set_task_phase(self, task_id: str, phase: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET active_phase = ?, updated_at = ? WHERE id = ?",
                (phase, now(), task_id),
            )
            self._conn.commit()
        self.task_event(task_id, "phase", {"phase": phase})

    def set_task_awaiting_approval(self, task_id: str, approval_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET status = 'awaiting_approval', updated_at = ?, approval_id = ?, "
                "active_phase = 'approval', auto_retry = 0 WHERE id = ?",
                (now(), approval_id, task_id),
            )
            self._conn.commit()
        self.task_event(task_id, "awaiting_approval", {"approval_id": approval_id})

    def finish_task(self, task_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET status = 'completed', updated_at = ?, result = ?, error = NULL, "
                "approval_id = NULL, active_phase = 'completed', auto_retry = 0 WHERE id = ?",
                (now(), json.dumps(redact(result), ensure_ascii=False, default=str), task_id),
            )
            self._conn.commit()
        self.task_event(task_id, "completed", result)

    def fail_task(self, task_id: str, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET status = 'failed', updated_at = ?, error = ?, "
                "active_phase = 'failed', auto_retry = 0 WHERE id = ?",
                (now(), error, task_id),
            )
            self._conn.commit()
        self.task_event(task_id, "failed", {"error": error})

    def cancel_task(self, task_id: str, reason: str = "Cancelled by the user") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET status = 'cancelled', updated_at = ?, error = ?, cancel_requested = 1, "
                "active_phase = 'cancelled', auto_retry = 0 WHERE id = ?",
                (now(), reason, task_id),
            )
            self._conn.commit()
        self.task_event(task_id, "cancelled", {"reason": reason})

    def request_task_cancel(self, task_id: str) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE tasks SET cancel_requested = 1, updated_at = ? "
                "WHERE id = ? AND status IN ('queued', 'running', 'awaiting_approval', 'interrupted')",
                (now(), task_id),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def latest_active_task(self) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT id FROM tasks WHERE status IN ('queued', 'running', 'awaiting_approval', 'interrupted') "
            "ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        return self.get_task(row["id"]) if row else None

    def task_event(self, task_id: str, kind: str, payload: Any) -> None:
        clean = redact(payload)
        with self._lock:
            self._conn.execute(
                "INSERT INTO task_events(task_id, created_at, kind, payload) VALUES (?, ?, ?, ?)",
                (task_id, now(), kind, json.dumps(clean, ensure_ascii=False, default=str)),
            )
            self._conn.commit()

    def task_events(self, task_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM task_events WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"])
            result.append(item)
        return result

    def create_task_approval(
        self,
        task_id: str,
        run_id: str,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        call_id: str,
        checkpoint: dict[str, Any],
    ) -> str:
        approval_id = uuid.uuid4().hex
        timestamp = now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO task_approvals VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
                (
                    approval_id,
                    task_id,
                    run_id,
                    session_id,
                    timestamp,
                    timestamp,
                    tool_name,
                    json.dumps(redact(arguments), ensure_ascii=False, default=str),
                    call_id,
                    json.dumps(checkpoint, ensure_ascii=False, default=str),
                ),
            )
            self._conn.commit()
        return approval_id

    def get_task_approval(self, approval_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM task_approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["arguments"] = json.loads(item["arguments"])
        item["checkpoint"] = json.loads(item["checkpoint"])
        return item

    def resolve_task_approval(self, approval_id: str, approved: bool) -> bool:
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE task_approvals SET status = ?, updated_at = ? WHERE id = ? AND status = 'pending'",
                ("approved" if approved else "denied", now(), approval_id),
            )
            self._conn.commit()
        return cursor.rowcount > 0

    def prepare_task_recovery(self) -> list[dict[str, Any]]:
        """Recover only work that has not crossed a side-effect approval boundary."""
        timestamp = now()
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET status = 'queued', active_phase = 'recovered', updated_at = ? "
                "WHERE status = 'running' AND auto_retry = 1",
                (timestamp,),
            )
            self._conn.execute(
                "UPDATE tasks SET status = 'interrupted', active_phase = 'needs_review', updated_at = ?, "
                "error = 'Jarvis restarted after an approved side effect; review before retrying.' "
                "WHERE status = 'running' AND auto_retry = 0",
                (timestamp,),
            )
            self._conn.commit()
        rows = self._conn.execute(
            "SELECT id FROM tasks WHERE status = 'queued' ORDER BY created_at"
        ).fetchall()
        return [self.get_task(row["id"]) for row in rows if row]

    def quality_metrics(self) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed, "
            "SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed, "
            "SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled, "
            "SUM(CASE WHEN rating = 1 THEN 1 ELSE 0 END) AS positive, "
            "SUM(CASE WHEN rating = -1 THEN 1 ELSE 0 END) AS negative, "
            "SUM(CASE WHEN correction IS NOT NULL THEN 1 ELSE 0 END) AS corrected "
            "FROM runs"
        ).fetchone()
        # sqlite3.Row iterates over values, unlike a dict; keys() is intentional.
        counts = {key: int(row[key] or 0) for key in row.keys()}  # noqa: SIM118
        rated = counts["positive"] + counts["negative"]
        total = counts["total"]
        completed = counts["completed"]
        counts.update(
            {
                "rated": rated,
                "positive_rate": counts["positive"] / rated if rated else None,
                "failure_rate": counts["failed"] / total if total else 0.0,
                "completion_rate": completed / total if total else 0.0,
                "training_eligible": len(self.exportable_runs()),
                "harness_versions": len(self.distinct_harness_contexts()),
            }
        )
        grader_rows = self._conn.execute(
            "SELECT grader, AVG(score) AS average, COUNT(*) AS samples FROM run_scores GROUP BY grader"
        ).fetchall()
        counts["graders"] = {
            row["grader"]: {"average": float(row["average"]), "samples": int(row["samples"])}
            for row in grader_rows
        }
        return counts

    def create_eval_snapshot(self, metrics: dict[str, Any], passed: bool, notes: str = "") -> str:
        snapshot_id = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                "INSERT INTO eval_snapshots(id, created_at, metrics, passed, notes) VALUES (?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    now(),
                    json.dumps(redact(metrics), ensure_ascii=False, default=str),
                    int(passed),
                    notes,
                ),
            )
            self._conn.commit()
        return snapshot_id

    def latest_eval_snapshot(self) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM eval_snapshots ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        item = dict(row)
        item["metrics"] = json.loads(item["metrics"])
        item["passed"] = bool(item["passed"])
        return item

    def register_model(
        self,
        name: str,
        artifact_path: str,
        base_model: str,
        metrics: dict[str, Any],
        status: str = "candidate",
    ) -> None:
        timestamp = now()
        with self._lock:
            self._conn.execute(
                "INSERT INTO model_registry(name, created_at, updated_at, artifact_path, base_model, metrics, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(name) DO UPDATE SET "
                "updated_at = excluded.updated_at, artifact_path = excluded.artifact_path, "
                "base_model = excluded.base_model, metrics = excluded.metrics, status = excluded.status",
                (
                    name,
                    timestamp,
                    timestamp,
                    artifact_path,
                    base_model,
                    json.dumps(redact(metrics), ensure_ascii=False, default=str),
                    status,
                ),
            )
            self._conn.commit()

    def promote_model(self, name: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT name FROM model_registry WHERE name = ? AND status = 'candidate'", (name,)
            ).fetchone()
            if not row:
                return False
            self._conn.execute(
                "UPDATE model_registry SET status = 'retired', updated_at = ? WHERE status = 'active'",
                (now(),),
            )
            self._conn.execute(
                "UPDATE model_registry SET status = 'active', updated_at = ? WHERE name = ?",
                (now(), name),
            )
            self._conn.commit()
        return True
