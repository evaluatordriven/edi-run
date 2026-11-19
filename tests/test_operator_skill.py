"""Operator skill scripts (skills/edi-operator/scripts/): RED-first tests.

Standalone module (does not re-run the inherited suite). Base-job builder
mirrors tests/test_runtime_batches.py::RuntimeBatchTest._make_base_job. Every
script is run via `sys.executable <script>` with cwd = repo root, env
EDI_AGENT_ID set, against REAL scaffolded jobs (`edi.py init` + hand-built
CL1 base job), never mocks.
"""
import hashlib
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "skills" / "edi-operator" / "scripts"
sys.path.insert(0, str(ROOT))

AGENT = "test-operator-agent"


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def run_script(name, *args, env_extra=None, agent=AGENT):
    env = dict(os.environ)
    env["EDI_AGENT_ID"] = agent
    env.pop("EDI_HOME", None)
    env.pop("EDI_REPO", None)
    if env_extra:
        for k, v in env_extra.items():
            if v is None:
                env.pop(k, None)
            else:
                env[k] = v
    return subprocess.run([sys.executable, str(SCRIPTS / name),
                           *[str(a) for a in args]],
                          capture_output=True, text=True, cwd=str(ROOT),
                          env=env, timeout=120)


def run_edi(*args):
    return subprocess.run([sys.executable, "edi.py", *[str(a) for a in args]],
                          capture_output=True, text=True, cwd=str(ROOT), timeout=120)


class OperatorScriptTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="edi_op_test_"))
        self.home = self.tmp / "home"
        self.home.mkdir()
        self.env = {"EDI_HOME": str(self.home)}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- base job builder (structure mirror of test_runtime_batches helpers) --

    def _make_base_job(self, name="job", level="CL1", pricing=None):
        job_dir = self.tmp / name
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
            "# Header\nHeld-out reference control figure: 99% (Source: Control Audit, 2026-01-01)\n",
            encoding="utf-8")
        (job_dir / "held_out/controls/planted.md").write_text("No header and no figure here.\n", encoding="utf-8")
        maker_script = job_dir / "scripts" / "maker.py"
        maker_script.write_text(
            "import pathlib, sys\n"
            "out = pathlib.Path(sys.argv[2]) / 'memo.md'\n"
            "out.write_text('# Header\\nFigure: 42% (Source: Q3, 2026-10-01)\\n')\n",
            encoding="utf-8")
        eval_script = job_dir / "scripts" / "eval_e1.py"
        eval_script.write_text(
            "import json, pathlib, sys\n"
            "pos = [a for a in sys.argv[1:] if not a.startswith('--')]\n"
            "target, dim = pathlib.Path(pos[0]), pos[1]\n"
            "txt = (target / 'memo.md').read_text() if target.is_dir() else target.read_text()\n"
            "score = 5.0 if (txt.startswith('# Header') if dim == 'D1' else 'Source:' in txt) else 1.0\n"
            "print(json.dumps({'score': score, 'observation': txt.strip()[:60], 'remedy': ''}))\n",
            encoding="utf-8")
        scale = {"anchors": {"1": "missing", "5": "present"}, "floor": 0.0, "threshold": 4.0}
        job_cfg = {
            "job": f"test-memo-job-{name}", "levelClaimed": level, "specVersion": "0.9.8",
            "bounds": {"max_rounds": 2, "max_cost_usd": 1.0, "max_wall_seconds": 60},
            "packetMode": "pass_r2", "subjectFiles": ["memo.md"],
            "bar": {
                "file": "bar.md", "digest": {"sha256": bar_digest},
                "frozenAt": "2026-10-05T09:00:00Z", "author": "Jordan Blake",
                "dimensions": [
                    {"id": "D1", "criterion": "Starts with # Header", "scale": scale},
                    {"id": "D2", "criterion": "Cites Source:", "scale": scale},
                ],
                "approvals": [
                    {"role": "buyer", "by": "Casey Park", "barDigest": {"sha256": bar_digest},
                     "at": "2026-10-05T08:40:00Z", "adopted": None},
                    {"role": "specialist", "by": "Riley Moss", "barDigest": {"sha256": bar_digest},
                     "at": "2026-10-05T08:50:00Z", "adopted": None},
                ],
                "publishedAt": None, "groundTruth": [], "dissent": [],
            },
            "maker": {"name": "Alex Kim", "kind": "script", "version": "1.0.0",
                      "method": {"file": "method.md"}, "model": None,
                      "command": [sys.executable, str(maker_script)]},
            "instrument": {"rubric": {"file": "rubric.md"}, "runner": {"file": "runner.md"}},
            "evaluators": [{
                "id": "E1", "name": "Taylor Grant", "kind": "script", "model": None,
                "script": {"name": "eval_e1.py", "author": "Morgan Lee",
                           "digest": {"sha256": sha256_bytes(eval_script.read_bytes())}},
                "command": [sys.executable, str(eval_script)], "runs": 2,
            }],
            "controls": {"clean": "held_out/controls/clean.md", "planted": "held_out/controls/planted.md"},
            "retention": {"location": f"local/records/{name}", "public": False},
            "disposition": {"state": "given", "by": "Casey Park", "acknowledgesUnobserved": []},
            "pricing": pricing if pricing is not None
                       else {"gpt-test": {"input_usd_per_mtok": 1.0, "output_usd_per_mtok": 2.0}},
        }
        (job_dir / "job.json").write_text(json.dumps(job_cfg, indent=2), encoding="utf-8")
        return job_dir

    def _run_job(self, job_dir, disposition="given"):
        self.assertEqual(run_edi("freeze", job_dir).returncode, 0)
        r = run_edi("run", job_dir)
        self.assertEqual(r.returncode, 0, r.stderr)
        if disposition:
            self.assertEqual(run_edi("finalize", job_dir, "--disposition", disposition,
                                     "--by", "Casey Park").returncode, 0)
        return r


