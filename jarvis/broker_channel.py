"""Private, bounded agent-to-supervisor execution transport."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid
from contextlib import suppress
from typing import Any

from opentelemetry import trace

from .broker_authority import AuthorizedBroker, request_digest
from .security import AccessDenied

MAX_MESSAGE = 200_000


class AgentBrokerClient:
    """Connect only to the supervisor channel inherited at process launch."""

    def __init__(self, descriptor: int) -> None:
        self._socket = socket.socket(fileno=descriptor)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._grants: dict[str, str] = {}
        self.tracer = trace.get_tracer(__name__)

    def set_tracer_provider(self, provider: Any) -> None:
        self.tracer = provider.get_tracer(__name__)

    def add_grant(self, operation: str, arguments: dict[str, Any], token: str) -> None:
        self._grants[request_digest(operation, arguments)] = token

    async def _request(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        with self.tracer.start_as_current_span("jarvis.broker.execute") as span:
            span.set_attribute("jarvis.execution.operation", operation)
            backend = (
                "shuru"
                if operation == "run_command"
                else "nono"
                if operation
                in {
                    "read_file",
                    "list_directory",
                    "search_files",
                    "write_file",
                    "run_native_command",
                }
                else "supervisor"
            )
            span.set_attribute("jarvis.execution.backend", backend)
            if backend in {"shuru", "nono"}:
                span.set_attribute(
                    "jarvis.execution.network",
                    "restricted" if arguments.get("network_hosts") else "denied",
                )
            if operation == "run_command":
                span.set_attribute(
                    "jarvis.execution.credential", arguments.get("credential", "none")
                )
                span.set_attribute(
                    "jarvis.execution.network_host_count", len(arguments.get("network_hosts") or [])
                )
            try:
                result = await self._send(operation, arguments)
                span.set_attribute("jarvis.execution.outcome", "completed")
                return result
            except Exception:
                span.set_attribute("jarvis.execution.outcome", "denied_or_failed")
                raise

    async def _send(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            if self._reader is None or self._writer is None:
                self._reader, self._writer = await asyncio.open_connection(
                    sock=self._socket, limit=MAX_MESSAGE + 1
                )
            request_id = uuid.uuid4().hex
            token = self._grants.pop(request_digest(operation, arguments), None)
            payload = (
                json.dumps(
                    {
                        "id": request_id,
                        "operation": operation,
                        "arguments": arguments,
                        "token": token,
                    },
                    ensure_ascii=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode()
                + b"\n"
            )
            if len(payload) > MAX_MESSAGE:
                raise AccessDenied("Broker request exceeds limit")
            self._writer.write(payload)
            await self._writer.drain()
            line = await self._reader.readline()
            if not line or len(line) > MAX_MESSAGE:
                raise AccessDenied("Broker channel closed or returned an invalid response")
            response = json.loads(line)
            if not isinstance(response, dict) or response.get("id") != request_id:
                raise AccessDenied("Broker returned a mismatched response")
            if response.get("ok") is not True:
                raise AccessDenied(str(response.get("error", "Broker denied request")))
            result = response.get("result")
            if not isinstance(result, dict):
                raise AccessDenied("Broker returned an invalid result")
            return result

    async def file(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._request(operation, arguments)

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
            "run_command",
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
            "run_native_command", {"argv": argv, "cwd": cwd, "timeout_seconds": timeout_seconds}
        )

    async def apply_patch(self, patch_id: str, sha256: str) -> dict[str, Any]:
        return await self._request("apply_reviewed_patch", {"patch_id": patch_id, "sha256": sha256})

    async def platform(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._request(operation, arguments)

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            with suppress(ConnectionError):
                await self._writer.wait_closed()
        else:
            self._socket.close()


def inherited_agent_channel() -> AgentBrokerClient | None:
    descriptor = os.environ.get("JARVIS_BROKER_FD")
    if descriptor is None:
        return None
    if not descriptor.isdecimal() or int(descriptor) < 3:
        raise RuntimeError("Invalid inherited broker channel")
    return AgentBrokerClient(int(descriptor))


async def serve_agent(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, broker: AuthorizedBroker
) -> None:
    """Serve one inherited channel. This channel cannot create approvals."""
    try:
        while True:
            try:
                line = await reader.readline()
            except ValueError:
                break
            if not line or len(line) > MAX_MESSAGE:
                break
            request_id = ""
            try:
                request: Any = json.loads(line)
                if not isinstance(request, dict) or set(request) - {
                    "id",
                    "operation",
                    "arguments",
                    "token",
                }:
                    raise ValueError("Invalid broker request")
                request_id = request.get("id")
                operation = request.get("operation")
                arguments = request.get("arguments")
                token = request.get("token")
                if (
                    not isinstance(request_id, str)
                    or len(request_id) > 128
                    or not isinstance(operation, str)
                    or not isinstance(arguments, dict)
                    or token is not None
                    and not isinstance(token, str)
                ):
                    raise ValueError("Invalid broker request")
                result = await broker.execute(operation, arguments, token)
                response = {"id": request_id, "ok": True, "result": result}
            except (ValueError, TypeError, OSError, AccessDenied) as exc:
                response = {"id": request_id, "ok": False, "error": str(exc)}
            data = json.dumps(response, ensure_ascii=True, separators=(",", ":")).encode() + b"\n"
            if len(data) > MAX_MESSAGE:
                data = (
                    json.dumps(
                        {"id": request_id, "ok": False, "error": "Broker response exceeds limit"}
                    ).encode()
                    + b"\n"
                )
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()
        with suppress(ConnectionError):
            await writer.wait_closed()
