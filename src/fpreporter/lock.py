"""Cross-process lock so only one collection runs at a time (scheduled task vs. retry from the UI)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path


class AlreadyLocked(Exception):
    """Another process holds the lock."""


@contextmanager
def exclusive_lock(path: Path) -> Iterator[None]:
    """Holds a non-blocking OS-level lock on `path` for the duration of the block.

    The OS releases the lock if the process dies, so a crash never leaves a stale lock behind.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as f:
        try:
            _lock(f.fileno())
        except OSError:
            raise AlreadyLocked(f"Another collection is already running (lock: {path})") from None
        try:
            yield
        finally:
            _unlock(f.fileno())


if os.name == "nt":
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)
