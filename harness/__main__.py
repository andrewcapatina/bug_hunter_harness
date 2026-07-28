"""CLI entry — `python -m harness --config configs/<bot>.json [--fix]`.

The single shared harness, one bot per config. Real adapters (git subprocess,
atomic file writes, py/json/bash syntax checks) live here; all the logic is in
the tested modules.
"""
from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from harness.fixer import run_fixer
from harness.gather import gather_resolved
from harness.model import ModelProfile, chat
from harness.run import run_bug_hunter
from harness.sidecar import append_ledger, write_result

FIX_SYSTEM = (
    "You propose a MINIMAL patch fixing ONE described bug. Output JSON only: "
    '{"reasoning": str, "patches": [{"file": str, "old_string": str, '
    '"new_string": str}]}. old_string MUST be a UNIQUE, verbatim snippet copied '
    "exactly from the file (whitespace included). Keep it tiny; change only what "
    "the fix requires. If unsure, return an empty patches list."
)


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

    def append(rel, content):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(content)

    return run, read, list_files, write, append


def _syntax_check(path, content):
    try:
        if path.endswith(".py"):
            compile(content, path, "exec")
        elif path.endswith(".json"):
            json.loads(content)
        return True, ""
    except (SyntaxError, ValueError) as e:
        return False, str(e)


def _make_propose(profile):
    def propose(finding, files):
        body = "\n\n".join(f"FILE {p}:\n{c}" for p, c in files.items())
        user = (f"BUG in {finding.path} ({finding.symbol}):\n{finding.hypothesis}\n"
                f"FIX IDEA: {finding.suggested_fix}\n\n{body}")
        data, _ = chat(profile, FIX_SYSTEM, user)
        return data
    return propose


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--diff-ref", default=None,
                    help="explicit git range (e.g. HEAD~3..HEAD) instead of the time window")
    ap.add_argument("--dry-run", action="store_true",
                    help="print findings JSON; write no sidecar/marker/ledger (parity gate)")
    ap.add_argument("--fix", action="store_true",
                    help="also run the guard-railed fixer on auto_applicable findings")
    a = ap.parse_args()

    config = json.loads(Path(a.config).read_text())
    if a.diff_ref:
        config.setdefault("gather", {})["diff_ref"] = a.diff_ref
    repo = config["repo_root"]
    run, read, list_files, write, append = _adapters(repo)
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

    if a.fix and result.findings:
        resolved = gather_resolved(run, read,
                                   config.get("fixer", {}).get("ledger_path", ""))
        outcomes = run_fixer(result.findings, config, read=read, write=write,
                             run=run, syntax_check=_syntax_check,
                             propose=_make_propose(profile), resolved=resolved)
        append_ledger(outcomes, config["fixer"]["ledger_path"], run_id,
                      now.isoformat(), append=append)

    print(f"STATUS={result.status}")
    print(f"REASON={result.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
