"""Integration: a real git repo, the real oracle, the loop end to end."""
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from conftest import ROOT, PySupplier, git, honest_fix, make_kernel, measure_branch
from deficit_loop import gitops
from deficit_loop.kernel import Kernel, KernelError
from deficit_loop.state import ACHIEVED, BLOCKED_EXTERNAL, BUDGET_EXHAUSTED, HALTED_UNSAFE, STALLED, BusyError, Store
from deficit_loop.supplier import SupplyResult


def test_drain_to_achieved(sample_repo):
    k = make_kernel(sample_repo)
    k.init()
    state = k.run()
    assert state.status == ACHIEVED
    assert [a.verdict for a in state.attempts] == ["ACCEPT"] * 5
    assert measure_branch(sample_repo, "deficit-loop/docstrings") == {"deficits": [], "scope": 8}
    assert len(measure_branch(sample_repo, "main")["deficits"]) == 5          # main untouched
    assert git(sample_repo, "status", "--porcelain") == ""                    # user's checkout untouched
    # all supply branches/worktrees cleaned, only ours remain
    branches = git(sample_repo, "branch", "--format=%(refname:short)").split()
    assert sorted(branches) == ["deficit-loop/docstrings", "main"]
    assert state.owned_branches == ["deficit-loop/docstrings"]
    assert [Path(p).name for p in state.owned_worktrees] == ["judge"]
    assert gitops.list_worktrees(sample_repo) == [sample_repo.resolve(), k.judge_wt.resolve()]
    events = [e["event"] for e in k.store.events()]
    assert events[:2] == ["init", "baseline"] and events[-1] == "finish" and events.count("attempt_end") == 5
    assert sorted(p.name for p in k.store.supply_logs.iterdir()) == [f"{i:03d}.out" for i in range(1, 6)]
    # running again is a no-op
    assert k.run().status == ACHIEVED and len(k.store.load().attempts) == 5


def test_cli_end_to_end(sample_repo):
    env = {**os.environ, "PYTHONPATH": str(ROOT)}

    def cli(*args):
        return subprocess.run([sys.executable, "-m", "deficit_loop", *args], cwd=sample_repo, env=env, capture_output=True, text=True)

    assert cli("status").returncode == 2
    assert cli("resume").returncode == 2
    assert cli("init").returncode == 0
    assert cli("init").returncode == 2                     # already initialised
    r = cli("run")
    assert r.returncode == 0, r.stderr
    assert "[finish] ACHIEVED" in r.stdout
    rep = cli("report")
    assert rep.returncode == 0 and "status:      ACHIEVED" in rep.stdout and rep.stdout.count("ACCEPT") == 5
    assert cli("status").returncode == 0


def test_resume_after_crash_discards_in_flight_supply(sample_repo):
    """The runner is SIGKILLed while a supplier is working (no finally blocks run)."""
    crash_spec = sample_repo / "crash.json"
    crash_spec.write_text((sample_repo / "deficit.json").read_text().replace(
        '"command": ["python3", "tools/fill_docstring.py"]',
        '"command": ["python3", "-c", "import os, signal, subprocess; subprocess.run([\'python3\', \'tools/fill_docstring.py\'], check=True); os.kill(os.getppid(), signal.SIGKILL)"]'))
    k = make_kernel(sample_repo)
    k.init()
    p = subprocess.run([sys.executable, "-m", "deficit_loop", "--spec", "crash.json", "run"], cwd=sample_repo,
                       env={**os.environ, "PYTHONPATH": str(ROOT)}, capture_output=True, text=True)
    assert p.returncode == -9, p.stderr
    crashed = Store(sample_repo, "docstrings").load()
    assert crashed.in_flight and crashed.in_flight["stage"] == "supply"
    # the half-done attempt left a worktree and a branch behind, both recorded as ours
    assert Path(crashed.in_flight["worktree"]).exists()
    assert gitops.branch_exists(sample_repo, crashed.in_flight["branch"])
    assert k.store.lock_path.exists()

    k2 = make_kernel(sample_repo, supplier=PySupplier(lambda wt, d: honest_fix(wt, d)))
    state = k2.run(require_existing=True)
    assert state.status == ACHIEVED and state.in_flight is None
    assert not Path(crashed.in_flight["worktree"]).exists()
    assert not gitops.branch_exists(sample_repo, crashed.in_flight["branch"])
    assert "recover" in [e["event"] for e in k2.store.events()]
    assert len(state.attempts) == 5                                    # the crashed attempt was never counted


