"""Keep task subprocesses in a private group and terminate them if their broker dies."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time


def main() -> None:
    if len(sys.argv) < 2 or os.getpgrp() != os.getpid():
        raise SystemExit("Supervisor requires a command and a private process group")
    parent = os.getppid()
    if parent == 1:
        raise SystemExit("Broker is no longer running")

    def watch_parent() -> None:
        while True:
            time.sleep(0.25)
            if os.getppid() != parent:
                os.killpg(os.getpgrp(), signal.SIGKILL)

    threading.Thread(target=watch_parent, daemon=True).start()
    arguments = sys.argv[1:]
    inherited: tuple[int, ...] = ()
    if len(arguments) >= 3 and arguments[0] == "--pass-fd":
        descriptor = int(arguments[1])
        if descriptor < 3:
            raise SystemExit("Invalid inherited descriptor")
        inherited = (descriptor,)
        arguments = arguments[2:]
    process = subprocess.Popen(arguments, pass_fds=inherited)
    status = process.wait()
    if status < 0:
        os.kill(os.getpid(), -status)
    raise SystemExit(status)


if __name__ == "__main__":
    main()
