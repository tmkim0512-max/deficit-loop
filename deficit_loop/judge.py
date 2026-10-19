"""The judge: decides whether a supply is adopted.

It never reads the supplier's self-report. It re-measures with the oracle in
the judgment worktree (integration branch + the supply merged in) and accepts
only when ALL of these hold:

  1. target_removed    — the deficit that was assigned is gone
  2. no_new_deficits   — no deficit key appears that was not there before
  3. scope_not_shrunk  — the oracle measured at least as much as the baseline
                         (blocks "fixing" by deleting the thing being measured)
  4. protected_intact  — the supply did not modify protected paths (oracle tooling)

``Guards`` exists so the red-check can switch each guard off and prove that
the corresponding test then fails. The CLI never exposes these switches.
"""
from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch

from .oracle import Measurement
from .supplier import SupplyResult


@dataclass(frozen=True)
class Guards:
    trust_supplier_report: bool = False   # True = the known-bad behaviour (believe exit 0)
    target_removed: bool = True
    no_new_deficits: bool = True
    scope_not_shrunk: bool = True
    protected_intact: bool = True
    stall_detection: bool = True          # used by the kernel, kept here so one object holds all switches


@dataclass(frozen=True)
class Verdict:
    accept: bool
    stage: str
    reason: str


def judge(
    guards: Guards,
    target: tuple[str, str],
    before: Measurement,
    after: Measurement,
    baseline_scope: int | None,
    changed: list[str],
    protected: tuple[str, ...],
    supply: SupplyResult,
) -> Verdict:
    if guards.trust_supplier_report:
        # Deliberately wrong behaviour, only reachable from the red-check.
        return Verdict(supply.ok, "self-report", "supplier exit 0" if supply.ok else "supplier failed")

    if guards.protected_intact:
        touched = [p for p in changed if any(p == pat or p.startswith(pat.rstrip("/") + "/") or fnmatch(p, pat) for pat in protected)]
        if touched:
            return Verdict(False, "guard", f"protected path modified: {', '.join(touched)}")

    if guards.scope_not_shrunk and baseline_scope is not None and after.scope is not None and after.scope < baseline_scope:
        return Verdict(False, "guard", f"scope shrank {baseline_scope} -> {after.scope} (measured things disappeared)")

    if guards.target_removed and target in after.keys:
        return Verdict(False, "measure", f"target still present: {target[0]}:{target[1]}")

    if guards.no_new_deficits:
        new = sorted(after.keys - before.keys)
        if new:
            return Verdict(False, "measure", "new deficits introduced: " + ", ".join(f"{k}:{t}" for k, t in new))

    return Verdict(True, "done", f"deficits {len(before.deficits)} -> {len(after.deficits)}")
