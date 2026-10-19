"""Suppliers: something that tries to remove one deficit inside an isolated worktree.

The kernel never trusts what a supplier *says*; its stdout is kept as a
"self-report" for the record only. What counts is what the oracle measures
afterwards (see judge.py).

Contract for any supplier command:
  * cwd        = the supply worktree (edit files here, do not commit — the kernel commits)
  * env        = DL_DEFICIT (JSON), DL_GOAL, DL_WORKTREE, DL_KIND, DL_TARGET, DL_DETAIL
  * stdin      = the deficit JSON
  * exit 0     = "I think I fixed it" (judge will check)
  * exit 75    = temporary external failure (rate limit, network) -> not a defect
  * other code = supplier failed
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import procs
from .spec import EXTERNAL_CMD_ENV, Deficit, GoalSpec

EX_TEMPFAIL = 75
EXTERNAL_PATTERNS = re.compile(
    r"rate.?limit|too many requests|\b429\b|\b5\d\d\b.*(server|gateway)|quota|"
    r"session limit|network|connection (refused|reset|error)|timed? ?out|unavailable|overloaded",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SupplyResult:
    ok: bool                 # exit 0
    external_error: bool     # transient infrastructure failure, not the supplier's fault
    report: str              # supplier stdout (self-report; never trusted by the judge)
    stderr: str
    returncode: int


class Supplier(Protocol):
    name: str

    def supply(self, spec: GoalSpec, deficit: Deficit, worktree: Path, timeout: float) -> SupplyResult: ...


def _env(spec: GoalSpec, deficit: Deficit, worktree: Path) -> dict[str, str]:
    return {
        **os.environ,
        "DL_DEFICIT": json.dumps(deficit.to_dict(), ensure_ascii=False),
        "DL_GOAL": spec.name,
        "DL_WORKTREE": str(worktree),
        "DL_KIND": deficit.kind,
        "DL_TARGET": deficit.target,
        "DL_DETAIL": deficit.detail,
    }


def _run(argv: list[str] | str, shell: bool, spec: GoalSpec, deficit: Deficit, worktree: Path, timeout: float) -> SupplyResult:
    try:
        p = procs.run(argv, shell=shell, cwd=worktree, timeout=timeout,
                      stdin_text=json.dumps(deficit.to_dict()), env=_env(spec, deficit, worktree))
    except FileNotFoundError as e:
        return SupplyResult(False, False, "", f"supplier command not found: {e}", -2)
    if p.timed_out:
        return SupplyResult(False, True, p.stdout, p.stderr, -1)
    external = p.returncode == EX_TEMPFAIL or (
        p.returncode != 0 and bool(EXTERNAL_PATTERNS.search(p.stderr + "\n" + p.stdout))
    )
    return SupplyResult(p.returncode == 0, external, p.stdout, p.stderr, p.returncode)


class ScriptSupplier:
    """Deterministic supplier: a fixed command list from the spec (default)."""

    name = "script"

    def __init__(self, command: list[str]):
        self.command = command

    def supply(self, spec: GoalSpec, deficit: Deficit, worktree: Path, timeout: float) -> SupplyResult:
        return _run(self.command, False, spec, deficit, worktree, timeout)


class ExternalCliSupplier:
    """An outside agent CLI given as a shell string via $DEFICIT_LOOP_AGENT_CMD.

    Example: DEFICIT_LOOP_AGENT_CMD='my-agent --prompt "Remove this deficit: $DL_DEFICIT"'
    """

    name = "external"

    def __init__(self, command: str):
        self.command = command

    def supply(self, spec: GoalSpec, deficit: Deficit, worktree: Path, timeout: float) -> SupplyResult:
        return _run(self.command, True, spec, deficit, worktree, timeout)


class SupplierUnavailable(RuntimeError):
    pass


def build_supplier(spec: GoalSpec) -> Supplier:
    if spec.supplier_kind == "script":
        assert spec.supplier_command
        return ScriptSupplier(spec.supplier_command)
    cmd = spec.resolve_external_command()
    if not cmd:
        raise SupplierUnavailable(f"supplier.kind=external but ${EXTERNAL_CMD_ENV} is not set; external supply is disabled")
    return ExternalCliSupplier(cmd)
