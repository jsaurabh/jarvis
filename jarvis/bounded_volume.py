"""Temporary size-limited macOS workspace volume for VM output."""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class VolumeError(RuntimeError):
    pass


def detach_volume(directory: Path) -> None:
    """Detach only the mountpoint owned by this staging directory."""
    mount = directory / "volume"
    if not os.path.ismount(mount):
        return
    info = subprocess.run(
        ["/usr/bin/hdiutil", "info", "-plist"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        timeout=30,
        check=True,
    )
    images = plistlib.loads(info.stdout).get("images", [])
    image = str(directory / "workspace.sparseimage")
    owned = any(
        entry.get("image-path") == image
        and any(
            entity.get("mount-point") == str(mount) for entity in entry.get("system-entities", [])
        )
        for entry in images
    )
    if not owned:
        raise VolumeError("Refusing to detach an unrecognized workspace volume")
    result = subprocess.run(
        ["/usr/bin/hdiutil", "detach", str(mount), "-quiet"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if result.returncode != 0 or os.path.ismount(mount):
        raise VolumeError("Unable to detach the temporary workspace volume")


@contextmanager
def bounded_volume(directory: Path, size_mb: int = 640) -> Iterator[Path]:
    """Attach a bounded sparse image; callers must hold the staging lease."""
    if sys.platform != "darwin" or not 32 <= size_mb <= 1024:
        raise VolumeError("A supported macOS workspace volume size is required")
    mount = directory / "volume"
    image = directory / "workspace.sparseimage"
    mount.mkdir(mode=0o700)
    try:
        subprocess.run(
            [
                "/usr/bin/hdiutil",
                "create",
                "-size",
                f"{size_mb}m",
                "-fs",
                "HFS+",
                "-type",
                "SPARSE",
                "-volname",
                "Jarvis Workspace",
                str(image),
                "-quiet",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=60,
            check=True,
        )
        subprocess.run(
            [
                "/usr/bin/hdiutil",
                "attach",
                str(image),
                "-mountpoint",
                str(mount),
                "-nobrowse",
                "-noverify",
                "-quiet",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=60,
            check=True,
        )
        if not os.path.ismount(mount):
            raise VolumeError("Temporary workspace volume was not mounted")
        yield mount
    finally:
        detach_volume(directory)
