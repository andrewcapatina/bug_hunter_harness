"""Input assembly — BUG_HUNTER_SPEC.md §1/§6.

Config-driven and side-effect-injected: `run` (a command → stdout), `read`
(repo-relative path → str|None), and `list_files` (glob → paths) are passed in,
so the same code serves the live harness and tests with no git/filesystem.

Produces the raw diff (units.py turns it into review units), the git log +
chain sidecars + bugs.md tail as context, the resolved-fingerprint set for
suppression (§6), and the first-seen registry that keeps display_ids stable.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

# Fixer/disposition outcomes that mean "don't re-emit this finding".
RESOLVED_OUTCOMES = {"applied", "fixed", "false_positive", "duplicate"}


@dataclass
class GatherResult:
    git_log: str
    diff: str
    diff_truncated: bool
    sidecars: list          # [{agent, status, reason, highlights}]
    bugs_md: str
    resolved: dict          # {fingerprint: reason} — do-not-re-emit (§6)
    first_seen: dict        # {fingerprint: date} — keeps display_ids stable (I5)


def _safe(run, args) -> str:
    try:
        return run(args) or ""
    except Exception:       # noqa: BLE001 — a failed git call degrades gracefully
        return ""


def git_log(run, since_hours) -> str:
    return _safe(run, ["git", "log", "--since", f"{since_hours} hours ago",
                       "--date=short", "--pretty=format:%h %ad %s"])


def git_diff(run, since_hours, max_chars) -> tuple[str, bool]:
    """Diff over the last `since_hours`; fall back to HEAD~5 when the reflog
    window is empty/absent (fresh clone). Capped at max_chars as a safety limit
    only — batching covers the whole diff, this just guards a runaway."""
    diff = _safe(run, ["git", "diff", f"HEAD@{{{since_hours} hours ago}}..HEAD"])
    if not diff.strip():
        diff = _safe(run, ["git", "diff", "HEAD~5..HEAD"])
    truncated = len(diff) > max_chars
    return (diff[:max_chars] if truncated else diff), truncated


def gather_sidecars(read, list_files, globs, run_id=None) -> list:
    out = []
    for g in globs or []:
        for path in sorted(list_files(g)):
            raw = read(path)
            if not raw:
                continue
            try:
                d = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                continue
            if run_id and d.get("run_id") not in (run_id, None):
                continue
            out.append({"agent": d.get("agent"), "status": d.get("status"),
                        "reason": d.get("reason"),
                        "highlights": (d.get("highlights") or [])[:8]})
    return out


def read_bugs_md(read, path, tail_chars=3000) -> str:
    raw = read(path) or ""
    return raw[-tail_chars:]


def gather_resolved(run, read, ledger_path, lookback_days=60) -> dict:
    """Fingerprints already resolved (fixed / false_positive / duplicate) — from
    the fixer ledger AND commit messages tagged `[auto-fix <fp>]`. Keyed by
    content fingerprint, so suppression survives across days (I5/§6)."""
    resolved: dict = {}
    for line in (read(ledger_path) or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        fp = rec.get("fingerprint") or rec.get("finding_id")
        outcome = (rec.get("outcome") or rec.get("disposition") or "").lower()
        if fp and outcome in RESOLVED_OUTCOMES:
            resolved[fp] = rec.get("reason") or rec.get("reasoning") or outcome
    log = _safe(run, ["git", "log", "--since", f"{lookback_days} days ago",
                      "--grep=auto-fix", "--pretty=format:%s"])
    for m in re.finditer(r"auto-fix\s+([0-9a-f]{8,16})", log):
        resolved.setdefault(m.group(1), "fixed (git)")
    return resolved


def read_first_seen(read, path) -> dict:
    raw = read(path) or ""
    try:
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, ValueError):
        return {}


def gather(config: dict, *, run, read, list_files, run_id=None) -> GatherResult:
    g = config.get("gather", {}) or {}
    diff, trunc = git_diff(run, g.get("since_hours", 24),
                           g.get("diff_max_chars", 200_000))
    return GatherResult(
        git_log=git_log(run, g.get("since_hours", 24)),
        diff=diff, diff_truncated=trunc,
        sidecars=gather_sidecars(read, list_files,
                                 g.get("reviewed_sidecars", []), run_id),
        bugs_md=read_bugs_md(read, g.get("bugs_md", "docs/bugs.md")),
        resolved=gather_resolved(run, read,
                                 (config.get("fixer") or {}).get("ledger_path", ""),
                                 g.get("resolved_lookback_days", 60)),
        first_seen=read_first_seen(read, g.get("first_seen_path", "")),
    )
