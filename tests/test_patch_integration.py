import asyncio
import os
import shutil
import socket
from pathlib import Path

import pytest

from jarvis.broker import ExecutionBroker
from jarvis.broker_authority import AuthorizedBroker, BrokerAuthority, PendingApprovals
from jarvis.broker_channel import AgentBrokerClient, serve_agent
from jarvis.config import Settings
from jarvis.execution import NonoFileExecutor, ShuruExecutor
from jarvis.patch_store import PatchStore
from jarvis.security import AccessDenied


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("JARVIS_TEST_SHURU") != "1" or not shutil.which("shuru"),
    reason="Requires initialized Shuru",
)
async def test_vm_patch_is_reviewed_approved_and_applied_once(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    file = workspace / "note.txt"
    file.write_text("original\n")
    settings = Settings(_env_file=None, jarvis_allowed_roots=[workspace])
    authority = BrokerAuthority()
    patches = PatchStore(settings.allowed_roots)
    pending = PendingApprovals(authority, patches)
    broker = AuthorizedBroker(
        ExecutionBroker(settings, NonoFileExecutor(), ShuruExecutor(patch_store=patches)),
        authority,
        patches,
    )
    command = {
        "argv": ["/bin/sh", "-c", "printf 'changed\\n' > note.txt; echo 'forged diff'"],
        "cwd": str(workspace),
        "timeout_seconds": 30,
        "preview_changes": True,
        "network_hosts": [],
        "credential": "none",
    }
    token = authority.approve("run_command", command)
    result = await broker.execute("run_command", command, token)
    proposal = result["host_review"]["proposal"]
    assert file.read_text() == "original\n"
    arguments = {"patch_id": proposal["patch_id"], "sha256": proposal["sha256"]}
    with pytest.raises(AccessDenied):
        await broker.execute("apply_reviewed_patch", arguments)
    description = pending.register("approval", "apply_reviewed_patch", arguments)
    assert "+changed" in description and "forged diff" not in description
    operation, approved, token = pending.resolve("approval", True)
    await broker.execute(operation, approved, token)
    assert file.read_text() == "changed\n"
    with pytest.raises(AccessDenied):
        await broker.execute(operation, approved, token)


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("JARVIS_TEST_SHURU") != "1" or not shutil.which("shuru"),
    reason="Requires initialized Shuru",
)
async def test_patch_review_and_application_cross_private_broker_channel(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    file = workspace / "note.txt"
    file.write_text("original\n")
    settings = Settings(_env_file=None, jarvis_allowed_roots=[workspace])
    authority = BrokerAuthority()
    patches = PatchStore(settings.allowed_roots)
    pending = PendingApprovals(authority, patches)
    broker = AuthorizedBroker(
        ExecutionBroker(settings, NonoFileExecutor(), ShuruExecutor(patch_store=patches)),
        authority,
        patches,
    )
    parent, child = socket.socketpair()
    reader, writer = await asyncio.open_connection(sock=parent)
    service = asyncio.create_task(serve_agent(reader, writer, broker))
    client = AgentBrokerClient(child.detach())
    command = {
        "argv": ["/bin/sh", "-c", "printf 'changed\\n' > note.txt"],
        "cwd": str(workspace),
        "timeout_seconds": 30,
        "preview_changes": True,
        "network_hosts": [],
        "credential": "none",
    }
    try:
        client.add_grant("run_command", command, authority.approve("run_command", command))
        result = await client.command(**command)
        proposal = result["host_review"]["proposal"]
        arguments = {"patch_id": proposal["patch_id"], "sha256": proposal["sha256"]}
        assert file.read_text() == "original\n"
        description = pending.register("patch", "apply_reviewed_patch", arguments)
        assert "+changed" in description
        operation, approved, token = pending.resolve("patch", True)
        client.add_grant(operation, approved, token)
        await client.apply_patch(**approved)
        assert file.read_text() == "changed\n"
        with pytest.raises(AccessDenied):
            await client.apply_patch(**approved)
    finally:
        await client.close()
        await asyncio.wait_for(service, 10)


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("JARVIS_TEST_SHURU") != "1" or not shutil.which("shuru"),
    reason="Requires initialized Shuru",
)
async def test_vm_multiple_files_each_require_separate_review(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "edit.txt").write_text("old\n")
    (workspace / "delete.txt").write_text("remove\n")
    settings = Settings(_env_file=None, jarvis_allowed_roots=[workspace])
    authority = BrokerAuthority()
    patches = PatchStore(settings.allowed_roots)
    pending = PendingApprovals(authority, patches)
    broker = AuthorizedBroker(
        ExecutionBroker(settings, NonoFileExecutor(), ShuruExecutor(patch_store=patches)),
        authority,
        patches,
    )
    command = {
        "argv": ["/bin/sh", "-c", "echo new > edit.txt; rm delete.txt; echo create > new.txt"],
        "cwd": str(workspace),
        "timeout_seconds": 30,
        "preview_changes": True,
        "network_hosts": [],
        "credential": "none",
    }
    result = await broker.execute("run_command", command, authority.approve("run_command", command))
    proposals = result["host_review"]["proposals"]
    assert len(proposals) == 3
    for index, proposal in enumerate(proposals):
        arguments = {"patch_id": proposal["patch_id"], "sha256": proposal["sha256"]}
        pending.register(str(index), "apply_reviewed_patch", arguments)
        operation, approved, token = pending.resolve(str(index), True)
        await broker.execute(operation, approved, token)
    assert (workspace / "edit.txt").read_text() == "new\n"
    assert (workspace / "new.txt").read_text() == "create\n"
    assert not (workspace / "delete.txt").exists()
