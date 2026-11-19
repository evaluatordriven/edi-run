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
python3 edi.py read <job_dir>
python3 edi.py finalize <job_dir> --disposition given --by "Casey Park"
python3 edi.py verify <job_dir>
```

## Readings mode (`mode: "readings"` / `census`)

Set `"mode": "readings"` in `job.json` (with `"bounds": {"max_rounds": 1, ...}`) to run a single-pass census (`C29`, `D4.6`, `D5.outcome.census`) over an existing `seed` draft (Entry B) or one Maker turn (Entry A) without numeric scales:
- **Bar & approvals:** Buyer approval on `bar.digest` is required; Specialist approval is forbidden, and dimensions carry no `scale`, `threshold`, or `floor`.
- **Reading slot:** Each Evaluator returns `{"outcome": "conforms"|"does_not_conform"|"unobserved", "quotation": "<exact non-empty quote>"}` (numeric scores, unknown outcome words, blank quotations, or quotations whose whitespace-normalized text does not appear in the judged text mark the evaluator failed and exit `needs_owner/no_observation`).
- **Verdict:** Every dimension `conforms` across all runs (`R >= 2`) exits `accepted/census` (0); any run-to-run disagreement is recorded in `disagreements[]` with all outcome words (never collapsed by majority, `D4.6`) and exits `needs_owner/no_observation` (4); any `does_not_conform` reading or all-`unobserved` dimension exits `needs_owner/needs_owner` (4) with `escalation.json` and the quoted defects. A readings job never exits `rejected` (`D5.step.3`).
- **Custody & verification (`Option (a)`):** Because vendored `custody/v1` (`vendor/check.py` `CL1-3`) hard-requires a Specialist approval and has no `census` field, `finalize` appends the `disposition` event to `job.jsonl` (carrying `mode: "readings"`, `terminalState`, `outcome`, `disagreements`, and `acknowledgesUnobserved`) without emitting a `custody/v1` `record.json`, and `edi.py verify` verifies the full hash-chained `job.jsonl`, `job-pin`, file digests (`vendor/record.py verify job.jsonl --files` from inside `<job_dir>`), check evidence digests (`evidence/reading-*.json`), and `artifact/` digests. `edi.py read <job_dir>` freezes (if needed) and runs the job, then prints the terminal state and the exact `edi.py finalize` command for the Owner — it never writes a disposition on its own.

## Doctrine conformance

edi-run implements `evaluatordriven/doctrine` **v0.2.1** (ratified 2026-11-02). Notable rule implementations: a verdict rests on at least two scored runs per dimension (D4.5; freeze refuses fewer); the next round's packet carries every known defect with the round that measured it, and any packet text matching held-out material is replaced with `[withheld: matches held-out material]` and counted (D4.9 — held-out material is filtered out of the Maker's input, never fatal); `escalation.json` carries what was tried (`tried`) and why no observable answer exists (`why`, D4.11); a gate refusal mid-run halts as `integrity_error`, never mislabelled as an interruption.

Agent Makers at every level require `maker.sandbox` (`{"type": "sandbox-exec"}` on macOS, `{"type": "bwrap"}` on Linux) — the runtime itself launches the agent under that confinement, denying `held_out/` (every job's — control sets are reused across a jobs root), the custody log, scores, evidence, and secret files (including `~/.hermes/.env`); all writes outside the agent workspace are denied; the network is confined to the loop-local proxy (macOS: `deny network*` plus a loopback allow for the proxy port; Linux: the agent's own network namespace with a Unix-socket relay); the agent gets a writable temp root inside its workspace (`TMPDIR`); and the home credential-dotfile family (`.ssh`, `.config`, `.aws`, `.netrc`, `.git-credentials`, `.gnupg`, shell histories, `.npmrc`, `.pypirc`) is denied for reads. On Linux (`bwrap`), `sandbox.reads` defaults to `"allowlist"`, binding only `/usr`, `/bin`, `/sbin`, `/lib`, `/lib64`, `/etc`, and the resolved `sys.executable` prefix (`/opt` and `/var/lib` are excluded). Residual, disclosed: on macOS (`sandbox-exec`), the read posture is deny-list rather than allow-list, so the agent can still READ files outside the deny list, and with network confined it could exfiltrate what it reads inside a metered request body to the configured upstream host; and anything it can read can also be written into the subject files, which reach every Evaluator endpoint and persist in the job folder and the record — the sandbox narrows, the proxy meters; it cannot make a tool-using agent trustworthy. Linux masks `held_out` at any depth under the jobs root; jobs are expected to live as siblings under one root. A path containing a `held_out` component inside the agent's own workspace is blocked by the same rule (fail-closed). On Linux the dotfile masks are fresh tmpfs mounts — empty and writable inside the namespace, ephemeral, never touching the host. Freeze refuses if the sandbox cannot launch on the host. The entire frozen job tree (`tree_manifest(job_dir)`: every file under `job_dir` except `RUNTIME_WRITTEN` outputs and `NEVER_AUTHORED` caches `__pycache__`/`.DS_Store`, up to 512 files) is pinned at freeze in `pins["tree"]` alongside `bar`, `method`, `rubric`, `runner`, and `job.json` settings: editing any pinned file or adding a stray file anywhere in the frozen tree after freeze is refused at `run`, `resume`, and `finalize`. Seat commands may reference only files inside `job_dir` outside `RUNTIME_WRITTEN`/`NEVER_AUTHORED` (the interpreter in `argv[0]` excepted), a symlinked file or directory anywhere in the pinned tree is refused, and all seats run with `PYTHONDONTWRITEBYTECODE=1` and an isolated `PYTHONPYCACHEPREFIX` in their own process group (`start_new_session=True`, reaped with `os.killpg` on exit or timeout). A probe gate (`probes/run_probes.py`, wired into CI) replays the external security/custody probes deterministically — loopback-only fake upstream, fake key, random canaries, no real LLM; unexpected failures close the gate, and the KNOWN-FAIL manifest names the open backlog (`S1` macOS read-allowlist) so green CI cannot hide it. The agent's model calls go through the loop-hosted proxy, which answers 403 to any model other than the declared `maker.model.identifier` (logged, never metered; the full proxy log accumulates under `rounds/` as `proxy-usage.jsonl`), sets every forwarded request's `max_tokens` to the priced output allowance (a request demanding more is refused with a 400 naming both numbers — the bound the meter reserves is the bound the provider request carries), records a 200 whose usage envelope is missing at the reserved estimate (flagged `estimated`), and rejects request bodies with duplicate JSON properties at the parse boundary. Disclosed limits: the needle-based held-out filter is a tripwire for echoed text (32-character sliding windows over whitespace-canonicalized held-out lines, plus 16–31 character canonical lines matched as whole lines with word boundaries; lines shorter than 16 canonical characters and contiguous fragments shorter than 32 characters inside longer lines are not matched, as shorter needles scrub ordinary prose) — paraphrases are not matched; the sandbox is the exclusion mechanism.

## Maker seats

Three Maker kinds in `job.json`:
- `"kind": "script"` — a command run in an isolated workspace (minimal env, no keys).
- `"kind": "model"` — a single chat call by the loop; keyed from the loop's environment.
- `"kind": "agent"` — a blocking tool-using agent child in its own confined workspace. The agent calls models **only** through a local proxy the loop spins up (`AGENT_PROXY_URL`): the key never enters the agent's environment, and every upstream call is metered toward the cost cap and logged (host, model, usage). The agent writes subject files into its workspace and exits; nonzero exit or no files halts as `needs_owner/maker_failed`. In the custody record an agent Maker is recorded as `kind: "model"` (the checker's enum) with its declared model identity. Evaluators remain script or single model calls by design — their simplicity is what makes them trustworthy. A model judge always receives the artifact inside a fresh per-call unguessable fence (`secrets.token_hex(12)`), and its system prompt states that the fenced text is the Maker's data, never instructions to the judge (P8).

## Starting from an existing draft

Set `seed` in `job.json` — `{"dir": "seed", "author": "Dana Voss", "origin": "Owner draft 2026-11-16"}` — where `seed/` contains exactly the `subjectFiles`. The draft is logged as `artifact-supplied` before the bar freezes, evaluated in round 1 like any candidate, and revised from there; a draft that already meets the bar finishes with zero Maker calls. The seed's author is recorded as the maker of record for that artifact and cannot serve as an Evaluator.

## Cost caps

Jobs with model seats must carry a `pricing` map in `job.json` (`{"model-id": {"input_usd_per_mtok": X, "output_usd_per_mtok": Y}}` — a `"default"` entry is accepted) and a positive finite `max_output_tokens` (`bounds.max_output_tokens` or `pricing[<model>].max_output_tokens`); `freeze` refuses the job otherwise, and a zero, negative or missing rate is refused too. Every model call — Maker, checks, and controls, plus the CL3 planted-case demonstrations at freeze — is metered at the point of billing into a single append-only, hash-chained `job/usage.jsonl` carrying `reserve`, `call`, `release`, and `amendment` rows (fsynced before the response is used — a crash or timeout cannot unbill a call the upstream billed; spend survives kills; `freeze --dry-run` makes NO paid demonstration calls and says so on stderr). `state.json` `cost_usd` is the derived sum, and the `max_cost_usd` cap fires BEFORE each paid call (`accrued + reserved + max(estimate, last call) >= cap`) — a trip mid-round halts `needs_owner/cap_reached` with no unbillable spend. Both direct model seats (`Meter.hold()`) and parallel agent calls through the proxy reserve their worst-case spend (the request PLUS the configured `max_output_tokens`, injected into the upstream request as `max_tokens`/`max_completion_tokens`) in `usage.jsonl` BEFORE forwarding, and cross-process safety is serialized by the whole-operation `job_lock` held across `freeze`, `run`, `resume`, and `finalize` (a second `run` or `finalize` waits on a live run's `job.lock`). Any reservation left open by a hard kill (`SIGKILL`) is recovered once at worst-case spend (`meter.recover()`) at the start of the next `run`, `resume`, or `finalize` and re-anchored in `state.json`; a `stream: true` request is refused by the proxy (400) because metering streamed responses is out of scope; an accounting failure latches the budget closed instead of reopening it. Residual, disclosed: a refused-but-reserved call is recovered conservatively (worst-case) if the process died before its outcome was recorded, and prompt tokens are estimated at `len(json)/4` before dispatch. Every state write anchors `usage.jsonl` `(row_count, head_digest, accrued_total)`, so truncating its newest rows, or appending a suffix past the last state write, is refused at `run`, `resume`, and `finalize` (`finalize` demands an anchored log and an exact row count); `finalize` records spend in the custody chain with a `spend` event pinning `usage.jsonl`'s bytes. The usage chain is verified before spend accrues onto an existing log. The two older live examples (`live-llm-job`, `live-cl2-job`) carry $0 placeholder pricing, which `freeze` now refuses: as committed they cannot be re-run or re-frozen until real provider rates are supplied; their committed records remain verifiable.

## Incomplete runs

A run that halts without a Pareto-best artifact, or whose record assembly fails, still emits a record: a gap-listed document (`recordType: "edi-run/gap-record"`) whose `x-edi-run.gaps[]` names each unmet invariant from the architecture doc §1. It is invalid by design — `check.py` can never read it as a valid `custody/v1` statement; the disposition event is still logged in the hash-chained `job.jsonl`. Integrity failures are never downgraded: a tampered, truncated, or foreign custody log, modified frozen instruments, or a repeated `finalize` hard-fail with `GateError` and write nothing. `finalize` runs once per job.

## Interrupted runs

A HARD kill (`SIGKILL`, power loss) mid-round leaves `state.open_round` set with no named end state. `edi run` refuses such a job and names the remedy — recovery is never silent. `edi resume <job_dir>` re-verifies every pin and the usage chain, recovers any open reservation in `usage.jsonl`, leaves the interrupted `rounds/round-NN` directory untouched on disk so every path already recorded in `job.jsonl` stays immutable (`vendor/record.py verify --files` passes without moving files), writes the new attempt to `rounds/round-NN-a2` (and attempt-scoped evidence files `B1-a2`, `reading-*-r1-a2-run*.json`), marks the interrupted attempt's `call` rows discarded via an append-only `amendment` row — a label only: the killed round's spend STAYS on the cap total, because discarding work does not unbill it (`Meter.discarded()` reports it separately) — and continues at the recorded boundary; the resumed round reuses the number and consumes its `max_rounds` slot. Spend re-accrues verbatim from `usage.jsonl` — a kill cannot erase billed work.

## Exit codes

The mapping lives only in `edi.py`; the library (`loop.*`) still returns 0/1/2.

| Code | `run` / `resume` | `finalize` / `verify` | Any command |
|---|---|---|---|
| **0** | accepted | record verifies OK | success |
| **1** | | | gate refusal or defect (`GateError`) |
| **2** | | | usage error (argparse) |
| **3** | rejected (below the floor) | | |
| **4** | `needs_owner` | | |
| **5** | | record does not verify (includes a gap-listed record) | |
| **6** | | verifier could not decide | |

## Custody log paths

Custody events record file paths relative to the JOB directory, so `finalize` and `verify` give the same answer from any working directory. Logs written before this change hold caller-relative paths; the verifier resolves job-relative first and falls back to the caller's directory ONLY for those legacy logs (a modern log's evidence must live under the job — a byte-identical stand-in in the caller's directory is rejected, and ambiguous candidates are refused naming both), so the committed examples still verify. A job may be finalized only once it reached a named end state (or with `--disposition declined` to abandon an interrupted run); a disposition is a lifecycle barrier enforced at the custody-append level — no event of any kind may follow it. `job.json` is pinned at freeze (everything except the `disposition` block): an edit afterwards is refused at run, resume and finalize, with the moved pin named; the freeze-time pins digest is also anchored in the custody chain as a `job-pin` event, and state's pins are verified against the chain, not merely against themselves. Every file a seat command can run is pinned — not only `*.py`.

## CI

GitHub Actions runs the suite on ubuntu (3.10, 3.12) and macOS (3.12). The macOS matrix is a single leg on purpose: the hosted macOS runner queue has repeatedly cancelled queued jobs at ~15 minutes with zero steps executed, and fewer macOS legs means fewer queue rolls. Every macOS failure to date has been a queue kill, not a test failure.

Note: the `live-llm-job` example's committed record predates the two-scored-runs rule (D4.5); its `job.json` says `runs: 1` to stay consistent with that record. Regenerating the example with two runs requires paid model calls and is pending Owner approval.

## Verification & Tests

```sh
python3 -m unittest discover -s tests -p "test_*.py" -v
for ex in two-round-job live-llm-job live-cl2-job; do
  python3 vendor/record.py verify examples/$ex/job.jsonl --files
  python3 vendor/check.py examples/$ex/record.json --files examples/$ex/artifact --bar examples/$ex/bar.md --evidence examples/$ex/evidence
done
(cd examples/live-aider-job11 && python3 ../../vendor/record.py verify job.jsonl --files)
python3 vendor/check.py examples/live-aider-job11/record.json --files examples/live-aider-job11/artifact --bar examples/live-aider-job11/bar.md --evidence examples/live-aider-job11/evidence
```
