import hashlib
import http.server
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import adapters  # noqa: E402
import gates  # noqa: E402
import loop  # noqa: E402
import check as custody_check  # vendored custody checker; resolves via loop.py's vendor path insert  # noqa: E402


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class _MockLLMHandler(http.server.BaseHTTPRequestHandler):
    mode = "ok"

    def log_message(self, format, *args):
        return

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        if self.__class__.mode == "error":
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"internal error")
            return
        usr = body["messages"][-1]["content"]
        if "ROUND PACKET" in usr:
            reply = '```json\n{"files": {"memo.md": "# Header\\nFigure: 42% (Source: Q3, 2026-10-01)\\n"}}\n```'
        else:
            score = 5.0 if ("# Header" in usr and "Source:" in usr) or ("# Header" in usr and '"id": "D1"' in usr) else 1.0
            reply = json.dumps({"score": score, "observation": "quoted text", "remedy": "fix it" if score < 4.0 else ""})
        resp = {"model": body.get("model", "mock-model"), "usage": {"total_tokens": 10}, "choices": [{"message": {"content": reply}}]}
        raw = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class TestEDIRuntime(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="edi_test_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_base_job(self, level="CL1", rounds_behavior="pass_r2", subdir="job"):
        job_dir = self.tmp / subdir
        if job_dir.exists():
            shutil.rmtree(job_dir)
        for d in ("held_out/controls", "held_out/planted", "evidence", "artifact", "scripts"):
            (job_dir / d).mkdir(parents=True)

        bar_path = job_dir / "bar.md"
        bar_path.write_text("# Bar\n- D1: Has header\n- D2: Has cited figure\n", encoding="utf-8")
        bar_digest = sha256_bytes(bar_path.read_bytes())

        (job_dir / "rubric.md").write_text("# Rubric\nCheck D1 header and D2 figure.\n", encoding="utf-8")
        (job_dir / "method.md").write_text("# Method\nDraft memo satisfying D1 and D2.\n", encoding="utf-8")
        (job_dir / "runner.md").write_text("# Runner\nedi-run v1\n", encoding="utf-8")

        (job_dir / "held_out/controls/clean.md").write_text(
            "# Header\nHeld-out reference control figure: 99% (Source: Control Audit, 2026-01-01)\n", encoding="utf-8"
        )
        (job_dir / "held_out/controls/planted.md").write_text("No header and no figure here.\n", encoding="utf-8")

        maker_script = job_dir / "scripts" / "maker.py"
        maker_script.write_text(
            "import json, pathlib, sys, time\n"
            "pkt = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
            "out = pathlib.Path(sys.argv[2]) / 'memo.md'\n"
            "r = pkt['round']\n"
            "mode = pkt.get('mode', 'pass_r2')\n"
            "if pathlib.Path('held_out').exists() or pathlib.Path('../held_out').exists():\n"
            "    sys.exit('Maker sandbox leak: held_out visible')\n"
            "if mode == 'pass_r2':\n"
            "    txt = '# Header\\nNo figure yet.\\n' if r == 1 else '# Header\\nFigure: 42% (Source: Q3, 2026-10-01)\\n'\n"
            "elif mode == 'dormant':\n"
            "    txt = '# Header\\nNo figure yet.\\n'\n"
            "elif mode == 'tradeoff':\n"
            "    txt = '# Header\\nNo figure yet.\\n' if r == 1 else 'No header\\nFigure: 42% (Source: Q3, 2026-10-01)\\n'\n"
            "elif mode == 'regress_r2':\n"
            "    txt = '# Header\\nNo figure yet.\\n' if r == 1 else 'Broken both\\n'\n"
            "elif mode == 'no_artifact':\n"
            "    sys.exit(0)\n"
            "elif mode == 'symlink_attack':\n"
            "    out.symlink_to(pathlib.Path(sys.argv[1]))\n"
            "    sys.exit(0)\n"
            "elif mode == 'slow':\n"
            "    time.sleep(5)\n"
            "    txt = '# Header\\n'\n"
            "out.write_text(txt, encoding='utf-8')\n",
            encoding="utf-8",
        )

        eval_script = job_dir / "scripts" / "eval_e1.py"
        eval_script.write_text(
            "import json, pathlib, sys\n"
            "pos = [a for a in sys.argv[1:] if not a.startswith('--')]\n"
            "target, dim = pathlib.Path(pos[0]), pos[1]\n"
            "fail_ctl = '--broken-control' in sys.argv\n"
            "always_pass = '--always-pass' in sys.argv\n"
            "always_low = '--always-low' in sys.argv\n"
            "txt = (target / 'memo.md').read_text() if target.is_dir() else target.read_text()\n"
            "if always_pass:\n"
            "    score = 5.0\n"
            "elif always_low:\n"
            "    score = 1.0 if ('Control Audit' not in txt and 'No header and no figure' not in txt) else (5.0 if 'Control Audit' in txt else 1.0)\n"
            "elif fail_ctl and 'No header and no figure' in txt:\n"
            "    score = 5.0\n"
            "elif dim == 'D1':\n"
            "    score = 5.0 if txt.startswith('# Header') else 1.0\n"
            "else:\n"
            "    score = 5.0 if 'Source:' in txt else 1.0\n"
            "print(json.dumps({'score': score, 'observation': txt.strip()[:60], 'remedy': 'Add missing element' if score < 4.0 else ''}))\n",
            encoding="utf-8",
        )

        scale = {"anchors": {"1": "missing", "5": "present"}, "floor": 0.0, "threshold": 4.0}
        job_cfg = {
            "job": "test-memo-job",
            "levelClaimed": level,
            "specVersion": "0.9.8",
            "bounds": {"max_rounds": 2, "max_cost_usd": 1.0, "max_wall_seconds": 60},
            "mode": rounds_behavior,
            "subjectFiles": ["memo.md"],
            "bar": {
                "file": "bar.md",
                "digest": {"sha256": bar_digest},
                "frozenAt": "2026-10-05T09:00:00Z",
                "author": "Jordan Blake",
                "dimensions": [
                    {"id": "D1", "criterion": "Starts with # Header", "scale": scale},
                    {"id": "D2", "criterion": "Cites Source:", "scale": scale},
                ],
                "approvals": [
                    {"role": "buyer", "by": "Casey Park", "barDigest": {"sha256": bar_digest}, "at": "2026-10-05T08:40:00Z", "adopted": None},
                    {"role": "specialist", "by": "Riley Moss", "barDigest": {"sha256": bar_digest}, "at": "2026-10-05T08:50:00Z", "adopted": None},
                ],
                "publishedAt": "https://example.com/bar/v1" if level == "CL3" else None,
                "groundTruth": [],
                "dissent": [],
            },
            "maker": {
                "name": "Alex Kim",
                "kind": "script",
                "version": "1.0.0",
                "method": {"file": "method.md"},
                "model": None,
                "command": [sys.executable, str(maker_script)],
            },
            "instrument": {"rubric": {"file": "rubric.md"}, "runner": {"file": "runner.md"}},
            "evaluators": [
                {
                    "id": "E1",
                    "name": "Taylor Grant",
                    "kind": "script",
                    "model": None,
                    "script": {
                        "name": "eval_e1.py",
                        "author": "Morgan Lee",
                        "digest": {"sha256": sha256_bytes(eval_script.read_bytes())},
                    },
                    "command": [sys.executable, str(eval_script)],
                    "runs": 2 if level in ("CL2", "CL3") else 1,
                }
            ],
            "controls": {"clean": "held_out/controls/clean.md", "planted": "held_out/controls/planted.md"},
            "retention": {"location": "local/records/test-memo-job", "public": False},
            "disposition": {"state": "given", "by": "Casey Park", "acknowledgesUnobserved": []},
        }
        (job_dir / "job.json").write_text(json.dumps(job_cfg, indent=2), encoding="utf-8")
        return job_dir

    # --- Unit Tests: gates.py ---

    def test_unit_safe_rel_and_sha256_file_reject_traversal_and_symlinks(self):
        f = self.tmp / "regular.txt"
        f.write_text("hello", encoding="utf-8")
        self.assertEqual(gates.safe_rel("regular.txt", self.tmp), f.resolve())
        for bad in ("", "   ", "/etc/passwd", "../escape.txt", "a/../../escape.txt"):
            with self.assertRaises(gates.GateError):
                gates.safe_rel(bad, self.tmp)
        sym = self.tmp / "link.txt"
        sym.symlink_to(f)
        with self.assertRaises(gates.GateError):
            gates.sha256_file(sym)
        with self.assertRaises(gates.GateError):
            gates.sha256_file(self.tmp / "nonexistent.txt")

    def test_unit_gate_bounds_edge_cases(self):
        job_dir = self._make_base_job()
        cfg = json.loads((job_dir / "job.json").read_text())
        for bad_rounds in (0, -1, 1.5, True, "2", None):
            c = json.loads(json.dumps(cfg))
            c["bounds"]["max_rounds"] = bad_rounds
            with self.assertRaises(gates.GateError):
                gates.gate_bounds(c)
        for k in ("max_cost_usd", "max_wall_seconds"):
            for bad_val in (0, -5.0, float("inf"), float("nan"), False, None):
                c = json.loads(json.dumps(cfg))
                c["bounds"][k] = bad_val
                with self.assertRaises(gates.GateError):
                    gates.gate_bounds(c)
        c = json.loads(json.dumps(cfg))
        c["subjectFiles"] = ["../secret.md"]
        with self.assertRaises(gates.GateError):
            gates.gate_bounds(c)

    def test_i01_freeze_ordering_approvals_and_baseline_entry_b(self):
        job_dir = self._make_base_job()
        cfg = json.loads((job_dir / "job.json").read_text())
        with self.assertRaises(gates.GateError):
            gates.gate_freeze(cfg, job_dir, started_at="2026-10-05T08:00:00Z")
        with self.assertRaises(gates.GateError):
            gates.gate_freeze(cfg, job_dir, started_at="2026-10-05T09:00:00Z")
        for missing_role in ("buyer", "specialist"):
            bad = json.loads(json.dumps(cfg))
            bad["bar"]["approvals"] = [a for a in bad["bar"]["approvals"] if a["role"] != missing_role]
            with self.assertRaises(gates.GateError):
                gates.gate_freeze(bad, job_dir, started_at="2026-10-05T10:00:00Z")
        base_cfg = json.loads(json.dumps(cfg))
        base_cfg["levelClaimed"] = "baseline"
        with self.assertRaises(gates.GateError):
            gates.gate_freeze(base_cfg, job_dir, started_at="2026-10-05T08:00:00Z")
        base_cfg["readsBeforeFreeze"] = ["memo.md"]
        with self.assertRaises(gates.GateError):
            gates.gate_freeze(base_cfg, job_dir, started_at="2026-10-05T10:00:00Z")
        gates.gate_freeze(base_cfg, job_dir, started_at="2026-10-05T08:00:00Z")

    def test_i03_seat_separation_and_cl2_model_family_matrix(self):
        job_dir = self._make_base_job(level="CL2")
        cfg = json.loads((job_dir / "job.json").read_text())
        bad1 = json.loads(json.dumps(cfg))
        bad1["evaluators"][0]["name"] = bad1["maker"]["name"]
        with self.assertRaises(gates.GateError):
            gates.gate_seats(bad1, job_dir)
        bad_author = json.loads(json.dumps(cfg))
        bad_author["evaluators"][0]["script"]["author"] = bad_author["bar"]["author"]
        with self.assertRaises(gates.GateError):
            gates.gate_seats(bad_author, job_dir)
        bad_runner = json.loads(json.dumps(cfg))
        bad_runner["evaluators"][0]["script"]["digest"]["sha256"] = gates.sha256_file(job_dir / "runner.md")
        with self.assertRaises(gates.GateError):
            gates.gate_seats(bad_runner, job_dir)
        with self.assertRaises(gates.GateError):
            gates.gate_seats(cfg, job_dir, approver_by="Taylor Grant")
        bad_bar_mk = json.loads(json.dumps(cfg))
        bad_bar_mk["bar"]["author"] = bad_bar_mk["maker"]["name"]
        with self.assertRaises(gates.GateError):
            gates.gate_seats(bad_bar_mk, job_dir)
        bad_same = json.loads(json.dumps(cfg))
        bad_same["levelClaimed"] = "CL1"
        bad_same["maker"]["kind"] = "model"
        bad_same["maker"]["model"] = {"family": "gpt", "identifier": "gpt-4o", "endpoint": "api.openai.com", "readOnly": False}
        bad_same["evaluators"] = [{
            "id": "E1", "name": "EvalModel", "kind": "model", "script": None,
            "model": {"family": "gpt", "identifier": "gpt-4o", "endpoint": "api.openai.com", "readOnly": True},
        }]
        with self.assertRaises(gates.GateError):
            gates.gate_seats(bad_same, job_dir)
        bad_same["evaluators"][0]["sameModelAsMaker"] = True
        gates.gate_seats(bad_same, job_dir)
        undisc = json.loads(json.dumps(cfg))
        undisc["maker"]["kind"] = "model"
        undisc["maker"]["model"] = {"family": "undisclosed", "identifier": "anon-1", "endpoint": "api.a.com", "readOnly": False}
        undisc["evaluators"] = [{
            "id": "E1", "name": "Eval1", "kind": "model", "script": None,
            "model": {"family": "claude", "identifier": "c1", "endpoint": "api.b.com", "readOnly": True},
        }]
        with self.assertRaises(gates.GateError):
            gates.gate_seats(undisc, job_dir)
        undisc["evaluators"].append({
            "id": "E2", "name": "Eval2", "kind": "model", "script": None,
            "model": {"family": "gemini", "identifier": "g1", "endpoint": "api.c.com", "readOnly": True},
        })
        gates.gate_seats(undisc, job_dir)

    def test_i04_held_out_leak_and_incumbent_mismatch(self):
        job_dir = self._make_base_job()
        with self.assertRaises(gates.GateError):
            gates.gate_packet({"round": 2, "best_artifact_digest": "wrong"}, job_dir, expected_incumbent_digest="expected")
        clean_hash = sha256_bytes((job_dir / "held_out/controls/clean.md").read_bytes())
        with self.assertRaises(gates.GateError):
            gates.gate_packet({"round": 1, "best_artifact_digest": None, "defects": [clean_hash.upper()]}, job_dir, None)
        with self.assertRaises(gates.GateError):
            gates.gate_packet({"round": 1, "best_artifact_digest": None, "defects": ["Held-out reference control figure: 99%"]}, job_dir, None)

    # --- Unit Tests: adapters.py (HTTP loopback + sandbox isolation) ---

    def test_unit_http_model_adapter_and_error_fallback(self):
        server = http.server.HTTPServer(("127.0.0.1", 0), _MockLLMHandler)
        port = server.server_port
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        try:
            job_dir = self._make_base_job()
            url = f"http://127.0.0.1:{port}/v1/chat/completions"
            maker_cfg = {"name": "HttpMaker", "kind": "model", "version": "1", "method": {"file": "method.md"},
                         "url": url, "model": {"family": "mock", "identifier": "mock-1", "endpoint": "127.0.0.1", "readOnly": False}}
            pkt_p = job_dir / "pkt.json"
            pkt_p.write_text(json.dumps({"round": 1, "defects": [], "workable_solutions": []}), encoding="utf-8")
            out_dir = job_dir / "http_out"
            adapters.run_maker(maker_cfg, pkt_p, out_dir, ["memo.md"], job_dir)
            self.assertIn("Source: Q3", (out_dir / "memo.md").read_text())

            eval_cfg = {"id": "EH", "name": "HttpEval", "kind": "model", "url": url,
                        "model": {"family": "mock", "identifier": "mock-eval", "endpoint": "127.0.0.1", "readOnly": True}}
            har_p = job_dir / "evidence" / "har.json"
            res = adapters.run_evaluator(eval_cfg, out_dir, "D1", job_dir=job_dir, harness_out=har_p)
            self.assertEqual(res["score"], 5.0)
            self.assertEqual(res["_endpoint"], "127.0.0.1")
            self.assertTrue(har_p.is_file())

            _MockLLMHandler.mode = "error"
            har_err = job_dir / "evidence" / "har_err.json"
            res_err = adapters.run_evaluator(eval_cfg, out_dir, "D1", job_dir=job_dir, harness_out=har_err)
            self.assertIn("unobserved", res_err)
            self.assertTrue(har_err.is_file())
        finally:
            _MockLLMHandler.mode = "ok"
            server.shutdown()
            server.server_close()

    def test_unit_maker_symlink_attack_rejected(self):
        job_dir = self._make_base_job(rounds_behavior="symlink_attack")
        loop.freeze_job(job_dir)
        with self.assertRaises(gates.GateError):
            loop.run_job(job_dir)

    # --- Unit & Integration Tests: loop.py state machine & edge cases ---

    def test_i02_i05_failed_control_voids_evaluator_checks_to_unobserved(self):
        job_dir = self._make_base_job()
        cfg = json.loads((job_dir / "job.json").read_text())
        cfg["evaluators"][0]["command"].append("--broken-control")
        cfg["disposition"]["acknowledgesUnobserved"] = ["D1", "D2"]
        (job_dir / "job.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")

        loop.freeze_job(job_dir)
        self.assertEqual(loop.run_job(job_dir), 2)
        self.assertEqual(loop.finalize_job(job_dir, state="given", by="Casey Park", ack_unobserved=["D1", "D2"]), 0)
        rec = json.loads((job_dir / "record.json").read_text())
        self.assertTrue(all(c["outcome"] == "unobserved" and c["score"] is None for c in rec["predicate"]["checks"]))

    def test_i06_pareto_tradeoff_and_dormant_channel(self):
        job_dormant = self._make_base_job(rounds_behavior="dormant")
        loop.freeze_job(job_dormant)
        self.assertEqual(loop.run_job(job_dormant), 2)
        st1 = json.loads((job_dormant / "state.json").read_text())
        self.assertEqual((st1["terminal_state"], st1["reason"]), ("needs_owner", "dormant_channel"))

        job_trade = self._make_base_job(rounds_behavior="tradeoff", subdir="job_trade")
        loop.freeze_job(job_trade)
        self.assertEqual(loop.run_job(job_trade), 2)
        st2 = json.loads((job_trade / "state.json").read_text())
        self.assertEqual((st2["terminal_state"], st2["reason"], st2["incumbent_round"]), ("needs_owner", "dimension_tradeoff", 1))

    def test_i07_incumbent_promotion_when_later_round_regresses(self):
        job_reg = self._make_base_job(rounds_behavior="regress_r2")
        loop.freeze_job(job_reg)
        self.assertEqual(loop.run_job(job_reg), 2)
        self.assertEqual(json.loads((job_reg / "state.json").read_text())["incumbent_round"], 1)
        self.assertEqual(loop.finalize_job(job_reg, state="not_yet_given", by="Casey Park"), 0)
        self.assertTrue((job_reg / "artifact" / "memo.md").read_text().startswith("# Header"))

    def test_unit_rejected_below_floor_and_no_artifact_and_timeout(self):
        job_rej = self._make_base_job(subdir="job_rej")
        cfg = json.loads((job_rej / "job.json").read_text())
        cfg["rejectFloor"] = 2.0
        (job_rej / "job.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        loop.freeze_job(job_rej)
        self.assertEqual(loop.run_job(job_rej), 1)
        st_rej = json.loads((job_rej / "state.json").read_text())
        self.assertEqual((st_rej["terminal_state"], st_rej["reason"]), ("rejected", "below_floor"))

        job_no = self._make_base_job(rounds_behavior="no_artifact", subdir="job_no")
        loop.freeze_job(job_no)
        self.assertEqual(loop.run_job(job_no), 2)
        st_no = json.loads((job_no / "state.json").read_text())
        self.assertEqual((st_no["terminal_state"], st_no["reason"]), ("needs_owner", "no_artifact"))
        # §6: an incomplete run emits a gap-listed record instead of raising
        self.assertEqual(loop.finalize_job(job_no, state="declined", by="Casey Park"), 1)
        rec = json.loads((job_no / "record.json").read_text())
        invs = {g["invariant"] for g in rec["x-edi-run"]["gaps"]}
        self.assertIn("I-06", invs)
        self.assertEqual(rec["x-edi-run"]["terminalState"], "needs_owner")
        self.assertEqual(rec["x-edi-run"]["reason"], "no_artifact")

        job_slow = self._make_base_job(rounds_behavior="slow", subdir="job_slow")
        cfg_s = json.loads((job_slow / "job.json").read_text())
        cfg_s["bounds"]["max_wall_seconds"] = 1.0
        (job_slow / "job.json").write_text(json.dumps(cfg_s, indent=2), encoding="utf-8")
        loop.freeze_job(job_slow)
        self.assertEqual(loop.run_job(job_slow), 2)
        st_s = json.loads((job_slow / "state.json").read_text())
        self.assertEqual((st_s["terminal_state"], st_s["reason"]), ("needs_owner", "cap_reached"))

    def test_gap_emission_assembly_failure_and_emitter_can_fail(self):
        # §6 path 2: inputs assembly failure becomes a gap-listed record, exit 1.
        job_rej = self._make_base_job(subdir="job_gap_asm")
        cfg = json.loads((job_rej / "job.json").read_text())
        cfg["rejectFloor"] = 2.0
        (job_rej / "job.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        loop.freeze_job(job_rej)
        self.assertEqual(loop.run_job(job_rej), 1)
        cfg = json.loads((job_rej / "job.json").read_text())
        del cfg["retention"]  # required custody field -> assembly fails
        (job_rej / "job.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        self.assertEqual(loop.finalize_job(job_rej, state="given", by="Casey Park"), 1)
        rec = json.loads((job_rej / "record.json").read_text())
        self.assertEqual(rec["recordType"], "edi-run/gap-record")
        asm = [g for g in rec["x-edi-run"]["gaps"] if g["invariant"] == "custody-assembly"]
        self.assertEqual(len(asm), 1)
        self.assertIn("retention", asm[0]["missing"])
        # provenance survives: the disposition event is in the custody log
        events = [json.loads(line) for line in (job_rej / "job.jsonl").read_text().splitlines()]
        self.assertTrue(any(e["type"] == "disposition" for e in events))
        # check.py rejects any non-custody document, so the emitter's own output can
        # never read OK -- the guarantee lives in the no-predicate construction.
        doctored = dict(rec)
        doctored["x-edi-run"] = dict(rec["x-edi-run"], gaps=[])
        rep = custody_check.verify(doctored, files=str(job_rej / "artifact"),
                                   evidence=str(job_rej / "evidence"),
                                   bar=str(job_rej / "bar.md"))
        self.assertNotEqual(rep["verdict"], "OK")
        # The emitter can fail: if a gap-listed record ever verifies OK, runtime raises.
        st_gap = json.loads((job_rej / "state.json").read_text())
        cfg_gap = json.loads((job_rej / "job.json").read_text())
        orig_verify = loop.custody_check.verify
        loop.custody_check.verify = lambda *a, **k: {"verdict": "OK"}
        try:
            with self.assertRaises(gates.GateError):
                loop._emit_gap_record(job_rej, st_gap, cfg_gap)
        finally:
            loop.custody_check.verify = orig_verify

    def test_integrity_tampered_log_hard_fails_never_gap_record(self):
        # Frontier-review blocker fix: a broken custody chain must raise, never
        # downgrade to a gap-listed record.
        job_ok = self._make_base_job(subdir="job_tamper")
        loop.freeze_job(job_ok)
        loop.run_job(job_ok)
        events = (job_ok / "job.jsonl").read_text().splitlines()
        tampered = json.loads(events[1])
        tampered["data"] = {"job": "FORGED"}
        events[1] = json.dumps(tampered)
        (job_ok / "job.jsonl").write_text("\n".join(events) + "\n", encoding="utf-8")
        before = (job_ok / "record.json").exists()
        with self.assertRaises(gates.GateError):
            loop.finalize_job(job_ok, state="given", by="Casey Park")
        self.assertFalse(before)  # no record existed; finalize must not write one

    def test_finalize_runs_once_double_disposition_refused(self):
        job_ok = self._make_base_job(subdir="job_once")
        loop.freeze_job(job_ok)
        loop.run_job(job_ok)
        self.assertEqual(loop.finalize_job(job_ok, state="given", by="Casey Park"), 0)
        with self.assertRaises(gates.GateError):
            loop.finalize_job(job_ok, state="declined", by="Casey Park")
        events = [json.loads(line) for line in (job_ok / "job.jsonl").read_text().splitlines()]
        self.assertEqual(sum(1 for e in events if e["type"] == "disposition"), 1)

    def test_unit_post_freeze_instrument_tamper_detected(self):
        job_dir = self._make_base_job()
        loop.freeze_job(job_dir)
        (job_dir / "rubric.md").write_text("# Tampered rubric\n", encoding="utf-8")
        with self.assertRaises(gates.GateError):
            loop.run_job(job_dir)

    def test_unit_baseline_entry_b_end_to_end(self):
        job_bl = self._make_base_job(level="baseline", subdir="job_bl")
        cfg = json.loads((job_bl / "job.json").read_text())
        cfg["readsBeforeFreeze"] = ["memo.md"]
        (job_bl / "job.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        self.assertEqual(loop.freeze_job(job_bl), 0)
        self.assertEqual(loop.run_job(job_bl), 0)
        self.assertEqual(loop.finalize_job(job_bl, state="given", by="Casey Park"), 0)
        rec = json.loads((job_bl / "record.json").read_text())
        self.assertEqual(rec["predicate"]["levelClaimed"], "baseline")

    def test_unit_cl3_demonstrations_and_lenient_evaluator_rejection(self):
        job_bad_cl3 = self._make_base_job(level="CL3", subdir="cl3_bad")
        cfg = json.loads((job_bad_cl3 / "job.json").read_text())
        cfg["evaluators"][0]["command"].append("--always-pass")
        (job_bad_cl3 / "job.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        with self.assertRaises(gates.GateError):
            loop.freeze_job(job_bad_cl3)

        job_cl3 = self._make_base_job(level="CL3", subdir="cl3_ok")
        self.assertEqual(loop.freeze_job(job_cl3), 0)
        st = json.loads((job_cl3 / "state.json").read_text())
        self.assertEqual(len(st["demonstrations"]), 2)

    def test_unit_multi_evaluator_disagreement_recording_cl1_10(self):
        job_dis = self._make_base_job(subdir="job_dis")
        cfg = json.loads((job_dis / "job.json").read_text())
        cfg["bounds"]["max_rounds"] = 1
        eval_s2 = job_dis / "scripts" / "eval_e2.py"
        shutil.copy2(job_dis / "scripts" / "eval_e1.py", eval_s2)
        cfg["evaluators"].append({
            "id": "E2", "name": "SecondEval", "kind": "script", "model": None,
            "script": {"name": "eval_e2.py", "author": "Samira Patel", "digest": {"sha256": sha256_bytes(eval_s2.read_bytes())}},
            "command": [sys.executable, str(eval_s2), "--always-low"], "runs": 1,
        })
        (job_dis / "job.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        loop.freeze_job(job_dis)
        self.assertEqual(loop.run_job(job_dis), 2)
        self.assertEqual(loop.finalize_job(job_dis, state="not_yet_given", by="Casey Park"), 0)
        rec = json.loads((job_dis / "record.json").read_text())
        self.assertEqual(len(rec["predicate"]["disagreements"]), 1)
        self.assertEqual(rec["predicate"]["disagreements"][0]["dimension"], "D1")

    def test_end_to_end_two_round_acceptance_and_custody_verification(self):
        job_ok = self._make_base_job(level="CL1", rounds_behavior="pass_r2")
        for cmd, exp in (
            ([sys.executable, str(ROOT / "edi.py"), "freeze", str(job_ok)], 0),
            ([sys.executable, str(ROOT / "edi.py"), "run", str(job_ok)], 0),
            ([sys.executable, str(ROOT / "edi.py"), "finalize", str(job_ok), "--disposition", "given", "--by", "Casey Park"], 0),
            ([sys.executable, str(ROOT / "edi.py"), "verify", str(job_ok)], 0),
        ):
            proc = subprocess.run(cmd, capture_output=True, text=True)
            self.assertEqual(proc.returncode, exp, f"cmd {cmd} failed:\nSTDOUT:{proc.stdout}\nSTDERR:{proc.stderr}")
        rec = json.loads((job_ok / "record.json").read_text())
        subj_hashes = {s["digest"]["sha256"] for s in rec["subject"]}
        ctl_hashes = {k["case"]["sha256"] for k in rec["predicate"]["controls"]}
        self.assertTrue(subj_hashes.isdisjoint(ctl_hashes))

    def test_committed_examples_verify(self):
        for ex, want_level in (("two-round-job", "CL1"), ("live-llm-job", "CL1"), ("live-cl2-job", "CL2")):
            ex_dir = ROOT / "examples" / ex
            proc = subprocess.run([sys.executable, str(ROOT / "edi.py"), "verify", str(ex_dir)], cwd=str(ROOT), capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, f"{ex} failed verify: {proc.stderr}")
            rec = json.loads((ex_dir / "record.json").read_text())
            self.assertEqual(rec["predicate"]["levelClaimed"], want_level)


if __name__ == "__main__":
    unittest.main()
