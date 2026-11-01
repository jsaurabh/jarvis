"""Supervisor-owned reviewed patch proposals; never exposed as raw host paths to a VM."""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import secrets
import stat
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from .patch_review import MAX_FILE_BYTES, review_workspace
from .security import AccessDenied, require_unprotected, resolve_allowed


class PatchStore:
    """Keep bounded proposals in supervisor memory until reviewed or expired."""

    def __init__(self, roots: tuple[Path, ...]) -> None:
        self.roots = roots
        self._patches: dict[str, tuple[float, Path, dict[str, Any]]] = {}

    def set_roots(self, roots: tuple[Path, ...]) -> None:
        self.roots = roots
        self._patches.clear()

    def describe(self, patch_id: str, digest: str) -> str:
        entry = self._patches.get(patch_id)
        if entry is None or entry[0] <= time.monotonic():
            raise AccessDenied("Patch is unknown or expired")
        if not secrets.compare_digest(entry[2]["sha256"], digest):
            raise AccessDenied("Patch digest does not match the proposal")
        return f"Workspace: {json.dumps(str(entry[1]), ensure_ascii=True)}\n\n{entry[2]['preview']}"

    def prepare(self, target: Path, baseline: Path, modified: Path) -> dict[str, Any]:
        proposals = self.prepare_all(target, baseline, modified)
        if len(proposals) != 1:
            for proposal in proposals:
                self._patches.pop(proposal["patch_id"], None)
            raise AccessDenied("Exactly one changed file can be proposed at a time")
        return proposals[0]

    def prepare_all(self, target: Path, baseline: Path, modified: Path) -> list[dict[str, Any]]:
        root = resolve_allowed(str(target), self.roots)
        require_unprotected(root, recursive=True)
        review = review_workspace(baseline, modified)
        now = time.monotonic()
        self._patches = {key: value for key, value in self._patches.items() if value[0] > now}
        if len(self._patches) + len(review["changes"]) > 10:
            raise AccessDenied("Too many pending patch proposals")
        if any(len(change["preview"].encode("ascii")) > 12_000 for change in review["changes"]):
            raise AccessDenied("Patch is too large to display for approval")
        proposals = []
        for change in review["changes"]:
            manifest = {"version": 1, "changes": [change], "preview": change["preview"]}
            encoded = json.dumps(manifest, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
            digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
            patch_id = secrets.token_urlsafe(24)
            self._patches[patch_id] = (now + 3600, root, {**manifest, "sha256": digest})
            proposals.append({"patch_id": patch_id, "sha256": digest, "preview": change["preview"]})
        return proposals

    def apply(self, patch_id: str, approved_sha256: str) -> dict[str, Any]:
        """Apply one reviewed file with an atomic replace and a preimage check.

        A non-cooperating local writer can race the last check; OS privilege
        separation and app-side coordination are still required by the caller.
        """
        entry = self._patches.pop(patch_id, None)
        if entry is None or entry[0] <= time.monotonic():
            raise AccessDenied("Patch is unknown, expired, or already used")
        _, root, review = entry
        if not secrets.compare_digest(str(review["sha256"]), approved_sha256):
            raise AccessDenied("Patch does not match the approved review")
        # Check the in-memory manifest as well as the approval binding.
        manifest = {key: value for key, value in review.items() if key != "sha256"}
        encoded = json.dumps(manifest, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
        if hashlib.sha256(encoded.encode("utf-8")).hexdigest() != approved_sha256:
            raise AccessDenied("Patch manifest integrity check failed")
        change = review["changes"][0]
        parts = Path(change["path"]).parts
        if (
            not parts
            or any(part in {"", ".", ".."} for part in parts)
            or Path(change["path"]).is_absolute()
        ):
            raise AccessDenied("Invalid patch path")
        target = resolve_allowed(str(root.joinpath(*parts)), self.roots)
        require_unprotected(target)
        if not target.is_relative_to(root):
            raise AccessDenied("Patch escapes its reviewed workspace")
        replacement = None
        if change["after_base64"] is not None:
            replacement = base64.b64decode(change["after_base64"], validate=True)
            if (
                len(replacement) > MAX_FILE_BYTES
                or hashlib.sha256(replacement).hexdigest() != change["after_sha256"]
            ):
                raise AccessDenied("Patch content failed integrity validation")

        directory = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        temporary = ".jarvis-patch-" + secrets.token_hex(16)
        original = -1
        temp_fd = -1
        created_directories: list[tuple[int, str]] = []
        applied = False
        try:
            for part in parts[:-1]:
                try:
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                    )
                except FileNotFoundError:
                    if change["before_sha256"] is not None:
                        raise
                    parent_fd = os.dup(directory)
                    try:
                        os.mkdir(part, 0o700, dir_fd=directory)
                    except FileExistsError:
                        os.close(parent_fd)
                    except BaseException:
                        os.close(parent_fd)
                        raise
                    else:
                        created_directories.append((parent_fd, part))
                    child = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory
                    )
                os.close(directory)
                directory = child
            if change["before_sha256"] is None:
                if replacement is None:
                    raise AccessDenied("Invalid creation proposal")
                temp_fd = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory,
                )
                with os.fdopen(temp_fd, "wb") as output:
                    temp_fd = -1
                    output.write(replacement)
                    output.flush()
                    os.fsync(output.fileno())
                os.link(
                    temporary,
                    parts[-1],
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
                applied = True
                return {"path": str(target), "sha256": change["after_sha256"]}
            original = os.open(
                parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            info = os.fstat(original)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size > MAX_FILE_BYTES
            ):
                raise AccessDenied("Patch target is not a single regular file")
            contents = bytearray()
            while chunk := os.read(original, 65536):
                contents.extend(chunk)
                if len(contents) > MAX_FILE_BYTES:
                    raise AccessDenied("Patch target exceeds the size limit")
            if hashlib.sha256(contents).hexdigest() != change["before_sha256"]:
                raise AccessDenied("File changed since the patch was prepared")
            if replacement is None:
                current = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
                if (
                    current.st_dev,
                    current.st_ino,
                    current.st_size,
                    current.st_mtime_ns,
                    current.st_ctime_ns,
                    current.st_nlink,
                ) != (
                    info.st_dev,
                    info.st_ino,
                    info.st_size,
                    info.st_mtime_ns,
                    info.st_ctime_ns,
                    info.st_nlink,
                ):
                    raise AccessDenied("File changed while the patch was being applied")
                os.unlink(parts[-1], dir_fd=directory)
                applied = True
                return {"path": str(target), "deleted": True}
            temp_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                info.st_mode & 0o777,
                dir_fd=directory,
            )
            if sys.platform != "darwin":
                raise AccessDenied("Safe patch metadata handling requires macOS")
            copy_metadata = ctypes.CDLL(None, use_errno=True).fcopyfile
            copy_metadata.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
            copy_metadata.restype = ctypes.c_int
            if copy_metadata(original, temp_fd, None, 0x7) != 0:
                raise AccessDenied("Unable to preserve file metadata safely")
            os.fchmod(temp_fd, info.st_mode & 0o777)
            with os.fdopen(temp_fd, "wb") as output:
                temp_fd = -1
                output.write(replacement)
                output.flush()
                os.fsync(output.fileno())
            current = os.stat(parts[-1], dir_fd=directory, follow_symlinks=False)
            if (
                current.st_dev,
                current.st_ino,
                current.st_size,
                current.st_mtime_ns,
                current.st_ctime_ns,
                current.st_nlink,
            ) != (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
                info.st_nlink,
            ):
                raise AccessDenied("File changed while the patch was being applied")
            os.replace(temporary, parts[-1], src_dir_fd=directory, dst_dir_fd=directory)
            applied = True
            return {"path": str(target), "sha256": change["after_sha256"]}
        finally:
            if temp_fd != -1:
                os.close(temp_fd)
            if original != -1:
                os.close(original)
            with suppress(FileNotFoundError):
                os.unlink(temporary, dir_fd=directory)
            os.close(directory)
            for parent_fd, part in reversed(created_directories):
                try:
                    if not applied:
                        with suppress(OSError):
                            os.rmdir(part, dir_fd=parent_fd)
                finally:
                    os.close(parent_fd)
