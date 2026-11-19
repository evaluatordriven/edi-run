"""Five readings-mode fixes (edi-run-memo-readings-fixes-2026-11-18.pdf & Grok 18e RD1-RD6)."""
import json, pathlib, subprocess, sys, unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
import gates, loop  # noqa: E402
import test_readings_mode as trm  # noqa: E402


class ReadingsFixesTests(trm.ReadingsModeTests):
    def test_fails_today_1_declined_needs_owner_job_finalizes_and_verifies(self):
        job = self._make_readings_job(subdir="rf1_declined", entry="B", eval_mode="'d2_fail'")
        loop.freeze_job(job)
        self.assertEqual(loop.run_job(job), 2)
        self.assertEqual(loop.finalize_job(job, state="declined", by="Casey Park"), 0)
        self.assertEqual(loop.verify_job(job), 0)
        p = subprocess.run([sys.executable, str(ROOT / "edi.py"), "verify", str(job)], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr + p.stdout)

    def test_fails_today_2_owner_gives_needs_owner_job_records_given_needs_owner_and_verifies(self):
        job = self._make_readings_job(subdir="rf2_given_no", entry="B", eval_mode="'d2_fail'")
        loop.freeze_job(job)
        self.assertEqual(loop.run_job(job), 2)
        self.assertEqual(loop.finalize_job(job, state="given", by="Casey Park"), 0)
        events = [json.loads(l) for l in (job / "job.jsonl").read_text().splitlines() if l.strip()]
        disp = [e for e in events if e["type"] == "disposition"][-1]["data"]
        self.assertEqual((disp.get("state"), disp.get("terminalState"), disp.get("outcome")), ("given", "needs_owner", "needs_owner"))
        self.assertEqual(loop.verify_job(job), 0)

    def test_guard_1_census_job_still_finalizes_and_verifies(self):
        job = self._make_readings_job(subdir="rf_g1", entry="B", eval_mode="'all_conform'")
        loop.freeze_job(job)
        self.assertEqual(loop.run_job(job), 0)
        self.assertEqual(loop.finalize_job(job, state="given", by="Casey Park"), 0)
        self.assertEqual(loop.verify_job(job), 0)

    def test_guard_2_forged_disposition_outcome_in_job_jsonl_still_fails_verify(self):
        job = self._make_readings_job(subdir="rf_g2", entry="B", eval_mode="'d2_fail'")
        loop.freeze_job(job)
        loop.run_job(job)
        loop.finalize_job(job, state="declined", by="Casey Park")
        log_p = job / "job.jsonl"
        lines = log_p.read_text().splitlines()
        last = json.loads(lines[-1])
        last["data"]["outcome"] = "census"
        lines[-1] = json.dumps(last)
        log_p.write_text("\n".join(lines) + "\n")
        self.assertEqual(loop.verify_job(job), 1)

    def test_fails_today_3_read_writes_no_disposition_on_census_job(self):
        job = self._make_readings_job(subdir="rf3_no_disp", entry="B", eval_mode="'all_conform'")
        self.assertEqual(loop.read_job(job), 0)
        events = [json.loads(l) for l in (job / "job.jsonl").read_text().splitlines() if l.strip()]
        self.assertFalse(any(e["type"] == "disposition" for e in events), "read must never write a disposition")

    def test_fails_today_4_read_never_records_bars_author_as_disposing(self):
        job = self._make_readings_job(subdir="rf4_no_bar_author", entry="B", eval_mode="'all_conform'")
        cfg = json.loads((job / "job.json").read_text())
        cfg["disposition"]["by"] = None
        (job / "job.json").write_text(json.dumps(cfg, indent=2))
        self.assertEqual(loop.read_job(job), 0)
        events = [json.loads(l) for l in (job / "job.jsonl").read_text().splitlines() if l.strip()]
        self.assertFalse(any(e["type"] == "disposition" and e.get("actor") == cfg["bar"]["author"] for e in events))

    def test_fails_today_5_edi_read_then_owner_finalize_then_verify_all_exit_0(self):
        job = self._make_readings_job(subdir="rf5_cli_flow", entry="B", eval_mode="'all_conform'")
        r_read = subprocess.run([sys.executable, str(ROOT / "edi.py"), "read", str(job)], capture_output=True, text=True)
        self.assertEqual(r_read.returncode, 0, r_read.stderr + r_read.stdout)
        self.assertIn("finalize", r_read.stdout + r_read.stderr)
        r_fin = subprocess.run([sys.executable, str(ROOT / "edi.py"), "finalize", str(job), "--disposition", "given", "--by", "Casey Park"],
                               capture_output=True, text=True)
        self.assertEqual(r_fin.returncode, 0, r_fin.stderr + r_fin.stdout)
        r_ver = subprocess.run([sys.executable, str(ROOT / "edi.py"), "verify", str(job)], capture_output=True, text=True)
        self.assertEqual(r_ver.returncode, 0, r_ver.stderr + r_ver.stdout)

    def test_fails_today_6_edited_reading_evidence_file_fails_verify(self):
        job = self._make_readings_job(subdir="rf6_edit_ev", entry="B", eval_mode="'all_conform'")
        loop.freeze_job(job); loop.run_job(job); loop.finalize_job(job, state="given", by="Casey Park")
        ev_file = sorted((job / "evidence").glob("reading-*.json"))[0]
        ev_file.write_text(ev_file.read_text().replace("# Header", "# Tampered"))
        self.assertEqual(loop.verify_job(job), 1)

    def test_fails_today_7_deleted_reading_evidence_file_fails_verify(self):
        job = self._make_readings_job(subdir="rf7_del_ev", entry="B", eval_mode="'all_conform'")
        loop.freeze_job(job); loop.run_job(job); loop.finalize_job(job, state="given", by="Casey Park")
        ev_file = sorted((job / "evidence").glob("reading-*.json"))[0]
        ev_file.unlink()
        self.assertEqual(loop.verify_job(job), 1)

    def test_guard_3_specialist_added_after_run_is_refused_at_finalize(self):
        job = self._make_readings_job(subdir="rf_g3", entry="B", eval_mode="'all_conform'")
        loop.freeze_job(job); loop.run_job(job)
        cfg = json.loads((job / "job.json").read_text())
        cfg["bar"]["approvals"].append({"role": "specialist", "by": "Riley Moss", "barDigest": cfg["bar"]["digest"], "at": "2026-10-05T08:50:00Z"})
        (job / "job.json").write_text(json.dumps(cfg, indent=2))
        with self.assertRaises(gates.GateError):
            loop.finalize_job(job, state="given", by="Casey Park")

    def test_fails_today_8_ungrounded_quotation_fails_evaluator_and_edited_artifact_fails_verify(self):
        # Grok RD2: quotation not in the judged text must fail the evaluator -> needs_owner / no_observation
        job = self._make_readings_job(subdir="rd2_ungrounded", entry="B",
                                      eval_mode="'all_conform'")
        ev = job / "scripts" / "eval_e1.py"
        ev.write_text(ev.read_text().replace("q = '# Header' if dim == 'D1' else 'Source: Q3'",
                                             "q = '# Header' if dim == 'D1' else 'HALLUCINATED QUOTE NOT IN FILE'"))
        cfg = json.loads((job / "job.json").read_text())
        cfg["evaluators"][0]["script"]["digest"]["sha256"] = trm.trb.sha256_bytes(ev.read_bytes())
        (job / "job.json").write_text(json.dumps(cfg, indent=2))
        loop.freeze_job(job); rc = loop.run_job(job)
        st = json.loads((job / "state.json").read_text())
        self.assertEqual((rc, st["terminal_state"], st["reason"]), (2, "needs_owner", "no_observation"))

        # Grok RD5: editing artifact/memo.md after finalize on a readings job must fail verify_job
        j_art = self._make_readings_job(subdir="rd5_art", entry="B", eval_mode="'all_conform'")
        loop.freeze_job(j_art); loop.run_job(j_art); loop.finalize_job(j_art, state="given", by="Casey Park")
        (j_art / "artifact" / "memo.md").write_text("# Tampered after finalize\n")
        self.assertEqual(loop.verify_job(j_art), 1)

        # Mid-batch cap_reached / no incumbent_round on a readings job still finalizes & verifies cleanly
        j_cap = self._make_readings_job(subdir="rd_no_inc", entry="B", eval_mode="'all_conform'")
        loop.freeze_job(j_cap)
        st_c = json.loads((j_cap / "state.json").read_text())
        st_c.update(terminal_state="needs_owner", reason="cap_reached", incumbent_round=None)
        (j_cap / "state.json").write_text(json.dumps(st_c))
        self.assertEqual(loop.finalize_job(j_cap, state="declined", by="Casey Park"), 0)
        self.assertEqual(loop.verify_job(j_cap), 0)
        self.assertFalse((j_cap / "record.json").exists())


if __name__ == "__main__":
    unittest.main()
