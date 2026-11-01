"""Local streaming Responses API gateway; the agent never receives the API key."""

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

MAX_REQUEST = 8 * 1024 * 1024
MAX_RESPONSE = 64 * 1024 * 1024


class ModelGateway:
    def __init__(
        self,
        token: str,
        key: str,
        proxy_port: int | None = None,
        ca_file: Path | None = None,
        local_backend: Any = None,
    ) -> None:
        if not token or not key:
            raise ValueError("Model gateway credentials are required")
        self.local_backend = local_backend
        self.inference_lock = asyncio.Lock()
        self.token = token
        self.key = key
        self.proxy_port = proxy_port
        self.ca_file = ca_file
        self.server: asyncio.Server | None = None
        self.tasks: set[asyncio.Task[Any]] = set()

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
        assert task
        self.tasks.add(task)
        connection = h11.Connection(h11.SERVER, max_incomplete_event_size=16384)
        started = False

        async def reject(status: int) -> None:
            nonlocal started
            writer.write(
                connection.send(
                    h11.Response(
                        status_code=status,
                        headers=[(b"content-length", b"0"), (b"connection", b"close")],
                    )
                )
            )
            writer.write(connection.send(h11.EndOfMessage()))
            started = True
            await writer.drain()

        try:
            body = bytearray()
            request = None
            while True:
                event = connection.next_event()
                if event is h11.NEED_DATA:
                    data = await asyncio.wait_for(reader.read(65536), 30)
                    connection.receive_data(data)
                    if not data:
                        return
                elif isinstance(event, h11.Request):
                    request = event
                    authorization = next(
                        (
                            value
                            for name, value in event.headers
                            if name.lower() == b"authorization"
                        ),
                        b"",
                    )
                    if not secrets.compare_digest(authorization, ("Bearer " + self.token).encode()):
                        await reject(403)
                        return
                    if (event.method, event.target) != (b"POST", b"/v1/responses"):
                        await reject(403)
                        return
                elif isinstance(event, h11.Data):
                    body.extend(event.data)
                    if len(body) > MAX_REQUEST:
                        await reject(413)
                        return
                elif isinstance(event, h11.EndOfMessage):
                    break
                elif isinstance(event, h11.ConnectionClosed):
                    return
            if request is None:
                return
            try:
                payload = json.loads(body)
                if not isinstance(payload, dict) or not isinstance(payload.get("model"), str):
                    raise ValueError
            except (ValueError, TypeError):
                await reject(400)
                return
            if self.local_backend is not None:
                try:
                    async with self.inference_lock:
                        inference = asyncio.create_task(
                            asyncio.to_thread(self.local_backend.respond, payload)
                        )
                        try:
                            response = await asyncio.shield(inference)
                        except asyncio.CancelledError:
                            await inference
                            raise
                    encoded = json.dumps(response).encode()
                    writer.write(
                        connection.send(
                            h11.Response(
                                status_code=200,
                                headers=[
                                    (b"content-type", b"application/json"),
                                    (b"connection", b"close"),
                                ],
                            )
                        )
                    )
                    started = True
                    writer.write(connection.send(h11.Data(data=encoded)))
                    writer.write(connection.send(h11.EndOfMessage()))
                    await writer.drain()
                except (ValueError, RuntimeError):
                    await reject(422)
                return
            verification = ssl.create_default_context(
                cafile=str(self.ca_file) if self.ca_file else None
            )
            if self.proxy_port and self.ca_file:
                verification.verify_flags &= ~ssl.VERIFY_X509_STRICT
            async with (
                httpx.AsyncClient(
                    trust_env=False,
                    timeout=120,
                    follow_redirects=False,
                    proxy=f"http://127.0.0.1:{self.proxy_port}" if self.proxy_port else None,
                    verify=verification,
                ) as client,
                client.stream(
                    "POST",
                    "https://api.openai.com/v1/responses",
                    headers={
                        "Authorization": "Bearer " + self.key,
                        "Content-Type": "application/json",
                    },
                    content=bytes(body),
                ) as upstream,
            ):
                headers = [(b"connection", b"close")]
                if content_type := upstream.headers.get("content-type"):
                    headers.append((b"content-type", content_type.encode("latin-1")))
                writer.write(
                    connection.send(h11.Response(status_code=upstream.status_code, headers=headers))
                )
                started = True
                secret = self.key.encode()
                pending = bytearray()
                received = 0
                async for chunk in upstream.aiter_bytes():
                    received += len(chunk)
                    if received > MAX_RESPONSE:
                        return
                    pending.extend(chunk)
                    # Keep enough trailing bytes to catch a key split across chunks.
                    while len(pending) >= len(secret):
                        match = pending.find(secret)
                        safe_length = len(pending) - len(secret) + 1
                        if match >= 0 and match < safe_length:
                            data = bytes(pending[:match]) + b"[REDACTED]"
                            del pending[: match + len(secret)]
                        else:
                            data = bytes(pending[:safe_length])
                            del pending[:safe_length]
                        if data:
                            writer.write(connection.send(h11.Data(data=data)))
                            await writer.drain()
                tail = bytes(pending).replace(secret, b"[REDACTED]")
                if tail:
                    writer.write(connection.send(h11.Data(data=tail)))
                writer.write(connection.send(h11.EndOfMessage()))
                await writer.drain()
        except (OSError, TimeoutError, h11.RemoteProtocolError, httpx.HTTPError):
            if not started:
                with suppress(Exception):
                    await reject(502)
        finally:
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            self.tasks.discard(task)
