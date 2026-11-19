# EDI Operator Skill — Specification

Status: SPEC (design approved by S2 2026-11-17; implementation pending)
Governs: `skills/edi-operator/` in this repository
Audience: any capable agent (Hermes Agent, Grok Bot, Claude CLI, …) operating edi-run **on behalf of the Owner**

---

## 0. Purpose

One skill, agent-agnostic, that teaches an agent to operate edi-run end-to-end on the
Owner's behalf: scaffold jobs, preflight them, freeze/run/finalize/verify, interpret
outcomes, produce escalation reviews, and carry the Owner's rulings into action —
while keeping every doctrine invariant intact.

Two design laws, inherited from the runtime:

- **Deterministic work → scripts. Agent judgment → SKILL.md prose.** Everything a
  script can compute must be a script; the skill text only tells the agent how to
  reason over script output and how to behave toward the Owner.
- **The custody chain never contains an opinion.** The skill and its scripts are
  read-only with respect to every custody artifact (`job.jsonl`, `state.json`,
  `record.json`, `held_out/`). Only edi-run writes those.

## 1. Environment contract

| Convention | Meaning |
|---|---|
| `EDI_REPO` | Path to this repository (default: locate `edi.py`). |
| `EDI_HOME` | Jobs root directory; every job lives at `$EDI_HOME/<job-name>/`. |
| `EDI_AGENT_ID` | The operating agent's declared identity (e.g. `hermes-vizh`, `grok-bot`). **Required.** Scripts refuse to run without it. |
| `EDI_NOTIFY` | Optional path/socket for the watcher's alert payload; delivery is agent-specific. |

Portability rule: scripts are Python 3 stdlib + POSIX shell only. No Hermes, no
third-party packages, no agent-specific tool calls inside `scripts/`. An agent needs
exactly two capabilities: read/write files, run shell commands.

## 2. Identity and seat separation (the load-bearing rule)

Seat separation extends to **agents**. The operating agent is a potential seat holder:

- If `EDI_AGENT_ID` authored the `bar.md`/`rubric.md`/`method.md` of a job, that agent
  may not operate (or act as) the Maker or an Evaluator for that job (CL1-6 extended).
- Symmetrically, the author of `bar.md`/`rubric.md`/`method.md` may never sit as the
  Maker or an Evaluator — an author judging with (or making against) their own
  instrument is self-endorsement — and the seed's author may not sit as an Evaluator.
- Agent authorship is recorded in the job's `AUTHORSHIP.md` (skill-created,
  Owner-visible, outside the custody chain): who authored bar, rubric, method, seed,
  and which agent is operating.
- `preflight.py` refuses a job whose declared author set collides with its seat set;
  `gate_authorship` enforces the same refusal at freeze.
- Two agents operating concurrently must not both act as Maker/Evaluator in one job;
  the skill instructs the agent to check `AUTHORSHIP.md` and stop on ambiguity.

## 3. Hard prohibitions (skill MUST state; agent MUST obey)

1. Never write to `job.jsonl`, `state.json`, `record.json`, `escalation.json`, or
   anything under `held_out/`.
2. Never edit `bar.md`, `rubric.md`, `method.md`, or `job.json` after `bar-frozen`.
3. Never retry a job that has a terminal state (`run-once` guard); a rerun is a new job.
4. Never bypass, weaken, or "work around" a gate; a `GateError` means diagnose, not retry.
5. Never fabricate, hand-enter, or "fix" scores or checks.
6. Never run a job whose `pricing` is missing or a `$0` placeholder without telling the
   Owner first (the runtime refuses missing pricing; `$0` placeholders pass but lie).
