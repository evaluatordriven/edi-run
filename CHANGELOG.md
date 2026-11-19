# edi-run CHANGELOG

## v0.1.2 (2026-11-19)

Fixes from the maintainer feedback on 0.1.1 (NOTE-TO-MAINTAINER-edi-run-0.1.1-2026-11-19) and the seed-author scope correction:

- `gate_authorship`: the seed's author is barred only from **Evaluator** seats (Ruling A scope). The raise-bounds successor path — re-freeze a successor job carrying the prior incumbent forward as seed, authored by the producing Maker — freezes again with an honest AUTHORSHIP.md.
- `finalize` no longer crashes (KeyError → treated as empty) when `job.json` has no `disposition` block; `verify` follows.
- `gate_freeze_pre` now validates the CL1-2 scale shape at freeze (anchors must be a non-empty object keyed by score; floor ≤ the lowest anchor score, and threshold between floor and the highest anchor) — a bad scale can no longer fail `verify` after spend.
- `freeze` on an empty or non-job folder refuses with a GateError instead of a traceback.
- **Breaking:** `adapters._load_key`: the `~/.hermes/.env` key fallback is now **opt-in** (`EDI_KEY_FALLBACK=1`). By default only the process environment supplies keys — a host that relied on the fallback must set the variable or export the key. Freeze refuses a model/agent seat whose key resolves empty, naming the remedy.
- Helper scripts (`preflight.py`, `record_summary.py`, `escalation_report.py`, `cost_report.py`) resolve the job path before invoking `edi.py`, so relative job paths work from any working directory.
- **Breaking (installed skill copies):** `common.repo_root()` no longer falls back to `~/Code/edi-run`: it requires `EDI_REPO`, or the script must live inside the repo tree (located relative to the script file, not the working directory).
- **Breaking (was shipped in v0.1.1):** `record_summary.py` scores changed shape from `{dim: number}` to `{dim: {observations[], min, max, straddles_threshold}}` — a split between Evaluators is never collapsed to the best score. Update anything reading the old shape.

## v0.1.1 (2026-11-19)

Operator-skill defect fixes (2026-11-19 briefing defects 1–6 + minors) and the 2026-11-18f release-review A1–A8 fixes; Ruling A (authorship/seats) enforced at freeze. See the v0.1.1 release notes on GitHub. **Note the breaking `record_summary` score-shape change listed under v0.1.2** — it shipped in v0.1.1 and is documented here because no changelog existed at the time.
