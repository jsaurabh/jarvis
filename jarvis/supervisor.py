"""Trusted supervisor for the confined agent and app-reviewed operations."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import signal
import socket
import stat
import sys
from contextlib import AsyncExitStack, suppress
from pathlib import Path
from typing import Any

from .broker import ExecutionBroker
from .broker_authority import AuthorizedBroker, BrokerAuthority, PendingApprovals
from .broker_channel import serve_agent
from .config import Settings
from .execution import NonoFileExecutor, ShuruExecutor
from .model_gateway import ModelGateway
from .observability import PhoenixServer
from .patch_store import PatchStore
from .security import AccessDenied
from .skills import SkillStore
from .staging import recover_orphans
from .tools import ToolExecutor


def validate_agent_state(settings: Settings) -> Path:
    state = settings.jarvis_db_path.resolve().parent
    code = Path(__file__).resolve().parent
    runtime = Path(sys.prefix).resolve()
    home = Path.home().resolve()
    if (
        home.is_relative_to(state)
        or code.is_relative_to(state)
        or runtime.is_relative_to(state)
        or any(root.is_relative_to(state) for root in settings.allowed_roots)
        or not settings.jarvis_skills_path.resolve().is_relative_to(state)
    ):
        raise AccessDenied(
            "Agent state must be a dedicated directory outside code and selected roots"
        )
    state.mkdir(parents=True, exist_ok=True)
    info = state.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        raise AccessDenied("Agent state directory is not owned by the current user")
    if info.st_mode & 0o022:
        raise AccessDenied("Agent state directory must not be writable by other users")
    skill_parts = settings.jarvis_skills_path.resolve().relative_to(state).parts
    if not skill_parts:
        raise AccessDenied("Skills must be in a dedicated state subdirectory")
    database = settings.jarvis_db_path.resolve().name
    allowed = {
        database,
        database + "-wal",
        database + "-shm",
        database + "-journal",
        "phoenix.db",
        "phoenix.db-wal",
        "phoenix.db-shm",
        "phoenix.db-journal",
        "phoenix.log",
        "native-session-id",
        ".DS_Store",
        "tmp",
        skill_parts[0],
    }
    for item in state.iterdir():
        metadata = item.lstat()
        if item.name not in allowed or stat.S_ISLNK(metadata.st_mode):
            raise AccessDenied("Agent state directory contains unexpected files or links")
        if stat.S_ISREG(metadata.st_mode) and metadata.st_nlink != 1:
            raise AccessDenied("Agent state directory contains a hard-linked file")
        if not (stat.S_ISREG(metadata.st_mode) or stat.S_ISDIR(metadata.st_mode)):
            raise AccessDenied("Agent state directory contains a special file")
    return state


class Supervisor:
    def __init__(self, settings: Settings, *, model_proxy: bool = True) -> None:
        self.settings = settings
        self.state = validate_agent_state(settings)
        self.authority = BrokerAuthority()
        self.patch_store = PatchStore(settings.allowed_roots)
        self.approvals = PendingApprovals(self.authority, self.patch_store)
        skill_store = SkillStore(settings.jarvis_skills_path)
        platform_executor = ToolExecutor(
            settings, file_executor=NonoFileExecutor(), command_executor=ShuruExecutor()
        )
        self.broker = AuthorizedBroker(
            ExecutionBroker(
                settings,
                NonoFileExecutor(),
                ShuruExecutor(
                    output_limit=settings.jarvis_tool_output_limit, patch_store=self.patch_store
                ),
            ),
            self.authority,
            self.patch_store,
            platform_executor,
            skill_store,
        )
        self.child: asyncio.subprocess.Process | None = None
        self.channel_task: asyncio.Task[None] | None = None
        self.descriptions: dict[str, str] = {}
        self.model_proxy = model_proxy
        self.resources = AsyncExitStack()
        self.phoenix = PhoenixServer(
            settings.jarvis_db_path.resolve().parent / "phoenix.db", settings.jarvis_phoenix_port
        )

    async def start(self) -> None:
        if self.settings.jarvis_base_url:
            raise RuntimeError("Custom model endpoints are not yet supported by the supervisor")
        from .model_lifecycle import active_backend

        registry_path = self.settings.jarvis_model_registry.resolve()
        if registry_path.is_relative_to(self.state):
            raise RuntimeError("Model registry must be outside writable agent state")
        selection = await asyncio.to_thread(active_backend, registry_path)
        if selection:
            self.settings.jarvis_model = "local:" + selection[0]
            self.settings.jarvis_enable_web_search = False
        await asyncio.to_thread(recover_orphans)
        state = self.state
        parent_socket, child_socket = socket.socketpair()
        descriptor = child_socket.fileno()
        scratch = state / "tmp"
        scratch.mkdir(mode=0o700, exist_ok=True)
        environment = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(state),
            "TMPDIR": str(scratch),
            "PYTHONNOUSERSITE": "1",
            "PYTHONUNBUFFERED": "1",
            "JARVIS_WORKER_PROCESS": "1",
            "JARVIS_CONFINED_AGENT": "1",
            "JARVIS_BROKER_FD": str(descriptor),
            "JARVIS_STATE_DIR": str(state),
            "JARVIS_SESSION_FILE": str(state / "native-session-id"),
            "JARVIS_TRACE_MODE": self.settings.jarvis_trace_mode,
            "JARVIS_PHOENIX_PORT": str(self.settings.jarvis_phoenix_port),
            "JARVIS_PHOENIX_MANAGED_EXTERNALLY": "1",
            "JARVIS_DB_PATH": str(self.settings.jarvis_db_path.resolve()),
            "JARVIS_SKILLS_PATH": str(self.settings.jarvis_skills_path.resolve()),
            "JARVIS_ALLOWED_ROOTS": os.pathsep.join(
                str(root) for root in self.settings.allowed_roots
            ),
            "OPENAI_API_KEY": secrets.token_urlsafe(32),
            "JARVIS_MODEL": self.settings.jarvis_model,
            "JARVIS_ENABLE_WRITES": str(self.settings.jarvis_enable_writes).lower(),
            "JARVIS_ENABLE_COMMANDS": str(self.settings.jarvis_enable_commands).lower(),
            "JARVIS_ENABLE_WEB_SEARCH": str(self.settings.jarvis_enable_web_search).lower(),
            "JARVIS_TOOL_OUTPUT_LIMIT": str(self.settings.jarvis_tool_output_limit),
        }
        try:
            if self.settings.jarvis_trace_mode != "off":
                await asyncio.to_thread(self.phoenix.start)
            if self.model_proxy:
                token = environment["OPENAI_API_KEY"]
                gateway = ModelGateway(
                    token,
                    self.settings.openai_api_key or "local",
                    local_backend=selection[1] if selection else None,
                )
                self.resources.push_async_callback(gateway.close)
                environment["JARVIS_MODEL_GATE_PORT"] = str(await gateway.start())
        except BaseException:
            parent_socket.close()
            child_socket.close()
            await self.resources.aclose()
            self.phoenix.stop()
            raise
        try:
            self.child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "jarvis.process_supervisor",
                "--pass-fd",
                str(descriptor),
                sys.executable,
                "-m",
                "jarvis.agent_daemon",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                pass_fds=(descriptor,),
                env=environment,
                start_new_session=True,
            )
        except BaseException:
            parent_socket.close()
            await self.resources.aclose()
            self.phoenix.stop()
            raise
        finally:
            child_socket.close()
        reader, writer = await asyncio.open_connection(sock=parent_socket, limit=200_001)
        self.channel_task = asyncio.create_task(serve_agent(reader, writer, self.broker))

    def approval_event(self, event: dict[str, Any]) -> dict[str, Any]:
        if event.get("status") != "awaiting_approval" or not event.get("approval_id"):
            return event
        request_id = event["approval_id"]
        if request_id not in self.descriptions:
            description = self.approvals.register(
                request_id, event["tool_name"], event["arguments"]
            )
            self.descriptions[request_id] = description
        return {**event, "message": self.descriptions[request_id]}

    async def forward(self, request: dict[str, Any]) -> None:
        assert self.child and self.child.stdin
        if request.get("action") == "filesystem-scope":
            payload = request.get("payload") or {}
            roots = payload.get("roots")
            if not isinstance(roots, list) or any(not isinstance(root, str) for root in roots):
                raise AccessDenied("Invalid filesystem scope")
            if any(Path(root).expanduser().resolve().is_relative_to(self.state) for root in roots):
                raise AccessDenied(
                    "The agent state directory cannot be selected as a user workspace"
                )
            self.settings.set_allowed_roots(roots)
            self.broker.revoke_active()
            self.patch_store.set_roots(self.settings.allowed_roots)
            self.authority.revoke_all()
            self.approvals.clear()
            self.descriptions.clear()
        if request.get("action") == "approve":
            payload = dict(request.get("payload") or {})
            request_id = payload.get("approval_id")
            result = self.approvals.resolve(request_id, payload.get("approved") is True)
            self.descriptions.pop(request_id, None)
            if result:
                operation, arguments, token = result
                payload["broker_grant"] = {
                    "operation": operation,
                    "arguments": arguments,
                    "token": token,
                }
            request = {**request, "payload": payload}
        self.child.stdin.write(json.dumps(request, ensure_ascii=True).encode() + b"\n")
        await self.child.stdin.drain()

    async def close(self) -> None:
        if self.child and self.child.stdin:
            self.child.stdin.close()
        if self.child and self.child.returncode is None:
            with suppress(ProcessLookupError):
                os.killpg(self.child.pid, signal.SIGTERM)
            with suppress(TimeoutError):
                await asyncio.wait_for(self.child.wait(), 5)
            if self.child.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(self.child.pid, signal.SIGKILL)
                await self.child.wait()
        if self.channel_task:
            self.channel_task.cancel()
            await asyncio.gather(self.channel_task, return_exceptions=True)
        await self.resources.aclose()
        self.phoenix.stop()

    async def serve(self) -> None:
        """Relay only through the native app's private stdin/stdout pipes."""
        await self.start()
        assert self.child and self.child.stdout and self.child.stderr
        output_lock = asyncio.Lock()

        async def send(message: dict[str, Any]) -> None:
            data = json.dumps(message, ensure_ascii=True, separators=(",", ":")) + "\n"
            async with output_lock:
                await asyncio.to_thread(sys.stdout.write, data)
                await asyncio.to_thread(sys.stdout.flush)

        async def relay_child() -> None:
            assert self.child and self.child.stdout
            while line := await self.child.stdout.readline():
                message: Any = None
                try:
                    message = json.loads(line)
                    if not isinstance(message, dict):
                        continue
                    if message.get("type") == "event" and isinstance(message.get("event"), dict):
                        message["event"] = self.approval_event(message["event"])
                    result = message.get("result")
                    if isinstance(result, dict) and result.get("status") == "approval_required":
                        approval = result.get("approval") or {}
                        request_id = approval.get("id")
                        if request_id not in self.descriptions:
                            raise AccessDenied("Supervisor could not verify the approval request")
                        approval["description"] = self.descriptions[request_id]
                        result["message"] = self.descriptions[request_id]
                    await send(message)
                except (ValueError, TypeError, KeyError, AccessDenied):
                    if isinstance(message, dict) and message.get("id"):
                        await send(
                            {
                                "id": message["id"],
                                "type": "response",
                                "result": {
                                    "status": "error",
                                    "message": "Supervisor refused an invalid approval request.",
                                },
                            }
                        )
                    else:
                        await send(
                            {
                                "type": "event",
                                "event": {
                                    "status": "failed",
                                    "message": "Supervisor refused an invalid approval request.",
                                },
                            }
                        )

        async def relay_errors() -> None:
            assert self.child and self.child.stderr
            while data := await self.child.stderr.read(4096):
                await asyncio.to_thread(sys.stderr.buffer.write, data)
                await asyncio.to_thread(sys.stderr.buffer.flush)

        output = asyncio.create_task(relay_child())
        errors = asyncio.create_task(relay_errors())
        checks: set[asyncio.Task[None]] = set()

        async def run_check(request_id: str) -> None:
            from .sandbox_check import verify_sandbox

            try:
                result = await verify_sandbox(self.settings)
            except (ValueError, OSError) as exc:
                result = {"status": "error", "message": str(exc)}
            await send({"id": request_id, "type": "response", "result": result})

        async def relay_input() -> None:
            input_reader = asyncio.StreamReader(limit=200_001)
            protocol = asyncio.StreamReaderProtocol(input_reader)
            transport, _ = await asyncio.get_running_loop().connect_read_pipe(
                lambda: protocol, sys.stdin
            )
            try:
                while line := await input_reader.readline():
                    request: Any = None
                    try:
                        request = json.loads(line)
                        if not isinstance(request, dict):
                            raise ValueError("Request must be an object")
                        if request.get("action") == "sandbox-selftest":
                            check = asyncio.create_task(run_check(str(request.get("id", ""))))
                            checks.add(check)
                            check.add_done_callback(checks.discard)
                            continue
                        await self.forward(request)
                    except (ValueError, TypeError, AccessDenied, OSError) as exc:
                        await send(
                            {
                                "id": request.get("id", "") if isinstance(request, dict) else "",
                                "type": "response",
                                "result": {"status": "error", "message": str(exc)},
                            }
                        )
            finally:
                transport.close()

        input_task = asyncio.create_task(relay_input())
        child_exit = asyncio.create_task(self.child.wait())
        try:
            await asyncio.wait((input_task, child_exit), return_when=asyncio.FIRST_COMPLETED)
            if child_exit.done() and not input_task.done():
                await send(
                    {
                        "type": "event",
                        "event": {
                            "status": "failed",
                            "message": "The confined agent stopped; Jarvis will restart it for the next request.",
                        },
                    }
                )
        finally:
            input_task.cancel()
            child_exit.cancel()
            await asyncio.gather(input_task, child_exit, return_exceptions=True)
            for check in checks:
                check.cancel()
            if checks:
                await asyncio.gather(*checks, return_exceptions=True)
            await self.close()
            output.cancel()
            errors.cancel()
            await asyncio.gather(output, errors, return_exceptions=True)


def experimental_supervisor(settings: Settings) -> Supervisor:
    if os.environ.get("JARVIS_SUPERVISOR_EXPERIMENTAL") != "1":
        raise AccessDenied("Supervisor is not enabled for production")
    return Supervisor(settings)


def main() -> None:
    asyncio.run(experimental_supervisor(Settings()).serve())


if __name__ == "__main__":
    main()
