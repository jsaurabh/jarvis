from pathlib import Path

import jarvis.daemon as daemon
from jarvis.store import TraceStore


def test_native_session_is_persistent(tmp_path: Path, monkeypatch) -> None:
    session_file = tmp_path / "native-session-id"
    monkeypatch.setattr(daemon, "SESSION_FILE", session_file)
    store = TraceStore(tmp_path / "trace.db")
    first = daemon.native_session(store)
    second = daemon.native_session(store)
    assert first == second
    assert session_file.read_text() == first
