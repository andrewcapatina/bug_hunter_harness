"""System-prompt assembly — BUG_HUNTER_SPEC.md §2/§4.

The prompt encodes the invariants as instructions to the model: whole-unit
protocol, mandatory trigger, reachability×impact severity, in-window-only
evidence, empty-is-honest, intentional-conventions-are-not-bugs, and the
do-not-re-emit-resolved rule. The bot-specific parts — identity preamble and
the domain glossary that stops the model calling real conventions bugs — come
from config.
"""
from __future__ import annotations

_SCHEMA_AND_RULES = """\
You review WHOLE definitions (function / class / block), each labeled with its
file and symbol, lines absolute, changed lines marked '*'. Report ONLY bugs
introduced or exposed by the changed lines and fully evidenced within a block
shown to you.

Output JSON ONLY: {"findings": [ ... ]}. Each finding:
  path, symbol, line   — a real file:line INSIDE a shown block (never guess)
  severity             — low|medium|high, = REACHABILITY × IMPACT
  confidence           — low|medium|high, = how sure you are it is real
  category             — logic|safety|perf|data|consistency|exec|docs
  trigger              — the concrete input/state that REACHES it on a real
                         path. REQUIRED. No constructible trigger => severity low.
  title                — short imperative
  hypothesis           — one paragraph, citing lines within the shown block
  suggested_fix        — one paragraph
  auto_fix_class       — auto_applicable | proposal | human_only
  regression_of        — a prior resolved fingerprint, only with fresh evidence

HARD RULES:
- You are shown the WHOLE definition. Do NOT report a missing return / guard /
  import / parameter unless it is absent from the code shown — the surrounding
  lines you need ARE here.
- Do NOT invent findings to fill a quota. Zero findings is a valid, honest answer.
- Intentional conventions are not bugs. Unusual names, domain codes, and field
  names (see the glossary) are not defects.
- Verify before you emit: can you construct the trigger? Is the cited evidence
  actually in the shown lines? If not, drop it."""


def _glossary_block(glossary: dict) -> str:
    if not glossary:
        return ""
    lines = [f"  - {term}: {meaning}" for term, meaning in glossary.items()]
    return ("\nGLOSSARY — these are correct, established conventions, NOT bugs:\n"
            + "\n".join(lines))


def _resolved_block(resolved: dict) -> str:
    if not resolved:
        return ""
    ids = ", ".join(sorted(resolved)[:60])
    return ("\nALREADY RESOLVED — do NOT re-emit these fingerprints unless you "
            "have NEW evidence of a regression (then set regression_of):\n  "
            + ids)


def build_system_prompt(config: dict, resolved: dict | None = None) -> str:
    identity = config.get(
        "identity_preamble",
        "You are a senior engineer reviewing changes to a trading bot.")
    return "\n\n".join(filter(None, [
        identity,
        _SCHEMA_AND_RULES,
        _glossary_block(config.get("glossary", {})),
        _resolved_block(resolved or {}),
    ]))
