import asyncio
import json

import httpx
import pytest

from jarvis.credential_gate import CredentialGate


@pytest.mark.asyncio
async def test_one_request_gate_enforces_budget_and_excludes_extra_calls() -> None:
    received = []

    async def upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        headers = await reader.readuntil(b"\r\n\r\n")
        length = next(
            int(line.split(b":", 1)[1])
            for line in headers.split(b"\r\n")
            if line.lower().startswith(b"content-length:")
        )
        received.append(json.loads(await reader.readexactly(length)))
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    gate = CredentialGate(
        "test-token",
        "synthetic-secret",
        upstream_base=f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}",
    )
    port = await gate.start()
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            url = f"http://127.0.0.1:{port}/openai/v1/responses"
            denied = await client.post(url, headers={"Authorization": "Bearer wrong"}, json={})
            assert denied.status_code == 403
            result = await client.post(
                url,
                headers={"Authorization": "Bearer test-token"},
                json={"model": "test", "input": "hello"},
            )
            assert result.status_code == 200 and result.text == "ok"
            assert received == [
                {"model": "test", "input": "hello", "max_output_tokens": 2048, "store": False}
            ]
            again = await client.post(
                url,
                headers={"Authorization": "Bearer test-token"},
                json={"model": "test", "input": "hello"},
            )
            assert again.status_code == 429
    finally:
        await gate.close()
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_gate_rejects_unbounded_or_hosted_requests() -> None:
    gate = CredentialGate("test-token", "synthetic-secret", upstream_base="http://127.0.0.1:1")
    port = await gate.start()
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            result = await client.post(
                f"http://127.0.0.1:{port}/openai/v1/responses",
                headers={"Authorization": "Bearer test-token"},
                json={"model": "test", "input": "hello", "tools": [{"type": "web_search"}]},
            )
            assert result.status_code == 400
    finally:
        await gate.close()
