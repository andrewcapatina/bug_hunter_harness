"""Batch packing + rendering — BUG_HUNTER_SPEC.md §2/§3.

Pack WHOLE review units into batches sized to the model profile's context
window (never a hardcoded char budget). Invariant I1: a unit is never split
across batches — a single unit larger than the budget is sent whole in its own
batch. Each unit is rendered with absolute line numbers and its changed lines
marked, so a finding can cite an exact file:line (I2) and the model can see
which lines actually changed.
"""
from __future__ import annotations

from math import ceil

from harness.units import ReviewUnit

# Heuristic: ~4 chars/token. Good enough for packing; the profile's window is
# the real ceiling and we keep a margin below it.
CHARS_PER_TOKEN = 4
PER_UNIT_OVERHEAD_TOKENS = 48        # header + line-number framing per unit
SAFETY_MARGIN_TOKENS = 512


def estimate_tokens(text: str) -> int:
    return ceil(len(text) / CHARS_PER_TOKEN)


def unit_tokens(u: ReviewUnit) -> int:
    return estimate_tokens(u.source) + PER_UNIT_OVERHEAD_TOKENS


def input_budget(profile, system_prompt: str) -> int:
    """Tokens available for the units in one request: the context window minus
    the output reservation, the system prompt, and a safety margin."""
    budget = (profile.context_window_tokens
              - profile.max_output_tokens
              - estimate_tokens(system_prompt)
              - SAFETY_MARGIN_TOKENS)
    return max(budget, 1)


def pack_batches(units: list[ReviewUnit], budget_tokens: int) -> list[list[ReviewUnit]]:
    """Greedy pack, order-preserving. A unit that alone exceeds the budget gets
    its own batch (whole — never split)."""
    batches: list[list[ReviewUnit]] = []
    cur: list[ReviewUnit] = []
    cur_tok = 0
    for u in units:
        ut = unit_tokens(u)
        if ut >= budget_tokens:              # oversized: flush, then send alone
            if cur:
                batches.append(cur)
                cur, cur_tok = [], 0
            batches.append([u])
            continue
        if cur and cur_tok + ut > budget_tokens:
            batches.append(cur)
            cur, cur_tok = [], 0
        cur.append(u)
        cur_tok += ut
    if cur:
        batches.append(cur)
    return batches


def _changed(line: int, ranges: list[tuple[int, int]]) -> bool:
    return any(s <= line <= e for s, e in ranges)


def render_unit(u: ReviewUnit) -> str:
    """The unit with absolute line numbers; changed lines flagged with '*'
    right after the colon so the model can tell edits from context."""
    changed = ", ".join(f"L{s}" if s == e else f"L{s}-L{e}"
                        for s, e in u.changed_lines) or "(none)"
    head = f"{u.path} :: {u.symbol}   [changed: {changed}]"
    out = [head]
    for i, line in enumerate(u.source.splitlines()):
        n = u.start_line + i
        mark = "*" if _changed(n, u.changed_lines) else " "
        out.append(f"{n:>5}:{mark}{line}")
    return "\n".join(out)


def render_batch(units: list[ReviewUnit], batch_idx: int, n_batches: int) -> str:
    files = sorted({u.path for u in units})
    header = (f"## Review batch {batch_idx} of {n_batches} "
              f"(files: {', '.join(files)})\n"
              f"Each block below is a WHOLE definition. Lines are absolute; "
              f"changed lines are marked with '*'. Report ONLY issues evidenced "
              f"within a block shown here — do not infer code you cannot see.")
    return header + "\n\n" + "\n\n".join(render_unit(u) for u in units)


def plan_batches(units: list[ReviewUnit], profile, system_prompt: str):
    """Convenience: pack units to the profile budget and render each batch.
    Returns [(batch_idx, rendered_user_prompt, [units]), ...]."""
    budget = input_budget(profile, system_prompt)
    packed = pack_batches(units, budget)
    n = len(packed)
    return [(i + 1, render_batch(b, i + 1, n), b) for i, b in enumerate(packed)]
