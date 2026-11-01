"""Create a bounded workspace copy inside a nono-restricted child."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

EXCLUDED_DIRS = {".git", ".ssh", ".aws", ".gnupg", ".kube", ".venv", "node_modules"}
EXCLUDED_FILES = {
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git-credentials",
    "credentials",
    "credentials.json",
}
MAX_FILES = 20000
MAX_BYTES = 512 * 1024 * 1024


def main() -> None:
    try:
        import nono_py

        request = json.load(sys.stdin)
        source = Path(request["source"])
        dest = Path(request["destination"])
        if dest.resolve().is_relative_to(source.resolve()):
            raise ValueError("Snapshot destination overlaps workspace")
        caps = nono_py.CapabilitySet()
        caps.allow_path(str(source), nono_py.AccessMode.READ)
        caps.allow_path(str(dest), nono_py.AccessMode.READ_WRITE)
        caps.block_network()
        nono_py.apply(caps)
        count = size = skipped = 0
        for current, directories, files in os.walk(source, followlinks=False):
            current_path = Path(current)
            kept = []
            for name in directories:
                path = current_path / name
                if name in EXCLUDED_DIRS or path.is_symlink():
                    skipped += 1
                else:
                    kept.append(name)
            directories[:] = kept
            relative = current_path.relative_to(source)
            output = dest / relative
            output.mkdir(parents=True, exist_ok=True)
            for name in files:
                lower = name.lower()
                if (
                    lower.startswith(".env")
                    or lower in EXCLUDED_FILES
                    or lower.endswith((".pem", ".key", ".p12", ".pfx"))
                ):
                    skipped += 1
                    continue
                path = current_path / name
                try:
                    if not stat.S_ISREG(os.lstat(path).st_mode):
                        skipped += 1
                        continue
                    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                except OSError:
                    skipped += 1
                    continue
                try:
                    info = os.fstat(fd)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        skipped += 1
                        continue
                    count += 1
                    if count > MAX_FILES or size + info.st_size > MAX_BYTES:
                        raise ValueError(
                            "Workspace exceeds the snapshot limit (20,000 files or 512 MiB)"
                        )
                    with os.fdopen(fd, "rb") as source_file:
                        fd = -1
                        with (output / name).open("xb") as destination_file:
                            while chunk := source_file.read(1024 * 1024):
                                size += len(chunk)
                                if size > MAX_BYTES:
                                    raise ValueError(
                                        "Workspace exceeds the snapshot limit (512 MiB)"
                                    )
                                destination_file.write(chunk)
                    (output / name).chmod(info.st_mode & 0o777)
                finally:
                    if fd != -1:
                        os.close(fd)
        print(json.dumps({"ok": True, "files": count, "bytes": size, "skipped": skipped}))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))


if __name__ == "__main__":
    main()
