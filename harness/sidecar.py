"""Per-bot output adapter — BUG_HUNTER_SPEC.md §7/§8.

Maps the canonical RunResult to a bot's on-disk artifacts: the findings
sidecar (full JSON), the chain marker line (status/ts/run_id/reason), and the
persisted first-seen registry that keeps display_ids stable. Paths are config
(§8), so crypto's `state/_chain` + `.claude/agents` marker and options'
`.claude/runs` timestamped file are the same code, different config.

`build_artifacts` is pure ({path: content}); `write_result` writes them via an
injected `write` — so the mapping is fully testable, and production supplies an
atomic writer.
"""
from __future__ import annotations

import json


def build_artifacts(result, config: dict, run_id: str, now_iso: str) -> dict:
    sc = config.get("sidecar", {}) or {}
    findings_path = sc["findings_path"]
    marker_path = sc["marker_path"]
    first_seen_path = (config.get("gather", {}) or {}).get("first_seen_path")

    sidecar_json = {
        "agent": "bug_hunter",
        "status": result.status,
        "ts": now_iso,
        "run_id": run_id,
        "reason": result.reason,
        "n_findings": len(result.findings),
        "n_units": result.n_units,
        "n_batches": result.n_batches,
        "n_batch_failures": result.n_batch_failures,
        "suppressed": result.suppressed,
        "diff_truncated": result.diff_truncated,
        "findings": result.findings_as_dicts(),
        "refuted": result.refuted,
    }
    marker = (f"status={result.status} ts={now_iso} run_id={run_id} "
              f"reason={result.reason}\n")

    artifacts = {
        findings_path: json.dumps(sidecar_json, indent=2) + "\n",
        marker_path: marker,
    }
    if first_seen_path:
        artifacts[first_seen_path] = json.dumps(result.first_seen, indent=2) + "\n"
    return artifacts


def write_result(result, config: dict, run_id: str, now_iso: str, *, write) -> dict:
    artifacts = build_artifacts(result, config, run_id, now_iso)
    for path, content in artifacts.items():
        write(path, content)
    return artifacts


def append_ledger(outcomes, ledger_path: str, run_id: str, now_iso: str, *,
                  append) -> None:
    """Append fixer outcomes to the ledger (ndjson, one per line), keyed by
    (run_id, fingerprint) for idempotency (§6)."""
    for o in outcomes:
        rec = {"ts": now_iso, "run_id": run_id, "fingerprint": o.fingerprint,
               "outcome": o.outcome, "files_changed": o.files_changed,
               "commit_sha": o.commit_sha, "reason": o.reason}
        append(ledger_path, json.dumps(rec) + "\n")