def test_resume_after_crash_during_merge_trial_resets_integration(sample_repo):
    k = make_kernel(sample_repo, supplier=PySupplier(lambda wt, d: honest_fix(wt, d)))
    k.init()
    before = gitops.head_sha(sample_repo, "deficit-loop/docstrings")
    # simulate: supply committed, merge trial started, then death before verdict
    sup = "deficit-loop/docstrings-supply-1"
    gitops.create_branch(sample_repo, sup, before)
    wt = k.store.worktrees_dir / "supply-1"
    gitops.add_worktree(sample_repo, wt, sup)
    (wt / "src" / "text.py").write_text("def slugify(s):\n    return s\n")
    gitops.commit_all(wt, "half")
    assert gitops.merge_no_ff(k.judge_wt, sup, "trial")
    s = k.store.load()
    s.in_flight = {"n": 1, "branch": sup, "worktree": str(wt), "before_sha": before, "stage": "merge",
                   "supply_sha": gitops.head_sha(sample_repo, sup), "started_at": time.time()}
    s.owned_branches.append(sup)
    s.owned_worktrees.append(str(wt))
    k.store.save(s)

    state = k.run()
    assert state.status == ACHIEVED
    assert "half" not in git(sample_repo, "log", "--format=%s", "deficit-loop/docstrings")
    assert measure_branch(sample_repo, "deficit-loop/docstrings")["scope"] == 8


def test_second_runner_gets_busy(sample_repo):
    k = make_kernel(sample_repo)
    k.init()
    other = Store(sample_repo, "docstrings")
    other.acquire()
    try:
        with pytest.raises(BusyError):
            k.run()
    finally:
        other.release()
    assert k.run().status == ACHIEVED


def test_budget_exhausted(sample_repo):
    k = make_kernel(sample_repo, max_attempts=2)
    k.init()
    state = k.run()
    assert state.status == BUDGET_EXHAUSTED and len(state.attempts) == 2 and "max_attempts=2" in state.stop_reason
    assert len(state.current_deficits) == 3


def test_time_budget_exhausted(sample_repo):
    k = make_kernel(sample_repo, max_seconds=0.0)
    k.init()
    state = k.run()
    assert state.status == BUDGET_EXHAUSTED and state.attempts == [] and "max_seconds" in state.stop_reason


def test_external_failures_block_without_counting_as_defects(sample_repo):
    k = make_kernel(sample_repo, supplier=PySupplier(lambda wt, d: SupplyResult(False, True, "", "HTTP 429 rate limit", 1)), external_k=2)
    k.init()
    state = k.run()
    assert state.status == BLOCKED_EXTERNAL
    assert [a.verdict for a in state.attempts] == ["EXTERNAL_ERROR"] * 2
    assert state.rejects == {} and state.no_progress == 0            # nobody is blamed
    assert "retry later" in state.stop_reason
    # after the outage, `resume` reopens the same state and finishes; other terminal states stay closed
    k2 = make_kernel(sample_repo, supplier=PySupplier(lambda wt, d: honest_fix(wt, d)))
    s2 = k2.run(require_existing=True)
    assert s2.status == ACHIEVED and s2.consecutive_external == 0 and len(s2.attempts) == 7
    assert "reopen" in [e["event"] for e in k2.store.events()]
    assert k2.run().status == ACHIEVED and len(k2.store.load().attempts) == 7


