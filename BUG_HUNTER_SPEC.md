# bug_hunter harness — specification

One shared implementation, one spec, N bots via JSON config, any model via a
profile. This document is the WHAT + WHY (like `TESTING_PLAN.md`); the code
that follows it is the HOW. Written 2026-07-27 from a survey of the two
diverged copies (crypto-bot + options-bot `bug_hunter_run.py` / `llm_batch.py`
/ `bug_fixer_run.py`) and a session of hand-verifying ~40 of their findings.

## Why this exists

`bug_hunter` runs once per daily chain: it reads the last 24h of code changes
and emits structured findings a downstream fixer resolves autonomously. Two
problems forced a ground-up rewrite:

1. **Drift.** The harness was copy-pasted per bot and the copies diverged
   (options' `bug_hunter_run.py` grew to 33KB with guardrails crypto never
   got). Bug fixes and improvements landed in one and not the other.
2. **False positives.** On a large diff the model produced mostly refuted
   findings — truncation hallucinations, out-of-window "missing return/guard"
   claims, file misattribution, dedup inflation (the *same* bug reported 8×).
   Diagnosis (verified against the code, not guessed): **~70% of the noise is
   the harness feeding the model context-starved diff slivers it cannot
   verify; ~30% is model limitation** (weak domain recall, poor confidence
   calibration). A frontier model would also hallucinate on a function cut
   mid-body — you cannot reason about code you were never shown.

The spec makes the false-positive classes **structurally impossible**, not
merely discouraged, and makes the harness **one codebase + per-bot config +
per-model profile** so it can neither drift nor hard-code a model again.

## 0. Invariants (non-negotiable; every design choice serves one)

1. **The model only judges code it can fully see.** Every review unit is a
   WHOLE enclosing definition (function / class / block) or a whole small
   file — never a bare diff hunk, never a unit split across batches.
2. **Evidence must be real and in-window.** Every finding cites a
   `file:symbol:line` that EXISTS and lies inside the shown unit. A finding
   whose evidence would require code outside the shown unit is dropped, not
   downgraded.
3. **Severity = reachability × impact.** A finding with no constructible
   `trigger` (concrete input/state reaching it on a real path) cannot be high.
4. **Confidence reflects verifiability, and unverified findings don't ship.**
   A finding a second, adversarial pass cannot reproduce never reaches a human
   or the fixer.
5. **Findings are identified by a stable content fingerprint**, not a per-run
   renumbered ID — so dedup and resolved-suppression work across days.
6. **A resolved finding is never re-emitted** without fresh regression
   evidence (`regression_of` + a citation).
7. **The harness is advisory and read-only to trading code.** It never halts
   trading and never edits code; only the guard-railed fixer edits, within
   allow-listed paths.
8. **One implementation, N configs.** Zero bot logic in the shared code;
   everything bot-specific is declarative config.
9. **The model is a profile, not code.** Swapping models (local ↔ frontier,
   DeepSeek ↔ other) is a config change, never a code change.

## 1. Failure catalog — the design driver

Each verified false-positive class from the session → the invariant that kills
it. This is the "incident list"; a change that reintroduces any row fails review.

