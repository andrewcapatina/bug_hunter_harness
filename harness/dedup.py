"""Semantic dedup — BUG_HUNTER_SPEC.md §5.

Both prior harnesses deduped structurally on (file, title) + a hypothesis hash,
which misses reworded restatements — the reason one freshness bug was reported
8× in a single run (different files cited, different wording each time). Here we
cluster by exact content fingerprint AND by hypothesis/title similarity, then
collapse each cluster to its most-severe member with all sources recorded.

Cluster membership uses the FIRST member as the representative (no chaining), so
near-identical restatements collapse without genuinely distinct bugs being
absorbed.
"""
from __future__ import annotations

import re
from dataclasses import replace

from harness.findings import Finding

DEFAULT_THRESHOLD = 0.5
MIN_SHARED_TOKENS = 3        # guard: a tiny finding can't merge into a big one
_SEV = {"low": 0, "medium": 1, "high": 2}
_CONF = {"low": 0, "medium": 1, "high": 2}

# Filler words dilute similarity so real restatements score low on Jaccard.
# Strip them (and pure numbers / 1-char tokens) so distinctive terms dominate.
_STOPWORDS = {
    "the", "a", "an", "is", "are", "be", "of", "to", "in", "on", "and", "or",
    "not", "when", "while", "for", "this", "that", "it", "its", "as", "with",
    "by", "if", "then", "so", "but", "will", "would", "may", "might", "can",
    "could", "does", "do", "has", "have", "per", "at", "from", "into", "than",
    "only", "also", "which", "where", "what", "was", "were", "no", "any",
}


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z]+", (text or "").lower())
            if len(w) > 1 and w not in _STOPWORDS}


def _overlap(a: set, b: set) -> float:
    """Overlap coefficient |a∩b|/min(|a|,|b|) — forgiving of length differences,
    unlike Jaccard, so a terse and a verbose restatement of one bug still match."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _similar(a: set, b: set, threshold: float) -> bool:
    return len(a & b) >= MIN_SHARED_TOKENS and _overlap(a, b) >= threshold


def _rep_tokens(f: Finding) -> set:
    return _tokens(f.title + " " + f.hypothesis)


def dedupe(findings: list[Finding], threshold: float = DEFAULT_THRESHOLD) -> list[Finding]:
    """Collapse duplicate + near-duplicate findings. Returns one Finding per
    cluster (the most-severe/confident member), with `found_by` listing every
    source label and a `(xN)` suffix when N>1 members merged."""
    clusters: list[dict] = []
    for f in findings:
        ftok = _rep_tokens(f)
        placed = False
        for cl in clusters:
            if f.fingerprint in cl["fps"] or _similar(ftok, cl["rep_tok"], threshold):
                cl["members"].append(f)
                cl["fps"].add(f.fingerprint)
                placed = True
                break
        if not placed:
            clusters.append({"fps": {f.fingerprint}, "rep_tok": ftok, "members": [f]})

    out: list[Finding] = []
    for cl in clusters:
        members = cl["members"]
        best = max(members, key=lambda f: (_SEV[f.severity], _CONF[f.confidence],
                                           len(f.hypothesis)))
        labels = sorted({m.found_by for m in members if m.found_by})
        found_by = ", ".join(labels)
        if len(members) > 1:
            found_by = f"{found_by} (x{len(members)})"
        out.append(replace(best, found_by=found_by))
    return out
