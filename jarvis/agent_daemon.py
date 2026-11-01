"""Entry point for a confined daemon launched by a trusted supervisor."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import daemon
from .agent_sandbox import confine_agent


def main() -> None:
    if os.environ.get("JARVIS_CONFINED_AGENT") != "1" or "JARVIS_BROKER_FD" not in os.environ:
        raise RuntimeError("A confined agent must be launched by its broker supervisor")
    state = Path(os.environ["JARVIS_STATE_DIR"]).resolve(strict=True)
    if not daemon.settings.jarvis_db_path.resolve().is_relative_to(state):
        raise RuntimeError("Agent database must be inside the assigned state directory")
    if not daemon.settings.jarvis_skills_path.resolve().is_relative_to(state):
        raise RuntimeError("Agent skills must be inside the assigned state directory")
    if not daemon.SESSION_FILE.resolve().is_relative_to(state):
        raise RuntimeError("Agent session must be inside the assigned state directory")
    if (
        daemon.settings.jarvis_trace_mode != "off"
        and os.environ.get("JARVIS_PHOENIX_MANAGED_EXTERNALLY") != "1"
    ):
        raise RuntimeError("Confined agent tracing requires supervisor-managed Phoenix")
    paths = {
        Path(__file__).resolve().parent,
        Path(sys.prefix).resolve(),
        Path(sys.base_prefix).resolve(),
        Path("/usr/lib"),
        Path("/usr/share"),
        Path("/System"),
    }
    paths.update(path for path in (Path("/dev/null"), Path("/dev/urandom")) if path.exists())
    proxy_value = os.environ.get("JARVIS_MODEL_GATE_PORT")
    if proxy_value is not None and not proxy_value.isdecimal():
        raise RuntimeError("Invalid supervisor model proxy port")
    trace_port = (
        daemon.settings.jarvis_phoenix_port if daemon.settings.jarvis_trace_mode != "off" else None
    )
    confine_agent(
        tuple(sorted(paths)),
        state,
        int(proxy_value) if proxy_value else None,
        trace_port,
        (daemon.settings.jarvis_skills_path,),
        tuple(
            state / name
            for name in ("phoenix.db", "phoenix.db-wal", "phoenix.db-shm", "phoenix.log")
        ),
    )
    daemon.main()


if __name__ == "__main__":
    main()
