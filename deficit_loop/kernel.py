"""The loop: measure -> pick one deficit -> supply in isolation -> judge by re-measuring -> adopt or discard.

Ends only in a terminal status with a recorded reason:
  ACHIEVED          oracle reports zero deficits on the integration branch            (final)
  STALLED           k consecutive attempts changed nothing (or every remaining deficit is exhausted)  (final)
  BUDGET_EXHAUSTED  attempts or wall-clock budget spent                                (final)
  BLOCKED_EXTERNAL  the supplier keeps failing for infrastructure reasons              (`resume` reopens)
  HALTED_UNSAFE     git state makes ownership undecidable; nothing was touched         (`resume` re-checks)
  HALTED_ERROR      the oracle or git itself failed on the integration branch          (`resume` reopens)
"""
from __future__ import annotations

import time
from pathlib import Path

from . import gitops, state as st
from .judge import Guards, Verdict, judge
from .oracle import Measurement, OracleError, measure
from .spec import Deficit, GoalSpec
from .state import Attempt, State, Store
from .supplier import Supplier, SupplyResult, build_supplier


class KernelError(RuntimeError):
    pass


class Kernel:
    def __init__(self, repo: Path, spec: GoalSpec, guards: Guards | None = None,
                 supplier: Supplier | None = None, log=print):
        self.repo = gitops.repo_root(repo)
        self.spec = spec
        self.guards = guards or Guards()
        self.store = Store(self.repo, spec.name)
        self._supplier = supplier
        self.log = log
        self._iter_t0: float | None = None     # monotonic start of the current loop iteration

    # ------------------------------------------------------------- budget
    def _charge(self, state: State) -> None:
        """Add the current iteration's wall time to the persisted budget (idempotent per iteration)."""
        if self._iter_t0 is not None:
            state.elapsed_seconds += time.monotonic() - self._iter_t0
            self._iter_t0 = None

    def _remaining(self, state: State) -> float:
        """Seconds left in the wall-clock budget right now, counting the running iteration."""
        running = (time.monotonic() - self._iter_t0) if self._iter_t0 is not None else 0.0
        return self.spec.budget.max_seconds - state.elapsed_seconds - running

    def _measure(self, state: State, wt: Path) -> Measurement:
        remaining = self._remaining(state)
        if remaining <= 0:
            raise OracleError("time budget expired before the oracle could start")
        return measure(self.spec, wt, timeout=min(self.spec.budget.oracle_timeout, remaining))

    @property
    def judge_wt(self) -> Path:
        return self.store.worktrees_dir / "judge"

    # ---------------------------------------------------------------- init
    def init(self) -> State:
        """Create the integration branch + judgment worktree and measure the baseline.

        Every resource is recorded as ours *before* it is created, so an interrupted
        init can be rerun: `init` continues an INITIALIZING state instead of refusing,
        and repairs a half-checked-out judgment worktree (everything there is ours).
        """
        gitops.preflight(self.repo)
        gitops.exclude_path(self.repo, st.STATE_DIRNAME)
        self.store.acquire()
        try:
            branch = self.spec.integration_branch
            if self.store.exists():
                state = self.store.load()
                if state.status != st.INITIALIZING:
                    raise KernelError(f"goal {self.spec.name!r} already initialised at {self.store.dir}; use run/resume")
                self.store.event("init_resume")
            else:
                if gitops.branch_exists(self.repo, branch):
                    raise gitops.UnsafeRepoError(f"branch {branch} already exists and is not recorded as ours; refusing to reuse it")
                if self.judge_wt.exists():
                    raise gitops.UnsafeRepoError(f"{self.judge_wt} already exists but no state records it; refusing to touch it")
                state = State(goal=self.spec.name, status=st.INITIALIZING, created_at=time.time(),
                              owned_branches=[branch], owned_worktrees=[str(self.judge_wt)])
                self.store.event("init", goal=self.spec.name, head=gitops.head_sha(self.repo), branch=branch)
                self.store.save(state)                      # ownership recorded first
            if not gitops.branch_exists(self.repo, branch):
                gitops.create_branch(self.repo, branch, "HEAD")
            if self.judge_wt.resolve() not in gitops.list_worktrees(self.repo):
                if self.judge_wt.exists():
                    raise gitops.UnsafeRepoError(f"{self.judge_wt} exists but is not a registered worktree; refusing to touch it")
                gitops.add_worktree(self.repo, self.judge_wt, branch)
            self._verify_judge_worktree(branch)
            if gitops.tracked_dirty(self.judge_wt) or gitops.merge_in_progress(self.judge_wt):
                gitops.reset_hard(self.judge_wt, gitops.head_sha(self.repo, branch))   # interrupted checkout: ours, repair
                self.store.event("init_repair_worktree")
            m = measure(self.spec, self.judge_wt)
            state.baseline_scope = m.scope
            state.initial_deficits = [d.to_dict() for d in m.deficits]
            state.current_deficits = state.initial_deficits
            state.status = st.RUNNING
            self.store.event("baseline", scope=m.scope, deficits=len(m.deficits))
            self.store.save(state)
            self.log(f"[init] {self.spec.name}: {len(m.deficits)} deficits, scope={m.scope}, branch={branch}")
            return state
        finally:
            self.store.release()

    # ---------------------------------------------------------------- run
    def run(self, require_existing: bool = False) -> State:
        if not self.store.exists():
            raise KernelError(f"no state for goal {self.spec.name!r}; run `init` first")
        self.store.acquire()
        try:
            state = self.store.load()
            if state.status == st.INITIALIZING:
                raise KernelError("init was interrupted; rerun `init` to complete it")
            if state.status in st.FINAL:
                self.log(f"[run] already {state.status}: {state.stop_reason}")
                return state
            if state.status in st.TERMINAL:                       # a halt is a pause with a reason, not the end
                self.store.event("reopen", from_status=state.status, reason=state.stop_reason)
                state.status, state.stop_reason, state.consecutive_external = st.RUNNING, "", 0
                self.store.save(state)
                self.log(f"[run] reopening after {state.stop_reason or 'halt'}")
            try:
                gitops.preflight(self.repo)
                self._verify_ownership(state)     # before touching anything
                self._recover(state)
                self._loop(state)
            except gitops.UnsafeRepoError as e:
                self._charge(state)
                self._finish(state, st.HALTED_UNSAFE, str(e))
            except (OracleError, gitops.GitError) as e:
                charged = self._iter_t0 is not None       # did THIS process run (and therefore charge) the iteration?
                self._charge(state)
                self._halt_error(state, e, charged)
            return state
        finally:
            self.store.release()

    def _halt_error(self, state: State, err: Exception, charged: bool) -> None:
        """An operational failure mid-attempt: roll the attempt back now if we safely can, then halt.
        *charged* = this process already charged the iteration's time (in-process failure); when the failure
        happened while recovering someone else's journal (fresh process), the journaled time is still owed."""
        reason = f"oracle failed on the integration branch: {err}" if isinstance(err, OracleError) else f"git failed: {err}"
        if state.in_flight:
            try:
                self._recover(state, charge_time=not charged)
            except gitops.UnsafeRepoError as e:
                return self._finish(state, st.HALTED_UNSAFE, f"{reason}; and recovery refused: {e}")
            except gitops.GitError as e:
                return self._finish(state, st.HALTED_ERROR, f"{reason}; recovery also failed ({e}); attempt left in_flight for `resume`")
        self._finish(state, st.HALTED_ERROR, reason)

    # ------------------------------------------------------------ ownership
    def _verify_ownership(self, state: State) -> None:
        branch = self.spec.integration_branch
        if branch not in state.owned_branches or not gitops.branch_exists(self.repo, branch):
            raise gitops.UnsafeRepoError(f"integration branch {branch} missing or not ours")
        ours = {Path(p).resolve() for p in state.owned_worktrees}
        base = self.store.worktrees_dir.resolve()
        for wt in gitops.list_worktrees(self.repo):
            if wt.is_relative_to(base) and wt not in ours:
                raise gitops.UnsafeRepoError(f"worktree {wt} is under our directory but not recorded as ours")
        if self.judge_wt.resolve() not in gitops.list_worktrees(self.repo):
            if self.judge_wt.exists():
                raise gitops.UnsafeRepoError(f"{self.judge_wt} exists but is not a registered worktree; refusing to touch it")
            gitops.add_worktree(self.repo, self.judge_wt, branch)   # ours by record; recreate
        self._verify_judge_worktree(branch)
        # with no attempt in flight the judgment tree must already be clean; otherwise someone else edited it
        if not state.in_flight and (gitops.tracked_dirty(self.judge_wt) or gitops.merge_in_progress(self.judge_wt)):
            raise gitops.UnsafeRepoError("judgment worktree has uncommitted tracked changes that no attempt explains; refusing to reset them")

    def _verify_judge_worktree(self, branch: str) -> None:
        """The judgment worktree must be a registered worktree checked out on *our* integration branch."""
        if self.judge_wt.resolve() not in gitops.list_worktrees(self.repo):
            raise gitops.UnsafeRepoError(f"{self.judge_wt} is not a registered worktree of this repository")
        on = gitops.current_branch(self.judge_wt)
        if on != branch:
            raise gitops.UnsafeRepoError(f"judgment worktree is on {on or 'detached HEAD'}, expected {branch}; refusing to reset it")

    # ------------------------------------------------------------ recovery
    def _recover(self, state: State, charge_time: bool = True) -> None:
        """Finish or undo whatever a previous crash left half-done.

        stage "supply"  : nothing merged -> discard the supply.
        stage "merge"   : a trial merge may be in progress or committed -> roll the judgment tree back to before_sha.
        stage "cleanup" : the verdict was already reached and journaled -> keep it (accepted merge stays,
                          rejected tree is already reset), finish the cleanup and record the attempt.
        """
        inf = state.in_flight
        if not inf:
            return
        self.store.event("recover", in_flight=inf)
        if inf.get("stage") == "cleanup":
            return self._finish_cleanup(state, inf, charge_time)
        if inf.get("stage") == "merge":
            self._rollback_merge_trial(inf)
        self._discard_supply(state, inf["branch"], Path(inf["worktree"]))
        state.interrupted += 1
        if charge_time:
            budget = self.spec.budget
            state.elapsed_seconds += min(max(0.0, time.time() - inf.get("started_at", time.time())), budget.supply_timeout + budget.oracle_timeout)
        state.in_flight = None
        self.store.save(state)
        self.log(f"[recover] discarded unfinished attempt {inf.get('n')} on {inf.get('branch')}")

    def _finish_cleanup(self, state: State, inf: dict, charge_time: bool = True) -> None:
        """A crash after the verdict: the judgment tree must be exactly where the verdict left it.
        charge_time=False when the caller already charged this iteration (in-process error path)."""
        a = Attempt(**inf["attempt"])
        expected = a.after_sha or inf["before_sha"]
        head = gitops.head_sha(self.judge_wt)
        if head != expected or gitops.merge_in_progress(self.judge_wt) or gitops.tracked_dirty(self.judge_wt):
            raise gitops.UnsafeRepoError(f"integration branch HEAD {head[:10]} is not where attempt {a.n}'s verdict left it ({expected[:10]}); refusing to continue")
        self._discard_supply(state, inf["branch"], Path(inf["worktree"]))     # idempotent
        if charge_time:
            state.elapsed_seconds += float(inf.get("iter_seconds", 0.0))       # the iteration's time, journaled with the verdict
        self._record_attempt(state, a, frozenset(tuple(k) for k in inf["before_keys"]), frozenset(tuple(k) for k in inf["after_keys"]) if inf.get("after_keys") is not None else None)
        self.log(f"[recover] completed attempt {a.n} ({a.verdict}) whose cleanup was interrupted")

    def _rollback_merge_trial(self, inf: dict) -> None:
        """Put the judgment worktree back to before_sha. Only our own trial merge may be discarded:
        HEAD must be before_sha itself (merge aborted / in progress / uncommitted) or a merge commit
        whose parents are exactly (before_sha, the supply commit journaled at merge time). Anything else is someone's work."""
        before, supply_sha = inf["before_sha"], inf.get("supply_sha")
        head = gitops.head_sha(self.judge_wt)
        if head != before and (supply_sha is None or gitops.parents(self.repo, head) != [before, supply_sha]):
            raise gitops.UnsafeRepoError(f"integration branch HEAD {head[:10]} is not our trial merge of {before[:10]}; refusing to reset it")
        gitops.reset_hard(self.judge_wt, before)              # also aborts a half-done merge and drops its index/worktree state

    # ---------------------------------------------------------------- loop
    def _loop(self, state: State) -> None:
        budget = self.spec.budget
        supplier = self._supplier or build_supplier(self.spec)
        while True:
            self._iter_t0 = time.monotonic()
            if self._remaining(state) <= 0:
                return self._finish(state, st.BUDGET_EXHAUSTED, f"max_seconds={budget.max_seconds} reached, {len(state.current_deficits)} deficits remain")
            if gitops.merge_in_progress(self.judge_wt) or gitops.tracked_dirty(self.judge_wt):
                raise gitops.UnsafeRepoError("judgment worktree is not a clean checkout of the integration branch; refusing to measure it")
            try:
                before = self._measure(state, self.judge_wt)
            except OracleError as e:
                if "time budget expired" in str(e) or self._remaining(state) <= 0:
                    return self._finish(state, st.BUDGET_EXHAUSTED, f"max_seconds={budget.max_seconds} reached ({e}), {len(state.current_deficits)} deficits remain")
                raise
            state.current_deficits = [d.to_dict() for d in before.deficits]
            self.store.save(state)
            if not before.deficits:
                return self._finish(state, st.ACHIEVED, "oracle reports 0 deficits")
            used = len(state.attempts) + state.interrupted
            if used >= budget.max_attempts:
                return self._finish(state, st.BUDGET_EXHAUSTED, f"max_attempts={budget.max_attempts} reached ({state.interrupted} interrupted), {len(before.deficits)} deficits remain")
            if self._remaining(state) <= 0:
                return self._finish(state, st.BUDGET_EXHAUSTED, f"max_seconds={budget.max_seconds} reached, {len(before.deficits)} deficits remain")
            target = self._pick(state, before.deficits)
            if target is None:
                return self._finish(state, st.STALLED, f"all {len(before.deficits)} remaining deficits rejected {budget.max_reject_per_key}x; none actionable")

            result = self._attempt(state, supplier, target, before, used + 1)
            if result is None:                       # deadline passed before the supplier could start
                return self._finish(state, st.BUDGET_EXHAUSTED, f"max_seconds={budget.max_seconds} reached before supply could start, {len(before.deficits)} deficits remain")
            attempt, after_keys = result
            self._record_attempt(state, attempt, before.keys, after_keys)

            if state.consecutive_external >= budget.external_k:
                return self._finish(state, st.BLOCKED_EXTERNAL,
                                    f"{state.consecutive_external} consecutive external supplier failures (last: {attempt.reason}); not a defect, retry later with `resume`")
            if self.guards.stall_detection and state.no_progress >= budget.stall_k:
                return self._finish(state, st.STALLED, f"{state.no_progress} consecutive attempts without progress (stall_k={budget.stall_k}); last: {attempt.reason}")

    def _record_attempt(self, state: State, attempt: Attempt, before_keys: frozenset, after_keys: frozenset | None) -> None:
        state.attempts.append(attempt)
        self._charge(state)
        self._account(state, attempt, before_keys, after_keys)
        state.in_flight = None
        self.store.event("attempt_end", n=attempt.n, verdict=attempt.verdict, stage=attempt.stage, reason=attempt.reason)
        self.store.save(state)
        self.log(f"[{attempt.n:>3}] {attempt.key[0]}:{attempt.key[1]} -> {attempt.verdict} ({attempt.stage}: {attempt.reason})")

    def _pick(self, state: State, deficits: list[Deficit]) -> Deficit | None:
        last_tried = {tuple(a.key): a.n for a in state.attempts}
        actionable = [d for d in deficits if state.rejects.get(State.key_str(d.key), 0) < self.spec.budget.max_reject_per_key]
        if not actionable:
            return None
        # least rejected first, then least recently tried: a stubborn target does not hog the loop
        return min(actionable, key=lambda d: (state.rejects.get(State.key_str(d.key), 0), last_tried.get(d.key, 0)))

    def _account(self, state: State, a: Attempt, before_keys: frozenset, after_keys: frozenset | None) -> None:
        if a.verdict == "BUDGET_ABORT":
            return                                   # not judged: no reject, no stall, no external count
        if a.verdict == "EXTERNAL_ERROR":
            state.consecutive_external += 1
            return                                   # neither a reject nor a stall step
        state.consecutive_external = 0
        # progress = the deficit set on the integration branch changed; a discarded supply leaves it as it was
        if a.verdict == "ACCEPT" and after_keys is not None and after_keys != before_keys:
            state.no_progress = 0
        else:
            state.no_progress += 1
        if a.verdict == "REJECT":
            k = State.key_str(tuple(a.key))
            state.rejects[k] = state.rejects.get(k, 0) + 1

    # -------------------------------------------------------------- attempt
    def _attempt(self, state: State, supplier: Supplier, target: Deficit, before: Measurement, n: int) -> tuple[Attempt, frozenset | None] | None:
        branch = f"{self.spec.integration_branch}-supply-{n}"
        wt = self.store.worktrees_dir / f"supply-{n}"
        before_sha = gitops.head_sha(self.repo, self.spec.integration_branch)
        if gitops.branch_exists(self.repo, branch) or wt.exists():
            raise gitops.UnsafeRepoError(f"{branch} / {wt} already exists but is not recorded as ours")
        state.in_flight = {"n": n, "branch": branch, "worktree": str(wt), "before_sha": before_sha, "stage": "supply", "started_at": time.time()}
        state.owned_branches.append(branch)
        state.owned_worktrees.append(str(wt))
        self.store.event("attempt_start", n=n, kind=target.kind, target=target.target, branch=branch, before_sha=before_sha)
        self.store.save(state)                                   # recorded as ours before it exists
        t0 = time.monotonic()
        gitops.create_branch(self.repo, branch, before_sha)
        gitops.add_worktree(self.repo, wt, branch)
        # No try/finally: if anything below raises, the in_flight journal drives recovery.
        remaining = self._remaining(state)
        if remaining <= 0:                           # expired while setting up: do not launch anything
            self._discard_supply(state, branch, wt)
            state.in_flight = None
            self.store.event("budget_expired_before_supply", n=n)
            self.store.save(state)
            return None
        res = supplier.supply(self.spec, target, wt, min(self.spec.budget.supply_timeout, remaining))
        verdict, after_sha, changed, after_keys = self._judge_supply(state, branch, wt, before_sha, target, before, res, n)
        self._write_supply_log(n, target, res, verdict, before_sha, branch, changed)
        attempt = Attempt(n=n, key=list(target.key), verdict=verdict_name(verdict, res), stage=verdict.stage, reason=verdict.reason,
                          supply_branch=branch, before_sha=before_sha, after_sha=after_sha,
                          supplier_report=res.report[-2000:], seconds=round(time.monotonic() - t0, 3))
        # Journal the verdict BEFORE deleting the supply branch: a crash during cleanup can then be finished, not undone.
        state.in_flight = {**(state.in_flight or {}), "stage": "cleanup", "attempt": attempt.__dict__,
                           "iter_seconds": time.monotonic() - self._iter_t0 if self._iter_t0 is not None else 0.0,
                           "before_keys": sorted(before.keys), "after_keys": sorted(after_keys) if after_keys is not None else None}
        self.store.event("verdict", n=n, verdict=attempt.verdict, stage=attempt.stage, reason=attempt.reason, after_sha=after_sha)
        self.store.save(state)
        self._discard_supply(state, branch, wt)
        return attempt, after_keys

    def _judge_supply(self, state: State, branch: str, wt: Path, before_sha: str, target: Deficit,
                      before: Measurement, res: SupplyResult, n: int) -> tuple[Verdict, str, list[str], frozenset | None]:
        # Exit code and stderr are diagnostics only. Whatever the supplier *changed* is measured, even if it
        # reported failure or hit a rate limit on the way; only "changed nothing" skips measurement.
        exit_note = "" if res.ok else (f" (external error, exit {res.returncode}: {_tail(res.stderr or res.report, 80)})" if res.external_error
                                       else f" (supplier exited {res.returncode}: {_tail(res.stderr, 80)})")
        if not gitops.commit_all(wt, f"deficit-loop supply #{n}: {target.kind}:{target.target}"):
            return Verdict(False, "supply" if res.external_error else "commit",
                           f"supplier changed nothing{exit_note or ' but reported success'}"), "", [], None
        changed = gitops.changed_paths(self.repo, before_sha, branch)
        supply_sha = gitops.head_sha(self.repo, branch)
        state.in_flight = {**(state.in_flight or {}), "stage": "merge", "supply_sha": supply_sha}
        self.store.event("merge_trial", branch=branch, before_sha=before_sha, supply_sha=supply_sha, changed=changed)
        self.store.save(state)
        if not gitops.merge_no_ff(self.judge_wt, branch, f"deficit-loop adopt {target.kind}:{target.target}"):
            return Verdict(False, "merge", "merge conflict with integration branch" + exit_note), "", changed, None
        if self._remaining(state) <= 0:              # no time left to judge: discard without blaming the deficit
            gitops.reset_hard(self.judge_wt, before_sha)
            return Verdict(False, "budget", "time budget expired before the verdict measurement; not judged"), "", changed, None
        try:
            after = self._measure(state, self.judge_wt)
        except OracleError as e:
            gitops.reset_hard(self.judge_wt, before_sha)
            return Verdict(False, "measure", f"oracle failed after supply: {e}"), "", changed, None
        v = judge(self.guards, target.key, before, after, state.baseline_scope, changed, self.spec.protected_paths, res)
        v = Verdict(v.accept, v.stage, v.reason + exit_note)
        if v.accept:
            return v, gitops.head_sha(self.judge_wt), changed, after.keys
        gitops.reset_hard(self.judge_wt, before_sha)
        return v, "", changed, None

    def _write_supply_log(self, n: int, target: Deficit, res: SupplyResult, v: Verdict, before_sha: str, branch: str, changed: list[str]) -> None:
        self.store.supply_logs.mkdir(parents=True, exist_ok=True)
        diff = gitops.git(self.repo, "diff", f"{before_sha}..{branch}", check=False).stdout if changed else ""
        (self.store.supply_logs / f"{n:03d}.out").write_text(
            f"# attempt {n} {target.kind}:{target.target}\n# verdict: {verdict_name(v, res)} stage={v.stage} reason={v.reason}\n"
            f"# supplier exit={res.returncode} external={res.external_error}\n--- stdout (self-report, not trusted) ---\n{res.report}\n--- stderr ---\n{res.stderr}\n--- diff ---\n{diff}")

    def _discard_supply(self, state: State, branch: str, wt: Path) -> None:
        """Remove a supply worktree+branch. Refuses anything not recorded as ours; forgets only what was really removed."""
        if str(wt) in state.owned_worktrees:
            gitops.remove_worktree(self.repo, wt)        # raises on failure -> stays recorded
            state.owned_worktrees.remove(str(wt))
        if branch in state.owned_branches:
            if gitops.branch_exists(self.repo, branch):
                gitops.delete_branch(self.repo, branch)
            state.owned_branches.remove(branch)
        self.store.save(state)

    def _finish(self, state: State, status: str, reason: str) -> None:
        self._charge(state)
        state.status, state.stop_reason = status, reason
        self.store.event("finish", status=status, reason=reason, attempts=len(state.attempts))
        self.store.save(state)
        self.log(f"[finish] {status}: {reason}")


def verdict_name(v: Verdict, res: SupplyResult) -> str:
    if v.accept:
        return "ACCEPT"
    if v.stage == "budget":
        return "BUDGET_ABORT"                        # discarded unjudged; blames nobody
    return "EXTERNAL_ERROR" if res.external_error else "REJECT"


def _tail(s: str, n: int = 160) -> str:
    s = s.strip().replace("\n", " | ")
    return s[-n:] if len(s) > n else s
