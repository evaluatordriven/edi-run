"""Section 5 requirement tests for readings mode (C29, D4.6, D5.outcome.census; 2026-11-18)."""
import json, pathlib, subprocess, sys, unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
import gates, loop  # noqa: E402
import test_runtime_batches as trb  # noqa: E402


READ_EVAL_SRC = """import json, pathlib, sys
pos = [a for a in sys.argv[1:] if not a.startswith('--')]
target, dim = pathlib.Path(pos[0]), pos[1]
txt = (target / 'memo.md').read_text() if target.is_dir() else target.read_text()
mode = __MODE_EXPR__
if 'Control Audit' in txt:
    out = {'outcome': 'does_not_conform' if mode == 'bad_control' else 'conforms', 'quotation': '# Header'}
elif 'No header' in txt:
    out = {'outcome': 'conforms' if mode == 'bad_control' else 'does_not_conform', 'quotation': 'No header'}
elif mode == 'all_conform':
    q = '# Header' if dim == 'D1' else 'Source: Q3'
    out = {'outcome': 'conforms', 'quotation': q}
elif mode == 'd2_fail':
    out = {'outcome': 'conforms' if dim == 'D1' else 'does_not_conform', 'quotation': '# Header' if dim == 'D1' else 'Figure: 42%'}
elif mode == 'numeric_slot':
    out = {'outcome': 5, 'quotation': '# Header'}
elif mode == 'bad_word':
    out = {'outcome': 'regenerate', 'quotation': '# Header'}
elif mode == 'blank_quote':
    out = {'outcome': 'conforms', 'quotation': '   '}
elif mode == 'all_unobserved':
    out = {'outcome': 'unobserved', 'reason': 'channel offline'}
elif mode == 'flap':
    flag = pathlib.Path(__file__).with_name(f'flap_{dim}.txt')
    first = not flag.exists()
    flag.write_text('1')
    out = {'outcome': 'conforms' if (first or dim != 'D1') else 'does_not_conform', 'quotation': '# Header'}
print(json.dumps(out))
"""


