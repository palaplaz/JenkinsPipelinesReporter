"""Starts `fpreporter collect` in the background on request from the UI."""

from __future__ import annotations

import os
import subprocess
import threading

# Exit codes of `fpreporter collect` that the runs table can't show by itself (no run row is written).
EXIT_MESSAGES = {
    2: "The collector stopped with a configuration error (e.g. JENKINS_USER/JENKINS_TOKEN not set "
       "for the process running the UI). See data/fpreporter.log.",
    3: "Another collection was already running, so the retry was skipped.",
}


class CollectorLauncher:
    def __init__(self, command: list[str]) -> None:
        self.command = command
        self._proc: subprocess.Popen | None = None
        self._guard = threading.Lock()

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def last_exit_code(self) -> int | None:
        """Exit code of the most recent launch, or None if none finished yet."""
        return None if self._proc is None else self._proc.poll()

    def start(self) -> bool:
        """Starts a collection unless one launched from here is still running. Returns whether it started."""
        with self._guard:
            if self.running:
                return False
            self._proc = subprocess.Popen(
                self.command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,  # the collector writes its own log file
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            return True
