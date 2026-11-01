"""One-use, argument-bound capabilities held by a trusted broker supervisor.

Only the trusted supervisor may create grants. Tokens received from an agent
are merely references to grants already held here; they are not approvals.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from .broker import ExecutionBroker
from .security import AccessDenied

SKILL_READ_OPERATIONS = frozenset({"list_skills", "read_skill"})
SKILL_APPROVAL_OPERATIONS = frozenset({"save_skill", "delete_skill"})
PLATFORM_READ_OPERATIONS = frozenset({"get_frontmost_application", "list_running_applications"})
PLATFORM_APPROVAL_OPERATIONS = frozenset(
    {
        "read_clipboard",
        "set_clipboard",
        "open_application",
        "open_url",
        "type_text",
        "press_key",
        "click_screen",
        "list_calendar_events",
        "create_calendar_event",
    }
)
READ_OPERATIONS = (
    frozenset({"read_file", "list_directory", "search_files"})
    | PLATFORM_READ_OPERATIONS
    | SKILL_READ_OPERATIONS
)
APPROVAL_OPERATIONS = (
    frozenset({"write_file", "run_command", "run_native_command", "apply_reviewed_patch"})
    | PLATFORM_APPROVAL_OPERATIONS
    | SKILL_APPROVAL_OPERATIONS
)


def request_digest(operation: str, arguments: dict[str, Any]) -> str:
    if operation not in READ_OPERATIONS | APPROVAL_OPERATIONS:
        raise AccessDenied("Unsupported broker operation")
    encoded = json.dumps(
        {"operation": operation, "arguments": arguments},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")
    if len(encoded) > 200_000:
        raise AccessDenied("Broker request exceeds the size limit")
    return hashlib.sha256(encoded).hexdigest()


def approval_text(operation: str, arguments: dict[str, Any]) -> str:
    """Render the exact approved arguments, escaping untrusted control characters."""
    request_digest(operation, arguments)
    rendered = json.dumps(arguments, sort_keys=True, ensure_ascii=True, indent=2, allow_nan=False)
    if len(rendered.encode("ascii")) > 16_000:
        raise AccessDenied("Request is too large to review safely")
    context = ""
    if operation == "run_command":
        hosts = arguments.get("network_hosts") or []
        context = "\nThis runs in a temporary Linux VM. "
        context += (
            "The listed HTTPS hosts use a local inspecting proxy." if hosts else "It is offline."
        )
        if arguments.get("credential") == "openai":
            context += " One restricted OpenAI request may send workspace data and incur charges."
    elif operation == "run_native_command":
        context = "\nThis runs project code on your Mac and may modify the working folder."
    return f"{operation}{context}\n\n{rendered}"


@dataclass(frozen=True)
class Grant:
    digest: str
    deadline: float


class BrokerAuthority:
    """Private supervisor state; never construct from an agent-supplied policy."""

    def __init__(self, ttl_seconds: float = 120) -> None:
        if not 0 < ttl_seconds <= 600:
            raise ValueError("Invalid broker grant lifetime")
        self.ttl_seconds = ttl_seconds
        self._grants: dict[str, Grant] = {}

    def revoke_all(self) -> None:
        self._grants.clear()

    def approve(self, operation: str, arguments: dict[str, Any]) -> str:
        if operation not in APPROVAL_OPERATIONS:
            raise AccessDenied("This operation cannot receive an approval grant")
        approval_text(operation, arguments)
        digest = request_digest(operation, arguments)
        self._grants = {
            token: grant
            for token, grant in self._grants.items()
            if grant.deadline > time.monotonic()
        }
        token = secrets.token_urlsafe(32)
        self._grants[token] = Grant(digest, time.monotonic() + self.ttl_seconds)
        return token

    def authorize(self, operation: str, arguments: dict[str, Any], token: str | None) -> None:
        digest = request_digest(operation, arguments)
        if operation in READ_OPERATIONS:
            return
        if not isinstance(token, str):
            raise AccessDenied("A separate user approval is required")
        grant = self._grants.pop(token, None)
        if (
            grant is None
            or grant.deadline <= time.monotonic()
            or not secrets.compare_digest(grant.digest, digest)
        ):
            raise AccessDenied("Approval is invalid, expired, used, or for different arguments")


@dataclass(frozen=True)
class PendingRequest:
    operation: str
    arguments: dict[str, Any]
    description: str


class PendingApprovals:
    """Hold immutable copies of requests reviewed through a trusted UI channel."""

    def __init__(self, authority: BrokerAuthority, patch_store: Any = None) -> None:
        self.authority = authority
        self.patch_store = patch_store
        self._requests: dict[str, PendingRequest] = {}

    def clear(self) -> None:
        self._requests.clear()

    def register(self, request_id: str, operation: str, arguments: dict[str, Any]) -> str:
        if not request_id or len(request_id) > 128 or operation not in APPROVAL_OPERATIONS:
            raise AccessDenied("Invalid approval request")
        if request_id in self._requests or len(self._requests) >= 100:
            raise AccessDenied("Duplicate or excessive approval requests")
        description = approval_text(operation, arguments)
        if operation == "apply_reviewed_patch":
            if self.patch_store is None or set(arguments) != {"patch_id", "sha256"}:
                raise AccessDenied("Patch approval is unavailable")
            description += "\n\n" + self.patch_store.describe(
                arguments["patch_id"], arguments["sha256"]
            )
        # Round-trip before displaying: later mutation of the agent's input cannot
        # change the request to which the user's approval applies.
        frozen = json.loads(json.dumps(arguments, ensure_ascii=True, allow_nan=False))
        self._requests[request_id] = PendingRequest(operation, frozen, description)
        return description

    def resolve(self, request_id: str, approved: bool) -> tuple[str, dict[str, Any], str] | None:
        request = self._requests.pop(request_id, None)
        if request is None:
            raise AccessDenied("Approval is unknown or already resolved")
        if not approved:
            return None
        if request.operation == "apply_reviewed_patch":
            if self.patch_store is None:
                raise AccessDenied("Patch approval is unavailable")
            self.patch_store.describe(request.arguments["patch_id"], request.arguments["sha256"])
        token = self.authority.approve(request.operation, request.arguments)
        return request.operation, request.arguments, token


class AuthorizedBroker:
    """Route agent requests through supervisor-owned policy and one-use grants."""

    def __init__(
        self,
        broker: ExecutionBroker,
        authority: BrokerAuthority,
        patch_store: Any = None,
        platform_executor: Any = None,
        skill_store: Any = None,
    ) -> None:
        self.broker = broker
        self.authority = authority
        self.patch_store = patch_store
        self.platform_executor = platform_executor
        self.skill_store = skill_store
        self._active: set[asyncio.Task[dict[str, Any]]] = set()

    def revoke_active(self) -> None:
        for task in self._active:
            task.cancel()

    async def execute(
        self, operation: str, arguments: dict[str, Any], token: str | None = None
    ) -> dict[str, Any]:
        self.authority.authorize(operation, arguments, token)
        task = asyncio.create_task(self._execute_authorized(operation, arguments))
        self._active.add(task)
        try:
            return await task
        except asyncio.CancelledError as exc:
            if task.cancelled() and not asyncio.current_task().cancelling():
                raise AccessDenied("Execution was cancelled because access changed") from exc
            raise
        finally:
            self._active.discard(task)

    async def _execute_authorized(
        self, operation: str, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        if operation in {"read_file", "list_directory", "search_files", "write_file"}:
            return await self.broker.file(operation, arguments)
        if operation == "run_command":
            return await self.broker.command(**arguments)
        if operation == "run_native_command":
            return await self.broker.native(**arguments)
        if operation == "apply_reviewed_patch" and self.patch_store is not None:
            if not self.broker.settings.jarvis_enable_writes:
                raise AccessDenied("Writes are disabled")
            if set(arguments) != {"patch_id", "sha256"}:
                raise AccessDenied("Invalid patch request")
            return self.patch_store.apply(arguments["patch_id"], arguments["sha256"])
        if operation in PLATFORM_READ_OPERATIONS | PLATFORM_APPROVAL_OPERATIONS:
            if self.platform_executor is None:
                raise AccessDenied("The supervised platform service is unavailable")
            result = await self.platform_executor.execute(operation, arguments)
            if not result.get("ok"):
                raise AccessDenied(str(result.get("error", "Platform request failed")))
            return result["result"]
        if operation in SKILL_READ_OPERATIONS | SKILL_APPROVAL_OPERATIONS:
            if self.skill_store is None:
                raise AccessDenied("The supervised skill service is unavailable")
            if (
                operation in SKILL_APPROVAL_OPERATIONS
                and not self.broker.settings.jarvis_enable_writes
            ):
                raise AccessDenied("Writes are disabled")
            if operation == "list_skills" and not arguments:
                return {"skills": self.skill_store.list()}
            if operation == "read_skill" and set(arguments) == {"name"}:
                return self.skill_store.read(arguments["name"])
            if operation == "save_skill" and set(arguments) == {"name", "content"}:
                saved = self.skill_store.save(arguments["name"], arguments["content"])
                return {"saved": True, "name": saved["name"]}
            if operation == "delete_skill" and set(arguments) == {"name"}:
                return {
                    "deleted": self.skill_store.delete(arguments["name"]),
                    "name": arguments["name"],
                }
            raise AccessDenied("Invalid skill request")
        raise AccessDenied("Unsupported broker operation")
