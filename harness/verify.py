"""Adversarial verify pass — BUG_HUNTER_SPEC.md §5.

Neither prior harness had this. After calibration + dedup, each surviving
finding gets a SECOND model call over the same whole-unit context, prompted to
REFUTE it: is the trigger reachable, is the cited evidence actually in the code,
is it an intentional convention? A finding that isn't upheld is dropped (I4).
This is the highest-leverage anti-false-positive step after whole-unit context,
because it catches plausible-but-wrong findings the first pass was confident in.

Fail-open on infrastructure errors: a verify CALL that fails (LLM down) keeps
the finding but marks it unverified — a network blip must not silently delete a
real bug. Only an explicit refute drops.
"""
from __future__ import annotations

from dataclasses import replace

from harness.batch import render_unit
from harness.model import chat

REFUTE_SYSTEM = (
    "You are a skeptical senior engineer trying to REFUTE a claimed bug. You are "
    "shown the FULL definition it cites — nothing is hidden outside it. Refute "
    "(upheld=false) if ANY of these hold: the trigger is not reachable on a real "
    "path; the cited evidence is not actually present in the shown code; the "
    "behavior is an intentional convention, not a defect; or verifying it would "
    "require code NOT shown here. Uphold (upheld=true) only if the bug is real, "
    "reachable, and fully evidenced in the shown code. Respond with JSON only: "
    '{"upheld": <bool>, "reason": "<one sentence>"}.'
)


def _find_unit(units, finding):
    for u in units:
        if u.path != finding.path:
            continue
        if finding.line is None or u.contains(finding.line):
            return u
    return None


def build_refute_prompt(finding, unit) -> str:
    return (
        f"CLAIMED BUG\n"
        f"  title: {finding.title}\n"
        f"  severity: {finding.severity}   category: {finding.category}\n"
        f"  cites: {finding.path}:{finding.line} ({finding.symbol})\n"
        f"  trigger: {finding.trigger}\n"
        f"  hypothesis: {finding.hypothesis}\n\n"
        f"FULL CODE IT CITES (whole definition; changed lines marked '*')\n"
        f"{render_unit(unit)}\n\n"
        f"Refute or uphold, per the rules."
    )


def verify_finding(finding, unit, profile) -> tuple[bool, str]:
    """(upheld, reason). No matching unit → refuted (cannot evidence it). A
    failed refute CALL raises — the caller handles it fail-open."""
    if unit is None:
        return False, "no matching code unit to verify against"
    data, _ = chat(profile, REFUTE_SYSTEM, build_refute_prompt(finding, unit))
    return bool(data.get("upheld", False)), str(data.get("reason", ""))


def verify_findings(findings, units, profile):
    """Adversarially verify each finding. Returns (kept, dropped); dropped is
    [{fingerprint, display_id, reason}] for the refuted ones. A finding kept
    despite a FAILED verify call (LLM blip) is tagged unverified — never
    silently deleted."""
    kept, dropped = [], []
    for f in findings:
        unit = _find_unit(units, f)
        try:
            upheld, reason = verify_finding(f, unit, profile)
        except Exception:  # noqa: BLE001 — fail-open: a blip must not delete a real bug
            kept.append(replace(f, found_by=f"{f.found_by} [unverified: refute-call-failed]"))
            continue
        if upheld:
            kept.append(f)
        else:
            dropped.append({"fingerprint": f.fingerprint,
                            "display_id": f.display_id, "reason": reason})
    return kept, dropped
