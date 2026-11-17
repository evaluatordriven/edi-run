# 2026 E-DI Runtime Solution Architecture (`edi-run`)

**Scope:** Reference runtime engine implementing `evaluatordriven/doctrine` (v0.1, 2026-10-29 lift) and emitting conforming `evaluatordriven/custody` records (`custody/v1`, SPEC v0.9.8, predicate `https://evaluatordriven.com/custody/v1`).
**Baseline reuse (Wrap vs. Build):** `evaluatordriven/custody` already ships three zero-dependency Python 3.10+ stdlib tools (`tools/record.py`, `tools/make_record.py`, `checker/check.py`). The runtime **wraps** those three files verbatim for hash-chained event logging, in-toto statement assembly, and post-run verification—building only the loop state machine, seat-boundary gates, and adapter dispatch (~350 LOC Python stdlib).

---

## 1. Normative Invariant Map

Every rule in `DOCTRINE.md` and `SPEC.md` maps to a deterministic runtime gate executed by the Orchestrator before advancing state:

- **I-01 · Provenance & Bar Freeze Before Work**
  - *Seed artifacts:* a job may supply an existing draft via `seed` in `job.json` (`{dir, author, origin, note}`) holding exactly the `subjectFiles`. `freeze` verifies it and logs an `artifact-supplied` event (actor = the seed's author) **before** `bar-frozen` — the material demonstrably pre-dates the bar. The seed enters the loop as an unvalidated round-1 candidate, evaluated by the full panel like any other artifact; if it meets the bar the job ends `accepted` with zero Maker calls. The seed's author is never credited to the Maker seat: a seed-authored winner is recorded with `maker.name = seed.author, kind = "person"`, and the seed author may not serve as Evaluator (CL1-6) nor hold the Buyer or Specialist seat. Seed files are pinned by digest at freeze and verified at run start — an edited seed is refused before any evaluation. `freeze` runs every refusing check before the first log write: a refused freeze leaves no `job.jsonl` behind for the corrected retry. An interrupted run (`Ctrl-C`, SIGTERM, or any crash) is saved atomically with a named end state (`interrupted` / `runtime_error`) and is never rerunnable.
  - *Normative source:* `DOCTRINE.md` §2 (4 provenance facts), §4 Principle 1, §5 Entry A/B; `SPEC.md` `CL1-1`, `CL1-2`, `CL1-3`, `BL-1`.
  - *Enforced mechanism:* `gate_freeze(job)`. For Entry A (`levelClaimed` in `CL1..CL3`), verifies `bar.md` exists, hashes it, verifies Buyer and Specialist approvals match `bar.digest` (`CL1-3`), and appends `bar-frozen` and `approval` events to `job.jsonl` **before** emitting `work-started` (`bar.frozenAt < startedAt`). For Entry B (`baseline`), requires `readsBeforeFreeze[]` (`BL-1`) and records `work-started` prior to `bar-frozen`.

- **I-02 · Evaluator Qualification & Planted Controls Before Scoring**
  - *Normative source:* `DOCTRINE.md` §4 Principle 2 & 5, §6; `SPEC.md` `CL1-5`, `CL1-9`, `CL3-1`.
  - *Enforced mechanism:* `gate_instruments(job)` + batch control runner. Pins `instrument.rubric` and `instrument.runner` digests (`CL1-5`). For `CL3`, runs each evaluator against a held-out planted failing case before its first dimension check (`demonstrations[]`, `CL3-1`). In every evaluation batch (`B1..Bn`), injects at least one known-conforming (`K_clean`) and one known-failing (`K_planted`) control case with non-zero sha256 digests (`CL1-9`). If any control's actual outcome != expected outcome, the runtime automatically rewrites all checks by that evaluator in that batch (or on that dimension) to `unobserved` (`CL1-9`) and writes raw control scores to `scores.jsonl` so `make_record.py` derives control outcomes deterministically (`G2.3`).

- **I-03 · Seat Separation & Model-Family Independence**
  - *Normative source:* `DOCTRINE.md` §3 (Table of Seats, Model Families); `SPEC.md` `CL1-6`, `CL1-11`, `CL1-16`, `CL2-2`, `CL2-3`.
  - *Enforced mechanism:* `gate_seats(job)`. Case-folded identity check (`check.py:same()`) enforcing:
    1. `evaluator.name != maker.name` and `evaluator.name != bar.author` (`CL1-6`).
    2. For script evaluators, `script.author` is neither Maker nor Bar author (`CL1-6`), and `script.digest != instrument.runner` (`CL1-16`).
    3. `disposition.by` (Approver/Owner) is not in `evaluators[]` (`CL1-6`).
    4. If `levelClaimed` in `{CL2, CL3}`, runs the exact `CL2_3(predicate)` algorithm (`SPEC.md` §5.3 lines 01–44): Maker cannot be Buyer/Specialist/Bar-author; at least one model Evaluator differs from Maker in both `family` and `endpoint`; on every dimension, `2 * same_family_or_undisclosed <= total_evaluators_on_dimension`.

- **I-04 · Readiness Check & Held-Out Exclusion**
  - *Normative source:* `DOCTRINE.md` §3 ("The Architect designs; the Orchestrator routes... Held-out material"), §4 Principle 9, §5 step 5.
  - *Enforced mechanism:* `gate_packet(packet, held_out_manifest)`. Verifies the Architect's packet pins all required input digests and contains zero bytes or digests matching `held_out_manifest` (control cases, planted cases, expected answers, and raw Evaluator control logs). Scans outbound Maker prompt/context for held-out file paths and sha256 digests; fails closed if a leak is detected.

- **I-05 · Independent Observation, Multi-Run Spread & Non-Averaging**
  - *Normative source:* `DOCTRINE.md` §4 Principle 3, 5, 6, 7; `SPEC.md` `CL1-7`, `CL1-8`, `CL1-10`, `CL1-17`, `CL2-1`.
  - *Enforced mechanism:* `evaluate_batch(job, round_n)`.
    1. Executes cheap deterministic script evaluators before model evaluators (`DOCTRINE.md` §4 Principle 5).
    2. Runs each Evaluator `R >= 2` times per dimension for `CL2/CL3` (`CL2-1`), computing `spread[]` (`runs`, `agreement`).
    3. Computes check `outcome` strictly via `CL1-17` (`score >= threshold` -> `conforms`, else `does_not_conform`; `floor <= score <= max(anchors)`).
    4. Never averages across dimensions or across Evaluators: any dimension with a `does_not_conform` check is appended to `violations[]` (`CL1-11`); any dimension where checks return different outcomes is appended to `disagreements[]` with check indexes (`CL1-10`) and routed to an Adjudicator seat or Owner.

- **I-06 · Pareto Best-Artifact Carry-Forward (Mechanical Selection)**
  - *Normative source:* `DOCTRINE.md` §3, §4 Principle 8 & 9, §5 step 5.
  - *Enforced mechanism:* `select_incumbent(prev_best, candidate)`. A revision replaces the incumbent (`prev_best`) **if and only if** it is strictly better on >=1 dimension and worse on none (Pareto dominance on Evaluator dimension scores). If a revision trades one dimension against another, both versions are retained and the loop escalates to `needs_owner` (`DOCTRINE.md` §4 Principle 9). If `sha256(candidate) == sha256(prev_best)`, the influence channel is dormant (`DOCTRINE.md` §4 Principle 8) and the loop halts immediately as `needs_owner`.

- **I-07 · Bounded Loop, Named Terminal States & Verdict/Disposition Split**
  - *Normative source:* `DOCTRINE.md` §4 Principle 10 & 11, §5 ("The exits"); `SPEC.md` `CL1-13`, §5.6, §6.
  - *Enforced mechanism:* `step_decision(job)`. Enforces explicit `max_rounds`, `max_cost_usd`, and `max_wall_seconds` (no defaults permitted; missing bounds fail `gate_freeze`). Every run terminates in an explicit loop verdict (`accepted` | `needs_owner` | `rejected`) plus reason tag (`all_conform`, `census`, `no_observation`, `no_artifact`, `cap_reached`, `dormant_channel`, `dimension_tradeoff`, `maker_failed`). Separately records the human Approver's `disposition` (`given` | `not_yet_given` | `declined`, `acknowledgesUnobserved[]`) via a `disposition` event in `job.jsonl` without mutating any check or verdict (`CL1-13`).
  - *Cost accounting:* every model call — Maker rounds, Evaluator checks, and control cases — accrues token usage from the harness envelopes into `state.json` (`token_usage`, `cost_usd`). `freeze` refuses a job with model seats and no `pricing` map (`{model: {input_usd_per_mtok, output_usd_per_mtok}}` in `job.json`; a `default` entry is accepted; providers reporting only `total_tokens` are priced at the higher rate). The wall clock runs on `time.time()`, not `monotonic`, so suspended time counts; elapsed time is banked into `wall_spent_seconds` at every round boundary and `run_job` executes once per job. `state.json` writes are atomic (`os.replace`). Model endpoints must be `https` (localhost exempt); credentials are never sent over plain http.

---

## 2. System Topology & State Machine

### Component Topology (4 Modules + Wrapped Custody Tools)

```
+-------------------------------------------------------------------+
|                        edi.py (CLI Entrypoint)                    |
+-------------------------------------------------------------------+
         |                        |                        |
         v                        v                        v
+-----------------+    +--------------------+    +------------------+
|   gates.py      |    |      loop.py       |    |   adapters.py    |
| - gate_freeze   |    | - state machine    |    | - run_script()   |
| - gate_seats    |--->| - Pareto selector  |--->| - call_model()   |
| - gate_packet   |    | - dormancy detector|    | - capture_har()  |
| - gate_bounds   |    | - batch assembler  |    |   (for CL1-15)   |
+-----------------+    +--------------------+    +------------------+
         |                        |                        |
         +------------------------+------------------------+
                                  |
                                  v
+-------------------------------------------------------------------+
|          Wrapped Custody Reference Tools (Unmodified)             |
|  - custody/tools/record.py       (append-only sha256 JSONL log)   |
|  - custody/tools/make_record.py  (log + inputs -> custody/v1)     |
|  - custody/checker/check.py      (independent verification gate)  |
+-------------------------------------------------------------------+
```

### Seat Boundaries at Runtime
- **Owner / Approver:** Supplies `job.json` bounds, approves `bar.md` (with Buyer/Specialist), receives `needs_owner` escalations (`escalation.json`: deterministic options + recommendation derived from the reason tag), and issues final `disposition` (`given` / `not_yet_given` / `declined`).
- **Architect:** Authors `bar.md`, `rubric.md`, `method.md`, and `packet.json` prior to `bar-frozen`. Locked out of runtime state once `work-started` is logged.
- **Seed author (optional):** Supplies an existing draft (`seed` in `job.json`) before `bar-frozen`. A provenance seat only — no runtime authority; their artifact is judged like any candidate, credited as its maker of record, and they may not serve as Evaluator (CL1-6).
- **Orchestrator (`loop.py`):** Deterministic state machine. Zero LLM judgment. Assembles round packets from `(best_artifact, measured_defects, measured_workable_solutions) \ held_out_set`.
- **Maker (`adapters.py`):** Invoked in an isolated working directory containing only the round packet, with a minimal environment. The packet is built without `held_out/`, `scores.jsonl`, or `job.jsonl` (`held_out/` is locked to `0700`/`0600` at freeze) — but the sandbox is cwd-level, not OS-confined: a hostile script Maker running as the same user could still read absolute paths. OS-level confinement is deliberately out of scope (Minimalism QAR); the runtime's exclusion is by construction, not by confinement.
- **Evaluator (`adapters.py`):** Invoked with `readOnly: true` (`CL2-2`) against a content-addressed snapshot of the candidate artifact (`package-round-N.tar.gz`) plus interleaved `held_out/` control cases, with a minimal environment (no inherited API keys or tokens). Cannot write to the artifact directory.

### Deterministic State Machine

```
[INIT]
  |-- gate_freeze() + gate_seats() + gate_bounds()
  |-- (CL3) run planted failure demonstrations -> log before work-started
  v
[FROZEN] --(log: bar-frozen, approval, work-started)--> [MAKE_ROUND_n]
  |
  v
[MAKE_ROUND_n]
  |-- gate_packet() ( enforces best-artifact handoff + held-out exclusion )
  |-- if seed job and n == 1: copy seed files -> log: artifact-produced (actor = seed author, data.seed)
  |     (no Maker call; the seed is evaluated like any candidate)
  |-- else: invoke Maker -> log: artifact-produced (or halt: no_artifact -> NEEDS_OWNER)
  |-- if sha256(artifact_n) == sha256(artifact_n-1) -> halt: dormant_channel -> NEEDS_OWNER
  v
[OBSERVE_AND_JUDGE_n]
  |-- run script evaluators + model evaluators (R runs) alongside K_clean & K_planted controls
  |-- write raw control scores to scores.jsonl; log: check, control, external
  |-- if control failed -> downgrade evaluator's batch checks to 'unobserved' (CL1-9)
  v
[DECIDE_STEP_n]
  |-- update incumbent via Pareto dominance (better on >=1 dim, worse on 0)
  |-- if Pareto trade-off across dimensions -> [TERMINAL: NEEDS_OWNER]
  |-- if all dimensions 'conforms' -> [TERMINAL: ACCEPTED]
  |-- if any dimension < scale.floor (reject floor) -> [TERMINAL: REJECTED]
  |-- if round == max_rounds or cost >= max_cost or time >= max_time -> [TERMINAL: NEEDS_OWNER]
  |-- else -> build Round n+1 packet from incumbent -> [MAKE_ROUND_n+1]
  v
[DISPOSITION_AND_EMIT]
  |-- record Owner disposition event ('given' | 'not_yet_given' | 'declined')
  |-- invoke make_record.py inputs.json record.json --log job.jsonl
  |-- invoke checker/check.py record.json --files ... --bar ... --evidence ...
```

---

## 3. Quality Attribute Trade-off Analysis

### Scenario Scorecard & Enforced Mechanisms
- **Traceability**
  - *Stimulus:* An auditor or Buyer receives `record.json`, the artifact directory, `bar.md`, and `evidence/` after job completion.
  - *Response:* `checker/check.py` verifies `CR-1..2`, `CL1-1..17`, `CL2-1..3` with exit code `0 (OK)`, and `record.py verify job.jsonl --files` confirms unbroken sha256 event chaining from `job-start` to `disposition`.
  - *Trade-off:* Every intermediate round artifact (`package-candidate.tar.gz`), harness log (`CL1-15`), and per-check reading (`CL1-8`) is written to disk and hashed synchronously. This trades small disk I/O (~1–5 ms per event) for 100% offline reconstructability (`DOCTRINE.md` §8 F6).
- **Consistency**
  - *Stimulus:* A misconfigured job attempts to use the same model endpoint for Maker and sole Evaluator at `CL2`, or a Maker revision regresses Dimension 1 while improving Dimension 2.
  - *Response:* `gate_seats()` aborts before `work-started` using `CL2_3`; `select_incumbent()` refuses to overwrite the incumbent on a dimension trade-off and transitions deterministically to `needs_owner`.
  - *Trade-off:* Strict fail-closed validation sacrifices "best-effort continuation" when an API endpoint routes to a fallback model in the Maker's family (caught via harness response headers for `CL1-15` / `check.endpoint`).
- **Efficiency**
  - *Stimulus:* A 3-round loop with 2 script evaluators and 2 model evaluators (`CL2`, 2 runs each).
  - *Response:* All gate checks, hashing, JSONL appends, and `check.py` verification execute in `<35 ms` total CPU time. Furthermore, cheap `script` evaluators run *before* `model` evaluators in each batch: if a deterministic script check breaches the reject floor or fails a structural prerequisite, the runtime skips paid model Evaluator calls for that broken draft (logging model dimensions as `unobserved` with reason `"short-circuited by script failure"` on intermediate rounds, while running the full panel on the final/incumbent artifact so `CL1-7` holds on the emitted record).
  - *Trade-off:* Short-circuiting intermediate rounds saves 40–70% of Evaluator token spend (`DOCTRINE.md` §8 F2) at the cost of deferring prose/semantic feedback until structural checks pass.
- **Minimalism**
  - *Stimulus:* Inspecting the repository dependency tree, process table, and module graph.
  - *Response:* `0` third-party runtime packages (`pyproject.toml` / `requirements.txt` empty; Python 3.10+ stdlib only: `hashlib`, `json`, `pathlib`, `subprocess`, `urllib.request`, `datetime`, `argparse`), `1` OS process per run, `4` files (`edi.py`, `gates.py`, `loop.py`, `adapters.py`) totaling `<400 LOC`, delegating all record construction and verification to the existing `custody` reference tools.
  - *Trade-off:* No concurrent distributed worker pool; rounds execute sequentially in a single process, which matches `DOCTRINE.md` §8 (*"What is claimed is repeatability, not scale"*).

### Sensitivity Points & Risks
- **Sensitivity Point 1 (`make_record.py` timestamp filtering):** `make_record.py` (line 342) includes only `check` and `control` events logged *after* the last `artifact-produced` event. **Architectural rule:** When Round `n` fails to beat Round `n-1` (incumbent carried forward), the runtime must either re-log the incumbent's snapshot as the terminal `artifact-produced` followed by its winning evaluation batch, or keep per-round logs and promote the incumbent round's slice at finalization so `make_record.py --log` binds checks to the winning artifact's timestamp and digest.
- **Sensitivity Point 2 (`CL1-15` External Harness Evidence):** `CL1-15` requires `external[]` evidence of model Evaluator configuration read by a party `!= maker.name`. **Architectural rule:** `adapters.py` writes raw HTTP response envelopes (recording actual answered `model` and `endpoint` host) to `evidence/harness-<eval_id>-<batch>.json`, and an independent script/auditor actor logs the `external` event after verifying the envelope.

---

## 4. Deletion Log (Minimalism Proof)

Every component below was evaluated and deleted because plain filesystem and stdlib primitives satisfy the normative requirements with smaller surface area:

- **Deleted: SQLite / Postgres / State Database**
  - *Why plain primitives suffice:* `custody/tools/record.py` already defines an append-only, sha256 hash-chained JSONL file (`job.jsonl`) as the canonical event store. Adding a database introduces dual-write drift between DB state and the custody log. `job.jsonl` + atomic file renames (`os.replace`) provide crash-safe state recovery in 0 dependencies.
- **Deleted: Third-Party `jsonschema` Package**
  - *Why plain primitives suffice:* `SPEC.md` §4 line 146 states that where `custody-v1.schema.json` and `SPEC.md` differ, `SPEC.md` governs, and `custody/checker/check.py` already implements 100% of `CR-1`, `CR-2` (structural schema validation), and `CL1..CL3` (semantic validation) in pure Python stdlib. Calling `check.py` directly eliminates the `jsonschema` + `referencing` + `rpds-py` C-extension dependency chain.
- **Deleted: LLM SDKs (`openai`, `anthropic`, `litellm`, `pydantic`)**
  - *Why plain primitives suffice:* A single 45-line `urllib.request` JSON transport function in `adapters.py` (plus `subprocess.run` for CLI/local-model Makers and script Evaluators) handles chat-completions HTTP calls AND captures the exact raw wire response and answering host needed for `CL1-15` and `CL2-3`—which SDK wrappers obscure.
- **Deleted: Background Daemon / Message Queue / Orchestrator Server**
  - *Why plain primitives suffice:* An E-DI job is a bounded loop (`max_rounds`, `max_cost_usd`, `max_wall_seconds`). Running as a foreground CLI invocation (`edi run <job_dir>`) that can pause at `needs_owner` (persisting state in `job.jsonl`) and resume via `edi finalize <job_dir>` requires zero background processes.
- **Deleted: Custom Custody Serializer**
  - *Why plain primitives suffice:* Re-implementing custody record assembly violates Wrap-vs-Build and risks subtle `G2.3` / `CL1-9` control-derivation bugs. Calling `custody/tools/make_record.py` directly guarantees bit-for-bit compatibility with the reference checker.

---

## 5. Interface & Storage Contracts

### CLI Entrypoints (`edi.py`)

```sh
# 1. Freeze bar, verify seat separation & bounds, qualify evaluators, and start job.jsonl
python3 edi.py freeze <job_dir>

# 2. Execute bounded Maker <-> Evaluator loop up to terminal state (accepted | needs_owner | rejected)
python3 edi.py run <job_dir>

# 3. Record Owner/Approver disposition, emit custody/v1 record.json, and run checker/check.py
python3 edi.py finalize <job_dir> --disposition given|not_yet_given|declined --by "<Owner Name>" [--ack-unobserved D1,D2]

# 4. Standalone verification wrapper (invokes record.py verify --files && checker/check.py)
python3 edi.py verify <job_dir>
```

**Exit codes (matching `checker/check.py` contract):**
- `0`: Gate / verification `OK` (or loop reached `accepted`).
- `1`: Deterministic violation / `FAILED` (or loop reached `rejected`).
- `2`: `NOT-CHECKABLE` / halted at `needs_owner` awaiting Owner disposition.

### File-Based Job Workspace Layout

```
<job_dir>/
├── job.json                  # Architect's job config: bounds, seats, levelClaimed, optional seed, optional pricing, adapter commands
├── inputs.json               # Base template for custody/tools/make_record.py
├── bar.md                    # Frozen bar document (hashed at freeze)
├── rubric.md                 # Frozen evaluator rubric (instrument.rubric)
├── method.md                 # Frozen maker instructions (maker.method)
├── seed/                     # Optional Owner-supplied draft; hashed at freeze, logged as artifact-supplied
├── held_out/                 # Modes 0700 to Orchestrator/Evaluator only; NEVER passed to Maker
│   ├── controls/             # K_clean and K_planted files per dimension
│   └── planted/              # CL3 pre-run planted failure cases
├── rounds/                   # Immutable per-round snapshots
│   └── round-01/
│       ├── packet.json       # Filtered input packet delivered to Maker (verified by gate_packet)
│       ├── artifact/         # Maker output files for Round 1
│       └── package-r01.tar.gz
├── artifact/                 # Symlink or atomic copy of the current Pareto-best artifact (subject)
├── evidence/                 # Cited evidence files for CL1-8 and CL1-15
│   ├── reading-D1-E1-r1.json # Structured Evaluator verdict + quoted observation
│   └── harness-E1-B1.json    # Raw transport log verified by non-Maker for CL1-15
├── scores.jsonl              # Raw control score rows consumed by make_record.py (G2.3)
├── job.jsonl                 # Hash-chained custody log managed via custody/tools/record.py
└── record.json               # Final emitted in-toto Statement (custody/v1)
```

### Custody-Record Emission Boundary

1. **During Execution (`freeze` and `run`):** `loop.py` never writes to `record.json` directly. Every state transition calls `record.append(job_jsonl, ...)` with canonical event types (`bar-frozen`, `approval`, `work-started`, `artifact-produced`, `check`, `control`, `external`, `disposition`) and exact file sha256 digests.
2. **At Incumbent Promotion:** Whenever the loop finishes (whether Round `k` won or an earlier Round `m < k` remained the Pareto-best incumbent), the Orchestrator materializes the winning round's artifact into `<job_dir>/artifact/`, records its authoritative `artifact-produced` and winning batch `check` / `control` events in `job.jsonl`, and writes `scores.jsonl`.
3. **At Finalization (`finalize`):**
   - Appends the human Approver's `disposition` event to `job.jsonl`.
   - Calls `make_record.from_log(inputs_data, "job.jsonl")` to compile `<job_dir>/record.json`.
   - Executes `checker/check.py record.json --files artifact --bar bar.md --evidence evidence` and refuses to mark the run finalized unless `check.py` exits `0` (or `2` when `disposition.state == "not_yet_given"` routes `to approver`).

---

## 6. Design Decision: Incomplete-Run Record Emission (Fail-Stop vs. Gap-Emission)

**Decision.** When a run halts or aborts before a complete, checker-passable record can be assembled (missing checks, missing disposition, cap reached mid-batch, crash), the runtime **emits a gap-listed record rather than emitting nothing**. The emitted `record.json` is marked invalid-by-design via an `x-edi-run.gaps[]` extension enumerating every unmet requirement, and `finalize` exits `1`. A run with zero gaps behaves exactly as specified in §5: full `make_record.py` assembly and a hard `check.py` gate.

**Normative basis.**
- *For emission:* `DOCTRINE.md` §0 — "the record of the work can be checked by anyone who needs to audit it," and §8 F6 (offline reconstructability). A silent fail-stop produces **no** artifact for a rejected or crashed job: the most failure-prone runs are precisely the ones an auditor cannot inspect. The doctrine's custody log (`job.jsonl`) already contains every event up to the halt; refusing to compile it discards evidence that already exists.
- *For marking:* `SPEC.md` §4 — SPEC governs over the JSON Schema, and the checker must never read a gap-listed record as valid. The `x-edi-run.gaps` extension makes incompleteness machine-readable, so `check.py` exits `1` (or `2`) on every gap-listed record and no downstream consumer can mistake it for a passing record. This mirrors `CL3-3`'s `NOT-CHECKABLE` posture: an unverifiable claim is labeled, not suppressed.

**Enforced mechanism.** `finalize` first verifies the custody log chain (`record.py verify`, files included) and refuses — hard `GateError`, no record written — on any integrity failure (tampered event, broken chain, altered evidence digest). Integrity violations are never converted into gap records. If the log is intact but the run is incomplete (no incumbent artifact), it appends the disposition event and invokes a thin gap-emitter that writes the gap-listed document plus `x-edi-run.gaps[]` (invariant id from §1, or `custody-assembly` with the assembler's error detail for shape/schema failures only). `finalize` runs once per job: a second call is refused (`GateError`) because the log already carries a `disposition` event.

**Where the invalidity guarantee lives.** The gap-listed document carries no custody `predicate`, so `check.py` cannot read it as a valid custody/v1 statement — the guarantee is the emitter's *construction*, not a rule inside the vendored checker (which has no knowledge of `x-edi-run.gaps`). `check.py` still runs at emission time as a tripwire: if a gap-listed document ever verifies OK, the emitter raises (emitter defect, fail-stop). The gap list is derived deterministically from the same invariant map in §1. The emitter is the *only* new code this decision adds (~40 LOC); it wraps, never re-implements, the custody tools (Wrap-vs-Build, §0 baseline).

**Rejected alternative — pure fail-stop** (`make_record.py` aborts, no `record.json`): honors the assembler's validity contract but breaks auditability for failed runs and converts a diagnosable halt into an undocumented one. Rejected alternative — **silent gap-filling**: emitting a structurally valid record with absent dimensions omitted or defaulted. This is prohibited outright; it would let a record pass `check.py` while hiding missing evaluation, violating `CL1-11`/`CL1-13`.

**Trade-off.** The gap-emitter is a second code path into record construction — a real consistency risk, mitigated by (a) deterministic derivation from §1's invariant map, (b) `check.py` as the sole arbiter of validity on every emitted record, and (c) the extension field making the invalid state explicit rather than inferable. Accepted because the alternative (no record on failure) costs more traceability than the second path costs consistency.

**Adopted:** 2026-11-17 by S2 (Owner), following the spirit review of the Plumbline→edi-run swap.
