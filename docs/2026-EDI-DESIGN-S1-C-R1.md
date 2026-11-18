# Design: S1 read-allowlist, C1–C7 metering/cap, R1 recovery

Status: proposed — implementation follows approval of the shape below.
Doctrine anchor: v0.2.1. Spirit: the custody chain never contains an opinion; spend
is owned; the sandbox narrows, the proxy meters; a refusal is a sentence, not silence.

## C1–C7 — spend is durable, metered at the point of billing

**Defects (probe-verified):** spend accrues only from a completed maker round's
self-report (C1/C2: crash and timeout lose the spend); the cap is checked at round
boundaries only (C3); not before each paid call (C4); evaluator runs after a failed
control are not counted (C5); a billed reply that cannot be parsed is not counted
(C6/C7).

**Design: one metering module, every billed call flows through it.**

- `metering.py`: `meter_call(who, model, url, body) -> (response, usage_row)`.
  Appends one row `{ts, who, model, endpoint, usage, cost_hint}` to
  **`job/usage.jsonl` (job root, next to `scores.jsonl`)** — NOT inside `rounds/`,
  which stays "immutable per-round snapshots" — **and fsyncs it (file and, on
  first append, the parent directory entry) before the response is used** — a row
  exists for every call whose upstream bill exists, regardless of what happens to
  the caller afterward. The kill of a seat cannot unbill a call the upstream
  billed. Residual, disclosed like every other limit: a crash between the
  upstream bill and the append still loses that call's row; the window is one
  append, not one round.
- The agent proxy becomes a consumer of the same module: each relayed call appends
  its row (it already logs; now it persists durably in the shared format).
- Authoritative spend = the usage log, not any seat's self-report. `loop` computes
  `cost_usd = Σ cost(rows)` at every state write (round end, maker crash, timeout,
  evaluator failure) — nothing a seat does can make the record under-count.
- **C4 — cap before each paid call:** `meter_call` refuses (raises
  `CapExceeded`) when `accrued + max(estimated_cost, last_call_cost) >=
  max_cost_usd`. Estimate = tokenized request × declared price; the price map is
  already mandatory (G2). There is no separate per-seat cap — `accrued` is the
  job total from the usage log; the cap simply fires wherever the next call
  would breach it, instead of only at round boundaries. Residual overshoot:
  `last_call_cost` as the floor means a call may exceed the cap by up to one
  prior call's cost; disclosed.
- **Mid-call state machine (named, not improvised):**
  - Maker-side trip (model or agent maker, before a call): the maker sees a
    metering refusal; the round ends and the run halts
    `needs_owner/cap_reached` — the same terminal as a boundary trip, wherever
    it fired. `cap_reached` is terminal at any point in the round; the arch §6
    "mid-batch cap → gap record" trigger is resolved as: an incomplete final
    round finalized with a disposition emits the gap-listed record (existing
    §6 semantics); a halted run itself is needs_owner, never a gap.
  - Evaluator-side trip mid-batch: checks already emitted in the batch stay;
    the remaining model checks of that batch do not run. A partially evaluated
    round never produces D4.9 feedback and never becomes incumbent — the run
    halts `needs_owner/cap_reached`; on `resume` (R1) the round restarts fresh
    (its meter rows are kept, its work is discarded and labeled
    `discarded_round: true` so a re-accrual cannot double-count).
- **C5/C6/C7:** model evaluators and model makers call `meter_call` too — with
  `who` distinguishing maker/evaluator/check/control. A parse failure after a
  billed reply still counts the row (the bill exists).
- Meter rows carry a hash chain (prev-row digest), like the record: a seat cannot
  edit its own spend history. The chain is verified at `resume` and at `finalize`
  (before spend enters the record) — a truncated or hand-edited `usage.jsonl`
  refuses instead of re-accruing. `usage.jsonl` is added to the agent sandbox deny
  list (spend telemetry must not leak into the seat that produced it).

**What does not change:** D4.9, the unidirectional pipeline, seats never see the
key, evaluator independence. The proxy remains the only thing that holds the key on
the agent path; model-maker/evaluator calls keep their direct URL but now meter.

## R1 — a hard kill is recoverable, never silent

Doc conflict to resolve alongside: arch I-01 says an interrupted run "is never
rerunnable"; this design amends I-01 — interrupted runs are rerunnable ONLY via
the explicit `edi.py resume`, which re-verifies every pin and re-accrues spend
from the verified usage log. Silence stays forbidden; the guard message names
the remedy.

