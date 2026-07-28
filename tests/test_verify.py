"""Tests for the adversarial verify pass (SPEC §5)."""
import unittest
from unittest import mock

from harness import verify
from harness.findings import Finding, fingerprint
from harness.model import ModelProfile, ModelError
from harness.units import ReviewUnit


def _finding(path="m.py", line=8, hyp="the guard is missing", title="missing guard"):
    fp = fingerprint(path, "foo", hyp)
    return Finding(fingerprint=fp, display_id=f"BH-x-{fp[:8]}", path=path,
                   symbol="foo", line=line, severity="high", confidence="high",
                   category="logic", trigger="x<0 reaches it", title=title,
                   hypothesis=hyp, suggested_fix="add guard",
                   auto_fix_class="proposal", regression_of=None, found_by="b1")


UNITS = [ReviewUnit(path="m.py", symbol="foo", kind="def", start_line=6,
                    end_line=10, source="def foo(x):\n    return x",
                    changed_lines=[(8, 8)])]
PROFILE = ModelProfile(endpoint="e", model="m")


class VerifyTests(unittest.TestCase):
    def test_upheld_finding_kept(self):
        with mock.patch.object(verify, "chat",
                               return_value=({"upheld": True, "reason": "real"}, None)):
            kept, dropped = verify.verify_findings([_finding()], UNITS, PROFILE)
        self.assertEqual(len(kept), 1)
        self.assertEqual(dropped, [])

    def test_refuted_finding_dropped_with_reason(self):
        with mock.patch.object(verify, "chat",
                               return_value=({"upheld": False,
                                              "reason": "guard is on the line above"}, None)):
            kept, dropped = verify.verify_findings([_finding()], UNITS, PROFILE)
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 1)
        self.assertIn("guard is on the line above", dropped[0]["reason"])

    def test_verify_call_failure_keeps_fail_open_and_marks(self):
        with mock.patch.object(verify, "chat", side_effect=ModelError("LLM down")):
            kept, dropped = verify.verify_findings([_finding()], UNITS, PROFILE)
        self.assertEqual(len(kept), 1)                # not lost to a blip
        self.assertIn("unverified", kept[0].found_by)
        self.assertEqual(dropped, [])

    def test_no_matching_unit_is_refuted(self):
        f = _finding(path="ghost.py")                 # no unit for this path
        # chat should not even be called; unit is None -> refuted
        with mock.patch.object(verify, "chat") as c:
            kept, dropped = verify.verify_findings([f], UNITS, PROFILE)
            c.assert_not_called()
        self.assertEqual(kept, [])
        self.assertEqual(len(dropped), 1)

    def test_refute_prompt_includes_the_full_unit(self):
        captured = {}

        def fake_chat(profile, system, user):
            captured["system"] = system
            captured["user"] = user
            return {"upheld": True, "reason": "ok"}, None

        with mock.patch.object(verify, "chat", side_effect=fake_chat):
            verify.verify_findings([_finding()], UNITS, PROFILE)
        self.assertIn("REFUTE", captured["system"])
        self.assertIn("def foo(x):", captured["user"])   # whole code shown
        self.assertIn("the guard is missing", captured["user"])


if __name__ == "__main__":
    unittest.main()
