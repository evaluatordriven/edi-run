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

Agent Makers at every level require `maker.sandbox` (`{"type": "sandbox-exec"}` on macOS, `{"type": "bwrap"}` on Linux) — the runtime itself launches the agent under that confinement, denying `held_out/` (every job's — control sets are reused across a jobs root), the custody log, scores, evidence, and secret files (including `~/.hermes/.env`); all writes outside the agent workspace are denied; the network is confined to the loop-local proxy (macOS: `deny network*` plus a loopback allow for the proxy port; Linux: the agent's own network namespace with a Unix-socket relay); the agent gets a writable temp root inside its workspace (`TMPDIR`); and the home credential-dotfile family (`.ssh`, `.config`, `.aws`, `.netrc`, `.git-credentials`, `.gnupg`, shell histories, `.npmrc`, `.pypirc`) is denied for reads. Residual, disclosed: the agent can still READ files outside the deny list (on Linux, anything on the filesystem not masked), and with network confined it could exfiltrate what it reads inside a metered request body to the configured upstream host; and anything it can read can also be written into the subject files, which reach every Evaluator endpoint and persist in the job folder and the record — the sandbox narrows, the proxy meters; it cannot make a tool-using agent trustworthy. Linux masks `held_out` at any depth under the jobs root; jobs are expected to live as siblings under one root. A path containing a `held_out` component inside the agent's own workspace is blocked by the same rule (fail-closed). On Linux the dotfile masks are fresh tmpfs mounts — empty and writable inside the namespace, ephemeral, never touching the host. Freeze refuses if the sandbox cannot launch on the host. The seat-code tree (`job/scripts/`) is pinned at freeze like the instruments — a script edited after freeze is refused at run start. Pricing entries with a zero, negative, non-numeric or non-finite (NaN/inf) rate are refused at freeze, and every model/agent seat must carry a non-empty model identifier (unpriced spend must never read as $0). Seat commands may reference only files inside the job dir (the interpreter in argv[0] excepted), a symlinked file or directory anywhere in the pinned tree is refused, and every python file under the job dir is pinned with the scripts tree so an imported helper cannot escape the pin; seat scripts' LOCAL dependencies are pinned too — Python seat scripts are AST-walked for transitively imported local modules, and non-Python seat scripts are scanned for referenced job files — but a helper invoked only indirectly by a wrapper in a way neither scan can see is a disclosed limit. A probe gate (`probes/run_probes.py`, wired into CI) replays the 2026-11-17b external recheck deterministically — loopback-only fake upstream, fake key, random canaries, no real LLM; unexpected failures close the gate, and the KNOWN-FAIL manifest names the open backlog (S1 read-allowlist) so green CI cannot hide it. The agent's model calls go through the loop-hosted proxy, which answers 403 to any model other than the declared `maker.model.identifier` (logged, never metered; the full proxy log accumulates under `rounds/` as `proxy-usage.jsonl`). Disclosed limits: the needle-based held-out filter is a tripwire for echoed text (32-character windows, case/whitespace-canonicalized) — paraphrases are not matched; held-out lines shorter than 32 characters are matched as WHOLE lines only (and lines shorter than 16 canonical characters are not matched at all — a shorter needle would scrub legitimate words); the sandbox is the exclusion mechanism; 

## Maker seats

Three Maker kinds in `job.json`:
- `"kind": "script"` — a command run in an isolated workspace (minimal env, no keys).
- `"kind": "model"` — a single chat call by the loop; keyed from the loop's environment.
- `"kind": "agent"` — a blocking tool-using agent child in its own confined workspace. The agent calls models **only** through a local proxy the loop spins up (`AGENT_PROXY_URL`): the key never enters the agent's environment, and every upstream call is metered toward the cost cap and logged (host, model, usage). The agent writes subject files into its workspace and exits; nonzero exit or no files halts as `needs_owner/maker_failed`. In the custody record an agent Maker is recorded as `kind: "model"` (the checker's enum) with its declared model identity. Evaluators remain script or single model calls by design — their simplicity is what makes them trustworthy. A model judge always receives the artifact inside a fresh per-call unguessable fence (`secrets.token_hex(12)`), and its system prompt states that the fenced text is the Maker's data, never instructions to the judge (P8).

