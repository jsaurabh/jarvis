import asyncio
import json
import os
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from jarvis.config import Settings
from jarvis.security import AccessDenied
from jarvis.supervisor import Supervisor


def test_supervisor_rejects_broad_agent_state_before_creating_skills(tmp_path: Path) -> None:
    home = Path.home()
    settings = Settings(
        _env_file=None,
        jarvis_db_path=home / "jarvis-unsafe-test.db",
        jarvis_skills_path=tmp_path / "skills",
        jarvis_allowed_roots=[],
    )
    with pytest.raises(AccessDenied, match="dedicated directory"):
        Supervisor(settings)
    assert not (tmp_path / "skills").exists()


def test_supervisor_rejects_state_directory_with_unrelated_files(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "personal.txt").write_text("untouched")
    settings = Settings(
        _env_file=None,
        jarvis_db_path=state / "jarvis.db",
        jarvis_skills_path=state / "skills",
        jarvis_allowed_roots=[],
    )
    with pytest.raises(AccessDenied, match="unexpected files"):
        Supervisor(settings)
    assert (state / "personal.txt").read_text() == "untouched"


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="Requires macOS Seatbelt")
async def test_supervisor_starts_real_confined_daemon_and_handles_ping(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    supervisor = Supervisor(
        Settings(
            _env_file=None,
            openai_api_key="synthetic-test-key",
            jarvis_db_path=state / "jarvis.db",
            jarvis_skills_path=state / "skills",
            jarvis_allowed_roots=[],
            jarvis_trace_mode="off",
        ),
        model_proxy=False,
    )
    try:
        await supervisor.start()
        assert supervisor.child and supervisor.child.stdout
        child_environment = subprocess.check_output(
            ["/bin/ps", "eww", "-p", str(supervisor.child.pid), "-o", "command="], text=True
        )
        assert "synthetic-test-key" not in child_environment
        await supervisor.forward({"id": "test", "action": "ping"})
        response = None
        for _ in range(4):
            line = await asyncio.wait_for(supervisor.child.stdout.readline(), 20)
            if not line:
                break
            item = json.loads(line)
            if item.get("id") == "test":
                response = item
                break
        if response is None and supervisor.child.stderr:
            stderr = await asyncio.wait_for(supervisor.child.stderr.read(), 5)
            pytest.fail(stderr.decode(errors="replace"))
        assert response and response["result"]["status"] == "completed"
        with pytest.raises(AccessDenied, match="state directory"):
            await supervisor.forward(
                {"id": "scope", "action": "filesystem-scope", "payload": {"roots": [str(state)]}}
            )
        assert supervisor.settings.allowed_roots == ()
        with pytest.raises(AccessDenied):
            await supervisor.forward(
                {
                    "id": "fake",
                    "action": "approve",
                    "payload": {"approval_id": "fake", "approved": True},
                }
            )
    finally:
        await supervisor.close()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="Requires macOS Seatbelt")
