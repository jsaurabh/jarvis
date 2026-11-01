"""Restricted child process for the fixed set of file operations."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from .config import Settings
from .tools import ToolExecutor


def main() -> None:
    try:
        import nono_py

        request = json.load(sys.stdin)
        operation = request["operation"]
        if operation not in {"read_file", "write_file", "list_directory", "search_files"}:
            raise ValueError("Unsupported file operation")
        root = Path(request["root"])
        args = request["arguments"]
        target = (
            Path(args["root" if operation == "search_files" else "path"]).expanduser().resolve()
        )
        if not target.is_relative_to(root):
            raise ValueError("Path is outside the approved root")
        if not nono_py.is_supported():
            raise RuntimeError("nono does not support this platform")
        caps = nono_py.CapabilitySet()
        mode = (
            nono_py.AccessMode.READ_WRITE if operation == "write_file" else nono_py.AccessMode.READ
        )
        if operation == "read_file":
            caps.allow_file(str(target), mode)
        elif operation == "write_file":
            parent = target.parent
            while not parent.exists() and parent != root:
                parent = parent.parent
            caps.allow_path(str(parent), mode)
        else:
            caps.allow_path(str(target), mode)
        caps.block_network()
        nono_py.apply(caps)
        executor = ToolExecutor(Settings(_env_file=None, jarvis_allowed_roots=[root]))
        handler = getattr(executor, "_" + operation)
        result = asyncio.run(handler(**request["arguments"]))
        response = {"ok": True, "result": result}
    except Exception as exc:
        response = {"ok": False, "error": str(exc)}
    print(json.dumps(response))


if __name__ == "__main__":
    main()
