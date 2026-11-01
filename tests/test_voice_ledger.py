"""Native event ordering test; no microphone or API session needed."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("swiftc") is None, reason="Requires Swift compiler")
def test_voice_event_ordering(tmp_path):
    source = Path(__file__).resolve().parents[1] / "macos/VoiceTraceLedger.swift"
    main = tmp_path / "main.swift"
    main.write_text("""
import Foundation
let ledger = VoiceTraceLedger()
var records: [(String, String, [String: Any])] = []
ledger.emit = { records.append(($0, $1, $2)) }
ledger.committed("input1")
ledger.started("response1")
ledger.delta("response1", text: "partial")
ledger.transcribed("input1", text: "first")
assert(records.isEmpty) // a partial delta is not a completed response
ledger.committed("input2")
ledger.started("response2")
ledger.completed("response1", answer: "partial", metadata: ["status": "cancelled"])
ledger.completed("response2", answer: "answer2", metadata: ["status": "completed"])
ledger.transcribed("input2", text: "second")
assert(records.count == 2)
assert(records[0].0 == "first" && records[1].0 == "second")
assert(records[0].2["status"] as? String == "cancelled")
ledger.completed("response1", answer: "duplicate", metadata: [:])
assert(records.count == 2)
ledger.committed("input3")
ledger.started("response3")
ledger.delta("response3", text: "unfinished")
ledger.disconnected()
assert(records.count == 3)
assert(records[2].1 == "unfinished")
assert(records[2].2["status"] as? String == "cancelled")
""")
    binary = tmp_path / "voice-test"
    subprocess.run(
        ["swiftc", str(source), str(main), "-o", str(binary)], check=True, capture_output=True
    )
    subprocess.run([str(binary)], check=True, capture_output=True)
