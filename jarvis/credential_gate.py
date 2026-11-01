"""One-request budget in front of the credential injection proxy."""

from __future__ import annotations

import asyncio
import json
import secrets
import ssl
from contextlib import suppress
from pathlib import Path
from typing import Any

import h11
import httpx

MAX_REQUEST_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_TOKENS = 2048


class CredentialGate:
    def __init__(
        self,
        token: str,
        credential: str,
        *,
        proxy_port: int | None = None,
        ca_file: Path | None = None,
        upstream_base: str = "https://api.openai.com",
    ) -> None:
        self.token = token
        self.credential = credential
        self.proxy_port = proxy_port
        self.ca_file = ca_file
        self.upstream_base = upstream_base
        self.server: asyncio.Server | None = None
        self.used = False
        self.tasks: set[asyncio.Task[Any]] = set()
        self.audit: list[dict[str, Any]] = []

    async def start(self) -> int:
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        assert self.server.sockets
        return self.server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self.server:
            self.server.close()
            await self.server.wait_closed()
        for task in list(self.tasks):
            task.cancel()
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        self.tasks.add(task)
        connection = h11.Connection(h11.SERVER, max_incomplete_event_size=16384)
        response_started = False

        async def reject(status: int) -> None:
            nonlocal response_started
            writer.write(
                connection.send(
                    h11.Response(
                        status_code=status,
                        headers=[(b"connection", b"close"), (b"content-length", b"0")],
                    )
                )
            )
            writer.write(connection.send(h11.EndOfMessage()))
            response_started = True
            self.audit.append({"decision": "deny", "status": status})
            await writer.drain()

        try:
            request = None
            body = bytearray()
            while True:
                event = connection.next_event()
                if event is h11.NEED_DATA:
                    data = await asyncio.wait_for(reader.read(65536), 10)
                    connection.receive_data(data)
                    if not data:
                        return
                elif isinstance(event, h11.Request):
                    request = event
                    auth = next(
                        (value for key, value in event.headers if key.lower() == b"authorization"),
                        b"",
                    )
                    if not secrets.compare_digest(auth, ("Bearer " + self.token).encode()):
                        await reject(403)
                        return
                    if self.used:
                        await reject(429)
                        return
                    self.used = True
                    if (event.method, event.target) not in {
                        (b"GET", b"/openai/v1/models"),
                        (b"POST", b"/openai/v1/responses"),
                    }:
                        await reject(403)
                        return
                elif isinstance(event, h11.Data):
                    body.extend(event.data)
                    if len(body) > MAX_REQUEST_BYTES:
                        await reject(413)
                        return
                elif isinstance(event, h11.EndOfMessage):
                    break
                elif isinstance(event, h11.ConnectionClosed):
                    return
            if request is None:
                return
            if request.method == b"POST":
                try:
                    data = json.loads(body)
                    if not isinstance(data, dict) or set(data) - {
                        "model",
                        "input",
                        "instructions",
                        "max_output_tokens",
                        "store",
                    }:
                        raise ValueError
                    if not isinstance(data.get("model"), str) or not isinstance(
                        data.get("input"), str
                    ):
                        raise ValueError
                    if "instructions" in data and not isinstance(data["instructions"], str):
                        raise ValueError
                    limit = data.get("max_output_tokens", MAX_OUTPUT_TOKENS)
                    if (
                        isinstance(limit, bool)
                        or not isinstance(limit, int)
                        or not 1 <= limit <= MAX_OUTPUT_TOKENS
                    ):
                        raise ValueError
                    if data.get("store", False) is not False:
                        raise ValueError
                    data["max_output_tokens"] = limit
                    data["store"] = False
                    body = bytearray(json.dumps(data, separators=(",", ":")).encode())
                except (ValueError, TypeError):
                    await reject(400)
                    return
            headers = {
                "Authorization": "Bearer " + self.credential,
                "Content-Type": "application/json",
            }
            url = self.upstream_base + request.target.decode("ascii").removeprefix("/openai")
            proxy = f"http://127.0.0.1:{self.proxy_port}" if self.proxy_port else None
            verification = (
                ssl.create_default_context(cafile=str(self.ca_file))
                if self.ca_file
                else ssl.create_default_context()
            )
            if self.ca_file and self.proxy_port:
                # Squid-generated leaf certificates omit AKI; the local CA is per-task.
                # Squid independently validates the actual upstream certificate.
                verification.verify_flags &= ~ssl.VERIFY_X509_STRICT
            async with (
                httpx.AsyncClient(
                    trust_env=False,
                    timeout=120,
                    follow_redirects=False,
                    proxy=proxy,
                    verify=verification,
                ) as client,
                client.stream(
                    request.method.decode("ascii"), url, headers=headers, content=bytes(body)
                ) as upstream,
            ):
                chunks = bytearray()
                async for chunk in upstream.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > MAX_RESPONSE_BYTES:
                        await reject(502)
                        return
                safe_body = bytes(chunks).replace(self.credential.encode(), b"[REDACTED]")
                response_headers = [
                    (b"connection", b"close"),
                    (b"content-length", str(len(safe_body)).encode()),
                ]
                if content_type := upstream.headers.get("content-type"):
                    response_headers.append((b"content-type", content_type.encode("latin-1")))
                writer.write(
                    connection.send(
                        h11.Response(status_code=upstream.status_code, headers=response_headers)
                    )
                )
                response_started = True
                self.audit.append(
                    {
                        "decision": "allow",
                        "method": request.method.decode("ascii"),
                        "status": upstream.status_code,
                    }
                )
                writer.write(connection.send(h11.Data(data=safe_body)))
                writer.write(connection.send(h11.EndOfMessage()))
                await writer.drain()
        except (OSError, TimeoutError, h11.RemoteProtocolError, httpx.HTTPError) as exc:
            self.audit.append({"decision": "error", "error_type": type(exc).__name__})
            if not response_started:
                with suppress(Exception):
                    await reject(502)
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            self.tasks.discard(task)
