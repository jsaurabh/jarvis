from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from .config import settings
from .core import JarvisCore
from .observability import PhoenixServer
from .store import TraceStore

SESSION_FILE = Path(os.environ.get("JARVIS_SESSION_FILE", "data/native-session-id"))


def native_session(store: TraceStore) -> str:
    if SESSION_FILE.exists():
        session_id = SESSION_FILE.read_text(encoding="utf-8").strip()
        if store.get_session(session_id):
            return session_id
    session_id = store.create_session()
    SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_FILE.write_text(session_id, encoding="utf-8")
    return session_id


class JsonLineDaemon:
    """Small private JSON-RPC transport over the native app's process pipes."""

    def __init__(self) -> None:
        self.store = TraceStore(settings.jarvis_db_path, settings.jarvis_trace_mode)
        self.session_id = native_session(self.store)
        self._write_lock = asyncio.Lock()
        self.phoenix = PhoenixServer(
            settings.jarvis_db_path.parent / "phoenix.db",
            settings.jarvis_phoenix_port,
        )
        if (
            settings.jarvis_trace_mode != "off"
            and os.environ.get("JARVIS_PHOENIX_MANAGED_EXTERNALLY") != "1"
        ):
            self.phoenix.start()
        self.core = JarvisCore(settings, store=self.store, event_sink=self.emit)
        self._requests: set[asyncio.Task[None]] = set()

    async def send(self, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        async with self._write_lock:
            await asyncio.to_thread(sys.stdout.write, data)
            await asyncio.to_thread(sys.stdout.flush)

    async def emit(self, event: dict[str, Any]) -> None:
        await self.send({"type": "event", "event": event})

    async def handle(self, request: dict[str, Any]) -> None:
        request_id = str(request.get("id", ""))
        action = request.get("action")
        payload = request.get("payload") or {}
        try:
            if action == "chat":
                result = await self.core.submit(self.session_id, str(payload["text"]))
            elif action == "approve":
                grant = payload.get("broker_grant")
                if os.environ.get("JARVIS_CONFINED_AGENT") == "1" and payload.get("approved"):
                    if not isinstance(grant, dict):
                        raise ValueError("A supervisor approval grant is required")
                    broker = self.core.agent.executor.broker
                    broker.add_grant(grant["operation"], grant["arguments"], grant["token"])
                result = await self.core.resolve_approval(
                    str(payload["approval_id"]), bool(payload["approved"])
                )
            elif action == "feedback":
                self.store.feedback(
                    str(payload["run_id"]), int(payload["rating"]), payload.get("correction")
                )
                result = {
                    "status": "completed",
                    "message": "Feedback saved.",
                    "run_id": str(payload["run_id"]),
                }
            elif action == "trace-turn":
                run_id = self.store.record_completed_turn(
                    self.session_id,
                    str(payload["user_text"]),
                    str(payload["assistant_text"]),
                    str(payload["model"]),
                    payload.get("metadata") or {},
                )
                result = {"status": "completed", "message": "Voice turn saved.", "run_id": run_id}
            elif action == "status":
                result = self.core.status(payload.get("task_id"))
            elif action == "tasks":
                result = self.core.list_tasks(int(payload.get("limit", 10)))
            elif action == "cancel":
                result = await self.core.cancel(payload.get("task_id"))
            elif action == "memory-remember":
                result = self.core.remember(
                    str(payload["content"]),
                    str(payload.get("kind", "fact")),
                    str(payload.get("scope", "global")),
                )
            elif action == "memory-recall":
                result = self.core.recall(
                    str(payload.get("query", "")),
                    int(payload.get("limit", 8)),
                    str(payload.get("scope", "global")),
                )
            elif action == "memory-list":
                result = self.core.list_memories(int(payload.get("limit", 50)))
            elif action == "memory-forget":
                result = self.core.forget(str(payload["memory_id"]))
            elif action == "reminder-create":
                repeat = payload.get("repeat_seconds")
                result = self.core.schedule_reminder(
                    str(payload["message"]),
                    str(payload["due_at"]),
                    int(repeat) if repeat is not None else None,
                )
            elif action == "reminders":
                result = self.core.list_reminders(int(payload.get("limit", 50)))
            elif action == "reminder-cancel":
                result = self.core.cancel_reminder(str(payload["reminder_id"]))
            elif action == "automation-create":
                result = self.core.create_automation(
                    self.session_id,
                    str(payload["name"]),
                    str(payload["prompt"]),
                    str(payload["schedule_type"]),
                    payload.get("run_at"),
                    int(payload["interval_seconds"])
                    if payload.get("interval_seconds") is not None
                    else None,
                    payload.get("local_time"),
                    payload.get("timezone"),
                )
            elif action == "automations":
                result = self.core.list_automations(int(payload.get("limit", 50)))
            elif action == "automation-manage":
                result = self.core.manage_automation(
                    str(payload["automation_id"]), str(payload["operation"])
                )
            elif action == "skills":
                result = self.core.list_skills()
            elif action == "quality":
                result = self.core.quality_report()
            elif action == "trace-viewer":
                if settings.jarvis_trace_mode == "off":
                    raise ValueError("Local tracing is disabled")
                await asyncio.to_thread(self.phoenix.start)
                result = {
                    "status": "completed",
                    "message": "Local Phoenix trace viewer is ready.",
                    "url": self.phoenix.url,
                }
            elif action == "filesystem-scope":
                roots = payload.get("roots")
                if not isinstance(roots, list) or not all(isinstance(root, str) for root in roots):
                    raise ValueError("Filesystem roots must be a list of folder paths")
                configured = settings.set_allowed_roots(roots)
                result = {
                    "status": "completed",
                    "message": "Filesystem access updated.",
                    "roots": [str(root) for root in configured],
                }
            elif action == "sandbox-selftest":
                from .sandbox_check import verify_sandbox

                provider = getattr(
                    getattr(self.core.agent, "observability", None), "provider", None
                )
                result = await verify_sandbox(settings, provider)
            elif action == "ping":
                result = {"status": "completed", "message": "Jarvis Core is ready."}
            elif action == "shutdown":
                result = {"status": "completed", "message": "Jarvis Core is stopping."}
                await self.send({"id": request_id, "type": "response", "result": result})
                raise SystemExit
            else:
                raise ValueError(f"Unknown daemon action: {action}")
        except asyncio.CancelledError:
            result = {"status": "cancelled", "message": "Request cancelled."}
        except Exception as exc:
            logging.exception("Jarvis daemon request failed")
            result = {"status": "error", "message": str(exc)}
        await self.send({"id": request_id, "type": "response", "result": result})

    async def serve(self) -> None:
        try:
            await self._serve()
        finally:
            self.phoenix.stop()

    async def _serve(self) -> None:
        await self.core.start()
        await self.emit({"status": "ready", "message": "Jarvis Core is ready."})
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise ValueError("Request must be a JSON object")
            except Exception as exc:
                await self.send(
                    {
                        "id": "",
                        "type": "response",
                        "result": {"status": "error", "message": str(exc)},
                    }
                )
                continue
            task = asyncio.create_task(self.handle(request))
            self._requests.add(task)
            task.add_done_callback(self._requests.discard)

        for task in list(self._requests):
            task.cancel()
        if self._requests:
            await asyncio.gather(*self._requests, return_exceptions=True)
        await self.core.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    asyncio.run(JsonLineDaemon().serve())


if __name__ == "__main__":
    main()