## Starting from an existing draft

Set `seed` in `job.json` — `{"dir": "seed", "author": "Dana Voss", "origin": "Owner draft 2026-11-16"}` — where `seed/` contains exactly the `subjectFiles`. The draft is logged as `artifact-supplied` before the bar freezes, evaluated in round 1 like any candidate, and revised from there; a draft that already meets the bar finishes with zero Maker calls. The seed's author is recorded as the maker of record for that artifact and cannot serve as an Evaluator.

## Cost caps

Jobs with model seats must carry a `pricing` map in `job.json` (`{"model-id": {"input_usd_per_mtok": X, "output_usd_per_mtok": Y}}` — a `"default"` entry is accepted); `freeze` refuses the job otherwise, and a zero, negative or missing rate is refused too. Every model call — Maker, checks, and controls, plus the CL3 planted-case demonstrations at freeze — is metered at the point of billing into an append-only, hash-chained `job/usage.jsonl` (fsynced before the response is used — a crash or timeout cannot unbill a call the upstream billed; spend survives kills; `freeze --dry-run` makes NO paid demonstration calls and says so on stderr). `state.json` `cost_usd` is the derived sum, and the `max_cost_usd` cap fires BEFORE each paid call (`accrued + max(estimate, last call) >= cap`) — a trip mid-round halts `needs_owner/cap_reached` with no unbillable spend. Parallel agent calls reserve their worst-case spend (the request PLUS the configured `bounds.max_output_tokens` — a job whose model seats can produce unbounded output is refused at admission) under the meter locks BEFORE the proxy forwards them, and the reservation ledger (`usage.resv.jsonl`) is durable and job-scoped: two Meter instances share one balance, and a kill between the upstream bill and the usage row is recovered as spend at the next Meter construction; a stream: true request is refused by the proxy (400) because metering streamed responses is out of scope; an accounting failure latches the budget closed instead of reopening it. Residual, disclosed: a refused-but-reserved call is recovered conservatively (worst-case) if the process died before its outcome was recorded. Every state write anchors the usage log (row count + head digest + accrued total), so truncating its newest rows, or appending a suffix past the last state write, is refused at run, resume and finalize (finalize demands an anchored log and an exact row count); `finalize` records spend in the custody chain under the meter lock with a `spend` event pinning `usage.jsonl`'s bytes. The usage chain is verified before spend accrues onto an existing log. The two live examples carry $0 placeholder pricing, which freeze now refuses: as committed they cannot be re-run or re-frozen until real provider rates are supplied; their committed records remain verifiable.

## Incomplete runs

A run that halts without a Pareto-best artifact, or whose record assembly fails, still emits a record: a gap-listed document (`recordType: "edi-run/gap-record"`) whose `x-edi-run.gaps[]` names each unmet invariant from the architecture doc §1. It is invalid by design — `check.py` can never read it as a valid `custody/v1` statement; the disposition event is still logged in the hash-chained `job.jsonl`. Integrity failures are never downgraded: a tampered, truncated, or foreign custody log, modified frozen instruments, or a repeated `finalize` hard-fail with `GateError` and write nothing. `finalize` runs once per job.

## Interrupted runs

A HARD kill (`SIGKILL`, power loss) mid-round leaves `state.open_round` set with no named end state. `edi run` refuses such a job and names the remedy — recovery is never silent. `edi resume <job_dir>` re-verifies every pin and the usage chain, moves the partial round out of `rounds/` (immutable) into `job/partial/`, marks that round's meter rows discarded via an append-only amendment — a label only: the killed round's spend STAYS on the cap total, because discarding work does not unbill it (`Meter.discarded()` reports it separately) — and continues at the recorded boundary; the resumed round reuses the number and consumes its `max_rounds` slot. Spend re-accrues verbatim from `usage.jsonl` — a kill cannot erase billed work.

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
```
