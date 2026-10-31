from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from .automations import AutomationStore
from .config import Settings
from .learning import harness_snapshot as build_harness_snapshot
from .memory import MemoryStore
from .reminders import ReminderStore
from .sdk_agent import SDKJarvisAgent, SDKTurn
from .skills import SkillStore
from .store import TraceStore
from .tools import ToolExecutor

EventSink = Callable[[dict[str, Any]], Awaitable[None]]


class JarvisCore:
    """Persistent task runtime above the model-specific agent loop."""

    def __init__(
        self,
        settings: Settings,
        store: TraceStore | None = None,
        agent: SDKJarvisAgent | None = None,
        event_sink: EventSink | None = None,
        memory: MemoryStore | None = None,
        reminders: ReminderStore | None = None,
        automations: AutomationStore | None = None,
        skills: SkillStore | None = None,
    ) -> None:
        self.settings = settings
        self.store = store or TraceStore(settings.jarvis_db_path, settings.jarvis_trace_mode)
        self.memory = memory or MemoryStore(settings.jarvis_db_path)
        self.reminders = reminders or ReminderStore(settings.jarvis_db_path)
        self.automations = automations or AutomationStore(settings.jarvis_db_path)
        self.skills = skills or SkillStore(settings.jarvis_skills_path)
        self.agent = agent or SDKJarvisAgent(
            settings,
            ToolExecutor(settings),
            reminders=self.reminders,
            skills=self.skills,
        )
        self.event_sink = event_sink
        self._active: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._reminder_task: asyncio.Task[None] | None = None
        self._automation_task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        for task in self.store.prepare_task_recovery():
            if task:
                self._spawn(task["id"], recovered=True)
        if not self._reminder_task or self._reminder_task.done():
            self._reminder_task = asyncio.create_task(
                self._reminder_loop(), name="jarvis-reminder-loop"
            )
        if not self._automation_task or self._automation_task.done():
            self._automation_task = asyncio.create_task(
                self._automation_loop(), name="jarvis-automation-loop"
            )

    async def close(self) -> None:
        if self._reminder_task and not self._reminder_task.done():
            self._reminder_task.cancel()
            await asyncio.gather(self._reminder_task, return_exceptions=True)
        self._reminder_task = None
        if self._automation_task and not self._automation_task.done():
            self._automation_task.cancel()
            await asyncio.gather(self._automation_task, return_exceptions=True)
        self._automation_task = None

    def remember(self, content: str, kind: str = "fact", scope: str = "global") -> dict[str, Any]:
        memory = self.memory.remember(content, kind=kind, scope=scope)
        return {
            "status": "completed",
            "message": "I’ll remember that.",
            "memory": memory,
        }

    def recall(self, query: str, limit: int = 8, scope: str = "global") -> dict[str, Any]:
        memories = self.memory.recall(query, limit=limit, scope=scope)
        return {
            "status": "completed",
            "message": f"Found {len(memories)} relevant memories.",
            "memories": memories,
        }

    def list_memories(self, limit: int = 50) -> dict[str, Any]:
        memories = self.memory.list(limit=limit)
        return {
            "status": "completed",
            "message": f"Jarvis has {len(memories)} saved memories in this view.",
            "memories": memories,
        }

    def forget(self, memory_id: str) -> dict[str, Any]:
        forgotten = self.memory.forget(memory_id)
        return {
            "status": "completed" if forgotten else "not_found",
            "message": "Memory forgotten." if forgotten else "That memory was not found.",
            "memory_id": memory_id,
        }

    def schedule_reminder(
        self,
        message: str,
        due_at: str,
        repeat_seconds: int | None = None,
    ) -> dict[str, Any]:
        reminder = self.reminders.create(
            message=message,
            due_at=due_at,
            repeat_seconds=repeat_seconds,
        )
        return {
            "status": "completed",
            "message": f"Reminder scheduled for {reminder['due_at']}.",
            "reminder": reminder,
        }

    def list_reminders(self, limit: int = 50) -> dict[str, Any]:
        reminders = self.reminders.list(limit=limit)
        return {
            "status": "completed",
            "message": f"Found {len(reminders)} active reminders.",
            "reminders": reminders,
        }

    def cancel_reminder(self, reminder_id: str) -> dict[str, Any]:
        cancelled = self.reminders.cancel(reminder_id)
        return {
            "status": "completed" if cancelled else "not_found",
            "message": "Reminder cancelled." if cancelled else "That reminder was not found.",
            "reminder_id": reminder_id,
        }

    def create_automation(
        self,
        session_id: str,
        name: str,
        prompt: str,
        schedule_type: str,
        run_at: str | None = None,
        interval_seconds: int | None = None,
        local_time: str | None = None,
        timezone: str | None = None,
    ) -> dict[str, Any]:
        if not self.store.get_session(session_id):
            raise ValueError("Unknown session")
        if schedule_type == "once" and run_at:
            automation = self.automations.create_once(session_id, name, prompt, run_at)
        elif schedule_type == "interval" and interval_seconds is not None:
            automation = self.automations.create_interval(
                session_id, name, prompt, interval_seconds
            )
        elif schedule_type == "daily" and local_time and timezone:
            automation = self.automations.create_daily(
                session_id, name, prompt, local_time, timezone
            )
        else:
            raise ValueError("Incomplete automation schedule")
        return {
            "status": "completed",
            "message": f"Automation '{automation['name']}' scheduled.",
            "automation": automation,
        }

    def list_automations(self, limit: int = 50) -> dict[str, Any]:
        automations = self.automations.list(limit=limit)
        return {
            "status": "completed",
            "message": f"Found {len(automations)} active automations.",
            "automations": automations,
        }

    def manage_automation(self, automation_id: str, operation: str) -> dict[str, Any]:
        handlers = {
            "pause": self.automations.pause,
            "resume": self.automations.resume,
            "cancel": self.automations.cancel,
        }
        if operation not in handlers:
            raise ValueError("Automation operation must be pause, resume, or cancel")
        changed = handlers[operation](automation_id)
        return {
            "status": "completed" if changed else "not_found",
            "message": f"Automation {operation}d."
            if changed
            else "Automation was not found or could not be changed.",
            "automation_id": automation_id,
        }

    def list_skills(self) -> dict[str, Any]:
        skills = self.skills.list()
        return {
            "status": "completed",
            "message": f"Found {len(skills)} installed skills.",
            "skills": skills,
        }

    def quality_report(self) -> dict[str, Any]:
        metrics = self.store.quality_metrics()
        return {
            "status": "completed",
            "message": "Jarvis quality metrics are ready.",
            "metrics": metrics,
        }

    async def submit(self, session_id: str, text: str) -> dict[str, Any]:
        if not self.store.get_session(session_id):
            raise ValueError("Unknown session")
        task_id = self.store.create_task(session_id, text)
        return await self._spawn(task_id)

    async def resolve_approval(self, approval_id: str, approved: bool) -> dict[str, Any]:
        approval = self.store.get_task_approval(approval_id)
        if not approval or approval["status"] != "pending":
            raise ValueError("Approval does not exist or is no longer pending")
        if not self.store.resolve_task_approval(approval_id, approved):
            raise ValueError("Approval was already resolved")

        task_id = approval["task_id"]
        run_id = approval["run_id"]
        self.store.start_task(task_id, run_id, phase="resuming", auto_retry=False)
        self.store.resume_run(run_id)
        self.store.event(
            run_id,
            "approval_granted" if approved else "approval_denied",
            {"approval_id": approval_id, "tool": approval["tool_name"]},
        )
        await self._emit(task_id, "running", "Resuming after approval")

        async def resume() -> dict[str, Any]:
            try:
                turn = await self.agent.resume(
                    approval["session_id"], approval["checkpoint"], approved
                )
                return await self._apply_turn(task_id, run_id, approval["session_id"], turn)
            except asyncio.CancelledError:
                self.store.cancel_run(run_id)
                self.store.cancel_task(task_id)
                await self._emit(task_id, "cancelled", "Task cancelled")
                self.automations.fail_for_task(task_id, "Task cancelled")
                return self._result(task_id, "cancelled", "Task cancelled.", run_id)
            except Exception as exc:
                self.store.fail_run(run_id, str(exc))
                self.store.fail_task(task_id, str(exc))
                await self._emit(task_id, "failed", str(exc))
                self.automations.fail_for_task(task_id, str(exc))
                raise

        return await self._track(task_id, resume())

    async def cancel(self, task_id: str | None = None) -> dict[str, Any]:
        task = self.store.get_task(task_id) if task_id else self.store.latest_active_task()
        if not task:
            return {"status": "completed", "message": "There is no active task to cancel."}
        task_id = task["id"]
        if not self.store.request_task_cancel(task_id):
            return self._result(
                task_id, task["status"], "That task is no longer active.", task.get("run_id")
            )
        active = self._active.get(task_id)
        if active and not active.done():
            active.cancel()
        else:
            if task.get("run_id"):
                self.store.cancel_run(task["run_id"])
            self.store.cancel_task(task_id)
            await self._emit(task_id, "cancelled", "Task cancelled")
            self.automations.fail_for_task(task_id, "Task cancelled")
        return self._result(task_id, "cancelled", "Task cancelled.", task.get("run_id"))

    def status(self, task_id: str | None = None) -> dict[str, Any]:
        task = self.store.get_task(task_id) if task_id else self.store.latest_active_task()
        if not task:
            return {"status": "idle", "message": "Jarvis has no active tasks.", "tasks": []}
        message = f"Task is {task['status']}: {task['request']}"
        if task.get("active_phase"):
            message += f" ({task['active_phase']})"
        return {
            "status": task["status"],
            "message": message,
            "task_id": task["id"],
            "run_id": task.get("run_id"),
            "approval_id": task.get("approval_id"),
        }

    def list_tasks(self, limit: int = 10) -> dict[str, Any]:
        tasks = self.store.list_tasks(limit=max(1, min(limit, 50)))
        return {"status": "completed", "message": f"Found {len(tasks)} tasks.", "tasks": tasks}

    async def _reminder_loop(self) -> None:
        try:
            while True:
                for reminder in self.reminders.claim_due():
                    await self._emit_reminder(reminder)
                    self.reminders.mark_fired(reminder["id"])
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            raise

    async def _automation_loop(self) -> None:
        try:
            while True:
                for automation in self.automations.claim_due():
                    asyncio.create_task(
                        self._launch_automation(automation),
                        name=f"jarvis-automation-{automation['id']}",
                    )
                await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise

    async def _launch_automation(self, automation: dict[str, Any]) -> None:
        task_id = self.store.create_task(automation["session_id"], automation["prompt"])
        self.automations.attach_task(automation["id"], task_id)
        await self._emit_event(
            {
                "status": "automation_running",
                "message": f"Running automation: {automation['name']}",
                "automation_id": automation["id"],
                "task_id": task_id,
            }
        )
        try:
            await self._spawn(task_id)
        except Exception:
            # _execute records and emits the concrete failure.
            return

    async def _emit_reminder(self, reminder: dict[str, Any]) -> None:
        await self._emit_event(
            {
                "status": "reminder",
                "message": reminder["message"],
                "reminder_id": reminder["id"],
                "due_at": reminder["due_at"],
            }
        )

    async def _emit_event(self, event: dict[str, Any]) -> None:
        if self.event_sink:
            await self.event_sink(event)

    def _spawn(self, task_id: str, recovered: bool = False) -> asyncio.Task[dict[str, Any]]:
        existing = self._active.get(task_id)
        if existing and not existing.done():
            return existing
        task = asyncio.create_task(self._execute(task_id, recovered), name=f"jarvis-task-{task_id}")
        self._active[task_id] = task
        task.add_done_callback(lambda _: self._active.pop(task_id, None))
        return task

    async def _track(self, task_id: str, awaitable: Awaitable[dict[str, Any]]) -> dict[str, Any]:
        task = asyncio.create_task(awaitable, name=f"jarvis-resume-{task_id}")
        self._active[task_id] = task
        try:
            return await task
        finally:
            self._active.pop(task_id, None)

    async def _execute(self, task_id: str, recovered: bool) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if not task:
            raise ValueError("Unknown task")
        run_id = self.store.create_run(
            task["session_id"], task["request"], self.settings.jarvis_model
        )
        interface = "scheduled_automation" if self.automations.for_task(task_id) else "agent_task"
        snapshot = getattr(self.agent, "harness_snapshot", None)
        if callable(snapshot):
            context = snapshot(interface=interface, task_id=task_id)
        else:
            # Test doubles and third-party agent adapters can still participate
            # in the trace format before they implement richer introspection.
            context = build_harness_snapshot(
                interface=interface,
                model=self.settings.jarvis_model,
                instructions="",
                tools=[],
                model_settings={},
                version="injected-agent",
                task_id=task_id,
            )
        self.store.set_run_context(run_id, context)
        self.store.start_task(task_id, run_id, phase="reasoning", auto_retry=True)
        if recovered:
            self.store.event(run_id, "restart_recovery", {"task_id": task_id})
        await self._emit(task_id, "running", "Working")
        try:
            turn = await self.agent.chat(task["session_id"], task["request"])
            return await self._apply_turn(task_id, run_id, task["session_id"], turn)
        except asyncio.CancelledError:
            self.store.cancel_run(run_id)
            self.store.cancel_task(task_id)
            await self._emit(task_id, "cancelled", "Task cancelled")
            self.automations.fail_for_task(task_id, "Task cancelled")
            return self._result(task_id, "cancelled", "Task cancelled.", run_id)
        except Exception as exc:
            self.store.fail_run(run_id, str(exc))
            self.store.fail_task(task_id, str(exc))
            await self._emit(task_id, "failed", str(exc))
            automation = self.automations.fail_for_task(task_id, str(exc))
            if automation:
                await self._emit_event(
                    {
                        "status": "automation_failed",
                        "message": f"Automation '{automation['name']}' failed: {exc}",
                        "automation_id": automation["id"],
                    }
                )
            raise

    async def _apply_turn(
        self, task_id: str, run_id: str, session_id: str, turn: SDKTurn
    ) -> dict[str, Any]:
        self.store.event(run_id, "sdk_trace", turn.trace)
        if turn.status == "approval_required":
            if not turn.checkpoint or not turn.tool_name:
                raise RuntimeError("Agent requested approval without resumable state")
            approval_id = self.store.create_task_approval(
                task_id,
                run_id,
                session_id,
                turn.tool_name,
                turn.arguments,
                turn.call_id or "",
                turn.checkpoint,
            )
            self.store.mark_run_awaiting_approval(run_id)
            self.store.set_task_awaiting_approval(task_id, approval_id)
            self.automations.awaiting_approval(task_id)
            description = self._approval_description(turn.tool_name, turn.arguments)
            self.store.event(
                run_id,
                "approval_requested",
                {"approval_id": approval_id, "tool": turn.tool_name, "arguments": turn.arguments},
            )
            await self._emit(task_id, "awaiting_approval", description)
            await self._emit_event(
                {
                    "task_id": task_id,
                    "status": "awaiting_approval",
                    "message": description,
                    "approval_id": approval_id,
                }
            )
            result = self._result(task_id, "approval_required", description, run_id)
            result["approval"] = {
                "id": approval_id,
                "tool": turn.tool_name,
                "description": description,
                "arguments": turn.arguments,
            }
            return result

        self.store.finish_run(run_id, turn.message)
        result = self._result(task_id, "completed", turn.message, run_id)
        self.store.finish_task(task_id, result)
        automation = self.automations.complete_for_task(task_id, turn.message)
        await self._emit(task_id, "completed", turn.message)
        if automation:
            await self._emit_event(
                {
                    "status": "automation_completed",
                    "message": turn.message,
                    "automation_id": automation["id"],
                    "automation_name": automation["name"],
                    "task_id": task_id,
                }
            )
        return result

    async def _emit(self, task_id: str, status: str, message: str) -> None:
        if self.event_sink:
            await self.event_sink({"task_id": task_id, "status": status, "message": message})

    @staticmethod
    def _result(task_id: str, status: str, message: str, run_id: str | None) -> dict[str, Any]:
        result: dict[str, Any] = {"status": status, "message": message, "task_id": task_id}
        if run_id:
            result["run_id"] = run_id
        return result

    @staticmethod
    def _approval_description(tool_name: str, arguments: dict[str, Any]) -> str:
        if tool_name == "write_file":
            return f"Write {len(str(arguments.get('content', '')))} characters to {arguments.get('path')}"
        if tool_name == "run_command":
            return "Run: " + " ".join(str(item) for item in arguments.get("argv", []))
        if tool_name == "read_clipboard":
            return "Read clipboard text and send it to the configured model"
        if tool_name == "list_calendar_events":
            return f"Read the next {arguments.get('days', 1)} day(s) of Calendar events"
        if tool_name == "type_text":
            return f"Type {len(str(arguments.get('text', '')))} characters into the frontmost app"
        if tool_name == "open_url":
            return f"Open {arguments.get('url')} in the default browser"
        if tool_name == "open_application":
            return f"Open the application {arguments.get('name')}"
        if tool_name == "click_screen":
            return f"Click screen coordinate ({arguments.get('x')}, {arguments.get('y')})"
        if tool_name == "create_calendar_event":
            return f"Create Calendar event: {arguments.get('title')}"
        if tool_name in {"save_skill", "delete_skill"}:
            return f"{tool_name.replace('_', ' ').title()}: {arguments.get('name')}"
        return f"Use {tool_name}"
