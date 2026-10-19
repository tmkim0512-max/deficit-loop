"""deficit-loop command line: init / run / status / resume / report."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import gitops, report as rp, state as st
from .kernel import Kernel, KernelError
from .oracle import OracleError
from .spec import GoalSpec, SpecError
from .state import BusyError, Store
from .supplier import SupplierUnavailable

EXIT = {st.ACHIEVED: 0, st.STALLED: 1, st.BUDGET_EXHAUSTED: 1, st.BLOCKED_EXTERNAL: 4, st.HALTED_UNSAFE: 5,
        st.HALTED_ERROR: 6, st.RUNNING: 1, st.INITIALIZING: 1}
EXIT_USAGE, EXIT_BUSY = 2, 3


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="deficit-loop", description="Drain a measurable goal: supply one deficit at a time, adopt only what an independent re-measurement confirms.")
    p.add_argument("--repo", default=".", help="path inside the git repository to work on (default: cwd)")
    p.add_argument("--spec", default="deficit.json", help="goal spec JSON (default: deficit.json, relative to --repo)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create the integration branch + judgment worktree, measure the baseline (rerun to complete an interrupted init)")
    sub.add_parser("run", help="start or continue draining until a terminal status")
    sub.add_parser("resume", help="continue an interrupted or BLOCKED_EXTERNAL run (fails if there is nothing to resume)")
    sub.add_parser("status", help="one-screen summary")
    sub.add_parser("report", help="final status + a table of every attempt and its verdict")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo = Path(args.repo).resolve()
    spec_path = Path(args.spec) if Path(args.spec).is_absolute() else repo / args.spec
    try:
        spec = GoalSpec.load(spec_path)
        if args.cmd in ("status", "report"):
            store = Store(gitops.repo_root(repo), spec.name)
            if not store.exists():
                print(f"no state for goal {spec.name!r} (run init first)", file=sys.stderr)
                return EXIT_USAGE
            state = store.load()
            print(rp.status(state) if args.cmd == "status" else rp.report(state))
            return EXIT[state.status]
        kernel = Kernel(repo, spec)
        if args.cmd == "init":
            kernel.init()
            return 0
        state = kernel.run(require_existing=(args.cmd == "resume"))
        return EXIT[state.status]
    except BusyError as e:
        print(f"BUSY: {e}", file=sys.stderr)
        return EXIT_BUSY
    except gitops.UnsafeRepoError as e:
        print(f"HALTED_UNSAFE: {e}", file=sys.stderr)
        return EXIT[st.HALTED_UNSAFE]
    except (SpecError, KernelError, SupplierUnavailable, OracleError, gitops.GitError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
