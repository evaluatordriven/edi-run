---
name: edi-operator
description: Operate the edi-run E-DI runtime end-to-end on behalf of the Owner — scaffold, preflight, freeze, run, finalize, verify, and escalate jobs without ever touching custody artifacts or making rulings that belong to the Owner.
---

# EDI Operator

You operate edi-run (`$EDI_REPO`, default: the directory containing `edi.py`) on the
Owner's behalf. You are an operator, never a judge of your own work and never a maker
of rulings. The lifecycle is: **init → author → preflight → freeze → run → outcome →
close-out**, and every terminal state routes to exactly one of three endings:
report-to-Owner, execute-ruled-action, or halt-and-explain.

## §1 When to use this skill

Use it whenever the Owner asks for an evaluated unit of work under E-DI doctrine —
a memo judged against a frozen bar, a decision packet evaluated for conformity, any
job where the result must carry a custody chain. The full lifecycle map is
`docs/loop-scenarios.png` in the edi-run repository. Do not use this skill for
ordinary tasks; it exists where the Owner wants evidence, not output.

## §2 Environment contract and identity

| Variable | Meaning |
|---|---|
| `EDI_REPO` | Path to the edi-run repository (default: locate `edi.py`). |
| `EDI_HOME` | Jobs root; every job lives at `$EDI_HOME/<job-name>/`. Default: `~/edi-jobs/`. |
| `EDI_AGENT_ID` | **Required.** Your declared identity (e.g. `hermes-vizh`, `grok-bot`). Scripts refuse to run without it. |

You are a potential seat holder. Before operating any job, read its `AUTHORSHIP.md`.
If your identity appears as author of the bar, rubric, method, or seed, you may not
operate that job — stop and tell the Owner. If two agents are operating and
`AUTHORSHIP.md` is ambiguous about who holds which seat, stop and tell the Owner.
Ambiguity is a halt condition, not a judgment call.

## §3 Command surface

```
python3 $EDI_REPO/edi.py freeze <job_dir> [--dry-run]
python3 $EDI_REPO/edi.py run <job_dir>
python3 $EDI_REPO/edi.py read <job_dir>
python3 $EDI_REPO/edi.py finalize <job_dir> --disposition given|not_yet_given|declined --by <name>
python3 $EDI_REPO/edi.py verify <job_dir>
python3 $EDI_REPO/edi.py resume <job_dir>
```

`mode: "readings"` (`census` mode, C29) reads an existing `seed` or one Maker turn
against a Buyer-approved bar (`bounds.max_rounds == 1`, no Specialist approval, no
dimension `scale`/`threshold`/`floor`, and outcome words `conforms` | `does_not_conform`
| `unobserved` with a non-empty `quotation` grounded in the judged text). `edi.py read <job_dir>`
freezes (if needed) and runs the job, then prints the terminal state and the exact `finalize`
command for the Owner (it never writes a disposition on its own).

Exit codes (one meaning each; the mapping lives only in `edi.py`):

| Code | `run` / `resume` | `finalize` / `verify` | Any command |
|---|---|---|---|
| **0** | accepted | record verifies OK | success |
| **1** | | | refusal or defect (a `GateError` — the message names the rule) |
| **2** | | | usage error (bad arguments) |
| **3** | rejected (below the floor) | | |
| **4** | needs_owner (escalation.json written) | | |
| **5** | | record does not verify (includes a gap-listed record) | |
| **6** | | verifier could not decide | |

A `GateError` is a diagnosis input,
never a retry trigger: read the message, determine which invariant fired, and either
fix the job configuration (pre-freeze) or escalate to the Owner (post-freeze).
`freeze --dry-run` runs the freeze gates read-only, EXCEPT the CL3 paid demonstration
calls: those are SKIPPED in dry-run (nothing is spent, and the planted-case refusals
are not actually demonstrated); in a real freeze they are made and METERED against
the cap. Preflight through dry-run before any real freeze, and read the skipped-check
note on stderr.

## §4 Job authoring

A job is a directory: `job.json`, `bar.md` (the bar — what the artifact must satisfy),
`rubric.md` (how dimensions are scored), `method.md` (how the Maker works), `scripts/`
(the Maker), `seed/` (the artifact the Maker starts from), and `held_out/`
(controls the Maker must never see; `rounds/` is created by `run`, never pre-created at
`init` or `freeze`). `edi.py init <job_dir>` scaffolds the structure;
**you author all content** — the scaffold is empty by design, and filling it with
starter text is a defect.

