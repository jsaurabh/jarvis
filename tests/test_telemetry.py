from pathlib import Path
from types import SimpleNamespace

from jarvis.store import TraceStore
from jarvis.telemetry import LocalTelemetryProcessor, export_telemetry


class Exportable(SimpleNamespace):
    def export(self) -> dict:
        return self.payload


def test_sdk_telemetry_is_correlated_and_exportable(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "jarvis.db")
    session_id = store.create_session()
    run_id = store.create_run(session_id, "organize notes", "test-model")
    processor = LocalTelemetryProcessor(store)
    trace_id = "trace_123"
    processor.on_trace_start(
        Exportable(
            payload={
                "object": "trace",
                "id": trace_id,
                "workflow_name": "Jarvis agent task",
                "group_id": session_id,
                "metadata": {"run_id": run_id, "task_id": "task_123"},
            }
        )
    )
    processor.on_span_end(
        Exportable(
            payload={
                "object": "trace.span",
                "id": "span_123",
                "trace_id": trace_id,
                "parent_id": None,
                "started_at": "2026-10-31T10:00:00+00:00",
                "ended_at": "2026-10-31T10:00:01+00:00",
                "span_data": {"type": "generation", "name": "model"},
                "error": None,
            }
        )
    )
    processor.on_trace_end(
        Exportable(
            payload={
                "object": "trace",
                "id": trace_id,
                "workflow_name": "Jarvis agent task",
                "group_id": session_id,
                "metadata": {"run_id": run_id, "task_id": "task_123"},
            }
        )
    )

    records = store.telemetry_for_run(run_id)
    assert len(records) == 2
    assert records[0]["run_id"] == run_id
    assert store.telemetry_metrics() == {
        "traces": 1,
        "spans": 1,
        "model_calls": 1,
        "tool_calls": 0,
        "average_span_ms": 1000.0,
        "correlated_records": 2,
    }

    output = tmp_path / "telemetry.jsonl"
    assert export_telemetry(store, output, run_id) == 2
    assert len(output.read_text(encoding="utf-8").splitlines()) == 2


def test_metadata_trace_mode_omits_span_content(tmp_path: Path) -> None:
    store = TraceStore(tmp_path / "jarvis.db", "metadata")
    store.record_telemetry(
        {
            "object": "trace.span",
            "id": "span_private",
            "trace_id": "trace_private",
            "started_at": "2026-10-31T10:00:00+00:00",
            "ended_at": "2026-10-31T10:00:01+00:00",
            "span_data": {"type": "function", "name": "read_file", "data": "private"},
        }
    )

    payload = store.telemetry_records()[0]["payload"]
    assert payload["span_data"] == {"type": "function", "name": "read_file"}
