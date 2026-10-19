"""Fake-success prevention. Each test pins one judge guard.

`scripts/red_check.py` re-runs each of these with its guard switched OFF
(`--guard-off <name>`) and demands that the test then FAILS. A test that
still passes with its guard off is not checking that guard.
"""
from pathlib import Path

from conftest import PySupplier, git, honest_fix, make_kernel, measure_branch
from deficit_loop import gitops
from deficit_loop.spec import Deficit
from deficit_loop.state import STALLED

INTEG = "deficit-loop/docstrings"


def _drain(sample_repo, guards, fn, **budget):
    k = make_kernel(sample_repo, guards=guards, supplier=PySupplier(fn), **budget)
    k.init()
    base = gitops.head_sha(sample_repo, INTEG)
    return k.run(), base


# guard: trust_supplier_report (unsafe value = True)
def test_liar_supplier_is_rejected(sample_repo, guards):
    """Supplier exits 0, writes a harmless change, fixes nothing. Only re-measurement can tell."""
    def liar(wt: Path, d: Deficit):
        (wt / "NOTES.md").write_text(f"fixed {d.target}, trust me\n")

    state, base = _drain(sample_repo, guards, liar, stall_k=2)
    assert gitops.head_sha(sample_repo, INTEG) == base, "UNSAFE: a supply that fixed nothing was adopted on the supplier's word"
    assert all(a.verdict == "REJECT" for a in state.attempts), [a.verdict for a in state.attempts]
    assert len(measure_branch(sample_repo, INTEG)["deficits"]) == 5
    assert state.status == STALLED


# guard: no_new_deficits
def test_new_deficit_is_rejected(sample_repo, guards):
    """Fixes the target but strips the docstring of another function on the way."""
    def careless(wt: Path, d: Deficit):
        honest_fix(wt, d)
        f = wt / "src" / "shapes.py"
        f.write_text(f.read_text().replace('    """Area of a rectangle."""\n', ""))

    state, base = _drain(sample_repo, guards, careless, stall_k=1)
    assert gitops.head_sha(sample_repo, INTEG) == base, "UNSAFE: a supply that introduced a new deficit was adopted"
    assert state.attempts[0].verdict == "REJECT" and "new deficits" in state.attempts[0].reason
    assert measure_branch(sample_repo, INTEG)["scope"] == 8


# guard: target_removed
def test_target_still_present_is_rejected(sample_repo, guards):
    """Fixes a *different* deficit than the one assigned: the set shrinks, but not the assignment."""
    def wrong_one(wt: Path, d: Deficit):
        others = [x for x in ["src/shapes.py::circle_area", "src/text.py::truncate"] if x != d.target]
        honest_fix(wt, Deficit(d.kind, others[0]))

    state, base = _drain(sample_repo, guards, wrong_one, stall_k=1)
    assert gitops.head_sha(sample_repo, INTEG) == base, "UNSAFE: a supply that left its assigned deficit in place was adopted"
    assert state.attempts[0].verdict == "REJECT" and "still present" in state.attempts[0].reason


# guard: scope_not_shrunk
def test_scope_shrink_is_rejected(sample_repo, guards):
    """'Fixes' the deficit by deleting the function that had it."""
    def delete_it(wt: Path, d: Deficit):
        path, name = d.target.split("::")
        src = (wt / path).read_text().splitlines(keepends=True)
        start = next(i for i, l in enumerate(src) if l.lstrip().startswith(("def " + name, "class " + name)))
        indent = len(src[start]) - len(src[start].lstrip())
        end = start + 1
        while end < len(src) and (not src[end].strip() or len(src[end]) - len(src[end].lstrip()) > indent):
            end += 1
        (wt / path).write_text("".join(src[:start] + src[end:]))

    state, base = _drain(sample_repo, guards, delete_it, stall_k=1)
    assert gitops.head_sha(sample_repo, INTEG) == base, "UNSAFE: deleting the measured function was adopted as a fix"
    assert state.attempts[0].verdict == "REJECT" and "scope shrank" in state.attempts[0].reason


# guard: protected_intact
def test_protected_path_is_rejected(sample_repo, guards):
    """Rewrites the oracle so it reports nothing. With the guard off the judge would believe it."""
    def tamper(wt: Path, d: Deficit):
        (wt / "tools" / "measure_docstrings.py").write_text('print(\'{"deficits": [], "scope": 8}\')\n')

    state, base = _drain(sample_repo, guards, tamper, stall_k=1)
    assert gitops.head_sha(sample_repo, INTEG) == base, "UNSAFE: a supply that rewrote the oracle was adopted"
    assert state.attempts[0].verdict == "REJECT" and "protected" in state.attempts[0].reason
    assert git(sample_repo, "show", f"{INTEG}:tools/measure_docstrings.py").startswith("#!/usr/bin/env python3")


# guard: stall_detection
def test_stall_detected(sample_repo, guards):
    """A supplier that never manages anything must stop after stall_k attempts, not run the budget down."""
    def broken(wt: Path, d: Deficit):
        from deficit_loop.supplier import SupplyResult
        return SupplyResult(False, False, "", "AssertionError", 1)

    state, _ = _drain(sample_repo, guards, broken, stall_k=3, max_attempts=50, max_reject_per_key=100)
    assert len(state.attempts) == 3, f"UNSAFE: ran {len(state.attempts)} attempts without progress instead of stopping at stall_k=3"
    assert state.status == STALLED
    assert "consecutive attempts without progress" in state.stop_reason
