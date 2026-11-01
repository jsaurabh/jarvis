import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from agents.tracing import get_trace_provider
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from jarvis.config import Settings
from jarvis.observability import LocalObservability, PhoenixServer
from jarvis.tools import ToolExecutor


def test_phoenix_server_is_loopback_only_and_offline(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("SOME_PASSWORD", "secret")
    server = PhoenixServer(tmp_path / "phoenix.db", 6006)

    command = server.command()
    assert command[command.index("--host") + 1] == "127.0.0.1"
    assert command[command.index("--port") + 1] == "6006"
    assert "--no-internet" in command
    assert command[command.index("--database-url") + 1].startswith("sqlite:////")

    environment = server.environment()
    assert "OPENAI_API_KEY" not in environment
    assert "SOME_PASSWORD" not in environment
    assert environment["PHOENIX_TELEMETRY_ENABLED"] == "false"
    assert environment["PHOENIX_ALLOW_EXTERNAL_RESOURCES"] == "false"


def test_observability_can_be_disabled(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        openai_api_key="test",
        jarvis_trace_mode="off",
        jarvis_db_path=tmp_path / "jarvis.db",
    )
    observability = LocalObservability(settings)
    assert observability.enabled is False
    assert observability.provider is None


def test_trace_viewer_refuses_unrelated_service_on_its_port(tmp_path: Path) -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        phoenix = PhoenixServer(tmp_path / "phoenix.db", server.server_port)
        with pytest.raises(RuntimeError, match="already in use"):
            phoenix.start()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
async def test_broker_exports_local_sandbox_span_without_file_content(tmp_path: Path) -> None:
    target = tmp_path / "data.txt"
    target.write_text("PRIVATE_TEST_CONTENT")
    settings = Settings(_env_file=None, jarvis_allowed_roots=[tmp_path])
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    executor = ToolExecutor(settings)
    executor.broker.set_tracer_provider(provider)
    result = await executor.execute(
        "read_file", {"path": str(target), "offset": 0, "max_chars": 100}
    )
    assert result["ok"]
    span = exporter.get_finished_spans()[0]
    assert span.name == "jarvis.broker.execute"
    assert span.attributes["jarvis.execution.backend"] == "nono"
    assert span.attributes["jarvis.execution.outcome"] == "completed"
    assert "PRIVATE_TEST_CONTENT" not in str(span.attributes)
    provider.shutdown()


def test_openinference_replaces_the_hosted_sdk_exporter(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        openai_api_key="test",
        jarvis_trace_mode="full",
        jarvis_phoenix_port=6099,
        jarvis_db_path=tmp_path / "jarvis.db",
    )
    observability = LocalObservability(settings)
    try:
        provider = get_trace_provider()
        processors = provider._multi_processor._processors  # noqa: SLF001
        assert [type(processor).__name__ for processor in processors] == [
            "OpenInferenceTracingProcessor"
        ]
    finally:
        observability.close()
