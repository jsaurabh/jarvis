"""OS confinement primitive for an agent worker, applied before untrusted work.

The caller must arrange its broker channel before entering this sandbox. This
primitive alone does not authenticate or authorize broker requests.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def confine_agent(
    read_paths: tuple[Path, ...],
    state_directory: Path,
    model_proxy_port: int | None = None,
    trace_port: int | None = None,
    protected_write_paths: tuple[Path, ...] = (),
    protected_files: tuple[Path, ...] = (),
) -> None:
    """Deny ungranted files, network, and child execution on macOS."""
    import nono_py

    if sys.platform != "darwin" or not nono_py.is_supported():
        raise RuntimeError("Agent confinement requires supported macOS sandboxing")
    state = state_directory.resolve(strict=True)
    if not state.is_dir():
        raise ValueError("Agent state path must be a directory")
    caps = nono_py.CapabilitySet()
    for path in read_paths:
        resolved = path.resolve(strict=True)
        if resolved == Path("/") or state.is_relative_to(resolved):
            raise ValueError("Agent read paths must not cover the state directory or root")
        if resolved.is_dir():
            caps.allow_path(str(resolved), nono_py.AccessMode.READ)
        else:
            caps.allow_file(str(resolved), nono_py.AccessMode.READ)
    caps.allow_path(str(state), nono_py.AccessMode.READ_WRITE)
    for path in protected_write_paths:
        protected = path.resolve(strict=True)
        if not protected.is_relative_to(state):
            raise ValueError("Protected agent paths must be within its state directory")
        caps.platform_rule(f"(deny file-write* (subpath {json.dumps(str(protected))}))")
    for path in protected_files:
        protected = path.resolve(strict=False)
        if not protected.is_relative_to(state):
            raise ValueError("Protected agent files must be within its state directory")
        caps.platform_rule(f"(deny file-read* file-write* (literal {json.dumps(str(protected))}))")
    caps.block_network()
    for port in (model_proxy_port, trace_port):
        if port is not None:
            if not 1 <= port <= 65535:
                raise ValueError("Invalid supervisor service port")
            caps.allow_localhost_port(port)
    caps.platform_rule("(deny process-exec)")
    nono_py.apply(caps)
