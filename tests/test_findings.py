"""Tests for findings schema, fingerprint, calibration (SPEC §4/§5)."""
import unittest

from harness.findings import (
    fingerprint, make_display_id, extract_evidence, in_window,
    normalize_finding, normalize_batch,
)
from harness.units import ReviewUnit


def _unit(path, symbol, start, end):
    return ReviewUnit(path=path, symbol=symbol, kind="def", start_line=start,
                      end_line=end, source="x", changed_lines=[(start, start)])


UNITS = [_unit("mod.py", "foo", 6, 10), _unit("mod.py", "Cls.bar", 20, 30)]
TODAY = "2026-07-28"
_NEVER = lambda fp: None                              # nothing seen before


def _raw(**kw):
    base = {"path": "mod.py", "line": 8, "severity": "high", "confidence": "high",
            "category": "logic", "trigger": "x<0 reaches it", "title": "t",
            "hypothesis": "the guard is missing", "suggested_fix": "add guard"}
    base.update(kw)
    return base


class FingerprintTests(unittest.TestCase):
    def test_stable_and_content_derived(self):
        a = fingerprint("mod.py", "foo", "the guard is missing")
        b = fingerprint("mod.py", "foo", "The Guard Is Missing!")   # normalized
        self.assertEqual(a, b)                        # case/punct-insensitive
        self.assertEqual(len(a), 16)

    def test_different_path_or_hypothesis_differs(self):
        base = fingerprint("mod.py", "foo", "h")
        self.assertNotEqual(base, fingerprint("other.py", "foo", "h"))
        self.assertNotEqual(base, fingerprint("mod.py", "foo", "different bug"))

    def test_display_id_is_content_stable(self):
        fp = fingerprint("mod.py", "foo", "h")
        self.assertEqual(make_display_id(fp, "2026-07-28"),
                         f"BH-2026-07-28-{fp[:8]}")


class EvidenceTests(unittest.TestCase):
    def test_new_shape(self):
        self.assertEqual(extract_evidence({"path": "a.py", "line": 5}), ("a.py", 5))

    def test_legacy_evidence_shape(self):
        self.assertEqual(
            extract_evidence({"evidence": [{"file": "a.py", "lines": "5-9"}]}),
            ("a.py", 5))

    def test_missing_line_ok(self):
        self.assertEqual(extract_evidence({"path": "a.py"}), ("a.py", None))


class InWindowTests(unittest.TestCase):
    def test_path_shown_line_inside(self):
        self.assertTrue(in_window("mod.py", 8, UNITS))

    def test_path_not_shown_rejected(self):
        self.assertFalse(in_window("ghost.py", 8, UNITS))   # misattribution

    def test_line_outside_any_unit_rejected(self):
        self.assertFalse(in_window("mod.py", 15, UNITS))    # between units

    def test_no_line_allowed_if_path_shown(self):
        self.assertTrue(in_window("mod.py", None, UNITS))


class NormalizeTests(unittest.TestCase):
    def test_valid_finding_passes_and_fingerprints(self):
        f = normalize_finding(_raw(), UNITS, today=TODAY,
                              first_seen_lookup=_NEVER, found_by="batch1")
        self.assertIsNotNone(f)
        self.assertEqual(f.symbol, "foo")             # inferred from unit at line 8
        self.assertEqual(f.severity, "high")
        self.assertTrue(f.display_id.startswith("BH-2026-07-28-"))

    def test_out_of_window_line_dropped(self):
        self.assertIsNone(normalize_finding(
            _raw(line=15), UNITS, today=TODAY,
            first_seen_lookup=_NEVER, found_by="b"))

    def test_misattributed_path_dropped(self):
        self.assertIsNone(normalize_finding(
            _raw(path="ghost.py"), UNITS, today=TODAY,
            first_seen_lookup=_NEVER, found_by="b"))

    def test_no_trigger_caps_severity_low(self):
        f = normalize_finding(_raw(trigger=""), UNITS, today=TODAY,
                              first_seen_lookup=_NEVER, found_by="b")
        self.assertEqual(f.severity, "low")           # I3

    def test_invalid_enums_defaulted(self):
        f = normalize_finding(_raw(severity="critical", category="weird",
                                   confidence="", auto_fix_class="x"),
                              UNITS, today=TODAY, first_seen_lookup=_NEVER,
                              found_by="b")
        self.assertEqual(f.severity, "low")           # unknown -> low (via trigger? no)
        self.assertEqual(f.category, "logic")
        self.assertEqual(f.confidence, "low")
        self.assertEqual(f.auto_fix_class, "proposal")

    def test_first_seen_lookup_sets_display_date(self):
        seen = lambda fp: "2026-07-01"
        f = normalize_finding(_raw(), UNITS, today=TODAY,
                              first_seen_lookup=seen, found_by="b")
        self.assertTrue(f.display_id.startswith("BH-2026-07-01-"))  # stable, not today

    def test_no_line_caps_confidence(self):
        f = normalize_finding(_raw(line=None, confidence="high"), UNITS,
                              today=TODAY, first_seen_lookup=_NEVER, found_by="b")
        self.assertEqual(f.confidence, "medium")

    def test_normalize_batch_drops_invalid(self):
        raws = [_raw(), _raw(path="ghost.py"), _raw(line=15)]
        out = normalize_batch(raws, UNITS, today=TODAY,
                              first_seen_lookup=_NEVER, found_by="b")
        self.assertEqual(len(out), 1)                 # only the valid one survives


if __name__ == "__main__":
    unittest.main()
