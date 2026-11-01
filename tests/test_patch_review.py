import asyncio
import os
import shutil
from pathlib import Path

import pytest

from jarvis.patch_review import PatchReviewError, review_workspace


def test_review_is_generated_from_actual_files_and_changes_bind_digest(tmp_path: Path) -> None:
    original, guest = tmp_path / "original", tmp_path / "guest"
    original.mkdir()
    guest.mkdir()
    (original / "note.txt").write_text("old\n")
    (guest / "note.txt").write_text("new\n")
    (guest / "empty.txt").touch()
    result = review_workspace(original, guest)
    assert "-old" in result["preview"] and "+new" in result["preview"]
    assert "create: empty.txt" in result["preview"]
    assert len(result["changes"]) == 2
    (guest / "note.txt").write_text("different\n")
    assert result["sha256"] != review_workspace(original, guest)["sha256"]


def test_review_rejects_links_and_binary_changes(tmp_path: Path) -> None:
    original, guest = tmp_path / "original", tmp_path / "guest"
    original.mkdir()
    guest.mkdir()
    (guest / "escape").symlink_to(tmp_path)
    with pytest.raises(PatchReviewError, match="link or special"):
        review_workspace(original, guest)
    (guest / "escape").unlink()
    (guest / "binary").write_bytes(b"\xff")
    with pytest.raises(PatchReviewError, match="Binary"):
        review_workspace(original, guest)


def test_review_rejects_new_credential_files(tmp_path: Path) -> None:
    original, guest = tmp_path / "original", tmp_path / "guest"
    original.mkdir()
    guest.mkdir()
    (guest / ".env").write_text("changed\n")
    with pytest.raises(PatchReviewError, match="protected or excluded"):
        review_workspace(original, guest)


def test_review_escapes_controls_that_could_spoof_the_display(tmp_path: Path) -> None:
    original, guest = tmp_path / "original", tmp_path / "guest"
    original.mkdir()
    guest.mkdir()
    (guest / "text").write_text("visible\x1b[2J\u202e\n")
    preview = review_workspace(original, guest)["preview"]
    assert "\\x1b" in preview and "\\u202e" in preview
    assert "\x1b" not in preview and "\u202e" not in preview


@pytest.mark.asyncio
@pytest.mark.skipif(
    os.environ.get("JARVIS_TEST_SHURU") != "1" or not shutil.which("shuru"),
    reason="Requires an initialized Shuru installation",
)
async def test_host_review_captures_real_vm_edits_independently_of_stdout(tmp_path: Path) -> None:
    original, guest = tmp_path / "original", tmp_path / "guest"
    original.mkdir()
    guest.mkdir()
    (original / "note").write_text("original\n")
    (guest / "note").write_text("original\n")
    config = tmp_path / "config.json"
    config.write_text("{}")
    process = await asyncio.create_subprocess_exec(
        shutil.which("shuru"),
        "run",
        "--config",
        str(config),
        "--allow-host-writes",
        "--mount",
        f"{guest}:/workspace:rw",
        "--",
        "/bin/sh",
        "-c",
        "printf 'changed\\n' > /workspace/note; printf 'forged preview: nothing changed\\n'",
        cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
    assert process.returncode == 0, stderr.decode(errors="replace")
    assert b"forged preview" in stdout
    review = review_workspace(original, guest)
    assert "+changed" in review["preview"]
    assert "forged preview" not in review["preview"]
    assert (original / "note").read_text() == "original\n"