class ReadingsModeTests(trb.RuntimeBatchTest):
    def _make_readings_job(self, subdir="rjob", entry="B", eval_mode="'all_conform'"):
        job = self._make_base_job(subdir=subdir)
        ev = job / "scripts" / "eval_e1.py"
        ev.write_text(READ_EVAL_SRC.replace("__MODE_EXPR__", eval_mode), encoding="utf-8")
        cfg = json.loads((job / "job.json").read_text(encoding="utf-8"))
        cfg["mode"] = "readings"
        cfg["bounds"]["max_rounds"] = 1
        for d in cfg["bar"]["dimensions"]:
            d.pop("scale", None)
        cfg["bar"]["approvals"] = [a for a in cfg["bar"]["approvals"] if a["role"] == "buyer"]
        cfg["evaluators"][0]["script"]["digest"]["sha256"] = trb.sha256_bytes(ev.read_bytes())
        if entry == "B":
            sdir = job / "seed"
            sdir.mkdir(exist_ok=True)
            (sdir / "memo.md").write_text("# Header\nFigure: 42% (Source: Q3, 2026-10-01)\n", encoding="utf-8")
            cfg["seed"] = {"dir": "seed", "author": "Dana Voss", "origin": "existing work"}
        (job / "job.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
        return job

    def test_entry_b_all_conform_exits_accepted_census_and_verifies(self):
        job = self._make_readings_job(subdir="eb_ok", entry="B", eval_mode="'all_conform'")
        self.assertEqual(loop.freeze_job(job), 0)
        self.assertEqual(loop.run_job(job), 0)
        st = json.loads((job / "state.json").read_text())
        self.assertEqual(st["terminal_state"], "accepted")
        self.assertEqual(st["reason"], "census")
        self.assertEqual(loop.finalize_job(job, state="given", by="Casey Park"), 0)
        self.assertFalse((job / "record.json").exists(), "Option (a): readings mode emits no custody/v1 record.json")
        events = [json.loads(l) for l in (job / "job.jsonl").read_text().splitlines() if l.strip()]
        disp = [e for e in events if e["type"] == "disposition"][-1]["data"]
        self.assertEqual(disp.get("terminalState"), "accepted")
        self.assertEqual(disp.get("outcome"), "census")
        r_ver = subprocess.run([sys.executable, str(ROOT / "vendor" / "record.py"), "verify", "job.jsonl", "--files"],
                               cwd=str(job), capture_output=True, text=True)
        self.assertEqual(r_ver.returncode, 0, r_ver.stderr + r_ver.stdout)
        self.assertEqual(loop.verify_job(job), 0)

    def test_entry_a_one_maker_turn_exits_accepted_census(self):
        job = self._make_readings_job(subdir="ea_ok", entry="A", eval_mode="'all_conform'")
        self.assertEqual(loop.freeze_job(job), 0)
        self.assertEqual(loop.run_job(job), 0)
        st = json.loads((job / "state.json").read_text())
        self.assertEqual(st["terminal_state"], "accepted")
        self.assertEqual(st["reason"], "census")
        self.assertEqual(len(st["rounds"]), 1)

    def test_stable_does_not_conform_exits_needs_owner_with_quotation_and_escalation(self):
        job = self._make_readings_job(subdir="dnc", entry="B", eval_mode="'d2_fail'")
        loop.freeze_job(job)
        rc = loop.run_job(job)
        self.assertEqual(rc, 2)  # needs_owner, never rejected
        st = json.loads((job / "state.json").read_text())
        self.assertEqual(st["terminal_state"], "needs_owner")
        self.assertEqual(st["reason"], "needs_owner")
        self.assertEqual(len(st["rounds"]), 1)  # no second round dispatched
        self.assertTrue((job / "escalation.json").is_file())
        events = [json.loads(l) for l in (job / "job.jsonl").read_text().splitlines() if l.strip()]
        dnc_checks = [e["data"] for e in events if e["type"] == "check" and e["data"]["outcome"] == "does_not_conform"]
        self.assertTrue(dnc_checks)
        self.assertEqual(dnc_checks[0].get("quotation"), "Figure: 42%")

    def test_readings_refuses_specialist_approval_at_freeze_and_via_pin_after_freeze(self):
        job = self._make_readings_job(subdir="spec_ref", entry="B")
        cfg = json.loads((job / "job.json").read_text())
        bar_sha = cfg["bar"]["digest"]["sha256"]
        cfg["bar"]["approvals"].append({"role": "specialist", "by": "Riley Moss", "barDigest": {"sha256": bar_sha}, "at": "2026-10-05T08:50:00Z"})
        (job / "job.json").write_text(json.dumps(cfg, indent=2))
        with self.assertRaises(gates.GateError):
            loop.freeze_job(job)
        # Remove specialist, freeze, then add specialist after freeze -> pin refuses run
        cfg["bar"]["approvals"] = [a for a in cfg["bar"]["approvals"] if a["role"] == "buyer"]
        (job / "job.json").write_text(json.dumps(cfg, indent=2))
        loop.freeze_job(job)
        cfg["bar"]["approvals"].append({"role": "specialist", "by": "Riley Moss", "barDigest": {"sha256": bar_sha}, "at": "2026-10-05T08:50:00Z"})
        (job / "job.json").write_text(json.dumps(cfg, indent=2))
        with self.assertRaises(gates.GateError):
            loop.run_job(job)

    def test_readings_refuses_scale_threshold_floor_max_rounds_gt_1_and_unknown_mode(self):
        for tag, mut in (
            ("scale", lambda c: c["bar"]["dimensions"][0].update(scale={"threshold": 4.0})),
            ("threshold", lambda c: c["bar"]["dimensions"][0].update(threshold=4.0)),
            ("floor", lambda c: c["bar"]["dimensions"][0].update(floor=1.0)),
            ("rounds", lambda c: c["bounds"].update(max_rounds=2)),
            ("bad_mode", lambda c: c.update(mode="hybrid")),
        ):
            job = self._make_readings_job(subdir=f"ref_{tag}", entry="B")
            cfg = json.loads((job / "job.json").read_text())
            mut(cfg)
            (job / "job.json").write_text(json.dumps(cfg, indent=2))
            with self.assertRaises(gates.GateError, msg=tag):
                loop.freeze_job(job)

    def test_readings_invalid_slot_blank_quote_flapping_unobserved_and_control_failure(self):
        # 1. Numeric slot -> evaluator failed (no_observation)
        j_num = self._make_readings_job(subdir="num", entry="B", eval_mode="'numeric_slot'")
        loop.freeze_job(j_num); loop.run_job(j_num)
        st = json.loads((j_num / "state.json").read_text())
        self.assertEqual((st["terminal_state"], st["reason"]), ("needs_owner", "no_observation"))

        # 2. Word outside the three (e.g. regenerate) -> evaluator failed
        j_word = self._make_readings_job(subdir="word", entry="B", eval_mode="'bad_word'")
        loop.freeze_job(j_word); loop.run_job(j_word)
        st = json.loads((j_word / "state.json").read_text())
        self.assertEqual((st["terminal_state"], st["reason"]), ("needs_owner", "no_observation"))

        # 3. Blank quotation -> no observation
        j_blank = self._make_readings_job(subdir="blank", entry="B", eval_mode="'blank_quote'")
        loop.freeze_job(j_blank); loop.run_job(j_blank)
        st = json.loads((j_blank / "state.json").read_text())
        self.assertEqual((st["terminal_state"], st["reason"]), ("needs_owner", "no_observation"))

        # 4. Flapping across runs -> recorded in disagreements with both words, exits needs_owner / no_observation
        j_flap = self._make_readings_job(subdir="flap", entry="B", eval_mode="'flap'")
        loop.freeze_job(j_flap); loop.run_job(j_flap)
        st = json.loads((j_flap / "state.json").read_text())
        self.assertEqual((st["terminal_state"], st["reason"]), ("needs_owner", "no_observation"))
        d1_words = [c["outcome"] for c in st["rounds"]["1"]["checks"] if c["dimension"] == "D1"]
        self.assertEqual(set(d1_words), {"conforms", "does_not_conform"})

        # 5. Dimension read unobserved on every run -> needs_owner / needs_owner (Decision 6)
        j_unobs = self._make_readings_job(subdir="unobs", entry="B", eval_mode="'all_unobserved'")
        loop.freeze_job(j_unobs); loop.run_job(j_unobs)
        st = json.loads((j_unobs / "state.json").read_text())
        self.assertEqual((st["terminal_state"], st["reason"]), ("needs_owner", "needs_owner"))

        # 6. Control that reads wrong -> instrument failed, outcome no_observation
        j_ctl = self._make_readings_job(subdir="bad_ctl", entry="B", eval_mode="'bad_control'")
        loop.freeze_job(j_ctl); loop.run_job(j_ctl)
        st = json.loads((j_ctl / "state.json").read_text())
        self.assertEqual((st["terminal_state"], st["reason"]), ("needs_owner", "no_observation"))


if __name__ == "__main__":
    unittest.main()
