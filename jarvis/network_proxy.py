"""Per-task forward proxy with a narrow HTTPS destination policy."""

from __future__ import annotations

import asyncio
import os
import re
import signal
import socket
import sys
from contextlib import suppress
from pathlib import Path

from .execution import ExecutionUnavailable

SQUID = Path("/opt/homebrew/opt/squid/sbin/squid")


def validate_hosts(hosts: list[str]) -> tuple[str, ...]:
    if not hosts or len(hosts) > 10:
        raise ValueError("Provide one to ten approved internet hostnames")
    result = []
    for host in hosts:
        if (
            not isinstance(host, str)
            or len(host) > 253
            or not re.fullmatch(
                r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", host
            )
            or host.endswith((".local", ".localhost", ".internal", ".invalid"))
        ):
            raise ValueError("Only exact public DNS hostnames are supported")
        if host not in result:
            result.append(host)
    return tuple(result)


class NetworkProxy:
    def __init__(self, directory: Path, hosts: tuple[str, ...]) -> None:
        self.directory = directory
        self.hosts = hosts
        self.process: asyncio.subprocess.Process | None = None
        self.port = 0

    @staticmethod
    async def _setup(*argv: str) -> None:
        process = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            code = await asyncio.wait_for(process.wait(), 10)
        except BaseException:
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                await process.wait()
            raise
        if code != 0:
            raise ExecutionUnavailable("Unable to initialize the network proxy")

    async def start(self) -> int:
        if not SQUID.is_file():
            raise ExecutionUnavailable("The approved network proxy is not installed")
        version = await asyncio.create_subprocess_exec(
            str(SQUID),
            "-v",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            output, _ = await asyncio.wait_for(version.communicate(), 5)
        except BaseException:
            if version.returncode is None:
                with suppress(ProcessLookupError):
                    version.kill()
                await version.wait()
            raise
        if version.returncode != 0 or not output.startswith(b"Squid Cache: Version 7.8\n"):
            raise ExecutionUnavailable("The network proxy version must be Squid 7.8")
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        certificate = self.directory / "proxy-cert.pem"
        private_key = self.directory / "proxy-key.pem"
        await self._setup(
            "/usr/bin/openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-keyout",
            str(private_key),
            "-out",
            str(certificate),
            "-days",
            "1",
            "-subj",
            "/CN=Jarvis temporary proxy",
        )
        private_key.chmod(0o600)
        cert_db = self.directory / "cert-db"
        certgen = "/opt/homebrew/opt/squid/libexec/security_file_certgen"
        await self._setup(
            certgen,
            "-c",
            "-s",
            str(cert_db),
            "-M",
            "4MB",
        )
        config = self.directory / "squid.conf"
        config.write_text(
            f"http_port 127.0.0.1:{self.port} ssl-bump tls-cert={certificate} tls-key={private_key} generate-host-certificates=on\n"
            "acl secure_port port 443\n"
            "acl connect method CONNECT\n"
            "acl https proto HTTPS\n"
            "acl blocked dst 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 "
            "169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.0.2.0/24 "
            "192.88.99.0/24 192.168.0.0/16 198.18.0.0/15 198.51.100.0/24 "
            "203.0.113.0/24 224.0.0.0/4 240.0.0.0/4 ::/0\n"
            f"acl approved dstdomain {' '.join(self.hosts)}\n"
            "http_access deny !secure_port\nhttp_access deny blocked\n"
            "http_access allow connect approved\nhttp_access allow https approved\nhttp_access deny all\n"
            f"sslcrtd_program {certgen} -s {cert_db} -M 4MB\nsslcrtd_children 1\n"
            "ssl_bump bump all\nsslproxy_cert_error deny all\n"
            "cache deny all\naccess_log none\n"
            f"cache_log {self.directory / 'squid.log'}\n"
            f"pid_filename {self.directory / 'squid.pid'}\n"
            f"coredump_dir {self.directory}\nvisible_hostname jarvis\n",
            encoding="utf-8",
        )
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "jarvis.process_supervisor",
            str(SQUID),
            "-N",
            "-n",
            "j",
            "-f",
            str(config),
            "-d",
            "0",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=self.directory,
            env={"PATH": "/usr/bin:/bin", "PYTHONNOUSERSITE": "1", "JARVIS_WORKER_PROCESS": "1"},
            start_new_session=True,
        )
        try:
            for _ in range(100):
                if self.process.returncode is not None:
                    log = self.directory / "squid.log"
                    detail = log.read_text(errors="replace")[-2000:] if log.exists() else ""
                    raise ExecutionUnavailable(
                        "The approved network proxy failed to start: " + detail
                    )
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
                    del reader
                    writer.close()
                    await writer.wait_closed()
                    return self.port
                except OSError:
                    await asyncio.sleep(0.05)
            raise ExecutionUnavailable("The approved network proxy did not become ready")
        except BaseException:
            await self.close()
            raise

    async def close(self) -> None:
        if self.process is not None:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 3)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