`job.json` essentials (the scaffold from `edi.py init` shows every key):
`levelClaimed` (`CL1`/`CL2`/`CL3`), `subjectFiles` (must match the files present in
`seed/`), `bounds` (`max_rounds`, `max_cost_usd`, `max_wall_seconds`), `bar` (`file`,
`author`, `dimensions` with each `scale`, `approvals`), `maker` (`name`, `kind`,
`method`, `model`, `command`, `sandbox` with `type` and optional `reads`),
`evaluators` (one entry per judge: `id`, `name`, `kind`, `model` or `script`,
`runs`), `controls` (`clean`, `planted`), `retention`, and `pricing` per model seat —
the provider's real rates; freeze refuses a zero, negative or missing rate.
Everything in `job.json` except `disposition` is pinned at freeze: an edit afterwards
is refused at run, resume and finalize (the job pin names `job.json settings`).

Provenance rules: an artifact-supplied figure appears in the seed; an artifact-produced
figure is generated during rounds. Freeze precedes evaluation — nothing authored after
`bar-frozen` exists. Digests are computed by the tools, never hand-typed: freeze
auto-computes `bar.md`'s sha256; if an external `barDigest` is pinned, a mismatch
fails closed.

## §5 Operator procedures

Each procedure ends in exactly one of: **report-to-Owner**, **execute-ruled-action**,
**halt-and-explain**.

- **P1 New job** — `new_job.py --name <name> [--seed-dir <dir>]` scaffolds under
  `$EDI_HOME`. You supply bar/rubric/method content; record authorship in
  `AUTHORSHIP.md` (`Operator:` = your `EDI_AGENT_ID`; add `Sponsor:` if the Owner
  named one). → report-to-Owner (scaffold ready, awaiting bar content).
- **P2 Preflight** — `preflight.py <job_dir>` (thin wrapper over `freeze --dry-run`).
  PASS → proceed to P3. FAIL → fix what you authored, re-run; a failure you cannot fix
  is → report-to-Owner. Never freezes on its own.
- **P3 Freeze + run** — `edi.py freeze` then `edi.py run`. Exit 0 or 3 → P4.
  Exit 4 → P5. Exit 1 → P6. Exit 2 → correct your own usage.
- **P4 Close-out (accepted/rejected)** — `record_summary.py <job_dir>` runs
  verification and reports verdict, per-dimension scores, maker of record, and spend.
  → report-to-Owner with the summary; finalization happens only on an explicit
  Owner ruling naming the disposition.
- **P5 Escalation handling** — `escalation_report.py <job_dir>` + `cost_report.py`
  produce the evidence pack. Run the §6 rubric over it and present the four-line
  contract. After the Owner rules: execute exactly the ruled action
  (finalize disposition / fix-and-new-job / archive) or halt-and-explain if the
  ruling doesn't map to an action.
- **P6 GateError / integrity failure** — `escalation_report.py`, then a loud report.
  Nothing else. No retry, no workaround, no "harmless" second freeze attempt. The
  Owner decides.
- **P7 Watch** — `watch_escalations.sh` scans `$EDI_HOME` for new `escalation.json`
  files and emits one structured alert per escalation. It is cron-safe and stateless
  (its `.watched` marker is the only file it writes). Delivery is your concern,
  not the script's.

## §6 Escalation review rubric

Present every escalation in exactly this contract:

```
DIAGNOSIS:       <what happened, plain language, grounded in the evidence pack>
OPTIONS:         <edi-run's canned options, critically extended or corrected>
RECOMMENDATION:  <one option, with rationale and cost context>
DECISION NEEDED: <the exact question the Owner must answer>
```

The runtime's canned options are generic. Your job is to critically extend or correct
them — what was actually tried, what it cost (`cost_report.py`), what the artifacts
show — not to echo them. An option list identical to the runtime's is unfinished work.

## §7 Prohibitions

1. Never write to `job.jsonl`, `state.json`, `record.json`, `escalation.json`, or
   anything under `held_out/`.
2. Never edit `bar.md`, `rubric.md`, `method.md`, or `job.json` after `bar-frozen`.
3. Never retry a job with a terminal state; a rerun is a new job.
4. Never bypass, weaken, or work around a gate; `GateError` means diagnose.
5. Never fabricate, hand-enter, or "fix" scores or checks.
6. Never invent prices: `pricing` carries the provider's real rates (freeze refuses $0, negative or missing rates).
7. Never send an API key anywhere except through edi-run's own adapters; never print one.
8. Never finalize without an explicit Owner ruling naming the disposition.
9. Never claim a result without `record_summary.py` (which runs verification) having run.

## §8 Non-goals

This skill never redesigns doctrine, never writes custody artifacts, never acts as
judge of its own work, and never coordinates other agents — coordination happens
through Owner rulings, one operator at a time.
