"""Loop and evaluator semantics invariants — findings raised in the 2026-11-17/18 briefings, re-homed by concern (not by review date).

Tests named test_r*_ depend on an Owner ruling (see the briefing's "Owner rulings").
Drop them if the Owner rules the other way.
"""
import json
import pathlib
import shutil
import tempfile
import unittest

import gates
import loop

from support_briefing import PASSING, _Base, _R2Base

REPO = pathlib.Path(loop.__file__).resolve().parent


# ---------------------------------------------------------------- Finding 1
class Finding1SeedProvenance(_Base):
    def test_1_seed_winner_is_credited_to_seed_author_in_the_log(self):
        """The artifact-produced event the record binds to must name the same maker as the record."""
        job = self._make_base_job(rounds_behavior="regress_r2", subdir="f1")
        (job / "seed").mkdir()
        (job / "seed" / "memo.md").write_text("# Header\nNo figure yet.\n", encoding="utf-8")
        cfg = self._cfg(job)
        cfg["seed"] = {"dir": "seed", "author": "Dana Voss", "origin": "Owner draft", "note": ""}
        self._save(job, cfg)
        loop.freeze_job(job)
        self.assertEqual(loop.run_job(job), 2)  # round 2 regresses, cap reached
        st = json.loads((job / "state.json").read_text())
        self.assertEqual((st["incumbent_round"], st["reason"]), (1, "cap_reached"))  # the seed won
        loop.finalize_job(job, state="given", by="Casey Park")
        rec = json.loads((job / "record.json").read_text())
        ev = self._events(job)
        last_prod = [e for e in ev if e["type"] == "artifact-produced"][-1]
        self.assertEqual(rec["predicate"]["maker"]["name"], "Dana Voss")
        self.assertEqual(last_prod["actor"], rec["predicate"]["maker"]["name"],
                         "custody log credits a different maker than the record for the shipped artifact")


# ---------------------------------------------------------------- Finding 2
class Finding2HeldOutCheck(_Base):
    def test_2a_prefix_collision_is_filtered_not_fatal(self):
        """D4.9: held-out material is filtered out of the next round's input. A candidate line that
        shares a 24-char prefix with a held-out file must not end the job."""
        job = self._make_base_job(subdir="f2a")
        self._maker(job, "{1: '# Header\\nHeld-out reference control figure is still pending\\n', 2: '" + PASSING + "'}")
        loop.freeze_job(job)
        try:
            rc = loop.run_job(job)
        except gates.GateError as e:
            self.fail(f"job killed by held-out heuristic instead of filtering the packet: {e}")
        self.assertEqual(rc, 0)
        pkt = (job / "rounds" / "round-02" / "packet.json").read_text().casefold()
        self.assertNotIn("held-out reference contr", pkt)  # filtered, not forwarded

    def test_2b_gate_error_during_run_is_not_labelled_interrupted(self):
        """D4.11: the Owner must not be told a gate failure was a Ctrl-C."""
        job = self._make_base_job(subdir="f2b")
        (job / "seed").mkdir()
        (job / "seed" / "memo.md").write_text("# Header\n" + "Figure: 42% (Source: Q3, 2026-10-01)\n", encoding="utf-8")
        cfg = self._cfg(job)
        cfg["seed"] = {"dir": "seed", "author": "Dana Voss", "origin": "", "note": ""}
        self._save(job, cfg)
        loop.freeze_job(job)
        (job / "seed" / "memo.md").write_text("# Header\nTAMPERED\n", encoding="utf-8")
        with self.assertRaises(gates.GateError):
            loop.run_job(job)
        st = json.loads((job / "state.json").read_text())
        esc = (job / "escalation.json").read_text()
        self.assertNotEqual(st["reason"], "interrupted")
        self.assertNotIn("Ctrl-C", esc)


