"""Run the measurement oracle inside a worktree and parse its deficit list."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from . import procs
from .spec import Deficit, GoalSpec, SpecError, parse_measurement


class OracleError(RuntimeError):
    pass


@dataclass(frozen=True)
class Measurement:
    deficits: list[Deficit]
    scope: int | None

    @property
    def keys(self) -> frozenset[tuple[str, str]]:
        return frozenset(d.key for d in self.deficits)


def measure(spec: GoalSpec, worktree: Path, timeout: float | None = None) -> Measurement:
    """Run the oracle. *timeout* (if given) caps the configured oracle_timeout, e.g. by the remaining time budget."""
    if timeout is None:
        timeout = spec.budget.oracle_timeout
    try:
        p = procs.run(spec.oracle, cwd=worktree, timeout=timeout,
                      env={**os.environ, "DL_WORKTREE": str(worktree)})
    except FileNotFoundError as e:
        raise OracleError(f"oracle command not found: {spec.oracle[0]}") from e
    if p.timed_out:
        raise OracleError(f"oracle timed out after {timeout:.1f}s")
    if p.returncode != 0:
        raise OracleError(f"oracle exited {p.returncode}: {p.stderr.strip()[:500]}")
    try:
        deficits, scope = parse_measurement(p.stdout)
    except SpecError as e:
        raise OracleError(str(e)) from e
    return Measurement(deficits=deficits, scope=scope)
