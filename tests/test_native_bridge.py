from pathlib import Path

import jarvis.native_bridge as bridge
from jarvis.store import TraceStore


def test_native_session_is_persistent(tmp_path: Path, monkeypatch) -> None:
    session_file = tmp_path / "native-session-id"
    monkeypatch.setattr(bridge, "SESSION_FILE", session_file)
    store = TraceStore(tmp_path / "trace.db")
    first = bridge.native_session(store)
    second = bridge.native_session(store)
    assert first == second
    assert session_file.read_text() == first


def test_native_bridge_parser() -> None:
    args = bridge.parser().parse_args(["approve", "approval-1", "true"])
    assert args.approval_id == "approval-1"
    assert args.approved == "true"
