"""Private, leased temporary directories with crash recovery."""

from __future__ import annotations

import fcntl
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path

MAGIC = b"jarvis-staging-v1\n"


def _root() -> Path:
    base = Path("/private/tmp") if sys.platform == "darwin" else Path("/tmp")
    root = base / f"jarvis-sandbox-{os.getuid()}"
    root.mkdir(mode=0o700, exist_ok=True)
    metadata = root.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        raise RuntimeError("Unsafe sandbox staging directory")
    return root


def cleanup_orphans(root: Path) -> None:
    for directory in root.iterdir():
        if (
            not directory.name.startswith("task-")
            or directory.is_symlink()
            or not directory.is_dir()
        ):
            continue
        try:
            fd = os.open(directory / ".jarvis-lease", os.O_RDWR | os.O_NOFOLLOW)
        except OSError:
            continue
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                continue
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                continue
            if os.read(fd, len(MAGIC)) != MAGIC:
                continue
            from .bounded_volume import detach_volume

            detach_volume(directory)
            shutil.rmtree(directory)
        finally:
            os.close(fd)


def recover_orphans() -> None:
    cleanup_orphans(_root())


@contextmanager
def staging() -> Iterator[Path]:
    root = _root()
    cleanup_orphans(root)
    directory = Path(tempfile.mkdtemp(prefix="task-", dir=root))
    try:
        lease = os.open(directory / ".jarvis-lease", os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    except BaseException:
        shutil.rmtree(directory)
        raise
    try:
        fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.write(lease, MAGIC)
        yield directory
    finally:
        try:
            from .bounded_volume import detach_volume

            detach_volume(directory)
            with suppress(FileNotFoundError):
                shutil.rmtree(directory)
        finally:
            os.close(lease)
