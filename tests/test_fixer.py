"""Tests for the guard-railed scripted fixer (SPEC §6). All I/O injected."""
import unittest

from harness.fixer import (
    path_allowed, validate_patches, apply_in_memory, apply_fix, run_fixer,
    FixOutcome,
)
from harness.findings import Finding


def _finding(path="mod.py", title="fix it", fp="fp1", fix_class="auto_applicable"):
    return Finding(fingerprint=fp, display_id="BH-x-fp1", path=path, symbol="foo",
                   line=2, severity="high", confidence="high", category="logic",
                   trigger="t", title=title, hypothesis="h", suggested_fix="s",
                   auto_fix_class=fix_class, regression_of=None, found_by="b")


CONFIG = {"fixer": {"allowed_paths": ["*.py"], "blocked_paths": ["*_creds.py"],
                    "max_patch_lines": 30, "consecutive_failure_halt": 2}}


def _env(files):
    store = dict(files)
    reads = lambda p: store.get(p)
    writes = lambda p, c: store.__setitem__(p, c)
    runlog = []

    def run(args):
        runlog.append(args)
        return "abc123\n" if args[:2] == ["git", "rev-parse"] else ""

    syntax_ok = lambda path, content: (True, "")
    return store, reads, writes, run, runlog, syntax_ok


class PathTests(unittest.TestCase):
    def test_blocked_wins(self):
        self.assertFalse(path_allowed("x_creds.py", ["*.py"], ["*_creds.py"]))

    def test_must_be_allowed(self):
        self.assertTrue(path_allowed("a.py", ["*.py"], []))
        self.assertFalse(path_allowed("a.txt", ["*.py"], []))

    def test_empty_allowlist_fails_closed(self):
        self.assertFalse(path_allowed("a.py", [], []))


class ValidateTests(unittest.TestCase):
    def _read(self, content):
        return lambda p: content

    def test_unique_old_string_ok(self):
        ok, _ = validate_patches(
            [{"file": "a.py", "old_string": "x = 1", "new_string": "x = 2"}],
            self._read("x = 1\n"), ["*.py"], [], 30)
        self.assertTrue(ok)

    def test_old_string_not_found(self):
        ok, r = validate_patches(
            [{"file": "a.py", "old_string": "nope", "new_string": "y"}],
            self._read("x = 1\n"), ["*.py"], [], 30)
        self.assertFalse(ok)
        self.assertIn("not found", r)

    def test_old_string_ambiguous(self):
        ok, r = validate_patches(
            [{"file": "a.py", "old_string": "x", "new_string": "y"}],
            self._read("x x x"), ["*.py"], [], 30)
        self.assertFalse(ok)
        self.assertIn("not unique", r)

    def test_too_large(self):
        big = "\n".join(str(i) for i in range(40))
        ok, r = validate_patches(
            [{"file": "a.py", "old_string": big, "new_string": "z"}],
            self._read(big), ["*.py"], [], 30)
        self.assertFalse(ok)
        self.assertIn("exceeds", r)

    def test_blocked_path(self):
        ok, r = validate_patches(
            [{"file": "x_creds.py", "old_string": "a", "new_string": "b"}],
            self._read("a"), ["*.py"], ["*_creds.py"], 30)
        self.assertFalse(ok)
        self.assertIn("not allowed", r)


class ApplyMemTests(unittest.TestCase):
    def test_applies_once(self):
        read = lambda p: "x = 1\ny = 1\n"
        out = apply_in_memory(
            [{"file": "a.py", "old_string": "x = 1", "new_string": "x = 2"}], read)
        self.assertEqual(out["a.py"], "x = 2\ny = 1\n")


