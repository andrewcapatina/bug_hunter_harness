"""Guard-railed scripted fixer — BUG_HUNTER_SPEC.md §6.

The LLM only proposes patches; Python enforces safety. Every fix must clear:
allow/deny path lists, a size cap, an exactly-once old_string match, a syntax
check, and all-or-nothing application — else it's an attempted_failed, no write.
Commits are tagged `[auto-fix <fingerprint>]`; the ledger is keyed by content
fingerprint (idempotent, stable across runs). A consecutive-failure breaker
stops a runaway.

All I/O injected (read/write/run/syntax_check) and the patch proposal (`propose`)
too, so the whole thing is testable with no git or model.
"""
from __future__ import annotations

import fnmatch
from dataclasses import dataclass

MAX_PATCH_LINES = 30


@dataclass
class FixOutcome:
    fingerprint: str
    outcome: str            # applied | attempted_failed | skipped_blocked
                            # | skipped_no_file | already_resolved | halted
    files_changed: list
    commit_sha: str | None
    reason: str


def path_allowed(path: str, allowed: list, blocked: list) -> bool:
    """Allow-list semantics: a blocked glob wins; otherwise the path MUST match
    an allowed glob. An empty allow-list allows nothing (fail-closed)."""
    for pat in blocked or []:
        if fnmatch.fnmatch(path, pat):
            return False
    return any(fnmatch.fnmatch(path, pat) for pat in (allowed or []))


def _patch_lines_ok(patch: dict, max_lines: int) -> bool:
    old = patch.get("old_string", "")
    new = patch.get("new_string", "")
    return max(len(old.splitlines()), len(new.splitlines())) <= max_lines


def validate_patches(patches, read, allowed, blocked, max_lines) -> tuple[bool, str]:
    if not patches:
        return False, "no patches proposed"
    for p in patches:
        path = p.get("file")
        if not path:
            return False, "patch missing file"
        if not path_allowed(path, allowed, blocked):
            return False, f"path not allowed: {path}"
        content = read(path)
        if content is None:
            return False, f"file not found: {path}"
        old = p.get("old_string", "")
        if not old:
            return False, "empty old_string"
        n = content.count(old)
        if n == 0:
            return False, f"old_string not found in {path}"
        if n > 1:
            return False, f"old_string not unique in {path} ({n}x)"
        if not _patch_lines_ok(p, max_lines):
            return False, f"patch exceeds {max_lines} lines"
    return True, "ok"


def apply_in_memory(patches, read) -> dict:
    """Apply all patches to in-memory copies (exactly-once replace). Returns
    {path: new_content}. Assumes validation passed."""
    edited: dict[str, str] = {}
    for p in patches:
        path = p["file"]
        content = edited.get(path, read(path))
        edited[path] = content.replace(p["old_string"], p["new_string"], 1)
    return edited


def apply_fix(finding, config, *, read, write, run, syntax_check, propose) -> FixOutcome:
    fx = config.get("fixer", {}) or {}
    allowed = fx.get("allowed_paths", [])
    blocked = fx.get("blocked_paths", [])
    max_lines = fx.get("max_patch_lines", MAX_PATCH_LINES)

    if not path_allowed(finding.path, allowed, blocked):
        return FixOutcome(finding.fingerprint, "skipped_blocked", [], None,
                          f"cited path not allowed: {finding.path}")
    if read(finding.path) is None:
        return FixOutcome(finding.fingerprint, "skipped_no_file", [], None,
                          f"cited file missing: {finding.path}")

    proposal = propose(finding, {finding.path: read(finding.path)}) or {}
    patches = proposal.get("patches", [])
    ok, reason = validate_patches(patches, read, allowed, blocked, max_lines)
    if not ok:
        return FixOutcome(finding.fingerprint, "attempted_failed", [], None, reason)

    edited = apply_in_memory(patches, read)
    for path, content in edited.items():
        sok, serr = syntax_check(path, content)
        if not sok:
            return FixOutcome(finding.fingerprint, "attempted_failed", [], None,
                              f"syntax check failed for {path}: {serr}")

    for path, content in edited.items():
        write(path, content)
    files = sorted(edited)
    run(["git", "add", *files])
    run(["git", "commit", "-m",
         f"[auto-fix {finding.fingerprint}] {finding.title}"])
    sha = (run(["git", "rev-parse", "HEAD"]) or "").strip() or None
    return FixOutcome(finding.fingerprint, "applied", files, sha, "applied")


def run_fixer(findings, config, *, read, write, run, syntax_check, propose,
              resolved) -> list:
    """Apply auto_applicable findings not already resolved, in order. A
    consecutive-failure breaker (config fixer.consecutive_failure_halt) stops a
    runaway. Returns FixOutcomes; the caller persists them to the ledger."""
    fx = config.get("fixer", {}) or {}
    halt = int(fx.get("consecutive_failure_halt", 3))
    outcomes: list[FixOutcome] = []
    consecutive = 0
    for f in findings:
        if f.auto_fix_class != "auto_applicable":
            continue
        if f.fingerprint in (resolved or {}):
            outcomes.append(FixOutcome(f.fingerprint, "already_resolved", [],
                                       None, "already in ledger"))
            continue
        out = apply_fix(f, config, read=read, write=write, run=run,
                        syntax_check=syntax_check, propose=propose)
        outcomes.append(out)
        if out.outcome == "attempted_failed":
            consecutive += 1
            if consecutive >= halt:
                outcomes.append(FixOutcome("-", "halted", [], None,
                                           f"{halt} consecutive failures"))
                break
        else:
            consecutive = 0
    return outcomes
