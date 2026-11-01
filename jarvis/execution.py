from __future__ import annotations

import asyncio
import json
import os
import secrets
import shlex
import shutil
import signal
import sys
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .staging import staging


class ExecutionUnavailable(ValueError):
    """The requested isolation could not be established."""


@dataclass(frozen=True)
class FileRequest:
    operation: str
    arguments: dict[str, Any]
    root: Path


class FileExecutor(Protocol):
    async def execute(self, request: FileRequest) -> dict[str, Any]: ...


@dataclass(frozen=True)
class CommandRequest:
    argv: tuple[str, ...]
    cwd: Path
    timeout_seconds: int
    preview_changes: bool = False
    network_hosts: tuple[str, ...] = ()
    credential: str = ""
    credential_secret: str = field(default="", repr=False)


class CommandExecutor(Protocol):
    async def execute(self, request: CommandRequest) -> dict[str, Any]: ...


class NonoCommandExecutor:
    """Run a pre-approved native executable with a fresh nono sandbox."""

    async def execute(self, request: CommandRequest) -> dict[str, Any]:
        with staging() as temporary:
            scratch = temporary / "scratch"
            scratch.mkdir(mode=0o700)
            return await self._execute(request, str(scratch))

    async def _execute(self, request: CommandRequest, temporary: str) -> dict[str, Any]:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "jarvis.process_supervisor",
            sys.executable,
            "-m",
            "jarvis.native_worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1", "JARVIS_WORKER_PROCESS": "1"},
        )

        async def collect() -> tuple[bytes, bytes]:
            assert process.stdin and process.stdout and process.stderr
            process.stdin.write(
                json.dumps(
                    {"argv": request.argv, "cwd": str(request.cwd), "temporary": temporary}
                ).encode()
            )
            await process.stdin.drain()
            process.stdin.close()

            async def bounded(stream: asyncio.StreamReader) -> bytes:
                result = bytearray()
                while data := await stream.read(65536):
                    result.extend(data[: max(0, 40000 - len(result))])
                return bytes(result)

            stdout, stderr, _ = await asyncio.gather(
                bounded(process.stdout), bounded(process.stderr), process.wait()
            )
            return stdout, stderr

        try:
            stdout, stderr = await asyncio.wait_for(collect(), request.timeout_seconds)
        except BaseException as exc:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            if isinstance(exc, TimeoutError):
                raise ExecutionUnavailable("Native command timed out") from exc
            raise
        from .security import redact_text

        return {
            "exit_code": process.returncode,
            "stdout": redact_text(stdout.decode(errors="replace")[:40000]),
            "stderr": redact_text(stderr.decode(errors="replace")[:40000]),
        }


