import sys

import pytest

from fpreporter.lock import AlreadyLocked, exclusive_lock
from fpreporter.web.launcher import CollectorLauncher


def test_second_lock_is_rejected_and_released_afterwards(tmp_path):
    path = tmp_path / "data" / "collect.lock"
    with exclusive_lock(path):
        with pytest.raises(AlreadyLocked):
            with exclusive_lock(path):
                pass
    with exclusive_lock(path):  # released after the first block
        pass


def test_lock_is_held_across_processes(tmp_path):
    path = tmp_path / "collect.lock"
    probe = (
        "import sys; from pathlib import Path; from fpreporter.lock import AlreadyLocked, exclusive_lock\n"
        "try:\n"
        "    with exclusive_lock(Path(sys.argv[1])): sys.exit(0)\n"
        "except AlreadyLocked: sys.exit(3)\n"
    )
    launcher = CollectorLauncher([sys.executable, "-c", probe, str(path)])
    with exclusive_lock(path):
        launcher.start()
        launcher._proc.wait(timeout=30)
        assert launcher.last_exit_code == 3


def test_launcher_runs_one_process_at_a_time(tmp_path):
    marker = tmp_path / "done"
    launcher = CollectorLauncher(
        [sys.executable, "-c", f"import time, pathlib; time.sleep(1); pathlib.Path(r'{marker}').touch()"]
    )
    assert launcher.last_exit_code is None and not launcher.running

    assert launcher.start() is True
    assert launcher.running
    assert launcher.start() is False  # still busy

    launcher._proc.wait(timeout=30)
    assert not launcher.running
    assert launcher.last_exit_code == 0
    assert marker.exists()
    assert launcher.start() is True  # can start again once finished
    launcher._proc.wait(timeout=30)
