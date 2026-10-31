from __future__ import annotations

import argparse
import json
import logging
import threading
from pathlib import Path
from typing import Any

from agents import TracingProcessor

from .store import TraceStore


class LocalTelemetryProcessor(TracingProcessor):
    """Mirror completed Agents SDK traces and spans into Jarvis's local trace store."""

    def __init__(self, store: TraceStore) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._run_ids: dict[str, str] = {}

    def on_trace_start(self, trace: Any) -> None:
        try:
            payload = trace.export() or {}
            metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            run_id = metadata.get("run_id")
            if isinstance(run_id, str) and run_id:
                with self._lock:
                    self._run_ids[str(payload.get("id", ""))] = run_id
        except Exception:
            logging.warning("Jarvis could not correlate an SDK trace", exc_info=False)

    def on_trace_end(self, trace: Any) -> None:
        try:
            payload = trace.export() or {}
            trace_id = str(payload.get("id", ""))
            with self._lock:
                run_id = self._run_ids.pop(trace_id, None)
            self.store.record_telemetry(payload, run_id)
        except Exception:
            logging.warning("Jarvis could not store an SDK trace", exc_info=False)

    def on_span_start(self, span: Any) -> None:
        del span

    def on_span_end(self, span: Any) -> None:
        try:
            payload = span.export() or {}
            with self._lock:
                run_id = self._run_ids.get(str(payload.get("trace_id", "")))
            self.store.record_telemetry(payload, run_id)
        except Exception:
            logging.warning("Jarvis could not store an SDK span", exc_info=False)

    def shutdown(self) -> None:
        with self._lock:
            self._run_ids.clear()

    def force_flush(self) -> None:
        return


def export_telemetry(store: TraceStore, output: Path, run_id: str | None = None) -> int:
    records = store.telemetry_records(run_id)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    return len(records)


def main() -> None:
    from .config import settings

    parser = argparse.ArgumentParser(description="Inspect or export local Jarvis telemetry")
    parser.add_argument("action", choices=["report", "export"])
    parser.add_argument("--db", type=Path, default=settings.jarvis_db_path)
    parser.add_argument("--output", type=Path, default=Path("exports/jarvis-telemetry.jsonl"))
    parser.add_argument("--run-id")
    args = parser.parse_args()
    store = TraceStore(args.db, "full")
    if args.action == "report":
        print(json.dumps(store.telemetry_metrics(), indent=2, sort_keys=True))
        return
    count = export_telemetry(store, args.output, args.run_id)
    print(f"Wrote {count} telemetry records to {args.output}")


if __name__ == "__main__":
    main()
