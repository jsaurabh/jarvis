import asyncio
import json
import os
import select
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.broker_authority import BrokerAuthority
from jarvis.security import AccessDenied


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires macOS Seatbelt")
def test_confined_agent_cannot_read_other_files_or_spawn_tools(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "protected").mkdir()
    (state / "trace.db").write_text("private trace")
    outside = tmp_path / "outside"
    outside.write_text("secret")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    script = r"""
import json, pathlib, socket, subprocess, sys
import os
from jarvis.agent_sandbox import confine_agent
state, outside = map(pathlib.Path, sys.argv[1:3])
confine_agent((pathlib.Path('/usr/bin/id'), pathlib.Path('/System')), state, protected_write_paths=(state / 'protected',), protected_files=(state / 'trace.db',))
(state / 'allowed').write_text('ok')
try:
    (state / 'protected' / 'file').write_text('bad')
    protected_blocked = False
except PermissionError:
    protected_blocked = True
try:
    (state / 'trace.db').read_text()
    trace_blocked = False
except PermissionError:
    trace_blocked = True
try:
    os.link(state / 'trace.db', state / 'trace-link')
    (state / 'trace-link').read_text()
    trace_link_blocked = False
except PermissionError:
    trace_link_blocked = True
try:
    outside.read_text()
    read_blocked = False
except PermissionError:
    read_blocked = True
try:
    (pathlib.Path('/System/Volumes/Data') / str(outside).lstrip('/')).read_text()
    alternate_blocked = False
except (PermissionError, FileNotFoundError):
    alternate_blocked = True
try:
    os.symlink(outside, state / 'link')
    (state / 'link').read_text()
    symlink_blocked = False
except PermissionError:
    symlink_blocked = True
try:
    os.link(outside, state / 'hardlink')
    (state / 'hardlink').read_text()
    hardlink_blocked = False
except PermissionError:
    hardlink_blocked = True
try:
    subprocess.run(['/usr/bin/id'], check=False, capture_output=True)
    exec_blocked = False
except PermissionError:
    exec_blocked = True
try:
    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    connection.settimeout(1)
    connection.connect(('127.0.0.1', int(sys.argv[3])))
    network_blocked = False
except PermissionError:
    network_blocked = True
print(json.dumps({'read_blocked': read_blocked, 'alternate_blocked': alternate_blocked, 'symlink_blocked': symlink_blocked, 'hardlink_blocked': hardlink_blocked, 'exec_blocked': exec_blocked, 'network_blocked': network_blocked, 'protected_blocked': protected_blocked, 'trace_blocked': trace_blocked, 'trace_link_blocked': trace_link_blocked}))
"""
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                str(state),
                str(outside),
                str(listener.getsockname()[1]),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env={**os.environ, "PYTHONNOUSERSITE": "1", "JARVIS_WORKER_PROCESS": "1"},
        )
    finally:
        listener.close()
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "read_blocked": True,
        "alternate_blocked": True,
        "symlink_blocked": True,
        "hardlink_blocked": True,
        "exec_blocked": True,
        "network_blocked": True,
        "protected_blocked": True,
        "trace_blocked": True,
        "trace_link_blocked": True,
    }
    assert (state / "allowed").read_text() == "ok"


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires macOS Seatbelt")
def test_confined_agent_cannot_signal_an_unrelated_process(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    victim = subprocess.Popen(["/bin/sleep", "30"])
    script = r"""
import os, pathlib, signal, sys
from jarvis.agent_sandbox import confine_agent
confine_agent((), pathlib.Path(sys.argv[1]))
try:
    os.kill(int(sys.argv[2]), signal.SIGTERM)
    print('signal allowed')
except PermissionError:
    print('signal denied')
"""
    try:
        attacker = subprocess.run(
            [sys.executable, "-c", script, str(state), str(victim.pid)],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
            env={**os.environ, "PYTHONNOUSERSITE": "1", "JARVIS_WORKER_PROCESS": "1"},
        )
        assert attacker.stdout.strip() == "signal denied"
        assert victim.poll() is None
    finally:
        if victim.poll() is None:
            victim.terminate()
        victim.wait(timeout=10)


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires macOS Seatbelt")
def test_confined_agent_cannot_forge_supervisor_approval(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("not available to agent")
    parent, child_socket = socket.socketpair()
    script = r"""
import json, pathlib, socket, sys
from jarvis.agent_sandbox import confine_agent
state, secret, descriptor = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), int(sys.argv[3])
channel = socket.socket(fileno=descriptor).makefile('rwb', buffering=0)
confine_agent((), state)
try:
    secret.read_text()
    blocked = False
except PermissionError:
    blocked = True
channel.write(json.dumps({'blocked': blocked, 'grant': 'forged'}).encode() + b'\n')
first = json.loads(channel.readline())
channel.write(json.dumps({'grant': first['token'], 'content': 'tampered'}).encode() + b'\n')
second = json.loads(channel.readline())
channel.write(json.dumps({'grant': first['token'], 'content': 'approved'}).encode() + b'\n')
third = json.loads(channel.readline())
print(json.dumps([first['denied'], second['denied'], third['denied']]))
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(state), str(secret), str(child_socket.fileno())],
        pass_fds=(child_socket.fileno(),),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONNOUSERSITE": "1", "JARVIS_WORKER_PROCESS": "1"},
    )
    child_socket.close()
    authority = BrokerAuthority()
    arguments = {"path": "/tmp/file", "content": "approved"}
    channel = parent.makefile("rwb", buffering=0)
    try:
        request = json.loads(channel.readline())
        assert request["blocked"] is True
        with pytest.raises(AccessDenied):
            authority.authorize("write_file", arguments, request["grant"])
        token = authority.approve("write_file", arguments)
        channel.write(json.dumps({"denied": True, "token": token}).encode() + b"\n")
        request = json.loads(channel.readline())
        with pytest.raises(AccessDenied):
            authority.authorize("write_file", {**arguments, "content": request["content"]}, token)
        channel.write(b'{"denied":true}\n')
        request = json.loads(channel.readline())
        with pytest.raises(AccessDenied):
            authority.authorize("write_file", arguments, request["grant"])
        channel.write(b'{"denied":true}\n')
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
        assert json.loads(stdout) == [True, True, True]
    finally:
        channel.close()
        parent.close()
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires macOS Seatbelt")
def test_actual_confined_daemon_starts_and_answers_ping(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    (state / "skills").mkdir()
    parent, child_socket = socket.socketpair()
    env = {
        **os.environ,
        "JARVIS_CONFINED_AGENT": "1",
        "JARVIS_BROKER_FD": str(child_socket.fileno()),
        "JARVIS_STATE_DIR": str(state),
        "JARVIS_DB_PATH": str(state / "jarvis.db"),
        "JARVIS_SKILLS_PATH": str(state / "skills"),
        "JARVIS_SESSION_FILE": str(state / "session"),
        "JARVIS_TRACE_MODE": "off",
        "OPENAI_API_KEY": "synthetic-test-key",
        "PYTHONNOUSERSITE": "1",
    }
    child = subprocess.Popen(
        [sys.executable, "-m", "jarvis.agent_daemon"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(child_socket.fileno(),),
        text=True,
        env=env,
    )
    child_socket.close()
    try:
        assert child.stdin and child.stdout
        child.stdin.write('{"id":"test","action":"ping"}\n')
        child.stdin.flush()
        data = b""
        while b'"id":"test"' not in data:
            ready, _, _ = select.select([child.stdout], [], [], 20)
            assert ready, "Confined daemon did not respond"
            chunk = os.read(child.stdout.fileno(), 65536)
            if not chunk:
                break
            data += chunk
        lines = [json.loads(line) for line in data.splitlines()]
        assert any(
            item.get("result", {}).get("message") == "Jarvis Core is ready." for item in lines
        )
        child.stdin.close()
        assert child.wait(timeout=10) == 0
    finally:
        parent.close()
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("JARVIS_TEST_NETWORK") != "1",
    reason="Requires macOS, Squid, and internet access",
)
async def test_confined_agent_reaches_only_supervisor_model_proxy(tmp_path: Path) -> None:
    from jarvis.network_proxy import NetworkProxy
    from jarvis.staging import staging

    state = tmp_path / "state"
    state.mkdir()
    script = r"""
import pathlib, ssl, sys
import httpx
from jarvis.agent_sandbox import confine_agent
state, certificate, port = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), int(sys.argv[3])
verification = ssl.create_default_context(cafile=str(certificate))
verification.verify_flags &= ~ssl.VERIFY_X509_STRICT
confine_agent((certificate, pathlib.Path(sys.prefix), pathlib.Path(sys.base_prefix), pathlib.Path('/usr/lib'), pathlib.Path('/usr/share'), pathlib.Path('/System')), state, port)
response = httpx.get('https://api.openai.com/v1/models', headers={'Authorization': 'Bearer synthetic-invalid-key'}, proxy=f'http://127.0.0.1:{port}', verify=verification, timeout=15, trust_env=False)
print(response.status_code)
"""
    with staging() as directory:
        proxy = NetworkProxy(directory, ("api.openai.com",))
        try:
            port = await proxy.start()
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                script,
                str(state),
                str(directory / "proxy-cert.pem"),
                str(port),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={
                    "PATH": "/usr/bin:/bin",
                    "PYTHONNOUSERSITE": "1",
                    "JARVIS_WORKER_PROCESS": "1",
                },
            )
            stdout, stderr = await asyncio.wait_for(child.communicate(), 25)
            assert child.returncode == 0, stderr.decode(errors="replace")
            assert stdout.strip() == b"401"
        finally:
            await proxy.close()


@pytest.mark.asyncio
@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("JARVIS_TEST_NETWORK") != "1",
    reason="Requires macOS, Squid, and internet access",
)
async def test_confined_sdk_uses_supervisor_model_proxy(tmp_path: Path) -> None:
    from jarvis.model_gateway import ModelGateway
    from jarvis.network_proxy import NetworkProxy
    from jarvis.staging import staging

    state = tmp_path / "state"
    state.mkdir()
    parent, child_socket = socket.socketpair()
    script = r"""
import asyncio, os, pathlib, sys
import openai
from jarvis.agent_sandbox import confine_agent
from jarvis.config import Settings
from jarvis.sdk_agent import SDKJarvisAgent
from jarvis.tools import ToolExecutor
state, certificate, port = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2]), int(sys.argv[3])
settings = Settings(_env_file=None, openai_api_key=os.environ['OPENAI_API_KEY'], jarvis_db_path=state / 'test.db', jarvis_trace_mode='off')
confine_agent((certificate, pathlib.Path(sys.prefix), pathlib.Path(sys.base_prefix), pathlib.Path('/usr/lib'), pathlib.Path('/usr/share'), pathlib.Path('/System')), state, port)
executor = ToolExecutor(settings)
agent = SDKJarvisAgent(settings, executor)
async def run():
    try:
        await agent.chat('test-session', 'test')
    except openai.AuthenticationError as exc:
        print(exc.status_code)
    finally:
        await executor.broker.close()
