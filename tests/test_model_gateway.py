import asyncio
import ssl
from pathlib import Path

import httpx
import pytest

from jarvis.model_gateway import ModelGateway


@pytest.mark.asyncio
async def test_gateway_limits_endpoint_and_redacts_key_across_stream_chunks(monkeypatch) -> None:
    import jarvis.model_gateway as module

    real_client = httpx.AsyncClient
    key = "sk-secret-example-key"
    observed = []

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first sk-secret-"
            await asyncio.sleep(0)
            yield b"example-key last"

    async def upstream(request: httpx.Request) -> httpx.Response:
        observed.append(request.headers.get("authorization"))
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks())

    def upstream_client(**kwargs):
        assert kwargs.get("proxy") == "http://127.0.0.1:12345"
        return real_client(transport=httpx.MockTransport(upstream), trust_env=False)

    monkeypatch.setattr(module.httpx, "AsyncClient", upstream_client)
    certificate = ssl.get_default_verify_paths().cafile
    assert certificate is not None
    gateway = ModelGateway("phantom", key, 12345, Path(certificate))
    port = await gateway.start()
    try:
        async with real_client(trust_env=False) as client:
            base = f"http://127.0.0.1:{port}"
            denied = await client.post(
                base + "/v1/responses",
                headers={"Authorization": "Bearer wrong"},
                json={"model": "test"},
            )
            assert denied.status_code == 403
            denied = await client.get(
                base + "/v1/models", headers={"Authorization": "Bearer phantom"}
            )
            assert denied.status_code == 403
            result = await client.post(
                base + "/v1/responses",
                headers={"Authorization": "Bearer phantom"},
                json={"model": "test"},
            )
            assert result.status_code == 200
            assert key not in result.text
            assert result.text == "first [REDACTED] last"
            assert observed == ["Bearer " + key]
    finally:
        await gateway.close()
