"""CLI entry — `python -m harness --config configs/<bot>.json`.

The single shared harness, one bot per config. Real adapters (git subprocess,
atomic file writes) live here; all the logic is in the tested modules.

Fixing is trading-harness-bug-fixer's job since 2026-07-30 (BUG_FIXER_SPEC.md
supersedes BUG_HUNTER_SPEC §6): the `--fix` flag and the in-repo propose/
syntax helpers were retired at that cutover. harness/fixer.py stays as the
vendored seed + tests, no longer invocable from here.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from harness.model import ModelProfile
from harness.run import run_bug_hunter
from harness.sidecar import write_result


def _adapters(repo: str):
    root = Path(repo)

    def run(args):
        return subprocess.run(args, cwd=str(root), capture_output=True,
                              text=True, timeout=300).stdout

    def read(rel):
        p = root / rel
        try:
            return p.read_text() if p.is_file() else None
        except OSError:
            return None

    def list_files(pattern):
        return [str(p.relative_to(root)) for p in root.glob(pattern) if p.is_file()]

    def write(rel, content):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(content)
        tmp.replace(p)

    return run, read, list_files, write


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--diff-ref", default=None,
                    help="explicit git range (e.g. HEAD~3..HEAD) instead of the time window")
    ap.add_argument("--dry-run", action="store_true",
                    help="print findings JSON; write no sidecar/marker/ledger (parity gate)")
    # --fix retired 2026-07-30: fixing is trading-harness-bug-fixer's job
    # (BUG_FIXER_SPEC.md supersedes BUG_HUNTER_SPEC §6). harness/fixer.py
    # remains as the vendored seed + its tests; it is no longer invocable.
    a = ap.parse_args()

    config = json.loads(Path(a.config).read_text())
    if a.diff_ref:
        config.setdefault("gather", {})["diff_ref"] = a.diff_ref
    repo = config["repo_root"]
    run, read, list_files, write = _adapters(repo)
    profile = ModelProfile.from_config(config["model_profile"])

    now = datetime.now(timezone.utc)
    run_id = a.run_id or now.strftime("%Y%m%dT%H%M%SZ")

    result = run_bug_hunter(config, profile, run=run, read=read,
                            list_files=list_files, today=now.date().isoformat(),
                            run_id=run_id)

    if a.dry_run:
        print(json.dumps({
            "status": result.status, "reason": result.reason,
            "n_units": result.n_units, "n_batches": result.n_batches,
            "n_batch_failures": result.n_batch_failures,
            "suppressed": result.suppressed, "diff_truncated": result.diff_truncated,
            "refuted": result.refuted,
            "findings": result.findings_as_dicts(),
        }, indent=2))
        return 0

    write_result(result, config, run_id, now.isoformat(), write=write)

    print(f"STATUS={result.status}")
    print(f"REASON={result.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
