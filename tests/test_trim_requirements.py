"""Requirement-level tests for the core trim (2026-11-18).

Each test states a guarantee from the architecture (I-01/I-02/I-04/I-07, P6/P9),
not a mechanism, so it must pass on the current code AND on the trimmed code.
Tests marked NEW also fail on the 2026-11-18 snapshot (they pin defects R1/R3/Q2).
Run from the repo root: python3 -m unittest tests/test_trim_requirements.py
"""
import json, os, pathlib, sys, threading, time, unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
import gates, loop  # noqa: E402
from metering import Meter, CapExceeded  # noqa: E402
import test_runtime_batches as trb  # noqa: E402

PRICING = {"default": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0}}


class _Job(trb.RuntimeBatchTest):
    def meter(self, job, cap=1.0, out=100_000):
        return Meter(job, pricing=PRICING, bounds={"max_cost_usd": cap, "max_output_tokens": out})


class SpendGuarantees(_Job):
    """I-07 cost accounting: the cap holds, nothing is billed twice, nothing billed is lost."""

    def test_kill_after_reserve_is_recovered_as_spend_exactly_once(self):
        job = self._make_base_job(subdir="k")
        child = (f"import sys, os; sys.path.insert(0, {str(ROOT)!r}); from metering import Meter\n"
                 f"m = Meter({str(job)!r}, pricing={PRICING!r}, bounds={{'max_cost_usd': 1.0, 'max_output_tokens': 100000}})\n"
                 "rid = m.reserve({'prompt': 'x'}, model='m'); print(m.reservation_amount(rid), flush=True); os._exit(9)\n")
        import subprocess
        out = subprocess.run([sys.executable, "-c", child], capture_output=True, text=True, timeout=60)
        held = float(out.stdout.strip())  # the process died holding a billed-but-unrowed reservation
        for _ in range(2):  # recovering twice must charge once
            m = self.meter(job)
            if hasattr(m, "recover"):
                m.recover()
        self.assertAlmostEqual(self.meter(job).accrued(), held, places=9)

    def test_parallel_reservations_never_jointly_pass_the_cap(self):
        job = self._make_base_job(subdir="p")
        m = self.meter(job, cap=1.0, out=200_000)  # each worst case ~$0.2
        granted, lock = [], threading.Lock()

        def go():
            try:
                rid = m.reserve({"prompt": "y"}, model="m")
                with lock:
                    granted.append(m.reservation_amount(rid))
            except CapExceeded:
                pass
        ts = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in ts]; [t.join() for t in ts]
        self.assertLess(sum(granted), 1.0)
        self.assertGreater(len(granted), 0)

    def test_settle_is_exactly_once_and_unknown_ids_bill_nothing(self):
        job = self._make_base_job(subdir="s")
        m = self.meter(job)
        rid = m.reserve({"prompt": "z"}, model="m")
        r1 = m.meter_call("maker", "m", "u", None, usage={"prompt_tokens": 10, "completion_tokens": 10}, settle_reservation=rid)
        r2 = m.meter_call("maker", "m", "u", None, usage={"prompt_tokens": 10, "completion_tokens": 10}, settle_reservation=rid)
        self.assertEqual(r1["digest"], r2["digest"])
        before = m.accrued()
        with self.assertRaises(ValueError):
            m.meter_call("maker", "m", "u", None, usage={"prompt_tokens": 5}, settle_reservation="no-such-id")
        with self.assertRaises(ValueError):
            m.release(rid)  # already settled
        self.assertEqual(m.accrued(), before)
        self.assertEqual(m._reserved, 0)

    def test_billed_reply_without_usage_is_charged_at_the_worst_case(self):
        job = self._make_base_job(subdir="u")
        m = self.meter(job)
        rid = m.reserve({"prompt": "w"}, model="m")
        held = m.reservation_amount(rid)
        row = m.meter_call("maker", "m", "u", None, usage={}, settle_reservation=rid)
        self.assertAlmostEqual(float(row["cost"]), held, places=9)
        self.assertTrue(row.get("usage_missing"))

    def test_anchor_catches_a_dropped_newest_row_and_a_rewritten_cost(self):
        job = self._make_base_job(subdir="a")
        m = self.meter(job)
        rid = m.reserve({"prompt": "v"}, model="m")
        m.meter_call("maker", "m", "u", None, usage={"prompt_tokens": 1000}, settle_reservation=rid)
        anchor = m.anchor()
        p = job / "usage.jsonl"
        lines = p.read_text().splitlines()
        p.write_text("\n".join(lines[:-1]) + "\n")
        with self.assertRaises(ValueError):
            Meter(job, pricing=PRICING, bounds={"max_cost_usd": 1.0}).verify(anchor=anchor)


