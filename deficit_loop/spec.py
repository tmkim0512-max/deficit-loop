"""Goal spec: what to measure, how to supply, when to stop.

A goal is a *measurable list of deficits*. The oracle command prints JSON:

    {"deficits": [{"kind": "...", "target": "...", "detail": "..."}, ...],
     "scope": <int, optional: how many things were measured>}

or just the bare list ``[{"kind": ..., "target": ..., "detail": ...}, ...]``.

Two deficits are the same deficit when ``(kind, target)`` match.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EXTERNAL_CMD_ENV = "DEFICIT_LOOP_AGENT_CMD"


class SpecError(ValueError):
    pass


@dataclass(frozen=True)
class Deficit:
    kind: str
    target: str
    detail: str = ""

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.target)

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "target": self.target, "detail": self.detail}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Deficit":
        try:
            return cls(kind=str(d["kind"]), target=str(d["target"]), detail=str(d.get("detail", "")))
        except (KeyError, TypeError) as e:
            raise SpecError(f"deficit needs 'kind' and 'target': {d!r}") from e


@dataclass(frozen=True)
class Budget:
    max_attempts: int = 30
    max_seconds: float = 1800.0
    stall_k: int = 3          # consecutive attempts with an unchanged deficit set -> STALLED
    max_reject_per_key: int = 2  # a deficit rejected this many times is no longer actionable
    external_k: int = 2       # consecutive external supplier failures -> BLOCKED_EXTERNAL
    supply_timeout: float = 600.0
    oracle_timeout: float = 300.0


@dataclass(frozen=True)
class GoalSpec:
    name: str
    oracle: list[str]
    supplier_kind: str                 # "script" | "external"
    supplier_command: list[str] | None  # for "script"
    protected_paths: tuple[str, ...] = ()  # paths a supply must not touch (oracle tooling etc.)
    budget: Budget = field(default_factory=Budget)

    @property
    def integration_branch(self) -> str:
        return f"deficit-loop/{self.name}"

    def resolve_external_command(self) -> str | None:
        """External agent CLI, taken from the environment. ``None`` = not configured."""
        return os.environ.get(EXTERNAL_CMD_ENV) or None

    @classmethod
    def load(cls, path: Path) -> "GoalSpec":
        try:
            raw = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise SpecError(f"cannot read spec {path}: {e}") from e
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "GoalSpec":
        try:
            name = str(raw["name"])
            oracle = [str(x) for x in raw["oracle"]]
            sup = raw["supplier"]
            kind = str(sup["kind"])
        except (KeyError, TypeError) as e:
            raise SpecError(f"spec needs name, oracle, supplier.kind: {e}") from e
        if not name or "/" in name or name.startswith("."):
            raise SpecError(f"bad goal name {name!r} (used as a branch segment)")
        if kind == "script":
            cmd = sup.get("command")
            if not isinstance(cmd, list) or not cmd:
                raise SpecError("supplier.kind=script needs a non-empty 'command' list")
            command: list[str] | None = [str(x) for x in cmd]
        elif kind == "external":
            command = None
        else:
            raise SpecError(f"unknown supplier.kind {kind!r} (script|external)")
        budget = Budget(**{k: v for k, v in raw.get("budget", {}).items() if k in Budget.__dataclass_fields__})
        return cls(
            name=name,
            oracle=oracle,
            supplier_kind=kind,
            supplier_command=command,
            protected_paths=tuple(str(p) for p in raw.get("protected_paths", [])),
            budget=budget,
        )


def parse_measurement(text: str) -> tuple[list[Deficit], int | None]:
    """Parse oracle stdout into (deficits, scope)."""
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise SpecError(f"oracle did not print JSON: {e}: {text[:200]!r}") from e
    if isinstance(raw, list):                       # bare list form, no scope
        raw = {"deficits": raw}
    if not isinstance(raw, dict) or not isinstance(raw.get("deficits"), list):
        raise SpecError("oracle JSON must be a list of deficits or an object with a 'deficits' list")
    deficits = [Deficit.from_dict(d) for d in raw["deficits"]]
    scope = raw.get("scope")
    if scope is not None and not isinstance(scope, int):
        raise SpecError("oracle 'scope' must be an integer when present")
    # same key twice is one deficit
    seen: dict[tuple[str, str], Deficit] = {}
    for d in deficits:
        seen.setdefault(d.key, d)
    return list(seen.values()), scope
