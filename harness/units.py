"""Review-unit builder — BUG_HUNTER_SPEC.md §2.

Turn a unified diff into WHOLE enclosing definitions, never bare hunks. This is
the dominant anti-false-positive lever: the model can only judge code it can
fully see (invariant I1), so a change inside a function is reviewed as the
whole function — its signature, guards, and returns included — bound to its
file and symbol (I2). A finding whose evidence lies outside the shown unit is
then droppable, because the unit IS the whole definition.

Pure functions, no filesystem coupling: `build_units` takes a `read_file`
callable so the same code serves the live harness and a golden-diff test.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field

# A file at/under this many lines is reviewed whole when it has no resolvable
# enclosing definition (config/markdown/small scripts). Larger unparseable
# files fall back to labeled hunk-only units that tell the model it is blind
# to the surroundings.
SMALL_FILE_MAX_LINES = 200

_UNSET = object()  # "segment not started" — distinct from None (module-level key)


@dataclass
class ReviewUnit:
    path: str
    symbol: str            # "func" | "Class.method" | "<module L10-L40>" | "<file>"
    kind: str              # def | class | module | file | hunk
    start_line: int        # 1-indexed, inclusive (in the NEW file)
    end_line: int          # 1-indexed, inclusive
    source: str            # full text of the unit, verbatim from the new file
    changed_lines: list[tuple[int, int]] = field(default_factory=list)

    def contains(self, line: int) -> bool:
        return self.start_line <= line <= self.end_line


# --------------------------------------------------------------------------- #
# Diff parsing — changed line ranges in the NEW file, per path.
# --------------------------------------------------------------------------- #
_DIFF_HEADER = re.compile(r"^diff --git a/(.+?) b/(.+)$")
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_diff(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """{new_path: [(new_start, new_end), ...]} — the new-file line span of each
    hunk (added + context + deletion-adjacent). Over-inclusive on purpose: we
    want whichever definition a hunk touches, then we send it whole.

    Deleted files (b/ == /dev/null) are omitted — there is nothing to review in
    the new state. Renames use the new path.
    """
    changed: dict[str, list[tuple[int, int]]] = {}
    path: str | None = None
    deleted = False
    new_line = 0
    remaining = 0
    for raw in diff_text.splitlines():
        m = _DIFF_HEADER.match(raw)
        if m:
            path = m.group(2)
            deleted = False
            remaining = 0
            continue
        if raw.startswith("+++ "):
            if raw[4:].strip() in ("/dev/null", "b/dev/null"):
                deleted = True
            continue
        if path is None or deleted:
            continue
        h = _HUNK.match(raw)
        if h:
            new_start = int(h.group(1))
            count = int(h.group(2)) if h.group(2) is not None else 1
            # Record the hunk's new-file span (clamp empty hunks to 1 line).
            end = new_start + max(count, 1) - 1
            changed.setdefault(path, []).append((new_start, end))
            new_line = new_start
            remaining = count
            continue
        if remaining <= 0:
            continue
        # Walk the hunk body to keep new_line honest (not strictly needed for
        # the span above, but guards malformed diffs).
        if raw.startswith("+") or raw.startswith(" "):
            new_line += 1
            remaining -= 1
        # '-' lines do not advance the new-file counter.
    return changed


# --------------------------------------------------------------------------- #
# Python: AST → innermost enclosing def/class per changed line.
# --------------------------------------------------------------------------- #
def _py_defs(source: str) -> list[tuple[int, int, str, str]]:
    """[(start_line, end_line, qualname, kind)] for every def/class, with
    decorators folded into the start so a decorator change maps to its target.
    Sorted innermost-first is done by the caller via smallest-span selection.
    """
    tree = ast.parse(source)
    out: list[tuple[int, int, str, str]] = []

    def walk(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = child.lineno
                if child.decorator_list:
                    start = min(start, min(d.lineno for d in child.decorator_list))
                end = getattr(child, "end_lineno", child.lineno)
                qual = f"{prefix}{child.name}"
                kind = "class" if isinstance(child, ast.ClassDef) else "def"
                out.append((start, end, qual, kind))
                walk(child, qual + ".")
    walk(tree, "")
    return out


def _enclosing(defs, line: int) -> tuple[int, int, str, str] | None:
    """Smallest-span def/class containing `line` (innermost method beats its
    class), or None if the line is module-level."""
    best = None
    for d in defs:
        s, e, _, _ = d
        if s <= line <= e and (best is None or (e - s) < (best[1] - best[0])):
            best = d
    return best


def _python_units(path: str, source: str,
                  ranges: list[tuple[int, int]]) -> list[ReviewUnit]:
    lines = source.splitlines()
    try:
        defs = _py_defs(source)
    except SyntaxError:
        return _fallback_units(path, source, ranges, reason="unparseable python")

    # Map each changed range's lines to their enclosing unit; group by unit.
    # `None` is a real key (module-level), so use a distinct sentinel for the
    # not-yet-started state — else a leading module segment is swallowed.
    by_unit: dict[tuple[int, int, str, str], list[tuple[int, int]]] = {}
    module_hits: list[tuple[int, int]] = []
    for (rs, re_) in ranges:
        cur_key = _UNSET
        seg_start = None
        for ln in range(rs, re_ + 1):
            key = _enclosing(defs, ln)
            if key != cur_key:
                if cur_key is not _UNSET:
                    _flush(by_unit, module_hits, cur_key, seg_start, ln - 1)
                cur_key, seg_start = key, ln
        if cur_key is not _UNSET:
            _flush(by_unit, module_hits, cur_key, seg_start, re_)

    units: list[ReviewUnit] = []
    for (s, e, qual, kind), chlines in sorted(by_unit.items()):
        units.append(ReviewUnit(
            path=path, symbol=qual, kind=kind, start_line=s, end_line=e,
            source="\n".join(lines[s - 1:e]), changed_lines=_merge(chlines)))
    # Module-level changes (imports, top-level constants) → a windowed unit.
    if module_hits:
        units.extend(_module_units(path, lines, _merge(module_hits)))
    return units


def _flush(by_unit, module_hits, key, seg_start, seg_end) -> None:
    if seg_start is None or seg_end < seg_start:
        return
    if key is None:
        module_hits.append((seg_start, seg_end))
    else:
        by_unit.setdefault(key, []).append((seg_start, seg_end))


def _module_units(path, lines, module_ranges) -> list[ReviewUnit]:
    """Top-level changes with no enclosing def: show a ±context window so the
    model sees the surrounding module lines, not a bare hunk."""
    pad = 8
    units = []
    for (s, e) in module_ranges:
        ws, we = max(1, s - pad), min(len(lines), e + pad)
        units.append(ReviewUnit(
            path=path, symbol=f"<module L{s}-L{e}>", kind="module",
            start_line=ws, end_line=we,
            source="\n".join(lines[ws - 1:we]), changed_lines=[(s, e)]))
    return units


def _merge(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not ranges:
        return []
    ranges = sorted(ranges)
    out = [ranges[0]]
    for s, e in ranges[1:]:
        if s <= out[-1][1] + 1:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


# --------------------------------------------------------------------------- #
# Non-Python / unparseable fallbacks.
# --------------------------------------------------------------------------- #
def _fallback_units(path, source, ranges, reason: str) -> list[ReviewUnit]:
    lines = source.splitlines()
    if len(lines) <= SMALL_FILE_MAX_LINES:
        return [ReviewUnit(path=path, symbol="<file>", kind="file",
                           start_line=1, end_line=len(lines) or 1,
                           source=source, changed_lines=_merge(ranges))]
    # Large + no structure: labeled hunk-only so the model self-limits (I1).
    units = []
    for (s, e) in _merge(ranges):
        ws, we = max(1, s - 3), min(len(lines), e + 3)
        units.append(ReviewUnit(
            path=path, symbol=f"<hunk L{s}-L{e}; no surrounding context>",
            kind="hunk", start_line=ws, end_line=we,
            source="\n".join(lines[ws - 1:we]), changed_lines=[(s, e)]))
    return units


# --------------------------------------------------------------------------- #
# Orchestrator.
# --------------------------------------------------------------------------- #
def build_units(diff_text: str, read_file) -> list[ReviewUnit]:
    """Diff + a `read_file(path)->str|None` (current NEW-state content) →
    whole-definition review units. A file `read_file` can't return (deleted /
    binary / unreadable) is skipped."""
    units: list[ReviewUnit] = []
    for path, ranges in parse_diff(diff_text).items():
        source = read_file(path)
        if source is None:
            continue
        if path.endswith(".py"):
            units.extend(_python_units(path, source, ranges))
        else:
            units.extend(_fallback_units(path, source, ranges, reason="non-python"))
    return units