| Failure (verified example) | Root cause | Killed by |
|---|---|---|
| Truncation hallucination — `out.pare` (real: `out.parent.mkdir`), `idecar_mode` (real: `if _sidecar_mode`), `volume_by_ho` | Batch cut a line/function mid-body; model treated the cut as the code | I1 (whole-unit, never split mid-function) |
| "Missing return / guard / param" that exists just outside the hunk — `_boundary_for_coid` length guard, `create_market_sell_order` params | Guard/return lived outside the ±3 diff-context lines | I1 + I2 |
| File misattribution — `funding_xs_scan` blamed on `backtest_v13_combined.py` (it's in `strategy_lab_run.py`) | Batch not bound to a filename | I2 (filename+symbol on every unit) |
| Domain-prior error — Coinbase perp codes (ETP/SLP/XLP) called "fake"; `ts_ms` not recognized as milliseconds | Model applied generic priors to bot conventions | Per-bot **glossary** in config (§8) + I4 |
| Dedup inflation — one freshness bug reported 8× in a run | Structural `(file, title)` dedup misses reworded restatements | I5 (fingerprint) + semantic dedup (§5) |
| Miscalibrated confidence — hallucinations marked "high" | No reachability gate, no verify pass | I3 + I4 + §5 verify |

Note: options' current harness already blunts several of these **in the prompt**
(mandatory `trigger`, reachability×impact, "verify before you emit"). The spec
keeps those AND adds the structural guarantees neither harness has (whole-unit
context, stable fingerprints, semantic dedup, a real verify pass).

## 2. Review-unit & context contract (the biggest lever)

Both current harnesses feed raw unified-diff hunks; crypto hard-chunks a large
file mid-body with `"... [continued from previous batch]"` markers. **This is
the single largest source of noise and it is removed.**

- **Discover** changed regions from `git diff` as today, but **expand** each
  changed region to its whole enclosing definition (language-aware: Python
  `def`/`class`, shell function, or — for JSON/MD/small files — the whole
  file). The diff says *what changed*; the model reviews *the whole thing that
  changed*, plus its signature and any guards/returns around it.
- **A review unit = `{path, symbol, start_line, full source of the definition,
  changed_line_ranges}`.** The model is shown the full unit and told which
  lines changed. It is instructed to reason about the whole unit and cite lines
  within it.
- **Never split a unit across batches.** A batch is a set of WHOLE units packed
  to the model profile's context budget (§3), not a hardcoded char count. A
  single unit larger than the budget is sent whole in its own batch — a giant
  function is rare and is better reviewed whole than sliced.
- **Every unit carries its `path` and `symbol`** in the prompt, so a finding
  cannot be misattributed.
- Fallback: a changed region with no resolvable enclosing definition (e.g. a
  config/data file) is sent as the whole file when small, else as the hunk
  **explicitly labeled "hunk-only, no surrounding context — do not infer
  missing code"** so the model self-limits.

This subsumes both bots' file-boundary splitting and upgrades hunk → whole-unit.

## 3. Model interface & profile (transferability)

The model is described by a **profile** (a config object), and the harness
derives its behavior from it. Swapping models is editing the profile.

Profile fields:
- `endpoint` — OpenAI-compatible `/v1` base URL.
- `model` — model id, or `"auto"` → discover the first id from `/v1/models`
  (crypto's approach; survives server-side model swaps).
- `context_window_tokens` — **batch sizing derives from this**, not a hardcoded
  4KB. (Both current harnesses hardcode 4KB calibrated on MiniMax-M3; that
  breaks the moment the model changes.)
- `max_output_tokens`, `request_timeout_s` (size from context + tok/s).
- `sampling` — one of: `"server_default"` (send no params — crypto's default,
  correct for reasoning models with a tuned `generation_config`); or an
  explicit `{temperature, top_p, top_k, min_p}` block (options' DeepSeek
  recipe). A `sampling_incompatible: [...]` list omits params a lane rejects
  (e.g. `min_p` on spec-decode → 400).
- `response_format` — `"json_object"` (guided JSON, load-bearing), `"json_schema"`
  if the server supports it (stronger), or `"none"` → rely on extraction.
- `reasoning_model` — bool; controls `</think>` stripping and sampling policy.

Shared, model-family-robustness behaviors the harness always applies (both
bots learned these the hard way — keep them):
- Guided JSON via `response_format`; robust extraction (skip past `</think>`,
  strip code fences, balanced-brace scan) for models that leak prose.
- Per-batch isolation: one batch's failure never sinks the run; one corrective
  retry per batch (crypto has this; port it).
- Centralize the call (options' `chat_batch`); retire crypto's inline
  `_llm_chat`.

The harness is thus **model-agnostic on discovery** (crypto's strength) AND
**model-agnostic on config surface** (options' strength), with the per-model
specifics that used to live in module constants + comments now in the profile.

## 4. Findings schema & calibration

Adopt options' richer schema as the floor; add the stable fingerprint.

```
{
  "fingerprint": "<stable content hash — §5; the canonical identity>",
  "display_id": "BH-<date-first-seen>-<fp8>",   // human-readable, content-derived, NEVER renumbered
  "path": "<repo-relative>", "symbol": "<enclosing def>", "line": <int>,
  "severity": "low|medium|high",        // reachability × impact (I3)
  "confidence": "low|medium|high",       // verifiability (I4)
  "category": "logic|safety|perf|data|consistency|exec|docs",
  "trigger": "<concrete input/state that reaches this on a real path>",  // REQUIRED
  "title": "<short imperative>",
  "hypothesis": "<one paragraph, cites lines within the shown unit>",
  "suggested_fix": "<one paragraph>",
  "auto_fix_class": "auto_applicable|proposal|human_only",  // fixer contract
  "regression_of": "<fingerprint of a prior resolved finding, or null>",
  "found_by": "<unit/batch label>"
}
```

Calibration rules (in the system prompt AND enforced post-hoc where possible):
- No constructible `trigger` → severity capped at low (I3).
- Evidence line not inside the shown unit → drop (I2).
- "Empty is honest" — never invent findings to fill a quota (both bots say
  this; keep it).
- Intentional conventions are not bugs (options' rule; keep it).

## 5. Fingerprint, semantic dedup, and the verify pass

**Content-hash identity (I5) — the canonical key everywhere.**
`fingerprint = sha256(normalize(path + "::" + symbol + "::" + normalize(hypothesis)))[:16]`,
where `normalize` lowercases and strips non-alphanumerics. The fingerprint —
NOT a positional ID — is the identity used for dedup, the ledger, resolved-
suppression, and the fixer commit tag. The same bug hashes identically across
runs and days.

**There is no per-run renumbering.** The prior harnesses assigned
`BH-<date>-NNN` gap-free every run, so a finding's ID changed day to day while
resolved-suppression tried to match on it — cross-day suppression silently
broke. Here the human-readable `display_id` is itself content-derived:
`BH-<date-first-seen>-<first 8 hex of fingerprint>`. It's readable AND stable —
the same bug shows the same display_id every run until it's actually fixed or
the code changes enough to change the hash. `date-first-seen` is looked up from
the ledger/resolved set by fingerprint (falls back to today on first sight),
never regenerated.

**Semantic dedup across units/batches.** Both bots dedup structurally on
`(first_file, title)` + a hypothesis hash — which misses reworded restatements
(hence 8× the same bug). Cluster instead by fingerprint AND
hypothesis-similarity (normalized-token Jaccard ≥ threshold, or embeddings if a
cheap embedder is in the profile); collapse a cluster to its
highest-severity/confidence member with `found_by` listing all sources.

**The verify pass (new — NEITHER harness has one).** After dedup, each
surviving finding gets a second, adversarial model call over the SAME whole-unit
context, prompted to REFUTE it: *is the trigger real and reachable? is the
cited evidence actually in the code? is this an intentional convention?* A
finding that isn't upheld is dropped (logged as self-refuted, not shown). This
is the highest-leverage anti-false-positive step after whole-unit context. It
is REQUIRED; the verifier may be the same model with a refute prompt, or a
stronger model if the config names one. Options' in-prompt "verify before you
emit" stays too (belt + suspenders).

## 6. Disposition & feedback loop

Adopt options' **deterministic guard-railed script fixer** (not crypto's
free-form Claude agent): the LLM proposes patches, Python enforces safety.

- Input: findings with `auto_fix_class == "auto_applicable"` not already in the
  ledger (by fingerprint).
- Per finding: read the cited whole file(s) → LLM proposes
  `{reasoning, patches:[{file, old_string, new_string}]}` → **validate**:
  path allow/deny lists (config), `max_patch_lines`, `old_string` appears
  exactly once, syntax check (`py_compile` / `json.loads` / `bash -n`) →
  apply all-or-nothing with revert on any failure → git commit
  (`[auto-fix <fingerprint>]`) + audit entry → append ledger.
- **Ledger keyed by (run_id, fingerprint)** for idempotency; record
  `{ts, run_id, fingerprint, outcome, files_changed, commit_sha, reason}`.
- Circuit breaker: `consecutive_failure_halt` (config) aborts the fixer.
- `proposal` / `human_only` findings → a proposals queue. Per the autonomy
  directive, build the autonomous path first; reserve `human_only` for
  genuinely unsafe classes (credential files, broker adapters).

**Resolved-suppression back into the hunter.** The hunter gathers resolved
**fingerprints** (ledger + `git log --grep` over a lookback window — options'
dual-source approach, but keyed by fingerprint not ID) and injects a
do-not-re-emit list. Re-emission requires `regression_of: <fingerprint>` + a
fresh citation, else the finding is dropped as noise.

## 7. Status / halt semantics (shared, identical for both bots)

- PASS: no findings, or only low/advisory.
- WARN: any high, OR ≥3 medium, OR ≥6 total, OR **any batch failed** (options'
  "silence would be a lie" — a partial run must not read as clean).
- SOFT_HALT: all batches failed (model unreachable), or input-gather failed.
- WARN is **advisory only** — never halts the chain; the harness never emits
  HARD_HALT. Research/quiet days are PASS.

The harness produces a **canonical findings artifact + `STATUS`/`REASON`**. A
thin per-bot adapter maps that to the bot's marker/sidecar convention (crypto's
marker-first staged→latest with `write_marker`; options' timestamped run file)
— the write contract is a config axis (§8), the harness logic is shared.

## 8. Per-bot config (JSON) — the schema

Everything the survey found hard-coded-to-bot becomes config. Shared logic
(gather windows, unit expansion, batching, prompt guardrails, extraction,
fingerprint, dedup, verify, status thresholds) is NOT configurable — it's the
spec.

```jsonc
{
  "bot": "crypto",
  "repo_root": "/path/to/your-bot",
  "invocation": { "kind": "docker", "container": "openclaw-coinbase-ticker-1" },
  //            or { "kind": "venv", "python": ".venv/bin/python" }
  "model_profile": {
    "endpoint": "http://YOUR_LLM_HOST:8001/v1",
    "model": "auto",
    "context_window_tokens": 64000,
    "max_output_tokens": 16384,
    "request_timeout_s": 1800,
    "sampling": "server_default",
    "response_format": "json_object",
    "reasoning_model": true
  },
  "gather": {
    "since_hours": 24,
    "diff_max_chars": 200000,
    "reviewed_sidecars": ["*_latest.json"],   // glob, or explicit names
    "broker_state": null,                       // options: {services, path_tmpl, key_whitelist}
    "bugs_md": "docs/bugs.md"
  },
  "identity_preamble": "Describe YOUR bot: live strategies and anything retired",
  "glossary": {                                 // kills domain-prior false positives
    "ETP|SLP|XPP|XLP|BIP": "real Coinbase perpetual-futures product codes, NOT typos",
    "ts_ms": "millisecond epoch timestamp field (not seconds)"
  },
  "sidecar": { "layout": "chain_marker", "dir": "state/_chain", "marker_dir": ".claude/agents" },
  "fixer": {
    "allowed_paths": ["*.py", "strategies/*.py"],
    "blocked_paths": ["*_creds*.py", "trade_executor.py"],
    "max_patch_lines": 30,
    "verify_commands": ["python3 -m py_compile {file}"],
    "consecutive_failure_halt": 3,
    "ledger_path": "state/_bug_dispositions.jsonl"
  }
}
```

Config axes (from the survey's coupling table): repo root & invocation
(docker vs venv), model profile, gather windows + reviewed-sidecar set +
optional broker-state gatherer + bugs.md path, bot identity preamble, the
domain glossary, sidecar/marker layout & write contract, and the fixer's
allow/block paths + verify commands + ledger path/schema. A second file,
`configs/options.json`, differs only in these values.

## 9. Shared-harness layout & migration

**Layout** (`Projects/trading-harness/`):
```
trading-harness/
├── BUG_HUNTER_SPEC.md            # this file
├── harness/
│   ├── gather.py                 # git window + sidecars + bugs.md + resolved fingerprints
│   ├── units.py                  # diff → whole-definition review units (§2)
│   ├── batch.py                  # pack whole units to the profile's context budget
│   ├── model.py                  # profile-driven OpenAI-compatible client (§3)
│   ├── findings.py               # schema, fingerprint, calibration (§4)
│   ├── dedup.py                  # semantic dedup (§5)
│   ├── verify.py                 # adversarial refute pass (§5)
│   ├── fixer.py                  # guard-railed script fixer (§6)
│   ├── status.py                 # PASS/WARN/SOFT_HALT (§7)
│   └── sidecar.py                # per-bot write-contract adapter (§7/§8)
├── configs/{crypto,options}.json
└── tests/
    └── golden/                   # fixed diffs → expected findings (regression)
```

**Migration / work order:**
1. Build the shared harness to this spec (units/context first — the lever).
2. Write `configs/crypto.json` and `configs/options.json` from the survey's
   coupling table.
3. **Parity-gate before cutover:** run the shared harness on a captured recent
   diff for each bot; confirm it reproduces the true findings and drops the
   verified false positives from this session's catalog (§1). A golden-diff
   test (fixed input → expected findings) makes the harness itself testable —
   same discipline as `TESTING_PLAN.md`.
4. Point both chains at `trading-harness/harness` with `--config <bot>.json`.
5. Retire the per-bot `bug_hunter_run.py` / `llm_batch.py` / `bug_fixer_run.py`
   (archive, don't delete, until a full cycle proves parity).

## 10. What we keep, and what's new (so nothing is lost)

**Keep from options** (its false-positive investment): the prompt batch
protocol, mandatory `trigger`, reachability×impact severity, "verify before you
emit", filename-bound batches, resolved-suppression with a `regression_of`
escape hatch, the scripted guard-railed idempotent fixer with `auto_fix_class`
triage, the broker-state input, and the centralized `chat_batch`.

**Keep from crypto:** `auto` model discovery from `/v1/models`,
`server_default` (no-sampling) as a first-class option, and the marker-first
staged→latest sidecar contract as one supported write layout.

**New in the spec (neither harness has today):** whole-definition review units
(the dominant lever), the model **profile** abstraction (transferability),
stable content **fingerprints** (fixes ID drift + cross-day suppression),
**semantic** dedup, a dedicated **adversarial verify pass**, and the per-bot
**glossary** that stops the model calling real conventions bugs.

Net expectation: most of what the model gets "wrong" today is code it was never
shown. Give it whole definitions, a refute pass, and a glossary — and the
false-positive rate should drop sharply without reaching for a bigger model.
