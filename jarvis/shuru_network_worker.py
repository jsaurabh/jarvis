"""Confine the Shuru host process to one local proxy port."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def main() -> None:
    import nono_py

    request = json.load(sys.stdin)
    caps = nono_py.CapabilitySet()
    for path in ("/usr", "/bin", "/System", "/opt/homebrew/bin", "/opt/homebrew/Cellar/shuru"):
        if Path(path).exists():
            caps.allow_path(path, nono_py.AccessMode.READ)
    for path in ("/dev/null", "/dev/urandom", "/dev/autofs_nowait"):
        if Path(path).exists():
            caps.allow_file(path, nono_py.AccessMode.READ_WRITE)
    caps.allow_path("/dev/fd", nono_py.AccessMode.READ_WRITE)
    caps.allow_path(request["temporary"], nono_py.AccessMode.READ_WRITE)
    caps.allow_path(str(Path.home() / ".local/share/shuru"), nono_py.AccessMode.READ_WRITE)
    caps.platform_rule(
        '(allow generic-issue-extension (extension-class "com.apple.virtualization.extension.fuse"))'
    )
    caps.platform_rule(
        '(allow file-issue-extension (extension-class "com.apple.app-sandbox.read") '
        + "(subpath "
        + json.dumps(str(Path(request["temporary"]).resolve()))
        + "))"
    )
    caps.platform_rule(
        '(allow file-issue-extension (extension-class "com.apple.app-sandbox.read-write") '
        + "(subpath "
        + json.dumps(str(Path(request["temporary"]).resolve()))
        + "))"
    )
    caps.block_network()
    for port in request["proxy_ports"]:
        caps.allow_localhost_port(port)
    nono_py.apply(caps)
    os.chdir(request["cwd"])
    os.execve(
        request["argv"][0], request["argv"], {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin"}
    )


if __name__ == "__main__":
    main()
