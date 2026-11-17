# edi-run

Minimal Python 3.10+ standard-library reference runtime for [Evaluator-Driven Iteration (E-DI)](https://github.com/evaluatordriven/doctrine) emitting conforming [`custody/v1` records](https://github.com/evaluatordriven/custody).

## Architecture

See [`docs/2026-EDI-Runtime-Solution-Architecture.md`](docs/2026-EDI-Runtime-Solution-Architecture.md).

## Examples

- `examples/two-round-job/` — Offline deterministic script Maker + script Evaluator (`CL1 · process`).
- `examples/live-llm-job/` — Live LLM Maker (`qwen/qwen3-32b`) + dual Evaluators (`StructGate` script + `anthropic/claude-opus-5.5` model) converging in 2 rounds (`CL1 · process`).
- `examples/live-cl2-job/` — Live multi-operator `CL2 · process` loop (`qwen/qwen3-32b` on `openrouter.ai` + `StructGate` script + `gemini-3-flash-preview` on `generativelanguage.googleapis.com`, `R=2` runs per dimension with `spread[]`).

## Usage

```sh
python3 edi.py freeze <job_dir>
python3 edi.py run <job_dir>
python3 edi.py finalize <job_dir> --disposition given --by "Casey Park"
python3 edi.py verify <job_dir>
```

## Doctrine conformance

edi-run implements `evaluatordriven/doctrine` **v0.2.1** (ratified 2026-11-02). Notable rule implementations: a verdict rests on at least two scored runs per dimension (D4.5; freeze refuses fewer); the next round's packet carries every known defect with the round that measured it, and any packet text matching held-out material is replaced with `[withheld: matches held-out material]` and counted (D4.9 — held-out material is filtered out of the Maker's input, never fatal); `escalation.json` carries what was tried (`tried`) and why no observable answer exists (`why`, D4.11); a gate refusal mid-run halts as `integrity_error`, never mislabelled as an interruption.

Agent Makers at every level require `maker.sandbox` (`{"type": "sandbox-exec"}` on macOS, `{"type": "bwrap"}` on Linux) — the runtime itself launches the agent under that confinement, denying `held_out/`, the custody log, scores, evidence, and secret files (including `~/.hermes/.env`); freeze refuses if the sandbox type is unavailable on the host. The agent's model calls go through the loop-hosted proxy, which answers 403 to any model other than the declared `maker.model.identifier` (logged, never metered; the full proxy log persists beside the round as `proxy-usage.jsonl`). Disclosed limits: the needle-based held-out filter catches contiguous fragments (including case and whitespace variants), not paraphrases; a keyless open endpoint named in `maker.url` remains a theoretical exfiltration channel for a hostile agent, since the sandbox cannot distinguish it from the proxy's loopback traffic.

## Maker seats

Three Maker kinds in `job.json`:
- `"kind": "script"` — a command run in an isolated workspace (minimal env, no keys).
- `"kind": "model"` — a single chat call by the loop; keyed from the loop's environment.
- `"kind": "agent"` — a blocking tool-using agent child in its own confined workspace. The agent calls models **only** through a local proxy the loop spins up (`AGENT_PROXY_URL`): the key never enters the agent's environment, and every upstream call is metered toward the cost cap and logged (host, model, usage). The agent writes subject files into its workspace and exits; nonzero exit or no files halts as `needs_owner/maker_failed`. In the custody record an agent Maker is recorded as `kind: "model"` (the checker's enum) with its declared model identity. Evaluators remain script or single model calls by design — their simplicity is what makes them trustworthy.

## Starting from an existing draft

Set `seed` in `job.json` — `{"dir": "seed", "author": "Dana Voss", "origin": "Owner draft 2026-11-16"}` — where `seed/` contains exactly the `subjectFiles`. The draft is logged as `artifact-supplied` before the bar freezes, evaluated in round 1 like any candidate, and revised from there; a draft that already meets the bar finishes with zero Maker calls. The seed's author is recorded as the maker of record for that artifact and cannot serve as an Evaluator.

## Cost caps

Jobs with model seats must carry a `pricing` map in `job.json` (`{"model-id": {"input_usd_per_mtok": X, "output_usd_per_mtok": Y}}` — a `"default"` entry is accepted); `freeze` refuses the job otherwise. Every model call — Maker, checks, and controls — accrues into `state.json` (`token_usage`, `cost_usd`) and the `max_cost_usd` cap is enforced. The two live examples ship with $0 placeholders so they freeze; fill in real rates before treating their cost figures as spend.

## Incomplete runs

A run that halts without a Pareto-best artifact, or whose record assembly fails, still emits a record: a gap-listed document (`recordType: "edi-run/gap-record"`) whose `x-edi-run.gaps[]` names each unmet invariant from the architecture doc §1. It is invalid by design — `check.py` can never read it as a valid `custody/v1` statement; the disposition event is still logged in the hash-chained `job.jsonl`. Integrity failures are never downgraded: a tampered, truncated, or foreign custody log, modified frozen instruments, or a repeated `finalize` hard-fail with `GateError` and write nothing. `finalize` runs once per job.

## Exit codes

- `0` — loop `accepted`, or verification `OK`
- `1` — loop `rejected`, checker `FAILED`, or a gap-listed record was emitted
- `2` — loop halted at `needs_owner`, checker `NOT-CHECKABLE`

## CI

GitHub Actions runs the suite on ubuntu (3.10, 3.12) and macOS (3.12). The macOS matrix is a single leg on purpose: the hosted macOS runner queue has repeatedly cancelled queued jobs at ~15 minutes with zero steps executed, and fewer macOS legs means fewer queue rolls. Every macOS failure to date has been a queue kill, not a test failure.

Note: the `live-llm-job` example's committed record predates the two-scored-runs rule (D4.5) and still verifies under the current checker; regenerating it requires paid model calls and is pending Owner approval. Its `job.json` already carries the conforming `runs: 2`.

## Verification & Tests

```sh
python3 -m unittest discover -s tests -p "test_*.py" -v
for ex in two-round-job live-llm-job live-cl2-job; do
  python3 vendor/record.py verify examples/$ex/job.jsonl --files
  python3 vendor/check.py examples/$ex/record.json --files examples/$ex/artifact --bar examples/$ex/bar.md --evidence examples/$ex/evidence
done
```