class ApplyFixTests(unittest.TestCase):
    def test_happy_path_applies_and_commits(self):
        store, read, write, run, runlog, syn = _env({"mod.py": "y = x + 0\n"})
        propose = lambda f, files: {"patches": [
            {"file": "mod.py", "old_string": "y = x + 0", "new_string": "y = x + 1"}]}
        out = apply_fix(_finding(), CONFIG, read=read, write=write, run=run,
                        syntax_check=syn, propose=propose)
        self.assertEqual(out.outcome, "applied")
        self.assertEqual(store["mod.py"], "y = x + 1\n")
        self.assertEqual(out.commit_sha, "abc123")
        self.assertIn(["git", "commit", "-m", "[auto-fix fp1] fix it"], runlog)

    def test_blocked_cited_path(self):
        _, read, write, run, _, syn = _env({"x_creds.py": "secret"})
        out = apply_fix(_finding(path="x_creds.py"), CONFIG, read=read, write=write,
                        run=run, syntax_check=syn, propose=lambda f, x: {})
        self.assertEqual(out.outcome, "skipped_blocked")

    def test_missing_file(self):
        _, read, write, run, _, syn = _env({})
        out = apply_fix(_finding(), CONFIG, read=read, write=write, run=run,
                        syntax_check=syn, propose=lambda f, x: {})
        self.assertEqual(out.outcome, "skipped_no_file")

    def test_ambiguous_patch_fails_no_write(self):
        store, read, write, run, _, syn = _env({"mod.py": "x x"})
        propose = lambda f, x: {"patches": [
            {"file": "mod.py", "old_string": "x", "new_string": "y"}]}
        out = apply_fix(_finding(), CONFIG, read=read, write=write, run=run,
                        syntax_check=syn, propose=propose)
        self.assertEqual(out.outcome, "attempted_failed")
        self.assertEqual(store["mod.py"], "x x")           # untouched

    def test_syntax_failure_reverts(self):
        store, read, write, run, _, _ = _env({"mod.py": "y = 0\n"})
        bad_syntax = lambda p, c: (False, "SyntaxError")
        propose = lambda f, x: {"patches": [
            {"file": "mod.py", "old_string": "y = 0", "new_string": "y = ("}]}
        out = apply_fix(_finding(), CONFIG, read=read, write=write, run=run,
                        syntax_check=bad_syntax, propose=propose)
        self.assertEqual(out.outcome, "attempted_failed")
        self.assertEqual(store["mod.py"], "y = 0\n")        # not written


class RunFixerTests(unittest.TestCase):
    def test_filters_non_auto_and_skips_resolved(self):
        store, read, write, run, _, syn = _env({"mod.py": "a\n"})
        propose = lambda f, x: {"patches": [
            {"file": "mod.py", "old_string": "a", "new_string": "b"}]}
        findings = [
            _finding(fp="p1", fix_class="proposal"),      # not auto -> skipped
            _finding(fp="p2"),                             # resolved -> skipped
            _finding(fp="p3"),                             # applied
        ]
        outs = run_fixer(findings, CONFIG, read=read, write=write, run=run,
                         syntax_check=syn, propose=propose, resolved={"p2": "x"})
        by = {o.fingerprint: o.outcome for o in outs}
        self.assertNotIn("p1", by)                         # filtered before outcome
        self.assertEqual(by["p2"], "already_resolved")
        self.assertEqual(by["p3"], "applied")

    def test_circuit_breaker_halts_after_consecutive_failures(self):
        _, read, write, run, _, syn = _env({"mod.py": "x x"})   # ambiguous -> fail
        propose = lambda f, x: {"patches": [
            {"file": "mod.py", "old_string": "x", "new_string": "y"}]}
        findings = [_finding(fp=f"p{i}") for i in range(5)]
        outs = run_fixer(findings, CONFIG, read=read, write=write, run=run,
                         syntax_check=syn, propose=propose, resolved={})
        self.assertEqual(outs[-1].outcome, "halted")       # halt=2 -> stops early
        self.assertLess(len([o for o in outs if o.outcome == "attempted_failed"]), 5)


if __name__ == "__main__":
    unittest.main()
