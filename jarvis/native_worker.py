"""Apply native capabilities before replacing this process with an approved tool."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> None:
    import nono_py

    request = json.load(sys.stdin)
    caps = nono_py.CapabilitySet()
    for path in (
        "/usr/bin",
        "/usr/lib",
        "/usr/share",
        "/bin",
        "/sbin",
        "/System",
        "/Library/Developer",
        "/Applications/Xcode.app",
    ):
        if Path(path).exists():
            caps.allow_path(path, nono_py.AccessMode.READ)
    caps.allow_path(request["cwd"], nono_py.AccessMode.READ_WRITE)
    caps.allow_path(request["temporary"], nono_py.AccessMode.READ_WRITE)
    caps.block_network()
    nono_py.apply(caps)
    os.chdir(request["cwd"])
    argv = request["argv"]
    if argv[0] in {"/usr/bin/swift", "/usr/bin/swiftc", "/usr/bin/clang"}:
        argv[0] = "/Library/Developer/CommandLineTools/usr/bin/" + Path(argv[0]).name
    os.execve(
        argv[0],
        argv,
        {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": request["cwd"],
            "DEVELOPER_DIR": "/Library/Developer/CommandLineTools",
            "TMPDIR": request["temporary"],
            "CLANG_MODULE_CACHE_PATH": request["temporary"],
            "SWIFT_MODULE_CACHE_PATH": request["temporary"],
        },
    )


if __name__ == "__main__":
    main()
