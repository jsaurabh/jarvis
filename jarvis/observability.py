from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any

from openinference.instrumentation import TraceConfig, using_attributes
from openinference.instrumentation.openai_agents import OpenAIAgentsInstrumentor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from .config import Settings


class PhoenixServer:
    """Own a self-hosted Phoenix process bound exclusively to localhost."""

    def __init__(self, database_path: Path, port: int) -> None:
        self.database_path = database_path.expanduser().resolve()
        self.port = port
        self.process: subprocess.Popen[bytes] | None = None
        self._log: Any = None
        self._owned = False

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    @property
    def trace_endpoint(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1/traces"

    @property
    def healthy(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.url}healthz", timeout=0.5) as response:
                return response.status == 200
        except (OSError, urllib.error.URLError):
            return False

    def command(self) -> list[str]:
        database_url = f"sqlite:///{self.database_path.as_posix()}"
        return [
            sys.executable,
            "-m",
            "jarvis.phoenix_local",
            "serve",
            "--host",
            "127.0.0.1",
            "--port",
            str(self.port),
            "--database-url",
            database_url,
            "--no-internet",
        ]

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        for key in list(environment):
            if any(token in key.upper() for token in ("API_KEY", "TOKEN", "PASSWORD", "SECRET")):
                environment.pop(key, None)
        environment.update(
            {
                "PHOENIX_TELEMETRY_ENABLED": "false",
                "PHOENIX_ALLOW_EXTERNAL_RESOURCES": "false",
                "PHOENIX_ENABLE_MCP_SERVER": "false",
                "NO_PROXY": "127.0.0.1,localhost",
            }
        )
        return environment

    def start(self, timeout: float = 30.0) -> None:
        if self.healthy:
            raise RuntimeError(f"Trace viewer port {self.port} is already in use")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        log_path = self.database_path.with_suffix(".log")
        self._log = log_path.open("ab")
        try:
            self.process = subprocess.Popen(
                [sys.executable, "-m", "jarvis.process_supervisor", *self.command()],
                stdin=subprocess.DEVNULL,
                stdout=self._log,
                stderr=subprocess.STDOUT,
                env=self.environment(),
                start_new_session=True,
            )
            self._owned = True
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if self.healthy and self._owns_listener():
                    logging.info("Local Phoenix trace viewer: %s", self.url)
                    return
                if self.process.poll() is not None:
                    raise RuntimeError(f"Phoenix exited during startup; see {log_path}")
                time.sleep(0.1)
            raise RuntimeError(f"Phoenix did not become ready; see {log_path}")
        except BaseException:
            self.stop()
            raise

    def _owns_listener(self) -> bool:
        if self.process is None or self.process.poll() is not None:
            return False
        result = subprocess.run(
            ["/usr/sbin/lsof", "-nP", "-t", f"-iTCP:{self.port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        try:
            listeners = [int(pid) for pid in result.stdout.splitlines()]
            return bool(listeners) and all(os.getpgid(pid) == self.process.pid for pid in listeners)
        except (ValueError, ProcessLookupError):
            return False

    def stop(self) -> None:
        if self._owned and self.process and self.process.poll() is None:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=2)
        self.process = None
        self._owned = False
        if self._log is not None:
            self._log.close()
            self._log = None


class LocalObservability:
    """OpenInference instrumentation exported via OTLP to local Phoenix only."""

    def __init__(self, settings: Settings) -> None:
        self.enabled = settings.jarvis_trace_mode != "off"
        self.instrumentor: OpenAIAgentsInstrumentor | None = None
        self.provider: TracerProvider | None = None
        if not self.enabled:
            return
        self.provider = TracerProvider(
            resource=Resource.create(
                {
                    "service.name": "jarvis",
                    "service.version": "0.1.0",
                    "deployment.environment.name": "local",
                }
            )
        )
        endpoint = f"http://127.0.0.1:{settings.jarvis_phoenix_port}/v1/traces"
        self.provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint)))
        hide_content = settings.jarvis_trace_mode == "metadata"
        config = TraceConfig(hide_inputs=hide_content, hide_outputs=hide_content)
        self.instrumentor = OpenAIAgentsInstrumentor()
        # Exclusive mode replaces the Agents SDK default processor, preventing
        # hosted trace export while leaving SDK tracing available locally.
        self.instrumentor.instrument(
            tracer_provider=self.provider,
            config=config,
            exclusive_processor=True,
        )

    @contextmanager
    def attributes(self, session_id: str, run_context: dict[str, Any] | None) -> Iterator[None]:
        if not self.enabled:
            yield
            return
        context = run_context or {}
        tags = ["jarvis"]
        if context.get("interface"):
            tags.append(str(context["interface"]))
        with using_attributes(
            session_id=session_id,
            metadata=context,
            tags=tags,
        ):
            yield

    def close(self) -> None:
        if self.instrumentor is not None:
            self.instrumentor.uninstrument()
        if self.provider is not None:
            self.provider.force_flush(timeout_millis=5_000)
            self.provider.shutdown()
        self.provider = None
        self.instrumentor = None
