import pathlib
lp = pathlib.Path("/Users/smoitozo/Code/edi-run/loop.py")
t = lp.read_text()
# P3-2: wall-clock maker_error names the bound
old = '''                state.update(terminal_state="needs_owner", reason="maker_failed", maker_error="maker wall-clock deadline exceeded")'''
new = '''                state.update(terminal_state="needs_owner", reason="maker_failed",
                             maker_error="maker wall-clock deadline exceeded (max_wall_seconds=%s)" % (cfg.get("bounds") or {}).get("max_wall_seconds"))'''
assert old in t
t = t.replace(old, new)
lp.write_text(t)
import ast; ast.parse(t); print("loop OK")

d = pathlib.Path("/Users/smoitozo/Code/edi-run/docs/2026-EDI-Runtime-Solution-Architecture.md")
s = d.read_text()
# P2-3: interim covers model makers too; designed mechanism in future tense
old2 = '''INTERIM (until `metering.py` lands per `docs/2026-EDI-DESIGN-S1-C-R1.md`): agent-maker spend accrues only from calls a completed maker round reports — a crash or timeout loses the in-flight round's spend, and the cap fires at round boundaries only — both disclosed limits that the metering design deletes.'''
new2 = '''INTERIM (until `metering.py` lands per `docs/2026-EDI-DESIGN-S1-C-R1.md`): no `usage.jsonl` exists yet — spend accrues only from usage that completed seat rounds report, a crash or timeout loses the in-flight round's spend (model-kind and agent-kind makers alike), and the cap fires at round boundaries only — disclosed limits that the metering design deletes.'''
assert old2 in s
s = s.replace(old2, new2)
# P3-4: §5 exit-code list gains the gap pointer
old3 = '''- `2`: `NOT-CHECKABLE` / halted at `needs_owner` awaiting Own'''
# find exact line
import re
m = re.search(r"- `2`: `NOT-CHECKABLE`[^\n]*\n", s)
assert m
s = s.replace(m.group(0), m.group(0) + "- Gap-listed emission (§6) exits `1` and is distinguished from a merit failure by the record's `recordType: \"edi-run/gap-record\"`, never by exit code alone.\n")
d.write_text(s)
print("arch doc OK")

dd = pathlib.Path("/Users/smoitozo/Code/edi-run/docs/2026-EDI-DESIGN-S1-C-R1.md")
t2 = dd.read_text()
# P2-5: relabel is an amendment row, never an in-place edit
old4 = '''  (moved OUT of `rounds/` — which stays
  immutable — to `job/partial/round-N/`), relabels that round's meter rows
  `discarded_round: true`, re-accrues spend from the durable usage log'''
new4 = '''  (moved OUT of `rounds/` — which stays
  immutable — to `job/partial/round-N/`), appends a superseding AMENDMENT row to
  the usage log (`{kind: "amendment", amends: [<row digests>], discarded_round:
  true}`) — meter rows are append-only and hash-chained, so no row is ever
  rewritten in place — and re-accrues spend from the durable usage log'''
assert old4 in t2
t2 = t2.replace(old4, new4)
# P3-6: round numbering rule
old5 = '''on `resume` (R1) the round restarts fresh
    (its meter rows are kept, its work is discarded and labeled
    `discarded_round: true` so a re-accrual cannot double-count).'''
new5 = '''on `resume` (R1) the round restarts fresh
    under the SAME round number, and the discarded attempt CONSUMES that
    `max_rounds` slot (the attempt was made and paid for — bounds are respected
    conservatively); its meter rows are kept and marked via the amendment row so
    a re-accrual cannot double-count.'''
assert old5 in t2
t2 = t2.replace(old5, new5)
dd.write_text(t2)
print("design doc OK")
