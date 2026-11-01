import os
import ssl
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from jarvis.credential_gate import CredentialGate
from jarvis.execution import CommandRequest, ShuruExecutor


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("JARVIS_TEST_SHURU") != "1", reason="Requires initialized Shuru and Squid"
)
async def test_vm_uses_phantom_credential_and_cannot_access_other_paths(
    tmp_path: Path,
) -> None:
    certificate = tmp_path / "cert.pem"
    private_key = tmp_path / "key.pem"
    subprocess.run(
        [
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
            "/CN=localhost",
            "-addext",
            "subjectAltName=DNS:localhost",
        ],
        check=True,
        capture_output=True,
    )
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"route-ok")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(certificate), str(private_key))
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    script = (
        'printf "%s\\n" "$OPENAI_API_KEY"; '
        'curl --noproxy "*" -fsS -H "Authorization: Bearer $OPENAI_API_KEY" "$OPENAI_BASE_URL/models"; '
        'curl --noproxy "*" -sS -o /dev/null -w "\\n%{http_code}\\n" '
        '-H "Authorization: Bearer $OPENAI_API_KEY" "$OPENAI_BASE_URL/forbidden"'
    )
    try:
        executor = ShuruExecutor(
            credential_factory=lambda token, secret, port, ca: CredentialGate(
                token,
                secret,
                upstream_base=f"https://localhost:{server.server_port}",
                ca_file=certificate,
            )
        )
        result = await executor.execute(
            CommandRequest(
                ("/bin/sh", "-c", script),
                workspace,
                30,
                False,
                ("example.com",),
                "openai",
                "synthetic-secret-only",
            )
        )
        assert result["exit_code"] == 0, result
        assert "[EPHEMERAL TOKEN]" in result["stdout"]
        assert "route-ok" in result["stdout"] and "429" in result["stdout"]
        assert "synthetic-secret-only" not in result["stdout"] + result["stderr"]
        assert seen == [("/v1/models", "Bearer synthetic-secret-only")]
        assert any(event["decision"] == "allow" for event in result["credential_audit"])
        assert any(event["decision"] == "deny" for event in result["credential_audit"])
        assert "synthetic-secret-only" not in str(result["credential_audit"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("JARVIS_TEST_NETWORK") != "1",
    reason="Requires Shuru, Squid, and public internet",
)
async def test_synthetic_credential_is_filtered_through_https_proxy(tmp_path: Path) -> None:
    secret = "synthetic-test-only-not-a-real-credential"
    executor = ShuruExecutor(
        credential_factory=lambda token, key, port, ca: CredentialGate(
            token, key, proxy_port=port, ca_file=ca, upstream_base="https://httpbin.org/anything"
        )
    )
    script = 'curl --noproxy "*" -fsS -H "Authorization: Bearer $OPENAI_API_KEY" "$OPENAI_BASE_URL/models"'
    result = await executor.execute(
        CommandRequest(
            ("/bin/sh", "-c", script), tmp_path, 30, False, ("httpbin.org",), "openai", secret
        )
    )
    assert result["exit_code"] == 0, result.get("credential_audit")
    assert secret not in result["stdout"] + result["stderr"]
    assert "[REDACTED]" in result["stdout"]