def test_cli_resume_after_external_block(sample_repo):
    spec = sample_repo / "flaky.json"
    spec.write_text((sample_repo / "deficit.json").read_text().replace(
        '"command": ["python3", "tools/fill_docstring.py"]',
        '"command": ["python3", "-c", "import sys; print(\'HTTP 429 Too Many Requests\', file=sys.stderr); sys.exit(1)"]'))
    env = {**os.environ, "PYTHONPATH": str(ROOT)}

    def cli(*args):
        return subprocess.run([sys.executable, "-m", "deficit_loop", *args], cwd=sample_repo, env=env, capture_output=True, text=True)

    assert cli("--spec", "flaky.json", "init").returncode == 0
    r = cli("--spec", "flaky.json", "run")
    assert r.returncode == 4 and "BLOCKED_EXTERNAL" in r.stdout, r.stdout + r.stderr
    r = cli("resume")                                   # outage over: the normal supplier takes over
    assert r.returncode == 0 and "reopening" in r.stdout and "[finish] ACHIEVED" in r.stdout, r.stdout + r.stderr


def test_fix_with_nonzero_exit_is_still_measured_and_adopted(sample_repo):
    """The exit code is diagnostics, not a verdict: a real fix from a supplier that then crashes is adopted."""
    def fix_then_crash(wt, d):
        honest_fix(wt, d)
        return SupplyResult(False, False, "", "Traceback: boom after writing", 1)

    k = make_kernel(sample_repo, supplier=PySupplier(fix_then_crash))
    k.init()
    state = k.run()
    assert state.status == ACHIEVED
    assert all(a.verdict == "ACCEPT" and "supplier exited 1" in a.reason for a in state.attempts)


def test_interrupted_attempts_count_toward_budget(sample_repo):
    k = make_kernel(sample_repo, max_attempts=3)
    k.init()
    s = k.store.load()
    s.in_flight = {"n": 1, "branch": "deficit-loop/docstrings-supply-1", "worktree": str(k.store.worktrees_dir / "supply-1"),
                   "before_sha": gitops.head_sha(sample_repo, "deficit-loop/docstrings"), "stage": "supply", "started_at": time.time() - 5}
    s.owned_branches.append(s.in_flight["branch"])
    s.owned_worktrees.append(s.in_flight["worktree"])
    k.store.save(s)
    state = k.run()
    assert state.interrupted == 1 and state.elapsed_seconds >= 5
    assert state.status == BUDGET_EXHAUSTED and len(state.attempts) == 2 and "1 interrupted" in state.stop_reason
    assert state.attempts[0].n == 2                                   # attempt numbers never reuse a crashed one


def test_interrupted_init_can_be_rerun(sample_repo):
    k = make_kernel(sample_repo)
    k.store.event("init")
    from deficit_loop.state import INITIALIZING, State
    k.store.save(State(goal="docstrings", status=INITIALIZING, owned_branches=["deficit-loop/docstrings"], owned_worktrees=[str(k.judge_wt)]))
    git(sample_repo, "branch", "deficit-loop/docstrings")             # crashed after creating the branch, before the worktree
    with pytest.raises(KernelError, match="init was interrupted"):
        k.run()
    assert k.init().status == "RUNNING"
    assert k.run().status == ACHIEVED


def test_recovery_refuses_to_reset_a_judge_worktree_on_another_branch(sample_repo):
    k = make_kernel(sample_repo)
    k.init()
    before = gitops.head_sha(sample_repo, "deficit-loop/docstrings")
    s = k.store.load()
    s.in_flight = {"n": 1, "branch": "deficit-loop/docstrings-supply-1", "worktree": str(k.store.worktrees_dir / "supply-1"),
                   "before_sha": before, "stage": "merge", "started_at": 0}
    s.owned_branches.append(s.in_flight["branch"])
    k.store.save(s)
    git(sample_repo, "branch", "precious")
    (sample_repo / "x").write_text("")
    git(k.judge_wt, "checkout", "-q", "precious")                      # someone moved the judge worktree elsewhere
    (k.judge_wt / "untracked.txt").write_text("do not delete me")
    state = k.run()
    assert state.status == HALTED_UNSAFE and "precious" in state.stop_reason
    assert (k.judge_wt / "untracked.txt").exists()
    assert gitops.head_sha(sample_repo, "precious") == before


