from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from agents import (
    Agent,
    ModelSettings,
    RunConfig,
    Runner,
    RunState,
    SQLiteSession,
    WebSearchTool,
    add_trace_processor,
    function_tool,
    set_default_openai_api,
    set_default_openai_client,
    set_tracing_disabled,
)
from openai import AsyncOpenAI

from .agent import AGENT_INSTRUCTIONS
from .config import Settings
from .learning import harness_snapshot as build_harness_snapshot
from .reminders import ReminderStore
from .security import redact
from .skills import SkillStore
from .store import TraceStore
from .telemetry import LocalTelemetryProcessor
from .tools import ToolExecutor


@dataclass(slots=True)
class SDKTurn:
    status: str
    message: str = ""
    checkpoint: dict[str, Any] | None = None
    tool_name: str | None = None
    arguments: dict[str, Any] = field(default_factory=dict)
    call_id: str | None = None
    trace: dict[str, Any] = field(default_factory=dict)


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class SDKJarvisAgent:
    """OpenAI Agents SDK adapter owned by the persistent Jarvis daemon."""

    def __init__(
        self,
        settings: Settings,
        executor: ToolExecutor,
        reminders: ReminderStore | None = None,
        skills: SkillStore | None = None,
        store: TraceStore | None = None,
    ) -> None:
        self.settings = settings
        self.executor = executor
        self.reminders = reminders
        self.skills = skills
        api_key = settings.openai_api_key or ("local" if settings.jarvis_base_url else "")
        if not api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")
        client_args: dict[str, Any] = {"api_key": api_key}
        if settings.jarvis_base_url:
            client_args["base_url"] = settings.jarvis_base_url
        set_default_openai_client(
            AsyncOpenAI(**client_args),
            use_for_tracing=settings.jarvis_base_url is None
            and settings.jarvis_trace_mode != "off",
        )
        set_default_openai_api("responses")
        set_tracing_disabled(
            settings.jarvis_trace_mode == "off" or settings.jarvis_base_url is not None
        )
        if store is not None and settings.jarvis_trace_mode != "off":
            add_trace_processor(LocalTelemetryProcessor(store))

        tools: list[Any] = self._local_tools()
        if settings.jarvis_enable_web_search:
            tools.append(WebSearchTool())
        self._tool_specs = [self._tool_spec(tool) for tool in tools]
        self.agent: Agent[Any] = Agent(
            name="Jarvis",
            instructions=AGENT_INSTRUCTIONS,
            model=settings.jarvis_model,
            model_settings=ModelSettings(parallel_tool_calls=False, store=True),
            tools=tools,
        )

    @staticmethod
    def _tool_spec(tool: Any) -> dict[str, Any]:
        name = getattr(tool, "name", None) or getattr(tool, "type", None) or tool.__class__.__name__
        spec = {
            "type": "function"
            if hasattr(tool, "params_json_schema")
            else str(getattr(tool, "type", "hosted")),
            "name": str(name),
            "description": str(getattr(tool, "description", "")),
        }
        parameters = getattr(tool, "params_json_schema", None)
        if parameters is not None:
            spec["parameters"] = _jsonable(parameters)
        approval = getattr(tool, "needs_approval", None)
        if isinstance(approval, bool):
            spec["needs_approval"] = approval
        return spec

    def harness_snapshot(
        self, interface: str = "agent_task", task_id: str | None = None
    ) -> dict[str, Any]:
        skills = self.skills.list() if self.skills else []
        return build_harness_snapshot(
            interface=interface,
            model=self.settings.jarvis_model,
            instructions=AGENT_INSTRUCTIONS,
            tools=self._tool_specs,
            model_settings={
                "parallel_tool_calls": False,
                "store": True,
                "max_turns": self.settings.jarvis_max_tool_steps,
                "base_url": bool(self.settings.jarvis_base_url),
            },
            skills=skills,
            task_id=task_id,
        )

    def _local_tools(self) -> list[Any]:
        executor = self.executor

        @function_tool(name_override="list_directory")
        async def list_directory(path: str, limit: int) -> dict[str, Any]:
            """List files and folders in an authorized local or mounted directory."""
            return await executor.execute("list_directory", {"path": path, "limit": limit})

        @function_tool(name_override="read_file")
        async def read_file(path: str, offset: int, max_chars: int) -> dict[str, Any]:
            """Read a UTF-8 text file. Use offsets for large files."""
            return await executor.execute(
                "read_file", {"path": path, "offset": offset, "max_chars": max_chars}
            )

        @function_tool(name_override="search_files")
        async def search_files(
            root: str, query: str, search_contents: bool, limit: int
        ) -> dict[str, Any]:
            """Search file names and optionally text content below a directory."""
            return await executor.execute(
                "search_files",
                {
                    "root": root,
                    "query": query,
                    "search_contents": search_contents,
                    "limit": limit,
                },
            )

        @function_tool(name_override="write_file", needs_approval=True)
        async def write_file(path: str, content: str, create_parents: bool) -> dict[str, Any]:
            """Create or replace a UTF-8 text file after explicit human approval."""
            return await executor.execute(
                "write_file",
                {"path": path, "content": content, "create_parents": create_parents},
            )

        @function_tool(name_override="run_command", needs_approval=True)
        async def run_command(argv: list[str], cwd: str, timeout_seconds: int) -> dict[str, Any]:
            """Run a local program without a shell after explicit human approval."""
            return await executor.execute(
                "run_command",
                {"argv": argv, "cwd": cwd, "timeout_seconds": timeout_seconds},
            )

        @function_tool(name_override="get_frontmost_application")
        async def get_frontmost_application() -> dict[str, Any]:
            """Return the name of the application currently in front."""
            return await executor.execute("get_frontmost_application", {})

        @function_tool(name_override="list_running_applications")
        async def list_running_applications() -> dict[str, Any]:
            """List visible applications currently running on this Mac."""
            return await executor.execute("list_running_applications", {})

        @function_tool(name_override="read_clipboard", needs_approval=True)
        async def read_clipboard() -> dict[str, Any]:
            """Read text from the clipboard after approval because it may contain private data."""
            return await executor.execute("read_clipboard", {})

        @function_tool(name_override="set_clipboard", needs_approval=True)
        async def set_clipboard(text: str) -> dict[str, Any]:
            """Replace clipboard text after explicit approval."""
            return await executor.execute("set_clipboard", {"text": text})

        @function_tool(name_override="open_application", needs_approval=True)
        async def open_application(name: str) -> dict[str, Any]:
            """Open a named macOS application after approval."""
            return await executor.execute("open_application", {"name": name})

        @function_tool(name_override="open_url", needs_approval=True)
        async def open_url(url: str) -> dict[str, Any]:
            """Open an absolute http or https URL in the default browser after approval."""
            return await executor.execute("open_url", {"url": url})

        @function_tool(name_override="type_text", needs_approval=True)
        async def type_text(text: str) -> dict[str, Any]:
            """Type text into the frontmost application after approval."""
            return await executor.execute("type_text", {"text": text})

        @function_tool(name_override="press_key", needs_approval=True)
        async def press_key(key: str) -> dict[str, Any]:
            """Press one supported navigation key in the frontmost application after approval."""
            return await executor.execute("press_key", {"key": key})

        @function_tool(name_override="click_screen", needs_approval=True)
        async def click_screen(x: int, y: int) -> dict[str, Any]:
            """Click an absolute screen coordinate after approval."""
            return await executor.execute("click_screen", {"x": x, "y": y})

        @function_tool(name_override="list_calendar_events", needs_approval=True)
        async def list_calendar_events(days: int) -> dict[str, Any]:
            """Read upcoming Apple Calendar events after approval because calendar data is private."""
            return await executor.execute("list_calendar_events", {"days": days})

        @function_tool(name_override="create_calendar_event", needs_approval=True)
        async def create_calendar_event(
            title: str, start_at: str, end_at: str, calendar_name: str
        ) -> dict[str, Any]:
            """Create an Apple Calendar event after approval."""
            return await executor.execute(
                "create_calendar_event",
                {
                    "title": title,
                    "start_at": start_at,
                    "end_at": end_at,
                    "calendar_name": calendar_name,
                },
            )

        tools = [
            list_directory,
            read_file,
            search_files,
            get_frontmost_application,
            list_running_applications,
            read_clipboard,
            set_clipboard,
            open_application,
            open_url,
            type_text,
            press_key,
            click_screen,
            list_calendar_events,
            create_calendar_event,
        ]
        if self.settings.jarvis_enable_writes:
            tools.append(write_file)
        if self.settings.jarvis_enable_commands:
            tools.append(run_command)
        if self.reminders:
            reminders = self.reminders

            @function_tool(name_override="schedule_reminder")
            async def schedule_reminder(
                message: str, due_at: str, repeat_seconds: int | None
            ) -> dict[str, Any]:
                """Schedule a durable local reminder after an explicit user request."""
                reminder = reminders.create(message, due_at, repeat_seconds)
                return {
                    "scheduled": True,
                    "reminder_id": reminder["id"],
                    "due_at": reminder["due_at"],
                }

            @function_tool(name_override="list_reminders")
            async def list_reminders(limit: int) -> list[dict[str, Any]]:
                """List active reminders when the user asks."""
                return reminders.list(limit=limit)

            @function_tool(name_override="cancel_reminder")
            async def cancel_reminder(reminder_id: str) -> dict[str, Any]:
                """Cancel a specific active reminder when the user asks."""
                return {"cancelled": reminders.cancel(reminder_id), "reminder_id": reminder_id}

            tools.extend([schedule_reminder, list_reminders, cancel_reminder])
        if self.skills:
            skills = self.skills

            @function_tool(name_override="list_skills")
            async def list_skills() -> list[dict[str, Any]]:
                """List installed, user-inspectable Jarvis skills."""
                return skills.list()

            @function_tool(name_override="read_skill")
            async def read_skill(name: str) -> dict[str, Any]:
                """Load one skill's operating instructions before applying that skill."""
                return skills.read(name)

            @function_tool(name_override="save_skill", needs_approval=True)
            async def save_skill(name: str, content: str) -> dict[str, Any]:
                """Create or update a local Jarvis skill after approval."""
                saved = skills.save(name, content)
                return {"saved": True, "name": saved["name"]}

            @function_tool(name_override="delete_skill", needs_approval=True)
            async def delete_skill(name: str) -> dict[str, Any]:
                """Delete a local Jarvis skill after approval."""
                return {"deleted": skills.delete(name), "name": name}

            tools.extend([list_skills, read_skill, save_skill, delete_skill])
        return tools

    def _session(self, session_id: str) -> SQLiteSession:
        return SQLiteSession(
            session_id,
            db_path=self.settings.jarvis_db_path,
            sessions_table="sdk_sessions",
            messages_table="sdk_messages",
        )

    @staticmethod
    def accepts_run_context() -> bool:
        return True

    def _run_config(self, session_id: str, run_context: dict[str, Any] | None = None) -> RunConfig:
        context = run_context or {}
        metadata = {
            key: context[key]
            for key in ("run_id", "task_id", "interface", "harness_sha256", "harness_version")
            if context.get(key) is not None
        }
        return RunConfig(
            workflow_name="Jarvis agent task",
            group_id=session_id,
            trace_metadata=metadata,
            trace_include_sensitive_data=self.settings.jarvis_openai_trace_content,
        )

    async def chat(
        self,
        session_id: str,
        text: str,
        run_context: dict[str, Any] | None = None,
    ) -> SDKTurn:
        result = await Runner.run(
            self.agent,
            text,
            session=self._session(session_id),
            max_turns=self.settings.jarvis_max_tool_steps,
            run_config=self._run_config(session_id, run_context),
        )
        return self._turn_from_result(result)

    async def resume(
        self,
        session_id: str,
        checkpoint: dict[str, Any],
        approved: bool,
        run_context: dict[str, Any] | None = None,
    ) -> SDKTurn:
        state = await RunState.from_json(self.agent, checkpoint)
        interruptions = state.get_interruptions()
        if not interruptions:
            raise ValueError("The saved agent run has no pending approval")
        if approved:
            state.approve(interruptions[0])
        else:
            state.reject(interruptions[0], rejection_message="The user denied this action.")
        result = await Runner.run(
            self.agent,
            state,
            session=self._session(session_id),
            max_turns=self.settings.jarvis_max_tool_steps,
            run_config=self._run_config(session_id, run_context),
        )
        return self._turn_from_result(result)

    def _turn_from_result(self, result: Any) -> SDKTurn:
        trace = {
            "last_agent": getattr(getattr(result, "last_agent", None), "name", "Jarvis"),
            "items": redact(_jsonable(result.to_input_list())),
        }
        interruptions = list(getattr(result, "interruptions", []))
        if interruptions:
            item = interruptions[0]
            raw = _jsonable(item.raw_item)
            arguments_value = raw.get("arguments", {}) if isinstance(raw, dict) else {}
            if isinstance(arguments_value, str):
                try:
                    arguments = json.loads(arguments_value)
                except json.JSONDecodeError:
                    arguments = {"raw": arguments_value}
            else:
                arguments = arguments_value if isinstance(arguments_value, dict) else {}
            tool_name = (
                item.tool_name or (raw.get("name") if isinstance(raw, dict) else None) or "tool"
            )
            call_id = ""
            if isinstance(raw, dict):
                call_id = str(raw.get("call_id") or raw.get("id") or "")
            return SDKTurn(
                status="approval_required",
                checkpoint=result.to_state().to_json(),
                tool_name=tool_name,
                arguments=arguments,
                call_id=call_id,
                trace=trace,
            )
        final = result.final_output
        message = (
            final if isinstance(final, str) else json.dumps(_jsonable(final), ensure_ascii=False)
        )
        return SDKTurn(status="completed", message=message, trace=trace)
