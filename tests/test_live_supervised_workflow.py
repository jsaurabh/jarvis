"""Opt-in, billable model test using only disposable files and a synthetic prompt."""

import json
import os
import select
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.skipif(
    sys.platform != "darwin" or os.environ.get("JARVIS_LIVE_TEST") != "1",
    reason="Requires explicit opt-in, macOS, and an OpenAI API key",
)
def test_live_supervised_agent_reviews_and_applies_patch(tmp_path: Path) -> None:
    state, workspace = tmp_path / "state", tmp_path / "workspace"
    state.mkdir()
    workspace.mkdir()
    target = workspace / "note.txt"
    target.write_text("original\n")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    key = os.environ["OPENAI_API_KEY"]
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": os.path.expanduser("~"),
        "PYTHONUNBUFFERED": "1",
        "JARVIS_SUPERVISOR_EXPERIMENTAL": "1",
        "JARVIS_TRACE_MODE": "full",
        "JARVIS_DB_PATH": str(state / "jarvis.db"),
        "JARVIS_SKILLS_PATH": str(state / "skills"),
        "JARVIS_PHOENIX_PORT": str(port),
        "JARVIS_ALLOWED_ROOTS": str(workspace),
        "JARVIS_ENABLE_WEB_SEARCH": "false",
        "OPENAI_API_KEY": key,
    }
    process = subprocess.Popen(
        [sys.executable, "-m", "jarvis.supervisor"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=env,
    )
    try:
        assert process.stdin and process.stdout

        def send(message: dict) -> None:
            process.stdin.write(json.dumps(message).encode() + b"\n")
            process.stdin.flush()

        command = "printf 'changed\\n' > note.txt"
        prompt = (
            f"In the folder {workspace}, use run_command with argv exactly "
            f'["/bin/sh", "-c", {json.dumps(command)}], cwd {workspace}, timeout_seconds 30, '
            "preview_changes true, network_hosts [], credential none. Then apply its proposed "
            "patch using apply_reviewed_patch. This is a temporary test file."
        )
        send({"id": "task", "action": "chat", "payload": {"text": prompt}})
        buffer = b""
        deadline = time.monotonic() + 100
        approvals: set[str] = set()
        terminal = None
        while time.monotonic() < deadline:
            readable, _, _ = select.select(
                [process.stdout], [], [], min(5, deadline - time.monotonic())
            )
            if not readable:
                if process.poll() is not None:
                    break
                continue
            chunk = os.read(process.stdout.fileno(), 65536)
            if not chunk:
                break
            buffer += chunk
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                message = json.loads(line)
                event = message.get("event", {})
                approval_id = event.get("approval_id")
                if (
                    event.get("status") == "awaiting_approval"
                    and approval_id
                    and approval_id not in approvals
                ):
                    operation = event.get("tool_name")
                    arguments = event.get("arguments", {})
                    if operation == "run_command":
                        assert arguments.get("argv") == ["/bin/sh", "-c", command]
                        assert arguments.get("cwd") == str(workspace)
                        assert arguments.get("preview_changes") is True
                        assert (
                            not arguments.get("network_hosts")
                            and arguments.get("credential") == "none"
                        )
                    elif operation == "apply_reviewed_patch":
                        assert "+changed" in event.get("message", "")
                    else:
                        pytest.fail("Unexpected approval request during live test")
                    assert target.read_text() == "original\n"
                    approvals.add(approval_id)
                    send(
                        {
                            "id": f"approval-{len(approvals)}",
                            "action": "approve",
                            "payload": {"approval_id": approval_id, "approved": True},
                        }
                    )
                if message.get("id", "").startswith("approval-"):
                    status = message.get("result", {}).get("status")
                    if status in {"completed", "failed", "error"}:
                        terminal = status
            if terminal:
                break
        assert terminal == "completed"
        assert len(approvals) == 2
        assert target.read_text() == "changed\n"
        trace_count = 0
        trace_deadline = time.monotonic() + 15
        while time.monotonic() < trace_deadline:
            try:
                with sqlite3.connect(state / "phoenix.db") as database:
                    trace_count = database.execute("SELECT COUNT(*) FROM spans").fetchone()[0]
                if trace_count:
                    break
            except sqlite3.OperationalError:
                pass
            time.sleep(0.25)
        assert trace_count > 0, "No local OpenInference spans were recorded"
    finally:
        if process.stdin:
            process.stdin.close()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