def test_stubborn_target_does_not_hog_the_loop(sample_repo):
    """A deficit the supplier cannot fix is rotated out; the others still get drained."""
    def fix_unless_slugify(wt, d):
        if d.target.endswith("slugify"):
            return SupplyResult(False, False, "", "cannot do it", 1)
        honest_fix(wt, d)

    k = make_kernel(sample_repo, supplier=PySupplier(fix_unless_slugify), stall_k=3, max_reject_per_key=2)
    k.init()
    state = k.run()
    assert state.status == STALLED and "none actionable" in state.stop_reason
    assert sum(a.verdict == "ACCEPT" for a in state.attempts) == 4
    assert state.rejects == {"missing_docstring\tsrc/text.py::slugify": 2}
    assert [d["target"] for d in state.current_deficits] == ["src/text.py::slugify"]


# ------------------------------------------------------------ ownership / safety


def test_init_refuses_foreign_branch_with_our_name(sample_repo):
    git(sample_repo, "branch", "deficit-loop/docstrings")
    with pytest.raises(gitops.UnsafeRepoError):
        make_kernel(sample_repo).init()


def test_shallow_clone_halts_before_touching_anything(sample_repo, tmp_path):
    shallow = tmp_path / "shallow"
    subprocess.run(["git", "clone", "-q", "--depth", "1", f"file://{sample_repo}", str(shallow)], check=True)
    with pytest.raises(gitops.UnsafeRepoError, match="shallow"):
        make_kernel(shallow).init()
    assert "deficit-loop/docstrings" not in git(shallow, "branch", "--format=%(refname:short)")


def test_run_halts_when_repo_turns_shallow_and_resumes_after_repair(sample_repo):
    k = make_kernel(sample_repo)
    k.init()
    (sample_repo / ".git" / "shallow").write_text(gitops.head_sha(sample_repo) + "\n")
    state = k.run()
    assert state.status == HALTED_UNSAFE and "shallow" in state.stop_reason and state.attempts == []
    (sample_repo / ".git" / "shallow").unlink()                        # `git fetch --unshallow` equivalent
    assert k.run(require_existing=True).status == ACHIEVED


def test_interrupted_uncommitted_merge_is_rolled_back_not_measured(sample_repo):
    """Crash after `git merge` staged files but before any commit: HEAD unchanged, MERGE_HEAD + merged files present.
    Measuring that tree would report the fix as done without it ever being committed."""
    k = make_kernel(sample_repo)
    k.init()
    before = gitops.head_sha(sample_repo, "deficit-loop/docstrings")
    sup = "deficit-loop/docstrings-supply-1"
    gitops.create_branch(sample_repo, sup, before)
    wt = k.store.worktrees_dir / "supply-1"
    gitops.add_worktree(sample_repo, wt, sup)
    honest_fix(wt, __import__("deficit_loop.spec", fromlist=["Deficit"]).Deficit("missing_docstring", "src/text.py::slugify"))
    gitops.commit_all(wt, "half")
    git(k.judge_wt, "merge", "--no-ff", "--no-commit", sup)
    assert gitops.merge_in_progress(k.judge_wt)
    s = k.store.load()
    s.in_flight = {"n": 1, "branch": sup, "worktree": str(wt), "before_sha": before, "stage": "merge", "started_at": time.time()}
    s.owned_branches.append(sup)
    s.owned_worktrees.append(str(wt))
    k.store.save(s)
    state = k.run()
    assert state.status == ACHIEVED and state.interrupted == 1
    assert not gitops.merge_in_progress(k.judge_wt)
    assert "half" not in git(sample_repo, "log", "--format=%s", "deficit-loop/docstrings")
    assert git(sample_repo, "log", "--format=%s", "--first-parent", "deficit-loop/docstrings").count("adopt") == 5


