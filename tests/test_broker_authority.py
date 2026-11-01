import asyncio

import pytest

from jarvis.broker import ExecutionBroker
from jarvis.broker_authority import (
    AuthorizedBroker,
    BrokerAuthority,
    PendingApprovals,
    approval_text,
)
from jarvis.config import Settings
from jarvis.execution import NonoFileExecutor, ShuruExecutor
from jarvis.security import AccessDenied
from jarvis.skills import SkillStore


def test_grant_cannot_be_forged_replayed_or_used_for_changed_arguments() -> None:
    authority = BrokerAuthority()
    original = {"path": "/tmp/note", "content": "approved"}
    with pytest.raises(AccessDenied):
        authority.authorize("write_file", original, "made-up")
    token = authority.approve("write_file", original)
    with pytest.raises(AccessDenied):
        authority.authorize("write_file", {**original, "content": "changed"}, token)
    with pytest.raises(AccessDenied):
        authority.authorize("write_file", original, token)
    token = authority.approve("write_file", original)
    authority.authorize("write_file", original, token)
    with pytest.raises(AccessDenied):
        authority.authorize("write_file", original, token)


def test_agent_cannot_turn_a_read_grant_into_a_write() -> None:
    authority = BrokerAuthority()
    authority.authorize("read_file", {"path": "/tmp/note"}, None)
    with pytest.raises(AccessDenied):
        authority.approve("read_file", {"path": "/tmp/note"})
    with pytest.raises(AccessDenied):
        authority.authorize("write_file", {"path": "/tmp/note"}, None)


def test_approval_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    from jarvis import broker_authority

    now = [100.0]
    monkeypatch.setattr(broker_authority.time, "monotonic", lambda: now[0])
    authority = BrokerAuthority(ttl_seconds=10)
    arguments = {"path": "/tmp/note", "content": "approved"}
    token = authority.approve("write_file", arguments)
    now[0] += 11
    with pytest.raises(AccessDenied):
        authority.authorize("write_file", arguments, token)


def test_approval_displays_all_arguments_without_control_sequences() -> None:
    description = approval_text("write_file", {"path": "/tmp/a", "content": "x\x1b[2J\u202e"})
    assert "\\u001b" in description and "\\u202e" in description
    assert "\x1b" not in description and "\u202e" not in description
    with pytest.raises(AccessDenied, match="too large"):
        approval_text("write_file", {"content": "x" * 20_000})


def test_pending_approval_is_frozen_and_cannot_be_resolved_twice() -> None:
    authority = BrokerAuthority()
    pending = PendingApprovals(authority)
    arguments = {"path": "/tmp/note", "content": "reviewed"}
    description = pending.register("id", "write_file", arguments)
    assert "reviewed" in description
    arguments["content"] = "changed"
    operation, approved, token = pending.resolve("id", True)
    assert approved["content"] == "reviewed"
    with pytest.raises(AccessDenied):
        authority.authorize(operation, arguments, token)
    with pytest.raises(AccessDenied):
        pending.resolve("id", True)


@pytest.mark.asyncio
async def test_supervisor_grant_controls_a_real_sandboxed_file_write(tmp_path) -> None:
    target = tmp_path / "document.txt"
    settings = Settings(_env_file=None, jarvis_allowed_roots=[tmp_path])
    authority = BrokerAuthority()
    broker = AuthorizedBroker(
        ExecutionBroker(settings, NonoFileExecutor(), ShuruExecutor()), authority
    )
    arguments = {"path": str(target), "content": "approved", "create_parents": False}
    with pytest.raises(AccessDenied):
        await broker.execute("write_file", arguments)
    assert not target.exists()
    token = authority.approve("write_file", arguments)
    await broker.execute("write_file", arguments, token)
    assert target.read_text() == "approved"
    with pytest.raises(AccessDenied):
        await broker.execute("write_file", arguments, token)


@pytest.mark.asyncio
async def test_platform_action_needs_exact_supervisor_grant(tmp_path) -> None:
    class Platform:
        called = False

        async def execute(self, name, arguments):
            self.called = True
            return {"ok": True, "result": {"name": name, "arguments": arguments}}

    platform = Platform()
    authority = BrokerAuthority()
    policy = ExecutionBroker(
        Settings(_env_file=None, jarvis_allowed_roots=[tmp_path]),
        NonoFileExecutor(),
        ShuruExecutor(),
    )
    broker = AuthorizedBroker(policy, authority, platform_executor=platform)
    with pytest.raises(AccessDenied):
        await broker.execute("open_url", {"url": "https://example.com"})
    assert not platform.called
    args = {"url": "https://example.com"}
    token = authority.approve("open_url", args)
    assert (await broker.execute("open_url", args, token))["name"] == "open_url"


@pytest.mark.asyncio
async def test_scope_change_cancels_inflight_broker_work(tmp_path) -> None:
    entered = asyncio.Event()

    class SlowFileExecutor:
        async def execute(self, request):
            entered.set()
            await asyncio.Event().wait()
            return {"unexpected": True}

    (tmp_path / "file").write_text("x")
    broker = AuthorizedBroker(
        ExecutionBroker(
            Settings(_env_file=None, jarvis_allowed_roots=[tmp_path]),
            SlowFileExecutor(),
            ShuruExecutor(),
        ),
        BrokerAuthority(),
    )
    task = asyncio.create_task(
        broker.execute("read_file", {"path": str(tmp_path / "file"), "offset": 0, "max_chars": 10})
    )
    await entered.wait()
    broker.revoke_active()
    with pytest.raises(AccessDenied, match="access changed"):
        await task


@pytest.mark.asyncio
async def test_skill_edits_require_supervisor_approval(tmp_path) -> None:
    authority = BrokerAuthority()
    skills = SkillStore(tmp_path / "skills")
    broker = AuthorizedBroker(
        ExecutionBroker(
            Settings(_env_file=None, jarvis_allowed_roots=[tmp_path]),
            NonoFileExecutor(),
            ShuruExecutor(),
        ),
        authority,
        skill_store=skills,
    )
    arguments = {"name": "custom-skill", "content": "# Custom\nReviewed instructions"}
    with pytest.raises(AccessDenied):
        await broker.execute("save_skill", arguments)
    token = authority.approve("save_skill", arguments)
    result = await broker.execute("save_skill", arguments, token)
    assert result["saved"] and skills.read("custom-skill")["content"].startswith("# Custom")
    broker.broker.settings.jarvis_enable_writes = False
    token = authority.approve("save_skill", arguments)
    with pytest.raises(AccessDenied, match="Writes are disabled"):
        await broker.execute("save_skill", arguments, token)
