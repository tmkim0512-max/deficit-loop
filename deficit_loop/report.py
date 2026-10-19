"""Human-readable status / report rendering (plain text tables, no dependencies)."""
from __future__ import annotations

from .state import State


def _table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(headers)]
    fmt = "  ".join("{:<" + str(w) + "}" for w in widths)
    lines = [fmt.format(*headers), fmt.format(*("-" * w for w in widths))]
    lines += [fmt.format(*r) for r in rows]
    return "\n".join(lines)


def status(state: State) -> str:
    accepted = sum(1 for a in state.attempts if a.verdict == "ACCEPT")
    rejected = sum(1 for a in state.attempts if a.verdict == "REJECT")
    external = sum(1 for a in state.attempts if a.verdict == "EXTERNAL_ERROR")
    lines = [
        f"goal:        {state.goal}",
        f"status:      {state.status}" + (f" — {state.stop_reason}" if state.stop_reason else ""),
        f"deficits:    {len(state.initial_deficits)} initial -> {len(state.current_deficits)} remaining",
        f"attempts:    {len(state.attempts)} (accept {accepted} / reject {rejected} / external {external})",
        f"no_progress: {state.no_progress} consecutive",
        f"elapsed:     {state.elapsed_seconds:.1f}s",
        f"baseline:    scope={state.baseline_scope}",
    ]
    if state.in_flight:
        lines.append(f"in_flight:   attempt {state.in_flight.get('n')} stage={state.in_flight.get('stage')} (will be discarded on resume)")
    return "\n".join(lines)


def report(state: State) -> str:
    rows = [[str(a.n), f"{a.key[0]}:{a.key[1]}", a.verdict, a.stage, a.reason[:70], f"{a.seconds:.1f}"] for a in state.attempts]
    remaining = [[d["kind"], d["target"], d.get("detail", "")[:60]] for d in state.current_deficits]
    out = [status(state), "", "attempts:", _table(["#", "deficit", "verdict", "stage", "reason", "sec"], rows)]
    if remaining:
        out += ["", "remaining deficits:", _table(["kind", "target", "detail"], remaining)]
    return "\n".join(out)
