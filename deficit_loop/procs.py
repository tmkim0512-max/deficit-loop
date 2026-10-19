"""Run a command in its own process group so a timeout kills the whole tree, not just the parent."""
from __future__ import annotations

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool


def run(argv: list[str] | str, *, cwd: Path, env: dict[str, str], timeout: float,
        shell: bool = False, stdin_text: str | None = None) -> Completed:
    """Like subprocess.run(capture_output=True, text=True) but the command gets its own session;
    on timeout the entire process group is SIGKILLed and reaped before returning."""
    p = subprocess.Popen(argv, shell=shell, cwd=cwd, env=env, text=True, start_new_session=True,
                         stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = p.communicate(stdin_text, timeout=timeout)
        return Completed(p.returncode, out, err, False)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        out, err = p.communicate()
        return Completed(-9, out or "", (err or "") + f"\n[deficit-loop] killed process group after {timeout}s timeout", True)
