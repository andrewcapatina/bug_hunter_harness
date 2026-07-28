"""Orchestrator — BUG_HUNTER_SPEC.md.

Wires the pipeline: gather → whole-unit build → batch → model → normalize
(calibrate + in-window gate) → resolved-suppression → semantic dedup →
adversarial verify → status. Pure of I/O beyond the injected run/read/list_files
and the profile-driven model client, so the whole run is testable end-to-end
with no git or LLM. Writing the result to a bot's sidecar/marker is a separate
per-bot adapter (sidecar.py); this returns the canonical result.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from harness.batch import plan_batches
from harness.dedup import dedupe
from harness.findings import Finding, normalize_batch
from harness.gather import gather
from harness.model import chat_batch
from harness.prompt import build_system_prompt
from harness.status import classify_status
from harness.units import build_units
from harness.verify import verify_findings


@dataclass
class RunResult:
    status: str
    reason: str
    findings: list          # verified, deduped Findings
    refuted: list           # [{fingerprint, display_id, reason}] dropped by verify
    first_seen: dict        # updated registry to persist (keeps display_ids stable)
    n_units: int
    n_batches: int
    n_batch_failures: int
    diff_truncated: bool
    suppressed: int         # resolved findings dropped before dedup

    def findings_as_dicts(self) -> list:
        return [f.to_dict() for f in self.findings]


def run_bug_hunter(config: dict, profile, *, run, read, list_files, today: str,
                   run_id: str | None = None) -> RunResult:
    g = gather(config, run=run, read=read, list_files=list_files, run_id=run_id)

    if not g.diff.strip():
        return RunResult("PASS", "no code changes in the window", [], [],
                         g.first_seen, 0, 0, 0, g.diff_truncated, 0)

    units = build_units(g.diff, read)
    if not units:
        return RunResult("PASS", "no reviewable definitions changed", [], [],
                         g.first_seen, 0, 0, 0, g.diff_truncated, 0)

    system = build_system_prompt(config, g.resolved)
    plan = plan_batches(units, profile, system)             # [(idx, user, units)]
    results = chat_batch(profile, [(f"batch{idx}", system, user)
                                   for idx, user, _ in plan])

    def first_seen_lookup(fp):
        return g.first_seen.get(fp)

    all_findings: list[Finding] = []
    n_fail = 0
    for (idx, _user, bunits), res in zip(plan, results):
        if not res.ok:
            n_fail += 1
            continue
        raw = (res.data or {}).get("findings", [])
        all_findings.extend(normalize_batch(
            raw, bunits, today=today, first_seen_lookup=first_seen_lookup,
            found_by=f"batch{idx}"))

    # §6 suppression: drop already-resolved fingerprints unless flagged a regression.
    kept, suppressed = [], 0
    for f in all_findings:
        if f.fingerprint in g.resolved and not f.regression_of:
            suppressed += 1
        else:
            kept.append(f)

    deduped = dedupe(kept)
    verified, refuted = verify_findings(deduped, units, profile)

    status, reason = classify_status(
        verified, n_batches=len(plan), n_batch_failures=n_fail,
        gather_failed=False)

    # Persist first-seen for stable display_ids: new fingerprints -> today.
    new_first_seen = dict(g.first_seen)
    for f in verified:
        new_first_seen.setdefault(f.fingerprint, today)

    return RunResult(status, reason, verified, refuted, new_first_seen,
                     len(units), len(plan), n_fail, g.diff_truncated, suppressed)
