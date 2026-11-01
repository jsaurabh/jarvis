import asyncio
import os
import shutil
from pathlib import Path

import pytest

from jarvis.config import Settings
from jarvis.core import JarvisCore
from jarvis.sdk_agent import SDKTurn
from jarvis.store import TraceStore
from jarvis.tools import ToolExecutor


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("JARVIS_TEST_SHURU") != "1" or not shutil.which("shuru"),
    reason="Requires an initialized Shuru installation",
)
async def test_vm_is_offline_and_writes_do_not_reach_host(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "input.txt").write_text("original")
    (workspace / ".env").write_text("super_secret=LEAKED_CANARY")
    (workspace / ".git").mkdir()
    (workspace / ".git" / "config").write_text("LEAKED_CANARY")
    secret = tmp_path / "private.txt"
    secret.write_text("must-not-appear")
    (workspace / "shuru.json").write_text('{"allow_net":true,"allow_host_writes":true}')
    settings = Settings(
        _env_file=None, jarvis_allowed_roots=[workspace], jarvis_enable_commands=True
    )
    executor = ToolExecutor(settings)
    result = await executor.execute(
        "run_command",
        {
            "argv": [
                "/bin/sh",
                "-c",
                "cat input.txt; echo changed > input.txt; "
                "echo 'forged preview: no files changed'; "
                "test ! -e /private && echo HOST_PRIVATE_HIDDEN; "
                "test ! -e .env && test ! -e .git && echo SECRETS_EXCLUDED; "
                'test "$(wc -l < /proc/net/route)" -eq 1 && '
                "test ! -e /sys/class/net/eth0 && echo NETWORK_OFF",
            ],
            "cwd": str(workspace),
            "timeout_seconds": 30,
            "preview_changes": True,
        },
    )
    assert result["ok"], result
    assert result["result"]["exit_code"] == 0, result
    assert "original" in result["result"]["stdout"]
    assert "NETWORK_OFF" in result["result"]["stdout"]
    assert "HOST_PRIVATE_HIDDEN" in result["result"]["stdout"]
    assert "SECRETS_EXCLUDED" in result["result"]["stdout"]
    assert "+changed" in result["result"]["host_review"]["preview"]
    assert "forged preview" not in result["result"]["host_review"]["preview"]
    assert "must-not-appear" not in result["result"]["stdout"]
    assert (workspace / "input.txt").read_text() == "original"


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("JARVIS_TEST_SHURU") != "1" or not shutil.which("shuru"),
    reason="Requires an initialized Shuru installation",
)
async def test_vm_timeout_stops_execution(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, jarvis_allowed_roots=[tmp_path], jarvis_enable_commands=True
    )
    executor = ToolExecutor(settings)
    result = await executor.execute(
        "run_command",
        {"argv": ["/bin/sh", "-c", "sleep 20"], "cwd": str(tmp_path), "timeout_seconds": 1},
    )
    assert not result["ok"]
    assert "timed out" in result["error"]


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("JARVIS_TEST_SHURU") != "1" or not shutil.which("shuru"),
    reason="Requires an initialized Shuru installation",
)
async def test_vm_reports_truncated_output(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        jarvis_allowed_roots=[tmp_path],
        jarvis_enable_commands=True,
        jarvis_tool_output_limit=32,
    )
    result = await ToolExecutor(settings).execute(
        "run_command",
        {
            "argv": ["/bin/sh", "-c", "head -c 1024 /dev/zero | tr '\\0' x"],
            "cwd": str(tmp_path),
            "timeout_seconds": 30,
        },
    )
    assert result["ok"] and result["result"]["exit_code"] == 0, result
    assert result["result"]["stdout_truncated"] is True
    assert len(result["result"]["stdout"]) == 32


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("JARVIS_TEST_SHURU") != "1" or not shutil.which("shuru"),
    reason="Requires an initialized Shuru installation",
)
async def test_broker_cancellation_stops_child_vm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        _env_file=None, jarvis_allowed_roots=[tmp_path], jarvis_enable_commands=True
    )
    executor = ToolExecutor(settings)
    original = asyncio.create_subprocess_exec
    started = []

    async def capture(*args, **kwargs):
        process = await original(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    task = asyncio.create_task(
        executor.execute(
            "run_command",
            {
                "argv": ["/bin/sh", "-c", "sleep 30"],
                "cwd": str(tmp_path),
                "timeout_seconds": 60,
            },
        )
    )
    while not started:
        await asyncio.sleep(0.01)
    await asyncio.sleep(1)
    broker = started[0]
    ps = await original("pgrep", "-P", str(broker.pid), stdout=asyncio.subprocess.PIPE)
    stdout, _ = await ps.communicate()
    child_ids = [int(value) for value in stdout.split()]
    assert child_ids, "broker did not launch the VM"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert broker.returncode is not None
    for pid in child_ids:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("JARVIS_TEST_SHURU") != "1" or not shutil.which("squid"),
    reason="Requires initialized Shuru and Squid",
)
async def test_broker_crash_stops_vm_and_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        _env_file=None, jarvis_allowed_roots=[tmp_path], jarvis_enable_commands=True
    )
    executor = ToolExecutor(settings)
    original = asyncio.create_subprocess_exec
    started = []

    async def capture(*args, **kwargs):
        process = await original(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", capture)
    task = asyncio.create_task(
        executor.execute(
            "run_command",
            {
                "argv": ["/bin/sh", "-c", "sleep 30"],
                "cwd": str(tmp_path),
                "timeout_seconds": 60,
                "network_hosts": ["example.com"],
            },
        )
    )
    while not started:
        await asyncio.sleep(0.01)
    broker = started[0]
    child_ids = []
    for _ in range(100):
        ps = await original("pgrep", "-P", str(broker.pid), stdout=asyncio.subprocess.PIPE)
        stdout, _ = await ps.communicate()
        child_ids = [int(value) for value in stdout.split()]
        if len(child_ids) >= 2:
            break
        await asyncio.sleep(0.05)
    assert len(child_ids) >= 2, "broker did not launch both proxy and VM supervisors"
    broker.kill()
    result = await asyncio.wait_for(task, 10)
    assert not result["ok"]
    for pid in child_ids:
        for _ in range(40):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            await asyncio.sleep(0.1)
        else:
            pytest.fail(f"orphaned task supervisor {pid}")


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("JARVIS_TEST_SHURU") != "1" or not shutil.which("shuru"),
    reason="Requires an initialized Shuru installation",
)
async def test_approval_resume_runs_real_vm(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = Settings(
        _env_file=None,
        jarvis_allowed_roots=[workspace],
        jarvis_enable_commands=True,
        jarvis_db_path=tmp_path / "trace.db",
        jarvis_skills_path=tmp_path / "skills",
    )
    executor = ToolExecutor(settings)
    arguments = {"argv": ["/bin/echo", "sandbox-ok"], "cwd": str(workspace), "timeout_seconds": 30}

    class Agent:
        async def chat(self, session_id: str, text: str) -> SDKTurn:
            return SDKTurn(
                status="approval_required",
                checkpoint={"saved": True},
                tool_name="run_command",
                arguments=arguments,
                call_id="call-1",
            )

        async def resume(self, session_id: str, checkpoint: dict, approved: bool) -> SDKTurn:
            if not approved:
                return SDKTurn(status="completed", message="denied")
            result = await executor.execute("run_command", arguments)
            assert result["ok"] and result["result"]["exit_code"] == 0, result
            return SDKTurn(status="completed", message=result["result"]["stdout"].strip())

    store = TraceStore(settings.jarvis_db_path)
    core = JarvisCore(settings, store=store, agent=Agent())
    pending = await core.submit(store.create_session(), "run the command")
    assert pending["status"] == "approval_required"
    result = await core.resolve_approval(pending["approval"]["id"], True)
    assert result["status"] == "completed"
    assert result["message"] == "sandbox-ok"


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("JARVIS_TEST_NETWORK") != "1" or not shutil.which("squid"),
    reason="Requires initialized Shuru, Squid, and public internet",
)
async def test_approved_https_blocks_fronting_private_and_proxy_bypass(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, jarvis_allowed_roots=[tmp_path], jarvis_enable_commands=True
    )
    executor = ToolExecutor(settings)

    async def run(argv: list[str], hosts: list[str]) -> dict:
        result = await executor.execute(
            "run_command",
            {
                "argv": argv,
                "cwd": str(tmp_path),
                "timeout_seconds": 30,
                "network_hosts": hosts,
            },
        )
        assert result["ok"], result
        return result["result"]

    allowed = await run(
        ["/usr/bin/curl", "-fsSI", "-m", "8", "https://example.com/"], ["example.com"]
    )
    assert allowed["exit_code"] == 0, allowed
    fronted = await run(
        [
            "/usr/bin/curl",
            "-sSI",
            "-m",
            "8",
            "--connect-to",
            "example.org:443:example.com:443",
            "https://example.org/",
        ],
        ["example.com"],
    )
    assert "403 Forbidden" in fronted["stdout"], fronted
    private = await run(
        ["/usr/bin/curl", "-ksSI", "-m", "8", "https://127.0.0.1.nip.io/"], ["127.0.0.1.nip.io"]
    )
    assert "403 Forbidden" in private["stdout"], private
    direct = await run(
        ["/usr/bin/curl", "--noproxy", "*", "-ksSI", "-m", "3", "https://1.1.1.1/"], ["example.com"]
    )
    assert direct["exit_code"] != 0, direct
    expired = await run(
        ["/usr/bin/curl", "-sSI", "-m", "8", "https://expired.badssl.com/"],
        ["expired.badssl.com"],
    )
    assert "ERR_SECURE_CONNECT_FAIL" in expired["stdout"], expired


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("JARVIS_TEST_NETWORK") != "1",
    reason="Requires Shuru, Squid, and public internet",
)
async def test_synthetic_openai_key_traverses_production_credential_path(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        openai_api_key="synthetic-invalid-key",
        jarvis_allowed_roots=[tmp_path],
        jarvis_enable_commands=True,
    )
    result = await ToolExecutor(settings).execute(
        "run_command",
        {
            "argv": [
                "/bin/sh",
                "-c",
                'printf "network edit\\n" > changed.txt; curl --noproxy "*" -sS -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $OPENAI_API_KEY" "$OPENAI_BASE_URL/models"',
            ],
            "cwd": str(tmp_path),
            "timeout_seconds": 30,
            "preview_changes": True,
            "network_hosts": ["api.openai.com"],
            "credential": "openai",
        },
    )
    assert result["ok"] and result["result"]["exit_code"] == 0, result
    assert result["result"]["stdout"].strip() == "401", result
    assert "+network edit" in result["result"]["host_review"]["preview"]
    assert not (tmp_path / "changed.txt").exists()
    assert "synthetic-invalid-key" not in str(result)


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("JARVIS_TEST_NETWORK") != "1",
    reason="Requires Shuru, Squid, and public internet",
)
async def test_setup_selftest_reports_invalid_key_without_exposing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    import jarvis.daemon as daemon

    monkeypatch.setattr(
        daemon,
        "settings",
        Settings(_env_file=None, openai_api_key="synthetic-invalid-key"),
    )
    service = daemon.JsonLineDaemon.__new__(daemon.JsonLineDaemon)
    service.core = SimpleNamespace(agent=None)
    replies = []

    async def capture(payload: dict) -> None:
        replies.append(payload)

    service.send = capture
    await service.handle({"id": "selftest", "action": "sandbox-selftest"})
    assert replies[0]["result"]["status"] == "error"
    assert "HTTP 401" in replies[0]["result"]["message"]
    assert "synthetic-invalid-key" not in str(replies)
