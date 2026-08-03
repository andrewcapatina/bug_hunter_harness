"""Status derivation — BUG_HUNTER_SPEC.md §7.

Shared, identical for every bot. Advisory only — the harness never halts
trading and never emits HARD_HALT. A partial run (some batches failed) can't
read as clean: it floors to WARN, because silence would be a lie.
"""
from __future__ import annotations


def classify_status(findings, *, n_batches: int, n_batch_failures: int,
                    gather_failed: bool = False,
                    batch_errors: list | None = None) -> tuple[str, str]:
    if gather_failed:
        return "SOFT_HALT", "input gather failed — nothing reviewed"
    if n_batches > 0 and n_batch_failures >= n_batches:
        # Report what ACTUALLY failed. This used to hardcode "(model
        # unreachable)" for every batch failure — a cause it never checked.
        # On 2026-08-02 the crypto chain reported "model unreachable" while
        # the endpoint was serving fine, which sent the diagnosis in exactly
        # the wrong direction. BatchResult.error already carries the real
        # message; it was being discarded.
        detail = ""
        if batch_errors:
            uniq = list(dict.fromkeys(e for e in batch_errors if e))
            if uniq:
                detail = f" — {uniq[0][:200]}"
                if len(uniq) > 1:
                    detail += f" (+{len(uniq) - 1} other error(s))"
        return "SOFT_HALT", f"all {n_batches} batches failed{detail}"

    n = len(findings)
    n_high = sum(1 for f in findings if f.severity == "high")
    n_med = sum(1 for f in findings if f.severity == "medium")
    n_low = n - n_high - n_med
    detail = f"{n_high} high, {n_med} medium, {n_low} low"
    if n_batch_failures:
        detail += f"; {n_batch_failures}/{n_batches} batches failed"

    if n_batch_failures or n_high or n_med >= 3 or n >= 6:
        return "WARN", f"{n} findings — {detail}"
    return "PASS", (f"{n} findings — {detail}" if n else "no findings")
