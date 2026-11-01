import json
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.config import Settings
from jarvis.tools import ToolExecutor


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="Tests macOS native sandbox")
async def test_native_tool_is_scoped_and_deny_by_default(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    secret = tmp_path / "secret.c"
    secret.write_text("PRIVATE_CANARY\n")
    executor = ToolExecutor(Settings(_env_file=None, jarvis_allowed_roots=[root]))
    result = await executor.execute(
        "run_native_command",
        {
            "argv": ["/usr/bin/clang", "--version"],
            "cwd": str(root),
            "timeout_seconds": 20,
        },
    )
    assert result["ok"] and result["result"]["exit_code"] == 0, result
    (root / "hello.c").write_text("int answer(void) { return 42; }\n")
    result = await executor.execute(
        "run_native_command",
        {
            "argv": ["/usr/bin/clang", "-c", "hello.c", "-o", "hello.o"],
            "cwd": str(root),
            "timeout_seconds": 20,
        },
    )
    assert result["ok"] and result["result"]["exit_code"] == 0, result
    assert (root / "hello.o").is_file()
    result = await executor.execute(
        "run_native_command",
        {
            "argv": ["/usr/bin/clang", "-x", "c", "-E", str(secret)],
            "cwd": str(root),
            "timeout_seconds": 20,
        },
    )
    assert result["ok"] and result["result"]["exit_code"] != 0, result
    assert "PRIVATE_CANARY" not in result["result"]["stdout"]
    result = await executor.execute(
        "run_native_command",
        {
            "argv": ["/bin/sh", "-c", "touch escaped"],
            "cwd": str(root),
            "timeout_seconds": 5,
        },
    )
    assert not result["ok"]
    assert not (root / "escaped").exists()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="Tests macOS native sandbox")
async def test_native_swift_cannot_read_outside_scope_or_open_network(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    secret = tmp_path / "private.txt"
    secret.write_text("PRIVATE_CANARY")
    target = subprocess.Popen(["/bin/sleep", "60"])
    script = (
        "import Foundation; import Darwin; "
        f"let p = {json.dumps(str(secret))}; "
        'print((try? String(contentsOfFile: p, encoding: .utf8)) == nil ? "FILE_DENIED" : "FILE_LEAK"); '
        "let fd = socket(AF_INET, SOCK_STREAM, 0); "
        "var address = sockaddr_in(); address.sin_len = UInt8(MemoryLayout<sockaddr_in>.size); "
        "address.sin_family = sa_family_t(AF_INET); address.sin_port = in_port_t(443).bigEndian; "
        'inet_pton(AF_INET, "1.1.1.1", &address.sin_addr); '
        "let r = withUnsafePointer(to: &address) { $0.withMemoryRebound(to: sockaddr.self, capacity: 1) "
        "{ connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size)) } }; "
        'print(r == -1 && (errno == EPERM || errno == EACCES) ? "NETWORK_DENIED" : "NETWORK_UNEXPECTED"); '
        f'let k = kill({target.pid}, SIGTERM); print(k == -1 && errno == EPERM ? "SIGNAL_DENIED" : "SIGNAL_ALLOWED")'
    )
    executor = ToolExecutor(Settings(_env_file=None, jarvis_allowed_roots=[root]))
    try:
        result = await executor.execute(
            "run_native_command",
            {"argv": ["/usr/bin/swift", "-e", script], "cwd": str(root), "timeout_seconds": 60},
        )
    finally:
        if target.poll() is None:
            target.terminate()
        target.wait()
    assert result["ok"] and result["result"]["exit_code"] == 0, result
    assert "FILE_DENIED" in result["result"]["stdout"]
    assert "NETWORK_DENIED" in result["result"]["stdout"]
    assert "SIGNAL_DENIED" in result["result"]["stdout"]