**Defect (probe R1):** after a hard kill, the run-once guard refuses the next
invocation with an opaque error; there is no clean recovery path.

**Design: interrupted-state is a first-class state.**

- `state.json` gains `run_state: frozen | running | interrupted | terminal`.
  The loop sets `running` at start and writes `interrupted` in a SIGTERM/atexit
  path; a *hard* kill (SIGKILL, power loss) leaves `running` behind — which the
  next invocation detects via a start-token (pid + boot-unique id written at
  start; if the pid is dead and no terminal state, the run was hard-killed).
- Recovery is **explicit**: `edi.py resume <job>` — the only way past the guard
  for an interrupted job. Plain `run` on an interrupted job refuses with the
  remedy sentence: "interrupted by a hard kill; resume with edi.py resume".
- `resume` discards the partial round (moved OUT of `rounds/` — which stays
  immutable — to `job/partial/round-N/`), relabels that round's meter rows
  `discarded_round: true`, re-accrues spend from the durable usage log (C1–C7
  make this exact), re-verifies all pins (bar, method, instruments, scripts,
  seeds), and continues from the round boundary. A resume is a new round, not a
  continuation of killed work — consistent with "restarts are new jobs" at seat
  level, while the job's custody chain stays one chain.
- No opinion enters the record: the record notes `resumed_after_interrupt` with
  the accrued spend; the Architect's analysis stays in the skill layer (R2 ruling).

## S1 — default-deny reads for the agent seat

**Defect (probe S1, KNOWN-FAIL):** reads outside the deny list are default-allow;
a canary next to the jobs root is readable and exfiltrable through the artifact.

**Design: allowlist, not deny-list.** The agent sandbox flips the read posture:
deny `file-read*` by default; allow exactly what a seat needs:

- the workspace subtree (packet, artifact, scripts, held_out under the job);
- the interpreter and its standard library + site-packages (resolved from the
  actual `sys.base_prefix` at freeze — the probe already exercises the exact argv);
- `/dev/urandom`, `/etc/{resolv.conf,hosts,services}` (resolver), the proxy relay
  socket (already the only egress);
- macOS: seatbelt `allow file-read*` subscope list; Linux: replace `--ro-bind / /`
  with explicit `--ro-bind` entries for the above (bwrap becomes allowlist-shaped
  the same way).

**Rollout (the risk is breaking legitimate seats, not missing a leak):**
1. Implement behind `SANDBOX_READS=allowlist` (default remains deny-list) so the
   probe gate can run both postures side by side.
2. Probe additions: S1 flips to expected-PASS; new S7 (interpreter + stdlib
   readable), S8 (site-packages import of a real module works), S9 (host
   `/usr/share`, `/opt`, other users' homes denied).
3. Full suite + probe gate green on both postures → flip the default, remove the
   env switch in the same commit, close S1's KNOWN_FAILS entry.
4. README residual updated: the deny list becomes an allowlist; the in-artifact
   exfil disclosure stays (the proxy meters; it cannot make a tool-using agent
   trustworthy).

## Doc amendments required in the same change (docs stay true)

- Arch I-07: spend source of truth = `job/usage.jsonl`; `state.json` `cost_usd`
  becomes the derived sum; delete the crash/timeout-loss disclosed limit (it is
  fixed); proxy logging folds into `metering.py`.
- Arch §2 Maker bullet + README "Cost caps": cap fires before each paid call;
  `cap_reached` is terminal at any point in the round; overshoot residual named.
- Arch §5 layout: `usage.jsonl` at job root; `rounds/` contract unchanged;
  `partial/` added for R1.
- Arch I-01: "never rerunnable" → "rerunnable only via explicit `edi.py resume`".
- Arch I-07 reason-tag list gains the tags the code already emits
  (`below_floor`, `interrupted`, `runtime_error`, `integrity_error`).
- Arch §1 I-02: `gate_instruments` pins the scripts tree and every job-dir python
  file (G1, merged); pricing refusal covers zero/negative/non-finite rates and
  empty identifiers (G2, merged) — the doc must match `main`.

## Sequencing

1. `metering.py` + cap-before-call (C1–C7) — self-contained, probe-verifiable,
   no doctrine interaction beyond spend.
2. R1 recovery — depends on the durable usage log for exact re-accrual.
3. S1 allowlist — biggest blast radius, staged rollout as above.

Each step: red-first tests, probe-gate entries flip in the same commit, suite green
on all legs, frontier reviews per the shipping bar.
