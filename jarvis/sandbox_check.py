"""User-initiated end-to-end sandbox and credential health check."""

from __future__ import annotations

from typing import Any

from .config import Settings
from .staging import staging
from .tools import ToolExecutor


async def verify_sandbox(settings: Settings, provider: Any = None) -> dict[str, str]:
    with staging() as workspace:
        test_settings = settings.model_copy(
            update={"jarvis_allowed_roots": [workspace], "jarvis_enable_commands": True}
        )
        executor = ToolExecutor(test_settings)
        if provider is not None:
            executor.broker.set_tracer_provider(provider)
        outcome = await executor.execute(
            "run_command",
            {
                "argv": [
                    "/bin/sh",
                    "-c",
                    'curl --noproxy "*" -fsS -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $OPENAI_API_KEY" "$OPENAI_BASE_URL/models"',
                ],
                "cwd": str(workspace),
                "timeout_seconds": 30,
                "preview_changes": False,
                "network_hosts": ["api.openai.com"],
                "credential": "openai",
            },
        )
    if not outcome.get("ok"):
        raise ValueError(str(outcome.get("error", "Isolated execution test failed")))
    details = outcome["result"]
    if details.get("exit_code") != 0 or details.get("stdout", "").strip() != "200":
        status = details.get("stdout", "").strip()
        suffix = f" (HTTP {status})" if len(status) == 3 and status.isdecimal() else ""
        raise ValueError(f"OpenAI credential check failed inside the isolated VM{suffix}")
    return {"status": "completed", "message": "Isolated execution and credential access verified."}
