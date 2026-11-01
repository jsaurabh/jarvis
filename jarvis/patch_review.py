"""Host-generated, bounded reviews of untrusted VM workspace changes.

This module deliberately does not apply changes. A caller must separately
authenticate the approval, recheck the destination, and perform safe writes.
"""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from .snapshot_worker import EXCLUDED_DIRS, EXCLUDED_FILES

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TREE_BYTES = 32 * 1024 * 1024
MAX_FILES = 2000
MAX_DIRECTORIES = 2000
MAX_DEPTH = 64
MAX_CHANGES = 100
MAX_REVIEW_BYTES = 128 * 1024


class PatchReviewError(ValueError):
    """The untrusted workspace cannot be safely represented for review."""


def _read_tree(root: Path) -> dict[str, bytes]:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    root_fd = os.open(root, flags)
    files: dict[str, bytes] = {}
    total = 0
    directory_count = 0

    def visit(directory: int, prefix: str) -> None:
        nonlocal total, directory_count
        directory_count += 1
        if directory_count > MAX_DIRECTORIES or prefix.count("/") >= MAX_DEPTH:
            raise PatchReviewError("Workspace directory limit exceeded")
        for name in sorted(os.listdir(directory)):
            relative = f"{prefix}/{name}" if prefix else name
            if name in {".", ".."} or "\x00" in name or "\n" in name or "\r" in name:
                raise PatchReviewError("Unsupported workspace path")
            info = os.stat(name, dir_fd=directory, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(name, flags, dir_fd=directory)
                try:
                    visit(child, relative)
                finally:
                    os.close(child)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise PatchReviewError("Workspace contains a link or special file")
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                actual = os.fstat(fd)
                if not stat.S_ISREG(actual.st_mode) or actual.st_nlink != 1:
                    raise PatchReviewError("Workspace file changed during review")
                if actual.st_size > MAX_FILE_BYTES:
                    raise PatchReviewError("A workspace file exceeds the review limit")
                data = bytearray()
                while chunk := os.read(fd, min(65536, MAX_FILE_BYTES + 1 - len(data))):
                    data.extend(chunk)
                    if len(data) > MAX_FILE_BYTES:
                        raise PatchReviewError("A workspace file exceeds the review limit")
                final = os.fstat(fd)
                if (actual.st_size, actual.st_mtime_ns, actual.st_ctime_ns) != (
                    final.st_size,
                    final.st_mtime_ns,
                    final.st_ctime_ns,
                ):
                    raise PatchReviewError("Workspace file changed during review")
                total += len(data)
                if total > MAX_TREE_BYTES or len(files) >= MAX_FILES:
                    raise PatchReviewError("Workspace exceeds the review limit")
                files[relative] = bytes(data)
            finally:
                os.close(fd)

    try:
        visit(root_fd, "")
    finally:
        os.close(root_fd)
    return files


def _display(data: bytes) -> list[str]:
    if b"\x00" in data:
        raise PatchReviewError("Binary changes cannot be reviewed")
    try:
        value = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PatchReviewError("Binary changes cannot be reviewed") from exc
    # Escape controls, bidirectional overrides, and other non-ASCII characters so
    # untrusted text cannot visually rewrite the surrounding approval dialog.
    return [
        line.encode("unicode_escape").decode("ascii") + "\n"
        for line in value.splitlines(keepends=True)
    ]


def review_workspace(baseline: Path, modified: Path) -> dict[str, Any]:
    """Build a complete review from host-read files, never from guest stdout."""
    before, after = _read_tree(baseline), _read_tree(modified)
    changes: list[dict[str, Any]] = []
    preview: list[str] = []
    for path in sorted(before.keys() | after.keys()):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        parts = path.split("/")
        name = parts[-1].lower()
        if (
            any(part.lower() in EXCLUDED_DIRS for part in parts)
            or name.startswith(".env")
            or name in EXCLUDED_FILES
            or name.endswith((".pem", ".key", ".p12", ".pfx"))
        ):
            raise PatchReviewError("Changes include a protected or excluded path")
        if len(changes) >= MAX_CHANGES:
            raise PatchReviewError("Too many changed files to review")
        safe_name = path.encode("unicode_escape").decode("ascii")
        old_lines = _display(old) if old is not None else []
        new_lines = _display(new) if new is not None else []
        operation = "create" if old is None else "delete" if new is None else "modify"
        file_preview = [f"{operation}: {safe_name}\n"]
        file_preview.extend(
            difflib.unified_diff(
                old_lines, new_lines, fromfile=f"before/{safe_name}", tofile=f"after/{safe_name}"
            )
        )
        preview.extend(file_preview)
        changes.append(
            {
                "path": path,
                "before_sha256": hashlib.sha256(old).hexdigest() if old is not None else None,
                "after_sha256": hashlib.sha256(new).hexdigest() if new is not None else None,
                "after_base64": base64.b64encode(new).decode("ascii") if new is not None else None,
                "preview": "".join(file_preview),
            }
        )
    rendered = "".join(preview)
    if len(rendered.encode("utf-8")) > MAX_REVIEW_BYTES:
        raise PatchReviewError("Change review exceeds the display limit")
    manifest = {"version": 1, "changes": changes, "preview": rendered}
    serialized = json.dumps(manifest, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
    return {**manifest, "sha256": hashlib.sha256(serialized.encode("utf-8")).hexdigest()}
