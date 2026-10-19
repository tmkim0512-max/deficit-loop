import dataclasses
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Callable

import pytest

from deficit_loop.judge import Guards
from deficit_loop.kernel import Kernel
from deficit_loop.spec import Budget, Deficit, GoalSpec
from deficit_loop.supplier import SupplyResult

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "sample_project"


def pytest_addoption(parser):
    parser.addoption("--guard-off", default=None,
                     help="(red-check only) name of a Guards field to switch to its unsafe value")


@pytest.fixture
def guards(request) -> Guards:
    """Default guards, or one guard switched to its unsafe value under --guard-off."""
    name = request.config.getoption("--guard-off")
    g = Guards()
    if name:
        assert name in {f.name for f in dataclasses.fields(Guards)}, f"unknown guard {name}"
        g = dataclasses.replace(g, **{name: not getattr(g, name)})
    return g


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@localhost", *args],
                          check=True, capture_output=True, text=True).stdout


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sample"
    shutil.copytree(SAMPLE, repo)
    git(repo, "init", "-q", "-b", "main")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@localhost", "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m", "sample")
    return repo


def load_spec(repo: Path, **budget) -> GoalSpec:
    spec = GoalSpec.load(repo / "deficit.json")
    if budget:
        spec = dataclasses.replace(spec, budget=dataclasses.replace(spec.budget, **budget))
    return spec


class PySupplier:
    """In-process supplier for tests: fn(worktree, deficit) -> SupplyResult | None (None = exit 0)."""

    name = "py"

    def __init__(self, fn: Callable[[Path, Deficit], SupplyResult | None]):
        self.fn = fn
        self.calls: list[Deficit] = []

    def supply(self, spec, deficit, worktree, timeout):
        self.calls.append(deficit)
        return self.fn(worktree, deficit) or SupplyResult(True, False, "done", "", 0)


def honest_fix(wt: Path, d: Deficit) -> None:
    """Same thing the demo supplier does, in-process."""
    subprocess.run(["python3", str(wt / "tools" / "fill_docstring.py")], cwd=wt, check=True,
                   env={"DL_DEFICIT": json.dumps(d.to_dict()), "PATH": os.environ["PATH"]})


def make_kernel(repo: Path, guards: Guards | None = None, supplier=None, **budget) -> Kernel:
    return Kernel(repo, load_spec(repo, **budget), guards=guards, supplier=supplier, log=lambda *_: None)


def measure_branch(repo: Path, branch: str) -> dict:
    """Run the *pristine* oracle (from the test's own copy) against a branch's tree, independent of the kernel."""
    tar = subprocess.run(["git", "-C", str(repo), "archive", branch], capture_output=True, check=True).stdout
    with tempfile.TemporaryDirectory() as t:
        tarfile.open(fileobj=io.BytesIO(tar)).extractall(t, filter="data")
        p = subprocess.run(["python3", str(SAMPLE / "tools" / "measure_docstrings.py"), "src"],
                           cwd=t, capture_output=True, text=True, check=True)
    return json.loads(p.stdout)