asyncio.run(run())
"""
    with staging() as directory:
        proxy = NetworkProxy(directory, ("api.openai.com",))
        try:
            port = await proxy.start()
            gateway = ModelGateway(
                "synthetic-phantom-token",
                "synthetic-invalid-key",
                port,
                directory / "proxy-cert.pem",
            )
            gateway_port = await gateway.start()
            descriptor = child_socket.fileno()
            env = {
                "PATH": "/usr/bin:/bin",
                "PYTHONNOUSERSITE": "1",
                "JARVIS_WORKER_PROCESS": "1",
                "JARVIS_CONFINED_AGENT": "1",
                "JARVIS_BROKER_FD": str(descriptor),
                "JARVIS_MODEL_GATE_PORT": str(gateway_port),
                "OPENAI_API_KEY": "synthetic-phantom-token",
            }
            child = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                script,
                str(state),
                str(directory / "proxy-cert.pem"),
                str(gateway_port),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                pass_fds=(descriptor,),
                env=env,
            )
            child_socket.close()
            stdout, stderr = await asyncio.wait_for(child.communicate(), 35)
            assert child.returncode == 0, stderr.decode(errors="replace")
            assert stdout.strip() == b"401"
        finally:
            parent.close()
            child_socket.close()
            if "gateway" in locals():
                await gateway.close()
            await proxy.close()