async def test_supervisor_starts_confined_daemon_with_local_tracing(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    supervisor = Supervisor(
        Settings(
            _env_file=None,
            openai_api_key="synthetic-test-key",
            jarvis_db_path=state / "jarvis.db",
            jarvis_skills_path=state / "skills",
            jarvis_allowed_roots=[],
            jarvis_trace_mode="full",
            jarvis_phoenix_port=port,
        ),
        model_proxy=False,
    )
    try:
        await supervisor.start()
        assert supervisor.phoenix.healthy
        assert supervisor.phoenix.process
        listeners = subprocess.check_output(
            ["/usr/sbin/lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], text=True
        )
        assert f"127.0.0.1:{port}" in listeners
        assert ":4317" not in listeners and "TCP *:" not in listeners
        assert supervisor.child and supervisor.child.stdout
        await supervisor.forward({"id": "test", "action": "ping"})
        messages = []
        for _ in range(4):
            line = await asyncio.wait_for(supervisor.child.stdout.readline(), 20)
            if not line:
                break
            message = json.loads(line)
            messages.append(message)
            if message.get("id") == "test":
                break
        if not any(message.get("id") == "test" for message in messages):
            assert supervisor.child.stderr
            pytest.fail((await supervisor.child.stderr.read()).decode(errors="replace"))
        assert supervisor.phoenix.healthy
    finally:
        await supervisor.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("JARVIS_TEST_NETWORK") != "1",
    reason="Requires macOS, Squid, and internet access",
)
async def test_supervised_agent_model_request_and_local_traces(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    supervisor = Supervisor(
        Settings(
            _env_file=None,
            openai_api_key="synthetic-invalid-key",
            jarvis_db_path=state / "jarvis.db",
            jarvis_skills_path=state / "skills",
            jarvis_allowed_roots=[],
            jarvis_trace_mode="full",
            jarvis_phoenix_port=port,
        )
    )
    try:
        await supervisor.start()
        assert supervisor.child and supervisor.child.stdout
        child_environment = subprocess.check_output(
            ["/bin/ps", "eww", "-p", str(supervisor.child.pid), "-o", "command="], text=True
        )
        assert "synthetic-invalid-key" not in child_environment
        await supervisor.forward({"id": "test", "action": "chat", "payload": {"text": "test"}})
        messages = []
        for _ in range(12):
            line = await asyncio.wait_for(supervisor.child.stdout.readline(), 30)
            if not line:
                break
            message = json.loads(line)
            messages.append(message)
            if message.get("id") == "test":
                break
        result = next(
            (message["result"] for message in messages if message.get("id") == "test"), None
        )
        assert result is not None, messages
        assert result["status"] in {"failed", "error"}, result
        assert "401" in json.dumps(result), result
        assert supervisor.phoenix.healthy
        assert "synthetic-invalid-key" not in json.dumps(messages)
    finally:
        await supervisor.close()


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires macOS Seatbelt")
def test_supervisor_private_stdio_bridge_starts_and_answers_ping(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    env = {
        **os.environ,
        "JARVIS_SUPERVISOR_EXPERIMENTAL": "1",
        "JARVIS_TRACE_MODE": "off",
        "JARVIS_DB_PATH": str(state / "jarvis.db"),
        "JARVIS_SKILLS_PATH": str(state / "skills"),
        "JARVIS_ALLOWED_ROOTS": "",
        "OPENAI_API_KEY": "synthetic-invalid-key",
        "PYTHONUNBUFFERED": "1",
    }
    child = subprocess.Popen(
        [sys.executable, "-m", "jarvis.supervisor"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert child.stdin and child.stdout
        child.stdin.write('{"id":"test","action":"ping"}\n')
        child.stdin.flush()
        data = b""
        while b'"id":"test"' not in data:
            ready, _, _ = select.select([child.stdout], [], [], 30)
            assert ready, "Supervisor did not respond"
            chunk = os.read(child.stdout.fileno(), 65536)
            if not chunk:
                break
            data += chunk
        messages = [json.loads(line) for line in data.splitlines()]
        assert any(message.get("result", {}).get("status") == "completed" for message in messages)
    finally:
        if child.stdin:
            child.stdin.close()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires macOS Seatbelt")
def test_supervisor_crash_stops_owned_services(tmp_path: Path) -> None:
    from jarvis.staging import recover_orphans

    state = tmp_path / "state"
    state.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "JARVIS_SUPERVISOR_EXPERIMENTAL": "1",
        "JARVIS_TRACE_MODE": "full",
        "JARVIS_PHOENIX_PORT": str(port),
        "JARVIS_DB_PATH": str(state / "jarvis.db"),
        "JARVIS_SKILLS_PATH": str(state / "skills"),
        "JARVIS_ALLOWED_ROOTS": "",
        "OPENAI_API_KEY": "synthetic-invalid-key",
        "PYTHONUNBUFFERED": "1",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "jarvis.supervisor"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert process.stdin and process.stdout
        process.stdin.write('{"id":"ready","action":"ping"}\n')
        process.stdin.flush()
        data = b""
        while b'"id":"ready"' not in data:
            ready, _, _ = select.select([process.stdout], [], [], 35)
            assert ready, "Supervisor did not start"
            chunk = os.read(process.stdout.fileno(), 65536)
            assert chunk, "Supervisor exited unexpectedly"
            data += chunk
        output = subprocess.check_output(["/usr/bin/pgrep", "-P", str(process.pid)], text=True)
        children = [int(pid) for pid in output.splitlines()]
        assert len(children) >= 2
        process.kill()
        process.wait(timeout=10)
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            alive = [
                pid
                for pid in children
                if subprocess.run(["/bin/ps", "-p", str(pid)], capture_output=True).returncode == 0
            ]
            if not alive:
                break
            time.sleep(0.2)
        assert not alive, f"Owned services survived: {alive}"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        recover_orphans()


@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("JARVIS_TEST_NETWORK") != "1",
    reason="Requires macOS, Shuru, Squid, and internet access",
)
def test_setup_check_runs_outside_confined_agent(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    env = {
        **os.environ,
        "JARVIS_SUPERVISOR_EXPERIMENTAL": "1",
        "JARVIS_TRACE_MODE": "off",
        "JARVIS_DB_PATH": str(state / "jarvis.db"),
        "JARVIS_SKILLS_PATH": str(state / "skills"),
        "JARVIS_ALLOWED_ROOTS": "",
        "OPENAI_API_KEY": "synthetic-invalid-key",
        "PYTHONUNBUFFERED": "1",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "jarvis.supervisor"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert process.stdin and process.stdout
        process.stdin.write('{"id":"check","action":"sandbox-selftest"}\n')
        process.stdin.flush()
        data = b""
        while b'"id":"check"' not in data:
            ready, _, _ = select.select([process.stdout], [], [], 40)
            assert ready, "Setup test did not respond"
            chunk = os.read(process.stdout.fileno(), 65536)
            assert chunk, "Supervisor exited unexpectedly"
            data += chunk
        messages = [json.loads(line) for line in data.splitlines()]
        result = next(message["result"] for message in messages if message.get("id") == "check")
        assert result["status"] == "error" and "HTTP 401" in result["message"]
        assert "synthetic-invalid-key" not in str(messages)
    finally:
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires macOS Seatbelt")
def test_supervisor_exits_if_confined_agent_crashes(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    env = {
        **os.environ,
        "JARVIS_SUPERVISOR_EXPERIMENTAL": "1",
        "JARVIS_TRACE_MODE": "off",
        "JARVIS_DB_PATH": str(state / "jarvis.db"),
        "JARVIS_SKILLS_PATH": str(state / "skills"),
        "JARVIS_ALLOWED_ROOTS": "",
        "OPENAI_API_KEY": "synthetic-invalid-key",
        "PYTHONUNBUFFERED": "1",
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "jarvis.supervisor"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        assert process.stdin and process.stdout
        process.stdin.write(b'{"id":"ready","action":"ping"}\n')
        process.stdin.flush()
        data = b""
        while b'"id":"ready"' not in data:
            ready, _, _ = select.select([process.stdout], [], [], 30)
            assert ready
            chunk = os.read(process.stdout.fileno(), 65536)
            assert chunk
            data += chunk
        children = subprocess.check_output(["/usr/bin/pgrep", "-P", str(process.pid)], text=True)
        wrappers = []
        for child in children.splitlines():
            command = subprocess.check_output(["/bin/ps", "-p", child, "-o", "command="], text=True)
            if "jarvis.agent_daemon" in command:
                wrappers.append(int(child))
        assert len(wrappers) == 1
        os.killpg(wrappers[0], signal.SIGKILL)
        assert process.wait(timeout=12) == 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
