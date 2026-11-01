"""Central policy decisions for filesystem and isolated command execution."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from opentelemetry import trace

from .config import Settings
from .execution import (
    CommandExecutor,
    CommandRequest,
    FileExecutor,
    FileRequest,
    NonoCommandExecutor,
)
from .security import AccessDenied, require_unprotected, resolve_allowed


@dataclass
class ExecutionBroker:
    settings: Settings
    files: FileExecutor
    commands: CommandExecutor
    native_commands: CommandExecutor = field(default_factory=NonoCommandExecutor)

    async def file(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if operation == "write_file" and not self.settings.jarvis_enable_writes:
            raise AccessDenied("Writes are disabled")
        path = arguments["root" if operation == "search_files" else "path"]
        with trace.get_tracer(__name__).start_as_current_span("jarvis.execution.file") as span:
            span.set_attribute("jarvis.execution.backend", "nono")
            span.set_attribute("jarvis.execution.operation", operation)
            target = resolve_allowed(path, self.settings.allowed_roots)
            require_unprotected(target, recursive=operation == "search_files")
            root = next(root for root in self.settings.allowed_roots if target.is_relative_to(root))
            return await self.files.execute(FileRequest(operation, arguments, root))

    async def command(
        self,
        argv: list[str],
        cwd: str,
        timeout_seconds: int,
        preview_changes: bool = False,
        network_hosts: list[str] | None = None,
        credential: str = "none",
    ) -> dict[str, Any]:
        if not self.settings.jarvis_enable_commands:
            raise AccessDenied("Commands are disabled")
        if (
            not argv
            or len(argv) > 64
            or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
        ):
            raise ValueError("Invalid command arguments")
        if sum(len(arg) for arg in argv) > 100_000:
            raise ValueError("Command arguments are too large")
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("Command timeout must be between 1 and 120 seconds")
        from .network_proxy import validate_hosts

        hosts = validate_hosts(network_hosts) if network_hosts else ()
        if credential not in {"none", "openai"}:
            raise AccessDenied("Unknown credential capability")
        if credential == "openai" and "api.openai.com" not in hosts:
            raise AccessDenied("OpenAI credential use requires approval for api.openai.com")
        with trace.get_tracer(__name__).start_as_current_span("jarvis.execution.command") as span:
            span.set_attribute("jarvis.execution.backend", "shuru")
            span.set_attribute("jarvis.execution.network", "restricted" if hosts else "disabled")
            target = resolve_allowed(cwd, self.settings.allowed_roots)
            require_unprotected(target, recursive=True)
            if not target.is_dir():
                raise ValueError(f"Not a directory: {target}")
            result = await self.commands.execute(
                CommandRequest(
                    tuple(argv),
                    target,
                    timeout_seconds,
                    preview_changes,
                    hosts,
                    "" if credential == "none" else credential,
                    self.settings.openai_api_key if credential == "openai" else "",
                )
            )
            span.set_attribute("jarvis.execution.exit_code", result["exit_code"])
            return result

    async def native(self, argv: list[str], cwd: str, timeout_seconds: int) -> dict[str, Any]:
        allowed = {"/usr/bin/swift", "/usr/bin/swiftc", "/usr/bin/clang"}
        if not self.settings.jarvis_enable_commands:
            raise AccessDenied("Commands are disabled")
        if (
            not argv
            or len(argv) > 64
            or argv[0] not in allowed
            or any(not isinstance(arg, str) or "\0" in arg for arg in argv)
            or sum(len(arg) for arg in argv) > 100_000
        ):
            raise AccessDenied("Native executable is not on the approved tool list")
        if not 1 <= timeout_seconds <= 120:
            raise ValueError("Command timeout must be between 1 and 120 seconds")
        target = resolve_allowed(cwd, self.settings.allowed_roots)
        require_unprotected(target, recursive=True)
        if not target.is_dir():
            raise ValueError(f"Not a directory: {target}")
        return await self.native_commands.execute(
            CommandRequest(tuple(argv), target, timeout_seconds)
        )


class BrokerClient:
    """Supervise a broker child over private pipes. The caller remains trusted."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.tracer = trace.get_tracer(__name__)

    def set_tracer_provider(self, provider: Any) -> None:
        self.tracer = provider.get_tracer(__name__)

    async def file(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._request("file", {"operation": operation, "arguments": arguments})

    async def command(
        self,
        argv: list[str],
        cwd: str,
        timeout_seconds: int,
        preview_changes: bool = False,
        network_hosts: list[str] | None = None,
        credential: str = "none",
    ) -> dict[str, Any]:
        return await self._request(
            "command",
            {
                "argv": argv,
                "cwd": cwd,
                "timeout_seconds": timeout_seconds,
                "preview_changes": preview_changes,
                "network_hosts": network_hosts or [],
                "credential": credential,
            },
        )

    async def native(self, argv: list[str], cwd: str, timeout_seconds: int) -> dict[str, Any]:
        return await self._request(
            "native", {"argv": argv, "cwd": cwd, "timeout_seconds": timeout_seconds}
        )

    async def _request(self, kind: str, arguments: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "kind": kind,
            "arguments": arguments,
            "roots": [str(root) for root in self.settings.allowed_roots],
            "writes": self.settings.jarvis_enable_writes,
            "commands": self.settings.jarvis_enable_commands,
            "output_limit": self.settings.jarvis_tool_output_limit,
        }
        if kind == "command" and arguments.get("credential") == "openai":
            payload["api_key"] = self.settings.openai_api_key
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "jarvis.broker_process",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env={
                "PATH": "/usr/bin:/bin",
                "PYTHONNOUSERSITE": "1",
                "HOME": os.path.expanduser("~"),
                "JARVIS_WORKER_PROCESS": "1",
            },
        )
        with self.tracer.start_as_current_span("jarvis.broker.execute") as span:
            span.set_attribute("jarvis.execution.backend", "shuru" if kind == "command" else "nono")
            span.set_attribute("jarvis.execution.operation", arguments.get("operation", kind))
            span.set_attribute(
                "jarvis.execution.network",
                "restricted" if arguments.get("network_hosts") else "denied",
            )
            if arguments.get("network_hosts"):
                span.set_attribute(
                    "jarvis.execution.network_host_count", len(arguments["network_hosts"])
                )
                span.set_attribute("jarvis.execution.tls_inspected_locally", True)
            span.set_attribute("jarvis.execution.credential", arguments.get("credential", "none"))
            span.set_attribute(
                "jarvis.execution.filesystem_mode",
                "disposable_overlay"
                if kind == "command"
                else "read_write"
                if kind == "native"
                else ("read_write" if arguments.get("operation") == "write_file" else "read_only"),
            )
            try:
                stdout, stderr = await process.communicate(json.dumps(payload).encode())
            except BaseException:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.send_signal(signal.SIGTERM)
                    try:
                        await asyncio.wait_for(process.wait(), 5)
                    except TimeoutError:
                        with suppress(ProcessLookupError):
                            process.kill()
                        await process.wait()
                raise
            if process.returncode != 0:
                raise AccessDenied(
                    "Execution broker failed: " + stderr.decode(errors="replace")[-1000:]
                )
            try:
                response = json.loads(stdout)
                if not response["ok"]:
                    span.set_attribute("jarvis.execution.outcome", "denied_or_failed")
                    raise AccessDenied(response["error"])
                result = response["result"]
                span.set_attribute("jarvis.execution.outcome", "completed")
                if (
                    kind == "command"
                    and isinstance(result, dict)
                    and isinstance(result.get("exit_code"), int)
                ):
                    span.set_attribute("jarvis.execution.exit_code", result["exit_code"])
                    span.set_attribute(
                        "jarvis.execution.stdout_truncated", bool(result.get("stdout_truncated"))
                    )
                    snapshot = result.get("snapshot")
                    if isinstance(snapshot, dict):
                        for field in ("files", "bytes", "excluded"):
                            if isinstance(snapshot.get(field), int):
                                span.set_attribute(
                                    "jarvis.execution.snapshot." + field, snapshot[field]
                                )
                return result
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise AccessDenied("Invalid execution broker response") from exc