def test_recovery_refuses_when_someone_committed_on_the_integration_branch(sample_repo):
    k = make_kernel(sample_repo)
    k.init()
    before = gitops.head_sha(sample_repo, "deficit-loop/docstrings")
    sup = "deficit-loop/docstrings-supply-1"
    gitops.create_branch(sample_repo, sup, before)
    (k.judge_wt / "HUMAN.md").write_text("mine\n")
    gitops.commit_all(k.judge_wt, "human work on the integration branch")
    human = gitops.head_sha(sample_repo, "deficit-loop/docstrings")
    s = k.store.load()
    s.in_flight = {"n": 1, "branch": sup, "worktree": str(k.store.worktrees_dir / "supply-1"), "before_sha": before, "stage": "merge", "started_at": 0}
    s.owned_branches.append(sup)
    k.store.save(s)
    state = k.run()
    assert state.status == HALTED_UNSAFE and "not our trial merge" in state.stop_reason
    assert gitops.head_sha(sample_repo, "deficit-loop/docstrings") == human     # untouched


def test_init_repairs_an_interrupted_checkout(sample_repo):
    """Crash during `git worktree add`: the worktree is registered but its files are not checked out."""
    from deficit_loop.state import INITIALIZING, State
    k = make_kernel(sample_repo)
    k.store.event("init")
    k.store.save(State(goal="docstrings", status=INITIALIZING, owned_branches=["deficit-loop/docstrings"], owned_worktrees=[str(k.judge_wt)]))
    git(sample_repo, "branch", "deficit-loop/docstrings")
    k.judge_wt.parent.mkdir(parents=True)
    git(sample_repo, "worktree", "add", "--no-checkout", "-q", str(k.judge_wt), "deficit-loop/docstrings")
    assert not (k.judge_wt / "tools").exists()
    assert k.init().status == "RUNNING"
    assert "init_repair_worktree" in [e["event"] for e in k.store.events()]
    assert k.run().status == ACHIEVED


def test_git_failure_after_merge_is_rolled_back_and_resumable(sample_repo, monkeypatch):
    """git fails right after the trial merge advanced HEAD: the attempt must not be left half-judged."""
    real = gitops.head_sha
    calls = {"n": 0}

    def flaky_head_sha(repo, ref="HEAD"):
        if Path(repo) == k.judge_wt and calls["n"] == 0:
            calls["n"] += 1
            raise gitops.GitError("simulated git failure after merge")
        return real(repo, ref)

    k = make_kernel(sample_repo)
    k.init()
    before = gitops.head_sha(sample_repo, "deficit-loop/docstrings")
    monkeypatch.setattr(gitops, "head_sha", flaky_head_sha)
    state = k.run()
    assert state.status == "HALTED_ERROR" and "simulated git failure" in state.stop_reason
    assert state.in_flight is None and state.interrupted == 1
    assert gitops.head_sha(sample_repo, "deficit-loop/docstrings") == before      # trial merge rolled back
    assert not gitops.merge_in_progress(k.judge_wt)
    assert k.run(require_existing=True).status == ACHIEVED


def _journal_accepted_cleanup(k, sample_repo, iter_seconds=0.6, branch_deleted=True):
    """Simulate: attempt 1 accepted, verdict journaled (stage cleanup), worktree removed, supply branch
    deleted (branch_deleted=True) or still present (crash just before deletion), then death."""
    before = gitops.head_sha(sample_repo, "deficit-loop/docstrings")
    sup = "deficit-loop/docstrings-supply-1"
    gitops.create_branch(sample_repo, sup, before)
    wt = k.store.worktrees_dir / "supply-1"
    gitops.add_worktree(sample_repo, wt, sup)
    from deficit_loop.spec import Deficit
    honest_fix(wt, Deficit("missing_docstring", "src/text.py::slugify"))
    gitops.commit_all(wt, "deficit-loop supply #1")
    supply_sha = gitops.head_sha(sample_repo, sup)
    assert gitops.merge_no_ff(k.judge_wt, sup, "deficit-loop adopt")
    after = gitops.head_sha(sample_repo, "deficit-loop/docstrings")
    gitops.remove_worktree(sample_repo, wt)
    if branch_deleted:
        gitops.delete_branch(sample_repo, sup)                          # cleanup got this far, then died
    s = k.store.load()
    attempt = {"n": 1, "key": ["missing_docstring", "src/text.py::slugify"], "verdict": "ACCEPT", "stage": "done",
               "reason": "deficits 5 -> 4", "supply_branch": sup, "before_sha": before, "after_sha": after, "supplier_report": "", "seconds": 0.1}
    keys = sorted({(d["kind"], d["target"]) for d in s.current_deficits})
    s.in_flight = {"n": 1, "branch": sup, "worktree": str(wt), "before_sha": before, "supply_sha": supply_sha, "stage": "cleanup",
                   "started_at": time.time(), "attempt": attempt, "before_keys": keys, "iter_seconds": iter_seconds,
                   "after_keys": [k_ for k_ in keys if k_[1] != "src/text.py::slugify"]}
    s.owned_branches.append(sup)
    s.owned_worktrees.append(str(wt))
    k.store.save(s)
    return after


