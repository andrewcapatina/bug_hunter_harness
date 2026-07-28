"""Findings: schema, content-hash identity, calibration — SPEC §4/§5.

Turns a raw model finding into a validated Finding, or drops it. This is where
invariants become enforcement:
  * I2 — evidence must be real and in-window: a cited path:line outside the
    units actually shown is dropped (kills misattribution + out-of-window
    "missing return/guard" claims that survive the whole-unit context).
  * I3 — severity = reachability × impact: no constructible `trigger` caps
    severity at low.
  * I5 — identity is a stable content fingerprint, never a per-run number;
    the human display_id is content-derived so it doesn't churn day to day.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass

VALID_SEVERITY = ("low", "medium", "high")
VALID_CONFIDENCE = ("low", "medium", "high")
VALID_CATEGORY = ("logic", "safety", "perf", "data", "consistency", "exec", "docs")
VALID_FIX_CLASS = ("auto_applicable", "proposal", "human_only")


@dataclass
class Finding:
    fingerprint: str
    display_id: str
    path: str
    symbol: str
    line: int | None
    severity: str
    confidence: str
    category: str
    trigger: str
    title: str
    hypothesis: str
    suggested_fix: str
    auto_fix_class: str
    regression_of: str | None
    found_by: str

    def to_dict(self) -> dict:
        return asdict(self)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def fingerprint(path: str, symbol: str, hypothesis: str) -> str:
    """Stable content identity (I5): sha256(normalize(path::symbol::
    normalize(hypothesis)))[:16]. Independent of any positional ID, so the same
    bug hashes identically across runs and days."""
    inner = f"{path}::{symbol}::{_norm(hypothesis)}"
    return hashlib.sha256(_norm(inner).encode("utf-8")).hexdigest()[:16]


def make_display_id(fp: str, date_first_seen: str) -> str:
    """BH-<date-first-seen>-<fp8>: human-readable AND content-stable (I5)."""
    return f"BH-{date_first_seen}-{fp[:8]}"


def _pick(value: str, valid: tuple, default: str) -> str:
    v = (value or "").strip().lower()
    return v if v in valid else default


def extract_evidence(raw: dict) -> tuple[str | None, int | None]:
    """Support both the new (path/line) and legacy (evidence:[{file,lines}])
    shapes. Returns (path, line) with line as the first int found, or None."""
    path = raw.get("path")
    line = raw.get("line")
    if path is None or line is None:
        ev = (raw.get("evidence") or [{}])[0] if raw.get("evidence") else {}
        path = path or ev.get("file")
        if line is None:
            m = re.search(r"\d+", str(ev.get("lines") or ""))
            line = int(m.group()) if m else None
    try:
        line = int(line) if line is not None else None
    except (TypeError, ValueError):
        line = None
    return path, line


def in_window(path: str | None, line: int | None, shown_units) -> bool:
    """I2: the cited path must be a unit shown in this batch, and a cited line
    must fall inside one of that path's units. A path not shown = misattribution
    → reject. No line = allowed but the caller caps confidence."""
    if not path:
        return False
    for_path = [u for u in shown_units if u.path == path]
    if not for_path:
        return False
    if line is None:
        return True
    return any(u.contains(line) for u in for_path)


def normalize_finding(raw: dict, shown_units, *, today: str,
                      first_seen_lookup, found_by: str) -> Finding | None:
    """Validate + calibrate a raw model finding, or None to drop it."""
    path, line = extract_evidence(raw)
    if not in_window(path, line, shown_units):
        return None                                   # I2: out-of-window → drop

    hypothesis = (raw.get("hypothesis") or "").strip()
    symbol = (raw.get("symbol") or "").strip()
    if not symbol:                                    # infer from the shown unit
        for u in shown_units:
            if u.path == path and (line is None or u.contains(line)):
                symbol = u.symbol
                break

    fp = fingerprint(path, symbol, hypothesis)
    date_first = first_seen_lookup(fp) or today

    severity = _pick(raw.get("severity"), VALID_SEVERITY, "low")
    confidence = _pick(raw.get("confidence"), VALID_CONFIDENCE, "low")
    trigger = (raw.get("trigger") or "").strip()
    if not trigger:                                   # I3: no reachability → low
        severity = "low"
    if line is None and confidence == "high":         # unverifiable line → cap
        confidence = "medium"

    return Finding(
        fingerprint=fp,
        display_id=make_display_id(fp, date_first),
        path=path, symbol=symbol, line=line,
        severity=severity, confidence=confidence,
        category=_pick(raw.get("category"), VALID_CATEGORY, "logic"),
        trigger=trigger,
        title=(raw.get("title") or "").strip(),
        hypothesis=hypothesis,
        suggested_fix=(raw.get("suggested_fix") or "").strip(),
        auto_fix_class=_pick(raw.get("auto_fix_class"), VALID_FIX_CLASS, "proposal"),
        regression_of=raw.get("regression_of") or None,
        found_by=found_by)


def normalize_batch(raw_findings, shown_units, *, today, first_seen_lookup,
                    found_by) -> list[Finding]:
    """Normalize all raw findings from one batch, dropping the invalid ones."""
    out = []
    for raw in raw_findings or []:
        f = normalize_finding(raw, shown_units, today=today,
                              first_seen_lookup=first_seen_lookup,
                              found_by=found_by)
        if f is not None:
            out.append(f)
    return out