class TestNewJob(OperatorScriptTest):
    def test_scaffolds_job_and_writes_operator_authorship(self):
        r = run_script("new_job.py", "--name", "alpha", env_extra=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        job_dir = self.home / "alpha"
        self.assertEqual(pathlib.Path(out["job_dir"]), job_dir)
        self.assertTrue(out["created"])
        self.assertEqual(pathlib.Path(out["authorship_path"]), job_dir / "AUTHORSHIP.md")
        self.assertIsNone(out["seed_dir"])
        for d in ("scripts", "seed"):
            self.assertTrue((job_dir / d).is_dir(), d)
        self.assertFalse((job_dir / "rounds").exists(), "init must not create rounds/ (N14)")
        auth = (job_dir / "AUTHORSHIP.md").read_text(encoding="utf-8")
        self.assertIn(f"Operator: {AGENT}", auth)
        for line in auth.splitlines():  # bar/rubric/method/seed authors stay blank
            if line.startswith(("Bar author:", "Rubric author:", "Method author:", "Seed author:")):
                self.assertEqual(line.split(":", 1)[1].strip(), "", line)
        # scaffold untouched controls content
        self.assertEqual((job_dir / "bar.md").read_text(encoding="utf-8"), "")

    def test_seed_dir_is_recorded_only_never_copied(self):
        seed = self.tmp / "seedsrc"
        seed.mkdir()
        (seed / "control-secret.md").write_text("held-out control content\n", encoding="utf-8")
        r = run_script("new_job.py", "--name", "beta", "--seed-dir", str(seed),
                       env_extra=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(pathlib.Path(out["seed_dir"]), seed)
        # nothing under the job mirrors the seed content
        for p in (self.home / "beta").rglob("*"):
            if p.is_file() and p.name != "AUTHORSHIP.md":
                self.assertNotIn("control-secret", p.read_text(errors="ignore"), p)

    def test_extra_author_recorded(self):
        r = run_script("new_job.py", "--name", "gamma", "--author", "Robin Vale",
                       env_extra=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Robin Vale",
                      (self.home / "gamma" / "AUTHORSHIP.md").read_text(encoding="utf-8"))

    def test_refuses_existing_job_exit_1(self):
        (self.home / "dup").mkdir()
        r = run_script("new_job.py", "--name", "dup", env_extra=self.env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("exists", r.stderr)

    def test_refuses_without_agent_id_exit_1(self):
        r = run_script("new_job.py", "--name", "noagent",
                       env_extra={**self.env, "EDI_AGENT_ID": None})
        self.assertEqual(r.returncode, 1)
        self.assertIn("EDI_AGENT_ID", r.stderr)

    def test_usage_error_exit_2(self):
        r = run_script("new_job.py", env_extra=self.env)
        self.assertEqual(r.returncode, 2)


class TestPreflight(OperatorScriptTest):
    def test_passes_on_good_job_readonly(self):
        job = self._make_base_job()
        before = {str(p): sha256_bytes(p.read_bytes()) for p in sorted(job.rglob("*")) if p.is_file()}
        r = run_script("preflight.py", job, env_extra=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertTrue(out["pass"])
        self.assertEqual(out["violations"], [])
        self.assertEqual(out["warnings"], [])
        after = {str(p): sha256_bytes(p.read_bytes()) for p in sorted(job.rglob("*")) if p.is_file()}
        self.assertEqual(before, after, "preflight wrote to the job dir")
        self.assertFalse((job / "job.jsonl").exists(), "preflight must NEVER freeze")

    def test_zero_price_seat_warns(self):
        job = self._make_base_job(pricing={"gpt-test": {"input_usd_per_mtok": 0.0,
                                                        "output_usd_per_mtok": 2.0}})
        r = run_script("preflight.py", job, env_extra=self.env)
        out = json.loads(r.stdout)
        self.assertTrue(any("pricing['gpt-test']" in w and "0" in w for w in out["warnings"]),
                        out["warnings"])
        # the runtime itself refuses zero rates, so this is also a violation
        self.assertFalse(out["pass"])

    def test_missing_bounds_field_is_violation(self):
        job = self._make_base_job()
        cfg = json.loads((job / "job.json").read_text())
        del cfg["bounds"]["max_wall_seconds"]
        (job / "job.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        r = run_script("preflight.py", job, env_extra=self.env)
        self.assertEqual(r.returncode, 1)
        out = json.loads(r.stdout)
        self.assertFalse(out["pass"])
        self.assertTrue(out["violations"], out)

    def test_missing_job_dir_exit_1(self):
        r = run_script("preflight.py", self.tmp / "nope", env_extra=self.env)
        self.assertEqual(r.returncode, 1)

    def test_usage_error_exit_2(self):
        r = run_script("preflight.py", env_extra=self.env)
        self.assertEqual(r.returncode, 2)


class TestCostReport(OperatorScriptTest):
    def test_fields_from_state_json(self):
        job = self._make_base_job()
        self._run_job(job)
        r = run_script("cost_report.py", job, env_extra=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["spent_usd"], json.loads((job / "state.json").read_text())["cost_usd"])
        self.assertEqual(out["wall_seconds"],
                         json.loads((job / "state.json").read_text())["wall_spent_seconds"])
        self.assertEqual(out["cap_usd"], json.loads((job / "job.json").read_text())["bounds"]["max_cost_usd"])  # read from job.json
        self.assertIsNone(out["per_model_tokens"])  # script seats burn no tokens

    def test_per_model_tokens_parsed(self):
        job = self._make_base_job()
        self._run_job(job)
        state = json.loads((job / "state.json").read_text())
        state["token_usage"] = {"gpt-test": {"prompt_tokens": 120, "completion_tokens": 45}}
        (job / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
        out = json.loads(run_script("cost_report.py", job, env_extra=self.env).stdout)
        self.assertEqual(out["per_model_tokens"],
                         [{"model": "gpt-test", "tokens_in": 120, "tokens_out": 45}])

    def test_missing_state_refuses_exit_1(self):
        job = self._make_base_job()
        r = run_script("cost_report.py", job, env_extra=self.env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("state.json", r.stderr)


class TestRecordSummary(OperatorScriptTest):
    def test_verified_job_summary(self):
        job = self._make_base_job()
        self._run_job(job)
        r = run_script("record_summary.py", job, env_extra=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertTrue(out["verified"])
        self.assertEqual(out["verify_exit_code"], 0)
        self.assertEqual(out["verdict"], "OK")
        self.assertEqual(out["disposition"], "given")
        self.assertEqual(out["disposition_by"], "Casey Park")
        self.assertEqual(out["scores"], {"D1": 5.0, "D2": 5.0})
        self.assertEqual(out["maker_of_record"], "Alex Kim")
        self.assertEqual(out["gaps"], [])

    def test_tampered_record_fails_verification(self):
        job = self._make_base_job()
        self._run_job(job)
        rec = json.loads((job / "record.json").read_text())
        rec["predicate"]["bar"]["digest"]["sha256"] = "0" * 64
        (job / "record.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
        r = run_script("record_summary.py", job, env_extra=self.env)
        self.assertEqual(r.returncode, 1)
        out = json.loads(r.stdout)
        self.assertFalse(out["verified"])
        self.assertEqual(out["verify_exit_code"], 5)  # edi.py EXIT_CODES["record_failed"]

    def test_unobserved_dimension_becomes_gap(self):
        job = self._make_base_job()
        self._run_job(job)
        rec = json.loads((job / "record.json").read_text())
        pred = rec["predicate"]
        pred["checks"].append({"batch": "B1", "dimension": "D2", "evaluator": "E1",
                               "outcome": "unobserved", "score": None, "run": 1,
                               "evidence": None, "failureCondition": "", "at": "x"})
        (job / "record.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
        out = json.loads(run_script("record_summary.py", job, env_extra=self.env).stdout)
        self.assertIn("E1:D2", out["gaps"])

    def test_usage_error_exit_2(self):
        r = run_script("record_summary.py", env_extra=self.env)
        self.assertEqual(r.returncode, 2)


class TestEscalationReport(OperatorScriptTest):
    def test_evidence_pack_with_escalation(self):
        job = self._make_base_job()
        self._run_job(job)
        esc = {"terminalState": "needs_owner", "reason": "cap_reached",
               "options": [{"id": "raise-bounds", "action": "edit bounds"}],
               "recommendation": "accept-incumbent if the incumbent meets the bar"}
        (job / "escalation.json").write_text(json.dumps(esc, indent=2), encoding="utf-8")
        (job / "AUTHORSHIP.md").write_text(
            f"Operator: {AGENT}\nBar author: Jordan Blake\n", encoding="utf-8")
        r = run_script("escalation_report.py", job, env_extra=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out["escalation"], esc)
        self.assertEqual(out["terminal_state"], "accepted")
        self.assertEqual(out["rounds_completed"], 1)
        self.assertTrue(out["last_10_log_events"])
        self.assertTrue(all(isinstance(e, dict) for e in out["last_10_log_events"]))
        self.assertTrue(out["artifact_digests"]["bar.md"])
        self.assertEqual(out["authorship"].get("Operator"), AGENT)
        self.assertEqual(out["error_fields"], {})

    def test_terminal_failure_without_escalation_file(self):
        job = self._make_base_job()
        self._run_job(job)
        state = json.loads((job / "state.json").read_text())
        state.update(terminal_state="needs_owner", reason="runtime_error",
                     runtime_error="meter exploded")
        (job / "state.json").write_text(json.dumps(state, indent=2), encoding="utf-8")
        r = run_script("escalation_report.py", job, env_extra=self.env)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertIsNone(out["escalation"])
        self.assertEqual(out["terminal_state"], "needs_owner")
        self.assertEqual(out["error_fields"].get("runtime_error"), "meter exploded")

    def test_no_escalation_and_no_failure_refuses_exit_1(self):
        job = self._make_base_job()
        self._run_job(job)
        r = run_script("escalation_report.py", job, env_extra=self.env)
        self.assertEqual(r.returncode, 1)
        self.assertIn("no escalation.json", r.stderr)

    def test_secret_shaped_values_are_scrubbed(self):
        job = self._make_base_job()
        self._run_job(job)
        esc = {"reason": "runtime_error", "note": "sk-abcdefghijklmnopqrs012345",
               "api_key": "sk-abcdefghijklmnopqrs012345"}
        (job / "escalation.json").write_text(json.dumps(esc, indent=2), encoding="utf-8")
        out = run_script("escalation_report.py", job, env_extra=self.env).stdout
        self.assertNotIn("sk-abcdefghijklmnopqrs012345", out)
        self.assertIn("[REDACTED]", out)


class TestWatchEscalations(OperatorScriptTest):
    def _watch(self, extra_env=None):
        merged = {**os.environ, **self.env, "EDI_AGENT_ID": AGENT}
        merged.update(extra_env or {})
        env = {k: v for k, v in merged.items() if v is not None}
        return subprocess.run(["sh", str(SCRIPTS / "watch_escalations.sh")],
                              capture_output=True, text=True, cwd=str(ROOT),
                              env=env, timeout=60)

    def _escalate(self, name, reason="cap_reached", reco="accept-incumbent"):
        job = self.home / name
        job.mkdir(parents=True)
        (job / "escalation.json").write_text(json.dumps(
            {"reason": reason, "recommendation": reco}), encoding="utf-8")
        return job

    def test_emits_reason_and_recommendation_then_dedupes(self):
        self._escalate("job-a")
        r1 = self._watch()
        self.assertEqual(r1.returncode, 0, r1.stderr)
        lines = r1.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1)
        path, reason, reco = lines[0].split("\t")
        self.assertEqual(pathlib.Path(path), self.home / "job-a")
        self.assertEqual(reason, "cap_reached")
        self.assertEqual(reco, "accept-incumbent")
        # second run: marker is newer, nothing re-fires
        r2 = self._watch()
        self.assertEqual(r2.stdout.strip(), "")
        # a NEW escalation after the marker fires again
        self._escalate("job-b", reason="no_artifact", reco="fix-maker")
        r3 = self._watch()
        lines = r3.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].endswith("job-b\tno_artifact\tfix-maker"))

    def test_a_new_escalation_is_never_missed_even_when_its_mtime_ties_the_marker(self):
        """NOTE round 3: the old `-newer $MARKER` scan missed escalations whose
        mtime tied the marker (strictly-newer comparison) — new escalations must
        never be missed. Dedup is per-job by (mtime_ns, size), not one timestamp."""
        self._escalate("job-a")
        self._watch()  # marker exists
        self._escalate("job-b", reason="maker_failed", reco="fix-maker")
        t = 1_700_000_000.0
        os.utime(self.home / ".watched", (t, t))
        os.utime(self.home / "job-b" / "escalation.json", (t, t))
        r = self._watch()
        lines = r.stdout.strip().splitlines()
        self.assertEqual(len(lines), 1, "a new escalation was missed because its mtime tied the marker")
        self.assertTrue(lines[0].endswith("job-b\tmaker_failed\tfix-maker"))
        r2 = self._watch()
        self.assertEqual(r2.stdout.strip(), "")

    def test_marker_is_the_only_file_written(self):
        self._escalate("job-c")
        before = set(p.name for p in self.home.iterdir())
        self._watch()
        after = set(p.name for p in self.home.iterdir())
        self.assertEqual(after - before, {".watched"})

    def test_refuses_without_agent_id(self):
        r = self._watch({"EDI_AGENT_ID": None})
        self.assertEqual(r.returncode, 1)

    def test_missing_home_is_cron_safe_noop(self):
        r = self._watch({"EDI_HOME": str(self.tmp / "does-not-exist")})
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