# ---------------------------------------------------------------- Finding 3
class Finding3DoctrineConformance(_Base):
    def test_3a_next_round_gets_every_known_defect(self):
        """D4.9 / D5.step.5: the next round receives every known defect with its measurement,
        including defects measured on a revision that did not replace the incumbent."""
        job = self._make_base_job(subdir="f3a")
        cfg = self._cfg(job)
        cfg["bounds"]["max_rounds"] = 3
        self._save(job, cfg)
        self._maker(job, "{1: '# Header\\nNo figure yet.\\n', 2: 'Broken both\\n', 3: '" + PASSING + "'}")
        loop.freeze_job(job)
        self.assertEqual(loop.run_job(job), 0)
        st = json.loads((job / "state.json").read_text())
        self.assertEqual(st["rounds"]["2"]["dim_scores"]["D1"], 1.0)  # round 2 measured a D1 defect
        pkt3 = json.loads((job / "rounds" / "round-03" / "packet.json").read_text())
        self.assertTrue(any(d["dimension"] == "D1" and d.get("round") == 2 and "score" in d for d in pkt3["defects"]),
                        f"round 2's measured D1 defect was dropped from round 3's input: {pkt3['defects']}")

    def test_3b_default_is_two_scored_runs(self):
        """D4.5: a verdict rests on at least two scored runs; one run is a sample."""
        job = self._make_base_job(subdir="f3b")
        cfg = self._cfg(job)
        cfg["evaluators"][0].pop("runs", None)
        self._save(job, cfg)
        loop.freeze_job(job)
        loop.run_job(job)
        st = json.loads((job / "state.json").read_text())
        for r, rd in st["rounds"].items():
            for d in ("D1", "D2"):
                n = sum(1 for c in rd["checks"] if c["dimension"] == d and c["evaluator"] == "E1")
                self.assertGreaterEqual(n, 2, f"round {r} {d}: {n} scored run(s)")

    def test_3b_freeze_refuses_a_single_run(self):
        job = self._make_base_job(subdir="f3b1")
        cfg = self._cfg(job)
        cfg["evaluators"][0]["runs"] = 1
        self._save(job, cfg)
        with self.assertRaises(gates.GateError):
            loop.freeze_job(job)

    def test_3c_escalation_says_what_was_tried_and_why(self):
        """D4.11: an escalation carries what was tried, why no observable answer exists,
        and options with a recommendation. Key names `tried` and `why` are proposals."""
        job = self._make_base_job(rounds_behavior="tradeoff", subdir="f3c")
        loop.freeze_job(job)
        self.assertEqual(loop.run_job(job), 2)
        esc = json.loads((job / "escalation.json").read_text())
        self.assertEqual(esc["reason"], "dimension_tradeoff")
        tried = esc.get("tried") or []
        self.assertEqual([t.get("round") for t in tried], [1, 2])
        self.assertTrue(all(set(t.get("dimScores", {})) == {"D1", "D2"} for t in tried))
        why = json.dumps(esc.get("why"))
        self.assertIn("D1", why)
        self.assertIn("D2", why)
        self.assertTrue(esc.get("options") and esc.get("recommendation"))


