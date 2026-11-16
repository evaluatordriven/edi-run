import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


class TestEDIRuntime(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="edi_test_"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_base_job(self, level="CL1", rounds_behavior="pass_r2", subdir="job"):
        """Create a self-contained job directory with deterministic script Maker and Evaluators."""
        job_dir = self.tmp / subdir
        if job_dir.exists():
            shutil.rmtree(job_dir)
        (job_dir / "held_out" / "controls").mkdir(parents=True)
        (job_dir / "held_out" / "planted").mkdir(parents=True)
        (job_dir / "evidence").mkdir(parents=True)
        (job_dir / "artifact").mkdir(parents=True)
        (job_dir / "scripts").mkdir(parents=True)

        bar_path = job_dir / "bar.md"
        bar_path.write_text("# Bar\n- D1: Has header\n- D2: Has cited figure\n", encoding="utf-8")
        bar_digest = sha256_bytes(bar_path.read_bytes())

        rubric_path = job_dir / "rubric.md"
        rubric_path.write_text("# Rubric\nCheck D1 header and D2 figure.\n", encoding="utf-8")
        method_path = job_dir / "method.md"
        method_path.write_text("# Method\nDraft memo satisfying D1 and D2.\n", encoding="utf-8")
        runner_path = job_dir / "runner.md"
        runner_path.write_text("# Runner\nedi-run v1\n", encoding="utf-8")

        clean_ctl = job_dir / "held_out" / "controls" / "clean.md"
        clean_ctl.write_text("# Header\nFigure: 42% (Source: Q3 Report, 2026-10-01)\n", encoding="utf-8")
        planted_ctl = job_dir / "held_out" / "controls" / "planted.md"
        planted_ctl.write_text("No header and no figure here.\n", encoding="utf-8")

        # Maker script: reads packet.json and writes artifact/memo.md
        maker_script = job_dir / "scripts" / "maker.py"
        maker_script.write_text(
            "import json, pathlib, sys\n"
            "pkt = json.loads(pathlib.Path(sys.argv[1]).read_text())\n"
            "out = pathlib.Path(sys.argv[2]) / 'memo.md'\n"
            "r = pkt['round']\n"
            "mode = pkt.get('mode', 'pass_r2')\n"
            "if mode == 'pass_r2':\n"
            "    txt = '# Header\\nNo figure yet.\\n' if r == 1 else '# Header\\nFigure: 42% (Source: Q3, 2026-10-01)\\n'\n"
            "elif mode == 'dormant':\n"
            "    txt = '# Header\\nNo figure yet.\\n'\n"
            "elif mode == 'tradeoff':\n"
            "    txt = '# Header\\nNo figure yet.\\n' if r == 1 else 'No header\\nFigure: 42% (Source: Q3, 2026-10-01)\\n'\n"
            "elif mode == 'regress_r2':\n"
            "    txt = '# Header\\nNo figure yet.\\n' if r == 1 else 'Broken both\\n'\n"
            "out.write_text(txt, encoding='utf-8')\n",
            encoding="utf-8",
        )

        # Evaluator script E1: checks D1 and D2
        eval_script = job_dir / "scripts" / "eval_e1.py"
        eval_script.write_text(
            "import json, pathlib, sys\n"
            "target = pathlib.Path(sys.argv[1])\n"
            "dim = sys.argv[2]\n"
            "fail_ctl = '--broken-control' in sys.argv\n"
            "txt = (target / 'memo.md').read_text() if target.is_dir() else target.read_text()\n"
            "if fail_ctl and 'No header and no figure' in txt:\n"
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
            "controls": {
                "clean": "held_out/controls/clean.md",
                "planted": "held_out/controls/planted.md",
            },
            "retention": {"location": "local/records/test-memo-job", "public": False},
            "disposition": {"state": "given", "by": "Casey Park", "acknowledgesUnobserved": []},
        }
        (job_dir / "job.json").write_text(json.dumps(job_cfg, indent=2), encoding="utf-8")
        return job_dir

    def test_i01_freeze_ordering_and_approvals(self):
        import gates
        job_dir = self._make_base_job()
        cfg = json.loads((job_dir / "job.json").read_text())
        # Negative 1: bar frozenAt >= startedAt on CL1
        with self.assertRaises(gates.GateError):
            gates.gate_freeze(cfg, job_dir, started_at="2026-10-05T08:00:00Z")
        # Negative 2: missing specialist approval
        bad_cfg = json.loads(json.dumps(cfg))
        bad_cfg["bar"]["approvals"] = [a for a in bad_cfg["bar"]["approvals"] if a["role"] != "specialist"]
        with self.assertRaises(gates.GateError):
            gates.gate_freeze(bad_cfg, job_dir, started_at="2026-10-05T10:00:00Z")
        # Positive
        gates.gate_freeze(cfg, job_dir, started_at="2026-10-05T10:00:00Z")

    def test_i03_seat_separation_and_cl2_model_family(self):
        import gates
        job_dir = self._make_base_job(level="CL2")
        cfg = json.loads((job_dir / "job.json").read_text())
        # Negative 1: Maker == Evaluator
        bad1 = json.loads(json.dumps(cfg))
        bad1["evaluators"][0]["name"] = bad1["maker"]["name"]
        with self.assertRaises(gates.GateError):
            gates.gate_seats(bad1, job_dir)

        # Negative 2: CL2 model Maker and model Evaluator share family/endpoint
        bad2 = json.loads(json.dumps(cfg))
        bad2["maker"]["kind"] = "model"
        bad2["maker"]["model"] = {"family": "gpt", "identifier": "gpt-4o", "endpoint": "api.openai.com", "readOnly": False}
        bad2["evaluators"] = [{
            "id": "E1", "name": "EvalModel", "kind": "model", "script": None,
            "model": {"family": "gpt", "identifier": "gpt-4o-mini", "endpoint": "api.openai.com", "readOnly": True},
        }]
        with self.assertRaises(gates.GateError):
            gates.gate_seats(bad2, job_dir)

        # Positive CL2: model Evaluator differs in both family and endpoint
        good2 = json.loads(json.dumps(bad2))
        good2["evaluators"][0]["model"] = {"family": "claude", "identifier": "claude-3-7-sonnet", "endpoint": "api.anthropic.com", "readOnly": True}
        gates.gate_seats(good2, job_dir)

    def test_i04_bounds_and_held_out_leak_prevention(self):
        import gates
        job_dir = self._make_base_job()
        cfg = json.loads((job_dir / "job.json").read_text())
        # Negative bounds: missing max_cost_usd
        bad_bounds = json.loads(json.dumps(cfg))
        del bad_bounds["bounds"]["max_cost_usd"]
        with self.assertRaises(gates.GateError):
            gates.gate_bounds(bad_bounds)

        # Negative packet: contains held-out path or sha256 digest
        clean_hash = sha256_bytes((job_dir / "held_out" / "controls" / "clean.md").read_bytes())
        leaked_pkt = {"round": 1, "best_artifact_digest": None, "defects": [f"Failed on {clean_hash}"]}
        with self.assertRaises(gates.GateError):
            gates.gate_packet(leaked_pkt, job_dir, expected_incumbent_digest=None)

        leaked_path_pkt = {"round": 1, "best_artifact_digest": None, "defects": ["See held_out/controls/clean.md"]}
        with self.assertRaises(gates.GateError):
            gates.gate_packet(leaked_path_pkt, job_dir, expected_incumbent_digest=None)

    def test_i02_i05_failed_control_voids_evaluator_checks_to_unobserved(self):
        import loop
        job_dir = self._make_base_job()
        cfg = json.loads((job_dir / "job.json").read_text())
        cfg["evaluators"][0]["command"].append("--broken-control")
        cfg["disposition"]["acknowledgesUnobserved"] = ["D1", "D2"]
        (job_dir / "job.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")

        loop.freeze_job(job_dir)
        rc = loop.run_job(job_dir)
        self.assertEqual(rc, 2)  # needs_owner due to unobserved checks
        rc_fin = loop.finalize_job(job_dir, state="given", by="Casey Park", ack_unobserved=["D1", "D2"])
        self.assertEqual(rc_fin, 0)
        rec = json.loads((job_dir / "record.json").read_text())
        for c in rec["predicate"]["checks"]:
            self.assertEqual(c["outcome"], "unobserved")
            self.assertIsNone(c["score"])

    def test_i06_pareto_tradeoff_and_dormant_channel(self):
        import loop
        # Case 1: Dormant channel (Round 2 produces identical sha256 to Round 1)
        job_dormant = self._make_base_job(rounds_behavior="dormant")
        loop.freeze_job(job_dormant)
        rc1 = loop.run_job(job_dormant)
        self.assertEqual(rc1, 2)
        state1 = json.loads((job_dormant / "state.json").read_text())
        self.assertEqual(state1["terminal_state"], "needs_owner")
        self.assertEqual(state1["reason"], "dormant_channel")

        # Case 2: Pareto trade-off (Round 2 fixes D2 but breaks D1)
        job_trade = self._make_base_job(rounds_behavior="tradeoff", subdir="job_trade")
        loop.freeze_job(job_trade)
        rc2 = loop.run_job(job_trade)
        self.assertEqual(rc2, 2)
        state2 = json.loads((job_trade / "state.json").read_text())
        self.assertEqual(state2["terminal_state"], "needs_owner")
        self.assertEqual(state2["reason"], "dimension_tradeoff")
        # Incumbent must remain Round 1
        self.assertEqual(state2["incumbent_round"], 1)

    def test_i07_incumbent_promotion_when_later_round_regresses(self):
        import loop
        job_reg = self._make_base_job(rounds_behavior="regress_r2")
        loop.freeze_job(job_reg)
        rc = loop.run_job(job_reg)
        self.assertEqual(rc, 2)  # cap reached, neither passed all, R1 > R2
        state = json.loads((job_reg / "state.json").read_text())
        self.assertEqual(state["incumbent_round"], 1)
        # Finalize with not_yet_given -> check.py should verify OK (verdict OK, routing 'to approver')
        rc_fin = loop.finalize_job(job_reg, state="not_yet_given", by="Casey Park")
        self.assertEqual(rc_fin, 0)
        # Artifact on disk must be Round 1's memo.md ('# Header\nNo figure yet.\n')
        self.assertTrue((job_reg / "artifact" / "memo.md").read_text().startswith("# Header"))

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


if __name__ == "__main__":
    unittest.main()
