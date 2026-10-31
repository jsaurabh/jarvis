import json
from pathlib import Path

from jarvis.export import export_dataset
from jarvis.flywheel import build_improvement_bundle
from jarvis.learning import sensitive_reasons
from jarvis.store import TraceStore


def test_only_vetted_runs_are_exported(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "trace.db")
    session = store.create_session()
    good = store.create_run(session, "hello", "test-model")
    store.finish_run(good, "hi")
    store.feedback(good, 1, None)
    unreviewed = store.create_run(session, "unreviewed", "test-model")
    store.finish_run(unreviewed, "do not train")

    output = tmp_path / "train.jsonl"
    train_count, eval_count, _ = export_dataset(store, output, "sft", 0)
    assert (train_count, eval_count) == (1, 0)
    row = json.loads(output.read_text().strip())
    assert row["messages"][-1]["content"] == "hi"


def test_correction_replaces_bad_answer(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "trace.db")
    session = store.create_session()
    run = store.create_run(session, "question", "test-model")
    store.finish_run(run, "wrong")
    store.feedback(run, -1, "correct")
    output = tmp_path / "train.jsonl"
    export_dataset(store, output, "sft", 0)
    row = json.loads(output.read_text().strip())
    assert row["messages"][-1]["content"] == "correct"


def test_privacy_filter_ignores_internal_ids_but_scans_content() -> None:
    internal = {
        "run_id": "12345678901234567890123456789012",
        "created_at": "2026-10-30T12:34:56-07:00",
        "messages": [{"role": "user", "content": "ordinary text"}],
    }
    assert sensitive_reasons(internal) == []
    internal["messages"][0]["content"] = "Email me at private@example.com"
    assert sensitive_reasons(internal) == ["email"]


def test_record_completed_realtime_turn(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "trace.db")
    session = store.create_session()

    run_id = store.record_completed_turn(
        session,
        "Hello Jarvis",
        "Hello. How can I help?",
        "gpt-realtime-2.1-mini",
        {"transport": "websocket", "interrupted": False},
    )

    run = store.runs_for_session(session)[0]
    assert run["id"] == run_id
    assert run["status"] == "completed"
    assert run["assistant_text"] == "Hello. How can I help?"
    assert store.events_for_run(run_id)[1]["kind"] == "realtime_metadata"


def test_agent_sft_preserves_tool_calls_and_harness(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "trace.db")
    session = store.create_session()
    run = store.create_run(session, "List my notes", "test-model")
    store.set_run_context(
        run,
        {
            "schema": "jarvis.harness.v1",
            "version": "test",
            "interface": "agent_task",
            "tools": [{"type": "function", "name": "list_directory"}],
            "harness_sha256": "abc",
        },
    )
    store.event(
        run,
        "tool_call",
        {"tool": "list_directory", "arguments": {"path": "/notes"}, "call_id": "call-1"},
    )
    store.event(
        run,
        "tool_result",
        {
            "tool": "list_directory",
            "call_id": "call-1",
            "output": {"ok": True, "entries": ["a.md"]},
        },
    )
    store.finish_run(run, "I found a.md")
    store.feedback(run, 1, None)

    output = tmp_path / "agent.jsonl"
    train, evaluation, _ = export_dataset(store, output, "agent_sft", 0)
    assert (train, evaluation) == (1, 0)
    row = json.loads(output.read_text())
    assert row["schema"] == "jarvis.agent-sft.v1"
    assert row["tools"][0]["name"] == "list_directory"
    assert any(message.get("tool_calls") for message in row["messages"])
    assert any(message.get("role") == "tool" for message in row["messages"])


def test_sensitive_training_example_is_excluded_by_default(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "trace.db")
    session = store.create_session()
    run = store.create_run(session, "Email me at private@example.com", "test-model")
    store.finish_run(run, "Okay")
    store.feedback(run, 1, None)
    output = tmp_path / "agent.jsonl"
    train, evaluation, _ = export_dataset(store, output, "agent_sft", 0)
    assert (train, evaluation) == (0, 0)
    manifest = json.loads((tmp_path / "agent.manifest.json").read_text())
    assert manifest["excluded_sensitive"][run] == ["email"]


def test_correction_creates_preference_and_harness_bundle(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "trace.db")
    session = store.create_session()
    run = store.create_run(session, "What is two plus two?", "test-model")
    store.finish_run(run, "Five")
    store.feedback(run, -1, "Four")
    store.record_telemetry(
        {
            "object": "trace.span",
            "id": "span-bundle",
            "trace_id": "trace-bundle",
            "span_data": {"type": "generation", "name": "model"},
        },
        run,
    )

    output = tmp_path / "preference.jsonl"
    train, evaluation, _ = export_dataset(store, output, "preference", 0)
    assert (train, evaluation) == (1, 0)
    row = json.loads(output.read_text())
    assert row["chosen"] == "Four"
    assert row["rejected"] == "Five"

    summary = build_improvement_bundle(store, tmp_path / "bundle")
    assert summary["regression_cases"] == 1
    assert summary["telemetry_records"] == 1
    assert (tmp_path / "bundle" / "sdk-telemetry.jsonl").exists()
    assert (tmp_path / "bundle" / "codex-handoff.md").exists()
