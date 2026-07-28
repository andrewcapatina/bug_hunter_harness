"""End-to-end orchestrator + status tests (SPEC — whole pipeline).

Mocks only the model transport: batch calls return findings, refute calls
(system contains 'REFUTE') return a verdict. No git, no LLM, no filesystem.
"""
import json
import unittest
from unittest import mock

from harness import model
from harness.findings import Finding, fingerprint
from harness.model import ModelProfile
from harness.run import run_bug_hunter
from harness.status import classify_status


def _sev(sev):
    f = mock.Mock(); f.severity = sev; return f


class StatusTests(unittest.TestCase):
    def test_pass_when_empty(self):
        self.assertEqual(classify_status([], n_batches=1, n_batch_failures=0)[0], "PASS")

    def test_warn_on_high(self):
        self.assertEqual(
            classify_status([_sev("high")], n_batches=1, n_batch_failures=0)[0], "WARN")

    def test_warn_on_three_medium(self):
        self.assertEqual(
            classify_status([_sev("medium")] * 3, n_batches=1, n_batch_failures=0)[0],
            "WARN")

    def test_warn_on_partial_batch_failure_even_with_no_findings(self):
        s, r = classify_status([], n_batches=3, n_batch_failures=1)
        self.assertEqual(s, "WARN")
        self.assertIn("batches failed", r)

    def test_soft_halt_when_all_batches_fail(self):
        self.assertEqual(
            classify_status([], n_batches=2, n_batch_failures=2)[0], "SOFT_HALT")

    def test_soft_halt_on_gather_failure(self):
        self.assertEqual(
            classify_status([], n_batches=0, n_batch_failures=0, gather_failed=True)[0],
            "SOFT_HALT")


MOD = "def foo(x):\n    y = x + 1\n    return y"
DIFF = ("diff --git a/mod.py b/mod.py\n--- a/mod.py\n+++ b/mod.py\n"
        "@@ -2,1 +2,1 @@ def foo(x):\n-    y = x + 0\n+    y = x + 1\n")
HYP = "y is computed but the result is wrong on negative input"
PROFILE = ModelProfile(endpoint="http://x/v1", model="m",
                       context_window_tokens=8000, max_output_tokens=1000)


def _config(ledger=""):
    return {
        "gather": {"since_hours": 24, "reviewed_sidecars": [],
                   "bugs_md": "docs/bugs.md", "first_seen_path": "reg.json"},
        "fixer": {"ledger_path": "ledger.ndjson"},
        "identity_preamble": "review the test bot",
        "_ledger": ledger,
    }


def _files(cfg):
    return {"mod.py": MOD, "reg.json": "{}", "ledger.ndjson": cfg.get("_ledger", "")}


def _run_with(cfg, *, finding_line=2, verify_upheld=True):
    files = _files(cfg)
    run = lambda a: DIFF if "HEAD@{24 hours ago}..HEAD" in " ".join(a) else ""
    read = lambda p: files.get(p)
    list_files = lambda g: []

    def post(url, payload, timeout, headers):
        system = payload["messages"][0]["content"]
        if "REFUTE" in system:
            return {"choices": [{"message": {"content":
                    json.dumps({"upheld": verify_upheld, "reason": "checked"})},
                    "finish_reason": "stop"}], "usage": {}}
        finding = {"path": "mod.py", "line": finding_line, "severity": "high",
                   "confidence": "high", "category": "logic",
                   "trigger": "foo(-1) reaches it", "title": "wrong on negatives",
                   "hypothesis": HYP, "suggested_fix": "guard x<0"}
        return {"choices": [{"message": {"content":
                json.dumps({"findings": [finding]})}, "finish_reason": "stop"}],
                "usage": {}}

    with mock.patch.object(model, "_http_post", side_effect=post):
        return run_bug_hunter(cfg, PROFILE, run=run, read=read,
                              list_files=list_files, today="2026-07-28")


class RunTests(unittest.TestCase):
    def test_valid_finding_upheld_yields_warn(self):
        r = _run_with(_config())
        self.assertEqual(r.status, "WARN")           # one high finding
        self.assertEqual(len(r.findings), 1)
        self.assertEqual(r.findings[0].symbol, "foo")   # inferred from unit
        self.assertEqual(r.findings[0].line, 2)
        self.assertEqual(r.refuted, [])
        self.assertEqual(r.n_units, 1)

    def test_out_of_window_finding_dropped(self):
        r = _run_with(_config(), finding_line=99)    # line not in any unit
        self.assertEqual(r.findings, [])
        self.assertEqual(r.status, "PASS")

    def test_verify_refuted_finding_dropped(self):
        r = _run_with(_config(), verify_upheld=False)
        self.assertEqual(r.findings, [])
        self.assertEqual(len(r.refuted), 1)
        self.assertEqual(r.status, "PASS")

    def test_resolved_fingerprint_suppressed(self):
        fp = fingerprint("mod.py", "foo", HYP)
        ledger = json.dumps({"fingerprint": fp, "outcome": "false_positive"})
        r = _run_with(_config(ledger=ledger))
        self.assertEqual(r.findings, [])
        self.assertEqual(r.suppressed, 1)

    def test_first_seen_registry_updated_for_new_finding(self):
        r = _run_with(_config())
        fp = r.findings[0].fingerprint
        self.assertEqual(r.first_seen.get(fp), "2026-07-28")

    def test_no_changes_is_pass(self):
        cfg = _config()
        files = _files(cfg)
        with mock.patch.object(model, "_http_post"):
            r = run_bug_hunter(cfg, PROFILE, run=lambda a: "",
                               read=lambda p: files.get(p),
                               list_files=lambda g: [], today="2026-07-28")
        self.assertEqual(r.status, "PASS")
        self.assertIn("no code changes", r.reason)


if __name__ == "__main__":
    unittest.main()