def test_crash_during_cleanup_after_verdict_is_completed_not_undone(sample_repo):
    """Crash after the verdict was journaled and the supply branch deleted, before the attempt was saved.
    The accepted merge must be kept and the attempt recorded, not rolled back or declared unsafe."""
    k = make_kernel(sample_repo, supplier=PySupplier(lambda wt, d: honest_fix(wt, d)))
    k.init()
    after = _journal_accepted_cleanup(k, sample_repo)
    state = k.run()
    assert state.status == ACHIEVED and state.interrupted == 0
    assert [a.n for a in state.attempts] == [1, 2, 3, 4, 5] and state.attempts[0].after_sha == after
    assert state.elapsed_seconds >= 0.6                                  # the interrupted iteration's time was restored
    assert git(sample_repo, "log", "--format=%s", "--first-parent", "deficit-loop/docstrings").count("adopt") == 5


def test_wall_clock_budget_bounds_slow_oracle_and_supplier(sample_repo):
    """Deadline is monotonic and shared by oracle and supplier: nothing starts or keeps running past it."""
    spec = sample_repo / "slow.json"
    spec.write_text((sample_repo / "deficit.json").read_text().replace(
        '"oracle": ["python3", "tools/measure_docstrings.py", "src"]',
        '"oracle": ["python3", "-c", "import time, runpy, sys; time.sleep(0.3); sys.argv=[\'m\', \'src\']; runpy.run_path(\'tools/measure_docstrings.py\', run_name=\'__main__\')"]').replace(
        '"max_seconds": 300', '"max_seconds": 1.0'))
    from deficit_loop.spec import GoalSpec
    k = Kernel(sample_repo, GoalSpec.load(spec), supplier=PySupplier(lambda wt, d: (time.sleep(0.6), honest_fix(wt, d))[1]), log=lambda *_: None)
    k.init()
    t0 = time.monotonic()
    state = k.run()
    wall = time.monotonic() - t0
    assert state.status == BUDGET_EXHAUSTED and "max_seconds" in state.stop_reason
    assert state.elapsed_seconds >= 1.0 and wall < 2.5, (state.elapsed_seconds, wall)
    assert len(state.attempts) <= 1


def test_expired_budget_never_launches_the_supplier(sample_repo, monkeypatch):
    """Setup (worktree creation) eats the last of the budget: the supplier must not be started at all."""
    real_add = gitops.add_worktree

    def slow_add(repo, path, branch):
        time.sleep(0.35)
        real_add(repo, path, branch)

    calls = []
    k = make_kernel(sample_repo, supplier=PySupplier(lambda wt, d: calls.append(d)), max_seconds=0.3)
    k.init()
    monkeypatch.setattr(gitops, "add_worktree", slow_add)
    state = k.run()
    assert state.status == BUDGET_EXHAUSTED and "before supply could start" in state.stop_reason
    assert calls == [] and state.attempts == [] and state.in_flight is None
    assert gitops.list_worktrees(sample_repo) == [sample_repo.resolve(), k.judge_wt.resolve()]   # setup undone