7. Never send any API key anywhere except via edi-run's own adapters; never print a key.
8. Never finalize without an explicit Owner ruling for the disposition.
9. Never claim a job result without `verify_job` (or the record's own verification) having run.

## 4. SKILL.md structure (the prose half)

| § | Content |
|---|---|
| 1 | When to use this skill; the operator lifecycle diagram reference (`docs/loop-scenarios.png`). |
| 2 | Environment contract (§1) and identity declaration (§2). |
| 3 | Command surface: `edi.py freeze/run/finalize/verify`; exit codes 0/1/2; `GateError` semantics. |
| 4 | Job authoring guide: `job.json` fields (bounds, seats, level, seed, pricing), `bar.md`, `rubric.md`, `method.md`, seed/ and held_out/ conventions; provenance rules (artifact-supplied vs artifact-produced; freeze precedes evaluation). |
| 5 | Operator procedures (see §5 below), each ending in exactly one of: report-to-Owner, execute-ruled-action, or halt-and-explain. |
| 6 | Escalation review rubric: fixed output contract **Diagnosis / Options / Recommendation / Decision needed**, plus cost context (`cost_report.py`) and the rule that edi-run's canned options are generic and must be critically extended, not echoed. |
| 7 | Prohibitions (§3, restated as behavior). |
| 8 | Non-goals: the skill never redesigns doctrine, never writes custody artifacts, never acts as Judge of its own work. |

## 5. Operator procedures (each = script(s) + decision points)

| # | Procedure | Scripts | Outcome |
|---|---|---|---|
| P1 | New job | `new_job.py` | Scaffolded container + skeleton `job.json`; Owner supplies bar/rubric/method content. |
| P2 | Preflight | `preflight.py` | PASS → freeze; FAIL → fix or report. Never freezes on its own. |
| P3 | Freeze + run | `edi.py` via skill | Report exit code semantics; on 2 → P5; on GateError → P6. |
| P4 | Accepted/rejected close-out | `record_summary.py` | Verify record, summarize verdict + per-dimension scores + spend, file to Owner. |
| P5 | Escalation handling | `escalation_report.py` + `cost_report.py` | Evidence pack → skill rubric (§6) → Owner review → apply ruling (finalize disposition / fix-and-new-job / archive). |
| P6 | GateError / integrity failure | `escalation_report.py` | Loud report; **no** action beyond diagnosis; Owner decides. |
| P7 | Watch | `watch_escalations.sh` | Cron-able; emits structured alert payload; delivery is the hosting agent's concern. |

## 6. Helper scripts (the deterministic half)

All in `skills/edi-operator/scripts/`. Common conventions: read-only over custody
artifacts; JSON on stdout; exit 0 = success, 1 = refusal/defect, 2 = usage error;
never prompt; never print secrets.

| Script | Inputs | Outputs | Notes |
|---|---|---|---|
| `new_job.py` | `--name`, `EDI_HOME`, optional `--seed-dir`, `--author` | Scaffolded job dir; skeleton `job.json` with field comments | Calls `edi.py init`; creates `AUTHORSHIP.md` skeleton. Never writes bar/controls content. |
| `preflight.py` | job dir, `EDI_AGENT_ID` | JSON: `{pass, violations[], warnings[]}` | Mirrors freeze gates read-only: pricing per model seat, seed files == subjectFiles, seat-name collisions (evaluators vs bar author vs seed author vs agent), bounds sanity. `$0`-placeholder pricing is refused outright by the runtime (G2) and surfaces as a violation. |
| `escalation_report.py` | job dir | JSON evidence pack: escalation reason+options, terminal state, rounds, last 10 log events, error fields, artifact digests | The single input to the §6 review rubric. |
| `cost_report.py` | job dir | JSON: `{spent_usd, cap_usd, per_model_tokens[], wall_seconds}` | From `state.json` only. |
| `record_summary.py` | job dir | JSON: `{verified, verdict, disposition, scores{dim: score}, maker_of_record, gaps[]}` | Runs `verify_job` internally; reports its exit code verbatim. |
| `watch_escalations.sh` | `EDI_HOME` | One alert line per new `escalation.json` (path + reason + recommendation) | Stateless scan; safe under cron; dedupes via a `.watched` marker file it owns (the only file it writes, outside job dirs). |

## 7. Escalation review output contract (fixed)

```
DIAGNOSIS:    <what happened, plain language, grounded in the evidence pack>
OPTIONS:      <edi-run's canned options, critically extended or corrected>
RECOMMENDATION: <one option, with rationale and cost context>
DECISION NEEDED: <the exact question the Owner must answer>
```

The agent presents this through its own channel; it never finalizes, re-runs, or
deletes anything without the Owner's explicit ruling naming the action.

## 8. Distribution

- Source of truth: this repo, `skills/edi-operator/`.
- Hosting agents install a thin pointer (SKILL.md that loads the repo copy) — the same
  vendoring pattern as `vendor/`.
- Version the skill with the repo: the skill is valid for the edi-run version it ships
  with; a runtime change that alters exit codes or event names must update the skill
  in the same PR.

## 9. Non-goals

- No multi-agent orchestration inside the skill (each agent operates independently;
  coordination happens through Owner rulings).
- No agent-as-Maker harness beyond what `kind: "agent"` already provides.
- No notification delivery, dashboards, or GUIs.
- No changes to doctrine, custody tools, or the loop.

## 10. Open questions (Owner to rule before implementation)

1. RESOLVED (2026-11-18, S2): jobs root defaults to `~/edi-jobs/`, overridable via
   `EDI_HOME`.
2. RESOLVED (2026-11-18, S2): AUTHORSHIP.md carries an optional `Sponsor:` line
   (who asked for the job) — auditability at zero gate cost.
3. RESOLVED (2026-11-17, round-2 briefing): build `edi.py freeze --dry-run` in the
   runtime rather than a second preflight inside the skill; `preflight.py` stays a thin
   wrapper around it. Land the sandbox fixes first — a freeze-time sandbox check that
   writes to the host is a defect the dry-run must not inherit.
