import subprocess
import sys
from pathlib import Path

from jarvis.staging import staging


def test_active_leases_are_preserved() -> None:
    with staging() as first:
        (first / "active").write_text("safe")
        with staging():
            assert (first / "active").read_text() == "safe"


def test_crashed_staging_is_recovered() -> None:
    code = (
        "import time; from jarvis.staging import staging; "
        "s = staging(); p = s.__enter__(); print(p, flush=True); time.sleep(60)"
    )
    process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None
        directory = Path(process.stdout.readline().strip())
        assert (directory / ".jarvis-lease").is_file()
        process.kill()
        process.wait(timeout=5)
        with staging():
            assert not directory.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