def test_in_process_cleanup_failure_charges_the_iteration_once(sample_repo, monkeypatch):
    """git fails during cleanup in the same process: run() charges the iteration, recovery must not add the journaled time again."""
    real_delete = gitops.delete_branch
    failed = []

    def flaky_delete(repo, name):
        if not failed:
            failed.append(name)
            raise gitops.GitError("simulated failure while deleting the supply branch")
        real_delete(repo, name)

    k = make_kernel(sample_repo, supplier=PySupplier(lambda wt, d: (time.sleep(1.0), honest_fix(wt, d))[1]), max_seconds=30)
    k.init()
    monkeypatch.setattr(gitops, "delete_branch", flaky_delete)
    state = k.run()
    assert state.status == "HALTED_ERROR" and state.in_flight is None
    assert len(state.attempts) == 1 and state.attempts[0].verdict == "ACCEPT" and state.attempts[0].seconds >= 1.0
    assert state.elapsed_seconds < 2.0, state.elapsed_seconds          # charged once (~1.2s), not twice (~2.4s)
    assert k.run(require_existing=True).status == ACHIEVED


def test_fresh_recovery_error_keeps_journaled_time_and_charges_once(sample_repo, monkeypatch):
    """A fresh process recovers a cleanup journal and hits one GitError on the way: the journaled
    iteration time must survive the retry and be added exactly once (it was never charged in this process)."""
    k = make_kernel(sample_repo, supplier=PySupplier(lambda wt, d: honest_fix(wt, d)))
    k.init()
    after = _journal_accepted_cleanup(k, sample_repo, iter_seconds=1.3, branch_deleted=False)
    real_delete = gitops.delete_branch
    failed = []

    def flaky_delete(repo, name):
        if not failed:
            failed.append(name)
            raise gitops.GitError("simulated transient failure during cleanup recovery")
        real_delete(repo, name)

    monkeypatch.setattr(gitops, "delete_branch", flaky_delete)
    state = make_kernel(sample_repo).run(require_existing=True)       # fresh Kernel, nothing charged yet
    assert state.status == "HALTED_ERROR" and "during cleanup recovery" in state.stop_reason and state.in_flight is None
    assert failed == ["deficit-loop/docstrings-supply-1"]                # the error hit during recovery, not a new attempt
    assert [a.n for a in state.attempts] == [1] and state.attempts[0].after_sha == after
    assert 1.3 <= state.elapsed_seconds < 1.4, state.elapsed_seconds   # journaled time kept, added once
    s2 = make_kernel(sample_repo).run(require_existing=True)
    assert s2.status == ACHIEVED and [a.n for a in s2.attempts] == [1, 2, 3, 4, 5]


def test_external_error_with_a_real_change_is_still_measured(sample_repo):
    """A supplier that fixed the deficit and then hit a rate limit: the fix is measured and adopted."""
    def fix_then_429(wt, d):
        honest_fix(wt, d)
        return SupplyResult(False, True, "", "HTTP 429 after writing", 1)

    k = make_kernel(sample_repo, supplier=PySupplier(fix_then_429))
    k.init()
    state = k.run()
    assert state.status == ACHIEVED and all(a.verdict == "ACCEPT" for a in state.attempts)


def test_foreign_worktree_under_our_dir_halts(sample_repo):
    k = make_kernel(sample_repo)
    k.init()
    git(sample_repo, "branch", "someone-elses")
    gitops.add_worktree(sample_repo, k.store.worktrees_dir / "stranger", "someone-elses")
    state = k.run()
    assert state.status == HALTED_UNSAFE and "not recorded as ours" in state.stop_reason
    assert (k.store.worktrees_dir / "stranger").exists()                 # not touched
    assert gitops.branch_exists(sample_repo, "someone-elses")


def test_discard_never_deletes_unowned(sample_repo):
    k = make_kernel(sample_repo)
    s = k.init()
    git(sample_repo, "branch", "keep-me")
    k._discard_supply(s, "keep-me", sample_repo / "nope")
    assert gitops.branch_exists(sample_repo, "keep-me")


def test_run_without_init_is_an_error(sample_repo):
    with pytest.raises(KernelError):
        make_kernel(sample_repo).run()
