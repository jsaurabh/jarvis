"""One-shot supervised broker; receives approved credentials over its private pipe."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
from pathlib import Path

from .broker import ExecutionBroker
from .config import Settings
from .execution import NonoFileExecutor, ShuruExecutor


async def run(request: dict) -> dict:
    settings = Settings(
        _env_file=None,
        openai_api_key=request.get("api_key", ""),
        jarvis_allowed_roots=[Path(root) for root in request["roots"]],
        jarvis_enable_writes=request["writes"],
        jarvis_enable_commands=request["commands"],
        jarvis_tool_output_limit=request["output_limit"],
    )
    broker = ExecutionBroker(
        settings, NonoFileExecutor(), ShuruExecutor(output_limit=settings.jarvis_tool_output_limit)
    )
    if request["kind"] == "file":
        return await broker.file(**request["arguments"])
    if request["kind"] == "command":
        return await broker.command(**request["arguments"])
    if request["kind"] == "native":
        return await broker.native(**request["arguments"])
    raise ValueError("Unsupported broker request")


async def main() -> None:
    task = asyncio.current_task()
    assert task is not None
    asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, task.cancel)
    parent_pid = os.getppid()

    async def watch_parent() -> None:
        while True:
            await asyncio.sleep(0.25)
            if os.getppid() != parent_pid:
                task.cancel()
                return

    watcher = asyncio.create_task(watch_parent())
    try:
        request = json.load(sys.stdin)
        result = await run(request)
        print(json.dumps({"ok": True, "result": result}))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
    finally:
        watcher.cancel()


if __name__ == "__main__":
    asyncio.run(main())
