"""Tests for semantic dedup (SPEC §5) — the 8x-inflation killer."""
import unittest

from harness.dedup import dedupe
from harness.findings import Finding, fingerprint


def _f(path, symbol, hypothesis, *, severity="medium", confidence="medium",
       title="t", found_by="b"):
    fp = fingerprint(path, symbol, hypothesis)
    return Finding(fingerprint=fp, display_id=f"BH-x-{fp[:8]}", path=path,
                   symbol=symbol, line=1, severity=severity, confidence=confidence,
                   category="logic", trigger="t", title=title, hypothesis=hypothesis,
                   suggested_fix="f", auto_fix_class="proposal", regression_of=None,
                   found_by=found_by)


class DedupTests(unittest.TestCase):
    def test_exact_fingerprint_dupes_collapse(self):
        a = _f("m.py", "foo", "same bug", found_by="batch1")
        b = _f("m.py", "foo", "same bug", found_by="batch2")
        out = dedupe([a, b])
        self.assertEqual(len(out), 1)
        self.assertIn("batch1", out[0].found_by)
        self.assertIn("batch2", out[0].found_by)
        self.assertIn("(x2)", out[0].found_by)

    def test_distinct_findings_stay_separate(self):
        a = _f("m.py", "foo", "the retry loop never terminates on empty input")
        b = _f("m.py", "bar", "cash is debited twice on a partial fill")
        self.assertEqual(len(dedupe([a, b])), 2)

    def test_reworded_near_dupes_collapse_keep_most_severe(self):
        # Same bug, three wordings, three cited files, three severities — the 8x
        # pattern in miniature. Should collapse to one, keeping the high.
        a = _f("refresh.py", "run", "data refresh reports success while ETH cache "
               "is stale and not actually refreshed", severity="high", found_by="b1")
        b = _f("summary.md", "run", "the data refresh success count overstates "
               "per-symbol freshness when ETH cache is stale", severity="medium",
               found_by="b2")
        c = _f("refresh.py", "run", "refresh success metric stale ETH cache not "
               "actually refreshed reports success", severity="low", found_by="b3")
        out = dedupe([a, b, c])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].severity, "high")     # most-severe member kept
        self.assertIn("(x3)", out[0].found_by)

    def test_similarity_threshold_respected(self):
        a = _f("m.py", "foo", "alpha beta gamma delta epsilon zeta")
        b = _f("m.py", "bar", "completely different words nothing shared here now")
        self.assertEqual(len(dedupe([a, b], threshold=0.5)), 2)

    def test_empty(self):
        self.assertEqual(dedupe([]), [])


if __name__ == "__main__":
    unittest.main()
