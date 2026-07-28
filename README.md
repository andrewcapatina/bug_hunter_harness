# trading-harness

One shared, model-agnostic autonomous code-review harness ("bug_hunter") for
both the crypto-bot and options-bot. Replaces the two diverged per-bot copies.

- **Spec:** [`BUG_HUNTER_SPEC.md`](BUG_HUNTER_SPEC.md) — the WHAT/WHY (invariants,
  failure catalog, contracts). Read it first.
- **Design goal:** the false-positive classes we diagnosed (context-starved diff
  slivers, misattribution, dedup inflation, miscalibration) are made
  *structurally impossible*, and the model is a swappable **profile**.

## Run

```bash
python -m harness --config configs/crypto.json           # review only
python -m harness --config configs/options.json --fix    # review + guard-railed fixer
```

Writes the findings sidecar + marker (paths per config) and prints
`STATUS=` / `REASON=`. `--fix` additionally applies `auto_applicable` findings
through the guard-railed fixer and appends the ledger.

## Shape

```
harness/
  units.py     diff -> WHOLE enclosing definitions (the dominant lever)
  model.py     profile-driven OpenAI-compatible client (transferability)
  batch.py     pack whole units to the profile's context budget
  findings.py  schema + content-hash identity + calibration (in-window gate)
  dedup.py     semantic dedup (collapses reworded restatements)
  verify.py    adversarial refute pass (drops plausible-but-wrong)
  gather.py    git window + sidecars + resolved fingerprints
  prompt.py    invariants-as-instructions + per-bot glossary
  status.py    PASS/WARN/SOFT_HALT (advisory)
  fixer.py     guard-railed scripted fixer
  sidecar.py   per-bot output adapter
  run.py       orchestrator
  __main__.py  CLI
configs/       one JSON per bot — everything bot-specific
tests/         109 tests, zero LLM/git/filesystem coupling
```

Everything bot-specific is config (repo path, model profile, sidecar/marker
layout, reviewed sidecars, glossary, fixer allow/deny paths + ledger). Zero bot
logic in the harness — it cannot drift again.

## Migration (per spec §9)

1. **Parity-gate** — run against a captured recent diff for each bot; confirm it
   reproduces the true bugs and drops the verified false positives from the
   session catalog. (A golden-diff test under `tests/golden/` makes this
   repeatable.)
2. Point both bots' chains at `python -m harness --config <bot>.json` (crypto via
   `docker exec`, options via its venv — the invocation is per-bot glue in each
   chain, not in the harness).
3. Archive (don't delete) the per-bot `bug_hunter_run.py` / `llm_batch.py` /
   `bug_fixer_run.py` until a full cycle proves parity.

## Model portability

Swapping models is editing `model_profile` in a config: `endpoint`, `model`
(or `"auto"` discovery), `context_window_tokens` (batch sizing derives from it),
`sampling` (`"server_default"` or an explicit block with a
`sampling_incompatible` drop-list), `response_format`, `reasoning_model`, and an
optional `api_key` for frontier models. No code change.
