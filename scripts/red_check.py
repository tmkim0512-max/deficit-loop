#!/usr/bin/env python3
"""Red check: prove the guard tests actually check their guards.

For every judge guard, run its test twice: with the guard ON (must pass: "1 passed")
and with the guard switched to its unsafe value via `--guard-off` (must FAIL on the
test's own "UNSAFE:"-marked assertion — any other failure, setup error or crash does not count).
A guard whose test survives being switched off is not protected -> exit 1.
"""
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUARD_TESTS = {
    "trust_supplier_report": "test_liar_supplier_is_rejected",
    "no_new_deficits": "test_new_deficit_is_rejected",
    "target_removed": "test_target_still_present_is_rejected",
    "scope_not_shrunk": "test_scope_shrink_is_rejected",
    "protected_intact": "test_protected_path_is_rejected",
    "stall_detection": "test_stall_detected",
}


def run(test: str, guard_off: str | None) -> tuple[int, str]:
    cmd = [sys.executable, "-m", "pytest", "-o", "addopts=-q", "-p", "no:cacheprovider", f"tests/test_guards.py::{test}"]
    if guard_off:
        cmd += ["--guard-off", guard_off]
    p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, env={**os.environ, "COLUMNS": "400"})  # keep summary lines untruncated
    summary = next((l for l in reversed(p.stdout.splitlines()) if l.strip()), "")
    if guard_off is None:
        return (0 if "1 passed" in summary else 1), summary
    # the guard-off run must fail on the test's own assertion, not on setup/errors/interruption
    failed_line = next((l for l in p.stdout.splitlines() if l.startswith("FAILED") and test in l), "")
    # ...and specifically the assertion that detects the unsafe outcome (marked "UNSAFE:" in the test)
    genuine = "1 failed" in summary and "AssertionError: UNSAFE:" in failed_line and "error" not in summary.split("failed")[-1]
    return (1 if genuine else 0), (failed_line or summary)


def main() -> int:
    rows, ok = [], True
    for guard, test in GUARD_TESTS.items():
        rc_on, s_on = run(test, None)
        rc_off, s_off = run(test, guard)
        good = rc_on == 0 and rc_off != 0
        ok &= good
        rows.append((guard, test, "pass" if rc_on == 0 else "FAIL", "fail" if rc_off != 0 else "PASS(!)", "ok" if good else "NOT PROTECTED", s_off))
    w = [max(len(r[i]) for r in rows + [("guard", "test", "guard on", "guard off", "verdict", "")]) for i in range(5)]
    print("  ".join(h.ljust(w[i]) for i, h in enumerate(["guard", "test", "guard on", "guard off", "verdict"])))
    print("  ".join("-" * x for x in w))
    for r in rows:
        print("  ".join(r[i].ljust(w[i]) for i in range(5)) + f"   # off: {r[5]}")
    print("\nred check:", "ALL GUARDS PROTECTED" if ok else "SOME GUARD IS NOT PROTECTED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
