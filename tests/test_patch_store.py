import subprocess
from pathlib import Path

import pytest

from jarvis.patch_store import PatchStore
from jarvis.security import AccessDenied


def workspaces(tmp_path: Path) -> tuple[Path, Path, Path]:
    target, baseline, modified = (tmp_path / name for name in ("target", "baseline", "modified"))
    for directory in (target, baseline, modified):
        directory.mkdir()
    (target / "note").write_text("old\n")
    (baseline / "note").write_text("old\n")
    (modified / "note").write_text("new\n")
    return target, baseline, modified


def test_reviewed_patch_applies_once_and_preserves_mode(tmp_path: Path) -> None:
    target, baseline, modified = workspaces(tmp_path)
    (target / "note").chmod(0o640)
    subprocess.run(
        ["/usr/bin/xattr", "-w", "com.jarvis.test", "preserved", str(target / "note")], check=True
    )
    store = PatchStore((target,))
    proposal = store.prepare(target, baseline, modified)
    assert "+new" in proposal["preview"]
    store.apply(proposal["patch_id"], proposal["sha256"])
    assert (target / "note").read_text() == "new\n"
    assert (target / "note").stat().st_mode & 0o777 == 0o640
    assert (
        subprocess.check_output(
            ["/usr/bin/xattr", "-p", "com.jarvis.test", str(target / "note")]
        ).strip()
        == b"preserved"
    )
    with pytest.raises(AccessDenied):
        store.apply(proposal["patch_id"], proposal["sha256"])


def test_patch_refuses_changed_target_or_wrong_approval(tmp_path: Path) -> None:
    target, baseline, modified = workspaces(tmp_path)
    store = PatchStore((target,))
    proposal = store.prepare(target, baseline, modified)
    (target / "note").write_text("user edit\n")
    with pytest.raises(AccessDenied, match="changed since"):
        store.apply(proposal["patch_id"], proposal["sha256"])
    assert (target / "note").read_text() == "user edit\n"
    proposal = store.prepare(target, baseline, modified)
    with pytest.raises(AccessDenied, match="approved review"):
        store.apply(proposal["patch_id"], "0" * 64)


def test_patch_refuses_symlink_swaps_and_multiple_files(tmp_path: Path) -> None:
    target, baseline, modified = workspaces(tmp_path)
    store = PatchStore((target,))
    proposal = store.prepare(target, baseline, modified)
    outside = tmp_path / "outside"
    outside.write_text("outside")
    (target / "note").unlink()
    (target / "note").symlink_to(outside)
    with pytest.raises((AccessDenied, OSError)):
        store.apply(proposal["patch_id"], proposal["sha256"])
    assert outside.read_text() == "outside"
    (modified / "other").write_text("another change")
    with pytest.raises(AccessDenied, match="Exactly one"):
        store.prepare(target, baseline, modified)


def test_revoking_scope_invalidates_pending_patch(tmp_path: Path) -> None:
    target, baseline, modified = workspaces(tmp_path)
    store = PatchStore((target,))
    proposal = store.prepare(target, baseline, modified)
    store.set_roots(())
    with pytest.raises(AccessDenied, match="unknown, expired"):
        store.apply(proposal["patch_id"], proposal["sha256"])
    assert (target / "note").read_text() == "old\n"


def test_multiple_files_are_proposed_individually_including_creation_and_deletion(
    tmp_path: Path,
) -> None:
    target, baseline, modified = workspaces(tmp_path)
    (target / "old").write_text("delete me\n")
    (baseline / "old").write_text("delete me\n")
    (modified / "new").write_text("created\n")
    store = PatchStore((target,))
    proposals = store.prepare_all(target, baseline, modified)
    assert len(proposals) == 3
    for proposal in proposals:
        store.apply(proposal["patch_id"], proposal["sha256"])
    assert (target / "note").read_text() == "new\n"
    assert (target / "new").read_text() == "created\n"
    assert not (target / "old").exists()


def test_creation_refuses_to_replace_a_new_user_file(tmp_path: Path) -> None:
    target, baseline, modified = workspaces(tmp_path)
    (modified / "new").write_text("proposed")
    store = PatchStore((target,))
    proposals = store.prepare_all(target, baseline, modified)
    creation = next(proposal for proposal in proposals if "create: new" in proposal["preview"])
    (target / "new").write_text("user created this")
    with pytest.raises(FileExistsError):
        store.apply(creation["patch_id"], creation["sha256"])
    assert (target / "new").read_text() == "user created this"


def test_creation_can_make_reviewed_parent_directories(tmp_path: Path) -> None:
    target, baseline, modified = workspaces(tmp_path)
    (modified / "new-dir").mkdir()
    (modified / "new-dir" / "file.txt").write_text("created\n")
    store = PatchStore((target,))
    proposal = next(
        proposal
        for proposal in store.prepare_all(target, baseline, modified)
        if "create: new-dir/file.txt" in proposal["preview"]
    )
    store.apply(proposal["patch_id"], proposal["sha256"])
    assert (target / "new-dir" / "file.txt").read_text() == "created\n"
