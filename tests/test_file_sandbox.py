import json
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.config import Settings
from jarvis.execution import ShuruExecutor
from jarvis.tools import ToolExecutor


@pytest.mark.skipif(sys.platform != "darwin", reason="Tests the macOS Seatbelt backend")
def test_nono_blocks_ungranted_file_and_network(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("private")
    code = """
import json, socket, sys
from nono_py import AccessMode, CapabilitySet, apply
caps = CapabilitySet()
caps.allow_path(sys.argv[1], AccessMode.READ)
caps.block_network()
apply(caps)
result = {}
try:
    open(sys.argv[2]).read()
    result["file_blocked"] = False
except OSError:
    result["file_blocked"] = True
try:
    sock = socket.socket()
    sock.settimeout(1)
    sock.connect(("1.1.1.1", 443))
    result["network_blocked"] = False
except PermissionError:
    result["network_blocked"] = True
print(json.dumps(result))
"""
    process = subprocess.run(
        [sys.executable, "-c", code, str(allowed), str(secret)],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    assert json.loads(process.stdout) == {"file_blocked": True, "network_blocked": True}


@pytest.mark.asyncio
async def test_write_and_command_fail_closed(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None, jarvis_allowed_roots=[tmp_path], jarvis_enable_commands=True
    )
    executor = ToolExecutor(settings, command_executor=ShuruExecutor(binary=tmp_path / "missing"))
    target = tmp_path / "new.txt"
    written = await executor.execute(
        "write_file", {"path": str(target), "content": "hello", "create_parents": False}
    )
    assert written["ok"] and target.read_text() == "hello"
    result = await executor.execute(
        "run_command",
        {
            "argv": ["/usr/bin/touch", str(tmp_path / "escaped")],
            "cwd": str(tmp_path),
            "timeout_seconds": 1,
        },
    )
    assert not result["ok"]
    assert not (tmp_path / "escaped").exists()


@pytest.mark.asyncio
async def test_sandboxed_write_rejects_hard_links(tmp_path: Path) -> None:
    root = tmp_path / "allowed"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    (root / "alias.txt").hardlink_to(outside)
    executor = ToolExecutor(Settings(_env_file=None, jarvis_allowed_roots=[root]))
    result = await executor.execute(
        "write_file",
        {"path": str(root / "alias.txt"), "content": "changed", "create_parents": False},
    )
    assert not result["ok"]
    assert outside.read_text() == "keep"


@pytest.mark.asyncio
async def test_unapproved_shuru_version_fails_closed(tmp_path: Path) -> None:
    binary = tmp_path / "shuru"
    binary.write_text("#!/bin/sh\necho 'shuru 999.0.0'\n")
    binary.chmod(0o700)
    settings = Settings(
        _env_file=None, jarvis_allowed_roots=[tmp_path], jarvis_enable_commands=True
    )
    executor = ToolExecutor(settings, command_executor=ShuruExecutor(binary=binary))
    result = await executor.execute(
        "run_command", {"argv": ["true"], "cwd": str(tmp_path), "timeout_seconds": 5}
    )
    assert not result["ok"]
    assert "version mismatch" in result["error"]


def test_worker_import_does_not_load_local_env(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("OPENAI_API_KEY=sentinel\n")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from jarvis.config import settings; print(bool(settings.openai_api_key))",
        ],
        cwd=tmp_path,
        env={"PATH": "/usr/bin:/bin", "JARVIS_WORKER_PROCESS": "1"},
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "False"
