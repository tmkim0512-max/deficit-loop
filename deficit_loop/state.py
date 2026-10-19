"""Crash-safe run state.

* ``state.json``  — whole state, written atomically (tmp + fsync + rename).
* ``events.jsonl`` — append-only audit log. Each step appends its event before
  saving the state that reflects it. ``state.json`` is the authority for
  recovery; the log is for reading what happened (it may lack the very last
  event if the process dies between the two writes, never the reverse).
* ``lock``        — ``flock`` exclusive lock; a second runner exits BUSY.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, IO

STATE_DIRNAME = ".deficit-loop"

# terminal statuses
ACHIEVED = "ACHIEVED"
STALLED = "STALLED"
BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
BLOCKED_EXTERNAL = "BLOCKED_EXTERNAL"
HALTED_UNSAFE = "HALTED_UNSAFE"
HALTED_ERROR = "HALTED_ERROR"
RUNNING = "RUNNING"
INITIALIZING = "INITIALIZING"
TERMINAL = {ACHIEVED, STALLED, BUDGET_EXHAUSTED, BLOCKED_EXTERNAL, HALTED_UNSAFE, HALTED_ERROR}
FINAL = {ACHIEVED, STALLED, BUDGET_EXHAUSTED}      # the rest are halts: `resume` reopens them


class BusyError(RuntimeError):
    pass


@dataclass
class Attempt:
    n: int
    key: list[str]
    verdict: str          # ACCEPT | REJECT | EXTERNAL_ERROR
    stage: str            # supply | commit | merge | measure | guard | done
    reason: str
    supply_branch: str
    before_sha: str
    after_sha: str = ""
    supplier_report: str = ""
    seconds: float = 0.0


@dataclass
class State:
    goal: str
    status: str = RUNNING
    stop_reason: str = ""
    created_at: float = 0.0
    elapsed_seconds: float = 0.0
    baseline_scope: int | None = None
    initial_deficits: list[dict[str, str]] = field(default_factory=list)
    current_deficits: list[dict[str, str]] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)
    rejects: dict[str, int] = field(default_factory=dict)       # "kind\ttarget" -> count
    no_progress: int = 0
    consecutive_external: int = 0
    interrupted: int = 0            # attempts killed mid-way; they count against max_attempts
    owned_branches: list[str] = field(default_factory=list)
    owned_worktrees: list[str] = field(default_factory=list)
    in_flight: dict[str, Any] | None = None   # supply/merge in progress (for crash recovery)

    @staticmethod
    def key_str(key: tuple[str, str]) -> str:
        return f"{key[0]}\t{key[1]}"

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "State":
        raw = json.loads(text)
        raw["attempts"] = [Attempt(**a) for a in raw.get("attempts", [])]
        return cls(**raw)


class Store:
    def __init__(self, repo: Path, goal: str):
        self.dir = repo / STATE_DIRNAME / goal
        self.state_path = self.dir / "state.json"
        self.events_path = self.dir / "events.jsonl"
        self.lock_path = self.dir / "lock"
        self.worktrees_dir = self.dir / "wt"
        self.supply_logs = self.dir / "supply"
        self._lock_fh: IO[str] | None = None

    def exists(self) -> bool:
        return self.state_path.exists()

    def load(self) -> State:
        return State.from_json(self.state_path.read_text())

    def save(self, state: State) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        with open(tmp, "w") as f:
            f.write(state.to_json())
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_path)
        dfd = os.open(self.dir, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)

    def event(self, name: str, **data: Any) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"ts": time.time(), "event": name, **data}, ensure_ascii=False).encode()
        with open(self.events_path, "ab+") as f:        # bytes: a record torn inside a UTF-8 sequence must not break appends
            f.seek(0, os.SEEK_END)
            if f.tell() > 0:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    f.write(b"\n")                      # the torn record stays its own (corrupt) line
            f.write(line + b"\n")
            f.flush()
            os.fsync(f.fileno())

    def events(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        out = []
        for line in self.events_path.read_bytes().decode("utf-8", errors="replace").splitlines():
            if line.strip():
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    out.append({"event": "corrupt_line", "raw": line})  # a torn last line is reported, not hidden
        return out

    def acquire(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "w")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as e:
            fh.close()
            raise BusyError(f"another runner holds {self.lock_path}") from e
        fh.write(str(os.getpid()))
        fh.flush()
        self._lock_fh = fh

    def release(self) -> None:
        if self._lock_fh is not None:
            fcntl.flock(self._lock_fh.fileno(), fcntl.LOCK_UN)
            self._lock_fh.close()
            self._lock_fh = None