# ------------------------------------------------------------------ Finding C
class FindingC_FilterPrecision(unittest.TestCase):
    """D4.9 asks that held-out material be filtered out of the Maker's input. It does not ask
    that the bar's own wording, or ordinary phrases, be filtered out with it."""

    def _job(self, bar, clean):
        d = pathlib.Path(tempfile.mkdtemp(prefix="edi_r2_filter_"))
        self.addCleanup(shutil.rmtree, d, True)
        (d / "held_out" / "controls").mkdir(parents=True)
        (d / "bar.md").write_text(bar, encoding="utf-8")
        (d / "method.md").write_text("# Method\nWrite the summary.\n", encoding="utf-8")
        (d / "held_out" / "controls" / "clean.md").write_text(clean, encoding="utf-8")
        return d

    def test_c1_a_remedy_quoting_the_bar_reaches_the_maker(self):
        d = self._job("# Bar\n- D1: include the section '## Root cause and prevention measures' verbatim\n",
                      "## Root cause and prevention measures (owner: SRE on call)\nThe queue backed up after a config push.\n")
        item = {"remedy": "Add the section '## Root cause and prevention measures' after the timeline."}
        out, n = gates.scrub_held_out([item], gates.held_out_needles(d))
        self.assertEqual((out[0]["remedy"], n), (item["remedy"], 0), "bar-required wording was withheld from the Maker")

    def test_c2_only_the_matching_span_is_withheld(self):
        sentence = "The queue backed up for forty minutes after a config push at 14:02."
        d = self._job("# Bar\n- D1: a timeline\n", sentence + "\n")
        items = [{"observation": f"Paragraph 2 copies a reference sentence: '{sentence}' Rewrite it from the incident log."},
                 {"observation": "Paragraph 3: 'the  QUEUE backed up for forty\nminutes after a CONFIG push at 14:02' again."}]
        out, n = gates.scrub_held_out(items, gates.held_out_needles(d))
        for o in out:
            self.assertNotIn("backed up for forty", " ".join(o["observation"].casefold().split()))  # still filtered
        self.assertIn("Paragraph 2 copies a reference sentence", out[0]["observation"])  # the rest survives
        self.assertIn("Rewrite it from the incident log.", out[0]["observation"])
        self.assertIn("again.", out[1]["observation"])
        self.assertGreaterEqual(n, 2)

    def test_c3_a_shared_common_phrase_is_not_a_leak(self):
        d = self._job("# Bar\n- D2: root cause named\n", "The fault was fixed in commit 4e1d by the on-call engineer.\n")
        item = {"observation": "The root-cause section says it was fixed in commit `9f2e1c`, with no follow-up."}
        out, n = gates.scrub_held_out([item], gates.held_out_needles(d))
        self.assertEqual((out[0]["observation"], n), (item["observation"], 0), "a 21-character common phrase was treated as a leak")

    def test_c4_the_committed_cl2_example_loses_no_line_whole(self):
        ex = REPO / "examples" / "live-cl2-job"
        needles = gates.held_out_needles(ex)
        lines = [ln for ln in (ex / "artifact" / "summary.md").read_text().splitlines() if ln.strip()]
        lost = [ln for ln in lines if gates.scrub_held_out([{"o": ln}], needles)[0][0]["o"] == gates.WITHHELD]
        self.assertEqual(lost, [], f"{len(lost)}/{len(lines)} lines of the accepted artifact would reach the Maker as [withheld]")


# ------------------------------------------------------------------ Finding I
class FindingI_ShortLineNeedle(_R2Base):
    def test_i_short_held_out_lines_are_whole_line_needles(self):
        """Recheck #3: a held-out line shorter than WINDOW (e.g. 29 chars) yielded no
        32-char window and passed the filter untouched -- for script/model Makers
        the filter is the ONLY exclusion mechanism."""
        job = self.tmp / "job-short"
        (job / "held_out").mkdir(parents=True)
        (job / "held_out" / "key.txt").write_text("No header and no figure here.\n")
        windows, digests, lines = gates.held_out_needles(job)
        self.assertIn(gates._canon("No header and no figure here."), lines)
        # frontier-review blocker regression: short common words must NOT become
        # substring needles (they scrubbed legitimate feedback and GateError-ed legal packets)
        (job / "held_out" / "labels.txt").write_text("yes\nno\nN/A\nStep 1\n")
        _, _, short = gates.held_out_needles(job)
        self.assertEqual(short, {gates._canon("No header and no figure here.")})  # <MIN_LINE skipped
        item = {"feedback": "The script prints yes at the end. We note the northern approach; do not reformat."}
        out, _ = gates.scrub_held_out([item], gates.held_out_needles(job))
        self.assertNotIn(gates.WITHHELD, out[0]["feedback"])  # no false-positive scrub
        # the 29-char line IS matched, but only whole-line: mid-word echoes survive,
        # a line-echo is redacted
        short_line = "No header and no figure here."
        out2, w2 = gates.scrub_held_out(
            [{"a": "Overall there was " + short_line + " in the control."}],
            gates.held_out_needles(job))
        self.assertEqual(w2, 1)
        self.assertIn(gates.WITHHELD, out2[0]["a"])
        out3, w3 = gates.scrub_held_out(
            [{"a": "the no-header and no-figure here-abouts clause"}],
            gates.held_out_needles(job))
        self.assertEqual(w3, 0)  # word-boundary respected: mid-word echo survives
        # verification follow-ups: punctuation- and quote-delimited echoes are leaks too
        out4, w4 = gates.scrub_held_out(
            [{"a": "the verdict was '" + short_line + "', so revise."}],
            gates.held_out_needles(job))
        self.assertEqual(w4, 1)
        out5, w5 = gates.scrub_held_out(
            [{"a": "Summary: " + short_line + "."}],
            gates.held_out_needles(job))
        self.assertEqual(w5, 1)


if __name__ == "__main__":
    unittest.main()
