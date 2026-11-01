import errno
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis.bounded_volume import bounded_volume
from jarvis.staging import recover_orphans, staging


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires macOS disk images")
def test_workspace_volume_has_a_real_size_limit_and_is_unmounted() -> None:
    with staging() as directory:
        with bounded_volume(directory, size_mb=32) as volume:
            assert os.path.ismount(volume)
            with pytest.raises(OSError) as error, (volume / "large").open("wb") as output:
                data = b"x" * (1024 * 1024)
                for _ in range(40):
                    output.write(data)
            assert error.value.errno == errno.ENOSPC
        assert not os.path.ismount(volume)
    assert not directory.exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="Requires macOS disk images")
def test_abandoned_volume_is_unmounted_during_recovery() -> None:
    script = """
import time
from jarvis.bounded_volume import bounded_volume
from jarvis.staging import staging
with staging() as directory, bounded_volume(directory, size_mb=32) as volume:
    print(volume, flush=True)
    time.sleep(60)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        volume = Path(child.stdout.readline().strip())
        assert volume.is_absolute() and os.path.ismount(volume)
        child.kill()
        child.wait(timeout=10)
        recover_orphans()
        assert not os.path.ismount(volume)
        assert not volume.parent.exists()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
