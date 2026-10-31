from pathlib import Path

from agents.tracing import get_trace_provider

from jarvis.config import Settings
from jarvis.observability import LocalObservability, PhoenixServer


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
