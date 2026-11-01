import asyncio
import json
import socket
import sys
from pathlib import Path

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from jarvis.broker import ExecutionBroker
from jarvis.broker_authority import AuthorizedBroker, BrokerAuthority
from jarvis.broker_channel import AgentBrokerClient, serve_agent
from jarvis.config import Settings
from jarvis.execution import NonoFileExecutor, ShuruExecutor
from jarvis.tools import ToolExecutor


@pytest.mark.asyncio
async def test_agent_channel_cannot_approve_or_replay_real_file_write(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    arguments = {"path": str(target), "content": "reviewed", "create_parents": False}
    authority = BrokerAuthority()
    policy = ExecutionBroker(
        Settings(_env_file=None, jarvis_allowed_roots=[tmp_path]),
        NonoFileExecutor(),
        ShuruExecutor(),
    )
    supervisor_socket, agent_socket = socket.socketpair()
    server_reader, server_writer = await asyncio.open_connection(sock=supervisor_socket)
    reader, writer = await asyncio.open_connection(sock=agent_socket)
    server = asyncio.create_task(
        serve_agent(server_reader, server_writer, AuthorizedBroker(policy, authority))
    )

    async def request(operation: str, token: str | None = None) -> dict:
        writer.write(
            json.dumps(
                {"id": "1", "operation": operation, "arguments": arguments, "token": token}
            ).encode()
            + b"\n"
        )
        await writer.drain()
        return json.loads(await reader.readline())

    try:
        assert not (await request("approve"))["ok"]
        assert not (await request("write_file"))["ok"]
        assert not target.exists()
        # Only the trusted supervisor, not the agent channel, can create this grant.
        token = authority.approve("write_file", arguments)
        assert (await request("write_file", token))["ok"]
        assert target.read_text() == "reviewed"
        assert not (await request("write_file", token))["ok"]
    finally:
        writer.close()
        await writer.wait_closed()
        await asyncio.wait_for(server, 10)


@pytest.mark.asyncio
async def test_channel_client_uses_only_a_matching_one_use_grant(tmp_path: Path) -> None:
    target = tmp_path / "file.txt"
    arguments = {"path": str(target), "content": "reviewed", "create_parents": False}
    authority = BrokerAuthority()
    policy = ExecutionBroker(
        Settings(_env_file=None, jarvis_allowed_roots=[tmp_path]),
        NonoFileExecutor(),
        ShuruExecutor(),
    )
    supervisor_socket, agent_socket = socket.socketpair()
    reader, writer = await asyncio.open_connection(sock=supervisor_socket)
    server = asyncio.create_task(serve_agent(reader, writer, AuthorizedBroker(policy, authority)))
    client = AgentBrokerClient(agent_socket.detach())
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    client.set_tracer_provider(provider)
    try:
        client.add_grant("write_file", arguments, authority.approve("write_file", arguments))
        await client.file("write_file", arguments)
        assert target.read_text() == "reviewed"
        assert (
            exporter.get_finished_spans()[0].attributes["jarvis.execution.outcome"] == "completed"
        )
        assert "reviewed" not in str(exporter.get_finished_spans()[0].attributes)
        with pytest.raises(ValueError, match="approval"):
            await client.file("write_file", arguments)
    finally:
        await client.close()
        await asyncio.wait_for(server, 10)
        provider.shutdown()


@pytest.mark.asyncio
async def test_tool_executor_routes_inherited_channel_through_supervisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "file.txt"
    arguments = {"path": str(target), "content": "reviewed", "create_parents": False}
    settings = Settings(_env_file=None, jarvis_allowed_roots=[tmp_path])
    authority = BrokerAuthority()
    policy = ExecutionBroker(settings, NonoFileExecutor(), ShuruExecutor())
    supervisor_socket, agent_socket = socket.socketpair()
    reader, writer = await asyncio.open_connection(sock=supervisor_socket)
    server = asyncio.create_task(serve_agent(reader, writer, AuthorizedBroker(policy, authority)))
    monkeypatch.setenv("JARVIS_BROKER_FD", str(agent_socket.detach()))
    executor = ToolExecutor(settings)
    try:
        assert not (await executor.execute("write_file", arguments))["ok"]
        executor.broker.add_grant(
            "write_file", arguments, authority.approve("write_file", arguments)
        )
        assert (await executor.execute("write_file", arguments))["ok"]
        assert target.read_text() == "reviewed"
    finally:
        await executor.broker.close()
        await asyncio.wait_for(server, 10)


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="Requires macOS Seatbelt")
async def test_confined_agent_reads_only_through_real_supervisor(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    target = workspace / "note.txt"
    target.write_text("brokered data")
    settings = Settings(_env_file=None, jarvis_allowed_roots=[workspace])
    authority = BrokerAuthority()
    policy = ExecutionBroker(settings, NonoFileExecutor(), ShuruExecutor())
    supervisor_socket, agent_socket = socket.socketpair()
    reader, writer = await asyncio.open_connection(sock=supervisor_socket)
    server = asyncio.create_task(serve_agent(reader, writer, AuthorizedBroker(policy, authority)))
    script = r"""
import asyncio, json, pathlib, sys
from jarvis.agent_sandbox import confine_agent
from jarvis.config import Settings
from jarvis.tools import ToolExecutor
state, target = map(pathlib.Path, sys.argv[1:])
executor = ToolExecutor(Settings(_env_file=None, jarvis_allowed_roots=[target.parent]))
confine_agent((pathlib.Path('/usr/lib'), pathlib.Path('/usr/share'), pathlib.Path('/System'), pathlib.Path(sys.prefix), pathlib.Path(sys.base_prefix)), state)
try:
    target.read_text()
    direct_blocked = False
except PermissionError:
    direct_blocked = True
async def run():
    result = await executor.execute('read_file', {'path':str(target), 'offset':0, 'max_chars':100})
    await executor.broker.close()
    print(json.dumps({'direct_blocked':direct_blocked,'result':result}))
asyncio.run(run())
"""
    descriptor = agent_socket.fileno()
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
        "JARVIS_WORKER_PROCESS": "1",
        "JARVIS_BROKER_FD": str(descriptor),
    }
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(state),
        str(target),
        pass_fds=(descriptor,),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    agent_socket.close()
    try:
        stdout, stderr = await asyncio.wait_for(child.communicate(), 20)
        assert child.returncode == 0, stderr.decode(errors="replace")
        result = json.loads(stdout)
        assert result["direct_blocked"] is True
        assert result["result"]["ok"] is True
        assert "brokered data" in str(result["result"])
    finally:
        if child.returncode is None:
            child.kill()
            await child.wait()
        await asyncio.wait_for(server, 10)