class ShuruExecutor:
    """Disposable, offline guest with a read-only host mount and guest overlay."""

    def __init__(
        self,
        binary: Path = Path("/opt/homebrew/bin/shuru"),
        output_limit: int = 40000,
        version: str = "0.11.0",
        credential_factory: Any = None,
        patch_store: Any = None,
    ):
        self.binary = binary
        self.output_limit = output_limit
        self.version = version
        self.credential_factory = credential_factory
        self.patch_store = patch_store

    async def execute(self, request: CommandRequest) -> dict[str, Any]:
        if not self.binary.is_file():
            raise ExecutionUnavailable("Shuru is not installed")
        try:
            version_process = await asyncio.create_subprocess_exec(
                str(self.binary),
                "--version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={"PATH": "/usr/bin:/bin"},
            )
        except OSError as exc:
            raise ExecutionUnavailable("Unable to start Shuru version check") from exc
        try:
            version_output, _ = await asyncio.wait_for(version_process.communicate(), 5)
        except BaseException as exc:
            if version_process.returncode is None:
                version_process.kill()
                await version_process.wait()
            if isinstance(exc, TimeoutError):
                raise ExecutionUnavailable("Unable to verify Shuru version") from exc
            raise
        if (
            version_process.returncode != 0
            or version_output.decode(errors="replace").strip() != f"shuru {self.version}"
        ):
            raise ExecutionUnavailable(f"Shuru version mismatch; expected {self.version}")
        if ":" in str(request.cwd):
            raise ExecutionUnavailable("Workspace paths containing colons are unsupported")
        async with AsyncExitStack() as cleanup:
            directory = str(cleanup.enter_context(staging()))
            config = Path(directory) / "shuru.json"
            config.write_text("{}", encoding="utf-8")
            workspace = Path(directory) / "workspace"
            workspace.mkdir(mode=0o700)
            snapshot = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "jarvis.process_supervisor",
                sys.executable,
                "-m",
                "jarvis.snapshot_worker",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                env={
                    "PATH": "/usr/bin:/bin",
                    "PYTHONNOUSERSITE": "1",
                    "JARVIS_WORKER_PROCESS": "1",
                },
            )
            try:
                snapshot_stdout, snapshot_stderr = await asyncio.wait_for(
                    snapshot.communicate(
                        json.dumps(
                            {"source": str(request.cwd), "destination": str(workspace)}
                        ).encode()
                    ),
                    60,
                )
            except BaseException:
                if snapshot.returncode is None:
                    with suppress(ProcessLookupError):
                        os.killpg(snapshot.pid, signal.SIGKILL)
                    await snapshot.wait()
                raise
            if snapshot.returncode != 0:
                raise ExecutionUnavailable(
                    "Workspace snapshot failed: " + snapshot_stderr.decode(errors="replace")[-1000:]
                )
            try:
                snapshot_result = json.loads(snapshot_stdout)
                if not snapshot_result["ok"]:
                    raise ExecutionUnavailable(
                        "Workspace snapshot failed: " + snapshot_result["error"]
                    )
            except (json.JSONDecodeError, KeyError, TypeError) as exc:
                raise ExecutionUnavailable("Invalid snapshot worker response") from exc
            host_review_enabled = request.preview_changes
            host_review = None
            review_error = None
            baseline = workspace
            if host_review_enabled:
                from .bounded_volume import bounded_volume

                volume = cleanup.enter_context(bounded_volume(Path(directory)))
                workspace = volume / "workspace"
                shutil.copytree(baseline, workspace, symlinks=False)
            try:
                mounts = [
                    "--mount",
                    f"{workspace}:/workspace:{'rw' if host_review_enabled else 'ro'}",
                ]
                script = 'cd /workspace && exec "$@"'
                network_flags: list[str] = []
                guest_env: list[str] = []
                proxy_port = 0
                gate_port = 0
                credential_token = ""
                runtime = Path(directory) / "runtime"
                if request.network_hosts or request.credential:
                    runtime.mkdir(mode=0o700)
                    mounts.extend(["--mount", f"{runtime}:/run/jarvis:ro"])
                if request.credential:
                    if request.credential != "openai" or not request.credential_secret:
                        raise ExecutionUnavailable("The approved OpenAI credential is unavailable")
                    credential_token = secrets.token_urlsafe(32)
                    credential_file = runtime / "credential.env"
                    credential_file.write_text(
                        "export OPENAI_BASE_URL=http://host.shuru.internal:3129/openai/v1\n"
                        + "export OPENAI_API_KEY="
                        + shlex.quote(credential_token)
                        + "\n",
                        encoding="utf-8",
                    )
                    credential_file.chmod(0o600)
                    script = ". /run/jarvis/credential.env; " + script
                if request.network_hosts or request.credential:
                    from .network_proxy import NetworkProxy

                    proxy = NetworkProxy(Path(directory), request.network_hosts)
                    cleanup.push_async_callback(proxy.close)
                    proxy_port = await proxy.start()
                    if request.credential:
                        from .credential_gate import CredentialGate

                        gate = (
                            self.credential_factory(
                                credential_token,
                                request.credential_secret,
                                proxy_port,
                                Path(directory) / "proxy-cert.pem",
                            )
                            if self.credential_factory
                            else CredentialGate(
                                credential_token,
                                request.credential_secret,
                                proxy_port=proxy_port,
                                ca_file=Path(directory) / "proxy-cert.pem",
                            )
                        )
                        cleanup.push_async_callback(gate.close)
                        gate_port = await gate.start()
                    ca_file = runtime / "proxy-ca.pem"
                    ca_file.write_bytes((Path(directory) / "proxy-cert.pem").read_bytes())
                    network_flags = [
                        "--allow-net",
                        "--allow-host",
                        "never.invalid",
                        "--expose-host",
                        f"{proxy_port}:3128",
                    ]
                    if gate_port:
                        network_flags.extend(["--expose-host", f"{gate_port}:3129"])
                    guest_env = [
                        "/usr/bin/env",
                        "HTTP_PROXY=http://host.shuru.internal:3128",
                        "HTTPS_PROXY=http://host.shuru.internal:3128",
                        "NO_PROXY=host.shuru.internal",
                        "CURL_CA_BUNDLE=/run/jarvis/proxy-ca.pem",
                        "SSL_CERT_FILE=/run/jarvis/proxy-ca.pem",
                        "REQUESTS_CA_BUNDLE=/run/jarvis/proxy-ca.pem",
                        "NODE_EXTRA_CA_CERTS=/run/jarvis/proxy-ca.pem",
                    ]
                command = [
                    str(self.binary),
                    "run",
                    "--config",
                    str(config),
                    "--cpus",
                    "2",
                    "--memory",
                    "2048",
                    "--disk-size",
                    "4096",
                    *(["--allow-host-writes"] if host_review_enabled else []),
                    *mounts,
                    *network_flags,
                    "--",
                    *guest_env,
                    "/bin/sh",
                    "-c",
                    script,
                    "sh",
                    *request.argv,
                ]
                host_argv = (
                    [sys.executable, "-m", "jarvis.shuru_network_worker"] if proxy_port else command
                )
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "jarvis.process_supervisor",
                    *host_argv,
                    cwd=directory,
                    env={
                        "HOME": str(Path.home()),
                        "PATH": "/usr/bin:/bin",
                        "JARVIS_WORKER_PROCESS": "1",
                    },
                    stdin=asyncio.subprocess.PIPE if proxy_port else asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    start_new_session=True,
                )
                if proxy_port:
                    assert process.stdin is not None
                    process.stdin.write(
                        json.dumps(
                            {
                                "argv": command,
                                "cwd": directory,
                                "temporary": directory,
                                "proxy_ports": [port for port in (proxy_port, gate_port) if port],
                            }
                        ).encode()
                    )
                    process.stdin.close()
            except OSError as exc:
                raise ExecutionUnavailable(f"Unable to start Shuru: {exc}") from exc

            async def drain(stream: asyncio.StreamReader) -> tuple[bytes, bool]:
                chunks = bytearray()
                truncated = False
                while data := await stream.read(65536):
                    if len(chunks) + len(data) > self.output_limit:
                        truncated = True
                    chunks.extend(data[: max(0, self.output_limit - len(chunks))])
                return bytes(chunks), truncated

            async def collect() -> tuple[tuple[bytes, bool], tuple[bytes, bool]]:
                assert process.stdout is not None and process.stderr is not None
                stdout, stderr, _ = await asyncio.gather(
                    drain(process.stdout), drain(process.stderr), process.wait()
                )
                return stdout, stderr

            try:
                (stdout, stdout_truncated), (stderr, stderr_truncated) = await asyncio.wait_for(
                    collect(), request.timeout_seconds
                )
            except BaseException as exc:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
                if isinstance(exc, TimeoutError):
                    raise ExecutionUnavailable(
                        f"VM execution timed out after {request.timeout_seconds} seconds"
                    ) from exc
                raise
            credential_audit = gate.audit if gate_port else []
            if host_review_enabled:
                from .patch_review import PatchReviewError, review_workspace

                try:
                    review = review_workspace(baseline, workspace)
                    host_review = {
                        "sha256": review["sha256"],
                        "preview": review["preview"],
                        "changes": [
                            {key: value for key, value in item.items() if key != "after_base64"}
                            for item in review["changes"]
                        ],
                    }
                    if self.patch_store is not None and review["changes"]:
                        try:
                            proposals = self.patch_store.prepare_all(
                                request.cwd, baseline, workspace
                            )
                            host_review["proposals"] = [
                                {key: value for key, value in proposal.items() if key != "preview"}
                                for proposal in proposals
                            ]
                            if len(proposals) == 1:
                                host_review["proposal"] = host_review["proposals"][0]
                        except ValueError as exc:
                            host_review["proposal_error"] = str(exc)
                except (OSError, PatchReviewError) as exc:
                    review_error = str(exc)
        from .security import redact_text

        stdout_text = stdout.decode(errors="replace")
        stderr_text = stderr.decode(errors="replace")
        if credential_token:
            stdout_text = stdout_text.replace(credential_token, "[EPHEMERAL TOKEN]")
            stderr_text = stderr_text.replace(credential_token, "[EPHEMERAL TOKEN]")
        return {
            "exit_code": process.returncode,
            "stdout": redact_text(stdout_text),
            "stderr": redact_text(stderr_text),
            "stdout_truncated": stdout_truncated,
            "stderr_truncated": stderr_truncated,
            "credential_audit": credential_audit,
            "host_review": host_review,
            "review_error": review_error,
            "snapshot": {
                "files": snapshot_result["files"],
                "bytes": snapshot_result["bytes"],
                "excluded": snapshot_result["skipped"],
            },
        }


class NonoFileExecutor:
    """Launch a fresh worker; the worker applies nono before touching user files."""

    async def execute(self, request: FileRequest) -> dict[str, Any]:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "jarvis.process_supervisor",
            sys.executable,
            "-m",
            "jarvis.file_worker",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1", "JARVIS_WORKER_PROCESS": "1"},
            start_new_session=True,
        )
        payload = json.dumps(
            {
                "operation": request.operation,
                "arguments": request.arguments,
                "root": str(request.root),
            }
        ).encode()
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(payload), 30)
        except BaseException:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
            raise
        if process.returncode != 0:
            raise ExecutionUnavailable(
                "Sandboxed file worker failed: " + stderr.decode(errors="replace")[-1000:]
            )
        try:
            response = json.loads(stdout)
            if not response["ok"]:
                raise ExecutionUnavailable(response["error"])
            return response["result"]
        except (ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, ExecutionUnavailable):
                raise
            raise ExecutionUnavailable("Invalid response from sandboxed file worker") from exc
