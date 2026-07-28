"""Tests for the per-bot output adapter (SPEC §7/§8)."""
import json
import unittest

from harness.sidecar import build_artifacts, write_result, append_ledger
from harness.run import RunResult
from harness.findings import Finding
from harness.fixer import FixOutcome


def _result(status="WARN"):
    f = Finding("fp1", "BH-x-fp1", "mod.py", "foo", 2, "high", "high", "logic",
                "trig", "title", "hyp", "fix", "auto_applicable", None, "b1")
    return RunResult(status=status, reason="1 findings — 1 high, 0 medium, 0 low",
                     findings=[f], refuted=[{"fingerprint": "fp2", "reason": "refuted"}],
                     first_seen={"fp1": "2026-07-28"}, n_units=1, n_batches=1,
                     n_batch_failures=0, diff_truncated=False, suppressed=1)


CONFIG = {"sidecar": {"findings_path": "state/_chain/bug_hunter_latest.json",
                      "marker_path": ".claude/agents/bug_hunter_last_run.txt"},
          "gather": {"first_seen_path": "state/bug_first_seen.json"}}


class BuildTests(unittest.TestCase):
    def test_produces_three_artifacts(self):
        arts = build_artifacts(_result(), CONFIG, "R1", "2026-07-28T00:00:00Z")
        self.assertEqual(set(arts), {
            "state/_chain/bug_hunter_latest.json",
            ".claude/agents/bug_hunter_last_run.txt",
            "state/bug_first_seen.json"})

    def test_sidecar_json_shape(self):
        arts = build_artifacts(_result(), CONFIG, "R1", "2026-07-28T00:00:00Z")
        d = json.loads(arts["state/_chain/bug_hunter_latest.json"])
        self.assertEqual(d["agent"], "bug_hunter")
        self.assertEqual(d["status"], "WARN")
        self.assertEqual(d["run_id"], "R1")
        self.assertEqual(d["n_findings"], 1)
        self.assertEqual(d["suppressed"], 1)
        self.assertEqual(d["findings"][0]["fingerprint"], "fp1")
        self.assertEqual(len(d["refuted"]), 1)

    def test_marker_line_format(self):
        arts = build_artifacts(_result(), CONFIG, "R1", "2026-07-28T00:00:00Z")
        marker = arts[".claude/agents/bug_hunter_last_run.txt"]
        self.assertTrue(marker.startswith(
            "status=WARN ts=2026-07-28T00:00:00Z run_id=R1 reason="))

    def test_first_seen_persisted(self):
        arts = build_artifacts(_result(), CONFIG, "R1", "2026-07-28T00:00:00Z")
        self.assertEqual(json.loads(arts["state/bug_first_seen.json"]),
                         {"fp1": "2026-07-28"})

    def test_write_result_writes_all(self):
        store = {}
        write_result(_result(), CONFIG, "R1", "2026-07-28T00:00:00Z",
                     write=lambda p, c: store.__setitem__(p, c))
        self.assertEqual(len(store), 3)


class LedgerTests(unittest.TestCase):
    def test_append_ledger_one_line_per_outcome(self):
        lines = []
        append_ledger([FixOutcome("fp1", "applied", ["mod.py"], "sha", "ok"),
                       FixOutcome("fp2", "attempted_failed", [], None, "ambiguous")],
                      "ledger.ndjson", "R1", "2026-07-28T00:00:00Z",
                      append=lambda p, c: lines.append((p, c)))
        self.assertEqual(len(lines), 2)
        rec = json.loads(lines[0][1])
        self.assertEqual(rec["fingerprint"], "fp1")
        self.assertEqual(rec["outcome"], "applied")
        self.assertEqual(rec["run_id"], "R1")


if __name__ == "__main__":
    unittest.main()
