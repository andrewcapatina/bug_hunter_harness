"""Tests for input assembly (SPEC §1/§6). run/read/list_files injected."""
import json
import unittest

from harness import gather as G
from harness.gather import (
    git_diff, gather_sidecars, gather_resolved, read_first_seen, read_bugs_md,
    gather,
)


class GitDiffTests(unittest.TestCase):
    def test_uses_time_window_when_nonempty(self):
        def run(args):
            return "DIFF_WINDOW" if "HEAD@{24 hours ago}..HEAD" in " ".join(args) else ""
        diff, trunc = git_diff(run, 24, 10_000)
        self.assertEqual(diff, "DIFF_WINDOW")
        self.assertFalse(trunc)

    def test_falls_back_to_head5_when_window_empty(self):
        def run(args):
            return "DIFF_HEAD5" if "HEAD~5..HEAD" in " ".join(args) else ""
        diff, _ = git_diff(run, 24, 10_000)
        self.assertEqual(diff, "DIFF_HEAD5")

    def test_truncation_flag_and_cap(self):
        big = "x" * 20_000
        diff, trunc = git_diff(lambda a: big, 24, 5_000)
        self.assertTrue(trunc)
        self.assertEqual(len(diff), 5_000)

    def test_failed_git_call_degrades(self):
        def run(args):
            raise RuntimeError("git exploded")
        diff, _ = git_diff(run, 24, 5_000)
        self.assertEqual(diff, "")

    def test_explicit_diff_ref_overrides_window(self):
        seen = {}
        def run(args):
            seen["args"] = args
            return "DIFF_REF"
        diff, _ = git_diff(run, 24, 10_000, diff_ref="HEAD~3..HEAD")
        self.assertEqual(diff, "DIFF_REF")
        self.assertIn("HEAD~3..HEAD", seen["args"])   # used the ref, not the window


class SidecarTests(unittest.TestCase):
    def _read(self, files):
        return lambda p: files.get(p)

    def test_projects_and_filters_by_run_id(self):
        files = {
            "a_latest.json": json.dumps({"agent": "a", "status": "PASS",
                                         "reason": "ok", "run_id": "R1",
                                         "highlights": list(range(20))}),
            "b_latest.json": json.dumps({"agent": "b", "status": "WARN",
                                         "run_id": "R0"}),   # stale run
        }
        out = gather_sidecars(self._read(files), lambda g: list(files), ["*"],
                              run_id="R1")
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["agent"], "a")
        self.assertEqual(len(out[0]["highlights"]), 8)       # capped

    def test_bad_json_skipped(self):
        files = {"x.json": "{not json"}
        self.assertEqual(
            gather_sidecars(self._read(files), lambda g: ["x.json"], ["*"]), [])


class ResolvedTests(unittest.TestCase):
    def test_ledger_and_git_grep_merge_keyed_by_fingerprint(self):
        ledger = "\n".join([
            json.dumps({"fingerprint": "aaaa1111", "outcome": "applied", "reason": "fixed it"}),
            json.dumps({"fingerprint": "bbbb2222", "outcome": "false_positive", "reason": "refuted"}),
            json.dumps({"fingerprint": "cccc3333", "outcome": "attempted_failed"}),  # NOT resolved
        ])
        read = lambda p: ledger if p == "ledger.ndjson" else None

        def run(args):
            return "auto-fix dddd4444 something" if "--grep=auto-fix" in args else ""

        resolved = gather_resolved(run, read, "ledger.ndjson")
        self.assertIn("aaaa1111", resolved)
        self.assertIn("bbbb2222", resolved)
        self.assertNotIn("cccc3333", resolved)               # not a resolved outcome
        self.assertIn("dddd4444", resolved)                  # from git grep


class RegistryTests(unittest.TestCase):
    def test_read_first_seen(self):
        read = lambda p: '{"aaaa": "2026-07-01"}'
        self.assertEqual(read_first_seen(read, "reg.json"), {"aaaa": "2026-07-01"})

    def test_missing_registry_is_empty(self):
        self.assertEqual(read_first_seen(lambda p: None, "reg.json"), {})

    def test_bugs_md_tail(self):
        self.assertEqual(read_bugs_md(lambda p: "abcdef", "bugs.md", tail_chars=3),
                         "def")


class GatherEndToEndTests(unittest.TestCase):
    def test_gather_wires_everything(self):
        config = {
            "gather": {"since_hours": 24, "reviewed_sidecars": ["*_latest.json"],
                       "bugs_md": "docs/bugs.md", "first_seen_path": "reg.json"},
            "fixer": {"ledger_path": "ledger.ndjson"},
        }
        files = {
            "docs/bugs.md": "known issue",
            "reg.json": '{"ff": "2026-07-02"}',
            "ledger.ndjson": json.dumps({"fingerprint": "ff", "outcome": "applied"}),
            "s_latest.json": json.dumps({"agent": "s", "status": "PASS", "run_id": "R"}),
        }
        run = lambda a: "DIFF" if "HEAD@{24 hours ago}..HEAD" in " ".join(a) else ""
        res = gather(config, run=run, read=lambda p: files.get(p),
                     list_files=lambda g: [k for k in files if k.endswith("_latest.json")],
                     run_id="R")
        self.assertEqual(res.diff, "DIFF")
        self.assertEqual(res.bugs_md, "known issue")
        self.assertIn("ff", res.resolved)
        self.assertEqual(res.first_seen, {"ff": "2026-07-02"})
        self.assertEqual(len(res.sidecars), 1)


if __name__ == "__main__":
    unittest.main()
