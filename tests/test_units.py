"""Golden tests for the review-unit builder (BUG_HUNTER_SPEC.md §2).

Proves the core anti-false-positive property: a diff hunk is expanded to its
WHOLE enclosing definition, filename+symbol-bound — so the model never sees a
sliver it could hallucinate around.
"""
import unittest

from harness import units
from harness.units import build_units, parse_diff

# A known file layout — line N below is exactly line N in the string, so the
# diff hunk headers can reference real line numbers.
MOD_LINES = [
    "import os",                     # 1
    "import sys",                    # 2
    "",                             # 3
    "CONST = 5",                     # 4
    "",                             # 5
    "def foo(x):",                   # 6
    "    if x < 0:",                 # 7
    "        return None",           # 8
    "    y = x + 1",                 # 9
    "    return y",                  # 10
    "",                             # 11
    "",                             # 12
    "class Cls:",                    # 13
    "    def method_a(self):",       # 14
    "        return 1",              # 15
    "",                             # 16
    "    def method_b(self, n):",    # 17
    "        total = 0",             # 18
    "        for i in range(n):",    # 19
    "            total += i",        # 20
    "        return total",          # 21
    "",                             # 22
    "",                             # 23
    "@deco",                        # 24
    "def decorated(a):",             # 25
    "    return a * 2",              # 26
]
MOD_SRC = "\n".join(MOD_LINES)

# Touches: line 4 (module const), 9 (inside foo), 20 (inside method_b),
# 24 (a decorator). Four hunks, one file.
DIFF = """diff --git a/mod.py b/mod.py
--- a/mod.py
+++ b/mod.py
@@ -4,1 +4,1 @@
-CONST = 4
+CONST = 5
@@ -9,1 +9,1 @@ def foo(x):
-    y = x + 0
+    y = x + 1
@@ -20,1 +20,1 @@ def method_b(self, n):
-            total += 1
+            total += i
@@ -24,1 +24,1 @@
-@olddeco
+@deco
"""

JSON_SRC = '{\n  "a": 1,\n  "b": 2\n}'
JSON_DIFF = """diff --git a/conf.json b/conf.json
--- a/conf.json
+++ b/conf.json
@@ -2,1 +2,1 @@
-  "a": 0,
+  "a": 1,
"""


def _reader(files):
    return lambda p: files.get(p)


class ParseDiffTests(unittest.TestCase):
    def test_new_file_ranges_per_path(self):
        ranges = parse_diff(DIFF)
        self.assertEqual(ranges["mod.py"], [(4, 4), (9, 9), (20, 20), (24, 24)])

    def test_deleted_file_omitted(self):
        d = ("diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n"
             "@@ -1,2 +0,0 @@\n-x = 1\n-y = 2\n")
        self.assertEqual(parse_diff(d), {})


class BuildUnitsTests(unittest.TestCase):
    def setUp(self):
        self.units = build_units(DIFF, _reader({"mod.py": MOD_SRC}))
        self.by_sym = {u.symbol: u for u in self.units}

    def test_one_unit_per_touched_definition(self):
        self.assertEqual(
            set(self.by_sym),
            {"<module L4-L4>", "foo", "Cls.method_b", "decorated"})

    def test_change_in_function_yields_whole_function(self):
        u = self.by_sym["foo"]
        self.assertEqual(u.kind, "def")
        self.assertEqual((u.start_line, u.end_line), (6, 10))
        self.assertIn("def foo(x):", u.source)
        self.assertIn("return y", u.source)          # whole body, not a sliver

    def test_change_in_method_yields_the_method_not_the_class(self):
        u = self.by_sym["Cls.method_b"]
        self.assertEqual((u.start_line, u.end_line), (17, 21))
        self.assertIn("def method_b", u.source)
        self.assertNotIn("def method_a", u.source)   # the METHOD, not whole class

    def test_decorator_change_folds_into_its_target(self):
        u = self.by_sym["decorated"]
        self.assertEqual(u.start_line, 24)           # decorator line, not the def
        self.assertIn("@deco", u.source)
        self.assertIn("def decorated", u.source)

    def test_module_level_change_is_windowed_not_whole_file(self):
        u = self.by_sym["<module L4-L4>"]
        self.assertEqual(u.kind, "module")
        self.assertIn("CONST = 5", u.source)
        self.assertLess(u.end_line - u.start_line, len(MOD_LINES))  # a window

    def test_changed_lines_recorded_within_unit(self):
        self.assertEqual(self.by_sym["foo"].changed_lines, [(9, 9)])


class NonPythonTests(unittest.TestCase):
    def test_small_non_python_file_reviewed_whole(self):
        u = build_units(JSON_DIFF, _reader({"conf.json": JSON_SRC}))
        self.assertEqual(len(u), 1)
        self.assertEqual(u[0].kind, "file")
        self.assertEqual(u[0].symbol, "<file>")
        self.assertIn('"a": 1', u[0].source)

    def test_unreadable_file_skipped(self):
        self.assertEqual(build_units(DIFF, _reader({})), [])  # read_file -> None


class UnparseablePythonTests(unittest.TestCase):
    def test_syntax_error_falls_back_not_crashes(self):
        broken = "def foo(:\n    pass\n"
        d = ("diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n"
             "@@ -1,1 +1,1 @@\n-x\n+y\n")
        u = build_units(d, _reader({"b.py": broken}))
        self.assertTrue(u)                            # produced something, no raise
        self.assertEqual(u[0].kind, "file")           # small -> whole file


if __name__ == "__main__":
    unittest.main()
