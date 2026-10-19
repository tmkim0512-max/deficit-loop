"""deficit-loop: an autonomous loop that drains a measurable goal.

A goal is a list of deficits produced by an oracle command. A supplier removes
one deficit at a time inside an isolated git worktree. An independent judge
re-measures and adopts the supply only when the deficit is gone and nothing
new broke. The loop stops with a recorded reason: ACHIEVED, STALLED,
BUDGET_EXHAUSTED, BLOCKED_EXTERNAL or HALTED_UNSAFE.
"""
__version__ = "0.1.0"
