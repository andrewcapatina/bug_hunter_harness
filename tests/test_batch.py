"""Tests for batch packing + rendering (BUG_HUNTER_SPEC.md §2/§3)."""
import unittest

from harness.batch import (
    estimate_tokens, unit_tokens, pack_batches, render_unit, render_batch,
    plan_batches, input_budget,
)
from harness.model import ModelProfile
from harness.units import ReviewUnit


def _unit(path, symbol, start, lines, changed=None):
    src = "\n".join(f"line{i}" for i in range(lines))
    return ReviewUnit(path=path, symbol=symbol, kind="def", start_line=start,
                      end_line=start + lines - 1, source=src,
                      changed_lines=changed or [(start, start)])


class EstimateTests(unittest.TestCase):
    def test_monotonic(self):
        self.assertLess(estimate_tokens("ab"), estimate_tokens("a" * 100))


class PackTests(unittest.TestCase):
    def test_small_units_pack_into_one_batch(self):
        us = [_unit("a.py", "f1", 1, 3), _unit("a.py", "f2", 10, 3)]
        batches = pack_batches(us, budget_tokens=10_000)
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]), 2)

    def test_splits_when_budget_exceeded_order_preserved(self):
        us = [_unit("a.py", f"f{i}", i * 100, 20) for i in range(6)]
        budget = unit_tokens(us[0]) * 2 + 1        # ~2 units per batch
        batches = pack_batches(us, budget)
        self.assertGreater(len(batches), 1)
        # every unit appears exactly once, order preserved (never split)
        flat = [u for b in batches for u in b]
        self.assertEqual([u.symbol for u in flat], [u.symbol for u in us])
        self.assertEqual(len(flat), len(us))

    def test_oversized_unit_goes_alone_not_dropped(self):
        big = _unit("a.py", "huge", 1, 5000)        # far over a tiny budget
        small = _unit("a.py", "small", 6000, 2)
        batches = pack_batches([small, big, small], budget_tokens=200)
        # big is whole in its own batch; nothing dropped
        self.assertIn(["huge"], [[u.symbol for u in b] for b in batches])
        flat = [u.symbol for b in batches for u in b]
        self.assertEqual(flat.count("huge"), 1)
        self.assertEqual(flat.count("small"), 2)

    def test_no_unit_is_ever_split(self):
        us = [_unit("a.py", f"f{i}", i * 50, 30) for i in range(10)]
        batches = pack_batches(us, budget_tokens=unit_tokens(us[0]) + 1)
        for b in batches:
            for u in b:
                self.assertIn(u.source, [x.source for x in us])  # intact


class RenderTests(unittest.TestCase):
    def test_render_unit_numbers_and_marks_changed(self):
        u = ReviewUnit(path="m.py", symbol="foo", kind="def", start_line=6,
                       end_line=8, source="def foo(x):\n    y = 1\n    return y",
                       changed_lines=[(7, 7)])
        r = render_unit(u)
        self.assertIn("m.py :: foo", r)
        self.assertIn("[changed: L7]", r)
        self.assertIn("    6: def foo(x):", r)       # line 6, unchanged (space)
        self.assertIn("    7:*    y = 1", r)          # line 7, changed (*)
        self.assertIn("    8:     return y", r)

    def test_render_batch_header_lists_files_and_index(self):
        us = [_unit("a.py", "f", 1, 2), _unit("b.py", "g", 1, 2)]
        r = render_batch(us, 2, 5)
        self.assertIn("Review batch 2 of 5", r)
        self.assertIn("files: a.py, b.py", r)
        self.assertIn("do not infer code you cannot see", r)


class PlanTests(unittest.TestCase):
    def test_plan_uses_profile_budget(self):
        p = ModelProfile(endpoint="e", context_window_tokens=2000,
                         max_output_tokens=500)
        self.assertGreater(input_budget(p, "sys prompt"), 0)
        self.assertLess(input_budget(p, "sys prompt"), 2000)
        us = [_unit("a.py", f"f{i}", i * 50, 20) for i in range(8)]
        plan = plan_batches(us, p, "sys prompt")
        # returns (idx, rendered, units); indices are 1..n
        self.assertEqual([row[0] for row in plan], list(range(1, len(plan) + 1)))
        self.assertTrue(all("Review batch" in row[1] for row in plan))


if __name__ == "__main__":
    unittest.main()