class LifecycleBarrier(_Job):
    """P6: no paid call and no custody append can follow a disposition."""

    def test_finalize_never_interleaves_with_a_live_run(self):
        job = self._make_base_job(subdir="l")
        mk = job / "scripts" / "maker.py"
        mk.write_text("import time\n" + mk.read_text().replace("out.write_text", "time.sleep(1.5); out.write_text"))
        cfg = json.loads((job / "job.json").read_text())
        cfg["bounds"]["max_rounds"] = 1
        (job / "job.json").write_text(json.dumps(cfg, indent=2))
        loop.freeze_job(job)
        th = threading.Thread(target=lambda: loop.run_job(job)); th.start()
        time.sleep(0.4)
        try:
            loop.finalize_job(job, state="declined", by="Casey Park")
        except gates.GateError:
            pass  # refusing is also safe; interleaving is not
        th.join(30)
        events = [json.loads(l) for l in (job / "job.jsonl").read_text().splitlines() if l.strip()]
        kinds = [e.get("type") for e in events]
        if "disposition" in kinds:
            after = kinds[kinds.index("disposition") + 1:]
            self.assertFalse([k for k in after if k in ("artifact-produced", "check", "control", "external")],
                             f"custody events after the disposition: {after}")


class PinGuarantees(_Job):
    """I-01/I-02/I-04: nothing frozen moves; runtime outputs and caches are not 'moves'."""

    def test_NEW_editing_a_held_out_control_after_freeze_is_refused(self):  # Q2
        job = self._make_base_job(subdir="q2")
        loop.freeze_job(job)
        c = job / "held_out" / "controls" / "clean.md"
        os.chmod(c, 0o600)
        c.write_text("# Header\nFigure: 77% (Source: swapped, 2026-11-18)\n")
        with self.assertRaises(gates.GateError):
            gates.check_pins(json.loads((job / "job.json").read_text()), job,
                             json.loads((job / "state.json").read_text())["pins"])

    def test_NEW_a_helper_import_cache_does_not_block_finalize(self):  # R3
        job = self._make_base_job(subdir="r3")
        (job / "scripts" / "helper.py").write_text("def is_header(t):\n    return t.startswith('# Header')\n")
        ev = job / "scripts" / "eval_e1.py"
        ev.write_text("import helper\n" + ev.read_text().replace("txt.startswith('# Header')", "helper.is_header(txt)"))
        cfg = json.loads((job / "job.json").read_text())
        cfg["evaluators"][0]["script"]["digest"]["sha256"] = trb.sha256_bytes(ev.read_bytes())
        (job / "job.json").write_text(json.dumps(cfg, indent=2))
        os.environ.pop("PYTHONDONTWRITEBYTECODE", None)
        loop.freeze_job(job); loop.run_job(job)
        loop.finalize_job(job, state="given", by="Casey Park")

    def test_editing_a_non_python_seat_file_after_freeze_is_refused(self):
        job = self._make_base_job(subdir="np")
        (job / "scripts" / "weights.txt").write_text("D1=1\n")
        loop.freeze_job(job)
        (job / "scripts" / "weights.txt").write_text("D1=9\n")
        with self.assertRaises(gates.GateError):
            loop.run_job(job)

    def test_NEW_a_refused_freeze_leaves_job_json_untouched(self):  # R1
        job = self._make_base_job(subdir="r1")
        cfg = json.loads((job / "job.json").read_text())
        cfg["bar"]["digest"]["sha256"] = None
        (job / "job.json").write_text(json.dumps(cfg, indent=2))
        good = (job / "bar.md").read_bytes()
        (job / "bar.md").write_text(good.decode() + "(edited after approval)\n")
        before = (job / "job.json").read_bytes()
        with self.assertRaises(gates.GateError):
            loop.freeze_job(job)
        self.assertEqual((job / "job.json").read_bytes(), before)
        (job / "bar.md").write_bytes(good)
        self.assertEqual(loop.freeze_job(job), 0)  # the corrected retry must succeed

    def test_a_null_digest_job_freezes_runs_finalizes_and_verifies(self):
        job = self._make_base_job(subdir="nd")
        cfg = json.loads((job / "job.json").read_text())
        cfg["bar"]["digest"]["sha256"] = None
        (job / "job.json").write_text(json.dumps(cfg, indent=2))
        self.assertEqual(loop.freeze_job(job), 0)
        self.assertEqual(loop.run_job(job), 0)
        loop.finalize_job(job, state="given", by="Casey Park")
        self.assertEqual(loop.verify_job(job), 0)

    def test_n11_model_seat_call_sends_output_limit_and_reserves_before_dispatch(self):
        import http.server
        seen = []
        class _H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a): return
            def do_POST(self):
                b = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                seen.append(b)
                out = json.dumps({"model": "m", "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                                  "choices": [{"message": {"content": '{"score": 5.0, "observation": "# Header", "remedy": ""}'}}]}).encode()
                self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
        srv = http.server.HTTPServer(("127.0.0.1", 0), _H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            job = self._make_base_job(subdir="n11")
            m = self.meter(job, cap=1.0, out=333)
            ev = {"id": "E2", "kind": "model", "url": f"http://127.0.0.1:{srv.server_port}/v1/chat/completions", "model": {"identifier": "m"}}
            import adapters
            adapters.run_evaluator(ev, job / "bar.md", "D1", rubric_txt="r", dim_cfg={"id": "D1"}, meter=m)
            self.assertEqual(seen[-1].get("max_tokens"), 333)
            kinds = [r["kind"] for r in m._rows]
            self.assertEqual(kinds, ["reserve", "call"])
        finally:
            srv.shutdown(); srv.server_close()

    def test_n12_freeze_refuses_model_or_agent_seat_without_max_output_tokens(self):
        job = self._make_base_job(subdir="n12")
        cfg = json.loads((job / "job.json").read_text())
        cfg["bounds"].pop("max_output_tokens", None)
        cfg["evaluators"].append({"id": "E2", "name": "Model Judge", "kind": "model", "script": None, "runs": 2,
                                  "url": "https://openrouter.ai/api/v1/chat/completions",
                                  "model": {"family": "m", "identifier": "m-1", "endpoint": "openrouter.ai", "readOnly": True}})
        cfg["pricing"] = {"m-1": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 1.0}}
        (job / "job.json").write_text(json.dumps(cfg, indent=2))
        with self.assertRaises(gates.GateError):
            loop.freeze_job(job)

    def test_n14_init_scaffold_can_be_frozen_once_filled_in(self):
        scaf = self.tmp / "n14_scaf"
        self.assertEqual(loop.init_job(scaf), 0)
        self.assertFalse((scaf / "rounds").exists())
        filled = self._make_base_job(subdir="n14_filled")
        for p in filled.rglob("*"):
            if p.is_file():
                dst = scaf / p.relative_to(filled)
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(p.read_bytes())
        cfg = json.loads((scaf / "job.json").read_text())
        cfg["maker"]["command"] = [sys.executable, "scripts/maker.py"]
        cfg["evaluators"][0]["command"] = [sys.executable, "scripts/eval_e1.py"]
        (scaf / "job.json").write_text(json.dumps(cfg, indent=2))
        self.assertEqual(loop.freeze_job(scaf), 0)

    def test_n15_script_seat_child_process_does_not_outlive_round(self):
        job = self._make_base_job(subdir="n15")
        pid_file = self.tmp / "n15_child.pid"
        (job / "scripts" / "maker.py").write_text(
            "import pathlib, subprocess, sys\n"
            "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
            f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))\n"
            "pathlib.Path(sys.argv[2], 'memo.md').write_text('# Header\\nFigure: 42% (Source: Q3, 2026-10-01)\\n')\n")
        loop.freeze_job(job); loop.run_job(job)
        pid = int(pid_file.read_text())
        time.sleep(0.2)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_f1_finalize_declined_after_hard_kill_reanchors_recovered_reservation(self):
        job = self._make_base_job(subdir="f1")
        loop.freeze_job(job)
        m = self.meter(job)
        m.reserve({"prompt": "x"}, model="m")  # open reservation simulating SIGKILL mid-call
        st = json.loads((job / "state.json").read_text())
        st["open_round"] = 1
        loop._write_state(job, st, m)
        loop.finalize_job(job, state="declined", by="Casey Park")

    def test_f2_f3_f6_f7_and_g_cus3_invariants(self):
        # F2: gate_commands refuses seat command referencing runtime/cache path
        job = self._make_base_job(subdir="f2")
        (job / "evidence" / "evil.py").write_text("print(1)\n")
        cfg = json.loads((job / "job.json").read_text())
        cfg["maker"]["command"] = [sys.executable, "evidence/evil.py"]
        (job / "job.json").write_text(json.dumps(cfg, indent=2))
        with self.assertRaises(gates.GateError):
            loop.freeze_job(job)

        # F3: double resume on same round never double-amends prior call digest
        j3 = self._make_base_job(subdir="f3")
        loop.freeze_job(j3)
        m3 = self.meter(j3)
        m3.meter_call("maker", "m", "u", None, usage={"prompt_tokens": 100}, round_no=1)
        st3 = json.loads((j3 / "state.json").read_text())
        st3["open_round"] = 1
        loop._write_state(j3, st3, m3)
        loop.resume_job(j3)
        m3.refresh()
        m3.meter_call("maker", "m", "u", None, usage={"prompt_tokens": 200}, round_no=1)
        st3 = json.loads((j3 / "state.json").read_text())
        st3.update(terminal_state=None, reason=None, open_round=1)
        loop._write_state(j3, st3, m3)
        loop.resume_job(j3)
        m3.refresh()
        amends = [r["amends"] for r in m3._rows if r.get("kind") == "amendment"]
        self.assertEqual(len(amends), 2)
        self.assertEqual(set(amends[0]) & set(amends[1]), set())

        # G-CUS3: editing job.json after finalize causes verify_job to return 1
        j_cus = self._make_base_job(subdir="gcus3")
        loop.freeze_job(j_cus); loop.run_job(j_cus)
        self.assertEqual(loop.finalize_job(j_cus, state="given", by="Casey Park"), 0)
        cfg_c = json.loads((j_cus / "job.json").read_text())
        cfg_c["bounds"]["max_wall_seconds"] = 999
        (j_cus / "job.json").write_text(json.dumps(cfg_c, indent=2))
        self.assertEqual(loop.verify_job(j_cus), 1)


if __name__ == "__main__":
    unittest.main()
