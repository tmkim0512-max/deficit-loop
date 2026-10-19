#!/usr/bin/env python3
"""End-to-end demo: copy examples/sample_project into a temp git repo and drain it.

Everything printed below is produced by running the real CLI; nothing is mocked.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "sample_project"


def sh(*args: str, cwd: Path, check: bool = True) -> int:
    print(f"$ {' '.join(args)}")
    p = subprocess.run(args, cwd=cwd, text=True, capture_output=True,
                       env={**os.environ, "PYTHONPATH": str(ROOT)})
    sys.stdout.write(p.stdout)
    sys.stderr.write(p.stderr)
    if check and p.returncode != 0:
        sys.exit(p.returncode)
    return p.returncode


def main() -> None:
    py = sys.executable
    with tempfile.TemporaryDirectory(prefix="deficit-loop-demo-") as tmp:
        repo = Path(tmp) / "sample_project"
        shutil.copytree(SAMPLE, repo)
        sh("git", "init", "-q", "-b", "main", cwd=repo)
        sh("git", "-c", "user.name=demo", "-c", "user.email=demo@localhost", "add", "-A", cwd=repo)
        sh("git", "-c", "user.name=demo", "-c", "user.email=demo@localhost", "commit", "-q", "-m", "sample project", cwd=repo)
        print(f"# repo: {repo}\n")
        sh(py, "-m", "deficit_loop", "init", cwd=repo)
        sh(py, "-m", "deficit_loop", "run", cwd=repo)
        sh(py, "-m", "deficit_loop", "report", cwd=repo)
        print()
        sh("git", "log", "--oneline", "--first-parent", "deficit-loop/docstrings", cwd=repo)
        sh("git", "diff", "--stat", "main..deficit-loop/docstrings", cwd=repo)
        print("\n# main is untouched (the oracle still sees 5 deficits there); the result lives on branch deficit-loop/docstrings")
        sh(py, "tools/measure_docstrings.py", "src", cwd=repo)


if __name__ == "__main__":
    main()
